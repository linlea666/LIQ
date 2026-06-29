"""足迹图（Footprint）轮询 · Market Action Analyzer 专用

数据源：Coinglass
  · /api/futures/volume/footprint-history
  · /api/spot/volume/footprint-history

返回结构（已实测）：
  data = [
      [<ts_sec>, [
          [price_lo, price_hi, buy_base, sell_base, buy_quote, sell_quote,
           buy_quote_agg, sell_quote_agg, buy_trades, sell_trades],
          ...                 # 一根 K 线下的多个价位 bucket
      ]],
      ...                     # 多根 K 线
  ]

本 poll 职责：
  1. 拉取最近 3 根 1h K 线的足迹（期货 + 现货）
  2. 存到 state.footprint_contract / state.footprint_spot（原始 buckets，maxlen=3）
  3. 不在 poll 层做派生计算，派生交给 processors.market_action.footprint_analyzer
"""

from __future__ import annotations

import logging
import time
from collections import deque
from typing import TYPE_CHECKING, Any, Optional

from config.settings import CoinConfig
from sources.coinglass import CoinglassSource

if TYPE_CHECKING:
    from engine import CoinState

logger = logging.getLogger(__name__)


def _parse_bar(raw_bar: Any) -> Optional[dict]:
    """解析单根 K 线的原始数据 → dict(ts, buckets: list[dict])"""
    if not isinstance(raw_bar, (list, tuple)) or len(raw_bar) < 2:
        return None
    ts_raw = raw_bar[0]
    rows = raw_bar[1]
    if not isinstance(rows, list) or not rows:
        return None
    try:
        ts = int(ts_raw)
    except (TypeError, ValueError):
        return None

    buckets: list[dict] = []
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) < 10:
            continue
        try:
            buckets.append({
                "price_lo": float(row[0]),
                "price_hi": float(row[1]),
                "buy_base": float(row[2]),
                "sell_base": float(row[3]),
                "buy_quote": float(row[4]),
                "sell_quote": float(row[5]),
                "buy_trades": int(row[8]),
                "sell_trades": int(row[9]),
            })
        except (TypeError, ValueError):
            continue

    if not buckets:
        return None
    return {"ts": ts, "buckets": buckets}


def _ensure_deque(state: "CoinState", attr: str, maxlen: int = 3) -> deque:
    """确保 state 上存在 deque 字段；若无则新建。"""
    dq = getattr(state, attr, None)
    if not isinstance(dq, deque):
        dq = deque(maxlen=maxlen)
        setattr(state, attr, dq)
    return dq


def _merge_bars(dq: deque, new_bars: list[dict]) -> int:
    """把新拉到的 bars 合并进 deque（按 ts 去重 + 覆盖更新当前未完 bar）。返回新增/更新数量。"""
    existing_ts = {b.get("ts"): i for i, b in enumerate(dq)}
    updated = 0
    for bar in new_bars:
        ts = bar.get("ts")
        if ts is None:
            continue
        if ts in existing_ts:
            idx = existing_ts[ts]
            dq[idx] = bar  # 覆盖（同一根 K 线累积更新）
            updated += 1
        else:
            dq.append(bar)
            updated += 1
    return updated


async def poll_footprint(
    cg: CoinglassSource,
    coin: CoinConfig,
    state: "CoinState",
    interval: str = "1h",
    limit: int = 3,
    exchange: str = "Binance",
) -> None:
    """合约 + 现货足迹图一次拉取，写入 state.footprint_contract / state.footprint_spot。"""
    contract_bars: list[dict] = []
    spot_bars: list[dict] = []
    spot_payload_seen = False

    # ── 合约 ──
    try:
        raw = await cg.fetch_futures_footprint_history(
            exchange=exchange, symbol=coin.symbol_cg_pair,
            interval=interval, limit=limit,
        )
        if isinstance(raw, list):
            for rb in raw:
                parsed = _parse_bar(rb)
                if parsed:
                    contract_bars.append(parsed)
    except Exception:
        logger.warning("poll_footprint contract failed | coin=%s", coin.ccy, exc_info=True)
        state.poll_failures["footprint_contract"] = "API调用失败"

    # ── 现货 ──
    try:
        raw = await cg.fetch_spot_footprint_history(
            exchange=exchange, symbol=coin.symbol_cg_pair,
            interval=interval, limit=limit,
        )
        if isinstance(raw, list):
            spot_payload_seen = bool(raw)
            interval_sec = {"5m": 300, "15m": 900, "1h": 3600, "4h": 14_400}.get(
                interval, 3600,
            )
            now = int(time.time())
            max_age = max(3600, (limit + 1) * interval_sec)
            for rb in raw:
                parsed = _parse_bar(rb)
                if parsed:
                    ts = int(parsed["ts"])
                    if ts > 10_000_000_000:
                        ts //= 1000
                    if ts > 0 and 0 <= now - ts <= max_age:
                        parsed["ts"] = ts
                        spot_bars.append(parsed)
    except Exception:
        logger.warning("poll_footprint spot failed | coin=%s", coin.ccy, exc_info=True)
        state.poll_failures["footprint_spot"] = "API调用失败"

    # ── 写入 state ──
    if contract_bars:
        dq_c = _ensure_deque(state, "footprint_contract", maxlen=max(3, limit))
        _merge_bars(dq_c, contract_bars)
        state.footprint_last_ts = int(time.time())
        state.poll_failures.pop("footprint_contract", None)

    if spot_bars:
        dq_s = _ensure_deque(state, "footprint_spot", maxlen=max(3, limit))
        _merge_bars(dq_s, spot_bars)
        state.footprint_spot_last_ts = int(time.time())
        state.poll_failures.pop("footprint_spot", None)
    elif spot_payload_seen:
        state.poll_failures["footprint_spot"] = "无有效或新鲜的现货Footprint"

    if "footprint_ready" not in state._log_once_keys and (contract_bars or spot_bars):
        state._log_once_keys.add("footprint_ready")
        logger.info(
            "Footprint 生效 | coin=%s contract_bars=%d spot_bars=%d interval=%s",
            coin.ccy, len(contract_bars), len(spot_bars), interval,
        )
