"""P4 虚假挂单识别增强测试。

覆盖：
  1) 撤单情境标记：wall_removed 事件附加 price_distance_at_removal，
     近距（<1%）撤单 explain 提示 + confidence 上调
  2) removal_risk 近距无成交撤单加分档（+0.15）
  3) 重挂指纹：_track_zone_reappearance 计数规则
     （min_gap 防噪、window 过期清零、GC 回收）
  4) trust 打折：reappeared_count ≥ 2 起扣分，封顶 3 次
"""

from __future__ import annotations

import pytest

from models.orderbook_pressure import LargeOrderLifecycle, WallZone
from processors.liquidity_wall_engine import (
    ENGINE_DEFAULTS,
    _compute_trust_breakdown,
    _compute_wall_removal_risk,
    _detect_zone_lifecycle_events,
    _track_zone_reappearance,
    reset_reappearance_registry_for_test,
)


def _zone(**kw) -> WallZone:
    base = dict(
        side="bid", price_low=99_000, price_high=99_200, price_mid=99_100,
        peak_price=99_100, distance_pct=-0.9, current_usd=2_000_000,
        max_usd_1h=2_000_000, avg_usd_1h=1_800_000, bin_count=2,
        seen_count=5, visible_minutes=25, persistence_score=0.55,
    )
    base.update(kw)
    return WallZone(**base)


def _ended_no_exec_lo(i: int = 1, price: float = 99_100) -> LargeOrderLifecycle:
    return LargeOrderLifecycle(
        id=i, side="bid", limit_price=price, start_time_ms=0,
        executed_usd_value=0, state="ended",
        start_quantity=0, current_quantity=0,
    )


# ════════════════════════════════════════════════════════════════════
# 1+2) 撤单情境标记 + removal_risk 近距加分
# ════════════════════════════════════════════════════════════════════
class TestNearRemoval:

    def test_removal_risk_near_distance_bonus(self):
        # 单笔无成交撤单占比 <0.5（1/3）→ 不触发 0.40 档；
        # persistence 高、厚度未滑坡 → 只剩近距档 + 快闪档可比对
        holding = [LargeOrderLifecycle(
            id=i, side="bid", limit_price=99_100, start_time_ms=0,
            executed_usd_value=0, state="holding",
            start_quantity=0, current_quantity=0,
            holding_age_sec=3600,
        ) for i in range(2, 4)]
        los = [_ended_no_exec_lo(1)] + holding
        # holding_age 均值 (0+3600+3600)/3 = 2400 ≥ 300 → 无快闪档
        near = _zone(distance_pct=-0.5, persistence_score=0.8,
                     current_usd=2_000_000, max_usd_1h=2_000_000)
        far = _zone(distance_pct=-3.0, persistence_score=0.8,
                    current_usd=2_000_000, max_usd_1h=2_000_000)
        risk_near = _compute_wall_removal_risk(near, los, ENGINE_DEFAULTS)
        risk_far = _compute_wall_removal_risk(far, los, ENGINE_DEFAULTS)
        assert risk_near - risk_far == pytest.approx(
            ENGINE_DEFAULTS["removal_near_bonus"], abs=0.001)

    def test_no_bonus_without_removed_orders(self):
        # 近距但没有无成交撤单 → 不加近距档。
        # holding_age_sec 是计算属性（start/end 时间未设 → 0），
        # 快闪档 +0.10 固定触发，作为基线扣除。
        z = _zone(distance_pct=-0.5, persistence_score=0.8)
        consumed = [LargeOrderLifecycle(
            id=1, side="bid", limit_price=99_100, start_time_ms=0,
            executed_usd_value=1_000_000, state="ended",
            start_quantity=0, current_quantity=0,
        )]
        risk = _compute_wall_removal_risk(z, consumed, ENGINE_DEFAULTS)
        assert risk == pytest.approx(0.10, abs=0.001)  # 仅快闪档，无近距档

    def test_removed_event_carries_distance(self):
        z = _zone(large_order_ids=[1], distance_pct=-0.5)
        los = [_ended_no_exec_lo(1)]
        events = _detect_zone_lifecycle_events(
            [z], los, last_price=99_500, cfg=ENGINE_DEFAULTS, now=1_700_000_000,
        )
        removed = [e for e in events if e.event_type == "wall_removed"]
        assert len(removed) == 1
        ev = removed[0]
        # |99100 - 99500| / 99500 × 100 ≈ 0.402%
        assert ev.price_distance_at_removal == pytest.approx(0.402, abs=0.005)
        assert "钓鱼单嫌疑" in ev.explain
        assert ev.confidence == pytest.approx(0.75)

    def test_removed_event_far_distance_no_flag(self):
        z = _zone(price_low=95_000, price_high=95_200, price_mid=95_100,
                  peak_price=95_100, distance_pct=-4.4, large_order_ids=[1])
        los = [_ended_no_exec_lo(1, price=95_100)]
        events = _detect_zone_lifecycle_events(
            [z], los, last_price=99_500, cfg=ENGINE_DEFAULTS, now=1_700_000_000,
        )
        removed = [e for e in events if e.event_type == "wall_removed"]
        assert len(removed) == 1
        ev = removed[0]
        assert ev.price_distance_at_removal == pytest.approx(4.422, abs=0.01)
        assert "钓鱼单嫌疑" not in ev.explain
        assert ev.confidence == pytest.approx(0.7)


