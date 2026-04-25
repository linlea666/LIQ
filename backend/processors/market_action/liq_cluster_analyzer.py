"""清算图上下簇分析 · 从 heatmap / liquidation_map → LiqClusterSnapshot

数据源优先级：
  1. LiquidationMap.clusters_above / clusters_below（processors/liquidation.py 已预聚合）
     · 还会附带 vacuum_zones，用于"路径化"扫单分析
  2. HeatmapData.data（list[HeatmapDataPoint(price, value, ts)]）
     · 仅作降级路径，无 top3/vacuum 输出（heatmap 是逐价位序列，无簇语义）
"""

from __future__ import annotations

from typing import Optional

from models.market_action import (
    LiqClusterEntry,
    LiqClusterSnapshot,
    VacuumZoneEntry,
)


def _point_price_value(lvl) -> Optional[tuple[float, float]]:
    """兼容 HeatmapDataPoint(BaseModel) / dict / list-tuple 三种结构。"""
    try:
        # BaseModel（有 .price / .value 属性）
        if hasattr(lvl, "price") and (hasattr(lvl, "value") or hasattr(lvl, "usd")):
            price = float(getattr(lvl, "price"))
            value = float(getattr(lvl, "value", None) or getattr(lvl, "usd", 0) or 0)
            return price, value
        if isinstance(lvl, dict):
            price = float(lvl.get("price", lvl.get("level", 0)))
            value = float(
                lvl.get("value",
                        lvl.get("usd",
                                lvl.get("volume",
                                        lvl.get("liq", 0))))
            )
            return price, value
        if isinstance(lvl, (list, tuple)) and len(lvl) >= 2:
            return float(lvl[0]), float(lvl[1])
    except (TypeError, ValueError):
        return None
    return None


def _build_from_preaggregated(
    liq_map,
    current_price: float,
) -> Optional[LiqClusterSnapshot]:
    """优先走 LiquidationMap.clusters_above / clusters_below（字段结构更清晰）。

    扩展输出：
      - top3_above / top3_below：按 total_usd 降序，距离百分比相对当前价折算
      - vacuum_zones：直接采用 LiquidationMap.vacuum_zones（processors/liquidation.py 已识别）
    """
    if liq_map is None:
        return None
    clusters_above = getattr(liq_map, "clusters_above", None) or []
    clusters_below = getattr(liq_map, "clusters_below", None) or []
    if not clusters_above and not clusters_below:
        return None
    snap = LiqClusterSnapshot()
    above_sum = sum(float(c.total_usd) for c in clusters_above if getattr(c, "total_usd", 0))
    below_sum = sum(float(c.total_usd) for c in clusters_below if getattr(c, "total_usd", 0))
    snap.above_cluster_usd = round(above_sum, 2)
    snap.below_cluster_usd = round(below_sum, 2)
    if clusters_above:
        nearest_a = min(clusters_above, key=lambda c: abs(c.price_center - current_price))
        snap.above_nearest_price = float(nearest_a.price_center)
        snap.above_distance_pct = round(
            (nearest_a.price_center - current_price) / current_price * 100, 3
        )
    if clusters_below:
        nearest_b = min(clusters_below, key=lambda c: abs(c.price_center - current_price))
        snap.below_nearest_price = float(nearest_b.price_center)
        snap.below_distance_pct = round(
            (current_price - nearest_b.price_center) / current_price * 100, 3
        )

    # ── top3 by total_usd（同侧降序）──
    def _to_entry(cluster, side: str) -> Optional[LiqClusterEntry]:
        try:
            price = float(cluster.price_center)
            total = float(cluster.total_usd)
            if total <= 0:
                return None
            if side == "above":
                dist = (price - current_price) / current_price * 100
            else:
                dist = (current_price - price) / current_price * 100
            return LiqClusterEntry(
                price=price,
                total_usd=round(total, 2),
                distance_pct=round(dist, 3),
            )
        except (TypeError, ValueError, AttributeError):
            return None

    top_a = [_to_entry(c, "above") for c in clusters_above]
    top_a = [e for e in top_a if e is not None]
    top_a.sort(key=lambda e: e.total_usd, reverse=True)
    snap.top3_above = top_a[:3]

    top_b = [_to_entry(c, "below") for c in clusters_below]
    top_b = [e for e in top_b if e is not None]
    top_b.sort(key=lambda e: e.total_usd, reverse=True)
    snap.top3_below = top_b[:3]

    # ── vacuum_zones（直接来自 LiquidationMap，已由 processors/liquidation.py 识别）──
    vacs_raw = getattr(liq_map, "vacuum_zones", None) or []
    vacs: list[VacuumZoneEntry] = []
    for v in vacs_raw:
        try:
            pf = float(getattr(v, "price_from", 0))
            pt = float(getattr(v, "price_to", 0))
            mp = float(getattr(v, "midpoint", 0) or ((pf + pt) / 2 if pf and pt else 0))
            note = str(getattr(v, "note", "") or "")
            if pf <= 0 or pt <= 0:
                continue
            vacs.append(VacuumZoneEntry(
                price_from=pf, price_to=pt, midpoint=mp, note=note,
            ))
        except (TypeError, ValueError, AttributeError):
            continue
    snap.vacuum_zones = vacs

    return snap


def build_cluster_snapshot(
    heatmap: Optional[object],
    current_price: Optional[float],
    liq_map: Optional[object] = None,
) -> LiqClusterSnapshot:
    """优先 LiquidationMap.clusters_{above,below}，回退 HeatmapData.data。"""
    snap = LiqClusterSnapshot()
    if current_price is None or current_price <= 0:
        return snap

    pre = _build_from_preaggregated(liq_map, current_price)
    if pre is not None and (pre.above_cluster_usd > 0 or pre.below_cluster_usd > 0):
        return pre

    if heatmap is None:
        return snap

    levels = None
    for attr in ("data", "levels", "price_levels", "rows"):
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
        pv = _point_price_value(lvl)
        if pv is None:
            continue
        price, liq_usd = pv
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
