from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from models.trend_monitor import (
    ActiveFlowSnapshot, DataQuality, ExchangeTransferFlowSnapshot,
    ExchangeTransferPoint, ExchangeTransferWindow, FlowWindow, TimeframeTrend,
    TrendEvent, TrendMachineContext, TrendSnapshot, WalletChartPoint,
    WalletFlowSnapshot,
)
from ai.trend_reviewer import TrendAIReviewer
from notifications.trend_alert import build_events
from processors.trend_monitor import (
    build_funding_snapshot, calculate_timeframe, parse_closed_klines,
    parse_cvd_deltas, parse_etf_flow, parse_exchange_transfer_flow,
    parse_wallet_flow, _oi_component,
)
from processors.trend_service import TrendService
from sources.coinglass import CoinglassSource, PriorityRateLimiter
from sources.looknode import LooknodeExchangeFlowSource
from storage.trend_store import TrendStore


def test_closed_klines_drop_open_bar_sort_and_dedupe():
    now = 2_000_000
    row = lambda ts, close, value: [ts, value, value + 2, value - 2, value + 1, 10, close]
    result = parse_closed_klines([
        row(1000, 1500, 100), row(500, 900, 90), row(1000, 1400, 101),
        row(1800, now + 1, 110),
    ], now_ms=now)
    assert [x["ts"] for x in result] == [500, 1000]
    assert result[-1]["open"] == 101


def test_cvd_is_rebuilt_from_buy_minus_sell_not_upstream_cumulative():
    now = 10_000
    raw = [
        {"time": 1000, "agg_taker_buy_vol": 10, "agg_taker_sell_vol": 4, "cum_vol_delta": -999999},
        {"time": 1300, "agg_taker_buy_vol": 1, "agg_taker_sell_vol": 5, "cum_vol_delta": 999999},
    ]
    rows = parse_cvd_deltas(raw, 300, now)
    assert [x["delta"] for x in rows] == [6, -4]
    assert [x["cvd_local"] for x in rows] == [6, 2]


def test_taker_history_contract_fields_feed_alert_baseline():
    rows = parse_cvd_deltas([
        {"time": 1000, "aggregated_buy_volume_usd": 12,
         "aggregated_sell_volume_usd": 5},
    ], 300, now_sec=2000)
    assert rows[0]["delta"] == 7


def test_missing_core_data_is_invalid_and_secondary_data_cannot_fill_it():
    result = calculate_timeframe("4h", [], [], [], [], now_sec=10_000)
    assert result.direction == "invalid"
    assert result.quality.valid is False


def test_stale_cvd_or_oi_hard_fails_timeframe():
    now = 100_000
    bars = [
        {"ts": (now - (40 - idx) * 14_400) * 1000, "open": 100, "high": 102,
         "low": 99, "close": 101, "volume": 10,
         "close_ts": (now - (39 - idx) * 14_400) * 1000 - 1}
        for idx in range(40)
    ]
    stale_flow = [{"ts": now - 50_000, "buy": 10, "sell": 5, "delta": 5},
                  {"ts": now - 46_400, "buy": 10, "sell": 5, "delta": 5}]
    stale_oi = [{"ts": now - 50_000, "close": 100}, {"ts": now - 46_400, "close": 101}]
    result = calculate_timeframe("4h", bars, stale_flow, stale_flow, stale_oi, now_sec=now)
    assert result.direction == "invalid"
    assert "过期" in result.quality.reason


def test_daily_closed_sources_remain_fresh_during_current_day():
    day_end = 1_700_006_400  # 整点日界仅用于构造一致窗口
    day_end -= day_end % 86400
    now = day_end + 5 * 3600
    bars = []
    for idx in range(30):
        start = day_end - (30 - idx) * 86400
        bars.append({
            "ts": start * 1000, "open": 100, "high": 102, "low": 99,
            "close": 101, "volume": 10, "close_ts": (start + 86400) * 1000 - 1,
        })
    start = day_end - 86400
    flows = [
        {"ts": start + idx * 3600, "buy": 10, "sell": 5, "delta": 5}
        for idx in range(24)
    ]
    oi = [
        {"ts": start - 3600 + idx * 3600, "close": 1000 + idx}
        for idx in range(25)
    ]
    result = calculate_timeframe("1d", bars, flows, flows, oi, now_sec=now)
    assert result.quality.valid is True


