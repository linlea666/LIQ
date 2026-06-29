from __future__ import annotations

import time

import pytest

from models.spot_accumulation import EvidenceScore, SpotLedgerEvent, SpotOpportunity
from processors.spot_accumulation_service import SpotAccumulationService
from storage.spot_accumulation_store import SpotIdempotencyConflict, SpotStorageCorruption


def _service(tmp_path) -> SpotAccumulationService:
    return SpotAccumulationService(str(tmp_path), lambda: None)


def _accepted_opportunity(now: int) -> SpotOpportunity:
    return SpotOpportunity(
        opportunity_id="recover-op",
        stage="insurance",
        bucket="core",
        allocation_usdt=1_000,
        reserved_usdt=1_000,
        status="accepted",
        price_zone_low=49_000,
        price_zone_high=51_000,
        trigger_price=50_000,
        scores=EvidenceScore(valuation=80, capital_flow=70, acceptance=75),
        created_at=now,
        updated_at=now,
    )


def test_reversal_that_would_break_later_sell_is_rejected_before_append(tmp_path):
    service = _service(tmp_path)
    now = int(time.time())
    buy = service.record_fill({
        "client_event_id": "buy",
        "side": "buy",
        "bucket": "core",
        "quantity_btc": 0.02,
        "price_usdt": 50_000,
        "fee_usdt": 0,
        "executed_at": now - 2,
    })
    service.record_fill({
        "client_event_id": "sell",
        "side": "sell",
        "bucket": "core",
        "quantity_btc": 0.02,
        "price_usdt": 55_000,
        "fee_usdt": 0,
        "executed_at": now - 1,
    })
    before = service.store.ledger_path.read_bytes()

    with pytest.raises(ValueError, match="oversell"):
        service.reverse_fill(buy.event_id, "reverse-buy")

    assert service.store.ledger_path.read_bytes() == before
    assert len(service.get_events()) == 2


def test_invalid_numbers_fees_and_future_time_never_reach_ledger(tmp_path):
    service = _service(tmp_path)
    base = {
        "side": "buy",
        "bucket": "core",
        "quantity_btc": 0.01,
        "price_usdt": 50_000,
        "fee_usdt": 0,
    }
    with pytest.raises(ValueError):
        service.record_fill({**base, "client_event_id": "nan", "price_usdt": float("nan")})
    with pytest.raises(ValueError):
        service.record_fill({**base, "client_event_id": "inf", "quantity_btc": float("inf")})
    with pytest.raises(ValueError, match="5分钟"):
        service.record_fill({
            **base,
            "client_event_id": "future",
            "executed_at": int(time.time()) + 301,
        })

    buy = service.record_fill({**base, "client_event_id": "fee-buy"})
    assert buy.event_type == "fill"
    with pytest.raises(ValueError, match="手续费"):
        service.record_fill({
            "client_event_id": "bad-sell-fee",
            "side": "sell",
            "bucket": "core",
            "quantity_btc": 0.01,
            "price_usdt": 50_000,
            "fee_usdt": 501,
        })
    assert len(service.get_events()) == 1


def test_same_second_events_use_sequence_and_idempotency_conflicts_fail(tmp_path):
    service = _service(tmp_path)
    ts = int(time.time())
    payload = {
        "client_event_id": "same-buy",
        "side": "buy",
        "bucket": "core",
        "quantity_btc": 0.01,
        "price_usdt": 50_000,
        "fee_usdt": 0,
        "executed_at": ts,
    }
    first = service.record_fill(payload)
    retry = service.record_fill(payload)
    assert retry.event_id == first.event_id

    service.record_fill({
        "client_event_id": "same-sell",
        "side": "sell",
        "bucket": "core",
        "quantity_btc": 0.01,
        "price_usdt": 50_000,
        "fee_usdt": 0,
        "executed_at": ts,
    })
    events = service.get_events()
    assert [event.sequence for event in events] == [1, 2]
    assert service.store.build_portfolio(service.config).total_btc == 0

    with pytest.raises(SpotIdempotencyConflict):
        service.record_fill({**payload, "quantity_btc": 0.02})
    assert len(service.get_events()) == 2


def test_state_file_can_be_deleted_and_rebuilt_from_journal_and_ledger(tmp_path):
    service = _service(tmp_path)
    now = int(time.time())
    opportunity = _accepted_opportunity(now)
    service.runtime.opportunities[opportunity.opportunity_id] = opportunity
    service._journal_runtime("decision", "test accepted")
    service.store.save_state(service.runtime)
    service.record_fill({
        "client_event_id": "linked-fill",
        "side": "buy",
        "bucket": "core",
        "quantity_btc": 0.01,
        "price_usdt": 50_000,
        "fee_usdt": 0,
        "opportunity_id": opportunity.opportunity_id,
    })
    service.store.state_path.unlink()

    restarted = _service(tmp_path)
    restored = restarted.runtime.opportunities[opportunity.opportunity_id]
    assert restarted.recovery_required is False
    assert restarted.store.build_portfolio(restarted.config).total_btc == 0.01
    assert restarted.runtime.last_filled_price == 50_000
    assert restored.filled_usdt == 500
    assert restored.reserved_usdt == 500
    assert restored.status == "accepted"


