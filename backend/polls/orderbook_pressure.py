"""挂单压力监测器：订单簿深度热力图轮询。

只负责数据拉取与最简解析，把原始 bins 写入：
  - ``state.orderbook_depth_snapshot``（latest 帧，沿用旧消费者）
  - ``state.orderbook_depth_history``（M1 滚动 deque，按 ts_sec 去重，maxlen=12 = 1h）

全部"找堆 / WallZone 聚合 / 持续性评分 / 行为事件"逻辑在 ``processors/orderbook_pressure.py``。

数据源：``/api/futures/orderbook/history``
  返回：list[ [ts_sec, [[bid_price, bid_qty_base], ...], [[ask_price, ask_qty_base], ...] ] ]
  - limit=12 时给出最近 1h 的 12 个 5m snapshot（M1 升级后默认）
  - 每个 snapshot ~30-40 KB，limit=12 ~ 360-480 KB / 次（仍可接受）

大单 lifecycle 的拉取仍在 ``polls/orderflow.poll_large_orders``，本 poll 不重复请求。
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from config.settings import CoinConfig
from models.orderbook_pressure import DepthBin, OrderbookDepthSnapshot
from sources.coinglass import CoinglassSource

if TYPE_CHECKING:
    from engine import CoinState

logger = logging.getLogger(__name__)


def _normalize_ts_sec(raw) -> int:
    """API ts 可能是秒或毫秒，统一为秒（与 MAA 其他字段保持一致）。"""
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return 0
    return v // 1000 if v > 10_000_000_000 else v


def _parse_bins(raw_bins) -> list[DepthBin]:
    """[[price, qty_base], ...] → list[DepthBin]，按价格升序。

    qty_base 单位是 base coin（如 BTC），usd = price × qty_base。
    """
    out: list[DepthBin] = []
    if not isinstance(raw_bins, list):
        return out
    for entry in raw_bins:
        if not (isinstance(entry, list) and len(entry) >= 2):
            continue
        try:
            price = float(entry[0])
            qty = float(entry[1])
        except (TypeError, ValueError):
            continue
        if price <= 0 or qty <= 0:
            continue
        out.append(DepthBin(
            price=price, quantity=qty, usd_value=price * qty,
        ))
    out.sort(key=lambda b: b.price)
    return out


def _parse_snapshot_row(row, exchange: str, symbol: str, coin: str):
    """[ts, bids, asks] → 拆出 ts_sec、bids 列表、asks 列表。返回 (ts, bids, asks) 或 None。"""
    if not (isinstance(row, list) and len(row) >= 3):
        return None
    ts = _normalize_ts_sec(row[0])
    bids = _parse_bins(row[1])
    asks = _parse_bins(row[2])
    if not bids and not asks:
        return None
    return ts, bids, asks


async def poll_orderbook_pressure(
    cg: CoinglassSource, coin: CoinConfig, state: "CoinState",
) -> None:
    """拉取订单簿深度热力图，写入 state.orderbook_depth_snapshot。

    使用单交易所深度（与 large_orders 来源一致，便于 L1+ 大单价位匹配）。
    后续真假分类的"小堆减量 vs 主动成交"对比依赖 prev_* snapshot。
    """
    # M1：limit=12（1h 滚动） · 一次拉满，本地按 ts_sec 去重维护历史
    data = await cg.fetch_orderbook_heatmap(
        exchange=coin.exchange_primary,
        symbol=coin.symbol_cg_pair,
        interval="5m",
        limit=12,
    )
    if not data or not isinstance(data, list):
        return

    # 依时间升序：保证 data[-1] 是 latest
    parsed = []
    for row in data:
        p = _parse_snapshot_row(row, coin.exchange_primary, coin.symbol_cg_pair, coin.ccy)
        if p:
            parsed.append(p)
    if not parsed:
        return
    parsed.sort(key=lambda x: x[0])

    # 取出已存在的 ts_sec，做去重写入
    existing_ts = {snap.ts_sec for snap in state.orderbook_depth_history}

    # ── 构造每帧 OrderbookDepthSnapshot 并按 ts_sec 去重写入 history ──
    new_frames_count = 0
    for ts, bids, asks in parsed:
        if ts in existing_ts:
            continue
        frame = OrderbookDepthSnapshot(
            coin=coin.ccy,
            exchange=coin.exchange_primary,
            symbol=coin.symbol_cg_pair,
            ts_sec=ts or int(time.time()),
            bids=bids,
            asks=asks,
            # prev_* 仍由 history 自身承载；保留旧字段空（向后兼容）
            prev_ts_sec=None,
            prev_bids=[],
            prev_asks=[],
        )
        state.orderbook_depth_history.append(frame)
        existing_ts.add(ts)
        new_frames_count += 1

    # ── 当前帧 + 上一帧（向后兼容旧消费者：仍喂 prev_* 字段）──
    latest_ts, latest_bids, latest_asks = parsed[-1]
    prev_ts, prev_bids, prev_asks = (None, [], [])
    if len(parsed) >= 2:
        prev_ts, prev_bids, prev_asks = parsed[-2]

    state.orderbook_depth_snapshot = OrderbookDepthSnapshot(
        coin=coin.ccy,
        exchange=coin.exchange_primary,
        symbol=coin.symbol_cg_pair,
        ts_sec=latest_ts or int(time.time()),
        bids=latest_bids,
        asks=latest_asks,
        prev_ts_sec=prev_ts,
        prev_bids=prev_bids,
        prev_asks=prev_asks,
    )

    if "orderbook_depth_ready" not in state._log_once_keys:
        state._log_once_keys.add("orderbook_depth_ready")
        logger.info(
            "订单簿深度热力图接通 | coin=%s exchange=%s bids_bins=%d asks_bins=%d "
            "history_size=%d new_frames=%d",
            coin.ccy, coin.exchange_primary,
            len(latest_bids), len(latest_asks),
            len(state.orderbook_depth_history), new_frames_count,
        )


async def poll_spot_orderbook_pressure(
    cg: CoinglassSource, coin: CoinConfig, state: "CoinState",
) -> None:
    """Phase A：现货 5m 深度热力图（``/api/spot/orderbook/history``）。

    与合约 ``poll_orderbook_pressure`` 用同一解析逻辑（_parse_snapshot_row）。
    数据写入 ``state.spot_orderbook_depth_history``（独立 deque），由
    ``processors/liquidity_wall_engine.build_liquidity_wall_outputs`` 拼接成
    "spot_only" / "spot+depth" 双源 WallZone。

    bin 间距实测 100 USD（合约 5-10 USD），但单 bin USD 量级与合约相近，
    阈值经 ``orderbook_pressure.config`` 同源（共用 wall_min_usd / seed_min_usd）。
    """
    data = await cg.fetch_spot_orderbook_heatmap(
        exchange=coin.exchange_primary,
        symbol=coin.symbol_cg_pair,
        interval="5m",
        limit=12,
    )
    if not data or not isinstance(data, list):
        return

    parsed = []
    for row in data:
        p = _parse_snapshot_row(row, coin.exchange_primary, coin.symbol_cg_pair, coin.ccy)
        if p:
            parsed.append(p)
    if not parsed:
        return
    parsed.sort(key=lambda x: x[0])

    existing_ts = {snap.ts_sec for snap in state.spot_orderbook_depth_history}
    new_frames_count = 0
    for ts, bids, asks in parsed:
        if ts in existing_ts:
            continue
        frame = OrderbookDepthSnapshot(
            coin=coin.ccy,
            exchange=coin.exchange_primary,
            symbol=coin.symbol_cg_pair,
            ts_sec=ts or int(time.time()),
            bids=bids,
            asks=asks,
            prev_ts_sec=None,
            prev_bids=[],
            prev_asks=[],
        )
        state.spot_orderbook_depth_history.append(frame)
        existing_ts.add(ts)
        new_frames_count += 1

    if "spot_orderbook_depth_ready" not in state._log_once_keys and new_frames_count > 0:
        state._log_once_keys.add("spot_orderbook_depth_ready")
        latest_ts, latest_bids, latest_asks = parsed[-1]
        logger.info(
            "现货订单簿深度热力图接通 | coin=%s exchange=%s bids_bins=%d asks_bins=%d "
            "history_size=%d new_frames=%d",
            coin.ccy, coin.exchange_primary,
            len(latest_bids), len(latest_asks),
            len(state.spot_orderbook_depth_history), new_frames_count,
        )
