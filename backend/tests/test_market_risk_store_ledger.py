from __future__ import annotations

from models.market_risk import EvidenceItem, MarketIncidentSnapshot, MarketRiskMachineContext
from storage.market_risk_store import MarketRiskStore


def _evidence(decision_time: int, confidence: float = 0.8) -> EvidenceItem:
    return EvidenceItem(
        evidence_id="ev_same", coin="BTC", pillar="spot_demand",
        causal_root="spot_aggressor_flow", name="spot_flow", direction="up",
        strength=0.9, confidence=confidence, event_time=100, observed_at=decision_time,
        decision_time=decision_time, watermark=100, source_id="fixture",
        config_version="cfg", calibration_version="cal", values={"imbalance": 0.9},
    )


def _snapshot(decision_time: int, confidence: float = 0.8) -> MarketIncidentSnapshot:
    return MarketIncidentSnapshot(
        coin="BTC", event_time=100, observed_at=decision_time,
        decision_time=decision_time, watermark=100, stage="watch",
        direction="up", incident_id="inc", episode_id="ep",
        evidence=[_evidence(decision_time, confidence)],
        config_version="cfg", calibration_version="cal",
    )


def test_ledger_dedupes_ticks_and_appends_confidence_revision(tmp_path) -> None:
    store = MarketRiskStore(str(tmp_path))
    context = MarketRiskMachineContext(
        coin="BTC", stage="watch", direction="up", incident_id="inc",
        episode_id="ep", incident_started_at=100, episode_started_at=100,
    )
    store.commit_evaluation(_snapshot(110), context)
    store.commit_evaluation(_snapshot(120), context)
    count = store._conn.execute("SELECT COUNT(*) FROM evidence_ledger").fetchone()[0]
    assert count == 1

    store.commit_evaluation(_snapshot(130, confidence=0.7), context)
    rows = store._conn.execute(
        "SELECT ledger_event_type FROM evidence_ledger ORDER BY decision_time"
    ).fetchall()
    assert [row[0] for row in rows] == ["evidence_added", "confidence_revised"]
    store.close()


def test_repeated_transition_email_dedup_key_enqueues_once(tmp_path) -> None:
    store = MarketRiskStore(str(tmp_path))
    context = MarketRiskMachineContext(
        coin="BTC", stage="warning", direction="up", incident_id="inc",
        episode_id="ep", incident_started_at=100, episode_started_at=100,
    )
    email = ("market-risk:ep:warning", "subject", "<p>risk</p>")
    store.commit_evaluation(_snapshot(110), context, email=email)
    store.commit_evaluation(_snapshot(120), context, email=email)
    count = store._conn.execute(
        "SELECT COUNT(*) FROM market_risk_email_outbox"
    ).fetchone()[0]
    assert count == 1
    store.close()


def test_context_fact_keeps_first_observation_and_appends_revision(tmp_path) -> None:
    store = MarketRiskStore(str(tmp_path))
    first = store.record_context_observation(
        "etf", "2026-08-19", 100, {"net": 10}, 200,
    )
    same = store.record_context_observation(
        "etf", "2026-08-19", 100, {"net": 10}, 220,
    )
    revised = store.record_context_observation(
        "etf", "2026-08-19", 100, {"net": 12}, 240,
    )
    assert first == {"version": 1, "first_observed_at": 200, "revised": False}
    assert same == first
    assert revised == {"version": 2, "first_observed_at": 200, "revised": True}
    assert store._conn.execute("SELECT COUNT(*) FROM context_fact_versions").fetchone()[0] == 2
    store.close()


def test_history_desc_cursor_returns_latest_page_without_changing_legacy_asc(tmp_path) -> None:
    store = MarketRiskStore(str(tmp_path))
    context = MarketRiskMachineContext(coin="BTC")
    for decision_time in (110, 120, 130):
        store.commit_evaluation(_snapshot(decision_time), context)
    assert [item["decision_time"] for item in store.history("BTC", limit=2)] == [110, 120]
    latest = store.history("BTC", limit=2, order="desc")
    assert [item["decision_time"] for item in latest] == [130, 120]
    older = store.history("BTC", limit=2, order="desc", cursor=120)
    assert [item["decision_time"] for item in older] == [110]
    store.close()


def test_readiness_uses_indexed_governance_columns_and_excludes_legacy_rows(tmp_path) -> None:
    store = MarketRiskStore(str(tmp_path))
    context = MarketRiskMachineContext(coin="BTC")
    store._conn.execute(
        """INSERT INTO snapshots(coin,decision_time,stage,payload)
           VALUES('BTC',90,'normal','{}')"""
    )
    store.commit_evaluation(_snapshot(110), context)
    invalid = _snapshot(120).model_copy(update={
        "quality_layer": "data_degraded",
        "valid_for_calibration": False,
        "pit_violations": ["future_source"],
    })
    store.commit_evaluation(invalid, context)
    stats = store.readiness_stats(0)
    assert stats == {
        "snapshot_count": 2,
        "pit_violations": 1,
        "valid_for_calibration": 1,
        "core_coverage": 0.5,
        "first_governed_at": 110,
    }
    store.close()


def test_governance_epoch_cannot_be_revived_by_restart(tmp_path) -> None:
    store = MarketRiskStore(str(tmp_path))
    epoch = store.ensure_governance_epoch(
        "market_risk:BTC", "identity-a", 100,
        {"raw_dropped_baseline": 3},
    )
    assert epoch["started_at"] == 100
    assert store.close_governance_epoch(
        "market_risk:BTC", "raw_event_queue_overflow", 150,
    )
    store.close()

    reopened = MarketRiskStore(str(tmp_path))
    status = reopened.governance_status("market_risk:BTC", 0)
    assert status["open"] is False
    assert status["last_reset_reason"] == "raw_event_queue_overflow"
    new_epoch = reopened.ensure_governance_epoch(
        "market_risk:BTC", "identity-a", 200,
        {"raw_dropped_baseline": 4},
    )
    assert new_epoch["started_at"] == 200
    assert new_epoch["payload"]["raw_dropped_baseline"] == 4
    reopened.close()


def test_governance_identity_change_resets_continuous_age(tmp_path) -> None:
    store = MarketRiskStore(str(tmp_path))
    store.ensure_governance_epoch("market_risk:BTC", "identity-a", 100)
    changed = store.ensure_governance_epoch("market_risk:BTC", "identity-b", 300)
    assert changed["started_at"] == 300
    status = store.governance_status("market_risk:BTC", 0)
    assert status["last_reset_reason"] == "identity_changed"
    assert status["hard_violations"] == 0
    store.close()
