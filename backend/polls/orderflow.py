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
from models.orderbook_pressure import LargeOrderLifecycle
from processors.cvd import detect_cvd_price_divergence
from sources.coinglass import CoinglassSource
from utils.time_series import dedupe_sorted_points, normalize_epoch_seconds

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
    # 12 根 5m delta 的和才是完整 1h；端点 CVD 相减只覆盖 11 个间隔。
    diff = delta_sum
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
                ts = normalize_epoch_seconds(item.get("time", item.get("t", 0)))
                buy = float(item.get("agg_taker_buy_vol", item.get("buyVolUsd", 0)))
                sell = float(item.get("agg_taker_sell_vol", item.get("sellVolUsd", 0)))
                cvd_val = float(item.get("cum_vol_delta", item.get("cvd", buy - sell)))
                points.append(CVDPoint(
                    ts=ts, buy_vol=buy, sell_vol=sell,
                    delta=buy - sell, cvd=cvd_val,
                ))
            except (ValueError, KeyError):
                continue

        points = dedupe_sorted_points(points, ts_getter=lambda p: p.ts)
        if points:
            trend, delta_1h = calc_cvd_trend(points)
            state.cvd_contract = CVDData(
                coin=coin.ccy, inst_type="CONTRACTS",
                ts=points[-1].ts, series=points,
                trend_1h=trend, delta_1h=delta_1h,
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
                ts = normalize_epoch_seconds(item.get("time", item.get("t", 0)))
                buy = float(item.get("agg_taker_buy_vol", item.get("buyVolUsd", 0)))
                sell = float(item.get("agg_taker_sell_vol", item.get("sellVolUsd", 0)))
                cvd_val = float(item.get("cum_vol_delta", item.get("cvd", buy - sell)))
                points.append(CVDPoint(
                    ts=ts, buy_vol=buy, sell_vol=sell,
                    delta=buy - sell, cvd=cvd_val,
                ))
            except (ValueError, KeyError):
                continue
        points = dedupe_sorted_points(points, ts_getter=lambda p: p.ts)
        if points:
            trend, delta = calc_cvd_trend(points)
            state.cvd_spot = CVDData(
                coin=coin.ccy, inst_type="SPOT",
                ts=points[-1].ts, series=points,
                trend_1h=trend, delta_1h=delta,
            )
            if state.candle_prices:
                state.cvd_spot = detect_cvd_price_divergence(
                    state.cvd_spot, state.candle_prices, state.candle_ts,
                )


async def poll_taker_volume(cg: CoinglassSource, coin: CoinConfig, state: CoinState) -> None:
    """获取 Taker 买卖量（保留 5m 序列供 Market Action Analyzer 使用）。"""
    contract_data = await cg.fetch_aggregated_taker_bs_history(
        coin.symbol_cg, interval="5m", limit=24,
    )
    spot_data = await cg.fetch_spot_aggregated_taker_bs(
        coin.symbol_cg, interval="5m", limit=24,
    )

    def _ts_sec(raw) -> int:
        try:
            v = int(raw)
        except (TypeError, ValueError):
            return 0
        return v // 1000 if v > 10_000_000_000 else v

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
                    "ts": _ts_sec(item.get("time", item.get("t", 0))),
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
                    "ts": _ts_sec(item.get("time", item.get("t", 0))),
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
        # ── MAA: 保留 12 点 5m 序列 · ts 归一为秒 ──
        def _ts_sec(raw) -> int:
            try:
                v = int(raw)
            except (TypeError, ValueError):
                return 0
            return v // 1000 if v > 10_000_000_000 else v

        series: list[dict] = []
        for item in data:
            try:
                bu = float(item.get("aggregated_bids_usd", 0))
                au = float(item.get("aggregated_asks_usd", 0))
                sp = (au - bu) / ((au + bu) / 2) * 100 if (au + bu) > 0 else 0
                series.append({
                    "ts": _ts_sec(item.get("time", item.get("t", 0))),
                    "bid_usd": bu,
                    "ask_usd": au,
                    "spread_pct": round(sp, 4),
                })
            except (TypeError, ValueError, KeyError):
                continue
        state.orderbook_series = series
    except (ValueError, KeyError, IndexError):
        pass


def _parse_side(raw) -> str:
    """Coinglass /large-limit-order(-history) 的 order_side 约定（2026-08-18 生产实测）：
        1 = ask（卖单 / 上方阻力）
        2 = bid（买单 / 下方支撑）

    实测方法：期货 273 条 + 现货 179 条 holding 大单对照现价——
    side=1 全部在现价上方、side=2 全部在现价下方（盘口几何唯一解）。
    历史教训：此处已两次修反（最初 2 当 bid 是对的，后被按错误"实测"
    翻转成 1=bid），任何再次改动必须先跑 side × (limit_price-现价)
    分布校验，禁止凭文档或猜测翻转。
    """
    if isinstance(raw, (int, float)):
        return "ask" if int(raw) == 1 else "bid"
    s = str(raw).strip().lower()
    if s in ("1", "ask", "sell"):
        return "ask"
    if s in ("2", "bid", "buy"):
        return "bid"
    return "ask"


def _parse_state(raw) -> str:
    """Coinglass order_state: 1=holding, 2=ended"""
    try:
        return "holding" if int(raw) == 1 else "ended"
    except (TypeError, ValueError):
        return "holding"


def _build_lifecycles(raw_list, fallback_exchange: str) -> list[LargeOrderLifecycle]:
    """把 Coinglass /large-limit-order(-history) 的 raw items 转成 Pydantic Lifecycle 列表。

    与 LargeOrderSnapshot 是兄弟数据：snapshot 只取 active 简化字段，lifecycle 保留全套。
    """
    out: list[LargeOrderLifecycle] = []
    if not raw_list or not isinstance(raw_list, list):
        return out
    for item in raw_list:
        if not isinstance(item, dict):
            continue
        try:
            out.append(LargeOrderLifecycle(
                id=int(item.get("id", 0)),
                side=_parse_side(item.get("order_side")),
                limit_price=float(item.get("limit_price", 0) or 0),
                start_time_ms=int(item.get("start_time", 0) or 0),
                end_time_ms=int(item["order_end_time"]) if item.get("order_end_time") else None,
                start_quantity=float(item.get("start_quantity", 0) or 0),
                current_quantity=float(item.get("current_quantity", 0) or 0),
                executed_volume=float(item.get("executed_volume", 0) or 0),
                executed_usd_value=float(item.get("executed_usd_value", 0) or 0),
                start_usd_value=float(item.get("start_usd_value", 0) or 0),
                current_usd_value=float(item.get("current_usd_value", 0) or 0),
                trade_count=int(item.get("trade_count", 0) or 0),
                state=_parse_state(item.get("order_state")),
                exchange_name=str(item.get("exchange_name") or fallback_exchange) or None,
            ))
        except (TypeError, ValueError):
            continue
    return out


async def poll_large_orders(cg: CoinglassSource, coin: CoinConfig, state: CoinState) -> None:
    """获取大单追踪：当前快照(LargeOrderSnapshot) + lifecycle 历史(state.large_orders_history)。

    实测：
      - /large-limit-order            ：含 holding(1) + 部分 ended(2) ~ 数百条
      - /large-limit-order-history    ：最近 1000 条 ended 大单的完整 lifecycle (~24h 跨度)
    两者合并后由 Orderbook Pressure 模块用于 L2"撤单 vs 被吃"判定。
    """
    snapshot_data = await cg.fetch_large_orders(coin.exchange_primary, coin.symbol_cg_pair)
    history_data = await cg.fetch_large_orders_history(coin.exchange_primary, coin.symbol_cg_pair)

    # ── (1) 当前快照（兼容旧前端 / KL tracker bid_walls/ask_walls） ──
    if snapshot_data:
        orders: list[LargeOrder] = []
        total_bid = total_ask = 0.0
        for item in snapshot_data:
            if not isinstance(item, dict):
                continue
            try:
                side = _parse_side(item.get("order_side", item.get("side")))
                size_usd = float(item.get("start_usd_value", item.get("volUsd", 0)) or 0)
                state_str = _parse_state(item.get("order_state"))
                orders.append(LargeOrder(
                    ts=int(item.get("start_time", item.get("time", 0)) or 0),
                    exchange=str(item.get("exchange_name", coin.exchange_primary)),
                    symbol=str(item.get("symbol", coin.symbol_cg_pair)),
                    price=float(item.get("limit_price", item.get("price", 0)) or 0),
                    size_usd=size_usd, side=side,
                    status="active" if state_str == "holding" else "ended",
                ))
                if side == "bid":
                    total_bid += size_usd
                else:
                    total_ask += size_usd
            except (ValueError, KeyError, TypeError):
                continue
        state.large_orders = LargeOrderSnapshot(
            symbol=coin.symbol_cg_pair, ts=int(time.time()),
            orders=orders, total_bid_usd=total_bid, total_ask_usd=total_ask,
        )

    # ── (2) Lifecycle 双轨：snapshot active + history ended，去重后写入 state ──
    snapshot_life = _build_lifecycles(snapshot_data or [], coin.exchange_primary)
    history_life = _build_lifecycles(history_data or [], coin.exchange_primary)

    seen: set[int] = set()
    merged: list[LargeOrderLifecycle] = []
    for lo in snapshot_life + history_life:
        if lo.id in seen or lo.id == 0:
            continue
        seen.add(lo.id)
        merged.append(lo)
    state.large_orders_history = merged

    if "large_orders_lifecycle_ready" not in state._log_once_keys and merged:
        state._log_once_keys.add("large_orders_lifecycle_ready")
        n_hold = sum(1 for x in merged if x.state == "holding")
        n_ended = len(merged) - n_hold
        logger.info(
            "大单 lifecycle 接通 | coin=%s total=%d holding=%d ended=%d",
            coin.ccy, len(merged), n_hold, n_ended,
        )


async def poll_spot_large_orders(cg: CoinglassSource, coin: CoinConfig, state: CoinState) -> None:
    """现货大单（M2.5：诉求"现货=真支撑、合约=清算磁铁"）。

    与 poll_large_orders（合约大单）互补：
      - 合约大单 ≈ 流动性墙 + 潜在清算磁铁（高杠杆挂单，本身可能是扫单目标）
      - 现货大单 ≈ 真买家/卖家（真金白银），是"真支撑/真阻力"的硬证据

    Liquidity Wall Engine 用 spot 大单匹配 zone 价区，输出 has_spot_confluence
    标志，用于前端 chip 区分（💎 真支撑 vs 默认合约墙）。

    端点：/api/spot/orderbook/large-limit-order(-history)
    quota：每币 ~2 calls/poll cycle，30 币 × 24h × 60min ≈ 1700 calls/day（无忧）。
    现货交易所：默认 Binance（spot 端点是按交易所查的）。
    """
    # spot 与 futures 用同一个 symbol（probe 验证：BTCUSDT 通用）
    snapshot_data = await cg.fetch_spot_large_orders(coin.exchange_primary, coin.symbol_cg_pair)
    history_data = await cg.fetch_spot_large_orders_history(coin.exchange_primary, coin.symbol_cg_pair)

    snapshot_life = _build_lifecycles(snapshot_data or [], coin.exchange_primary)
    history_life = _build_lifecycles(history_data or [], coin.exchange_primary)

    seen: set[int] = set()
    merged: list[LargeOrderLifecycle] = []
    for lo in snapshot_life + history_life:
        if lo.id in seen or lo.id == 0:
            continue
        seen.add(lo.id)
        merged.append(lo)
    state.spot_large_orders_history = merged

    if "spot_large_orders_ready" not in state._log_once_keys and merged:
        state._log_once_keys.add("spot_large_orders_ready")
        n_hold = sum(1 for x in merged if x.state == "holding")
        logger.info(
            "现货大单接通 | coin=%s total=%d holding=%d",
            coin.ccy, len(merged), n_hold,
        )
