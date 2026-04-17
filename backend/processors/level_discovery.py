"""Level Discovery Engine — 三维候选位生成

三大维度：
  A. 价格结构 (Swing H/L, 前高前低, 未回补影线, 成交密集区)
  B. 数学指标 (EMA 簇, SMA200, BMSA, Fibonacci, Pivot, Ichimoku, BOLL)
  C. 资金仓位 (清算簇, VP POC/VA, VWAP, 订单墙, 链上周期价)

每个候选位输出 RawCandidate，后续由 confluence_scoring 做聚类打分。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from models.flow import CyclePositionData, RangeSignalData


def fmt_usd_cn(usd: float) -> str:
    """将 USD 金额格式化为中文单位（亿/千万/百万/万）。

    正确处理负值（保留符号）和小值（<1万直接显示美元）。
    """
    sign = "-" if usd < 0 else ""
    a = abs(usd)
    if a >= 1e8:
        return f"{sign}{a / 1e8:.1f}亿"
    if a >= 1e7:
        return f"{sign}{a / 1e7:.0f}千万"
    if a >= 1e6:
        return f"{sign}{a / 1e6:.0f}百万"
    if a >= 1e4:
        return f"{sign}{a / 1e4:.0f}万"
    if a >= 1:
        return f"{sign}{a:,.0f}"
    return "0"
from models.liquidation import LiquidationMap
from models.market import CandleData, OrderBookAnalysis, VolumeProfileData
from processors.ta_core import (
    BMSAResult,
    FibLevel,
    IchimokuResult,
    KeltnerResult,
    calc_bmsa,
    calc_ema,
    calc_fibonacci_levels,
    calc_ichimoku,
    calc_keltner,
    calc_pivot_classic,
    calc_sma,
    detect_swings,
    find_major_swing,
    last_valid,
)

logger = logging.getLogger(__name__)


def detect_round_numbers(
    price: float,
    radius_pct: float = 3.0,
) -> list[dict]:
    """检测当前价附近的心理整数关口（散户挂单/止损密集带）。

    输入：
      price: 当前价
      radius_pct: 搜索半径（%），默认 ±3%

    返回 list[dict]，每项含：
      price / side / source / source_tag / base_score

    步长根据币价自适应：
      >= 10000   → 主 1000  / 次 500    (BTC 档)
      >= 1000    → 主 100   / 次 50     (ETH 档)
      >= 100     → 主 10    / 次 5      (SOL 档)
      >= 10      → 主 1     / 次 0.5    (小币 10 档)
      >= 1       → 主 0.1   / 次 None
      < 1        → 主 0.01  / 次 None

    主步长：base_score=15（source_tag='round_major'）
    次步长：base_score=8 （source_tag='round_minor'）

    不对当前价本身打标签（距离 < 步长 5% 的视为已在位内）。
    """
    out: list[dict] = []
    if price <= 0:
        return out

    if price >= 10000:
        major, minor = 1000.0, 500.0
    elif price >= 1000:
        major, minor = 100.0, 50.0
    elif price >= 100:
        major, minor = 10.0, 5.0
    elif price >= 10:
        major, minor = 1.0, 0.5
    elif price >= 1:
        major, minor = 0.1, None  # type: ignore[assignment]
    else:
        major, minor = 0.01, None  # type: ignore[assignment]

    radius = price * radius_pct / 100.0
    low = price - radius
    high = price + radius

    def _emit(step: float, tag: str, score: float, label_prefix: str) -> None:
        # 在 [low, high] 区间内枚举 step 的倍数
        start = int(low // step)
        end = int(high // step) + 1
        for k in range(start, end):
            rn = round(step * k, 6)
            if rn <= 0:
                continue
            # 过滤"与当前价几乎重合"（<0.05 * step）
            if abs(rn - price) < step * 0.05:
                continue
            if rn < low or rn > high:
                continue
            side = "support" if rn < price else "resistance"
            # 格式化价格显示
            if step >= 1:
                pretty = f"${int(rn):,}"
            elif step >= 0.1:
                pretty = f"${rn:.1f}"
            else:
                pretty = f"${rn:.2f}"
            out.append({
                "price": rn, "side": side,
                "source": f"{label_prefix}{pretty}",
                "source_tag": tag,
                "base_score": score,
            })

    _emit(major, "round_major", 15.0, "心理关口")
    if minor is not None:
        # 次级关口：排除与主级重合的
        major_set = {round(c["price"], 6) for c in out}
        before = len(out)
        _emit(minor, "round_minor", 8.0, "心理关口(次)")
        # 去重
        out[before:] = [c for c in out[before:] if round(c["price"], 6) not in major_set]

    return out


def detect_oi_surge_zones(
    oi_history: list[dict],
    candles: list[CandleData] | None,
    threshold_pct: float = 5.0,
) -> list[tuple[float, float]]:
    """检测 OI 骤变区 — OI 在短时间内变化 > threshold_pct 的价格区间。

    Returns: list of (price, oi_change_pct)
    """
    if not oi_history or not candles or len(oi_history) < 3:
        return []
    zones: list[tuple[float, float]] = []
    for i in range(1, len(oi_history)):
        prev_oi = oi_history[i - 1].get("oi", 0)
        curr_oi = oi_history[i].get("oi", 0)
        if prev_oi <= 0:
            continue
        change_pct = (curr_oi - prev_oi) / prev_oi * 100
        if abs(change_pct) >= threshold_pct:
            ts = oi_history[i].get("ts", 0)
            closest_candle = min(candles, key=lambda c: abs(c.ts - ts), default=None)
            if closest_candle:
                price = (closest_candle.high + closest_candle.low) / 2
                zones.append((round(price, 2), round(change_pct, 2)))
    return zones


@dataclass
class RawCandidate:
    """单个候选价位（未合并前）"""
    price: float
    side: str                  # "support" / "resistance"
    dimension: str             # "price_structure" / "math_indicator" / "capital_flow"
    source: str                # 可读来源描述
    source_tag: str            # 机器用标签 (如 "swing_high_4h")
    base_score: float = 0     # 维度内基础评分 (0-50)
    timeframe: str = ""        # "1H"/"4H"/"1D"/"1W"
    data_age_hours: float = 0  # 数据新鲜度（小时）


@dataclass
class DiscoveryResult:
    """Level Discovery 输出"""
    candidates: list[RawCandidate] = field(default_factory=list)
    ichimoku: IchimokuResult | None = None
    bmsa: BMSAResult | None = None
    keltner: KeltnerResult | None = None
    fib_levels: list[FibLevel] = field(default_factory=list)
    fib_swing_high: float = 0
    fib_swing_low: float = 0
    fib_direction: str = ""
    sma200d: float | None = None


def discover_levels(
    current_price: float,
    atr: float,
    candles_1h: list[CandleData] | None = None,
    candles_4h: list[CandleData] | None = None,
    candles_1d: list[CandleData] | None = None,
    candles_1w: list[CandleData] | None = None,
    liq_map: LiquidationMap | None = None,
    liq_map_7d: LiquidationMap | None = None,
    vp: VolumeProfileData | None = None,
    orderbook: OrderBookAnalysis | None = None,
    ema_daily: dict[int, float] | None = None,
    sma200_daily: float | None = None,
    boll_data: dict | None = None,
    boll_4h_data: dict | None = None,
    vwap: float = 0,
    cycle_position: CyclePositionData | None = None,
    range_signal: RangeSignalData | None = None,
    oi_history: list[dict] | None = None,
) -> DiscoveryResult:
    """执行三维候选位发现，返回所有候选位 + 辅助数据。"""
    if current_price <= 0:
        return DiscoveryResult()

    result = DiscoveryResult()
    result.sma200d = sma200_daily
    cands = result.candidates

    # ── 维度 A: 价格结构 ──
    _discover_price_structure(
        cands, current_price, atr,
        candles_4h, candles_1d, candles_1w,
        range_signal, vp,
    )

    # ── 维度 B: 数学指标 ──
    _discover_math_indicators(
        cands, result, current_price, atr,
        candles_1d, candles_1w,
        ema_daily, sma200_daily,
        boll_data, boll_4h_data,
    )

    # ── 维度 C: 资金与仓位 ──
    _discover_capital_flow(
        cands, current_price, atr,
        liq_map, liq_map_7d,
        vp, orderbook, vwap,
        cycle_position,
        oi_history, candles_1h,
    )

    return result


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 维度 A: 价格结构
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _discover_price_structure(
    cands: list[RawCandidate],
    price: float,
    atr: float,
    candles_4h: list[CandleData] | None,
    candles_1d: list[CandleData] | None,
    candles_1w: list[CandleData] | None,
    range_signal: RangeSignalData | None,
    vp: VolumeProfileData | None,
):
    _add_swing_levels(cands, candles_4h, price, "4H", lookback=5, base_score=25)
    _add_swing_levels(cands, candles_1d, price, "1D", lookback=5, base_score=35)
    _add_swing_levels(cands, candles_1w, price, "1W", lookback=3, base_score=45)

    if range_signal:
        if range_signal.unfilled_wick_low and range_signal.unfilled_wick_low < price:
            cands.append(RawCandidate(
                price=range_signal.unfilled_wick_low, side="support",
                dimension="price_structure", source="未回补日线下影线",
                source_tag="unfilled_wick_low", base_score=30, timeframe="1D",
            ))
        if range_signal.unfilled_wick_high and range_signal.unfilled_wick_high > price:
            cands.append(RawCandidate(
                price=range_signal.unfilled_wick_high, side="resistance",
                dimension="price_structure", source="未回补日线上影线",
                source_tag="unfilled_wick_high", base_score=30, timeframe="1D",
            ))

    if vp and vp.bins:
        sorted_bins = sorted(vp.bins, key=lambda b: b.volume, reverse=True)
        for bin_ in sorted_bins[:3]:
            mid = (bin_.price_low + bin_.price_high) / 2
            if abs(mid - vp.poc_price) / price < 0.003:
                continue
            side = "support" if mid < price else "resistance"
            cands.append(RawCandidate(
                price=round(mid, 2), side=side,
                dimension="price_structure", source=f"成交密集区(HVN)",
                source_tag="hvn", base_score=20, timeframe="1H",
            ))

    # ── 心理整数关口（散户挂单/止损密集区）──
    for rn in detect_round_numbers(price, radius_pct=3.0):
        cands.append(RawCandidate(
            price=rn["price"], side=rn["side"],
            dimension="price_structure",
            source=rn["source"], source_tag=rn["source_tag"],
            base_score=rn["base_score"], timeframe="1D",
        ))


def _add_swing_levels(
    cands: list[RawCandidate],
    candles: list[CandleData] | None,
    price: float,
    tf: str,
    lookback: int,
    base_score: float,
):
    if not candles or len(candles) < 2 * lookback + 1:
        return
    highs = [c.high for c in candles]
    lows = [c.low for c in candles]
    ts_list = [c.ts for c in candles]
    swings = detect_swings(highs, lows, ts_list, lookback)

    is_weekly = tf == "1W"
    max_dist = 25.0 if is_weekly else 15.0
    decay_floor = 0.5 if is_weekly else 0.3

    seen: set[str] = set()
    for sp in reversed(swings):
        side = "resistance" if sp.kind == "high" else "support"
        key = f"{side}_{round(sp.price / price, 3)}"
        if key in seen:
            continue
        seen.add(key)
        dist_pct = abs(sp.price - price) / price * 100
        if dist_pct > max_dist:
            continue
        cands.append(RawCandidate(
            price=round(sp.price, 2), side=side,
            dimension="price_structure",
            source=f"{tf}前{'高' if sp.kind == 'high' else '低'}",
            source_tag=f"swing_{sp.kind}_{tf.lower()}",
            base_score=base_score * max(decay_floor, 1 - dist_pct / max_dist),
            timeframe=tf,
        ))
        if len(seen) >= 8:
            break


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 维度 B: 数学指标
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _discover_math_indicators(
    cands: list[RawCandidate],
    result: DiscoveryResult,
    price: float,
    atr: float,
    candles_1d: list[CandleData] | None,
    candles_1w: list[CandleData] | None,
    ema_daily: dict[int, float] | None,
    sma200_daily: float | None,
    boll_data: dict | None,
    boll_4h_data: dict | None,
):
    # EMA 簇
    ema_daily = ema_daily or {}
    ema_prices = []
    for period, val in sorted(ema_daily.items()):
        if val and val > 0 and 0.001 < abs(val - price) / price < 0.15:
            side = "support" if val < price else "resistance"
            cands.append(RawCandidate(
                price=round(val, 2), side=side,
                dimension="math_indicator", source=f"日线EMA{period}",
                source_tag=f"ema_{period}_1d", base_score=18, timeframe="1D",
            ))
            ema_prices.append(val)

    # 均线共振区检测：多根 EMA 聚集在 ATR 宽度内
    if len(ema_prices) >= 2:
        ema_prices.sort()
        for i in range(len(ema_prices) - 1):
            if abs(ema_prices[i+1] - ema_prices[i]) < atr * 0.5:
                mid = (ema_prices[i] + ema_prices[i+1]) / 2
                side = "support" if mid < price else "resistance"
                cands.append(RawCandidate(
                    price=round(mid, 2), side=side,
                    dimension="math_indicator", source="均线共振区",
                    source_tag="ma_cluster", base_score=30, timeframe="1D",
                ))

    # 200 日 SMA — 多空分界线（特殊标记，高权重）
    if sma200_daily and sma200_daily > 0 and abs(sma200_daily - price) / price < 0.20:
        side = "support" if sma200_daily < price else "resistance"
        cands.append(RawCandidate(
            price=round(sma200_daily, 2), side=side,
            dimension="math_indicator", source="200日SMA(多空分界线)",
            source_tag="sma_200_1d", base_score=40, timeframe="1D",
        ))

    # Bull Market Support Band
    if candles_1w and len(candles_1w) >= 21:
        weekly_closes = [c.close for c in candles_1w]
        bmsa = calc_bmsa(weekly_closes)
        result.bmsa = bmsa
        if bmsa.band_upper and bmsa.band_lower:
            if abs(bmsa.band_upper - price) / price < 0.20:
                side = "support" if bmsa.band_upper < price else "resistance"
                cands.append(RawCandidate(
                    price=round(bmsa.band_upper, 2), side=side,
                    dimension="math_indicator", source="牛市支撑带上沿(20W SMA)",
                    source_tag="bmsa_upper", base_score=35, timeframe="1W",
                ))
            if abs(bmsa.band_lower - price) / price < 0.20:
                side = "support" if bmsa.band_lower < price else "resistance"
                cands.append(RawCandidate(
                    price=round(bmsa.band_lower, 2), side=side,
                    dimension="math_indicator", source="牛市支撑带下沿(21W EMA)",
                    source_tag="bmsa_lower", base_score=35, timeframe="1W",
                ))

    # Fibonacci
    if candles_1d and len(candles_1d) >= 30:
        highs = [c.high for c in candles_1d]
        lows = [c.low for c in candles_1d]
        ts_list = [c.ts for c in candles_1d]
        sh, sl = find_major_swing(highs, lows, ts_list, lookback=10)
        if sh and sl and sh.price > sl.price:
            direction = "up" if sh.index > sl.index else "down"
            fib_levels = calc_fibonacci_levels(sh.price, sl.price, direction)
            result.fib_levels = fib_levels
            result.fib_swing_high = sh.price
            result.fib_swing_low = sl.price
            result.fib_direction = direction
            key_ratios = {0.382, 0.5, 0.618}
            for fl in fib_levels:
                if fl.ratio not in key_ratios:
                    continue
                if fl.price <= 0 or abs(fl.price - price) / price > 0.15:
                    continue
                side = "support" if fl.price < price else "resistance"
                label = "黄金支撑区" if fl.ratio == 0.618 and side == "support" else fl.label
                cands.append(RawCandidate(
                    price=fl.price, side=side,
                    dimension="math_indicator", source=label,
                    source_tag=f"fib_{fl.ratio}", base_score=22, timeframe="1D",
                ))

    # Pivot Points (日线)
    if candles_1d and len(candles_1d) >= 2:
        prev = candles_1d[-2]
        pivot = calc_pivot_classic(prev.high, prev.low, prev.close)
        for label, val in [
            ("Pivot", pivot.pivot), ("Pivot S1", pivot.s1), ("Pivot S2", pivot.s2),
            ("Pivot R1", pivot.r1), ("Pivot R2", pivot.r2),
        ]:
            if val > 0 and abs(val - price) / price < 0.10:
                side = "support" if val < price else "resistance"
                cands.append(RawCandidate(
                    price=round(val, 2), side=side,
                    dimension="math_indicator", source=label,
                    source_tag=f"pivot_{label.lower().replace(' ', '_')}",
                    base_score=15, timeframe="1D",
                ))

    # Ichimoku
    if candles_1d and len(candles_1d) >= 52:
        highs_d = [c.high for c in candles_1d]
        lows_d = [c.low for c in candles_1d]
        closes_d = [c.close for c in candles_1d]
        ichi = calc_ichimoku(highs_d, lows_d, closes_d)
        result.ichimoku = ichi
        if ichi.cloud_top and abs(ichi.cloud_top - price) / price < 0.10:
            side = "support" if ichi.cloud_top < price else "resistance"
            cands.append(RawCandidate(
                price=round(ichi.cloud_top, 2), side=side,
                dimension="math_indicator", source="一目均衡云层上沿",
                source_tag="ichimoku_cloud_top", base_score=20, timeframe="1D",
            ))
        if ichi.cloud_bottom and abs(ichi.cloud_bottom - price) / price < 0.10:
            side = "support" if ichi.cloud_bottom < price else "resistance"
            cands.append(RawCandidate(
                price=round(ichi.cloud_bottom, 2), side=side,
                dimension="math_indicator", source="一目均衡云层下沿",
                source_tag="ichimoku_cloud_bottom", base_score=20, timeframe="1D",
            ))
        if ichi.kijun and abs(ichi.kijun - price) / price < 0.05:
            side = "support" if ichi.kijun < price else "resistance"
            cands.append(RawCandidate(
                price=round(ichi.kijun, 2), side=side,
                dimension="math_indicator", source="一目均衡基准线",
                source_tag="ichimoku_kijun", base_score=15, timeframe="1D",
            ))

    # Bollinger Bands
    if boll_data:
        for key, tag, label in [
            ("upper", "boll_upper_1d", "日线布林上轨"),
            ("lower", "boll_lower_1d", "日线布林下轨"),
        ]:
            val = boll_data.get(key)
            if val and val > 0 and abs(val - price) / price < 0.10:
                side = "support" if val < price else "resistance"
                cands.append(RawCandidate(
                    price=round(val, 2), side=side,
                    dimension="math_indicator", source=label,
                    source_tag=tag, base_score=12, timeframe="1D",
                ))

    # Keltner Channel (从日线 K 线本地算)
    if candles_1d and len(candles_1d) >= 30:
        closes_d = [c.close for c in candles_1d]
        highs_d = [c.high for c in candles_1d]
        lows_d = [c.low for c in candles_1d]
        kc = calc_keltner(closes_d, highs_d, lows_d)
        result.keltner = kc

    # 箱体边界（从 range_signal 继承）
    # 已在价格结构里处理了 unfilled wick；range 的 upper/lower 可作为候选
    # 但避免与 MA120/MA60 重复，这里不重复添加


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 维度 C: 资金与仓位
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _discover_capital_flow(
    cands: list[RawCandidate],
    price: float,
    atr: float,
    liq_map: LiquidationMap | None,
    liq_map_7d: LiquidationMap | None,
    vp: VolumeProfileData | None,
    orderbook: OrderBookAnalysis | None,
    vwap: float,
    cycle_position: CyclePositionData | None,
    oi_history: list[dict] | None = None,
    candles_1h: list[CandleData] | None = None,
):
    # 清算簇（24h — 高权重，数据新鲜）
    if liq_map:
        for c in liq_map.clusters_below:
            if c.distance_pct > 15:
                continue
            score = min(c.total_usd / 1e6, 40)
            cands.append(RawCandidate(
                price=c.price_from, side="support",
                dimension="capital_flow",
                source=f"{c.dominant_leverage}x多头清算{fmt_usd_cn(c.total_usd)}",
                source_tag="liq_cluster_below_1d",
                base_score=score, timeframe="1D", data_age_hours=0,
            ))
        for c in liq_map.clusters_above:
            if c.distance_pct > 15:
                continue
            score = min(c.total_usd / 1e6, 40)
            cands.append(RawCandidate(
                price=c.price_to, side="resistance",
                dimension="capital_flow",
                source=f"{c.dominant_leverage}x空头清算{fmt_usd_cn(c.total_usd)}",
                source_tag="liq_cluster_above_1d",
                base_score=score, timeframe="1D", data_age_hours=0,
            ))

    # 清算簇（7d — 远距覆盖，较低权重，各方向最多 6 个）
    _MAX_7D_LIQ_PER_SIDE = 6
    if liq_map_7d:
        existing_prices = {round(c.price, -1) for c in cands}
        below_sorted = sorted(
            [c for c in liq_map_7d.clusters_below if 5 <= c.distance_pct <= 20],
            key=lambda c: c.total_usd, reverse=True,
        )
        added_below = 0
        for c in below_sorted:
            if added_below >= _MAX_7D_LIQ_PER_SIDE:
                break
            if round(c.price_from, -1) in existing_prices:
                continue
            cands.append(RawCandidate(
                price=c.price_from, side="support",
                dimension="capital_flow",
                source=f"7d清算簇{fmt_usd_cn(c.total_usd)}",
                source_tag="liq_cluster_below_7d",
                base_score=min(c.total_usd / 1e6, 25) * 0.7,
                timeframe="1D", data_age_hours=72,
            ))
            added_below += 1
        above_sorted = sorted(
            [c for c in liq_map_7d.clusters_above if 5 <= c.distance_pct <= 20],
            key=lambda c: c.total_usd, reverse=True,
        )
        added_above = 0
        for c in above_sorted:
            if added_above >= _MAX_7D_LIQ_PER_SIDE:
                break
            if round(c.price_to, -1) in existing_prices:
                continue
            cands.append(RawCandidate(
                price=c.price_to, side="resistance",
                dimension="capital_flow",
                source=f"7d清算簇{fmt_usd_cn(c.total_usd)}",
                source_tag="liq_cluster_above_7d",
                base_score=min(c.total_usd / 1e6, 25) * 0.7,
                timeframe="1D", data_age_hours=72,
            ))
            added_above += 1

    # Volume Profile POC + VA
    if vp:
        if vp.poc_price > 0 and abs(vp.poc_price - price) / price < 0.10:
            side = "support" if vp.poc_price < price else "resistance"
            cands.append(RawCandidate(
                price=round(vp.poc_price, 2), side=side,
                dimension="capital_flow", source="VP成交量控制点(POC)",
                source_tag="vp_poc", base_score=30, timeframe="1H",
            ))
        if vp.value_area_low > 0 and vp.value_area_low < price:
            cands.append(RawCandidate(
                price=round(vp.value_area_low, 2), side="support",
                dimension="capital_flow", source="VP价值区下沿(VAL)",
                source_tag="vp_val", base_score=20, timeframe="1H",
            ))
        if vp.value_area_high > 0 and vp.value_area_high > price:
            cands.append(RawCandidate(
                price=round(vp.value_area_high, 2), side="resistance",
                dimension="capital_flow", source="VP价值区上沿(VAH)",
                source_tag="vp_vah", base_score=20, timeframe="1H",
            ))

    # VWAP
    if vwap > 0 and abs(vwap - price) / price < 0.10:
        side = "support" if vwap < price else "resistance"
        cands.append(RawCandidate(
            price=round(vwap, 2), side=side,
            dimension="capital_flow", source="日线VWAP",
            source_tag="vwap", base_score=15, timeframe="1D",
        ))

    # 订单簿大单
    if orderbook:
        for wall in orderbook.bid_walls[:3]:
            usd_m = wall.size_usd / 1e6
            if usd_m < 0.5:
                continue
            cands.append(RawCandidate(
                price=round(wall.price, 2), side="support",
                dimension="capital_flow", source=f"买墙{fmt_usd_cn(wall.size_usd)}",
                source_tag="orderbook_bid_wall",
                base_score=min(usd_m * 10, 25), timeframe="1H",
                data_age_hours=0,
            ))
        for wall in orderbook.ask_walls[:3]:
            usd_m = wall.size_usd / 1e6
            if usd_m < 0.5:
                continue
            cands.append(RawCandidate(
                price=round(wall.price, 2), side="resistance",
                dimension="capital_flow", source=f"卖墙{fmt_usd_cn(wall.size_usd)}",
                source_tag="orderbook_ask_wall",
                base_score=min(usd_m * 10, 25), timeframe="1H",
                data_age_hours=0,
            ))

    # 链上周期价位（BTC 特有）
    if cycle_position:
        onchain_levels = [
            (cycle_position.sma_200w, 40, "200周均线(周期支撑)"),
            (cycle_position.sth_cost_1d, 25, "STH成本(短期盈亏线)"),
            (cycle_position.sth_cost_1w, 20, "STH成本1周"),
            (cycle_position.sth_cost_1m, 18, "STH成本1月"),
            (cycle_position.pi_350dma, 22, "Pi周期350DMA"),
            (cycle_position.cvdd, 35, "CVDD(已销毁币天价值)"),
        ]
        for val, score, source in onchain_levels:
            if not val or val <= 0:
                continue
            dist_pct = abs(val - price) / price * 100
            if dist_pct > 25:
                continue
            side = "support" if val < price else "resistance"
            cands.append(RawCandidate(
                price=round(val, 2), side=side,
                dimension="capital_flow", source=source,
                source_tag=f"onchain_{source[:4].lower()}",
                base_score=score, timeframe="1W",
            ))

    # OI 骤变区
    if oi_history and candles_1h:
        surge_zones = detect_oi_surge_zones(oi_history, candles_1h, threshold_pct=5.0)
        for surge_price, change_pct in surge_zones:
            if abs(surge_price - price) / price > 0.10:
                continue
            if change_pct > 0:
                side = "resistance" if surge_price > price else "support"
                source = f"OI骤增区(+{change_pct:.0f}%)"
            else:
                side = "support" if surge_price < price else "resistance"
                source = f"OI骤减区({change_pct:.0f}%)"
            cands.append(RawCandidate(
                price=surge_price, side=side,
                dimension="capital_flow", source=source,
                source_tag="oi_surge_zone",
                base_score=min(abs(change_pct) * 2, 30), timeframe="1H",
                data_age_hours=0,
            ))
