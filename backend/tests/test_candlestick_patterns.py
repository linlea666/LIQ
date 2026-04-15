"""K 线反转形态检测单元测试"""

import pytest
from models.market import CandleData
from processors.candlestick_patterns import (
    detect_reversal_pattern,
    _is_pin_bar,
    _is_engulfing,
    _is_doji,
)


def _c(o, h, l, c):
    return CandleData(coin="BTC", ts=0, o=o, h=h, l=l, c=c)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Pin Bar
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestPinBar:
    def test_bullish_hammer(self):
        c = _c(o=100, h=101, l=94, c=100.5)
        assert _is_pin_bar(c, "support") is True

    def test_bearish_shooting_star(self):
        c = _c(o=100, h=106, l=99, c=99.5)
        assert _is_pin_bar(c, "resistance") is True

    def test_not_pin_bar_large_body(self):
        c = _c(o=100, h=105, l=98, c=104)
        assert _is_pin_bar(c, "support") is False

    def test_wrong_side(self):
        c = _c(o=100, h=101, l=94, c=100.5)
        assert _is_pin_bar(c, "resistance") is False

    def test_zero_range(self):
        c = _c(o=100, h=100, l=100, c=100)
        assert _is_pin_bar(c, "support") is False


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Engulfing
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestEngulfing:
    def test_bullish_engulfing(self):
        prev = _c(o=102, h=102.5, l=99, c=99.5)
        curr = _c(o=99, h=103.5, l=98.5, c=103)
        assert _is_engulfing(prev, curr, "support") is True

    def test_bearish_engulfing(self):
        prev = _c(o=99, h=103, l=98.5, c=102.5)
        curr = _c(o=103, h=103.5, l=98, c=98.5)
        assert _is_engulfing(prev, curr, "resistance") is True

    def test_not_engulfing_same_direction(self):
        prev = _c(o=100, h=103, l=99, c=102)
        curr = _c(o=102, h=104, l=101, c=103.5)
        assert _is_engulfing(prev, curr, "support") is False

    def test_not_engulfing_too_small(self):
        prev = _c(o=100, h=105, l=99, c=104)
        curr = _c(o=104, h=104.5, l=102, c=102.5)
        assert _is_engulfing(prev, curr, "resistance") is False


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Doji
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestDoji:
    def test_doji(self):
        c = _c(o=100, h=103, l=97, c=100.2)
        assert _is_doji(c) is True

    def test_not_doji(self):
        c = _c(o=100, h=105, l=98, c=104)
        assert _is_doji(c) is False


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# detect_reversal_pattern 综合
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestDetectReversalPattern:
    def test_pin_bar_beats_doji(self):
        candles = [
            _c(o=100, h=101, l=99, c=100),
            _c(o=100, h=100.5, l=94, c=100.2),
        ]
        result = detect_reversal_pattern(candles, "support")
        assert result.found is True
        assert result.name == "锤子线"
        assert result.strength > 0.5

    def test_engulfing_detected(self):
        candles = [
            _c(o=102, h=102.5, l=99, c=99.5),
            _c(o=99, h=103.5, l=98.5, c=103),
        ]
        result = detect_reversal_pattern(candles, "support")
        assert result.found is True
        assert "吞没" in result.name

    def test_no_pattern(self):
        candles = [
            _c(o=100, h=101, l=99, c=100.5),
            _c(o=100.5, h=101.5, l=100, c=101),
        ]
        result = detect_reversal_pattern(candles, "support")
        assert result.found is False

    def test_none_candles(self):
        assert detect_reversal_pattern(None, "support").found is False

    def test_too_few_candles(self):
        assert detect_reversal_pattern([_c(100, 101, 99, 100)], "support").found is False

    def test_resistance_shooting_star(self):
        candles = [
            _c(o=100, h=101, l=99, c=100),
            _c(o=100, h=106, l=99.5, c=100.3),
        ]
        result = detect_reversal_pattern(candles, "resistance")
        assert result.found is True
        assert result.name == "射击之星"
