from __future__ import annotations

import time
from types import SimpleNamespace

from models.key_level import KeyLevelSnapshotV2, KeyLevelV2
from models.orderbook_pressure import OrderbookPressureSnapshot, WallZone
from models.spot_accumulation import (
    BucketPosition,
    EvidenceScore,
    SpotAccumulationConfig,
    SpotAccumulationFacts,
    SpotDataQuality,
    SpotMetricFact,
    SpotOpportunity,
    SpotPortfolio,
)
from processors.spot_accumulation_view import (
    build_conditional_ladder,
    build_decision_summary,
    build_support_map,
)
from processors.absorption_detector import detect_absorption_zones
from storage.spot_accumulation_store import (
    SpotLedgerExecutionSummary,
    SpotStageExecution,
)


def _fact(value, now: int, source: str) -> SpotMetricFact:
    return SpotMetricFact(
        value=value,
        source_timestamp=now,
        freshness="fresh",
        parse_status="ok",
        included_in_score=True,
        source=source,
    )


def _facts(now: int, price: float = 100_000) -> SpotAccumulationFacts:
    return SpotAccumulationFacts(
        timestamp=now,
        price=price,
        cycle_ath=126_000,
        drawdown_pct=20.63,
        scores=EvidenceScore(valuation=70, capital_flow=50, acceptance=68),
        data_quality=SpotDataQuality(can_open_new_opportunity=True),
        source_timestamps={
            "orderbook_pressure": now,
            "footprint_spot": now,
            "key_levels": now,
        },
        metric_facts={
            "persistent_spot_wall": _fact(True, now, "spot_orderbook_pressure"),
            "footprint_absorption": _fact(True, now, "coinglass_spot_footprint"),
            "key_level_reclaimed": _fact(False, now, "key_levels"),
            "price_vs_sth": _fact(1.25, now, "cycle_position"),
            "price_vs_200w": _fact(1.4, now, "cycle_position"),
        },
    )


def _wall(now: int) -> WallZone:
    return WallZone.model_validate({
        "wall_zone_id": "spot-bid-99k",
        "side": "bid",
        "price_low": 98_900,
        "price_high": 99_100,
        "price_mid": 99_000,
        "peak_price": 99_000,
        "distance_pct": -1.0,
        "current_usd": 5_000_000,
        "max_usd_1h": 6_000_000,
        "max_usd_8h": 7_000_000,
        "avg_usd_1h": 4_000_000,
        "bin_count": 3,
        "seen_count": 12,
        "visible_minutes": 60,
        "persistence_score": 0.8,
        "persistence_score_8h": 0.7,
        "source": "spot_only",
        "exchange_count": 2,
        "support_resistance_trust_score": 0.86,
        "sweep_attractiveness_score": 0.1,
        "break_through_risk": 0.1,
        "trust_score": 0.86,
        "raw_trust_score": 0.86,
        "has_spot_confluence": True,
        "spot_current_usd": 3_000_000,
        "coinbase_spot_usd": 1_000_000,
        "coinbase_spot_confluence": True,
        "last_seen_ts": now,
    })


def _state(now: int, *, with_wall: bool = True, with_footprint: bool = True):
    wall = _wall(now)
    op = OrderbookPressureSnapshot(
        coin="BTC",
        ts_sec=now,
        last_price=100_000,
        atr=1_000,
        walls_below=[wall] if with_wall else [],
        walls_above=[],
    )
    level = KeyLevelV2(
        price=99_000,
        side="support",
        final_score=90,
        confluence_score=85,
        strength_tier="S",
    )
    footprint = [{
        "ts": now,
        "buckets": [
            {"price_lo": 98_950, "price_hi": 99_050, "buy_quote": 700_000, "sell_quote": 700_000},
            {"price_lo": 99_500, "price_hi": 99_600, "buy_quote": 10_000, "sell_quote": 20_000},
        ],
    }] if with_footprint else []
    return SimpleNamespace(
        atr=1_000,
        key_level_snapshot_v2=KeyLevelSnapshotV2(ts=now, current_price=100_000, levels=[level]),
        orderbook_pressure_snapshot=op,
        liq_maps={},
        footprint_spot=footprint,
        cycle_position=SimpleNamespace(sth_cost_1d=80_000, sma_200w=70_000),
    )


