"""L4 SignalSynthesizer 单测（D02 核心：候选信号 → ExecutionPlan）"""

from __future__ import annotations

import time

import pytest

from models.candidate_signal import CandidateSignal
from models.execution_plan import SafetyGateResult
from models.market_structure import MarketStructure
from processors.market_regime import compute_regime_from_state
from processors.signal_synthesizer import (
    _aggregate_price_params, _traffic_light_for, _vote_primary_direction,
    synthesize,
)


def _cs(**kw) -> CandidateSignal:
    defaults = dict(
        source="tracker_v2.bounced",
        source_id="id1",
        ts=int(time.time()),
        action="long",
        direction="bullish",
        anchor_price=72000,
        entry_price=72200,
        stop_loss=71400,
        tp1=73500,
        rr_ratio=2.5,
        confidence="A",
        score=72,
        provenance={"coin": "BTC", "cascade_risk": 0.1},
    )
    defaults.update(kw)
    return CandidateSignal(**defaults)


def _regime(direction: str = "bullish") -> "RegimeSnapshot":
    return compute_regime_from_state(
        coin="BTC", price=72500, atr_14=1800,
        boll_data={"upper": 74000, "middle": 72500, "lower": 71000},
        structure=MarketStructure(direction=direction, confidence=0.6) if direction != "none" else None,
    )


class TestVoteDirection:
    def test_bullish_majority(self):
        candidates = [
            _cs(direction="bullish", confidence="A", source="tracker_v2.bounced", source_id="a"),
            _cs(direction="bullish", confidence="B", source="tracker_v2.swept", source_id="b"),
            _cs(direction="bearish", confidence="C", source="levels.sniper", source_id="c"),
        ]
        direction, aligned = _vote_primary_direction(candidates)
        assert direction == "bullish"
        assert len(aligned) >= 2

    def test_bearish_majority(self):
        candidates = [
            _cs(direction="bearish", confidence="A", source_id="a", action="short"),
            _cs(direction="bullish", confidence="C", source_id="b"),
        ]
        direction, _ = _vote_primary_direction(candidates)
        assert direction == "bearish"

    def test_empty_neutral(self):
        direction, aligned = _vote_primary_direction([])
        assert direction == "neutral"
        assert aligned == []


class TestAggregatePriceParams:
    def test_entry_zone_spans_min_max(self):
        aligned = [
            _cs(entry_price=72200, source_id="a"),
            _cs(entry_price=72500, source_id="b"),
            _cs(entry_price=72000, source_id="c"),
        ]
        p = _aggregate_price_params(aligned)
        assert p["entry_low"] == 72000
        assert p["entry_high"] == 72500

    def test_sl_uses_min_for_long(self):
        aligned = [
            _cs(action="long", stop_loss=71400, source_id="a"),
            _cs(action="long", stop_loss=71000, source_id="b"),
        ]
        p = _aggregate_price_params(aligned)
        assert p["stop_loss"] == 71000  # 做多取最低 SL 更严格

    def test_rr_recomputed(self):
        aligned = [
            _cs(action="long", entry_price=72000, stop_loss=71000, tp1=74000),
        ]
        p = _aggregate_price_params(aligned)
        assert p["rr_ratio"] == 2.0  # (74000-72000)/(72000-71000)


class TestTrafficLight:
    def test_green(self):
        assert _traffic_light_for(80, False) == "green"

    def test_yellow(self):
        assert _traffic_light_for(65, False) == "yellow"

    def test_orange(self):
        assert _traffic_light_for(50, False) == "orange"

    def test_red_low_score(self):
        assert _traffic_light_for(30, False) == "red"

    def test_safety_triggered_downgrades(self):
        assert _traffic_light_for(80, True) == "orange"
        assert _traffic_light_for(40, True) == "red"


class TestSynthesizeEndToEnd:
    def test_bullish_a_tier_green(self):
        regime = _regime("bullish")
        candidates = [_cs(confidence="A")]
        plan = synthesize(
            coin="BTC", candidates=candidates, regime=regime,
            current_price=72500,
        )
        assert plan.action == "long"
        assert plan.direction == "bullish"
        assert plan.tier_hint == "A"
        assert plan.execution_score >= 65
        assert plan.traffic_light in ("green", "yellow")
        assert plan.entry_zone_low is not None
        assert plan.position_size_pct is not None and plan.position_size_pct > 0
        assert plan.headline != ""

    def test_safety_block_forces_wait(self):
        regime = _regime("bullish")
        candidates = [_cs(confidence="A")]
        safety = SafetyGateResult(
            g1_extreme_vol="block", triggered=True, block_reason="ATR 极端",
        )
        plan = synthesize(
            coin="BTC", candidates=candidates, regime=regime,
            current_price=72500, safety_gates=safety,
        )
        assert plan.safety_gates.triggered is True
        assert plan.execution_score < 50  # -40 safety_gate_delta
        assert plan.traffic_light in ("red", "orange")
        assert plan.position_size_pct == 0

    def test_empty_candidates_waits(self):
        regime = _regime("bullish")
        plan = synthesize(
            coin="BTC", candidates=[], regime=regime, current_price=72500,
        )
        assert plan.action == "wait"
        assert plan.direction == "neutral"

    def test_cascade_risk_penalty(self):
        regime = _regime("bullish")
        low_risk = synthesize(
            coin="BTC", current_price=72500, regime=regime,
            candidates=[_cs(confidence="A", provenance={"coin": "BTC", "cascade_risk": 0.0})],
        )
        high_risk = synthesize(
            coin="BTC", current_price=72500, regime=regime,
            candidates=[_cs(confidence="A", provenance={"coin": "BTC", "cascade_risk": 0.8})],
        )
        assert high_risk.execution_score < low_risk.execution_score
        assert high_risk.breakdown.cascade_penalty < 0

    def test_corroboration_bonus_multi_source(self):
        regime = _regime("bullish")
        single = synthesize(
            coin="BTC", current_price=72500, regime=regime,
            candidates=[_cs(source="tracker_v2.bounced", source_id="a", confidence="A")],
        )
        multi = synthesize(
            coin="BTC", current_price=72500, regime=regime,
            candidates=[
                _cs(source="tracker_v2.bounced", source_id="a", confidence="A"),
                _cs(source="range_signal.observe", source_id="b", confidence="B"),
                _cs(source="levels.sniper", source_id="c", confidence="B"),
            ],
        )
        assert multi.breakdown.corroboration_bonus > single.breakdown.corroboration_bonus

    def test_regime_extreme_caps_position(self):
        # 极端 regime 下仓位要显著压缩
        regime_extreme = compute_regime_from_state(
            coin="BTC", price=72500, atr_14=8000,  # atr_pct=11%
            structure=MarketStructure(direction="bullish", confidence=0.6),
        )
        plan = synthesize(
            coin="BTC", current_price=72500, regime=regime_extreme,
            candidates=[_cs(confidence="A")],
        )
        assert regime_extreme.regime == "extreme"
        # regime_alignment 会被推荐权重 0.2 打到 -15
        assert plan.breakdown.regime_alignment <= -10
        # 仓位会被 regime 压缩
        assert plan.position_size_pct is not None and plan.position_size_pct < 20
