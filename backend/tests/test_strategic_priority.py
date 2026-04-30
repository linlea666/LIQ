"""PR-2 · Strategic Priority 公式 + 排序回归测试。

覆盖 5 类对象（PriceZone / Opportunity / KeyLevel / Wall / LiqCluster）：
  1. proximity_score 衰减曲线（边界 + 中点）
  2. 各 priority_score 公式权重生效（关键字段单调性）
  3. 排序函数分组正确（above / below / current 边界）
  4. Top N 截断后 Top 1 是真正的"最相关"样本（端到端验证）
"""

from __future__ import annotations

import pytest


# ────────────────────────────────────────────────────────────────────────────
# 共享辅助
# ────────────────────────────────────────────────────────────────────────────

class TestProximityScore:
    def test_zero_distance_full(self):
        from ai.strategic_priority import proximity_score
        assert proximity_score(0.0) == pytest.approx(1.0)

    def test_max_distance_zero(self):
        from ai.strategic_priority import proximity_score
        assert proximity_score(5.0) == pytest.approx(0.0)
        assert proximity_score(10.0) == pytest.approx(0.0)

    def test_midpoint(self):
        from ai.strategic_priority import proximity_score
        assert proximity_score(2.5) == pytest.approx(0.5)

    def test_sign_irrelevant(self):
        from ai.strategic_priority import proximity_score
        assert proximity_score(-1.0) == proximity_score(1.0)


# ────────────────────────────────────────────────────────────────────────────
# 1. PriceZone
# ────────────────────────────────────────────────────────────────────────────

def _make_zone(
    *,
    zone_id: str = "z1",
    distance_pct: float = 0.0,
    dominant_role: str = "spot_defense",
    support_trust: float = 0.5,
    resistance_trust: float = 0.0,
    sweep_attractiveness: float = 0.0,
    data_confidence: float = 0.5,
    wall_zone_ids: list = (),
    key_level_prices: list = (),
    evidence: list = (),
):
    from models.trading_brain import BrainPriceZone

    return BrainPriceZone(
        zone_id=zone_id,
        coin="BTC",
        price_low=64900.0,
        price_high=65100.0,
        price_mid=65000.0,
        distance_pct=distance_pct,
        dominant_role=dominant_role,  # type: ignore[arg-type]
        support_trust=support_trust,
        resistance_trust=resistance_trust,
        sweep_attractiveness=sweep_attractiveness,
        data_confidence=data_confidence,
        wall_zone_ids=list(wall_zone_ids),
        key_level_prices=list(key_level_prices),
        evidence=list(evidence),
    )


class TestPriceZonePriority:
    def test_role_weight_effect(self):
        """role 决定 0.20 部分；spot_defense 必须高于 other 在其他字段相同时。"""
        from ai.strategic_priority import price_zone_priority

        z_spot = _make_zone(dominant_role="spot_defense")
        z_other = _make_zone(dominant_role="other")
        assert price_zone_priority(z_spot) > price_zone_priority(z_other)

    def test_proximity_dominates_strength(self):
        """近场弱 zone 必须排在远场强 zone 之前（proximity 权重最大 0.30）。"""
        from ai.strategic_priority import price_zone_priority

        near_weak = _make_zone(distance_pct=0.2, support_trust=0.3)
        far_strong = _make_zone(distance_pct=4.5, support_trust=1.0)
        assert price_zone_priority(near_weak) > price_zone_priority(far_strong)

    def test_clip_01(self):
        """所有字段拉满时不超过 1.0；全 0 时不小于 0.0。"""
        from ai.strategic_priority import price_zone_priority

        z_max = _make_zone(
            distance_pct=0.0,
            dominant_role="spot_defense",
            support_trust=1.0,
            data_confidence=1.0,
            wall_zone_ids=["w"] * 5,
            key_level_prices=[1.0] * 5,
            evidence=["e"] * 6,
        )
        assert 0.0 <= price_zone_priority(z_max) <= 1.0

        z_min = _make_zone(distance_pct=10.0, dominant_role="other",
                           support_trust=0.0, data_confidence=0.0)
        assert 0.0 <= price_zone_priority(z_min) <= 1.0


