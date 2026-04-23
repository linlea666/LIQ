"""清算图上下簇分析 · 从 heatmap → LiqClusterSnapshot"""

from __future__ import annotations

from typing import Optional

from models.market_action import LiqClusterSnapshot


def build_cluster_snapshot(
    heatmap: Optional[object],
    current_price: Optional[float],
) -> LiqClusterSnapshot:
    """heatmap: HeatmapData 对象（含 price_levels: list[(price, long_liq, short_liq)] 或类似）
    本函数做最兼容的解析——不同版本 HeatmapData 字段名略有差异。
    """
    snap = LiqClusterSnapshot()
    if heatmap is None or current_price is None or current_price <= 0:
        return snap

    # 兼容多种结构：HeatmapData 可能有 levels / price_levels / y_data
    levels = None
    for attr in ("levels", "price_levels", "data", "rows"):
        v = getattr(heatmap, attr, None)
        if isinstance(v, list) and v:
            levels = v
            break
    if not levels:
        return snap

    above_sum = 0.0
    below_sum = 0.0
    above_near: Optional[float] = None
    below_near: Optional[float] = None

    for lvl in levels:
        try:
            if isinstance(lvl, dict):
                price = float(lvl.get("price", lvl.get("level", 0)))
                liq_usd = float(lvl.get("usd", lvl.get("volume", lvl.get("liq", 0))))
            elif isinstance(lvl, (list, tuple)) and len(lvl) >= 2:
                price = float(lvl[0])
                liq_usd = float(lvl[1])
            else:
                continue
        except (TypeError, ValueError):
            continue
        if price <= 0 or liq_usd <= 0:
            continue
        if price > current_price:
            above_sum += liq_usd
            if above_near is None or abs(price - current_price) < abs(above_near - current_price):
                above_near = price
        elif price < current_price:
            below_sum += liq_usd
            if below_near is None or abs(price - current_price) < abs(below_near - current_price):
                below_near = price

    snap.above_cluster_usd = round(above_sum, 2)
    snap.below_cluster_usd = round(below_sum, 2)
    snap.above_nearest_price = above_near
    snap.below_nearest_price = below_near
    if above_near:
        snap.above_distance_pct = round((above_near - current_price) / current_price * 100, 3)
    if below_near:
        snap.below_distance_pct = round((current_price - below_near) / current_price * 100, 3)

    if above_sum > 0 and below_sum > 0:
        ratio = above_sum / below_sum
        if ratio > 1.5:
            snap.bias = "short_squeeze_fuel"
        elif ratio < 0.67:
            snap.bias = "long_squeeze_fuel"
        else:
            snap.bias = "balanced"
    elif above_sum > 0:
        snap.bias = "short_squeeze_fuel"
    elif below_sum > 0:
        snap.bias = "long_squeeze_fuel"

    return snap


def build_sweep_snapshot(
    liq_sweep_events,
    window_min: int = 30,
):
    """从 state.liq_sweep_events（deque）推断近 window 分钟的清算热区触发情况。"""
    from models.market_action import LiqSweepSnapshot
    import time as _time

    snap = LiqSweepSnapshot(recent_window_min=window_min)
    if not liq_sweep_events:
        return snap
    now = int(_time.time())
    cutoff = now - window_min * 60
    recent = [e for e in liq_sweep_events if int(getattr(e, "ts", 0) or e.get("ts", 0) if isinstance(e, dict) else 0) >= cutoff]
    snap.recent_sweeps_count = len(recent)
    if recent:
        last = recent[-1]
        last_ts = int(getattr(last, "ts", 0) or (last.get("ts", 0) if isinstance(last, dict) else 0))
        side = getattr(last, "side", None) or (last.get("side") if isinstance(last, dict) else None)
        snap.last_sweep_ts = last_ts
        if side in ("long_side", "short_side"):
            snap.last_sweep_side = side
        # 连续同向 3+
        same_side = [e for e in recent[-3:] if (getattr(e, "side", None) == side) or (isinstance(e, dict) and e.get("side") == side)]
        snap.continuous_trigger = len(same_side) >= 3
    return snap
