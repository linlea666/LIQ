"""关键价位计算引擎：支撑/阻力/止损安全区/入场区/插针风险区"""

from __future__ import annotations

import logging

from models.levels import (
    EntryZone,
    LevelAnalysis,
    PinRiskZone,
    PriceLevel,
    StopLossZone,
)
from models.liquidation import LiqCluster, LiquidationMap, VacuumZone
from models.market import OrderBookAnalysis, VolumeProfileData

logger = logging.getLogger(__name__)


def calculate_levels(
    coin: str,
    current_price: float,
    liq_map: LiquidationMap | None,
    vp: VolumeProfileData | None,
    orderbook: OrderBookAnalysis | None,
    atr: float,
    vwap: float,
) -> LevelAnalysis:
    """
    综合多维数据计算全部关键价位。
    每个价位都有 strength 评分（多维共振越多分越高）和来源说明。
    """
    support_candidates: list[dict] = []
    resistance_candidates: list[dict] = []

    # ── 维度1: 清算地图 ──
    if liq_map:
        for c in liq_map.clusters_below:
            support_candidates.append({
                "price": c.price_from,
                "score": min(c.total_usd / 1e6, 50),
                "source": f"{c.dominant_leverage}x多头清算${c.total_usd / 1e6:.0f}M",
            })
        for c in liq_map.clusters_above:
            resistance_candidates.append({
                "price": c.price_to,
                "score": min(c.total_usd / 1e6, 50),
                "source": f"{c.dominant_leverage}x空头清算${c.total_usd / 1e6:.0f}M",
            })

    # ── 维度2: Volume Profile ──
    if vp:
        support_candidates.append({
            "price": vp.poc_price,
            "score": 30,
            "source": "Volume Profile POC",
        })
        if vp.value_area_low < current_price:
            support_candidates.append({
                "price": vp.value_area_low,
                "score": 20,
                "source": "Value Area下沿",
            })
        if vp.value_area_high > current_price:
            resistance_candidates.append({
                "price": vp.value_area_high,
                "score": 20,
                "source": "Value Area上沿",
            })

    # ── 维度3: VWAP ──
    if vwap > 0:
        target = support_candidates if vwap < current_price else resistance_candidates
        target.append({
            "price": vwap,
            "score": 15,
            "source": "VWAP日线",
        })

    # ── 维度4: 订单簿大单 ──
    if orderbook:
        for wall in orderbook.bid_walls:
            support_candidates.append({
                "price": wall.price,
                "score": min(wall.size * 2, 25),
                "source": f"买墙{wall.size:.0f}",
            })
        for wall in orderbook.ask_walls:
            resistance_candidates.append({
                "price": wall.price,
                "score": min(wall.size * 2, 25),
                "source": f"卖墙{wall.size:.0f}",
            })

    supports = _merge_and_rank(support_candidates, current_price, "support")
    resistances = _merge_and_rank(resistance_candidates, current_price, "resistance")

    stop_loss_zones = _calc_stop_loss_zones(
        current_price, liq_map, atr,
    )

    entry_zones = _calc_entry_zones(supports, resistances, liq_map)

    pin_risk_zones = _calc_pin_risk(liq_map, current_price)

    return LevelAnalysis(
        coin=coin,
        ts=0,
        current_price=current_price,
        supports=supports[:5],
        resistances=resistances[:5],
        stop_loss_zones=stop_loss_zones,
        entry_zones=entry_zones,
        pin_risk_zones=pin_risk_zones,
    )


def _merge_and_rank(
    candidates: list[dict],
    current_price: float,
    level_type: str,
    tolerance_pct: float = 0.3,
) -> list[PriceLevel]:
    """合并相近价位，多维共振的叠加得分"""
    if not candidates:
        return []

    candidates.sort(key=lambda c: c["price"])

    merged: list[dict] = []
    for c in candidates:
        found = False
        for m in merged:
            if abs(c["price"] - m["price"]) / current_price * 100 < tolerance_pct:
                m["score"] += c["score"]
                m["sources"].append(c["source"])
                m["price"] = (m["price"] + c["price"]) / 2
                found = True
                break
        if not found:
            merged.append({
                "price": c["price"],
                "score": c["score"],
                "sources": [c["source"]],
            })

    merged.sort(key=lambda m: m["score"], reverse=True)

    results: list[PriceLevel] = []
    prefix = "S" if level_type == "support" else "R"
    for i, m in enumerate(merged[:5]):
        results.append(PriceLevel(
            price=round(m["price"], 2),
            label=f"{prefix}{i + 1}",
            level_type=level_type,
            strength=min(m["score"], 100),
            sources=m["sources"],
            note=f"共{len(m['sources'])}维共振",
        ))

    return results


