"""策略 C · Range Edge Fade 单测"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from models.market import CandleData
from models.scalp_signal import StrategyName
from processors.scalp_signal.base_strategy import StrategyContext
from processors.scalp_signal.strategy_c_range_edge import (
    RANGE_LOWER_PCT_MAX,
    RANGE_UPPER_PCT_MIN,
    RSI_OVERBOUGHT_MIN,
    RSI_OVERSOLD_MAX,
    RangeEdgeFadeStrategy,
)


# ────────────────────────────────────────────────────────────────────────────
# 工厂
# ────────────────────────────────────────────────────────────────────────────

def _bullish_pin(price: float = 78400, ts: int = 0) -> CandleData:
    return CandleData(coin="BTC", ts=ts, o=price + 10, h=price + 25, l=price - 20, c=price + 20)


def _bearish_pin(price: float = 78400, ts: int = 0) -> CandleData:
    return CandleData(coin="BTC", ts=ts, o=price - 10, h=price + 20, l=price - 25, c=price - 20)


def _normal(price: float = 78400, ts: int = 0) -> CandleData:
    return CandleData(coin="BTC", ts=ts, o=price - 5, h=price + 8, l=price - 8, c=price + 5)


def _state(
    *,
    last_price: float = 78400.0,
    rsi: float | None = 30.0,
    candles: list[CandleData] | None = None,
    # P0-5 二次确认 fixture（默认全过，可单独覆写测拒绝路径）
    adx: float = 18.0,
    box_state: str = "mature",
    box_width_pct: float = 3.0,
    range_upper_test_count: int = 3,
    range_lower_test_count: int = 4,
    range_signal: object | None = None,
    regime_snapshot: object | None = None,
):
    if regime_snapshot is None and adx is not None:
        regime_snapshot = SimpleNamespace(adx=adx, atr_pct=0.5)
    if range_signal is None:
        range_signal = SimpleNamespace(
            box_state=box_state,
            box_width_pct=box_width_pct,
            range_upper_test_count=range_upper_test_count,
            range_lower_test_count=range_lower_test_count,
        )
    return SimpleNamespace(
        ticker=SimpleNamespace(last=last_price, ts=0),
        rsi_14=rsi,
        candles_15m=candles or [_normal(), _bullish_pin(78400, ts=1)],
        regime_snapshot=regime_snapshot,
        range_signal=range_signal,
    )


def _ctx(
    state, *,
    regime: str = "range",
    range_position_pct: float | None = 15.0,
) -> StrategyContext:
    return StrategyContext(
        state=state, coin="BTC", horizon_min=30,
        regime=regime, range_position_pct=range_position_pct, bias_score=0.0,
    )


# ════════════════════════════════════════════════════════════════════════════
# 元数据
# ════════════════════════════════════════════════════════════════════════════

class TestStrategyCMetadata:
    def test_class_constants(self):
        s = RangeEdgeFadeStrategy()
        assert s.name == StrategyName.C_RANGE_EDGE_FADE
        assert s.suitable_regimes == {"range"}  # 仅 range
        assert s.suitable_horizons == {30, 60}

    def test_not_applicable_in_trend(self):
        s = RangeEdgeFadeStrategy()
        assert s.is_applicable("trend_up", 30) is False
        assert s.is_applicable("range", 30) is True


# ════════════════════════════════════════════════════════════════════════════
# 触发场景
# ════════════════════════════════════════════════════════════════════════════

class TestLowerEdgeTriggers:
    def test_perfect_lower_edge_setup(self):
        """位置 15 + RSI 28 + 锤子线 → up"""
        state = _state(rsi=28.0, candles=[_normal(), _bullish_pin(78400, ts=1)])
        ctx = _ctx(state, range_position_pct=15.0)
        c = RangeEdgeFadeStrategy().detect(ctx)
        assert c is not None
        assert c.direction == "up"
        assert c.extra_data["edge"] == "lower"

    def test_lower_boundary_position(self):
        """边界值 20 也触发"""
        state = _state(rsi=30.0)
        c = RangeEdgeFadeStrategy().detect(_ctx(state, range_position_pct=20.0))
        assert c is not None


class TestUpperEdgeTriggers:
    def test_perfect_upper_edge_setup(self):
        """位置 85 + RSI 72 + 射击之星 → down"""
        state = _state(rsi=72.0, candles=[_normal(), _bearish_pin(78400, ts=1)])
        ctx = _ctx(state, range_position_pct=85.0)
        c = RangeEdgeFadeStrategy().detect(ctx)
        assert c is not None
        assert c.direction == "down"
        assert c.extra_data["edge"] == "upper"

    def test_upper_boundary_position(self):
        state = _state(rsi=70.0, candles=[_normal(), _bearish_pin()])
        c = RangeEdgeFadeStrategy().detect(_ctx(state, range_position_pct=80.0))
        assert c is not None


# ════════════════════════════════════════════════════════════════════════════
# 拒绝场景
# ════════════════════════════════════════════════════════════════════════════

class TestRejectConditions:
    def test_non_range_regime_rejected(self):
        """regime ≠ range → 拒绝"""
        state = _state(rsi=28.0)
        c = RangeEdgeFadeStrategy().detect(_ctx(state, regime="trend_up"))
        assert c is None

    def test_no_position_rejected(self):
        """range_position_pct=None → 拒绝"""
        state = _state(rsi=28.0)
        assert RangeEdgeFadeStrategy().detect(_ctx(state, range_position_pct=None)) is None

    def test_middle_position_rejected(self):
        """中部 50 → 拒绝（既非上沿也非下沿）"""
        state = _state(rsi=28.0)
        assert RangeEdgeFadeStrategy().detect(_ctx(state, range_position_pct=50.0)) is None

    def test_lower_edge_no_oversold_rejected(self):
        """位置在下沿但 RSI 不超卖 → 拒绝"""
        state = _state(rsi=45.0)  # > 35 阈值
        assert RangeEdgeFadeStrategy().detect(_ctx(state, range_position_pct=15.0)) is None

    def test_upper_edge_no_overbought_rejected(self):
        """位置在上沿但 RSI 不超买 → 拒绝"""
        state = _state(rsi=55.0, candles=[_normal(), _bearish_pin()])
        assert RangeEdgeFadeStrategy().detect(_ctx(state, range_position_pct=85.0)) is None

    def test_no_rsi_rejected(self):
        state = _state(rsi=None)
        assert RangeEdgeFadeStrategy().detect(_ctx(state, range_position_pct=15.0)) is None

    def test_no_pattern_rejected(self):
        """K 线无形态 → 拒绝"""
        state = _state(rsi=28.0, candles=[_normal(), _normal()])
        assert RangeEdgeFadeStrategy().detect(_ctx(state, range_position_pct=15.0)) is None

    def test_lower_with_bearish_pattern_rejected(self):
        """下沿出现看跌形态 → 形态检测会返回 not found → 拒绝"""
        state = _state(rsi=28.0, candles=[_normal(), _bearish_pin()])
        assert RangeEdgeFadeStrategy().detect(_ctx(state, range_position_pct=15.0)) is None

    def test_no_ticker_rejected(self):
        state = _state(rsi=28.0)
        state.ticker = None
        assert RangeEdgeFadeStrategy().detect(_ctx(state, range_position_pct=15.0)) is None


# ════════════════════════════════════════════════════════════════════════════
# raw_strength 计算
# ════════════════════════════════════════════════════════════════════════════

class TestRawStrength:
    def test_extreme_position_higher_strength(self):
        """位置越极端，raw_strength 越高"""
        edge = _state(rsi=25.0)
        boundary = _state(rsi=25.0)
        c_edge = RangeEdgeFadeStrategy().detect(_ctx(edge, range_position_pct=5.0))
        c_boundary = RangeEdgeFadeStrategy().detect(_ctx(boundary, range_position_pct=20.0))
        assert c_edge is not None and c_boundary is not None
        assert c_edge.raw_strength > c_boundary.raw_strength

    def test_extreme_rsi_higher_strength(self):
        """RSI 越极端，raw_strength 越高"""
        weak = _state(rsi=33.0)
        strong = _state(rsi=18.0)
        c_weak = RangeEdgeFadeStrategy().detect(_ctx(weak, range_position_pct=15.0))
        c_strong = RangeEdgeFadeStrategy().detect(_ctx(strong, range_position_pct=15.0))
        assert c_strong.raw_strength > c_weak.raw_strength

    def test_strength_clamped(self):
        """所有奖励叠满不超过 1.0"""
        state = _state(rsi=10.0)  # 极端超卖
        c = RangeEdgeFadeStrategy().detect(_ctx(state, range_position_pct=2.0))  # 极端下沿
        assert 0.0 < c.raw_strength <= 1.0


# ════════════════════════════════════════════════════════════════════════════
# Evidence
# ════════════════════════════════════════════════════════════════════════════

class TestEvidence:
    def test_evidence_dimensions(self):
        state = _state(rsi=28.0)
        c = RangeEdgeFadeStrategy().detect(_ctx(state, range_position_pct=15.0))
        dims = {ev.dimension for ev in c.evidence}
        # P0-5 加入 RangeIntegrity 维度
        assert dims == {"RangePosition", "RangeIntegrity", "RSI", "Pattern"}

    def test_evidence_text_lower(self):
        state = _state(rsi=28.0)
        c = RangeEdgeFadeStrategy().detect(_ctx(state, range_position_pct=15.0))
        rp_ev = next(e for e in c.evidence if e.dimension == "RangePosition")
        assert "下沿" in rp_ev.observation
        rsi_ev = next(e for e in c.evidence if e.dimension == "RSI")
        assert "超卖" in rsi_ev.observation
        assert "28" in rsi_ev.observation

    def test_evidence_text_upper(self):
        state = _state(rsi=72.0, candles=[_normal(), _bearish_pin()])
        c = RangeEdgeFadeStrategy().detect(_ctx(state, range_position_pct=85.0))
        rp_ev = next(e for e in c.evidence if e.dimension == "RangePosition")
        assert "上沿" in rp_ev.observation
        rsi_ev = next(e for e in c.evidence if e.dimension == "RSI")
        assert "超买" in rsi_ev.observation


# ════════════════════════════════════════════════════════════════════════════
# P0-5 二次确认拒绝路径
# ════════════════════════════════════════════════════════════════════════════

class TestSecondaryConfirmations:
    """P0-5：4 重二次确认，缺一即拒"""

    def test_high_adx_rejected(self):
        """ADX ≥ 25 → 趋势抬头，区间不真"""
        state = _state(rsi=28.0, adx=28.0)  # 趋势开始
        assert RangeEdgeFadeStrategy().detect(_ctx(state, range_position_pct=15.0)) is None

    def test_no_adx_rejected(self):
        """ADX 缺失 → fail-closed"""
        state = _state(rsi=28.0, regime_snapshot=SimpleNamespace(adx=0.0))
        assert RangeEdgeFadeStrategy().detect(_ctx(state, range_position_pct=15.0)) is None

    def test_no_regime_snapshot_rejected(self):
        """完全没 regime_snapshot → fail-closed"""
        state = _state(rsi=28.0)
        state.regime_snapshot = None
        assert RangeEdgeFadeStrategy().detect(_ctx(state, range_position_pct=15.0)) is None

    def test_test_count_too_low_rejected(self):
        """边界测试不足 → 区间未充分形成"""
        state = _state(
            rsi=28.0, range_upper_test_count=1, range_lower_test_count=1,
        )
        assert RangeEdgeFadeStrategy().detect(_ctx(state, range_position_pct=15.0)) is None

    def test_box_state_broken_rejected(self):
        """箱体已破位 → 拒绝"""
        state = _state(rsi=28.0, box_state="broken")
        assert RangeEdgeFadeStrategy().detect(_ctx(state, range_position_pct=15.0)) is None

    def test_box_state_breaking_up_rejected(self):
        state = _state(rsi=28.0, box_state="breaking_up")
        assert RangeEdgeFadeStrategy().detect(_ctx(state, range_position_pct=15.0)) is None

    def test_box_state_forming_rejected(self):
        """forming 不算"已确认"区间 → 拒绝"""
        state = _state(rsi=28.0, box_state="forming")
        assert RangeEdgeFadeStrategy().detect(_ctx(state, range_position_pct=15.0)) is None

    def test_box_too_narrow_rejected(self):
        """箱体宽度 < 1.5% → 噪音内部"""
        state = _state(rsi=28.0, box_width_pct=0.8)
        assert RangeEdgeFadeStrategy().detect(_ctx(state, range_position_pct=15.0)) is None

    def test_no_range_signal_rejected(self):
        """range_signal 缺失 → fail-closed"""
        state = _state(rsi=28.0, range_signal=None)
        # 显式置 None（覆盖 _state 的默认）
        state.range_signal = None
        assert RangeEdgeFadeStrategy().detect(_ctx(state, range_position_pct=15.0)) is None

    def test_box_state_squeeze_accepted(self):
        """squeeze 也是合法的"区间真"状态"""
        state = _state(rsi=28.0, box_state="squeeze")
        c = RangeEdgeFadeStrategy().detect(_ctx(state, range_position_pct=15.0))
        assert c is not None
        # 二次确认通过给的 +0.05 强度奖励，validate triggered_conditions 包含
        assert any("box_state=squeeze" in t for t in c.triggered_conditions)
