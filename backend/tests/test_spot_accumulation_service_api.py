from __future__ import annotations

import copy
import time
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ai.spot_accumulation_explainer import SpotAccumulationExplainer
from api.routes_spot_accumulation import router, set_components
from models.flow import CVDData, CVDPoint
from models.key_level import BehaviorEval, KeyLevelSnapshotV2, KeyLevelV2
from models.spot_accumulation import EvidenceScore, SpotOpportunity
from processors.spot_accumulation_service import SpotAccumulationService


def _state(now: int):
    daily = [
        SimpleNamespace(
            ts=(now - (29 - i) * 86_400) * 1000,
            high=126_000 if i == 0 else 55_000,
            low=45_000,
            close=50_000,
        )
        for i in range(30)
    ]
    weekly = [
        SimpleNamespace(
            ts=(now - (22 - i) * 7 * 86_400) * 1000,
            high=126_000,
            low=40_000,
            close=50_000 + i * 100,
        )
        for i in range(23)
    ]
    cvd_points = [
        CVDPoint(ts=(now - (11 - i) * 300) * 1000, buy_vol=10, sell_vol=5, delta=5, cvd=100 + i * 5)
        for i in range(12)
    ]
    candle_ts = [(now - 3600) * 1000, now * 1000]
    return SimpleNamespace(
        ticker=SimpleNamespace(last=50_000, ts=now * 1000),
        candles_daily=daily,
        candles_weekly=weekly,
        cycle_position=SimpleNamespace(
            ts=now,
            ahr999_value=0.5,
            sma_200w=48_000,
            sth_cost_1d=55_000,
        ),
        market_index=SimpleNamespace(ts=now, btc_mvrv=0.9, ahr999=0.5),
        stablecoin_mcap=SimpleNamespace(ts=now, history=[
            SimpleNamespace(total_mcap=100_000_000_000),
            SimpleNamespace(total_mcap=101_000_000_000),
        ]),
        coinbase_premium=SimpleNamespace(ts=now, current_premium=0.001),
        cvd_spot=CVDData(
            coin="BTC", inst_type="SPOT", series=cvd_points,
            delta_1h=80_000_000, trend_1h="rising",
        ),
        taker_spot_series=[
            {"ts": now - (11 - i) * 300, "delta_usd": 8_000_000}
            for i in range(12)
        ],
        candle_prices=[50_500, 50_000],
        candle_ts=candle_ts,
        candles_1h=[],
        footprint_contract=[],
        footprint_spot=[],
        footprint_last_ts=now,
        footprint_spot_last_ts=now,
        orderbook_pressure_snapshot=SimpleNamespace(ts_sec=now, walls_below=[]),
        key_level_snapshot_v2=SimpleNamespace(ts=now, levels=[]),
    )


def _service(tmp_path) -> SpotAccumulationService:
    now = int(time.time())
    state = _state(now)
    service = SpotAccumulationService(str(tmp_path), lambda: state)
    service.long_term = {
        "spot_netflow": {"net_flow_usd_24h": "300000000"},
        "etf_flow": [
            {"timestamp": now - (4 - i) * 86_400, "flow_usd": 100_000_000}
            for i in range(5)
        ],
        "nupl": [{"net_unpnl": 0.05}],
        "reserve_risk": [{"reserve_risk_index": 0.001}],
        "puell": [{"puell_multiple": 0.6}],
        "sth_sopr": [{"sth_sopr": 0.97}],
        "timestamps": {
            "spot_netflow": now,
            "etf_flow": now,
            "nupl": now,
            "reserve_risk": now,
            "puell": now,
            "sth_sopr": now,
        },
    }
    return service


def test_service_builds_isolated_snapshot_and_persists_ath(tmp_path):
    service = _service(tmp_path)
    snapshot = service.evaluate()
    assert snapshot is not None
    assert snapshot.facts.cycle_ath == 126_000
    assert snapshot.facts.drawdown_pct > 60
    assert snapshot.facts.data_quality.can_open_new_opportunity is True
    assert snapshot.facts.data_quality.layer_quality["valuation"].passed is True
    assert snapshot.facts.data_quality.completeness < 1
    assert snapshot.portfolio.total_cash_usdt == 20_000
    reloaded = SpotAccumulationService(str(tmp_path), service._state_getter)
    assert reloaded.runtime.cycle_ath == 126_000


