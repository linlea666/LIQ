"""
订单流相关轮询：CVD、Taker 成交量、订单簿深度、大单追踪。
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from config.settings import CoinConfig
from models.flow import CVDData, CVDPoint, TakerFlowData
from models.market import OrderBookAnalysis
from models.orderbook_ext import LargeOrder, LargeOrderSnapshot
from processors.cvd import detect_cvd_price_divergence
from sources.coinglass import CoinglassSource

if TYPE_CHECKING:
    from engine import CoinState

logger = logging.getLogger(__name__)

# 与 Engine._log_keys_once 一致：每个 tag 仅首次打印 API 字段名
_logged_api_tags: set[str] = set()


def _log_api_fields_once(tag: str, sample) -> None:
    if tag in _logged_api_tags:
        return
    _logged_api_tags.add(tag)
    if isinstance(sample, dict):
        logger.info("API fields [%s]: %s", tag, list(sample.keys())[:20])
    elif isinstance(sample, list) and sample and isinstance(sample[0], dict):
        logger.info("API fields [%s]: %s", tag, list(sample[0].keys())[:20])


def calc_cvd_trend(points: list[CVDPoint], lookback: int = 12) -> tuple[str, float]:
    if len(points) < 2:
        return "flat", 0.0
    recent = points[-lookback:]
    delta_sum = sum(p.delta for p in recent)
    start_cvd = recent[0].cvd
    end_cvd = recent[-1].cvd
    diff = end_cvd - start_cvd
    abs_values = [abs(p.delta) for p in recent if p.delta != 0]
    median_abs = sorted(abs_values)[len(abs_values) // 2] if abs_values else 1.0
    threshold = max(median_abs * 0.5, abs(delta_sum) * 0.05)
    if diff > threshold:
        return "rising", delta_sum
    elif diff < -threshold:
        return "declining", delta_sum
    return "flat", delta_sum


async def poll_cvd(cg: CoinglassSource, coin: CoinConfig, state: CoinState) -> None:
    """从 Coinglass 直接获取 CVD"""
    contract_data = await cg.fetch_aggregated_cvd_history(
        coin.symbol_cg, interval="5m", limit=100,
    )
    _log_api_fields_once("cvd-futures", contract_data)
    if contract_data and isinstance(contract_data, list):
        points = []
        for item in contract_data:
            try:
                ts = int(item.get("time", item.get("t", 0)))
                buy = float(item.get("agg_taker_buy_vol", item.get("buyVolUsd", 0)))
                sell = float(item.get("agg_taker_sell_vol", item.get("sellVolUsd", 0)))
                cvd_val = float(item.get("cum_vol_delta", item.get("cvd", buy - sell)))
                points.append(CVDPoint(
                    ts=ts, buy_vol=buy, sell_vol=sell,
                    delta=buy - sell, cvd=cvd_val,
                ))
            except (ValueError, KeyError):
                continue

        if points:
            trend, delta_1h = calc_cvd_trend(points)
            state.cvd_contract = CVDData(
                coin=coin.ccy, inst_type="CONTRACTS",
                series=points, trend_1h=trend, delta_1h=delta_1h,
            )
            if state.candle_prices:
                state.cvd_contract = detect_cvd_price_divergence(
                    state.cvd_contract, state.candle_prices, state.candle_ts,
                )

    spot_data = await cg.fetch_spot_aggregated_cvd(
        coin.symbol_cg, interval="5m", limit=100,
    )
    if spot_data and isinstance(spot_data, list):
        points = []
        for item in spot_data:
            try:
                ts = int(item.get("time", item.get("t", 0)))
                buy = float(item.get("agg_taker_buy_vol", item.get("buyVolUsd", 0)))
                sell = float(item.get("agg_taker_sell_vol", item.get("sellVolUsd", 0)))
                cvd_val = float(item.get("cum_vol_delta", item.get("cvd", buy - sell)))
                points.append(CVDPoint(
                    ts=ts, buy_vol=buy, sell_vol=sell,
                    delta=buy - sell, cvd=cvd_val,
                ))
            except (ValueError, KeyError):
                continue
        if points:
            trend, delta = calc_cvd_trend(points)
            state.cvd_spot = CVDData(
                coin=coin.ccy, inst_type="SPOT",
                series=points, trend_1h=trend, delta_1h=delta,
            )


async def poll_taker_volume(cg: CoinglassSource, coin: CoinConfig, state: CoinState) -> None:
    """获取 Taker 买卖量（保留 5m 序列供 Market Action Analyzer 使用）。"""
    contract_data = await cg.fetch_aggregated_taker_bs_history(
        coin.symbol_cg, interval="5m", limit=24,
    )
    spot_data = await cg.fetch_spot_aggregated_taker_bs(
        coin.symbol_cg, interval="5m", limit=24,
    )

    c_buy = c_sell = s_buy = s_sell = 0.0
    contract_series: list[dict] = []
    spot_series: list[dict] = []
    if contract_data and isinstance(contract_data, list):
        for item in contract_data:
            try:
                buy = float(item.get("aggregated_buy_volume_usd", item.get("buyVolUsd", 0)))
                sell = float(item.get("aggregated_sell_volume_usd", item.get("sellVolUsd", 0)))
                c_buy += buy
                c_sell += sell
                contract_series.append({
                    "ts": int(item.get("time", item.get("t", 0))),
                    "buy_usd": buy, "sell_usd": sell,
                    "delta_usd": buy - sell,
                })
            except (ValueError, KeyError):
                continue

    if spot_data and isinstance(spot_data, list):
        for item in spot_data:
            try:
                buy = float(item.get("aggregated_buy_volume_usd", item.get("buyVolUsd", 0)))
                sell = float(item.get("aggregated_sell_volume_usd", item.get("sellVolUsd", 0)))
                s_buy += buy
                s_sell += sell
                spot_series.append({
                    "ts": int(item.get("time", item.get("t", 0))),
                    "buy_usd": buy, "sell_usd": sell,
                    "delta_usd": buy - sell,
                })
            except (ValueError, KeyError):
                continue

    total = c_buy + c_sell + s_buy + s_sell
    buy_ratio = (c_buy + s_buy) / total if total > 0 else 0.5
    state.taker_flow = TakerFlowData(
        coin=coin.ccy, ts=int(time.time()),
        buy_ratio=round(buy_ratio, 3),
        sell_ratio=round(1 - buy_ratio, 3),
        dominant="buyers" if buy_ratio > 0.55 else "sellers" if buy_ratio < 0.45 else "balanced",
        contract_buy_vol=c_buy, contract_sell_vol=c_sell,
        spot_buy_vol=s_buy, spot_sell_vol=s_sell,
    )
    # ── MAA: 保留 5m 序列 ──
    state.taker_contract_series = contract_series[-12:]
    state.taker_spot_series = spot_series[-12:]


async def poll_orderbook_depth(cg: CoinglassSource, coin: CoinConfig, state: CoinState) -> None:
    """获取订单簿深度聚合数据（同时保留近 12 点 5m 序列供 Market Action Analyzer 使用）。"""
    data = await cg.fetch_orderbook_aggregated_ask_bids(
        coin.symbol_cg, interval="5m", limit=12,
    )
    if not data or not isinstance(data, list):
        return
    try:
        last = data[-1]
        bid_usd = float(last.get("aggregated_bids_usd", 0))
        ask_usd = float(last.get("aggregated_asks_usd", 0))
        spread = (ask_usd - bid_usd) / ((ask_usd + bid_usd) / 2) * 100 if (ask_usd + bid_usd) > 0 else 0
        state.orderbook = OrderBookAnalysis(
            coin=coin.ccy, ts=int(time.time()),
            bid_total_usd=bid_usd, ask_total_usd=ask_usd,
            spread_pct=round(spread, 2),
        )
        # ── MAA: 保留 12 点 5m 序列 ──
        series: list[dict] = []
        for item in data:
            try:
                bu = float(item.get("aggregated_bids_usd", 0))
                au = float(item.get("aggregated_asks_usd", 0))
                sp = (au - bu) / ((au + bu) / 2) * 100 if (au + bu) > 0 else 0
                series.append({
                    "ts": int(item.get("time", item.get("t", 0))),
                    "bid_usd": bu,
                    "ask_usd": au,
                    "spread_pct": round(sp, 4),
                })
            except (TypeError, ValueError, KeyError):
                continue
        state.orderbook_series = series
    except (ValueError, KeyError, IndexError):
        pass


async def poll_large_orders(cg: CoinglassSource, coin: CoinConfig, state: CoinState) -> None:
    """获取大单追踪"""
    data = await cg.fetch_large_orders(coin.exchange_primary, coin.symbol_cg_pair)
    if not data:
        return

    orders = []
    total_bid = total_ask = 0.0
    for item in data:
        try:
            raw_side = str(item.get("order_side", item.get("side", ""))).lower()
            side = "bid" if raw_side in ("buy", "bid", "2") else "ask"
            size_usd = float(item.get("start_usd_value", item.get("volUsd", 0)))
            orders.append(LargeOrder(
                ts=int(item.get("start_time", item.get("time", 0))),
                exchange=item.get("exchange_name", coin.exchange_primary),
                symbol=item.get("symbol", coin.symbol_cg_pair),
                price=float(item.get("limit_price", item.get("price", 0))),
                size_usd=size_usd,
                side=side,
                status=item.get("order_state", item.get("status", "active")),
            ))
            if side == "bid":
                total_bid += size_usd
            else:
                total_ask += size_usd
        except (ValueError, KeyError):
            continue

    state.large_orders = LargeOrderSnapshot(
        symbol=coin.symbol_cg_pair, ts=int(time.time()),
        orders=orders, total_bid_usd=total_bid, total_ask_usd=total_ask,
    )
