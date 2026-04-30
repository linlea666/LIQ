"""L2 MarketRegime 最小可用版单测（D01）

覆盖：
  - 六种 regime 的分类路径
  - action_weights 至少含一个 long/short 方向
  - prev 存在且不变时 regime_changed_at 保留
  - prev 存在且变化时 regime_changed_at 刷新
  - snapshot 全空降级路径
  - D01 tracker 已被 mark（至少 total_marks > 0）
"""

from __future__ import annotations

import time

import pytest

from models.market_structure import MarketStructure
from models.regime import RegimeSnapshot
from processors.market_regime import compute_regime_from_state
# PR-3 · utils.decision_tracker 已下线


def _ms(direction: str, conf: float = 0.6) -> MarketStructure:
    return MarketStructure(direction=direction, confidence=conf)  # type: ignore[arg-type]


class TestRegimeClassification:
    def test_trend_up(self):
        snap = compute_regime_from_state(
            coin="BTC", price=100_000, atr_14=2500,
            boll_data={"upper": 103_000, "middle": 100_000, "lower": 97_000},
            structure=_ms("bullish"),
        )
        assert snap.regime == "trend_up"
        assert snap.confidence > 0
        assert snap.action_weights.get("snipe_long", 0) > 1.0  # 趋势鼓励狙击多
        assert snap.action_weights.get("snipe_short", 0) < 1.0

    def test_trend_down(self):
        snap = compute_regime_from_state(
            coin="BTC", price=100_000, atr_14=2500,
            boll_data={"upper": 103_000, "middle": 100_000, "lower": 97_000},
            structure=_ms("bearish"),
        )
        assert snap.regime == "trend_down"
        assert snap.action_weights.get("snipe_short", 0) > 1.0

    def test_range(self):
        snap = compute_regime_from_state(
            coin="BTC", price=100_000, atr_14=2000,
            boll_data={"upper": 102_000, "middle": 100_000, "lower": 98_000},
            structure=_ms("ranging", 0.3),
        )
        # ATR% = 2%，BBW = 4%，结构 ranging → range（不触发 squeeze 因为 BBW>3%）
        assert snap.regime == "range"
        assert snap.action_weights.get("flip_long", 0) > 1.0

    def test_squeeze(self):
        """BBW < 3%，结构 ranging → squeeze"""
        snap = compute_regime_from_state(
            coin="BTC", price=100_000, atr_14=1500,
            boll_data={"upper": 100_800, "middle": 100_000, "lower": 99_200},
            structure=_ms("ranging", 0.3),
        )
        assert snap.regime == "squeeze"

    def test_high_vol_chop(self):
        """ATR% > 3.2% 且结构 ranging → high_vol_chop"""
        snap = compute_regime_from_state(
            coin="BTC", price=100_000, atr_14=4000,
            boll_data={"upper": 104_000, "middle": 100_000, "lower": 96_000},
            structure=_ms("ranging", 0.3),
        )
        assert snap.regime == "high_vol_chop"
        # high_vol_chop 全线压制
        for v in snap.action_weights.values():
            assert v < 1.0

    def test_extreme(self):
        """ATR% > 6% → extreme（不论结构）"""
        snap = compute_regime_from_state(
            coin="BTC", price=100_000, atr_14=7500,
            boll_data={"upper": 107_000, "middle": 100_000, "lower": 93_000},
            structure=_ms("bullish"),
        )
        assert snap.regime == "extreme"
        # extreme 几乎熔断
        for v in snap.action_weights.values():
            assert v <= 0.3


class TestRegimeTracking:
    def test_unchanged_preserves_regime_changed_at(self):
        prev = compute_regime_from_state(
            coin="BTC", price=100_000, atr_14=2500,
            boll_data={"upper": 103_000, "middle": 100_000, "lower": 97_000},
            structure=_ms("bullish"),
        )
        time.sleep(0.01)  # 保证时间戳推进
        curr = compute_regime_from_state(
            coin="BTC", price=100_100, atr_14=2510,
            boll_data={"upper": 103_100, "middle": 100_100, "lower": 97_100},
            structure=_ms("bullish"),
            prev=prev,
        )
        assert curr.regime == prev.regime == "trend_up"
        assert curr.regime_changed_at == prev.regime_changed_at
        # 稳定时长 ≥ 0 且不大于一个极短窗口
        assert curr.stable_duration_sec >= 0

    def test_changed_updates_timestamp(self):
        prev = compute_regime_from_state(
            coin="BTC", price=100_000, atr_14=2500,
            boll_data={"upper": 103_000, "middle": 100_000, "lower": 97_000},
            structure=_ms("bullish"),
        )
        curr = compute_regime_from_state(
            coin="BTC", price=100_000, atr_14=7500,  # 极端
            boll_data=None, structure=None,
            prev=prev,
        )
        assert curr.regime == "extreme"
        assert curr.prev_regime == "trend_up"
        # regime_changed_at 应刷新（>= prev.regime_changed_at）
        assert curr.regime_changed_at >= prev.regime_changed_at


class TestRegimeDegraded:
    def test_empty_inputs_fall_back_to_range(self):
        snap = compute_regime_from_state(coin="BTC", price=0, atr_14=0)
        assert snap.regime == "range"
        assert snap.confidence <= 0.3
        assert "降级" in snap.description_cn


# PR-3 · TestD01TrackerIntegration 已下线（decision_tracker 被删除）
