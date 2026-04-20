"""L3 SignalBus + KeyLevelSignal adapter 单测（D02 上游）"""

from __future__ import annotations

import time

import pytest

from models.candidate_signal import CandidateSignal
from models.key_level import KeyLevelSignal, KeyLevelV2
from processors.signal_bus import (
    SignalBus, adapt_key_level_signal, get_bus, reset_bus_for_tests,
)


@pytest.fixture(autouse=True)
def _reset_bus():
    reset_bus_for_tests()
    yield
    reset_bus_for_tests()


def _mk(**kw):
    defaults = dict(
        source="tracker_v2.bounced",
        source_id="BTC:72000:bounced",
        ts=int(time.time()),
        action="long",
        direction="bullish",
        anchor_price=72000,
        confidence="A",
        score=72.0,
        provenance={"coin": "BTC"},
    )
    defaults.update(kw)
    return CandidateSignal(**defaults)


class TestSignalBusBasic:
    def test_ingest_and_query(self):
        bus = SignalBus()
        assert bus.ingest(_mk()) is True
        items = bus.query("BTC")
        assert len(items) == 1
        assert items[0].source == "tracker_v2.bounced"

    def test_idempotent_dedupe(self):
        bus = SignalBus()
        sig = _mk()
        assert bus.ingest(sig) is True
        assert bus.ingest(sig) is False  # 同 key 幂等
        assert bus.stats()["dup_dropped_total"] == 1

    def test_missing_coin_drops(self):
        bus = SignalBus()
        sig = _mk(provenance={})  # 无 coin
        assert bus.ingest(sig) is False

    def test_query_filter_by_source(self):
        bus = SignalBus()
        bus.ingest(_mk(source="tracker_v2.bounced", source_id="a", ts=1000))
        bus.ingest(_mk(source="range_signal.observe", source_id="b", ts=1100))
        items = bus.query("BTC", sources=["tracker_v2."])
        assert len(items) == 1
        assert items[0].source == "tracker_v2.bounced"

    def test_prune_expired(self):
        bus = SignalBus(max_age_sec=3600)
        old_sig = _mk(source_id="old", ts=int(time.time()) - 7200)  # 2h ago
        fresh_sig = _mk(source_id="fresh", ts=int(time.time()))
        bus.ingest(old_sig)
        bus.ingest(fresh_sig)
        removed = bus.prune_expired()
        assert removed >= 1
        remaining = bus.query("BTC", min_ts=0)
        assert all(s.source_id != "old" for s in remaining)

    def test_bucket_cap_drops_oldest(self):
        bus = SignalBus(max_per_coin=3)
        for i in range(5):
            bus.ingest(_mk(source_id=f"id_{i}", ts=1000 + i))
        items = bus.query("BTC")
        ids = [s.source_id for s in items]
        assert len(ids) == 3
        # 应保留最新 3 条（id_2, id_3, id_4）
        assert "id_0" not in ids and "id_1" not in ids

    def test_singleton(self):
        b1 = get_bus()
        b2 = get_bus()
        assert b1 is b2


class TestAdaptKeyLevelSignal:
    def test_snipe_long_mapping(self):
        kl_sig = KeyLevelSignal(
            level_price=72000, side="support", state="swept", action="snipe_long",
            confidence="A", entry_price=72200, stop_loss=71400, tp1=73500, rr_ratio=2.5,
            reason="扫穿反抽",
        )
        cs = adapt_key_level_signal("BTC", kl_sig)
        assert cs.source == "tracker_v2.swept"
        assert cs.action == "long"
        assert cs.direction == "bullish"
        assert cs.confidence == "A"
        assert cs.score == 72.0  # A-tier raw score
        assert cs.anchor_price == 72000
        assert cs.entry_price == 72200
        assert cs.stop_loss == 71400
        assert cs.rr_ratio == 2.5
        assert cs.provenance.get("coin") == "BTC"

    def test_flip_short_mapping(self):
        kl_sig = KeyLevelSignal(
            level_price=75000, side="resistance", state="flip_broken",
            action="flip_short", confidence="B",
        )
        cs = adapt_key_level_signal("BTC", kl_sig)
        assert cs.action == "short"
        assert cs.direction == "bearish"

    def test_wait_mapping(self):
        kl_sig = KeyLevelSignal(
            level_price=72000, side="support", state="idle",
            action="wait_sweep", confidence="C",
        )
        cs = adapt_key_level_signal("BTC", kl_sig)
        assert cs.action == "wait"
        assert cs.direction == "neutral"

    def test_level_provenance_merged(self):
        kl_sig = KeyLevelSignal(
            level_price=72000, side="support", state="bounced",
            action="snipe_long", confidence="A",
        )
        kl_lv = KeyLevelV2(
            price=72000, side="support",
            confluence_score=80, strength_tier="A",
            cascade_risk=0.3, final_score=82, timeframe="4H",
        )
        cs = adapt_key_level_signal("BTC", kl_sig, kl_lv)
        assert cs.provenance["cascade_risk"] == 0.3
        assert cs.provenance["strength_tier"] == "A"
        assert cs.provenance["timeframe"] == "4H"
        assert cs.provenance["final_score"] == 82.0

    def test_expires_at_is_set(self):
        kl_sig = KeyLevelSignal(
            level_price=72000, side="support", state="swept",
            action="snipe_long", confidence="A",
        )
        cs = adapt_key_level_signal("BTC", kl_sig)
        assert cs.expires_at is not None
        assert cs.expires_at - cs.ts == 3600
