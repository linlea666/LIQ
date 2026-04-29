"""交易大脑聚合 builder 单元测试（只读聚合、不重新打分）。"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from models.key_level import KeyLevelSnapshotV2, KeyLevelV2
from models.liquidation import LiqCluster, LiquidationMap, LiqLeverageGroup
from models.orderbook_pressure import OrderbookPressureSnapshot, WallEvent, WallZone
from models.trading_brain import TradingBrainSnapshot
from processors.trading_brain_builder import build_trading_brain_snapshot, merge_tolerance


def _wall(**kw) -> WallZone:
    base = dict(
        wall_zone_id=kw.get("wall_zone_id", "w1"),
        side=kw.get("side", "bid"),
        price_low=kw.get("price_low", 99000.0),
        price_high=kw.get("price_high", 101000.0),
        price_mid=kw.get("price_mid", 100000.0),
        peak_price=kw.get("peak_price", 100000.0),
        distance_pct=kw.get("distance_pct", -0.5),
        current_usd=kw.get("current_usd", 2_000_000.0),
        max_usd_1h=kw.get("max_usd_1h", 2_500_000.0),
        avg_usd_1h=kw.get("avg_usd_1h", 2_000_000.0),
        bin_count=kw.get("bin_count", 3),
        seen_count=kw.get("seen_count", 5),
        visible_minutes=kw.get("visible_minutes", 30.0),
        persistence_score=kw.get("persistence_score", 0.7),
        source=kw.get("source", "depth_only"),
        exchange_count=kw.get("exchange_count", 2),
        support_resistance_trust_score=kw.get("support_resistance_trust_score", 0.6),
        sweep_attractiveness_score=kw.get("sweep_attractiveness_score", 0.35),
        break_through_risk=kw.get("break_through_risk", 0.25),
        trust_score=kw.get("trust_score", 0.7),
        raw_trust_score=kw.get("raw_trust_score", 0.7),
        has_spot_confluence=kw.get("has_spot_confluence", False),
        coinbase_spot_confluence=kw.get("coinbase_spot_confluence", False),
    )
    base.update(kw)
    return WallZone.model_validate(base)


def test_merge_tolerance_with_atr():
    t = merge_tolerance(100_000.0, atr=800.0)
    assert t == max(400.0, 300.0)


def test_merge_tolerance_no_atr():
    t = merge_tolerance(100_000.0, atr=0.0)
    assert abs(t - 300.0) < 1e-6


def test_build_merges_adjacent_wall_and_level():
    last = 100_000.0
    w = _wall(price_mid=100_200.0, price_low=100_150.0, price_high=100_250.0, side="ask")
    lv = KeyLevelV2(
        price=100_220.0,
        side="resistance",
        strength_tier="B",
        confluence_score=55.0,
        final_score=62.0,
        cascade_risk=0.15,
    )
    kl = KeyLevelSnapshotV2(ts=1, current_price=last, atr=500.0, levels=[lv])
    op = OrderbookPressureSnapshot(
        coin="BTC",
        ts_sec=int(time.time()),
        last_price=last,
        atr=500.0,
        walls_above=[w],
        walls_below=[],
    )
    snap = build_trading_brain_snapshot(
        coin="BTC",
        last_price=last,
        atr=500.0,
        kl=kl,
        op=op,
        liq=None,
    )
    merged = [z for z in snap.zones if z.roles.key_level and z.roles.futures_liquidity_wall]
    assert len(merged) >= 1
    assert any(100_150.0 <= z.price_mid <= 100_260.0 for z in merged)


def test_pure_liquidation_not_in_support_resistance_rankings():
    last = 100_000.0
    cluster = LiqCluster(
        price_center=95_000.0,
        price_from=94_800.0,
        price_to=95_200.0,
        total_usd=80_000_000.0,
        side="long",
        exchange_count=4,
    )
    liq = LiquidationMap(
        coin="BTC",
        ts=1,
        cycle="1d",
        leverage_groups=[LiqLeverageGroup(leverage="50", short_bands=[], long_bands=[])],
        clusters_below=[cluster],
        clusters_above=[],
    )
    snap = build_trading_brain_snapshot(
        coin="BTC",
        last_price=last,
        atr=800.0,
        kl=None,
        op=None,
        liq=liq,
    )
    liq_only = [
        z for z in snap.zones
        if z.roles.liquidation_magnet and not z.roles.key_level and not z.roles.spot_supply_wall
    ]
    assert liq_only
    for z in liq_only:
        assert z.zone_id not in snap.rankings.support_trust
        assert z.zone_id not in snap.rankings.resistance_trust


def test_missing_sources_notes_and_partial_snapshot():
    last = 50_000.0
    snap = build_trading_brain_snapshot(
        coin="BTC",
        last_price=last,
        atr=100.0,
        kl=None,
        op=None,
        liq=None,
    )
    assert isinstance(snap, TradingBrainSnapshot)
    assert len(snap.data_quality.notes) >= 3


def test_wall_events_recent_only():
    last = 100_000.0
    now = int(time.time())
    op = OrderbookPressureSnapshot(
        coin="BTC",
        ts_sec=now,
        last_price=last,
        wall_events=[
            WallEvent(ts_sec=now - 60, side="bid", price_mid=99_500.0, event_type="wall_appeared"),
            WallEvent(ts_sec=now - 4000, side="ask", price_mid=100_500.0, event_type="wall_weakened"),
        ],
    )
    snap = build_trading_brain_snapshot(
        coin="BTC",
        last_price=last,
        atr=400.0,
        kl=None,
        op=op,
        liq=None,
    )
    assert len(snap.events) == 1
    assert "新出现" in snap.events[0].message


def test_dominant_role_spot_defense():
    """现货墙 + Coinbase + 关键位高 trust → spot_defense（防守位）。"""
    last = 100_000.0
    w = _wall(
        price_mid=99_000.0, price_low=98_900.0, price_high=99_100.0,
        side="bid", source="spot+depth",
        has_spot_confluence=True, coinbase_spot_confluence=True,
        support_resistance_trust_score=0.85,
    )
    lv = KeyLevelV2(price=99_010.0, side="support", strength_tier="A",
                    confluence_score=72.0, final_score=78.0)
    kl = KeyLevelSnapshotV2(ts=1, levels=[lv])
    op = OrderbookPressureSnapshot(coin="BTC", ts_sec=1, last_price=last, walls_below=[w])
    snap = build_trading_brain_snapshot(coin="BTC", last_price=last, atr=400.0, kl=kl, op=op, liq=None)
    z0 = next((z for z in snap.zones if 98_800 <= z.price_mid <= 99_200), None)
    assert z0 is not None
    # 这个 zone 没有清算磁铁/合约目标 → spot_defense
    assert z0.dominant_role in ("spot_defense", "contested")
    assert z0.zone_id in snap.rankings.top_defenses or z0.zone_id in snap.rankings.top_contested


def test_spot_book_brackets_and_caps():
    """Phase B: spot_book 应按 near/mid/far 分桶并截断；现货厚度独立计算。"""
    last = 100_000.0
    near_ask = _wall(
        wall_zone_id="ask_near", side="ask",
        price_mid=100_300.0, price_low=100_250.0, price_high=100_350.0,
        peak_price=100_300.0, distance_pct=0.30,
        current_usd=2_000_000.0, dual_source=True,
        spot_current_usd=900_000.0, coinbase_spot_usd=200_000.0,
        coinbase_spot_confluence=True,
    )
    mid_ask = _wall(
        wall_zone_id="ask_mid", side="ask",
        price_mid=101_500.0, price_low=101_400.0, price_high=101_600.0,
        peak_price=101_500.0, distance_pct=1.50,
        current_usd=1_500_000.0,
        spot_current_usd=0.0,
    )
    far_ask = _wall(
        wall_zone_id="ask_far", side="ask",
        price_mid=104_000.0, price_low=103_950.0, price_high=104_050.0,
        peak_price=104_000.0, distance_pct=4.00,
        current_usd=900_000.0,
    )
    too_far_ask = _wall(
        wall_zone_id="ask_xfar", side="ask",
        price_mid=110_000.0, price_low=109_950.0, price_high=110_050.0,
        peak_price=110_000.0, distance_pct=10.00,
        current_usd=500_000.0,
    )
    near_bid = _wall(
        wall_zone_id="bid_near", side="bid",
        price_mid=99_700.0, price_low=99_650.0, price_high=99_750.0,
        peak_price=99_700.0, distance_pct=-0.30,
        current_usd=2_500_000.0,
    )
    op = OrderbookPressureSnapshot(
        coin="BTC", ts_sec=int(time.time()), last_price=last,
        walls_above=[near_ask, mid_ask, far_ask, too_far_ask],
        walls_below=[near_bid],
    )
    snap = build_trading_brain_snapshot(
        coin="BTC", last_price=last, atr=400.0, kl=None, op=op, liq=None,
    )
    sb = snap.spot_book
    assert sb is not None
    assert all(item.wall_zone_id != "ask_xfar" for item in sb.asks)
    near = [x for x in sb.asks if x.bracket == "near"]
    mid = [x for x in sb.asks if x.bracket == "mid"]
    far = [x for x in sb.asks if x.bracket == "far"]
    assert near and near[0].wall_zone_id == "ask_near"
    assert mid and mid[0].wall_zone_id == "ask_mid"
    assert far and far[0].wall_zone_id == "ask_far"
    near_item = near[0]
    assert near_item.spot_usd == 900_000.0 + 200_000.0
    assert near_item.futures_usd == max(2_000_000.0 - 1_100_000.0, 0.0)
    assert near_item.is_dual_source is True
    assert near_item.has_coinbase is True
    assert sb.bracket_caps == {"near": 8, "mid": 8, "far": 6}
    assert sb.bids and sb.bids[0].wall_zone_id == "bid_near"


def test_spot_book_none_when_no_orderbook():
    """无 OrderbookPressureSnapshot 时 spot_book 应为 None。"""
    snap = build_trading_brain_snapshot(
        coin="BTC", last_price=100_000.0, atr=400.0, kl=None, op=None, liq=None,
    )
    assert snap.spot_book is None


def test_dominant_role_pure_liquidation_magnet():
    """只有清算簇 → liquidation_magnet。"""
    last = 100_000.0
    cluster = LiqCluster(price_center=95_000.0, price_from=94_900, price_to=95_100,
                         total_usd=50_000_000.0, side="long", exchange_count=3)
    liq = LiquidationMap(
        coin="BTC", ts=1, cycle="1d",
        leverage_groups=[LiqLeverageGroup(leverage="50", short_bands=[], long_bands=[])],
        clusters_below=[cluster],
    )
    snap = build_trading_brain_snapshot(coin="BTC", last_price=last, atr=600.0,
                                         kl=None, op=None, liq=liq)
    z0 = next(z for z in snap.zones if 94_800 <= z.price_mid <= 95_200)
    assert z0.dominant_role == "liquidation_magnet"
    assert z0.zone_id in snap.rankings.top_targets


def test_dominant_role_contested_when_spot_and_target_overlap():
    """现货墙 + 合约墙 + 清算磁铁 同价区 → contested。"""
    last = 100_000.0
    w_spot = _wall(price_mid=99_000.0, price_low=98_900, price_high=99_100,
                   side="bid", source="spot+depth", has_spot_confluence=True,
                   support_resistance_trust_score=0.7)
    w_fut = _wall(price_mid=99_010.0, price_low=98_910, price_high=99_110,
                  side="bid", source="depth_only",
                  support_resistance_trust_score=0.4)
    cluster = LiqCluster(price_center=99_050.0, price_from=98_950, price_to=99_150,
                         total_usd=80_000_000.0, side="long", exchange_count=4)
    liq = LiquidationMap(
        coin="BTC", ts=1, cycle="1d",
        leverage_groups=[LiqLeverageGroup(leverage="50", short_bands=[], long_bands=[])],
        clusters_below=[cluster],
    )
    op = OrderbookPressureSnapshot(coin="BTC", ts_sec=1, last_price=last,
                                   walls_below=[w_spot, w_fut])
    snap = build_trading_brain_snapshot(coin="BTC", last_price=last, atr=400.0,
                                         kl=None, op=op, liq=liq)
    z0 = next(z for z in snap.zones if 98_800 <= z.price_mid <= 99_200)
    assert z0.dominant_role == "contested"
    assert z0.zone_id in snap.rankings.top_contested


def test_dominant_role_futures_target_no_spot():
    """合约墙 + 清算磁铁，无现货 → futures_target。"""
    last = 100_000.0
    w = _wall(price_mid=98_500.0, price_low=98_400, price_high=98_600,
              side="bid", source="depth_only", has_spot_confluence=False,
              support_resistance_trust_score=0.45)
    cluster = LiqCluster(price_center=98_500.0, price_from=98_400, price_to=98_600,
                         total_usd=60_000_000.0, side="long", exchange_count=3)
    liq = LiquidationMap(
        coin="BTC", ts=1, cycle="1d",
        leverage_groups=[LiqLeverageGroup(leverage="50", short_bands=[], long_bands=[])],
        clusters_below=[cluster],
    )
    op = OrderbookPressureSnapshot(coin="BTC", ts_sec=1, last_price=last, walls_below=[w])
    snap = build_trading_brain_snapshot(coin="BTC", last_price=last, atr=400.0,
                                         kl=None, op=op, liq=liq)
    z0 = next(z for z in snap.zones if 98_300 <= z.price_mid <= 98_700)
    assert z0.dominant_role == "futures_target"
    assert z0.zone_id in snap.rankings.top_targets


def test_dominant_role_key_level_only_low_trust():
    """关键位 trust 低 + 无墙 → key_level_only。"""
    last = 100_000.0
    lv = KeyLevelV2(price=98_000.0, side="support", strength_tier="C",
                    confluence_score=30.0, final_score=35.0)
    kl = KeyLevelSnapshotV2(ts=1, levels=[lv])
    snap = build_trading_brain_snapshot(coin="BTC", last_price=last, atr=400.0,
                                         kl=kl, op=None, liq=None)
    z0 = next(z for z in snap.zones if 97_900 <= z.price_mid <= 98_100)
    assert z0.dominant_role == "key_level_only"


def test_partial_ready_flags_in_data_quality():
    snap = build_trading_brain_snapshot(coin="BTC", last_price=50_000.0, atr=200.0,
                                         kl=None, op=None, liq=None)
    assert snap.data_quality.is_partial_ready is True
    assert snap.data_quality.ready_count == 0
    assert snap.data_quality.total_count == 3


def test_summary_nonempty_with_zones():
    last = 100_000.0
    w = _wall(
        price_mid=99_000.0,
        price_low=98_800.0,
        price_high=99_200.0,
        side="bid",
        source="spot_only",
        has_spot_confluence=True,
    )
    lv = KeyLevelV2(price=99_050.0, side="support", strength_tier="A", confluence_score=70.0, final_score=75.0)
    kl = KeyLevelSnapshotV2(ts=1, levels=[lv])
    op = OrderbookPressureSnapshot(coin="BTC", ts_sec=1, last_price=last, walls_below=[w])
    snap = build_trading_brain_snapshot(coin="BTC", last_price=last, atr=600.0, kl=kl, op=op, liq=None)
    assert "关键位" in snap.summary or "现货" in snap.summary or "支撑" in snap.summary
