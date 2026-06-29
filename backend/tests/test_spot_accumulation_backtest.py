from processors.spot_accumulation_backtest import BacktestPoint, run_backtest


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
    assert amounts[-1] == 4_000


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
    assert amounts == [1_500, 3_000, 4_500, 4_500, 6_000]