def _portfolio() -> SpotPortfolio:
    return SpotPortfolio(
        initial_capital_usdt=20_000,
        buckets={
            "core": BucketPosition(bucket="core", cash_usdt=13_000),
            "swing": BucketPosition(bucket="swing", cash_usdt=4_000),
            "tail": BucketPosition(bucket="tail", cash_usdt=3_000),
        },
        total_cash_usdt=20_000,
    )


def _absorption(state, now: int):
    return detect_absorption_zones(
        footprint_contract=None,
        footprint_spot=state.footprint_spot,
        current_price=100_000,
        now_ts=now,
    )


def test_support_map_separates_visible_wall_and_executed_absorption():
    now = int(time.time())
    state = _state(now)
    support_map, zones = build_support_map(
        state, _facts(now), now=now, absorption=_absorption(state, now),
    )
    assert zones
    row = next(item for item in support_map if item.spot_wall_usd > 0)
    assert row.binance_spot_usd == 3_000_000
    assert row.coinbase_spot_usd == 1_000_000
    assert row.absorption_usd == 1_400_000
    assert row.absorption_bar_count == 1
    assert row.is_fresh is True
    assert row.anchor_eligible is True


def test_ladder_maps_near_to_far_without_reserving_money():
    now = int(time.time())
    state = _state(now)
    facts = _facts(now)
    _, zones = build_support_map(state, facts, now=now, absorption=_absorption(state, now))
    rows = build_conditional_ladder(
        state, SpotAccumulationConfig(), facts, _portfolio(), [], zones,
        SpotLedgerExecutionSummary(),
    )
    mapped = [row for row in rows if row.reference_price_mid is not None]
    assert [row.reference_price_mid for row in mapped] == sorted(
        [row.reference_price_mid for row in mapped], reverse=True,
    )
    assert rows[0].target_usdt == 1_000
    assert rows[0].status == "conditional"
    assert rows[0].is_actionable is False
    assert all(row.opportunity_id is None for row in rows)
    assert rows[-1].status == "waiting_event"
    assert rows[-1].reference_price_mid is None


def test_actionable_price_only_comes_from_eligible_opportunity():
    now = int(time.time())
    facts = _facts(now)
    opportunity = SpotOpportunity(
        opportunity_id="eligible-1",
        stage="insurance",
        bucket="core",
        allocation_usdt=1_000,
        reserved_usdt=1_000,
        status="eligible",
        price_zone_low=99_700,
        price_zone_high=100_300,
        trigger_price=100_000,
        scores=facts.scores,
        created_at=now,
        updated_at=now,
    )
    state = _state(now)
    _, zones = build_support_map(state, facts, now=now, absorption=_absorption(state, now))
    rows = build_conditional_ladder(
        state, SpotAccumulationConfig(), facts, _portfolio(), [opportunity], zones,
        SpotLedgerExecutionSummary(),
    )
    assert rows[0].status == "eligible"
    assert rows[0].is_actionable is True
    assert rows[0].reference_price_low == 99_700
    summary = build_decision_summary(facts, rows, [opportunity], now=now)
    assert summary.state == "eligible"
    assert summary.headline == "现在可手工买入"
    assert summary.amount_usdt == 1_000


def test_stale_or_missing_sources_do_not_create_reference_prices():
    now = int(time.time())
    facts = _facts(now)
    for item in facts.metric_facts.values():
        item.freshness = "stale"
        item.included_in_score = False
    state = _state(now, with_wall=False, with_footprint=False)
    support_map, zones = build_support_map(state, facts, now=now)
    rows = build_conditional_ladder(
        state, SpotAccumulationConfig(), facts, _portfolio(), [], zones,
        SpotLedgerExecutionSummary(),
    )
    assert support_map == []
    assert all(row.reference_price_mid is None for row in rows)
    assert all(
        row.status == ("waiting_event" if row.pricing_mode == "event_driven" else "waiting_anchor")
        for row in rows
    )


def test_absorption_is_assigned_to_only_one_nearest_wall():
    now = int(time.time())
    state = _state(now)
    first = _wall(now)
    second = first.model_copy(update={
        "wall_zone_id": "spot-bid-98850",
        "price_low": 98_800,
        "price_high": 98_900,
        "price_mid": 98_850,
        "peak_price": 98_850,
        "distance_pct": -1.15,
    })
    state.orderbook_pressure_snapshot = OrderbookPressureSnapshot(
        coin="BTC", ts_sec=now, last_price=100_000, atr=1_000,
        walls_below=[first, second], walls_above=[],
    )
    rows, _ = build_support_map(
        state, _facts(now), now=now, absorption=_absorption(state, now),
    )
    assert sum(row.absorption_usd for row in rows) == 1_400_000
    assert sum(row.absorption_usd > 0 for row in rows) == 1


