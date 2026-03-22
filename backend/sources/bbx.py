"""BBX 数据源：清算地图 + 资金费率 + 多空比 + ETF + 全网爆仓 + market/index"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

from config.settings import CoinConfig, get_settings
from models.flow import (
    ETFFlowData,
    ETFFlowDay,
    ExchangeFundingRate,
    GlobalLiquidationData,
    LongShortRatioData,
    LongShortRatioExchange,
    MarketIndexData,
    MarketIndexItem,
    MultiFundingRateData,
)
from models.liquidation import LiqBand, LiqLeverageGroup, LiquidationMap
from sources.base import DataSource

logger = logging.getLogger(__name__)


class BBXLiquidationSource(DataSource):
    """从 bbx.com 拉取清算热力图数据"""

    def __init__(self):
        cfg = get_settings().bbx
        super().__init__(name="bbx_liquidation", timeout_sec=cfg.timeout_sec)
        self._base_url = cfg.base_url
        self._module = cfg.module
        self._poll_interval = cfg.poll_interval_sec
        self._cycles = cfg.cycles

    def get_poll_interval(self) -> int:
        return self._poll_interval

    async def fetch(self, coin: CoinConfig) -> dict[str, LiquidationMap]:
        """拉取所有周期的清算地图，返回 {cycle: LiquidationMap}"""
        results: dict[str, LiquidationMap] = {}
        for cycle in self._cycles:
            liq_map = await self._fetch_cycle(coin, cycle)
            if liq_map:
                results[cycle] = liq_map
        return results

    async def _fetch_cycle(self, coin: CoinConfig, cycle: str) -> LiquidationMap | None:
        url = f"{self._base_url}?module={self._module}"
        body = {
            "symbol": coin.symbol_bbx,
            "cycle": cycle,
            "multiple": "",
            "lan": "zh",
        }
        try:
            data = await self._get_json(url, method="POST", json_body=body)
        except Exception:
            logger.error("BBX API request failed | coin=%s cycle=%s", coin.ccy, cycle, exc_info=True)
            return None

        if not data.get("success"):
            logger.warning("BBX API returned success=false | coin=%s cycle=%s", coin.ccy, cycle)
            return None

        raw = data.get("data", {})
        ts = raw.get("timestamp", 0)
        if isinstance(ts, str):
            ts = int(ts) if ts.isdigit() else 0

        leverage_groups: list[LiqLeverageGroup] = []
        for lev in ("10", "25", "50", "100"):
            group_raw = raw.get(lev)
            if not group_raw or not isinstance(group_raw, dict):
                continue

            short_bands = [
                LiqBand(price_from=b[0], price_to=b[1], turnover_usd=b[2])
                for b in group_raw.get("short", [])
            ]
            long_bands = [
                LiqBand(price_from=b[0], price_to=b[1], turnover_usd=b[2])
                for b in group_raw.get("long", [])
            ]

            leverage_groups.append(LiqLeverageGroup(
                leverage=lev,
                short_bands=short_bands,
                long_bands=long_bands,
                short_total_usd=sum(b.turnover_usd for b in short_bands),
                long_total_usd=sum(b.turnover_usd for b in long_bands),
            ))

        logger.debug(
            "BBX parsed | coin=%s cycle=%s leverages=%d",
            coin.ccy, cycle, len(leverage_groups),
        )

        return LiquidationMap(
            coin=coin.ccy,
            ts=ts,
            cycle=cycle,
            leverage_groups=leverage_groups,
        )


class BBXExtendedSource(DataSource):
    """BBX 扩展数据源：资金费率/多空比/ETF/market-index/全网爆仓"""

    def __init__(self):
        cfg = get_settings().bbx
        super().__init__(name="bbx_extended", timeout_sec=cfg.timeout_sec)
        self._cfg = cfg
        self._poll_interval = cfg.extended_poll_sec

    def get_poll_interval(self) -> int:
        return self._poll_interval

    async def fetch(self, coin: CoinConfig) -> Any:
        """此类不走统一 fetch_with_retry 通道，各方法独立调用并自行标记健康状态"""
        return None

    @staticmethod
    def _is_response_ok(data: dict) -> bool:
        """BBX 不同端点使用不同的成功标志（success / code / 无标志仅含 data）"""
        if "success" in data:
            return bool(data["success"])
        code = data.get("code")
        if code is not None:
            return code in (0, "0", 200, "200")
        return "data" in data

    async def fetch_multi_funding(self, coin: str = "btc") -> Optional[MultiFundingRateData]:
        """多交易所资金费率（BBX 一次返回 6 所 × current/3d/7d/30d）"""
        url = f"{self._cfg.funding_url}?lan=zh-Hans"
        try:
            data = await self._get_json(url)
            self._mark_success()
        except Exception:
            self._mark_failure()
            logger.error("BBX funding-rate fetch failed", exc_info=True)
            return None

        if not self._is_response_ok(data):
            logger.warning("BBX funding-rate bad response | keys=%s", list(data.keys()))
            return None

        raw_data = data.get("data", {})
        rows = raw_data.get("dataList", []) if isinstance(raw_data, dict) else []
        coin_upper = coin.upper()
        exchanges: list[ExchangeFundingRate] = []
        for row in rows:
            symbol = (row.get("symbolName") or "").upper()
            if coin_upper not in symbol:
                continue
            exchanges.append(ExchangeFundingRate(
                exchange=row.get("exchangeName", ""),
                current=_safe_float(row.get("currentRate")),
                avg_3d=_safe_float(row.get("rate3dAvg")),
                avg_7d=_safe_float(row.get("rate7dAvg")),
                avg_30d=_safe_float(row.get("rate30dAvg")),
            ))

        valid = [e for e in exchanges if e.current is not None]
        avg_current = sum(e.current for e in valid) / len(valid) if valid else 0
        valid_7d = [e for e in exchanges if e.avg_7d is not None]
        avg_7d = sum(e.avg_7d for e in valid_7d) / len(valid_7d) if valid_7d else 0

        interp = "中性"
        if avg_current > 0.01:
            interp = "多头极度拥挤"
        elif avg_current > 0.005:
            interp = "多头拥挤"
        elif avg_current < -0.01:
            interp = "空头极度拥挤"
        elif avg_current < -0.005:
            interp = "空头拥挤"

        return MultiFundingRateData(
            coin=coin_upper,
            ts=int(time.time()),
            exchanges=exchanges,
            avg_current=avg_current,
            avg_7d=avg_7d,
            interpretation=interp,
        )

    async def fetch_ls_ratio(self, coin: str = "btc", cycle: str = "1h") -> Optional[LongShortRatioData]:
        """各交易所多空比"""
        url = f"{self._cfg.ls_ratio_url}?module=upgrade.long-short-ratio.exchanges"
        body = {"symbol": coin.lower(), "cycle": cycle, "lan": "cn"}
        try:
            data = await self._get_json(url, method="POST", json_body=body)
            self._mark_success()
        except Exception:
            # SOL 等小币种 BBX 可能不支持，仅降级为 WARNING 且不影响全局健康
            logger.warning("BBX ls-ratio fetch failed | coin=%s", coin)
            return None

        if not self._is_response_ok(data):
            return None

        raw_data = data.get("data", {})
        rows = raw_data.get("list", []) if isinstance(raw_data, dict) else []
        exchanges: list[LongShortRatioExchange] = []
        for row in rows:
            long_pct = _safe_float(row.get("longRate"), 50)
            short_pct = _safe_float(row.get("shortRate"), 50)
            ratio = long_pct / short_pct if short_pct > 0 else 1.0
            exchanges.append(LongShortRatioExchange(
                exchange=row.get("exchangeName", ""),
                long_pct=long_pct,
                short_pct=short_pct,
                ratio=round(ratio, 3),
            ))

        _EXCHANGE_WEIGHT = {
            "binance": 0.40, "okx": 0.25, "okex": 0.25,
            "bybit": 0.15, "bitget": 0.08, "gate": 0.06,
            "huobi": 0.06, "htx": 0.06,
        }
        total_w = 0.0
        weighted_sum = 0.0
        for e in exchanges:
            w = _EXCHANGE_WEIGHT.get(e.exchange.lower(), 0.05)
            weighted_sum += e.ratio * w
            total_w += w
        avg_ratio = weighted_sum / total_w if total_w > 0 else 1.0
        interp = "多头主导" if avg_ratio > 1.3 else "空头主导" if avg_ratio < 0.77 else "多空均衡"

        return LongShortRatioData(
            coin=coin.upper(),
            ts=int(time.time()),
            cycle=cycle,
            exchanges=exchanges,
            avg_ratio=round(avg_ratio, 3),
            interpretation=interp,
        )

    async def fetch_etf_flow(self, etf_type: str = "us-btc") -> Optional[ETFFlowData]:
        """BTC ETF 资金流"""
        body = {"type": etf_type, "lan": "zh-Hans"}
        try:
            data = await self._get_json(self._cfg.etf_flow_url, method="POST", json_body=body)
            self._mark_success()
        except Exception:
            self._mark_failure()
            logger.error("BBX etf-flow fetch failed", exc_info=True)
            return None

        if not self._is_response_ok(data):
            logger.warning("BBX etf-flow bad response | keys=%s", list(data.keys()))
            return None

        raw_data = data.get("data", {})
        rows = raw_data.get("list", []) if isinstance(raw_data, dict) else (raw_data if isinstance(raw_data, list) else [])
        days: list[ETFFlowDay] = []
        for row in rows[:7]:
            total = _safe_float(row.get("totalNetflow"), 0)
            days.append(ETFFlowDay(
                date=row.get("date", ""),
                total_net=total,
                detail={k: v for k, v in row.items() if k not in ("date", "totalNetflow")},
            ))

        net_3d = sum(d.total_net for d in days[:3])
        trend = "inflow" if net_3d > 0 else "outflow" if net_3d < 0 else "mixed"

        return ETFFlowData(
            ts=int(time.time()),
            recent_days=days,
            net_3d=net_3d,
            trend=trend,
        )

    async def fetch_global_liquidation(self) -> Optional[GlobalLiquidationData]:
        """全网爆仓统计"""
        url = f"{self._cfg.global_liq_url}?module=v7.market.futures-liquidation"
        body = {"currency": "usd", "lan": "cn"}
        try:
            data = await self._get_json(url, method="POST", json_body=body)
            self._mark_success()
        except Exception:
            self._mark_failure()
            logger.error("BBX global-liq fetch failed", exc_info=True)
            return None

        if not self._is_response_ok(data):
            logger.warning("BBX global-liq bad response | keys=%s", list(data.keys()))
            return None

        d = data.get("data", {})
        if not isinstance(d, dict):
            return None
        long_1h = _safe_float(d.get("longLiqUsd1h"), 0)
        short_1h = _safe_float(d.get("shortLiqUsd1h"), 0)
        long_24h = _safe_float(d.get("longLiqUsd24h"), 0)
        short_24h = _safe_float(d.get("shortLiqUsd24h"), 0)

        return GlobalLiquidationData(
            ts=int(time.time()),
            long_1h_usd=long_1h,
            short_1h_usd=short_1h,
            long_24h_usd=long_24h,
            short_24h_usd=short_24h,
            ratio_1h=long_1h / short_1h if short_1h > 0 else 1.0,
            ratio_24h=long_24h / short_24h if short_24h > 0 else 1.0,
            largest_single_usd=_safe_float(d.get("largestLiqUsd"), 0),
        )

    async def fetch_market_index(self) -> Optional[MarketIndexData]:
        """BBX market/index 精选指标（一次性获取 20+ 指标）"""
        url = f"{self._cfg.market_index_url}?module=v1/market/index"
        body = {"open_time": int(time.time()), "lan": "zh-CN"}
        try:
            data = await self._get_json(url, method="POST", json_body=body)
            self._mark_success()
        except Exception:
            self._mark_failure()
            logger.error("BBX market-index fetch failed", exc_info=True)
            return None

        if not self._is_response_ok(data):
            logger.warning("BBX market-index bad response | keys=%s", list(data.keys()))
            return None

        try:
            raw_data = data.get("data")
            if isinstance(raw_data, list):
                items = raw_data
            elif isinstance(raw_data, dict):
                items = raw_data.get("list", [])
            else:
                items = []

            key_map = {
                "i:fgi:alternative": "fear_greed",
                "i:bitcoin_percentage_of_market_capitalization": "btc_dominance",
                "max_pain:btc": "btc_max_pain",
                "i:dvol:btc": "btc_dvol",
                "i:options_oi_ratio:btc": "btc_put_call_oi",
                "i:mvrv:btc": "btc_mvrv",
                "i:dxy": "dxy",
                "i:nasdaq": "nasdaq",
                "i:spx": "sp500",
                "i:gold": "gold",
                "i:btc_balance:binance": "binance_btc_balance",
                "lsprbtc:okex": "okx_ls_ratio_btc",
                "lsprbtc:binance": "binance_ls_ratio_btc",
            }

            result = MarketIndexData(ts=int(time.time()))
            all_items: list[MarketIndexItem] = []

            for item in items:
                if not isinstance(item, dict):
                    continue
                key = item.get("key", "") or item.get("id", "")
                name = item.get("name", "")
                val = _safe_float(item.get("value") or item.get("last"))
                if val is None:
                    continue

                all_items.append(MarketIndexItem(
                    key=key,
                    name=name,
                    value=val,
                    change_pct=_safe_float(item.get("changeRate") or item.get("change24h")),
                ))

                attr = key_map.get(key)
                if attr:
                    setattr(result, attr, val)

            result.raw_items = all_items
            if all_items:
                logger.info("BBX market-index OK | items=%d", len(all_items))
            return result
        except Exception:
            logger.error("BBX market-index parse error", exc_info=True)
            return None


def _safe_float(v: Any, default: Optional[float] = None) -> Optional[float]:
    if v is None:
        return default
    try:
        return float(v)
    except (ValueError, TypeError):
        return default


def create_bbx_source() -> BBXLiquidationSource:
    return BBXLiquidationSource()


def create_bbx_extended_source() -> BBXExtendedSource:
    return BBXExtendedSource()
