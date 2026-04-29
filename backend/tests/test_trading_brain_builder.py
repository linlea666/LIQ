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


def test_merge_tolerance_clamps_extreme_atr_to_upper():
    """P2-3：极端 ATR 时 raw 容差被 0.8% × price 上限 clamp，避免过度合并。"""
    t = merge_tolerance(100_000.0, atr=10_000.0)  # 0.5 × 10000 = 5000，超 800 上限
    assert t == 100_000.0 * 0.008
    # 下限：ATR=0 但价格极小（理论上不会触发，仍要保证不塌零）
    t2 = merge_tolerance(100.0, atr=0.0)
    assert t2 == 100.0 * 0.003  # 0.3 仍在 [0.15%=0.15, 0.8%=0.8] 区间内


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


def test_fut_book_bins_and_magnets_overlay():
    """Phase C: fut_book 应含合约侧 bin（按距离分桶）+ 清算磁铁叠加，
    且磁铁同价区的 bin 应被标记为 is_attached_magnet。"""
    last = 100_000.0
    near_ask = _wall(
        wall_zone_id="ask_near", side="ask",
        price_mid=100_300.0, price_low=100_250.0, price_high=100_350.0,
        peak_price=100_300.0, distance_pct=0.30,
        current_usd=2_000_000.0,
        spot_current_usd=400_000.0, coinbase_spot_usd=100_000.0,
        sweep_attractiveness_score=0.55, persistence_score=0.6,
        break_through_risk=0.40,
    )
    mid_bid = _wall(
        wall_zone_id="bid_mid", side="bid",
        price_mid=98_500.0, price_low=98_400.0, price_high=98_600.0,
        peak_price=98_500.0, distance_pct=-1.50,
        current_usd=1_200_000.0,
        spot_current_usd=0.0, coinbase_spot_usd=0.0,
    )
    op = OrderbookPressureSnapshot(
        coin="BTC", ts_sec=int(time.time()), last_price=last,
        walls_above=[near_ask], walls_below=[mid_bid],
    )
    liq = LiquidationMap(
        coin="BTC", ts=1, cycle="1d",
        leverage_groups=[LiqLeverageGroup(leverage="50", short_bands=[], long_bands=[])],
        clusters_above=[
            LiqCluster(price_center=100_310.0, price_from=100_280, price_to=100_340,
                       total_usd=30_000_000.0, side="short", exchange_count=4,
                       dominant_leverage="50"),
        ],
        clusters_below=[
            LiqCluster(price_center=95_000.0, price_from=94_900, price_to=95_100,
                       total_usd=50_000_000.0, side="long", exchange_count=3),
        ],
    )
    snap = build_trading_brain_snapshot(
        coin="BTC", last_price=last, atr=400.0, kl=None, op=op, liq=liq,
    )
    fb = snap.fut_book
    assert fb is not None
    near = next((b for b in fb.bins_above if b.wall_zone_id == "ask_near"), None)
    assert near is not None
    assert near.futures_usd == max(2_000_000.0 - 500_000.0, 0.0)
    assert near.is_attached_magnet is True
    full = next((b for b in fb.bins_below if b.wall_zone_id == "bid_mid"), None)
    assert full is not None
    assert full.futures_usd == 1_200_000.0
    assert any(m.magnet_kind == "liq_cluster" and m.side == "above" for m in fb.magnets)
    assert any(m.magnet_kind == "liq_cluster" and m.side == "below" for m in fb.magnets)
    far_below = next((m for m in fb.magnets if m.price == 95_000.0), None)
    assert far_below is not None
    assert far_below.distance_pct == round((95_000.0 - last) / last * 100.0, 4)


