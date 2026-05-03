"""Step 4 单测 · ConfidenceScorer + VetoGate"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from models.scalp_signal import EvidenceItem, ScalpDirection, StrategyName

from processors.scalp_signal.base_strategy import StrategyCandidate, StrategyContext
from processors.scalp_signal.confidence_scorer import ConfidenceScorer
from processors.scalp_signal.veto_gate import VetoGate


def _candidate(
    *, direction: ScalpDirection = "up",
    raw: float = 0.85, ref: float = 78400.0,
) -> StrategyCandidate:
    return StrategyCandidate(
        direction=direction,
        reference_price=ref,
        raw_strength=raw,
        evidence=[EvidenceItem(dimension="Test", observation="x", weight="high")],
        triggered_conditions=["test"],
    )


def _ctx(
    *, state, bias: float = 0.4, bias_components: dict = None,
    direction_aware: bool = True,
) -> StrategyContext:
    return StrategyContext(
        state=state,
        coin="BTC",
        horizon_min=30,
        regime="range",
        range_position_pct=80.0,
        bias_score=bias,
        bias_components=bias_components or {"maa_1h": 0.4},
    )


def _state(
    *,
    now: int,
    ticker_age: int = 5,
    cvd_age: int = 30,
    candle_age_sec: int = 300,         # 5min ago，在 5s ~ 16min 范围内
    kl_levels: list | None = None,
    regime_changed_age: int = 600,     # 10min（已稳定）
    regime: str = "range",
):
    return SimpleNamespace(
        ticker=SimpleNamespace(ts=now - ticker_age, last=78400.0),
        cvd_contract=SimpleNamespace(ts=now - cvd_age),
        candles_15m=[SimpleNamespace(ts=now - candle_age_sec)],
        key_level_snapshot_v2=(
            SimpleNamespace(levels=kl_levels) if kl_levels is not None else None
        ),
        regime_snapshot=SimpleNamespace(
            regime=regime,
            confidence=0.85,
            regime_changed_at=now - regime_changed_age,
        ),
    )


# ════════════════════════════════════════════════════════════════════════════
# ConfidenceScorer
# ════════════════════════════════════════════════════════════════════════════

class TestConfidenceScorer:
    def test_basic_score_in_range(self):
        now = int(time.time())
        s = _state(now=now)
        ctx = _ctx(state=s, bias=0.4)
        result = ConfidenceScorer().score(_candidate(), ctx, now_ts=now)
        assert 0 <= result.confidence <= 100
        # P0-2: hit_probability 默认 None（未注入 calibration_lookup）
        assert result.hit_probability is None
        assert result.hit_probability_source == "uncalibrated"
        # raw_strength=0.85 + bias=+0.4 + 冷启动 0.55 winrate → 应该 60+
        assert result.confidence >= 60

    def test_max_confidence_perfect_inputs(self):
        now = int(time.time())
        s = _state(
            now=now, ticker_age=2, cvd_age=10, candle_age_sec=60,
            kl_levels=[SimpleNamespace(price=78420, final_score=95, strength_tier="S",
                                       is_stale=False)],
        )
        ctx = _ctx(state=s, bias=1.0, bias_components={"maa_1h": 0.5, "ms_1h": 0.3, "ms_1d": 0.2})
        # P0-3 样本量充足时，0.85 winrate 完全生效
        result = ConfidenceScorer().score(_candidate(raw=1.0), ctx,
                                          historical_winrate=0.85,
                                          historical_winrate_sample_size=200,
                                          now_ts=now)
        # 所有因子接近上限：0.4 + 0.25 + 0.15×0.95 + 0.10 + 0.10×0.85 = 0.97 → 97
        assert result.confidence >= 95
        # P0-2: 没注入 calibration_lookup → hit_probability=None
        assert result.hit_probability is None

    def test_hit_probability_calibrated_when_lookup_provided(self):
        """P0-2: 提供 calibration_lookup 且样本足够 → hit_probability 来自校准曲线"""
        now = int(time.time())
        s = _state(now=now)
        ctx = _ctx(state=s, bias=0.4)
        from models.scalp_signal import StrategyName
        captured = {}

        def lookup(strategy: StrategyName, conf: int) -> tuple[float, int]:
            captured["called"] = (strategy, conf)
            return (0.62, 50)  # 命中率 0.62, 样本量 50

        result = ConfidenceScorer().score(
            _candidate(), ctx,
            strategy_name=StrategyName.A_SWEEP_RECLAIM,
            calibration_lookup=lookup, now_ts=now,
        )
        assert "called" in captured
        assert result.hit_probability == 0.62
        assert result.hit_probability_source == "calibrated"
        assert result.calibration_sample_size == 50

    def test_hit_probability_uncalibrated_when_low_samples(self):
        """P0-2: 样本量 < CALIBRATION_MIN_BUCKET_SAMPLES (30) → uncalibrated"""
        now = int(time.time())
        s = _state(now=now)
        ctx = _ctx(state=s, bias=0.4)
        from models.scalp_signal import StrategyName

        def lookup(_s, _c) -> tuple[float, int]:
            return (0.62, 10)  # 样本不足

        result = ConfidenceScorer().score(
            _candidate(), ctx,
            strategy_name=StrategyName.A_SWEEP_RECLAIM,
            calibration_lookup=lookup, now_ts=now,
        )
        assert result.hit_probability is None
        assert result.hit_probability_source == "uncalibrated"
        # calibration_sample_size 仍记录实际桶内样本量（10），便于前端透明展示

    def test_alignment_factor_inverts_for_down_direction(self):
        """direction=down + bias 强负 → 高对齐分"""
        now = int(time.time())
        s = _state(now=now)
        ctx = _ctx(state=s, bias=-0.8)
        result = ConfidenceScorer().score(_candidate(direction="down"), ctx, now_ts=now)
        assert result.factor_breakdown.multi_tf_alignment >= 0.85

    def test_alignment_factor_zero_when_opposite(self):
        """direction=up + bias 强负 → 对齐分接近 0"""
        now = int(time.time())
        s = _state(now=now)
        ctx = _ctx(state=s, bias=-0.8)
        result = ConfidenceScorer().score(_candidate(direction="up"), ctx, now_ts=now)
        assert result.factor_breakdown.multi_tf_alignment < 0.15

    def test_kl_quality_no_levels(self):
        now = int(time.time())
        s = _state(now=now, kl_levels=[])
        ctx = _ctx(state=s)
        result = ConfidenceScorer().score(_candidate(), ctx, now_ts=now)
        assert result.factor_breakdown.key_level_quality == 0.0

    def test_kl_quality_picks_nearest_in_1pct(self):
        now = int(time.time())
        s = _state(now=now, kl_levels=[
            SimpleNamespace(price=78420, final_score=80, strength_tier="A", is_stale=False),
            SimpleNamespace(price=85000, final_score=95, strength_tier="S", is_stale=False),  # 太远
        ])
        ctx = _ctx(state=s)
        result = ConfidenceScorer().score(_candidate(ref=78400), ctx, now_ts=now)
        # 应该选 78420 那个，final_score=80 → 0.8
        assert result.factor_breakdown.key_level_quality == pytest.approx(0.8, abs=1e-6)

    def test_kl_quality_excludes_far_levels(self):
        now = int(time.time())
        s = _state(now=now, kl_levels=[
            SimpleNamespace(price=80000, final_score=95, strength_tier="S", is_stale=False),  # 远
        ])
        ctx = _ctx(state=s)
        result = ConfidenceScorer().score(_candidate(ref=78400), ctx, now_ts=now)
        # 距离 ~2%，超出 1% 阈值
        assert result.factor_breakdown.key_level_quality == 0.0

    def test_kl_stale_decays(self):
        now = int(time.time())
        s = _state(now=now, kl_levels=[
            SimpleNamespace(price=78420, final_score=80, strength_tier="A", is_stale=True),
        ])
        ctx = _ctx(state=s)
        result = ConfidenceScorer().score(_candidate(ref=78400), ctx, now_ts=now)
        # 0.8 × 0.7 = 0.56
        assert result.factor_breakdown.key_level_quality == pytest.approx(0.56, abs=1e-6)

    def test_freshness_three_sources_ok(self):
        now = int(time.time())
        s = _state(now=now)  # 默认所有源都新鲜
        ctx = _ctx(state=s)
        result = ConfidenceScorer().score(_candidate(), ctx, now_ts=now)
        assert result.factor_breakdown.data_freshness == 1.0

    def test_freshness_one_source_stale(self):
        now = int(time.time())
        s = _state(now=now, ticker_age=120)  # ticker 太旧
        ctx = _ctx(state=s)
        result = ConfidenceScorer().score(_candidate(), ctx, now_ts=now)
        assert result.factor_breakdown.data_freshness == 0.7

    def test_freshness_all_stale(self):
        now = int(time.time())
        s = _state(now=now, ticker_age=999, cvd_age=999, candle_age_sec=99999)
        ctx = _ctx(state=s)
        result = ConfidenceScorer().score(_candidate(), ctx, now_ts=now)
        assert result.factor_breakdown.data_freshness == 0.0

    def test_historical_winrate_default_cold_start(self):
        """P0-3 改造：不传 winrate / 样本量为 0 → 完全用默认 0.55"""
        now = int(time.time())
        s = _state(now=now)
        ctx = _ctx(state=s)
        result = ConfidenceScorer().score(_candidate(), ctx, now_ts=now)
        assert result.factor_breakdown.historical_winrate == 0.55
        assert result.factor_breakdown.historical_winrate_sample_size == 0
        assert result.factor_breakdown.historical_winrate_blended_with_default is True

    def test_historical_winrate_full_trust_at_100_samples(self):
        """P0-3：样本量 ≥ 100 时完全采用真实命中率（不再 blending）"""
        now = int(time.time())
        s = _state(now=now)
        ctx = _ctx(state=s)
        result = ConfidenceScorer().score(
            _candidate(), ctx,
            historical_winrate=0.80, historical_winrate_sample_size=100,
            now_ts=now,
        )
        assert result.factor_breakdown.historical_winrate == pytest.approx(0.80, abs=1e-6)
        assert result.factor_breakdown.historical_winrate_blended_with_default is False

    def test_historical_winrate_blended_at_50_samples(self):
        """P0-3：样本量 50 → 真实 0.80 × 0.5 + 默认 0.55 × 0.5 = 0.675"""
        now = int(time.time())
        s = _state(now=now)
        ctx = _ctx(state=s)
        result = ConfidenceScorer().score(
            _candidate(), ctx,
            historical_winrate=0.80, historical_winrate_sample_size=50,
            now_ts=now,
        )
        assert result.factor_breakdown.historical_winrate == pytest.approx(0.675, abs=1e-6)
        assert result.factor_breakdown.historical_winrate_blended_with_default is True

    def test_historical_winrate_clamped(self):
        """P0-3：超界仍被 [0, 1] 截断（极端样本量充足时）"""
        now = int(time.time())
        s = _state(now=now)
        ctx = _ctx(state=s)
        result = ConfidenceScorer().score(
            _candidate(), ctx,
            historical_winrate=1.5, historical_winrate_sample_size=200,
            now_ts=now,
        )
        assert result.factor_breakdown.historical_winrate == 1.0
        result2 = ConfidenceScorer().score(
            _candidate(), ctx,
            historical_winrate=-0.5, historical_winrate_sample_size=200,
            now_ts=now,
        )
        assert result2.factor_breakdown.historical_winrate == 0.0

    def test_extra_evidence_emitted(self):
        """打分应附带 MTF / KL / DataFreshness 三类 extra evidence"""
        now = int(time.time())
        s = _state(now=now, kl_levels=[
            SimpleNamespace(price=78420, final_score=80, strength_tier="A", is_stale=False),
        ])
        ctx = _ctx(state=s, bias=0.5)
        result = ConfidenceScorer().score(_candidate(), ctx, now_ts=now)
        dims = {ev.dimension for ev in result.extra_evidence}
        assert "MTF" in dims
        assert "KeyLevel" in dims
        assert "DataFreshness" in dims

    def test_confidence_monotone(self):
        """confidence 越高，输入条件越好（hit_probability P0-2 后由 calibration 决定）"""
        now = int(time.time())
        s = _state(now=now)
        ctx_low = _ctx(state=s, bias=-0.3)
        ctx_high = _ctx(state=s, bias=0.8)
        r_low = ConfidenceScorer().score(_candidate(raw=0.3), ctx_low, now_ts=now)
        r_high = ConfidenceScorer().score(_candidate(raw=1.0), ctx_high, now_ts=now)
        assert r_low.confidence < r_high.confidence
        # hit_probability 在两者都没注入 lookup 时均为 None，不再做单调性
        assert r_low.hit_probability is None
        assert r_high.hit_probability is None


# ════════════════════════════════════════════════════════════════════════════
# VetoGate
# ════════════════════════════════════════════════════════════════════════════

class TestVetoGate:
    def test_all_pass_baseline(self):
        now = int(time.time())
        s = _state(now=now)
        ctx = _ctx(state=s, bias=0.3)
        result = VetoGate().evaluate(_candidate(), ctx, now_ts=now)
        assert result.passed is True
        assert result.reason_blocked is None
        assert "bar_closed" in result.reasons_passed
        assert "ticker_fresh" in result.reasons_passed
        assert "cooldown_clear" in result.reasons_passed
        assert "no_blackswan" in result.reasons_passed
        assert "bias_consistent" in result.reasons_passed
        assert "regime_stable" in result.reasons_passed

    def test_block_no_15m_candles(self):
        now = int(time.time())
        s = _state(now=now)
        s.candles_15m = []
        ctx = _ctx(state=s)
        result = VetoGate().evaluate(_candidate(), ctx, now_ts=now)
        assert result.passed is False
        assert result.reason_blocked == "bar_not_closed"

    def test_block_15m_too_old(self):
        now = int(time.time())
        s = _state(now=now, candle_age_sec=20 * 60)  # 20min 前的 K，超过 16min
        ctx = _ctx(state=s)
        result = VetoGate().evaluate(_candidate(), ctx, now_ts=now)
        assert result.passed is False
        assert result.reason_blocked == "bar_not_closed"

    def test_block_15m_too_fresh(self):
        """K 线刚开盘 (< 5s)，正在剧烈跳动 → 拒绝"""
        now = int(time.time())
        s = _state(now=now, candle_age_sec=2)  # 2s 前
        ctx = _ctx(state=s)
        result = VetoGate().evaluate(_candidate(), ctx, now_ts=now)
        assert result.passed is False
        assert result.reason_blocked == "bar_not_closed"

    def test_block_ticker_stale(self):
        now = int(time.time())
        s = _state(now=now, ticker_age=120)  # 120s 旧
        ctx = _ctx(state=s)
        result = VetoGate().evaluate(_candidate(), ctx, now_ts=now)
        assert result.passed is False
        assert result.reason_blocked == "ticker_stale"

    def test_block_cooldown(self):
        now = int(time.time())
        s = _state(now=now)
        ctx = _ctx(state=s)
        # 上次信号在 30 min 前，cooldown=60min → 拒绝
        result = VetoGate().evaluate(
            _candidate(), ctx, last_signal_ts=now - 1800,
            cooldown_sec=3600, now_ts=now,
        )
        assert result.passed is False
        assert result.reason_blocked == "cooldown"

    def test_cooldown_clear_after_window(self):
        now = int(time.time())
        s = _state(now=now)
        ctx = _ctx(state=s)
        # 上次信号在 90min 前，cooldown=60min → 通过
        result = VetoGate().evaluate(
            _candidate(), ctx, last_signal_ts=now - 5400,
            cooldown_sec=3600, now_ts=now,
        )
        assert result.passed is True

    def test_block_blackswan_active(self):
        now = int(time.time())
        s = _state(now=now)
        ctx = _ctx(state=s)
        result = VetoGate().evaluate(
            _candidate(), ctx,
            blackswan_active=True, blackswan_age_sec=600,  # 10min 前的黑天鹅
            now_ts=now,
        )
        assert result.passed is False
        assert result.reason_blocked == "blackswan_active"

    def test_blackswan_outside_window_clears(self):
        now = int(time.time())
        s = _state(now=now)
        ctx = _ctx(state=s)
        # 60min 前的黑天鹅，超过 30min 窗口 → 通过
        result = VetoGate().evaluate(
            _candidate(), ctx,
            blackswan_active=True, blackswan_age_sec=3600,
            now_ts=now,
        )
        assert result.passed is True

    def test_block_bias_opposite_strong(self):
        now = int(time.time())
        s = _state(now=now)
        # candidate=up + bias=-0.6 → 强反向
        ctx = _ctx(state=s, bias=-0.6)
        result = VetoGate().evaluate(_candidate(direction="up"), ctx, now_ts=now)
        assert result.passed is False
        assert result.reason_blocked == "bias_opposite"

    def test_bias_opposite_below_threshold_pass(self):
        now = int(time.time())
        s = _state(now=now)
        # candidate=up + bias=-0.3 → 反向但不够强（阈值 0.5）
        ctx = _ctx(state=s, bias=-0.3)
        result = VetoGate().evaluate(_candidate(direction="up"), ctx, now_ts=now)
        assert result.passed is True

    def test_block_regime_just_changed(self):
        now = int(time.time())
        s = _state(now=now, regime_changed_age=120)  # 2min 前刚切
        ctx = _ctx(state=s)
        result = VetoGate().evaluate(_candidate(), ctx, now_ts=now)
        assert result.passed is False
        assert result.reason_blocked == "regime_unstable"

    def test_no_regime_snapshot_blocks(self):
        now = int(time.time())
        s = _state(now=now)
        s.regime_snapshot = None
        ctx = _ctx(state=s)
        result = VetoGate().evaluate(_candidate(), ctx, now_ts=now)
        assert result.passed is False
        assert result.reason_blocked == "regime_unstable"

    def test_first_fail_short_circuits(self):
        """同时多个 fail 时，应该返回第一个（按检查顺序）"""
        now = int(time.time())
        s = _state(now=now, ticker_age=120, regime_changed_age=60)  # 都 fail
        s.candles_15m = []  # 这个先 fail
        ctx = _ctx(state=s, bias=-0.6)  # 这个也 fail
        result = VetoGate().evaluate(_candidate(direction="up"), ctx, now_ts=now)
        assert result.passed is False
        assert result.reason_blocked == "bar_not_closed"  # 第一项

    def test_blackswan_active_unknown_age_blocks(self):
        """blackswan active=True 但没给 age → 保守拒绝"""
        now = int(time.time())
        s = _state(now=now)
        ctx = _ctx(state=s)
        result = VetoGate().evaluate(
            _candidate(), ctx, blackswan_active=True, now_ts=now,
        )
        assert result.passed is False
        assert result.reason_blocked == "blackswan_active"