def test_configured_total_capital_drives_api_budget_and_opportunity_amounts(tmp_path):
    service = _service(tmp_path)
    service.evaluate()
    set_components(service, SpotAccumulationExplainer())
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    preview = client.post(
        "/api/spot-accumulation/config/preview",
        json={"expected_policy_version": 1, "initial_capital_usdt": 30_000},
    ).json()
    response = client.patch("/api/spot-accumulation/config", json={
        "expected_policy_version": 1,
        "preview_hash": preview["preview_hash"],
        "initial_capital_usdt": 30_000,
    })
    assert response.status_code == 200
    config = response.json()
    assert config["core_budget_usdt"] == 19_500
    assert config["swing_budget_usdt"] == 6_000
    assert config["tail_budget_usdt"] == 4_500
    assert config["max_swing_loss_usdt"] == 300
    assert config["policy_version"] == 2

    snapshot = client.get("/api/spot-accumulation/BTC/snapshot").json()
    assert snapshot["portfolio"]["initial_capital_usdt"] == 30_000
    insurance = next(
        item for item in snapshot["opportunities"]
        if item["stage"] == "insurance" and item["policy_version"] == 2
        and item["status"] != "invalidated"
    )
    assert insurance["allocation_usdt"] == 1_500


def test_config_rejects_capital_below_existing_manual_spend(tmp_path):
    service = _service(tmp_path)
    service.evaluate()
    service.record_fill({
        "client_event_id": "manual-existing",
        "side": "buy",
        "bucket": "core",
        "quantity_btc": 0.02,
        "price_usdt": 50_000,
        "fee_usdt": 0,
    })
    set_components(service, SpotAccumulationExplainer())
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    response = client.post(
        "/api/spot-accumulation/config/preview",
        json={"expected_policy_version": 1, "initial_capital_usdt": 1_000},
    )
    assert response.status_code == 200
    assert response.json()["errors"]
    assert service.config.initial_capital_usdt == 20_000


def test_rest_fill_is_idempotent_and_reversible(tmp_path):
    service = _service(tmp_path)
    service.evaluate()
    set_components(service, SpotAccumulationExplainer())
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    assert client.get("/api/spot-accumulation/BTC/snapshot").status_code == 200
    payload = {
        "client_event_id": "client-fill-1",
        "side": "buy",
        "bucket": "core",
        "quantity_btc": 0.02,
        "price_usdt": 50_000,
        "fee_usdt": 1,
    }
    first = client.post("/api/spot-accumulation/BTC/fills", json=payload)
    second = client.post("/api/spot-accumulation/BTC/fills", json=payload)
    assert first.status_code == second.status_code == 200
    assert first.json()["event_id"] == second.json()["event_id"]

    ledger = client.get("/api/spot-accumulation/BTC/ledger").json()
    assert ledger["portfolio"]["total_btc"] == 0.02
    event_id = first.json()["event_id"]
    reversed_response = client.post(
        f"/api/spot-accumulation/BTC/fills/{event_id}/reverse",
        json={"client_event_id": "reverse-1", "note": "test"},
    )
    assert reversed_response.status_code == 200
    ledger = client.get("/api/spot-accumulation/BTC/ledger").json()
    assert ledger["portfolio"]["total_btc"] == 0


def test_rest_idempotency_payload_conflict_returns_409(tmp_path):
    service = _service(tmp_path)
    service.evaluate()
    set_components(service, SpotAccumulationExplainer())
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    payload = {
        "client_event_id": "same-key",
        "side": "buy",
        "bucket": "core",
        "quantity_btc": 0.01,
        "price_usdt": 50_000,
    }
    assert client.post("/api/spot-accumulation/BTC/fills", json=payload).status_code == 200
    conflict = client.post(
        "/api/spot-accumulation/BTC/fills",
        json={**payload, "quantity_btc": 0.02},
    )
    assert conflict.status_code == 409
    assert "不同载荷" in conflict.json()["detail"]


def test_rest_rejects_overspend_and_non_btc(tmp_path):
    service = _service(tmp_path)
    service.evaluate()
    set_components(service, SpotAccumulationExplainer())
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    assert client.get("/api/spot-accumulation/ETH/snapshot").status_code == 400
    response = client.post("/api/spot-accumulation/BTC/fills", json={
        "client_event_id": "too-big",
        "side": "buy",
        "bucket": "core",
        "quantity_btc": 1,
        "price_usdt": 50_000,
        "fee_usdt": 0,
    })
    assert response.status_code == 400
    assert "预留预算" in response.json()["detail"] or "overspend" in response.json()["detail"]


