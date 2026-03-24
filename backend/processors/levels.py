"""关键价位计算引擎：支撑/阻力/止损安全区/入场区/插针风险区/狙击挂单/阶梯埋伏"""

from __future__ import annotations

import logging
from typing import Optional

from config.settings import get_settings
from models.levels import (
    EntryZone,
    LadderEntry,
    LadderPlan,
    LevelAnalysis,
    PinRiskZone,
    PriceLevel,
    SniperEntry,
    StopLossZone,
)
from models.liquidation import LiqCluster, LiquidationMap, VacuumZone
from models.market import OrderBookAnalysis, VolumeProfileData

logger = logging.getLogger(__name__)


def calculate_levels(
    coin: str,
    current_price: float,
    liq_map: Optional[LiquidationMap],
    vp: Optional[VolumeProfileData],
    orderbook: Optional[OrderBookAnalysis],
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
            usd_m = wall.size_usd / 1e6
            support_candidates.append({
                "price": wall.price,
                "score": min(usd_m * 10, 25),
                "source": f"买墙${usd_m:.1f}M",
            })
        for wall in orderbook.ask_walls:
            usd_m = wall.size_usd / 1e6
            resistance_candidates.append({
                "price": wall.price,
                "score": min(usd_m * 10, 25),
                "source": f"卖墙${usd_m:.1f}M",
            })

    supports = _merge_and_rank(support_candidates, current_price, "support")
    resistances = _merge_and_rank(resistance_candidates, current_price, "resistance")

    stop_loss_zones = _calc_stop_loss_zones(
        current_price, liq_map, atr,
    )

    entry_zones = _calc_entry_zones(supports, resistances, liq_map)

    pin_risk_zones = _calc_pin_risk(liq_map, current_price)

    sniper_entries = _calc_sniper_entries(
        current_price, liq_map, atr, supports, resistances, vp,
    )

    ladder_plans = _calc_ladder_plans(
        current_price, liq_map, atr, supports, resistances, vp,
    )

    return LevelAnalysis(
        coin=coin,
        ts=0,
        current_price=current_price,
        supports=supports[:5],
        resistances=resistances[:5],
        stop_loss_zones=stop_loss_zones,
        entry_zones=entry_zones,
        pin_risk_zones=pin_risk_zones,
        sniper_entries=sniper_entries,
        ladder_plans=ladder_plans,
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
    """
    止损安全区设计原则（防猎杀核心）：
    1. 止损必须在清算密集区之外（不被连带爆仓）
    2. 优先放置在清算真空区内（庄家扫完清算池后价格会回弹的地带）
    3. 避开整数关口（$70000, $69000 等猎杀热门价位）
    4. ATR 倍数动态调整（波动大时拉远止损）
    """
    zones: list[StopLossZone] = []
    if atr <= 0:
        return zones

    base_mult = 1.5
    dynamic_mult = base_mult
    if current_price > 0:
        atr_pct = atr / current_price * 100
        if atr_pct > 3:
            dynamic_mult = 2.0
        elif atr_pct > 2:
            dynamic_mult = 1.8

    zones.append(_build_sl(
        "long", current_price, liq_map, atr, dynamic_mult,
    ))
    zones.append(_build_sl(
        "short", current_price, liq_map, atr, dynamic_mult,
    ))
    return zones


def _build_sl(
    direction: str,
    price: float,
    liq_map: LiquidationMap | None,
    atr: float,
    multiplier: float,
) -> StopLossZone:
    is_long = direction == "long"
    raw_sl = price - multiplier * atr if is_long else price + multiplier * atr
    reasons: list[str] = [f"ATR×{multiplier:.1f}(${atr:.0f})"]

    if liq_map:
        clusters = liq_map.clusters_below if is_long else liq_map.clusters_above
        if clusters:
            nearest = clusters[0]
            cluster_edge = nearest.price_from if is_long else nearest.price_to
            if is_long and raw_sl > cluster_edge:
                raw_sl = cluster_edge - atr * 0.3
                reasons.append(f"穿越清算簇下沿${cluster_edge:.0f}")
            elif not is_long and raw_sl < cluster_edge:
                raw_sl = cluster_edge + atr * 0.3
                reasons.append(f"穿越清算簇上沿${cluster_edge:.0f}")

        vacuums = liq_map.vacuum_zones or []
        if is_long:
            candidates = [v for v in vacuums if v.price_to < price and v.price_from < raw_sl < v.price_to]
        else:
            candidates = [v for v in vacuums if v.price_from > price and v.price_from < raw_sl < v.price_to]

        if not candidates:
            if is_long:
                candidates = [v for v in vacuums if v.price_to < price and abs(v.midpoint - raw_sl) < atr]
            else:
                candidates = [v for v in vacuums if v.price_from > price and abs(v.midpoint - raw_sl) < atr]

        if candidates:
            best_v = min(candidates, key=lambda v: abs(v.midpoint - raw_sl))
            if is_long:
                raw_sl = best_v.price_from + (best_v.price_to - best_v.price_from) * 0.3
            else:
                raw_sl = best_v.price_to - (best_v.price_to - best_v.price_from) * 0.3
            reasons.insert(0, f"真空区${best_v.price_from:.0f}-${best_v.price_to:.0f}")

    raw_sl = _avoid_round_number(raw_sl, price)
    reasons.append("避开整数关口")

    atr_mult = abs(price - raw_sl) / atr if atr > 0 else 0
    zone_pad = atr * 0.15
    return StopLossZone(
        direction=direction,
        price=round(raw_sl, 2),
        zone_from=round(raw_sl - zone_pad, 2) if is_long else round(raw_sl + zone_pad, 2),
        zone_to=round(raw_sl + zone_pad, 2) if is_long else round(raw_sl - zone_pad, 2),
        reasons=reasons,
        atr_multiple=round(atr_mult, 1),
    )


def _avoid_round_number(sl: float, current_price: float) -> float:
    """偏移止损价远离整数关口（1000/500/100 的整倍数）"""
    if current_price >= 10000:
        steps = [1000, 500]
    elif current_price >= 1000:
        steps = [100, 50]
    elif current_price >= 100:
        steps = [10, 5]
    else:
        return sl

    offset = current_price * 0.001
    for step in steps:
        nearest_round = round(sl / step) * step
        if abs(sl - nearest_round) < offset:
            if sl < current_price:
                sl = nearest_round - offset
            else:
                sl = nearest_round + offset
            break
    return sl


def _calc_entry_zones(
    supports: list[PriceLevel],
    resistances: list[PriceLevel],
    liq_map: Optional[LiquidationMap],
) -> list[EntryZone]:
    """最佳入场区间：取前2个强支撑/阻力，附近观察区"""
    zones: list[EntryZone] = []

    for s in supports[:2]:
        if s.price <= 0:
            continue
        spread = s.price * 0.003
        zones.append(EntryZone(
            direction="long",
            price_from=round(s.price, 2),
            price_to=round(s.price + spread, 2),
            confluence_sources=s.sources,
            confirmation_note="价格到达后观察CVD是否转正、OI是否企稳、订单簿买墙",
        ))

    for r in resistances[:2]:
        if r.price <= 0:
            continue
        spread = r.price * 0.003
        zones.append(EntryZone(
            direction="short",
            price_from=round(r.price - spread, 2),
            price_to=round(r.price, 2),
            confluence_sources=r.sources,
            confirmation_note="清算扫完后观察OI是否骤降、CVD是否背离、卖墙堆积",
        ))

    return zones


def _calc_sniper_entries(
    current_price: float,
    liq_map: Optional[LiquidationMap],
    atr: float,
    supports: list[PriceLevel],
    resistances: list[PriceLevel],
    vp: Optional[VolumeProfileData],
) -> list[SniperEntry]:
    """
    狙击挂单计算（小亏大赚哲学）：
    在清算密集区边缘设置极端限价单，止损在真空区内，止盈指向对侧清算磁吸点。
    只输出 R:R >= min_sniper_rr（配置 processors.levels.min_sniper_rr，默认 2.5）的计划。
    """
    if not liq_map or atr <= 0:
        return []

    entries: list[SniperEntry] = []
    vacuums = liq_map.vacuum_zones or []
    min_rr = float(get_settings().processors.levels.get("min_sniper_rr", 2.5))

    for cluster in liq_map.clusters_below[:3]:
        if cluster.distance_pct > 5 or cluster.distance_pct < 0.3:
            continue
        entry = cluster.price_from + atr * 0.1
        entry = _avoid_round_number(entry, current_price)

        sl_candidates = [v for v in vacuums
                         if v.price_to <= cluster.price_from and v.price_from < entry]
        if sl_candidates:
            best_v = max(sl_candidates, key=lambda v: v.price_to)
            sl = best_v.price_from + (best_v.price_to - best_v.price_from) * 0.3
        else:
            sl = entry - atr * 1.8
        sl = _avoid_round_number(sl, current_price)

        risk = abs(entry - sl)
        if risk <= 0:
            continue

        tp1 = current_price
        if vp and vp.poc_price > entry:
            tp1 = vp.poc_price
        if resistances and resistances[0].price > entry:
            tp1 = max(tp1, resistances[0].price)
        tp1 = max(tp1, entry + risk * min_rr)

        tp2 = tp1
        if liq_map.clusters_above:
            tp2 = liq_map.clusters_above[0].price_center

        rr1 = (tp1 - entry) / risk
        rr2 = (tp2 - entry) / risk if tp2 > entry else rr1

        if rr1 < min_rr:
            continue

        entries.append(SniperEntry(
            direction="long",
            entry_price=round(entry, 2),
            stop_loss=round(sl, 2),
            take_profit_1=round(tp1, 2),
            take_profit_2=round(tp2, 2),
            rr_ratio_1=round(rr1, 1),
            rr_ratio_2=round(rr2, 1),
            risk_usd_per_unit=round(risk, 2),
            cluster_usd=cluster.total_usd,
            logic=[
                f"多头清算簇${cluster.total_usd / 1e6:.0f}M在${cluster.price_from:.0f}-${cluster.price_to:.0f}",
                f"入场于清算簇上沿+ATR缓冲",
                f"止损在{'真空区内' if sl_candidates else 'ATR外扩'}",
                f"止盈指向对侧清算磁吸点",
            ],
        ))

    for cluster in liq_map.clusters_above[:3]:
        if cluster.distance_pct > 5 or cluster.distance_pct < 0.3:
            continue
        entry = cluster.price_to - atr * 0.1
        entry = _avoid_round_number(entry, current_price)

        sl_candidates = [v for v in vacuums
                         if v.price_from >= cluster.price_to and v.price_to > entry]
        if sl_candidates:
            best_v = min(sl_candidates, key=lambda v: v.price_from)
            sl = best_v.price_to - (best_v.price_to - best_v.price_from) * 0.3
        else:
            sl = entry + atr * 1.8
        sl = _avoid_round_number(sl, current_price)

        risk = abs(sl - entry)
        if risk <= 0:
            continue

        tp1 = current_price
        if vp and vp.poc_price < entry:
            tp1 = vp.poc_price
        if supports and supports[0].price < entry:
            tp1 = min(tp1, supports[0].price)
        tp1 = min(tp1, entry - risk * min_rr)

        tp2 = tp1
        if liq_map.clusters_below:
            tp2 = liq_map.clusters_below[0].price_center

        rr1 = (entry - tp1) / risk
        rr2 = (entry - tp2) / risk if tp2 < entry else rr1

        if rr1 < min_rr:
            continue

        entries.append(SniperEntry(
            direction="short",
            entry_price=round(entry, 2),
            stop_loss=round(sl, 2),
            take_profit_1=round(tp1, 2),
            take_profit_2=round(tp2, 2),
            rr_ratio_1=round(rr1, 1),
            rr_ratio_2=round(rr2, 1),
            risk_usd_per_unit=round(risk, 2),
            cluster_usd=cluster.total_usd,
            logic=[
                f"空头清算簇${cluster.total_usd / 1e6:.0f}M在${cluster.price_from:.0f}-${cluster.price_to:.0f}",
                f"入场于清算簇下沿-ATR缓冲",
                f"止损在{'真空区内' if sl_candidates else 'ATR外扩'}",
                f"止盈指向对侧清算磁吸点",
            ],
        ))

    entries.sort(key=lambda e: e.rr_ratio_1, reverse=True)
    return entries[:4]


def _calc_ladder_plans(
    current_price: float,
    liq_map: Optional[LiquidationMap],
    atr: float,
    supports: list[PriceLevel],
    resistances: list[PriceLevel],
    vp: Optional[VolumeProfileData],
) -> list[LadderPlan]:
    """
    阶梯式底部/顶部埋伏计划（Scaled-In Limit Order Strategy）。

    核心哲学：「不预测底部，用多层网去捕捉底部」
    - 在多个清算密集区的"区域性底部"分层挂单
    - 每层独立止损，止损放在该区域清算真空区内（防止连带爆仓）
    - 越深层仓位权重越大（倒金字塔加仓）
    - 止盈统一指向对侧清算磁吸点或POC
    - 总风险预算控制：全部层止损总额 ≤ 配置的账户风险上限

    与狙击挂单的区别：
    - 狙击挂单：单层精准猎杀位，距当前价近(≤5%)，R:R高但机会少
    - 阶梯计划：多层宽幅覆盖，距当前价远(5%-50%)，求覆盖广度而非单层精度
    """
    if not liq_map or atr <= 0 or current_price <= 0:
        return []

    cfg = get_settings().processors.levels
    max_tiers = int(cfg.get("ladder_max_tiers", 5))
    min_rr = float(cfg.get("ladder_min_rr", 3.0))
    max_distance_pct = float(cfg.get("ladder_max_distance_pct", 50.0))
    min_distance_pct = float(cfg.get("ladder_min_distance_pct", 5.0))
    total_risk_budget = float(cfg.get("ladder_total_risk_pct", 10.0))

    plans: list[LadderPlan] = []

    long_plan = _build_ladder_long(
        current_price, liq_map, atr, supports, resistances, vp,
        max_tiers, min_rr, min_distance_pct, max_distance_pct, total_risk_budget,
    )
    if long_plan:
        plans.append(long_plan)

    short_plan = _build_ladder_short(
        current_price, liq_map, atr, supports, resistances, vp,
        max_tiers, min_rr, min_distance_pct, max_distance_pct, total_risk_budget,
    )
    if short_plan:
        plans.append(short_plan)

    return plans


def _build_ladder_long(
    current_price: float,
    liq_map: LiquidationMap,
    atr: float,
    supports: list[PriceLevel],
    resistances: list[PriceLevel],
    vp: Optional[VolumeProfileData],
    max_tiers: int,
    min_rr: float,
    min_dist_pct: float,
    max_dist_pct: float,
    total_risk_pct: float,
) -> Optional[LadderPlan]:
    """构建做多方向的阶梯埋伏计划"""
    vacuums = liq_map.vacuum_zones or []

    eligible_clusters = [
        c for c in liq_map.clusters_below
        if min_dist_pct <= c.distance_pct <= max_dist_pct
    ]
    if not eligible_clusters:
        return None

    eligible_clusters.sort(key=lambda c: c.total_usd, reverse=True)
    selected = eligible_clusters[:max_tiers]
    selected.sort(key=lambda c: c.price_from, reverse=True)

    tp_target = current_price
    if vp and vp.poc_price > current_price * 0.95:
        tp_target = max(tp_target, vp.poc_price)
    if resistances:
        tp_target = max(tp_target, resistances[0].price)
    if liq_map.clusters_above:
        above_target = liq_map.clusters_above[0].price_center
        tp_target = max(tp_target, above_target)

    entries: list[LadderEntry] = []
    raw_weights: list[float] = []

    for tier_idx, cluster in enumerate(selected):
        entry = cluster.price_from + atr * 0.05
        entry = _avoid_round_number(entry, current_price)

        sl_candidates = [
            v for v in vacuums
            if v.price_to <= cluster.price_from
        ]
        if sl_candidates:
            best_v = max(sl_candidates, key=lambda v: v.price_to)
            sl = best_v.price_from + (best_v.price_to - best_v.price_from) * 0.2
        else:
            sl = entry - atr * 2.0
        sl = _avoid_round_number(sl, current_price)

        risk = abs(entry - sl)
        if risk <= 0:
            continue

        reward = tp_target - entry
        if reward <= 0:
            continue

        rr = reward / risk
        if rr < min_rr:
            continue

        depth_ratio = (current_price - entry) / current_price
        weight = 1.0 + depth_ratio * 3.0
        raw_weights.append(weight)

        zone_label = (
            f"${cluster.price_from:,.0f}-${cluster.price_to:,.0f} "
            f"清算簇({cluster.dominant_leverage}x, ${cluster.total_usd / 1e6:.0f}M)"
        )

        logic = [
            f"多头清算簇${cluster.total_usd / 1e6:.0f}M，距当前{cluster.distance_pct:.1f}%",
            f"入场于清算簇下沿+ATR微缓冲(扫完清算池后反弹起点)",
            f"止损在{'清算真空区内(庄家扫完后不会继续跌的区域)' if sl_candidates else 'ATR外扩(无合适真空区)'}",
            f"止盈指向${tp_target:,.0f}(对侧磁吸点)",
        ]

        inv_parts = [f"价格1H收盘跌破${sl:,.0f}"]
        if sl_candidates:
            inv_parts.append(f"真空区${sl_candidates[-1].price_from:,.0f}-${sl_candidates[-1].price_to:,.0f}被有效突破")

        entries.append(LadderEntry(
            tier=tier_idx + 1,
            entry_price=round(entry, 2),
            stop_loss=round(sl, 2),
            take_profit=round(tp_target, 2),
            rr_ratio=round(rr, 1),
            position_weight=0,
            risk_pct=0,
            zone_label=zone_label,
            entry_logic=logic,
            invalidation=" 或 ".join(inv_parts),
        ))

    if not entries:
        return None

    total_raw = sum(raw_weights) if raw_weights else 1
    for i, e in enumerate(entries):
        w = raw_weights[i] / total_raw if i < len(raw_weights) else 0
        e.position_weight = round(w, 3)
        e.risk_pct = round(total_risk_pct * w, 2)

    best_rr = max(e.rr_ratio for e in entries)
    worst_loss = total_risk_pct

    price_high = entries[0].entry_price
    price_low = entries[-1].entry_price
    coverage = f"${price_low:,.0f} - ${price_high:,.0f}"

    hit_any_prob_desc = f"覆盖{len(entries)}个清算密集区底部"
    summary = (
        f"在${price_low:,.0f}-${price_high:,.0f}区间分{len(entries)}层埋伏多单, "
        f"总风险{total_risk_pct:.1f}%, "
        f"任一层吃到底部反弹至${tp_target:,.0f}即可获利, "
        f"最佳R:R={best_rr:.1f}:1"
    )

    return LadderPlan(
        direction="long",
        tier_count=len(entries),
        entries=entries,
        total_risk_pct=round(total_risk_pct, 2),
        best_case_rr=round(best_rr, 1),
        worst_case_loss_pct=round(worst_loss, 2),
        expected_edge=f"小亏大赚: 全扫损亏{worst_loss:.1f}%, 任一层命中可赚{best_rr:.0f}倍止损距离. {hit_any_prob_desc}",
        plan_summary=summary,
        coverage_range=coverage,
    )


def _build_ladder_short(
    current_price: float,
    liq_map: LiquidationMap,
    atr: float,
    supports: list[PriceLevel],
    resistances: list[PriceLevel],
    vp: Optional[VolumeProfileData],
    max_tiers: int,
    min_rr: float,
    min_dist_pct: float,
    max_dist_pct: float,
    total_risk_pct: float,
) -> Optional[LadderPlan]:
    """构建做空方向的阶梯埋伏计划"""
    vacuums = liq_map.vacuum_zones or []

    eligible_clusters = [
        c for c in liq_map.clusters_above
        if min_dist_pct <= c.distance_pct <= max_dist_pct
    ]
    if not eligible_clusters:
        return None

    eligible_clusters.sort(key=lambda c: c.total_usd, reverse=True)
    selected = eligible_clusters[:max_tiers]
    selected.sort(key=lambda c: c.price_to)

    tp_target = current_price
    if vp and vp.poc_price < current_price * 1.05:
        tp_target = min(tp_target, vp.poc_price)
    if supports:
        tp_target = min(tp_target, supports[0].price)
    if liq_map.clusters_below:
        below_target = liq_map.clusters_below[0].price_center
        tp_target = min(tp_target, below_target)

    entries: list[LadderEntry] = []
    raw_weights: list[float] = []

    for tier_idx, cluster in enumerate(selected):
        entry = cluster.price_to - atr * 0.05
        entry = _avoid_round_number(entry, current_price)

        sl_candidates = [
            v for v in vacuums
            if v.price_from >= cluster.price_to
        ]
        if sl_candidates:
            best_v = min(sl_candidates, key=lambda v: v.price_from)
            sl = best_v.price_to - (best_v.price_to - best_v.price_from) * 0.2
        else:
            sl = entry + atr * 2.0
        sl = _avoid_round_number(sl, current_price)

        risk = abs(sl - entry)
        if risk <= 0:
            continue

        reward = entry - tp_target
        if reward <= 0:
            continue

        rr = reward / risk
        if rr < min_rr:
            continue

        height_ratio = (entry - current_price) / current_price
        weight = 1.0 + height_ratio * 3.0
        raw_weights.append(weight)

        zone_label = (
            f"${cluster.price_from:,.0f}-${cluster.price_to:,.0f} "
            f"清算簇({cluster.dominant_leverage}x, ${cluster.total_usd / 1e6:.0f}M)"
        )

        logic = [
            f"空头清算簇${cluster.total_usd / 1e6:.0f}M，距当前{cluster.distance_pct:.1f}%",
            f"入场于清算簇上沿-ATR微缓冲(扫完清算池后回落起点)",
            f"止损在{'清算真空区内(庄家扫完后不会继续涨的区域)' if sl_candidates else 'ATR外扩(无合适真空区)'}",
            f"止盈指向${tp_target:,.0f}(对侧磁吸点)",
        ]

        inv_parts = [f"价格1H收盘突破${sl:,.0f}"]
        if sl_candidates:
            inv_parts.append(f"真空区${sl_candidates[0].price_from:,.0f}-${sl_candidates[0].price_to:,.0f}被有效突破")

        entries.append(LadderEntry(
            tier=tier_idx + 1,
            entry_price=round(entry, 2),
            stop_loss=round(sl, 2),
            take_profit=round(tp_target, 2),
            rr_ratio=round(rr, 1),
            position_weight=0,
            risk_pct=0,
            zone_label=zone_label,
            entry_logic=logic,
            invalidation=" 或 ".join(inv_parts),
        ))

    if not entries:
        return None

    total_raw = sum(raw_weights) if raw_weights else 1
    for i, e in enumerate(entries):
        w = raw_weights[i] / total_raw if i < len(raw_weights) else 0
        e.position_weight = round(w, 3)
        e.risk_pct = round(total_risk_pct * w, 2)

    best_rr = max(e.rr_ratio for e in entries)
    worst_loss = total_risk_pct

    price_low = entries[0].entry_price
    price_high = entries[-1].entry_price
    coverage = f"${price_low:,.0f} - ${price_high:,.0f}"

    hit_any_prob_desc = f"覆盖{len(entries)}个清算密集区顶部"
    summary = (
        f"在${price_low:,.0f}-${price_high:,.0f}区间分{len(entries)}层埋伏空单, "
        f"总风险{total_risk_pct:.1f}%, "
        f"任一层吃到顶部回落至${tp_target:,.0f}即可获利, "
        f"最佳R:R={best_rr:.1f}:1"
    )

    return LadderPlan(
        direction="short",
        tier_count=len(entries),
        entries=entries,
        total_risk_pct=round(total_risk_pct, 2),
        best_case_rr=round(best_rr, 1),
        worst_case_loss_pct=round(worst_loss, 2),
        expected_edge=f"小亏大赚: 全扫损亏{worst_loss:.1f}%, 任一层命中可赚{best_rr:.0f}倍止损距离. {hit_any_prob_desc}",
        plan_summary=summary,
        coverage_range=coverage,
    )


def _calc_pin_risk(
    liq_map: Optional[LiquidationMap],
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
