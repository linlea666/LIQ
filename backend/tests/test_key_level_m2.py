"""
Key Level V3 · M2（评分体系核心升级）单元测试

覆盖点：
1. RawCandidate.evidence_group 字段 + resolve_evidence_group 映射
2. _score_cluster 输出 evidence_groups + independent_group_count
3. _calc_tier_v2：独立组数 + final_score 双因子评级
4. classify_s_level：S-Macro / S-Liquidity / S-Micro / S-Composite 4 分型
5. _calc_contradiction_penalty：6 类矛盾扣分
6. apply_directional_modifier：OI/Funding 方向化加分
7. apply_ttl_decay：TTL 指数衰减
8. CascadeComponents 4 子分输出
"""
from __future__ import annotations

import time

import pytest

from models.key_level import CascadeComponents, KeyLevelV2
from models.liquidation import LiqCluster, LiquidationMap
from processors.confluence_scoring import (
    SOURCE_TTL_HOURS,
    _calc_contradiction_penalty,
    _calc_tier_v2,
    _Cluster,
    _resolve_ttl_hours,
    _score_cluster,
    apply_directional_modifier,
    apply_ttl_decay,
    classify_s_level,
    count_independent_groups,
)
from processors.level_discovery import (
    SOURCE_TAG_TO_EVIDENCE_GROUP,
    RawCandidate,
    resolve_evidence_group,
)


# ─────────────────────────────────────────────────────────────────
# 1. evidence_group 映射（R1）
# ─────────────────────────────────────────────────────────────────

class TestEvidenceGroupMapping:
    def test_explicit_mappings(self):
        """显式 source_tag 映射到正确的 8 组之一。"""
        assert SOURCE_TAG_TO_EVIDENCE_GROUP["sma_200_1d"] == "macro_technical"
        assert SOURCE_TAG_TO_EVIDENCE_GROUP["liq_cluster_below_30d"] == "liquidation_macro"
        assert SOURCE_TAG_TO_EVIDENCE_GROUP["liq_cluster_above_7d"] == "liquidation_meso"
        assert SOURCE_TAG_TO_EVIDENCE_GROUP["liq_cluster_below_1d"] == "liquidation_short"
        assert SOURCE_TAG_TO_EVIDENCE_GROUP["footprint_stacked"] == "microstructure_local"
        assert SOURCE_TAG_TO_EVIDENCE_GROUP["oi_surge_zone"] == "flow_dynamic"
        assert SOURCE_TAG_TO_EVIDENCE_GROUP["round_major"] == "structure_anchor"
        assert SOURCE_TAG_TO_EVIDENCE_GROUP["vwap"] == "local_technical"

    def test_swing_wildcard(self):
        """swing_*_<tf> 通配兜底到 structure_anchor。"""
        assert resolve_evidence_group("swing_high_1w") == "structure_anchor"
        assert resolve_evidence_group("swing_low_4h") == "structure_anchor"
        assert resolve_evidence_group("swing_unknown_xx") == "structure_anchor"

    def test_liq_cluster_wildcard(self):
        """liq_cluster_<...>_<period> 兜底按周期分组。"""
        assert resolve_evidence_group("liq_cluster_xxx_30d") == "liquidation_macro"
        assert resolve_evidence_group("liq_cluster_xxx_7d") == "liquidation_meso"
        assert resolve_evidence_group("liq_cluster_xxx_1d") == "liquidation_short"

    def test_unknown_fallback(self):
        """未映射 tag 兜底到 local_technical（不返回空字符串）。"""
        assert resolve_evidence_group("totally_unknown_tag") == "local_technical"

    def test_ema_special(self):
        """ema_200_* 走 macro，其他 ema_* 走 local。"""
        assert resolve_evidence_group("ema_200_1d") == "macro_technical"
        assert resolve_evidence_group("ema_50_1d") == "local_technical"


# ─────────────────────────────────────────────────────────────────
# 2. _score_cluster 输出 evidence_groups（R1+R2）
# ─────────────────────────────────────────────────────────────────

def _make_candidate(
    price: float,
    side: str,
    source_tag: str,
    base_score: float = 30,
    dimension: str = "math_indicator",
) -> RawCandidate:
    c = RawCandidate(
        price=price, side=side, dimension=dimension,
        source=f"src_{source_tag}", source_tag=source_tag,
        base_score=base_score, timeframe="1D",
    )
    c.evidence_group = resolve_evidence_group(source_tag)
    return c


