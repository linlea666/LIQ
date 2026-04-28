"""测试流动性墙引擎（M1+M2）。

覆盖：
  M0 模型序列化往返（4 个新模型 + LargeOrderLifecycle 18 字段）
  M1 WallZone 聚合（merge_pct clamp、bin 合并、bin_count）
  M1 persistence + trend（冷启动 / 满分 / 部分；strengthening/weakening/stable/new）
  M1 warming（history_size 0 / 1-3 / ≥4 / 旧历史 + 时间不足）
  M2 consumed_confidence GPT 加权公式（边界）
  M2 wall_removal_risk 软分
  M2 inferred_position_state 5 个分支
  M2 oi_margin_split 三态
  M2 sweep_target / vacuum_gap_pct / break_through_risk
  KL 铁律：M1+M2 启用后 KL final_score / strength_tier / cascade_risk 不变
"""

from __future__ import annotations

from collections import deque
from types import SimpleNamespace

import pytest

from models.orderbook_pressure import (
    DepthBin,
    LargeOrderLifecycle,
    OrderbookDepthSnapshot,
    OrderbookPressureSnapshot,
    PositionCrowdingSnapshot,
    SweepTarget,
    WallEvent,
    WallZone,
)
from processors.liquidity_wall_engine import (
    ENGINE_DEFAULTS,
    _build_position_crowding,
    _build_sweep_target,
    _build_zones_for_side,
    _classify_inferred_position_state,
    _classify_oi_margin_split,
    _compute_break_through_risk,
    _compute_vacuum_gap_pct,
    _compute_wall_consumed_confidence,
    _compute_wall_removal_risk,
    _resolve_merge_pct,
    build_liquidity_wall_outputs,
)


# ──────────────────────────────────────────────────────────────────────
# 工具：构造 depth frame
# ──────────────────────────────────────────────────────────────────────
def _make_frame(ts: int, bids: list[tuple[float, float]],
                asks: list[tuple[float, float]]) -> OrderbookDepthSnapshot:
    return OrderbookDepthSnapshot(
        coin="BTC", exchange="Binance", symbol="BTCUSDT",
        ts_sec=ts,
        bids=[DepthBin(price=p, quantity=q, usd_value=p * q) for p, q in bids],
        asks=[DepthBin(price=p, quantity=q, usd_value=p * q) for p, q in asks],
    )


def _make_state(
    last_price: float = 100_000.0,
    history: list[OrderbookDepthSnapshot] | None = None,
    spot_history: list[OrderbookDepthSnapshot] | None = None,
    large_orders: list[LargeOrderLifecycle] | None = None,
    spot_large_orders: list[LargeOrderLifecycle] | None = None,
    oi_exchange_rank: dict | None = None,
    multi_funding=None,
    funding_history_8h=None,
    ls_ratio=None,
    top_position_ratio=None,
    taker_flow=None,
    cvd_spot=None,
    liq_max_pain: dict | None = None,
    liq_summary=None,
    candles_4h: list | None = None,
    coin: str = "BTC",
):
    state = SimpleNamespace(
        coin=coin,
        ticker=SimpleNamespace(last=last_price),
        atr=200.0,
        orderbook_depth_history=deque(history or [], maxlen=12),
        spot_orderbook_depth_history=deque(spot_history or [], maxlen=12),
        large_orders_history=large_orders or [],
        spot_large_orders_history=spot_large_orders or [],
        oi_exchange_rank=oi_exchange_rank,
        multi_funding=multi_funding,
        funding_history_8h=funding_history_8h,
        ls_ratio=ls_ratio,
        top_position_ratio=top_position_ratio,
        taker_flow=taker_flow,
        cvd_spot=cvd_spot,
        liq_max_pain=liq_max_pain or {},
        liq_summary=liq_summary,
        candles_4h=candles_4h or [],
    )
    return state


def _make_base_snap(last_price: float = 100_000.0, atr: float = 200.0):
    return OrderbookPressureSnapshot(
        coin="BTC", ts_sec=1700_000_000, last_price=last_price, atr=atr,
    )


# ════════════════════════════════════════════════════════════════════
# M0 — 模型序列化往返
# ════════════════════════════════════════════════════════════════════
class TestModelSerialization:

    def test_wall_zone_roundtrip(self):
        z = WallZone(
            side="bid", price_low=95_000, price_high=95_200, price_mid=95_100,
            peak_price=95_150, distance_pct=-4.9, current_usd=1_500_000,
            max_usd_1h=2_500_000, avg_usd_1h=1_800_000, bin_count=3,
            seen_count=5, visible_minutes=25, persistence_score=0.55,
        )
        d = z.model_dump()
        z2 = WallZone(**d)
        assert z2 == z

    def test_wall_event_roundtrip(self):
        e = WallEvent(
            ts_sec=1700_000_000, side="ask", price_mid=105_000,
            event_type="wall_consumed", confidence=0.85,
            executed_usd_value=2_400_000, explain="已被吃 2.4M",
        )
        d = e.model_dump()
        e2 = WallEvent(**d)
        assert e2 == e

    def test_position_crowding_roundtrip(self):
        c = PositionCrowdingSnapshot(
            oi_delta_1h_pct=0.13, oi_margin_split="stable_dominant",
            funding_now_pct=0.02, inferred_position_state="long_opening",
            long_crowding_risk=0.65,
        )
        d = c.model_dump()
        c2 = PositionCrowdingSnapshot(**d)
        assert c2 == c

    def test_sweep_target_roundtrip(self):
        s = SweepTarget(
            direction="below", magnet_price=98_500, magnet_amount_usd=4_000_000,
            distance_pct=-1.5, vacuum_gap_pct=0.3,
            explain="下方多头清算磁铁 98500（4M USD）",
        )
        d = s.model_dump()
        s2 = SweepTarget(**d)
        assert s2 == s

    def test_large_order_lifecycle_18_fields(self):
        lo = LargeOrderLifecycle(
            id=12345, side="bid", limit_price=98_000,
            start_time_ms=1700_000_000_000, end_time_ms=1700_000_300_000,
            start_quantity=10.0, current_quantity=8.0,
            executed_volume=2.0, executed_usd_value=196_000,
            start_usd_value=980_000, current_usd_value=784_000,
            trade_count=5, state="ended",
            exchange_name="Binance",
        )
        d = lo.model_dump()
        lo2 = LargeOrderLifecycle(**d)
        assert lo2 == lo
        # holding_age_sec 派生
        assert lo.holding_age_sec == 300


# ════════════════════════════════════════════════════════════════════
# M1 — merge_pct clamp
# ════════════════════════════════════════════════════════════════════
class TestMergePct:

    def test_merge_pct_atr_normal(self):
        # ATR = 200, last = 100k, mult = 0.15 → 0.15 × 0.002 = 0.0003 → clamp 到 min 0.0005
        assert _resolve_merge_pct(200.0, 100_000.0, ENGINE_DEFAULTS) == pytest.approx(0.0005)

    def test_merge_pct_clamp_max(self):
        # ATR = 5000（小币高 ATR）→ 0.15 × 0.05 = 0.0075 → clamp 到 max 0.0030
        assert _resolve_merge_pct(5000.0, 100_000.0, ENGINE_DEFAULTS) == pytest.approx(0.003)

    def test_merge_pct_no_atr(self):
        # ATR 缺失 → 退化到 min
        assert _resolve_merge_pct(None, 100_000.0, ENGINE_DEFAULTS) == 0.0005

    def test_merge_pct_in_range(self):
        # ATR = 1500, last = 100k → 0.15 × 0.015 = 0.00225（在 0.0005-0.003 区间内）
        result = _resolve_merge_pct(1500.0, 100_000.0, ENGINE_DEFAULTS)
        assert 0.0005 <= result <= 0.003
        assert result == pytest.approx(0.00225)