def test_wall_and_absorption_freshness_are_independent():
    now = int(time.time())
    state = _state(now)
    facts = _facts(now)
    facts.metric_facts["footprint_absorption"].freshness = "stale"
    rows, _ = build_support_map(
        state, facts, now=now, absorption=_absorption(state, now),
    )
    row = next(item for item in rows if item.spot_wall_usd > 0)
    assert row.wall_fresh is True
    assert row.absorption_fresh is False
    assert row.is_fresh is False


def test_core_cash_shortfall_does_not_borrow_from_other_buckets():
    now = int(time.time())
    state = _state(now)
    facts = _facts(now)
    _, zones = build_support_map(state, facts, now=now, absorption=_absorption(state, now))
    portfolio = _portfolio()
    portfolio.buckets["core"].cash_usdt = 0
    portfolio.total_cash_usdt = 7_000
    rows = build_conditional_ladder(
        state, SpotAccumulationConfig(), facts, portfolio, [], zones,
        SpotLedgerExecutionSummary(unassigned_core_buy_usdt=13_000),
    )
    assert rows[0].planned_usdt == 0
    assert rows[0].cash_shortfall_usdt == rows[0].remaining_usdt
    assert rows[0].estimated_btc is None
    assert "策略外核心买入" in "；".join(rows[0].blockers)


def test_filled_stage_uses_ledger_price_and_event_stages_wait_for_confirmation():
    now = int(time.time())
    state = _state(now)
    facts = _facts(now)
    _, zones = build_support_map(state, facts, now=now, absorption=_absorption(state, now))
    summary = SpotLedgerExecutionSummary(stages={
        "insurance": SpotStageExecution(
            spent_usdt=1_000, quantity_btc=0.0111111111, fee_usdt=0,
        ),
    })
    rows = build_conditional_ladder(
        state, SpotAccumulationConfig(), facts, _portfolio(), [], zones, summary,
    )
    assert rows[0].status == "filled"
    assert round(rows[0].historical_average_price) == 90_000
    assert rows[0].reference_price_mid is None
    assert rows[3].status == "waiting_event"
    assert rows[3].reference_price_mid is None
    assert rows[4].status == "waiting_event"
    assert rows[4].reference_price_mid is None


def test_non_core_decision_summary_exposes_action_opportunity_id():
    now = int(time.time())
    facts = _facts(now)
    opportunity = SpotOpportunity(
        opportunity_id="tail-action",
        stage="tail_extreme",
        bucket="tail",
        allocation_usdt=1_000,
        reserved_usdt=1_000,
        status="eligible",
        price_zone_low=90_000,
        price_zone_high=91_000,
        trigger_price=90_500,
        scores=facts.scores,
        created_at=now,
        updated_at=now,
    )
    decision = build_decision_summary(facts, [], [opportunity], now=now)
    assert decision.opportunity_id == "tail-action"
    assert decision.bucket == "tail"
    assert decision.state == "eligible"


def test_event_stage_gets_price_only_after_confirmation():
    now = int(time.time())
    state = _state(now)
    facts = _facts(now)
    _, zones = build_support_map(state, facts, now=now, absorption=_absorption(state, now))
    capitulation = SpotOpportunity(
        opportunity_id="cap-confirmed",
        stage="capitulation",
        bucket="core",
        allocation_usdt=3_000,
        status="observing",
        price_zone_low=87_000,
        price_zone_high=88_000,
        trigger_price=87_500,
        scores=facts.scores,
        created_at=now,
        updated_at=now,
    )
    waiting = build_conditional_ladder(
        state, SpotAccumulationConfig(), facts, _portfolio(), [capitulation], zones,
        SpotLedgerExecutionSummary(), capitulation_confirmed=False,
    )
    confirmed = build_conditional_ladder(
        state, SpotAccumulationConfig(), facts, _portfolio(), [capitulation], zones,
        SpotLedgerExecutionSummary(), capitulation_confirmed=True,
    )
    assert waiting[3].status == "waiting_event"
    assert waiting[3].reference_price_mid is None
    assert confirmed[3].status == "conditional"
    assert confirmed[3].reference_price_mid == 87_500
