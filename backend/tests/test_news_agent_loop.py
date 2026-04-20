"""Unit tests · D13 news_agent_loop（全部 mock，零网络零 AI）"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import pytest

from models.geo_risk import GeoRiskEvent, GeoRiskOverview
from models.news_brief import NewsBrief, NewsBriefSection
from models.news_event import AssetImpact, MarketEventSignal, RawNewsItem
from processors.news_agent_loop import run_news_tick
from processors.news_ledger import NewsLedger, reset_ledger
from processors.news_brief import reset_current_brief, get_current_brief
from processors.signal_bus import SignalBus


# ── Fakes ─────────────────────────────────────────────


class FakeAnalyzer:
    available = True


class FakeNarrativeTracker:
    def __init__(self):
        self.ingested: list[MarketEventSignal] = []
        self.reactions = []

    def ingest(self, sig):
        self.ingested.append(sig)
        return None

    def record_price_reaction(self, *a, **kw):
        self.reactions.append((a, kw))

    def get_active(self, limit=20):
        return []

    def decay(self, now_ts=None):
        return 0


class FakeGeoTracker:
    def __init__(self):
        self._events_on_ingest: list[GeoRiskEvent] = []
        self.ingested_count = 0
        self._geo_events_to_return: list[GeoRiskEvent] = []

    def set_next_geo_events(self, events: list[GeoRiskEvent]):
        self._geo_events_to_return = list(events)

    def ingest(self, sig):
        self.ingested_count += 1
        if self._geo_events_to_return:
            return self._geo_events_to_return.pop(0)
        return None

    def get_all_states(self):
        return []

    def get_overview(self):
        return GeoRiskOverview(
            overall_level=0, overall_label="PEACE", overall_emoji="🟢",
            overall_summary_cn="", updated_at=int(time.time()),
        )

    def decay(self, now_ts=None):
        return 0


def _make_raw(eid: str, ts: int, title: str = "hello BTC surges") -> RawNewsItem:
    return RawNewsItem(
        source_type="okx",
        external_id=eid,
        publish_time=ts * 1000,
        fetch_time=ts,
        title=title,
        content=title * 5,
    )


def _make_sig(eid: str, ts: int, *, tier: str = "major", direction: str = "bullish",
              risk_type: str = "macro_economic", impact: int = 3) -> MarketEventSignal:
    return MarketEventSignal(
        event_id=eid,
        ts=ts,
        direction=direction,  # type: ignore[arg-type]
        tier=tier,  # type: ignore[arg-type]
        risk_type=risk_type,  # type: ignore[arg-type]
        impact_score=impact,
        confidence=0.8,
        narrative_theme="Test_Theme",
        summary_cn=f"summary-{eid}",
        impact_on_assets=[AssetImpact(asset="BTC", direction=direction, magnitude="high")],  # type: ignore[arg-type]
    )


# ── Tests ─────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _reset_singletons():
    reset_ledger()
    reset_current_brief()
    yield
    reset_ledger()
    reset_current_brief()


class TestRunNewsTickBasics:
    @pytest.mark.asyncio
    async def test_empty_fetch_does_not_fail(self):
        async def _fetch():
            return []

        ledger = NewsLedger()
        bus = SignalBus()
        stats = await run_news_tick(
            current_btc_price=72000,
            registry_fetch=_fetch,
            analyzer=FakeAnalyzer(),
            narrative_tracker=FakeNarrativeTracker(),
            geo_tracker=FakeGeoTracker(),
            ledger=ledger,
            bus=bus,
        )
        assert stats.fetched == 0
        assert stats.structured == 0
        assert stats.error == ""
        assert ledger.size() == 0

    @pytest.mark.asyncio
    async def test_full_pipeline_writes_to_ledger_and_bus(self):
        now = int(time.time())
        items = [_make_raw("e1", now), _make_raw("e2", now)]

        async def _fetch():
            return items

        def _filter(items_in):
            from processors.news_filter import FilterStats
            tier_map = {i.external_id: "major" for i in items_in}
            stats = FilterStats()
            stats.input_count = len(items_in)
            stats.kept_count = len(items_in)
            return list(items_in), tier_map, stats

        async def _structurer(items_in, tier_map, **_kw):
            return [_make_sig(i.external_id, now) for i in items_in]

        narrative = FakeNarrativeTracker()
        geo = FakeGeoTracker()
        ledger = NewsLedger()
        bus = SignalBus()

        stats = await run_news_tick(
            current_btc_price=72000,
            registry_fetch=_fetch,
            filter_fn=_filter,
            structurer=_structurer,
            analyzer=FakeAnalyzer(),
            narrative_tracker=narrative,
            geo_tracker=geo,
            ledger=ledger,
            bus=bus,
        )
        assert stats.fetched == 2
        assert stats.kept_after_filter == 2
        assert stats.structured == 2
        assert len(narrative.ingested) == 2
        assert ledger.size() == 2
        # 每条 news event 产出 1 条 news_event.ai 候选信号
        assert stats.signals_pushed_news == 2
        assert stats.error == ""

    @pytest.mark.asyncio
    async def test_analyzer_unavailable_skips_structuring(self):
        now = int(time.time())
        items = [_make_raw("e1", now)]

        async def _fetch():
            return items

        def _filter(items_in):
            from processors.news_filter import FilterStats
            return list(items_in), {"e1": "major"}, FilterStats()

        class _Unavail:
            available = False

        ledger = NewsLedger()
        bus = SignalBus()
        stats = await run_news_tick(
            current_btc_price=72000,
            registry_fetch=_fetch,
            filter_fn=_filter,
            analyzer=_Unavail(),
            narrative_tracker=FakeNarrativeTracker(),
            geo_tracker=FakeGeoTracker(),
            ledger=ledger,
            bus=bus,
        )
        assert stats.fetched == 1
        assert stats.kept_after_filter == 1
        assert stats.structured == 0
        assert ledger.size() == 0


class TestGeoAndBlackswan:
    @pytest.mark.asyncio
    async def test_geo_event_emits_signal_and_detects_blackswan(self):
        now = int(time.time())
        items = [_make_raw("e1", now)]

        async def _fetch():
            return items

        def _filter(items_in):
            from processors.news_filter import FilterStats
            return list(items_in), {"e1": "major"}, FilterStats()

        async def _structurer(items_in, tier_map, **_kw):
            return [_make_sig("e1", now, risk_type="geopolitical")]

        geo = FakeGeoTracker()
        geo.set_next_geo_events([
            GeoRiskEvent(
                event_id="g1", theme_id="Middle_East_Iran", ts=now,
                level_before=0, level_after=5, severity="escalation",
                estimated_direction="bearish", confidence=0.9,
                is_blackswan=True,
            ),
        ])

        ledger = NewsLedger()
        bus = SignalBus()

        async def _brief_fn(events, themes, overview, **_kw):
            return NewsBrief(
                version=42,
                updated_at=now,
                ts_range_start=now - 3600,
                ts_range_end=now,
                update_trigger="blackswan",
                sections=[NewsBriefSection(bullets=["b"])],
                tracked_themes=[],
            )

        stats = await run_news_tick(
            current_btc_price=72000,
            registry_fetch=_fetch,
            filter_fn=_filter,
            structurer=_structurer,
            brief_fn=_brief_fn,
            analyzer=FakeAnalyzer(),
            narrative_tracker=FakeNarrativeTracker(),
            geo_tracker=geo,
            ledger=ledger,
            bus=bus,
        )
        assert stats.blackswan_hit is True
        assert stats.signals_pushed_geo == 1
        assert stats.brief_triggered is True
        assert stats.brief_version_after == 42
        assert get_current_brief() is not None

    @pytest.mark.asyncio
    async def test_blackswan_tier_alone_triggers_brief(self):
        now = int(time.time())
        items = [_make_raw("e1", now)]

        async def _fetch():
            return items

        def _filter(items_in):
            from processors.news_filter import FilterStats
            return list(items_in), {"e1": "blackswan"}, FilterStats()

        async def _structurer(items_in, tier_map, **_kw):
            return [_make_sig("e1", now, tier="blackswan")]

        async def _brief_fn(events, themes, overview, **_kw):
            return NewsBrief(
                version=7, updated_at=now,
                ts_range_start=now - 3600, ts_range_end=now,
                update_trigger="blackswan",
                sections=[], tracked_themes=[],
            )

        stats = await run_news_tick(
            current_btc_price=72000,
            registry_fetch=_fetch,
            filter_fn=_filter,
            structurer=_structurer,
            brief_fn=_brief_fn,
            analyzer=FakeAnalyzer(),
            narrative_tracker=FakeNarrativeTracker(),
            geo_tracker=FakeGeoTracker(),
            ledger=NewsLedger(),
            bus=SignalBus(),
        )
        assert stats.blackswan_hit is True
        assert stats.brief_triggered is True


class TestBackfillBranch:
    @pytest.mark.asyncio
    async def test_backfill_invoked_when_history_available(self):
        now = int(time.time())
        items = [_make_raw("e1", now - 7200)]

        async def _fetch():
            return items

        def _filter(items_in):
            from processors.news_filter import FilterStats
            return list(items_in), {"e1": "major"}, FilterStats()

        async def _structurer(items_in, tier_map, **_kw):
            return [_make_sig("e1", now - 7200)]

        call_log = {}

        def _bf(events, price_history, now_ts, narrative_tracker=None):
            call_log["called"] = True
            call_log["n_events"] = len(events)
            return list(events), {"processed": len(events), "newly_complete": 1}

        stats = await run_news_tick(
            current_btc_price=72000,
            price_history=[{"ts": now - 7200, "price": 72000.0}, {"ts": now, "price": 72500.0}],
            do_backfill=True,
            registry_fetch=_fetch,
            filter_fn=_filter,
            structurer=_structurer,
            backfill_fn=_bf,
            analyzer=FakeAnalyzer(),
            narrative_tracker=FakeNarrativeTracker(),
            geo_tracker=FakeGeoTracker(),
            ledger=NewsLedger(),
            bus=SignalBus(),
        )
        assert call_log.get("called") is True
        assert stats.backfill_processed >= 1

    @pytest.mark.asyncio
    async def test_backfill_skipped_without_history(self):
        async def _fetch():
            return []

        called = {"bf": False}

        def _bf(*a, **kw):
            called["bf"] = True
            return [], {}

        stats = await run_news_tick(
            current_btc_price=72000,
            price_history=[],
            do_backfill=True,
            registry_fetch=_fetch,
            analyzer=FakeAnalyzer(),
            narrative_tracker=FakeNarrativeTracker(),
            geo_tracker=FakeGeoTracker(),
            ledger=NewsLedger(),
            bus=SignalBus(),
            backfill_fn=_bf,
        )
        assert called["bf"] is False
        assert stats.backfill_processed == 0


class TestResilience:
    @pytest.mark.asyncio
    async def test_fetch_exception_does_not_crash(self):
        async def _fetch():
            raise RuntimeError("boom")

        stats = await run_news_tick(
            current_btc_price=72000,
            registry_fetch=_fetch,
            analyzer=FakeAnalyzer(),
            narrative_tracker=FakeNarrativeTracker(),
            geo_tracker=FakeGeoTracker(),
            ledger=NewsLedger(),
            bus=SignalBus(),
        )
        assert stats.fetched == 0
        assert "fetch_error" in stats.extra
        assert stats.error == ""  # 外层没再抛

    @pytest.mark.asyncio
    async def test_structurer_exception_does_not_crash(self):
        now = int(time.time())

        async def _fetch():
            return [_make_raw("e1", now)]

        def _filter(items_in):
            from processors.news_filter import FilterStats
            return list(items_in), {"e1": "major"}, FilterStats()

        async def _bad_struct(*a, **kw):
            raise RuntimeError("ai exploded")

        stats = await run_news_tick(
            current_btc_price=72000,
            registry_fetch=_fetch,
            filter_fn=_filter,
            structurer=_bad_struct,
            analyzer=FakeAnalyzer(),
            narrative_tracker=FakeNarrativeTracker(),
            geo_tracker=FakeGeoTracker(),
            ledger=NewsLedger(),
            bus=SignalBus(),
        )
        assert stats.structured == 0
        assert "structurer_error" in stats.extra