class TestSortPriceZones:
    def test_grouping_boundaries(self):
        """0.30 阈值是 current/above/below 的硬分界。"""
        from ai.strategic_priority import sort_price_zones

        zones = [
            _make_zone(zone_id="cur_pos", distance_pct=0.25),
            _make_zone(zone_id="cur_neg", distance_pct=-0.20),
            _make_zone(zone_id="above_edge", distance_pct=0.30),
            _make_zone(zone_id="below_edge", distance_pct=-0.30),
            _make_zone(zone_id="far_above", distance_pct=3.0),
            _make_zone(zone_id="far_below", distance_pct=-2.5),
        ]
        cur, above, below = sort_price_zones(zones)
        cur_ids = {z.zone_id for z in cur}
        above_ids = {z.zone_id for z in above}
        below_ids = {z.zone_id for z in below}
        assert cur_ids == {"cur_pos", "cur_neg"}
        assert above_ids == {"above_edge", "far_above"}
        assert below_ids == {"below_edge", "far_below"}

    def test_within_group_sorted_desc(self):
        """组内按 priority 降序，近场 spot_defense 排第一。"""
        from ai.strategic_priority import sort_price_zones

        zones = [
            _make_zone(zone_id="far_other", distance_pct=2.0, dominant_role="other"),
            _make_zone(zone_id="near_spot", distance_pct=0.4, dominant_role="spot_defense",
                       support_trust=0.9),
            _make_zone(zone_id="far_spot", distance_pct=4.0, dominant_role="spot_defense"),
        ]
        _, above, _ = sort_price_zones(zones)
        assert above[0].zone_id == "near_spot"


# ────────────────────────────────────────────────────────────────────────────
# 2. Opportunity
# ────────────────────────────────────────────────────────────────────────────

def _make_opp(
    *,
    setup_id: str = "s1",
    state_name: str = "forming",
    opportunity_score: float = 0.5,
    asymmetry_score: float = 0.5,
    data_confidence: float = 0.5,
    rrs: list = (),
    setup_type: str = "support_limit_probe",
    direction: str = "long",
):
    from models.trading_brain import (
        SetupRiskPlan, SetupState, SetupTarget, TradeSetupCandidate,
    )

    return TradeSetupCandidate(
        setup_id=setup_id,
        coin="BTC",
        zone_id="z1",
        setup_type=setup_type,  # type: ignore[arg-type]
        direction=direction,  # type: ignore[arg-type]
        risk_plan=SetupRiskPlan(soft_invalidation=64500, hard_stop=64200),
        targets=[SetupTarget(price=66000 + i, type="t", rr=r) for i, r in enumerate(rrs)],
        asymmetry_score=asymmetry_score,
        opportunity_score=opportunity_score,
        data_confidence=data_confidence,
        state=SetupState(name=state_name),  # type: ignore[arg-type]
    )


class TestOpportunityPriority:
    def test_state_weight_dominance(self):
        """同分情况下 confirmed > waiting > forming > missed。"""
        from ai.strategic_priority import opportunity_priority

        confirmed = _make_opp(state_name="confirmed", rrs=[2.0])
        waiting = _make_opp(state_name="waiting_for_trigger", rrs=[2.0])
        forming = _make_opp(state_name="forming", rrs=[2.0])
        missed = _make_opp(state_name="missed", rrs=[2.0])
        assert (
            opportunity_priority(confirmed)
            > opportunity_priority(waiting)
            > opportunity_priority(forming)
            > opportunity_priority(missed)
        )

    def test_rr_uses_max(self):
        """rr_score 取所有 targets 的 max rr，不是平均。"""
        from ai.strategic_priority import opportunity_priority

        opp_low = _make_opp(rrs=[1.0])
        opp_high = _make_opp(rrs=[1.0, 4.5])
        assert opportunity_priority(opp_high) > opportunity_priority(opp_low)

    def test_sort_desc(self):
        from ai.strategic_priority import sort_opportunities

        opps = [
            _make_opp(setup_id="weak", state_name="forming", rrs=[1.0]),
            _make_opp(setup_id="strong", state_name="confirmed",
                      opportunity_score=0.9, asymmetry_score=0.9, rrs=[3.5]),
        ]
        sorted_opps = sort_opportunities(opps)
        assert sorted_opps[0].setup_id == "strong"


