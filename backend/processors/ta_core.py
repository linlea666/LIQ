"""通用技术分析核心计算

所有函数为纯函数，输入 list[float]（按时间升序），输出同长度序列或标量。
供 range_signal / level_discovery / confluence_scoring 等模块复用。

包含：SMA / EMA / MACD / Fibonacci / Pivot Points / Ichimoku /
      Keltner Channel / BMSA / Swing 检测 / BB Squeeze 检测
"""

from __future__ import annotations

from dataclasses import dataclass, field


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


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ATR (Wilder)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def calc_atr(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    period: int = 14,
) -> list[float | None]:
    """Wilder 平滑 ATR。"""
    n = len(highs)
    if n < 2 or n != len(lows) or n != len(closes):
        return [None] * n
    trs: list[float] = [highs[0] - lows[0]]
    for i in range(1, n):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        trs.append(tr)
    result: list[float | None] = [None] * (period - 1)
    atr_val = sum(trs[:period]) / period
    result.append(atr_val)
    for i in range(period, n):
        atr_val = (atr_val * (period - 1) + trs[i]) / period
        result.append(atr_val)
    return result


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Fibonacci Retracement / Extension
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

FIB_RATIOS = [0.0, 0.236, 0.382, 0.5, 0.618, 0.786, 1.0]
FIB_EXT_RATIOS = [1.272, 1.618, 2.0]


@dataclass
class FibLevel:
    ratio: float
    price: float
    label: str  # "Fib 0.618" etc.


def calc_fibonacci_levels(
    swing_high: float,
    swing_low: float,
    direction: str = "up",
) -> list[FibLevel]:
    """计算斐波那契回撤 + 扩展位。

    direction="up"  : 上升波段回撤 (high→low 方向)
    direction="down": 下降波段回撤 (low→high 方向)
    """
    if swing_high <= swing_low or swing_low <= 0:
        return []
    span = swing_high - swing_low
    levels: list[FibLevel] = []

    if direction == "up":
        for r in FIB_RATIOS:
            price = swing_high - span * r
            levels.append(FibLevel(ratio=r, price=round(price, 2), label=f"Fib {r}"))
        for r in FIB_EXT_RATIOS:
            price = swing_high + span * (r - 1.0)
            levels.append(FibLevel(ratio=r, price=round(price, 2), label=f"Fib Ext {r}"))
    else:
        for r in FIB_RATIOS:
            price = swing_low + span * r
            levels.append(FibLevel(ratio=r, price=round(price, 2), label=f"Fib {r}"))
        for r in FIB_EXT_RATIOS:
            price = swing_low - span * (r - 1.0)
            if price > 0:
                levels.append(FibLevel(ratio=r, price=round(price, 2), label=f"Fib Ext {r}"))

    return levels


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Pivot Points (Classic / Fibonacci / Camarilla)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dataclass
class PivotLevels:
    pivot: float
    r1: float
    r2: float
    r3: float
    s1: float
    s2: float
    s3: float
    method: str  # "classic" / "fibonacci" / "camarilla"


def calc_pivot_classic(high: float, low: float, close: float) -> PivotLevels:
    p = (high + low + close) / 3
    return PivotLevels(
        pivot=round(p, 2),
        r1=round(2 * p - low, 2),
        r2=round(p + (high - low), 2),
        r3=round(high + 2 * (p - low), 2),
        s1=round(2 * p - high, 2),
        s2=round(p - (high - low), 2),
        s3=round(low - 2 * (high - p), 2),
        method="classic",
    )


def calc_pivot_fibonacci(high: float, low: float, close: float) -> PivotLevels:
    p = (high + low + close) / 3
    r = high - low
    return PivotLevels(
        pivot=round(p, 2),
        r1=round(p + 0.382 * r, 2),
        r2=round(p + 0.618 * r, 2),
        r3=round(p + 1.000 * r, 2),
        s1=round(p - 0.382 * r, 2),
        s2=round(p - 0.618 * r, 2),
        s3=round(p - 1.000 * r, 2),
        method="fibonacci",
    )


