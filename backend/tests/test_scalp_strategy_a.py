"""策略 A · Sweep & Reclaim 单测"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from models.market import CandleData
from models.scalp_signal import StrategyName
from processors.scalp_signal.base_strategy import StrategyContext
from processors.scalp_signal.strategy_a_sweep import (
    KL_FINAL_SCORE_MIN,
    KL_NEAR_PCT_MAX,
    SweepReclaimStrategy,
)


# ────────────────────────────────────────────────────────────────────────────
# K 线工厂（生成 pin bar / engulfing / 普通 K）
# ────────────────────────────────────────────────────────────────────────────

def _bullish_pin(price: float = 78400, ts: int = 0) -> CandleData:
    """看涨锤子：长下影、小实体（实体 ≤ 33%，下影 ≥ 2× 实体）

    open=78410, close=78420, high=78425, low=78380
    body=10, total=45, lower_wick=30, upper_wick=5
    body/total ≈ 22% ≤ 33%, lower_wick(30) ≥ body(10)*2 ✓
    """
    return CandleData(
        coin="BTC", ts=ts,
        o=price + 10, h=price + 25, l=price - 20, c=price + 20,
    )


def _bearish_pin(price: float = 78400, ts: int = 0) -> CandleData:
    """看跌射击之星：长上影"""
    return CandleData(
        coin="BTC", ts=ts,
        o=price - 10, h=price + 20, l=price - 25, c=price - 20,
    )


def _normal_candle(price: float = 78400, ts: int = 0) -> CandleData:
    """普通 K（无形态）"""
    return CandleData(
        coin="BTC", ts=ts,
        o=price - 5, h=price + 8, l=price - 8, c=price + 5,
    )


def _state(
    *,
    last_price: float = 78400.0,
    candles: list[CandleData] | None = None,
    levels: list | None = None,
):
    return SimpleNamespace(
        ticker=SimpleNamespace(last=last_price, ts=0),
        candles_15m=candles or [_normal_candle(), _normal_candle()],
        key_level_snapshot_v2=(
            SimpleNamespace(levels=levels) if levels is not None else None
        ),
    )


def _ctx(state) -> StrategyContext:
    return StrategyContext(
        state=state, coin="BTC", horizon_min=30,
        regime="range", range_position_pct=20.0, bias_score=0.3,
    )


def _kl(
    *, side: str = "support", state: str = "swept", price: float = 78380,
    final_score: float = 80, strength_tier: str = "A",
    bounce_quality: str = "", sweep_usd: float = 5_000_000,
):
    return SimpleNamespace(
        side=side, state=state, price=price,
        final_score=final_score, strength_tier=strength_tier,
        bounce_quality=bounce_quality, sweep_usd=sweep_usd,
    )


# ════════════════════════════════════════════════════════════════════════════
# 触发逻辑
# ════════════════════════════════════════════════════════════════════════════

class TestStrategyAMetadata:
    def test_class_constants(self):
        s = SweepReclaimStrategy()
        assert s.name == StrategyName.A_SWEEP_RECLAIM
        assert "扫单" in s.display_name
        assert s.suitable_regimes == {"trend_up", "trend_down", "range"}
        assert s.suitable_horizons == {30, 60}

    def test_is_applicable(self):
        s = SweepReclaimStrategy()
        assert s.is_applicable("range", 30) is True
        assert s.is_applicable("trend_up", 60) is True
        assert s.is_applicable("squeeze", 30) is False
        assert s.is_applicable("range", 10) is False  # 不支持 10
        assert s.is_applicable("range", 60) is True


class TestTriggerConditions:
    def test_perfect_long_setup_bullish_signal(self):
        """支撑被扫破后回收 + 锤子线 → 信号 up"""
        state = _state(
            last_price=78400.0,
            candles=[_normal_candle(), _bullish_pin(78400, ts=1)],
            levels=[_kl(side="support", state="swept", price=78380, final_score=85)],
        )
        s = SweepReclaimStrategy()
        candidate = s.detect(_ctx(state))
        assert candidate is not None
        assert candidate.direction == "up"
        assert candidate.reference_price == 78400.0
        assert 0.5 < candidate.raw_strength <= 1.0

    def test_perfect_short_setup_bearish_signal(self):
        """阻力被扫破后回收 + 射击之星 → 信号 down"""
        state = _state(
            last_price=78400.0,
            candles=[_normal_candle(), _bearish_pin(78400, ts=1)],
            levels=[_kl(side="resistance", state="swept", price=78420, final_score=85)],
        )
        s = SweepReclaimStrategy()
        candidate = s.detect(_ctx(state))
        assert candidate is not None
        assert candidate.direction == "down"

    def test_fake_break_state_also_triggers(self):
        """state=fake_break 也属于 eligible"""
        state = _state(
            last_price=78400.0,
            candles=[_normal_candle(), _bullish_pin(78400, ts=1)],
            levels=[_kl(side="support", state="fake_break", price=78380, final_score=80)],
        )
        candidate = SweepReclaimStrategy().detect(_ctx(state))
        assert candidate is not None
        assert "fake_break" in candidate.extra_data["kl_state"]


class TestRejectConditions:
    def test_no_kl_snapshot(self):
        state = _state(
            candles=[_normal_candle(), _bullish_pin()],
            levels=None,
        )
        assert SweepReclaimStrategy().detect(_ctx(state)) is None

    def test_no_kl_levels(self):
        state = _state(
            candles=[_normal_candle(), _bullish_pin()],
            levels=[],
        )
        assert SweepReclaimStrategy().detect(_ctx(state)) is None

    def test_kl_state_idle_rejected(self):
        """state=idle 不在 eligible 集合中"""
        state = _state(
            candles=[_normal_candle(), _bullish_pin(78400)],
            levels=[_kl(side="support", state="idle", price=78380, final_score=80)],
        )
        assert SweepReclaimStrategy().detect(_ctx(state)) is None

    def test_kl_state_bounced_rejected(self):
        """state=bounced 已经反弹完，不再适合（信号晚到）"""
        state = _state(
            candles=[_normal_candle(), _bullish_pin(78400)],
            levels=[_kl(side="support", state="bounced", price=78380, final_score=80)],
        )
        assert SweepReclaimStrategy().detect(_ctx(state)) is None

    def test_kl_too_far_rejected(self):
        """KL 距离 > 0.5% 拒绝"""
        state = _state(
            last_price=78400.0,
            candles=[_normal_candle(), _bullish_pin(78400)],
            # 78380 → 距 78400 = 0.025%（OK），改成 77000 → 1.79% 远
            levels=[_kl(side="support", state="swept", price=77000, final_score=85)],
        )
        assert SweepReclaimStrategy().detect(_ctx(state)) is None

    def test_kl_low_score_rejected(self):
        """final_score < 50 拒绝"""
        state = _state(
            last_price=78400.0,
            candles=[_normal_candle(), _bullish_pin(78400)],
            levels=[_kl(side="support", state="swept", price=78380, final_score=40)],
        )
        assert SweepReclaimStrategy().detect(_ctx(state)) is None

    def test_no_pattern_rejected(self):
        """K 线无反转形态 → 拒绝"""
        state = _state(
            candles=[_normal_candle(), _normal_candle()],
            levels=[_kl(side="support", state="swept", price=78380, final_score=85)],
        )
        assert SweepReclaimStrategy().detect(_ctx(state)) is None

    def test_pattern_side_mismatch_rejected(self):
        """支撑位但出现的是看跌形态（射击之星）→ 形态检测会返回 not found → 拒绝"""
        state = _state(
            last_price=78400.0,
            candles=[_normal_candle(), _bearish_pin(78400)],  # 看跌
            levels=[_kl(side="support", state="swept", price=78380, final_score=85)],
        )
        assert SweepReclaimStrategy().detect(_ctx(state)) is None

    def test_no_ticker_rejected(self):
        state = _state(candles=[_normal_candle(), _bullish_pin()],
                       levels=[_kl(side="support", state="swept", final_score=80)])
        state.ticker = None
        assert SweepReclaimStrategy().detect(_ctx(state)) is None

    def test_too_few_candles_rejected(self):
        state = _state(
            candles=[_bullish_pin(78400)],  # 只有 1 根，需要 ≥ 2
            levels=[_kl(side="support", state="swept", price=78380, final_score=85)],
        )
        assert SweepReclaimStrategy().detect(_ctx(state)) is None


class TestRawStrength:
    def test_high_score_kl_higher_strength(self):
        """KL final_score 越高，raw_strength 越高"""
        state_low = _state(
            candles=[_normal_candle(), _bullish_pin(78400)],
            levels=[_kl(side="support", state="swept", price=78380, final_score=55)],
        )
        state_high = _state(
            candles=[_normal_candle(), _bullish_pin(78400)],
            levels=[_kl(side="support", state="swept", price=78380, final_score=95)],
        )
        c_low = SweepReclaimStrategy().detect(_ctx(state_low))
        c_high = SweepReclaimStrategy().detect(_ctx(state_high))
        assert c_low and c_high
        assert c_high.raw_strength > c_low.raw_strength

    def test_fake_break_higher_than_swept(self):
        """同 KL，fake_break > swept（state 奖励 +0.05）"""
        state_swept = _state(
            candles=[_normal_candle(), _bullish_pin(78400)],
            levels=[_kl(side="support", state="swept", price=78380, final_score=80)],
        )
        state_fake = _state(
            candles=[_normal_candle(), _bullish_pin(78400)],
            levels=[_kl(side="support", state="fake_break", price=78380, final_score=80)],
        )
        c_swept = SweepReclaimStrategy().detect(_ctx(state_swept))
        c_fake = SweepReclaimStrategy().detect(_ctx(state_fake))
        assert c_fake.raw_strength > c_swept.raw_strength
        assert c_fake.raw_strength - c_swept.raw_strength == pytest.approx(0.05, abs=1e-9)

    def test_proactive_bounce_quality_bonus(self):
        state = _state(
            candles=[_normal_candle(), _bullish_pin(78400)],
            levels=[_kl(side="support", state="swept", price=78380, final_score=80,
                        bounce_quality="proactive")],
        )
        c = SweepReclaimStrategy().detect(_ctx(state))
        # 期望 raw_strength ≥ 0.85（base 0.5 + kl 0.18 + pat 0.14 + bounce 0.05 = 0.87）
        assert c.raw_strength >= 0.85

    def test_strength_clamped_to_one(self):
        """所有奖励叠满也不超过 1.0"""
        state = _state(
            candles=[_normal_candle(), _bullish_pin(78400)],
            levels=[_kl(side="support", state="fake_break", price=78380,
                        final_score=100, bounce_quality="proactive")],
        )
        c = SweepReclaimStrategy().detect(_ctx(state))
        assert 0.0 < c.raw_strength <= 1.0


class TestKLSelection:
    def test_picks_highest_score_when_multiple_eligible(self):
        """多个 KL 满足条件时，选 final_score 最高"""
        state = _state(
            last_price=78400.0,
            candles=[_normal_candle(), _bullish_pin(78400)],
            levels=[
                _kl(side="support", state="swept", price=78390, final_score=60),
                _kl(side="support", state="swept", price=78380, final_score=90),  # 选这个
                _kl(side="support", state="fake_break", price=78395, final_score=75),
            ],
        )
        c = SweepReclaimStrategy().detect(_ctx(state))
        assert c is not None
        assert c.extra_data["kl_final_score"] == 90

    def test_skips_invalid_among_valid(self):
        """混合有效 + 无效 KL 时，只挑有效的"""
        state = _state(
            last_price=78400.0,
            candles=[_normal_candle(), _bullish_pin(78400)],
            levels=[
                _kl(side="support", state="idle", price=78380, final_score=95),  # state 不行
                _kl(side="support", state="swept", price=77000, final_score=95),  # 太远
                _kl(side="support", state="swept", price=78380, final_score=70),  # 选这个
            ],
        )
        c = SweepReclaimStrategy().detect(_ctx(state))
        assert c is not None
        assert c.extra_data["kl_final_score"] == 70


class TestEvidenceContent:
    def test_evidence_contains_sweep_and_pattern(self):
        state = _state(
            candles=[_normal_candle(), _bullish_pin(78400)],
            levels=[_kl(side="support", state="swept", price=78380, final_score=85,
                        sweep_usd=8_500_000)],
        )
        c = SweepReclaimStrategy().detect(_ctx(state))
        dims = {ev.dimension for ev in c.evidence}
        assert dims == {"Sweep", "Pattern"}
        sweep_text = next(ev.observation for ev in c.evidence if ev.dimension == "Sweep")
        assert "78380" in sweep_text
        assert "8.5M" in sweep_text or "8.5" in sweep_text
        pattern_text = next(ev.observation for ev in c.evidence if ev.dimension == "Pattern")
        assert "锤子线" in pattern_text

    def test_extra_data_complete(self):
        state = _state(
            candles=[_normal_candle(), _bullish_pin(78400)],
            levels=[_kl(side="support", state="swept", price=78380, final_score=85)],
        )
        c = SweepReclaimStrategy().detect(_ctx(state))
        assert c.extra_data["kl_side"] == "support"
        assert c.extra_data["kl_state"] == "swept"
        assert c.extra_data["pattern_name"] == "锤子线"
