from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from models.spot_accumulation import SpotAccumulationFacts, SpotMetricFact
from polls.footprint import poll_footprint
from processors.spot_accumulation import score_facts
from processors.spot_accumulation_service import SpotAccumulationService


def test_missing_values_are_excluded_instead_of_scored_neutral():
    facts = SpotAccumulationFacts(
        timestamp=int(time.time()),
        price=50_000,
        cycle_ath=100_000,
        drawdown_pct=50,
        valuation_inputs={"mvrv": None, "ahr999": None},
    )
    score = score_facts(facts)
    assert score.valuation == 50
    assert score.capital_flow == 0
    assert score.acceptance == 0


def test_stale_metric_is_displayed_but_excluded_from_score():
    facts = SpotAccumulationFacts(
        timestamp=int(time.time()),
        price=50_000,
        cycle_ath=100_000,
        drawdown_pct=50,
        valuation_inputs={"mvrv": 0.75},
        metric_facts={
            "drawdown_pct": SpotMetricFact(
                value=50, source_timestamp=int(time.time()), freshness="fresh",
                parse_status="ok", included_in_score=True, source="ticker",
            ),
            "mvrv": SpotMetricFact(
                value=0.75, source_timestamp=int(time.time()) - 99_999,
                freshness="stale", parse_status="ok", included_in_score=False,
                source="bbx",
            ),
        },
    )
    score = score_facts(facts)
    assert score.valuation == 50
    assert facts.metric_facts["mvrv"].score is None


def test_timed_series_rejects_empty_unknown_and_future_timestamp():
    now = int(time.time())
    assert SpotAccumulationService._parse_timed_series([], "flow_usd", now)[2] == "empty"
    assert SpotAccumulationService._parse_timed_series(
        [{"timestamp": now, "unknown": 1}], "flow_usd", now,
    )[2] == "missing_field"
    assert SpotAccumulationService._parse_timed_series(
        [{"timestamp": now + 600, "flow_usd": 1}], "flow_usd", now,
    )[2] == "invalid_timestamp"


def test_etf_older_than_three_days_is_not_included():
    now = int(time.time())
    fact = SpotAccumulationService._metric_fact(
        100_000_000, now - 3 * 86_400 - 1, now, 3 * 86_400, "coinglass_etf", "ok",
    )
    assert fact.freshness == "stale"
    assert fact.included_in_score is False


def test_layer_coverage_requires_spot_netflow_and_core_acceptance_sources():
    now = int(time.time())

    def fresh(value=1.0):
        return SpotMetricFact(
            value=value, source_timestamp=now, freshness="fresh",
            parse_status="ok", included_in_score=True, source="test",
        )

    names = [
        "drawdown_pct", "mvrv", "ahr999", "price_vs_200w", "price_vs_sth",
        "nupl", "reserve_risk", "puell", "sth_sopr", "sth_supply_change_30d_pct",
        "etf_flow_5d_usd", "exchange_balance_7d_pct", "spot_netflow_24h_usd",
        "stablecoin_change_7d_pct", "coinbase_premium", "spot_cvd_delta_1h",
        "spot_taker_delta_1h", "footprint_absorption", "persistent_spot_wall",
        "coinbase_confluence", "key_level_reclaimed",
    ]
    metrics = {name: fresh(False if "absorption" in name else 1.0) for name in names}
    metrics["spot_netflow_24h_usd"] = SpotMetricFact(source="test")
    quality = SpotAccumulationService._quality(metrics)
    assert quality.layer_quality["capital_flow"].passed is False
    assert quality.can_open_new_opportunity is False

    metrics["spot_netflow_24h_usd"] = fresh()
    metrics["spot_cvd_delta_1h"] = SpotMetricFact(source="test")
    quality = SpotAccumulationService._quality(metrics)
    assert quality.layer_quality["acceptance"].passed is False


