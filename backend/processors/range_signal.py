"""均线箱体信号处理器：多时间框架 MA + MACD → 箱体识别 → 信号分级

策略内核（来源：UP主"数字投行洪七公"系统化提炼）：
- 箱体上沿 = 日线 MA120（≈2日线 MA60）阻力，MACD 0 轴下方时有效
- 箱体下沿 = 未回补影线低点 或 周线 MA60 支撑
- A 级做空 = 价格反弹至上沿 + MACD 0 轴下方
- A 级做多 = 价格下探至下沿 + 流动性扫取确认
- 中间区域 = 无信号，不交易
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from models.flow import RangeSignalData
from models.market import CandleData
from processors.ta_core import calc_macd, calc_sma, last_valid

logger = logging.getLogger(__name__)


def calculate_range_signal(
    candles_1h: list[CandleData],
    candles_1d: list[CandleData],
    candles_1w: list[CandleData],
    current_price: float,
    atr: float,
    sweep_above_1h: float = 0,
    sweep_below_1h: float = 0,
    cps: float | None = None,
    cfg: dict | None = None,
) -> RangeSignalData | None:
    """从多时间框架K线计算箱体信号。

    Args:
        candles_1h: 1H K线（用于 wick gap 检测）
        candles_1d: 日线K线（MA60/MA120/MACD）
        candles_1w: 周线K线（MA60）
        current_price: 当前价格
        atr: 1H ATR(14)
        sweep_above_1h/sweep_below_1h: 近1h 扫取的 BSL/SSL 金额
        cps: CPS 周期评分（可选）
        cfg: range_signal 配置参数

    Returns:
        RangeSignalData 或 None（数据不足时）
    """
    if not candles_1d or len(candles_1d) < 26 or current_price <= 0:
        return None

    cfg = cfg or {}
    near_pct = cfg.get("near_boundary_pct", 1.5)
    wick_body_ratio = cfg.get("wick_body_ratio", 0.30)
    wick_min_atr_mult = cfg.get("wick_min_atr_mult", 1.2)

    daily_closes = [c.close for c in candles_1d]

    # ── MA 计算 ──
    sma60_d = calc_sma(daily_closes, 60)
    sma120_d = calc_sma(daily_closes, 120)
    ma60_daily = last_valid(sma60_d)
    ma120_daily = last_valid(sma120_d)

    ma60_weekly = None
    if candles_1w and len(candles_1w) >= 60:
        weekly_closes = [c.close for c in candles_1w]
        sma60_w = calc_sma(weekly_closes, 60)
        ma60_weekly = last_valid(sma60_w)

    # ── MACD（日线级别）──
    macd_result = calc_macd(daily_closes, fast=12, slow=26, signal=9)
    macd_above_zero = macd_result["above_zero"]
    macd_histogram = last_valid(macd_result["histogram"])
    macd_hist_rising = macd_result["histogram_rising"]

    # ── 未回补影线检测（unfilled wick）──
    unfilled_low, unfilled_high = _detect_unfilled_wicks(
        candles_1d, current_price, atr, wick_body_ratio, wick_min_atr_mult,
    )

    # ── 箱体边界识别 ──
    range_upper, upper_src = _identify_upper_boundary(
        ma120_daily, ma60_daily, current_price,
    )
    range_lower, lower_src = _identify_lower_boundary(
        unfilled_low, ma60_weekly, ma60_daily, current_price,
    )

    # ── 价格在箱体中的位置 ──
    price_pos, pos_pct = _calc_price_position(
        current_price, range_upper, range_lower, near_pct,
    )

    # ── 信号分级 ──
    grade, direction, reason = _grade_signal(
        price_pos, pos_pct, near_pct,
        macd_above_zero, macd_hist_rising,
        sweep_above_1h, sweep_below_1h,
        range_upper, range_lower, current_price,
    )

    # ── CPS 一致性 ──
    cps_aligned = False
    if cps is not None and direction:
        if direction == "long" and cps >= 4:
            cps_aligned = True
        elif direction == "short" and cps <= 6:
            cps_aligned = True

    sweep_confirmed = (
        (direction == "long" and sweep_below_1h > 0) or
        (direction == "short" and sweep_above_1h > 0)
    )

    return RangeSignalData(
        ts=int(time.time()),
        ma60_daily=_round_opt(ma60_daily),
        ma120_daily=_round_opt(ma120_daily),
        ma60_weekly=_round_opt(ma60_weekly),
        macd_daily_above_zero=macd_above_zero,
        macd_daily_histogram=round(macd_histogram, 2) if macd_histogram is not None else None,
        macd_daily_hist_rising=macd_hist_rising,
        range_upper=_round_opt(range_upper),
        range_upper_source=upper_src,
        range_lower=_round_opt(range_lower),
        range_lower_source=lower_src,
        price_position=price_pos,
        price_position_pct=round(pos_pct, 1),
        unfilled_wick_low=_round_opt(unfilled_low),
        unfilled_wick_high=_round_opt(unfilled_high),
        signal_grade=grade,
        signal_direction=direction,
        signal_reason=reason,
        sweep_confirmed=sweep_confirmed,
        cps_aligned=cps_aligned,
    )


# ── 内部函数 ──


def _detect_unfilled_wicks(
    candles: list[CandleData],
    current_price: float,
    atr: float,
    body_ratio_threshold: float,
    min_atr_mult: float,
) -> tuple[float | None, float | None]:
    """检测最近未被回补的大影线。

    "大影线"定义：K线实体占比 ≤ body_ratio_threshold 的那一侧，
    且影线长度 ≥ atr * min_atr_mult。

    从最近的 K 线往回扫描，找到第一个下影线低点尚未被后续价格触及的。
    """
    if atr <= 0 or len(candles) < 2:
        return None, None

    unfilled_low: float | None = None
    unfilled_high: float | None = None
    min_atr_len = atr * min_atr_mult

    for i in range(len(candles) - 2, max(len(candles) - 30, -1), -1):
        c = candles[i]
        body = abs(c.close - c.open)
        total = c.high - c.low
        if total <= 0:
            continue

        lower_wick = min(c.open, c.close) - c.low
        upper_wick = c.high - max(c.open, c.close)

        body_pct = body / total

        # 下影线检测
        if (unfilled_low is None
                and lower_wick >= min_atr_len
                and body_pct >= (1 - body_ratio_threshold)):
            wick_low = c.low
            filled = any(candles[j].low <= wick_low * 1.002 for j in range(i + 1, len(candles)))
            if not filled:
                unfilled_low = wick_low

        # 上影线检测
        if (unfilled_high is None
                and upper_wick >= min_atr_len
                and body_pct >= (1 - body_ratio_threshold)):
            wick_high = c.high
            filled = any(candles[j].high >= wick_high * 0.998 for j in range(i + 1, len(candles)))
            if not filled:
                unfilled_high = wick_high

        if unfilled_low is not None and unfilled_high is not None:
            break

    return unfilled_low, unfilled_high


def _identify_upper_boundary(
    ma120_daily: float | None,
    ma60_daily: float | None,
    current_price: float,
) -> tuple[float | None, str]:
    """识别箱体上沿：优先 MA120（≈2日线MA60），其次日线 MA60。"""
    if ma120_daily is not None and ma120_daily > current_price:
        return ma120_daily, "日线MA120(≈2日MA60)"
    if ma60_daily is not None and ma60_daily > current_price:
        return ma60_daily, "日线MA60"
    if ma120_daily is not None:
        return ma120_daily, "日线MA120(≈2日MA60)"
    if ma60_daily is not None:
        return ma60_daily, "日线MA60"
    return None, ""


def _identify_lower_boundary(
    unfilled_low: float | None,
    ma60_weekly: float | None,
    ma60_daily: float | None,
    current_price: float,
) -> tuple[float | None, str]:
    """识别箱体下沿：优先未回补影线低点，其次周线 MA60，最后日线 MA60。"""
    if unfilled_low is not None and unfilled_low < current_price:
        return unfilled_low, "未回补影线低点"
    if ma60_weekly is not None and ma60_weekly < current_price:
        return ma60_weekly, "周线MA60"
    if ma60_daily is not None and ma60_daily < current_price:
        return ma60_daily, "日线MA60"
    return None, ""


def _calc_price_position(
    price: float,
    upper: float | None,
    lower: float | None,
    near_pct: float,
) -> tuple[str, float]:
    """计算价格在箱体中的相对位置。"""
    if upper is None or lower is None or upper <= lower:
        return "middle", 50.0

    span = upper - lower
    pct = (price - lower) / span * 100
    pct = max(0.0, min(100.0, pct))

    if pct >= (100 - near_pct / (span / price * 100) * 100):
        dist_to_upper = abs(price - upper) / price * 100
        if dist_to_upper <= near_pct:
            return "near_upper", pct

    dist_to_lower = abs(price - lower) / price * 100
    if dist_to_lower <= near_pct:
        return "near_lower", pct

    return "middle", pct


def _grade_signal(
    price_pos: str,
    pos_pct: float,
    near_pct: float,
    macd_above_zero: bool | None,
    macd_hist_rising: bool | None,
    sweep_above: float,
    sweep_below: float,
    range_upper: float | None,
    range_lower: float | None,
    current_price: float,
) -> tuple[str | None, str | None, str]:
    """根据价格位置 + MACD 状态 + sweep 事件给出信号分级。

    A 级条件：
    - 做空：near_upper + MACD 0轴下方
    - 做多：near_lower + 下方流动性被扫取(sweep_below > 0)

    B 级条件：
    - 做空：near_upper（MACD 未确认/0轴上方）
    - 做多：near_lower（无 sweep 确认）
    """
    if price_pos == "near_upper" and range_upper is not None:
        if macd_above_zero is False:
            reason = (
                f"价格接近箱体上沿${range_upper:,.0f}(MA阻力) + "
                f"MACD在0轴下方 → A级做空信号"
            )
            if sweep_above > 0:
                reason += "（已扫取上方BSL，动能减弱确认）"
            return "A", "short", reason
        else:
            reason = f"价格接近箱体上沿${range_upper:,.0f}(MA阻力)"
            if macd_above_zero is True:
                reason += "，但MACD在0轴上方（多头趋势），做空需谨慎"
            return "B", "short", reason

    if price_pos == "near_lower" and range_lower is not None:
        if sweep_below > 0:
            reason = (
                f"价格接近箱体下沿${range_lower:,.0f} + "
                f"下方SSL已被扫取(${sweep_below/1e6:.1f}M) → A级做多信号"
            )
            return "A", "long", reason
        else:
            reason = f"价格接近箱体下沿${range_lower:,.0f}，等待流动性扫取确认升级为A级"
            return "B", "long", reason

    return None, None, ""


def _round_opt(v: float | None, ndigits: int = 2) -> float | None:
    return round(v, ndigits) if v is not None else None
