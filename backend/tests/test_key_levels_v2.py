"""关键位 V2 核心逻辑单元测试

覆盖：
- ta_core 新增算法（Fibonacci / Pivot / Ichimoku / Keltner / Swing / BB Squeeze / ATR）
- confluence_scoring（聚类 / 评分 / 分类 / 合并）
- key_level_tracker_v2（状态转移 / OI 确认 / 信号生成 / 冲突解决 / 智能 TP）
- detect_oi_surge_zones
"""
from __future__ import annotations

import sys
import os
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from models.key_level import KeyLevelV2, KeyLevelSnapshotV2, KeyLevelSignal
from models.market import CandleData
from processors.ta_core import (
    calc_fibonacci_levels, calc_pivot_classic, calc_pivot_fibonacci,
    calc_pivot_camarilla, calc_ichimoku, calc_keltner, calc_bmsa,
    calc_atr, detect_swings, find_major_swing, detect_bb_squeeze,
    KeltnerResult,
)
from processors.confluence_scoring import (
    score_and_build_snapshot, _cluster_candidates, _calc_tier,
)
from processors.level_discovery import (
    RawCandidate, DiscoveryResult, detect_oi_surge_zones,
)
from processors.key_level_tracker_v2 import (
    run_tracker_v2, _calc_atr_factor, _is_broken,
    _volume_confirms_break, _oi_confirms_break, _find_opposite_target,
    _generate_scalp_signal, _DEFAULT_CFG,
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ta_core
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestFibonacci:
    def test_up_direction_basic(self):
        levels = calc_fibonacci_levels(100.0, 80.0, direction="up")
        prices = {fl.ratio: fl.price for fl in levels}
        assert prices[0.0] == 100.0
        assert prices[0.5] == 90.0
        assert prices[0.618] == pytest.approx(87.64, abs=0.01)
        assert prices[1.0] == 80.0

    def test_down_direction_basic(self):
        levels = calc_fibonacci_levels(100.0, 80.0, direction="down")
        prices = {fl.ratio: fl.price for fl in levels}
        assert prices[0.0] == 80.0
        assert prices[0.5] == 90.0
        assert prices[1.0] == 100.0

    def test_invalid_returns_empty(self):
        assert calc_fibonacci_levels(80, 100) == []
        assert calc_fibonacci_levels(100, 0) == []

    def test_extension_levels_present(self):
        levels = calc_fibonacci_levels(100.0, 80.0, direction="up")
        ext_ratios = {fl.ratio for fl in levels if fl.ratio > 1.0}
        assert 1.272 in ext_ratios
        assert 1.618 in ext_ratios


class TestPivotPoints:
    def test_classic_pivot(self):
        p = calc_pivot_classic(high=110, low=90, close=100)
        assert p.pivot == 100.0
        assert p.r1 == 110.0
        assert p.s1 == 90.0
        assert p.method == "classic"

    def test_fibonacci_pivot(self):
        p = calc_pivot_fibonacci(high=110, low=90, close=100)
        assert p.pivot == 100.0
        assert p.r1 == pytest.approx(107.64, abs=0.01)
        assert p.s1 == pytest.approx(92.36, abs=0.01)

    def test_camarilla_pivot(self):
        p = calc_pivot_camarilla(high=110, low=90, close=100)
        assert p.r1 < p.r2 < p.r3
        assert p.s1 > p.s2 > p.s3


class TestIchimoku:
    @staticmethod
    def _make_series(n, base=100, step=0.5):
        highs = [base + i * step + 5 for i in range(n)]
        lows = [base + i * step - 5 for i in range(n)]
        closes = [base + i * step for i in range(n)]
        return highs, lows, closes

    def test_insufficient_data(self):
        h, l, c = self._make_series(30)
        result = calc_ichimoku(h, l, c)
        assert result.tenkan is None

    def test_valid_data(self):
        h, l, c = self._make_series(100)
        result = calc_ichimoku(h, l, c)
        assert result.tenkan is not None
        assert result.kijun is not None
        assert result.cloud_bullish is not None

    def test_zero_value_not_dropped(self):
        h = [10.0] * 100
        l = [0.0] * 100
        c = [5.0] * 100
        result = calc_ichimoku(h, l, c)
        assert result.tenkan == 5.0


class TestKeltner:
    def test_basic(self):
        n = 50
        closes = [100 + i * 0.1 for i in range(n)]
        highs = [c + 2 for c in closes]
        lows = [c - 2 for c in closes]
        kc = calc_keltner(closes, highs, lows)
        assert kc.middle is not None
        assert kc.upper > kc.middle > kc.lower

    def test_insufficient_data(self):
        kc = calc_keltner([100], [101], [99])
        assert kc.upper is None


class TestSwingDetection:
    def test_detect_basic(self):
        highs = [10, 11, 15, 12, 10, 9, 8, 7, 6, 10, 14, 20, 15, 12, 11]
        lows = [8, 9, 13, 10, 8, 7, 5, 4, 3, 8, 12, 18, 13, 10, 9]
        swings = detect_swings(highs, lows, lookback=2)
        kinds = [s.kind for s in swings]
        assert "high" in kinds
        assert "low" in kinds

    def test_not_enough_data(self):
        assert detect_swings([1, 2, 3], [0, 1, 2], lookback=5) == []


class TestBBSqueeze:
    def test_squeeze_detected(self):
        kc = KeltnerResult(upper=110, middle=100, lower=90)
        result = detect_bb_squeeze(bb_upper=105, bb_lower=95, bb_middle=100, kc=kc)
        assert result.is_squeeze is True

    def test_no_squeeze(self):
        kc = KeltnerResult(upper=103, middle=100, lower=97)
        result = detect_bb_squeeze(bb_upper=110, bb_lower=90, bb_middle=100, kc=kc)
        assert result.is_squeeze is False

    def test_fallback_without_keltner(self):
        result = detect_bb_squeeze(bb_upper=101, bb_lower=99, bb_middle=100, kc=None)
        assert result.is_squeeze is True
        assert result.bb_width_pct < 4.0


class TestATR:
    def test_basic(self):
        n = 20
        highs = [100 + i for i in range(n)]
        lows = [98 + i for i in range(n)]
        closes = [99 + i for i in range(n)]
        atr = calc_atr(highs, lows, closes, period=14)
        assert atr[-1] is not None
        assert atr[-1] > 0

    def test_short_data(self):
        atr = calc_atr([100], [99], [99.5])
        assert all(v is None for v in atr)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# confluence_scoring
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _make_candidates():
    return [
        RawCandidate(
            price=100_000, side="support", dimension="price_structure",
            source="1D前低", source_tag="swing_low_1d", base_score=35, timeframe="1D",
        ),
        RawCandidate(
            price=100_050, side="support", dimension="capital_flow",
            source="VP成交量控制点(POC)", source_tag="vp_poc", base_score=30, timeframe="1H",
        ),
        RawCandidate(
            price=100_020, side="support", dimension="math_indicator",
            source="日线EMA200", source_tag="ema_200_1d", base_score=18, timeframe="1D",
        ),
        RawCandidate(
            price=105_000, side="resistance", dimension="capital_flow",
            source="50x空头清算$15M", source_tag="liq_cluster_above_1d",
            base_score=15, timeframe="1D",
        ),
    ]


class TestConfluenceScoring:
    def test_clustering_merges_nearby(self):
        cands = _make_candidates()[:3]
        clusters = _cluster_candidates(cands, tolerance=0.001)
        assert len(clusters) == 1
        assert clusters[0].count == 3

    def test_clustering_separates_far(self):
        cands = _make_candidates()
        clusters = _cluster_candidates(cands, tolerance=0.001)
        assert len(clusters) == 2

    def test_clustering_separates_sides(self):
        c1 = RawCandidate(
            price=100_000, side="support", dimension="price_structure",
            source="A", source_tag="a", base_score=10,
        )
        c2 = RawCandidate(
            price=100_010, side="resistance", dimension="price_structure",
            source="B", source_tag="b", base_score=10,
        )
        clusters = _cluster_candidates([c1, c2], tolerance=0.001)
        assert len(clusters) == 2

    def test_multi_dimension_bonus(self):
        cands = _make_candidates()[:3]
        dr = DiscoveryResult(candidates=cands)
        snapshot = score_and_build_snapshot(dr, current_price=100_100, atr=500)
        supports = [lv for lv in snapshot.levels if lv.side == "support"]
        assert len(supports) >= 1
        assert supports[0].confluence_score > 30

    def test_tier_thresholds(self):
        assert _calc_tier(75) == "S"
        assert _calc_tier(50) == "A"
        assert _calc_tier(30) == "B"
        assert _calc_tier(10) == "C"

    def test_structure_summary_generated(self):
        cands = _make_candidates()
        dr = DiscoveryResult(candidates=cands)
        snapshot = score_and_build_snapshot(dr, current_price=102_000, atr=500)
        assert "市场结构" in snapshot.structure_summary


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# key_level_tracker_v2
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _make_snapshot(levels, price=100_000, atr=500):
    return KeyLevelSnapshotV2(
        ts=int(time.time()), current_price=price, atr=atr,
        levels=levels, signals=[],
    )


class TestTrackerV2:
    def test_idle_to_approaching(self):
        lv = KeyLevelV2(price=100_000, side="support", state="idle")
        snap = _make_snapshot([lv], price=100_800, atr=500)
        snap = run_tracker_v2(snap, liq_map=None, sweep_events_1h=[])
        assert snap.levels[0].state == "approaching"

    def test_stays_idle_when_far(self):
        lv = KeyLevelV2(price=100_000, side="support", state="idle")
        snap = _make_snapshot([lv], price=110_000, atr=500)
        snap = run_tracker_v2(snap, liq_map=None, sweep_events_1h=[])
        assert snap.levels[0].state == "idle"

    def test_approaching_to_testing(self):
        lv = KeyLevelV2(price=100_000, side="support", state="approaching",
                        state_ts=int(time.time()) - 100)
        snap = _make_snapshot([lv], price=100_100, atr=500)
        snap = run_tracker_v2(snap, liq_map=None, sweep_events_1h=[])
        assert snap.levels[0].state == "testing"
        assert snap.levels[0].test_count >= 1

    def test_approaching_retreats_to_idle(self):
        lv = KeyLevelV2(price=100_000, side="support", state="approaching",
                        state_ts=int(time.time()) - 100)
        snap = _make_snapshot([lv], price=104_000, atr=500)
        snap = run_tracker_v2(snap, liq_map=None, sweep_events_1h=[])
        assert snap.levels[0].state == "idle"

    def test_testing_to_bounced(self):
        lv = KeyLevelV2(price=100_000, side="support", state="testing",
                        state_ts=int(time.time()) - 100, test_count=1)
        snap = _make_snapshot([lv], price=101_200, atr=500)
        snap = run_tracker_v2(snap, liq_map=None, sweep_events_1h=[])
        assert snap.levels[0].state == "bounced"

    def test_atr_factor_range(self):
        assert 0.5 <= _calc_atr_factor(500, 100_000) <= 2.0
        assert _calc_atr_factor(5000, 100_000) > _calc_atr_factor(200, 100_000)
        assert _calc_atr_factor(0, 100_000) == 0.5  # ATR=0 → 极低波动 → 最小因子

    def test_is_broken_support(self):
        lv = KeyLevelV2(price=100_000, side="support")
        assert _is_broken(lv, 99_500, depth_pct=0.3) is True
        assert _is_broken(lv, 99_800, depth_pct=0.3) is False

    def test_is_broken_resistance(self):
        lv = KeyLevelV2(price=100_000, side="resistance")
        assert _is_broken(lv, 100_500, depth_pct=0.3) is True
        assert _is_broken(lv, 100_200, depth_pct=0.3) is False

    def test_volume_confirms_break(self):
        assert _volume_confirms_break(True, 100, 90) is True
        assert _volume_confirms_break(True, 100, 50) is False
        assert _volume_confirms_break(False, 90, 100) is True
        assert _volume_confirms_break(False, 50, 100) is False
        assert _volume_confirms_break(True, 0, 0) is True

    def test_oi_confirms_break(self):
        assert _oi_confirms_break(5.0) is True
        assert _oi_confirms_break(0) is True
        assert _oi_confirms_break(-2.0) is True
        assert _oi_confirms_break(-4.0) is False

    def test_find_opposite_target_long(self):
        lv = KeyLevelV2(price=100_000, side="support")
        targets = [
            KeyLevelV2(price=100_500, side="resistance"),
            KeyLevelV2(price=103_000, side="resistance"),
        ]
        tp = _find_opposite_target(lv, 100_000, targets, atr=500)
        assert tp >= 101_000

    def test_find_opposite_target_short(self):
        lv = KeyLevelV2(price=100_000, side="resistance")
        targets = [
            KeyLevelV2(price=99_500, side="support"),
            KeyLevelV2(price=97_000, side="support"),
        ]
        tp = _find_opposite_target(lv, 100_000, targets, atr=500)
        assert tp <= 99_000

    def test_find_opposite_target_fallback(self):
        lv = KeyLevelV2(price=100_000, side="support")
        tp = _find_opposite_target(lv, 100_000, [], atr=500)
        assert tp == 101_500

    def test_signal_generation_on_swept(self):
        lv = KeyLevelV2(
            price=100_000, side="support", state="swept",
            state_ts=int(time.time()) - 60, sweep_usd=5_000_000,
        )
        res = KeyLevelV2(price=102_000, side="resistance", state="idle")
        snap = _make_snapshot([lv, res], price=100_100, atr=500)
        snap = run_tracker_v2(snap, liq_map=None, sweep_events_1h=[])
        a_signals = [s for s in snap.signals if s.confidence == "A"]
        assert len(a_signals) >= 1
        assert a_signals[0].action == "snipe_long"
        assert a_signals[0].rr_ratio > 0

    def test_signal_expiration(self):
        old_ts = int(time.time()) - 20_000
        lv = KeyLevelV2(
            price=100_000, side="support", state="bounced",
            state_ts=old_ts, test_count=1,
        )
        snap = _make_snapshot([lv], price=101_000, atr=500)
        snap = run_tracker_v2(snap, liq_map=None, sweep_events_1h=[])
        assert len(snap.signals) == 0

    def test_conflict_resolution(self):
        lv_sup = KeyLevelV2(
            price=99_000, side="support", state="swept",
            state_ts=int(time.time()) - 60, sweep_usd=10_000_000,
        )
        lv_res = KeyLevelV2(
            price=101_000, side="resistance", state="swept",
            state_ts=int(time.time()) - 60, sweep_usd=10_000_000,
        )
        snap = _make_snapshot([lv_sup, lv_res], price=100_000, atr=500)
        snap = run_tracker_v2(
            snap, liq_map=None, sweep_events_1h=[], temperature_score=30,
        )
        a_signals = [s for s in snap.signals if s.confidence == "A"]
        b_signals = [s for s in snap.signals if s.confidence == "B"]
        assert len(a_signals) == 1
        assert len(b_signals) >= 1
        assert a_signals[0].action in ("snipe_long", "flip_long")

    def test_find_opposite_target_respects_min_rr_atr_mult(self):
        """P0-1 回归：scalp 场景应能传 min_rr_atr_mult=0.5 让 TP 贴近最近对侧 level。"""
        lv = KeyLevelV2(price=100_000, side="support")
        nearby_resistance = [KeyLevelV2(price=100_300, side="resistance")]
        # 默认 mult=2.0 → TP 被抬到 price+2×ATR = 101_000（忽略 100_300 这个最近对侧）
        tp_default = _find_opposite_target(lv, 100_000, nearby_resistance, atr=500)
        assert tp_default == 101_000
        # mult=0.5 → TP 贴近最近对侧 level = 100_300（max(100_300, 100_250)）
        tp_scalp = _find_opposite_target(
            lv, 100_000, nearby_resistance, atr=500, min_rr_atr_mult=0.5,
        )
        assert tp_scalp == 100_300


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# _generate_scalp_signal（日内极小止损档）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _hammer_candles(base_price: float) -> list[CandleData]:
    """构造带锤子线形态的 15m K 线（看涨反转，strength=0.85）。

    最后一根：实体小、下影线长（≥2 倍实体）、上影线短。
    """
    prev = CandleData(coin="BTC", ts=1700000000, o=base_price + 50,
                      h=base_price + 80, l=base_price, c=base_price + 20, vol=100)
    hammer = CandleData(coin="BTC", ts=1700000900,
                        o=base_price, h=base_price + 60,
                        l=base_price - 200, c=base_price + 50, vol=150)
    return [prev, hammer]


def _engulfing_candles(base_price: float) -> list[CandleData]:
    """构造看涨吞没形态的 15m K 线（strength=0.80）。"""
    prev = CandleData(coin="BTC", ts=1700000000, o=base_price + 100,
                      h=base_price + 110, l=base_price + 80, c=base_price + 90, vol=100)
    bull = CandleData(coin="BTC", ts=1700000900, o=base_price + 80,
                      h=base_price + 220, l=base_price + 70, c=base_price + 200, vol=200)
    return [prev, bull]


def _doji_candles(base_price: float) -> list[CandleData]:
    """构造十字星（strength=0.50，<scalp_min_pattern_strength=0.6 应被过滤）。"""
    prev = CandleData(coin="BTC", ts=1700000000, o=base_price + 50,
                      h=base_price + 80, l=base_price, c=base_price + 20, vol=100)
    doji = CandleData(coin="BTC", ts=1700000900, o=base_price + 50,
                      h=base_price + 250, l=base_price - 200, c=base_price + 52, vol=120)
    return [prev, doji]


def _fresh_level(price: float, side: str, tier: str, state: str,
                 cascade: float = 0.0, state_ts: int | None = None) -> KeyLevelV2:
    if state_ts is None:
        state_ts = int(time.time()) - 60
    return KeyLevelV2(
        price=price, side=side, strength_tier=tier, state=state,
        state_ts=state_ts, cascade_risk=cascade, distance_pct=0.0,
    )


class TestScalpSignal:
    """日内极小止损档（scalp）信号生成器单元测试。"""

    def test_s_tier_hammer_produces_a_level_long(self):
        """S 级支撑位 + 锤子线(0.85) → A 级 scalp_long。"""
        lv = _fresh_level(100_000, "support", "S", "bounced")
        # opposite 需足够远保证 R:R ≥ 1.5（SL 宽度约 0.2%×价格=200，entry buffer 50，risk=250 → TP 距 entry ≥ 375）
        opposite = [KeyLevelV2(price=100_600, side="resistance")]
        candles = _hammer_candles(100_000)

        sig = _generate_scalp_signal(
            lv, price=100_000, atr=500, all_levels=[lv, *opposite],
            candles_15m=candles, cfg=_DEFAULT_CFG, now=int(time.time()),
        )
        assert sig is not None
        assert sig.action == "scalp_long"
        assert sig.confidence == "A"
        assert sig.entry_price > lv.price  # 支撑位多头入场略高于 level
        assert sig.stop_loss < lv.price    # 止损在 level 下方
        assert sig.tp1 > sig.entry_price
        assert sig.rr_ratio >= 1.5

    def test_a_tier_engulfing_produces_b_level(self):
        """A 级阻力位 + 看跌吞没(0.80) → B 级 scalp_short。

        说明：scalp 的 A 级要求 S 级 level **且** pattern_strength≥0.8；
        A 级 level + 吞没(0.80) 满足 >=0.8，但 level 不是 S 级 → B 级。
        """
        lv = _fresh_level(100_000, "resistance", "A", "flipped")
        opposite = [KeyLevelV2(price=99_400, side="support")]
        # 构造看跌吞没：prev 阳线被 curr 阴线实体完全包裹
        prev = CandleData(coin="BTC", ts=1700000000, o=99_900, h=99_960,
                          l=99_890, c=99_950, vol=100)
        bear = CandleData(coin="BTC", ts=1700000900, o=100_000, h=100_050,
                          l=99_850, c=99_870, vol=200)

        sig = _generate_scalp_signal(
            lv, price=100_000, atr=500, all_levels=[lv, *opposite],
            candles_15m=[prev, bear], cfg=_DEFAULT_CFG, now=int(time.time()),
        )
        assert sig is not None
        assert sig.action == "scalp_short"
        assert sig.confidence == "B"

    def test_doji_filtered_by_min_pattern_strength(self):
        """十字星 strength=0.50 < scalp_min_pattern_strength=0.6 → 不产出。"""
        lv = _fresh_level(100_000, "support", "S", "bounced")
        opposite = [KeyLevelV2(price=100_300, side="resistance")]
        sig = _generate_scalp_signal(
            lv, price=100_000, atr=500, all_levels=[lv, *opposite],
            candles_15m=_doji_candles(100_000), cfg=_DEFAULT_CFG, now=int(time.time()),
        )
        assert sig is None

    def test_b_tier_level_filtered(self):
        """B 级 level → 不产出（scalp 要求 S/A 级）。"""
        lv = _fresh_level(100_000, "support", "B", "bounced")
        sig = _generate_scalp_signal(
            lv, price=100_000, atr=500, all_levels=[lv],
            candles_15m=_hammer_candles(100_000),
            cfg=_DEFAULT_CFG, now=int(time.time()),
        )
        assert sig is None

    def test_idle_state_filtered(self):
        """idle/approaching/broken 状态 → 不产出。"""
        lv = _fresh_level(100_000, "support", "S", "idle")
        sig = _generate_scalp_signal(
            lv, price=100_000, atr=500, all_levels=[lv],
            candles_15m=_hammer_candles(100_000),
            cfg=_DEFAULT_CFG, now=int(time.time()),
        )
        assert sig is None

    def test_distance_too_far_filtered(self):
        """距当前价 > scalp_max_distance_pct (0.8%) → 不产出。"""
        lv = _fresh_level(100_000, "support", "S", "bounced")
        lv.distance_pct = -1.5  # 偏离 1.5%
        sig = _generate_scalp_signal(
            lv, price=101_500, atr=500, all_levels=[lv],
            candles_15m=_hammer_candles(100_000),
            cfg=_DEFAULT_CFG, now=int(time.time()),
        )
        assert sig is None

    def test_cascade_risk_filtered(self):
        """cascade_risk >= scalp_max_cascade (0.5) → 不产出（级联踩踏区禁止 scalp）。"""
        lv = _fresh_level(100_000, "support", "S", "bounced", cascade=0.6)
        sig = _generate_scalp_signal(
            lv, price=100_000, atr=500, all_levels=[lv],
            candles_15m=_hammer_candles(100_000),
            cfg=_DEFAULT_CFG, now=int(time.time()),
        )
        assert sig is None

    def test_no_15m_candles_returns_none(self):
        """candles_15m 为空或过短 → 不产出。"""
        lv = _fresh_level(100_000, "support", "S", "bounced")
        for candles in (None, [], _hammer_candles(100_000)[:1]):
            sig = _generate_scalp_signal(
                lv, price=100_000, atr=500, all_levels=[lv],
                candles_15m=candles, cfg=_DEFAULT_CFG, now=int(time.time()),
            )
            assert sig is None

    def test_atr_zero_returns_none(self):
        """ATR=0 → 不产出（防止除零 / 异常止损）。"""
        lv = _fresh_level(100_000, "support", "S", "bounced")
        sig = _generate_scalp_signal(
            lv, price=100_000, atr=0, all_levels=[lv],
            candles_15m=_hammer_candles(100_000),
            cfg=_DEFAULT_CFG, now=int(time.time()),
        )
        assert sig is None

    def test_expired_state_filtered(self):
        """state_ts 超过 scalp_signal_expire_sec (30 分钟) → 不产出（P1-1 回归）。"""
        now = int(time.time())
        lv = _fresh_level(100_000, "support", "S", "bounced",
                          state_ts=now - 3600)  # 60 分钟前进入状态
        opposite = [KeyLevelV2(price=100_300, side="resistance")]
        sig = _generate_scalp_signal(
            lv, price=100_000, atr=500, all_levels=[lv, *opposite],
            candles_15m=_hammer_candles(100_000), cfg=_DEFAULT_CFG, now=now,
        )
        assert sig is None

    def test_sl_is_tiny_and_rr_uses_nearby_target(self):
        """P0-1 回归：SL 应极小 (≤0.5×ATR)，TP 应贴近最近对侧 level 而非被抬到 2×ATR。"""
        lv = _fresh_level(100_000, "support", "S", "bounced")
        nearby_res = KeyLevelV2(price=100_600, side="resistance")
        sig = _generate_scalp_signal(
            lv, price=100_000, atr=500, all_levels=[lv, nearby_res],
            candles_15m=_hammer_candles(100_000),
            cfg=_DEFAULT_CFG, now=int(time.time()),
        )
        assert sig is not None
        sl_width = abs(sig.entry_price - sig.stop_loss)
        assert sl_width <= 500 * 0.5 + 1   # SL ≤ 0.5×ATR + 浮点余量
        # TP 不应被 2×ATR=1000 抬到 101_000，应贴近最近对侧 100_600
        assert sig.tp1 <= 100_700, f"TP {sig.tp1} should stay near opposite level 100_600, not be raised to price+2×ATR=101_000"

    def test_integration_via_run_tracker_v2(self):
        """黑盒集成测试：run_tracker_v2 能正确调用 scalp 生成器并输出信号。"""
        now = int(time.time())
        lv = _fresh_level(100_000, "support", "S", "bounced", state_ts=now - 60)
        res = KeyLevelV2(price=100_600, side="resistance", state="idle")
        snap = _make_snapshot([lv, res], price=100_050, atr=500)
        snap = run_tracker_v2(
            snap, liq_map=None, sweep_events_1h=[],
            candles_15m=_hammer_candles(100_000),
        )
        scalp_sigs = [s for s in snap.signals if s.action == "scalp_long"]
        assert len(scalp_sigs) == 1
        assert scalp_sigs[0].confidence == "A"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# detect_oi_surge_zones
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _make_candles(n, base_ts=1700000000, base_price=100_000):
    return [
        CandleData(
            coin="BTC", ts=base_ts + i * 3600,
            o=base_price + i * 10, h=base_price + i * 10 + 50,
            l=base_price + i * 10 - 50, c=base_price + i * 10 + 5,
            vol=1000,
        )
        for i in range(n)
    ]


class TestOISurgeZones:
    def test_surge_detected(self):
        oi_hist = [
            {"ts": 1700000000, "oi": 100_000},
            {"ts": 1700003600, "oi": 100_000},
            {"ts": 1700007200, "oi": 110_000},
        ]
        candles = _make_candles(5)
        zones = detect_oi_surge_zones(oi_hist, candles, threshold_pct=5.0)
        assert len(zones) >= 1
        assert zones[0][1] == 10.0

    def test_no_surge_below_threshold(self):
        oi_hist = [
            {"ts": 1700000000, "oi": 100_000},
            {"ts": 1700003600, "oi": 101_000},
            {"ts": 1700007200, "oi": 102_000},
        ]
        candles = _make_candles(5)
        zones = detect_oi_surge_zones(oi_hist, candles, threshold_pct=5.0)
        assert len(zones) == 0

    def test_empty_input(self):
        assert detect_oi_surge_zones([], None) == []
        assert detect_oi_surge_zones(None, []) == []