# ════════════════════════════════════════════════════════════════════
# 3) 重挂指纹注册表
# ════════════════════════════════════════════════════════════════════
class TestReappearanceTracking:

    def setup_method(self):
        reset_reappearance_registry_for_test()

    def teardown_method(self):
        reset_reappearance_registry_for_test()

    def _step(self, coin, zone_ids, now):
        zones = []
        for zid in zone_ids:
            z = _zone()
            z.wall_zone_id = zid
            zones.append(z)
        _track_zone_reappearance(coin, zones, now, ENGINE_DEFAULTS)
        return zones

    def test_reappear_within_window_counts(self):
        t0 = 1_700_000_000
        self._step("BTC", ["aaa"], t0)                 # 首次出现
        self._step("BTC", [], t0 + 10)                 # 消失（记 missing_since）
        zones = self._step("BTC", ["aaa"], t0 + 10 + 120)  # 120s 后重现（≥60s ≤900s）
        assert zones[0].reappeared_count == 1

    def test_short_gap_is_noise(self):
        # 消失 <60s 即重现 → 帧级噪声，不计数
        t0 = 1_700_000_000
        self._step("BTC", ["aaa"], t0)
        self._step("BTC", [], t0 + 7)
        zones = self._step("BTC", ["aaa"], t0 + 7 + 30)
        assert zones[0].reappeared_count == 0

    def test_gap_beyond_window_resets(self):
        # 先攒 2 次 reappear，再超窗消失重现 → 清零（旧指纹过期）
        t0 = 1_700_000_000
        self._step("BTC", ["aaa"], t0)
        self._step("BTC", [], t0 + 10)
        self._step("BTC", ["aaa"], t0 + 130)           # count=1
        self._step("BTC", [], t0 + 140)
        zones = self._step("BTC", ["aaa"], t0 + 260)   # count=2
        assert zones[0].reappeared_count == 2
        self._step("BTC", [], t0 + 270)
        zones = self._step("BTC", ["aaa"], t0 + 270 + 1000)  # >900s
        assert zones[0].reappeared_count == 0

    def test_coins_isolated(self):
        t0 = 1_700_000_000
        self._step("BTC", ["aaa"], t0)
        self._step("BTC", [], t0 + 10)
        self._step("ETH", ["aaa"], t0 + 130)           # 不同币同 id 不串
        zones = self._step("ETH", ["aaa"], t0 + 200)
        assert zones[0].reappeared_count == 0
        zones = self._step("BTC", ["aaa"], t0 + 130)
        assert zones[0].reappeared_count == 1

    def test_gc_after_long_absence(self):
        from processors.liquidity_wall_engine import (
            _REAPPEAR_GC_SECONDS,
            _REAPPEAR_REGISTRY,
        )
        t0 = 1_700_000_000
        self._step("BTC", ["aaa"], t0)
        self._step("BTC", [], t0 + 10)
        # 超过 GC 阈值后任意一帧触发回收
        self._step("BTC", ["bbb"], t0 + _REAPPEAR_GC_SECONDS + 60)
        assert "aaa" not in _REAPPEAR_REGISTRY.get("BTC", {})
        assert "bbb" in _REAPPEAR_REGISTRY.get("BTC", {})


# ════════════════════════════════════════════════════════════════════
# 4) trust 打折
# ════════════════════════════════════════════════════════════════════
class TestReappearTrustPenalty:

    def test_no_penalty_below_two(self):
        z0 = _zone(reappeared_count=0)
        z1 = _zone(reappeared_count=1)
        f0, c0 = _compute_trust_breakdown(z0, ENGINE_DEFAULTS)
        f1, c1 = _compute_trust_breakdown(z1, ENGINE_DEFAULTS)
        assert f0 == f1
        assert "reappear_penalty" not in c0
        assert "reappear_penalty" not in c1

    def test_penalty_scales_and_caps(self):
        base, _ = _compute_trust_breakdown(_zone(reappeared_count=0),
                                           ENGINE_DEFAULTS)
        unit = ENGINE_DEFAULTS["reappear_trust_penalty"]
        f2, c2 = _compute_trust_breakdown(_zone(reappeared_count=2),
                                          ENGINE_DEFAULTS)
        assert base - f2 == pytest.approx(unit * 2, abs=0.001)
        assert c2["reappear_penalty"] == pytest.approx(-unit * 2, abs=0.001)
        # 封顶 3 次：count=5 与 count=3 扣分相同
        f3, _ = _compute_trust_breakdown(_zone(reappeared_count=3),
                                         ENGINE_DEFAULTS)
        f5, c5 = _compute_trust_breakdown(_zone(reappeared_count=5),
                                          ENGINE_DEFAULTS)
        assert f3 == f5
        assert c5["reappear_penalty"] == pytest.approx(-unit * 3, abs=0.001)