def test_funding_modifier_is_bounded_and_never_contains_direction():
    history = [{"close": "0.02"}] * 89 + [{"close": "0.04"}]
    result = build_funding_snapshot(0.0002, 0.00022, history, 0.2, True)
    assert -8 <= result.confidence_modifier <= 3
    assert not hasattr(result, "direction")
    assert result.oi_weighted_rate == pytest.approx(0.0004)


def test_wallet_daily_windows_filter_inactive_and_crosscheck():
    now = int(time.time())
    balance_list = [
        {"exchange_name": "Binance", "balance": 1000, "change_1d": 100, "change_7d": 200, "change_30d": 300},
        {"exchange_name": "Kraken", "balance": 500, "change_1d": 50, "change_7d": 100, "change_30d": 150},
        {"exchange_name": "FTX", "balance": 99999, "change_1d": 99999, "change_7d": 99999},
    ]
    times = [now - (7 - idx) * 86400 for idx in range(8)]
    chart = {
        "time_list": [x * 1000 for x in times],
        "price_list": [60_000 + idx * 100 for idx in range(8)],
        "data_list": [
            {"Binance": 900 + idx * 10, "Kraken": 450 + idx * 5, "FTX": 99999}
            for idx in range(8)
        ],
    }
    result = parse_wallet_flow(balance_list, chart, now)
    assert result.total_balance_btc == 1500
    assert {x.exchange for x in result.contributions} == {"Binance", "Kraken"}
    assert result.change_3d_btc == pytest.approx(45)
    assert set(result.exchange_charts) == {"Binance", "Kraken"}
    # list 7d positive and chart 7d positive => crosscheck passes
    assert result.direction_consistent is True


def test_wallet_chart_accepts_documented_data_map_shape():
    now = int(time.time())
    times = [(now - (3 - idx) * 86400) * 1000 for idx in range(4)]
    result = parse_wallet_flow(
        [{"exchange_name": "Binance", "balance": 1030, "change_1d": 10,
          "change_7d": 70, "change_30d": 100}],
        {"time_list": times, "price_list": [1, 2, 3, 4],
         "data_map": {"Binance": [1000, 1010, 1020, 1030],
                      "Coinbase Pro": [500, 501, 502, 503], "FTX": [9, 9, 9, 9]}},
        now,
    )
    assert len(result.chart) == 4
    assert result.change_3d_btc == 33
    assert set(result.exchange_charts) == {"Binance", "Coinbase"}


def test_wallet_modifier_uses_majority_direction_not_dissenting_3d_window():
    now = int(time.time())
    deltas = [2000, 2000, 2000, 1000, -1000, -1000, -1000]
    balances = [100_000.0]
    for delta in deltas:
        balances.append(balances[-1] + delta)
    times = [now - (7 - idx) * 86400 for idx in range(8)]
    result = parse_wallet_flow(
        [{"exchange_name": "Binance", "balance": balances[-1],
          "change_1d": 1000, "change_7d": sum(deltas), "change_30d": 5000}],
        {"time_list": [ts * 1000 for ts in times], "price_list": [60_000] * 8,
         "data_map": {"Binance": balances}},
        now,
    )
    assert result.change_3d_btc == -3000
    assert result.change_7d_btc == 4000
    assert result.direction_consistent is True
    assert result.confidence_modifier < 0  # majority inflow is bearish market bias


def _looknode_raw(days: int = 400, *, latest_age_hours: int = 30):
    now = int(time.time())
    latest = (now - latest_age_hours * 3600) // 86400 * 86400
    times = [latest - (days - 1 - idx) * 86400 for idx in range(days)]
    return {
        "inflow": [{"t": ts * 1000, "v": 1000 + idx} for idx, ts in enumerate(times)],
        "outflow": [{"t": ts * 1000, "v": 900 + idx / 2} for idx, ts in enumerate(times)],
        "fetched_at": now,
    }


