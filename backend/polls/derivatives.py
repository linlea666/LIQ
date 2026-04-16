"""
衍生品 / 资金费率相关 Coinglass 轮询（模块级 async 函数）。

由 Engine 注入 cg、state(s)、percentile 等依赖；不在此模块内调用 recompute。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from config.settings import CoinConfig

if TYPE_CHECKING:
    from engine import CoinState
from models.flow import (
    BasisData,
    ExchangeFundingRate,
    FundingRateData,
    LongShortRatioData,
    LongShortRatioExchange,
    MultiFundingRateData,
    OIData,
    OISnapshot,
)
from models.market import TickerData
from processors.percentile import PercentileTracker
from sources.binance_futures import BinanceFuturesSource
from sources.coinglass import CoinglassSource

logger = logging.getLogger(__name__)


def log_api_fields_once(tag: str, sample: Any, logged_keys: set[str]) -> None:
    """与 Engine._log_keys_once 等价：每个 tag 仅记录一次 API 字段名。"""
    if tag in logged_keys:
        return
    logged_keys.add(tag)
    if isinstance(sample, dict):
        logger.info("API fields [%s]: %s", tag, list(sample.keys())[:20])
    elif isinstance(sample, list) and sample and isinstance(sample[0], dict):
        logger.info("API fields [%s]: %s", tag, list(sample[0].keys())[:20])


async def poll_ticker_all(
    cg: CoinglassSource,
    states: dict[str, CoinState],
    supported_coins: list[str],
    get_coin: Callable[[str], CoinConfig],
    percentile: PercentileTracker,
    logged_keys: set[str],
    bn: BinanceFuturesSource | None = None,
) -> None:
    """coins-markets 一次获取全币种行情（纯 Binance，不回退 Coinglass）。"""
    del cg
    if not bn:
        logger.warning("Binance ticker 源未注入，ticker 跳过")
        return
    data = await bn.fetch_tickers_24h()
    if not data:
        return
    log_api_fields_once("ticker-markets", data, logged_keys)

    symbol_to_ccy: dict[str, str] = {}
    for c in supported_coins:
        coin_cfg = get_coin(c)
        # 兼容 Coinglass(BTC) 与 Binance(BTCUSDT) 两种 symbol 形态
        symbol_to_ccy[coin_cfg.symbol_cg] = c
        symbol_to_ccy[coin_cfg.symbol_cg_pair] = c

    for item in data:
        symbol = str(item.get("symbol", ""))
        ccy = symbol_to_ccy.get(symbol)
        if not ccy and symbol.endswith("USDT"):
            ccy = symbol_to_ccy.get(symbol.replace("USDT", ""))
        if not ccy:
            continue

        state = states[ccy]
        try:
            price = float(item.get("current_price", item.get("lastPrice", item.get("price", 0))))
            if price <= 0:
                continue
            chg_pct = float(item.get("price_change_percent_24h", item.get("priceChangePercent", 0)))
            open_24h = price / (1 + chg_pct / 100) if chg_pct != 0 else price
            state.ticker = TickerData(
                coin=ccy,
                ts=int(time.time() * 1000),
                last=price,
                high_24h=float(item.get("high24h", item.get("highPrice", price))),
                low_24h=float(item.get("low24h", item.get("lowPrice", price))),
                vol_24h=float(item.get("volUsd24h", item.get("quoteVolume", 0))),
                change_24h=round(price - open_24h, 2),
                change_pct_24h=round(chg_pct, 2),
            )

            oi_usd = float(item.get("open_interest_usd", item.get("openInterest", 0)))
            if oi_usd > 0:
                snapshot = OISnapshot(
                    coin=ccy, ts=int(time.time()),
                    oi=oi_usd, oi_usd=oi_usd,
                )
                state.oi_history.append(snapshot)
                percentile.push(ccy, "oi", oi_usd)
            if "binance_ticker_ready" not in state._log_once_keys:
                state._log_once_keys.add("binance_ticker_ready")
                logger.info(
                    "Binance ticker 生效 | coin=%s last=%.4f chg24h=%.2f%%",
                    ccy, price, chg_pct,
                )
        except (ValueError, KeyError):
            continue


async def poll_oi(
    cg: CoinglassSource,
    coin: CoinConfig,
    state: CoinState,
) -> None:
    """获取 OI 聚合历史。"""
    data = await cg.fetch_oi_aggregated_history(
        coin.symbol_cg, interval="5m", limit=50,
    )
    if not data:
        return

    for item in data:
        try:
            oi_usd = float(item.get("close", item.get("openInterest", item.get("value", 0))))
            ts = int(item.get("time", item.get("t", 0)))
            if oi_usd > 0:
                snapshot = OISnapshot(
                    coin=coin.ccy, ts=ts, oi=oi_usd, oi_usd=oi_usd,
                )
                state.oi_history.append(snapshot)
        except (ValueError, KeyError):
            continue

    if state.oi_history:
        current = state.oi_history[-1]
        first = state.oi_history[0]
        change_1h = 0.0
        if first.oi_usd > 0:
            change_1h = (current.oi_usd - first.oi_usd) / first.oi_usd * 100

        recent_5m = list(state.oi_history)[-30:]
        change_5m = 0.0
        if recent_5m and recent_5m[0].oi_usd > 0:
            change_5m = (current.oi_usd - recent_5m[0].oi_usd) / recent_5m[0].oi_usd * 100

        trend = "stable"
        if change_1h > 3:
            trend = "surging"
        elif change_1h < -3:
            trend = "declining"

        state.oi = OIData(
            coin=coin.ccy, ts=current.ts,
            current_usd=current.oi_usd,
            change_1h_pct=round(change_1h, 2),
            change_5m_pct=round(change_5m, 2),
            trend=trend,
        )


async def poll_funding_all(
    cg: CoinglassSource,
    states: dict[str, CoinState],
    supported_coins: list[str],
    get_coin: Callable[[str], CoinConfig],
    percentile: PercentileTracker,
    logged_keys: set[str],
) -> None:
    """获取全币种多交易所资金费率。"""
    data = await cg.fetch_fr_exchange_list()
    if not data:
        return
    log_api_fields_once("funding-rate", data, logged_keys)

    symbol_to_ccy = {
        get_coin(c).symbol_cg: c
        for c in supported_coins
    }

    for item in data:
        symbol = item.get("symbol", "")
        ccy = symbol_to_ccy.get(symbol)
        if not ccy:
            continue

        state = states[ccy]
        exchanges = []
        avg_current = 0.0
        count = 0
        okx_rate = None
        bn_rate = None

        margin_list = item.get("stablecoin_margin_list", item.get("uMarginList", []))
        all_rates: list[float] = []
        for ex_item in margin_list:
            ex_name = ex_item.get("exchange", ex_item.get("exchangeName", ""))
            rate = ex_item.get("funding_rate", ex_item.get("rate"))
            if rate is not None:
                rate = float(rate)
                exchanges.append(ExchangeFundingRate(
                    exchange=ex_name, current=rate,
                ))
                all_rates.append(rate)
                count += 1
                if "okx" in ex_name.lower() or "okex" in ex_name.lower():
                    okx_rate = rate
                elif "binance" in ex_name.lower():
                    bn_rate = rate

        if len(all_rates) >= 3:
            sorted_rates = sorted(all_rates)
            median = sorted_rates[len(sorted_rates) // 2]
            filtered = [r for r in all_rates if abs(r - median) < 10 * max(abs(median), 0.0005)]
            avg_current = sum(filtered) / len(filtered) if filtered else 0
        elif all_rates:
            avg_current = sum(all_rates) / len(all_rates)

        interp = "中性"
        if avg_current > 0.0005:
            interp = "多头拥挤"
        elif avg_current < -0.0005:
            interp = "空头拥挤"

        state.multi_funding = MultiFundingRateData(
            coin=ccy, ts=int(time.time()),
            exchanges=exchanges,
            avg_current=round(avg_current, 6),
            interpretation=interp,
        )

        state.funding = FundingRateData(
            coin=ccy, ts=int(time.time()),
            okx_rate=okx_rate, binance_rate=bn_rate,
            avg_rate=round(avg_current, 6),
            interpretation=interp,
        )
        percentile.push(ccy, "funding", avg_current)


async def poll_ls_ratio(
    cg: CoinglassSource,
    coin: CoinConfig,
    state: CoinState,
    logged_keys: set[str],
) -> None:
    """获取多空比（全局 + 大户账户 + 大户持仓）。"""
    exchange = coin.exchange_primary

    global_data = await cg.fetch_global_ls_ratio_history(
        exchange, coin.symbol_cg_pair, interval="1h", limit=1,
    )
    log_api_fields_once("ls-ratio-global", global_data, logged_keys)
    if global_data and isinstance(global_data, list) and len(global_data) > 0:
        item = global_data[-1]
        long_pct = float(item.get("global_account_long_percent", item.get("longAccount", 50)))
        short_pct = float(item.get("global_account_short_percent", item.get("shortAccount", 50)))
        ratio = float(item.get("global_account_long_short_ratio", long_pct / short_pct if short_pct > 0 else 1.0))
        state.ls_ratio = LongShortRatioData(
            coin=coin.ccy, ts=int(time.time()),
            dimension="global",
            exchanges=[LongShortRatioExchange(
                exchange=exchange, long_pct=long_pct, short_pct=short_pct, ratio=ratio,
            )],
            avg_ratio=ratio,
        )

    top_acct_data = await cg.fetch_top_ls_account_ratio_history(
        exchange, coin.symbol_cg_pair, interval="1h", limit=1,
    )
    if top_acct_data and isinstance(top_acct_data, list) and len(top_acct_data) > 0:
        item = top_acct_data[-1]
        long_pct = float(item.get("top_account_long_percent", item.get("longAccount", 50)))
        short_pct = float(item.get("top_account_short_percent", item.get("shortAccount", 50)))
        ratio = float(item.get("top_account_long_short_ratio", long_pct / short_pct if short_pct > 0 else 1.0))
        state.ls_ratio_top_account = LongShortRatioData(
            coin=coin.ccy, ts=int(time.time()),
            dimension="top_account",
            exchanges=[LongShortRatioExchange(
                exchange=exchange, long_pct=long_pct, short_pct=short_pct, ratio=ratio,
            )],
            avg_ratio=ratio,
        )

    top_pos_data = await cg.fetch_top_ls_position_ratio_history(
        exchange, coin.symbol_cg_pair, interval="1h", limit=1,
    )
    if top_pos_data and isinstance(top_pos_data, list) and len(top_pos_data) > 0:
        item = top_pos_data[-1]
        long_pct = float(item.get("top_position_long_percent", item.get("longPosition", 50)))
        short_pct = float(item.get("top_position_short_percent", item.get("shortPosition", 50)))
        ratio = float(item.get("top_position_long_short_ratio", long_pct / short_pct if short_pct > 0 else 1.0))
        state.ls_ratio_top_position = LongShortRatioData(
            coin=coin.ccy, ts=int(time.time()),
            dimension="top_position",
            exchanges=[LongShortRatioExchange(
                exchange=exchange, long_pct=long_pct, short_pct=short_pct, ratio=ratio,
            )],
            avg_ratio=ratio,
        )


async def poll_basis(
    cg: CoinglassSource,
    coin: CoinConfig,
    state: CoinState,
    bn: BinanceFuturesSource | None = None,
) -> None:
    """获取期现溢价：纯 Binance premiumIndex 本地计算（不回退 Coinglass）。"""
    del cg
    if not bn:
        logger.warning("Binance basis 源未注入 | coin=%s", coin.ccy)
        return
    try:
        premium = await bn.fetch_premium_index(coin.symbol_cg_pair)
        if not premium:
            return
        mark_price = float(premium.get("markPrice", 0))
        index_price = float(premium.get("indexPrice", 0))
        if mark_price <= 0 or index_price <= 0:
            return
        basis_pct = (mark_price - index_price) / index_price * 100
        interp = "合约偏贵" if basis_pct > 0.1 else "合约折价" if basis_pct < -0.1 else "中性"
        state.basis = BasisData(
            coin=coin.ccy,
            ts=int(time.time()),
            mark_price=mark_price,
            index_price=index_price,
            basis_pct=round(basis_pct, 4),
            interpretation=interp,
        )
        if "binance_basis_ready" not in state._log_once_keys:
            state._log_once_keys.add("binance_basis_ready")
            logger.info(
                "Binance basis 生效 | coin=%s basis_pct=%.4f mark=%.2f index=%.2f",
                coin.ccy, basis_pct, mark_price, index_price,
            )
    except Exception:
        logger.warning("poll_basis (binance) failed | coin=%s", coin.ccy, exc_info=True)


async def poll_oi_exchange_rank(
    cg: CoinglassSource,
    coin: CoinConfig,
    state: CoinState,
) -> None:
    """获取交易所 OI 持仓占比排名。"""
    data = await cg.fetch_oi_exchange_list(symbol=coin.symbol_cg)
    if not data or not isinstance(data, list):
        return
    try:
        exchanges = []
        total_oi = 0.0
        for item in data:
            ex = item.get("exchange", "")
            if ex == "All":
                total_oi = float(item.get("open_interest_usd", 0))
                continue
            exchanges.append({
                "exchange": ex,
                "oi_usd": float(item.get("open_interest_usd", 0)),
                "change_1h": float(item.get("open_interest_change_percent_1h", 0)),
                "change_24h": float(item.get("open_interest_change_percent_24h", 0)),
            })
        exchanges.sort(key=lambda x: x["oi_usd"], reverse=True)
        state.oi_exchange_rank = {
            "ts": int(time.time()),
            "total_oi_usd": total_oi,
            "exchanges": exchanges[:8],
        }
    except (ValueError, KeyError):
        logger.debug("oi_exchange_rank parse failed", exc_info=True)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# §9h: 净持仓 + 合约资金流 + TD 序列
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def poll_net_position(
    cg: CoinglassSource, coin: CoinConfig, state: "CoinState",
) -> None:
    """拉取净持仓 v2 (1h)，取最新值与趋势。"""
    try:
        rows = await cg.fetch_net_position_v2_history(
            exchange="Binance", symbol=coin.symbol_cg_pair, interval="1h", limit=24,
        )
        if not rows:
            return
        vals = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            v = r.get("netPosition", r.get("net_position"))
            if v is None:
                v = r.get("netPositionChangeCum", r.get("net_position_change_cum"))
            if v is None:
                v = r.get("netPositionChange", r.get("net_position_change"))
            if v is not None:
                vals.append(float(v))
        if not vals:
            return
        state.net_position_latest = vals[-1]
        if len(vals) >= 4:
            recent_avg = sum(vals[-4:]) / 4
            older_avg = sum(vals[:4]) / 4
            diff = recent_avg - older_avg
            base = max(abs(older_avg), 1e-9)
            pct_change = diff / base
            if pct_change > 0.05:
                state.net_position_trend = "上升(多头增仓)"
            elif pct_change < -0.05:
                state.net_position_trend = "下降(多头减仓)"
            else:
                state.net_position_trend = "持平"
    except Exception:
        logger.warning("poll_net_position failed", exc_info=True)
        state.poll_failures["net_position"] = "API调用失败"


async def poll_futures_coin_netflow(
    cg: CoinglassSource, coin: CoinConfig, state: "CoinState",
) -> None:
    """拉取合约币种净资金流 (1h)，取最近 1h 净值与趋势。"""
    try:
        rows = await cg.fetch_futures_coin_netflow(
            symbol=coin.symbol_cg_pair, interval="1h", limit=24,
        )
        if not rows:
            return
        if isinstance(rows, dict):
            v1h = rows.get("net_flow_usd_1h", rows.get("netFlowUsd1h"))
            if v1h is not None:
                state.futures_coin_netflow_1h = float(v1h)
            v15m = rows.get("net_flow_usd_15m", rows.get("netFlowUsd15m"))
            v4h = rows.get("net_flow_usd_4h", rows.get("netFlowUsd4h"))
            trend_votes = [x for x in (v15m, v1h, v4h) if x is not None]
            if trend_votes:
                pos = sum(1 for x in trend_votes if float(x) > 0)
                state.futures_coin_netflow_trend = "持续流入" if pos >= 2 else "持续流出"
            state.poll_failures.pop("coin_netflow", None)
            return
        if not isinstance(rows, list):
            return
        vals = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            v = r.get("netflow", r.get("netFlow", r.get("value")))
            if v is not None:
                vals.append(float(v))
        if not vals:
            return
        state.futures_coin_netflow_1h = vals[-1]
        if len(vals) >= 4:
            recent = sum(1 for v in vals[-4:] if v > 0)
            if recent >= 3:
                state.futures_coin_netflow_trend = "持续流入"
            elif recent <= 1:
                state.futures_coin_netflow_trend = "持续流出"
            else:
                state.futures_coin_netflow_trend = "交替"
        state.poll_failures.pop("coin_netflow", None)
    except Exception:
        logger.warning("poll_futures_coin_netflow failed", exc_info=True)
        state.poll_failures["coin_netflow"] = "API调用失败"


async def poll_td_sequential(
    cg: CoinglassSource, coin: CoinConfig, state: "CoinState",
) -> None:
    """拉取 TD 序列 (1d)，取最新计数与方向。"""
    try:
        rows = await cg.fetch_td_sequential(
            exchange="Binance", symbol=coin.symbol_cg_pair, interval="1d", limit=10,
        )
        if not rows:
            return
        latest = rows[-1] if isinstance(rows, list) and rows else None
        if not latest:
            return
        count = latest.get("td_count", latest.get("tdCount", latest.get("count")))
        direction = latest.get("td_direction", latest.get("tdDirection",
                               latest.get("direction", "")))
        if count is not None:
            state.td_sequential_count = int(count)
        if direction:
            state.td_sequential_direction = str(direction)
        state.poll_failures.pop("td_sequential", None)
    except Exception:
        logger.warning("poll_td_sequential failed", exc_info=True)
        state.poll_failures["td_sequential"] = "API调用失败"
