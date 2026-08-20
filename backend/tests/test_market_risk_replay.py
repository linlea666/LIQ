from __future__ import annotations

import pytest

from models.market_risk import EvidenceItem, MarketIncidentSnapshot, SourceQuality
from processors.market_risk_replay import PITEventEnvelope, point_in_time_replay, validate_snapshot_pit


def test_pit_replay_orders_by_decision_time_and_dedupes_source_sequence() -> None:
    events = [
        PITEventEnvelope("spot", 10, 20, 30, 20, "2", {"v": 2}),
        PITEventEnvelope("spot", 5, 10, 15, 10, "1", {"v": 1}),
        PITEventEnvelope("spot", 5, 12, 16, 12, "1", {"v": 1}),
    ]
    output = point_in_time_replay(events, lambda event: event.payload["v"])
    assert output == [1, 2]


def test_pit_replay_rejects_future_observation() -> None:
    with pytest.raises(ValueError, match="observed_at"):
        point_in_time_replay([
            PITEventEnvelope("spot", 10, 30, 20, 10, "1", {}),
        ], lambda event: event)


def test_snapshot_validator_blocks_late_known_at_backfill() -> None:
    quality = SourceQuality(
        source_id="spot", availability="available", freshness="fresh",
        completeness=1, continuity="continuous", validity="valid",
        as_of=90, observed_at=95, watermark=90, decision_usable=True,
    )
    evidence = EvidenceItem(
        evidence_id="ev", coin="BTC", pillar="spot_demand", causal_root="spot",
        name="spot", direction="up", event_time=80, observed_at=95,
        decision_time=100, watermark=90, source_id="spot",
        config_version="cfg", calibration_version="cal",
    )
    snapshot = MarketIncidentSnapshot(
        coin="BTC", event_time=80, observed_at=100, decision_time=100,
        watermark=90, evidence=[evidence], source_quality={"spot": quality},
        context={"etf": {"known_at": 101}},
        config_version="cfg", calibration_version="cal",
    )
    assert validate_snapshot_pit(snapshot) == ["context.etf:known_at_after_decision"]
