from __future__ import annotations

import time
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ai.spot_accumulation_explainer import SpotAccumulationExplainer
from api.routes_spot_accumulation import router, set_components
from models.flow import CVDData, CVDPoint
from models.spot_accumulation import EvidenceScore, SpotOpportunity
from processors.spot_accumulation_service import SpotAccumulationService


def _state(now: int):
    daily = [
        SimpleNamespace(high=126_000 if i == 0 else 55_000, low=45_000, close=50_000)
        for i in range(30)
    ]
    weekly = [SimpleNamespace(high=126_000, low=40_000, close=50_000 + i * 100) for i in range(22)]
    cvd_points = [
        CVDPoint(ts=(now - (11 - i) * 300), buy_vol=10, sell_vol=5, delta=5, cvd=100 + i * 5)
        for i in range(12)
    ]
    return SimpleNamespace(
        ticker=SimpleNamespace(last=50_000, ts=now * 1000),
        candles_daily=daily,
        candles_weekly=weekly,
        cycle_position=SimpleNamespace(
            ahr999_value=0.5,
            sma_200w=48_000,
            sth_cost_1d=55_000,
        ),
        market_index=SimpleNamespace(btc_mvrv=0.9, ahr999=0.5),
        stablecoin_mcap=SimpleNamespace(history=[
            SimpleNamespace(total_mcap=100_000_000_000),
            SimpleNamespace(total_mcap=101_000_000_000),
        ]),
        coinbase_premium=SimpleNamespace(current_premium=0.001),
        cvd_spot=CVDData(
            coin="BTC", inst_type="SPOT", series=cvd_points,
            delta_1h=80_000_000, trend_1h="rising",
        ),
        taker_spot_series=[{"delta_usd": 8_000_000}] * 12,
        footprint_contract=[],
        footprint_spot=[],
        footprint_last_ts=now,
        orderbook_pressure_snapshot=SimpleNamespace(ts_sec=now, walls_below=[]),
        key_level_snapshot_v2=SimpleNamespace(levels=[]),
    )


def _service(tmp_path) -> SpotAccumulationService:
    now = int(time.time())
    state = _state(now)
    service = SpotAccumulationService(str(tmp_path), lambda: state)
    service.long_term = {
        "spot_netflow": {"net_flow_usd_24h": "300000000"},
        "etf_flow": [{"flow_usd": 100_000_000}] * 5,
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
    assert snapshot.facts.data_quality.completeness == 1
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

    response = client.patch(
        "/api/spot-accumulation/config",
        json={"initial_capital_usdt": 30_000},
    )
    assert response.status_code == 200
    config = response.json()
    assert config["core_budget_usdt"] == 19_500
    assert config["swing_budget_usdt"] == 6_000
    assert config["tail_budget_usdt"] == 4_500
    assert config["max_swing_loss_usdt"] == 300

    snapshot = client.get("/api/spot-accumulation/BTC/snapshot").json()
    assert snapshot["portfolio"]["initial_capital_usdt"] == 30_000
    insurance = next(item for item in snapshot["opportunities"] if item["stage"] == "insurance")
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

    response = client.patch(
        "/api/spot-accumulation/config",
        json={"initial_capital_usdt": 1_000},
    )
    assert response.status_code == 400
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
    assert restored.reserved_usdt == 1_000
    assert restored.status == "accepted"


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
        assert "尚未达标" in str(exc)
