"""Unit tests · D13 NewsLedger"""

from __future__ import annotations

import json
import os
import time
import tempfile

import pytest

from models.news_event import EnrichedNewsEvent, MarketEventSignal, RawNewsItem
from processors.news_ledger import (
    NewsLedger,
    _DEFAULT_MAX_AGE_SEC,
    _RECENT_WINDOW_SEC,
    get_ledger,
    reset_ledger,
)


def _make_event(event_id: str, ts: int, tier: str = "normal", direction: str = "bullish") -> EnrichedNewsEvent:
    raw = RawNewsItem(
        source_type="okx",
        external_id=event_id,
        publish_time=ts * 1000,
        fetch_time=ts,
        title=f"title-{event_id}",
    )
    sig = MarketEventSignal(
        event_id=event_id,
        ts=ts,
        direction=direction,  # type: ignore[arg-type]
        tier=tier,  # type: ignore[arg-type]
        impact_score=3,
        confidence=0.8,
    )
    return EnrichedNewsEvent(raw=raw, structured=sig)


class TestNewsLedgerBasics:
    def test_upsert_adds_and_updates(self):
        l = NewsLedger()
        ev1 = _make_event("e1", int(time.time()))
        added, updated = l.upsert_many([ev1])
        assert (added, updated) == (1, 0)
        assert l.size() == 1
        assert l.contains("e1")

        # 重复 event_id → update
        ev1b = _make_event("e1", int(time.time()), tier="major")
        added, updated = l.upsert_many([ev1b])
        assert (added, updated) == (0, 1)
        assert l.get("e1").structured.tier == "major"

    def test_upsert_skips_empty_event_id(self):
        l = NewsLedger()
        ev = _make_event("", int(time.time()))
        added, updated = l.upsert_many([ev])
        assert (added, updated) == (0, 0)
        assert l.size() == 0

    def test_single_upsert_returns_added_bool(self):
        l = NewsLedger()
        assert l.upsert(_make_event("e1", int(time.time()))) is True
        assert l.upsert(_make_event("e1", int(time.time()))) is False

    def test_stats_shape(self):
        l = NewsLedger()
        now = int(time.time())
        l.upsert_many([
            _make_event("e1", now, tier="major"),
            _make_event("e2", now - 3600, tier="blackswan"),
            _make_event("e3", now - _RECENT_WINDOW_SEC - 10, tier="normal"),  # 超 24h
        ])
        st = l.stats()
        assert st["total"] == 3
        assert st["recent_24h"] == 2
        assert st["by_tier_24h"]["major"] == 1
        assert st["by_tier_24h"]["blackswan"] == 1
        assert st["pending_backfill"] == 3  # 默认都是 pending


class TestNewsLedgerPruning:
    def test_prune_expired_removes_old(self):
        # 初始化时 age 够大，写入后再手动 prune 触发淘汰
        l = NewsLedger(max_age_sec=3600 * 100)
        now = int(time.time())
        l.upsert_many([
            _make_event("new", now),
            _make_event("old", now - 7200),  # 2h 前
        ])
        # 动态改小窗口后再 prune
        l._max_age_sec = 3600
        removed = l.prune_expired(now_ts=now)
        assert removed == 1
        assert l.contains("new")
        assert not l.contains("old")

    def test_capacity_limit_drops_oldest(self):
        l = NewsLedger(max_events=3)
        now = int(time.time())
        for i in range(5):
            l.upsert_many([_make_event(f"e{i}", now + i)])
        assert l.size() == 3
        # 前两个被 LRU 丢弃
        assert not l.contains("e0")
        assert not l.contains("e1")
        assert l.contains("e2")
        assert l.contains("e4")


class TestNewsLedgerQueries:
    def test_get_recent_filters_window(self):
        l = NewsLedger()
        now = int(time.time())
        l.upsert_many([
            _make_event("now", now),
            _make_event("1h_ago", now - 3600),
            _make_event("30h_ago", now - 30 * 3600),
        ])
        recent = l.get_recent(window_sec=24 * 3600)
        ids = {e.structured.event_id for e in recent}
        assert ids == {"now", "1h_ago"}

    def test_get_pending_backfill_filters_complete(self):
        l = NewsLedger()
        now = int(time.time())
        ev_pending = _make_event("p1", now)
        ev_done = _make_event("p2", now)
        ev_done.backfill_status = "complete"
        l.upsert_many([ev_pending, ev_done])
        pending = l.get_pending_backfill()
        assert len(pending) == 1
        assert pending[0].structured.event_id == "p1"

    def test_replace_many_only_updates_existing(self):
        l = NewsLedger()
        now = int(time.time())
        l.upsert_many([_make_event("e1", now)])
        updated_ev = _make_event("e1", now)
        updated_ev.backfill_status = "complete"
        new_ev = _make_event("e2", now)  # 不存在 → 不应被写入
        updated = l.replace_many([updated_ev, new_ev])
        assert updated == 1
        assert l.get("e1").backfill_status == "complete"
        assert not l.contains("e2")


class TestNewsLedgerPersistence:
    def test_persist_and_reload(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "ledger.json")
            l = NewsLedger(persist_path=path)
            now = int(time.time())
            l.upsert_many([_make_event("e1", now), _make_event("e2", now)])
            assert l.persist_to_disk(force=True) is True
            assert os.path.exists(path)

            l2 = NewsLedger(persist_path=path)
            assert l2.size() == 2
            assert l2.contains("e1")

    def test_persist_throttle(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "ledger.json")
            l = NewsLedger(persist_path=path)
            l.upsert_many([_make_event("e1", int(time.time()))])
            assert l.persist_to_disk(force=True) is True
            # 立即再次非 force 调用应被节流掉
            assert l.persist_to_disk(force=False) is False

    def test_reload_skips_invalid_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "ledger.json")
            bad = {"events": [{"invalid": True}, {"also_invalid": 1}]}
            with open(path, "w", encoding="utf-8") as f:
                json.dump(bad, f)
            l = NewsLedger(persist_path=path)
            assert l.size() == 0


class TestSingleton:
    def test_singleton_returns_same_instance(self):
        reset_ledger()
        a = get_ledger()
        b = get_ledger()
        assert a is b

    def test_reset_creates_new_instance(self):
        reset_ledger()
        a = get_ledger()
        reset_ledger()
        b = get_ledger()
        assert a is not b