class TestScoreClusterEvidenceGroups:
    def test_single_group_count_1(self):
        """单一来源 → independent_group_count == 1。"""
        cl = _Cluster(price_sum=100, count=1, side="support")
        cl.candidates = [_make_candidate(100, "support", "vwap")]
        lv = _score_cluster(cl, price=100, atr=2, now=int(time.time()))
        assert lv.independent_group_count == 1
        assert lv.evidence_groups == ["local_technical"]

    def test_multi_group_count(self):
        """多组来源 → 去重后正确组数。"""
        cl = _Cluster(price_sum=300, count=3, side="support")
        cl.candidates = [
            _make_candidate(100, "support", "vwap"),                # local_technical
            _make_candidate(100, "support", "liq_cluster_below_7d"),  # liquidation_meso
            _make_candidate(100, "support", "footprint_stacked"),     # microstructure_local
        ]
        lv = _score_cluster(cl, price=100, atr=2, now=int(time.time()))
        assert lv.independent_group_count == 3
        assert "local_technical" in lv.evidence_groups
        assert "liquidation_meso" in lv.evidence_groups
        assert "microstructure_local" in lv.evidence_groups

    def test_same_group_dedup(self):
        """同组多源仅计 1（GPT 评审核心：避免"5 个清算源"伪 S）。"""
        cl = _Cluster(price_sum=200, count=2, side="support")
        cl.candidates = [
            _make_candidate(100, "support", "vwap"),       # local_technical
            _make_candidate(100, "support", "fib_0.618"),  # local_technical（同组）
        ]
        lv = _score_cluster(cl, price=100, atr=2, now=int(time.time()))
        assert lv.independent_group_count == 1


# ─────────────────────────────────────────────────────────────────
# 3. _calc_tier_v2 评级（R2）
# ─────────────────────────────────────────────────────────────────

class TestCalcTierV2:
    def _make_lv(self, group_count: int, score: float) -> KeyLevelV2:
        return KeyLevelV2(
            price=100, side="support", final_score=score,
            independent_group_count=group_count,
            evidence_groups=[f"group_{i}" for i in range(group_count)],
        )

    def test_s_requires_3_groups_and_70(self):
        assert _calc_tier_v2(self._make_lv(3, 70)) == "S"
        assert _calc_tier_v2(self._make_lv(3, 90)) == "S"

    def test_s_demoted_when_groups_lt_3(self):
        """高分但单一组数 → 降级 A（解决"5 弱源也升 S"问题）。"""
        # 2 组 + 70 分 → A（不能升 S，因 group_count<3）
        lv = self._make_lv(2, 70)
        assert _calc_tier_v2(lv) == "A"
        # 1 组 + 80 分 → A（special-case：高分单组也算 A）
        lv = self._make_lv(1, 80)
        assert _calc_tier_v2(lv) == "A"

    def test_s_demoted_when_score_lt_70(self):
        """组数够但分数不够 → 降级 A。"""
        lv = self._make_lv(3, 65)
        assert _calc_tier_v2(lv) == "A"

    def test_a_normal(self):
        assert _calc_tier_v2(self._make_lv(2, 50)) == "A"
        assert _calc_tier_v2(self._make_lv(2, 45)) == "A"

    def test_b(self):
        assert _calc_tier_v2(self._make_lv(1, 30)) == "B"
        assert _calc_tier_v2(self._make_lv(2, 25)) == "B"

    def test_c(self):
        assert _calc_tier_v2(self._make_lv(0, 50)) == "C"  # 0 组直接 C
        assert _calc_tier_v2(self._make_lv(1, 20)) == "C"  # 分数不够


# ─────────────────────────────────────────────────────────────────
# 4. classify_s_level 4 分型（R3）
# ─────────────────────────────────────────────────────────────────

