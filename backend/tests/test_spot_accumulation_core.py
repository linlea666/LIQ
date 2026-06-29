from __future__ import annotations

import json
import time

import pytest

from models.spot_accumulation import (
    EvidenceScore,
    SpotAccumulationConfig,
    SpotAccumulationFacts,
    SpotAccumulationRuntimeState,
    SpotDataQuality,
    SpotLedgerEvent,
)
from processors.spot_accumulation import (
    build_opportunities,
    build_swing_opportunity,
    build_tail_opportunities,
    score_facts,
)
from storage.spot_accumulation_store import SpotAccumulationStore


def _facts(**updates) -> SpotAccumulationFacts:
    raw = dict(
        timestamp=int(time.time()),
        price=50_000,
        cycle_ath=126_000,
        drawdown_pct=60.32,
        valuation_inputs={
            "mvrv": 0.9,
            "ahr999": 0.5,
            "price_vs_200w": 1.0,
            "price_vs_sth": 0.85,
            "nupl": 0.05,
            "reserve_risk": 0.001,
            "puell": 0.6,
            "sth_sopr": 0.97,
        },
        capital_inputs={
            "etf_flow_5d_usd": 500_000_000,
            "exchange_balance_7d_pct": -2.0,
            "spot_netflow_24h_usd": 300_000_000,
            "stablecoin_change_7d_pct": 1.0,
            "coinbase_premium": 0.001,
        },
        acceptance_inputs={
            "spot_cvd_delta_1h": 80_000_000,
            "spot_taker_delta_1h": 70_000_000,
            "footprint_absorption": True,
            "persistent_spot_wall": True,
            "coinbase_confluence": True,
            "key_level_reclaimed": True,
        },
        data_quality=SpotDataQuality(completeness=1, can_open_new_opportunity=True),
    )
    raw.update(updates)
    facts = SpotAccumulationFacts(**raw)
    facts.scores = score_facts(facts)
    return facts


def _fill(event_id: str, client_id: str, *, side="buy", bucket="core", qty=0.02,
          price=50_000, fee=1.0, opportunity_id=None) -> SpotLedgerEvent:
    now = int(time.time())
    return SpotLedgerEvent(
        event_id=event_id,
        client_event_id=client_id,
        side=side,
        bucket=bucket,
        quantity_btc=qty,
        price_usdt=price,
        fee_usdt=fee,
        executed_at=now,
        created_at=now,
        opportunity_id=opportunity_id,
        policy_override=bucket == "core" and side == "sell",
    )


def test_default_budget_is_exactly_20k():
    cfg = SpotAccumulationConfig()
    assert cfg.core_budget_usdt + cfg.swing_budget_usdt + cfg.tail_budget_usdt == 20_000


def test_budget_and_every_tranche_scale_from_configured_capital():
    cfg = SpotAccumulationConfig(initial_capital_usdt=30_000)
    assert cfg.core_budget_usdt == 19_500
    assert cfg.swing_budget_usdt == 6_000
    assert cfg.tail_budget_usdt == 4_500
    assert cfg.core_stage_allocations() == {
        "insurance": 1_500,
        "value_1": 3_000,
        "deep_value": 4_500,
        "capitulation": 4_500,
        "bottom_confirmed": 6_000,
    }
    assert cfg.tail_tranche_usdt == 1_500
    assert cfg.max_swing_loss_usdt == 300


def test_legacy_absolute_budget_config_migrates_to_ratios(tmp_path):
    store = SpotAccumulationStore(str(tmp_path))
    store.config_path.write_text(json.dumps({
        "version": 1,
        "coin": "BTC",
        "initial_capital_usdt": 20_000,
        "core_budget_usdt": 13_000,
        "swing_budget_usdt": 4_000,
        "tail_budget_usdt": 3_000,
        "insurance_cap_usdt": 1_000,
        "max_swing_loss_usdt": 200,
    }), encoding="utf-8")
    cfg = store.load_config()
    assert cfg.core_ratio == pytest.approx(0.65)
    assert cfg.max_swing_loss_ratio == pytest.approx(0.01)
    assert cfg.public_dump()["core_budget_usdt"] == 13_000