def test_fut_book_only_magnets_without_orderbook():
    """无 OrderbookPressureSnapshot 但有清算簇时，仍应输出 fut_book（只含 magnets）。"""
    last = 100_000.0
    liq = LiquidationMap(
        coin="BTC", ts=1, cycle="1d",
        leverage_groups=[LiqLeverageGroup(leverage="50", short_bands=[], long_bands=[])],
        clusters_below=[
            LiqCluster(price_center=99_300.0, price_from=99_200, price_to=99_400,
                       total_usd=20_000_000.0, side="long", exchange_count=2),
        ],
    )
    snap = build_trading_brain_snapshot(
        coin="BTC", last_price=last, atr=400.0, kl=None, op=None, liq=liq,
    )
    fb = snap.fut_book
    assert fb is not None
    assert fb.bins_above == [] and fb.bins_below == []
    assert any(m.price == 99_300.0 for m in fb.magnets)


def test_fut_book_none_when_no_data():
    """无 op 也无 liq 时 fut_book 应为 None。"""
    snap = build_trading_brain_snapshot(
        coin="BTC", last_price=100_000.0, atr=400.0, kl=None, op=None, liq=None,
    )
    assert snap.fut_book is None


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


# ────────────────────────────────────────────────────────────────────────────
# P1-B 修复回归：zone_id 必须跨帧稳定（否则状态机 / selectedId / setup_id 全错配）
# ────────────────────────────────────────────────────────────────────────────


def _build_btc_snapshot(*, last=100_000.0, atr=400.0, w_mid=99_000.0,
                         w_lo=98_800.0, w_hi=99_200.0, w_usd=2_000_000.0,
                         lv_price=99_050.0):
    """复用：构造一个含 spot 买墙 + 支撑关键位 的 BTC 快照。"""
    w = _wall(
        price_mid=w_mid, price_low=w_lo, price_high=w_hi, side="bid",
        source="spot_only", has_spot_confluence=True, current_usd=w_usd,
    )
    lv = KeyLevelV2(
        price=lv_price, side="support", strength_tier="A",
        confluence_score=72.0, final_score=78.0,
    )
    kl = KeyLevelSnapshotV2(ts=1, levels=[lv])
    op = OrderbookPressureSnapshot(
        coin="BTC", ts_sec=int(time.time()), last_price=last, walls_below=[w],
    )
    return build_trading_brain_snapshot(
        coin="BTC", last_price=last, atr=atr, kl=kl, op=op, liq=None,
    )


def test_zone_id_stable_across_identical_rebuild():
    """两次完全相同的输入应产出一致的 zone_id 集合。"""
    s1 = _build_btc_snapshot()
    s2 = _build_btc_snapshot()
    ids1 = sorted(z.zone_id for z in s1.zones)
    ids2 = sorted(z.zone_id for z in s2.zones)
    assert ids1 == ids2 and ids1, "zone_id 集合在相同输入下必须一致"


def test_zone_id_stable_under_minor_wall_thickness_perturbation():
    """墙厚度 ±5% 微动 → 同一价格区的 zone_id 不变（修复前会变）。"""
    s1 = _build_btc_snapshot(w_usd=2_000_000.0)
    s2 = _build_btc_snapshot(w_usd=2_100_000.0)  # +5%
    s3 = _build_btc_snapshot(w_usd=1_900_000.0)  # -5%
    z1 = next(z for z in s1.zones if 98_800 <= z.price_mid <= 99_200)
    z2 = next(z for z in s2.zones if 98_800 <= z.price_mid <= 99_200)
    z3 = next(z for z in s3.zones if 98_800 <= z.price_mid <= 99_200)
    assert z1.zone_id == z2.zone_id == z3.zone_id


def test_zone_id_stable_under_minor_price_drift_within_bucket():
    """价格在桶宽内漂移（< 0.25 ATR）→ zone_id 不变。"""
    s1 = _build_btc_snapshot(w_mid=99_000.0, lv_price=99_050.0, atr=400.0)
    # 漂移 50 USD = 0.125 ATR，远小于桶宽 max(100, 150)=150
    s2 = _build_btc_snapshot(w_mid=99_050.0, lv_price=99_100.0, atr=400.0)
    z1 = next(z for z in s1.zones if 98_900 <= z.price_mid <= 99_200)
    z2 = next(z for z in s2.zones if 98_900 <= z.price_mid <= 99_200)
    assert z1.zone_id == z2.zone_id


