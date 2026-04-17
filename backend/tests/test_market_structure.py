"""市场结构识别器单元测试

覆盖：
- 边界：空/不足数据
- 方向判定：bullish / bearish / ranging
- 事件检测：BOS_up / BOS_down / CHoCH_up / CHoCH_down
- 置信度 & 操作偏置
- 毛刺过滤（min_gap_pct / ATR）
- Summary 白话
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from models.market import CandleData
from models.market_structure import MarketStructure
from processors.market_structure import (
    detect_market_structure,
    _classify_direction,
    _detect_event,
    _filter_close_swings,
    _calc_bias,
)
from processors.ta_core import SwingPoint


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 测试辅助
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _make_zigzag(
    waves: list[float],
    bars_per_wave: int = 7,
    start_ts: int = 1700000000,
    interval: int = 3600,
    wick: float = 0.002,
) -> list[CandleData]:
    """waves = 折线顶点，相邻线性插值生成 K 线序列。"""
    prices: list[float] = []
    for i in range(len(waves) - 1):
        start, end = waves[i], waves[i + 1]
        for j in range(bars_per_wave):
            prices.append(start + (end - start) * j / bars_per_wave)
    prices.append(waves[-1])
    return [
        CandleData(
            coin="BTC",
            ts=start_ts + i * interval,
            o=p,
            h=p * (1 + wick),
            l=p * (1 - wick),
            c=p,
            vol=1000.0,
        )
        for i, p in enumerate(prices)
    ]


def _flat(n: int, price: float = 100.0) -> list[CandleData]:
    return [
        CandleData(
            coin="BTC",
            ts=1700000000 + i * 3600,
            o=price,
            h=price,
            l=price,
            c=price,
            vol=1000.0,
        )
        for i in range(n)
    ]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 边界
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestBoundary:
    def test_empty_candles_returns_none(self):
        assert detect_market_structure([]) is None

    def test_insufficient_candles_returns_none(self):
        """默认 min_candles=50，少于此数返回 None。"""
        assert detect_market_structure(_flat(30)) is None

    def test_zero_price_returns_none(self):
        candles = [
            CandleData(coin="BTC", ts=i, o=0, h=0, l=0, c=0, vol=0)
            for i in range(60)
        ]
        assert detect_market_structure(candles) is None

    def test_flat_prices_ranging(self):
        """完全平坦 → 方向必为 ranging（即使 detect_swings 因 >= 比较产生同价"swing"）。"""
        res = detect_market_structure(_flat(60), min_candles=30)
        assert res is not None
        assert res.direction == "ranging"
        # 平坦序列毛刺过滤后至多各 1 个 high/low（同价合并），不够判定趋势
        assert len(res.swing_highs) <= 1
        assert len(res.swing_lows) <= 1


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 方向判定
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestDirection:
    def test_bullish_hh_hl(self):
        """HH + HL 连续递增 → bullish。"""
        waves = [100, 110, 102, 115, 105, 120, 108]
        candles = _make_zigzag(waves, bars_per_wave=8)
        res = detect_market_structure(candles, min_candles=30)
        assert res is not None
        assert res.direction == "bullish"
        assert res.operate_bias in ("long_only", "stand_aside")
        assert res.confidence > 0

    def test_bearish_lh_ll(self):
        """LH + LL 连续递减 → bearish。"""
        waves = [120, 108, 118, 104, 115, 100, 112]
        candles = _make_zigzag(waves, bars_per_wave=8)
        res = detect_market_structure(candles, min_candles=30)
        assert res is not None
        assert res.direction == "bearish"
        assert res.operate_bias in ("short_only", "stand_aside")

    def test_ranging_mixed(self):
        """波峰波谷互相缠绕，无明显趋势 → ranging 或 transitioning。"""
        waves = [100, 108, 102, 108, 102, 108, 102]  # 平顶平底
        candles = _make_zigzag(waves, bars_per_wave=8)
        res = detect_market_structure(candles, min_candles=30)
        assert res is not None
        # 完全等高的顶底，direction 应非 bullish/bearish
        assert res.direction in ("ranging", "transitioning")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 事件检测（_detect_event 直接测）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestEvent:
    def test_bos_up_in_bullish(self):
        highs = [SwingPoint(index=40, price=115, kind="high", ts=100)]
        lows = [SwingPoint(index=30, price=105, kind="low", ts=90)]
        ev, ts, price = _detect_event(120, highs, lows, "bullish", 200)
        assert ev == "BOS_up"
        assert ts == 200
        assert price == 120

    def test_choch_down_in_bullish(self):
        highs = [SwingPoint(index=40, price=115, kind="high", ts=100)]
        lows = [SwingPoint(index=30, price=105, kind="low", ts=90)]
        ev, _, _ = _detect_event(100, highs, lows, "bullish", 200)
        assert ev == "CHoCH_down"

    def test_bos_down_in_bearish(self):
        highs = [SwingPoint(index=30, price=115, kind="high", ts=90)]
        lows = [SwingPoint(index=40, price=105, kind="low", ts=100)]
        ev, _, _ = _detect_event(100, highs, lows, "bearish", 200)
        assert ev == "BOS_down"

    def test_choch_up_in_bearish(self):
        highs = [SwingPoint(index=30, price=115, kind="high", ts=90)]
        lows = [SwingPoint(index=40, price=105, kind="low", ts=100)]
        ev, _, _ = _detect_event(120, highs, lows, "bearish", 200)
        assert ev == "CHoCH_up"

    def test_no_event_inside_range(self):
        highs = [SwingPoint(index=40, price=115, kind="high", ts=100)]
        lows = [SwingPoint(index=30, price=105, kind="low", ts=90)]
        ev, _, _ = _detect_event(110, highs, lows, "bullish", 200)
        assert ev == ""

    def test_no_swings_no_event(self):
        ev, ts, price = _detect_event(100, [], [], "ranging", 200)
        assert ev == ""
        assert ts == 0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 操作偏置
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestOperateBias:
    def test_bullish_high_conf_long_only(self):
        assert _calc_bias("bullish", 0.8) == "long_only"

    def test_bearish_high_conf_short_only(self):
        assert _calc_bias("bearish", 0.8) == "short_only"

    def test_ranging_both_ok(self):
        assert _calc_bias("ranging", 0.5) == "both_ok"

    def test_low_conf_stand_aside(self):
        assert _calc_bias("bullish", 0.1) == "stand_aside"
        assert _calc_bias("bearish", 0.2) == "stand_aside"

    def test_transitioning_stand_aside(self):
        assert _calc_bias("transitioning", 0.8) == "stand_aside"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 毛刺过滤
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestFilterCloseSwings:
    def test_merge_close_highs_keeps_extreme(self):
        """两个相近的 high，距离小于 min_gap → 保留更高的。"""
        swings = [
            SwingPoint(index=10, price=100.0, kind="high"),
            SwingPoint(index=12, price=100.3, kind="high"),  # 只差 0.3
        ]
        result = _filter_close_swings(swings, min_gap=1.0)
        assert len(result) == 1
        assert result[0].price == 100.3

    def test_keeps_distinct_swings(self):
        swings = [
            SwingPoint(index=10, price=100.0, kind="high"),
            SwingPoint(index=20, price=105.0, kind="high"),
        ]
        result = _filter_close_swings(swings, min_gap=1.0)
        assert len(result) == 2

    def test_different_kinds_not_merged(self):
        """一 high 一 low 即使价相近也不合并。"""
        swings = [
            SwingPoint(index=10, price=100.0, kind="high"),
            SwingPoint(index=12, price=100.2, kind="low"),
        ]
        result = _filter_close_swings(swings, min_gap=1.0)
        assert len(result) == 2

    def test_zero_gap_noop(self):
        swings = [
            SwingPoint(index=10, price=100.0, kind="high"),
            SwingPoint(index=12, price=100.0, kind="high"),
        ]
        result = _filter_close_swings(swings, min_gap=0)
        assert len(result) == 2


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# _classify_direction 直接测（避免端到端依赖）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestClassify:
    def test_not_enough_swings_ranging(self):
        assert _classify_direction([], []) == "ranging"
        assert _classify_direction(
            [SwingPoint(index=10, price=100, kind="high")],
            [SwingPoint(index=5, price=90, kind="low")],
        ) == "ranging"

    def test_hh_hl_bullish(self):
        highs = [
            SwingPoint(index=20, price=115, kind="high"),
            SwingPoint(index=10, price=110, kind="high"),
        ]
        lows = [
            SwingPoint(index=15, price=105, kind="low"),
            SwingPoint(index=5, price=100, kind="low"),
        ]
        assert _classify_direction(highs, lows) == "bullish"

    def test_lh_ll_bearish(self):
        highs = [
            SwingPoint(index=20, price=110, kind="high"),
            SwingPoint(index=10, price=115, kind="high"),
        ]
        lows = [
            SwingPoint(index=15, price=100, kind="low"),
            SwingPoint(index=5, price=105, kind="low"),
        ]
        assert _classify_direction(highs, lows) == "bearish"

    def test_hh_ll_transitioning(self):
        """HH + LL（高点抬高但低点走低）→ transitioning"""
        highs = [
            SwingPoint(index=20, price=115, kind="high"),
            SwingPoint(index=10, price=110, kind="high"),
        ]
        lows = [
            SwingPoint(index=15, price=100, kind="low"),
            SwingPoint(index=5, price=105, kind="low"),
        ]
        assert _classify_direction(highs, lows) == "transitioning"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 端到端 Summary
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestSummary:
    def test_summary_contains_direction(self):
        waves = [100, 110, 102, 115, 105, 120, 108]
        candles = _make_zigzag(waves, bars_per_wave=8)
        res = detect_market_structure(candles, min_candles=30)
        assert res is not None
        assert "上升" in res.summary or "结构" in res.summary
        assert "1h" in res.summary

    def test_summary_contains_range(self):
        waves = [100, 110, 102, 115, 105, 120, 108]
        candles = _make_zigzag(waves, bars_per_wave=8)
        res = detect_market_structure(candles, min_candles=30)
        assert res is not None
        assert "$" in res.summary


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ATR 毛刺过滤
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestAtrGap:
    def test_atr_enlarges_gap_filter(self):
        """传入大 ATR，更多相近摆动会被合并。"""
        waves = [100, 110, 108, 112, 109, 115, 108]  # 小毛刺 108-110
        candles = _make_zigzag(waves, bars_per_wave=8)

        res_no_atr = detect_market_structure(candles, atr=None, min_candles=30)
        res_big_atr = detect_market_structure(candles, atr=5.0, min_candles=30)
        assert res_no_atr is not None and res_big_atr is not None
        # ATR 过滤后 swing 数量应 ≤ 未过滤
        assert (
            len(res_big_atr.swing_highs) + len(res_big_atr.swing_lows)
        ) <= (
            len(res_no_atr.swing_highs) + len(res_no_atr.swing_lows)
        )