def calc_pivot_camarilla(high: float, low: float, close: float) -> PivotLevels:
    r = high - low
    return PivotLevels(
        pivot=round((high + low + close) / 3, 2),
        r1=round(close + r * 1.1 / 12, 2),
        r2=round(close + r * 1.1 / 6, 2),
        r3=round(close + r * 1.1 / 4, 2),
        s1=round(close - r * 1.1 / 12, 2),
        s2=round(close - r * 1.1 / 6, 2),
        s3=round(close - r * 1.1 / 4, 2),
        method="camarilla",
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Ichimoku Cloud
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dataclass
class IchimokuResult:
    tenkan: float | None = None       # 转换线 (9)
    kijun: float | None = None        # 基准线 (26)
    senkou_a: float | None = None     # 先行 A
    senkou_b: float | None = None     # 先行 B (52)
    cloud_top: float | None = None    # 云层上沿
    cloud_bottom: float | None = None # 云层下沿
    cloud_bullish: bool | None = None # 云层方向


def _period_midpoint(highs: list[float], lows: list[float], end: int, period: int) -> float | None:
    if end < period - 1 or end >= len(highs):
        return None
    seg_h = highs[end - period + 1: end + 1]
    seg_l = lows[end - period + 1: end + 1]
    return (max(seg_h) + min(seg_l)) / 2


def calc_ichimoku(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    tenkan_period: int = 9,
    kijun_period: int = 26,
    senkou_b_period: int = 52,
) -> IchimokuResult:
    """计算一目均衡表最新值（当前 bar 的云层 = 26 bars 前计算的先行值）。"""
    n = len(highs)
    if n < senkou_b_period or n != len(lows) or n != len(closes):
        return IchimokuResult()

    last = n - 1
    tenkan = _period_midpoint(highs, lows, last, tenkan_period)
    kijun = _period_midpoint(highs, lows, last, kijun_period)

    sa_idx = last - kijun_period
    if sa_idx >= tenkan_period - 1 and sa_idx >= kijun_period - 1:
        t = _period_midpoint(highs, lows, sa_idx, tenkan_period)
        k = _period_midpoint(highs, lows, sa_idx, kijun_period)
        senkou_a = (t + k) / 2 if t is not None and k is not None else None
    else:
        senkou_a = None

    sb_idx = last - kijun_period
    senkou_b = _period_midpoint(highs, lows, sb_idx, senkou_b_period) if sb_idx >= senkou_b_period - 1 else None

    cloud_top = None
    cloud_bottom = None
    cloud_bullish = None
    if senkou_a is not None and senkou_b is not None:
        cloud_top = max(senkou_a, senkou_b)
        cloud_bottom = min(senkou_a, senkou_b)
        cloud_bullish = senkou_a > senkou_b

    return IchimokuResult(
        tenkan=round(tenkan, 2) if tenkan is not None else None,
        kijun=round(kijun, 2) if kijun is not None else None,
        senkou_a=round(senkou_a, 2) if senkou_a is not None else None,
        senkou_b=round(senkou_b, 2) if senkou_b is not None else None,
        cloud_top=round(cloud_top, 2) if cloud_top is not None else None,
        cloud_bottom=round(cloud_bottom, 2) if cloud_bottom is not None else None,
        cloud_bullish=cloud_bullish,
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Keltner Channel
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dataclass
class KeltnerResult:
    upper: float | None = None
    middle: float | None = None
    lower: float | None = None


def calc_keltner(
    closes: list[float],
    highs: list[float],
    lows: list[float],
    period: int = 20,
    atr_mult: float = 1.5,
    atr_period: int = 10,
) -> KeltnerResult:
    """Keltner Channel = EMA(period) +/- atr_mult * ATR(atr_period)。"""
    n = len(closes)
    if n < max(period, atr_period) + 1:
        return KeltnerResult()

    ema_series = calc_ema(closes, period)
    atr_series = calc_atr(highs, lows, closes, atr_period)

    mid = last_valid(ema_series)
    atr_val = last_valid(atr_series)
    if mid is None or atr_val is None:
        return KeltnerResult()

    return KeltnerResult(
        upper=round(mid + atr_mult * atr_val, 2),
        middle=round(mid, 2),
        lower=round(mid - atr_mult * atr_val, 2),
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Bull Market Support Band (20W SMA + 21W EMA)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dataclass
class BMSAResult:
    sma_20w: float | None = None
    ema_21w: float | None = None
    band_upper: float | None = None
    band_lower: float | None = None


def calc_bmsa(weekly_closes: list[float]) -> BMSAResult:
    """牛市支撑带：20 周 SMA + 21 周 EMA。"""
    if len(weekly_closes) < 21:
        return BMSAResult()
    sma20 = last_valid(calc_sma(weekly_closes, 20))
    ema21 = last_valid(calc_ema(weekly_closes, 21))
    if sma20 is None or ema21 is None:
        return BMSAResult()
    return BMSAResult(
        sma_20w=round(sma20, 2),
        ema_21w=round(ema21, 2),
        band_upper=round(max(sma20, ema21), 2),
        band_lower=round(min(sma20, ema21), 2),
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Swing High / Low Detection
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dataclass
class SwingPoint:
    index: int
    price: float
    kind: str      # "high" / "low"
    ts: int = 0    # 可选时间戳


def detect_swings(
    highs: list[float],
    lows: list[float],
    timestamps: list[int] | None = None,
    lookback: int = 5,
) -> list[SwingPoint]:
    """检测 Swing High / Swing Low（前后各 lookback 根 bar 的极值）。"""
    n = len(highs)
    if n != len(lows) or n < 2 * lookback + 1:
        return []
    ts = timestamps or [0] * n
    points: list[SwingPoint] = []

    for i in range(lookback, n - lookback):
        is_high = all(highs[i] >= highs[j] for j in range(i - lookback, i + lookback + 1) if j != i)
        if is_high:
            points.append(SwingPoint(index=i, price=highs[i], kind="high", ts=ts[i]))

        is_low = all(lows[i] <= lows[j] for j in range(i - lookback, i + lookback + 1) if j != i)
        if is_low:
            points.append(SwingPoint(index=i, price=lows[i], kind="low", ts=ts[i]))

    return points


def find_major_swing(
    highs: list[float],
    lows: list[float],
    timestamps: list[int] | None = None,
    lookback: int = 10,
    min_swings: int = 2,
) -> tuple[SwingPoint | None, SwingPoint | None]:
    """找出最近一轮大波段的 Swing High / Swing Low 用于 Fibonacci 计算。

    Returns (swing_high, swing_low) 或 (None, None)。
    """
    points = detect_swings(highs, lows, timestamps, lookback)
    if len(points) < min_swings:
        points = detect_swings(highs, lows, timestamps, max(3, lookback // 2))
    if not points:
        return None, None

    swing_highs = [p for p in points if p.kind == "high"]
    swing_lows = [p for p in points if p.kind == "low"]
    if not swing_highs or not swing_lows:
        return None, None

    sh = max(swing_highs, key=lambda p: p.price)
    sl = min(swing_lows, key=lambda p: p.price)
    return sh, sl


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Bollinger Band Squeeze Detection
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dataclass
class BBSqueezeResult:
    is_squeeze: bool = False
    squeeze_direction: str = ""   # "up" / "down" / "unknown"
    bb_upper: float | None = None
    bb_lower: float | None = None
    bb_width_pct: float | None = None  # BB 宽度占中轨百分比
    kc_upper: float | None = None
    kc_lower: float | None = None


def detect_bb_squeeze(
    bb_upper: float | None,
    bb_lower: float | None,
    bb_middle: float | None,
    kc: KeltnerResult | None = None,
    macd_histogram: float | None = None,
) -> BBSqueezeResult:
    """布林带挤压检测：BB 通道完全被 Keltner 通道包裹 = Squeeze。

    当无 Keltner 数据时，使用 BB 宽度百分比 < 4% 作为替代条件。
    """
    if bb_upper is None or bb_lower is None or bb_middle is None or bb_middle <= 0:
        return BBSqueezeResult()

    bb_width_pct = (bb_upper - bb_lower) / bb_middle * 100

    is_squeeze = False
    if kc is not None and kc.upper is not None and kc.lower is not None:
        is_squeeze = bb_upper < kc.upper and bb_lower > kc.lower
    else:
        is_squeeze = bb_width_pct < 4.0

    direction = "unknown"
    if macd_histogram is not None:
        direction = "up" if macd_histogram > 0 else "down"

    return BBSqueezeResult(
        is_squeeze=is_squeeze,
        squeeze_direction=direction if is_squeeze else "",
        bb_upper=bb_upper,
        bb_lower=bb_lower,
        bb_width_pct=round(bb_width_pct, 2),
        kc_upper=kc.upper if kc else None,
        kc_lower=kc.lower if kc else None,
    )