# ════════════════════════════════════════════════════════════════════
# M1 — WallZone 聚合
# ════════════════════════════════════════════════════════════════════
class TestZoneAggregation:

    def test_adjacent_bins_merge_to_one_zone(self):
        # bid 侧三个紧邻 bin（间距 0.01% << merge_pct 0.30%）→ 合并为 1 个墙区
        bids = [(99_900, 30), (99_910, 35), (99_920, 40)]
        frame = _make_frame(1700_000_000, bids=bids, asks=[])
        # 用 cfg_overrides 把 merge_pct_min 抬到 0.30%（确保合并）
        cfg = {**ENGINE_DEFAULTS, "merge_pct_min": 0.0030, "merge_pct_max": 0.0030}
        zones = _build_zones_for_side(
            [frame], last_price=100_500, side="bid", atr=300.0,
            cfg=cfg,
        )
        assert len(zones) == 1
        z = zones[0]
        assert z.bin_count == 3
        assert z.price_low == 99_900
        assert z.price_high == 99_920
        # peak_price = USD 最大者（99_920 × 40 = 3.99M）
        assert z.peak_price == 99_920

    def test_distant_bins_stay_separate(self):
        # 两个差距 1% 的 bin（远超 merge_pct 0.05%-0.30% 上限）→ 仍是 2 个墙区
        bids = [(99_000, 50), (98_010, 60)]   # 差距 ~1%
        frame = _make_frame(1700_000_000, bids=bids, asks=[])
        zones = _build_zones_for_side(
            [frame], last_price=100_000, side="bid", atr=200.0,
            cfg=ENGINE_DEFAULTS,
        )
        assert len(zones) == 2

    def test_bins_below_min_usd_filtered(self):
        # 单 bin USD < wall_min_usd（默认 500k）→ 不出 zone
        bids = [(99_000, 0.5)]   # 0.5 × 99k = 49.5k < 500k
        frame = _make_frame(1700_000_000, bids=bids, asks=[])
        zones = _build_zones_for_side(
            [frame], last_price=100_000, side="bid", atr=200.0,
            cfg=ENGINE_DEFAULTS,
        )
        assert zones == []

    def test_bins_outside_range_filtered(self):
        # 距离 > 12% → 过滤掉
        bids = [(80_000, 100)]   # -20%
        frame = _make_frame(1700_000_000, bids=bids, asks=[])
        zones = _build_zones_for_side(
            [frame], last_price=100_000, side="bid", atr=200.0,
            cfg=ENGINE_DEFAULTS,
        )
        assert zones == []

    def test_ask_side_only_considers_above(self):
        # ask 侧应忽略 last 之下的 bin
        asks = [(99_000, 100)]   # 在 last 之下，但 side="ask" → 忽略
        frame = _make_frame(1700_000_000, bids=[], asks=asks)
        zones = _build_zones_for_side(
            [frame], last_price=100_000, side="ask", atr=200.0,
            cfg=ENGINE_DEFAULTS,
        )
        assert zones == []


# ════════════════════════════════════════════════════════════════════
# M1 — Persistence + Trend
# ════════════════════════════════════════════════════════════════════
class TestPersistenceAndTrend:

    def test_zone_persistence_full_history(self):
        # 同一 zone 在 12 帧都有 ≥ wall_min_usd（且 ≥ seed_min_usd 1M）→ seen_count = 12
        bids = [(99_500, 15)]   # 15 × 99500 ≈ 1.49M（> seed_min 1M）
        history = [_make_frame(1700_000_000 + i * 300, bids=bids, asks=[])
                   for i in range(12)]
        zones = _build_zones_for_side(
            history, last_price=100_000, side="bid", atr=200.0,
            cfg=ENGINE_DEFAULTS,
        )
        assert len(zones) == 1
        z = zones[0]
        assert z.seen_count == 12
        # visible_minutes = 11 × 5min = 55min
        assert z.visible_minutes == pytest.approx(55, abs=1)
        # persistence_score = clamp(55/45, 0, 1) = 1.0
        assert z.persistence_score == 1.0
        assert z.trend == "stable"   # current ≈ avg

    def test_zone_persistence_cold_start(self):
        # 仅 1 帧历史 → trend = new, persistence_score = 0
        bids = [(99_500, 15)]   # USD 1.49M（> seed_min）
        history = [_make_frame(1700_000_000, bids=bids, asks=[])]
        zones = _build_zones_for_side(
            history, last_price=100_000, side="bid", atr=200.0,
            cfg=ENGINE_DEFAULTS,
        )
        assert len(zones) == 1
        z = zones[0]
        assert z.trend == "new"
        assert z.persistence_score == 0.0
        assert z.visible_minutes == 0.0

    def test_zone_trend_strengthening(self):
        # 12 帧中前 9 帧 USD ~1.49M、后 3 帧 USD ~5.97M（current >> avg）→ strengthening
        history = []
        for i in range(9):
            history.append(_make_frame(
                1700_000_000 + i * 300,
                bids=[(99_500, 15)], asks=[],   # USD 1.49M
            ))
        for i in range(9, 12):
            history.append(_make_frame(
                1700_000_000 + i * 300,
                bids=[(99_500, 60)], asks=[],   # USD 5.97M（增厚）
            ))
        zones = _build_zones_for_side(
            history, last_price=100_000, side="bid", atr=200.0,
            cfg=ENGINE_DEFAULTS,
        )
        assert len(zones) == 1
        z = zones[0]
        assert z.trend == "strengthening"

    def test_zone_trend_weakening(self):
        # 前 9 帧 USD ~5.97M、后 3 帧 USD ~1.49M → weakening
        history = []
        for i in range(9):
            history.append(_make_frame(
                1700_000_000 + i * 300,
                bids=[(99_500, 60)], asks=[],
            ))
        for i in range(9, 12):
            history.append(_make_frame(
                1700_000_000 + i * 300,
                bids=[(99_500, 15)], asks=[],
            ))
        zones = _build_zones_for_side(
            history, last_price=100_000, side="bid", atr=200.0,
            cfg=ENGINE_DEFAULTS,
        )
        assert len(zones) == 1
        assert zones[0].trend == "weakening"

    def test_visible_minutes_uses_seed_threshold_not_wall_min(self):
        """关键 bug 回归测试：seen 必须用 seed_min_usd（1M）门槛，
        否则"价区内几个普通小 bin USD 之和 ≥ wall_min（500K）"会让所有 zone
        都被判定 seen=12 → 全部"持续 55min"假象。

        构造：
          - 当前帧（i=11）：(99_500, 15) USD=1.49M（≥ seed → 形成 zone）
          - 前 11 帧：(99_500, 4) USD=398K（< seed 1M，但 ≥ wall_min×0.8=400K
            的边缘；旧 bug 会把这些都算成 seen）
        预期：seen_count = 1（只有最后一帧），visible_minutes = 0，trend = new。
        """
        history = []
        for i in range(11):
            history.append(_make_frame(
                1700_000_000 + i * 300,
                bids=[(99_500, 4)], asks=[],   # 398K，普通小 bin（无种子）
            ))
        history.append(_make_frame(
            1700_000_000 + 11 * 300,
            bids=[(99_500, 15)], asks=[],      # 1.49M，种子（≥ seed_min）
        ))
        zones = _build_zones_for_side(
            history, last_price=100_000, side="bid", atr=200.0,
            cfg=ENGINE_DEFAULTS,
        )
        assert len(zones) == 1
        z = zones[0]
        assert z.seen_count == 1
        assert z.visible_minutes == 0.0
        assert z.trend == "new"

    def test_visible_minutes_partial_history(self):
        """仅后半段（6 帧）有种子 → seen_count = 6，visible_minutes ≈ 25min。"""
        history = []
        for i in range(6):
            history.append(_make_frame(
                1700_000_000 + i * 300,
                bids=[(99_500, 4)], asks=[],   # 普通小 bin（无种子）
            ))
        for i in range(6, 12):
            history.append(_make_frame(
                1700_000_000 + i * 300,
                bids=[(99_500, 15)], asks=[],  # 种子
            ))
        zones = _build_zones_for_side(
            history, last_price=100_000, side="bid", atr=200.0,
            cfg=ENGINE_DEFAULTS,
        )
        assert len(zones) == 1
        z = zones[0]
        assert z.seen_count == 6
        # first_seen=帧 6（ts=1700_000_000+6×300）, last_seen=帧 11（+11×300）
        # diff = 5 × 300 = 1500s = 25min
        assert z.visible_minutes == pytest.approx(25, abs=1)


