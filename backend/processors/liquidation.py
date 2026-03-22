"""清算地图处理：密集区识别、真空区检测、失衡度计算"""

from __future__ import annotations

import logging

from models.liquidation import (
    LiqCluster,
    LiquidationMap,
    VacuumZone,
)

logger = logging.getLogger(__name__)


def process_liquidation_map(
    liq_map: LiquidationMap,
    current_price: float,
    min_cluster_usd: float = 10_000_000,
) -> LiquidationMap:
    """
    对原始清算地图进行深度处理：
    1. 跨杠杆聚合找出密集区
    2. 按当前价格分为 above/below
    3. 识别真空区
    4. 计算多空失衡度
    """
    all_short_bands = []
    all_long_bands = []
    for group in liq_map.leverage_groups:
        for band in group.short_bands:
            all_short_bands.append((band.price_from, band.price_to, band.turnover_usd, group.leverage))
        for band in group.long_bands:
            all_long_bands.append((band.price_from, band.price_to, band.turnover_usd, group.leverage))

    clusters_above = _find_clusters(all_short_bands, current_price, "short", min_cluster_usd, side_filter="above")
    clusters_below = _find_clusters(all_long_bands, current_price, "long", min_cluster_usd, side_filter="below")

    for c in clusters_above:
        c.distance_pct = round((c.price_center - current_price) / current_price * 100, 2)
    for c in clusters_below:
        c.distance_pct = round((current_price - c.price_center) / current_price * 100, 2)

    clusters_above.sort(key=lambda c: c.distance_pct)
    clusters_below.sort(key=lambda c: c.distance_pct)

    all_clusters = clusters_above + clusters_below
    vacuum_zones = _find_vacuum_zones(all_clusters, current_price)

    total_above = sum(c.total_usd for c in clusters_above) if clusters_above else 0
    total_below = sum(c.total_usd for c in clusters_below) if clusters_below else 0
    imbalance = (total_above / total_below) if total_below > 0 else 999

    liq_map.clusters_above = clusters_above
    liq_map.clusters_below = clusters_below
    liq_map.vacuum_zones = vacuum_zones
    liq_map.imbalance_ratio = round(imbalance, 2)

    return liq_map


def _auto_bucket_width(price: float) -> float:
    """根据当前价格自适应桶宽度，约为价格的 0.15%"""
    if price >= 10000:
        return 100.0
    elif price >= 1000:
        return 10.0
    elif price >= 100:
        return 1.0
    elif price >= 10:
        return 0.1
    return 0.01


def _find_clusters(
    bands: list[tuple],
    current_price: float,
    side: str,
    min_usd: float,
    side_filter: str,
) -> list[LiqCluster]:
    """
    将散落的清算区间按价格聚合成密集区。
    同一价格区间内不同杠杆的清算量叠加。
    """
    if not bands:
        return []

    if side_filter == "above":
        bands = [b for b in bands if b[1] > current_price]
    else:
        bands = [b for b in bands if b[0] < current_price]

    if not bands:
        return []

    bucket_width = _auto_bucket_width(current_price)
    price_buckets: dict[float, dict] = {}
    for price_from, price_to, usd, leverage in bands:
        mid = round((price_from + price_to) / 2, 1)
        bucket_key = round(mid / bucket_width) * bucket_width

        if bucket_key not in price_buckets:
            price_buckets[bucket_key] = {
                "total_usd": 0,
                "price_min": price_from,
                "price_max": price_to,
                "leverages": {},
            }

        b = price_buckets[bucket_key]
        b["total_usd"] += usd
        b["price_min"] = min(b["price_min"], price_from)
        b["price_max"] = max(b["price_max"], price_to)
        b["leverages"][leverage] = b["leverages"].get(leverage, 0) + usd

    clusters = []
    for key, b in price_buckets.items():
        if b["total_usd"] < min_usd:
            continue
        dom_lev = max(b["leverages"], key=b["leverages"].get) if b["leverages"] else ""
        clusters.append(LiqCluster(
            price_center=key,
            price_from=b["price_min"],
            price_to=b["price_max"],
            total_usd=round(b["total_usd"], 2),
            side=side,
            dominant_leverage=dom_lev,
        ))

    clusters.sort(key=lambda c: c.total_usd, reverse=True)
    return clusters


def _find_vacuum_zones(
    clusters: list[LiqCluster],
    current_price: float,
    min_gap_pct: float = 0.5,
) -> list[VacuumZone]:
    """识别清算真空区：两个相邻密集区之间的空白地带"""
    if len(clusters) < 2:
        return []

    sorted_clusters = sorted(clusters, key=lambda c: c.price_center)
    vacuums = []

    for i in range(len(sorted_clusters) - 1):
        c1 = sorted_clusters[i]
        c2 = sorted_clusters[i + 1]
        gap_from = c1.price_to
        gap_to = c2.price_from

        if gap_to <= gap_from:
            continue

        gap_pct = (gap_to - gap_from) / current_price * 100
        if gap_pct >= min_gap_pct:
            mid = round((gap_from + gap_to) / 2, 2)
            vacuums.append(VacuumZone(
                price_from=gap_from,
                price_to=gap_to,
                midpoint=mid,
                note=f"真空区跨度{gap_pct:.1f}%，适合放置止损",
            ))

    return vacuums
