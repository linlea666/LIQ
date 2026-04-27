"""Confluence Scoring Engine — 多维共振聚类评分与分类

核心算法：
1. 动态容差聚类：tolerance = max(0.15%, 0.3 * ATR / price)
2. 共振评分 = sum(source_weight * time_decay * distance_bonus)
3. 分类标签：STRONG_SUPPORT / MODERATE / BULL_BEAR_LINE / BREAKOUT / FIB
4. 输出按距现价排序，不限固定数量
"""

from __future__ import annotations

import hashlib
import logging
import math
import time
from dataclasses import dataclass, field
from typing import Optional

from models.key_level import (
    BullBearLine,
    BreakoutZone,
    DataFreshness,
    FibSnapshot,
    KeyLevelSnapshotV2,
    KeyLevelV2,
    LifecycleEvent,
)
from models.regime import RegimeSnapshot
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

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# M2 · TTL & 时间衰减（GPT V3 评审采纳）
# 旧 _time_decay 用 24h/168h 阶梯，对短 TTL 数据（footprint/orderbook）过粗
# 新 apply_ttl_decay：按 source_tag → SOURCE_TTL_HOURS 取 TTL 后做 exp 衰减
# 对 macro 源（200W SMA、月线 EMA）TTL 极长，几乎不衰减
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SOURCE_TTL_HOURS: dict[str, float] = {
    # 微结构（极短 TTL）
    "footprint_stacked": 2.0,
    "absorption_zone_support": 4.0,
    "absorption_zone_resistance": 4.0,
    "oi_surge_zone": 4.0,
    # 短周期清算
    "liq_cluster_below_1d": 24.0,
    "liq_cluster_above_1d": 24.0,
    # 中周期清算
    "liq_cluster_below_7d": 168.0,
    "liq_cluster_above_7d": 168.0,
    # 长周期清算
    "liq_cluster_below_30d": 720.0,
    "liq_cluster_above_30d": 720.0,
    # 本地技术（中 TTL）
    "vwap": 24.0,
    "vp_poc": 168.0,
    "vp_val": 168.0,
    "vp_vah": 168.0,
    "ichimoku_cloud_top": 24.0,
    "ichimoku_cloud_bottom": 24.0,
    "ichimoku_kijun": 24.0,
    "fib_0.382": 168.0,
    "fib_0.5": 168.0,
    "fib_0.618": 168.0,
    "fib_0.786": 168.0,
    "pivot_p": 24.0,
    "pivot_r1": 24.0,
    "pivot_r2": 24.0,
    "pivot_s1": 24.0,
    "pivot_s2": 24.0,
    "ma_cluster": 168.0,
    "ema_50_1d": 168.0,
    "ema_100_1d": 168.0,
    # 宏观（长 TTL）
    "ema_200_1d": 720.0,
    "sma_200_1d": 8760.0,    # 200d SMA — 1 年 TTL
    "bmsa_upper": 8760.0,
    "bmsa_lower": 8760.0,
    # 结构锚点（中长 TTL）
    "round_major": 8760.0,    # 整数关口几乎不过期
    "round_minor": 720.0,
    "unfilled_wick_low": 168.0,
    "unfilled_wick_high": 168.0,
    "hvn": 168.0,
    "onchain_real": 8760.0,
    "onchain_btc-": 8760.0,
    "onchain_etfm": 8760.0,
}

# 默认 TTL（未匹配的 source_tag fallback）
DEFAULT_TTL_HOURS = 24.0


def _resolve_ttl_hours(source_tag: str) -> float:
    """source_tag → TTL 小时数（含通配兜底）。"""
    if source_tag in SOURCE_TTL_HOURS:
        return SOURCE_TTL_HOURS[source_tag]
    if source_tag.startswith("swing_"):
        # swing_*_1w / 1d / 4h / 1h 按时间框架定 TTL
        if "_1w" in source_tag:
            return 720.0
        if "_1d" in source_tag:
            return 168.0
        if "_4h" in source_tag:
            return 48.0
        return 24.0
    if source_tag.startswith("liq_cluster_"):
        if source_tag.endswith("_30d"):
            return 720.0
        if source_tag.endswith("_7d"):
            return 168.0
        return 24.0
    return DEFAULT_TTL_HOURS