# ────────────────────────────────────────────────────────────────────────────
# 3. KeyLevel V3
# ────────────────────────────────────────────────────────────────────────────

def _make_kl(
    *,
    price: float = 65000.0,
    timeframe: str = "4h",
    s_class: str = "B",
    final_score: float = 60.0,
    evidence_groups: list = (),
    lifecycle_events: list = (),
):
    """构造最小 KeyLevelV2（仅填本测试关心字段；其余 Optional）。"""
    from models.key_level import KeyLevelV2

    return KeyLevelV2(
        level_id=f"kl_{price}",
        price=price,
        timeframe=timeframe,  # type: ignore[arg-type]
        side="support",
        s_class=s_class,  # type: ignore[arg-type]
        final_score=final_score,
        evidence_groups=list(evidence_groups),  # type: ignore[arg-type]
        lifecycle_events=list(lifecycle_events),  # type: ignore[arg-type]
    )


class TestKeyLevelPriority:
    def test_proximity_largest_weight(self):
        """近场 4h S 级 vs 远端 1w S 级 → 近场胜（distance 权重 0.35 最大）。"""
        from ai.strategic_priority import key_level_priority

        near = _make_kl(price=65100, timeframe="4h", s_class="S")
        far = _make_kl(price=80000, timeframe="1w", s_class="S")
        assert key_level_priority(near, 65000) > key_level_priority(far, 65000)

    def test_strength_tier_monotonic(self):
        """同距离同时间框 → s_class 越高分越高。"""
        from ai.strategic_priority import key_level_priority

        s_lvl = _make_kl(price=65300, s_class="S")
        a_lvl = _make_kl(price=65300, s_class="A")
        c_lvl = _make_kl(price=65300, s_class="C")
        assert (
            key_level_priority(s_lvl, 65000)
            > key_level_priority(a_lvl, 65000)
            > key_level_priority(c_lvl, 65000)
        )

    def test_sort_grouping_at_1pct(self):
        """sort_key_levels 用 1% 阈值分 near / above / below。"""
        from ai.strategic_priority import sort_key_levels

        kls = [
            _make_kl(price=65500),     # +0.77% → near
            _make_kl(price=66000),     # +1.54% → above
            _make_kl(price=64500),     # -0.77% → near
            _make_kl(price=63000),     # -3.08% → below
        ]
        near, above, below = sort_key_levels(kls, 65000)
        assert {kl.price for kl in near} == {65500, 64500}
        assert {kl.price for kl in above} == {66000}
        assert {kl.price for kl in below} == {63000}


# ────────────────────────────────────────────────────────────────────────────
# 4. WallZone
# ────────────────────────────────────────────────────────────────────────────

def _make_wall(
    *,
    zone_id: str = "w1",
    side: str = "ask",
    price_mid: float = 66000.0,
    distance_pct: float = 1.5,
    current_usd: float = 5_000_000.0,
    trust_score: float = 0.7,
    visible_minutes: float = 30.0,
    coinbase_spot_confluence: bool = False,
):
    from models.orderbook_pressure import WallZone

    return WallZone(
        zone_id=zone_id,
        side=side,  # type: ignore[arg-type]
        price_low=price_mid - 50,
        price_high=price_mid + 50,
        price_mid=price_mid,
        peak_price=price_mid,
        distance_pct=distance_pct,
        current_usd=current_usd,
        max_usd_1h=current_usd * 1.2,
        avg_usd_1h=current_usd,
        bin_count=3,
        seen_count=10,
        persistence_score=0.6,
        trust_score=trust_score,
        visible_minutes=visible_minutes,
        coinbase_spot_confluence=coinbase_spot_confluence,
    )


