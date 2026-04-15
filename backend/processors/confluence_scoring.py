"""Confluence Scoring Engine — 多维共振聚类评分与分类

核心算法：
1. 动态容差聚类：tolerance = max(0.15%, 0.3 * ATR / price)
2. 共振评分 = sum(source_weight * time_decay * distance_bonus)
3. 分类标签：STRONG_SUPPORT / MODERATE / BULL_BEAR_LINE / BREAKOUT / FIB
4. 输出按距现价排序，不限固定数量
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field

from models.key_level import (
    BullBearLine,
    BreakoutZone,
    FibSnapshot,
    KeyLevelSnapshotV2,
    KeyLevelV2,
)
from processors.level_discovery import DiscoveryResult, RawCandidate
from processors.ta_core import BBSqueezeResult, detect_bb_squeeze

logger = logging.getLogger(__name__)

DIMENSION_WEIGHTS = {
    "price_structure": 1.3,
    "capital_flow": 1.2,
    "math_indicator": 1.0,
}

STRENGTH_THRESHOLDS = {
    "S": 70,
    "A": 45,
    "B": 25,
}


@dataclass
class _Cluster:
    """内部聚类桶"""
    price_sum: float = 0
    count: int = 0
    candidates: list[RawCandidate] = field(default_factory=list)
    side: str = ""

    @property
    def price(self) -> float:
        return self.price_sum / self.count if self.count else 0


def score_and_build_snapshot(
    discovery: DiscoveryResult,
    current_price: float,
    atr: float,
    prev_levels: list[KeyLevelV2] | None = None,
    boll_data: dict | None = None,
    boll_4h_data: dict | None = None,
    macd_histogram: float | None = None,
) -> KeyLevelSnapshotV2:
    """主入口：从 DiscoveryResult 生成 KeyLevelSnapshotV2。"""
    now = int(time.time())
    if current_price <= 0 or atr <= 0:
        return KeyLevelSnapshotV2(ts=now, current_price=current_price, atr=atr)

    candidates = discovery.candidates

    # 1. 动态容差聚类
    tolerance = max(0.0015, 0.3 * atr / current_price)
    clusters = _cluster_candidates(candidates, tolerance)

    # 2. 共振评分
    scored_levels: list[KeyLevelV2] = []
    for cl in clusters:
        level = _score_cluster(cl, current_price, atr, now)
        if level.confluence_score < 10:
            continue
        scored_levels.append(level)

    # 3. 合并已有追踪状态
    if prev_levels:
        scored_levels = _merge_with_prev(scored_levels, prev_levels, current_price, atr)

    # 4. 分类标签
    for lv in scored_levels:
        lv.strength_tier = _calc_tier(lv.confluence_score)
        lv.category = _classify(lv)
        _update_distance(lv, current_price)

    # 5. 排序：非 idle 优先 → 强度高 → 距离近
    scored_levels.sort(key=lambda lv: (
        0 if lv.state != "idle" else 1,
        -lv.confluence_score,
        abs(lv.distance_pct),
    ))

    # 6. 构建特殊展示区
    bull_bear = _build_bull_bear_line(discovery, current_price)
    breakout = _build_breakout_zone(boll_data, boll_4h_data, discovery.keltner, macd_histogram)
    fib_snap = _build_fib_snapshot(discovery)

    # 7. 市场结构摘要
    supports = [lv for lv in scored_levels if lv.side == "support" and lv.price < current_price]
    resistances = [lv for lv in scored_levels if lv.side == "resistance" and lv.price > current_price]
    nearest_sup = min(supports, key=lambda l: abs(l.distance_pct)) if supports else None
    nearest_res = min(resistances, key=lambda l: abs(l.distance_pct)) if resistances else None
    structure = _summarize_structure(current_price, bull_bear, nearest_sup, nearest_res)

    # 8. 多周期分层
    tf_info = _find_tf_levels(scored_levels, current_price)

    return KeyLevelSnapshotV2(
        ts=now,
        current_price=round(current_price, 2),
        atr=round(atr, 2),
        levels=scored_levels,
        bull_bear_line=bull_bear,
        breakout_zone=breakout,
        fib_snapshot=fib_snap,
        signals=[],
        active_count=sum(1 for lv in scored_levels if lv.state != "idle"),
        structure_summary=structure,
        nearest_strong_support=nearest_sup.price if nearest_sup else None,
        nearest_strong_resistance=nearest_res.price if nearest_res else None,
        daily_strong_support=tf_info["daily_sup"],
        daily_strong_resistance=tf_info["daily_res"],
        weekly_strong_support=tf_info["weekly_sup"],
        weekly_strong_resistance=tf_info["weekly_res"],
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 聚类
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _cluster_candidates(
    candidates: list[RawCandidate],
    tolerance: float,
) -> list[_Cluster]:
    sorted_cands = sorted(candidates, key=lambda c: c.price)
    clusters: list[_Cluster] = []

    for cand in sorted_cands:
        merged = False
        for cl in clusters:
            if cl.count > 0 and abs(cand.price - cl.price) / max(cl.price, 1) < tolerance:
                if cl.side == cand.side or cl.side == "":
                    cl.price_sum += cand.price
                    cl.count += 1
                    cl.candidates.append(cand)
                    if not cl.side:
                        cl.side = cand.side
                    merged = True
                    break
        if not merged:
            clusters.append(_Cluster(
                price_sum=cand.price, count=1,
                candidates=[cand], side=cand.side,
            ))

    return clusters


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 评分
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _score_cluster(
    cl: _Cluster,
    price: float,
    atr: float,
    now: int,
) -> KeyLevelV2:
    total_score = 0.0
    sources: list[str] = []
    source_tags: set[str] = set()
    dimensions: set[str] = set()
    best_tf = ""
    best_tf_score = 0.0

    for cand in cl.candidates:
        dim_weight = DIMENSION_WEIGHTS.get(cand.dimension, 1.0)
        time_decay = _time_decay(cand.data_age_hours)
        dist_pct = abs(cl.price - price) / price * 100
        dist_bonus = _distance_bonus(dist_pct)
        score = cand.base_score * dim_weight * time_decay * dist_bonus
        total_score += score
        sources.append(cand.source)
        source_tags.add(cand.source_tag)
        dimensions.add(cand.dimension)

        if score > best_tf_score:
            best_tf_score = score
            best_tf = cand.timeframe

    # 多维共振奖励：跨 2 个维度 +20%，跨 3 个维度 +40%
    if len(dimensions) >= 3:
        total_score *= 1.4
    elif len(dimensions) >= 2:
        total_score *= 1.2

    total_score = min(100, total_score)

    note = _generate_note(cl, sources, total_score, price)

    return KeyLevelV2(
        price=round(cl.price, 2),
        side=cl.side,
        sources=sources[:8],
        source_count=len(set(sources)),
        confluence_score=round(total_score, 1),
        timeframe=best_tf,
        first_seen_ts=now,
        last_confirmed_ts=now,
        note=note,
    )


def _time_decay(age_hours: float) -> float:
    if age_hours <= 24:
        return 1.0
    if age_hours <= 168:
        return 0.7
    return 0.4


def _distance_bonus(dist_pct: float) -> float:
    """越近现价 bonus 越高（指数衰减）"""
    return math.exp(-dist_pct / 8.0)


def _calc_tier(score: float) -> str:
    for tier, threshold in STRENGTH_THRESHOLDS.items():
        if score >= threshold:
            return tier
    return "C"


def _classify(lv: KeyLevelV2) -> str:
    is_sma200 = any("200日SMA" in s or "sma_200" in s for s in lv.sources)
    is_bmsa = any("牛市支撑带" in s or "bmsa" in s for s in lv.sources)

    if is_sma200 or is_bmsa:
        return f"bull_bear_{lv.side}"
    if any("Fib" in s for s in lv.sources):
        if lv.strength_tier in ("S", "A"):
            return f"strong_{lv.side}"
        return f"fib_{lv.side}"
    if lv.strength_tier in ("S", "A"):
        return f"strong_{lv.side}"
    return f"moderate_{lv.side}"


def _generate_note(cl: _Cluster, sources: list[str], score: float, price: float) -> str:
    """生成白话说明"""
    n = len(set(sources))
    side_cn = "支撑" if cl.side == "support" else "阻力"
    dist = abs(cl.price - price) / price * 100

    if score >= 70:
        strength = "极强"
    elif score >= 45:
        strength = "较强"
    elif score >= 25:
        strength = "中等"
    else:
        strength = "较弱"

    top_sources = list(dict.fromkeys(sources))[:3]
    sources_str = "、".join(top_sources)
    return f"{strength}{side_cn}位，{n}维共振({sources_str})，距现价{dist:.1f}%"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 合并历史状态
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _merge_with_prev(
    new_levels: list[KeyLevelV2],
    prev_levels: list[KeyLevelV2],
    price: float,
    atr: float,
) -> list[KeyLevelV2]:
    merge_tol = max(0.002, 0.2 * atr / price) if price > 0 else 0.002
    matched: set[int] = set()

    for lv in new_levels:
        for i, prev in enumerate(prev_levels):
            if i in matched:
                continue
            if abs(lv.price - prev.price) / max(lv.price, 1) < merge_tol:
                lv.state = prev.state
                lv.state_ts = prev.state_ts
                lv.prev_state = prev.prev_state
                lv.test_count = prev.test_count
                lv.sweep_usd = prev.sweep_usd
                lv.lowest_wick = prev.lowest_wick
                lv.break_start_ts = prev.break_start_ts
                lv.cascade_risk = prev.cascade_risk
                lv.cascade_layers = prev.cascade_layers
                lv.cascade_total_usd = prev.cascade_total_usd
                lv.first_seen_ts = prev.first_seen_ts
                matched.add(i)
                break

    return new_levels


def _update_distance(lv: KeyLevelV2, price: float):
    if price > 0:
        lv.distance_pct = round((lv.price - price) / price * 100, 3)
        new_side = "resistance" if lv.price > price else "support"
        if new_side != lv.side:
            old_cn = "支撑" if lv.side == "support" else "阻力"
            new_cn = "支撑" if new_side == "support" else "阻力"
            lv.side = new_side
            lv.category = _classify(lv)
            if old_cn in lv.note:
                lv.note = lv.note.replace(old_cn, new_cn)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 特殊展示构建
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _build_bull_bear_line(discovery: DiscoveryResult, price: float) -> BullBearLine:
    bb = BullBearLine()
    bb.sma200d = discovery.sma200d

    if discovery.bmsa:
        bb.bmsa_upper = discovery.bmsa.sma_20w
        bb.bmsa_lower = discovery.bmsa.ema_21w

    if discovery.ichimoku:
        bb.ichimoku_cloud_top = discovery.ichimoku.cloud_top
        bb.ichimoku_cloud_bottom = discovery.ichimoku.cloud_bottom

    # 判断多空格局
    bull_signals = 0
    bear_signals = 0
    reasons = []

    if bb.sma200d and price > 0:
        if price > bb.sma200d:
            bull_signals += 2
            reasons.append("价格在200日均线上方")
        else:
            bear_signals += 2
            reasons.append("价格在200日均线下方")

    if bb.bmsa_upper and bb.bmsa_lower and price > 0:
        if price > bb.bmsa_upper:
            bull_signals += 1
            reasons.append("价格在牛市支撑带上方")
        elif price < bb.bmsa_lower:
            bear_signals += 1
            reasons.append("价格在牛市支撑带下方")

    if discovery.ichimoku and discovery.ichimoku.cloud_bullish is not None:
        if discovery.ichimoku.cloud_bullish:
            bull_signals += 1
        else:
            bear_signals += 1

    if bull_signals > bear_signals:
        bb.current_regime = "bull"
    elif bear_signals > bull_signals:
        bb.current_regime = "bear"
    else:
        bb.current_regime = "neutral"

    bb.regime_reason = "；".join(reasons) if reasons else "数据不足"
    return bb


def _build_breakout_zone(
    boll_data: dict | None,
    boll_4h_data: dict | None,
    kc: object | None,
    macd_hist: float | None,
) -> BreakoutZone | None:
    boll = boll_4h_data or boll_data
    if not boll:
        return None

    from processors.ta_core import KeltnerResult
    kc_result = kc if isinstance(kc, KeltnerResult) else None

    squeeze = detect_bb_squeeze(
        bb_upper=boll.get("upper"),
        bb_lower=boll.get("lower"),
        bb_middle=boll.get("middle"),
        kc=kc_result,
        macd_histogram=macd_hist,
    )

    if not squeeze.is_squeeze and (squeeze.bb_width_pct is None or squeeze.bb_width_pct > 6):
        return None

    note = ""
    if squeeze.is_squeeze:
        dir_cn = {"up": "偏上", "down": "偏下"}.get(squeeze.squeeze_direction, "方向未定")
        note = f"布林带挤压中(宽度{squeeze.bb_width_pct:.1f}%)，蓄力{dir_cn}突破"
    else:
        note = f"布林带较窄(宽度{squeeze.bb_width_pct:.1f}%)，关注突破方向"

    return BreakoutZone(
        bb_squeeze=squeeze.is_squeeze,
        squeeze_direction=squeeze.squeeze_direction,
        bb_upper=squeeze.bb_upper,
        bb_lower=squeeze.bb_lower,
        keltner_upper=squeeze.kc_upper,
        keltner_lower=squeeze.kc_lower,
        note=note,
    )


def _build_fib_snapshot(discovery: DiscoveryResult) -> FibSnapshot | None:
    if not discovery.fib_levels:
        return None
    return FibSnapshot(
        swing_high=discovery.fib_swing_high,
        swing_low=discovery.fib_swing_low,
        direction=discovery.fib_direction,
        levels=[
            {"ratio": fl.ratio, "price": fl.price, "label": fl.label}
            for fl in discovery.fib_levels
        ],
    )


def _find_zone_or_single(
    levels: list[KeyLevelV2],
    price: float,
    side: str,
) -> str | None:
    """在已筛选的 level 中识别支撑/阻力带或单一价位。"""
    strong = [
        lv for lv in levels
        if lv.side == side
        and ((lv.price < price) if side == "support" else (lv.price > price))
    ]
    if not strong:
        return None
    strong.sort(key=lambda l: abs(l.distance_pct))
    best = strong[0]
    zone_peers = [
        lv for lv in strong
        if abs(lv.price - best.price) / max(best.price, 1) < 0.005
        and lv is not best
    ]
    if zone_peers:
        lo = min(best.price, *(p.price for p in zone_peers))
        hi = max(best.price, *(p.price for p in zone_peers))
        return f"${lo:,.0f}~${hi:,.0f}({abs(best.distance_pct):.1f}%)"
    return f"${best.price:,.0f}({abs(best.distance_pct):.1f}%)"


def _find_tf_levels(
    scored_levels: list[KeyLevelV2],
    price: float,
) -> dict:
    """按日线/周线分层提取最强支撑阻力。"""
    result: dict[str, str | None] = {
        "daily_sup": None, "daily_res": None,
        "weekly_sup": None, "weekly_res": None,
    }

    _is_weekly_source = lambda lv: any(
        kw in s for s in lv.sources
        for kw in ("1W", "牛市支撑带", "bmsa", "200W", "STH", "Pi Cycle", "CVDD")
    )
    daily_levels = [
        lv for lv in scored_levels
        if lv.timeframe in ("1D", "4H") and lv.strength_tier in ("S", "A")
    ]
    weekly_levels = [
        lv for lv in scored_levels
        if (lv.timeframe == "1W" or _is_weekly_source(lv))
        and lv.strength_tier in ("S", "A", "B")
    ]

    result["daily_sup"] = _find_zone_or_single(daily_levels, price, "support")
    result["daily_res"] = _find_zone_or_single(daily_levels, price, "resistance")
    result["weekly_sup"] = _find_zone_or_single(weekly_levels, price, "support")
    result["weekly_res"] = _find_zone_or_single(weekly_levels, price, "resistance")
    return result


def _summarize_structure(
    price: float,
    bb: BullBearLine,
    nearest_sup: KeyLevelV2 | None,
    nearest_res: KeyLevelV2 | None,
) -> str:
    regime = {"bull": "偏多", "bear": "偏空", "neutral": "震荡"}.get(bb.current_regime, "待定")
    parts = [f"市场结构{regime}"]
    if nearest_sup:
        parts.append(f"最近强支撑${nearest_sup.price:,.0f}({abs(nearest_sup.distance_pct):.1f}%)")
    if nearest_res:
        parts.append(f"最近强阻力${nearest_res.price:,.0f}({abs(nearest_res.distance_pct):.1f}%)")
    return "，".join(parts)
