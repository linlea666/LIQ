from __future__ import annotations

import pytest

from storage.onchain_entity_store import OnchainEntityStore


def test_internal_rebalance_dedup_known_at_and_reorg(tmp_path) -> None:
    store = OnchainEntityStore(str(tmp_path))
    store.register_label(
        entity_id="exchange_x", entity_label="Exchange X", address="a",
        label_source="fixture", confidence=0.9, valid_from=1, known_at=100,
    )
    store.register_label(
        entity_id="exchange_x", entity_label="Exchange X", address="b",
        label_source="fixture", confidence=0.9, valid_from=1, known_at=100,
    )
    event = store.ingest_transfer(
        tx_id="tx", output_index=0, from_address="a", to_address="b",
        amount_base=100, event_time=120, observed_at=130, decision_time=130,
        confirmations=3, source_id="bitcoin_node",
    )
    assert event.event_type == "internal_rebalance"
    duplicate = store.ingest_transfer(
        tx_id="tx", output_index=0, from_address="a", to_address="b",
        amount_base=100, event_time=120, observed_at=140, decision_time=140,
        confirmations=4, source_id="bitcoin_node",
    )
    assert duplicate.event_id == event.event_id
    assert len(store.recent_events(decision_time=200, since=0)) == 1
    assert store.mark_reorg(event.event_id, decision_time=150) is True
    assert store.mark_reorg(event.event_id, decision_time=160) is False
    assert store.recent_events(decision_time=200, since=0) == []
    store.close()


def test_label_added_later_cannot_backfill_old_decision(tmp_path) -> None:
    store = OnchainEntityStore(str(tmp_path))
    store.register_label(
        entity_id="fund", entity_label="Fund", address="known_later",
        label_source="fixture", confidence=0.8, valid_from=1, known_at=200,
    )
    event = store.ingest_transfer(
        tx_id="old", output_index=0, from_address="unknown", to_address="known_later",
        amount_base=10, event_time=100, observed_at=120, decision_time=120,
        confirmations=6, source_id="bitcoin_node",
    )
    assert event.event_type == "unknown_counterparty"
    assert event.entity_id == "unknown"
    store.close()


def test_onchain_rejects_future_observation(tmp_path) -> None:
    store = OnchainEntityStore(str(tmp_path))
    try:
        with pytest.raises(ValueError, match="invalid PIT"):
            store.ingest_transfer(
                tx_id="future", output_index=0, from_address="a", to_address="b",
                amount_base=1, event_time=100, observed_at=120, decision_time=110,
                confirmations=1, source_id="fixture",
            )
    finally:
        store.close()