def apply_ttl_decay(base_score: float, source_tag: str, age_hours: float) -> float:
    """对单候选位 base_score 做 TTL 指数衰减（M2 · 替代 _time_decay 阶梯）。

    公式：score' = base_score × exp(-age_hours / ttl)
    - age_hours = 0 → 1.0 不衰减
    - age_hours = ttl → ~0.37
    - age_hours = 2×ttl → ~0.135
    设计：对 macro 源（ttl=8760h）几乎不衰减；对 footprint（ttl=2h）严格衰减。
    """
    if age_hours <= 0:
        return base_score
    ttl = _resolve_ttl_hours(source_tag)
    if ttl <= 0:
        return base_score
    return base_score * math.exp(-age_hours / ttl)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# M3 · R8 · regime-aware scoring 权重表（GPT V3 评审采纳）
# 设计：
#   - 6 种 regime × 8 evidence_groups → modifier ∈ [0.85, 1.10]
#   - 趋势市：结构 / 趋势相关组加权，短线清算降权
#   - 震荡市：本地技术 / 短线清算加权（扫单频繁），结构降权
#   - squeeze：所有组温和降权（待方向选择，可信度暂降）
#   - high_vol_chop / extreme：大幅降权（噪声大，关键位失效频发）
# 对每个 level 取「其 evidence_groups 中 modifier 最大值」作为最终乘子，
# 避免单一弱组拖累多组共振强位。
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
REGIME_WEIGHT_VERSION = "3.0"

REGIME_MODIFIER_TABLE: dict[str, dict[str, float]] = {
    "trend_up": {
        "structure_anchor": 1.10,
        "macro_technical": 1.05,
        "local_technical": 0.95,
        "liquidation_macro": 1.05,
        "liquidation_meso": 1.00,
        "liquidation_short": 0.95,
        "microstructure_local": 0.95,
        "flow_dynamic": 1.05,
    },
    "trend_down": {
        "structure_anchor": 1.10,
        "macro_technical": 1.05,
        "local_technical": 0.95,
        "liquidation_macro": 1.05,
        "liquidation_meso": 1.00,
        "liquidation_short": 0.95,
        "microstructure_local": 0.95,
        "flow_dynamic": 1.05,
    },
    "range": {
        "structure_anchor": 1.05,
        "macro_technical": 0.95,
        "local_technical": 1.10,
        "liquidation_macro": 0.95,
        "liquidation_meso": 1.05,
        "liquidation_short": 1.10,
        "microstructure_local": 1.10,
        "flow_dynamic": 1.00,
    },
    "squeeze": {
        "structure_anchor": 0.95,
        "macro_technical": 0.95,
        "local_technical": 0.95,
        "liquidation_macro": 0.95,
        "liquidation_meso": 0.95,
        "liquidation_short": 0.95,
        "microstructure_local": 0.95,
        "flow_dynamic": 0.95,
    },
    "high_vol_chop": {
        "structure_anchor": 0.90,
        "macro_technical": 0.90,
        "local_technical": 0.90,
        "liquidation_macro": 0.90,
        "liquidation_meso": 0.90,
        "liquidation_short": 0.90,
        "microstructure_local": 0.90,
        "flow_dynamic": 0.90,
    },
    "extreme": {
        "structure_anchor": 0.85,
        "macro_technical": 0.85,
        "local_technical": 0.85,
        "liquidation_macro": 0.85,
        "liquidation_meso": 0.85,
        "liquidation_short": 0.85,
        "microstructure_local": 0.85,
        "flow_dynamic": 0.85,
    },
}

# regime label 中文映射（前端 / NOFX 可解释性）
REGIME_LABEL_CN: dict[str, str] = {
    "trend_up": "趋势上涨",
    "trend_down": "趋势下跌",
    "range": "箱体震荡",
    "squeeze": "蓄力收敛",
    "high_vol_chop": "高波动无序",
    "extreme": "极端行情",
}


def apply_regime_modifier(lv: KeyLevelV2, regime: str) -> float:
    """根据 regime + lv.evidence_groups 返回 final_score 的乘子（M3 · R8）。

    返回 multiplier ∈ [0.85, 1.10]；若 regime 为空 / 不识别 / lv 无 evidence_groups
    则返回 1.0（中性，不影响）。

    取值规则：
      - 取 lv.evidence_groups 中各组对应 modifier 的最大值（避免单一弱组拖累强位）
      - 若 lv.evidence_groups 为空 → 用 8 组中位数 1.0 不调整
    """
    if not regime or regime not in REGIME_MODIFIER_TABLE:
        return 1.0
    if not lv.evidence_groups:
        return 1.0
    table = REGIME_MODIFIER_TABLE[regime]
    return max((table.get(g, 1.0) for g in lv.evidence_groups), default=1.0)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# M3 · R9 · level_id 稳定 hash（GPT V3 评审采纳）
