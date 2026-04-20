"""D04+ price_reaction_backfill 纯函数单测"""

from __future__ import annotations

import time

import pytest

from models.narrative import NarrativePriceReaction
from models.news_event import (
    AssetImpact, EnrichedNewsEvent, MarketEventSignal, RawNewsItem,
)
from processors.price_reaction_backfill import (
    _check_direction_match, _compute_delta_pct, _find_price_at, _resolve_status,
    backfill_price_reactions,
)


HOUR = 3600
DAY = 24 * HOUR


def _make_event(
    event_id: str = "e1",
    publish_sec: int = 0,
    direction: str = "bullish",
    impact: int = 3,
    status: str = "pending",
) -> EnrichedNewsEvent:
    publish_sec = publish_sec or int(time.time()) - 2 * DAY  # 默认 2 天前
    return EnrichedNewsEvent(
        raw=RawNewsItem(
            source_type="okx",
            source_author="x",
            external_id=event_id,
            publish_time=publish_sec * 1000,
            fetch_time=publish_sec,
            title="t",
            content="c",
        ),
        structured=MarketEventSignal(
            event_id=event_id,
            ts=publish_sec,
            direction=direction,
            impact_score=impact,
            narrative_theme="Fed_Rate_Policy",
        ),
        backfill_status=status,
    )


def _ph(*pts) -> list[dict]:
    """构造 price_history: _ph((ts, price), ...)"""
    return [{"ts": int(ts), "price": float(p)} for ts, p in pts]


# ── 辅助函数单测 ──

def test_compute_delta_pct_basic():
    assert _compute_delta_pct(100.0, 110.0) == 10.0
    assert _compute_delta_pct(100.0, 90.0) == -10.0
    assert _compute_delta_pct(0, 50) == 0.0
    assert _compute_delta_pct(-1, 50) == 0.0
    assert _compute_delta_pct("bad", 50) == 0.0
    assert _compute_delta_pct(50, "bad") == 0.0


def test_find_price_at_exact_match():
    ph = _ph((100, 50.0), (200, 60.0), (300, 70.0))
    idx = [p["ts"] for p in ph]
    assert _find_price_at(ph, idx, 200, tolerance_sec=5) == 60.0


def test_find_price_at_within_tolerance():
    ph = _ph((100, 50.0), (205, 60.0), (300, 70.0))
    idx = [p["ts"] for p in ph]
    # target=200 最近的是 205（差5秒），容忍 10 秒 → 命中 60.0
    assert _find_price_at(ph, idx, 200, tolerance_sec=10) == 60.0


def test_find_price_at_beyond_tolerance():
    ph = _ph((100, 50.0), (1000, 60.0))
    idx = [p["ts"] for p in ph]
    # target=500 最近的 100（差 400 秒） > tolerance 50 → None
    assert _find_price_at(ph, idx, 500, tolerance_sec=50) is None


def test_find_price_at_picks_nearest():
    ph = _ph((100, 50.0), (250, 60.0), (300, 70.0))
    idx = [p["ts"] for p in ph]
    # target=200，左(100,差100)右(250,差50) → 60.0
    assert _find_price_at(ph, idx, 200, tolerance_sec=200) == 60.0


def test_find_price_at_empty():
    assert _find_price_at([], [], 100, 10) is None


def test_check_direction_match_bullish_up():
    assert _check_direction_match("bullish", 1.5) is True
    assert _check_direction_match("bullish", -1.5) is False


def test_check_direction_match_bearish():
    assert _check_direction_match("bearish", -2.0) is True
    assert _check_direction_match("bearish", 2.0) is False


def test_check_direction_match_small_move_ignored():
    assert _check_direction_match("bullish", 0.1) is None
    assert _check_direction_match("bearish", -0.1) is None


def test_check_direction_match_neutral():
    assert _check_direction_match("neutral", 5.0) is None
    assert _check_direction_match("potential_reversal", 5.0) is None


def test_resolve_status_all_filled_returns_complete():
    now = int(time.time())
    ev = _make_event(publish_sec=now - 2 * DAY)
    ev.delta_pct_1h = 1.0
    ev.delta_pct_2h = 2.0
    ev.delta_pct_24h = 3.0
    assert _resolve_status(ev, now, now - 2 * DAY) == "complete"


def test_resolve_status_pending_when_no_fills():
    now = int(time.time())
    ev = _make_event(publish_sec=now - 100)
    assert _resolve_status(ev, now, now - 100) == "pending"


def test_resolve_status_partial_when_some_filled():
    now = int(time.time())
    ev = _make_event(publish_sec=now - 2 * HOUR)
    ev.delta_pct_1h = 1.0
    # 还不足 24h，24h 档未填 → partial
    assert _resolve_status(ev, now, now - 2 * HOUR) == "partial"


def test_resolve_status_partial_after_24h_incomplete():
    """超过 24h 但 24h 档无法填 → 最多 partial"""
    now = int(time.time())
    ev = _make_event(publish_sec=now - 2 * DAY)
    ev.delta_pct_1h = 1.0
    # 1h 已填 但 24h 档缺失（模拟历史不可用）
    assert _resolve_status(ev, now, now - 2 * DAY) == "partial"


# ── 主入口单测 ──

def test_backfill_empty_events():
    out, stats = backfill_price_reactions([], _ph((100, 50.0)), now_ts=1000)
    assert out == []
    assert stats["processed"] == 0