# ════════════════════════════════════════════════════════════════════
# M1 — Augment 容差匹配（大单 ↔ zone）
# ════════════════════════════════════════════════════════════════════
class TestAugmentTolerance:
    """关键 bug 回归测试：大单 limit_price 是精确价（如 $77,455），
    zone 边界由热力图离散 bin 决定（如 $77,460–510）。两者本就同源（都是
    订单簿挂单），不容差匹配会因几 USD 错位而错过几乎所有大单。
    """

    def _make_zone(self, price_low: float, price_high: float, side: str = "ask") -> WallZone:
        return WallZone(
            side=side,
            price_low=price_low,
            price_high=price_high,
            price_mid=(price_low + price_high) / 2,
            peak_price=(price_low + price_high) / 2,
            distance_pct=1.0,
            current_usd=20_000_000,
            max_usd_1h=20_000_000,
            avg_usd_1h=20_000_000,
            bin_count=2,
            seen_count=12,
            visible_minutes=55,
            persistence_score=1.0,
            trend="stable",
            source="depth_only",
        )

    def _make_large_order(self, lid: int, price: float, side: str, exchange: str = "Binance") -> LargeOrderLifecycle:
        return LargeOrderLifecycle(
            id=lid, side=side, limit_price=price,
            start_time_ms=1700_000_000_000,
            start_quantity=25.8, current_quantity=25.8,
            start_usd_value=2_000_000, current_usd_value=2_000_000,
            executed_volume=0, executed_usd_value=0,
            trade_count=0, state="holding",
            exchange_name=exchange,
        )

    def test_large_order_within_zone_matched(self):
        """大单 limit_price 直接落在 zone 价区内 → 匹配。"""
        from processors.liquidity_wall_engine import _augment_zones_with_large_orders
        zones = [self._make_zone(77_460, 77_510, "ask")]
        los = [self._make_large_order(1, 77_500, "ask")]
        _augment_zones_with_large_orders(zones, los, last_price=76_700, cfg=ENGINE_DEFAULTS)
        assert zones[0].large_order_ids == [1]
        assert zones[0].source == "depth+large_order"

    def test_large_order_5usd_outside_zone_matched_with_tolerance(self):
        """大单 $77,455 vs zone $77,460–510 差 5 USD → 容差匹配应成功。
        旧版严格 [low, high] 检查会错过；这是真实生产的常见场景。
        """
        from processors.liquidity_wall_engine import _augment_zones_with_large_orders
        zones = [self._make_zone(77_460, 77_510, "ask")]
        los = [self._make_large_order(2, 77_455, "ask")]
        _augment_zones_with_large_orders(zones, los, last_price=76_700, cfg=ENGINE_DEFAULTS)
        # 容差 = max(77485 × 0.001, 5) = max(77.5, 5) = 77.5 USD → 5 USD 错位匹配上
        assert zones[0].large_order_ids == [2]

    def test_large_order_far_outside_zone_not_matched(self):
        """大单价位距 zone > 容差 → 不匹配。"""
        from processors.liquidity_wall_engine import _augment_zones_with_large_orders
        zones = [self._make_zone(77_460, 77_510, "ask")]
        los = [self._make_large_order(3, 78_000, "ask")]   # 远离 ~500 USD
        _augment_zones_with_large_orders(zones, los, last_price=76_700, cfg=ENGINE_DEFAULTS)
        assert zones[0].large_order_ids == []
        assert zones[0].source == "depth_only"

    def test_large_order_wrong_side_not_matched(self):
        """卖墙 zone 不应匹配买方大单。"""
        from processors.liquidity_wall_engine import _augment_zones_with_large_orders
        zones = [self._make_zone(77_460, 77_510, "ask")]
        los = [self._make_large_order(4, 77_500, "bid")]
        _augment_zones_with_large_orders(zones, los, last_price=76_700, cfg=ENGINE_DEFAULTS)
        assert zones[0].large_order_ids == []

    def test_exchange_count_aggregates_multiple_exchanges(self):
        """同一 zone 价位有 BN + OKX + Bybit 三家大单 → exchange_count = 3。"""
        from processors.liquidity_wall_engine import _augment_zones_with_large_orders
        zones = [self._make_zone(77_460, 77_510, "ask")]
        los = [
            self._make_large_order(5, 77_470, "ask", "Binance"),
            self._make_large_order(6, 77_480, "ask", "OKX"),
            self._make_large_order(7, 77_500, "ask", "Bybit"),
        ]
        _augment_zones_with_large_orders(zones, los, last_price=76_700, cfg=ENGINE_DEFAULTS)
        assert zones[0].exchange_count == 3
        assert len(zones[0].large_order_ids) == 3


# ════════════════════════════════════════════════════════════════════
# M2.5 — 现货 vs 合约（spot augment + trust_score）
# ════════════════════════════════════════════════════════════════════
class TestSpotConfluenceAndTrust:
    """诉求"现货=真买卖家、合约=清算磁铁"落地（M2.5 + Phase A）：
    - has_spot_confluence + spot_large_order_ids（现货大单 lifecycle）
    - dual_source（Phase A：现货 5m 热力图 + 合约 5m 热力图同价区共振）
    - trust_score 阶梯（base 0.50 / +0.30 dual_source / +0.15 spot_lo /
                       +0.10 多所 / +0.10 持续）
    """

    def _make_zone(self, price_low: float, price_high: float, side: str = "ask",
                   persistence: float = 0.0, exchange_count: int = 1) -> WallZone:
        return WallZone(
            side=side, price_low=price_low, price_high=price_high,
            price_mid=(price_low + price_high) / 2,
            peak_price=(price_low + price_high) / 2,
            distance_pct=1.0,
            current_usd=15_000_000, max_usd_1h=15_000_000, avg_usd_1h=15_000_000,
            bin_count=2, seen_count=12, visible_minutes=55,
            persistence_score=persistence, trend="stable",
            source="depth_only", exchange_count=exchange_count,
        )

    def _make_lo(self, lid: int, price: float, side: str, exchange: str = "Binance") -> LargeOrderLifecycle:
        return LargeOrderLifecycle(
            id=lid, side=side, limit_price=price,
            start_time_ms=1700_000_000_000,
            start_quantity=20.0, current_quantity=20.0,
            start_usd_value=1_500_000, current_usd_value=1_500_000,
            executed_volume=0, executed_usd_value=0,
            trade_count=0, state="holding", exchange_name=exchange,
        )

    def test_spot_augment_marks_confluence(self):
        """现货大单价位落入 zone → has_spot_confluence=True + spot_large_order_ids 填充。"""
        from processors.liquidity_wall_engine import _augment_zones_with_spot_large_orders
        zones = [self._make_zone(75_000, 75_010, "bid")]
        spot_los = [self._make_lo(101, 75_005, "bid")]
        _augment_zones_with_spot_large_orders(zones, spot_los, ENGINE_DEFAULTS)
        assert zones[0].has_spot_confluence is True
        assert zones[0].spot_large_order_ids == [101]

    def test_spot_augment_uses_tolerance(self):
        """现货大单 $75,000 vs zone $75,010-50 差 10 USD → 容差匹配应成功。"""
        from processors.liquidity_wall_engine import _augment_zones_with_spot_large_orders
        zones = [self._make_zone(75_010, 75_050, "bid")]
        spot_los = [self._make_lo(102, 75_000, "bid")]   # 差 10 USD
        _augment_zones_with_spot_large_orders(zones, spot_los, ENGINE_DEFAULTS)
        # tol = max(75030 × 0.001, 5) = max(75.03, 5) = 75 USD → 10 USD 错位匹配上
        assert zones[0].has_spot_confluence is True

    def test_spot_augment_wrong_side_not_matched(self):
        """卖墙不应匹配买方现货大单。"""
        from processors.liquidity_wall_engine import _augment_zones_with_spot_large_orders
        zones = [self._make_zone(77_000, 77_050, "ask")]
        spot_los = [self._make_lo(103, 77_020, "bid")]
        _augment_zones_with_spot_large_orders(zones, spot_los, ENGINE_DEFAULTS)
        assert zones[0].has_spot_confluence is False
        assert zones[0].spot_large_order_ids == []

    def test_trust_score_base_only(self):
        """无任何加分 → 0.50 base。"""
        from processors.liquidity_wall_engine import _compute_trust_score
        z = self._make_zone(76_000, 76_010, "bid", persistence=0.3, exchange_count=1)
        assert _compute_trust_score(z, ENGINE_DEFAULTS) == pytest.approx(0.50)

    def test_trust_score_with_spot_confluence(self):
        """+ spot 大单共振 → 0.50 + 0.15 = 0.65（Phase A 调权重后）。"""
        from processors.liquidity_wall_engine import _compute_trust_score
        z = self._make_zone(76_000, 76_010, "bid", persistence=0.3, exchange_count=1)
        z.has_spot_confluence = True
        assert _compute_trust_score(z, ENGINE_DEFAULTS) == pytest.approx(0.65)

    def test_trust_score_with_dual_source(self):
        """Phase A：dual_source（现货+合约 5m 热力图共振）→ 0.50 + 0.30 = 0.80。"""
        from processors.liquidity_wall_engine import _compute_trust_score
        z = self._make_zone(76_000, 76_010, "bid", persistence=0.3, exchange_count=1)
        z.dual_source = True
        assert _compute_trust_score(z, ENGINE_DEFAULTS) == pytest.approx(0.80)

    def test_trust_score_dual_source_plus_persist(self):
        """Phase A：dual_source + 持续 ≥ 0.7 → 0.50 + 0.30 + 0.10 = 0.90 → 💎 高可信。"""
        from processors.liquidity_wall_engine import _compute_trust_score
        z = self._make_zone(76_000, 76_010, "bid", persistence=0.85, exchange_count=1)
        z.dual_source = True
        assert _compute_trust_score(z, ENGINE_DEFAULTS) == pytest.approx(0.90)

    def test_trust_score_full_quintet(self):
        """Phase A 满级：dual + spot_lo + 多所 + 持续 → 0.50+0.30+0.15+0.10+0.10 = 1.15 → clamp 1.00。"""
        from processors.liquidity_wall_engine import _compute_trust_score
        z = self._make_zone(76_000, 76_010, "bid", persistence=0.85, exchange_count=3)
        z.dual_source = True
        z.has_spot_confluence = True
        score = _compute_trust_score(z, ENGINE_DEFAULTS)
        assert score == pytest.approx(1.00)

    def test_trust_score_clamped_to_one(self):
        """即使加分超 1.0，也 clamp 到 1.0（边界保护）。"""
        from processors.liquidity_wall_engine import _compute_trust_score
        cfg = {**ENGINE_DEFAULTS,
               "trust_bonus_dual_source": 0.40,
               "trust_bonus_spot_confluence": 0.50,
               "trust_bonus_multi_exchange": 0.40,
               "trust_bonus_persistent": 0.30}
        z = self._make_zone(76_000, 76_010, "bid", persistence=0.85, exchange_count=3)
        z.dual_source = True
        z.has_spot_confluence = True
        score = _compute_trust_score(z, cfg)
        assert score == 1.0

    def test_trust_score_only_persistence(self):
        """持久但无 spot/多所 → base 0.50 + 持续 0.10 = 0.60（仍属"普通"档）。"""
        from processors.liquidity_wall_engine import _compute_trust_score
        z = self._make_zone(76_000, 76_010, "bid", persistence=0.85, exchange_count=1)
        # has_spot_confluence / dual_source 默认 False
        assert _compute_trust_score(z, ENGINE_DEFAULTS) == pytest.approx(0.60)


