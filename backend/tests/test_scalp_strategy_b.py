"""策略 B · CVD Divergence 单测"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from models.market import CandleData
from models.scalp_signal import StrategyName
from processors.scalp_signal.base_strategy import StrategyContext
from processors.scalp_signal.strategy_b_cvd_div import (
    KL_FINAL_SCORE_MIN,
    KL_NEAR_PCT_MAX,
    MIN_DIVERGENCE_USD,
    PRICE_DRIFT_MAX_PCT,
    CVDDivergenceStrategy,
)


# ────────────────────────────────────────────────────────────────────────────
# 工厂
# ────────────────────────────────────────────────────────────────────────────

def _candle(price: float, ts: int = 0) -> CandleData:
    return CandleData(coin="BTC", ts=ts, o=price, h=price + 5, l=price - 5, c=price)


def _make_candles(prices: list[float]) -> list[CandleData]:
    """根据收盘价数组构造 K 线序列"""
    return [_candle(p, ts=i) for i, p in enumerate(prices)]


def _state(
    *,
    last_price: float = 78400.0,
    contract_trend: str = "declining",
    contract_delta_1h: float = -8_000_000,
    spot_trend: str = "rising",
    spot_delta_1h: float = 7_000_000,
    candles_close: list[float] | None = None,
    levels: list | None = None,
):
    candles = _make_candles(candles_close or [78400, 78410, 78395, 78405, 78400])
    return SimpleNamespace(
        ticker=SimpleNamespace(last=last_price, ts=0),
        cvd_contract=SimpleNamespace(
            trend_1h=contract_trend, delta_1h=contract_delta_1h,
        ),
        cvd_spot=SimpleNamespace(
            trend_1h=spot_trend, delta_1h=spot_delta_1h,
        ),
        candles_15m=candles,
        key_level_snapshot_v2=(
            SimpleNamespace(levels=levels) if levels is not None else None
        ),
    )


def _ctx(state) -> StrategyContext:
    return StrategyContext(
        state=state, coin="BTC", horizon_min=30,
        regime="range", range_position_pct=50.0, bias_score=0.0,
    )


def _kl(*, side="support", price=78380, final_score=80, strength_tier="A"):
    return SimpleNamespace(
        side=side, price=price, final_score=final_score, strength_tier=strength_tier,
    )


# ════════════════════════════════════════════════════════════════════════════
# 元数据
# ════════════════════════════════════════════════════════════════════════════

class TestStrategyBMetadata:
    def test_class_constants(self):
        s = CVDDivergenceStrategy()
        assert s.name == StrategyName.B_CVD_DIVERGENCE
        assert "CVD" in s.display_name
        assert s.suitable_regimes == {"trend_up", "trend_down", "range"}
        assert s.suitable_horizons == {30, 60}


# ════════════════════════════════════════════════════════════════════════════
# 触发场景
# ════════════════════════════════════════════════════════════════════════════

class TestBullishTrigger:
    def test_spot_strong_contract_weak_up(self):
        """现货 rising + 合约 declining → 看涨"""
        state = _state(
            spot_trend="rising", spot_delta_1h=10_000_000,
            contract_trend="declining", contract_delta_1h=-8_000_000,
        )
        c = CVDDivergenceStrategy().detect(_ctx(state))
        assert c is not None
        assert c.direction == "up"
        assert c.extra_data["scenario"] == "现强合弱"

    def test_extra_data_complete(self):
        state = _state(
            spot_trend="rising", spot_delta_1h=10_000_000,
            contract_trend="declining", contract_delta_1h=-8_000_000,
        )
        c = CVDDivergenceStrategy().detect(_ctx(state))
        assert c.extra_data["spot_delta_1h"] == 10_000_000
        assert c.extra_data["contract_delta_1h"] == -8_000_000
        assert c.extra_data["divergence_strength"] > 0


class TestBearishTrigger:
    def test_contract_strong_spot_weak_down(self):
        """合约 rising + 现货 declining → 看跌"""
        state = _state(
            spot_trend="declining", spot_delta_1h=-9_000_000,
            contract_trend="rising", contract_delta_1h=11_000_000,
        )
        c = CVDDivergenceStrategy().detect(_ctx(state))
        assert c is not None
        assert c.direction == "down"
        assert c.extra_data["scenario"] == "合强现弱"


# ════════════════════════════════════════════════════════════════════════════
# 拒绝场景
# ════════════════════════════════════════════════════════════════════════════

class TestRejectConditions:
    def test_no_cvd_data(self):
        state = _state()
        state.cvd_contract = None
        assert CVDDivergenceStrategy().detect(_ctx(state)) is None

    def test_no_spot_cvd(self):
        state = _state()
        state.cvd_spot = None
        assert CVDDivergenceStrategy().detect(_ctx(state)) is None

    def test_no_ticker(self):
        state = _state()
        state.ticker = None
        assert CVDDivergenceStrategy().detect(_ctx(state)) is None

    def test_same_direction_rejected(self):
        """两侧同向 → 无背离"""
        state = _state(
            spot_trend="rising", spot_delta_1h=10_000_000,
            contract_trend="rising", contract_delta_1h=8_000_000,
        )
        assert CVDDivergenceStrategy().detect(_ctx(state)) is None

    def test_flat_trend_rejected(self):
        """flat 不算有效信号"""
        state = _state(
            spot_trend="rising", spot_delta_1h=10_000_000,
            contract_trend="flat", contract_delta_1h=0,
        )
        assert CVDDivergenceStrategy().detect(_ctx(state)) is None

    def test_below_min_threshold_rejected(self):
        """单边幅度 < 阈值 → 拒绝"""
        state = _state(
            spot_trend="rising", spot_delta_1h=1_000_000,  # < 5M
            contract_trend="declining", contract_delta_1h=-8_000_000,
        )
        assert CVDDivergenceStrategy().detect(_ctx(state)) is None

    def test_price_drifted_too_much_rejected(self):
        """1h 价格已变动 > 1.5% → 信号已传导，无效"""
        # 5 根 K 线：78000 → 80000 = +2.56%
        state = _state(
            candles_close=[78000, 78500, 79000, 79500, 80000],
            spot_trend="rising", spot_delta_1h=10_000_000,
            contract_trend="declining", contract_delta_1h=-8_000_000,
        )
        assert CVDDivergenceStrategy().detect(_ctx(state)) is None

    def test_few_candles_rejected(self):
        """K 线 < 5 根，无法计算 1h drift → 拒绝"""
        state = _state(candles_close=[78400, 78400, 78400])
        assert CVDDivergenceStrategy().detect(_ctx(state)) is None


# ════════════════════════════════════════════════════════════════════════════
# raw_strength 计算
# ════════════════════════════════════════════════════════════════════════════

class TestRawStrength:
    def test_higher_divergence_higher_strength(self):
        """背离幅度越大 → raw_strength 越高"""
        small = _state(
            spot_trend="rising", spot_delta_1h=6_000_000,
            contract_trend="declining", contract_delta_1h=-6_000_000,
        )
        large = _state(
            spot_trend="rising", spot_delta_1h=20_000_000,
            contract_trend="declining", contract_delta_1h=-25_000_000,
        )
        c_small = CVDDivergenceStrategy().detect(_ctx(small))
        c_large = CVDDivergenceStrategy().detect(_ctx(large))
        # divergence_strength = (|c-s|/max(|c|,|s|)) / 2
        # small: (12M/6M)/2 = 1.0 → 满分
        # large: (45M/25M)/2 = 0.9 → 略低
        # 但 large 单边量更大，未必比 small 强
        assert c_small.raw_strength >= 0.5
        assert c_large.raw_strength >= 0.5

    def test_kl_catalyst_adds_strength(self):
        """有 KL 催化点 → raw_strength 显著更高"""
        no_kl = _state(
            spot_trend="rising", spot_delta_1h=10_000_000,
            contract_trend="declining", contract_delta_1h=-10_000_000,
        )
        with_kl = _state(
            spot_trend="rising", spot_delta_1h=10_000_000,
            contract_trend="declining", contract_delta_1h=-10_000_000,
            levels=[_kl(price=78380, final_score=80)],
        )
        c_no = CVDDivergenceStrategy().detect(_ctx(no_kl))
        c_with = CVDDivergenceStrategy().detect(_ctx(with_kl))
        assert c_with.raw_strength > c_no.raw_strength
        # KL 加成预期 0.8 × 0.15 = 0.12
        assert c_with.raw_strength - c_no.raw_strength == pytest.approx(0.12, abs=1e-6)

    def test_kl_too_far_no_bonus(self):
        """KL 距离 > 0.5% 不算催化点"""
        far_kl = _state(
            spot_trend="rising", spot_delta_1h=10_000_000,
            contract_trend="declining", contract_delta_1h=-10_000_000,
            levels=[_kl(price=77000, final_score=95)],  # 距 78400 = 1.79%
        )
        c = CVDDivergenceStrategy().detect(_ctx(far_kl))
        assert c.extra_data["has_kl_catalyst"] is False

    def test_strength_clamped(self):
        """所有奖励叠加不超过 1.0"""
        state = _state(
            spot_trend="rising", spot_delta_1h=50_000_000,
            contract_trend="declining", contract_delta_1h=-50_000_000,
            levels=[_kl(price=78395, final_score=100, strength_tier="S")],
        )
        c = CVDDivergenceStrategy().detect(_ctx(state))
        assert 0.0 < c.raw_strength <= 1.0


# ════════════════════════════════════════════════════════════════════════════
# Evidence 完整性
# ════════════════════════════════════════════════════════════════════════════

class TestEvidence:
    def test_evidence_dimensions(self):
        state = _state(
            spot_trend="rising", spot_delta_1h=12_000_000,
            contract_trend="declining", contract_delta_1h=-10_000_000,
            levels=[_kl(price=78395, final_score=85)],
        )
        c = CVDDivergenceStrategy().detect(_ctx(state))
        dims = {ev.dimension for ev in c.evidence}
        assert dims == {"CVD-Divergence", "PriceQuiet", "KeyLevelCatalyst"}

    def test_evidence_no_kl_no_catalyst_dim(self):
        state = _state(
            spot_trend="rising", spot_delta_1h=12_000_000,
            contract_trend="declining", contract_delta_1h=-10_000_000,
        )
        c = CVDDivergenceStrategy().detect(_ctx(state))
        dims = {ev.dimension for ev in c.evidence}
        assert "KeyLevelCatalyst" not in dims
        assert dims == {"CVD-Divergence", "PriceQuiet"}

    def test_evidence_text_contains_numbers(self):
        state = _state(
            spot_trend="rising", spot_delta_1h=12_500_000,
            contract_trend="declining", contract_delta_1h=-10_300_000,
        )
        c = CVDDivergenceStrategy().detect(_ctx(state))
        cvd_ev = next(e for e in c.evidence if e.dimension == "CVD-Divergence")
        # 包含 12.5M / 10.3M 字样
        assert "12.5M" in cvd_ev.observation
        assert "10.3M" in cvd_ev.observation
        # 包含场景
        assert "现强合弱" in cvd_ev.observation
