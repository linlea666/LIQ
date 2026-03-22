"""Volume Profile + VWAP + ATR 计算"""

from __future__ import annotations

import logging

from models.market import CandleData, VolumeProfileBin, VolumeProfileData

logger = logging.getLogger(__name__)


def calc_volume_profile(
    candles: list[CandleData],
    num_bins: int = 50,
    coin: str = "BTC",
) -> VolumeProfileData | None:
    """
    从K线数据计算 Volume Profile：
    - POC (Point of Control): 成交量最密集的价格区间
    - Value Area: 包含 68% 成交量的价格区间
    - VWAP
    """
    if len(candles) < 5:
        return None

    all_highs = [c.high for c in candles]
    all_lows = [c.low for c in candles]
    price_max = max(all_highs)
    price_min = min(all_lows)
    price_range = price_max - price_min

    if price_range <= 0:
        return None

    bin_size = price_range / num_bins
    bins: list[dict] = []
    for i in range(num_bins):
        low = price_min + i * bin_size
        high = low + bin_size
        bins.append({"low": low, "high": high, "vol": 0.0})

    total_vol = 0.0
    vwap_numerator = 0.0
    vwap_denominator = 0.0

    for c in candles:
        typical_price = (c.high + c.low + c.close) / 3
        vol = c.vol_ccy if c.vol_ccy > 0 else c.vol

        vwap_numerator += typical_price * vol
        vwap_denominator += vol
        total_vol += vol

        c_low = min(c.low, c.high)
        c_high = max(c.low, c.high)
        c_span = c_high - c_low
        for b in bins:
            overlap_lo = max(b["low"], c_low)
            overlap_hi = min(b["high"], c_high)
            if overlap_hi > overlap_lo:
                fraction = (overlap_hi - overlap_lo) / c_span if c_span > 0 else 1.0
                b["vol"] += vol * fraction

    vwap = vwap_numerator / vwap_denominator if vwap_denominator > 0 else candles[-1].close

    poc_bin = max(bins, key=lambda b: b["vol"])
    poc_price = (poc_bin["low"] + poc_bin["high"]) / 2

    poc_idx = bins.index(poc_bin)
    va_cumulative = poc_bin["vol"]
    va_lo_idx = poc_idx
    va_hi_idx = poc_idx
    target_vol = total_vol * 0.68
    while va_cumulative < target_vol:
        expand_lo = bins[va_lo_idx - 1]["vol"] if va_lo_idx > 0 else -1
        expand_hi = bins[va_hi_idx + 1]["vol"] if va_hi_idx < num_bins - 1 else -1
        if expand_lo < 0 and expand_hi < 0:
            break
        if expand_lo >= expand_hi:
            va_lo_idx -= 1
            va_cumulative += bins[va_lo_idx]["vol"]
        else:
            va_hi_idx += 1
            va_cumulative += bins[va_hi_idx]["vol"]

    va_low = bins[va_lo_idx]["low"]
    va_high = bins[va_hi_idx]["high"]

    profile_bins = [
        VolumeProfileBin(
            price_low=b["low"],
            price_high=b["high"],
            volume=b["vol"],
        )
        for b in bins
    ]

    ts = candles[-1].ts if candles else 0
    return VolumeProfileData(
        coin=coin,
        ts=ts,
        bins=profile_bins,
        poc_price=round(poc_price, 2),
        value_area_high=round(va_high, 2),
        value_area_low=round(va_low, 2),
        vwap=round(vwap, 2),
    )


def calc_atr(candles: list[CandleData], period: int = 14) -> float:
    """Average True Range"""
    if len(candles) < period + 1:
        return 0.0

    trs: list[float] = []
    for i in range(1, len(candles)):
        h, l, pc = candles[i].high, candles[i].low, candles[i - 1].close
        tr = max(h - l, abs(h - pc), abs(l - pc))
        trs.append(tr)

    if len(trs) < period:
        return sum(trs) / len(trs) if trs else 0.0

    return sum(trs[-period:]) / period