class TestClassifySLevel:
    def test_non_s_returns_empty(self):
        lv = KeyLevelV2(price=100, side="support", strength_tier="A")
        assert classify_s_level(lv) == ""

    def test_s_liquidity(self):
        """≥4 所共振 + 至少 2 个清算组 → S-Liquidity。"""
        lv = KeyLevelV2(
            price=100, side="support", strength_tier="S",
            exchange_count=4,
            evidence_groups=["liquidation_meso", "liquidation_macro"],
        )
        assert classify_s_level(lv) == "S-Liquidity"

    def test_s_macro(self):
        """宏观技术 + 无微结构 → S-Macro。"""
        lv = KeyLevelV2(
            price=100, side="support", strength_tier="S",
            exchange_count=1,
            evidence_groups=["macro_technical", "structure_anchor"],
        )
        assert classify_s_level(lv) == "S-Macro"

    def test_s_micro(self):
        """微结构 + flow 共振 + 无宏观 → S-Micro。"""
        lv = KeyLevelV2(
            price=100, side="support", strength_tier="S",
            exchange_count=1,
            evidence_groups=["microstructure_local", "flow_dynamic"],
        )
        assert classify_s_level(lv) == "S-Micro"

    def test_s_composite_fallback(self):
        """既非纯宏观也非纯微观 → S-Composite。"""
        lv = KeyLevelV2(
            price=100, side="support", strength_tier="S",
            exchange_count=2,  # 不达 S-Liquidity 4 所标准
            evidence_groups=["local_technical", "liquidation_short", "structure_anchor"],
        )
        # 没有 macro_technical → 不是 S-Macro
        # 没有 microstructure_local + flow_dynamic 双中 → 不是 S-Micro
        # → S-Composite
        assert classify_s_level(lv) == "S-Composite"

    def test_s_macro_demoted_if_has_micro(self):
        """有宏观但也有微结构 → 不是 S-Macro，降为 S-Composite。"""
        lv = KeyLevelV2(
            price=100, side="support", strength_tier="S",
            exchange_count=1,
            evidence_groups=["macro_technical", "microstructure_local"],
        )
        # macro_technical 存在但 has_micro=True → 跳过 S-Macro
        # micro_groups 只有 1 个 → 不是 S-Micro
        # → S-Composite
        assert classify_s_level(lv) == "S-Composite"


# ─────────────────────────────────────────────────────────────────
# 5. 矛盾扣分（R4）
# ─────────────────────────────────────────────────────────────────

class TestContradictionPenalty:
    def test_no_penalty_for_healthy_level(self):
        lv = KeyLevelV2(
            price=100, side="support", distance_pct=2,
            independent_group_count=3,
            evidence_groups=["local_technical", "liquidation_meso", "structure_anchor"],
        )
        penalty, reasons = _calc_contradiction_penalty(lv)
        assert penalty == 0
        assert reasons == []

    def test_single_group_penalty(self):
        lv = KeyLevelV2(
            price=100, side="support", distance_pct=2,
            independent_group_count=1, evidence_groups=["local_technical"],
        )
        penalty, reasons = _calc_contradiction_penalty(lv)
        assert penalty >= 10
        assert "单一证据组" in reasons

    def test_distance_too_far(self):
        lv = KeyLevelV2(
            price=100, side="support", distance_pct=20,
            independent_group_count=2,
            evidence_groups=["local_technical", "liquidation_meso"],
        )
        penalty, reasons = _calc_contradiction_penalty(lv)
        assert penalty >= 8
        assert "距现价>15%" in reasons

    def test_cvd_bearish_against_support(self):
        lv = KeyLevelV2(
            price=100, side="support", distance_pct=2,
            independent_group_count=2,
            evidence_groups=["local_technical", "liquidation_meso"],
        )
        penalty, reasons = _calc_contradiction_penalty(lv, cvd_divergence="bearish")
        assert penalty >= 12
        assert "CVD 持续看跌" in reasons

    def test_funding_extreme_against_support(self):
        lv = KeyLevelV2(
            price=100, side="support", distance_pct=2,
            independent_group_count=2,
            evidence_groups=["local_technical", "liquidation_meso"],
        )
        penalty, reasons = _calc_contradiction_penalty(lv, funding_rate=0.0005)
        assert penalty >= 10
        assert "多头资金极拥挤" in reasons

    def test_stale_data(self):
        """V3-P1-4：stale penalty 减半到 -2（freshness 已 ×0.85 软衰减）。"""
        lv = KeyLevelV2(
            price=100, side="support", distance_pct=2,
            is_stale=True,
            independent_group_count=2,
            evidence_groups=["local_technical", "liquidation_meso"],
        )
        penalty, reasons = _calc_contradiction_penalty(lv)
        assert penalty == 2
        assert "主源数据偏旧" in reasons

    def test_total_penalty_capped(self):
        """V3-P2-3：6 类极端共触不超过 25 分上限。"""
        # 同时触发 1（单组）+3（CVD 反向）+4（funding 极拥挤）+5（stale）+6（仅 1d 清算）
        # 原始累加：10+12+10+2+6 = 40 → cap 后 25
        lv = KeyLevelV2(
            price=100, side="support", distance_pct=2,
            is_stale=True,
            independent_group_count=1,
            evidence_groups=["liquidation_short"],
        )
        penalty, reasons = _calc_contradiction_penalty(
            lv, cvd_divergence="bearish", funding_rate=0.0005,
        )
        assert penalty == 25  # cap 上限
        assert len(reasons) >= 5

    def test_short_only_liquidation_conflict(self):
        """仅 1d 清算无中长周期支撑 → 扣 6 分。"""
        lv = KeyLevelV2(
            price=100, side="support", distance_pct=2,
            independent_group_count=1,
            evidence_groups=["liquidation_short"],
        )
        penalty, reasons = _calc_contradiction_penalty(lv)
        # 单一证据组 +10 + 仅短周期清算 +6
        assert penalty >= 16
        assert "仅 1d 清算（中长周期未确认）" in reasons