def test_looknode_daily_flow_contract_and_windows():
    result = parse_exchange_transfer_flow(_looknode_raw(), int(time.time()))
    assert result.quality.valid is True
    assert result.quality.points == 400
    assert [window.window for window in result.windows] == ["1d", "3d", "7d", "30d"]
    assert len(result.chart) == 120
    assert result.windows[0].netflow_btc > 0
    assert result.score_weight == 0


def test_looknode_multiday_percentile_excludes_entire_current_window():
    raw = _looknode_raw()
    for row in raw["inflow"]:
        row["v"] = 1000.0
    for row in raw["outflow"]:
        row["v"] = 1000.0
    # The current 7d sum is zero, but six current days are individually large.
    # If the baseline overlaps the current window, its percentile falls below
    # P100 even though every fully historical seven-day sum is zero.
    for row, delta in zip(raw["inflow"][-7:], [100, -100, 100, -100, 100, -100, 0]):
        row["v"] += delta
    result = parse_exchange_transfer_flow(raw, int(time.time()))
    seven_day = next(window for window in result.windows if window.window == "7d")
    assert seven_day.netflow_btc == 0
    assert seven_day.abs_net_percentile_365d == 100.0


def test_looknode_misaligned_dates_fail_quality_gate():
    raw = _looknode_raw()
    raw["outflow"] = raw["outflow"][:-1]
    result = parse_exchange_transfer_flow(raw, int(time.time()))
    assert result.quality.valid is False
    assert "日期不完全对齐" in result.quality.reason


def test_looknode_ignores_ancient_rounding_artifact_but_rejects_recent_negative():
    raw = _looknode_raw(800)
    raw["outflow"][0]["v"] = -0.0001
    assert parse_exchange_transfer_flow(raw, int(time.time())).quality.valid is True
    raw["outflow"][-1]["v"] = -0.0001
    result = parse_exchange_transfer_flow(raw, int(time.time()))
    assert result.quality.valid is False
    assert "日期不完全对齐" in result.quality.reason or "非法" in result.quality.reason


def test_looknode_material_conflict_only_zeroes_wallet_modifier(tmp_path):
    settings = SimpleNamespace(
        trend_monitor=SimpleNamespace(
            enabled=True, evaluation_interval_sec=300, data_dir=str(tmp_path),
            algorithm_version="test", email_enabled=False, footprint_enabled=False,
        ),
        looknode=SimpleNamespace(
            crosscheck_abs_percentile=75, crosscheck_min_net_ratio=0.03,
        ),
        notifications=SimpleNamespace(email=SimpleNamespace(enabled=False)),
    )
    service = TrendService(coinglass=object(), binance=object(), settings=settings)
    chart = [
        WalletChartPoint(ts=idx * 86400, balance_btc=100_000 + idx,
                         net_change_btc=1.0)
        for idx in range(380)
    ]
    for idx in range(373, 380):
        chart[idx].net_change_btc = 100.0
    wallet = WalletFlowSnapshot(
        change_7d_btc=700, chart=chart,
        confidence_modifier=-3,
        quality=DataQuality(valid=True, status="fresh", points=380),
    )
    transfer = ExchangeTransferFlowSnapshot(
        windows=[ExchangeTransferWindow(
            window="7d", inflow_btc=900, outflow_btc=1100, netflow_btc=-200,
            net_ratio=-0.1, abs_net_percentile_365d=90,
        )],
        quality=DataQuality(valid=True, status="fresh", points=400),
    )
    assert service._apply_wallet_crosscheck(wallet, transfer, 3.0) == 0
    assert transfer.cross_source_status == "conflict"

    aligned = transfer.model_copy(deep=True)
    aligned.windows[0].netflow_btc = 200
    aligned.windows[0].net_ratio = 0.1
    assert service._apply_wallet_crosscheck(wallet, aligned, 3.0) == 3.0
    assert aligned.cross_source_status == "confirmed"

    stale = transfer.model_copy(deep=True)
    stale.quality = DataQuality(valid=False, status="stale", points=400)
    assert service._apply_wallet_crosscheck(wallet, stale, 3.0) == 3.0
    assert stale.cross_source_status == "unavailable"
    service.store.close()


