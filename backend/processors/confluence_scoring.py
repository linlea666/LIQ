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
    DataFreshness,
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

# M1: 跨所共识 multiplier 上限（避免极端放大）
# 公式：min(CONSENSUS_MULTIPLIER_CAP, 1.0 + 0.15 × (exchange_count - 1))
CONSENSUS_MULTIPLIER_CAP = 1.6

# M1: 失效价 ATR 倍数 - 由 strength_tier 决定
# S 级越强 → 失效阈值越远（市场需更大反向力量才能否定 S 级位）
INVALIDATION_ATR_MULT = {"S": 2.0, "A": 1.5, "B": 1.0, "C": 0.5}


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
    freshness: DataFreshness | None = None,
) -> KeyLevelSnapshotV2:
    """主入口：从 DiscoveryResult 生成 KeyLevelSnapshotV2。

    M1 新参数：
      - freshness：DataFreshness（来自 key_level_freshness.compute_freshness(state)）
        若提供，对每个 level 应用 _apply_freshness_to_level 软衰减（不破坏 tier）。
    """
    now = int(time.time())
    if current_price <= 0 or atr <= 0:
        snap = KeyLevelSnapshotV2(ts=now, current_price=current_price, atr=atr)
        snap.data_freshness = freshness
        return snap

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

    # 3. 合并已有追踪状态（bounce_count / test_count / sweep_usd 从 prev 继承）
    if prev_levels:
        scored_levels = _merge_with_prev(scored_levels, prev_levels, current_price, atr)

    # 4. Phase 2：历史验证 + 屏障 + 时间衰减 → final_score → tier
    #    必须放在合并状态之后，因为 historical_validity 依赖 bounce_count / test_count
    for lv in scored_levels:
        lv.historical_validity = _calc_historical_validity(lv)
        lv.barrier_score = _calc_barrier_score(lv, now)
        lv.final_score = _calc_final_score(lv, now)
        lv.strength_tier = _calc_tier(lv.final_score)
        lv.category = _classify(lv)
        _update_distance(lv, current_price)

        # M1: 算法化失效价（基于 strength_tier × ATR）
        inv_price, inv_cond, inv_mult = _calc_invalidation(lv, atr)
        lv.invalidation_price = inv_price
        lv.invalidation_condition = inv_cond
        lv.invalidation_atr_mult = inv_mult

    # M1: 应用数据血统软衰减（不改 tier，只衰减 final_score）
    if freshness is not None:
        from processors.key_level_freshness import apply_freshness_to_level
        for lv in scored_levels:
            apply_freshness_to_level(lv, freshness)

    # 5. 排序：非 idle 优先 → final_score 高 → 距离近
    scored_levels.sort(key=lambda lv: (
        0 if lv.state != "idle" else 1,
        -lv.final_score,
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
        # M1: magnet_levels 由 engine 层在 discover_magnets 之后塞入
        magnet_levels=[],
        data_freshness=freshness,
    )


def _calc_invalidation(lv: KeyLevelV2, atr: float) -> tuple[float | None, str, float]:
    """计算算法化失效价 + 条件描述。

    设计：
      - support: invalidation_price = price - mult × ATR （跌破即失效）
      - resistance: invalidation_price = price + mult × ATR （突破即失效）
      - mult 由 strength_tier 决定：S=2.0 / A=1.5 / B=1.0 / C=0.5
        意义：S 级位需要 2×ATR 的反向力量才被否定（不轻易失效）

    返回：(invalidation_price, condition_label, atr_mult_used)
    """
    if atr <= 0 or lv.price <= 0:
        return None, "", 0.0
    mult = INVALIDATION_ATR_MULT.get(lv.strength_tier, 1.0)
    if lv.side == "support":
        inv = lv.price - mult * atr
        cond = f"1h 收盘 < ${inv:,.0f}"
    else:
        inv = lv.price + mult * atr
        cond = f"1h 收盘 > ${inv:,.0f}"
    return round(inv, 2), cond, mult


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

    # M1: 跨所共识 - 取簇内所有 liq 候选 exchange_count 的最大值
    # 设计：一根 cluster 只要任一 liq 源（1d/7d/30d）有多所共振，就受益于共识
    max_exchange_count = 1
    max_leverage_intensity = 0.0
    dominant_leverage_label = ""
    dominant_leverage_usd = 0.0

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

        # 提取 cluster 元数据（仅 liq_cluster_* 候选填充）
        meta = cand.cluster_meta or {}
        if meta:
            ec = int(meta.get("exchange_count", 1) or 1)
            if ec > max_exchange_count:
                max_exchange_count = ec
            li = float(meta.get("leverage_intensity", 0.0) or 0.0)
            usd = float(meta.get("total_usd", 0.0) or 0.0)
            # 取贡献 USD 最大的簇的 leverage 作主导（按金额加权）
            if usd > dominant_leverage_usd and meta.get("dominant_leverage"):
                dominant_leverage_usd = usd
                dominant_leverage_label = str(meta.get("dominant_leverage") or "")
                if li > max_leverage_intensity:
                    max_leverage_intensity = li

    # 多维共振奖励：跨 2 个维度 +20%，跨 3 个维度 +40%
    if len(dimensions) >= 3:
        total_score *= 1.4
    elif len(dimensions) >= 2:
        total_score *= 1.2

    # M1: 跨所共识 multiplier（仅当有清算源时生效）
    consensus_mult = 1.0
    if max_exchange_count > 1:
        consensus_mult = min(
            CONSENSUS_MULTIPLIER_CAP,
            1.0 + 0.15 * (max_exchange_count - 1),
        )
        total_score *= consensus_mult

    total_score = min(100, total_score)

    note = _generate_note(cl, sources, total_score, price)
    chips = _build_explain_chips(
        sources, dimensions, max_exchange_count, dominant_leverage_label,
    )

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
        # M1 新增字段
        exchange_count=max_exchange_count,
        consensus_multiplier=round(consensus_mult, 3),
        dominant_leverage=dominant_leverage_label,
        leverage_intensity=round(max_leverage_intensity, 3),
        explain_chips=chips,
    )