def test_zone_id_changes_when_role_group_changes():
    """role_group 切换时（defense ↔ target）zone_id 必须变化，
    避免不同语义的价格区被合并到同一 id 上。"""
    last = 100_000.0
    # 同价格区，第一帧是 spot 买墙（defense），第二帧切成纯清算磁铁（target）
    w = _wall(
        price_mid=99_000.0, price_low=98_900.0, price_high=99_100.0,
        side="bid", source="spot_only", has_spot_confluence=True,
    )
    op_def = OrderbookPressureSnapshot(
        coin="BTC", ts_sec=int(time.time()), last_price=last, walls_below=[w],
    )
    s_def = build_trading_brain_snapshot(
        coin="BTC", last_price=last, atr=400.0, kl=None, op=op_def, liq=None,
    )
    z_def = next(
        z for z in s_def.zones if z.dominant_role in ("spot_defense", "contested")
    )

    cluster = LiqCluster(
        side="long", price_from=98_900.0, price_to=99_100.0, price_center=99_000.0,
        total_usd=80_000_000.0, exchange_count=3,
    )
    liq = LiquidationMap(
        coin="BTC", ts=int(time.time()), cycle="1d",
        leverage_groups=[LiqLeverageGroup(leverage="50")],
        clusters_above=[], clusters_below=[cluster],
    )
    s_tgt = build_trading_brain_snapshot(
        coin="BTC", last_price=last, atr=400.0, kl=None, op=None, liq=liq,
    )
    z_tgt = next(z for z in s_tgt.zones if z.dominant_role == "liquidation_magnet")
    assert z_def.zone_id != z_tgt.zone_id, "防御簇与磁铁簇即使同价区也必须有不同 zone_id"


# ────────────────────────────────────────────────────────────────────────────
# P1-A 修复回归：setup state 跨 build 持久化（修复"伪状态机"）
# ────────────────────────────────────────────────────────────────────────────


def _build_strong_long_setup_snapshot(prev_states=None):
    """构造一个能稳定生成 support_limit_probe 的快照（distance ~ -1%、双源墙 + S 级关键位）。"""
    last = 100_000.0
    w = _wall(
        price_mid=99_000.0, price_low=98_900.0, price_high=99_100.0,
        side="bid", source="spot+depth", has_spot_confluence=True,
        coinbase_spot_confluence=True,
        support_resistance_trust_score=0.85,
        sweep_attractiveness_score=0.30, break_through_risk=0.20,
        trust_score=0.85, spot_current_usd=1_500_000.0,
        coinbase_spot_usd=400_000.0, current_usd=2_500_000.0,
        distance_pct=-1.0,
    )
    w_tgt = _wall(
        wall_zone_id="w_target", price_mid=101_500.0, price_low=101_400.0,
        price_high=101_600.0, side="ask", source="spot+depth",
        has_spot_confluence=True, support_resistance_trust_score=0.78,
        trust_score=0.78, distance_pct=1.5,
    )
    lv = KeyLevelV2(
        price=99_050.0, side="support", strength_tier="S",
        confluence_score=82.0, final_score=88.0,
    )
    kl = KeyLevelSnapshotV2(ts=int(time.time()), levels=[lv])
    op = OrderbookPressureSnapshot(
        coin="BTC", ts_sec=int(time.time()), last_price=last,
        walls_above=[w_tgt], walls_below=[w],
    )
    return build_trading_brain_snapshot(
        coin="BTC", last_price=last, atr=400.0, kl=kl, op=op, liq=None,
        prev_setup_states=prev_states,
    )


