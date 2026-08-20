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