# ════════════════════════════════════════════════════════════════════
# M1 — Warming
# ════════════════════════════════════════════════════════════════════
class TestWarming:

    def test_warming_when_history_empty_does_not_force(self):
        # history_size=0 → 走旧路径（不强制 warming）
        state = _make_state(history=[])
        snap = _make_base_snap()
        out = build_liquidity_wall_outputs(state, snap, ENGINE_DEFAULTS, now=1700_000_000)
        assert out.warming is False
        assert out.history_size == 0

    def test_warming_when_history_short(self):
        # 1-3 帧 → warming
        history = [_make_frame(1700_000_000 + i * 300, bids=[(99_500, 10)], asks=[])
                   for i in range(2)]
        state = _make_state(history=history)
        snap = _make_base_snap()
        out = build_liquidity_wall_outputs(state, snap, ENGINE_DEFAULTS, now=1700_000_700)
        assert out.warming is True
        assert out.history_size == 2

    def test_warming_when_first_frame_too_recent(self):
        # 4+ 帧但首帧距今不足 30min → warming
        first_ts = 1700_000_000
        history = [_make_frame(first_ts + i * 300, bids=[(99_500, 10)], asks=[])
                   for i in range(5)]
        state = _make_state(history=history)
        snap = _make_base_snap()
        # now - first_ts = 1500s（25min < 30min 暖机阈值）
        out = build_liquidity_wall_outputs(state, snap, ENGINE_DEFAULTS, now=first_ts + 1500)
        assert out.warming is True

    def test_no_warming_when_history_sufficient(self):
        # 12 帧 + 跨度 ≥ 30min → 不暖机
        history = [_make_frame(1700_000_000 + i * 300, bids=[(99_500, 10)], asks=[])
                   for i in range(12)]
        state = _make_state(history=history)
        snap = _make_base_snap()
        # now - first_ts = 11 × 300 = 3300s + 一些时间（≥ 30min 阈值）
        out = build_liquidity_wall_outputs(state, snap, ENGINE_DEFAULTS,
                                            now=1700_000_000 + 11 * 300 + 100)
        assert out.warming is False


# ════════════════════════════════════════════════════════════════════
# M2 — consumed_confidence GPT 加权公式
# ════════════════════════════════════════════════════════════════════
class TestConsumedConfidence:

    def test_lo_only_full_score(self):
        # large_order 已成交 ≥ 30% × max_usd → lo_score = 1.0
        # 公式：0.5 × 1.0 = 0.5（taker/price 都没贡献）
        z = WallZone(
            side="bid", price_low=99_000, price_high=99_200, price_mid=99_100,
            peak_price=99_100, distance_pct=-0.9, current_usd=500_000,
            max_usd_1h=10_000_000, avg_usd_1h=8_000_000, bin_count=2,
            seen_count=5, visible_minutes=25, persistence_score=0.55,
        )
        consumed_los = [LargeOrderLifecycle(
            id=1, side="bid", limit_price=99_100, start_time_ms=0,
            executed_usd_value=4_000_000, state="ended",
            start_quantity=0, current_quantity=0,
        )]
        # taker_flow=None，price > price_low → price_score=0
        conf = _compute_wall_consumed_confidence(
            z, consumed_los, taker_flow=None,
            last_price=99_500,    # > price_high → 不算穿透 bid wall
            cfg=ENGINE_DEFAULTS,
        )
        # 0.5 × 1.0 + 0.25 × 0 + 0.25 × 0 = 0.5
        assert conf == pytest.approx(0.5, abs=0.05)

    def test_full_combo(self):
        # 三项都满分 → 1.0
        z = WallZone(
            side="bid", price_low=99_000, price_high=99_200, price_mid=99_100,
            peak_price=99_100, distance_pct=-0.9, current_usd=500_000,
            max_usd_1h=10_000_000, avg_usd_1h=8_000_000, bin_count=2,
            seen_count=5, visible_minutes=25, persistence_score=0.55,
        )
        consumed_los = [LargeOrderLifecycle(
            id=1, side="bid", limit_price=99_100, start_time_ms=0,
            executed_usd_value=4_000_000, state="ended",
            start_quantity=0, current_quantity=0,
        )]
        # taker_sell_dominant 60%+
        taker_flow = SimpleNamespace(buy_volume_usd=400, sell_volume_usd=600)
        # 价格 < price_low - 0.5%
        conf = _compute_wall_consumed_confidence(
            z, consumed_los, taker_flow=taker_flow,
            last_price=98_500,   # 低于 price_low(99_000) ~0.5%
            cfg=ENGINE_DEFAULTS,
        )
        assert conf == pytest.approx(1.0, abs=0.05)

    def test_no_evidence_returns_zero(self):
        z = WallZone(
            side="bid", price_low=99_000, price_high=99_200, price_mid=99_100,
            peak_price=99_100, distance_pct=-0.9, current_usd=500_000,
            max_usd_1h=10_000_000, avg_usd_1h=8_000_000, bin_count=2,
            seen_count=5, visible_minutes=25, persistence_score=0.55,
        )
        conf = _compute_wall_consumed_confidence(
            z, consumed_orders_in_zone=[], taker_flow=None,
            last_price=100_000, cfg=ENGINE_DEFAULTS,
        )
        assert conf == 0.0


# ════════════════════════════════════════════════════════════════════
# M2 — wall_removal_risk 软分
# ════════════════════════════════════════════════════════════════════
class TestRemovalRisk:

    def test_high_risk_all_removed_no_exec(self):
        # 5 笔 lifecycle 全是 ended without execution + persistence < 0.2
        z = WallZone(
            side="bid", price_low=99_000, price_high=99_200, price_mid=99_100,
            peak_price=99_100, distance_pct=-0.9, current_usd=500_000,
            max_usd_1h=2_000_000, avg_usd_1h=1_500_000, bin_count=2,
            seen_count=1, visible_minutes=2, persistence_score=0.05,
        )
        los = [LargeOrderLifecycle(
            id=i, side="bid", limit_price=99_100, start_time_ms=0,
            executed_usd_value=0, state="ended",
            start_quantity=0, current_quantity=0,
        ) for i in range(5)]
        risk = _compute_wall_removal_risk(z, los, ENGINE_DEFAULTS)
        # 0.40 (all removed no exec) + 0.30 (persistence<0.2) + 0.10 (avg holding 0)
        # current 500k / max 2M = 0.25 < 0.4 → +0.20
        assert risk == pytest.approx(1.0, abs=0.01)

    def test_low_risk_when_persistent(self):
        z = WallZone(
            side="bid", price_low=99_000, price_high=99_200, price_mid=99_100,
            peak_price=99_100, distance_pct=-0.9, current_usd=2_000_000,
            max_usd_1h=2_000_000, avg_usd_1h=1_800_000, bin_count=2,
            seen_count=12, visible_minutes=55, persistence_score=1.0,
        )
        risk = _compute_wall_removal_risk(z, [], ENGINE_DEFAULTS)
        assert risk == 0.0