def test_setup_state_persists_across_builds():
    """注入 prev_setup_states 后，状态机能从上一帧的 triggered 推进到 confirmation_pending
    （而不是被 opportunity_engine 重置回 forming/waiting）。"""
    snap1 = _build_strong_long_setup_snapshot()
    long_setups = [
        s for s in snap1.opportunities
        if s.setup_type == "support_limit_probe"
    ]
    assert long_setups, "首帧应能生成 support_limit_probe（用于回归 setup_id 链路）"
    sid = long_setups[0].setup_id

    # 模拟"上一帧"已推进到 triggered（在生产里这会由 advance_all 在前一帧达成）
    from models.trading_brain import SetupState
    prev_states = {sid: SetupState(name="triggered", since_ts=int(time.time()) - 60)}

    snap2 = _build_strong_long_setup_snapshot(prev_states=prev_states)
    same = next(s for s in snap2.opportunities if s.setup_id == sid)
    # 关键：第二帧不能再被重置回 forming/waiting；至少应保留 triggered 或推进
    assert same.state.name in (
        "triggered", "confirmation_pending", "confirmed", "invalidated",
        "cancelled", "missed", "cooldown",
    ), f"prev=triggered 注入后不应回退到 forming/waiting，got {same.state.name}"


def test_setup_id_stable_across_rebuild_enables_persistence():
    """setup_id 必须跨 build 稳定（依赖 P1-B zone_id 稳定 + setup_type 不变）。
    否则 prev_setup_states 字典 key 永远找不到。"""
    s1 = _build_strong_long_setup_snapshot()
    s2 = _build_strong_long_setup_snapshot()
    ids1 = {s.setup_id for s in s1.opportunities}
    ids2 = {s.setup_id for s in s2.opportunities}
    assert ids1 and ids1 == ids2, f"setup_id 集合必须跨 build 一致；s1={ids1} s2={ids2}"


# ────────────────────────────────────────────────────────────────────────────
# Mutation guard：铁律强制单测（防回归）
# ────────────────────────────────────────────────────────────────────────────


def test_does_not_mutate_keylevel_or_wall_score_fields():
    """Trading Brain 必须只读消费 KL/Wall，不能改它们的评分字段。
    回归 hash 对比：build 前后 final_score / strength_tier / cascade_risk /
    support_resistance_trust_score / sweep_attractiveness_score /
    break_through_risk / trust_score 完全不变。"""
    last = 100_000.0
    lv = KeyLevelV2(
        price=99_050.0, side="support", strength_tier="A",
        confluence_score=72.0, final_score=78.0, cascade_risk=0.18,
    )
    w = _wall(
        price_mid=99_000.0, price_low=98_900.0, price_high=99_100.0,
        side="bid", source="spot_only", has_spot_confluence=True,
        support_resistance_trust_score=0.85, sweep_attractiveness_score=0.30,
        break_through_risk=0.22, trust_score=0.78,
    )
    kl = KeyLevelSnapshotV2(ts=1, levels=[lv])
    op = OrderbookPressureSnapshot(
        coin="BTC", ts_sec=1, last_price=last, walls_below=[w],
    )

    before = {
        "kl": (lv.final_score, lv.strength_tier, lv.cascade_risk, lv.confluence_score),
        "w": (
            w.support_resistance_trust_score,
            w.sweep_attractiveness_score,
            w.break_through_risk,
            w.trust_score,
            w.strength_tier,
        ),
    }
    build_trading_brain_snapshot(
        coin="BTC", last_price=last, atr=400.0, kl=kl, op=op, liq=None,
    )
    after = {
        "kl": (lv.final_score, lv.strength_tier, lv.cascade_risk, lv.confluence_score),
        "w": (
            w.support_resistance_trust_score,
            w.sweep_attractiveness_score,
            w.break_through_risk,
            w.trust_score,
            w.strength_tier,
        ),
    }
    assert before == after, f"铁律违反：上游评分字段被修改\nbefore={before}\nafter={after}"


