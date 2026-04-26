"""挂单压力监测器单元测试 (L1-L4 + 顶层 + 信号生成器)

覆盖要点：
- L1 detect_walls：±range 过滤 / 双闸阈值 / 同价位合并 / 排序与 max_walls
- L1+ tag_with_large_orders：同价位关联 / side 隔离 / has_active_whale
- L2 classify_change：大单优先 (eaten / cancelled / holding) / 散堆减量 (eaten / cancelled)
- L3 classify_pressure：ask/bid 全分支 (real_R/fake_R/fake_R_break/untested)
- L4 augment_with_absorption：confluence + confidence +25
- compute_pressure_snapshot：数据不足返回 None / 正常组装 / 汇总字段
- generate_pressure_signals：触发条件 + 30min 同价去重
"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from models.market import CandleData
from models.market_action import AbsorptionSnapshot, AbsorptionZone
from models.orderbook_pressure import (
    DepthBin,
    LargeOrderLifecycle,
    OrderbookDepthSnapshot,
    PressureWall,
)
from processors.orderbook_pressure import (
    DEFAULTS,
    augment_with_absorption,
    classify_change,
    classify_pressure,
    compute_pressure_snapshot,
    detect_walls,
    tag_with_large_orders,
)
from processors.orderbook_pressure_signal import (
    generate_pressure_signals,
    reset_dedup_for_test,
)

# ── 公共工具 ──────────────────────────────────────────────────────────────


def _bin(price: float, qty: float) -> DepthBin:
    return DepthBin(price=price, quantity=qty, usd_value=price * qty)


def _depth(
    last_price: float,
    bids: list[DepthBin],
    asks: list[DepthBin],
    *,
    prev_bids: list[DepthBin] | None = None,
    prev_asks: list[DepthBin] | None = None,
    ts_sec: int = 1_700_000_000,
) -> OrderbookDepthSnapshot:
    snap = OrderbookDepthSnapshot(
        coin="BTC", exchange="Binance", symbol="BTCUSDT",
        ts_sec=ts_sec, bids=bids, asks=asks,
    )
    if prev_bids is not None or prev_asks is not None:
        snap.prev_ts_sec = ts_sec - 90
        snap.prev_bids = prev_bids or []
        snap.prev_asks = prev_asks or []
    return snap


def _candle(ts_sec: int, close: float, *, high: float | None = None,
            low: float | None = None) -> CandleData:
    return CandleData(coin="BTC", ts=ts_sec * 1000,
                      o=close, h=high if high is not None else close,
                      l=low if low is not None else close, c=close)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# L1 — detect_walls
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_detect_walls_filters_range_and_picks_top():
    """±2% 内挑出大单堆，±2% 外的不算（本测试聚焦"范围过滤"，
    显式 override range_pct=2.0 与全局默认值解耦）。"""
    last = 100.0
    asks = [
        _bin(101.0, 100),    # $10100  ── 在范围内
        _bin(101.5, 100),    # $10150
        _bin(102.0, 60_000),  # $6,000,000  ✓ 大单墙
        _bin(105.0, 80_000),  # $8,400,000 但超出 ±2% 范围 → 过滤掉
    ]
    bids = [
        _bin(99.0, 50_000),  # $4,950,000  ✓ 买墙
        _bin(98.5, 200),     # $19,700
    ]
    cfg = {**DEFAULTS, "range_pct": 2.0}
    walls = detect_walls(_depth(last, bids, asks), last, cfg)
    sides = sorted({w.side for w in walls})
    assert sides == ["ask", "bid"]
    ask_w = next(w for w in walls if w.side == "ask")
    bid_w = next(w for w in walls if w.side == "bid")
    # 范围外 105 不应入选
    assert 101.5 <= ask_w.price_mid <= 102.5
    # 仅大单达到 wall_min_usd，小数额被双闸过滤
    assert ask_w.size_usd >= DEFAULTS["wall_min_usd"]
    assert bid_w.size_usd >= DEFAULTS["wall_min_usd"]


def test_detect_walls_merges_neighbouring_bins():
    """相邻 ±0.05% 价位的大单合并成一个墙。

    用 cfg_override 把 wall_size_top_pct 提高到 1.0，让两个 bin 都过双闸阈值；
    主要验证合并逻辑（默认 top 20% 在 2 个 bin 时只取 1 个，覆盖不到 merge）。
    """
    last = 1000.0
    asks = [
        _bin(1010.0, 700),    # $707,000  ✓ 大墙
        _bin(1010.4, 800),    # $808,000  ✓ 大墙，与 1010.0 相距 0.4 < 0.5 (merge_tol)
    ]
    cfg = {**DEFAULTS, "wall_size_top_pct": 1.0}
    walls = detect_walls(_depth(last, [], asks), last, cfg)
    asks_w = [w for w in walls if w.side == "ask"]
    assert len(asks_w) == 1
    merged = asks_w[0]
    assert merged.price_lo == 1010.0 and merged.price_hi == 1010.4
    assert merged.size_usd >= 707_000 + 808_000 - 1


def test_detect_walls_skips_when_no_data():
    """空 depth / last_price=0 → 直接返回空，不抛错。"""
    assert detect_walls(_depth(100.0, [], []), 100.0, DEFAULTS) == []
    snap = _depth(100.0, [_bin(99, 1)], [_bin(101, 1)])
    assert detect_walls(snap, 0.0, DEFAULTS) == []


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# L1+ — tag_with_large_orders
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_tag_with_large_orders_associates_same_side_only():
    """ask wall 只关联 ask 大单，对侧 bid 大单不应误关联。"""
    wall_ask = PressureWall(
        side="ask", price_lo=100.0, price_hi=100.2, price_mid=100.1,
        distance_pct=0.1, size_usd=1_000_000, size_base=10,
    )
    wall_bid = PressureWall(
        side="bid", price_lo=98.0, price_hi=98.2, price_mid=98.1,
        distance_pct=-1.9, size_usd=900_000, size_base=10,
    )
    los = [
        LargeOrderLifecycle(id=1, side="ask", limit_price=100.05,
                            start_time_ms=1, start_quantity=100,
                            current_quantity=100, state="holding",
                            start_usd_value=10_000, current_usd_value=10_000),
        LargeOrderLifecycle(id=2, side="bid", limit_price=100.05,  # 价格落在 ask 区间
                            start_time_ms=1, start_quantity=80,
                            current_quantity=80, state="holding",
                            start_usd_value=8_000, current_usd_value=8_000),
        LargeOrderLifecycle(id=3, side="bid", limit_price=98.10,
                            start_time_ms=1, start_quantity=60,
                            current_quantity=60, state="holding",
                            start_usd_value=6_000, current_usd_value=6_000),
    ]
    tag_with_large_orders([wall_ask, wall_bid], los, last_price=100.0, cfg=DEFAULTS)
    assert wall_ask.large_order_ids == [1]
    assert wall_ask.has_active_whale is True
    assert wall_bid.large_order_ids == [3]
    assert wall_bid.has_active_whale is True


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# L2 — classify_change
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _make_tagged_wall(side="ask") -> PressureWall:
    w = PressureWall(
        side=side, price_lo=100.0, price_hi=100.2, price_mid=100.1,
        distance_pct=0.1, size_usd=1_000_000, size_base=10,
    )
    w.large_order_ids = [42]
    w.large_order_count = 1
    return w


def test_classify_change_eaten_via_large_order():
    wall = _make_tagged_wall("ask")
    now = 1_700_000_000
    los = [LargeOrderLifecycle(
        id=42, side="ask", limit_price=100.1,
        start_time_ms=(now - 1000) * 1000,
        start_quantity=10.0, current_quantity=1.0,
        executed_volume=9.0, executed_usd_value=900.0,
        start_usd_value=1000.0, current_usd_value=100.0,
        state="ended", end_time_ms=(now - 100) * 1000,
    )]
    classify_change([wall], depth=_depth(100.0, [], []), large_orders=los,
                    taker_series=[], cfg=DEFAULTS, now_sec=now)
    assert wall.change_kind == "eaten"
    assert wall.eaten_usd >= 900.0


def test_classify_change_cancelled_via_large_order():
    wall = _make_tagged_wall("ask")
    now = 1_700_000_000
    los = [LargeOrderLifecycle(
        id=42, side="ask", limit_price=100.1,
        start_time_ms=(now - 500) * 1000,
        start_quantity=10.0, current_quantity=0.5,
        executed_volume=0.5, executed_usd_value=50.0,
        start_usd_value=1000.0, current_usd_value=50.0,
        state="ended", end_time_ms=(now - 100) * 1000,
    )]
    classify_change([wall], depth=_depth(100.0, [], []), large_orders=los,
                    taker_series=[], cfg=DEFAULTS, now_sec=now)
    assert wall.change_kind == "cancelled"
    assert wall.cancelled_usd > wall.eaten_usd


def test_classify_change_holding_via_large_order():
    wall = _make_tagged_wall("bid")
    now = 1_700_000_000
    los = [LargeOrderLifecycle(
        id=42, side="bid", limit_price=100.1,
        start_time_ms=(now - 200) * 1000,
        start_quantity=10.0, current_quantity=10.0,
        executed_volume=0.0, executed_usd_value=0.0,
        start_usd_value=1000.0, current_usd_value=1000.0,
        state="holding",
    )]
    classify_change([wall], depth=_depth(100.0, [], []), large_orders=los,
                    taker_series=[], cfg=DEFAULTS, now_sec=now)
    assert wall.change_kind == "holding"


def test_classify_change_depth_delta_eaten():
    """无大单关联时走散堆减量路径：减少量 ≤ taker_buy → eaten。"""
    now = 1_700_000_000
    wall = PressureWall(
        side="ask", price_lo=100.0, price_hi=100.5, price_mid=100.25,
        distance_pct=0.25, size_usd=600_000, size_base=6,
    )
    prev_asks = [_bin(100.1, 60), _bin(100.4, 40)]   # $10,016 prev
    cur_asks = [_bin(100.1, 10), _bin(100.4, 5)]     # 减少了 ~$8500 base coin
    depth = _depth(100.0, [], cur_asks, prev_asks=prev_asks)
    # 同窗内主动买盘成交 ≥ 减少量
    taker = [{"ts": now - 10, "buy_usd": 20_000, "sell_usd": 0}]
    classify_change([wall], depth=depth, large_orders=[], taker_series=taker,
                    cfg=DEFAULTS, now_sec=now)
    assert wall.change_kind == "eaten"


def test_classify_change_depth_delta_cancelled():
    """减少量远 > taker → cancelled。"""
    now = 1_700_000_000
    wall = PressureWall(
        side="ask", price_lo=100.0, price_hi=100.5, price_mid=100.25,
        distance_pct=0.25, size_usd=600_000, size_base=6,
    )
    prev_asks = [_bin(100.1, 60), _bin(100.4, 40)]
    cur_asks = [_bin(100.1, 5), _bin(100.4, 5)]
    depth = _depth(100.0, [], cur_asks, prev_asks=prev_asks)
    taker = [{"ts": now - 10, "buy_usd": 200, "sell_usd": 0}]
    classify_change([wall], depth=depth, large_orders=[], taker_series=taker,
                    cfg=DEFAULTS, now_sec=now)
    assert wall.change_kind == "cancelled"
    assert wall.cancelled_usd > 0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# L3 — classify_pressure
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _state_with_candles(last: float, candles: list[CandleData],
                        *, cvd_trend: str | None = None):
    """构造最小 state 给 classify_pressure 使用（仅访问 candles_15m / cvd_contract）。"""
    cvd = SimpleNamespace(trend_1h=cvd_trend) if cvd_trend else None
    return SimpleNamespace(
        coin="BTC", ticker=SimpleNamespace(last=last),
        candles_15m=candles, cvd_contract=cvd,
        atr=None, large_orders_history=[],
        taker_contract_series=[],
        footprint_contract=[], footprint_spot=[],
        orderbook_depth_snapshot=None,
    )


def test_classify_pressure_real_resistance_with_cvd_rising():
    now = 1_700_000_000
    wall = PressureWall(
        side="ask", price_lo=100.0, price_hi=100.5, price_mid=100.25,
        distance_pct=0.25, size_usd=1_000_000, size_base=10,
    )
    wall.change_kind = "eaten"
    candles = [_candle(now - 600, close=100.0, high=100.3, low=99.8)]
    state = _state_with_candles(100.0, candles, cvd_trend="rising")
    classify_pressure([wall], state, DEFAULTS, now)
    assert wall.label == "real_R"
    assert wall.confidence >= 80


def test_classify_pressure_fake_resistance_break():
    now = 1_700_000_000
    wall = PressureWall(
        side="ask", price_lo=100.0, price_hi=100.5, price_mid=100.25,
        distance_pct=0.25, size_usd=1_000_000, size_base=10,
    )
    wall.change_kind = "cancelled"
    # 价格已突破到 102 → broken_up=True
    candles = [
        _candle(now - 400, close=100.6, high=100.8, low=100.0),
        _candle(now - 100, close=102.0, high=102.0, low=100.5),
    ]
    state = _state_with_candles(102.0, candles)
    classify_pressure([wall], state, DEFAULTS, now)
    assert wall.label == "fake_R_break"


def test_classify_pressure_real_support_with_cvd_declining():
    now = 1_700_000_000
    wall = PressureWall(
        side="bid", price_lo=99.0, price_hi=99.5, price_mid=99.25,
        distance_pct=-0.75, size_usd=1_000_000, size_base=10,
    )
    wall.change_kind = "eaten"
    candles = [_candle(now - 600, close=100.0, high=100.2, low=99.4)]
    state = _state_with_candles(100.0, candles, cvd_trend="declining")
    classify_pressure([wall], state, DEFAULTS, now)
    assert wall.label == "real_S"
    assert wall.confidence >= 80


def test_classify_pressure_untested_when_price_far():
    now = 1_700_000_000
    wall = PressureWall(
        side="ask", price_lo=110.0, price_hi=110.5, price_mid=110.25,
        distance_pct=10.25, size_usd=1_000_000, size_base=10,
    )
    wall.change_kind = "holding"
    candles = [_candle(now - 600, close=100.0, high=100.5, low=99.8)]
    state = _state_with_candles(100.0, candles)
    classify_pressure([wall], state, DEFAULTS, now)
    assert wall.label == "untested"


def test_classify_pressure_partial_ask_touched_unbroken_is_real_R():
    """partial + touched + 未破：卖墙部分被吃 + 价格被压住 → real_R 候选。

    回归 P0 bug：之前 _label_one 没有 partial 分支，全部 fallthrough 到 untested 25。
    """
    now = 1_700_000_000
    wall = PressureWall(
        side="ask", price_lo=100.0, price_hi=100.5, price_mid=100.25,
        distance_pct=0.25, size_usd=1_000_000, size_base=10,
    )
    wall.change_kind = "partial"
    candles = [_candle(now - 600, close=100.0, high=100.3, low=99.8)]
    state = _state_with_candles(100.0, candles)
    classify_pressure([wall], state, DEFAULTS, now)
    assert wall.label == "real_R"
    assert wall.confidence == 60


def test_classify_pressure_partial_bid_touched_unbroken_is_real_S():
    """partial + touched + 未破：买墙部分被吃 + 价格守住 → real_S 候选。"""
    now = 1_700_000_000
    wall = PressureWall(
        side="bid", price_lo=99.0, price_hi=99.5, price_mid=99.25,
        distance_pct=-0.75, size_usd=1_000_000, size_base=10,
    )
    wall.change_kind = "partial"
    candles = [_candle(now - 600, close=100.0, high=100.2, low=99.4)]
    state = _state_with_candles(100.0, candles)
    classify_pressure([wall], state, DEFAULTS, now)
    assert wall.label == "real_S"
    assert wall.confidence == 60


def test_classify_pressure_partial_broken_is_fake_break():
    """partial + 已破 → fake_R_break（部分被吃但顶不住，墙已失效）。"""
    now = 1_700_000_000
    wall = PressureWall(
        side="ask", price_lo=100.0, price_hi=100.5, price_mid=100.25,
        distance_pct=0.25, size_usd=1_000_000, size_base=10,
    )
    wall.change_kind = "partial"
    candles = [
        _candle(now - 400, close=100.6, high=100.8, low=100.0),
        _candle(now - 100, close=102.0, high=102.0, low=100.5),
    ]
    state = _state_with_candles(102.0, candles)
    classify_pressure([wall], state, DEFAULTS, now)
    assert wall.label == "fake_R_break"
    assert wall.confidence == 45


def test_classify_pressure_partial_untouched_is_untested_higher_score():
    """partial + 未触：减量来源不清 + 价格未到 → untested 但置信度 40（高于 holding+untouched 的 30）。"""
    now = 1_700_000_000
    wall = PressureWall(
        side="ask", price_lo=110.0, price_hi=110.5, price_mid=110.25,
        distance_pct=10.25, size_usd=1_000_000, size_base=10,
    )
    wall.change_kind = "partial"
    candles = [_candle(now - 600, close=100.0, high=100.5, low=99.8)]
    state = _state_with_candles(100.0, candles)
    classify_pressure([wall], state, DEFAULTS, now)
    assert wall.label == "untested"
    assert wall.confidence == 40  # 比 holding+untouched 的 30 高


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# L4 — augment_with_absorption
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_augment_with_absorption_boosts_confidence():
    wall = PressureWall(
        side="ask", price_lo=100.0, price_hi=100.5, price_mid=100.25,
        distance_pct=0.25, size_usd=1_000_000, size_base=10,
        label="real_R", confidence=70, reason="卖墙被吃",
    )
    zone = AbsorptionZone(
        price=100.20, side="resistance", taker_volume_usd=5_000_000,
        delta_pct_abs_avg=0.05, bar_count=2, age_hours=0.5,
    )
    snap = AbsorptionSnapshot(zones_resistance=[zone], total_zone_count=1,
                              strongest_resistance=zone, lookback_bars=3)
    augment_with_absorption([wall], snap, last_price=100.0, atr=2.0, cfg=DEFAULTS)
    assert wall.confluence_with_absorption is True
    assert wall.confidence == 95
    assert "absorption_zone" in wall.reason


def test_augment_with_absorption_no_match_when_far():
    wall = PressureWall(
        side="ask", price_lo=100.0, price_hi=100.5, price_mid=100.25,
        distance_pct=0.25, size_usd=1_000_000, size_base=10,
        label="real_R", confidence=70,
    )
    zone = AbsorptionZone(
        price=110.0, side="resistance", taker_volume_usd=5_000_000,
        delta_pct_abs_avg=0.05, bar_count=2, age_hours=0.5,
    )
    snap = AbsorptionSnapshot(zones_resistance=[zone], total_zone_count=1,
                              strongest_resistance=zone, lookback_bars=3)
    augment_with_absorption([wall], snap, last_price=100.0, atr=2.0, cfg=DEFAULTS)
    assert wall.confluence_with_absorption is False
    assert wall.confidence == 70


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 强度 tier 派生（_assign_strength_tier）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_assign_strength_tier_real_with_boost_promotes_to_s():
    from processors.orderbook_pressure import _assign_strength_tier

    # real_R + confidence 78 + 共振大单 → S（boost 把 75-84 区间提升到 S）
    wall = PressureWall(
        side="ask", price_lo=100, price_hi=100.5, price_mid=100.25,
        distance_pct=0.25, size_usd=1_000_000, size_base=10,
        label="real_R", confidence=78, large_order_count=2,
    )
    assert _assign_strength_tier(wall) == "S"


def test_assign_strength_tier_real_high_confidence_is_s():
    from processors.orderbook_pressure import _assign_strength_tier

    # real_S + confidence ≥ 85 即使无 boost 也是 S
    wall = PressureWall(
        side="bid", price_lo=99.5, price_hi=100, price_mid=99.75,
        distance_pct=-0.25, size_usd=1_000_000, size_base=10,
        label="real_S", confidence=88,
    )
    assert _assign_strength_tier(wall) == "S"


def test_assign_strength_tier_real_70_is_a():
    from processors.orderbook_pressure import _assign_strength_tier

    wall = PressureWall(
        side="ask", price_lo=100, price_hi=100.5, price_mid=100.25,
        distance_pct=0.25, size_usd=1_000_000, size_base=10,
        label="real_R", confidence=70,
    )
    assert _assign_strength_tier(wall) == "A"


def test_assign_strength_tier_fake_capped_to_b():
    from processors.orderbook_pressure import _assign_strength_tier

    # spoof 撤单墙即使 confidence 很高也封顶 B（避免误导）
    wall = PressureWall(
        side="ask", price_lo=100, price_hi=100.5, price_mid=100.25,
        distance_pct=0.25, size_usd=1_000_000, size_base=10,
        label="fake_R", confidence=90,
    )
    assert _assign_strength_tier(wall) == "B"


def test_assign_strength_tier_fake_break_is_c():
    from processors.orderbook_pressure import _assign_strength_tier

    # 已突破墙强制 C
    wall = PressureWall(
        side="ask", price_lo=100, price_hi=100.5, price_mid=100.25,
        distance_pct=0.25, size_usd=1_000_000, size_base=10,
        label="fake_R_break", confidence=80,
    )
    assert _assign_strength_tier(wall) == "C"


def test_assign_strength_tier_untested_is_b_or_c():
    from processors.orderbook_pressure import _assign_strength_tier

    high = PressureWall(
        side="ask", price_lo=100, price_hi=100.5, price_mid=100.25,
        distance_pct=0.25, size_usd=1_000_000, size_base=10,
        label="untested", confidence=60,
    )
    low = PressureWall(
        side="ask", price_lo=100, price_hi=100.5, price_mid=100.25,
        distance_pct=0.25, size_usd=1_000_000, size_base=10,
        label="untested", confidence=30,
    )
    assert _assign_strength_tier(high) == "B"
    assert _assign_strength_tier(low) == "C"


def test_compute_snapshot_writes_tier_for_each_wall():
    """端到端：确保 compute_pressure_snapshot 输出的每个 wall 都带 tier。"""
    now = 1_700_000_000
    last = 100.0
    asks = [_bin(100.4, 60_000)]
    bids = [_bin(99.6, 50_000)]
    depth = _depth(last, bids, asks)
    state = SimpleNamespace(
        coin="BTC", ticker=SimpleNamespace(last=last),
        orderbook_depth_snapshot=depth,
        large_orders_history=[],
        taker_contract_series=[],
        candles_15m=[],
        cvd_contract=None,
        footprint_contract=[], footprint_spot=[],
        atr=1.0,
    )
    snap = compute_pressure_snapshot(state, now_sec=now)
    assert snap is not None and snap.walls
    for w in snap.walls:
        assert w.strength_tier in ("S", "A", "B", "C")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 顶层 compute_pressure_snapshot
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_compute_pressure_snapshot_returns_none_on_missing_data():
    # 无 ticker
    s1 = SimpleNamespace(coin="BTC", ticker=None, orderbook_depth_snapshot=None)
    assert compute_pressure_snapshot(s1) is None

    # 有 ticker 无 depth
    s2 = SimpleNamespace(coin="BTC", ticker=SimpleNamespace(last=100.0),
                         orderbook_depth_snapshot=None)
    assert compute_pressure_snapshot(s2) is None


def test_compute_pressure_snapshot_full_assembly():
    now = 1_700_000_000
    last = 100.0
    asks = [_bin(100.2, 50), _bin(100.4, 60_000)]   # 100.4 是大墙 ($6.024M)
    bids = [_bin(99.6, 50_000), _bin(99.4, 200)]    # 99.6 是大墙 ($4.98M)
    depth = _depth(last, bids, asks,
                   prev_bids=[_bin(99.6, 60_000), _bin(99.4, 200)],
                   prev_asks=[_bin(100.2, 50), _bin(100.4, 70_000)])
    candles = [_candle(now - 500, close=100.0, high=100.3, low=99.7)]
    state = SimpleNamespace(
        coin="BTC", ticker=SimpleNamespace(last=last),
        orderbook_depth_snapshot=depth,
        large_orders_history=[],
        taker_contract_series=[
            {"ts": now - 30, "buy_usd": 5_000_000, "sell_usd": 4_000_000},
        ],
        candles_15m=candles,
        cvd_contract=SimpleNamespace(trend_1h="rising"),
        footprint_contract=[], footprint_spot=[],
        atr=1.0,
    )
    snap = compute_pressure_snapshot(state, now_sec=now)
    assert snap is not None
    assert snap.coin == "BTC"
    assert snap.ts_sec == now
    assert snap.last_price == last
    assert len(snap.walls) >= 2
    # 顶层汇总：买墙落在 99.6 区间，理论应作为 top_support 候选
    assert snap.top_support is None or 99.4 <= snap.top_support <= 99.7


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 信号生成 + 30min 同价去重
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_generate_signals_and_dedup_window():
    reset_dedup_for_test()
    now = 1_700_000_000
    real_R = PressureWall(
        side="ask", price_lo=100.0, price_hi=100.2, price_mid=100.1,
        distance_pct=0.1, size_usd=1_000_000, size_base=10,
        label="real_R", confidence=80, reason="ok",
    )
    real_S = PressureWall(
        side="bid", price_lo=99.5, price_hi=99.7, price_mid=99.6,
        distance_pct=-0.4, size_usd=1_000_000, size_base=10,
        label="real_S", confidence=80, reason="ok",
    )
    snap = compute_snap_for_signals(now, last=100.0, walls=[real_R, real_S], atr=1.0)

    sigs1 = generate_pressure_signals(snap, now_sec=now)
    sides = sorted(s.side for s in sigs1)
    assert sides == ["long", "short"]

    # 30 min 内重复 → 全部去重
    sigs2 = generate_pressure_signals(snap, now_sec=now + 60)
    assert sigs2 == []

    # 跨过 30 min 窗口 → 重新触发
    sigs3 = generate_pressure_signals(snap, now_sec=now + 1900)
    assert len(sigs3) == 2


def test_generate_signals_skip_low_confidence():
    reset_dedup_for_test()
    now = 1_700_000_000
    weak_R = PressureWall(
        side="ask", price_lo=100.0, price_hi=100.2, price_mid=100.1,
        distance_pct=0.1, size_usd=1_000_000, size_base=10,
        label="real_R", confidence=40,  # < 60 默认门槛
    )
    snap = compute_snap_for_signals(now, last=100.0, walls=[weak_R], atr=1.0)
    assert generate_pressure_signals(snap, now_sec=now) == []


def test_generate_signals_skip_when_far_from_price():
    reset_dedup_for_test()
    now = 1_700_000_000
    far_R = PressureWall(
        side="ask", price_lo=110.0, price_hi=110.2, price_mid=110.1,
        distance_pct=10.1, size_usd=1_000_000, size_base=10,
        label="real_R", confidence=80,
    )
    snap = compute_snap_for_signals(now, last=100.0, walls=[far_R], atr=1.0)
    assert generate_pressure_signals(snap, now_sec=now) == []


# 辅助：构造 OrderbookPressureSnapshot 给 signal 测试用
def compute_snap_for_signals(ts_sec, last, walls, atr):
    from models.orderbook_pressure import OrderbookPressureSnapshot
    return OrderbookPressureSnapshot(
        coin="BTC", ts_sec=ts_sec, last_price=last, atr=atr,
        walls=walls, sample_count_depth=2, sample_count_large_history=0,
        data_quality="ok",
    )