def test_partial_fill_tracks_remaining_allocation_and_reversal(tmp_path):
    service = _service(tmp_path)
    service.evaluate()
    now = int(time.time())
    opportunity = SpotOpportunity(
        opportunity_id="partial-op",
        stage="insurance",
        bucket="core",
        allocation_usdt=1_000,
        reserved_usdt=1_000,
        status="accepted",
        price_zone_low=49_000,
        price_zone_high=51_000,
        trigger_price=50_000,
        scores=EvidenceScore(valuation=80, capital_flow=70, acceptance=70),
        created_at=now,
        updated_at=now,
    )
    service.runtime.opportunities[opportunity.opportunity_id] = opportunity
    service.store.save_state(service.runtime)

    first = service.record_fill({
        "client_event_id": "partial-1",
        "side": "buy",
        "bucket": "core",
        "quantity_btc": 0.01,
        "price_usdt": 50_000,
        "fee_usdt": 0,
        "opportunity_id": "partial-op",
    })
    current = service.runtime.opportunities["partial-op"]
    assert current.status == "accepted"
    assert current.filled_usdt == 500
    assert current.reserved_usdt == 500

    try:
        service.record_fill({
            "client_event_id": "partial-too-big",
            "side": "buy",
            "bucket": "core",
            "quantity_btc": 0.011,
            "price_usdt": 50_000,
            "fee_usdt": 0,
            "opportunity_id": "partial-op",
        })
        assert False, "应拒绝超过机会剩余额度的成交"
    except ValueError as exc:
        assert "超过机会剩余额度" in str(exc)

    service.reverse_fill(first.event_id, "partial-reverse")
    restored = service.runtime.opportunities["partial-op"]
    assert restored.filled_usdt == 0
    assert restored.reserved_usdt == 0
    assert restored.status == "invalidated"


def test_service_cannot_accept_or_fill_blocked_opportunity(tmp_path):
    service = _service(tmp_path)
    snapshot = service.evaluate()
    assert snapshot is not None
    blocked = next(item for item in snapshot.opportunities if item.status == "observing")
    try:
        service.decide_opportunity(blocked.opportunity_id, "accepted")
        assert False, "观察中的机会不能被强制接受"
    except ValueError as exc:
        assert "只有已达标" in str(exc)
    try:
        service.record_fill({
            "client_event_id": "blocked-fill",
            "side": "buy",
            "bucket": blocked.bucket,
            "quantity_btc": 0.001,
            "price_usdt": 50_000,
            "fee_usdt": 0,
            "opportunity_id": blocked.opportunity_id,
        })
        assert False, "未达标机会不能关联成交"
    except ValueError as exc:
        assert "必须先接受" in str(exc)


def test_market_deterioration_invalidates_eligible_and_accepted_uses_fixed_grace(tmp_path):
    service = _service(tmp_path)
    service.config.core_thresholds = {
        stage: {"v": 0, "m": 0, "a": 0}
        for stage in service.config.core_thresholds
    }
    snapshot = service.evaluate()
    assert snapshot is not None
    eligible = next(item for item in snapshot.opportunities if item.status == "eligible")

    accepted = service.decide_opportunity(eligible.opportunity_id, "accepted")
    first_expiry = accepted.grace_expires_at
    assert first_expiry is not None
    retried = service.decide_opportunity(eligible.opportunity_id, "accepted")
    assert retried.grace_expires_at == first_expiry

    state = service._state_getter()
    state.cvd_spot = None
    service.evaluate()
    assert service.runtime.opportunities[eligible.opportunity_id].status == "accepted"

    service.runtime.opportunities[eligible.opportunity_id].grace_expires_at = int(time.time()) - 1
    service.evaluate()
    assert service.runtime.opportunities[eligible.opportunity_id].status == "invalidated"
    assert service.runtime.opportunities[eligible.opportunity_id].reserved_usdt == 0