def test_snapshot_json_contains_no_trade_instructions():
    """Trading Brain 严禁输出"买入/卖出/开多/开空/入场信号/概率"等交易指令措辞。"""
    snap = _build_strong_long_setup_snapshot()
    js = snap.model_dump_json()
    BANNED = ["买入信号", "卖出信号", "开多信号", "开空信号", "入场信号",
              "立即买入", "立即卖出", "马上做多", "马上做空"]
    for kw in BANNED:
        assert kw not in js, f"发现违规交易指令措辞：{kw}"
    # 概率措辞：只允许"打穿风险评分"，禁止"打穿概率/胜率/probability"
    assert "打穿概率" not in js, "禁止用'打穿概率'，应称'打穿风险评分'"
    assert "胜率" not in js, "MVP 阶段禁止显示胜率（无后验校准）"


# ────────────────────────────────────────────────────────────────────────────
# P1-D 修复回归：support/resistance 拆 strength + fragility 两层
# ────────────────────────────────────────────────────────────────────────────


def test_trust_equals_strength_when_no_attack():
    """无 active_attack / removal_risk 时，trust 应等于 strength（向后兼容）。"""
    last = 100_000.0
    w = _wall(
        price_mid=99_000.0, price_low=98_900.0, price_high=99_100.0,
        side="bid", source="spot_only", has_spot_confluence=True,
        support_resistance_trust_score=0.85,
        active_attack_score=0.0, wall_removal_risk=0.0,
    )
    op = OrderbookPressureSnapshot(
        coin="BTC", ts_sec=1, last_price=last, walls_below=[w],
    )
    snap = build_trading_brain_snapshot(
        coin="BTC", last_price=last, atr=400.0, kl=None, op=op, liq=None,
    )
    z = next(z for z in snap.zones if 98_900 <= z.price_mid <= 99_100)
    assert z.support_strength == 0.85
    assert z.support_fragility == 0.0
    assert abs(z.support_trust - 0.85) < 0.001


def test_trust_calibrated_down_when_futures_wall_under_attack():
    """同区合约墙有高 active_attack → support_trust 被打折，但 strength 保持原值。"""
    last = 100_000.0
    # 现货支撑墙（提供 strength）
    w_spot = _wall(
        wall_zone_id="w_spot", price_mid=99_000.0, price_low=98_950.0,
        price_high=99_050.0, side="bid", source="spot_only",
        has_spot_confluence=True, support_resistance_trust_score=0.85,
        active_attack_score=0.0,
    )
    # 同区合约买墙（提供 fragility）
    w_fut = _wall(
        wall_zone_id="w_fut", price_mid=99_000.0, price_low=98_950.0,
        price_high=99_050.0, side="bid", source="depth_only",
        has_spot_confluence=False, support_resistance_trust_score=0.40,
        active_attack_score=0.80, wall_removal_risk=0.50,
    )
    op = OrderbookPressureSnapshot(
        coin="BTC", ts_sec=1, last_price=last, walls_below=[w_spot, w_fut],
    )
    snap = build_trading_brain_snapshot(
        coin="BTC", last_price=last, atr=400.0, kl=None, op=op, liq=None,
    )
    z = next(z for z in snap.zones if 98_950 <= z.price_mid <= 99_050)
    # strength 应保留现货墙的 0.85（合约墙 0.40 较低不取）
    assert z.support_strength >= 0.85 - 1e-3
    # fragility = 0.6 × 0.80 + 0.4 × 0.50 = 0.68
    assert abs(z.support_fragility - 0.68) < 0.01
    # trust = 0.85 × (1 - 0.5 × 0.68) = 0.85 × 0.66 = 0.561
    assert abs(z.support_trust - 0.561) < 0.01
    # evidence 应有脆性警示
    assert any("支撑脆性" in e for e in z.evidence)


