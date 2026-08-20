from __future__ import annotations

import json
from pathlib import Path
from dataclasses import fields

from config.settings import MarketRiskConfig, _build_settings, _load_yaml
from engine import CoinState
from models.flow import FundingRateData, GlobalLiquidationData, OIData, OISnapshot
from models.liquidation import LiqCluster, LiqLeverageGroup, LiquidationMap
from models.market import TickerData
from models.orderbook_pressure import OrderbookPressureSnapshot, WallZone
from processors.market_risk_engine import MarketRiskEngine, compute_leverage_refill_ratio


def _state(now: int) -> CoinState:
    state = CoinState("BTC")
    state.ticker = TickerData(
        coin="BTC", ts=now * 1000, last=70_000, high_24h=71_000,
        low_24h=63_000, vol_24h=10_000_000_000,
        change_24h=6_000, change_pct_24h=9.3,
    )
    history = [
        OISnapshot(
            coin="BTC", ts=now - 3600 + index * 300,
            oi=100_000 + index * 250, oi_usd=7_000_000_000,
            oi_contracts=100_000 + index * 250,
            oi_base_equivalent=100_000 + index * 250,
            oi_usd_notional=7_000_000_000,
            contract_type="linear", contract_size=1,
            margin_asset="USDT", mark_price=70_000,
            decision_valid=True, source="fixture",
        )
        for index in range(13)
    ]
    state.oi_history.extend(history)
    state.oi = OIData(
        coin="BTC", ts=now, current_usd=7_000_000_000,
        change_1h_pct=10.0, change_5m_pct=0.2,
        history=history, current_contracts=103_000,
        current_base_equivalent=103_000, current_usd_notional=7_000_000_000,
        decision_change_5m_pct=0.24, decision_change_1h_pct=3.0,
        decision_unit="contracts", decision_valid=True,
        source="fixture", contract_type="linear", contract_size=1,
        margin_asset="USDT", mark_price=70_000,
    )
    state.funding = FundingRateData(
        coin="BTC", ts=now, avg_rate=-0.0007,
        predicted_rate_observed=-0.0007, observed_at=now, source="fixture",
    )
    state.global_liq = GlobalLiquidationData(
        ts=now, long_1h_usd=10_000_000, short_1h_usd=180_000_000,
    )
    state.liq_maps["1d"] = LiquidationMap(
        coin="BTC", ts=now, cycle="1d",
        leverage_groups=[LiqLeverageGroup(leverage="50")],
        clusters_above=[LiqCluster(
            price_center=71_000, price_from=70_800, price_to=71_200,
            total_usd=450_000_000, side="short",
        )],
    )
    wall = WallZone(
        wall_zone_id="ask-1", side="ask", price_low=70_800,
        price_high=71_000, price_mid=70_900, peak_price=70_900,
        distance_pct=1.29, current_usd=20_000_000,
        max_usd_1h=30_000_000, avg_usd_1h=22_000_000,
        bin_count=3, seen_count=12, visible_minutes=60,
        persistence_score=0.9, active_attack_score=0.82,
        wall_removal_risk=0.2, trust_score=0.75,
    )
    state.orderbook_pressure_snapshot = OrderbookPressureSnapshot(
        coin="BTC", ts_sec=now, last_price=70_000,
        wall_zones=[wall], walls_above=[wall], data_quality="ok",
    )
    return state


def _engine(tmp_path: Path, state: CoinState) -> MarketRiskEngine:
    backend_root = Path(__file__).resolve().parents[1]
    config = MarketRiskConfig(
        data_dir=str(tmp_path), raw_event_store_enabled=False,
        shadow_mode=True, email_enabled=False,
    )
    engine = MarketRiskEngine(
        config=config, state_getter=lambda coin: state,
        backend_root=str(backend_root),
    )

    def flow(_coin: str, market: str):
        return {
            "as_of": state.oi.ts - state.oi.ts % 60,
            "first_bucket_ts": state.oi.ts - state.oi.ts % 60 - 240,
            "continuity": "continuous",
            "aggressor_buy_quote": 90_000_000 if market == "spot" else 80_000_000,
            "aggressor_sell_quote": 30_000_000 if market == "spot" else 40_000_000,
            "total_quote": 120_000_000,
        }

    engine._trade_flow = flow  # type: ignore[method-assign]
    return engine


