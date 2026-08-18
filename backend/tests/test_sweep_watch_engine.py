"""W4-T1 阶段 4 · sweep_watch_engine 单元测试。

覆盖：
  1. 代表 zone 选择（强角色优先；无强 zone → None）
  2. 5 态机各 phase 判定（含边界）
  3. 3 派生分公式正确（含边界）
  4. CVD 对齐 / 反向打分
  5. 触发观察 + 失效条件按 (side, phase) 模板正确产出
  6. trace_log 完整性（关键步骤都有 entry）
  7. 主入口 build_sweep_watch 端到端集成
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from models.sweep_watch import BrainSweepWatch, SweepWatchSide
from models.trading_brain import (
    BrainContextChips,
    BrainEvent,
    BrainPriceZone,
    BrainScenario,
    BrainZoneRoles,
)
from processors.sweep_watch_engine import (
    _calc_continuation_risk,
    _calc_reversal_potential,
    _cvd_against_score,
    _cvd_alignment_score,
    _decide_phase,
    _select_representative,
    _TraceRecorder,
    build_sweep_watch,
)


# ─────────────────────────────────────────────────────────────────────
# 工具
# ─────────────────────────────────────────────────────────────────────
def _zone(
    *,
    zone_id: str = "z_default",
    coin: str = "BTC",
    price_mid: float = 75_000.0,
    distance_pct: float = -1.0,
    dominant_role: str = "spot_defense",
    support_strength: float = 0.7,
    support_fragility: float = 0.1,
    resistance_strength: float = 0.0,
    resistance_fragility: float = 0.0,
    sweep_attractiveness: float = 0.3,
    break_through_risk: float = 0.4,
    data_confidence: float = 0.8,
    wall_zone_ids: list | None = None,
) -> BrainPriceZone:
    return BrainPriceZone(
        zone_id=zone_id,
        coin=coin,
        price_low=price_mid * 0.998,
        price_high=price_mid * 1.002,
        price_mid=price_mid,
        distance_pct=distance_pct,
        roles=BrainZoneRoles(spot_supply_wall=True),
        dominant_role=dominant_role,  # type: ignore[arg-type]
        dominant_label=f"测试区 {dominant_role}",
        wall_zone_ids=wall_zone_ids or [f"wall_{zone_id}"],
        support_trust=support_strength * (1 - 0.5 * support_fragility),
        resistance_trust=resistance_strength * (1 - 0.5 * resistance_fragility),
        support_strength=support_strength,
        support_fragility=support_fragility,
        resistance_strength=resistance_strength,
        resistance_fragility=resistance_fragility,
        sweep_attractiveness=sweep_attractiveness,
        break_through_risk=break_through_risk,
        data_confidence=data_confidence,
        evidence=["测试证据"],
        scenario=BrainScenario(),
    )


def _ctx(cvd_fut: str = "", cvd_spot: str = "") -> BrainContextChips:
    return BrainContextChips(cvd_contract_trend=cvd_fut, cvd_spot_trend=cvd_spot)


def _wall_consumed_event(zone_id: str, ts: int, side: str = "买侧") -> BrainEvent:
    return BrainEvent(
        ts=ts, layer="spot", price_mid=75_000.0, zone_id=zone_id,
        message=f"{side}墙被成交消耗", source="liquidity_wall_engine",
    )


# ─────────────────────────────────────────────────────────────────────
# 1. 代表 zone 选择
# ─────────────────────────────────────────────────────────────────────
class TestSelectRepresentative:
    def test_below_picks_closest_strong_role(self):
        trace = _TraceRecorder("below")
        zones = [
            _zone(zone_id="far", distance_pct=-3.0, dominant_role="spot_defense"),
            _zone(zone_id="near", distance_pct=-0.5, dominant_role="spot_defense"),
        ]
        picked = _select_representative("below", zones, trace)
        assert picked is not None
        assert picked.zone_id == "near"

    def test_above_picks_only_above_zones(self):
        trace = _TraceRecorder("above")
        zones = [
            _zone(zone_id="below_only", distance_pct=-0.3, dominant_role="spot_defense"),
            _zone(zone_id="above1", distance_pct=1.0, dominant_role="spot_defense"),
        ]
        picked = _select_representative("above", zones, trace)
        assert picked is not None
        assert picked.zone_id == "above1"

    def test_skips_other_role(self):
        """other 角色不算强角色，跳过。"""
        trace = _TraceRecorder("below")
        zones = [
            _zone(zone_id="weak", distance_pct=-0.2, dominant_role="other"),
            _zone(zone_id="strong_far", distance_pct=-2.5, dominant_role="liquidation_magnet"),
        ]
        picked = _select_representative("below", zones, trace)
        assert picked is not None
        assert picked.zone_id == "strong_far"

    def test_no_strong_zone_returns_none(self):
        trace = _TraceRecorder("below")
        zones = [
            _zone(zone_id="z1", distance_pct=-0.5, dominant_role="other"),
            _zone(zone_id="z2", distance_pct=-1.0, dominant_role="key_level_only"),
        ]
        picked = _select_representative("below", zones, trace)
        assert picked is None
        # 必须留下 trace（即使返回 None）
        assert any(e.step == "select_representative" for e in trace.entries)

    def test_records_trace_entry(self):
        trace = _TraceRecorder("below")
        zones = [_zone(zone_id="z1", distance_pct=-0.5)]
        _select_representative("below", zones, trace)
        entry = next(e for e in trace.entries if e.step == "select_representative")
        assert entry.side == "below"
        # 缺口 1：rule_hit 改为按桶式排序的语义化命名
        assert entry.rule_hit in {
            "near_bucket_high_attractiveness",
            "mid_bucket_closest_distance",
            "far_bucket_closest_distance",
        }
        # |d|=0.5 ≤ 1.5 → 必然落入近桶
        assert entry.rule_hit == "near_bucket_high_attractiveness"
        assert entry.output is not None
        assert entry.output["zone_id"] == "z1"
        # 缺口 1：trace output 新增 sweep_attractiveness 字段（透明化）
        assert "sweep_attractiveness" in entry.output

    def test_near_bucket_picks_high_sa_over_closer_low_sa(self):
        """缺口 1：近桶（|d|≤1.5%）内，SA 高的赢距离更近的。

        修复前：strong.sort(key=|distance|) → 选 closer_low_sa（距离 0.3）
        修复后：近桶 SA 优先 → 选 farther_high_sa（SA 0.9）
        """
        trace = _TraceRecorder("below")
        zones = [
            _zone(
                zone_id="closer_low_sa", distance_pct=-0.3,
                dominant_role="spot_defense", sweep_attractiveness=0.10,
            ),
            _zone(
                zone_id="farther_high_sa", distance_pct=-1.2,
                dominant_role="spot_defense", sweep_attractiveness=0.90,
            ),
        ]
        picked = _select_representative("below", zones, trace)
        assert picked is not None
        assert picked.zone_id == "farther_high_sa"

    def test_far_bucket_still_picks_closest_distance(self):
        """缺口 1：中/远桶（|d|>1.5%）仍按距离升序，SA 不抢镜。

        防止"远端 SA 极高的 zone 抢走代表位"导致代表区跳出 approaching 阈值。
        """
        trace = _TraceRecorder("below")
        zones = [
            _zone(
                zone_id="farther_high_sa", distance_pct=-3.0,
                dominant_role="spot_defense", sweep_attractiveness=0.95,
            ),
            _zone(
                zone_id="closer_low_sa", distance_pct=-2.0,
                dominant_role="spot_defense", sweep_attractiveness=0.10,
            ),
        ]
        picked = _select_representative("below", zones, trace)
        assert picked is not None
        assert picked.zone_id == "closer_low_sa"

    def test_bucket_boundary_at_1_5_pct(self):
        """缺口 1 边界：|d|==1.5% 应归近桶（≤ 阈值），SA 优先生效。"""
        trace = _TraceRecorder("below")
        zones = [
            _zone(
                zone_id="boundary_high_sa", distance_pct=-1.5,
                dominant_role="spot_defense", sweep_attractiveness=0.80,
            ),
            _zone(
                zone_id="just_outside_low_sa", distance_pct=-1.6,
                dominant_role="spot_defense", sweep_attractiveness=0.10,
            ),
        ]
        picked = _select_representative("below", zones, trace)
        assert picked is not None
        # boundary 在近桶（SA 优先）→ 它必赢
        assert picked.zone_id == "boundary_high_sa"


# ─────────────────────────────────────────────────────────────────────
# 2. 5 态机
# ─────────────────────────────────────────────────────────────────────
class TestPhaseDecision:
    def test_waiting_far(self):
        # 下方 zone（price_low=74_850, price_high=75_150），
        # last_price=77_000 在区间之上 → 未穿破 + distance|2.5|>1.5 → waiting
        trace = _TraceRecorder("below")
        z = _zone(distance_pct=-2.5, wall_zone_ids=["w1"])
        phase = _decide_phase("below", z, 77_000, [], None, 1_000_000, trace)
        assert phase == "waiting"

    def test_approaching_within_threshold(self):
        trace = _TraceRecorder("below")
        z = _zone(distance_pct=-1.0, wall_zone_ids=["w1"])
        # 区间外但靠近：last_price=75_500 > price_high=75_150 + |distance|=1.0 ≤ 1.5
        phase = _decide_phase("below", z, 75_500, [], None, 1_000_000, trace)
        assert phase == "approaching"

    def test_in_sweep_via_consumed_event(self):
        # 已扫 + 未收回 + CVD 中性（ctx=None → alignment=0.5 < 0.6）
        # → 严格归 in_sweep（不是 swept_continuing）；P0#4 修复后语义与文档对齐
        trace = _TraceRecorder("below")
        z = _zone(distance_pct=-0.3, wall_zone_ids=["w1"])
        ev = _wall_consumed_event("w1", ts=999_900)
        phase = _decide_phase("below", z, 73_000, [ev], None, 1_000_000, trace)
        assert phase == "in_sweep"

    def test_swept_reclaiming_when_price_back_inside(self):
        trace = _TraceRecorder("below")
        z = _zone(distance_pct=-0.3, price_mid=75_000, wall_zone_ids=["w1"])
        ev = _wall_consumed_event("w1", ts=999_900)
        # last_price 在区间 [74_850, 75_150] 内 → reclaimed=True
        phase = _decide_phase("below", z, 75_000, [ev], None, 1_000_000, trace)
        assert phase == "swept_reclaiming"

    def test_swept_continuing_with_cvd_alignment(self):
        trace = _TraceRecorder("below")
        z = _zone(distance_pct=-0.3, price_mid=75_000, wall_zone_ids=["w1"])
        ev = _wall_consumed_event("w1", ts=999_900)
        ctx = _ctx(cvd_fut="falling", cvd_spot="falling")
        # 价格在区间外 + CVD 同向下跌 → swept_continuing
        phase = _decide_phase("below", z, 73_000, [ev], ctx, 1_000_000, trace)
        assert phase == "swept_continuing"

    def test_p0_consumed_no_reclaim_cvd_against_stays_in_sweep(self):
        """P0#4 回归：consumed + 未收回 + CVD 反向（below 看 rising）→ 必须 in_sweep。

        修复前会错误归 swept_continuing（仅看 consumed/reclaim 不看 CVD），
        导致前端把"墙被吃但 CVD 反弹"误判为级联，与文档语义冲突。
        """
        trace = _TraceRecorder("below")
        z = _zone(distance_pct=-0.3, price_mid=75_000, wall_zone_ids=["w1"])
        ev = _wall_consumed_event("w1", ts=999_900)
        # below 侧 CVD 上涨 = 反向（多头 reload）→ alignment=0.0
        ctx = _ctx(cvd_fut="rising", cvd_spot="rising")
        phase = _decide_phase("below", z, 73_000, [ev], ctx, 1_000_000, trace)
        assert phase == "in_sweep"
        entry = next(e for e in trace.entries if e.step == "phase_decision")
        assert entry.rule_hit == "in_sweep_no_strong_continuation"

    def test_above_in_sweep_when_price_pierced_up(self):
        # 上方价格穿破 + CVD 中性（ctx=None → above 侧 alignment=0.5 < 0.6）
        # → in_sweep（保守，等结构反应）
        trace = _TraceRecorder("above")
        z = _zone(distance_pct=0.5, price_mid=75_000)
        phase = _decide_phase("above", z, 76_000, [], None, 1_000_000, trace)
        assert phase == "in_sweep"

    def test_records_trace_entry(self):
        trace = _TraceRecorder("below")
        z = _zone(distance_pct=-1.0)
        # last_price=75_500 在区间(74_850, 75_150)之上，未跌破 → approaching
        _decide_phase("below", z, 75_500, [], None, 1_000_000, trace)
        entry = next(e for e in trace.entries if e.step == "phase_decision")
        assert entry.output == "approaching"
        assert "distance_pct" in entry.inputs


# ─────────────────────────────────────────────────────────────────────
# 3. CVD 对齐打分
# ─────────────────────────────────────────────────────────────────────
class TestCVDScoring:
    def test_below_falling_cvd_high_alignment(self):
        ctx = _ctx(cvd_fut="falling", cvd_spot="falling")
        assert _cvd_alignment_score("below", ctx) == 1.0

    def test_below_rising_cvd_low_alignment(self):
        ctx = _ctx(cvd_fut="rising", cvd_spot="rising")
        assert _cvd_alignment_score("below", ctx) == 0.0

    def test_above_rising_cvd_high_alignment(self):
        ctx = _ctx(cvd_fut="rising", cvd_spot="rising")
        assert _cvd_alignment_score("above", ctx) == 1.0

    def test_against_is_complement_of_alignment(self):
        ctx = _ctx(cvd_fut="falling", cvd_spot="falling")
        assert _cvd_against_score("below", ctx) + _cvd_alignment_score("below", ctx) == 1.0

    def test_no_ctx_returns_05(self):
        assert _cvd_alignment_score("below", None) == 0.5
        assert _cvd_against_score("below", None) == 0.5

    def test_below_declining_cvd_high_alignment(self):
        """生产端 _calc_trend 输出 declining（非 falling），必须命中强跌。"""
        ctx = _ctx(cvd_fut="declining", cvd_spot="declining")
        assert _cvd_alignment_score("below", ctx) == 1.0

    def test_above_declining_cvd_low_alignment(self):
        ctx = _ctx(cvd_fut="declining", cvd_spot="declining")
        assert _cvd_alignment_score("above", ctx) == 0.0

    def test_flat_cvd_neutral(self):
        ctx = _ctx(cvd_fut="flat", cvd_spot="flat")
        assert _cvd_alignment_score("below", ctx) == 0.5

    def test_mixed_enum_aliases(self):
        """历史别名（up/down/bearish）也应经 normalize_trend 归一化。"""
        ctx = _ctx(cvd_fut="down", cvd_spot="bearish")
        assert _cvd_alignment_score("below", ctx) == 1.0
        ctx2 = _ctx(cvd_fut="up", cvd_spot="bullish")
        assert _cvd_alignment_score("below", ctx2) == 0.0


# ─────────────────────────────────────────────────────────────────────
# 4. 3 派生分公式
# ─────────────────────────────────────────────────────────────────────
class TestReversalPotential:
    def test_strong_support_high_data_high_reversal(self):
        trace = _TraceRecorder("below")
        z = _zone(support_strength=1.0, support_fragility=0.0, data_confidence=1.0)
        ctx = _ctx(cvd_fut="rising", cvd_spot="rising")  # below 侧 CVD 反向 = 满分
        rp = _calc_reversal_potential("below", z, ctx, trace)
        # 0.40×1 + 0.20×1 + 0.20×1 + 0.20×1 = 1.0
        assert rp == pytest.approx(1.0, abs=0.01)

    def test_zero_inputs_zero_reversal(self):
        trace = _TraceRecorder("below")
        z = _zone(support_strength=0.0, support_fragility=1.0, data_confidence=0.0)
        ctx = _ctx(cvd_fut="falling", cvd_spot="falling")  # below 侧 CVD 同向 = against=0
        rp = _calc_reversal_potential("below", z, ctx, trace)
        assert rp == pytest.approx(0.0, abs=0.01)

    def test_above_uses_resistance_fields(self):
        trace = _TraceRecorder("above")
        z = _zone(
            support_strength=0.0,
            resistance_strength=0.8,
            resistance_fragility=0.0,
            data_confidence=0.8,
        )
        ctx = _ctx(cvd_fut="falling", cvd_spot="falling")  # above 侧 falling = against=1
        rp = _calc_reversal_potential("above", z, ctx, trace)
        # 0.40×0.8 + 0.20×1 + 0.20×0.8 + 0.20×1 = 0.32+0.20+0.16+0.20 = 0.88
        assert rp == pytest.approx(0.88, abs=0.01)

    def test_records_trace_with_breakdown(self):
        trace = _TraceRecorder("below")
        z = _zone(support_strength=0.5, support_fragility=0.2, data_confidence=0.6)
        _calc_reversal_potential("below", z, None, trace)
        entry = next(e for e in trace.entries if e.step == "reversal_potential")
        assert "contributions" in entry.inputs
        assert entry.output is not None
        assert 0 <= entry.output <= 1


class TestContinuationRisk:
    def test_high_btr_high_sa_high_risk(self):
        trace = _TraceRecorder("below")
        z = _zone(
            break_through_risk=1.0, sweep_attractiveness=1.0,
            data_confidence=0.0, support_strength=0.0, support_fragility=1.0,
        )
        ctx = _ctx(cvd_fut="falling", cvd_spot="falling")
        cr = _calc_continuation_risk("below", z, ctx, trace)
        # 0.35×1 + 0.25×1 + 0.20×1 + 0.10×1 + 0.10×1 = 1.00
        assert cr == pytest.approx(1.0, abs=0.01)

    def test_zero_inputs_zero_risk(self):
        trace = _TraceRecorder("below")
        z = _zone(
            break_through_risk=0.0, sweep_attractiveness=0.0,
            data_confidence=1.0, support_strength=1.0, support_fragility=0.0,
        )
        ctx = _ctx(cvd_fut="rising", cvd_spot="rising")  # below 侧 CVD 反向 = alignment=0
        cr = _calc_continuation_risk("below", z, ctx, trace)
        assert cr == pytest.approx(0.0, abs=0.01)

    def test_records_trace_with_contributions(self):
        trace = _TraceRecorder("below")
        z = _zone(break_through_risk=0.5, sweep_attractiveness=0.4)
        _calc_continuation_risk("below", z, None, trace)
        entry = next(e for e in trace.entries if e.step == "continuation_risk")
        assert "contributions" in entry.inputs


# ─────────────────────────────────────────────────────────────────────
# 5. 主入口集成
# ─────────────────────────────────────────────────────────────────────
class TestBuildSweepWatchIntegration:
    def test_returns_below_and_above_when_both_have_strong_zones(self):
        zones = [
            _zone(zone_id="zb", distance_pct=-0.5, dominant_role="spot_defense"),
            _zone(zone_id="za", distance_pct=0.8, dominant_role="liquidation_magnet"),
        ]
        sw = build_sweep_watch(
            coin="BTC", last_price=75_000, zones=zones, events=[],
            ctx=None, now_sec=1_000_000,
        )
        assert isinstance(sw, BrainSweepWatch)
        assert sw.below is not None
        assert sw.above is not None
        assert sw.below.representative_zone_id == "zb"
        assert sw.above.representative_zone_id == "za"

    def test_returns_none_for_side_without_strong_zones(self):
        # 只有上方有强 zone
        zones = [_zone(zone_id="za", distance_pct=0.8, dominant_role="spot_defense")]
        sw = build_sweep_watch(
            coin="BTC", last_price=75_000, zones=zones, events=[],
            ctx=None, now_sec=1_000_000,
        )
        assert sw.below is None
        assert sw.above is not None

    def test_trace_log_has_all_key_steps_per_side(self):
        zones = [
            _zone(zone_id="zb", distance_pct=-0.5, dominant_role="spot_defense"),
            _zone(zone_id="za", distance_pct=0.8, dominant_role="spot_defense"),
        ]
        sw = build_sweep_watch(
            coin="BTC", last_price=75_000, zones=zones, events=[],
            ctx=None, now_sec=1_000_000,
        )
        below_steps = [e.step for e in sw.trace_log if e.side == "below"]
        above_steps = [e.step for e in sw.trace_log if e.side == "above"]
        for steps in (below_steps, above_steps):
            assert "select_representative" in steps
            assert "phase_decision" in steps
            assert "sweep_attractiveness" in steps
            assert "reversal_potential" in steps
            assert "continuation_risk" in steps
            assert "triggers_invalidations" in steps

    def test_no_strong_zone_only_records_select_representative_trace(self):
        """无强角色时，trace 仍含 select_representative 但其他步骤跳过。"""
        zones = [_zone(zone_id="weak", distance_pct=-0.5, dominant_role="other")]
        sw = build_sweep_watch(
            coin="BTC", last_price=75_000, zones=zones, events=[],
            ctx=None, now_sec=1_000_000,
        )
        assert sw.below is None
        below_steps = [e.step for e in sw.trace_log if e.side == "below"]
        # 至少要有 select_representative，但不应有 phase_decision
        assert "select_representative" in below_steps
        assert "phase_decision" not in below_steps

    def test_triggers_invalidations_count_capped(self):
        zones = [_zone(zone_id="zb", distance_pct=-0.5, dominant_role="spot_defense")]
        sw = build_sweep_watch(
            coin="BTC", last_price=75_000, zones=zones, events=[],
            ctx=None, now_sec=1_000_000,
        )
        assert sw.below is not None
        assert len(sw.below.triggers) <= 3
        assert len(sw.below.invalidations) <= 2

    def test_waiting_template_differs_from_approaching(self):
        """缺口 5：waiting (距 > 1.5%) 的触发模板讲"会不会接近"，
        而 approaching (距 ≤ 1.5%) 讲"扫到怎样"。两者不应共用模板。
        """
        # waiting：距离 -2.5%，price 在区间外 + 远
        z_wait = _zone(zone_id="zw", price_mid=75_000, distance_pct=-2.5,
                       dominant_role="spot_defense")
        sw_wait = build_sweep_watch(
            coin="BTC", last_price=77_000, zones=[z_wait], events=[],
            ctx=None, now_sec=1_000_000,
        )
        assert sw_wait.below is not None
        assert sw_wait.below.sweep_phase == "waiting"
        wait_triggers = sw_wait.below.triggers

        # approaching：距离 -1.0%，靠近但未触发
        z_app = _zone(zone_id="za", price_mid=75_000, distance_pct=-1.0,
                      dominant_role="spot_defense")
        sw_app = build_sweep_watch(
            coin="BTC", last_price=75_500, zones=[z_app], events=[],
            ctx=None, now_sec=1_000_000,
        )
        assert sw_app.below is not None
        assert sw_app.below.sweep_phase == "approaching"
        app_triggers = sw_app.below.triggers

        # 两套模板不应相同（waiting 讲"靠近"，approaching 讲"5min 收回"）
        assert wait_triggers != app_triggers
        # waiting 模板第一条应包含"靠近"语义
        assert any("靠近" in t for t in wait_triggers)
        # approaching 模板第一条应包含"5min" 时间窗口语义
        assert any("5min" in t for t in app_triggers)

    def test_above_waiting_template_differs_from_approaching(self):
        """缺口 5：上方侧同样需要 waiting 与 approaching 模板分化。"""
        z_wait = _zone(zone_id="zw", price_mid=75_000, distance_pct=2.5,
                       dominant_role="spot_defense", resistance_strength=0.7)
        sw_wait = build_sweep_watch(
            coin="BTC", last_price=73_000, zones=[z_wait], events=[],
            ctx=None, now_sec=1_000_000,
        )
        assert sw_wait.above is not None
        assert sw_wait.above.sweep_phase == "waiting"
        # waiting 模板讲"靠近"，不讲"5min 跌回"
        assert any("靠近" in t for t in sw_wait.above.triggers)
        assert not any("跌回" in t for t in sw_wait.above.triggers)

    def test_phase_swept_reclaiming_end_to_end(self):
        zones = [_zone(zone_id="zb", price_mid=75_000, distance_pct=-0.3,
                       dominant_role="spot_defense", wall_zone_ids=["w1"])]
        ev = _wall_consumed_event("w1", ts=999_900)
        sw = build_sweep_watch(
            coin="BTC", last_price=75_000, zones=zones, events=[ev],
            ctx=None, now_sec=1_000_000,
        )
        assert sw.below is not None
        assert sw.below.sweep_phase == "swept_reclaiming"
        # 该 phase 的 trigger 应包含"站稳"关键词
        assert any("站稳" in t for t in sw.below.triggers)


# ─────────────────────────────────────────────────────────────────────
# 6. 模型字段完整性（防止漏导出）
# ─────────────────────────────────────────────────────────────────────
def test_sweep_watch_side_serialization_has_all_fields():
    side = SweepWatchSide(
        direction="below",
        label="下方多头止损带",
        representative_zone_id="zb",
        representative_zone_label="测试区",
        price_band=(74_900.0, 75_100.0),
        distance_pct=-0.3,
        sweep_phase="approaching",
        sweep_attractiveness=0.5,
        reversal_potential=0.4,
        continuation_risk=0.6,
        triggers=["t1"],
        invalidations=["i1"],
    )
    d = side.model_dump()
    for f in [
        "direction", "label", "representative_zone_id", "price_band",
        "distance_pct", "sweep_phase", "sweep_attractiveness",
        "reversal_potential", "continuation_risk", "triggers", "invalidations",
    ]:
        assert f in d