# 设计：
#   - 基于 ATR 自适应价格桶 → 同一关键位在不同 ATR 下保持 ID 稳定
#   - 不含 side：side 翻转时 level_id 保留（生命周期里记录 flipped 事件）
#   - 不含 coin：API 路径已带 {coin}，coin 内唯一即可
# 输出：12 字符 hex（约 16M 桶空间，单币种内极不可能碰撞）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def make_level_id(price: float, atr: float) -> str:
    """生成稳定 level_id：sha1("bucket:{round(price/bucket_size)}")[:12]。

    bucket_size = max(atr * 0.5, price * 0.001)
      - 小币 ATR 大 → 桶大；大币 ATR 小 → 桶小
      - price * 0.001 兜底（避免 ATR=0 时除零）

    例：BTC price=63000 atr=400 → bucket_size=200 → bucket=315 → id="abc123def456"
    同一 level 在 ±0.5×ATR 范围内移动均得到相同 ID（保持稳定）
    """
    if price <= 0:
        return ""
    bucket_size = max(atr * 0.5, price * 0.001)
    if bucket_size <= 0:
        return ""
    bucket = round(price / bucket_size)
    raw = f"bucket:{bucket}"
    return hashlib.sha1(raw.encode()).hexdigest()[:12]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# M3 · R9 · lifecycle 事件 diff（每轮快照与 prev 比较）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# 事件门槛（可调）
_LIFECYCLE_SCORE_DELTA_THRESHOLD = 5.0   # final_score 变化 ≥ 5 分才记为 strengthen/weaken
_LIFECYCLE_MAX_EVENTS = 20               # 单 level 最多保留事件数（防内存膨胀）

_TIER_RANK = {"S": 4, "A": 3, "B": 2, "C": 1, "": 0}


def _detect_lifecycle_events(
    prev: Optional[KeyLevelV2],
    new: KeyLevelV2,
    now: int,
) -> list[LifecycleEvent]:
    """对比 prev 与 new，产出本轮新增的 lifecycle events（M3 · R9）。

    检测：
      - born:                prev 为 None 时
      - strengthening:       final_score 上涨 ≥ 阈值
      - weakening:           final_score 下跌 ≥ 阈值
      - tier_upgraded:       strength_tier 提升（C→B / B→A / A→S）
      - tier_downgraded:     strength_tier 下降
      - flipped:             side 翻转（support↔resistance）

    不在此处检测的（由 tracker_v2._set_state 直接 push）：
      - tested / reacted / broken / fake_break
      （这些状态在 tracker 主循环中确定，且需考虑 OI/CVD 确认逻辑）

    expired 由 history API 端在快照对比时识别（不属 lv 自身字段）
    """
    events: list[LifecycleEvent] = []
    if prev is None:
        events.append(LifecycleEvent(
            ts=now,
            event_type="born",
            detail=f"首次出现 · {new.strength_tier}级 · score {new.final_score:.0f}",
            score_after=new.final_score,
            tier_after=new.strength_tier,
            state_after=new.state,
            layer="scoring",
        ))
        return events

    # final_score 变化检测
    delta = new.final_score - prev.final_score
    if delta >= _LIFECYCLE_SCORE_DELTA_THRESHOLD:
        events.append(LifecycleEvent(
            ts=now,
            event_type="strengthening",
            detail=f"评分上涨 +{delta:.1f}（{prev.final_score:.0f}→{new.final_score:.0f}）",
            score_before=prev.final_score,
            score_after=new.final_score,
            layer="scoring",
        ))
    elif delta <= -_LIFECYCLE_SCORE_DELTA_THRESHOLD:
        events.append(LifecycleEvent(
            ts=now,
            event_type="weakening",
            detail=f"评分下跌 {delta:.1f}（{prev.final_score:.0f}→{new.final_score:.0f}）",
            score_before=prev.final_score,
            score_after=new.final_score,
            layer="scoring",
        ))

    # tier 变化检测
    pr = _TIER_RANK.get(prev.strength_tier, 0)
    nr = _TIER_RANK.get(new.strength_tier, 0)
    if nr > pr:
        events.append(LifecycleEvent(
            ts=now,
            event_type="tier_upgraded",
            detail=f"强度等级提升 {prev.strength_tier} → {new.strength_tier}",
            tier_before=prev.strength_tier,
            tier_after=new.strength_tier,
            score_before=prev.final_score,
            score_after=new.final_score,
            layer="scoring",
        ))
    elif nr < pr:
        events.append(LifecycleEvent(
            ts=now,
            event_type="tier_downgraded",
            detail=f"强度等级下降 {prev.strength_tier} → {new.strength_tier}",
            tier_before=prev.strength_tier,
            tier_after=new.strength_tier,
            score_before=prev.final_score,
            score_after=new.final_score,
            layer="scoring",
        ))

    # side 翻转检测（support↔resistance）
    if prev.side and new.side and prev.side != new.side:
        side_cn = {"support": "支撑", "resistance": "阻力"}
        events.append(LifecycleEvent(
            ts=now,
            event_type="flipped",
            detail=f"角色翻转 {side_cn.get(prev.side, prev.side)} → {side_cn.get(new.side, new.side)}",
            score_before=prev.final_score,
            score_after=new.final_score,
            tier_before=prev.strength_tier,
            tier_after=new.strength_tier,
            state_before=prev.state,
            state_after=new.state,
            layer="scoring",
        ))

    return events