def test_store_rebuild_idempotency_and_reversal(tmp_path):
    store = SpotAccumulationStore(str(tmp_path))
    cfg = SpotAccumulationConfig()
    buy = _fill("f1", "client-1")
    assert store.append_event(buy).event_id == "f1"
    assert store.append_event(buy).event_id == "f1"
    assert len(store.load_events()) == 1

    portfolio = store.build_portfolio(cfg)
    assert portfolio.total_btc == pytest.approx(0.02)
    assert portfolio.buckets["core"].cash_usdt == pytest.approx(11_999.0)

    now = int(time.time())
    reversal = SpotLedgerEvent(
        event_id="r1",
        client_event_id="client-r1",
        event_type="reversal",
        reverses_event_id="f1",
        executed_at=now,
        created_at=now,
    )
    store.append_event(reversal)
    rebuilt = store.build_portfolio(cfg)
    assert rebuilt.total_btc == 0
    assert rebuilt.total_cash_usdt == 20_000


def test_swing_profit_moves_to_core_budget(tmp_path):
    store = SpotAccumulationStore(str(tmp_path))
    cfg = SpotAccumulationConfig()
    store.append_event(_fill("b1", "b1", bucket="swing", qty=0.05, price=40_000, fee=0))
    store.append_event(_fill("s1", "s1", side="sell", bucket="swing", qty=0.05,
                             price=44_000, fee=0))
    portfolio = store.build_portfolio(cfg)
    assert portfolio.buckets["swing"].cash_usdt == pytest.approx(4_000)
    assert portfolio.buckets["core"].cash_usdt == pytest.approx(13_200)
    assert portfolio.core_bonus_from_swing_usdt == pytest.approx(200)


def test_overspend_and_oversell_are_rejected_on_rebuild(tmp_path):
    store = SpotAccumulationStore(str(tmp_path))
    store.append_event(_fill("too-big", "too-big", qty=1, price=50_000, fee=0))
    with pytest.raises(ValueError, match="overspend"):
        store.build_portfolio(SpotAccumulationConfig())


def test_missing_or_stale_data_never_releases_budget():
    facts = _facts(
        data_quality=SpotDataQuality(
            completeness=0.5,
            missing_sources=["spot_cvd"],
            can_open_new_opportunity=False,
        )
    )
    facts.scores = EvidenceScore(valuation=95, capital_flow=95, acceptance=95)
    opportunities = build_opportunities(
        facts,
        SpotAccumulationRuntimeState(),
        {"core": 13_000, "swing": 4_000, "tail": 3_000},
        {"core": 0, "swing": 0, "tail": 0},
        capitulation_confirmed=True,
        weekly_reclaim_confirmed=True,
    )
    assert opportunities
    assert all(item.status == "observing" for item in opportunities)
    assert all("数据完整度或新鲜度不足" in item.blocked_by for item in opportunities)


def test_high_quality_facts_release_only_one_core_stage_at_a_time():
    facts = _facts()
    facts.scores = EvidenceScore(valuation=95, capital_flow=95, acceptance=95)
    opportunities = build_opportunities(
        facts,
        SpotAccumulationRuntimeState(),
        {"core": 13_000, "swing": 4_000, "tail": 3_000},
        {"core": 0, "swing": 0, "tail": 0},
        capitulation_confirmed=True,
        weekly_reclaim_confirmed=True,
    )
    eligible = [item for item in opportunities if item.status == "eligible"]
    assert [item.stage for item in eligible] == ["insurance"]
    assert sum(item.reserved_usdt for item in eligible) == 1_000
    later = [item for item in opportunities if item.stage != "insurance"]
    assert all("等待同批次前序子档完成或跳过" in item.blocked_by for item in later)
    assert len({item.batch_id for item in opportunities}) == 1
    assert [item.batch_sequence for item in opportunities] == [1, 2, 3, 4, 5]