class TestWallPriority:
    def test_trust_monotonic(self):
        """trust_score 单调递增（其余字段固定）。"""
        from ai.strategic_priority import wall_priority

        low = _make_wall(trust_score=0.3)
        high = _make_wall(trust_score=0.95)
        assert wall_priority(high) > wall_priority(low)

    def test_size_uses_log(self):
        """1M / 10M / 100M 大小差异通过 log 反映（不会因尺度悬殊压缩）。"""
        from ai.strategic_priority import wall_priority

        w_1m = _make_wall(current_usd=1_000_000)
        w_10m = _make_wall(current_usd=10_000_000)
        w_100m = _make_wall(current_usd=100_000_000)
        assert wall_priority(w_10m) > wall_priority(w_1m)
        assert wall_priority(w_100m) > wall_priority(w_10m)

    def test_coinbase_bonus(self):
        """Coinbase 共振墙在其他字段相同时优先。"""
        from ai.strategic_priority import wall_priority

        plain = _make_wall(coinbase_spot_confluence=False)
        cb = _make_wall(coinbase_spot_confluence=True)
        assert wall_priority(cb) > wall_priority(plain)


# ────────────────────────────────────────────────────────────────────────────
# 5. LiqCluster
# ────────────────────────────────────────────────────────────────────────────

def _make_liq(
    *,
    price_center: float = 66000.0,
    distance_pct: float = 1.5,
    total_usd: float = 1_000_000.0,
    side: str = "short",
    exchange_count: int = 2,
    leverage_intensity: float = 0.5,
):
    from models.liquidation import LiqCluster

    return LiqCluster(
        price_center=price_center,
        price_from=price_center - 100,
        price_to=price_center + 100,
        total_usd=total_usd,
        side=side,
        distance_pct=distance_pct,
        exchange_count=exchange_count,
        leverage_intensity=leverage_intensity,
    )


class TestLiqClusterPriority:
    def test_amount_log_scale(self):
        """100k / 1M / 10M / 100M 单调递增（log 曲线 clamp [0,1]）。"""
        from ai.strategic_priority import liq_cluster_priority

        scores = [
            liq_cluster_priority(_make_liq(total_usd=100_000)),
            liq_cluster_priority(_make_liq(total_usd=1_000_000)),
            liq_cluster_priority(_make_liq(total_usd=10_000_000)),
            liq_cluster_priority(_make_liq(total_usd=100_000_000)),
        ]
        assert scores == sorted(scores)  # 升序

    def test_timeframe_weight_effect(self):
        """同样的 cluster 在 1d / 7d / 30d 评分递减。"""
        from ai.strategic_priority import LIQ_TIMEFRAME_WEIGHT, liq_cluster_priority

        c = _make_liq()
        s_1d = liq_cluster_priority(c, timeframe_weight=LIQ_TIMEFRAME_WEIGHT["1d"])
        s_7d = liq_cluster_priority(c, timeframe_weight=LIQ_TIMEFRAME_WEIGHT["7d"])
        s_30d = liq_cluster_priority(c, timeframe_weight=LIQ_TIMEFRAME_WEIGHT["30d"])
        assert s_1d > s_7d > s_30d

    def test_sort_clusters(self):
        """sort_liq_clusters 按 priority 降序，近大簇排第一。"""
        from ai.strategic_priority import sort_liq_clusters

        clusters = [
            _make_liq(price_center=70000, distance_pct=8.0, total_usd=500_000),
            _make_liq(price_center=66000, distance_pct=1.5, total_usd=10_000_000),
            _make_liq(price_center=68000, distance_pct=4.0, total_usd=2_000_000),
        ]
        sorted_clusters = sort_liq_clusters(clusters, "1d")
        # 距离最近 + 金额最大 → 第一
        assert sorted_clusters[0].price_center == 66000