# ─────────────────────────────────────────────────────────────────
# 6. apply_directional_modifier（R7）
# ─────────────────────────────────────────────────────────────────

class TestDirectionalModifier:
    def test_no_modifier_far_level(self):
        """远位（>5%）不受 OI 影响。"""
        lv = KeyLevelV2(price=100, side="support", distance_pct=10)
        delta = apply_directional_modifier(lv, oi_high_percentile=True)
        assert delta == 0

    def test_oi_high_near_support(self):
        lv = KeyLevelV2(price=100, side="support", distance_pct=3)
        delta = apply_directional_modifier(lv, oi_high_percentile=True)
        assert delta == 3.0

    def test_funding_long_crowded_resistance_boost(self):
        """多头极拥挤 → 阻力位 +4。"""
        lv = KeyLevelV2(price=100, side="resistance", distance_pct=3)
        delta = apply_directional_modifier(lv, funding_extreme=0.0005)
        assert delta == 4.0

    def test_funding_long_crowded_support_penalty(self):
        """多头极拥挤 → 支撑位 -2。"""
        lv = KeyLevelV2(price=100, side="support", distance_pct=3)
        delta = apply_directional_modifier(lv, funding_extreme=0.0005)
        assert delta == -2.0

    def test_funding_short_crowded_support_boost(self):
        lv = KeyLevelV2(price=100, side="support", distance_pct=3)
        delta = apply_directional_modifier(lv, funding_extreme=-0.0005)
        assert delta == 4.0

    def test_combined_oi_and_funding(self):
        """OI 高 + funding 极正 + 阻力位 → 同时加 3 + 4 = 7。"""
        lv = KeyLevelV2(price=100, side="resistance", distance_pct=3)
        delta = apply_directional_modifier(lv, oi_high_percentile=True, funding_extreme=0.0005)
        assert delta == 7.0


# ─────────────────────────────────────────────────────────────────
# 7. apply_ttl_decay（R6）
# ─────────────────────────────────────────────────────────────────