def test_core_batch_advances_only_after_current_child_is_filled(tmp_path):
    service = _service(tmp_path)
    service.config.core_thresholds = {
        stage: {"v": 0, "m": 0, "a": 0}
        for stage in service.config.core_thresholds
    }
    snapshot = service.evaluate()
    assert snapshot is not None
    first = next(item for item in snapshot.opportunities if item.status == "eligible")
    siblings = sorted(
        [item for item in snapshot.opportunities if item.batch_id == first.batch_id],
        key=lambda item: item.batch_sequence or 0,
    )
    assert len(siblings) >= 2
    service.decide_opportunity(first.opportunity_id, "accepted")
    service.record_fill({
        "client_event_id": "complete-first-child",
        "side": "buy",
        "bucket": first.bucket,
        "quantity_btc": first.allocation_usdt / first.trigger_price,
        "price_usdt": first.trigger_price,
        "fee_usdt": 0,
        "opportunity_id": first.opportunity_id,
    })
    assert service.runtime.opportunities[first.opportunity_id].status == "filled"
    assert service.runtime.opportunities[siblings[1].opportunity_id].status == "eligible"


def test_health_reports_policy_version_not_schema_version(tmp_path):
    service = _service(tmp_path)
    service.evaluate()
    preview = service.preview_config({"initial_capital_usdt": 30_000})
    service.update_config(
        {"initial_capital_usdt": 30_000},
        expected_policy_version=1,
        preview_hash=preview["preview_hash"],
    )
    health = service.health()
    assert health["schema_version"] == 3
    assert health["policy_version"] == 2


def test_config_preview_rejects_illegal_ratios_and_stale_commit(tmp_path):
    service = _service(tmp_path)
    set_components(service, SpotAccumulationExplainer())
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    invalid = client.post("/api/spot-accumulation/config/preview", json={
        "expected_policy_version": 1,
        "core_ratio": 0.8,
    })
    assert invalid.status_code == 200
    assert invalid.json()["errors"]

    preview = client.post("/api/spot-accumulation/config/preview", json={
        "expected_policy_version": 1,
        "initial_capital_usdt": 30_000,
    }).json()
    stale_hash = client.patch("/api/spot-accumulation/config", json={
        "expected_policy_version": 1,
        "preview_hash": "0" * 64,
        "initial_capital_usdt": 30_000,
    })
    assert stale_hash.status_code == 409
    assert "预览已过期" in stale_hash.json()["detail"]

    committed = client.patch("/api/spot-accumulation/config", json={
        "expected_policy_version": 1,
        "preview_hash": preview["preview_hash"],
        "initial_capital_usdt": 30_000,
    })
    assert committed.status_code == 200
    stale_version = client.post("/api/spot-accumulation/config/preview", json={
        "expected_policy_version": 1,
        "initial_capital_usdt": 40_000,
    })
    assert stale_version.status_code == 409


def test_email_notification_marker_is_persisted_and_only_emitted_once(tmp_path):
    service = _service(tmp_path)
    service.config.email_notifications = True
    now = int(time.time())
    opportunity = SpotOpportunity(
        opportunity_id="email-once",
        stage="insurance",
        bucket="core",
        allocation_usdt=1_000,
        reserved_usdt=1_000,
        status="eligible",
        price_zone_low=49_000,
        price_zone_high=51_000,
        trigger_price=50_000,
        scores=EvidenceScore(valuation=80, capital_flow=70, acceptance=70),
        created_at=now,
        updated_at=now,
    )
    service.runtime.opportunities[opportunity.opportunity_id] = opportunity
    assert [item.opportunity_id for item in service.pending_email_notifications()] == ["email-once"]
    service.mark_email_notification_sent("email-once")
    assert service.pending_email_notifications() == []
    restarted = SpotAccumulationService(str(tmp_path), service._state_getter)
    restarted.config.email_notifications = True
    assert restarted.runtime.opportunities["email-once"].notification_sent_at is not None
    assert restarted.pending_email_notifications() == []


def test_monthly_archive_contains_replayable_full_fact_snapshot(tmp_path):
    service = _service(tmp_path)
    snapshot = service.evaluate()
    assert snapshot is not None
    records = service.store.load_facts_snapshots()
    assert records
    record = records[-1]
    assert record["archive_schema_version"] == 2
    assert record["record_type"] == "spot_accumulation_full_fact_snapshot"
    assert record["policy_version"] == service.config.policy_version
    assert record["facts"]["metric_facts"]["spot_netflow_24h_usd"]["source_timestamp"] > 0
    assert "parse_status" in record["score_breakdown"]["etf_flow_5d_usd"]
    assert record["config"]["core_thresholds"] == service.config.core_thresholds
    assert record["opportunities"]
    assert record["opportunity_changes"]
    assert "portfolio" in record and "blocking_reasons" in record


