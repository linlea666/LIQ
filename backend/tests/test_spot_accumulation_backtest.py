from models.spot_accumulation import SpotAccumulationConfig
from processors.spot_accumulation_backtest import (
    BacktestPoint,
    build_valuation_points,
    run_backtest,
)


def test_backtest_uses_every_drawdown_over_30_percent_without_overspending():
    day = 86400
    prices = [
        100, 95, 80, 69, 60, 75, 100,  # episode 1 recovers
        120, 100, 83, 70, 60, 90,       # episode 2 remains open
    ]
    points = [
        BacktestPoint(
            ts=index * 7 * day,
            price=price,
            valuation_score=min(95, 40 + (120 - price)),
        )
        for index, price in enumerate(prices)
    ]
    results = run_backtest(points)
    assert len(results) == 2
    assert all(item.episode.max_drawdown_pct >= 30 for item in results)
    for item in results:
        for result in item.strategies.values():
            assert result.invested_usdt <= 13_000
            assert result.ending_cash_usdt >= 0
            if result.btc_acquired:
                assert result.average_cost > 0


def test_fixed_ladder_buys_more_as_drawdown_deepens():
    points = [
        BacktestPoint(ts=0, price=100, valuation_score=20),
        BacktestPoint(ts=86400, price=69, valuation_score=60),
        BacktestPoint(ts=2 * 86400, price=54, valuation_score=70),
        BacktestPoint(ts=3 * 86400, price=44, valuation_score=80),
        BacktestPoint(ts=4 * 86400, price=29, valuation_score=95),
    ]
    result = run_backtest(points)[0].strategies["fixed_drawdown"]
    amounts = [trade.amount_usdt for trade in result.trades]
    assert sum(amounts) == 13_000
    # 深档倾斜后 capitulation（倒数第二档）最重，bottom_confirmed 降为兜底
    assert amounts == [1_000, 2_000, 3_000, 4_000, 3_000]


def test_backtest_scales_core_ladder_from_configured_total_capital():
    points = [
        BacktestPoint(ts=0, price=100, valuation_score=20),
        BacktestPoint(ts=86400, price=69, valuation_score=60),
        BacktestPoint(ts=2 * 86400, price=54, valuation_score=70),
        BacktestPoint(ts=3 * 86400, price=44, valuation_score=80),
        BacktestPoint(ts=4 * 86400, price=29, valuation_score=95),
    ]
    result = run_backtest(points, total_capital_usdt=30_000)[0].strategies["fixed_drawdown"]
    amounts = [trade.amount_usdt for trade in result.trades]
    assert sum(amounts) == 19_500
    assert amounts == [1_500, 3_000, 4_500, 6_000, 4_500]


def test_backtest_reads_non_default_bucket_and_stage_ratios_from_config():
    config = SpotAccumulationConfig(
        initial_capital_usdt=10_000,
        core_ratio=0.50,
        swing_ratio=0.30,
        tail_ratio=0.20,
        insurance_ratio=0.05,
        core_stage_ratios={
            "insurance": 0.05,
            "value_1": 0.08,
            "deep_value": 0.12,
            "capitulation": 0.10,
            "bottom_confirmed": 0.15,
        },
    )
    points = [
        BacktestPoint(ts=0, price=100, valuation_score=20),
        BacktestPoint(ts=86400, price=69, valuation_score=95),
        BacktestPoint(ts=2 * 86400, price=54, valuation_score=95),
        BacktestPoint(ts=3 * 86400, price=44, valuation_score=95),
        BacktestPoint(ts=4 * 86400, price=29, valuation_score=95),
    ]
    result = run_backtest(points, config=config)[0].strategies["fixed_drawdown"]
    assert [trade.amount_usdt for trade in result.trades] == [500, 800, 1_200, 1_000, 1_500]
    assert result.invested_usdt == 5_000


def test_backtest_applies_fee_and_slippage_without_overspending():
    points = [
        BacktestPoint(ts=0, price=100, valuation_score=20),
        BacktestPoint(ts=86400, price=60, valuation_score=95),
    ]
    result = run_backtest(points, fee_bps=10, slippage_bps=20)[0].strategies["fixed_drawdown"]
    assert result.fees_usdt > 0
    assert result.slippage_usdt > 0
    assert result.btc_acquired < result.invested_usdt / 60
    assert result.average_cost > 60
    assert result.invested_usdt + result.ending_cash_usdt == 13_000


def test_bottom_confirmation_uses_elapsed_time_not_point_count():
    day = 86400
    dense = [BacktestPoint(ts=0, price=100, valuation_score=20)]
    dense.extend(
        BacktestPoint(
            ts=day + index * 6 * 3600,
            price=60 if index == 0 else 72,
            valuation_score=95,
        )
        for index in range(21)
    )
    early = run_backtest(dense)[0].strategies["dynamic_valuation"]
    assert "bottom_confirmed" not in {trade.stage for trade in early.trades}

    after_twenty_days = dense + [
        BacktestPoint(ts=22 * day, price=72, valuation_score=95),
    ]
    mature = run_backtest(after_twenty_days)[0].strategies["dynamic_valuation"]
    assert "bottom_confirmed" in {trade.stage for trade in mature.trades}


def test_historical_builder_uses_only_past_ath_and_enforces_v_coverage():
    common = {
        "mvrv": 1.0,
        "ahr999": 0.6,
        "sma_200w": 70,
        "sth_cost": 90,
        "nupl": 0.1,
        "reserve_risk": 0.001,
        "puell": 0.7,
        "sth_sopr": 0.98,
        "sth_supply_change_30d_pct": -1,
    }
    points = build_valuation_points([
        {"ts": 1, "price": 100, **common},
        {"ts": 2, "price": 80, **common},
        {"ts": 3, "price": 120, "cycle_ath": 120, **common},
        {"ts": 4, "price": 60, "mvrv": 1.0},
    ])
    assert len(points) == 4
    assert points[1].valuation_available is True
    # 第二行只能看到此前100的ATH；未来第三行120不能反向扩大其回撤。
    assert points[1].valuation_score < points[3].valuation_score
    assert points[3].valuation_available is False
    assert points[3].valuation_coverage == 2


def test_historical_builder_ignores_unknown_fields_and_invalid_rows():
    points = build_valuation_points([
        {"ts": "bad", "price": 100},
        {"ts": 1, "price": float("nan")},
        {"ts": 2, "price": "80", "mvrv": "1.0", "unknown": "ignored"},
    ])
    assert len(points) == 1
    assert points[0].ts == 2
    assert points[0].price == 80