class TestTTLDecay:
    def test_zero_age_no_decay(self):
        assert apply_ttl_decay(100, "vwap", 0) == 100

    def test_age_eq_ttl_decay_to_037(self):
        """age = ttl → exp(-1) ≈ 0.368"""
        ttl = SOURCE_TTL_HOURS["vwap"]  # 24
        decayed = apply_ttl_decay(100, "vwap", ttl)
        assert 36 < decayed < 38

    def test_macro_source_minimal_decay(self):
        """sma_200_1d TTL=8760h（1 年），1 周龄几乎不衰减。"""
        decayed = apply_ttl_decay(100, "sma_200_1d", 168)  # 1 周
        assert decayed > 98  # 几乎不衰减

    def test_micro_source_steep_decay(self):
        """footprint_stacked TTL=2h，4 小时龄严重衰减。"""
        decayed = apply_ttl_decay(100, "footprint_stacked", 4)
        assert decayed < 20  # exp(-2) ≈ 0.135 → 13.5

    def test_unknown_tag_default_ttl(self):
        """未匹配 tag 用 DEFAULT_TTL_HOURS=24。"""
        ttl = _resolve_ttl_hours("totally_unknown")
        assert ttl == 24

    def test_swing_tf_specific_ttl(self):
        """swing_*_<tf> 按 timeframe 取 TTL。"""
        assert _resolve_ttl_hours("swing_high_1w") == 720
        assert _resolve_ttl_hours("swing_low_1d") == 168
        assert _resolve_ttl_hours("swing_high_4h") == 48


# ─────────────────────────────────────────────────────────────────
# 8. CascadeComponents（R5）
# ─────────────────────────────────────────────────────────────────

class TestCascadeComponents:
    def test_default_zero(self):
        cc = CascadeComponents()
        assert cc.count_score == 0
        assert cc.usd_score == 0
        assert cc.velocity_score == 0
        assert cc.leverage_score == 0

    def test_custom_values(self):
        cc = CascadeComponents(
            count_score=0.6, usd_score=0.4,
            velocity_score=0.8, leverage_score=0.7,
        )
        assert cc.count_score == 0.6
        assert cc.velocity_score == 0.8


# ─────────────────────────────────────────────────────────────────
# 8b. cascade_risk 与 4 子分一致性（V3 P1-1 修复）
# ─────────────────────────────────────────────────────────────────