def _build_explain_chips(
    sources: list[str],
    dimensions: set[str],
    exchange_count: int,
    dominant_leverage: str,
) -> list[str]:
    """生成"为何重要"白话 chips（前端 chip 渲染）。

    设计：
      - 每个 chip 是一个独立证据点（≤8 字符）
      - 顺序：清算 > 微观结构 > 价格结构 > 数学指标 > 共识 > 杠杆主导
      - 去重 + 截断（最多 6 个，避免 UI 拥挤）
    """
    chips: list[str] = []

    # 清算/资金维度的关键信号
    has_30d = any("30d清算" in s for s in sources)
    has_7d = any("7d清算" in s for s in sources)
    has_1d = any("清算" in s and "30d" not in s and "7d" not in s for s in sources)
    has_max_pain = any("痛点" in s for s in sources)
    has_footprint_stacked = any("Footprint" in s for s in sources)
    has_absorption = any("吸收带" in s for s in sources)

    if has_30d:
        chips.append("30d清算簇")
    if has_7d:
        chips.append("7d清算簇")
    if has_1d:
        chips.append("1d清算簇")
    if has_max_pain:
        chips.append("清算痛点")
    if has_footprint_stacked:
        chips.append("Footprint失衡")
    if has_absorption:
        chips.append("吸收带")

    # 价格结构
    if any("VWAP" in s for s in sources):
        chips.append("VWAP")
    if any("POC" in s or "HVN" in s for s in sources):
        chips.append("VP集中区")
    if any("EMA" in s or "SMA" in s for s in sources):
        chips.append("均线")
    if any("Fib" in s or "黄金" in s for s in sources):
        chips.append("Fib")
    if any("心理关口" in s or "整数" in s for s in sources):
        chips.append("心理关口")

    # 共识 / 杠杆
    if exchange_count >= 3:
        chips.append(f"{exchange_count}所共振")
    elif exchange_count == 2:
        chips.append("2所共振")
    if dominant_leverage:
        chips.append(f"{dominant_leverage}x主导")

    # 去重保序，最多 6 个
    seen: set[str] = set()
    out: list[str] = []
    for c in chips:
        if c in seen:
            continue
        seen.add(c)
        out.append(c)
        if len(out) >= 6:
            break
    return out


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


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Phase 2：历史验证 + 结构屏障 + 时间衰减 → final_score
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _calc_historical_validity(lv: KeyLevelV2) -> float:
    """0~1：基于 bounce_count / test_count / sweep_usd 组合得出历史有效性。

    设计：
      - 每次成功反弹 +0.3（最大 3 次累计 0.9）
      - 被测试的"存在感" +0.08/次（封顶 5 次即 0.4）
      - sweep_usd 达 $10M +0.2（证明被真实挂单阻击）
      - 若当前状态为 broken —— 历史有效性打 50% 折扣（被破过，可信度下调）
      - 若 flipped 状态 —— 再加 0.1（翻转位通常变为反向强位）
    返回值 clamp 到 [0, 1]。
    """
    base = 0.0
    base += min(lv.bounce_count, 3) * 0.30
    base += min(lv.test_count, 5) * 0.08
    if lv.sweep_usd > 0:
        base += min(lv.sweep_usd / 1e7, 1.0) * 0.20

    if lv.state == "broken":
        base *= 0.5
    elif lv.state == "flipped":
        base += 0.10

    return round(max(0.0, min(1.0, base)), 3)


