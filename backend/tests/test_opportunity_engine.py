"""OpportunityEngine 单元测试。

覆盖：
1. support_limit_probe / resistance_limit_probe 生成
2. fake_break_reclaim 生成（前置态 forming + direction=neutral）
3. RR 计算正确（targets 来自其它 zones）
4. 软失效 + 硬止损（按 ATR）
5. 双方案 aggressive + conservative
6. cancel_conditions 完整 + stale 时插入降权
7. 严筛门槛（trust 不足、距离过远、data_confidence 不足、regime 冲突）
8. opportunity_score 排序稳定
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from models.trading_brain import (
    BrainContextChips,
    BrainDataQuality,
    BrainPriceZone,
    BrainZoneRoles,
)
from processors.opportunity_engine import build_opportunities


def _zone(
    *,
    coin="BTC",
    zone_id="z1",
    price_mid=99_000.0,
    distance_pct=-1.0,
    roles_kw=None,
    dominant_role="spot_defense",
    support_trust=0.80,
    resistance_trust=0.0,
    sweep_attractiveness=0.30,
    break_through_risk=0.20,
    data_confidence=0.85,
    evidence=None,
) -> BrainPriceZone:
    rk = BrainZoneRoles(**(roles_kw or {"key_level": True, "spot_supply_wall": True}))
    return BrainPriceZone(
        zone_id=zone_id,
        coin=coin,
        price_low=price_mid * 0.998,
        price_high=price_mid * 1.002,
        price_mid=price_mid,
        distance_pct=distance_pct,
        roles=rk,
        dominant_role=dominant_role,
        dominant_label="测试区",
        support_trust=support_trust,
        resistance_trust=resistance_trust,
        sweep_attractiveness=sweep_attractiveness,
        break_through_risk=break_through_risk,
        data_confidence=data_confidence,
        evidence=evidence or ["S级关键支撑", "现货买墙", "Coinbase 共振"],
    )


def _target_zone_above(price_mid: float, role="spot_defense") -> BrainPriceZone:
    return _zone(
        zone_id=f"t_above_{int(price_mid)}",
        price_mid=price_mid,
        distance_pct=2.0,
        dominant_role=role,
        support_trust=0.40,
        resistance_trust=0.70,
    )


def _target_zone_below(price_mid: float, role="liquidation_magnet") -> BrainPriceZone:
    return _zone(
        zone_id=f"t_below_{int(price_mid)}",
        price_mid=price_mid,
        distance_pct=-2.0,
        dominant_role=role,
        support_trust=0.0,
        resistance_trust=0.0,
        sweep_attractiveness=0.7,
    )


def test_support_limit_probe_generated_with_valid_zone():
    last = 100_000.0
    sup = _zone(price_mid=99_000.0, distance_pct=-1.0)
    t1 = _target_zone_above(101_000.0, role="spot_defense")
    t2 = _target_zone_above(103_000.0, role="liquidation_magnet")
    opps = build_opportunities(
        zones=[sup, t1, t2], last_price=last, atr=400.0,
    )
    assert any(o.setup_type == "support_limit_probe" for o in opps)
    o = next(o for o in opps if o.setup_type == "support_limit_probe")
    assert o.direction == "long"
    assert len(o.entry_styles) == 2
    assert {s.style for s in o.entry_styles} == {"aggressive", "conservative"}
    assert o.risk_plan.soft_invalidation > o.risk_plan.hard_stop
    assert max(t.rr for t in o.targets) >= 2.0
    assert o.cancel_conditions
    assert o.state.name in ("forming", "waiting_for_trigger")


def test_resistance_limit_probe_generated_with_valid_zone():
    last = 100_000.0
    res = _zone(
        price_mid=101_000.0,
        distance_pct=1.0,
        support_trust=0.0,
        resistance_trust=0.85,
        roles_kw={"key_level": True, "spot_supply_wall": True},
        dominant_role="spot_defense",
    )
    t = _target_zone_below(99_000.0, role="spot_defense")
    t2 = _target_zone_below(97_000.0, role="liquidation_magnet")
    opps = build_opportunities(zones=[res, t, t2], last_price=last, atr=400.0)
    o = next(o for o in opps if o.setup_type == "resistance_limit_probe")
    assert o.direction == "short"
    assert o.risk_plan.hard_stop > o.risk_plan.soft_invalidation
    assert all(t.rr > 0 for t in o.targets)


def test_fake_break_reclaim_pre_state_is_neutral():
    last = 100_000.0
    sup = _zone(price_mid=99_000.0, distance_pct=-1.0)
    t = _target_zone_above(101_500.0)
    opps = build_opportunities(zones=[sup, t], last_price=last, atr=400.0)
    fakes = [o for o in opps if o.setup_type.startswith("fake_break_reclaim")]
    assert fakes, "应至少生成一条扫破收回观察"
    f = fakes[0]
    assert f.direction == "neutral"
    assert f.state.name == "forming"
    assert "扫破" in f.cancel_conditions[0] or "扫破" in f.state.pending_reason


def test_strict_threshold_filter_low_trust_zone_dropped():
    last = 100_000.0
    weak = _zone(
        price_mid=99_000.0, distance_pct=-1.0,
        support_trust=0.50,  # < 0.70
    )
    t = _target_zone_above(101_500.0)
    opps = build_opportunities(zones=[weak, t], last_price=last, atr=400.0)
    assert not any(o.setup_type == "support_limit_probe" for o in opps)


def test_distance_too_far_drops_zone():
    last = 100_000.0
    far = _zone(price_mid=95_000.0, distance_pct=-5.0)
    t = _target_zone_above(101_500.0)
    opps = build_opportunities(zones=[far, t], last_price=last, atr=400.0)
    assert not any(o.zone_id == far.zone_id for o in opps)


def test_low_data_confidence_drops_zone():
    last = 100_000.0
    z = _zone(price_mid=99_000.0, distance_pct=-1.0, data_confidence=0.50)
    t = _target_zone_above(101_500.0)
    opps = build_opportunities(zones=[z, t], last_price=last, atr=400.0)
    assert not any(o.zone_id == z.zone_id and o.setup_type == "support_limit_probe" for o in opps)


def test_regime_trend_down_blocks_long_setup():
    last = 100_000.0
    z = _zone(price_mid=99_000.0, distance_pct=-1.0)
    t = _target_zone_above(101_500.0)
    ctx = BrainContextChips(regime="trend_down")
    opps = build_opportunities(
        zones=[z, t], last_price=last, atr=400.0, ctx=ctx,
    )
    assert not any(o.direction == "long" for o in opps)


def test_stale_data_inserts_downgrade_in_cancel_conditions():
    last = 100_000.0
    z = _zone(price_mid=99_000.0, distance_pct=-1.0)
    t = _target_zone_above(101_500.0)
    dq = BrainDataQuality(is_partial_ready=True, ready_count=2, total_count=3)
    opps = build_opportunities(zones=[z, t], last_price=last, atr=400.0, dq=dq)
    o = next(o for o in opps if o.setup_type == "support_limit_probe")
    assert any("数据未就绪" in c or "stale" in c for c in o.cancel_conditions)


def test_opportunity_score_sorted_descending():
    last = 100_000.0
    z1 = _zone(zone_id="z1", price_mid=99_500.0, distance_pct=-0.5,
               support_trust=0.85, data_confidence=0.90)
    z2 = _zone(zone_id="z2", price_mid=99_000.0, distance_pct=-1.0,
               support_trust=0.72, data_confidence=0.78)
    t = _target_zone_above(101_500.0)
    opps = build_opportunities(zones=[z1, z2, t], last_price=last, atr=400.0)
    scores = [o.opportunity_score for o in opps]
    assert scores == sorted(scores, reverse=True)


def test_atr_zero_falls_back_to_pct_buffer():
    """ATR=0 时应使用 0.3% × price 兜底，避免 hard_stop 与 zone 重叠。"""
    last = 100_000.0
    z = _zone(price_mid=99_000.0, distance_pct=-1.0)
    t = _target_zone_above(101_500.0)
    opps = build_opportunities(zones=[z, t], last_price=last, atr=0.0)
    o = next(o for o in opps if o.setup_type == "support_limit_probe")
    assert o.risk_plan.hard_stop < z.price_low
    assert (z.price_low - o.risk_plan.hard_stop) > 0


# ────────────────────────────────────────────────────────────────────────────
# P0 回归：T1 RR 门槛（修复 max(t.rr) 误放低 T1 RR 的 setup）
# ────────────────────────────────────────────────────────────────────────────


def test_t1_rr_below_threshold_drops_setup_even_if_t3_high():
    """P0 修复：T1（首要目标）RR < 2.0 即使 T2/T3 RR 很高也必须拒绝。

    旧 bug：用 max(t.rr for t in targets) ≥ 2 判定，等价于"任一远期 target
    RR 高就放行"，会让 OpportunityBoard 显示 T1 RR=0.7~0.9 的伪机会。
    与"高盈亏比机会雷达"定位严重矛盾（GPT 第 15 节痛点）。

    场景：support 99000；T1 99400 (近且 RR 低 ~0.77)；T3 105000 (远 RR ~11)。
    """
    last = 100_000.0
    sup = _zone(price_mid=99_000.0, distance_pct=-1.0)
    near_low_rr = _target_zone_above(99_400.0, role="key_level_only")
    far_high_rr = _target_zone_above(105_000.0, role="liquidation_magnet")
    opps = build_opportunities(
        zones=[sup, near_low_rr, far_high_rr], last_price=last, atr=400.0,
    )
    # 即使整体 max RR 很高，T1 RR < 2.0 也必须拒绝该 setup
    long_setups = [o for o in opps if o.setup_type == "support_limit_probe"]
    assert not long_setups, (
        f"T1 RR < 2.0 时不应生成 support_limit_probe；实际产出: "
        f"{[(o.setup_id, [t.rr for t in o.targets]) for o in long_setups]}"
    )


def test_t1_rr_meets_threshold_setup_passes():
    """T1 RR ≥ 2.0 时 setup 正常生成（保留正向场景的回归保障）。"""
    last = 100_000.0
    sup = _zone(price_mid=99_000.0, distance_pct=-1.0)
    # T1 价格 101_500：reward ~2500 / risk ~520 → RR ~4.8 远超门槛
    t1 = _target_zone_above(101_500.0)
    opps = build_opportunities(zones=[sup, t1], last_price=last, atr=400.0)
    long_setups = [o for o in opps if o.setup_type == "support_limit_probe"]
    assert long_setups, "T1 RR ≥ 2.0 应正常生成 setup"
    assert long_setups[0].targets[0].rr >= 2.0


def test_t1_rr_threshold_applies_to_resistance_short():
    """空头方向同样适用：T1 RR < 2.0 必须被拒。"""
    last = 100_000.0
    res = _zone(
        price_mid=101_000.0,
        distance_pct=1.0,
        support_trust=0.0,
        resistance_trust=0.85,
        roles_kw={"key_level": True, "spot_supply_wall": True},
        dominant_role="spot_defense",
    )
    near_low_rr = _target_zone_below(100_600.0, role="key_level_only")
    far_high_rr = _target_zone_below(95_000.0, role="liquidation_magnet")
    opps = build_opportunities(
        zones=[res, near_low_rr, far_high_rr], last_price=last, atr=400.0,
    )
    short_setups = [o for o in opps if o.setup_type == "resistance_limit_probe"]
    assert not short_setups, (
        f"T1 RR < 2.0 时不应生成 resistance_limit_probe；实际产出: "
        f"{[(o.setup_id, [t.rr for t in o.targets]) for o in short_setups]}"
    )


# ────────────────────────────────────────────────────────────────────────────
# P1-C 修复回归：invalidation_clarity 钟形函数（替代旧"风险越大越高分"单调函数）
# ────────────────────────────────────────────────────────────────────────────


def test_invalidation_clarity_bell_curve_peaks_at_06_atr():
    """钟形函数：peak 在 0.6 ATR；偏离时应单调下降。"""
    from processors.opportunity_engine import _invalidation_clarity
    atr = 400.0
    price = 100_000.0
    s_too_close = _invalidation_clarity(0.10 * atr, atr, price)
    s_close = _invalidation_clarity(0.30 * atr, atr, price)
    s_optimal = _invalidation_clarity(0.60 * atr, atr, price)
    s_far = _invalidation_clarity(1.00 * atr, atr, price)
    s_too_far = _invalidation_clarity(2.00 * atr, atr, price)
    # 0.6 ATR 是峰值
    assert s_optimal == max(s_too_close, s_close, s_optimal, s_far, s_too_far)
    assert s_optimal >= 0.99  # 接近 1.0
    # 两侧单调下降（除非已撞 floor）
    assert s_close > s_too_close
    assert s_far < s_optimal
    # 极远端被 floor 截断到 0.30
    assert s_too_far <= 0.31
    # 旧 bug 直接验证：风险越大评分越高 → 应被推翻
    assert s_far < s_optimal, "风险越大评分越高的旧逻辑必须被打破"


def test_invalidation_clarity_atr_missing_uses_price_proxy():
    """ATR=0 时退化为 price × 0.4% 作为单位尺度（不应崩溃也不应永远得 floor）。"""
    from processors.opportunity_engine import _invalidation_clarity
    price = 100_000.0
    proxy_unit = price * 0.004  # 400
    s = _invalidation_clarity(0.6 * proxy_unit, atr=0.0, ref_price=price)
    assert 0.99 <= s <= 1.0  # 0.6 个 proxy 单位应到达 peak


def test_path_obstacle_score_filled_for_t2_t3():
    """P1-F：T1 path_obstacle=0；T2/T3 应统计中间路径上 zone 的 sweep_attractiveness。"""
    last = 100_000.0
    z_anchor = _zone(price_mid=99_000.0, distance_pct=-1.0, support_trust=0.85)
    # 路径上插入一个高 sweep_attractiveness 的清算磁铁
    z_obstacle = _zone(
        zone_id="obs1", price_mid=100_500.0, distance_pct=0.5,
        dominant_role="liquidation_magnet", support_trust=0.0,
        sweep_attractiveness=0.85,
    )
    z_t1 = _target_zone_above(101_500.0)  # T2
    z_t2 = _target_zone_above(102_500.0)  # T3
    opps = build_opportunities(
        zones=[z_anchor, z_obstacle, z_t1, z_t2], last_price=last, atr=400.0,
    )
    o = next(o for o in opps if o.setup_type == "support_limit_probe")
    # T1 = z_obstacle 自身（最近的上方 zone，是磁铁）
    # T2/T3 = z_t1, z_t2，路径上有 z_obstacle 提供 0.85 sweep_attractiveness
    if len(o.targets) >= 2:
        # T1 自身路径阻力为 0（zone.price_high 到 z_obstacle.price_mid 之间无其他 zone）
        # T2 路径上有 z_obstacle 在区间内 → path_obstacle_score > 0
        t1, t2 = o.targets[0], o.targets[1]
        assert t1.path_obstacle_score == 0.0
        assert t2.path_obstacle_score > 0.0, (
            f"T2 路径上有磁铁 z_obstacle，path_obstacle 应非零，got {t2.path_obstacle_score}"
        )


def test_real_setup_far_stop_gets_lower_asymmetry_than_optimal_stop():
    """端到端：同样的 zone/target，hard_stop 远的 setup 不对称分应低于 hard_stop 适中的。
    （这是 P1-C 修复后才会成立的关键回归。）"""
    last = 100_000.0
    # 用同 zone 但人为构造 atr 极小（让 _HARD_BUFFER_ATR=0.8 的 hard_stop 处于 0.6 ATR 附近）
    z = _zone(
        price_mid=99_000.0, distance_pct=-1.0,
        support_trust=0.85, data_confidence=0.85,
    )
    t = _target_zone_above(102_000.0)
    opps_norm = build_opportunities(zones=[z, t], last_price=last, atr=400.0)
    # 模拟"宽止损 3×ATR"场景：把 atr 放大 3 倍但 hard_stop 仍按 0.8 atr_new 算
    # → hard_stop 距 zone_mid ≈ 2.4 旧 ATR，远超 0.6 ATR 峰值
    opps_far = build_opportunities(zones=[z, t], last_price=last, atr=1600.0)
    o_norm = next(o for o in opps_norm if o.setup_type == "support_limit_probe")
    o_far = next(
        (o for o in opps_far if o.setup_type == "support_limit_probe"), None,
    )
    # ATR 放大后可能因 RR 不足滤掉 → 这本身也是钟形函数的合理结果
    if o_far is None:
        return
    assert o_norm.asymmetry_score >= o_far.asymmetry_score, (
        f"宽止损 setup 的不对称分应不高于适中止损：norm={o_norm.asymmetry_score} far={o_far.asymmetry_score}"
    )


# ────────────────────────────────────────────────────────────────────────────
# G-10：build_opportunities reject_log + diagnose_opportunities 拒绝理由收集
# ────────────────────────────────────────────────────────────────────────────


class TestRejectionDiagnostics:
    def test_build_opportunities_default_no_reject_log_no_side_effect(self):
        """旧调用方（不传 reject_log）行为完全不变。"""
        last = 100_000.0
        sup = _zone(price_mid=99_000.0, distance_pct=-1.0)
        t = _target_zone_above(101_500.0)
        opps_old = build_opportunities(zones=[sup, t], last_price=last, atr=400.0)
        opps_new = build_opportunities(
            zones=[sup, t], last_price=last, atr=400.0, reject_log=None,
        )
        assert [o.setup_id for o in opps_old] == [o.setup_id for o in opps_new]

    def test_low_trust_zone_records_trust_reason(self):
        """support_trust=0.50 的 zone 应记录 trust_too_low（support_limit_probe 路径）。"""
        from processors.opportunity_engine import OpportunityRejection
        last = 100_000.0
        weak = _zone(price_mid=99_000.0, distance_pct=-1.0, support_trust=0.50)
        t = _target_zone_above(101_500.0)
        log: list = []
        build_opportunities(
            zones=[weak, t], last_price=last, atr=400.0, reject_log=log,
        )
        assert all(isinstance(r, OpportunityRejection) for r in log)
        slr = [
            r for r in log
            if r.zone_id == weak.zone_id
            and r.setup_type == "support_limit_probe"
        ]
        assert slr, "弱信任 zone 在 support_limit_probe 必须留下拒绝记录"
        assert slr[0].reason_code == "trust_too_low"
        assert "support_trust" in slr[0].detail

    def test_distance_too_far_recorded(self):
        last = 100_000.0
        far = _zone(price_mid=95_000.0, distance_pct=-5.0)
        t = _target_zone_above(101_500.0)
        log: list = []
        build_opportunities(
            zones=[far, t], last_price=last, atr=400.0, reject_log=log,
        )
        rs = [r for r in log if r.zone_id == far.zone_id]
        assert any(r.reason_code == "distance_too_far" for r in rs), (
            f"距离过远 zone 必须留下 distance_too_far 拒绝记录，实际: "
            f"{[(r.zone_id, r.setup_type, r.reason_code) for r in rs]}"
        )

    def test_regime_blocks_long_recorded(self):
        last = 100_000.0
        z = _zone(price_mid=99_000.0, distance_pct=-1.0)
        t = _target_zone_above(101_500.0)
        ctx = BrainContextChips(regime="trend_down")
        log: list = []
        build_opportunities(
            zones=[z, t], last_price=last, atr=400.0, ctx=ctx, reject_log=log,
        )
        long_rs = [
            r for r in log
            if r.zone_id == z.zone_id and r.direction == "long"
        ]
        assert any(r.reason_code == "regime_blocks" for r in long_rs), (
            f"trend_down regime 必须在多头路径留下 regime_blocks 记录"
        )

    def test_rr_insufficient_recorded(self):
        """T1 RR < 2.0 的 setup 必须留下 rr_insufficient（不应只是 silent drop）。"""
        last = 100_000.0
        sup = _zone(price_mid=99_000.0, distance_pct=-1.0)
        near_low_rr = _target_zone_above(99_400.0, role="key_level_only")
        log: list = []
        build_opportunities(
            zones=[sup, near_low_rr], last_price=last, atr=400.0, reject_log=log,
        )
        slr = [
            r for r in log
            if r.zone_id == sup.zone_id
            and r.setup_type == "support_limit_probe"
        ]
        assert any(r.reason_code == "rr_insufficient" for r in slr)

    def test_diagnose_opportunities_returns_full_log(self):
        """diagnose_opportunities 是 reject_log 的便捷封装；返回值非 None。"""
        from processors.opportunity_engine import diagnose_opportunities
        last = 100_000.0
        weak = _zone(price_mid=99_000.0, distance_pct=-1.0, support_trust=0.50)
        rejections = diagnose_opportunities(
            zones=[weak], last_price=last, atr=400.0,
        )
        # 4 个 setup_type × 1 个 zone = 至多 4 条；至少应有 1 条
        assert len(rejections) >= 1
        codes = {r.reason_code for r in rejections}
        # 弱信任 + 无 target zone 至少触发 trust 或 targets 类拒绝
        assert codes & {"trust_too_low", "targets_missing", "rr_insufficient"}

    def test_diagnose_with_empty_zones_returns_empty(self):
        from processors.opportunity_engine import diagnose_opportunities
        assert diagnose_opportunities(
            zones=[], last_price=100_000.0, atr=400.0,
        ) == []

    def test_diagnose_with_zero_price_returns_empty(self):
        from processors.opportunity_engine import diagnose_opportunities
        z = _zone(price_mid=99_000.0, distance_pct=-1.0)
        assert diagnose_opportunities(
            zones=[z], last_price=0.0, atr=400.0,
        ) == []
