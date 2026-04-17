"""市场结构识别器（Market Structure Detector）

核心算法（Wyckoff / SMC 学派标准做法）：
  1. Fractal 识别 swing high / swing low（复用 ta_core.detect_swings）
  2. 毛刺过滤：相邻同类型 swing 距离 < max(0.5%, 0.8*ATR) 只留更极端的
  3. 方向判定：最近 2 个 high + 2 个 low
       HH + HL → bullish
       LH + LL → bearish
       其他    → ranging / transitioning
  4. 事件检测：
       bullish: price > 最新 swing_high → BOS_up（延续）
       bullish: price < 最新 swing_low  → CHoCH_down（反转）
       bearish: 反之
  5. 置信度：摆动数量 + 方向一致性 + 事件新鲜度 + 区间宽度
  6. 操作偏置：根据方向与置信度给出 long_only / short_only / both_ok / stand_aside

不改变现有任何调用点，独立可用。
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from models.market import CandleData
from models.market_structure import MarketStructure, SwingPoint as PydSwingPoint
from processors.ta_core import SwingPoint as TaSwingPoint
from processors.ta_core import detect_swings

logger = logging.getLogger(__name__)

# 默认参数
DEFAULT_FRACTAL_K = 3   # 1h K 线左右各 3 根（博主 1h 视角对齐）
DEFAULT_MIN_CANDLES = 50
DEFAULT_MIN_GAP_PCT = 0.5   # 摆动点最小间距 0.5%（过滤毛刺）
DEFAULT_ATR_GAP_FACTOR = 0.8  # 或 0.8 × ATR 取较大

# BOS 权威提权窗口（见 _apply_bos_authority_override）
BOS_OVERRIDE_MAX_AGE_HOURS = 6


def detect_market_structure(
    candles: list[CandleData],
    *,
    atr: float | None = None,
    timeframe: str = "1h",
    fractal_k: int = DEFAULT_FRACTAL_K,
    min_candles: int = DEFAULT_MIN_CANDLES,
    min_gap_pct: float = DEFAULT_MIN_GAP_PCT,
    atr_gap_factor: float = DEFAULT_ATR_GAP_FACTOR,
    now_ts: int | None = None,
) -> Optional[MarketStructure]:
    """基于 swing 识别价格结构方向 + BOS/CHoCH 事件。

    Args:
        candles: 已排序（旧→新）的 K 线。
        atr: 当前 ATR，用于毛刺过滤阈值。None 则只用百分比阈值。
        timeframe: 展示用标签（"1h" / "4h"）。
        fractal_k: 摆动点识别窗口（左右各 k 根）。
        min_candles: 少于此数返回 None。

    Returns:
        MarketStructure 或 None（数据不足）。
    """
    if not candles or len(candles) < min_candles:
        return None

    highs = [c.high for c in candles]
    lows = [c.low for c in candles]
    closes = [c.close for c in candles]
    ts_list = [c.ts for c in candles]
    price = closes[-1]
    if price <= 0:
        return None
    now_ts = now_ts or int(time.time())

    raw_swings = detect_swings(highs, lows, ts_list, lookback=fractal_k)
    if not raw_swings:
        return MarketStructure(
            timeframe=timeframe,
            direction="ranging",
            summary=f"{timeframe} 摆动点不足，结构待观察",
        )

    min_gap = max(
        price * min_gap_pct / 100.0,
        (atr or 0) * atr_gap_factor,
    )
    filtered = _filter_close_swings(raw_swings, min_gap)

    # 按 index 倒序（最新在前）——后续 "[0]" 就是最近的摆动点
    highs_pts = sorted(
        [p for p in filtered if p.kind == "high"],
        key=lambda p: p.index,
        reverse=True,
    )
    lows_pts = sorted(
        [p for p in filtered if p.kind == "low"],
        key=lambda p: p.index,
        reverse=True,
    )

    raw_direction = _classify_direction(highs_pts, lows_pts)

    event, event_ts, event_price = _detect_event(
        price, highs_pts, lows_pts, raw_direction, ts_list[-1],
    )

    # BOS 权威提权：化解 sweep 针尖污染导致的伪 transitioning
    direction = _apply_bos_authority_override(
        raw_direction, event, event_ts, highs_pts, lows_pts, now_ts,
    )

    struct_high = highs_pts[0].price if highs_pts else 0.0
    struct_low = lows_pts[0].price if lows_pts else 0.0

    confidence = _calc_confidence(
        highs_pts, lows_pts, direction, event, event_ts, now_ts,
        struct_high, struct_low, price,
    )

    operate_bias = _calc_bias(direction, confidence)

    py_highs = [_to_pyd(p, fractal_k) for p in highs_pts[:5]]
    py_lows = [_to_pyd(p, fractal_k) for p in lows_pts[:5]]

    summary = _gen_summary(
        direction, event, event_ts, now_ts, struct_high, struct_low, timeframe,
    )

    return MarketStructure(
        timeframe=timeframe,
        direction=direction,
        swing_highs=py_highs,
        swing_lows=py_lows,
        last_event=event,
        event_ts=event_ts,
        event_price=round(event_price, 2) if event_price else 0.0,
        structure_high=round(struct_high, 2),
        structure_low=round(struct_low, 2),
        operate_bias=operate_bias,
        confidence=round(confidence, 2),
        summary=summary,
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 内部工具函数
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _filter_close_swings(
    swings: list[TaSwingPoint], min_gap: float,
) -> list[TaSwingPoint]:
    """相邻同类型 swing 间距 < min_gap 时只保留更极端的那个（抗毛刺）。"""
    if min_gap <= 0 or not swings:
        return list(swings)
    sorted_sw = sorted(swings, key=lambda p: p.index)
    result: list[TaSwingPoint] = []
    for p in sorted_sw:
        merged = False
        for i, r in enumerate(result):
            if r.kind != p.kind:
                continue
            if abs(r.price - p.price) < min_gap:
                if (p.kind == "high" and p.price > r.price) or (
                    p.kind == "low" and p.price < r.price
                ):
                    result[i] = p
                merged = True
                break
        if not merged:
            result.append(p)
    return result


def _classify_direction(
    highs: list[TaSwingPoint],  # sorted desc by index (newest first)
    lows: list[TaSwingPoint],
) -> str:
    """HH+HL → bullish, LH+LL → bearish, 其他 → ranging / transitioning"""
    if len(highs) < 2 or len(lows) < 2:
        return "ranging"

    hh = highs[0].price > highs[1].price
    hl = lows[0].price > lows[1].price
    lh = highs[0].price < highs[1].price
    ll = lows[0].price < lows[1].price

    if hh and hl:
        return "bullish"
    if lh and ll:
        return "bearish"
    if (hh and ll) or (lh and hl):
        return "transitioning"
    return "ranging"


def _detect_event(
    price: float,
    highs: list[TaSwingPoint],
    lows: list[TaSwingPoint],
    direction: str,
    latest_ts: int,
) -> tuple[str, int, float]:
    """检测 BOS / CHoCH。"""
    if not highs or not lows:
        return "", 0, 0.0

    last_high = highs[0].price
    last_low = lows[0].price

    if direction == "bullish":
        if price > last_high:
            return "BOS_up", latest_ts, price
        if price < last_low:
            return "CHoCH_down", latest_ts, price
    elif direction == "bearish":
        if price < last_low:
            return "BOS_down", latest_ts, price
        if price > last_high:
            return "CHoCH_up", latest_ts, price
    else:
        # ranging / transitioning — 哪边破哪边给事件
        if price > last_high:
            return "BOS_up", latest_ts, price
        if price < last_low:
            return "BOS_down", latest_ts, price
    return "", 0, 0.0


def _calc_confidence(
    highs: list[TaSwingPoint],
    lows: list[TaSwingPoint],
    direction: str,
    event: str,
    event_ts: int,
    now_ts: int,
    struct_high: float,
    struct_low: float,
    price: float,
) -> float:
    """置信度 0~1"""
    conf = 0.0

    if len(highs) + len(lows) >= 4:
        conf += 0.3

    if direction == "bullish" and _consecutive_trend(highs, lows, "up") >= 2:
        conf += 0.3
    elif direction == "bearish" and _consecutive_trend(highs, lows, "down") >= 2:
        conf += 0.3
    elif direction == "ranging":
        conf += 0.2  # ranging 本身也算一种"明确状态"

    if event and event_ts and now_ts - event_ts <= 24 * 3600:
        conf += 0.2

    if struct_high > 0 and struct_low > 0 and price > 0:
        pct = abs(struct_high - struct_low) / price * 100
        if pct >= 1.0:
            conf += 0.2

    return min(1.0, conf)


def _count_consecutive_highs_up(highs: list[TaSwingPoint]) -> int:
    """最近连续 HH（higher-high）次数，最多看 4 个。"""
    count = 0
    for i in range(min(len(highs), 4) - 1):
        if highs[i].price > highs[i + 1].price:
            count += 1
        else:
            break
    return count


def _count_consecutive_lows_down(lows: list[TaSwingPoint]) -> int:
    """最近连续 LL（lower-low）次数，最多看 4 个。"""
    count = 0
    for i in range(min(len(lows), 4) - 1):
        if lows[i].price < lows[i + 1].price:
            count += 1
        else:
            break
    return count


def _apply_bos_authority_override(
    direction: str,
    event: str,
    event_ts: int,
    highs: list[TaSwingPoint],
    lows: list[TaSwingPoint],
    now_ts: int,
    max_age_h: int = BOS_OVERRIDE_MAX_AGE_HOURS,
) -> str:
    """BOS 权威提权（SMC continuation 原则）。

    根因：swing low/high 可能被一根"扫流动性"（sweep）的尖针污染，
    导致 _classify_direction 出现 HH+LL 或 LH+HL 的"伪 transitioning"。
    当近期（<6h）出现新鲜 BOS_up/down 且主导方向的连续性成立时，
    BOS 本身是比"最新 2 个 swing 比较"更强的结构延续信号，按 SMC
    标准应当覆盖混合判定。

    规则（仅在 direction == "transitioning" 时生效，保守）：
      - BOS_up + 事件新鲜 + 连续 ≥2 次 HH → bullish
      - BOS_down + 事件新鲜 + 连续 ≥2 次 LL → bearish
      - 其他 → 保持 transitioning（观望）
    """
    if direction != "transitioning":
        return direction
    if not event or not event_ts:
        return direction
    if now_ts - event_ts > max_age_h * 3600:
        return direction

    if event == "BOS_up" and _count_consecutive_highs_up(highs) >= 2:
        return "bullish"
    if event == "BOS_down" and _count_consecutive_lows_down(lows) >= 2:
        return "bearish"
    return direction


def _consecutive_trend(
    highs: list[TaSwingPoint], lows: list[TaSwingPoint], kind: str,
) -> int:
    """最近连续 HH(up) / LL(down) 组合数，最多看 4 个 swing。"""
    count = 0
    cmp_fn = (lambda a, b: a > b) if kind == "up" else (lambda a, b: a < b)
    for i in range(min(len(highs), 4) - 1):
        if cmp_fn(highs[i].price, highs[i + 1].price):
            count += 1
        else:
            break
    for i in range(min(len(lows), 4) - 1):
        if cmp_fn(lows[i].price, lows[i + 1].price):
            count += 1
        else:
            break
    return count


def _calc_bias(direction: str, confidence: float) -> str:
    if confidence < 0.3:
        return "stand_aside"
    if direction == "bullish":
        return "long_only"
    if direction == "bearish":
        return "short_only"
    if direction == "ranging":
        return "both_ok"
    return "stand_aside"


def _to_pyd(p: TaSwingPoint, strength: int) -> PydSwingPoint:
    return PydSwingPoint(
        ts=p.ts,
        price=round(p.price, 2),
        kind=p.kind,
        strength=strength,
    )


def _gen_summary(
    direction: str,
    event: str,
    event_ts: int,
    now_ts: int,
    struct_high: float,
    struct_low: float,
    tf: str,
) -> str:
    direction_zh = {
        "bullish": "上升结构",
        "bearish": "下降结构",
        "ranging": "横向震荡",
        "transitioning": "结构转换中",
    }.get(direction, "结构不明")
    parts = [f"{tf} {direction_zh}"]

    event_zh = {
        "BOS_up": "BOS 向上",
        "BOS_down": "BOS 向下",
        "CHoCH_up": "CHoCH 向上",
        "CHoCH_down": "CHoCH 向下",
    }.get(event, "")
    if event_zh and event_ts:
        hours = max(0, (now_ts - event_ts) // 3600)
        if hours < 1:
            time_zh = "刚刚"
        elif hours < 24:
            time_zh = f"{int(hours)}h 前"
        else:
            time_zh = f"{int(hours // 24)}d 前"
        parts.append(f"{event_zh} · {time_zh}")

    if struct_high > 0 and struct_low > 0:
        parts.append(f"结构区间 ${struct_low:,.0f}~${struct_high:,.0f}")

    return "，".join(parts)
