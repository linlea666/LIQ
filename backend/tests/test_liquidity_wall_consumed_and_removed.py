"""W2-T5 单元测试：第 7 类事件 wall_consumed_and_removed。

覆盖：
  触发：同帧 ended_with_exec AND ended_no_exec 都非空
  不触发：仅 consumed / 仅 removed / 都没有
  事件字段：confidence=0.75 / executed_usd_value 正确累加 / explain 含关键词
  向后兼容：第 7 事件叠加，不替代既有 wall_consumed / wall_removed
  W1-T4 联动：wall_zone_id 正确携带
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from models.orderbook_pressure import (
    LargeOrderLifecycle,
    WallZone,
)
from processors.liquidity_wall_engine import (
    ENGINE_DEFAULTS,
    _detect_zone_lifecycle_events,
)


def _make_zone(
    side: str = "bid",
    *,
    large_order_ids: list[int] = None,
    wall_zone_id: str = "test-zone-001",
) -> WallZone:
    return WallZone(
        side=side,
        price_low=76050.0, price_high=76080.0,
        price_mid=76065.0, peak_price=76065.0, distance_pct=-0.30,
        current_usd=1_000_000.0, max_usd_1h=2_000_000.0, avg_usd_1h=1_500_000.0,
        bin_count=3, seen_count=10, visible_minutes=40, persistence_score=0.5,
        large_order_ids=large_order_ids or [],
        wall_zone_id=wall_zone_id,
    )


def _make_lo(
    lo_id: int,
    *,
    state: str = "holding",
    executed_usd: float = 0.0,
    limit_price: float = 76060.0,
    start_qty: float = 5.0,
    current_qty: float = 5.0,
    end_time_ms: int = None,
) -> LargeOrderLifecycle:
    return LargeOrderLifecycle(
        id=lo_id,
        side="bid",
        limit_price=limit_price,
        start_time_ms=1_700_000_000_000,
        end_time_ms=end_time_ms,
        start_quantity=start_qty,
        current_quantity=current_qty,
        executed_usd_value=executed_usd,
        start_usd_value=start_qty * limit_price,
        current_usd_value=current_qty * limit_price,
        state=state,
    )


# ─────────────────────────────────────────────────────────────────
# 1. 触发条件
# ─────────────────────────────────────────────────────────────────

class TestConsumedAndRemovedTrigger:
    def test_both_consumed_and_removed_triggers_compound_event(self):
        """同帧既有 ended_with_exec 又有 ended_no_exec → 第 7 事件触发。"""
        zone = _make_zone(large_order_ids=[1, 2])
        los = [
            _make_lo(1, state="ended", executed_usd=300_000),  # consumed
            _make_lo(2, state="ended", executed_usd=0.0),      # removed
        ]
        events = _detect_zone_lifecycle_events(
            [zone], los, last_price=76100.0, cfg=ENGINE_DEFAULTS, now=1_700_000_300,
        )
        compound = [e for e in events if e.event_type == "wall_consumed_and_removed"]
        assert len(compound) == 1, "第 7 事件必须触发"

    def test_only_consumed_no_compound_event(self):
        """仅 consumed → 不触发第 7 事件。"""
        zone = _make_zone(large_order_ids=[1])
        los = [_make_lo(1, state="ended", executed_usd=300_000)]
        events = _detect_zone_lifecycle_events(
            [zone], los, last_price=76100.0, cfg=ENGINE_DEFAULTS, now=1_700_000_300,
        )
        compound = [e for e in events if e.event_type == "wall_consumed_and_removed"]
        assert len(compound) == 0
        # 但 wall_consumed 仍正常触发（向后兼容）
        consumed = [e for e in events if e.event_type == "wall_consumed"]
        assert len(consumed) == 1

    def test_only_removed_no_compound_event(self):
        """仅 removed → 不触发第 7 事件。"""
        zone = _make_zone(large_order_ids=[1])
        los = [_make_lo(1, state="ended", executed_usd=0.0)]
        events = _detect_zone_lifecycle_events(
            [zone], los, last_price=76100.0, cfg=ENGINE_DEFAULTS, now=1_700_000_300,
        )
        compound = [e for e in events if e.event_type == "wall_consumed_and_removed"]
        assert len(compound) == 0
        removed = [e for e in events if e.event_type == "wall_removed"]
        assert len(removed) == 1

    def test_only_holding_no_compound_event(self):
        """全部 holding → 不触发任何 ended 事件。"""
        zone = _make_zone(large_order_ids=[1, 2])
        los = [
            _make_lo(1, state="holding"),
            _make_lo(2, state="holding"),
        ]
        events = _detect_zone_lifecycle_events(
            [zone], los, last_price=76100.0, cfg=ENGINE_DEFAULTS, now=1_700_000_300,
        )
        compound = [e for e in events if e.event_type == "wall_consumed_and_removed"]
        assert len(compound) == 0


# ─────────────────────────────────────────────────────────────────
# 2. 事件字段
# ─────────────────────────────────────────────────────────────────

class TestCompoundEventFields:
    def test_confidence_higher_than_single(self):
        """第 7 事件 confidence=0.75，高于单一 consumed/removed (0.7)。"""
        zone = _make_zone(large_order_ids=[1, 2])
        los = [
            _make_lo(1, state="ended", executed_usd=300_000),
            _make_lo(2, state="ended", executed_usd=0.0),
        ]
        events = _detect_zone_lifecycle_events(
            [zone], los, last_price=76100.0, cfg=ENGINE_DEFAULTS, now=1_700_000_300,
        )
        compound = next(e for e in events if e.event_type == "wall_consumed_and_removed")
        assert compound.confidence == 0.75

    def test_executed_usd_value_aggregates_consumed_only(self):
        """executed_usd_value 仅累加 ended_with_exec 部分。"""
        zone = _make_zone(large_order_ids=[1, 2, 3])
        los = [
            _make_lo(1, state="ended", executed_usd=200_000),
            _make_lo(2, state="ended", executed_usd=400_000),
            _make_lo(3, state="ended", executed_usd=0.0),  # removed 部分不计
        ]
        events = _detect_zone_lifecycle_events(
            [zone], los, last_price=76100.0, cfg=ENGINE_DEFAULTS, now=1_700_000_300,
        )
        compound = next(e for e in events if e.event_type == "wall_consumed_and_removed")
        assert compound.executed_usd_value == 600_000

    def test_explain_contains_keywords(self):
        """explain 文案含关键词便于前端 / AI 理解。"""
        zone = _make_zone(large_order_ids=[1, 2])
        los = [
            _make_lo(1, state="ended", executed_usd=500_000),
            _make_lo(2, state="ended", executed_usd=0.0),
        ]
        events = _detect_zone_lifecycle_events(
            [zone], los, last_price=76100.0, cfg=ENGINE_DEFAULTS, now=1_700_000_300,
        )
        compound = next(e for e in events if e.event_type == "wall_consumed_and_removed")
        assert "试盘后撤退" in compound.explain
        assert "0.5M USD" in compound.explain  # 500k formatted
        assert "撤单 1 笔" in compound.explain

    def test_size_before_after_correct(self):
        """size_before/after_usd 引用 zone.max_usd_1h / current_usd。"""
        zone = _make_zone(large_order_ids=[1, 2])
        los = [
            _make_lo(1, state="ended", executed_usd=300_000),
            _make_lo(2, state="ended", executed_usd=0.0),
        ]
        events = _detect_zone_lifecycle_events(
            [zone], los, last_price=76100.0, cfg=ENGINE_DEFAULTS, now=1_700_000_300,
        )
        compound = next(e for e in events if e.event_type == "wall_consumed_and_removed")
        assert compound.size_before_usd == 2_000_000  # zone.max_usd_1h
        assert compound.size_after_usd == 1_000_000   # zone.current_usd


# ─────────────────────────────────────────────────────────────────
# 3. 向后兼容：第 7 事件叠加，不替代既有事件
# ─────────────────────────────────────────────────────────────────

class TestBackwardCompatibility:
    def test_compound_event_adds_to_existing_consumed_and_removed(self):
        """同帧仍独立发 wall_consumed + wall_removed；第 7 事件是叠加而非替代。
           前端 / AI / archiver 可继续消费旧两个事件，不需升级。"""
        zone = _make_zone(large_order_ids=[1, 2])
        los = [
            _make_lo(1, state="ended", executed_usd=300_000),
            _make_lo(2, state="ended", executed_usd=0.0),
        ]
        events = _detect_zone_lifecycle_events(
            [zone], los, last_price=76100.0, cfg=ENGINE_DEFAULTS, now=1_700_000_300,
        )
        types = {e.event_type for e in events}
        assert "wall_consumed" in types
        assert "wall_removed" in types
        assert "wall_consumed_and_removed" in types


# ─────────────────────────────────────────────────────────────────
# 4. W1-T4 联动：wall_zone_id 正确携带
# ─────────────────────────────────────────────────────────────────

class TestZoneIdPropagation:
    def test_compound_event_carries_wall_zone_id(self):
        """第 7 事件必须正确带上 zone.wall_zone_id（与其他事件一致）。"""
        zone = _make_zone(large_order_ids=[1, 2], wall_zone_id="abc12345")
        los = [
            _make_lo(1, state="ended", executed_usd=300_000),
            _make_lo(2, state="ended", executed_usd=0.0),
        ]
        events = _detect_zone_lifecycle_events(
            [zone], los, last_price=76100.0, cfg=ENGINE_DEFAULTS, now=1_700_000_300,
        )
        compound = next(e for e in events if e.event_type == "wall_consumed_and_removed")
        assert compound.wall_zone_id == "abc12345"

    def test_compound_event_side_matches_zone(self):
        """side 跟随 zone（bid/ask）。"""
        # bid wall
        zone_bid = _make_zone(side="bid", large_order_ids=[1, 2], wall_zone_id="bid-001")
        los = [
            _make_lo(1, state="ended", executed_usd=300_000),
            _make_lo(2, state="ended", executed_usd=0.0),
        ]
        events = _detect_zone_lifecycle_events(
            [zone_bid], los, last_price=76100.0, cfg=ENGINE_DEFAULTS, now=1_700_000_300,
        )
        compound = next(e for e in events if e.event_type == "wall_consumed_and_removed")
        assert compound.side == "bid"

        # ask wall
        zone_ask = _make_zone(side="ask", large_order_ids=[1, 2], wall_zone_id="ask-001")
        events_ask = _detect_zone_lifecycle_events(
            [zone_ask], los, last_price=76100.0, cfg=ENGINE_DEFAULTS, now=1_700_000_300,
        )
        compound_ask = next(e for e in events_ask if e.event_type == "wall_consumed_and_removed")
        assert compound_ask.side == "ask"
