"""Point-in-time replay primitives for the market-risk engine."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable, Optional

from models.market_risk import MarketIncidentSnapshot


@dataclass(frozen=True)
class PITEventEnvelope:
    source_id: str
    event_time: int
    observed_at: int
    decision_time: int
    watermark: int
    source_sequence: str
    payload: dict[str, Any]


def validate_snapshot_pit(snapshot: MarketIncidentSnapshot) -> list[str]:
    """返回所有未来数据/时钟逆序违规；空列表才可计入回测。"""
    violations: list[str] = []
    if snapshot.observed_at > snapshot.decision_time:
        violations.append("snapshot_observed_after_decision")
    if snapshot.watermark > snapshot.decision_time:
        violations.append("snapshot_watermark_after_decision")
    for item in snapshot.evidence:
        prefix = item.evidence_id
        if item.event_time > item.observed_at:
            violations.append(f"{prefix}:event_after_observed")
        if item.observed_at > item.decision_time:
            violations.append(f"{prefix}:observed_after_decision")
        if item.decision_time > snapshot.decision_time:
            violations.append(f"{prefix}:evidence_decided_after_snapshot")
        if item.watermark > item.decision_time:
            violations.append(f"{prefix}:watermark_after_decision")
    for source_id, quality in snapshot.source_quality.items():
        if quality.as_of > snapshot.decision_time:
            violations.append(f"{source_id}:as_of_after_decision")
        if quality.observed_at > snapshot.decision_time:
            violations.append(f"{source_id}:quality_observed_after_decision")
        if quality.watermark > snapshot.decision_time:
            violations.append(f"{source_id}:quality_watermark_after_decision")

    def _walk_known_at(value: Any, path: str) -> None:
        if isinstance(value, dict):
            known_at = value.get("known_at")
            if known_at is not None:
                try:
                    if int(known_at) > snapshot.decision_time:
                        violations.append(f"{path}:known_at_after_decision")
                except (TypeError, ValueError):
                    violations.append(f"{path}:invalid_known_at")
            for key, child in value.items():
                _walk_known_at(child, f"{path}.{key}")
        elif isinstance(value, list):
            for index, child in enumerate(value):
                _walk_known_at(child, f"{path}[{index}]")

    _walk_known_at(snapshot.context, "context")
    return violations


def point_in_time_replay(
    events: Iterable[PITEventEnvelope],
    apply_event: Callable[[PITEventEnvelope], Optional[Any]],
) -> list[Any]:
    """严格按 decision_time 回放；迟到数据只会在后续 decision_time 追加。"""
    ordered = sorted(
        events,
        key=lambda item: (
            item.decision_time, item.observed_at, item.source_id, item.source_sequence,
        ),
    )
    seen: set[tuple[str, str, int]] = set()
    outputs: list[Any] = []
    previous_decision = -1
    for event in ordered:
        if event.event_time > event.observed_at:
            raise ValueError("event_time cannot be after observed_at")
        if event.observed_at > event.decision_time:
            raise ValueError("observed_at cannot be after decision_time")
        if event.watermark > event.decision_time:
            raise ValueError("watermark cannot be after decision_time")
        if event.decision_time < previous_decision:
            raise ValueError("decision_time regression")
        identity = (event.source_id, event.source_sequence, event.event_time)
        if identity in seen:
            continue
        seen.add(identity)
        result = apply_event(event)
        if result is not None:
            outputs.append(result)
        previous_decision = event.decision_time
    return outputs