def _calc_stop_loss_zones(
    current_price: float,
    liq_map: LiquidationMap | None,
    atr: float,
) -> list[StopLossZone]:
    """止损安全区：放在清算真空带内，且超过1.5倍ATR"""
    zones: list[StopLossZone] = []
    multiplier = 1.5

    # 做多止损
    long_sl = current_price - multiplier * atr
    reasons_long = [f"超出{multiplier}倍ATR(${atr:.0f})"]

    if liq_map and liq_map.vacuum_zones:
        below_vacuums = [v for v in liq_map.vacuum_zones if v.midpoint < current_price]
        if below_vacuums:
            best = min(below_vacuums, key=lambda v: abs(v.midpoint - long_sl))
            long_sl = best.midpoint
            reasons_long.insert(0, f"清算真空区${best.price_from:.0f}-${best.price_to:.0f}")

    zones.append(StopLossZone(
        direction="long",
        price=round(long_sl, 2),
        zone_from=round(long_sl - atr * 0.2, 2),
        zone_to=round(long_sl + atr * 0.2, 2),
        reasons=reasons_long,
        atr_multiple=round((current_price - long_sl) / atr, 1) if atr > 0 else 0,
    ))

    # 做空止损
    short_sl = current_price + multiplier * atr
    reasons_short = [f"超出{multiplier}倍ATR(${atr:.0f})"]

    if liq_map and liq_map.vacuum_zones:
        above_vacuums = [v for v in liq_map.vacuum_zones if v.midpoint > current_price]
        if above_vacuums:
            best = min(above_vacuums, key=lambda v: abs(v.midpoint - short_sl))
            short_sl = best.midpoint
            reasons_short.insert(0, f"清算真空区${best.price_from:.0f}-${best.price_to:.0f}")

    zones.append(StopLossZone(
        direction="short",
        price=round(short_sl, 2),
        zone_from=round(short_sl - atr * 0.2, 2),
        zone_to=round(short_sl + atr * 0.2, 2),
        reasons=reasons_short,
        atr_multiple=round((short_sl - current_price) / atr, 1) if atr > 0 else 0,
    ))

    return zones


def _calc_entry_zones(
    supports: list[PriceLevel],
    resistances: list[PriceLevel],
    liq_map: LiquidationMap | None,
) -> list[EntryZone]:
    """最佳入场区间：强支撑/阻力附近"""
    zones: list[EntryZone] = []

    if supports:
        best_s = supports[0]
        spread = best_s.price * 0.003
        zones.append(EntryZone(
            direction="long",
            price_from=round(best_s.price, 2),
            price_to=round(best_s.price + spread, 2),
            confluence_sources=best_s.sources,
            confirmation_note="价格到达后观察CVD是否转正、OI是否企稳",
        ))

    if resistances:
        best_r = resistances[0]
        spread = best_r.price * 0.003
        zones.append(EntryZone(
            direction="short",
            price_from=round(best_r.price - spread, 2),
            price_to=round(best_r.price, 2),
            confluence_sources=best_r.sources,
            confirmation_note="清算扫完后观察OI是否骤降、CVD是否背离",
        ))

    return zones


def _calc_pin_risk(
    liq_map: LiquidationMap | None,
    current_price: float,
) -> list[PinRiskZone]:
    """插针高危区：距当前价格最近的大清算池"""
    if not liq_map:
        return []

    zones: list[PinRiskZone] = []

    for c in liq_map.clusters_above[:3]:
        zones.append(PinRiskZone(
            price=c.price_center,
            side="above",
            liq_amount_usd=c.total_usd,
            note=f"空头清算${c.total_usd / 1e6:.0f}M，磁吸效应强",
        ))

    for c in liq_map.clusters_below[:3]:
        zones.append(PinRiskZone(
            price=c.price_center,
            side="below",
            liq_amount_usd=c.total_usd,
            note=f"多头清算${c.total_usd / 1e6:.0f}M，下插针目标",
        ))

    return zones
