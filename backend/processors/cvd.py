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
    检测 CVD 与价格的背离（基于时间戳对齐）：
    CVD 为 5 分钟级别, price_series 为 1H K 线。
    用 CVD 时间窗口去匹配同期 K 线价格, 避免因时间粒度不同导致错位。
    """
    if len(cvd.series) < 12:
        return cvd
    if len(price_series) < 2 or len(price_ts) != len(price_series):
        return cvd

    cvd_start_ts = cvd.series[0].ts
    cvd_end_ts = cvd.series[-1].ts
    cvd_mid_ts = cvd.series[len(cvd.series) // 2].ts

    one_hour_ms = 3_600_000
    aligned = [(p, t) for p, t in zip(price_series, price_ts)
               if cvd_start_ts - one_hour_ms <= t <= cvd_end_ts + one_hour_ms]

    if len(aligned) < 2:
        return cvd

    earlier_prices = [p for p, t in aligned if t <= cvd_mid_ts]
    recent_prices = [p for p, t in aligned if t > cvd_mid_ts]

    if not earlier_prices or not recent_prices:
        mid = len(aligned) // 2
        earlier_prices = [p for p, _ in aligned[:mid]]
        recent_prices = [p for p, _ in aligned[mid:]]

    if not earlier_prices or not recent_prices:
        return cvd

    half = len(cvd.series) // 2
    earlier_cvd = [p.cvd for p in cvd.series[:half]]
    recent_cvd = [p.cvd for p in cvd.series[half:]]

    min_price_pct = 0.003
    earlier_price_max = max(earlier_prices)
    earlier_price_min = min(earlier_prices)
    recent_price_max = max(recent_prices)
    recent_price_min = min(recent_prices)

    price_new_high = (recent_price_max > earlier_price_max and
                      (recent_price_max - earlier_price_max) / earlier_price_max > min_price_pct)
    price_new_low = (recent_price_min < earlier_price_min and
                     (earlier_price_min - recent_price_min) / earlier_price_min > min_price_pct)

    cvd_new_high = max(recent_cvd) > max(earlier_cvd)
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

    abs_values = [abs(p.delta) for p in recent if p.delta != 0]
    median_abs = sorted(abs_values)[len(abs_values) // 2] if abs_values else 1.0
    threshold = max(median_abs * 0.5, abs(delta_sum) * 0.05)
    if diff > threshold:
        return "rising", delta_sum
    elif diff < -threshold:
        return "declining", delta_sum
    else:
        return "flat", delta_sum