def _tf(tf: str, score: float, spot: bool = True) -> TimeframeTrend:
    return TimeframeTrend(
        timeframe=tf, score=score,
        direction="bullish" if score > 0 else "bearish",
        spot_confirms=spot, quality=DataQuality(valid=True, points=50),
    )


def test_confirmation_counts_only_distinct_closed_5m_bars(tmp_path):
    settings = SimpleNamespace(
        trend_monitor=SimpleNamespace(
            enabled=True, evaluation_interval_sec=300, data_dir=str(tmp_path),
            algorithm_version="test", email_enabled=False, footprint_enabled=False,
        ),
        notifications=SimpleNamespace(email=SimpleNamespace(enabled=False)),
    )
    service = TrendService(coinglass=object(), binance=object(), settings=settings)
    assert service._advance_state("bullish", True, 300) == ("bullish_candidate", 1)
    assert service._advance_state("bullish", True, 300) == ("bullish_candidate", 1)
    assert service._advance_state("bullish", True, 600) == ("bullish_candidate", 2)
    assert service._advance_state("bullish", True, 900) == ("bullish_confirmed", 3)
    service.store.close()


def test_store_outbox_dedup_and_snapshot_tables(tmp_path):
    store = TrendStore(str(tmp_path))
    assert store.enqueue_email("same", "subject", "body") is True
    assert store.enqueue_email("same", "subject", "body") is False
    assert len(store.due_outbox()) == 1
    snapshot = TrendSnapshot(
        ts=1000, closed_5m_ts=900, algorithm_version="test", state="range",
        direction="range", core_score=0, confidence=0,
    )
    snapshot.wallet_flow.chart = [
        WalletChartPoint(ts=100, balance_btc=123.0, net_change_btc=1.0)
    ]
    snapshot.exchange_transfer_flow.chart = [
        ExchangeTransferPoint(ts=100, inflow_btc=10, outflow_btc=8, netflow_btc=2)
    ]
    store.save_snapshot(snapshot)
    assert store.latest_snapshot().wallet_flow.chart[0].balance_btc == 123.0
    assert store.latest_snapshot().exchange_transfer_flow.chart[0].netflow_btc == 2
    assert store.history(1)[0]["wallet_flow"]["chart"] == []
    assert store.history(1)[0]["exchange_transfer_flow"]["chart"] == []
    store.upsert_exchange_transfer_flows(
        [(100, 10.0, 8.0, 2.0), (200, 9.0, 11.0, -2.0)], fetched_at_ts=300,
    )
    persisted = store.load_exchange_transfer_flows()
    assert persisted is not None
    assert persisted["inflow"] == [{"t": 100000, "v": 10.0}, {"t": 200000, "v": 9.0}]
    assert persisted["outflow"][-1] == {"t": 200000, "v": 11.0}
    assert persisted["fetched_at"] == 300
    store.close()


def test_initial_range_does_not_generate_email_event(tmp_path):
    store = TrendStore(str(tmp_path))
    snapshot = TrendSnapshot(
        ts=1000, closed_5m_ts=900, algorithm_version="test", state="range",
        direction="range", core_score=0, confidence=0,
    )
    assert build_events(snapshot, None, store) == []
    store.close()


@pytest.mark.asyncio
async def test_coinglass_singleflight_merges_same_full_cache_key(monkeypatch, tmp_path):
    source = CoinglassSource("https://example.invalid", "key", rate_per_min=10)
    source._cache = {}
    calls = 0

    async def fake_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.02)
        return {"ok": True}

    monkeypatch.setattr(source, "_request_once", fake_once)
    one, two = await asyncio.gather(
        source._request("/api/test", {"symbol": "BTC"}),
        source._request("/api/test", {"symbol": "BTC"}),
    )
    assert one == two == {"ok": True}
    assert calls == 1
    await source.close()