def test_state_machine_advances_only_one_stage_per_background_tick(tmp_path: Path):
    now = 1_787_156_400
    state = _state(now)
    engine = _engine(tmp_path, state)
    try:
        first = engine.evaluate_coin("BTC", now)
        second = engine.evaluate_coin("BTC", now + 30)
        third = engine.evaluate_coin("BTC", now + 60)
        assert [first.stage, second.stage, third.stage] == ["watch", "warning", "critical"]
        assert first.incident_id == second.incident_id == third.incident_id
        assert first.episode_id == second.episode_id == third.episode_id
        assert third.spot_confirmed is True
        assert third.independent_root_count >= 3
        assert third.notification_eligible is False
        assert all("news" not in item.source_id and "ai" not in item.source_id for item in third.evidence)
    finally:
        engine.store.close()
        engine.onchain_store.close()


def test_data_degraded_freezes_stage_and_blocks_transition(tmp_path: Path):
    now = 1_787_156_400
    state = _state(now)
    engine = _engine(tmp_path, state)
    try:
        engine.evaluate_coin("BTC", now)
        warning = engine.evaluate_coin("BTC", now + 30)
        state.orderbook_pressure_snapshot.data_quality = "stale"
        frozen = engine.evaluate_coin("BTC", now + 60)
        assert warning.stage == "warning"
        assert frozen.stage == "warning"
        assert frozen.quality_layer == "data_degraded"
        assert frozen.notification_eligible is False
        assert "禁止升级" in frozen.transition_reason
    finally:
        engine.store.close()
        engine.onchain_store.close()


def test_same_causal_root_is_counted_once(tmp_path: Path):
    now = 1_787_156_400
    state = _state(now)
    state.oi.decision_change_1h_pct = -3.0
    engine = _engine(tmp_path, state)
    try:
        snapshot = engine.evaluate_coin("BTC", now)
        unwind_items = [item for item in snapshot.evidence if item.causal_root == "position_unwind"]
        assert len(unwind_items) >= 2
        assert snapshot.causal_roots.count("position_unwind") <= 1
    finally:
        engine.store.close()
        engine.onchain_store.close()


def test_market_risk_unknown_config_key_fails_startup():
    raw = _load_yaml()
    raw = json.loads(json.dumps(raw))
    raw["market_risk"]["typo_threshold"] = 123
    try:
        _build_settings(raw)
    except ValueError as exc:
        assert "unknown market_risk config keys" in str(exc)
    else:
        raise AssertionError("unknown config key must fail closed")


def test_every_market_risk_config_field_has_declared_consumer():
    field_names = {field.name for field in fields(MarketRiskConfig)}
    registry = MarketRiskConfig.consumer_registry()
    assert set(registry) == field_names
    assert all(owner.strip() for owner in registry.values())


def test_store_repeated_read_does_not_advance_machine(tmp_path: Path):
    now = 1_787_156_400
    state = _state(now)
    engine = _engine(tmp_path, state)
    try:
        evaluated = engine.evaluate_coin("BTC", now)
        first_read = engine.latest("BTC")
        second_read = engine.latest("BTC")
        assert first_read == second_read == evaluated
        context = engine.store.load_machine_context("BTC")
        assert context is not None and context.stage == "watch"
    finally:
        engine.store.close()
        engine.onchain_store.close()


def test_duplicate_decision_time_is_idempotent(tmp_path: Path):
    now = 1_787_156_400
    state = _state(now)
    engine = _engine(tmp_path, state)
    try:
        first = engine.evaluate_coin("BTC", now)
        duplicate = engine.evaluate_coin("BTC", now)
        assert duplicate == first
        assert duplicate.stage == "watch"
        transitions = engine.store._conn.execute(
            "SELECT COUNT(*) FROM transitions"
        ).fetchone()[0]
        assert transitions == 1
    finally:
        engine.store.close()
        engine.onchain_store.close()


def test_lrr_handles_secondary_dip_over_refill_and_unknown_direction():
    def oi(values):
        history = [
            OISnapshot(
                coin="BTC", ts=index, oi=value, oi_usd=value * 100,
                oi_contracts=value, oi_base_equivalent=value,
                contract_type="linear", contract_size=1,
                decision_valid=True,
            )
            for index, value in enumerate(values)
        ]
        return OIData(coin="BTC", ts=len(values), current_usd=1, history=history)

    dip = compute_leverage_refill_ratio(oi([100, 90, 96, 92]), 0.01)
    assert dip["status"] == "secondary_deleveraging"
    assert dip["direction"] == "unknown"

    over = compute_leverage_refill_ratio(oi([100, 90, 105, 115]), 0.2)
    assert over["status"] == "over_refill"
    assert over["ratio"] == 2.5
    assert over["direction"] == "up"

    small = compute_leverage_refill_ratio(oi([100, 100, 100, 100]), 0.2)
    assert small == {"status": "unavailable", "reason": "release_denominator_too_small"}
