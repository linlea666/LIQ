"""Coinglass 统一数据源：通过 keystore 代理访问 Coinglass V4/V3 API。

包含 Token Bucket 限流器（10 次/分钟）、自动 V3/V4 路径分发、
分类方法组（Futures/Spot/Options/OnChain/Indicator/ETF/Other）。
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Optional

import aiohttp

from sources.base import DataSource, CoinConfig

logger = logging.getLogger(__name__)


class TokenBucketLimiter:
    """令牌桶限流器，控制每分钟请求次数。"""

    def __init__(self, rate_per_min: int = 10):
        self._rate = rate_per_min
        self._tokens = float(rate_per_min)
        self._max_tokens = float(rate_per_min)
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()
        self._daily_count = 0
        self._daily_reset_ts = time.time()

    async def acquire(self):
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_refill
            self._tokens = min(self._max_tokens, self._tokens + elapsed * (self._rate / 60.0))
            self._last_refill = now

            if self._tokens < 1.0:
                wait = (1.0 - self._tokens) / (self._rate / 60.0)
                logger.debug("Rate limiter: waiting %.1fs", wait)
                await asyncio.sleep(wait)
                self._tokens = 1.0

            self._tokens -= 1.0
            self._daily_count += 1

            if time.time() - self._daily_reset_ts > 86400:
                self._daily_count = 0
                self._daily_reset_ts = time.time()

    @property
    def daily_count(self) -> int:
        return self._daily_count


class CoinglassSource(DataSource):
    """Coinglass API 统一客户端。

    所有 V4 端点通过 /v4/api/... 路径访问，V3 通过 /v3/api/... 路径。
    默认使用 V4。
    """

    def __init__(self, base_url: str, api_key: str, timeout_sec: int = 15,
                 rate_per_min: int = 10):
        super().__init__(name="coinglass", timeout_sec=timeout_sec, max_retries=2)
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._limiter = TokenBucketLimiter(rate_per_min)
        self._headers = {"X-Api-Key": api_key}

    def get_poll_interval(self) -> int:
        return 10

    async def fetch(self, coin: CoinConfig) -> Any:
        return None

    @property
    def daily_request_count(self) -> int:
        return self._limiter.daily_count

    # ── 核心请求方法 ──

    async def _request(self, path: str, params: Optional[dict] = None,
                       version: str = "v4") -> Optional[dict]:
        """发起单次 API 请求，自动限流、错误处理。"""
        await self._limiter.acquire()

        url = f"{self._base_url}/{version}{path}"
        session = await self.get_session()
        t0 = time.time()

        try:
            async with session.get(url, params=params, headers=self._headers) as resp:
                latency = (time.time() - t0) * 1000
                if resp.status == 429:
                    logger.warning("Coinglass 429 rate limited | path=%s", path)
                    self._mark_failure()
                    await asyncio.sleep(10)
                    return None
                resp.raise_for_status()
                data = await resp.json()
                self._mark_success(latency)

                code = data.get("code")
                if code not in (None, "0", 0, "20000", 20000):
                    logger.warning("Coinglass API error | path=%s code=%s msg=%s",
                                   path, code, data.get("msg", ""))
                    return None

                return data.get("data") if "data" in data else data
        except aiohttp.ClientResponseError as e:
            self._mark_failure()
            logger.error("Coinglass HTTP %d | path=%s | %s", e.status, path, str(e))
            return None
        except Exception:
            self._mark_failure()
            logger.error("Coinglass request failed | path=%s", path, exc_info=True)
            return None

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Futures > Trading Market
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    async def fetch_supported_coins(self) -> Optional[list]:
        return await self._request("/api/futures/supported-coins")

    async def fetch_coins_markets(self, exchange_list: str = "") -> Optional[list]:
        """一次获取全币种行情+OI+涨跌幅，最高效的行情端点。"""
        params = {}
        if exchange_list:
            params["exchange_list"] = exchange_list
        return await self._request("/api/futures/coins-markets", params or None)

    async def fetch_pairs_markets(self, symbol: str) -> Optional[list]:
        return await self._request("/api/futures/pairs-markets", {"symbol": symbol})

    async def fetch_price_history(self, exchange: str, symbol: str,
                                  interval: str = "1h", limit: int = 200) -> Optional[list]:
        return await self._request("/api/futures/price/history", {
            "exchange": exchange, "symbol": symbol,
            "interval": interval, "limit": str(limit),
        })

    async def fetch_coins_price_change(self) -> Optional[list]:
        return await self._request("/api/futures/coins-price-change")

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Futures > Open Interest
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    async def fetch_oi_exchange_list(self, symbol: str = "BTC") -> Optional[list]:
        return await self._request("/api/futures/open-interest/exchange-list",
                                   {"symbol": symbol})

    async def fetch_oi_history(self, exchange: str, symbol: str,
                               interval: str = "1h", limit: int = 200,
                               unit: str = "usd") -> Optional[list]:
        return await self._request("/api/futures/open-interest/history", {
            "exchange": exchange, "symbol": symbol,
            "interval": interval, "limit": str(limit), "unit": unit,
        })

    async def fetch_oi_aggregated_history(self, symbol: str, interval: str = "1h",
                                          limit: int = 200, unit: str = "usd") -> Optional[list]:
        return await self._request("/api/futures/open-interest/aggregated-history", {
            "symbol": symbol, "interval": interval,
            "limit": str(limit), "unit": unit,
        })

    async def fetch_oi_aggregated_stablecoin_history(self, symbol: str, interval: str = "1h",
                                                     limit: int = 200) -> Optional[list]:
        return await self._request("/api/futures/open-interest/aggregated-stablecoin-history", {
            "symbol": symbol, "interval": interval, "limit": str(limit),
        })

    async def fetch_oi_aggregated_coin_margin_history(self, symbol: str, interval: str = "1h",
                                                      limit: int = 200) -> Optional[list]:
        return await self._request("/api/futures/open-interest/aggregated-coin-margin-history", {
            "symbol": symbol, "interval": interval, "limit": str(limit),
        })

    async def fetch_oi_exchange_history_chart(self, symbol: str, range_: str = "24h",
                                              unit: str = "usd") -> Optional[dict]:
        return await self._request("/api/futures/open-interest/exchange-history-chart", {
            "symbol": symbol, "range": range_, "unit": unit,
        })

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Futures > Funding Rate
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    async def fetch_fr_exchange_list(self) -> Optional[list]:
        return await self._request("/api/futures/funding-rate/exchange-list")

    async def fetch_fr_history(self, exchange: str, symbol: str,
                               interval: str = "1h", limit: int = 200) -> Optional[list]:
        return await self._request("/api/futures/funding-rate/history", {
            "exchange": exchange, "symbol": symbol,
            "interval": interval, "limit": str(limit),
        })

    async def fetch_fr_oi_weight_history(self, symbol: str, interval: str = "1h",
                                         limit: int = 200) -> Optional[list]:
        return await self._request("/api/futures/funding-rate/oi-weight-history", {
            "symbol": symbol, "interval": interval, "limit": str(limit),
        })

    async def fetch_fr_vol_weight_history(self, symbol: str, interval: str = "1h",
                                          limit: int = 200) -> Optional[list]:
        return await self._request("/api/futures/funding-rate/vol-weight-history", {
            "symbol": symbol, "interval": interval, "limit": str(limit),
        })

    async def fetch_fr_accumulated_exchange_list(self, range_: str = "7d") -> Optional[list]:
        return await self._request("/api/futures/funding-rate/accumulated-exchange-list",
                                   {"range": range_})

    async def fetch_fr_arbitrage(self, usd: str = "10000",
                                 exchange_list: str = "") -> Optional[list]:
        params = {"usd": usd}
        if exchange_list:
            params["exchange_list"] = exchange_list
        return await self._request("/api/futures/funding-rate/arbitrage", params)

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Futures > Long/Short Ratio
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    async def fetch_global_ls_ratio_history(self, exchange: str, symbol: str,
                                            interval: str = "1h",
                                            limit: int = 200) -> Optional[list]:
        return await self._request(
            "/api/futures/global-long-short-account-ratio/history", {
                "exchange": exchange, "symbol": symbol,
                "interval": interval, "limit": str(limit),
            })

    async def fetch_top_ls_account_ratio_history(self, exchange: str, symbol: str,
                                                 interval: str = "1h",
                                                 limit: int = 200) -> Optional[list]:
        return await self._request(
            "/api/futures/top-long-short-account-ratio/history", {
                "exchange": exchange, "symbol": symbol,
                "interval": interval, "limit": str(limit),
            })

    async def fetch_top_ls_position_ratio_history(self, exchange: str, symbol: str,
                                                  interval: str = "1h",
                                                  limit: int = 200) -> Optional[list]:
        return await self._request(
            "/api/futures/top-long-short-position-ratio/history", {
                "exchange": exchange, "symbol": symbol,
                "interval": interval, "limit": str(limit),
            })

    async def fetch_taker_bs_exchange_list(self, symbol: str,
                                           range_: str = "24h") -> Optional[list]:
        return await self._request(
            "/api/futures/taker-buy-sell-volume/exchange-list", {
                "symbol": symbol, "range": range_,
            })

    async def fetch_net_position_history(self, exchange: str, symbol: str,
                                         interval: str = "1h",
                                         limit: int = 200) -> Optional[list]:
        return await self._request("/api/futures/net-position/history", {
            "exchange": exchange, "symbol": symbol,
            "interval": interval, "limit": str(limit),
        })

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Futures > Liquidation
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    async def fetch_liquidation_history(self, exchange: str, symbol: str,
                                        interval: str = "1h",
                                        limit: int = 200) -> Optional[list]:
        return await self._request("/api/futures/liquidation/history", {
            "exchange": exchange, "symbol": symbol,
            "interval": interval, "limit": str(limit),
        })

    async def fetch_liquidation_aggregated_history(self, symbol: str,
                                                   interval: str = "1h",
                                                   limit: int = 200) -> Optional[list]:
        return await self._request("/api/futures/liquidation/aggregated-history", {
            "symbol": symbol, "interval": interval, "limit": str(limit),
        })

    async def fetch_liquidation_coin_list(self, exchange: str = "Binance") -> Optional[list]:
        return await self._request("/api/futures/liquidation/coin-list",
                                   {"exchange": exchange})

    async def fetch_liquidation_exchange_list(self, range_: str = "24h",
                                              symbol: str = "") -> Optional[list]:
        params: dict = {"range": range_}
        if symbol:
            params["symbol"] = symbol
        return await self._request("/api/futures/liquidation/exchange-list", params)

    async def fetch_liquidation_order(self, exchange: str = "", symbol: str = "",
                                      min_amount: float = 0) -> Optional[list]:
        params: dict = {}
        if exchange:
            params["exchange"] = exchange
        if symbol:
            params["symbol"] = symbol
        if min_amount > 0:
            params["min_liquidation_amount"] = str(min_amount)
        return await self._request("/api/futures/liquidation/order", params or None)

    async def fetch_liquidation_heatmap(self, exchange: str, symbol: str,
                                        range_: str = "24h",
                                        model: int = 1) -> Optional[dict]:
        return await self._request(
            f"/api/futures/liquidation/heatmap/model{model}", {
                "exchange": exchange, "symbol": symbol, "range": range_,
            })

    async def fetch_liquidation_aggregated_heatmap(self, symbol: str,
                                                   range_: str = "24h",
                                                   model: int = 1) -> Optional[dict]:
        return await self._request(
            f"/api/futures/liquidation/aggregated-heatmap/model{model}", {
                "symbol": symbol, "range": range_,
            })

    async def fetch_liquidation_map(self, exchange: str, symbol: str,
                                    range_: str = "7d") -> Optional[dict]:
        return await self._request("/api/futures/liquidation/map", {
            "exchange": exchange, "symbol": symbol, "range": range_,
        })

    async def fetch_liquidation_aggregated_map(self, symbol: str,
                                               range_: str = "7d") -> Optional[dict]:
        return await self._request("/api/futures/liquidation/aggregated-map", {
            "symbol": symbol, "range": range_,
        })

    async def fetch_liquidation_max_pain(self, range_: str = "24h") -> Optional[list]:
        return await self._request("/api/futures/liquidation/max-pain",
                                   {"range": range_})

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Futures > Orderbook
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    async def fetch_orderbook_ask_bids_history(self, exchange: str, symbol: str,
                                               interval: str = "5m", limit: int = 200,
                                               range_pct: str = "1") -> Optional[list]:
        return await self._request("/api/futures/orderbook/ask-bids-history", {
            "exchange": exchange, "symbol": symbol,
            "interval": interval, "limit": str(limit), "range": range_pct,
        })

    async def fetch_orderbook_aggregated_ask_bids(self, symbol: str,
                                                  interval: str = "5m",
                                                  limit: int = 200,
                                                  range_pct: str = "1") -> Optional[list]:
        return await self._request("/api/futures/orderbook/aggregated-ask-bids-history", {
            "symbol": symbol, "interval": interval,
            "limit": str(limit), "range": range_pct,
        })

    async def fetch_orderbook_heatmap(self, exchange: str, symbol: str,
                                      limit: int = 100) -> Optional[list]:
        return await self._request("/api/futures/orderbook/history", {
            "exchange": exchange, "symbol": symbol, "limit": str(limit),
        })

    async def fetch_large_orders(self, exchange: str, symbol: str) -> Optional[list]:
        return await self._request("/api/futures/orderbook/large-limit-order", {
            "exchange": exchange, "symbol": symbol,
        })

    async def fetch_large_orders_history(self, exchange: str, symbol: str,
                                         start_time: str = "",
                                         end_time: str = "") -> Optional[list]:
        params: dict = {"exchange": exchange, "symbol": symbol}
        if start_time:
            params["start_time"] = start_time
        if end_time:
            params["end_time"] = end_time
        return await self._request("/api/futures/orderbook/large-limit-order-history", params)

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Futures > Taker Buy/Sell & CVD
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    async def fetch_taker_buy_sell_history(self, exchange: str, symbol: str,
                                           interval: str = "1h",
                                           limit: int = 200) -> Optional[list]:
        return await self._request("/api/futures/v2/taker-buy-sell-volume/history", {
            "exchange": exchange, "symbol": symbol,
            "interval": interval, "limit": str(limit),
        })

    async def fetch_aggregated_taker_bs_history(self, symbol: str,
                                                interval: str = "1h",
                                                limit: int = 200,
                                                unit: str = "usd") -> Optional[list]:
        return await self._request("/api/futures/aggregated-taker-buy-sell-volume/history", {
            "symbol": symbol, "interval": interval,
            "limit": str(limit), "unit": unit,
        })

    async def fetch_cvd_history(self, exchange: str, symbol: str,
                                interval: str = "1h",
                                limit: int = 200) -> Optional[list]:
        return await self._request("/api/futures/cvd/history", {
            "exchange": exchange, "symbol": symbol,
            "interval": interval, "limit": str(limit),
        })

    async def fetch_aggregated_cvd_history(self, symbol: str, interval: str = "1h",
                                           limit: int = 200,
                                           unit: str = "usd") -> Optional[list]:
        return await self._request("/api/futures/aggregated-cvd/history", {
            "symbol": symbol, "interval": interval,
            "limit": str(limit), "unit": unit,
        })

    async def fetch_footprint_history(self, exchange: str, symbol: str,
                                      interval: str = "1h",
                                      limit: int = 200) -> Optional[list]:
        return await self._request("/api/futures/volume/footprint-history", {
            "exchange": exchange, "symbol": symbol,
            "interval": interval, "limit": str(limit),
        })

    async def fetch_futures_netflow_list(self, per_page: int = 20,
                                         page: int = 1) -> Optional[list]:
        return await self._request("/api/futures/netflow-list", {
            "per_page": str(per_page), "page": str(page),
        })

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Futures > Hyperliquid
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    async def fetch_hyperliquid_whale_alert(self) -> Optional[list]:
        return await self._request("/api/hyperliquid/whale-alert")

    async def fetch_hyperliquid_whale_position(self) -> Optional[list]:
        return await self._request("/api/hyperliquid/whale-position")

    async def fetch_hyperliquid_position(self, symbol: str) -> Optional[list]:
        return await self._request("/api/hyperliquid/position", {"symbol": symbol})

    async def fetch_hyperliquid_ls_ratio(self, symbol: str, interval: str = "1h",
                                         limit: int = 200) -> Optional[list]:
        return await self._request(
            "/api/hyperliquid/global-long-short-account-ratio/history", {
                "symbol": symbol, "interval": interval, "limit": str(limit),
            })

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Spot
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    async def fetch_spot_coins_markets(self) -> Optional[list]:
        return await self._request("/api/spot/coins-markets")

    async def fetch_spot_price_history(self, exchange: str, symbol: str,
                                       interval: str = "1h",
                                       limit: int = 200) -> Optional[list]:
        return await self._request("/api/spot/price/history", {
            "exchange": exchange, "symbol": symbol,
            "interval": interval, "limit": str(limit),
        })

    async def fetch_spot_taker_bs_history(self, exchange: str, symbol: str,
                                          interval: str = "1h",
                                          limit: int = 200) -> Optional[list]:
        return await self._request("/api/spot/taker-buy-sell-volume/history", {
            "exchange": exchange, "symbol": symbol,
            "interval": interval, "limit": str(limit),
        })

    async def fetch_spot_aggregated_taker_bs(self, symbol: str, interval: str = "1h",
                                             limit: int = 200) -> Optional[list]:
        return await self._request("/api/spot/aggregated-taker-buy-sell-volume/history", {
            "symbol": symbol, "interval": interval, "limit": str(limit),
        })

    async def fetch_spot_cvd_history(self, exchange: str, symbol: str,
                                     interval: str = "1h",
                                     limit: int = 200) -> Optional[list]:
        return await self._request("/api/spot/cvd/history", {
            "exchange": exchange, "symbol": symbol,
            "interval": interval, "limit": str(limit),
        })

    async def fetch_spot_aggregated_cvd(self, symbol: str, interval: str = "1h",
                                        limit: int = 200) -> Optional[list]:
        return await self._request("/api/spot/aggregated-cvd/history", {
            "symbol": symbol, "interval": interval, "limit": str(limit),
        })

    async def fetch_spot_large_orders(self, exchange: str, symbol: str) -> Optional[list]:
        return await self._request("/api/spot/orderbook/large-limit-order", {
            "exchange": exchange, "symbol": symbol,
        })

    async def fetch_spot_orderbook_heatmap(self, exchange: str, symbol: str,
                                           interval: str = "5m",
                                           limit: int = 100) -> Optional[list]:
        return await self._request("/api/spot/orderbook/history", {
            "exchange": exchange, "symbol": symbol,
            "interval": interval, "limit": str(limit),
        })

    async def fetch_spot_coin_netflow(self, symbol: str,
                                      exchange_list: str = "Binance,OKX,Bybit") -> Optional[dict]:
        return await self._request("/api/spot/coin/netflow", {
            "symbol": symbol, "exchange_list": exchange_list,
        })

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Options
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    async def fetch_option_max_pain(self, symbol: str = "BTC",
                                    exchange: str = "Deribit") -> Optional[list]:
        return await self._request("/api/option/max-pain", {
            "symbol": symbol, "exchange": exchange,
        })

    async def fetch_option_info(self, symbol: str = "BTC") -> Optional[dict]:
        return await self._request("/api/option/info", {"symbol": symbol})

    async def fetch_option_exchange_oi_history(self, symbol: str = "BTC",
                                               range_: str = "30d") -> Optional[dict]:
        return await self._request("/api/option/exchange-oi-history", {
            "symbol": symbol, "range": range_,
        })

    async def fetch_option_exchange_vol_history(self, symbol: str = "BTC",
                                                range_: str = "30d") -> Optional[dict]:
        return await self._request("/api/option/exchange-vol-history", {
            "symbol": symbol, "range": range_,
        })

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # On-Chain
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    async def fetch_exchange_assets(self, exchange: str = "Binance") -> Optional[list]:
        return await self._request("/api/exchange/assets", {"exchange": exchange})

    async def fetch_exchange_balance_list(self, symbol: str = "BTC") -> Optional[list]:
        return await self._request("/api/exchange/balance/list", {"symbol": symbol})

    async def fetch_exchange_balance_chart(self, symbol: str = "BTC") -> Optional[dict]:
        return await self._request("/api/exchange/balance/chart", {"symbol": symbol})

    async def fetch_whale_transfer(self, symbol: str = "") -> Optional[list]:
        params = {}
        if symbol:
            params["symbol"] = symbol
        return await self._request("/api/chain/v2/whale-transfer", params or None)

    async def fetch_onchain_transfers(self, symbol: str = "",
                                      min_usd: str = "1000000") -> Optional[list]:
        params: dict = {"min_usd": min_usd}
        if symbol:
            params["symbol"] = symbol
        return await self._request("/api/exchange/chain/tx/list", params)

    async def fetch_token_unlock_list(self, per_page: int = 20,
                                      page: int = 1) -> Optional[list]:
        return await self._request("/api/coin/unlock-list", {
            "per_page": str(per_page), "page": str(page),
        })

    async def fetch_token_vesting(self, symbol: str) -> Optional[dict]:
        return await self._request("/api/coin/vesting", {"symbol": symbol})

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Indicators > Futures (技术指标)
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    async def fetch_rsi(self, exchange: str, symbol: str, interval: str = "1d",
                        limit: int = 200, window: int = 14,
                        series_type: str = "close") -> Optional[list]:
        return await self._request("/api/futures/indicators/rsi", {
            "exchange": exchange, "symbol": symbol,
            "interval": interval, "limit": str(limit),
            "window": str(window), "series_type": series_type,
        })

    async def fetch_rsi_list(self) -> Optional[list]:
        return await self._request("/api/futures/rsi/list")

    async def fetch_ma(self, exchange: str, symbol: str, interval: str = "1d",
                       limit: int = 200, window: int = 60,
                       series_type: str = "close") -> Optional[list]:
        return await self._request("/api/futures/indicators/ma", {
            "exchange": exchange, "symbol": symbol,
            "interval": interval, "limit": str(limit),
            "window": str(window), "series_type": series_type,
        })

    async def fetch_ma_list(self) -> Optional[list]:
        return await self._request("/api/futures/ma/list")

    async def fetch_ema(self, exchange: str, symbol: str, interval: str = "1d",
                        limit: int = 200, window: int = 20,
                        series_type: str = "close") -> Optional[list]:
        return await self._request("/api/futures/indicators/ema", {
            "exchange": exchange, "symbol": symbol,
            "interval": interval, "limit": str(limit),
            "window": str(window), "series_type": series_type,
        })

    async def fetch_ema_list(self) -> Optional[list]:
        return await self._request("/api/futures/ema/list")

    async def fetch_boll(self, exchange: str, symbol: str, interval: str = "1d",
                         limit: int = 200, window: int = 20,
                         mult: float = 2.0) -> Optional[list]:
        return await self._request("/api/futures/indicators/boll", {
            "exchange": exchange, "symbol": symbol,
            "interval": interval, "limit": str(limit),
            "window": str(window), "mult": str(mult),
        })

    async def fetch_macd(self, exchange: str, symbol: str, interval: str = "1d",
                         limit: int = 200, fast_window: int = 12,
                         slow_window: int = 26,
                         signal_window: int = 9) -> Optional[list]:
        return await self._request("/api/futures/indicators/macd", {
            "exchange": exchange, "symbol": symbol,
            "interval": interval, "limit": str(limit),
            "fast_window": str(fast_window),
            "slow_window": str(slow_window),
            "signal_window": str(signal_window),
        })

    async def fetch_macd_list(self) -> Optional[list]:
        return await self._request("/api/futures/macd/list")

    async def fetch_atr(self, exchange: str, symbol: str, interval: str = "1h",
                        limit: int = 200, window: int = 14) -> Optional[list]:
        return await self._request("/api/futures/indicators/avg-true-range", {
            "exchange": exchange, "symbol": symbol,
            "interval": interval, "limit": str(limit),
            "window": str(window),
        })

    async def fetch_atr_list(self) -> Optional[list]:
        return await self._request("/api/futures/avg-true-range/list")

    async def fetch_basis_history(self, exchange: str, symbol: str,
                                  interval: str = "1h",
                                  limit: int = 200) -> Optional[list]:
        return await self._request("/api/futures/basis/history", {
            "exchange": exchange, "symbol": symbol,
            "interval": interval, "limit": str(limit),
        })

    async def fetch_whale_index(self, exchange: str, symbol: str,
                                interval: str = "1h",
                                limit: int = 200) -> Optional[list]:
        return await self._request("/api/futures/whale-index/history", {
            "exchange": exchange, "symbol": symbol,
            "interval": interval, "limit": str(limit),
        })

    async def fetch_cgdi_index(self) -> Optional[list]:
        return await self._request("/api/futures/cgdi-index/history")

    async def fetch_cdri_index(self) -> Optional[list]:
        return await self._request("/api/futures/cdri-index/history")

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Indicators > Bitcoin (链上周期)
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    async def fetch_ahr999(self) -> Optional[list]:
        return await self._request("/api/index/ahr999")

    async def fetch_puell_multiple(self) -> Optional[list]:
        return await self._request("/api/index/puell-multiple")

    async def fetch_stock_flow(self) -> Optional[list]:
        return await self._request("/api/index/stock-flow")

    async def fetch_pi_cycle(self) -> Optional[list]:
        return await self._request("/api/index/pi-cycle-indicator")

    async def fetch_golden_ratio(self) -> Optional[list]:
        return await self._request("/api/index/golden-ratio-multiplier")

    async def fetch_two_year_ma(self) -> Optional[list]:
        return await self._request("/api/index/2-year-ma-multiplier")

    async def fetch_200w_ma_heatmap(self) -> Optional[list]:
        return await self._request("/api/index/200-week-moving-average-heatmap")

    async def fetch_rainbow_chart(self) -> Optional[list]:
        return await self._request("/api/index/bitcoin/rainbow-chart")

    async def fetch_profitable_days(self) -> Optional[list]:
        return await self._request("/api/index/bitcoin/profitable-days")

    async def fetch_bubble_index(self) -> Optional[list]:
        return await self._request("/api/index/bitcoin/bubble-index")

    async def fetch_bull_market_peak(self) -> Optional[list]:
        return await self._request("/api/bull-market-peak-indicator")

    async def fetch_sth_sopr(self) -> Optional[list]:
        return await self._request("/api/index/bitcoin-sth-sopr")

    async def fetch_lth_sopr(self) -> Optional[list]:
        return await self._request("/api/index/bitcoin-lth-sopr")

    async def fetch_sth_realized_price(self) -> Optional[list]:
        return await self._request("/api/index/bitcoin-sth-realized-price")

    async def fetch_lth_realized_price(self) -> Optional[list]:
        return await self._request("/api/index/bitcoin-lth-realized-price")

    async def fetch_sth_supply(self) -> Optional[list]:
        return await self._request("/api/index/bitcoin-short-term-holder-supply")

    async def fetch_lth_supply(self) -> Optional[list]:
        return await self._request("/api/index/bitcoin-long-term-holder-supply")

    async def fetch_rhodl_ratio(self) -> Optional[list]:
        return await self._request("/api/index/bitcoin-rhodl-ratio")

    async def fetch_reserve_risk(self) -> Optional[list]:
        return await self._request("/api/index/bitcoin-reserve-risk")

    async def fetch_active_addresses(self) -> Optional[list]:
        return await self._request("/api/index/bitcoin-active-addresses")

    async def fetch_new_addresses(self) -> Optional[list]:
        return await self._request("/api/index/bitcoin-new-addresses")

    async def fetch_nupl(self) -> Optional[list]:
        return await self._request("/api/index/bitcoin-net-unrealized-profit-loss")

    async def fetch_btc_correlation(self) -> Optional[list]:
        return await self._request("/api/index/bitcoin-correlation")

    async def fetch_btc_macro_oscillator(self) -> Optional[list]:
        return await self._request("/api/index/bitcoin-macro-oscillator")

    async def fetch_btc_vs_global_m2(self) -> Optional[list]:
        return await self._request("/api/index/bitcoin-vs-global-m2-growth")

    async def fetch_btc_vs_us_m2(self) -> Optional[list]:
        return await self._request("/api/index/bitcoin-vs-us-m2-growth")

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Indicators > Market
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    async def fetch_fear_greed(self) -> Optional[list]:
        return await self._request("/api/index/fear-greed-history")

    async def fetch_stablecoin_mcap(self) -> Optional[list]:
        return await self._request("/api/index/stableCoin-marketCap-history")

    async def fetch_altcoin_season(self) -> Optional[list]:
        return await self._request("/api/index/altcoin-season")

    async def fetch_btc_dominance(self) -> Optional[list]:
        return await self._request("/api/index/bitcoin-dominance")

    async def fetch_option_vs_futures_oi_ratio(self) -> Optional[list]:
        return await self._request("/api/index/option-vs-futures-oi-ratio")

    async def fetch_exchange_transparency(self) -> Optional[list]:
        return await self._request("/api/exchange_assets_transparency/list")

    async def fetch_futures_spot_volume_ratio(self, symbol: str = "BTCUSDT",
                                              interval: str = "1h",
                                              limit: int = 200) -> Optional[list]:
        return await self._request("/api/futures_spot_volume_ratio", {
            "symbol": symbol, "interval": interval, "limit": str(limit),
        })

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Indicators > Spot
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    async def fetch_coinbase_premium(self, interval: str = "1h",
                                     limit: int = 200) -> Optional[list]:
        return await self._request("/api/coinbase-premium-index", {
            "interval": interval, "limit": str(limit),
        })

    async def fetch_bitfinex_margin_ls(self, symbol: str = "BTC",
                                       interval: str = "1h",
                                       limit: int = 200) -> Optional[list]:
        params: dict = {"interval": interval, "limit": str(limit)}
        if symbol:
            params["symbol"] = symbol
        return await self._request("/api/bitfinex-margin-long-short", params)

    async def fetch_borrow_interest_rate(self, exchange: str, symbol: str,
                                         interval: str = "1h",
                                         limit: int = 200) -> Optional[list]:
        return await self._request("/api/borrow-interest-rate/history", {
            "exchange": exchange, "symbol": symbol,
            "interval": interval, "limit": str(limit),
        })

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # ETF
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    async def fetch_btc_etf_list(self) -> Optional[list]:
        return await self._request("/api/etf/bitcoin/list")

    async def fetch_btc_etf_flow_history(self) -> Optional[list]:
        return await self._request("/api/etf/bitcoin/flow-history")

    async def fetch_btc_etf_net_assets_history(self, ticker: str = "") -> Optional[list]:
        params = {}
        if ticker:
            params["ticker"] = ticker
        return await self._request("/api/etf/bitcoin/net-assets/history", params or None)

    async def fetch_btc_etf_premium_discount(self, ticker: str = "") -> Optional[list]:
        params = {}
        if ticker:
            params["ticker"] = ticker
        return await self._request("/api/etf/bitcoin/premium-discount/history", params or None)

    async def fetch_eth_etf_list(self) -> Optional[list]:
        return await self._request("/api/etf/ethereum/list")

    async def fetch_eth_etf_flow_history(self) -> Optional[list]:
        return await self._request("/api/etf/ethereum/flow-history")

    async def fetch_eth_etf_net_assets(self) -> Optional[list]:
        return await self._request("/api/etf/ethereum/net-assets/history")

    async def fetch_sol_etf_flow_history(self) -> Optional[list]:
        return await self._request("/api/etf/solana/flow-history")

    async def fetch_xrp_etf_flow_history(self) -> Optional[list]:
        return await self._request("/api/etf/xrp/flow-history")

    async def fetch_grayscale_holdings(self) -> Optional[list]:
        return await self._request("/api/grayscale/holdings-list")

    async def fetch_grayscale_premium(self, symbol: str) -> Optional[list]:
        return await self._request("/api/grayscale/premium-history", {"symbol": symbol})

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Other
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    async def fetch_economic_data(self, language: str = "zh") -> Optional[list]:
        params: dict = {"language": language}
        now_ms = int(time.time() * 1000)
        params["start_time"] = str(now_ms - 15 * 86400 * 1000)
        params["end_time"] = str(now_ms + 15 * 86400 * 1000)
        return await self._request("/api/calendar/economic-data", params)

    async def fetch_news(self, language: str = "zh", per_page: int = 20,
                         page: int = 1) -> Optional[list]:
        return await self._request("/api/article/list", {
            "language": language, "per_page": str(per_page), "page": str(page),
        })

    async def fetch_account_subscription(self) -> Optional[dict]:
        return await self._request("/api/user/account/subscription")


def create_coinglass_source() -> CoinglassSource:
    """从环境变量/配置创建 Coinglass 数据源实例。"""
    from config.settings import get_settings
    cfg = get_settings().coinglass
    api_key = os.getenv(cfg.api_key_env, cfg.api_key_default)
    return CoinglassSource(
        base_url=cfg.base_url,
        api_key=api_key,
        timeout_sec=cfg.timeout_sec,
        rate_per_min=cfg.rate_limit_per_min,
    )