def test_core_and_tail_opportunities_use_configured_capital():
    cfg = SpotAccumulationConfig(initial_capital_usdt=30_000)
    facts = _facts()
    facts.scores = EvidenceScore(valuation=95, capital_flow=95, acceptance=95)
    core = build_opportunities(
        facts,
        SpotAccumulationRuntimeState(),
        {"core": cfg.core_budget_usdt, "swing": cfg.swing_budget_usdt, "tail": cfg.tail_budget_usdt},
        {"core": 0, "swing": 0, "tail": 0},
        capitulation_confirmed=True,
        weekly_reclaim_confirmed=True,
        stage_allocations=cfg.core_stage_allocations(),
    )
    assert next(item for item in core if item.stage == "insurance").allocation_usdt == 1_500
    tail = build_tail_opportunities(
        facts,
        SpotAccumulationRuntimeState(),
        cfg.tail_budget_usdt,
        0,
        capitulation_confirmed=True,
        weekly_reclaim_confirmed=False,
        tranche_usdt=cfg.tail_tranche_usdt,
    )
    assert tail[0].allocation_usdt == 1_500


def test_new_downside_batch_needs_lower_price_gap():
    facts = _facts()
    facts.scores = EvidenceScore(valuation=95, capital_flow=95, acceptance=95)
    runtime = SpotAccumulationRuntimeState(last_filled_price=50_000)
    same_price = build_opportunities(
        facts, runtime,
        {"core": 12_000, "swing": 4_000, "tail": 3_000},
        {"core": 0, "swing": 0, "tail": 0},
    )
    assert [item.stage for item in same_price] == ["insurance"]

    lower = facts.model_copy(deep=True)
    lower.price = 45_000
    next_items = build_opportunities(
        lower, SpotAccumulationRuntimeState(last_filled_price=50_000),
        {"core": 12_000, "swing": 4_000, "tail": 3_000},
        {"core": 0, "swing": 0, "tail": 0},
    )
    assert any(item.stage == "deep_value" for item in next_items)


def test_tail_mode_is_three_sequential_tranches():
    facts = _facts()
    facts.scores = EvidenceScore(valuation=95, capital_flow=70, acceptance=90)
    runtime = SpotAccumulationRuntimeState()
    tail = build_tail_opportunities(
        facts, runtime, 3_000, 0,
        capitulation_confirmed=True,
        weekly_reclaim_confirmed=False,
    )
    assert len(tail) == 1
    assert tail[0].stage == "tail_extreme"
    assert tail[0].allocation_usdt == 1_000

    tail[0].status = "filled"
    tail[0].updated_at = int(time.time())
    runtime.opportunities[tail[0].opportunity_id] = tail[0]
    runtime.tail_mode = "extreme"
    second = build_tail_opportunities(
        facts, runtime, 2_000, 0,
        capitulation_confirmed=True,
        weekly_reclaim_confirmed=False,
    )
    assert len(second) == 1
    assert second[0].status == "observing"
    assert "极端尾部下一档价格间距不足" in second[0].blocked_by


def test_swing_position_is_capped_by_200_usdt_risk():
    facts = _facts()
    facts.scores = EvidenceScore(valuation=50, capital_flow=60, acceptance=90)
    swing = build_swing_opportunity(
        facts,
        SpotAccumulationRuntimeState(),
        4_000,
        0,
        support_price=49_500,
        stop_price=47_500,
        target_price=55_000,
        has_open_position=False,
    )
    assert len(swing) == 1
    assert swing[0].status == "eligible"
    assert swing[0].allocation_usdt == pytest.approx(4_000)
    assert swing[0].expected_rr == pytest.approx(2.0)


def test_observing_swing_id_is_stable_when_support_price_moves():
    facts = _facts()
    facts.scores = EvidenceScore(valuation=50, capital_flow=40, acceptance=20)
    runtime = SpotAccumulationRuntimeState()
    first = build_swing_opportunity(
        facts, runtime, 4_000, 0,
        support_price=49_500, stop_price=47_500, target_price=55_000,
        has_open_position=False,
    )[0]
    second = build_swing_opportunity(
        facts, runtime, 4_000, 0,
        support_price=49_000, stop_price=47_000, target_price=55_000,
        has_open_position=False,
    )[0]
    assert first.status == second.status == "observing"
    assert first.opportunity_id == second.opportunity_id
    assert first.price_zone_low != second.price_zone_low
