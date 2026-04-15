"""箱体信号处理器 V2 — 基于关键位多维共振的智能箱体检测

架构：箱体 = 关键位 V2 的"消费者"，从 KeyLevelSnapshotV2 中提取
最近的强支撑/阻力对作为箱体边界，叠加状态机、突破概率、信号分级。

保留洪七公策略的 MA/MACD 方向性过滤作为加分项。
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from models.flow import RangeSignalData
from models.key_level import KeyLevelSnapshotV2, KeyLevelV2
from models.market import CandleData
from processors.ta_core import calc_macd, calc_sma, last_valid

logger = logging.getLogger(__name__)

# 箱体状态常量
_ST_NONE = "none"
_ST_FORMING = "forming"
_ST_CONFIRMED = "confirmed"
_ST_MATURE = "mature"
_ST_SQUEEZE = "squeeze"
_ST_BREAKING_UP = "breaking_up"
_ST_BREAKING_DOWN = "breaking_down"
_ST_BROKEN = "broken"

_MIN_BOX_WIDTH_PCT = 2.5   # 核心箱体最小宽度 2.5%
_MAX_BOX_WIDTH_PCT = 15.0  # 核心箱体最大宽度 15%
_MIN_BOUNDARY_SCORE = 15   # 边界最小共振分
_MATURE_HOURS = 72         # 72h 后进入 mature

_CAPITAL_KEYWORDS = ("清算", "liq", "orderbook", "挂单", "大额")

def _is_structural_source(sources: list[str]) -> bool:
    """判断 level 来源是否包含结构性维度（非纯资金/清算）。"""
    for s in sources:
        s_lower = s.lower()
        if not any(kw in s_lower for kw in _CAPITAL_KEYWORDS):
            return True
    return False

def calculate_range_signal(
    kl_snapshot: KeyLevelSnapshotV2 | None,
    current_price: float,
    atr: float,
    candles_1d: list[CandleData] | None = None,
    candles_1w: list[CandleData] | None = None,
    prev_range: RangeSignalData | None = None,
    sweep_above_1h: float = 0,
    sweep_below_1h: float = 0,
    cps: float | None = None,
    bb_squeeze: bool = False,
    oi_change_1h: float = 0,
    funding_rate: float | None = None,
    orderbook_bid_total: float = 0,
    orderbook_ask_total: float = 0,
    cfg: dict | None = None,
) -> RangeSignalData | None:
    """从关键位 V2 快照计算箱体信号。

    核心流程：
    1. 从 kl_snapshot.levels 提取最近的强支撑 → 下沿，强阻力 → 上沿
    2. 验证箱体合法性（宽度、共振分）
    3. 叠加 MA/MACD 方向性过滤
    4. 状态机转换（forming → confirmed → mature → squeeze → breaking）
    5. 突破概率评估
    6. 信号分级
    """
    if current_price <= 0:
        return None

    cfg = cfg or {}
    near_pct = cfg.get("near_boundary_pct", 1.5)
    now_ts = int(time.time())

    # ── 1. 从关键位提取双层箱体边界 ──
    (upper_lv, lower_lv), (micro_up_lv, micro_dn_lv) = _extract_dual_boundaries(
        kl_snapshot, current_price,
    )

    range_upper = upper_lv.price if upper_lv else None
    range_lower = lower_lv.price if lower_lv else None
    micro_upper = micro_up_lv.price if micro_up_lv else None
    micro_lower = micro_dn_lv.price if micro_dn_lv else None

    # ── 2. MA 计算（保留洪七公参考）──
    ma60_daily = ma120_daily = ma60_weekly = None
    macd_above_zero = macd_histogram = macd_hist_rising = None

    if candles_1d and len(candles_1d) >= 26:
        daily_closes = [c.close for c in candles_1d]
        sma60_d = calc_sma(daily_closes, 60)
        sma120_d = calc_sma(daily_closes, 120)
        ma60_daily = last_valid(sma60_d)
        ma120_daily = last_valid(sma120_d)

        macd_result = calc_macd(daily_closes, fast=12, slow=26, signal=9)
        macd_above_zero = macd_result["above_zero"]
        macd_histogram = last_valid(macd_result["histogram"])
        macd_hist_rising = macd_result["histogram_rising"]

    if candles_1w and len(candles_1w) >= 60:
        weekly_closes = [c.close for c in candles_1w]
        sma60_w = calc_sma(weekly_closes, 60)
        ma60_weekly = last_valid(sma60_w)

    # ── 3. 未回补影线（保留）──
    wick_body_ratio = cfg.get("wick_body_ratio", 0.30)
    wick_min_atr_mult = cfg.get("wick_min_atr_mult", 1.2)
    unfilled_low, unfilled_high = None, None
    if candles_1d and atr > 0:
        unfilled_low, unfilled_high = _detect_unfilled_wicks(
            candles_1d, current_price, atr, wick_body_ratio, wick_min_atr_mult,
        )

    # ── 4. 箱体合法性验证 ──
    has_box = False
    box_width_pct = 0.0
    if range_upper and range_lower and range_upper > range_lower:
        box_width_pct = (range_upper - range_lower) / current_price * 100
        min_score = cfg.get("min_boundary_score", _MIN_BOUNDARY_SCORE)
        upper_ok = upper_lv and upper_lv.confluence_score >= min_score
        lower_ok = lower_lv and lower_lv.confluence_score >= min_score
        has_box = (
            _MIN_BOX_WIDTH_PCT <= box_width_pct <= _MAX_BOX_WIDTH_PCT
            and upper_ok
            and lower_ok
        )

    # ── 5. 价格位置 ──
    price_pos, pos_pct = _calc_price_position(
        current_price, range_upper, range_lower, near_pct,
    )

    # ── 6. 共振因子收集 ──
    sweep_confirmed = (
        (price_pos == "near_lower" and sweep_below_1h > 0) or
        (price_pos == "near_upper" and sweep_above_1h > 0)
    )
    cps_aligned = cps is not None and 3 <= cps <= 7
    oi_buildup = abs(oi_change_1h) > 3.0 if oi_change_1h else False
    volume_declining = _check_volume_declining(candles_1d) if candles_1d else False
    funding_extreme = funding_rate is not None and abs(funding_rate) > 0.03
    ob_imbalance = ""
    if orderbook_bid_total > 0 and orderbook_ask_total > 0:
        ratio = orderbook_bid_total / orderbook_ask_total
        if ratio > 1.5:
            ob_imbalance = "bid_heavy"
        elif ratio < 0.67:
            ob_imbalance = "ask_heavy"

    confluence_flags = [
        sweep_confirmed, cps_aligned, bb_squeeze, oi_buildup,
        volume_declining, funding_extreme, bool(ob_imbalance), has_box,
    ]
    confluence_count = sum(confluence_flags)

    # ── 7. 箱体状态机 ──
    box_state, box_state_ts, box_age_hours = _transition_box_state(
        has_box, price_pos, bb_squeeze,
        prev_range, now_ts,
        range_upper, range_lower, current_price,
    )

    # ── 8. 箱体质量评分 ──
    box_quality = _calc_box_quality(
        has_box, upper_lv, lower_lv, box_age_hours,
        volume_declining, bb_squeeze, box_width_pct,
    )

    # ── 9. 突破概率 ──
    breakout_prob, breakout_bias, breakout_reason = _calc_breakout_probability(
        box_state, bb_squeeze, oi_buildup, funding_extreme,
        ob_imbalance, volume_declining, box_age_hours,
        macd_above_zero, macd_hist_rising,
    )

    # ── 10. 信号分级 ──
    grade, direction, reason, entry, sl, tp1, rr = _grade_signal_v2(
        price_pos, has_box, box_state,
        macd_above_zero, macd_hist_rising,
        sweep_confirmed, bb_squeeze,
        upper_lv, lower_lv, current_price, atr,
        breakout_prob, breakout_bias,
    )

    micro_width = 0.0
    if micro_upper and micro_lower and micro_upper > micro_lower:
        micro_width = (micro_upper - micro_lower) / current_price * 100

    return RangeSignalData(
        ts=now_ts,
        range_upper=_round_opt(range_upper),
        range_upper_source=_build_source_str(upper_lv),
        range_upper_tier=upper_lv.strength_tier if upper_lv else "",
        range_upper_score=upper_lv.confluence_score if upper_lv else 0,
        range_upper_test_count=upper_lv.test_count if upper_lv else 0,
        range_lower=_round_opt(range_lower),
        range_lower_source=_build_source_str(lower_lv),
        range_lower_tier=lower_lv.strength_tier if lower_lv else "",
        range_lower_score=lower_lv.confluence_score if lower_lv else 0,
        range_lower_test_count=lower_lv.test_count if lower_lv else 0,
        micro_upper=_round_opt(micro_upper),
        micro_upper_source=_build_source_str(micro_up_lv),
        micro_upper_tier=micro_up_lv.strength_tier if micro_up_lv else "",
        micro_lower=_round_opt(micro_lower),
        micro_lower_source=_build_source_str(micro_dn_lv),
        micro_lower_tier=micro_dn_lv.strength_tier if micro_dn_lv else "",
        micro_width_pct=round(micro_width, 2),
        price_position=price_pos,
        price_position_pct=round(pos_pct, 1),
        box_state=box_state,
        box_state_ts=box_state_ts,
        box_age_hours=round(box_age_hours, 1),
        box_width_pct=round(box_width_pct, 2),
        box_quality=box_quality,
        breakout_probability=round(breakout_prob, 2),
        breakout_direction_bias=breakout_bias,
        breakout_reason=breakout_reason,
        ma60_daily=_round_opt(ma60_daily),
        ma120_daily=_round_opt(ma120_daily),
        ma60_weekly=_round_opt(ma60_weekly),
        macd_daily_above_zero=macd_above_zero,
        macd_daily_histogram=round(macd_histogram, 2) if macd_histogram is not None else None,
        macd_daily_hist_rising=macd_hist_rising,
        unfilled_wick_low=_round_opt(unfilled_low),
        unfilled_wick_high=_round_opt(unfilled_high),
        signal_grade=grade,
        signal_direction=direction,
        signal_reason=reason,
        signal_entry=_round_opt(entry),
        signal_stop_loss=_round_opt(sl),
        signal_tp1=_round_opt(tp1),
        signal_rr_ratio=round(rr, 1) if rr else None,
        sweep_confirmed=sweep_confirmed,
        cps_aligned=cps_aligned,
        bb_squeeze=bb_squeeze,
        oi_buildup=oi_buildup,
        volume_declining=volume_declining,
        funding_extreme=funding_extreme,
        orderbook_imbalance=ob_imbalance,
        confluence_count=confluence_count,
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 内部函数
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


_BoundaryPair = tuple[Optional[KeyLevelV2], Optional[KeyLevelV2]]


def _extract_dual_boundaries(
    snapshot: Optional[KeyLevelSnapshotV2],
    price: float,
) -> tuple[_BoundaryPair, _BoundaryPair]:
    """提取双层箱体边界：核心箱体(结构性) + 微观区间(最近)。

    核心箱体：优先选结构性来源（swing/VP/MA/Fib），宽度 ≥ 2.5%。
    微观区间：最近的 S/A/B 级 level（不限来源），作为短期参考。

    返回 ((core_upper, core_lower), (micro_upper, micro_lower))
    """
    empty: _BoundaryPair = (None, None)
    if not snapshot or not snapshot.levels:
        return empty, empty

    _MAX_DIST = 15.0
    tier_rank = {"S": 0, "A": 1, "B": 2, "C": 3}

    def _dist_bucket(dist_pct: float) -> int:
        if dist_pct <= 3.0:
            return 0
        if dist_pct <= 6.0:
            return 1
        return 2

    sab = [
        lv for lv in snapshot.levels
        if lv.strength_tier in ("S", "A", "B")
        and abs(lv.price - price) / max(price, 1) * 100 <= _MAX_DIST
    ]

    above_all = [lv for lv in sab if lv.price > price]
    below_all = [lv for lv in sab if lv.price < price]

    def _sort_key(lv: KeyLevelV2, sign: float = 1.0):
        dist = abs(lv.price - price) / max(price, 1) * 100
        return (_dist_bucket(dist), tier_rank.get(lv.strength_tier, 9), sign * (lv.price - price))

    above_all.sort(key=lambda l: _sort_key(l, 1.0))
    below_all.sort(key=lambda l: _sort_key(l, -1.0))

    micro_upper = above_all[0] if above_all else None
    micro_lower = below_all[0] if below_all else None

    above_struct = [lv for lv in above_all if _is_structural_source(lv.sources)]
    below_struct = [lv for lv in below_all if _is_structural_source(lv.sources)]

    core_upper, core_lower = _pick_core_pair(
        above_struct or above_all, below_struct or below_all, price,
    )

    return (core_upper, core_lower), (micro_upper, micro_lower)


def _pick_core_pair(
    above: list[KeyLevelV2],
    below: list[KeyLevelV2],
    price: float,
) -> _BoundaryPair:
    """从候选中选出宽度 ≥ _MIN_BOX_WIDTH_PCT 的最优核心箱体对。"""
    for u in above:
        for l in below:
            width = (u.price - l.price) / max(price, 1) * 100
            if _MIN_BOX_WIDTH_PCT <= width <= _MAX_BOX_WIDTH_PCT:
                return u, l
    return (above[0] if above else None, below[0] if below else None)


def _build_source_str(lv: KeyLevelV2 | None) -> str:
    if not lv or not lv.sources:
        return ""
    return " + ".join(lv.sources[:3])


def _calc_price_position(
    price: float,
    upper: float | None,
    lower: float | None,
    near_pct: float,
) -> tuple[str, float]:
    """计算价格在箱体中的相对位置。"""
    if upper is None or lower is None or upper <= lower:
        if upper and price > upper:
            return "above", 100.0
        if lower and price < lower:
            return "below", 0.0
        return "middle", 50.0

    span = upper - lower
    pct = (price - lower) / span * 100
    pct = max(0.0, min(100.0, pct))

    if price > upper:
        return "above", 100.0
    if price < lower:
        return "below", 0.0

    dist_to_upper = abs(price - upper) / price * 100
    if dist_to_upper <= near_pct:
        return "near_upper", pct

    dist_to_lower = abs(price - lower) / price * 100
    if dist_to_lower <= near_pct:
        return "near_lower", pct

    return "middle", pct


def _transition_box_state(
    has_box: bool,
    price_pos: str,
    bb_squeeze: bool,
    prev: RangeSignalData | None,
    now_ts: int,
    upper: float | None,
    lower: float | None,
    price: float,
) -> tuple[str, int, float]:
    """箱体状态机转换。

    none → forming → confirmed → mature → squeeze → breaking_up/down → broken
    """
    prev_state = prev.box_state if prev else _ST_NONE
    prev_ts = prev.box_state_ts if prev else now_ts
    prev_upper = prev.range_upper if prev else None
    prev_lower = prev.range_lower if prev else None

    age_hours = (now_ts - prev_ts) / 3600 if prev_ts else 0

    def _same_box() -> bool:
        if not upper or not lower or not prev_upper or not prev_lower:
            return False
        u_drift = abs(upper - prev_upper) / max(price, 1) * 100
        l_drift = abs(lower - prev_lower) / max(price, 1) * 100
        return u_drift < 2.0 and l_drift < 2.0

    if not has_box:
        if prev_state in (_ST_CONFIRMED, _ST_MATURE, _ST_SQUEEZE):
            if price_pos == "above":
                return _ST_BREAKING_UP, prev_ts, age_hours
            if price_pos == "below":
                return _ST_BREAKING_DOWN, prev_ts, age_hours
        return _ST_NONE, now_ts, 0

    same = _same_box()

    if prev_state == _ST_NONE:
        return _ST_FORMING, now_ts, 0

    if prev_state == _ST_FORMING:
        if same and age_hours >= 4:
            return _ST_CONFIRMED, prev_ts, age_hours
        if not same:
            return _ST_FORMING, now_ts, 0
        return _ST_FORMING, prev_ts, age_hours

    if prev_state == _ST_CONFIRMED:
        if not same:
            return _ST_FORMING, now_ts, 0
        if age_hours >= _MATURE_HOURS:
            return _ST_MATURE, prev_ts, age_hours
        return _ST_CONFIRMED, prev_ts, age_hours

    if prev_state == _ST_MATURE:
        if not same:
            return _ST_FORMING, now_ts, 0
        if bb_squeeze:
            return _ST_SQUEEZE, prev_ts, age_hours
        return _ST_MATURE, prev_ts, age_hours

    if prev_state == _ST_SQUEEZE:
        if price_pos == "above":
            return _ST_BREAKING_UP, prev_ts, age_hours
        if price_pos == "below":
            return _ST_BREAKING_DOWN, prev_ts, age_hours
        if not bb_squeeze:
            return _ST_MATURE, prev_ts, age_hours
        return _ST_SQUEEZE, prev_ts, age_hours

    if prev_state in (_ST_BREAKING_UP, _ST_BREAKING_DOWN):
        if has_box and price_pos in ("near_upper", "near_lower", "middle"):
            return _ST_CONFIRMED, prev_ts, age_hours
        return _ST_BROKEN, now_ts, 0

    return _ST_NONE, now_ts, 0


def _calc_box_quality(
    has_box: bool,
    upper_lv: KeyLevelV2 | None,
    lower_lv: KeyLevelV2 | None,
    age_hours: float,
    volume_declining: bool,
    bb_squeeze: bool,
    width_pct: float,
) -> int:
    if not has_box:
        return 0

    score = 0.0
    if upper_lv:
        score += min(upper_lv.confluence_score, 40)
    if lower_lv:
        score += min(lower_lv.confluence_score, 40)

    if age_hours >= _MATURE_HOURS:
        score += 10
    elif age_hours >= 24:
        score += 5

    if volume_declining:
        score += 5
    if bb_squeeze:
        score += 5

    if 3.0 <= width_pct <= 8.0:
        score += 5
    elif width_pct > 12.0:
        score -= 5

    return max(0, min(100, int(score)))


def _calc_breakout_probability(
    box_state: str,
    bb_squeeze: bool,
    oi_buildup: bool,
    funding_extreme: bool,
    ob_imbalance: str,
    volume_declining: bool,
    age_hours: float,
    macd_above_zero: bool | None,
    macd_hist_rising: bool | None,
) -> tuple[float, str, str]:
    """评估箱体突破概率和方向偏向。"""
    if box_state in (_ST_NONE, _ST_BROKEN):
        return 0, "", ""

    prob = 0.0
    reasons = []
    up_bias = 0
    down_bias = 0

    if bb_squeeze:
        prob += 0.25
        reasons.append("BB Squeeze 蓄力中")

    if oi_buildup:
        prob += 0.15
        reasons.append("OI 持续堆积")

    if funding_extreme:
        prob += 0.10
        reasons.append("资金费率极端")

    if volume_declining and age_hours >= 48:
        prob += 0.10
        reasons.append("成交量持续萎缩")

    if age_hours >= _MATURE_HOURS:
        prob += 0.10
        reasons.append(f"箱体已维持{age_hours:.0f}h")

    if box_state == _ST_SQUEEZE:
        prob += 0.15

    if macd_above_zero is True:
        up_bias += 1
    elif macd_above_zero is False:
        down_bias += 1

    if macd_hist_rising is True:
        up_bias += 1
    elif macd_hist_rising is False:
        down_bias += 1

    if ob_imbalance == "bid_heavy":
        up_bias += 1
    elif ob_imbalance == "ask_heavy":
        down_bias += 1

    if up_bias > down_bias:
        bias = "up"
    elif down_bias > up_bias:
        bias = "down"
    else:
        bias = "neutral"

    prob = min(prob, 0.95)
    reason_str = "；".join(reasons) if reasons else ""
    return prob, bias, reason_str


def _grade_signal_v2(
    price_pos: str,
    has_box: bool,
    box_state: str,
    macd_above_zero: bool | None,
    macd_hist_rising: bool | None,
    sweep_confirmed: bool,
    bb_squeeze: bool,
    upper_lv: KeyLevelV2 | None,
    lower_lv: KeyLevelV2 | None,
    price: float,
    atr: float,
    breakout_prob: float,
    breakout_bias: str,
) -> tuple[str | None, str | None, str, float | None, float | None, float | None, float | None]:
    """信号分级（返回 grade, direction, reason, entry, sl, tp1, rr）。

    S 级：边界 S/A 级 + MACD 确认 + sweep 确认 + 箱体 mature/squeeze
    A 级：边界 S/A 级 + MACD 确认（或 sweep 确认）
    B 级：接近边界 + 部分确认
    """
    if not has_box or price_pos in ("middle", "above", "below"):
        if box_state in (_ST_BREAKING_UP, _ST_BREAKING_DOWN):
            direction = "long" if box_state == _ST_BREAKING_UP else "short"
            reason = f"箱体正在向{'上' if direction == 'long' else '下'}突破"
            if breakout_prob >= 0.5:
                reason += f"(突破概率{breakout_prob:.0%})"
            return "B", direction, reason, None, None, None, None
        return None, None, "", None, None, None, None

    if price_pos == "near_upper" and upper_lv:
        direction = "short"
        tier = upper_lv.strength_tier
        entry = price
        sl = upper_lv.price + atr * 1.0 if atr > 0 else None
        tp1 = lower_lv.price if lower_lv else (price - atr * 3) if atr > 0 else None
        rr = abs(entry - tp1) / abs(sl - entry) if sl and tp1 and sl != entry else None

        macd_bearish = macd_above_zero is False
        is_mature = box_state in (_ST_MATURE, _ST_SQUEEZE)

        if tier in ("S", "A") and macd_bearish and sweep_confirmed and is_mature:
            grade = "S"
            reason = (
                f"价格到达{tier}级箱体上沿${upper_lv.price:,.0f}"
                f"({upper_lv.source_count}维共振) + MACD空头 + 流动性扫取确认"
                f" + 箱体成熟{box_state}"
            )
        elif tier in ("S", "A") and (macd_bearish or sweep_confirmed):
            grade = "A"
            confirms = []
            if macd_bearish:
                confirms.append("MACD在0轴下方")
            if sweep_confirmed:
                confirms.append("流动性扫取确认")
            reason = (
                f"价格到达{tier}级箱体上沿${upper_lv.price:,.0f}"
                f"({upper_lv.source_count}维共振)，{'、'.join(confirms)}"
            )
        else:
            grade = "B"
            reason = f"价格接近箱体上沿${upper_lv.price:,.0f}({tier}级)"
            if macd_above_zero is True:
                reason += "，但MACD在0轴上方，逆势做空需谨慎"

        return grade, direction, reason, _round_opt(entry), _round_opt(sl), _round_opt(tp1), rr

    if price_pos == "near_lower" and lower_lv:
        direction = "long"
        tier = lower_lv.strength_tier
        entry = price
        sl = lower_lv.price - atr * 1.0 if atr > 0 else None
        tp1 = upper_lv.price if upper_lv else (price + atr * 3) if atr > 0 else None
        rr = abs(tp1 - entry) / abs(entry - sl) if sl and tp1 and sl != entry else None

        macd_bullish = macd_above_zero is True
        is_mature = box_state in (_ST_MATURE, _ST_SQUEEZE)

        if tier in ("S", "A") and sweep_confirmed and macd_bullish and is_mature:
            grade = "S"
            reason = (
                f"价格到达{tier}级箱体下沿${lower_lv.price:,.0f}"
                f"({lower_lv.source_count}维共振) + 流动性扫取确认 + MACD多头"
                f" + 箱体成熟{box_state}"
            )
        elif tier in ("S", "A") and (sweep_confirmed or macd_bullish):
            grade = "A"
            confirms = []
            if sweep_confirmed:
                confirms.append("流动性扫取确认")
            if macd_bullish:
                confirms.append("MACD在0轴上方")
            reason = (
                f"价格到达{tier}级箱体下沿${lower_lv.price:,.0f}"
                f"({lower_lv.source_count}维共振)，{'、'.join(confirms)}"
            )
        else:
            grade = "B"
            reason = f"价格接近箱体下沿${lower_lv.price:,.0f}({tier}级)"
            if not sweep_confirmed:
                reason += "，等待流动性扫取确认可升级"

        return grade, direction, reason, _round_opt(entry), _round_opt(sl), _round_opt(tp1), rr

    return None, None, "", None, None, None, None


def _check_volume_declining(candles: list[CandleData]) -> bool:
    """检查最近 5 根日K成交量是否递减趋势。"""
    if len(candles) < 10:
        return False
    recent = candles[-5:]
    vols = [c.vol for c in recent if c.vol and c.vol > 0]
    if len(vols) < 4:
        return False
    decline_count = sum(1 for i in range(1, len(vols)) if vols[i] < vols[i - 1])
    return decline_count >= 3


def _detect_unfilled_wicks(
    candles: list[CandleData],
    current_price: float,
    atr: float,
    body_ratio_threshold: float,
    min_atr_mult: float,
) -> tuple[float | None, float | None]:
    """检测最近未被回补的大影线（保留自 V1）。"""
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

        if (unfilled_low is None
                and lower_wick >= min_atr_len
                and body_pct <= body_ratio_threshold):
            wick_low = c.low
            filled = any(candles[j].low <= wick_low * 1.002 for j in range(i + 1, len(candles)))
            if not filled:
                unfilled_low = wick_low

        if (unfilled_high is None
                and upper_wick >= min_atr_len
                and body_pct <= body_ratio_threshold):
            wick_high = c.high
            filled = any(candles[j].high >= wick_high * 0.998 for j in range(i + 1, len(candles)))
            if not filled:
                unfilled_high = wick_high

        if unfilled_low is not None and unfilled_high is not None:
            break

    return unfilled_low, unfilled_high


def _round_opt(v: float | None, ndigits: int = 2) -> float | None:
    return round(v, ndigits) if v is not None else None
