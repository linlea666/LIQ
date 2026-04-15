"""箱体信号 V2 单元测试"""

import time
import pytest
from models.key_level import KeyLevelV2, KeyLevelSnapshotV2
from models.flow import RangeSignalData
from processors.range_signal import (
    calculate_range_signal,
    _extract_box_boundaries,
    _calc_price_position,
    _transition_box_state,
    _calc_box_quality,
    _calc_breakout_probability,
    _grade_signal_v2,
    _check_volume_declining,
)


# ── helpers ──

def _make_level(price, tier="A", side="support", score=30, sources=None, test_count=0, state="idle"):
    return KeyLevelV2(
        price=price, side=side, strength_tier=tier,
        confluence_score=score, source_count=len(sources or ["a", "b"]),
        sources=sources or ["src1", "src2"], test_count=test_count,
        state=state,
    )

def _make_snapshot(levels, price=85000, atr=500):
    return KeyLevelSnapshotV2(
        ts=int(time.time()), current_price=price, atr=atr,
        levels=levels,
    )

def _make_prev_range(box_state="confirmed", upper=86000, lower=83000, ts=None):
    ts = ts or int(time.time()) - 3600
    return RangeSignalData(
        ts=ts, range_upper=upper, range_lower=lower,
        box_state=box_state, box_state_ts=ts,
        price_position="middle", price_position_pct=50,
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 边界提取
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestExtractBoundaries:
    def test_basic(self):
        levels = [
            _make_level(83000, "A", "support"),
            _make_level(86000, "A", "resistance"),
            _make_level(80000, "B", "support"),
            _make_level(90000, "B", "resistance"),
        ]
        snap = _make_snapshot(levels)
        upper, lower = _extract_box_boundaries(snap, 85000)
        assert upper.price == 86000
        assert lower.price == 83000

    def test_prefers_stronger_tier(self):
        levels = [
            _make_level(85500, "B", "resistance"),
            _make_level(86000, "S", "resistance"),
        ]
        snap = _make_snapshot(levels)
        upper, lower = _extract_box_boundaries(snap, 85000)
        assert upper.price == 86000

    def test_none_when_no_levels(self):
        snap = _make_snapshot([])
        upper, lower = _extract_box_boundaries(snap, 85000)
        assert upper is None
        assert lower is None

    def test_excludes_c_tier(self):
        levels = [_make_level(86000, "C", "resistance")]
        snap = _make_snapshot(levels)
        upper, lower = _extract_box_boundaries(snap, 85000)
        assert upper is None

    def test_none_snapshot(self):
        upper, lower = _extract_box_boundaries(None, 85000)
        assert upper is None
        assert lower is None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 价格位置
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestPricePosition:
    def test_middle(self):
        pos, pct = _calc_price_position(84500, 86000, 83000, 1.5)
        assert pos == "middle"
        assert 40 < pct < 60

    def test_near_upper(self):
        pos, _ = _calc_price_position(85800, 86000, 83000, 1.5)
        assert pos == "near_upper"

    def test_near_lower(self):
        pos, _ = _calc_price_position(83200, 86000, 83000, 1.5)
        assert pos == "near_lower"

    def test_above(self):
        pos, pct = _calc_price_position(87000, 86000, 83000, 1.5)
        assert pos == "above"
        assert pct == 100.0

    def test_below(self):
        pos, pct = _calc_price_position(82000, 86000, 83000, 1.5)
        assert pos == "below"
        assert pct == 0.0

    def test_no_bounds(self):
        pos, pct = _calc_price_position(85000, None, None, 1.5)
        assert pos == "middle"
        assert pct == 50.0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 状态机
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestBoxStateMachine:
    def test_none_to_forming(self):
        state, ts, age = _transition_box_state(
            True, "middle", False, None, int(time.time()),
            86000, 83000, 85000,
        )
        assert state == "forming"

    def test_forming_to_confirmed(self):
        now = int(time.time())
        prev = _make_prev_range("forming", ts=now - 5 * 3600)
        state, _, age = _transition_box_state(
            True, "middle", False, prev, now,
            86000, 83000, 85000,
        )
        assert state == "confirmed"
        assert age >= 4

    def test_confirmed_to_mature(self):
        now = int(time.time())
        prev = _make_prev_range("confirmed", ts=now - 80 * 3600)
        state, _, _ = _transition_box_state(
            True, "middle", False, prev, now,
            86000, 83000, 85000,
        )
        assert state == "mature"

    def test_mature_to_squeeze(self):
        now = int(time.time())
        prev = _make_prev_range("mature", ts=now - 100 * 3600)
        state, _, _ = _transition_box_state(
            True, "middle", True, prev, now,
            86000, 83000, 85000,
        )
        assert state == "squeeze"

    def test_squeeze_to_breaking_up(self):
        now = int(time.time())
        prev = _make_prev_range("squeeze", ts=now - 100 * 3600)
        state, _, _ = _transition_box_state(
            True, "above", False, prev, now,
            86000, 83000, 87000,
        )
        assert state == "breaking_up"

    def test_no_box_returns_none(self):
        state, _, _ = _transition_box_state(
            False, "middle", False, None, int(time.time()),
            None, None, 85000,
        )
        assert state == "none"

    def test_boundary_drift_resets(self):
        """If boundaries shift > 2%, state resets to forming."""
        now = int(time.time())
        prev = _make_prev_range("confirmed", upper=90000, lower=83000, ts=now - 10 * 3600)
        state, _, _ = _transition_box_state(
            True, "middle", False, prev, now,
            86000, 83000, 85000,
        )
        assert state == "forming"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 箱体质量
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestBoxQuality:
    def test_high_quality(self):
        upper = _make_level(86000, "S", score=50)
        lower = _make_level(83000, "A", score=35)
        q = _calc_box_quality(True, upper, lower, 100, True, True, 4.0)
        assert q >= 70

    def test_no_box_zero(self):
        assert _calc_box_quality(False, None, None, 0, False, False, 0) == 0

    def test_low_quality(self):
        upper = _make_level(86000, "B", score=10)
        lower = _make_level(83000, "C", score=5)
        q = _calc_box_quality(True, upper, lower, 2, False, False, 13.0)
        assert q < 30


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 突破概率
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestBreakoutProbability:
    def test_squeeze_high_prob(self):
        prob, bias, reason = _calc_breakout_probability(
            "squeeze", True, True, True, "ask_heavy", True, 100,
            False, False,
        )
        assert prob >= 0.7
        assert "BB Squeeze" in reason

    def test_none_state_zero(self):
        prob, _, _ = _calc_breakout_probability(
            "none", False, False, False, "", False, 0, None, None,
        )
        assert prob == 0

    def test_bias_up(self):
        _, bias, _ = _calc_breakout_probability(
            "confirmed", False, False, False, "bid_heavy", False, 50,
            True, True,
        )
        assert bias == "up"

    def test_bias_down(self):
        _, bias, _ = _calc_breakout_probability(
            "confirmed", False, False, False, "ask_heavy", False, 50,
            False, False,
        )
        assert bias == "down"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 信号分级
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestGradeSignal:
    def test_s_grade_short(self):
        upper = _make_level(86000, "S", score=60)
        lower = _make_level(83000, "A", score=40)
        grade, direction, reason, *_ = _grade_signal_v2(
            "near_upper", True, "mature",
            False, False,  # MACD below zero
            True,  # sweep
            False,
            upper, lower, 85800, 500,
            0.3, "up",
        )
        assert grade == "S"
        assert direction == "short"

    def test_a_grade_long(self):
        upper = _make_level(86000, "A", score=40)
        lower = _make_level(83000, "A", score=40)
        grade, direction, *_ = _grade_signal_v2(
            "near_lower", True, "confirmed",
            True, True,  # MACD above zero
            False,  # no sweep
            False,
            upper, lower, 83200, 500,
            0.2, "neutral",
        )
        assert grade == "A"
        assert direction == "long"

    def test_b_grade_no_confirm(self):
        upper = _make_level(86000, "B", score=20)
        lower = _make_level(83000, "B", score=20)
        grade, *_ = _grade_signal_v2(
            "near_upper", True, "confirmed",
            True, True,  # MACD above zero (wrong side)
            False,
            False,
            upper, lower, 85800, 500,
            0.1, "neutral",
        )
        assert grade == "B"

    def test_middle_no_signal(self):
        grade, direction, *_ = _grade_signal_v2(
            "middle", True, "confirmed",
            None, None, False, False,
            None, None, 84500, 500, 0.1, "",
        )
        assert grade is None
        assert direction is None

    def test_breaking_gives_b_signal(self):
        grade, direction, *_ = _grade_signal_v2(
            "above", False, "breaking_up",
            None, None, False, False,
            None, None, 87000, 500, 0.5, "up",
        )
        assert grade == "B"
        assert direction == "long"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 成交量检测
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestVolumeDeclining:
    def _candle(self, i, v):
        from models.market import CandleData
        return CandleData(coin="BTC", ts=i, o=100, h=101, l=99, c=100, vol=v)

    def test_declining(self):
        candles = [self._candle(i, 1000 - i * 50) for i in range(10)]
        assert _check_volume_declining(candles) is True

    def test_not_declining(self):
        candles = [self._candle(i, 500 + i * 100) for i in range(10)]
        assert _check_volume_declining(candles) is False

    def test_too_few(self):
        candles = [self._candle(i, 100) for i in range(5)]
        assert _check_volume_declining(candles) is False


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 集成测试
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestCalculateRangeSignal:
    def test_with_valid_snapshot(self):
        levels = [
            _make_level(86000, "A", "resistance", score=30),
            _make_level(83000, "A", "support", score=25),
        ]
        snap = _make_snapshot(levels, price=84500)
        result = calculate_range_signal(
            kl_snapshot=snap,
            current_price=84500,
            atr=500,
        )
        assert result is not None
        assert result.range_upper == 86000
        assert result.range_lower == 83000
        assert result.box_state == "forming"

    def test_no_snapshot(self):
        result = calculate_range_signal(
            kl_snapshot=None,
            current_price=85000,
            atr=500,
        )
        assert result is not None
        assert result.box_state == "none"

    def test_zero_price_returns_none(self):
        assert calculate_range_signal(None, 0, 500) is None

    def test_preserves_state_across_calls(self):
        levels = [
            _make_level(86000, "A", "resistance", score=30),
            _make_level(83000, "A", "support", score=25),
        ]
        snap = _make_snapshot(levels, price=84500)
        first = calculate_range_signal(kl_snapshot=snap, current_price=84500, atr=500)
        assert first.box_state == "forming"

        # Simulate time passing with prev_range
        first.box_state_ts = int(time.time()) - 5 * 3600
        second = calculate_range_signal(
            kl_snapshot=snap, current_price=84500, atr=500,
            prev_range=first,
        )
        assert second.box_state == "confirmed"
