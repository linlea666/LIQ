"""CVD 计算引擎：基于 OKX taker-volume 的简化方案"""

from __future__ import annotations

import logging

from models.flow import CVDData, CVDPoint

logger = logging.getLogger(__name__)


def build_cvd(
    raw_points: list[CVDPoint],
    inst_type: str,
    coin: str,
) -> CVDData:
    """
    从 OKX taker-volume 原始数据构建 CVD 数据集。
    raw_points 已经由 okx_rest.fetch_taker_volume() 预计算了逐点 cvd。
    此处负责趋势判断和背离检测。
    """
    if not raw_points:
        return CVDData(coin=coin, inst_type=inst_type, series=[])

    trend_1h, delta_1h = _calc_trend(raw_points, lookback_points=12)

    return CVDData(
        coin=coin,
        inst_type=inst_type,
        series=raw_points,
        trend_1h=trend_1h,
        delta_1h=delta_1h,
    )


def detect_cvd_price_divergence(
    cvd: CVDData,
    price_series: list[float],
    price_ts: list[int],
) -> CVDData:
    """
    检测 CVD 与价格的背离：
    - 价格创新高但 CVD 未创新高 → 顶背离 (bearish)
    - 价格创新低但 CVD 未创新低 → 底背离 (bullish)
    """
    if len(cvd.series) < 12 or len(price_series) < 12:
        return cvd

    recent_prices = price_series[-12:]
    recent_cvd = [p.cvd for p in cvd.series[-12:]]
    earlier_prices = price_series[-24:-12] if len(price_series) >= 24 else price_series[:12]
    earlier_cvd = [p.cvd for p in cvd.series[-24:-12]] if len(cvd.series) >= 24 else [p.cvd for p in cvd.series[:12]]

    if not earlier_prices or not earlier_cvd:
        return cvd

    price_new_high = max(recent_prices) > max(earlier_prices)
    cvd_new_high = max(recent_cvd) > max(earlier_cvd)
    price_new_low = min(recent_prices) < min(earlier_prices)
    cvd_new_low = min(recent_cvd) < min(earlier_cvd)

    if price_new_high and not cvd_new_high:
        cvd.has_divergence = True
        cvd.divergence_note = "顶背离: 价格创新高但CVD未跟随，主力可能在派发"
    elif price_new_low and not cvd_new_low:
        cvd.has_divergence = True
        cvd.divergence_note = "底背离: 价格创新低但CVD未跟随，抛压可能衰竭"
    else:
        cvd.has_divergence = False
        cvd.divergence_note = ""

    return cvd


def _calc_trend(points: list[CVDPoint], lookback_points: int = 12) -> tuple[str, float]:
    """计算最近 lookback_points 个数据点的 CVD 趋势"""
    if len(points) < 2:
        return "flat", 0.0

    recent = points[-lookback_points:]
    delta_sum = sum(p.delta for p in recent)

    if len(recent) < 2:
        return "flat", delta_sum

    start_cvd = recent[0].cvd
    end_cvd = recent[-1].cvd
    diff = end_cvd - start_cvd

    threshold = abs(delta_sum) * 0.1 if delta_sum != 0 else 1.0
    if diff > threshold:
        return "rising", delta_sum
    elif diff < -threshold:
        return "declining", delta_sum
    else:
        return "flat", delta_sum
