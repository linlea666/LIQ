"""K 线反转形态检测 — pin bar / engulfing / doji

用于关键位 tracker 的入场确认加分：
  bounced/flipped 状态下检测最近 4H K线是否出现反转形态，
  有形态 → 信号升级一档，无形态 → 逻辑不变。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from models.market import CandleData


@dataclass
class PatternResult:
    found: bool
    name: str = ""
    strength: float = 0.0  # 0~1


def detect_reversal_pattern(
    candles: list[CandleData] | None,
    side: str,
) -> PatternResult:
    """检测最近 K 线是否出现与 side 方向匹配的反转形态。

    side="support" → 看涨反转（锤子线、看涨吞没、十字星）
    side="resistance" → 看跌反转（射击之星、看跌吞没、十字星）

    返回最强的单个形态结果。
    """
    if not candles or len(candles) < 2:
        return PatternResult(found=False)

    latest = candles[-1]
    prev = candles[-2]

    results: list[PatternResult] = []

    if _is_pin_bar(latest, side):
        name = "锤子线" if side == "support" else "射击之星"
        results.append(PatternResult(found=True, name=name, strength=0.85))

    if _is_engulfing(prev, latest, side):
        name = "看涨吞没" if side == "support" else "看跌吞没"
        results.append(PatternResult(found=True, name=name, strength=0.80))

    if _is_doji(latest):
        results.append(PatternResult(found=True, name="十字星", strength=0.50))

    if not results:
        return PatternResult(found=False)

    return max(results, key=lambda r: r.strength)


def _is_pin_bar(c: CandleData, side: str, min_wick_ratio: float = 2.0) -> bool:
    """Pin Bar / 锤子线 / 射击之星检测。

    支撑位：长下影线 >= 2 倍实体，实体占总长 <= 33%
    阻力位：长上影线 >= 2 倍实体，实体占总长 <= 33%
    """
    body = abs(c.close - c.open)
    total = c.high - c.low
    if total <= 0:
        return False

    lower_wick = min(c.open, c.close) - c.low
    upper_wick = c.high - max(c.open, c.close)

    if body / total > 0.33:
        return False

    if side == "support":
        return lower_wick >= body * min_wick_ratio and lower_wick > upper_wick
    else:
        return upper_wick >= body * min_wick_ratio and upper_wick > lower_wick


def _is_engulfing(prev: CandleData, curr: CandleData, side: str) -> bool:
    """吞没形态检测。

    看涨吞没(support)：前阴后阳，当前实体完全包裹前一根实体
    看跌吞没(resistance)：前阳后阴，当前实体完全包裹前一根实体
    """
    prev_body = abs(prev.close - prev.open)
    curr_body = abs(curr.close - curr.open)

    if prev_body <= 0 or curr_body <= prev_body:
        return False

    curr_body_lo = min(curr.open, curr.close)
    curr_body_hi = max(curr.open, curr.close)
    prev_body_lo = min(prev.open, prev.close)
    prev_body_hi = max(prev.open, prev.close)

    wraps = curr_body_lo <= prev_body_lo and curr_body_hi >= prev_body_hi

    if not wraps:
        return False

    if side == "support":
        return prev.close < prev.open and curr.close > curr.open
    else:
        return prev.close > prev.open and curr.close < curr.open


def _is_doji(c: CandleData, max_body_ratio: float = 0.10) -> bool:
    """十字星检测：实体占总长 <= 10%。"""
    total = c.high - c.low
    if total <= 0:
        return False
    body = abs(c.close - c.open)
    return body / total <= max_body_ratio