def test_crash_after_ledger_append_is_reconciled_on_restart(tmp_path):
    service = _service(tmp_path)
    now = int(time.time())
    opportunity = _accepted_opportunity(now)
    service.runtime.opportunities[opportunity.opportunity_id] = opportunity
    service._journal_runtime("decision", "before simulated crash")
    service.store.save_state(service.runtime)
    event = SpotLedgerEvent(
        event_id="crash-fill",
        client_event_id="crash-fill-client",
        side="buy",
        bucket="core",
        quantity_btc=0.01,
        price_usdt=50_000,
        fee_usdt=0,
        executed_at=now,
        created_at=now,
        opportunity_id=opportunity.opportunity_id,
        opportunity_stage=opportunity.stage,
        opportunity_allocation_usdt=opportunity.allocation_usdt,
    )
    service.store.commit_event(event, service.config)
    service.store.state_path.unlink()

    restarted = _service(tmp_path)
    restored = restarted.runtime.opportunities[opportunity.opportunity_id]
    assert restored.filled_usdt == 500
    assert restored.reserved_usdt == 500
    assert restarted.runtime.last_filled_price == 50_000


def test_corrupt_state_cache_is_rebuilt_from_journal(tmp_path):
    service = _service(tmp_path)
    service.runtime.tail_mode = "extreme"
    service._journal_runtime("decision", "persist tail mode")
    service.store.save_state(service.runtime)
    service.store.state_path.write_text("{broken", encoding="utf-8")

    restarted = _service(tmp_path)
    assert restarted.recovery_required is False
    assert restarted.runtime.tail_mode == "extreme"
    assert restarted.store.load_state().tail_mode == "extreme"


def test_corrupt_ledger_fails_closed_instead_of_skipping_line(tmp_path):
    service = _service(tmp_path)
    service.store.ledger_path.write_text("{not-json}\n", encoding="utf-8")

    restarted = _service(tmp_path)
    assert restarted.recovery_required is True
    assert restarted.get_snapshot() is None
    with pytest.raises(SpotStorageCorruption, match="第1行损坏"):
        restarted.record_fill({
            "client_event_id": "blocked",
            "side": "buy",
            "bucket": "core",
            "quantity_btc": 0.01,
            "price_usdt": 50_000,
        })


def test_partial_fill_invalidated_by_market_stays_invalidated_after_restart(tmp_path):
    service = _service(tmp_path)
    now = int(time.time())
    opportunity = _accepted_opportunity(now)
    service.runtime.opportunities[opportunity.opportunity_id] = opportunity
    service._journal_runtime("decision", "accepted")
    service.record_fill({
        "client_event_id": "partial-before-invalidation",
        "side": "buy",
        "bucket": "core",
        "quantity_btc": 0.01,
        "price_usdt": 50_000,
        "opportunity_id": opportunity.opportunity_id,
    })
    current = service.runtime.opportunities[opportunity.opportunity_id]
    current.status = "invalidated"
    current.reserved_usdt = 0
    service._journal_runtime("market", "conditions deteriorated")
    service.store.save_state(service.runtime)

    restarted = _service(tmp_path)
    restored = restarted.runtime.opportunities[opportunity.opportunity_id]
    assert restored.filled_usdt == 500
    assert restored.status == "invalidated"
    assert restored.reserved_usdt == 0


def test_second_reversal_with_different_key_is_rejected(tmp_path):
    service = _service(tmp_path)
    fill = service.record_fill({
        "client_event_id": "buy-once",
        "side": "buy",
        "bucket": "core",
        "quantity_btc": 0.01,
        "price_usdt": 50_000,
    })
    first = service.reverse_fill(fill.event_id, "reverse-once")
    assert service.reverse_fill(fill.event_id, "reverse-once").event_id == first.event_id
    with pytest.raises(SpotIdempotencyConflict, match="已被冲正"):
        service.reverse_fill(fill.event_id, "reverse-again")
    assert len(service.get_events()) == 2


def test_v1_config_is_backed_up_once_and_persisted_as_v3(tmp_path):
    root = tmp_path / "spot_accumulation"
    root.mkdir()
    (root / "config.json").write_text(
        '{"version":1,"initial_capital_usdt":20000,"core_budget_usdt":13000,'
        '"swing_budget_usdt":4000,"tail_budget_usdt":3000}',
        encoding="utf-8",
    )
    service = _service(tmp_path)
    assert service.config.schema_version == 3
    assert (root / "migration_backup_v2" / "config.json").exists()
    assert '"schema_version": 3' in (root / "config.json").read_text(encoding="utf-8")
