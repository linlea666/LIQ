"""BTC现货抄底V层历史回测与慢周期输入构建。

只验证具有历史可得性的估值层。M/A层必须使用线上影子快照做前向验证，
不得把本模块结果描述为全栈策略回测。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Iterable, Literal, Optional

from models.spot_accumulation import (
    SpotAccumulationConfig,
    SpotAccumulationFacts,
    SpotMetricFact,
)
from processors.spot_accumulation import score_facts


StrategyName = Literal["dynamic_valuation", "fixed_drawdown", "weekly_dca"]
DAY_SECONDS = 86_400


@dataclass(frozen=True)
class BacktestPoint:
    ts: int
    price: float
    valuation_score: float
    daily_atr_pct: float = 0.0
    valuation_available: bool = True
    valuation_coverage: int = 10


@dataclass(frozen=True)
class ValuationHistoryRow:
    """真实日级价格与可获得慢周期估值序列的一行。"""

    ts: int
    price: float
    cycle_ath: Optional[float] = None
    mvrv: Optional[float] = None
    ahr999: Optional[float] = None
    sma_200w: Optional[float] = None
    sth_cost: Optional[float] = None
    nupl: Optional[float] = None
    reserve_risk: Optional[float] = None
    puell: Optional[float] = None
    sth_sopr: Optional[float] = None
    sth_supply_change_30d_pct: Optional[float] = None
    daily_atr_pct: float = 0.0


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
    execution_price: float = 0.0
    fee_usdt: float = 0.0
    slippage_usdt: float = 0.0


@dataclass
class StrategyResult:
    strategy: StrategyName
    trades: list[BacktestTrade] = field(default_factory=list)
    invested_usdt: float = 0.0
    btc_acquired: float = 0.0
    average_cost: float = 0.0
    ending_cash_usdt: float = 0.0
    max_drawdown_from_cost_pct: float = 0.0
    fees_usdt: float = 0.0
    slippage_usdt: float = 0.0


@dataclass
class EpisodeResult:
    episode: DrawdownEpisode
    strategies: dict[StrategyName, StrategyResult]
    capability: str = "valuation_layer_historical_backtest"
    label: str = "V层历史回测"


def _finite(value: Any) -> Optional[float]:
    try:
        parsed = float(value) if value is not None else None
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed is not None and math.isfinite(parsed) else None


def build_valuation_points(
    rows: Iterable[ValuationHistoryRow | dict[str, Any]],
    *,
    minimum_fresh_metrics: int = 6,
) -> list[BacktestPoint]:
    """用逐日可得数据构建V层输入；ATH只使用当日及以前数据。"""
    normalized: list[ValuationHistoryRow] = []
    for raw in rows:
        if isinstance(raw, ValuationHistoryRow):
            row = raw
        elif isinstance(raw, dict):
            try:
                ts = int(raw.get("ts") or 0)
            except (TypeError, ValueError, OverflowError):
                continue
            price = _finite(raw.get("price"))
            if ts <= 0 or price is None or price <= 0:
                continue
            row = ValuationHistoryRow(
                ts=ts,
                price=price,
                cycle_ath=_finite(raw.get("cycle_ath")),
                mvrv=_finite(raw.get("mvrv")),
                ahr999=_finite(raw.get("ahr999")),
                sma_200w=_finite(raw.get("sma_200w")),
                sth_cost=_finite(raw.get("sth_cost")),
                nupl=_finite(raw.get("nupl")),
                reserve_risk=_finite(raw.get("reserve_risk")),
                puell=_finite(raw.get("puell")),
                sth_sopr=_finite(raw.get("sth_sopr")),
                sth_supply_change_30d_pct=_finite(raw.get("sth_supply_change_30d_pct")),
                daily_atr_pct=_finite(raw.get("daily_atr_pct")) or 0.0,
            )
        else:
            continue
        if row.ts <= 0 or _finite(row.price) is None or row.price <= 0:
            continue
        normalized.append(row)
    normalized.sort(key=lambda item: item.ts)

    output: list[BacktestPoint] = []
    running_ath = 0.0
    for row in normalized:
        explicit_ath = _finite(row.cycle_ath) or 0.0
        running_ath = max(running_ath, row.price, explicit_ath)
        drawdown = max(0.0, (running_ath - row.price) / running_ath * 100)
        valuation_inputs = {
            "mvrv": _finite(row.mvrv),
            "ahr999": _finite(row.ahr999),
            "price_vs_200w": (
                row.price / row.sma_200w
                if _finite(row.sma_200w) and float(row.sma_200w) > 0 else None
            ),
            "price_vs_sth": (
                row.price / row.sth_cost
                if _finite(row.sth_cost) and float(row.sth_cost) > 0 else None
            ),
            "nupl": _finite(row.nupl),
            "reserve_risk": _finite(row.reserve_risk),
            "puell": _finite(row.puell),
            "sth_sopr": _finite(row.sth_sopr),
            "sth_supply_change_30d_pct": _finite(row.sth_supply_change_30d_pct),
        }
        values = {"drawdown_pct": drawdown, **valuation_inputs}
        coverage = sum(value is not None for value in values.values())
        metric_facts = {
            name: SpotMetricFact(
                value=value,
                source_timestamp=row.ts,
                freshness="fresh" if value is not None else "missing",
                parse_status="ok" if value is not None else "missing",
                included_in_score=value is not None,
                source="historical_daily_series",
            )
            for name, value in values.items()
        }
        facts = SpotAccumulationFacts(
            timestamp=row.ts,
            price=row.price,
            cycle_ath=running_ath,
            drawdown_pct=drawdown,
            valuation_inputs=valuation_inputs,
            metric_facts=metric_facts,
        )
        score = score_facts(facts).valuation
        output.append(BacktestPoint(
            ts=row.ts,
            price=row.price,
            valuation_score=score,
            daily_atr_pct=max(0.0, _finite(row.daily_atr_pct) or 0.0),
            valuation_available=coverage >= minimum_fresh_metrics,
            valuation_coverage=coverage,
        ))
    return output


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
                    peak_ts=peak.ts, peak_price=peak.price, start_ts=start_ts,
                    end_ts=point.ts, max_drawdown_pct=max_dd,
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
            peak_ts=peak.ts, peak_price=peak.price, start_ts=start_ts,
            end_ts=active[-1].ts, max_drawdown_pct=max_dd, points=tuple(active),
        ))
    return episodes


def run_backtest(
    points: Iterable[BacktestPoint],
    total_capital_usdt: Optional[float] = None,
    *,
    config: Optional[SpotAccumulationConfig] = None,
    fee_bps: float = 10.0,
    slippage_bps: float = 5.0,
) -> list[EpisodeResult]:
    """运行配置驱动的V层回测；保留total_capital_usdt旧调用兼容。"""
    cfg = (config or SpotAccumulationConfig()).model_copy(deep=True)
    if total_capital_usdt is not None:
        cfg.initial_capital_usdt = total_capital_usdt
        cfg = SpotAccumulationConfig.model_validate(cfg.model_dump())
    if cfg.initial_capital_usdt <= 0:
        raise ValueError("total capital must be greater than 0")
    if not math.isfinite(fee_bps) or not math.isfinite(slippage_bps):
        raise ValueError("fee_bps and slippage_bps must be finite")
    if fee_bps < 0 or slippage_bps < 0:
        raise ValueError("fee_bps and slippage_bps cannot be negative")
    return [
        EpisodeResult(
            episode=episode,
            strategies={
                "dynamic_valuation": _simulate_dynamic(episode, cfg, fee_bps, slippage_bps),
                "fixed_drawdown": _simulate_fixed(episode, cfg, fee_bps, slippage_bps),
                "weekly_dca": _simulate_dca(episode, cfg, fee_bps, slippage_bps),
            },
        )
        for episode in identify_drawdown_episodes(points)
    ]


def _simulate_dynamic(
    episode: DrawdownEpisode,
    config: SpotAccumulationConfig,
    fee_bps: float,
    slippage_bps: float,
) -> StrategyResult:
    allocations = config.core_stage_allocations()
    stages = ["insurance", "value_1", "deep_value", "capitulation"]
    result = StrategyResult(
        strategy="dynamic_valuation", ending_cash_usdt=config.core_budget_usdt,
    )
    fired: set[str] = set()
    running_low = float("inf")
    running_low_ts = 0
    last_buy_price = 0.0
    for point in episode.points:
        if point.price < running_low:
            running_low = point.price
            running_low_ts = point.ts
        for stage in stages:
            threshold = config.core_thresholds[stage]["v"]
            if stage in fired or not point.valuation_available or point.valuation_score < threshold:
                continue
            required_gap_pct = max(
                config.min_price_gap_ratio * 100,
                config.atr_gap_multiplier * point.daily_atr_pct,
            )
            if last_buy_price:
                gap_pct = (last_buy_price - point.price) / last_buy_price * 100
                if point.price >= last_buy_price or gap_pct < required_gap_pct:
                    continue
            _buy(result, point, allocations[stage], stage, fee_bps, slippage_bps)
            fired.add(stage)
            last_buy_price = point.price
            break
        # 右侧代理只使用运行低点之后真实经过的20日，不再用数组下标。
        if (
            "bottom_confirmed" not in fired
            and running_low_ts > 0
            and point.ts - running_low_ts >= 20 * DAY_SECONDS
            and point.price >= running_low * 1.20
            and point.valuation_available
            and point.valuation_score >= config.core_thresholds["bottom_confirmed"]["v"]
        ):
            _buy(
                result, point, allocations["bottom_confirmed"], "bottom_confirmed",
                fee_bps, slippage_bps,
            )
            fired.add("bottom_confirmed")
        _mark_drawdown(result, point.price)
    return _finish(result)


def _simulate_fixed(
    episode: DrawdownEpisode,
    config: SpotAccumulationConfig,
    fee_bps: float,
    slippage_bps: float,
) -> StrategyResult:
    amounts = list(config.core_stage_allocations().values())
    ladder = list(zip(("dd30", "dd45", "dd55", "dd65", "dd70"), (30, 45, 55, 65, 70), amounts))
    result = StrategyResult(strategy="fixed_drawdown", ending_cash_usdt=config.core_budget_usdt)
    fired: set[str] = set()
    for point in episode.points:
        dd = (episode.peak_price - point.price) / episode.peak_price * 100
        for stage, threshold, amount in ladder:
            if stage not in fired and dd >= threshold:
                _buy(result, point, amount, stage, fee_bps, slippage_bps)
                fired.add(stage)
        _mark_drawdown(result, point.price)
    return _finish(result)


def _simulate_dca(
    episode: DrawdownEpisode,
    config: SpotAccumulationConfig,
    fee_bps: float,
    slippage_bps: float,
) -> StrategyResult:
    result = StrategyResult(strategy="weekly_dca", ending_cash_usdt=config.core_budget_usdt)
    last_buy_ts = 0
    weekly_amount = config.core_budget_usdt / 26.0
    for point in episode.points:
        if result.ending_cash_usdt <= 0:
            break
        if not last_buy_ts or point.ts - last_buy_ts >= 7 * DAY_SECONDS:
            _buy(
                result, point, min(weekly_amount, result.ending_cash_usdt), "weekly",
                fee_bps, slippage_bps,
            )
            last_buy_ts = point.ts
        _mark_drawdown(result, point.price)
    return _finish(result)


def _buy(
    result: StrategyResult,
    point: BacktestPoint,
    amount: float,
    stage: str,
    fee_bps: float,
    slippage_bps: float,
) -> None:
    amount = max(0.0, min(amount, result.ending_cash_usdt))
    if amount <= 0 or point.price <= 0:
        return
    fee_rate = fee_bps / 10_000.0
    execution_price = point.price * (1 + slippage_bps / 10_000.0)
    quote_before_fee = amount / (1 + fee_rate)
    fee = amount - quote_before_fee
    quantity = quote_before_fee / execution_price
    slippage_cost = max(0.0, quote_before_fee - quantity * point.price)
    result.trades.append(BacktestTrade(
        point.ts, point.price, amount, stage,
        execution_price=execution_price, fee_usdt=fee, slippage_usdt=slippage_cost,
    ))
    result.invested_usdt += amount
    result.btc_acquired += quantity
    result.ending_cash_usdt -= amount
    result.fees_usdt += fee
    result.slippage_usdt += slippage_cost
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
    result.fees_usdt = round(result.fees_usdt, 2)
    result.slippage_usdt = round(result.slippage_usdt, 2)
    return result