def test_weekly_confirmation_ignores_open_week():
    now = int(time.time())
    closes = [100.0] * 20 + [110.0, 105.0, 200.0]
    candles = [
        SimpleNamespace(ts=(now - (22 - i) * 7 * 86_400) * 1000, close=close)
        for i, close in enumerate(closes)
    ]
    state = SimpleNamespace(candles_weekly=candles)
    assert SpotAccumulationService._weekly_reclaim_confirmed(state) is False


def test_contract_wall_flag_without_real_spot_thickness_cannot_count():
    now = int(time.time())
    wall = SimpleNamespace(
        price_mid=49_500, last_seen_ts=now, persistence_score=0.8,
        support_resistance_trust_score=0.8, dual_source=True,
        spot_current_usd=0, has_spot_confluence=False, spot_large_order_ids=[],
        coinbase_spot_confluence=False, coinbase_spot_usd=0, coinbase_num_orders=0,
    )
    state = SimpleNamespace(
        orderbook_pressure_snapshot=SimpleNamespace(walls_below=[wall]),
        coinbase_orderbook=None,
    )
    persistent, coinbase = SpotAccumulationService._wall_evidence(state, 50_000)
    assert persistent is False
    assert coinbase is None
    wall.spot_current_usd = 1_000_000
    persistent, _ = SpotAccumulationService._wall_evidence(state, 50_000)
    assert persistent is True


@pytest.mark.asyncio
async def test_spot_footprint_has_independent_success_timestamp():
    class CG:
        async def fetch_futures_footprint_history(self, **_kwargs):
            return None

        async def fetch_spot_footprint_history(self, **_kwargs):
            return [[int(time.time()), [[49_000, 49_100, 1, 2, 3, 4, 3, 4, 5, 6]]]]

    state = SimpleNamespace(
        footprint_contract=[], footprint_spot=[], footprint_last_ts=None,
        footprint_spot_last_ts=None, poll_failures={}, _log_once_keys=set(),
    )
    coin = SimpleNamespace(ccy="BTC", symbol_cg_pair="BTCUSDT")
    await poll_footprint(CG(), coin, state)
    assert state.footprint_last_ts is None
    assert state.footprint_spot_last_ts is not None
    assert len(state.footprint_spot) == 1


@pytest.mark.asyncio
async def test_future_spot_footprint_does_not_refresh_timestamp():
    class CG:
        async def fetch_futures_footprint_history(self, **_kwargs):
            return None

        async def fetch_spot_footprint_history(self, **_kwargs):
            future = int(time.time()) + 600
            return [[future, [[49_000, 49_100, 1, 2, 3, 4, 3, 4, 5, 6]]]]

    state = SimpleNamespace(
        footprint_contract=[], footprint_spot=[], footprint_last_ts=None,
        footprint_spot_last_ts=None, poll_failures={}, _log_once_keys=set(),
    )
    coin = SimpleNamespace(ccy="BTC", symbol_cg_pair="BTCUSDT")
    await poll_footprint(CG(), coin, state)
    assert state.footprint_spot_last_ts is None
    assert "footprint_spot" in state.poll_failures


@pytest.mark.asyncio
async def test_empty_fast_poll_does_not_refresh_previous_source_timestamp(tmp_path):
    class CG:
        async def fetch_spot_coin_netflow(self, _symbol):
            return {}

        async def fetch_btc_etf_flow_history(self):
            return []

    service = SpotAccumulationService(str(tmp_path), lambda: None)
    service.long_term = {
        "spot_netflow": {"net_flow_usd_24h": 1.0},
        "etf_flow": [{"timestamp": 1, "flow_usd": 1.0}],
        "timestamps": {"spot_netflow": 123, "etf_flow": 456},
    }
    await service.poll_fast(CG())
    assert service.long_term["timestamps"] == {"spot_netflow": 123, "etf_flow": 456}
    assert service.long_term["parse_status"]["spot_netflow"] == "empty"
    assert service.long_term["parse_status"]["etf_flow"] == "empty"
