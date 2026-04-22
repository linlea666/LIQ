"""测试 processors/roll_market_adapter.build_market_context_from_state

使用 SimpleNamespace 模拟 CoinState，避免引擎依赖。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from processors.roll_market_adapter import build_market_context_from_state


def _ticker(price: float = 60000.0):
    return SimpleNamespace(last=price, high_24h=price, low_24h=price)


def _full_state(price: float = 60000.0) -> SimpleNamespace:
    """构造一个"字段齐全、data_quality=ok"的 state。"""
    return SimpleNamespace(
        ticker=_ticker(price),
        atr=120.0,
        regime_snapshot=SimpleNamespace(regime="trend_up", confidence=0.72),
        market_structure=SimpleNamespace(
            direction="bullish", last_event="BOS_up", event_ts=0,
        ),
        market_structure_1d=SimpleNamespace(
            direction="bullish", last_event="BOS_up", event_ts=0,
        ),
        market_structure_1w=None,
        trend_exhaustion=SimpleNamespace(overall_state="healthy_continuation"),
        key_level_snapshot_v2=SimpleNamespace(
            levels=[
                SimpleNamespace(
                    price=price * 0.985, side="support", state="bounced",
                    confluence_score=78.0, pattern_detected="锤子线",
                ),
                SimpleNamespace(
                    price=price * 1.03, side="resistance", state="approaching",
                    confluence_score=52.0, pattern_detected="",
                ),
            ],
        ),
        cvd_contract=SimpleNamespace(
            has_divergence=True, divergence_type="bullish", delta_1h=1000.0,
        ),
        cvd_spot=None,
        funding=SimpleNamespace(current_rate=0.012, weighted_funding=0.011),
        range_signal=SimpleNamespace(squeeze_state="pending"),
    )


class TestHappyPath:
    def test_full_state_maps_all_fields(self):
        state = _full_state(60000.0)
        mc = build_market_context_from_state(state, ts=1234567890)

        assert mc is not None
        assert mc.ts == 1234567890
        assert mc.current_price == 60000.0
        assert mc.atr == 120.0
        assert mc.regime == "trend_up"
        assert mc.regime_confidence == pytest.approx(0.72)
        assert mc.ms_direction_4h == "bullish"
        assert mc.ms_direction_1h == "bullish"
        assert mc.ms_last_event_4h == "BOS"
        assert mc.ms_last_event_side_4h == "long"
        assert mc.te_overall_state == "healthy_continuation"
        assert mc.te_overall_score == pytest.approx(0.6)
        assert mc.nearest_level is not None
        assert mc.nearest_level.price == pytest.approx(60000.0 * 0.985)
        assert mc.nearest_level.kind == "support"
        assert mc.nearest_level.state == "bounced"
        assert mc.reversal_pattern == "pin_bar_support"
        assert mc.cvd_divergence == "bull_div"
        assert mc.funding_rate == pytest.approx(0.012)
        assert mc.squeeze_state == "pending"
        assert mc.data_quality == "ok"
        assert mc.missing_inputs == []


class TestGuards:
    def test_none_state_returns_none(self):
        assert build_market_context_from_state(None) is None

    def test_missing_ticker_returns_none(self):
        state = SimpleNamespace(ticker=None)
        assert build_market_context_from_state(state) is None

    def test_zero_price_returns_none(self):
        state = SimpleNamespace(ticker=_ticker(0.0))
        assert build_market_context_from_state(state) is None


class TestMarketStructureMapping:
    @pytest.mark.parametrize("raw_event,expected_kind,expected_side", [
        ("BOS_up", "BOS", "long"),
        ("BOS_down", "BOS", "short"),
        ("CHoCH_up", "CHoCH", "long"),
        ("CHoCH_down", "CHoCH", "short"),
        ("", "none", None),
        (None, "none", None),
    ])
    def test_last_event_parsing(self, raw_event, expected_kind, expected_side):
        state = _full_state()
        state.market_structure_1d = SimpleNamespace(
            direction="bullish",
            last_event=raw_event or "",
        )
        mc = build_market_context_from_state(state)
        assert mc is not None
        assert mc.ms_last_event_4h == expected_kind
        assert mc.ms_last_event_side_4h == expected_side

    def test_missing_ms_4h_falls_back_to_1h(self):
        state = _full_state()
        state.market_structure_1d = None
        state.market_structure = SimpleNamespace(
            direction="bearish", last_event="CHoCH_down",
        )
        mc = build_market_context_from_state(state)
        assert mc is not None
        assert mc.ms_direction_4h == "bearish"
        assert mc.ms_last_event_4h == "CHoCH"
        assert mc.ms_last_event_side_4h == "short"


class TestTrendExhaustion:
    @pytest.mark.parametrize("raw,expected_state,min_score,max_score", [
        ("healthy_continuation", "healthy_continuation", 0.5, 0.7),
        ("momentum_fading",      "exhaustion_warn",     -0.6, -0.4),
        ("exhaustion_warn",      "exhaustion_warn",     -0.6, -0.4),
        ("structural_reversal",  "exhaustion_confirmed", -0.9, -0.7),
        ("neutral",              "neutral",              -0.01, 0.01),
    ])
    def test_state_mapping_and_score(self, raw, expected_state, min_score, max_score):
        state = _full_state()
        state.trend_exhaustion = SimpleNamespace(overall_state=raw)
        mc = build_market_context_from_state(state)
        assert mc is not None
        assert mc.te_overall_state == expected_state
        assert min_score <= mc.te_overall_score <= max_score

    def test_missing_te_marks_partial_when_combined(self):
        state = _full_state()
        state.trend_exhaustion = None
        state.regime_snapshot = None
        mc = build_market_context_from_state(state)
        assert mc is not None
        assert "trend_exhaustion" in mc.missing_inputs
        assert "regime" in mc.missing_inputs
        # 只缺 2 个 → partial（仍可用）
        assert mc.data_quality == "partial"


class TestKeyLevelMapping:
    def test_nearest_selection_by_abs_distance(self):
        price = 60000.0
        state = _full_state(price)
        state.key_level_snapshot_v2 = SimpleNamespace(levels=[
            SimpleNamespace(price=price + 5000, side="resistance", state="idle",
                            confluence_score=80.0, pattern_detected=""),
            SimpleNamespace(price=price - 100, side="support", state="approaching",
                            confluence_score=40.0, pattern_detected=""),
            SimpleNamespace(price=price - 5000, side="support", state="bounced",
                            confluence_score=90.0, pattern_detected=""),
        ])
        mc = build_market_context_from_state(state)
        assert mc is not None
        assert mc.nearest_level.price == pytest.approx(price - 100)
        assert mc.nearest_level.kind == "support"

    @pytest.mark.parametrize("raw_state,mapped", [
        ("idle", "approaching"),
        ("approaching", "approaching"),
        ("testing", "tested"),
        ("swept", "tested"),
        ("bounced", "bounced"),
        ("broken", "broken"),
        ("fake_break", "fake_break"),
        ("flipped", "broken"),
    ])
    def test_state_translation(self, raw_state, mapped):
        state = _full_state()
        state.key_level_snapshot_v2 = SimpleNamespace(levels=[
            SimpleNamespace(price=59500.0, side="support", state=raw_state,
                            confluence_score=60.0, pattern_detected=""),
        ])
        mc = build_market_context_from_state(state)
        assert mc is not None
        assert mc.nearest_level.state == mapped

    def test_no_levels_marks_missing(self):
        state = _full_state()
        state.key_level_snapshot_v2 = SimpleNamespace(levels=[])
        mc = build_market_context_from_state(state)
        assert mc is not None
        assert mc.nearest_level is None
        assert "key_levels" in mc.missing_inputs


class TestPatternAndCVD:
    @pytest.mark.parametrize("name,expected", [
        ("锤子线", "pin_bar_support"),
        ("射击之星", "pin_bar_resistance"),
        ("看涨吞没", "engulfing_bullish"),
        ("看跌吞没", "engulfing_bearish"),
        ("十字星", "doji_reversal"),
        ("", "none"),
        ("未知形态", "none"),
    ])
    def test_reversal_pattern(self, name, expected):
        state = _full_state()
        state.key_level_snapshot_v2 = SimpleNamespace(levels=[
            SimpleNamespace(price=59500.0, side="support", state="bounced",
                            confluence_score=70.0, pattern_detected=name),
        ])
        mc = build_market_context_from_state(state)
        assert mc is not None
        assert mc.reversal_pattern == expected

    def test_cvd_divergence_prefers_contract(self):
        state = _full_state()
        state.cvd_contract = SimpleNamespace(
            has_divergence=True, divergence_type="bearish", delta_1h=-100.0,
        )
        state.cvd_spot = SimpleNamespace(
            has_divergence=True, divergence_type="bullish", delta_1h=100.0,
        )
        mc = build_market_context_from_state(state)
        assert mc.cvd_divergence == "bear_div"

    def test_cvd_divergence_falls_back_to_spot(self):
        state = _full_state()
        state.cvd_contract = None
        state.cvd_spot = SimpleNamespace(
            has_divergence=True, divergence_type="bullish", delta_1h=50.0,
        )
        mc = build_market_context_from_state(state)
        assert mc.cvd_divergence == "bull_div"

    def test_cvd_no_divergence_is_none(self):
        state = _full_state()
        state.cvd_contract = SimpleNamespace(
            has_divergence=False, divergence_type="", delta_1h=0.0,
        )
        mc = build_market_context_from_state(state)
        assert mc.cvd_divergence == "none"

    def test_cvd_without_type_uses_delta_sign(self):
        state = _full_state()
        state.cvd_contract = SimpleNamespace(
            has_divergence=True, divergence_type="", delta_1h=-500.0,
        )
        mc = build_market_context_from_state(state)
        assert mc.cvd_divergence == "bear_div"


class TestSqueezeAndFunding:
    @pytest.mark.parametrize("raw,expected", [
        ("pending", "pending"),
        ("in_squeeze", "pending"),
        ("released_up", "released_up"),
        ("breakout_up", "released_up"),
        ("released_down", "released_down"),
        ("breakout_down", "released_down"),
        ("", "none"),
        (None, "none"),
    ])
    def test_squeeze_state_mapping(self, raw, expected):
        state = _full_state()
        state.range_signal = SimpleNamespace(squeeze_state=raw)
        mc = build_market_context_from_state(state)
        assert mc.squeeze_state == expected

    def test_funding_rate_extraction(self):
        state = _full_state()
        state.funding = SimpleNamespace(current_rate=0.025, weighted_funding=None)
        mc = build_market_context_from_state(state)
        assert mc.funding_rate == pytest.approx(0.025)

    def test_funding_rate_none_when_invalid(self):
        state = _full_state()
        state.funding = SimpleNamespace(current_rate=None, weighted_funding=None)
        mc = build_market_context_from_state(state)
        assert mc.funding_rate is None


class TestDataQuality:
    def test_full_state_is_ok(self):
        state = _full_state()
        mc = build_market_context_from_state(state)
        assert mc.data_quality == "ok"

    def test_missing_atr_marks_partial(self):
        state = _full_state()
        state.atr = 0.0
        mc = build_market_context_from_state(state)
        assert mc.data_quality == "partial"
        assert "atr" in mc.missing_inputs

    def test_heavy_missing_marks_insufficient(self):
        state = SimpleNamespace(
            ticker=_ticker(60000.0),
            atr=0.0,
            regime_snapshot=None,
            market_structure=None,
            market_structure_1d=None,
            trend_exhaustion=None,
            key_level_snapshot_v2=None,
            cvd_contract=None,
            cvd_spot=None,
            funding=None,
            range_signal=None,
        )
        mc = build_market_context_from_state(state)
        assert mc is not None
        assert mc.data_quality == "insufficient"
        assert len(mc.missing_inputs) >= 4