def test_spot_evaluation_does_not_mutate_shared_old_module_state(tmp_path):
    service = _service(tmp_path)
    state = service._state_getter()
    before = {
        "cvd_spot": state.cvd_spot.model_dump(mode="json"),
        "footprint_contract": copy.deepcopy(state.footprint_contract),
        "footprint_spot": copy.deepcopy(state.footprint_spot),
        "footprint_last_ts": state.footprint_last_ts,
        "key_levels": copy.deepcopy(state.key_level_snapshot_v2),
        "orderbook": copy.deepcopy(state.orderbook_pressure_snapshot),
    }
    assert service.evaluate() is not None
    assert state.cvd_spot.model_dump(mode="json") == before["cvd_spot"]
    assert state.footprint_contract == before["footprint_contract"]
    assert state.footprint_spot == before["footprint_spot"]
    assert state.footprint_last_ts == before["footprint_last_ts"]
    assert state.key_level_snapshot_v2 == before["key_levels"]
    assert state.orderbook_pressure_snapshot == before["orderbook"]


def test_real_key_level_behavior_is_nested_and_does_not_crash(tmp_path):
    service = _service(tmp_path)
    state = service._state_getter()
    state.key_level_snapshot_v2 = KeyLevelSnapshotV2(
        ts=int(time.time()),
        current_price=50_000,
        levels=[KeyLevelV2(
            price=49_500,
            side="support",
            state="broken",
            behavior=BehaviorEval(
                capitulation_bottom_score=0.8,
                behavior_state="capitulation_flush",
            ),
        )],
    )
    assert service._capitulation_confirmed(state) is True
    assert service.evaluate() is not None


def test_key_level_without_behavior_is_safe(tmp_path):
    service = _service(tmp_path)
    state = service._state_getter()
    state.key_level_snapshot_v2 = KeyLevelSnapshotV2(
        ts=int(time.time()),
        current_price=50_000,
        levels=[KeyLevelV2(price=49_500, side="support", state="idle")],
    )
    assert service._capitulation_confirmed(state) is False
    assert service._key_level_reclaimed(state, 50_000) is False


def test_contract_only_footprint_never_counts_as_spot_absorption(tmp_path):
    service = _service(tmp_path)
    state = service._state_getter()
    state.footprint_contract = [{
        "ts": int(time.time()),
        "buckets": [{
            "price_lo": 49_800,
            "price_hi": 49_900,
            "buy_quote": 5_000_000,
            "sell_quote": 10_000,
            "buy_trades": 10,
            "sell_trades": 10,
        }],
    }]
    state.footprint_spot = []
    state.footprint_spot_last_ts = None
    snapshot = service.evaluate()
    metric = snapshot.facts.metric_facts["footprint_absorption"]
    assert snapshot.facts.acceptance_inputs["footprint_absorption"] is None
    assert metric.included_in_score is False
    assert metric.score is None


def test_malformed_long_term_metadata_fails_closed_without_crashing(tmp_path):
    service = _service(tmp_path)
    service.long_term["timestamps"] = ["invalid"]
    service.long_term["parse_status"] = "invalid"
    service.long_term["spot_netflow"] = ["invalid"]
    snapshot = service.evaluate()
    assert snapshot is not None
    assert snapshot.facts.data_quality.can_open_new_opportunity is False
    assert snapshot.facts.metric_facts["spot_netflow_24h_usd"].included_in_score is False


def test_snapshot_runtime_error_is_structured_503(monkeypatch, tmp_path):
    service = _service(tmp_path)

    def broken_evaluate():
        raise AttributeError("synthetic field mismatch")

    monkeypatch.setattr(service, "evaluate", broken_evaluate)
    set_components(service, SpotAccumulationExplainer())
    app = FastAPI()
    app.include_router(router)
    response = TestClient(app).get("/api/spot-accumulation/BTC/snapshot")
    assert response.status_code == 503
    detail = response.json()["detail"]
    assert detail["health"]["status"] == "error"
    assert "AttributeError" in detail["health"]["last_evaluation_error"]