def _calc_barrier_score(lv: KeyLevelV2, now: int) -> float:
    """0~8：结构屏障加分（仅存活时间维度）。

    修复历史（D05）：
      旧版同时用 cascade_layers × 2.5（封顶 12）给 barrier 加分，又把同一批
      clusters 计算为 cascade_risk 警告 → 造成 "A 级信号 + 级联风险 60%" 的
      双重计数，用户无法判断是否要信任信号。

      本次移除 cascade_layers 贡献，避免级联簇既"升档 A 级"又"警告踩踏"。
      cascade_risk 继续在 UI / AI prompt 中作为独立风险指标展示；是否惩罚 
      confluence_score 改由下游 Synthesizer / SafetyGate 按方向性决定。

    当前仅保留：
      存活时间 = min(age_days / 30, 1) * 8（封顶 8）：老关键位更被市场认可
    """
    if lv.first_seen_ts <= 0:
        return 0.0
    age_days = max(0, (now - lv.first_seen_ts) / 86400.0)
    barrier = min(age_days / 30.0, 1.0) * 8.0
    return round(barrier, 2)


def _final_time_decay_multiplier(lv: KeyLevelV2, now: int) -> float:
    """0.5~1.0：基于 last_confirmed_ts 的软衰减（不同于 _time_decay 作用于原始 cand）。"""
    if lv.last_confirmed_ts <= 0:
        return 1.0
    age_hours = max(0.0, (now - lv.last_confirmed_ts) / 3600.0)
    if age_hours <= 24:
        return 1.0
    if age_hours <= 72:
        return 0.9
    if age_hours <= 168:
        return 0.75
    return 0.5


def _calc_final_score(lv: KeyLevelV2, now: int) -> float:
    """final_score = clip( confluence × 时间衰减 × (1 + validity×0.3) + barrier, 0, 100 )

    设计意图：
      - 新增信号不"白得分"：仍以 confluence_score 为主干，barrier / validity 只是修饰
      - 历史验证多次 → 最多 +30% 乘数加成
      - barrier 仅代表"存活时间"加分（最多 +8），老位更被市场认可
      - broken 位通过 historical_validity 自动降权（已在 _calc_historical_validity 里）
      - 注：D05 修复后 barrier_score 理论上限从 20 降至 8（移除 cascade_layers 贡献）
    """
    time_mult = _final_time_decay_multiplier(lv, now)
    validity_mult = 1.0 + lv.historical_validity * 0.3
    core = lv.confluence_score * time_mult * validity_mult
    total = core + lv.barrier_score
    return round(max(0.0, min(100.0, total)), 1)


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
                # Phase 2：bounce_count 从状态机累计，必须从 prev 继承
                lv.bounce_count = prev.bounce_count
                lv.fake_break_count = prev.fake_break_count
                # M1: next_magnet_price / vacuum_gap_pct 由 tracker_v2 在 prev 上计算，
                # 必须从 prev 继承（每轮 discovery 重新打分会重置这些字段）
                lv.next_magnet_price = prev.next_magnet_price
                lv.vacuum_gap_pct = prev.vacuum_gap_pct
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