def test_backfill_empty_price_history_returns_pending():
    now = int(time.time())
    events = [_make_event(event_id="e1", publish_sec=now - 2 * HOUR)]
    out, stats = backfill_price_reactions(events, [], now_ts=now)
    assert len(out) == 1
    assert stats["processed"] == 1
    assert stats["still_pending"] == 1
    assert out[0].backfill_status == "pending"


def test_backfill_partial_after_1h():
    now = int(time.time())
    pub = now - 2 * HOUR  # 已过 2h
    events = [_make_event(event_id="e1", publish_sec=pub, direction="bullish")]
    # 构造 publish_sec + 1h 前后 5 分钟内有点
    ph = _ph(
        (pub, 50000.0),
        (pub + HOUR, 51000.0),
        (pub + 2 * HOUR, 51500.0),
    )
    out, stats = backfill_price_reactions(events, ph, now_ts=now)
    assert len(out) == 1
    ev = out[0]
    assert ev.price_at_publish == 50000.0
    assert ev.delta_pct_1h == 2.0  # (51000 - 50000) / 50000 * 100 = 2.0
    assert ev.delta_pct_2h == 3.0
    assert ev.delta_pct_24h is None  # 未过 24h
    # age = 2h，1h+2h 都已填但 24h 未填 → partial
    assert ev.backfill_status == "partial"
    assert stats["newly_partial"] == 1
    assert ev.backfilled_at > 0


def test_backfill_complete_after_24h():
    now = int(time.time())
    pub = now - 2 * DAY  # 已过 2 天
    events = [_make_event(event_id="e1", publish_sec=pub)]
    ph = _ph(
        (pub, 50000.0),
        (pub + HOUR, 51000.0),
        (pub + 2 * HOUR, 52000.0),
        (pub + DAY, 55000.0),
    )
    out, stats = backfill_price_reactions(events, ph, now_ts=now)
    ev = out[0]
    assert ev.delta_pct_1h == 2.0
    assert ev.delta_pct_2h == 4.0
    assert ev.delta_pct_24h == 10.0
    assert ev.backfill_status == "complete"
    assert stats["newly_complete"] == 1


def test_backfill_does_not_mutate_input():
    now = int(time.time())
    pub = now - 2 * HOUR
    events = [_make_event(event_id="e1", publish_sec=pub)]
    ph = _ph((pub, 100.0), (pub + HOUR, 105.0))

    original_status = events[0].backfill_status
    out, _ = backfill_price_reactions(events, ph, now_ts=now)
    assert events[0].backfill_status == original_status
    assert events[0].delta_pct_1h is None
    assert out[0] is not events[0]


def test_backfill_skips_events_without_publish_time():
    now = int(time.time())
    ev = _make_event(event_id="e1")
    ev.structured.ts = 0
    ev.raw.publish_time = 0
    out, stats = backfill_price_reactions([ev], _ph((now, 100.0)), now_ts=now)
    assert out[0].backfill_status == "pending"
    assert stats["still_pending"] == 1


def test_backfill_calls_narrative_tracker_on_1h_fill():
    now = int(time.time())
    pub = now - 2 * HOUR
    events = [_make_event(event_id="e1", publish_sec=pub, direction="bullish")]
    ph = _ph((pub, 100.0), (pub + HOUR, 101.0))

    class FakeTracker:
        def __init__(self):
            self.called = []

        def record_price_reaction(self, event_id, reaction):
            self.called.append((event_id, reaction))
            return True

    tracker = FakeTracker()
    out, stats = backfill_price_reactions(
        events, ph, now_ts=now, narrative_tracker=tracker,
    )
    assert stats["narrative_reactions_recorded"] == 1
    assert len(tracker.called) == 1
    eid, reaction = tracker.called[0]
    assert eid == "e1"
    assert reaction.direction == "bullish"
    assert reaction.delta_pct_1h == 1.0
    assert reaction.matched_direction is True


def test_backfill_no_narrative_call_when_tracker_none():
    now = int(time.time())
    pub = now - 2 * HOUR
    events = [_make_event(event_id="e1", publish_sec=pub)]
    ph = _ph((pub, 100.0), (pub + HOUR, 101.0))
    out, stats = backfill_price_reactions(events, ph, now_ts=now)
    assert stats["narrative_reactions_recorded"] == 0


def test_backfill_multiple_events_varying_age():
    now = int(time.time())
    events = [
        _make_event(event_id="e_young", publish_sec=now - 30 * 60),  # 未过 1h
        _make_event(event_id="e_mid", publish_sec=now - 3 * HOUR),
        _make_event(event_id="e_old", publish_sec=now - 2 * DAY),
    ]
    # 为所有三条事件提供 publish / +1h / +2h / +24h 的点
    ph_points = []
    for ev in events:
        t = ev.structured.ts
        ph_points.extend([
            (t, 100.0),
            (t + HOUR, 101.0),
            (t + 2 * HOUR, 102.0),
            (t + DAY, 110.0),
        ])
    ph_points.sort(key=lambda x: x[0])
    ph = _ph(*ph_points)

    out, stats = backfill_price_reactions(events, ph, now_ts=now)
    statuses = {ev.structured.event_id: ev.backfill_status for ev in out}
    assert statuses["e_young"] == "pending"  # 还不够 1h
    assert statuses["e_mid"] == "partial"    # 1h/2h 已填，24h 未到
    assert statuses["e_old"] == "complete"   # 三档齐全
    assert stats["newly_partial"] == 1
    assert stats["newly_complete"] == 1
    assert stats["still_pending"] == 1