def test_fragility_only_counts_pure_futures_wall_not_spot():
    """现货墙自身的 active_attack 不应触发 fragility（语义：现货被买 ≠ 被攻击）。"""
    last = 100_000.0
    # 仅一个现货墙，自带高 active_attack（罕见但不应被错误归类）
    w = _wall(
        price_mid=99_000.0, price_low=98_950.0, price_high=99_050.0,
        side="bid", source="spot_only", has_spot_confluence=True,
        support_resistance_trust_score=0.80,
        active_attack_score=0.90, wall_removal_risk=0.40,
    )
    op = OrderbookPressureSnapshot(
        coin="BTC", ts_sec=1, last_price=last, walls_below=[w],
    )
    snap = build_trading_brain_snapshot(
        coin="BTC", last_price=last, atr=400.0, kl=None, op=op, liq=None,
    )
    z = next(z for z in snap.zones if 98_950 <= z.price_mid <= 99_050)
    # fragility 应为 0（现货墙的攻击信号不计入支撑脆性）
    assert z.support_fragility == 0.0
    assert abs(z.support_trust - 0.80) < 0.001


# ────────────────────────────────────────────────────────────────────────────
# 档位 2A · 长/短窗口字段透传 (BrainSpotBookItem / BrainFutBin)
# ────────────────────────────────────────────────────────────────────────────


def test_long_window_fields_propagate_to_brain_spot_book_item():
    """WallZone.max_usd_8h / persistence_score_8h 应透传到 BrainSpotBookItem。"""
    last = 100_000.0
    w = _wall(
        wall_zone_id="w_8h_spot", side="bid",
        price_mid=99_700.0, price_low=99_650.0, price_high=99_750.0,
        peak_price=99_700.0, distance_pct=-0.30,
        current_usd=2_500_000.0,
        max_usd_1h=3_000_000.0,
        persistence_score=0.80,
        max_usd_8h=5_200_000.0,
        avg_usd_8h=3_500_000.0,
        seen_count_8h=84,
        persistence_score_8h=1.0,
    )
    op = OrderbookPressureSnapshot(
        coin="BTC", ts_sec=int(time.time()), last_price=last,
        walls_below=[w],
    )
    snap = build_trading_brain_snapshot(
        coin="BTC", last_price=last, atr=400.0, kl=None, op=op, liq=None,
    )
    sb = snap.spot_book
    assert sb is not None
    item = next(i for i in sb.bids if i.wall_zone_id == "w_8h_spot")
    assert abs(item.max_usd_1h - 3_000_000.0) < 1e-3
    assert abs(item.max_usd_8h - 5_200_000.0) < 1e-3
    assert abs(item.persistence_score - 0.80) < 1e-3
    assert abs(item.persistence_score_8h - 1.0) < 1e-3


def test_long_window_fields_propagate_to_brain_fut_bin():
    """WallZone.max_usd_8h / persistence_score_8h 应透传到 BrainFutBin。"""
    last = 100_000.0
    w = _wall(
        wall_zone_id="w_8h_fut", side="ask",
        price_mid=100_500.0, price_low=100_450.0, price_high=100_550.0,
        peak_price=100_500.0, distance_pct=0.50,
        current_usd=1_800_000.0,
        max_usd_1h=2_100_000.0,
        persistence_score=0.65,
        max_usd_8h=3_950_000.0,
        avg_usd_8h=2_400_000.0,
        seen_count_8h=88,
        persistence_score_8h=0.92,
    )
    op = OrderbookPressureSnapshot(
        coin="BTC", ts_sec=int(time.time()), last_price=last,
        walls_above=[w],
    )
    snap = build_trading_brain_snapshot(
        coin="BTC", last_price=last, atr=400.0, kl=None, op=op, liq=None,
    )
    fb = snap.fut_book
    assert fb is not None
    bin_item = next(b for b in fb.bins_above if b.wall_zone_id == "w_8h_fut")
    assert abs(bin_item.max_usd_1h - 2_100_000.0) < 1e-3
    assert abs(bin_item.max_usd_8h - 3_950_000.0) < 1e-3
    assert abs(bin_item.persistence_score - 0.65) < 1e-3
    assert abs(bin_item.persistence_score_8h - 0.92) < 1e-3