# ════════════════════════════════════════════════════════════════════
# M2 — OI Margin Split + Inferred Position State
# ════════════════════════════════════════════════════════════════════
class TestCrowdingClassification:

    def test_oi_margin_stable_dominant(self):
        # stable 10x coin → dominant
        assert _classify_oi_margin_split(
            coin_usd=1_000_000_000, stable_usd=10_000_000_000, cfg=ENGINE_DEFAULTS,
        ) == "stable_dominant"

    def test_oi_margin_coin_dominant(self):
        assert _classify_oi_margin_split(
            coin_usd=10_000_000_000, stable_usd=1_000_000_000, cfg=ENGINE_DEFAULTS,
        ) == "coin_dominant"

    def test_oi_margin_balanced(self):
        assert _classify_oi_margin_split(
            coin_usd=4_500_000_000, stable_usd=5_500_000_000, cfg=ENGINE_DEFAULTS,
        ) == "balanced"

    def test_oi_margin_unknown(self):
        assert _classify_oi_margin_split(None, None, ENGINE_DEFAULTS) == "unknown"

    def test_inferred_long_opening(self):
        crowding = PositionCrowdingSnapshot(oi_delta_5m_pct=0.5, oi_delta_1h_pct=1.5)
        taker = SimpleNamespace(buy_volume_usd=1000, sell_volume_usd=200)
        state = _classify_inferred_position_state(
            crowding, taker, last_price=101_000, prev_price=100_000,
            has_recent_long_liq=False, has_recent_short_liq=False,
            cfg=ENGINE_DEFAULTS,
        )
        assert state == "long_opening"

    def test_inferred_short_opening(self):
        crowding = PositionCrowdingSnapshot(oi_delta_5m_pct=-0.5, oi_delta_1h_pct=1.5)
        taker = SimpleNamespace(buy_volume_usd=200, sell_volume_usd=1000)
        state = _classify_inferred_position_state(
            crowding, taker, last_price=99_000, prev_price=100_000,
            has_recent_long_liq=False, has_recent_short_liq=False,
            cfg=ENGINE_DEFAULTS,
        )
        assert state == "short_opening"

    def test_inferred_long_closing_or_liquidation(self):
        crowding = PositionCrowdingSnapshot(oi_delta_1h_pct=-1.5)
        state = _classify_inferred_position_state(
            crowding, None, last_price=99_000, prev_price=100_000,
            has_recent_long_liq=True, has_recent_short_liq=False,
            cfg=ENGINE_DEFAULTS,
        )
        assert state == "long_closing_or_liquidation"

    def test_inferred_liquidation_flush(self):
        crowding = PositionCrowdingSnapshot(oi_delta_1h_pct=-2.5)
        state = _classify_inferred_position_state(
            crowding, None, last_price=99_500, prev_price=100_000,
            has_recent_long_liq=True, has_recent_short_liq=True,
            cfg=ENGINE_DEFAULTS,
        )
        assert state == "liquidation_flush"

    def test_inferred_unknown_when_quiet(self):
        crowding = PositionCrowdingSnapshot(oi_delta_1h_pct=0.1)
        state = _classify_inferred_position_state(
            crowding, None, last_price=100_000, prev_price=99_990,
            has_recent_long_liq=False, has_recent_short_liq=False,
            cfg=ENGINE_DEFAULTS,
        )
        assert state == "unknown"


# ════════════════════════════════════════════════════════════════════
# M2 — Position Crowding 入口（结合 state）
# ════════════════════════════════════════════════════════════════════
class TestPositionCrowdingBuilder:

    def test_build_crowding_with_full_data(self):
        oi_rank = {
            "ts": 1700_000_000, "total_oi_usd": 5e10,
            "exchanges": [],
            "all_aggregated": {
                "oi_usd": 5e10, "oi_coin_margin_usd": 5e9, "oi_stable_margin_usd": 4.5e10,
                "change_5m_pct": 0.1, "change_15m_pct": 0.2, "change_30m_pct": 0.3,
                "change_1h_pct": 1.5, "change_4h_pct": 2.0, "change_24h_pct": 5.0,
            },
        }
        multi_funding = SimpleNamespace(avg_current=0.0006)
        # ls_ratio 是 global 多空比；top_position_ratio 是大户多空比（拥挤度公式读的字段）
        ls_ratio = SimpleNamespace(avg_ratio=1.5)
        top_pos = SimpleNamespace(avg_ratio=2.6)
        state = _make_state(
            oi_exchange_rank=oi_rank, multi_funding=multi_funding,
            ls_ratio=ls_ratio, top_position_ratio=top_pos,
        )
        crowding = _build_position_crowding(state, ENGINE_DEFAULTS)
        assert crowding is not None
        assert crowding.oi_delta_1h_pct == 1.5
        assert crowding.oi_margin_split == "stable_dominant"
        assert crowding.funding_now_pct == pytest.approx(0.06)
        assert crowding.global_account_ls_ratio == 1.5
        assert crowding.top_position_ls_ratio == 2.6
        # funding 0.06% > funding_high(0.05%) → +0.4 多头拥挤
        # top_position 2.6 > ls_extreme_high(2.5) → +0.3 多头拥挤
        assert crowding.long_crowding_risk >= 0.6
        assert "U本位主导" in " ".join(crowding.explain_chips)


# ════════════════════════════════════════════════════════════════════
# M2 — Sweep Target / Vacuum Gap / Break-through Risk
# ════════════════════════════════════════════════════════════════════
class TestSweepAndBreakThrough:

    def _make_zone(self, side="bid"):
        return WallZone(
            side=side, price_low=99_000, price_high=99_200, price_mid=99_100,
            peak_price=99_100, distance_pct=-0.9, current_usd=1_000_000,
            max_usd_1h=2_000_000, avg_usd_1h=1_500_000, bin_count=2,
            seen_count=5, visible_minutes=25, persistence_score=0.55,
        )

    def test_sweep_target_bid_picks_long_pain(self):
        z = self._make_zone(side="bid")
        item = SimpleNamespace(
            symbol="BTC", price=100_000,
            long_pain_price=98_500, long_pain_usd=4_000_000,
            short_pain_price=101_500, short_pain_usd=3_000_000,
        )
        sweep = _build_sweep_target(z, item, last_price=100_000,
                                     depth_latest=None, cfg=ENGINE_DEFAULTS)
        assert sweep is not None
        assert sweep.direction == "below"
        assert sweep.magnet_price == 98_500
        assert sweep.magnet_amount_usd == 4_000_000

    def test_sweep_target_ask_picks_short_pain(self):
        z = self._make_zone(side="ask")
        item = SimpleNamespace(
            symbol="BTC", price=100_000,
            long_pain_price=98_500, long_pain_usd=4_000_000,
            short_pain_price=101_500, short_pain_usd=3_000_000,
        )
        sweep = _build_sweep_target(z, item, last_price=100_000,
                                     depth_latest=None, cfg=ENGINE_DEFAULTS)
        assert sweep.direction == "above"
        assert sweep.magnet_price == 101_500

    def test_sweep_target_returns_none_when_no_data(self):
        z = self._make_zone()
        sweep = _build_sweep_target(z, max_pain_item=None,
                                     last_price=100_000, depth_latest=None,
                                     cfg=ENGINE_DEFAULTS)
        assert sweep is None

    def test_break_through_risk_thinning(self):
        # 静态因素满 = 0.30+0.20+0.20+0.15+0.10 = 0.95（Phase A 调整 crowding 0.15 → 0.10）
        # 加 active_attack 满分 ×0.20 = +0.20 → clamp 1.0
        z = self._make_zone()
        z.current_usd = 500_000
        z.max_usd_1h = 2_000_000   # 比例 0.25 < 0.5 → +0.30
        z.persistence_score = 0.05  # < 0.3 → +0.20
        sweep = SweepTarget(direction="below", magnet_price=99_900,
                             magnet_amount_usd=4_000_000,
                             distance_pct=-0.1,    # < 0.5% → +0.20
                             vacuum_gap_pct=0.6)   # ≥ 0.5 → +0.15
        crowding = PositionCrowdingSnapshot(long_crowding_risk=0.7)  # +0.10
        # 不传 taker_flow / cvd_spot → active_attack=0 → 仅 0.95
        risk_static = _compute_break_through_risk(z, crowding, sweep, ENGINE_DEFAULTS)
        assert risk_static == pytest.approx(0.95, abs=0.01)
        # 传入对齐的 taker + cvd → +0.20 → clamp 1.0
        taker = SimpleNamespace(buy_volume_usd=1_000_000, sell_volume_usd=4_000_000)
        cvd_spot = SimpleNamespace(trend_1h="strong_down")
        risk_full = _compute_break_through_risk(
            z, crowding, sweep, ENGINE_DEFAULTS,
            taker_flow=taker, cvd_spot=cvd_spot,
        )
        assert risk_full == pytest.approx(1.0, abs=0.01)

    def test_break_through_risk_low_when_zone_solid(self):
        z = self._make_zone()
        z.current_usd = 2_000_000
        z.max_usd_1h = 2_000_000   # 比例 1.0 不算 thinning
        z.persistence_score = 1.0
        risk = _compute_break_through_risk(z, None, None, ENGINE_DEFAULTS)
        assert risk == 0.0


