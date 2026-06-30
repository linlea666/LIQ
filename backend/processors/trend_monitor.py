"""BTC 原生趋势计算。

本模块只接受底层 K 线、逐根主动买卖量和 OI 序列。禁止依赖现有市场结构、
方向投票、Regime、趋势衰竭、MAA 或现有 CVD/OI 派生结论。
"""

from __future__ import annotations

import math
import statistics
import time
from datetime import datetime, timezone
from typing import Any, Optional

from models.trend_monitor import (
    ActiveFlowSnapshot, DataQuality, EtfFlowSnapshot, ExchangeTransferFlowSnapshot,
    ExchangeTransferPoint, ExchangeTransferWindow, FlowWindow, FundingSnapshot,
    TimeframeTrend, WalletChartPoint, WalletContribution, WalletFlowSnapshot,
)


_TF_SECONDS = {"15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}
_TF_WEIGHTS = {
    "15m": (0.35, 0.45, 0.20),
    "1h": (0.40, 0.40, 0.20),
    "4h": (0.50, 0.30, 0.20),
    "1d": (0.55, 0.25, 0.20),
}
_INACTIVE_EXCHANGES = {"ftx", "bittrex"}
_KNOWN_EXCHANGES = {
    "binance", "coinbase", "kraken", "bitfinex", "bybit", "okx", "bitstamp",
    "gemini", "kucoin", "gate", "gate.io", "huobi", "htx", "bitget", "bitflyer",
    "bithumb", "bitmex", "coinone", "poloniex", "coinex", "korbit", "gopax",
}


def _canonical_exchange(name: str) -> str:
    return "Coinbase" if name.strip().lower() == "coinbase pro" else name.strip()


def _num(value: Any) -> Optional[float]:
    try:
        parsed = float(value)
        return parsed if math.isfinite(parsed) else None
    except (TypeError, ValueError):
        return None


def _clamp(value: float, low: float = -100.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def _percentile(values: list[float], value: float) -> Optional[float]:
    if not values:
        return None
    return 100.0 * sum(v <= value for v in values) / len(values)


def _ema(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    alpha = 2.0 / (period + 1.0)
    out = [values[0]]
    for value in values[1:]:
        out.append(alpha * value + (1 - alpha) * out[-1])
    return out


def _wilder_latest(values: list[float], period: int) -> float:
    """返回Wilder平滑后的最新值；输入不足时退化为均值。"""
    if not values:
        return 0.0
    if len(values) <= period:
        return statistics.fmean(values)
    smoothed = sum(values[:period])
    for value in values[period:]:
        smoothed = smoothed - smoothed / period + value
    return smoothed / period


def parse_closed_klines(raw: Any, now_ms: Optional[int] = None) -> list[dict[str, float]]:
    """解析 Binance 原生 K 线并严格丢弃未闭合 bar、乱序和重复点。"""
    now_ms = now_ms or int(time.time() * 1000)
    by_ts: dict[int, dict[str, float]] = {}
    if not isinstance(raw, list):
        return []
    for row in raw:
        try:
            if not isinstance(row, (list, tuple)) or len(row) < 7:
                continue
            ts, close_ts = int(row[0]), int(row[6])
            if close_ts >= now_ms:
                continue
            o, h, low, close, volume = map(float, row[1:6])
            if min(o, h, low, close) <= 0 or volume < 0:
                continue
            by_ts[ts] = {"ts": ts, "open": o, "high": h, "low": low,
                         "close": close, "volume": volume, "close_ts": close_ts}
        except (TypeError, ValueError):
            continue
    return [by_ts[ts] for ts in sorted(by_ts)]


def parse_cvd_deltas(raw: Any, interval_sec: int, now_sec: Optional[int] = None) -> list[dict]:
    """从每根 buy-sell 重建 CVD；绝不使用上游 cum_vol_delta 基准。"""
    now_sec = now_sec or int(time.time())
    by_ts: dict[int, dict] = {}
    if not isinstance(raw, list):
        return []
    for item in raw:
        if not isinstance(item, dict):
            continue
        ts_raw = item.get("time", item.get("t"))
        buy = _num(item.get(
            "agg_taker_buy_vol",
            item.get("aggregated_buy_volume_usd", item.get("buyVolUsd")),
        ))
        sell = _num(item.get(
            "agg_taker_sell_vol",
            item.get("aggregated_sell_volume_usd", item.get("sellVolUsd")),
        ))
        try:
            ts = int(ts_raw)
            ts = ts // 1000 if ts > 10_000_000_000 else ts
        except (TypeError, ValueError):
            continue
        if buy is None or sell is None or buy < 0 or sell < 0 or ts + interval_sec > now_sec:
            continue
        by_ts[ts] = {"ts": ts, "buy": buy, "sell": sell, "delta": buy - sell}
    cumulative = 0.0
    out = []
    for ts in sorted(by_ts):
        row = by_ts[ts]
        cumulative += row["delta"]
        out.append({**row, "cvd_local": cumulative})
    return out


def parse_oi(raw: Any, interval_sec: int, now_sec: Optional[int] = None) -> list[dict]:
    now_sec = now_sec or int(time.time())
    out: dict[int, dict] = {}
    if not isinstance(raw, list):
        return []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            ts = int(item.get("time", item.get("t", 0)))
            ts = ts // 1000 if ts > 10_000_000_000 else ts
            close = float(item.get("close", item.get("c")))
        except (TypeError, ValueError):
            continue
        if ts > 0 and close > 0 and ts + interval_sec <= now_sec:
            out[ts] = {"ts": ts, "close": close}
    return [out[ts] for ts in sorted(out)]


def _price_volume_score(bars: list[dict]) -> tuple[float, list[str]]:
    closes = [b["close"] for b in bars]
    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]
    volumes = [b["volume"] for b in bars]
    lookback = min(14, len(bars) - 1)
    if lookback < 5:
        return 0.0, ["K线覆盖不足"]

    true_ranges = []
    plus_dm, minus_dm = [], []
    for idx in range(1, len(bars)):
        true_ranges.append(max(
            highs[idx] - lows[idx], abs(highs[idx] - closes[idx - 1]),
            abs(lows[idx] - closes[idx - 1]),
        ))
        plus_dm.append(max(0.0, highs[idx] - highs[idx - 1]) if highs[idx] - highs[idx - 1] > lows[idx - 1] - lows[idx] else 0.0)
        minus_dm.append(max(0.0, lows[idx - 1] - lows[idx]) if lows[idx - 1] - lows[idx] > highs[idx] - highs[idx - 1] else 0.0)
    atr = _wilder_latest(true_ranges, lookback) or closes[-1] * 0.001
    regression_values = closes[-(lookback + 1):]
    x_mean = lookback / 2.0
    y_mean = statistics.fmean(regression_values)
    denominator = sum((idx - x_mean) ** 2 for idx in range(lookback + 1)) or 1.0
    slope = sum(
        (idx - x_mean) * (value - y_mean)
        for idx, value in enumerate(regression_values)
    ) / denominator
    regression_move_atr = slope * lookback / atr
    regression = _clamp(math.tanh(regression_move_atr / 3.0) * 55)

    ema = _ema(closes, min(20, len(closes)))
    ema_span = min(5, len(ema) - 1)
    ema_slope = ((ema[-1] - ema[-1 - ema_span]) / ema_span) / atr
    ema_component = _clamp(math.tanh(ema_slope * 2.0) * 25)

    tr_smoothed = _wilder_latest(true_ranges, lookback) or 1.0
    plus_di = 100 * _wilder_latest(plus_dm, lookback) / tr_smoothed
    minus_di = 100 * _wilder_latest(minus_dm, lookback) / tr_smoothed
    dx_values: list[float] = []
    for end in range(lookback, len(true_ranges) + 1):
        tr_window = _wilder_latest(true_ranges[:end], lookback) or 1.0
        pdi = 100 * _wilder_latest(plus_dm[:end], lookback) / tr_window
        mdi = 100 * _wilder_latest(minus_dm[:end], lookback) / tr_window
        dx_values.append(100 * abs(pdi - mdi) / (pdi + mdi) if pdi + mdi else 0.0)
    adx = _wilder_latest(dx_values, lookback) if dx_values else 0.0
    directional_ratio = (plus_di - minus_di) / (plus_di + minus_di) if plus_di + minus_di else 0.0
    dmi_component = _clamp(directional_ratio * min(1.0, adx / 25.0) * 20.0, -20, 20)

    baseline_volume = statistics.median(volumes[-min(30, len(volumes)):]) or 1.0
    volume_ratio = statistics.fmean(volumes[-min(3, len(volumes)):]) / baseline_volume
    strength = _clamp(0.75 + 0.25 * volume_ratio, 0.75, 1.25)
    score = _clamp((regression + ema_component + dmi_component) * strength)
    return score, [
        f"OLS回归斜率/ATR={regression_move_atr:.2f}", f"EMA斜率/ATR={ema_slope:.3f}",
        f"DMI差={plus_di-minus_di:.1f}，ADX={adx:.1f}", f"成交量强度={volume_ratio:.2f}x",
    ]


def _flow_component(rows: list[dict]) -> float:
    if not rows:
        return 0.0
    delta = sum(row["delta"] for row in rows)
    total = sum(row["buy"] + row["sell"] for row in rows)
    ratio = delta / total if total else 0.0
    return _clamp(math.tanh(ratio * 8.0) * 100)


def _oi_component(oi_rows: list[dict], bars: list[dict]) -> tuple[float, str]:
    if len(oi_rows) < 2 or len(bars) < 2:
        return 0.0, "OI数据不足"
    start_price, end_price = bars[-2]["close"], bars[-1]["close"]
    # CoinGlass聚合OI按USD返回，必须除以同期价格去除名义价格机械效应。
    oi_start_coin = oi_rows[0]["close"] / start_price
    oi_end_coin = oi_rows[-1]["close"] / end_price
    oi_change = oi_end_coin / oi_start_coin - 1.0
    price_change = end_price / start_price - 1.0
    volumes = [float(bar["volume"]) for bar in bars[-30:]]
    volume_ratio = volumes[-1] / (statistics.median(volumes) or 1.0)
    if abs(oi_change) < 1e-6:
        return 0.0, "去价格化OI基本不变，不确认趋势"
    if oi_change > 0:
        strength = _clamp(abs(oi_change) * 2500 * _clamp(volume_ratio, 0.5, 1.5), 10, 100)
        if price_change > 0:
            return strength, "价格上涨+去价格化OI增加：新增多头参与"
        if price_change < 0:
            return -strength, "价格下跌+去价格化OI增加：新增空头参与"
        return 0.0, "OI增加但价格无方向"
    if price_change > 0:
        return 0.0, "价格上涨+OI下降：空头回补，不确认趋势"
    if price_change < 0:
        return 0.0, "价格下跌+OI下降：多头止损，不确认趋势"
    return 0.0, "OI下降且价格无方向"


def calculate_timeframe(
    timeframe: str, bars: list[dict], spot_rows: list[dict], futures_rows: list[dict],
    oi_rows: list[dict], now_sec: Optional[int] = None,
    weights: Optional[tuple[float, float, float]] = None,
    direction_threshold: float = 25.0,
) -> TimeframeTrend:
    now_sec = now_sec or int(time.time())
    expected = _TF_SECONDS[timeframe]
    max_age = expected * 2 + 120
    age = now_sec - int(bars[-1]["close_ts"] // 1000) if bars else None
    source_interval = 300 if timeframe in ("15m", "1h") else 3600
    source_ages = [
        now_sec - (int(rows[-1]["ts"]) + source_interval)
        for rows in (spot_rows, futures_rows, oi_rows) if rows
    ]
    # 序列被严格对齐到“最后一根已闭合目标K线”，其年龄会随当前目标周期推进而增长；
    # 不能用底层5m/1h间隔判过期，否则1h/4h/1d在大部分时间都会被误判无效。
    source_stale = len(source_ages) != 3 or max(source_ages) > max_age
    recent_bars = bars[-30:]
    bars_continuous = all(
        recent_bars[idx]["ts"] - recent_bars[idx - 1]["ts"] <= expected * 1000 * 1.5
        for idx in range(1, len(recent_bars))
    )
    expected_flow_points = expected // source_interval
    expected_oi_points = expected_flow_points + 1
    target_start = int(bars[-1]["ts"] // 1000) if bars else 0
    target_end = int((bars[-1]["close_ts"] + 1) // 1000) if bars else 0

    def _continuous(rows: list[dict], expected_points: int, *, oi_anchor: bool = False) -> bool:
        if len(rows) < expected_points:
            return False
        recent = rows[-expected_points:]
        if any(
            recent[idx]["ts"] - recent[idx - 1]["ts"] != source_interval
            for idx in range(1, len(recent))
        ):
            return False
        first_end = int(recent[0]["ts"]) + source_interval
        last_end = int(recent[-1]["ts"]) + source_interval
        expected_first = target_start if oi_anchor else target_start + source_interval
        return abs(first_end - expected_first) <= 2 and abs(last_end - target_end) <= 2

    sources_aligned = (
        _continuous(spot_rows, expected_flow_points)
        and _continuous(futures_rows, expected_flow_points)
        and _continuous(oi_rows, expected_oi_points, oi_anchor=True)
    )
    enough = len(bars) >= 30 and bars_continuous and sources_aligned
    if not enough or age is None or age > max_age or source_stale:
        if source_stale:
            reason = "CVD/OI核心序列过期"
        elif not enough:
            reason = "核心数据覆盖不足、时间戳未对齐或K线存在缺口"
        else:
            reason = f"闭合K线过期{age}s"
        return TimeframeTrend(
            timeframe=timeframe, score=0, direction="invalid",
            quality=DataQuality(
                valid=False,
                age_sec=max([age or 0, *source_ages]) if source_ages else age,
                points=len(bars), reason=reason,
            ),
            reasons=[reason],
        )
    price_score, reasons = _price_volume_score(bars)
    spot_score = _flow_component(spot_rows)
    futures_score = _flow_component(futures_rows)
    flow_score = 0.60 * spot_score + 0.40 * futures_score
    oi_score, oi_text = _oi_component(oi_rows, bars)
    price_w, flow_w, oi_w = weights or _TF_WEIGHTS[timeframe]
    score = _clamp(price_w * price_score + flow_w * flow_score + oi_w * oi_score)
    direction = (
        "bullish" if score >= direction_threshold
        else "bearish" if score <= -direction_threshold else "range"
    )
    spot_confirms = (score > 0 and spot_score > 10) or (score < 0 and spot_score < -10)
    return TimeframeTrend(
        timeframe=timeframe, score=round(score, 2), direction=direction,
        price_volume_score=round(price_score, 2), orderflow_score=round(flow_score, 2),
        oi_participation_score=round(oi_score, 2), spot_confirms=spot_confirms,
        oi_interpretation=oi_text,
        reasons=[*reasons, f"现货订单流={spot_score:.1f}", f"合约订单流={futures_score:.1f}", oi_text],
        quality=DataQuality(
            valid=True, age_sec=max(0, age, *source_ages), points=len(bars),
        ),
    )


def calculate_core_direction(
    timeframes: dict[str, TimeframeTrend],
    weights: tuple[float, float, float] = (0.30, 0.50, 0.20),
    direction_threshold: float = 25.0,
) -> tuple[float, str]:
    required = ("1h", "4h", "1d")
    if any(key not in timeframes or not timeframes[key].quality.valid for key in required):
        return 0.0, "invalid"
    w1, w4, wd = weights
    score = w1 * timeframes["1h"].score + w4 * timeframes["4h"].score + wd * timeframes["1d"].score
    direction = (
        "bullish" if score >= direction_threshold
        else "bearish" if score <= -direction_threshold else "range"
    )
    return round(_clamp(score), 2), direction


def parse_active_flow(
    raw: Any, market: str, cvd_sign: float = 0.0, *, fetched_at: Optional[int] = None,
) -> ActiveFlowSnapshot:
    semantics = "现货taker主动买入减主动卖出，非链上充值提现" if market == "spot" else "合约taker主动买入减主动卖出，代表杠杆主动成交，非链上资金"
    windows = []
    if isinstance(raw, dict):
        for window in ("1h", "4h", "24h", "3d", "7d"):
            buy = _num(raw.get(f"taker_buy_volume_usd_{window}")) or 0.0
            sell = _num(raw.get(f"taker_sell_volume_usd_{window}")) or 0.0
            net = _num(raw.get(f"net_flow_usd_{window}"))
            net = (buy - sell) if net is None else net
            total = buy + sell
            windows.append(FlowWindow(
                window=window, buy_usd=buy, sell_usd=sell, net_usd=net,
                net_ratio=net / total if total else 0.0,
            ))
    valid = bool(windows and any(w.buy_usd + w.sell_usd > 0 for w in windows))
    first_net = windows[0].net_usd if windows else 0.0
    consistent = None if cvd_sign == 0 or first_net == 0 else (cvd_sign * first_net > 0)
    return ActiveFlowSnapshot(
        market=market, semantics=semantics, windows=windows, cvd_consistent=consistent,
        quality=DataQuality(
            valid=valid, points=len(windows), reason="" if valid else "无有效主动成交净流",
            age_sec=max(0, int(time.time()) - fetched_at) if fetched_at else None,
            as_of_ts=fetched_at, fetched_at_ts=fetched_at,
            status="fresh" if valid else "missing",
        ),
    )


def parse_exchange_transfer_flow(
    raw: Any,
    now_sec: Optional[int] = None,
    *,
    stale_after_sec: int = 60 * 3600,
    history_days: int = 730,
) -> ExchangeTransferFlowSnapshot:
    """Parse Looknode seven-exchange daily BTC inflow/outflow.

    The source is independent from the CoinGlass wallet balance series.  It has
    zero standalone score weight and is used later only as a material 7d
    cross-source quality check.
    """
    now_sec = now_sec or int(time.time())
    if not isinstance(raw, dict):
        return ExchangeTransferFlowSnapshot(
            quality=DataQuality(
                valid=False, status="missing", reason="Looknode交易所流入流出尚未加载",
            ),
        )

    def parse_side(value: Any) -> tuple[dict[int, float], bool]:
        result: dict[int, float] = {}
        valid_rows = True
        if not isinstance(value, list):
            return result, False
        # Upstream contains a 2010-era -0.0001 floating cleanup artifact.  Only
        # retained production history participates in validation and scoring.
        for row in value[-max(400, history_days):]:
            if not isinstance(row, dict):
                valid_rows = False
                continue
            ts_value = _num(row.get("t"))
            flow_value = _num(row.get("v"))
            if ts_value is None or flow_value is None or flow_value < 0:
                valid_rows = False
                continue
            ts = int(ts_value)
            ts = ts // 1000 if ts > 10_000_000_000 else ts
            if ts <= 0:
                valid_rows = False
                continue
            result[ts] = flow_value
        return result, valid_rows

    inflow, inflow_rows_valid = parse_side(raw.get("inflow"))
    outflow, outflow_rows_valid = parse_side(raw.get("outflow"))
    timestamps_match = bool(inflow and outflow and set(inflow) == set(outflow))
    timestamps = sorted(set(inflow) & set(outflow))
    points = [
        ExchangeTransferPoint(
            ts=ts,
            inflow_btc=inflow[ts],
            outflow_btc=outflow[ts],
            netflow_btc=inflow[ts] - outflow[ts],
        )
        for ts in timestamps
    ][-max(400, history_days):]
    recent = points[-30:]
    recent_daily = len(recent) >= 30 and all(
        recent[idx].ts - recent[idx - 1].ts == 86400 for idx in range(1, len(recent))
    )
    latest_ts = points[-1].ts if points else None
    age = max(0, now_sec - latest_ts) if latest_ts is not None else None
    valid = bool(
        inflow_rows_valid and outflow_rows_valid and timestamps_match and recent_daily
        and age is not None and age <= stale_after_sec
    )

    windows: list[ExchangeTransferWindow] = []
    for days, name in ((1, "1d"), (3, "3d"), (7, "7d"), (30, "30d")):
        if len(points) < days:
            continue
        current = points[-days:]
        inflow_sum = sum(point.inflow_btc for point in current)
        outflow_sum = sum(point.outflow_btc for point in current)
        net_sum = inflow_sum - outflow_sum
        history_in: list[float] = []
        history_out: list[float] = []
        history_abs_net: list[float] = []
        # Exclude the current window from its own baseline.
        # Historical observations must end before the current window starts.
        # Excluding only the latest point would still leak current days into
        # the 3d/7d/30d baseline and suppress genuine extremes.
        for end in range(days, len(points) - days + 1):
            sample = points[end - days:end]
            sample_in = sum(point.inflow_btc for point in sample)
            sample_out = sum(point.outflow_btc for point in sample)
            history_in.append(sample_in)
            history_out.append(sample_out)
            history_abs_net.append(abs(sample_in - sample_out))
        history_in = history_in[-365:]
        history_out = history_out[-365:]
        history_abs_net = history_abs_net[-365:]
        sign = 1 if net_sum > 0 else -1 if net_sum < 0 else 0
        same_sign_days = sum(
            point.netflow_btc * sign > 0 for point in current
        ) if sign else 0
        windows.append(ExchangeTransferWindow(
            window=name,
            inflow_btc=round(inflow_sum, 8),
            outflow_btc=round(outflow_sum, 8),
            netflow_btc=round(net_sum, 8),
            net_ratio=net_sum / (inflow_sum + outflow_sum) if inflow_sum + outflow_sum else 0,
            inflow_percentile_365d=_percentile(history_in, inflow_sum),
            outflow_percentile_365d=_percentile(history_out, outflow_sum),
            abs_net_percentile_365d=_percentile(history_abs_net, abs(net_sum)),
            same_sign_days=same_sign_days,
        ))

    activity_regime = "unknown"
    one_day = next((window for window in windows if window.window == "1d"), None)
    if valid and one_day:
        in_extreme = (one_day.inflow_percentile_365d or 0) >= 99
        out_extreme = (one_day.outflow_percentile_365d or 0) >= 99
        if in_extreme and out_extreme and abs(one_day.net_ratio) < 0.10:
            activity_regime = "high_turnover"
        elif in_extreme and one_day.netflow_btc > 0:
            activity_regime = "high_inflow"
        elif out_extreme and one_day.netflow_btc < 0:
            activity_regime = "high_outflow"
        else:
            activity_regime = "normal"

    if not points:
        reason = "Looknode交易所流入流出为空"
        status = "missing"
    elif not timestamps_match:
        reason = "Looknode流入与流出日期不完全对齐"
        status = "stale"
    elif not recent_daily:
        reason = "Looknode最近30日数据不是连续日级序列"
        status = "stale"
    elif age is not None and age > stale_after_sec:
        reason = "Looknode日级数据超过60小时未更新"
        status = "stale"
    elif not inflow_rows_valid or not outflow_rows_valid:
        reason = "Looknode存在非法时间戳或负数/非有限值"
        status = "stale"
    else:
        reason = ""
        status = "fresh"
    return ExchangeTransferFlowSnapshot(
        latest_date_ts=latest_ts,
        windows=windows,
        chart=points[-120:],
        activity_regime=activity_regime,
        quality=DataQuality(
            valid=valid,
            age_sec=age,
            points=len(points),
            reason=reason,
            as_of_ts=latest_ts,
            fetched_at_ts=int(raw.get("fetched_at") or now_sec),
            status=status,
        ),
    )


def _field(row: dict, *names: str) -> float:
    for name in names:
        value = _num(row.get(name))
        if value is not None:
            return value
    return 0.0


def parse_wallet_flow(
    balance_list: Any, chart: Any, now_sec: Optional[int] = None,
    modifier_scale_btc: float = 5000.0,
) -> WalletFlowSnapshot:
    now_sec = now_sec or int(time.time())
    contribution_map: dict[str, WalletContribution] = {}
    if isinstance(balance_list, list):
        for row in balance_list:
            if not isinstance(row, dict):
                continue
            exchange = _canonical_exchange(str(row.get("exchange_name", row.get("exchange", ""))))
            if not exchange or exchange.lower() in _INACTIVE_EXCHANGES:
                continue
            current = contribution_map.setdefault(exchange, WalletContribution(exchange=exchange))
            current.balance_btc += _field(row, "balance", "total_balance", "balance_btc")
            current.change_1d_btc += _field(row, "change_1d", "balance_change_1d", "change_1d_btc")
            current.change_7d_btc += _field(row, "change_7d", "balance_change_7d", "change_7d_btc")
            current.change_30d_btc += _field(row, "change_30d", "balance_change_30d", "change_30d_btc")
    contributions = sorted(contribution_map.values(), key=lambda row: row.balance_btc, reverse=True)
    tracked_exchanges = {row.exchange.lower() for row in contributions} | _KNOWN_EXCHANGES

    points: list[WalletChartPoint] = []
    exchange_points: dict[str, list[WalletChartPoint]] = {}
    if isinstance(chart, dict):
        times = chart.get("time_list") or []
        prices = chart.get("price_list") or []
        values = chart.get("data_list", chart.get("data_map", []))
        for idx, ts_raw in enumerate(times):
            try:
                ts = int(ts_raw)
                ts = ts // 1000 if ts > 10_000_000_000 else ts
            except (TypeError, ValueError):
                continue
            entry = values[idx] if isinstance(values, list) and idx < len(values) else None
            price = _num(prices[idx]) if idx < len(prices) else None
            total = 0.0
            if isinstance(values, dict):
                for exchange, series in values.items():
                    canonical = _canonical_exchange(exchange)
                    if canonical.lower() not in tracked_exchanges or not isinstance(series, list) or idx >= len(series):
                        continue
                    value = _num(series[idx])
                    if value is None or value <= 0:
                        continue
                    total += value
                    exchange_points.setdefault(canonical, []).append(WalletChartPoint(
                        ts=ts, price=price, balance_btc=value,
                    ))
            elif isinstance(entry, dict):
                for key, raw in entry.items():
                    value = _num(raw)
                    canonical = _canonical_exchange(key)
                    if canonical.lower() in tracked_exchanges and value is not None and value > 0:
                        series = exchange_points.setdefault(canonical, [])
                        series.append(WalletChartPoint(
                            ts=ts, price=price,
                            balance_btc=value,
                        ))
                total = sum(
                    value for key, raw in entry.items()
                    if _canonical_exchange(key).lower() in tracked_exchanges and (value := _num(raw)) is not None and value > 0
                )
                if not total and len(entry) == 1:
                    total = _num(next(iter(entry.values()))) or 0.0
            if total <= 0:
                continue
            points.append(WalletChartPoint(ts=ts, price=price, balance_btc=total))
    points.sort(key=lambda p: p.ts)
    for idx in range(1, len(points)):
        gap = points[idx].ts - points[idx - 1].ts
        if 20 * 3600 <= gap <= 28 * 3600:
            points[idx].net_change_btc = points[idx].balance_btc - points[idx - 1].balance_btc
    for series in exchange_points.values():
        series.sort(key=lambda p: p.ts)
        for idx in range(1, len(series)):
            gap = series[idx].ts - series[idx - 1].ts
            if 20 * 3600 <= gap <= 28 * 3600:
                series[idx].net_change_btc = series[idx].balance_btc - series[idx - 1].balance_btc

    total_balance = sum(row.balance_btc for row in contributions)
    change_1d = sum(row.change_1d_btc for row in contributions) if contributions else None
    change_7d_list = sum(row.change_7d_btc for row in contributions) if contributions else None
    change_30d = sum(row.change_30d_btc for row in contributions) if contributions else None
    deltas = [p.net_change_btc for p in points if p.net_change_btc is not None]

    def _recent_sum(days: int) -> Optional[float]:
        recent_points = points[-days:]
        if len(recent_points) < days or any(point.net_change_btc is None for point in recent_points):
            return None
        return sum(float(point.net_change_btc) for point in recent_points)

    change_3d = _recent_sum(3)
    chart_7d = _recent_sum(7)
    change_7d = chart_7d if chart_7d is not None else change_7d_list

    streak = 0
    if points and points[-1].net_change_btc is not None:
        last_delta = float(points[-1].net_change_btc)
        sign = 1 if last_delta > 0 else -1 if last_delta < 0 else 0
        for point in reversed(points):
            value = point.net_change_btc
            if value is not None and sign and value * sign > 0:
                streak += sign
            else:
                break
    zscore = None
    recent = deltas[-90:]
    if len(recent) >= 20 and deltas:
        median = statistics.median(recent)
        mad = statistics.median(abs(v - median) for v in recent)
        if mad > 0:
            zscore = 0.6745 * (deltas[-1] - median) / mad

    directional = [v for v in (change_1d, change_3d, change_7d) if v is not None and v != 0]
    pos = sum(v > 0 for v in directional)
    neg = sum(v < 0 for v in directional)
    seven_day_crosscheck_ok = not (
        chart_7d is not None and change_7d_list is not None
        and chart_7d != 0 and change_7d_list != 0 and chart_7d * change_7d_list < 0
    )
    consistent = max(pos, neg) >= 2 and seven_day_crosscheck_ok
    abs_total = sum(abs(row.change_1d_btc) for row in contributions)
    dominant = max((abs(row.change_1d_btc) for row in contributions), default=0.0) / abs_total if abs_total else 0.0
    latest_ts = points[-1].ts if points else 0
    age = now_sec - latest_ts if latest_ts else None
    recent_gaps_ok = all(
        20 * 3600 <= points[idx].ts - points[idx - 1].ts <= 28 * 3600
        for idx in range(max(1, len(points) - 7), len(points))
    )
    valid = bool(
        contributions and len(points) >= 8 and age is not None
        and age <= 36 * 3600 and recent_gaps_ok
    )
    modifier = 0.0
    reason = "钱包流不满足质量门，不修正趋势"
    if valid and consistent:
        majority_sign = 1 if pos > neg else -1
        daily_rates = [
            value / days for value, days in (
                (change_1d, 1), (change_3d, 3), (change_7d, 7),
            )
            if value is not None and value * majority_sign > 0
        ]
        # Preserve the original 3d calibration while ensuring a dissenting 3d
        # window can never reverse the majority direction.
        anchor = statistics.median(daily_rates) * 3 if daily_rates else 0.0
        modifier = _clamp(-math.tanh(anchor / max(1.0, modifier_scale_btc)) * 5.0, -5, 5)
        if dominant > 0.70:
            modifier *= 0.5
            reason = "方向一致但单一交易所贡献超过70%，修正降半"
        else:
            reason = "1d/3d/7d至少两个窗口方向一致，7d列表与图表交叉校验通过"
    elif not seven_day_crosscheck_ok:
        reason = "7d列表与图表方向冲突，不修正趋势"
    return WalletFlowSnapshot(
        total_balance_btc=total_balance, change_1d_btc=change_1d,
        change_3d_btc=change_3d, change_7d_btc=change_7d,
        change_30d_btc=change_30d, consecutive_direction_days=streak,
        robust_zscore_90d=zscore, direction_consistent=consistent,
        dominant_exchange_ratio=dominant, contributions=contributions,
        chart=points[-400:],
        exchange_charts={key: value[-400:] for key, value in exchange_points.items()},
        confidence_modifier=round(modifier, 2),
        modifier_reason=reason,
        quality=DataQuality(valid=valid, age_sec=age, points=len(points),
                            reason="" if valid else "余额历史缺失、日级间隔异常或超过36小时未更新",
                            as_of_ts=latest_ts or None, fetched_at_ts=now_sec,
                            status="fresh" if valid else "stale" if points else "missing"),
    )


def build_funding_snapshot(
    binance_rate: Optional[float], okx_rate: Optional[float], raw_history: Any,
    basis_pct: Optional[float], oi_near_high: bool, basis_expanding: bool = False,
    core_direction: str = "range", now_sec: Optional[int] = None,
) -> FundingSnapshot:
    now_sec = now_sec or int(time.time())
    raw_points: list[float] = []
    raw_times: list[int] = []
    if isinstance(raw_history, list):
        for row in raw_history:
            if not isinstance(row, dict):
                continue
            value = _num(row.get("close", row.get("c")))
            if value is not None:
                raw_points.append(value)
                try:
                    ts = int(row.get("time", row.get("t", 0)))
                    raw_times.append(ts // 1000 if ts > 10_000_000_000 else ts)
                except (TypeError, ValueError):
                    raw_times.append(0)
    official = [v for v in (binance_rate, okx_rate) if v is not None]
    reference = abs(statistics.fmean(official)) if official else 0.0
    if raw_points:
        med = statistics.median(abs(v) for v in raw_points)
        # 官方当前值过近零时不能拿来做比例尺，否则会把正常历史误缩小100倍。
        use_reference = reference >= 1e-6
        if (use_reference and med / reference > 50) or (
            not use_reference and max(abs(v) for v in raw_points) > 0.003
        ):
            raw_points = [v * 0.01 for v in raw_points]
    current = raw_points[-1] if raw_points else (statistics.fmean(official) if official else None)
    avg = lambda n: statistics.fmean(raw_points[-n:]) if raw_points else None
    streak = 0
    if raw_points and raw_points[-1] != 0:
        sign = 1 if raw_points[-1] > 0 else -1
        for value in reversed(raw_points):
            if value * sign > 0:
                streak += sign
            else:
                break
    pct = _percentile(raw_points, current) if current is not None else None
    crowding = "neutral"
    market_bias = "neutral"
    modifier = 0.0
    reason = "Funding/Basis未达到拥挤修正门"
    basis_same = basis_pct is not None and current is not None and basis_pct * current > 0
    if current is not None and pct is not None and oi_near_high and basis_same and basis_expanding:
        if pct >= 95 and current > 0:
            crowding, market_bias = "long_crowded", "bullish"
            modifier = -8.0 if core_direction == "bullish" else 3.0 if core_direction == "bearish" else 0.0
            reason = "正Funding极端、OI高位且Basis同向；按核心方向处理拥挤风险"
        elif pct <= 5 and current < 0:
            crowding, market_bias = "short_crowded", "bearish"
            modifier = -8.0 if core_direction == "bearish" else 3.0 if core_direction == "bullish" else 0.0
            reason = "负Funding极端、OI高位且Basis同向；按核心方向处理拥挤风险"
        elif 55 <= pct <= 85 and current > 0:
            market_bias = "bullish"
            modifier = 3.0 if core_direction == "bullish" else 0.0
            reason = "温和正Funding与Basis同向；仅在多头核心方向下增强"
        elif 15 <= pct <= 45 and current < 0:
            market_bias = "bearish"
            modifier = 3.0 if core_direction == "bearish" else 0.0
            reason = "温和负Funding与Basis同向；仅在空头核心方向下增强"
    latest_ts = max(raw_times, default=0)
    age = now_sec - latest_ts if latest_ts else None
    quality_valid = bool(official and raw_points and age is not None and age <= 16 * 3600)
    return FundingSnapshot(
        binance_rate=binance_rate, okx_rate=okx_rate, oi_weighted_rate=current,
        avg_24h=avg(3), avg_3d=avg(9), avg_7d=avg(21),
        same_sign_settlements=streak, percentile_30d=pct, basis_pct=basis_pct,
        crowding=crowding, market_bias=market_bias,
        confidence_modifier=modifier if quality_valid else 0.0, modifier_reason=reason,
        quality=DataQuality(
            valid=quality_valid, age_sec=age, points=len(raw_points),
            reason="" if quality_valid else "官方Funding缺失或历史超过16小时未更新",
            as_of_ts=latest_ts or None, fetched_at_ts=now_sec,
            status="fresh" if quality_valid else "stale" if raw_points else "missing",
        ),
    )


def parse_etf_flow(
    raw: Any, core_direction: str, now_sec: Optional[int] = None,
) -> EtfFlowSnapshot:
    now_sec = now_sec or int(time.time())
    rows: dict[int, float] = {}
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            has_value = any(name in item for name in ("flow_usd", "total_netflow", "totalNetflow", "netflow"))
            if not has_value:
                continue
            value = _field(item, "flow_usd", "total_netflow", "totalNetflow", "netflow")
            ts = item.get("timestamp", item.get("time"))
            try:
                ts_int = int(ts)
                ts_sec = ts_int // 1000 if ts_int > 10_000_000_000 else ts_int
            except (TypeError, ValueError):
                continue
            if ts_sec <= 0:
                continue
            weekday = datetime.fromtimestamp(ts_sec, tz=timezone.utc).weekday()
            components = item.get("etf_flows")
            component_values = (
                [row.get("flow_usd") for row in components if isinstance(row, dict) and "flow_usd" in row]
                if isinstance(components, list) else []
            )
            # 周末及上游节假日占位零不视为交易日；真实零值若有成分明细则保留。
            if weekday >= 5 or (value == 0 and not component_values):
                continue
            rows[ts_sec] = value
    ordered = sorted(rows.items())
    values = [value for _, value in ordered]
    one = values[-1] if values else None
    three = sum(values[-3:]) if len(values) >= 3 else None
    five = sum(values[-5:]) if len(values) >= 5 else None
    latest_ts = ordered[-1][0] if ordered else None
    age = now_sec - latest_ts if latest_ts else None
    fresh = bool(values and age is not None and age <= 7 * 86400)
    modifier = 0.0
    anchor = three if three is not None else one
    if fresh and anchor is not None and core_direction in ("bullish", "bearish"):
        same = (anchor > 0 and core_direction == "bullish") or (anchor < 0 and core_direction == "bearish")
        modifier = (1 if same else -1) * min(5.0, abs(anchor) / 200_000_000 * 5.0)
    return EtfFlowSnapshot(
        net_1d_usd=one, net_3_sessions_usd=three, net_5_sessions_usd=five,
        latest_session_ts=latest_ts, confidence_modifier=round(modifier, 2),
        quality=DataQuality(
            valid=fresh, age_sec=age, points=len(values),
            reason="" if fresh else "ETF交易日数据缺失或超过7天未更新",
            as_of_ts=latest_ts, fetched_at_ts=now_sec,
            status="fresh" if fresh else "stale" if values else "missing",
        ),
    )