def test_spot_book_item_splits_binance_and_coinbase():
    """方案 C：BrainSpotBookItem 应独立透传 Binance / Coinbase 两路现货厚度
    + Coinbase 单档最大 USD（机构大单标记用）。"""
    last = 100_000.0
    w = _wall(
        wall_zone_id="w_split", side="bid",
        price_mid=99_700.0, price_low=99_650.0, price_high=99_750.0,
        distance_pct=-0.30,
        current_usd=4_500_000.0,
        spot_current_usd=1_800_000.0,           # Binance 5m
        coinbase_spot_usd=900_000.0,            # Coinbase 瞬时
        coinbase_max_single_order_usd=4_464_000.0,  # 单档机构大单
        coinbase_spot_confluence=True,
        dual_source=True,
    )
    op = OrderbookPressureSnapshot(
        coin="BTC", ts_sec=int(time.time()), last_price=last,
        walls_below=[w],
    )
    snap = build_trading_brain_snapshot(
        coin="BTC", last_price=last, atr=400.0, kl=None, op=op, liq=None,
    )
    sb = snap.spot_book
    assert sb is not None
    item = next(i for i in sb.bids if i.wall_zone_id == "w_split")
    # 拆分字段独立可读
    assert abs(item.binance_spot_usd - 1_800_000.0) < 1e-3
    assert abs(item.coinbase_spot_usd - 900_000.0) < 1e-3
    assert abs(item.coinbase_max_single_order_usd - 4_464_000.0) < 1e-3
    # 合并字段保留向后兼容（spot_usd = Binance + Coinbase）
    assert abs(item.spot_usd - 2_700_000.0) < 1e-3
    # 合约 = total - spot_usd 不受拆分影响
    assert abs(item.futures_usd - 1_800_000.0) < 1e-3


def test_spot_book_item_split_zero_when_no_coinbase_data():
    """无 Coinbase 数据时 coinbase_* 字段应为 0，binance_spot_usd 仍可独立读。"""
    last = 100_000.0
    w = _wall(
        wall_zone_id="w_no_cb", side="ask",
        price_mid=100_300.0, price_low=100_250.0, price_high=100_350.0,
        distance_pct=0.30,
        current_usd=2_000_000.0,
        spot_current_usd=1_500_000.0,
        # 不传 coinbase_* → WallZone 默认 0
    )
    op = OrderbookPressureSnapshot(
        coin="BTC", ts_sec=int(time.time()), last_price=last,
        walls_above=[w],
    )
    snap = build_trading_brain_snapshot(
        coin="BTC", last_price=last, atr=400.0, kl=None, op=op, liq=None,
    )
    sb = snap.spot_book
    assert sb is not None
    item = next(i for i in sb.asks if i.wall_zone_id == "w_no_cb")
    assert abs(item.binance_spot_usd - 1_500_000.0) < 1e-3
    assert item.coinbase_spot_usd == 0.0
    assert item.coinbase_max_single_order_usd == 0.0
    assert abs(item.spot_usd - 1_500_000.0) < 1e-3


def test_long_window_default_zero_when_wall_zone_lacks_fields():
    """旧 WallZone（无 8h 字段）应透传为 0，不破坏序列化。"""
    last = 100_000.0
    w = _wall(  # 默认 _wall 不传 max_usd_8h 等 → WallZone 字段值为 0
        wall_zone_id="w_legacy", side="bid",
        price_mid=99_500.0, price_low=99_450.0, price_high=99_550.0,
        distance_pct=-0.50,
    )
    op = OrderbookPressureSnapshot(
        coin="BTC", ts_sec=int(time.time()), last_price=last,
        walls_below=[w],
    )
    snap = build_trading_brain_snapshot(
        coin="BTC", last_price=last, atr=400.0, kl=None, op=op, liq=None,
    )
    sb = snap.spot_book
    assert sb is not None
    item = next(i for i in sb.bids if i.wall_zone_id == "w_legacy")
    assert item.max_usd_8h == 0.0
    assert item.persistence_score_8h == 0.0