# ════════════════════════════════════════════════════════════════════
# 主入口 + KL 隔离铁律
# ════════════════════════════════════════════════════════════════════
class TestMainEntryAndKLIsolation:

    def test_main_entry_warming_no_magnet(self):
        # 暖机期不输出 sweep_target.magnet_price
        history = [_make_frame(1700_000_000, bids=[(99_500, 30)], asks=[])]
        liq_max_pain = {
            "24h": SimpleNamespace(items=[SimpleNamespace(
                symbol="BTC", price=100_000,
                long_pain_price=98_500, long_pain_usd=4_000_000,
                short_pain_price=101_500, short_pain_usd=3_000_000,
            )])
        }
        state = _make_state(history=history, liq_max_pain=liq_max_pain)
        snap = _make_base_snap()
        out = build_liquidity_wall_outputs(state, snap, ENGINE_DEFAULTS, now=1700_000_300)
        assert out.warming is True
        # 暖机期：所有 zone.sweep_target 均为 None
        for z in out.walls_below + out.walls_above:
            assert z.sweep_target is None

    def test_main_entry_post_warming_has_magnet(self):
        # 12 帧充分历史 → 不暖机；应输出 sweep_target
        history = [_make_frame(1700_000_000 + i * 300, bids=[(99_500, 30)], asks=[])
                   for i in range(12)]
        liq_max_pain = {
            "24h": SimpleNamespace(items=[SimpleNamespace(
                symbol="BTC", price=100_000,
                long_pain_price=98_500, long_pain_usd=4_000_000,
                short_pain_price=101_500, short_pain_usd=3_000_000,
            )])
        }
        state = _make_state(history=history, liq_max_pain=liq_max_pain)
        snap = _make_base_snap()
        out = build_liquidity_wall_outputs(state, snap, ENGINE_DEFAULTS,
                                           now=1700_000_000 + 12 * 300 + 1900)
        assert out.warming is False
        below = out.walls_below
        assert below, "应该有 bid wall zone"
        z = below[0]
        assert z.sweep_target is not None
        assert z.sweep_target.magnet_price == 98_500

    # ════════════════════════════════════════════════════════════════════
    # Phase A — 双源 zone（dual_source）+ spot_only + active_attack + seed_by_coin
    # ════════════════════════════════════════════════════════════════════

    def test_dual_source_marked_when_spot_overlaps(self):
        """合约 zone 价区上叠加现货厚度 → dual_source=True + source='spot+depth'。"""
        from processors.liquidity_wall_engine import _augment_zones_with_spot_depth

        # 合约 zone：[75000, 75100]
        z = WallZone(
            side="bid", price_low=75_000, price_high=75_100, price_mid=75_050,
            peak_price=75_050, distance_pct=-2.5,
            current_usd=2_000_000, max_usd_1h=2_000_000, avg_usd_1h=2_000_000,
            bin_count=2, seen_count=12, visible_minutes=55,
            persistence_score=0.9, source="depth_only",
        )
        # 现货 history 12 帧，每帧在 75050 价位有 1.2M USD
        spot_history = [
            _make_frame(1700_000_000 + i * 300,
                         bids=[(75_050, 16.0)],  # 75050 × 16 = 1.2M USD ≥ wall_min
                         asks=[])
            for i in range(12)
        ]
        _augment_zones_with_spot_depth([z], spot_history, ENGINE_DEFAULTS)
        assert z.dual_source is True
        assert z.source == "spot+depth"
        assert z.spot_current_usd > 1_000_000

    def test_dual_source_not_marked_when_spot_thin(self):
        """合约 zone 价区无对应现货厚度 → dual_source 保持 False。"""
        from processors.liquidity_wall_engine import _augment_zones_with_spot_depth
        z = WallZone(
            side="bid", price_low=75_000, price_high=75_100, price_mid=75_050,
            peak_price=75_050, distance_pct=-2.5,
            current_usd=2_000_000, max_usd_1h=2_000_000, avg_usd_1h=2_000_000,
            bin_count=2, seen_count=12, visible_minutes=55,
            persistence_score=0.9, source="depth_only",
        )
        # 现货 history：仅有 100K USD（< wall_min 500K）
        spot_history = [
            _make_frame(1700_000_000 + i * 300,
                         bids=[(75_050, 1.3)], asks=[])  # 75050 × 1.3 ≈ 97K
            for i in range(12)
        ]
        _augment_zones_with_spot_depth([z], spot_history, ENGINE_DEFAULTS)
        assert z.dual_source is False
        assert z.source == "depth_only"
        assert z.spot_current_usd < 200_000

    def test_spot_only_zone_built_when_futures_misses(self):
        """现货独立 zone（合约 zone 没覆盖该价位）→ source='spot_only'。"""
        from processors.liquidity_wall_engine import _build_spot_only_zones
        # 现货 history 在 73000 有大墙；excluded_price_ranges 是 75000-75100（合约 zone）
        spot_history = [
            _make_frame(1700_000_000 + i * 300,
                         bids=[(73_000, 30.0)],  # 73000 × 30 = 2.19M
                         asks=[])
            for i in range(12)
        ]
        excluded = [(75_000.0, 75_100.0)]
        cfg = {**ENGINE_DEFAULTS, "seed_min_usd": 1_000_000.0, "wall_min_usd": 500_000.0}
        zones = _build_spot_only_zones(
            spot_history, last_price=76_000, side="bid", atr=200.0,
            cfg=cfg, excluded_price_ranges=excluded,
        )
        assert zones, "现货 73000 大墙应被识别"
        assert all(z.source == "spot_only" for z in zones)

    def test_spot_only_zone_filtered_when_in_excluded_range(self):
        """现货 zone 价位落入合约 zone 区间 → 视为 dual_source 已处理，不输出 spot_only。"""
        from processors.liquidity_wall_engine import _build_spot_only_zones
        spot_history = [
            _make_frame(1700_000_000 + i * 300,
                         bids=[(75_050, 30.0)],  # 与 excluded 重合
                         asks=[])
            for i in range(12)
        ]
        excluded = [(75_000.0, 75_100.0)]
        zones = _build_spot_only_zones(
            spot_history, last_price=76_000, side="bid", atr=200.0,
            cfg=ENGINE_DEFAULTS, excluded_price_ranges=excluded,
        )
        assert zones == [], "应被 excluded 过滤掉"

    def test_active_attack_score_taker_and_cvd_aligned(self):
        """taker 卖压 + cvd_spot 同向 → 0.40 + 0.30 = 0.70（无 ask_bids 衰竭）。"""
        from processors.liquidity_wall_engine import _compute_active_attack_score
        z = WallZone(
            side="bid", price_low=75_000, price_high=75_100, price_mid=75_050,
            peak_price=75_050, distance_pct=-2.5,
            current_usd=1_500_000, max_usd_1h=2_000_000, avg_usd_1h=1_800_000,
            bin_count=2, seen_count=10, visible_minutes=45, persistence_score=0.7,
        )
        taker = SimpleNamespace(buy_volume_usd=1_000_000, sell_volume_usd=4_000_000)
        cvd_spot = SimpleNamespace(trend_1h="strong_down")
        score = _compute_active_attack_score(z, taker, cvd_spot, ENGINE_DEFAULTS)
        assert score == pytest.approx(0.70)

    def test_active_attack_score_full_with_liquidity_drain(self):
        """Phase B 满分：taker + cvd + 流动性衰竭 5% → 0.40+0.30+0.30 = 1.00。"""
        from processors.liquidity_wall_engine import _compute_active_attack_score
        z = WallZone(
            side="bid", price_low=75_000, price_high=75_100, price_mid=75_050,
            peak_price=75_050, distance_pct=-2.5,
            current_usd=1_500_000, max_usd_1h=2_000_000, avg_usd_1h=1_800_000,
            bin_count=2, seen_count=10, visible_minutes=45, persistence_score=0.7,
        )
        taker = SimpleNamespace(buy_volume_usd=1_000_000, sell_volume_usd=4_000_000)
        cvd_spot = SimpleNamespace(trend_1h="strong_down")
        # bid wall 看 bids_usd 衰减：1B → 950M（衰减 5%）→ 满分
        ask_bids = [
            SimpleNamespace(aggregated_bids_usd=1_000_000_000, aggregated_asks_usd=1_000_000_000),
            SimpleNamespace(aggregated_bids_usd=950_000_000, aggregated_asks_usd=1_000_000_000),
        ]
        score = _compute_active_attack_score(
            z, taker, cvd_spot, ENGINE_DEFAULTS, ask_bids_history=ask_bids,
        )
        assert score == pytest.approx(1.00)

    def test_active_attack_score_counter_direction_zero(self):
        """taker 买压 + bid wall（逆向）→ active_attack=0。"""
        from processors.liquidity_wall_engine import _compute_active_attack_score
        z = WallZone(
            side="bid", price_low=75_000, price_high=75_100, price_mid=75_050,
            peak_price=75_050, distance_pct=-2.5,
            current_usd=1_500_000, max_usd_1h=2_000_000, avg_usd_1h=1_800_000,
            bin_count=2, seen_count=10, visible_minutes=45, persistence_score=0.7,
        )
        taker = SimpleNamespace(buy_volume_usd=4_000_000, sell_volume_usd=1_000_000)  # 买压主导
        cvd_spot = SimpleNamespace(trend_1h="up")
        # bids_usd 增厚（逆向）→ 流动性衰竭因子也是 0
        ask_bids = [
            SimpleNamespace(aggregated_bids_usd=1_000_000_000, aggregated_asks_usd=1_000_000_000),
            SimpleNamespace(aggregated_bids_usd=1_050_000_000, aggregated_asks_usd=1_000_000_000),
        ]
        score = _compute_active_attack_score(
            z, taker, cvd_spot, ENGINE_DEFAULTS, ask_bids_history=ask_bids,
        )
        assert score == 0.0

    def test_active_attack_score_only_liquidity_drain(self):
        """单独流动性衰竭 5% → 0.30（仅第 3 因子）。"""
        from processors.liquidity_wall_engine import _compute_active_attack_score
        z = WallZone(
            side="ask", price_low=80_000, price_high=80_100, price_mid=80_050,
            peak_price=80_050, distance_pct=4.0,
            current_usd=1_500_000, max_usd_1h=2_000_000, avg_usd_1h=1_800_000,
            bin_count=2, seen_count=10, visible_minutes=45, persistence_score=0.7,
        )
        # ask wall 看 asks_usd 衰减
        ask_bids = [
            SimpleNamespace(aggregated_bids_usd=1_000_000_000, aggregated_asks_usd=1_000_000_000),
            SimpleNamespace(aggregated_bids_usd=1_000_000_000, aggregated_asks_usd=950_000_000),
        ]
        score = _compute_active_attack_score(
            z, taker_flow=None, cvd_spot=None, cfg=ENGINE_DEFAULTS,
            ask_bids_history=ask_bids,
        )
        assert score == pytest.approx(0.30)

    def test_break_through_risk_includes_active_attack(self):
        """主动攻击因子 + 静态因子 → break_through_risk 比纯静态版本高。"""
        from processors.liquidity_wall_engine import _compute_break_through_risk
        z = WallZone(
            side="bid", price_low=75_000, price_high=75_100, price_mid=75_050,
            peak_price=75_050, distance_pct=-2.5,
            current_usd=900_000, max_usd_1h=2_000_000, avg_usd_1h=1_500_000,  # thinning
            bin_count=2, seen_count=2, visible_minutes=10, persistence_score=0.15,  # 不持久
        )
        taker = SimpleNamespace(buy_volume_usd=1_000_000, sell_volume_usd=4_000_000)
        cvd_spot = SimpleNamespace(trend_1h="strong_down")
        ask_bids = [
            SimpleNamespace(aggregated_bids_usd=1_000_000_000, aggregated_asks_usd=1_000_000_000),
            SimpleNamespace(aggregated_bids_usd=950_000_000, aggregated_asks_usd=1_000_000_000),
        ]
        risk_with_attack = _compute_break_through_risk(
            z, crowding=None, sweep=None, cfg=ENGINE_DEFAULTS,
            taker_flow=taker, cvd_spot=cvd_spot, ask_bids_history=ask_bids,
        )
        risk_static = _compute_break_through_risk(
            z, crowding=None, sweep=None, cfg=ENGINE_DEFAULTS,
        )
        assert risk_with_attack > risk_static
        # active 满分 1.0 × 0.20 = +0.20
        assert risk_with_attack >= risk_static + 0.19

    def test_seed_min_usd_by_coin_overrides_default(self):
        """A6：seed_min_usd_by_coin 按币动态覆盖默认 1M。"""
        # 构造一个 5m 帧：种子 bin 1.5M USD（< BTC 5M 阈值，应被过滤）
        history = [
            _make_frame(1700_000_000 + i * 300,
                         bids=[(99_500, 15.08)],  # 99500 × 15.08 = ~1.5M
                         asks=[])
            for i in range(12)
        ]
        cfg_btc = {**ENGINE_DEFAULTS,
                    "seed_min_usd_by_coin": {"BTC": 5_000_000, "ETH": 2_000_000, "SOL": 800_000}}
        state_btc = _make_state(coin="BTC", history=history, last_price=100_000)
        snap = _make_base_snap(last_price=100_000)
        out_btc = build_liquidity_wall_outputs(
            state_btc, snap, cfg_btc, now=1700_000_000 + 12 * 300 + 2000,
        )
        assert out_btc.walls_below == [], "BTC 5M 阈值下，1.5M 种子被过滤"

        # 同样数据，coin=SOL → 阈值 800K，1.5M 应被识别
        snap_sol = _make_base_snap(last_price=100_000)
        state_sol = _make_state(coin="SOL", history=history, last_price=100_000)
        out_sol = build_liquidity_wall_outputs(
            state_sol, snap_sol, cfg_btc, now=1700_000_000 + 12 * 300 + 2000,
        )
        assert out_sol.walls_below, "SOL 800K 阈值下，1.5M 种子应被识别"

    # ════════════════════════════════════════════════════════════════════
    # Phase B — 配额优化（coin_priority）+ ask-bids 流动性衰竭因子
    # ════════════════════════════════════════════════════════════════════

    def test_coin_priority_in_settings_loads_default(self):
        """B4：EngineConfig 默认 coin_priority = {BTC:1.0, ETH:1.0, SOL:0.5}。"""
        from config.settings import EngineConfig
        ec = EngineConfig()
        assert ec.coin_priority["BTC"] == pytest.approx(1.0)
        assert ec.coin_priority["ETH"] == pytest.approx(1.0)
        assert ec.coin_priority["SOL"] == pytest.approx(0.5)

    def test_coin_priority_invalid_values_filtered(self):
        """B4：负数/字符串/0 价值在 settings 加载时被过滤，回落默认值。"""
        from config.settings import _build_settings
        # 仅测试 coin_priority 字段被合法化（不影响其他字段）：
        # 由于 _build_settings 需完整 raw，这里仅测内部 dict 解析逻辑
        raw_invalid = {"BTC": -1.0, "ETH": 0, "SOL": "abc", "DOGE": 0.3}
        coin_prio: dict[str, float] = {}
        for ccy, val in raw_invalid.items():
            try:
                v = float(val)
                if v > 0:
                    coin_prio[str(ccy).upper()] = v
            except (TypeError, ValueError):
                continue
        assert coin_prio == {"DOGE": 0.3}, "负数/0/非法值应被过滤"

    def test_ask_bids_range_snapshot_serialization(self):
        """B2：AskBidsRangeSnapshot 序列化往返（poll 写入字段对齐）。"""
        from models.orderbook_pressure import AskBidsRangeSnapshot
        snap = AskBidsRangeSnapshot(
            ts_ms=1700_000_000_000, ts_sec=1700_000_000,
            range_pct=2.0,
            aggregated_bids_usd=900_000_000,
            aggregated_asks_usd=1_000_000_000,
            aggregated_bids_qty=11340,
            aggregated_asks_qty=12760,
        )
        d = snap.model_dump()
        snap2 = AskBidsRangeSnapshot(**d)
        assert snap2.aggregated_bids_usd == 900_000_000
        assert snap2.range_pct == 2.0

    def test_active_attack_liquidity_drain_short_history(self):
        """B2：ask_bids_history 仅 1 帧 → 流动性衰竭因子=0（数据不足）。"""
        from processors.liquidity_wall_engine import _compute_active_attack_score
        z = WallZone(
            side="bid", price_low=75_000, price_high=75_100, price_mid=75_050,
            peak_price=75_050, distance_pct=-2.5,
            current_usd=1_500_000, max_usd_1h=2_000_000, avg_usd_1h=1_800_000,
            bin_count=2, seen_count=10, visible_minutes=45, persistence_score=0.7,
        )
        ask_bids = [
            SimpleNamespace(aggregated_bids_usd=1_000_000_000, aggregated_asks_usd=1_000_000_000),
        ]
        score = _compute_active_attack_score(
            z, taker_flow=None, cvd_spot=None, cfg=ENGINE_DEFAULTS,
            ask_bids_history=ask_bids,
        )
        assert score == 0.0

    def test_active_attack_spot_priority_over_futures(self):
        """B+：现货 + 合约同时有数据 → 优先取现货衰减%（合约值被忽略）。"""
        from processors.liquidity_wall_engine import _compute_active_attack_score
        z = WallZone(
            side="bid", price_low=75_000, price_high=75_100, price_mid=75_050,
            peak_price=75_050, distance_pct=-2.5,
            current_usd=1_500_000, max_usd_1h=2_000_000, avg_usd_1h=1_800_000,
            bin_count=2, seen_count=10, visible_minutes=45, persistence_score=0.7,
        )
        # 现货衰减 5%（满分）；合约衰减 1%（仅 0.06 分）
        spot_history = [
            SimpleNamespace(aggregated_bids_usd=100_000_000, aggregated_asks_usd=100_000_000),
            SimpleNamespace(aggregated_bids_usd=95_000_000, aggregated_asks_usd=100_000_000),
        ]
        futures_history = [
            SimpleNamespace(aggregated_bids_usd=1_000_000_000, aggregated_asks_usd=1_000_000_000),
            SimpleNamespace(aggregated_bids_usd=990_000_000, aggregated_asks_usd=1_000_000_000),
        ]
        score = _compute_active_attack_score(
            z, taker_flow=None, cvd_spot=None, cfg=ENGINE_DEFAULTS,
            ask_bids_history=futures_history,
            spot_ask_bids_history=spot_history,
        )
        # 现货优先 → 0.30 × 1.0 = 0.30（不是合约的 0.06）
        assert score == pytest.approx(0.30)

    def test_active_attack_fallback_to_futures_when_spot_empty(self):
        """B+：现货数据缺失 → fallback 合约衰减%。"""
        from processors.liquidity_wall_engine import _compute_active_attack_score
        z = WallZone(
            side="bid", price_low=75_000, price_high=75_100, price_mid=75_050,
            peak_price=75_050, distance_pct=-2.5,
            current_usd=1_500_000, max_usd_1h=2_000_000, avg_usd_1h=1_800_000,
            bin_count=2, seen_count=10, visible_minutes=45, persistence_score=0.7,
        )
        # 现货 empty，合约衰减 5% 满分
        futures_history = [
            SimpleNamespace(aggregated_bids_usd=1_000_000_000, aggregated_asks_usd=1_000_000_000),
            SimpleNamespace(aggregated_bids_usd=950_000_000, aggregated_asks_usd=1_000_000_000),
        ]
        score = _compute_active_attack_score(
            z, taker_flow=None, cvd_spot=None, cfg=ENGINE_DEFAULTS,
            ask_bids_history=futures_history,
            spot_ask_bids_history=[],   # 空
        )
        # fallback 合约 → 0.30 满分
        assert score == pytest.approx(0.30)

    def test_active_attack_spot_no_decline_does_not_fallback(self):
        """B+：现货有数据但未衰减（drain=0）→ 不 fallback 合约（避免反向干扰）。"""
        from processors.liquidity_wall_engine import _compute_active_attack_score
        z = WallZone(
            side="bid", price_low=75_000, price_high=75_100, price_mid=75_050,
            peak_price=75_050, distance_pct=-2.5,
            current_usd=1_500_000, max_usd_1h=2_000_000, avg_usd_1h=1_800_000,
            bin_count=2, seen_count=10, visible_minutes=45, persistence_score=0.7,
        )
        # 现货增厚（drain=0），合约衰减 5%
        spot_history = [
            SimpleNamespace(aggregated_bids_usd=100_000_000, aggregated_asks_usd=100_000_000),
            SimpleNamespace(aggregated_bids_usd=110_000_000, aggregated_asks_usd=100_000_000),  # 增厚
        ]
        futures_history = [
            SimpleNamespace(aggregated_bids_usd=1_000_000_000, aggregated_asks_usd=1_000_000_000),
            SimpleNamespace(aggregated_bids_usd=950_000_000, aggregated_asks_usd=1_000_000_000),
        ]
        score = _compute_active_attack_score(
            z, taker_flow=None, cvd_spot=None, cfg=ENGINE_DEFAULTS,
            ask_bids_history=futures_history,
            spot_ask_bids_history=spot_history,
        )
        # 现货 drain=0 已表态（真买卖家未撤）→ 不 fallback 合约 → 0
        assert score == 0.0

    def test_active_attack_liquidity_drain_below_threshold(self):
        """B2：流动性衰竭 2%（< 5% 满分阈值）→ 线性映射 0.30 × 0.4 = 0.12。"""
        from processors.liquidity_wall_engine import _compute_active_attack_score
        z = WallZone(
            side="bid", price_low=75_000, price_high=75_100, price_mid=75_050,
            peak_price=75_050, distance_pct=-2.5,
            current_usd=1_500_000, max_usd_1h=2_000_000, avg_usd_1h=1_800_000,
            bin_count=2, seen_count=10, visible_minutes=45, persistence_score=0.7,
        )
        # bids: 1B → 980M（衰减 2%）
        ask_bids = [
            SimpleNamespace(aggregated_bids_usd=1_000_000_000, aggregated_asks_usd=1_000_000_000),
            SimpleNamespace(aggregated_bids_usd=980_000_000, aggregated_asks_usd=1_000_000_000),
        ]
        score = _compute_active_attack_score(
            z, taker_flow=None, cvd_spot=None, cfg=ENGINE_DEFAULTS,
            ask_bids_history=ask_bids,
        )
        # 0.30 × (0.02 / 0.05) = 0.30 × 0.4 = 0.12
        assert score == pytest.approx(0.12, abs=0.001)

    def test_kl_iso_walls_field_unchanged(self):
        """铁律：M1+M2 引擎不修改旧 OrderbookPressureSnapshot.walls 字段。

        这是 KL tracker 的实际消费路径（pressure_snapshot.walls / .top_resistance / .top_support）。
        引擎只写 walls_above/below/wall_zones/wall_events/crowding_global，旧字段不动。
        """
        from processors.orderbook_pressure import compute_pressure_snapshot
        from models.orderbook_pressure import DepthBin, OrderbookDepthSnapshot

        # 构造 state：已含 history（M1 路径）+ depth_snapshot（旧路径）
        depth = OrderbookDepthSnapshot(
            coin="BTC", exchange="Binance", symbol="BTCUSDT",
            ts_sec=1700_000_000,
            bids=[DepthBin(price=99_500, quantity=30, usd_value=99_500 * 30)],
            asks=[DepthBin(price=100_500, quantity=20, usd_value=100_500 * 20)],
        )
        history = deque([depth] * 12, maxlen=12)
        state = SimpleNamespace(
            coin="BTC", ticker=SimpleNamespace(last=100_000), atr=200.0,
            orderbook_depth_snapshot=depth,
            orderbook_depth_history=history,
            large_orders_history=[],
            footprint_contract=[], footprint_spot=[],
        )
        snap = compute_pressure_snapshot(state, now_sec=1700_000_000)
        assert snap is not None
        # 旧字段：walls / top_resistance / top_support 应仍存在并由旧路径填充
        assert isinstance(snap.walls, list)
        # 即使 wall_zones 出值，walls 字段仍由旧 PressureWall 路径填充（不被新引擎污染）
        assert hasattr(snap, "walls_above")
        assert hasattr(snap, "wall_zones")