def _trim_lifecycle_events(events: list[LifecycleEvent]) -> list[LifecycleEvent]:
    """保留最近 N 条事件，防止内存膨胀（V3-P2-9：永远保留首个 born）。

    设计：
      1. 事件数 ≤ N：原样返回
      2. 事件数 > N：取最近 N 条；若首个 `born` 不在尾部 N 条内，则
         保留首个 born + 最近 (N-1) 条，确保 lifecycle API 不丢失"出生事件"
         （这是 lifecycle 时间线的语义起点，丢失会让 UI 时间线无头）
    """
    if len(events) <= _LIFECYCLE_MAX_EVENTS:
        return events
    tail = events[-_LIFECYCLE_MAX_EVENTS:]
    first_born = next((e for e in events if e.event_type == "born"), None)
    if first_born is not None and first_born not in tail:
        return [first_born, *events[-(_LIFECYCLE_MAX_EVENTS - 1):]]
    return tail


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
    # M2 新增（全部 Optional/默认值，向后兼容）
    cvd_divergence: str = "",
    funding_rate: float = 0.0,
    oi_high_percentile: bool = False,
    # M3 新增（全部 Optional/默认值，向后兼容）
    regime_snapshot: Optional[RegimeSnapshot] = None,
) -> KeyLevelSnapshotV2:
    """主入口：从 DiscoveryResult 生成 KeyLevelSnapshotV2。

    M1 新参数：
      - freshness：DataFreshness（来自 key_level_freshness.compute_freshness(state)）
        若提供，对每个 level 应用 _apply_freshness_to_level 软衰减（不破坏 tier）。

    M2 新参数：
      - cvd_divergence: 当前 CVD 背离方向（"bullish"/"bearish"/""）
        用于 _calc_contradiction_penalty 第 3 类（CVD 反向矛盾扣分）
      - funding_rate: 当前 funding 费率（用于矛盾扣分 + 方向 modifier）
      - oi_high_percentile: OI 是否处于历史高百分位（局部加分）

    M3 新参数：
      - regime_snapshot: 当前市场 regime 快照（来自 state.regime_snapshot）
        若提供，对每个 level 按 evidence_groups 应用 regime_modifier，
        并在 snapshot 头部写入 regime / regime_confidence 上下文。
        regime_modifier ∈ [0.85, 1.10]，影响 final_score 进而影响 tier。
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
        _update_distance(lv, current_price)

    # M1: 应用数据血统软衰减（不改 tier，只衰减 final_score）
    #     必须先于 _calc_tier_v2，因 stale 软衰减影响分数 → tier
    if freshness is not None:
        from processors.key_level_freshness import apply_freshness_to_level
        for lv in scored_levels:
            apply_freshness_to_level(lv, freshness)

    # M2: 方向化 OI/Funding modifier（局部加/扣分，非全局）
    #     必须在 freshness 之后、tier 判定之前应用
    # V3-P2-1：modifier 触发时同步追加 explain_chips，让 UI/AI 能看到原因
    for lv in scored_levels:
        delta = apply_directional_modifier(
            lv, oi_high_percentile=oi_high_percentile, funding_extreme=funding_rate,
        )
        lv.final_score = round(max(0.0, min(100.0, lv.final_score + delta)), 1)
        # ── explain chip 透传（与 contradiction_reasons 互补）──
        # contradiction_reasons：处理"反向矛盾"（资金对立 → 扣分）
        # explain_chips：处理 OI 拥挤 + funding 顺势放大（加分场景）
        if oi_high_percentile and abs(lv.distance_pct) < 5:
            lv.explain_chips.append("📊 OI 拥挤近位")
        if funding_rate >= 0.0003 and lv.side == "resistance":
            lv.explain_chips.append("🔥 多头极拥挤·阻力顺势")
        elif funding_rate <= -0.0003 and lv.side == "support":
            lv.explain_chips.append("🔥 空头极拥挤·支撑顺势")

    # M2: 矛盾扣分（contradiction_penalty）— 6 类，仅扣分不重置 tier
    for lv in scored_levels:
        penalty, reasons = _calc_contradiction_penalty(
            lv, cvd_divergence=cvd_divergence, funding_rate=funding_rate,
        )
        lv.contradiction_penalty = penalty
        lv.contradiction_reasons = reasons
        if penalty > 0:
            lv.final_score = round(max(0.0, lv.final_score - penalty), 1)

    # M3 · R8: regime-aware modifier（contradiction 之后、tier 之前）
    # 极端 regime 下乘子 0.85 会主动降级 tier；趋势市强位 1.10 升级
    regime_label = ""
    if regime_snapshot is not None:
        regime_label = regime_snapshot.regime or ""
    if regime_label:
        for lv in scored_levels:
            mult = apply_regime_modifier(lv, regime_label)
            lv.regime_modifier_applied = round(mult, 3)
            lv.regime_at_score = regime_label
            lv.regime_weight_version = REGIME_WEIGHT_VERSION
            if mult != 1.0:
                lv.final_score = round(max(0.0, min(100.0, lv.final_score * mult)), 1)

    # 5. tier 判定（M2 双因子；旧 _calc_tier 已 deprecated，仅历史测试引用）
    for lv in scored_levels:
        lv.strength_tier = _calc_tier_v2(lv)
        # M2: S 级 4 分型
        lv.s_class = classify_s_level(lv)
        lv.category = _classify(lv)

        # M1: 算法化失效价（必须放在 tier 之后，因 mult 由 tier 决定）
        inv_price, inv_cond, inv_mult = _calc_invalidation(lv, atr)
        lv.invalidation_price = inv_price
        lv.invalidation_condition = inv_cond
        lv.invalidation_atr_mult = inv_mult

    # M3 · R9: level_id 赋值（基于 ATR/price 桶的稳定 hash）
    # 在 _merge_with_prev 阶段已继承 prev.level_id 的 level，保留其 ID 不变；
    # 未匹配 prev（新出现）的 level，此处生成新 ID
    for lv in scored_levels:
        if not lv.level_id:
            lv.level_id = make_level_id(lv.price, atr)

    # M3 · R9: lifecycle 事件 diff（与 prev_levels 按 level_id 比较）
    # 必须放最后：依赖 strength_tier / final_score / side 全部确定
    if prev_levels:
        prev_by_id: dict[str, KeyLevelV2] = {p.level_id: p for p in prev_levels if p.level_id}
    else:
        prev_by_id = {}
    now_int = int(now)
    for lv in scored_levels:
        prev_lv = prev_by_id.get(lv.level_id)
        new_events = _detect_lifecycle_events(prev_lv, lv, now_int)
        if prev_lv and prev_lv.lifecycle_events:
            lv.lifecycle_events = _trim_lifecycle_events(
                list(prev_lv.lifecycle_events) + new_events
            )
        else:
            # 无 prev：本轮第一次见 → born 事件已在 _detect_lifecycle_events 中产出
            lv.lifecycle_events = _trim_lifecycle_events(new_events)

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

    # M3: regime 上下文（snapshot 头部，便于前端 chip 渲染）
    snap_regime = ""
    snap_regime_conf = 0.0
    snap_regime_desc = ""
    snap_regime_weight_ver = ""
    if regime_snapshot is not None:
        snap_regime = regime_snapshot.regime or ""
        snap_regime_conf = float(regime_snapshot.confidence or 0.0)
        snap_regime_desc = regime_snapshot.description_cn or ""
        if snap_regime:
            snap_regime_weight_ver = REGIME_WEIGHT_VERSION

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
        # M3: regime 上下文
        regime=snap_regime,
        regime_confidence=round(snap_regime_conf, 3),
        regime_description=snap_regime_desc,
        regime_weight_version=snap_regime_weight_ver,
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
    # V3-P2-8：状态机 broken 判定使用 15m 已收盘 bar（_closed_bar_confirms_break），
    # 文案需对齐同一 timeframe，避免用户/AI 误用 1h 口径判定失效。
    if lv.side == "support":
        inv = lv.price - mult * atr
        cond = f"15m 收盘 < ${inv:,.0f}"
    else:
        inv = lv.price + mult * atr
        cond = f"15m 收盘 > ${inv:,.0f}"
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
    evidence_groups_set: set[str] = set()  # M2: 独立证据组
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
        # M2: 用 TTL exp 衰减替代旧 _time_decay 阶梯（按 source_tag 取 TTL）
        decayed_base = apply_ttl_decay(cand.base_score, cand.source_tag, cand.data_age_hours)
        dist_pct = abs(cl.price - price) / price * 100
        dist_bonus = _distance_bonus(dist_pct)
        score = decayed_base * dim_weight * dist_bonus
        total_score += score
        sources.append(cand.source)
        source_tags.add(cand.source_tag)
        dimensions.add(cand.dimension)

        # M2: 收集独立证据组（兜底用 resolve_evidence_group，但 discover 出口已 fill）
        if cand.evidence_group:
            evidence_groups_set.add(cand.evidence_group)

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

    # M2: 输出独立证据组（按预定义 8 组顺序排列，方便 UI 渲染）
    evidence_groups_ordered = _order_evidence_groups(evidence_groups_set)

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
        # M2 新增字段
        evidence_groups=evidence_groups_ordered,
        independent_group_count=len(evidence_groups_ordered),
    )


# M2: 8 组定义顺序（前端渲染稳定性）
_EVIDENCE_GROUP_ORDER = (
    "structure_anchor",
    "macro_technical",
    "local_technical",
    "liquidation_macro",
    "liquidation_meso",
    "liquidation_short",
    "microstructure_local",
    "flow_dynamic",
)


def _order_evidence_groups(groups: set[str]) -> list[str]:
    """将 set → list 按 8 组预定义顺序输出（保证 UI 渲染稳定）。"""
    return [g for g in _EVIDENCE_GROUP_ORDER if g in groups]


def count_independent_groups(lv: KeyLevelV2) -> int:
    """对外接口：返回该 level 的独立证据组数。

    使用场景：confluence_scoring 评级、AI 解释、单元测试
    """
    return len(set(lv.evidence_groups))


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
    """[DEPRECATED] 旧版单因子 tier 判定。

    生产路径已切换到 `_calc_tier_v2`（双因子：独立组数 + final_score）。
    保留此函数仅为兼容历史单测（test_key_levels_v2.py），新代码不应使用。
    """
    for tier, threshold in STRENGTH_THRESHOLDS.items():
        if score >= threshold:
            return tier
    return "C"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# M2 · 评级核心：独立组数 + final_score 双因子（GPT V3 评审采纳）
# 旧版只看 final_score，导致"单一来源高分"也能升 S（伪 S 信号）
# 新版：先看独立组数（≥3 才有资格 S），再看分数；解决"虚高 S"问题
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _calc_tier_v2(lv: KeyLevelV2) -> str:
    """M2 评级：独立组数（gating）+ final_score（gating）双因子。

    规则：
      - S：独立组数 ≥ 3 且 final_score ≥ 70  （任一不满足降 A）
      - A：独立组数 ≥ 2 且 final_score ≥ 45  （或 S gating 不够但分数 ≥ 70）
      - B：独立组数 ≥ 1 且 final_score ≥ 25
      - C：其他

    设计：
      - 独立组数 = 8 大证据组中命中的不同组数（同组多源仅算 1）
      - 这是 GPT 评审的核心建议：避免"5 个清算源 + 0 技术源"也升 S
      - 保留 final_score gating：低分位即使多组也只能 B（如 5 弱源）
    """
    n = lv.independent_group_count
    s = lv.final_score
    if n >= 3 and s >= 70:
        return "S"
    if (n >= 2 and s >= 45) or (n >= 1 and s >= 70):
        return "A"
    if n >= 1 and s >= 25:
        return "B"
    return "C"


def classify_s_level(lv: KeyLevelV2) -> str:
    """对 S 级位做 4 分型（仅 strength_tier == 'S' 时调用，否则返回 ""）。

    4 分型：
      - S-Macro:     长周期独占（macro_technical / liquidation_macro / structure_anchor 周月线）
      - S-Liquidity: 跨所共振强清算簇（exchange_count ≥ 4 且 liquidation_* 至少 2 组）
      - S-Micro:     盘口微结构 + flow 共振（microstructure_local + flow_dynamic）
      - S-Composite: 默认/兜底（≥3 独立组但不属上述任一类）

    设计：
      - 优先级 Liquidity > Macro > Micro > Composite（专业交易语言：先看流动性证据）
      - 仅返回类型字符串，不影响评分；UI/AI 用此做 badge / 文案分流
    """
    if lv.strength_tier != "S":
        return ""
    groups = set(lv.evidence_groups)

    # 1. S-Liquidity：≥4 所共振 + 至少 2 个清算组
    liq_groups = groups & {"liquidation_macro", "liquidation_meso", "liquidation_short"}
    if lv.exchange_count >= 4 and len(liq_groups) >= 2:
        return "S-Liquidity"

    # 2. S-Macro：宏观技术 / 长周期清算 + 至少 1 个旁证组（且无微结构）
    # V3-P2-5 修订：旧版 `if macro_groups and not has_micro` 门槛过松，
    # 单一 200W SMA 即可成 S-Macro。现要求 evidence_groups 至少 2 组（含 macro），
    # 与"S 必须 ≥3 独立组"整体哲学呼应（S-Macro 至少 1 macro + 1 旁证）。
    macro_groups = groups & {"macro_technical", "liquidation_macro"}
    has_micro = bool(groups & {"microstructure_local", "flow_dynamic"})
    if macro_groups and not has_micro and len(groups) >= 2:
        return "S-Macro"

    # 3. S-Micro：微结构 + flow 共振（无宏观证据 → 是短期挤压位）
    micro_groups = groups & {"microstructure_local", "flow_dynamic"}
    if len(micro_groups) >= 2 and not (groups & {"macro_technical", "liquidation_macro"}):
        return "S-Micro"

    # 4. 兜底：复合型
    return "S-Composite"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# M2 · 矛盾扣分（contradiction_penalty） × 6 类
# 设计：单源/单组/方向冲突等"软矛盾"在 final_score 末段扣分（不重置 tier）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# V3-P2-3：contradiction 总扣分上限（防止 6 类极端共触积累 ~50 分误杀真位）
# 25 分阈值 = 强 A 级或弱 S 级允许的最大降级幅度（A=70 → 45 落到 B；S=85 → 60 落到 A）
_MAX_CONTRADICTION_PENALTY = 25.0


def _calc_contradiction_penalty(
    lv: KeyLevelV2,
    cvd_divergence: str = "",   # "bullish" / "bearish" / ""（来自 CVD divergence detector）
    funding_rate: float = 0.0,  # 当前 funding_rate（>0.0001 视为偏多拥挤）
) -> tuple[float, list[str]]:
    """计算 6 类矛盾扣分（M2 · GPT V3 评审采纳；V3 验收 P1-4 / P2-3 修订）。

    6 类（每类最多扣 1 项，避免重复扣分）：
      1. 来源单一：仅 1 独立组 → -10
      2. 距离过远：abs(distance_pct) > 15 → -8
      3. CVD 反向：support 但 cvd 持续看跌 / resistance 但持续看涨 → -12
      4. Funding 反向拥挤：support 但 funding 极正（多头拥挤）/ resistance 但极负 → -10
      5. 数据 stale：is_stale=True → -2（V3 P1-4 减半：freshness 已 ×0.85 软衰减，
         此处仅做"轻量提示"避免与 freshness 双重重罚；保留 reason 让 UI/AI 仍能解释）
      6. 多周期清算冲突：仅有 liquidation_short 但无 meso/macro 支撑 → -6

    总扣分上限（V3 P2-3）：
      所有类别累加后不超过 _MAX_CONTRADICTION_PENALTY=25，避免极端组合（如 1+3+4+6
      合计 ~38）把一个本来 80 分的强位直接打到 40 分以下造成误杀。

    返回：(penalty_total, reasons)
    """
    penalty = 0.0
    reasons: list[str] = []

    # 1. 来源单一
    if lv.independent_group_count <= 1:
        penalty += 10
        reasons.append("单一证据组")

    # 2. 距离过远
    if abs(lv.distance_pct) > 15:
        penalty += 8
        reasons.append("距现价>15%")

    # 3. CVD 反向
    if cvd_divergence:
        if lv.side == "support" and cvd_divergence == "bearish":
            penalty += 12
            reasons.append("CVD 持续看跌")
        elif lv.side == "resistance" and cvd_divergence == "bullish":
            penalty += 12
            reasons.append("CVD 持续看涨")

    # 4. Funding 反向拥挤（funding 极值阈值：±0.0001 = 0.01%）
    if funding_rate >= 0.0003 and lv.side == "support":
        # 多头极拥挤 + 期望支撑反弹 → 反向风险
        penalty += 10
        reasons.append("多头资金极拥挤")
    elif funding_rate <= -0.0003 and lv.side == "resistance":
        penalty += 10
        reasons.append("空头资金极拥挤")

    # 5. 数据 stale（V3-P1-4：减半到 2 分，避免与 freshness ×0.85 双重重罚）
    if lv.is_stale:
        penalty += 2
        reasons.append("主源数据偏旧")

    # 6. 多周期清算冲突（仅短周期清算无中长周期支撑）
    groups = set(lv.evidence_groups)
    has_short_only = (
        "liquidation_short" in groups
        and "liquidation_meso" not in groups
        and "liquidation_macro" not in groups
        and len(groups) == 1
    )
    if has_short_only:
        penalty += 6
        reasons.append("仅 1d 清算（中长周期未确认）")

    # V3-P2-3：总扣分 cap
    penalty = min(penalty, _MAX_CONTRADICTION_PENALTY)
    return round(penalty, 1), reasons


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# M2 · 方向化 OI/Funding modifier（局部化，非全局）
# GPT 评审：原全局 OI 高百分位 +5 会影响所有 level；M2 改为按方向局部加分
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def apply_directional_modifier(
    lv: KeyLevelV2,
    oi_high_percentile: bool = False,   # OI 是否处于高百分位（来自 facts_collector）
    funding_extreme: float = 0.0,       # funding 极值方向：>0=多头极拥挤 / <0=空头极拥挤
) -> float:
    """对单个 level 的 final_score 应用 OI 近位拥挤加成 + Funding 方向化 modifier。

    设计（V3-P1-3 修订：OI 改为非方向化语义）：
      - OI 高百分位 + 近位（distance < 5%）：support / resistance 都 +3
        理由：OI 高百分位仅表示"持仓拥挤"，本身不含价格方向，故只做"近位风险加成"。
        若未来接入 OI slope/方向化数据，可在此基础上扩展真方向化判定（M4 候选）。
      - Funding 极值：真方向化（funding 极值含明确方向信息）
        - 多头极拥挤（>=0.0003）：阻力位 +4（顺势加速被吃），支撑位 -2（被吃概率高）
        - 空头极拥挤（<=-0.0003）：支撑位 +4，阻力位 -2
        - 注意：与 _calc_contradiction_penalty 第 4 类（"极拥挤位 + 反向"扣分）不冲突，
          那里软扣分，这里方向化加减分。

    返回：modifier delta（直接 += 到 final_score；可正可负）
    """
    delta = 0.0
    # OI 近位拥挤加成（非方向化）
    if oi_high_percentile and abs(lv.distance_pct) < 5:
        delta += 3.0

    # Funding 方向化
    if funding_extreme >= 0.0003:  # 多头极拥挤
        if lv.side == "resistance":
            delta += 4.0
        elif lv.side == "support":
            delta -= 2.0
    elif funding_extreme <= -0.0003:  # 空头极拥挤
        if lv.side == "support":
            delta += 4.0
        elif lv.side == "resistance":
            delta -= 2.0

    return round(delta, 1)


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
                # M3 · R9: 继承 level_id 保持稳定（生命周期连续性的关键）
                # lifecycle_events 不在此处继承——由主流程末尾的 _detect_lifecycle_events
                # 按 level_id 二次配对，避免双重写入
                if prev.level_id:
                    lv.level_id = prev.level_id
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
