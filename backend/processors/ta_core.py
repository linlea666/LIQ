"""通用技术分析核心计算：SMA / EMA / MACD

所有函数为纯函数，输入 list[float]（按时间升序），输出同长度序列或标量。
供 range_signal / 未来其他 processor 复用。
"""

from __future__ import annotations


def calc_sma(values: list[float], period: int) -> list[float | None]:
    """简单移动平均。前 period-1 个点返回 None。"""
    if period <= 0 or not values:
        return [None] * len(values)
    result: list[float | None] = []
    window_sum = 0.0
    for i, v in enumerate(values):
        window_sum += v
        if i >= period:
            window_sum -= values[i - period]
        if i >= period - 1:
            result.append(window_sum / period)
        else:
            result.append(None)
    return result


def calc_ema(values: list[float], period: int) -> list[float | None]:
    """指数移动平均 (EMA)。前 period-1 个点返回 None，第 period 个点用 SMA 初始化。"""
    if period <= 0 or len(values) < period:
        return [None] * len(values)
    result: list[float | None] = [None] * (period - 1)
    sma_init = sum(values[:period]) / period
    result.append(sma_init)
    k = 2.0 / (period + 1)
    prev = sma_init
    for v in values[period:]:
        ema_val = v * k + prev * (1 - k)
        result.append(ema_val)
        prev = ema_val
    return result


def calc_macd(
    values: list[float],
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> dict:
    """标准 MACD (fast EMA - slow EMA) + Signal EMA + Histogram。

    Returns:
        dict with keys:
        - macd_line: list[float|None]
        - signal_line: list[float|None]
        - histogram: list[float|None]
        - above_zero: bool|None  (最新 MACD 线是否 > 0)
        - histogram_rising: bool|None (最近 histogram 是否在上升)
    """
    n = len(values)
    ema_fast = calc_ema(values, fast)
    ema_slow = calc_ema(values, slow)

    macd_line: list[float | None] = []
    for i in range(n):
        if ema_fast[i] is not None and ema_slow[i] is not None:
            macd_line.append(ema_fast[i] - ema_slow[i])
        else:
            macd_line.append(None)

    macd_valid = [v for v in macd_line if v is not None]
    sig_raw = calc_ema(macd_valid, signal) if len(macd_valid) >= signal else [None] * len(macd_valid)

    signal_line: list[float | None] = [None] * (n - len(macd_valid))
    sig_idx = 0
    for i in range(n):
        if macd_line[i] is not None:
            signal_line.append(sig_raw[sig_idx] if sig_idx < len(sig_raw) else None)
            sig_idx += 1

    histogram: list[float | None] = []
    for i in range(n):
        if macd_line[i] is not None and signal_line[i] is not None:
            histogram.append(macd_line[i] - signal_line[i])
        else:
            histogram.append(None)

    last_macd = next((v for v in reversed(macd_line) if v is not None), None)
    above_zero = last_macd > 0 if last_macd is not None else None

    hist_valid = [v for v in histogram if v is not None]
    histogram_rising = None
    if len(hist_valid) >= 2:
        histogram_rising = hist_valid[-1] > hist_valid[-2]

    return {
        "macd_line": macd_line,
        "signal_line": signal_line,
        "histogram": histogram,
        "above_zero": above_zero,
        "histogram_rising": histogram_rising,
    }


def last_valid(series: list[float | None]) -> float | None:
    """取序列最后一个非 None 值。"""
    for v in reversed(series):
        if v is not None:
            return v
    return None
