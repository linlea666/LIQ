"""Step 3 基础设施单测 · BaseStrategy / Registry / RegimeGate / MTFBias"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Optional

import pytest

from models.scalp_signal import (
    EvidenceItem,
    ScalpConfig,
    StrategyName,
)

from processors.scalp_signal.base_strategy import (
    BaseStrategy,
    StrategyCandidate,
    StrategyContext,
)
from processors.scalp_signal.mtf_bias import MTFBiasComputer
from processors.scalp_signal.regime_gate import RegimeGate, RegimeGateResult
from processors.scalp_signal.strategy_registry import StrategyRegistry


# ────────────────────────────────────────────────────────────────────────────
# 假策略（用于 registry / context / base_strategy 测试）
# ────────────────────────────────────────────────────────────────────────────

class FakeStrategy(BaseStrategy):
    name = StrategyName.A_SWEEP_RECLAIM
    display_name = "Fake A"
    suitable_regimes = {"range", "trend_up", "trend_down"}
    suitable_horizons = {30}

    def detect(self, ctx: StrategyContext) -> Optional[StrategyCandidate]:
        if ctx.bias_score > 0.3:
            return StrategyCandidate(
                direction="up",
                reference_price=78400.0,
                raw_strength=0.85,
                evidence=[EvidenceItem(dimension="Test", observation="多偏置触发", weight="high")],
                triggered_conditions=["bias>0.3"],
            )
        return None


class FakeStrategy2(BaseStrategy):
    name = StrategyName.B_CVD_DIVERGENCE
    display_name = "Fake B"
    suitable_regimes = {"range"}
    suitable_horizons = {10, 30, 60}

    def detect(self, ctx: StrategyContext) -> Optional[StrategyCandidate]:
        return None


# ────────────────────────────────────────────────────────────────────────────
# BaseStrategy.safe_attr / is_applicable
# ────────────────────────────────────────────────────────────────────────────

class TestBaseStrategy:
    def test_is_applicable_true(self):
        s = FakeStrategy()
        assert s.is_applicable("range", 30) is True
        assert s.is_applicable("trend_up", 30) is True

    def test_is_applicable_false_regime(self):
        s = FakeStrategy()
        assert s.is_applicable("squeeze", 30) is False
        assert s.is_applicable("extreme", 30) is False

    def test_is_applicable_false_horizon(self):
        s = FakeStrategy()
        assert s.is_applicable("range", 10) is False  # 仅支持 30

    def test_safe_attr_chain(self):
        obj = SimpleNamespace(
            ticker=SimpleNamespace(last=78400.0),
            empty=None,
        )
        assert BaseStrategy.safe_attr(obj, "ticker", "last") == 78400.0
        assert BaseStrategy.safe_attr(obj, "ticker", "missing", default=999) == 999
        assert BaseStrategy.safe_attr(obj, "empty", "anything", default="x") == "x"
        assert BaseStrategy.safe_attr(None, "anything", default=42) == 42


# ────────────────────────────────────────────────────────────────────────────
# StrategyRegistry
# ────────────────────────────────────────────────────────────────────────────

class TestStrategyRegistry:
    def test_register_and_get(self):
        reg = StrategyRegistry()
        s1 = FakeStrategy()
        reg.register(s1)
        assert reg.get(StrategyName.A_SWEEP_RECLAIM) is s1
        assert reg.get(StrategyName.B_CVD_DIVERGENCE) is None
        assert len(reg) == 1
        assert StrategyName.A_SWEEP_RECLAIM in reg

    def test_register_duplicate_raises(self):
        reg = StrategyRegistry()
        reg.register(FakeStrategy())
        with pytest.raises(ValueError, match="already registered"):
            reg.register(FakeStrategy())

    def test_all_preserves_order(self):
        reg = StrategyRegistry()
        s1 = FakeStrategy()
        s2 = FakeStrategy2()
        reg.register(s1)
        reg.register(s2)
        assert reg.all() == [s1, s2]
        assert reg.names() == [StrategyName.A_SWEEP_RECLAIM, StrategyName.B_CVD_DIVERGENCE]

    def test_enabled_for_filters_by_config_and_applicability(self):
        reg = StrategyRegistry()
        reg.register(FakeStrategy())
        reg.register(FakeStrategy2())
        cfg = ScalpConfig()

        # 默认全关 → 0
        result = reg.enabled_for(cfg, "range", 30)
        assert result == []

        # 开启 A → 应当返回 A
        cfg.strategies[StrategyName.A_SWEEP_RECLAIM].enabled = True
        result = reg.enabled_for(cfg, "range", 30)
        assert len(result) == 1 and result[0].name == StrategyName.A_SWEEP_RECLAIM

        # 开启 B 且 horizon=10 → A 不适用，B 适用
        cfg.strategies[StrategyName.B_CVD_DIVERGENCE].enabled = True
        result = reg.enabled_for(cfg, "range", 10)
        assert len(result) == 1 and result[0].name == StrategyName.B_CVD_DIVERGENCE

    def test_enabled_for_unsupported_regime_filtered(self):
        reg = StrategyRegistry()
        reg.register(FakeStrategy())  # 不含 squeeze
        cfg = ScalpConfig()
        cfg.strategies[StrategyName.A_SWEEP_RECLAIM].enabled = True

        assert reg.enabled_for(cfg, "squeeze", 30) == []
        assert len(reg.enabled_for(cfg, "range", 30)) == 1


# ────────────────────────────────────────────────────────────────────────────
# RegimeGate
# ────────────────────────────────────────────────────────────────────────────

class TestRegimeGate:
    def test_no_snapshot_blocks(self):
        state = SimpleNamespace(regime_snapshot=None, range_signal=None)
        result = RegimeGate().evaluate(state)
        assert result.allow is False
        assert result.skip_reason == "no_regime_snapshot"

    def test_extreme_blocks(self):
        state = SimpleNamespace(
            regime_snapshot=SimpleNamespace(regime="extreme", confidence=0.9),
            range_signal=None,
        )
        result = RegimeGate().evaluate(state)
        assert result.allow is False
        assert result.regime == "extreme"
        assert result.skip_reason == "regime_extreme_block"

    def test_high_vol_chop_blocks(self):
        state = SimpleNamespace(
            regime_snapshot=SimpleNamespace(regime="high_vol_chop", confidence=0.7),
            range_signal=None,
        )
        result = RegimeGate().evaluate(state)
        assert result.allow is False
        assert result.skip_reason == "regime_high_vol_chop_block"

    def test_range_allows(self):
        state = SimpleNamespace(
            regime_snapshot=SimpleNamespace(regime="range", confidence=0.85),
            range_signal=SimpleNamespace(price_position_pct=82.5),
        )
        result = RegimeGate().evaluate(state)
        assert result.allow is True
        assert result.regime == "range"
        assert result.range_position_pct == 82.5
        assert result.regime_confidence == 0.85

    def test_trend_up_allows(self):
        state = SimpleNamespace(
            regime_snapshot=SimpleNamespace(regime="trend_up", confidence=0.92),
            range_signal=None,
        )
        result = RegimeGate().evaluate(state)
        assert result.allow is True
        assert result.range_position_pct is None

    def test_squeeze_allows(self):
        """squeeze 不 block（V1 不上 squeeze 策略，但有些策略可能想观察 squeeze 边缘）"""
        state = SimpleNamespace(
            regime_snapshot=SimpleNamespace(regime="squeeze", confidence=0.6),
            range_signal=SimpleNamespace(price_position_pct=50),
        )
        result = RegimeGate().evaluate(state)
        assert result.allow is True

    def test_invalid_range_signal_field(self):
        """range_signal.price_position_pct 是奇怪类型时不抛错"""
        state = SimpleNamespace(
            regime_snapshot=SimpleNamespace(regime="range", confidence=0.8),
            range_signal=SimpleNamespace(price_position_pct="not_a_number"),
        )
        result = RegimeGate().evaluate(state)
        assert result.allow is True
        assert result.range_position_pct is None  # 容错为 None

    def test_custom_block_set(self):
        gate = RegimeGate(block_set=frozenset({"range"}))  # 反常配置：把 range 也 block
        state = SimpleNamespace(
            regime_snapshot=SimpleNamespace(regime="range", confidence=0.8),
            range_signal=None,
        )
        result = RegimeGate().evaluate(state)  # 默认 gate
        assert result.allow is True

        result2 = gate.evaluate(state)
        assert result2.allow is False
        assert result2.skip_reason == "regime_range_block"


# ────────────────────────────────────────────────────────────────────────────
# MTFBiasComputer
# ────────────────────────────────────────────────────────────────────────────

def _mk_state(
    *,
    scenario: Optional[str] = None,
    accepted_scenario: Optional[str] = None,
    operate_bias_1h: Optional[str] = None,
    direction_1d: Optional[str] = None,
    direction_1w: Optional[str] = None,
):
    """构造伪 state，便于 mtf_bias 测试"""
    market_action_report = None
    if scenario is not None or accepted_scenario is not None:
        stab = SimpleNamespace(accepted_scenario=accepted_scenario)
        market_action_report = SimpleNamespace(scenario=scenario, stability=stab)

    market_structure = None
    if operate_bias_1h is not None:
        market_structure = SimpleNamespace(operate_bias=operate_bias_1h)

    market_structure_1d = SimpleNamespace(direction=direction_1d) if direction_1d else None
    market_structure_1w = SimpleNamespace(direction=direction_1w) if direction_1w else None

    return SimpleNamespace(
        market_action_report=market_action_report,
        market_structure=market_structure,
        market_structure_1d=market_structure_1d,
        market_structure_1w=market_structure_1w,
    )


class TestMTFBias:
    def test_empty_state_neutral(self):
        state = _mk_state()
        result = MTFBiasComputer().compute(state, 30)
        assert result.bias_score == 0.0
        assert result.alignment == "neutral"
        assert "无可用上下文" in result.summary_cn

    def test_strong_bullish_aligned(self):
        state = _mk_state(
            scenario="trend_continuation_up",
            operate_bias_1h="long_only",
            direction_1d="bullish",
            direction_1w="bullish",
        )
        result = MTFBiasComputer().compute(state, 60)
        # 0.5 + 0.3 + 0.2 + 0.1 = 1.1 → clamp 到 1.0
        assert result.bias_score == 1.0
        assert result.alignment == "bullish_aligned"
        assert "强多" in result.summary_cn
        assert result.components["maa_1h"] == 0.5
        assert result.components["ms_1h"] == 0.3
        assert result.components["ms_1d"] == 0.2
        assert result.components["ms_1w"] == 0.1

    def test_strong_bearish_aligned(self):
        state = _mk_state(
            scenario="trend_continuation_down",
            operate_bias_1h="short_only",
            direction_1d="bearish",
        )
        result = MTFBiasComputer().compute(state, 30)
        # 30min horizon 不计入 1w
        assert result.bias_score == pytest.approx(-1.0, abs=1e-9)
        assert result.alignment == "bearish_aligned"
        assert "ms_1w" not in result.components

    def test_horizon_10_excludes_daily_weekly(self):
        state = _mk_state(
            scenario="range_bound",
            direction_1d="bullish",
            direction_1w="bullish",
        )
        result = MTFBiasComputer().compute(state, 10)
        assert "ms_1d" not in result.components
        assert "ms_1w" not in result.components

    def test_horizon_30_excludes_weekly(self):
        state = _mk_state(
            scenario="trend_continuation_up",
            direction_1d="bullish",
            direction_1w="bullish",
        )
        result = MTFBiasComputer().compute(state, 30)
        assert "ms_1d" in result.components
        assert "ms_1w" not in result.components

    def test_fake_breakout_inverts(self):
        """fake_breakout_up 应给负偏置（即将下行）"""
        state = _mk_state(scenario="fake_breakout_up")
        result = MTFBiasComputer().compute(state, 30)
        assert result.bias_score == -0.4
        assert result.alignment == "bearish_aligned"

    def test_exhaustion_top_bearish(self):
        state = _mk_state(scenario="exhaustion_top")
        result = MTFBiasComputer().compute(state, 30)
        assert result.bias_score == pytest.approx(-0.3, abs=1e-9)

    def test_mixed_components(self):
        """1h 看多、1d 看空 → mixed"""
        state = _mk_state(
            scenario="trend_continuation_up",   # +0.5
            direction_1d="bearish",              # -0.2
        )
        result = MTFBiasComputer().compute(state, 30)
        assert result.bias_score == pytest.approx(0.3, abs=1e-9)
        assert result.alignment == "mixed"

    def test_accepted_scenario_overrides_raw(self):
        """stability.accepted_scenario 优先于 raw scenario"""
        state = _mk_state(
            scenario="trend_continuation_up",       # +0.5
            accepted_scenario="trend_continuation_down",  # -0.5（防抖后）
        )
        result = MTFBiasComputer().compute(state, 30)
        assert result.bias_score == -0.5
        assert result.alignment == "bearish_aligned"

    def test_unknown_scenario_zero(self):
        state = _mk_state(scenario="some_new_unknown_scenario")
        result = MTFBiasComputer().compute(state, 30)
        assert result.components.get("maa_1h", 0) == 0.0

    def test_clamp_negative_overflow(self):
        """理论 max -1.1 应被 clamp 到 -1.0"""
        state = _mk_state(
            scenario="trend_continuation_down",  # -0.5
            operate_bias_1h="short_only",         # -0.3
            direction_1d="bearish",                # -0.2
            direction_1w="bearish",                # -0.1
        )
        result = MTFBiasComputer().compute(state, 60)
        assert result.bias_score == -1.0


# ────────────────────────────────────────────────────────────────────────────
# StrategyContext + 集成串
# ────────────────────────────────────────────────────────────────────────────

class TestIntegration:
    def test_context_to_detect_to_candidate(self):
        ctx = StrategyContext(
            state=SimpleNamespace(),
            coin="BTC",
            horizon_min=30,
            regime="range",
            range_position_pct=80.0,
            bias_score=0.45,
        )
        s = FakeStrategy()
        candidate = s.detect(ctx)
        assert candidate is not None
        assert candidate.direction == "up"
        assert candidate.raw_strength == 0.85

    def test_context_low_bias_no_trigger(self):
        ctx = StrategyContext(
            state=SimpleNamespace(), coin="BTC", horizon_min=30,
            regime="range", bias_score=0.1,
        )
        s = FakeStrategy()
        assert s.detect(ctx) is None
