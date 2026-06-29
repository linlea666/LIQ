"""现货抄底慢周期回测。

只验证可获得完整历史的日级估值层，不伪造ETF、订单簿或Footprint跨周期历史。
算法逐日推进，触发时只使用当日及以前数据。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Literal


StrategyName = Literal["dynamic_valuation", "fixed_drawdown", "weekly_dca"]


@dataclass(frozen=True)
class BacktestPoint:
    ts: int
    price: float
    valuation_score: float


@dataclass(frozen=True)
class DrawdownEpisode:
    peak_ts: int
    peak_price: float
    start_ts: int
    end_ts: int
    max_drawdown_pct: float
    points: tuple[BacktestPoint, ...]


@dataclass(frozen=True)
class BacktestTrade:
    ts: int
    price: float
    amount_usdt: float
    stage: str


@dataclass
class StrategyResult:
    strategy: StrategyName
    trades: list[BacktestTrade] = field(default_factory=list)
    invested_usdt: float = 0.0
    btc_acquired: float = 0.0
    average_cost: float = 0.0
    ending_cash_usdt: float = 0.0
    max_drawdown_from_cost_pct: float = 0.0


@dataclass
class EpisodeResult:
    episode: DrawdownEpisode
    strategies: dict[StrategyName, StrategyResult]


def identify_drawdown_episodes(
    points: Iterable[BacktestPoint],
    threshold_pct: float = 30.0,
) -> list[DrawdownEpisode]:
    ordered = sorted((point for point in points if point.price > 0), key=lambda point: point.ts)
    if not ordered:
        return []
    peak = ordered[0]
    active: list[BacktestPoint] = []
    start_ts = 0
    max_dd = 0.0
    episodes: list[DrawdownEpisode] = []
    for point in ordered[1:]:
        if point.price >= peak.price:
            if active:
                episodes.append(DrawdownEpisode(
                    peak_ts=peak.ts,
                    peak_price=peak.price,
                    start_ts=start_ts,
                    end_ts=point.ts,
                    max_drawdown_pct=max_dd,
                    points=tuple(active + [point]),
                ))
                active = []
                start_ts = 0
                max_dd = 0.0
            peak = point
            continue
        drawdown = (peak.price - point.price) / peak.price * 100
        if drawdown >= threshold_pct and not active:
            start_ts = point.ts
        if start_ts:
            active.append(point)
            max_dd = max(max_dd, drawdown)
    if active:
        episodes.append(DrawdownEpisode(
            peak_ts=peak.ts,
            peak_price=peak.price,
            start_ts=start_ts,
            end_ts=active[-1].ts,
            max_drawdown_pct=max_dd,
            points=tuple(active),
        ))
    return episodes


def run_backtest(
    points: Iterable[BacktestPoint],
    total_capital_usdt: float = 20_000.0,
) -> list[EpisodeResult]:
    if total_capital_usdt <= 0:
        raise ValueError("total_capital_usdt must be greater than 0")
    core_capital = total_capital_usdt * 0.65
    return [
        EpisodeResult(
            episode=episode,
            strategies={
                "dynamic_valuation": _simulate_dynamic(episode, total_capital_usdt),
                "fixed_drawdown": _simulate_fixed(episode, total_capital_usdt),
                "weekly_dca": _simulate_dca(episode, core_capital, total_capital_usdt),
            },
        )
        for episode in identify_drawdown_episodes(points)
    ]


def _simulate_dynamic(episode: DrawdownEpisode, total_capital: float) -> StrategyResult:
    # 与生产核心预算一致；历史层只能验证V，M/A留给前向验证。
    stages = [
        ("insurance", 55.0, total_capital * 0.05),
        ("value_1", 65.0, total_capital * 0.10),
        ("deep_value", 75.0, total_capital * 0.15),
        ("capitulation", 80.0, total_capital * 0.15),
    ]
    result = StrategyResult(strategy="dynamic_valuation", ending_cash_usdt=total_capital * 0.65)
    fired: set[str] = set()
    running_low = float("inf")
    last_buy_price = 0.0
    for index, point in enumerate(episode.points):
        running_low = min(running_low, point.price)
        for stage, threshold, amount in stages:
            if stage in fired or point.valuation_score < threshold:
                continue
            if last_buy_price and point.price > last_buy_price * 0.95:
                continue
            _buy(result, point, amount, stage)
            fired.add(stage)
            last_buy_price = point.price
            break
        # 无前视右侧确认代理：运行低点后上涨20%，且至少已有20个交易日观察。
        if (
            "bottom_confirmed" not in fired
            and index >= 20
            and point.price >= running_low * 1.20
            and point.valuation_score >= 60
        ):
            _buy(
                result,
                point,
                min(total_capital * 0.20, result.ending_cash_usdt),
                "bottom_confirmed",
            )
            fired.add("bottom_confirmed")
        _mark_drawdown(result, point.price)
    return _finish(result)


def _simulate_fixed(episode: DrawdownEpisode, total_capital: float) -> StrategyResult:
    ladder = [
        ("dd30", 30.0, total_capital * 0.05),
        ("dd45", 45.0, total_capital * 0.10),
        ("dd55", 55.0, total_capital * 0.15),
        ("dd65", 65.0, total_capital * 0.15),
        ("dd70", 70.0, total_capital * 0.20),
    ]
    result = StrategyResult(strategy="fixed_drawdown", ending_cash_usdt=total_capital * 0.65)
    fired: set[str] = set()
    for point in episode.points:
        dd = (episode.peak_price - point.price) / episode.peak_price * 100
        for stage, threshold, amount in ladder:
            if stage not in fired and dd >= threshold:
                _buy(result, point, amount, stage)
                fired.add(stage)
        _mark_drawdown(result, point.price)
    return _finish(result)


def _simulate_dca(
    episode: DrawdownEpisode,
    core_capital: float,
    total_capital: float,
) -> StrategyResult:
    result = StrategyResult(strategy="weekly_dca", ending_cash_usdt=core_capital)
    last_buy_ts = 0
    for point in episode.points:
        if result.ending_cash_usdt <= 0:
            break
        if not last_buy_ts or point.ts - last_buy_ts >= 7 * 86400:
            _buy(result, point, min(total_capital * 0.025, result.ending_cash_usdt), "weekly")
            last_buy_ts = point.ts
        _mark_drawdown(result, point.price)
    return _finish(result)


def _buy(result: StrategyResult, point: BacktestPoint, amount: float, stage: str) -> None:
    amount = max(0.0, min(amount, result.ending_cash_usdt))
    if amount <= 0 or point.price <= 0:
        return
    result.trades.append(BacktestTrade(point.ts, point.price, amount, stage))
    result.invested_usdt += amount
    result.btc_acquired += amount / point.price
    result.ending_cash_usdt -= amount
    result.average_cost = result.invested_usdt / result.btc_acquired


def _mark_drawdown(result: StrategyResult, price: float) -> None:
    if result.average_cost > 0 and price < result.average_cost:
        dd = (result.average_cost - price) / result.average_cost * 100
        result.max_drawdown_from_cost_pct = max(result.max_drawdown_from_cost_pct, dd)


def _finish(result: StrategyResult) -> StrategyResult:
    result.invested_usdt = round(result.invested_usdt, 2)
    result.btc_acquired = round(result.btc_acquired, 12)
    result.average_cost = round(result.average_cost, 2)
    result.ending_cash_usdt = round(result.ending_cash_usdt, 2)
    result.max_drawdown_from_cost_pct = round(result.max_drawdown_from_cost_pct, 2)
    return result
