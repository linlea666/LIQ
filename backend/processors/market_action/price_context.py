"""价格上下文派生 · swing H/L + 20d 区间 + VP POC/VAH/VAL 相对位置"""

from __future__ import annotations

from typing import Optional

from models.market_action import PriceContextSnapshot


def _compute_swing(candles_1h: list, lookback: int = 20) -> tuple[Optional[float], Optional[float]]:
    """对最近 N 根 1h K 线取 swing high/low。candles 可能是 dict 或 CandleData 对象。"""
    if not candles_1h:
        return None, None
    recent = candles_1h[-lookback:]
    highs = []
    lows = []
    for c in recent:
        if isinstance(c, dict):
            h = c.get("high", c.get("h"))
            l = c.get("low", c.get("l"))
        else:
            h = getattr(c, "high", None)
            l = getattr(c, "low", None)
        if h is not None:
            try:
                highs.append(float(h))
            except (TypeError, ValueError):
                pass
        if l is not None:
            try:
                lows.append(float(l))
            except (TypeError, ValueError):
                pass
    return (max(highs) if highs else None), (min(lows) if lows else None)


def _compute_20d_range(candles_daily: list, lookback: int = 20) -> tuple[Optional[float], Optional[float]]:
    if not candles_daily:
        return None, None
    recent = candles_daily[-lookback:]
    highs = []
    lows = []
    for c in recent:
        if isinstance(c, dict):
            h = c.get("high", c.get("h"))
            l = c.get("low", c.get("l"))
        else:
            h = getattr(c, "high", None)
            l = getattr(c, "low", None)
        if h is not None:
            try:
                highs.append(float(h))
            except (TypeError, ValueError):
                pass
        if l is not None:
            try:
                lows.append(float(l))
            except (TypeError, ValueError):
                pass
    return (max(highs) if highs else None), (min(lows) if lows else None)


def build_price_context(
    current_price: Optional[float],
    candles_1h: Optional[list],
    candles_daily: Optional[list],
    volume_profile: Optional[object],
) -> PriceContextSnapshot:
    snap = PriceContextSnapshot()
    if current_price is None or current_price <= 0:
        return snap

    swing_h, swing_l = _compute_swing(candles_1h or [])
    range_h, range_l = _compute_20d_range(candles_daily or [])
    snap.swing_high_1h = swing_h
    snap.swing_low_1h = swing_l
    snap.range_20d_high = range_h
    snap.range_20d_low = range_l

    if range_h is not None and range_l is not None and range_h > range_l:
        pos = (current_price - range_l) / (range_h - range_l) * 100
        snap.range_position_pct = round(max(0.0, min(100.0, pos)), 2)

    if swing_h is not None:
        snap.distance_to_swing_high_pct = round((swing_h - current_price) / current_price * 100, 3)
    if swing_l is not None:
        snap.distance_to_swing_low_pct = round((current_price - swing_l) / current_price * 100, 3)

    # Volume Profile：兼容 VolumeProfileData / dict
    poc = vah = val = None
    if volume_profile is not None:
        for attr in ("poc_price", "poc"):
            v = getattr(volume_profile, attr, None) if not isinstance(volume_profile, dict) else volume_profile.get(attr)
            if v is not None:
                try:
                    poc = float(v)
                    break
                except (TypeError, ValueError):
                    pass
        for attr in ("vah_price", "vah"):
            v = getattr(volume_profile, attr, None) if not isinstance(volume_profile, dict) else volume_profile.get(attr)
            if v is not None:
                try:
                    vah = float(v)
                    break
                except (TypeError, ValueError):
                    pass
        for attr in ("val_price", "val"):
            v = getattr(volume_profile, attr, None) if not isinstance(volume_profile, dict) else volume_profile.get(attr)
            if v is not None:
                try:
                    val = float(v)
                    break
                except (TypeError, ValueError):
                    pass

    snap.poc_price = poc
    snap.vah_price = vah
    snap.val_price = val
    if poc is not None:
        if current_price > poc * 1.002:
            snap.price_vs_poc = "above"
        elif current_price < poc * 0.998:
            snap.price_vs_poc = "below"
        else:
            snap.price_vs_poc = "at"

    return snap
