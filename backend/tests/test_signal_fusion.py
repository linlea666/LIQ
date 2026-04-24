"""D15 Signal Fusion 单测（双引擎融合层）

覆盖：
  - 共识分类：strong / agree / math_lead / ai_lead / conflict
  - 最终打分公式（strong=max / agree=加权 / lead=×0.85 / conflict=min-10）
  - 入场参数合并（long 取交集/更严止损；conflict 全 None）
  - SafetyGate block → action 强制 avoid
  - SafetyGate warn → execute 降级为 reduce_size
  - Divergence 匹配（conflict 时）
  - 动作与仓位：execute / reduce_size / wait
  - traffic light 按分数 + safety 决策
"""

from __future__ import annotations

import time

import pytest

from models.ai_trader_report import AIFactorMatrix, AITraderReport, AITradingPlan
from models.execution_plan import (
    ExecutionPlan, ExecutionScoreBreakdown, SafetyGateResult,
)
from models.fused_decision import DivergenceStats
from processors.signal_fusion import (
    _classify_consensus, _compute_final_score, _consensus_stars, _math_brief,
    _ai_brief, _merge_entry_params, fuse_decisions,
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _mk_plan(
    *,
    direction: str = "bullish",
    action: str = "long",
    score: float = 70.0,
    position_pct: float = 35.0,
    entry_low: float = 72100.0,
    entry_high: float = 72400.0,
    sl: float = 71500.0,
    tp1: float = 73800.0,
    tp2: float = 75200.0,
    rr: float = 2.3,
    safety_triggered: bool = False,
    safety_block: bool = False,
) -> ExecutionPlan:
    gates = SafetyGateResult(triggered=safety_triggered)
    if safety_block:
        gates.g5_blackswan = "block"
        gates.triggered = True
        gates.block_reason = "blackswan"
    elif safety_triggered:
        gates.g1_extreme_vol = "warn"
    return ExecutionPlan(
        coin="BTC",
        ts=int(time.time()),
        current_price=72500.0,
        regime="trend_up",
        regime_confidence=0.7,
        execution_score=score,
        traffic_light="green",
        headline=f"{action}",
        action=action,  # type: ignore[arg-type]
        direction=direction,  # type: ignore[arg-type]
        tier_hint="A",
        entry_zone_low=entry_low,
        entry_zone_high=entry_high,
        stop_loss=sl,
        tp1=tp1, tp2=tp2, rr_ratio=rr,
        position_size_pct=position_pct,
        breakdown=ExecutionScoreBreakdown(final_score=score),
        safety_gates=gates,
    )


def _mk_report(
    *,
    bias: str = "bullish",
    conviction: int = 70,
    direction: str = "long",
    entry_low: float = 72200.0,
    entry_high: float = 72500.0,
    sl: float = 71400.0,
    tp1: float = 73700.0,
    position_pct: float = 35.0,
    with_plan: bool = True,
) -> AITraderReport:
    plans: list[AITradingPlan] = []
    if with_plan:
        plans.append(AITradingPlan(
            priority=1,
            direction=direction,  # type: ignore[arg-type]
            entry_zone_low=entry_low,
            entry_zone_high=entry_high,
            stop_loss=sl,
            tp1=tp1, tp2=75100.0, rr_ratio=2.1,
            position_suggestion_pct=position_pct,
            conviction=conviction,
            tier_hint="A",
        ))
    return AITraderReport(
        coin="BTC",
        ts=int(time.time()),
        price_at_analysis=72500.0,
        model="deepseek-v4-flash",
        bias=bias,  # type: ignore[arg-type]
        conviction=conviction,
        trading_plans=plans,
        factor_matrix=AIFactorMatrix(sections=[], overall_bias=bias),  # type: ignore[arg-type]
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Consensus classification
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestConsensusClassify:
    def test_strong_consensus(self):
        plan = _mk_plan(score=80)
        report = _mk_report(conviction=80)
        m, a = _math_brief(plan), _ai_brief(report)
        assert _classify_consensus(m, a) == "strong"

    def test_agree_when_both_bullish_scores_moderate(self):
        plan = _mk_plan(score=65)
        report = _mk_report(conviction=65)
        assert _classify_consensus(_math_brief(plan), _ai_brief(report)) == "agree"

    def test_math_lead_when_ai_neutral(self):
        plan = _mk_plan(score=70)
        report = _mk_report(bias="neutral", conviction=40, direction="wait")
        assert _classify_consensus(_math_brief(plan), _ai_brief(report)) == "math_lead"

    def test_ai_lead_when_math_neutral(self):
        plan = _mk_plan(direction="neutral", action="wait", score=45)
        report = _mk_report(conviction=70)
        assert _classify_consensus(_math_brief(plan), _ai_brief(report)) == "ai_lead"

    def test_conflict_when_opposite(self):
        plan = _mk_plan(direction="bullish", action="long", score=70)
        report = _mk_report(bias="bearish", direction="short", conviction=65)
        assert _classify_consensus(_math_brief(plan), _ai_brief(report)) == "conflict"

    def test_both_neutral_is_both_wait(self):
        """P0-1 · 两方都观望必须归入 both_wait，不再误判为 agree。"""
        plan = _mk_plan(direction="neutral", action="wait", score=45)
        report = _mk_report(bias="neutral", direction="wait", conviction=40)
        assert _classify_consensus(_math_brief(plan), _ai_brief(report)) == "both_wait"

    def test_bias_same_but_ai_waits_downgrades_to_math_lead(self):
        """P0-1 · AI 有 bias 但建议 wait（未就绪）→ 不应归为 agree，应 math_lead。"""
        plan = _mk_plan(direction="bullish", action="long", score=70)
        report = _mk_report(bias="bullish", direction="wait", conviction=55)
        assert _classify_consensus(_math_brief(plan), _ai_brief(report)) == "math_lead"

    def test_bias_same_but_math_waits_downgrades_to_ai_lead(self):
        """P0-1 · 数学 bias=bullish 但 action=wait → ai_lead。"""
        plan = _mk_plan(direction="bullish", action="wait", score=55)
        report = _mk_report(bias="bullish", direction="long", conviction=70)
        assert _classify_consensus(_math_brief(plan), _ai_brief(report)) == "ai_lead"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Score computation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestFinalScore:
    def test_strong_takes_max(self):
        plan = _mk_plan(score=80)
        report = _mk_report(conviction=85)
        m, a = _math_brief(plan), _ai_brief(report)
        assert _compute_final_score(m, a, "strong") == 85.0

    def test_agree_weighted(self):
        plan = _mk_plan(score=70)
        report = _mk_report(conviction=60)
        m, a = _math_brief(plan), _ai_brief(report)
        score = _compute_final_score(m, a, "agree")
        # 0.55 * 70 + 0.45 * 60 = 65.5
        assert score == pytest.approx(65.5, abs=0.01)

    def test_math_lead_discounted(self):
        plan = _mk_plan(score=70)
        report = _mk_report(bias="neutral", conviction=40, direction="wait")
        m, a = _math_brief(plan), _ai_brief(report)
        assert _compute_final_score(m, a, "math_lead") == pytest.approx(59.5, abs=0.01)

    def test_conflict_penalty(self):
        plan = _mk_plan(score=70)
        report = _mk_report(bias="bearish", direction="short", conviction=60)
        m, a = _math_brief(plan), _ai_brief(report)
        # min(70, 60) - 10 = 50
        assert _compute_final_score(m, a, "conflict") == 50.0

    def test_score_clipped_to_0_100(self):
        plan = _mk_plan(score=10)
        report = _mk_report(conviction=10)
        m, a = _math_brief(plan), _ai_brief(report)
        score = _compute_final_score(m, a, "conflict")  # 10 - 10 = 0
        assert 0.0 <= score <= 100.0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Consensus stars
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestStars:
    def test_strong_small_delta_is_5(self):
        assert _consensus_stars("strong", 5) == 5

    def test_strong_big_delta_is_4(self):
        assert _consensus_stars("strong", 20) == 4

    def test_conflict_is_1(self):
        assert _consensus_stars("conflict", 10) == 1


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Entry param merge
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestEntryMerge:
    def test_agree_long_takes_stricter(self):
        plan = _mk_plan(entry_low=72100, entry_high=72400, sl=71500, tp1=73800)
        report = _mk_report(entry_low=72200, entry_high=72500, sl=71400, tp1=73500)
        params = _merge_entry_params(plan, report, "agree")
        assert params["entry_zone_low"] == 72200  # max(lows) for long
        assert params["entry_zone_high"] == 72400  # min(highs) for long
        assert params["stop_loss"] == 71500  # max (更严止损)
        assert params["tp1"] == 73500  # min (更保守)

    def test_conflict_returns_all_none(self):
        plan = _mk_plan()
        report = _mk_report(bias="bearish", direction="short")
        params = _merge_entry_params(plan, report, "conflict")
        assert all(v is None for v in params.values())

    def test_math_lead_uses_math(self):
        plan = _mk_plan(entry_low=72100, entry_high=72400)
        report = _mk_report(bias="neutral", direction="wait")
        params = _merge_entry_params(plan, report, "math_lead")
        assert params["entry_zone_low"] == 72100

    def test_ai_lead_uses_ai(self):
        plan = _mk_plan(direction="neutral", action="wait")
        report = _mk_report(entry_low=72200, entry_high=72500)
        params = _merge_entry_params(plan, report, "ai_lead")
        assert params["entry_zone_low"] == 72200


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# End-to-end fuse_decisions
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestFuseDecisions:
    def test_strong_consensus_returns_execute_green(self):
        plan = _mk_plan(score=80)
        report = _mk_report(conviction=80)
        decision = fuse_decisions("BTC", plan, report)
        assert decision.consensus_level == "strong"
        assert decision.recommended_action == "execute"
        assert decision.traffic_light == "green"
        assert decision.recommended_position_pct > 0
        assert decision.entry_zone_low is not None
        assert decision.stop_loss is not None
        assert decision.consensus_stars >= 4

    def test_agree_consensus_returns_execute(self):
        plan = _mk_plan(score=65)
        report = _mk_report(conviction=65)
        decision = fuse_decisions("BTC", plan, report)
        assert decision.consensus_level == "agree"
        assert decision.recommended_action == "execute"

    def test_math_lead_returns_reduce_size(self):
        plan = _mk_plan(score=70)
        report = _mk_report(bias="neutral", direction="wait", conviction=40)
        decision = fuse_decisions("BTC", plan, report)
        assert decision.consensus_level == "math_lead"
        assert decision.recommended_action == "reduce_size"

    def test_conflict_small_delta_waits(self):
        plan = _mk_plan(direction="bullish", action="long", score=60)
        report = _mk_report(bias="bearish", direction="short", conviction=55)
        decision = fuse_decisions("BTC", plan, report)
        assert decision.consensus_level == "conflict"
        assert decision.recommended_action == "wait"
        assert decision.recommended_position_pct == 0.0
        assert decision.entry_zone_low is None
        assert decision.traffic_light == "orange"
        assert decision.consensus_stars == 1
        assert decision.divergence_summary_cn

    def test_conflict_big_delta_reduces_toward_strong_side(self):
        plan = _mk_plan(score=85)  # 数学信心强
        report = _mk_report(bias="bearish", direction="short", conviction=45)
        decision = fuse_decisions("BTC", plan, report)
        assert decision.consensus_level == "conflict"
        assert decision.recommended_action == "reduce_size"
        assert decision.recommended_position_pct > 0

    def test_both_wait_forces_wait_no_fake_execute(self):
        """P0-1 · 双方 neutral/wait 时必须产生 wait（旧 bug 会 execute 20% 仓位）。"""
        plan = _mk_plan(direction="neutral", action="wait", score=45)
        report = _mk_report(bias="neutral", direction="wait", conviction=40)
        decision = fuse_decisions("BTC", plan, report)
        assert decision.consensus_level == "both_wait"
        assert decision.recommended_action == "wait"
        assert decision.recommended_position_pct == 0.0
        assert decision.entry_zone_low is None
        assert decision.entry_zone_high is None
        assert decision.stop_loss is None

    def test_safety_gate_block_forces_avoid(self):
        plan = _mk_plan(score=80, safety_block=True)
        report = _mk_report(conviction=80)
        decision = fuse_decisions("BTC", plan, report)
        assert decision.recommended_action == "avoid"
        assert decision.recommended_position_pct == 0.0
        assert decision.traffic_light == "red"
        assert decision.safety_gate_triggered is True

    def test_safety_gate_warn_downgrades_execute_to_reduce(self):
        plan = _mk_plan(score=80, safety_triggered=True)
        report = _mk_report(conviction=80)
        decision = fuse_decisions("BTC", plan, report)
        # safety warn（非 block） → execute 降为 reduce_size，仓位折半
        assert decision.recommended_action == "reduce_size"
        assert decision.recommended_position_pct > 0
        assert decision.safety_gate_triggered is True

    def test_divergence_stats_matched_on_conflict(self):
        plan = _mk_plan(direction="bullish", action="long", score=65)
        report = _mk_report(bias="bearish", direction="short", conviction=55)
        stats = [
            DivergenceStats(
                divergence_type="math_long_ai_short",
                sample_size=30,
                math_win_rate=0.58, ai_win_rate=0.32,
                avg_delta_pct_24h=1.2,
                winner_hint_cn="历史上数学引擎胜率更高",
            ),
        ]
        decision = fuse_decisions("BTC", plan, report, divergence_stats=stats)
        assert decision.historical_divergence is not None
        assert decision.historical_divergence.sample_size == 30

    def test_no_divergence_stats_on_agree(self):
        plan = _mk_plan(score=75)
        report = _mk_report(conviction=75)
        stats = [DivergenceStats(divergence_type="math_long_ai_short", sample_size=5)]
        decision = fuse_decisions("BTC", plan, report, divergence_stats=stats)
        assert decision.historical_divergence is None
        assert decision.divergence_summary_cn == ""

    def test_geo_overview_populates_flags(self):
        from models.geo_risk import GeoRiskOverview
        plan = _mk_plan(score=70)
        report = _mk_report(conviction=70)
        geo = GeoRiskOverview(
            overall_level=3, overall_label="CRISIS", overall_emoji="🟠",
            escalation_count_24h=1, has_blackswan_24h=False,
        )
        decision = fuse_decisions("BTC", plan, report, geo_overview=geo,
                                  active_themes_count=4)
        assert decision.geo_risk_overall_level == 3
        assert decision.geo_risk_label == "CRISIS"
        assert decision.active_themes_count == 4

    def test_headline_and_one_liner_populated(self):
        plan = _mk_plan(score=80)
        report = _mk_report(conviction=80)
        decision = fuse_decisions("BTC", plan, report)
        assert decision.headline
        assert decision.one_liner
        assert "共识" in decision.headline or "强共识" in decision.headline