@pytest.mark.asyncio
async def test_looknode_six_hour_cache_avoids_duplicate_pair_fetch(monkeypatch):
    source = LooknodeExchangeFlowSource(SimpleNamespace(
        timeout_sec=1, base_url="https://example.invalid", cache_ttl_sec=21600,
    ))
    calls = 0

    async def fake_pair():
        nonlocal calls
        calls += 1
        return _looknode_raw()

    monkeypatch.setattr(source, "_fetch_pair_once", fake_pair)
    first = await source.fetch_exchange_flows()
    second = await source.fetch_exchange_flows()
    assert first == second
    assert calls == 1
    await source.close()


def test_oi_usd_price_mechanical_increase_is_removed():
    bars = [{"close": 100, "volume": 10}, {"close": 110, "volume": 10}]
    # USD OI上涨10%，但除以同期价格后持仓币数量不变。
    score, reason = _oi_component(
        [{"ts": 1, "close": 1000}, {"ts": 2, "close": 1100}], bars,
    )
    assert score == 0
    assert "OI下降" not in reason


def test_funding_modifier_is_direction_aware_for_long_crowding():
    history = [{"time": idx * 28_800_000, "close": "0.02"} for idx in range(89)]
    history.append({"time": 89 * 28_800_000, "close": "0.04"})
    bullish = build_funding_snapshot(
        0.0002, 0.00022, history, 0.2, True, True, "bullish", now_sec=2_600_000,
    )
    bearish = build_funding_snapshot(
        0.0002, 0.00022, history, 0.2, True, True, "bearish", now_sec=2_600_000,
    )
    assert bullish.crowding == "long_crowded"
    assert bullish.confidence_modifier == -8
    assert bearish.confidence_modifier == 3


def test_etf_stale_and_placeholder_zero_do_not_modify_direction():
    start = 1_700_000_000
    now = start + 20 * 86400
    raw = [
        {"timestamp": start * 1000, "flow_usd": 100, "etf_flows": [{"flow_usd": 100}]},
        {"timestamp": (start + 86400) * 1000, "flow_usd": 0, "etf_flows": [{}]},
    ]
    result = parse_etf_flow(raw, "bullish", now_sec=now)
    assert result.quality.valid is False
    assert result.quality.status == "stale"
    assert result.confidence_modifier == 0
    assert result.quality.points == 1


class _AlwaysExtremeStore:
    def record_flow(self, *args):
        raise AssertionError("rolling NetFlow must not overwrite closed baseline")

    def flow_abs_percentile(self, *args, **kwargs):
        return 99.9


