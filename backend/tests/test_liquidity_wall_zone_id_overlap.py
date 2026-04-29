"""W1-T4 单元测试：wall_zone_id 稳定性 + bin 区间 overlap 加权。

覆盖：
  ID 稳定性：
    - 同一物理墙 ±0.5 ATR 漂移内 wall_zone_id 不变
    - 不同 side / 不同 coin / 不同价区 ID 不同
    - peak_price <= 0 返回空串
    - ATR 缺失时按价格 0.15% 兜底
    - 12 hex chars 格式
  bin overlap：
    - bin 完全在 zone 内 → ratio=1.0
    - bin 完全在 zone 外 → ratio=0
    - bin 部分跨越 zone → ratio ∈ (0, 1) 严格按比例
    - bin_half_width=0 退化为点匹配（向后兼容）
  bin step 估算：
    - 单一间距（中位数=该间距）
    - 间距混杂（取中位数）
    - bins < 2 返回 0
  wall_event 关联 wall_zone_id：
    - trend 派生事件（appeared/strengthened/weakened）带正确 ID
    - large_orders 派生事件（consumed/removed/reloaded）带正确 ID
  spot bin 跨多个 zone 不重复加 dual_source（核心修复点）
"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from models.orderbook_pressure import (
    DepthBin,
    LargeOrderLifecycle,
    OrderbookDepthSnapshot,
    WallZone,
)
from processors.liquidity_wall_engine import (
    ENGINE_DEFAULTS,
    _augment_zones_with_spot_depth,
    _bin_overlap_ratio,
    _build_wall_zone_id,
    _detect_zone_lifecycle_events,
    _estimate_bin_step,
)


# ─────────────────────────────────────────────────────────────────
# 1. wall_zone_id 稳定性
# ─────────────────────────────────────────────────────────────────

class TestWallZoneIdStability:
    def test_same_zone_with_drift_yields_same_id(self):
        """ATR=200，bucket_size = max(100, 76450*0.0015≈115, 0.5) = 115。
        peak=76400 / bucket=115 → bucket_idx=664（同桶，ID 一致）。
        peak=76450 / bucket=115 → bucket_idx=664（同桶，ID 一致）。
        peak=76500 / bucket=115 → bucket_idx=665（跳到下一桶，ID 不同）。"""
        atr = 200.0
        id_a = _build_wall_zone_id("BTC", "ask", 76400.0, atr)
        id_b = _build_wall_zone_id("BTC", "ask", 76450.0, atr)
        id_c = _build_wall_zone_id("BTC", "ask", 76499.0, atr)
        # 同桶内
        assert id_a == id_b == id_c
        # 跨桶
        id_d = _build_wall_zone_id("BTC", "ask", 76600.0, atr)
        assert id_d != id_a

    def test_different_side_yields_different_id(self):
        atr = 200.0
        id_ask = _build_wall_zone_id("BTC", "ask", 76450.0, atr)
        id_bid = _build_wall_zone_id("BTC", "bid", 76450.0, atr)
        assert id_ask != id_bid

    def test_different_coin_yields_different_id(self):
        atr = 50.0
        id_btc = _build_wall_zone_id("BTC", "ask", 3500.0, atr)
        id_eth = _build_wall_zone_id("ETH", "ask", 3500.0, atr)
        assert id_btc != id_eth

    def test_invalid_peak_returns_empty(self):
        assert _build_wall_zone_id("BTC", "ask", 0.0, 200.0) == ""
        assert _build_wall_zone_id("BTC", "ask", -1.0, 200.0) == ""

    def test_atr_missing_falls_back_to_price_pct(self):
        """ATR 缺失 → bucket = max(0, 76450×0.0015≈115, 0.5) = 115。
        与 ATR=0 等价，仍然可生成稳定 ID。"""
        id_no_atr = _build_wall_zone_id("BTC", "ask", 76450.0, None)
        id_zero_atr = _build_wall_zone_id("BTC", "ask", 76450.0, 0.0)
        assert id_no_atr == id_zero_atr
        assert len(id_no_atr) == 12

    def test_id_format_is_12_hex_chars(self):
        wid = _build_wall_zone_id("BTC", "ask", 76450.0, 200.0)
        assert len(wid) == 12
        # 全 hex
        int(wid, 16)


# ─────────────────────────────────────────────────────────────────
# 2. bin overlap ratio
# ─────────────────────────────────────────────────────────────────

class TestBinOverlapRatio:
    def test_bin_fully_inside_zone_ratio_one(self):
        # bin [76095, 76105]，zone [76050, 76150]，bin 完全在 zone 内
        ratio = _bin_overlap_ratio(76100.0, 5.0, 76050.0, 76150.0)
        assert ratio == 1.0

    def test_bin_fully_outside_zone_ratio_zero(self):
        # bin [76050, 76150]，zone [76300, 76400]
        ratio = _bin_overlap_ratio(76100.0, 50.0, 76300.0, 76400.0)
        assert ratio == 0.0

    def test_bin_half_overlaps_zone(self):
        """bin [76050, 76150] 100 USD 宽，zone [76100, 76200]，重叠区间 [76100, 76150] 50 USD。
        ratio = 50/100 = 0.5。这是 W1-T4 修复的核心场景：粗 spot bin 部分覆盖 zone。"""
        ratio = _bin_overlap_ratio(76100.0, 50.0, 76100.0, 76200.0)
        assert ratio == 0.5

    def test_bin_zone_smaller_than_bin(self):
        """zone [76060, 76090] 30 USD，spot bin [76050, 76150] 100 USD。
        重叠 = 30/100 = 0.3。这是 W1-T4 真实场景：futures zone 跨度小于 spot bin 间距。"""
        ratio = _bin_overlap_ratio(76100.0, 50.0, 76060.0, 76090.0)
        assert ratio == 0.3

    def test_zero_half_width_falls_back_to_point_match(self):
        # 半宽=0 → 点匹配（向后兼容旧行为）
        assert _bin_overlap_ratio(76100.0, 0.0, 76050.0, 76150.0) == 1.0
        assert _bin_overlap_ratio(76300.0, 0.0, 76050.0, 76150.0) == 0.0

    def test_partial_overlap_at_zone_left_edge(self):
        # bin [76050, 76150]，zone [76130, 76200]，重叠 [76130, 76150] = 20/100 = 0.2
        ratio = _bin_overlap_ratio(76100.0, 50.0, 76130.0, 76200.0)
        assert ratio == pytest.approx(0.2, abs=1e-9)


# ─────────────────────────────────────────────────────────────────
# 3. bin step 估算
# ─────────────────────────────────────────────────────────────────

class TestEstimateBinStep:
    def test_uniform_step_returns_step(self):
        bins = [
            DepthBin(price=p, quantity=1.0, usd_value=p)
            for p in [76000, 76100, 76200, 76300, 76400]
        ]
        assert _estimate_bin_step(bins) == 100.0

    def test_irregular_steps_returns_median(self):
        # 间距 [50, 100, 100, 200] → 中位数 = 100
        bins = [
            DepthBin(price=p, quantity=1.0, usd_value=p)
            for p in [76000, 76050, 76150, 76250, 76450]
        ]
        assert _estimate_bin_step(bins) == 100.0

    def test_too_few_bins_returns_zero(self):
        assert _estimate_bin_step([]) == 0.0
        assert _estimate_bin_step([DepthBin(price=1, quantity=1, usd_value=1)]) == 0.0

    def test_same_price_returns_zero(self):
        bins = [DepthBin(price=76000, quantity=1, usd_value=1) for _ in range(3)]
        assert _estimate_bin_step(bins) == 0.0


# ─────────────────────────────────────────────────────────────────
# 4. spot bin 跨多个 zone 不重复加 dual_source（核心修复）
# ─────────────────────────────────────────────────────────────────

class TestSpotBinNotDoubleCounted:
    def _make_zone(self, price_low: float, price_high: float, side: str = "ask") -> WallZone:
        peak = (price_low + price_high) / 2
        return WallZone(
            wall_zone_id="",
            side=side,
            price_low=price_low, price_high=price_high,
            price_mid=peak, peak_price=peak,
            distance_pct=0.0,
            current_usd=2_000_000.0, max_usd_1h=2_000_000.0,
            avg_usd_1h=2_000_000.0, bin_count=3,
            seen_count=10, visible_minutes=40, persistence_score=0.8,
        )

    def _make_spot_history(self, bins_per_frame: list[list[tuple[float, float]]]) -> list:
        """每帧 bins 是 (price, usd_value) 列表（asks）。"""
        out = []
        for i, bins in enumerate(bins_per_frame):
            out.append(OrderbookDepthSnapshot(
                coin="BTC", exchange="Binance", symbol="BTCUSDT",
                ts_sec=1_700_000_000 + i * 300,
                bids=[],
                asks=[DepthBin(price=p, quantity=u/p, usd_value=u) for p, u in bins],
            ))
        return out

    def test_single_spot_bin_split_proportionally_between_two_zones(self):
        """关键场景：spot bin 间距 100 USD（76050-76150），两个相邻 futures zone：
          zone1: [76055, 76075]（20 USD 跨度，靠左）
          zone2: [76125, 76145]（20 USD 跨度，靠右）
        spot bin 76100 厚度 5_000_000 USD：
          - zone1 应分到 20/100 × 5M = 1M USD
          - zone2 应分到 20/100 × 5M = 1M USD
          - 两者总和 ≤ 5M（不会超出 spot bin 真实 USD）
        旧实现（点匹配）：两个 zone 都不会拿到（76100 不在 zone1 也不在 zone2），漏算
        若直接用宽匹配（zone.lo - bin_step <= price <= zone.hi + bin_step）：
          会让两个 zone 都拿全额 5M，重复计入。
        """
        zone1 = self._make_zone(76055, 76075)
        zone2 = self._make_zone(76125, 76145)
        zones = [zone1, zone2]

        # 提供一个稀疏的 bins 让 _estimate_bin_step 推出 100
        spot_history = self._make_spot_history([
            [(76000, 1_000_000), (76100, 5_000_000), (76200, 1_000_000)],
        ])
        cfg = {**ENGINE_DEFAULTS, "wall_min_usd": 500_000}
        _augment_zones_with_spot_depth(zones, spot_history, cfg)

        # 每个 zone 拿到的 USD 在 [0, 5M] 之间
        # zone1 重叠 [76055,76075] 与 [76050,76150] = 20，bin 100 USD → 0.2 × 5M = 1M
        # zone2 重叠 [76125,76145] 与 [76050,76150] = 20，bin 100 USD → 0.2 × 5M = 1M
        assert zone1.spot_current_usd == pytest.approx(1_000_000.0, abs=1.0)
        assert zone2.spot_current_usd == pytest.approx(1_000_000.0, abs=1.0)
        # 两 zone USD 和不超过 spot bin 真实 USD（关键不变量：单 bin 不能被超额计入）
        assert zone1.spot_current_usd + zone2.spot_current_usd <= 5_000_000.0 + 1.0

    def test_spot_bin_smaller_than_zone_full_credit(self):
        """spot bin 间距 5 USD（细颗粒），zone 跨度 30 USD：
          中间 bin 完全在 zone 内 → ratio=1.0；两侧边缘 bin（半宽 2.5 跨过 zone
          边界）→ ratio=0.5。
          7 个 bin 各 100K：5 个内部 ×1.0 + 2 个边缘 ×0.5 = 600K（区间 overlap
          的物理正确性：边缘 bin 只算一半，不会出现整 bin 误计入超出 zone 的部分）。
        """
        zone = self._make_zone(76055, 76085)
        spot_history = self._make_spot_history([
            [(p, 100_000) for p in [76055, 76060, 76065, 76070, 76075, 76080, 76085]],
        ])
        cfg = {**ENGINE_DEFAULTS, "wall_min_usd": 500_000}
        _augment_zones_with_spot_depth([zone], spot_history, cfg)

        # 5 内部 + 2 边缘 ×0.5 = 600K
        assert zone.spot_current_usd == pytest.approx(600_000.0, abs=1.0)

    def test_dense_bins_well_inside_zone_full_credit(self):
        """全部 bin 中心严格在 zone 内（含两侧 buffer），所有 bin 全额计入。
        bins 间距 5 USD，半宽 2.5 USD，zone 比 bins 范围两侧各宽出 5 USD：
          每个 bin [center-2.5, center+2.5] 完全落入 zone → ratio=1.0。
        """
        zone = self._make_zone(76050, 76090)  # 比 bins 范围两侧各宽 5 USD
        spot_history = self._make_spot_history([
            [(p, 100_000) for p in [76055, 76060, 76065, 76070, 76075, 76080, 76085]],
        ])
        cfg = {**ENGINE_DEFAULTS, "wall_min_usd": 500_000}
        _augment_zones_with_spot_depth([zone], spot_history, cfg)
        assert zone.spot_current_usd == pytest.approx(700_000.0, abs=1.0)


# ─────────────────────────────────────────────────────────────────
# 5. wall_event 关联 wall_zone_id
# ─────────────────────────────────────────────────────────────────

class TestWallEventCarriesId:
    def _make_zone_with_id(self, side: str, peak: float, zid: str, **kw) -> WallZone:
        defaults = dict(
            wall_zone_id=zid,
            side=side, price_low=peak - 10, price_high=peak + 10,
            price_mid=peak, peak_price=peak, distance_pct=0.0,
            current_usd=2_000_000.0, max_usd_1h=2_000_000.0, avg_usd_1h=2_000_000.0,
            bin_count=2, seen_count=5, visible_minutes=20, persistence_score=0.5,
            trend="new",
        )
        defaults.update(kw)
        return WallZone(**defaults)

    def test_trend_appeared_event_carries_zone_id(self):
        zone = self._make_zone_with_id("ask", 76450.0, "abc123def456", trend="new")
        events = _detect_zone_lifecycle_events([zone], [], 76300.0, ENGINE_DEFAULTS, now=1_700_000_000)
        assert any(e.event_type == "wall_appeared" for e in events)
        for e in events:
            assert e.wall_zone_id == "abc123def456"

    def test_strengthening_event_carries_zone_id(self):
        zone = self._make_zone_with_id(
            "bid", 75800.0, "deadbeef0001",
            trend="strengthening",
            current_usd=8_000_000.0, avg_usd_1h=4_000_000.0,
        )
        events = _detect_zone_lifecycle_events([zone], [], 76000.0, ENGINE_DEFAULTS, now=1_700_000_000)
        assert any(e.event_type == "wall_strengthened" for e in events)
        for e in events:
            assert e.wall_zone_id == "deadbeef0001"

    def test_consumed_event_carries_zone_id(self):
        zone = self._make_zone_with_id(
            "bid", 75800.0, "feedface9999",
            trend="weakening",
            large_order_ids=[1001],
        )
        large_orders = [
            LargeOrderLifecycle(
                id=1001, side="bid", limit_price=75800.0,
                start_time_ms=1_700_000_000_000, end_time_ms=1_700_000_300_000,
                start_quantity=10.0, current_quantity=0.0,
                executed_volume=10.0, executed_usd_value=8e5,
                start_usd_value=8e5, current_usd_value=0.0,
                trade_count=1, state="ended", exchange_name="Binance",
            ),
        ]
        events = _detect_zone_lifecycle_events([zone], large_orders, 76000.0, ENGINE_DEFAULTS, now=1_700_000_400)
        consumed = [e for e in events if e.event_type == "wall_consumed"]
        assert len(consumed) == 1
        assert consumed[0].wall_zone_id == "feedface9999"

    def test_zone_without_id_event_id_is_empty(self):
        """zone.wall_zone_id 为空时（非主流程构造的测试夹具），事件 id 也为空，不抛异常。"""
        zone = self._make_zone_with_id("ask", 76450.0, "", trend="new")
        events = _detect_zone_lifecycle_events([zone], [], 76300.0, ENGINE_DEFAULTS, now=1_700_000_000)
        for e in events:
            assert e.wall_zone_id == ""