class TestCascadeRiskAggregation:
    """验证 cascade_risk 由 4 子分加权聚合得出（P1-1 真聚合修复）。

    权重：usd 0.35 / velocity 0.30 / leverage 0.20 / count 0.15
    距离衰减：dist_decay = 1 / (1 + dist_pct / 3)
    """

    def _run_cascade(self, clusters_below, lv_price=100.0, current_price=100.0):
        from processors.key_level_tracker_v2 import _calc_cascade_risk
        lv = KeyLevelV2(price=lv_price, side="support")
        liq_map = LiquidationMap(
            coin="BTC",
            ts=int(time.time()),
            cycle="1d",
            current_price=current_price,
            leverage_groups=[],
            clusters_below=clusters_below,
            clusters_above=[],
        )
        _calc_cascade_risk(lv, liq_map, current_price, cfg={})
        return lv

    def test_no_clusters_zero_risk(self):
        lv = self._run_cascade([])
        assert lv.cascade_risk == 0
        assert lv.cascade_components.count_score == 0

    def test_full_score_clusters(self):
        # 5 个紧凑 + 高 USD + 高杠杆的簇 → 4 子分都接近 1
        clusters = [
            LiqCluster(
                price_from=99.5 - i * 0.1,
                price_to=99.6 - i * 0.1,
                price_center=99.55 - i * 0.1,
                total_usd=80_000_000,  # 累计 ≥ 200M → usd_score=1
                side="long",
                leverage_intensity=0.8,  # >0.7 → leverage_score=1
            )
            for i in range(5)
        ]
        lv = self._run_cascade(clusters, lv_price=100.0, current_price=100.0)
        cc = lv.cascade_components
        assert cc.count_score == 1.0
        assert cc.usd_score == 1.0
        assert cc.leverage_score == 1.0
        # vacuum_gap_pct = (100 - 99.55)/100*100 = 0.45 ≤ 0.5 → velocity=1
        assert cc.velocity_score == 1.0
        # dist_decay = 1/(1+0/3) = 1，weighted=1 → cascade_risk=1.0
        assert lv.cascade_risk == 1.0

    def test_aggregation_formula(self):
        # 单簇受控：count=1/5=0.2，USD=100M→usd=0.5，leverage=0.35→0.5
        clusters = [
            LiqCluster(
                price_from=98.5, price_to=99.0, price_center=98.75,
                total_usd=100_000_000,
                side="long",
                leverage_intensity=0.35,
            )
        ]
        lv = self._run_cascade(clusters, lv_price=100.0, current_price=100.0)
        cc = lv.cascade_components
        assert cc.count_score == 0.2
        assert cc.usd_score == 0.5
        assert cc.leverage_score == 0.5
        # vacuum_gap_pct = 1.25%（0.5< 1.25 < 5），velocity 线性插值
        # = 1 - (1.25-0.5)/4.5*0.8 ≈ 0.867
        assert 0.85 < cc.velocity_score < 0.88
        # weighted = 0.35*0.5 + 0.30*0.867 + 0.20*0.5 + 0.15*0.2 ≈ 0.565
        # dist_decay = 1（lv 在当前价）
        assert 0.55 < lv.cascade_risk < 0.58

    def test_distance_decay_applied(self):
        clusters = [
            LiqCluster(
                price_from=88.5, price_to=89.0, price_center=88.75,
                total_usd=200_000_000, side="long", leverage_intensity=0.8,
            )
        ]
        # lv 距当前价 10%，dist_decay = 1/(1+10/3) ≈ 0.231
        lv = self._run_cascade(clusters, lv_price=90.0, current_price=100.0)
        # weighted ≈ 0.35*1 + 0.30*0.2 + 0.20*1 + 0.15*0.2 = 0.64
        # （vacuum_gap_pct = (90-88.75)/100*100 = 1.25% → velocity≈0.867)
        # 但 dist_decay 仅作用于聚合后总分
        # 这里仅断言 cascade_risk 显著低于 1.0（验证 dist_decay 生效）
        assert lv.cascade_risk < 0.3

    def test_fallback_to_7d_when_1d_empty(self):
        """V3-P2-10：1d liq_map 无簇时，回退到 7d 计算 magnet。"""
        from processors.key_level_tracker_v2 import _calc_cascade_risk
        lv = KeyLevelV2(price=100.0, side="support")
        # 1d 没有簇
        liq_map_1d = LiquidationMap(
            coin="BTC", ts=int(time.time()), cycle="1d",
            current_price=100.0, leverage_groups=[],
            clusters_below=[], clusters_above=[],
        )
        # 7d 有簇
        liq_map_7d = LiquidationMap(
            coin="BTC", ts=int(time.time()), cycle="7d",
            current_price=100.0, leverage_groups=[],
            clusters_below=[
                LiqCluster(
                    price_from=98.0, price_to=98.5, price_center=98.25,
                    total_usd=50_000_000, side="long", leverage_intensity=0.5,
                ),
            ],
            clusters_above=[],
        )
        _calc_cascade_risk(lv, liq_map_1d, 100.0, cfg={}, liq_map_7d=liq_map_7d)
        assert lv.next_magnet_price == 98.25
        assert lv.cascade_layers == 1
        assert lv.cascade_total_usd == 50_000_000


# ─────────────────────────────────────────────────────────────────
# 4b. S-Macro 门槛收紧（V3 P2-5）
# ─────────────────────────────────────────────────────────────────

class TestSMacroTightened:
    """单一 macro 来源不再能成 S-Macro，必须有至少 1 个旁证组。"""

    def test_single_macro_demoted(self):
        lv = KeyLevelV2(
            price=100, side="support", strength_tier="S",
            exchange_count=1,
            evidence_groups=["macro_technical"],  # 仅 1 组
        )
        assert classify_s_level(lv) != "S-Macro"
        assert classify_s_level(lv) == "S-Composite"

    def test_macro_with_supporting_group_pass(self):
        # macro + structure_anchor 旁证 → 仍然 S-Macro
        lv = KeyLevelV2(
            price=100, side="support", strength_tier="S",
            exchange_count=1,
            evidence_groups=["macro_technical", "structure_anchor"],
        )
        assert classify_s_level(lv) == "S-Macro"


# ─────────────────────────────────────────────────────────────────
# 9. count_independent_groups 工具函数
# ─────────────────────────────────────────────────────────────────

class TestCountIndependentGroups:
    def test_empty(self):
        lv = KeyLevelV2(price=100, side="support", evidence_groups=[])
        assert count_independent_groups(lv) == 0

    def test_dedup(self):
        lv = KeyLevelV2(
            price=100, side="support",
            evidence_groups=["a", "a", "b"],  # 应去重
        )
        assert count_independent_groups(lv) == 2
