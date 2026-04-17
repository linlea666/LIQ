"""箱体信号 V2 单元测试 — MA 骨架 + 微观区间"""

import time
import pytest
from models.key_level import KeyLevelV2, KeyLevelSnapshotV2
from models.flow import RangeSignalData
from processors.range_signal import (
    calculate_range_signal,
    _pick_ma_boundary,
    _extract_micro_boundaries,
    _calc_price_position,
    _transition_box_state,
    _calc_box_quality,
    _calc_breakout_probability,
    _grade_signal_v2,
    _check_volume_declining,
    _compute_ms_alignment,
    _append_ms_hint,
)
from models.market_structure import MarketStructure


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
# MA 骨架边界
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestPickMaBoundary:
    def test_above_picks_nearest_ma(self):
        val, src = _pick_ma_boundary(85000, 69000, 77000, 95000, "above")
        assert val == 95000
        assert "周线" in src

    def test_below_picks_nearest_ma(self):
        val, src = _pick_ma_boundary(85000, 69000, 77000, 95000, "below")
        assert val == 77000
        assert "MA120" in src

    def test_price_between_ma60_and_ma120(self):
        upper, u_src = _pick_ma_boundary(73000, 69000, 77000, 95000, "above")
        lower, l_src = _pick_ma_boundary(73000, 69000, 77000, 95000, "below")
        assert upper == 77000
        assert lower == 69000

    def test_all_above(self):
        val, src = _pick_ma_boundary(60000, 69000, 77000, 95000, "below")
        assert val is None

    def test_all_below(self):
        val, src = _pick_ma_boundary(100000, 69000, 77000, 95000, "above")
        assert val is None

    def test_none_mas(self):
        val, src = _pick_ma_boundary(85000, None, None, None, "above")
        assert val is None

    def test_weekly_ma_used_when_only_above(self):
        val, src = _pick_ma_boundary(90000, 69000, 77000, 95000, "above")
        assert val == 95000
        assert "周线" in src


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 微观区间
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestMicroBoundaries:
    def test_basic(self):
        levels = [
            _make_level(86000, "A", "resistance"),
            _make_level(83000, "A", "support"),
        ]
        snap = _make_snapshot(levels)
        micro_u, micro_l = _extract_micro_boundaries(snap, 85000)
        assert micro_u.price == 86000
        assert micro_l.price == 83000

    def test_excludes_c_tier(self):
        levels = [_make_level(86000, "C", "resistance")]
        snap = _make_snapshot(levels)
        micro_u, _ = _extract_micro_boundaries(snap, 85000)
        assert micro_u is None

    def test_none_snapshot(self):
        micro_u, micro_l = _extract_micro_boundaries(None, 85000)
        assert micro_u is None and micro_l is None

    def test_prefers_closer_in_same_bucket(self):
        levels = [
            _make_level(85500, "A", "resistance"),
            _make_level(87000, "A", "resistance"),
        ]
        snap = _make_snapshot(levels, price=85000)
        micro_u, _ = _extract_micro_boundaries(snap, 85000)
        assert micro_u.price == 85500


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
        q = _calc_box_quality(True, 100, True, True, 5.0)
        assert q >= 70

    def test_no_box_zero(self):
        assert _calc_box_quality(False, 0, False, False, 0) == 0

    def test_medium_width(self):
        q = _calc_box_quality(True, 10, False, False, 5.0)
        assert q >= 50

    def test_too_wide_penalty(self):
        q_normal = _calc_box_quality(True, 10, False, False, 5.0)
        q_wide = _calc_box_quality(True, 10, False, False, 13.0)
        assert q_wide < q_normal


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
        grade, direction, reason, *_ = _grade_signal_v2(
            "near_upper", True, "mature",
            False, False,
            True, False,
            86000, 83000, "日线MA120", "日线MA60",
            85800, 500, 0.3, "up",
        )
        assert grade == "S"
        assert direction == "short"

    def test_a_grade_long(self):
        grade, direction, *_ = _grade_signal_v2(
            "near_lower", True, "confirmed",
            True, True,
            False, False,
            86000, 83000, "日线MA120", "日线MA60",
            83200, 500, 0.2, "neutral",
        )
        assert grade == "A"
        assert direction == "long"

    def test_b_grade_no_confirm(self):
        grade, *_ = _grade_signal_v2(
            "near_upper", True, "confirmed",
            True, True,
            False, False,
            86000, 83000, "日线MA120", "日线MA60",
            85800, 500, 0.1, "neutral",
        )
        assert grade == "B"

    def test_middle_no_signal(self):
        grade, direction, *_ = _grade_signal_v2(
            "middle", True, "confirmed",
            None, None, False, False,
            86000, 83000, "日线MA120", "日线MA60",
            84500, 500, 0.1, "",
        )
        assert grade is None
        assert direction is None

    def test_breaking_gives_b_signal(self):
        grade, direction, *_ = _grade_signal_v2(
            "above", False, "breaking_up",
            None, None, False, False,
            86000, 83000, "日线MA120", "日线MA60",
            87000, 500, 0.5, "up",
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
    def _make_daily_candles(self, n=130):
        """生成模拟日线蜡烛，MA60 ≈ 83000, MA120 ≈ 80000"""
        from models.market import CandleData
        candles = []
        for i in range(n):
            p = 80000 + i * 50
            candles.append(CandleData(coin="BTC", ts=i * 86400, o=p, h=p + 100, l=p - 100, c=p, vol=1000))
        return candles

    def test_ma_driven_core_box(self):
        candles = self._make_daily_candles(130)
        price = candles[-1].close
        levels = [
            _make_level(price + 500, "A", "resistance", score=30),
            _make_level(price - 500, "A", "support", score=25),
        ]
        snap = _make_snapshot(levels, price=price)
        result = calculate_range_signal(
            kl_snapshot=snap,
            current_price=price,
            atr=500,
            candles_1d=candles,
        )
        assert result is not None
        if result.range_upper:
            assert "MA" in (result.range_upper_source or "")
        assert result.micro_upper is not None or result.micro_lower is not None

    def test_no_snapshot_still_has_ma_box(self):
        candles = self._make_daily_candles(130)
        price = candles[-1].close
        result = calculate_range_signal(
            kl_snapshot=None,
            current_price=price,
            atr=500,
            candles_1d=candles,
        )
        assert result is not None
        assert result.micro_upper is None
        assert result.micro_lower is None

    def test_zero_price_returns_none(self):
        assert calculate_range_signal(None, 0, 500) is None

    def test_no_candles_no_box(self):
        result = calculate_range_signal(
            kl_snapshot=None,
            current_price=85000,
            atr=500,
        )
        assert result is not None
        assert result.range_upper is None
        assert result.range_lower is None
        assert result.box_state == "none"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Commit 3：市场结构对齐（_compute_ms_alignment + _append_ms_hint）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _make_ms(direction="bullish", bias="long_only", event="BOS_up", conf=0.9):
    return MarketStructure(
        timeframe="1h",
        direction=direction,
        operate_bias=bias,
        last_event=event,
        confidence=conf,
    )


class TestMsAlignment:
    def test_aligned_long_with_long_only(self):
        assert _compute_ms_alignment("long", _make_ms(bias="long_only")) == "aligned"

    def test_aligned_short_with_short_only(self):
        assert _compute_ms_alignment(
            "short", _make_ms(direction="bearish", bias="short_only"),
        ) == "aligned"

    def test_conflict_long_with_short_only(self):
        assert _compute_ms_alignment(
            "long", _make_ms(direction="bearish", bias="short_only"),
        ) == "conflict"

    def test_conflict_stand_aside_always(self):
        """stand_aside 对任何箱体方向都是冲突（提醒观望）。"""
        ms = _make_ms(direction="transitioning", bias="stand_aside")
        assert _compute_ms_alignment("long", ms) == "conflict"
        assert _compute_ms_alignment("short", ms) == "conflict"

    def test_both_ok_always_aligned(self):
        ms = _make_ms(direction="ranging", bias="both_ok")
        assert _compute_ms_alignment("long", ms) == "aligned"
        assert _compute_ms_alignment("short", ms) == "aligned"

    def test_unknown_when_no_ms(self):
        assert _compute_ms_alignment("long", None) == "unknown"

    def test_unknown_when_no_signal_direction(self):
        assert _compute_ms_alignment(None, _make_ms()) == "unknown"


class TestMsHintAppend:
    def test_aligned_hint(self):
        out = _append_ms_hint("入场点到达MA60(85000)", "aligned")
        assert "✅" in out and "结构方向一致" in out

    def test_conflict_hint(self):
        out = _append_ms_hint("入场点到达MA60(85000)", "conflict")
        assert "⚠️" in out and "结构方向冲突" in out

    def test_neutral_no_change(self):
        original = "入场点到达MA60(85000)"
        assert _append_ms_hint(original, "neutral") == original
        assert _append_ms_hint(original, "unknown") == original
        assert _append_ms_hint(original, "") == original


class TestRangeSignalMsIntegration:
    """端到端：calculate_range_signal 接收 ms 参数并填充字段。"""

    def test_ms_fields_populated(self):
        ms = _make_ms(direction="bullish", bias="long_only",
                     event="BOS_up", conf=0.85)
        result = calculate_range_signal(
            kl_snapshot=None,
            current_price=85000,
            atr=500,
            market_structure=ms,
        )
        assert result is not None
        assert result.ms_direction == "bullish"
        assert result.ms_event == "BOS_up"
        assert result.ms_bias == "long_only"
        assert result.ms_confidence == 0.85
        # 没有 signal direction 时 alignment 应为 unknown
        assert result.ms_alignment == "unknown"

    def test_backward_compat_no_ms(self):
        """不传 ms 时所有字段默认值，保持向后兼容。"""
        result = calculate_range_signal(
            kl_snapshot=None,
            current_price=85000,
            atr=500,
        )
        assert result is not None
        assert result.ms_direction is None
        assert result.ms_event is None
        assert result.ms_bias is None
        assert result.ms_confidence == 0.0
        assert result.ms_alignment == "unknown"

    def test_empty_event_stored_as_none(self):
        ms = _make_ms(event="")
        result = calculate_range_signal(
            kl_snapshot=None,
            current_price=85000,
            atr=500,
            market_structure=ms,
        )
        assert result is not None
        assert result.ms_event is None