def _flow_snapshot(ts: int, spot_window: str = "1h", futures_window: str = "1h") -> TrendSnapshot:
    snapshot = TrendSnapshot(
        ts=ts, closed_5m_ts=(ts // 300) * 300 - 300, algorithm_version="test",
        state="bullish_watch", direction="bullish", core_score=30, confidence=30,
    )
    def flow(market: str, window: str) -> ActiveFlowSnapshot:
        return ActiveFlowSnapshot(
            market=market, semantics="", quality=DataQuality(valid=True),
            windows=[FlowWindow(window=window, buy_usd=120, sell_usd=80,
                                net_usd=40, net_ratio=0.2)],
        )
    snapshot.active_flows = {
        "spot": flow("spot", spot_window), "futures": flow("futures", futures_window),
    }
    return snapshot


def test_active_flow_dedup_uses_window_end_and_resonance_requires_same_window():
    base = 10 * 3600 + 600
    first = build_events(_flow_snapshot(base), None, _AlwaysExtremeStore())
    second = build_events(_flow_snapshot(base + 300), None, _AlwaysExtremeStore())
    first_keys = {event.event_type: event.dedup_key for event in first}
    second_keys = {event.event_type: event.dedup_key for event in second}
    assert first_keys["spot_active_flow_extreme"] == second_keys["spot_active_flow_extreme"]
    assert first_keys["cross_market_flow_resonance"] == second_keys["cross_market_flow_resonance"]

    mixed = build_events(
        _flow_snapshot(base, spot_window="1h", futures_window="24h"),
        None, _AlwaysExtremeStore(),
    )
    assert not any(event.event_type == "cross_market_flow_resonance" for event in mixed)


def test_hourly_flow_percentile_uses_closed_window_boundary(tmp_path):
    store = TrendStore(str(tmp_path))
    end_ts = 2_000_000 - (2_000_000 % 3600)
    store.record_flows("spot", "1h", [
        (end_ts - (720 - idx) * 3600, float(idx + 1), 1000.0)
        for idx in range(720)
    ])
    assert store.flow_abs_percentile(
        "spot", "1h", 500.0, 30, min_samples=720, as_of_ts=end_ts,
    ) is not None
    store.close()


def test_pending_auxiliary_source_does_not_emit_false_recovery(tmp_path):
    store = TrendStore(str(tmp_path))
    previous = TrendSnapshot(
        ts=1000, closed_5m_ts=900, algorithm_version="test", state="range",
        direction="range", core_score=0, confidence=0,
    )
    current = previous.model_copy(deep=True)
    current.ts = 1300
    current.closed_5m_ts = 1200
    previous.active_flows["spot"] = ActiveFlowSnapshot(
        market="spot", semantics="",
        quality=DataQuality(valid=False, status="pending", reason="启动加载"),
    )
    current.active_flows["spot"] = ActiveFlowSnapshot(
        market="spot", semantics="",
        quality=DataQuality(valid=True, status="fresh", points=5),
    )
    assert not any(
        event.event_type == "data_source_recovered"
        for event in build_events(current, previous, store)
    )
    store.close()


def test_source_recovery_requires_persisted_invalid_transition(tmp_path):
    store = TrendStore(str(tmp_path))
    valid = TrendSnapshot(
        ts=1000, closed_5m_ts=900, algorithm_version="test", state="range",
        direction="range", core_score=0, confidence=0,
    )
    invalid = valid.model_copy(deep=True)
    invalid.ts, invalid.closed_5m_ts = 1300, 1200
    valid.active_flows["spot"] = ActiveFlowSnapshot(
        market="spot", semantics="", quality=DataQuality(valid=True, status="fresh"),
    )
    invalid.active_flows["spot"] = ActiveFlowSnapshot(
        market="spot", semantics="",
        quality=DataQuality(valid=False, status="stale", reason="超时"),
    )
    invalid_events = build_events(invalid, valid, store)
    source_invalid = next(
        event for event in invalid_events if event.event_type == "data_source_invalid"
    )
    assert store.add_event(source_invalid) is True
    recovered = valid.model_copy(deep=True)
    recovered.ts, recovered.closed_5m_ts = 1600, 1500
    recovery_events = build_events(recovered, invalid, store)
    assert any(event.event_type == "data_source_recovered" for event in recovery_events)
    store.close()


def test_looknode_high_turnover_alert_is_neutral_and_deduplicated(tmp_path):
    store = TrendStore(str(tmp_path))
    snapshot = TrendSnapshot(
        ts=2000, closed_5m_ts=1800, algorithm_version="test", state="range",
        direction="range", core_score=0, confidence=0,
    )
    snapshot.exchange_transfer_flow = ExchangeTransferFlowSnapshot(
        latest_date_ts=86400,
        windows=[ExchangeTransferWindow(
            window="1d", inflow_btc=30_000, outflow_btc=29_500,
            netflow_btc=500, net_ratio=0.0084,
            inflow_percentile_365d=99.5, outflow_percentile_365d=99.4,
        )],
        chart=[ExchangeTransferPoint(
            ts=86400, inflow_btc=30_000, outflow_btc=29_500, netflow_btc=500,
        )],
        quality=DataQuality(valid=True, status="fresh", points=365),
    )
    events = build_events(snapshot, None, store)
    turnover = [event for event in events if event.event_type == "looknode_exchange_high_turnover"]
    assert len(turnover) == 1
    assert "不作方向解读" in turnover[0].message
    store.close()


@pytest.mark.asyncio
async def test_trend_ai_402_opens_six_hour_circuit_breaker():
    reviewer = TrendAIReviewer(SimpleNamespace(
        model="test", timeout_sec=1, api_key="key", api_base="https://example.invalid",
    ))
    calls = 0

    class PaymentRequired(Exception):
        status_code = 402

    class FakeCompletions:
        async def create(self, **kwargs):
            nonlocal calls
            calls += 1
            raise PaymentRequired("no balance")

    reviewer._client = SimpleNamespace(
        chat=SimpleNamespace(completions=FakeCompletions()),
    )
    snapshot = TrendSnapshot(
        ts=1, closed_5m_ts=0, algorithm_version="test",
        state="bearish_watch", direction="bearish", core_score=-30, confidence=30,
    )
    first = await reviewer.review(snapshot)
    second = await reviewer.review(snapshot)
    assert first[0] == second[0] == "not_run"
    assert calls == 1
    assert "不影响原生算法" in second[1]
    assert "HTTP 402" in reviewer.suspension_message()


def test_machine_context_survives_weakening_restart(tmp_path):
    store = TrendStore(str(tmp_path))
    context = TrendMachineContext(confirmed_direction="bullish", last_counted_bar=900)
    store.save_machine_context(context, 1000)
    store.close()
    reopened = TrendStore(str(tmp_path))
    assert reopened.load_machine_context() == context
    reopened.close()


def test_ai_veto_cannot_commit_proposed_confirmation(tmp_path):
    settings = SimpleNamespace(
        trend_monitor=SimpleNamespace(
            enabled=True, evaluation_interval_sec=300, data_dir=str(tmp_path),
            algorithm_version="test", email_enabled=False, footprint_enabled=False,
            ai_review_enabled=False, confirmation_bars=3,
        ),
        notifications=SimpleNamespace(email=SimpleNamespace(enabled=False)),
    )
    service = TrendService(coinglass=object(), binance=object(), settings=settings)
    service._advance_state("bullish", True, 300)
    service._advance_state("bullish", True, 600)
    state, _, proposed = service._propose_state("bullish", True, 900)
    assert state == "bullish_confirmed" and proposed.confirmed_direction == "bullish"
    final = service._finalize_machine(proposed, "veto", "bullish_watch")
    assert final.confirmed_direction is None
    assert final.confirmation_count == 0
    service.store.close()


def test_event_and_outbox_are_committed_atomically(tmp_path):
    store = TrendStore(str(tmp_path))
    event = TrendEvent(
        ts=1, event_type="test", title="t", message="m", dedup_key="atomic",
    )
    assert store.persist_event_and_email(event, "subject", "html") is True
    assert store.persist_event_and_email(event, "subject", "html") is False
    claimed = store.claim_due_outbox()
    assert len(claimed) == 1 and claimed[0]["status"] == "sending"
    store.mark_outbox_failed(claimed[0]["id"], 1, "fail")
    store.close()


@pytest.mark.asyncio
async def test_priority_limiter_preserves_p1_p2_budget_under_p0_pressure(monkeypatch):
    import sources.coinglass as cg_module

    clock = [1000.0]
    real_sleep = asyncio.sleep

    async def virtual_sleep(delay):
        clock[0] += max(0.0, delay)
        await real_sleep(0)

    monkeypatch.setattr(cg_module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(cg_module.asyncio, "sleep", virtual_sleep)
    limiter = PriorityRateLimiter(10)
    released = []

    async def acquire(priority):
        await limiter.acquire(priority, f"/{priority}")
        released.append(priority)

    tasks = [acquire("P0") for _ in range(8)] + [acquire("P1") for _ in range(5)] + [acquire("P2") for _ in range(2)]
    await asyncio.gather(*tasks)
    first_window = released[:10]
    assert first_window.count("P0") >= 3
    assert first_window.count("P1") >= 5
    assert first_window.count("P2") >= 2


@pytest.mark.asyncio
async def test_disk_cache_does_not_resurrect_expired_market_data(tmp_path):
    source = CoinglassSource("https://example.invalid", "key")
    source._cache.clear()
    cache_file = tmp_path / "cache.json"
    cache_file.write_text(json.dumps({
        "expired": {"expire_ts": time.time() - 1, "data": {"stale": True}},
        "fresh": {"expire_ts": time.time() + 60, "data": {"fresh": True}},
    }))
    source._cache_file = str(cache_file)
    source._load_disk_cache()
    assert "expired" not in source._cache
    assert "fresh" in source._cache
    await source.close()
