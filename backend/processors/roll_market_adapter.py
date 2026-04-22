"""滚仓市场上下文适配器 —— CoinState → MarketContext

职责：
  - 从主引擎的 CoinState（大而全）抽取滚仓引擎真正关心的字段
  - 做必要的 enum 映射（避免类型重叠时的耦合）
  - 对缺失字段提供安全默认，不让 evaluate 挂掉

设计约束：
  - 纯函数 + 鸭子类型（不 import CoinState 类，只用 getattr）
  - 不做二次计算（数值计算已在上游 processor 完成）
  - 对无法计算的 regime/结构/衰竭等字段，返回合理默认 + data_quality=partial

调用链：
    engine._roll_eval_loop
      → build_market_context_from_state(state)
      → service.evaluate_all(provider=lambda coin: build_...)
"""

from __future__ import annotations

import time
from typing import Any, Optional

from processors.roll_position_engine import (
    KeyLevelRef,
    MarketContext,
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 枚举映射表（上游原始值 → RollEngine 期望值）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# TrendExhaustionSignal.overall_state → MarketContext.te_overall_state
_TE_STATE_MAP = {
    "healthy_continuation": "healthy_continuation",
    "momentum_fading":      "exhaustion_warn",     # 动能衰减视作早期警戒
    "exhaustion_warn":      "exhaustion_warn",
    "structural_reversal":  "exhaustion_confirmed",
    "neutral":              "neutral",
}

# 由 te_overall_state 推导 overall_score（-1 ~ 1）
# 无数值原始 score 时的启发式兜底；正=延续强，负=衰竭强
_TE_STATE_SCORE = {
    "healthy_continuation":  0.6,
    "momentum_continuation": 0.6,     # 保留兼容
    "neutral":               0.0,
    "exhaustion_warn":      -0.5,
    "exhaustion_confirmed": -0.8,
}

# KeyLevelV2.state → MarketContext.KeyLevelState
_KL_STATE_MAP = {
    "idle":        "approaching",
    "approaching": "approaching",
    "testing":     "tested",
    "swept":       "tested",
    "bounced":     "bounced",
    "broken":      "broken",
    "fake_break":  "fake_break",
    "flipped":     "broken",
}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 子提取器（每个字段独立一段，便于单测）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _extract_regime(state: Any) -> tuple[Optional[str], float]:
    snap = getattr(state, "regime_snapshot", None)
    if snap is None:
        return None, 0.0
    return getattr(snap, "regime", None), float(getattr(snap, "confidence", 0.0) or 0.0)


def _extract_market_structure(ms: Any) -> tuple[Optional[str], str, Optional[str]]:
    """返回 (direction, last_event, last_event_side)。

    last_event_side: long / short / None。由 BOS_up/CHoCH_up 推导。
    """
    if ms is None:
        return None, "none", None
    direction = getattr(ms, "direction", None)
    raw_event = getattr(ms, "last_event", "") or ""

    if raw_event.startswith("BOS"):
        event_kind = "BOS"
    elif raw_event.startswith("CHoCH"):
        event_kind = "CHoCH"
    else:
        event_kind = "none"

    if raw_event.endswith("_up"):
        side = "long"
    elif raw_event.endswith("_down"):
        side = "short"
    else:
        side = None

    return direction, event_kind, side


def _extract_trend_exhaustion(te: Any) -> tuple[Optional[str], float]:
    if te is None:
        return None, 0.0
    raw = getattr(te, "overall_state", None)
    mapped = _TE_STATE_MAP.get(raw, "neutral") if raw else None
    score = _TE_STATE_SCORE.get(mapped or "neutral", 0.0)
    return mapped, score


def _extract_nearest_key_level(
    key_level_snapshot: Any,
    current_price: float,
) -> Optional[KeyLevelRef]:
    if key_level_snapshot is None or current_price <= 0:
        return None
    levels = getattr(key_level_snapshot, "levels", None) or []
    if not levels:
        return None

    # 距现价最近的一个关键位
    nearest = min(
        levels,
        key=lambda lv: abs(getattr(lv, "price", 0.0) - current_price),
    )
    price = float(getattr(nearest, "price", 0.0) or 0.0)
    if price <= 0:
        return None

    raw_side = getattr(nearest, "side", "support") or "support"
    kind = "support" if raw_side == "support" else "resistance"
    raw_state = getattr(nearest, "state", "idle") or "idle"
    state = _KL_STATE_MAP.get(raw_state, "approaching")

    return KeyLevelRef(
        price=price,
        kind=kind,
        state=state,
        confluence_score=float(getattr(nearest, "confluence_score", 50.0) or 50.0),
        distance_pct=abs(price - current_price) / current_price * 100.0,
    )


def _extract_reversal_pattern(key_level_snapshot: Any) -> str:
    """取最近关键位命中的 K 线形态（若有）。

    KeyLevelV2.pattern_detected 是中文名称，这里做翻译：
        锤子线 → pin_bar_support
        射击之星 → pin_bar_resistance
        看涨吞没 → engulfing_bullish
        看跌吞没 → engulfing_bearish
        十字星   → doji_reversal
    """
    if key_level_snapshot is None:
        return "none"
    levels = getattr(key_level_snapshot, "levels", None) or []
    for lv in levels:
        pat = (getattr(lv, "pattern_detected", "") or "").strip()
        side = getattr(lv, "side", "")
        if not pat:
            continue
        if pat in ("锤子线", "Hammer"):
            return "pin_bar_support"
        if pat in ("射击之星", "Shooting Star"):
            return "pin_bar_resistance"
        if pat in ("看涨吞没", "Bullish Engulfing"):
            return "engulfing_bullish"
        if pat in ("看跌吞没", "Bearish Engulfing"):
            return "engulfing_bearish"
        if pat in ("十字星", "Doji"):
            return "doji_reversal"
        _ = side   # 保留变量以示可扩展
    return "none"


def _extract_cvd_divergence(cvd_contract: Any, cvd_spot: Any) -> str:
    """优先合约 CVD，其次现货。返回 bull_div / bear_div / none。"""
    for cvd in (cvd_contract, cvd_spot):
        if cvd is None:
            continue
        if not getattr(cvd, "has_divergence", False):
            continue
        # 约定：CVDData 无 divergence_type 字段时，按 delta_1h 方向与价格方向推导
        # 此处为保守兜底：没有明确方向就视为 bear_div（更保守的"减仓倾向"）
        dtype = getattr(cvd, "divergence_type", "") or ""
        if dtype == "bullish":
            return "bull_div"
        if dtype == "bearish":
            return "bear_div"
        delta_1h = float(getattr(cvd, "delta_1h", 0.0) or 0.0)
        return "bear_div" if delta_1h < 0 else "bull_div"
    return "none"


def _extract_funding(funding: Any) -> Optional[float]:
    if funding is None:
        return None
    val = getattr(funding, "current_rate", None)
    if val is None:
        val = getattr(funding, "weighted_funding", None)
    try:
        return float(val) if val is not None else None
    except (TypeError, ValueError):
        return None


def _extract_squeeze_state(range_signal: Any) -> str:
    """BB Squeeze 状态。range_signal 若无相应字段则为 none。"""
    if range_signal is None:
        return "none"
    sq = getattr(range_signal, "squeeze", None)
    if sq is None:
        # range_signal 可能直接挂 squeeze_state 字段
        raw = getattr(range_signal, "squeeze_state", None)
    else:
        raw = getattr(sq, "state", None)
    if not raw:
        return "none"
    if raw in ("pending", "in_squeeze"):
        return "pending"
    if raw in ("released_up", "breakout_up"):
        return "released_up"
    if raw in ("released_down", "breakout_down"):
        return "released_down"
    return "none"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 主入口
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def build_market_context_from_state(
    state: Any,
    ts: Optional[int] = None,
) -> Optional[MarketContext]:
    """从 CoinState 构造 MarketContext；无价格直接返回 None。"""
    if state is None:
        return None
    ticker = getattr(state, "ticker", None)
    if ticker is None:
        return None
    price = float(getattr(ticker, "last", 0.0) or 0.0)
    if price <= 0:
        return None

    regime, regime_conf = _extract_regime(state)

    # 4h / 1h 结构：当前引擎缺 4h MTF，用 1h (market_structure) + 1d 作为兜底
    ms_1h = getattr(state, "market_structure", None)
    ms_4h = getattr(state, "market_structure_1d", None) or ms_1h

    ms_dir_4h, ms_event_4h, ms_event_side_4h = _extract_market_structure(ms_4h)
    ms_dir_1h, _, _ = _extract_market_structure(ms_1h)

    te_state, te_score = _extract_trend_exhaustion(getattr(state, "trend_exhaustion", None))

    kl_snap = getattr(state, "key_level_snapshot_v2", None)
    nearest = _extract_nearest_key_level(kl_snap, price)
    reversal = _extract_reversal_pattern(kl_snap)

    cvd_div = _extract_cvd_divergence(
        getattr(state, "cvd_contract", None),
        getattr(state, "cvd_spot", None),
    )
    funding_rate = _extract_funding(getattr(state, "funding", None))
    squeeze = _extract_squeeze_state(getattr(state, "range_signal", None))

    # 数据健康
    missing: list[str] = []
    if regime is None:
        missing.append("regime")
    if ms_dir_4h is None:
        missing.append("market_structure_4h")
    if te_state is None:
        missing.append("trend_exhaustion")
    if nearest is None:
        missing.append("key_levels")
    atr = float(getattr(state, "atr", 0.0) or 0.0)
    if atr <= 0:
        missing.append("atr")

    if atr <= 0 or regime is None:
        quality = "partial"
    else:
        quality = "ok"
    # 关键必需字段（price 已校验）缺太多 → insufficient
    if len(missing) >= 4:
        quality = "insufficient"

    return MarketContext(
        ts=ts or int(time.time()),
        current_price=price,
        atr=atr,
        regime=regime,
        regime_confidence=regime_conf,
        ms_direction_4h=ms_dir_4h,
        ms_direction_1h=ms_dir_1h,
        ms_last_event_4h=ms_event_4h,  # type: ignore[arg-type]
        ms_last_event_side_4h=ms_event_side_4h,  # type: ignore[arg-type]
        te_overall_state=te_state,  # type: ignore[arg-type]
        te_overall_score=te_score,
        nearest_level=nearest,
        reversal_pattern=reversal,  # type: ignore[arg-type]
        cvd_divergence=cvd_div,  # type: ignore[arg-type]
        funding_rate=funding_rate,
        squeeze_state=squeeze,  # type: ignore[arg-type]
        data_quality=quality,  # type: ignore[arg-type]
        missing_inputs=missing,
    )
