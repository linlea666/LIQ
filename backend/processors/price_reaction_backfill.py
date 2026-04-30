"""新闻价格反应回填器（D04+）

职责：
  - 对账本里 backfill_status in (pending, partial) 的 EnrichedNewsEvent 扫描
  - 在 price_history 中匹配 publish_time 前后 +1h / +2h / +24h 的价格
  - 填充 price_at_publish / price_after_Nh / delta_pct_Nh
  - 同步回写 NarrativeTracker.record_price_reaction（仅 delta_pct_1h 已有时）

落实日志锚点：
  - D.D04_BACKTEST_LOOP：每次 run 上报 processed / newly_complete / reactions_recorded

特性：
  - 纯函数：不就地修改入参，返回新的 EnrichedNewsEvent 列表副本
  - price_history 假设按 ts 升序排序；内部使用 bisect 二分查找
  - 1h/2h 档缺失不影响 complete 判定（仅 24h 已填且 event 已过 24h 才算 complete）
  - 支持 "部分填充" 状态 partial（1h 已填但 2h/24h 未到）
"""

from __future__ import annotations

import bisect
import logging
import time
from typing import Any, Optional

from models.narrative import NarrativePriceReaction
from models.news_event import EnrichedNewsEvent

logger = logging.getLogger(__name__)


_HOUR = 3600
_DAY = 24 * _HOUR


def backfill_price_reactions(
    events: list[EnrichedNewsEvent],
    price_history: list[dict],
    now_ts: int,
    *,
    forward_fill_tolerance_min: int = 5,
    narrative_tracker: Any = None,
) -> tuple[list[EnrichedNewsEvent], dict]:
    """扫描事件列表，按可填充程度回写价格反应。

    参数：
      events            — 账本中 backfill_status != 'complete' 的事件
      price_history     — 已按 ts 升序排序的 [{ts: int(sec), price: float}] 序列
      now_ts            — 当前秒级时间
      forward_fill_tolerance_min — 精确分钟价格缺失时，允许最多向前/向后 N 分钟匹配
      narrative_tracker — 可选；若提供且 1h 已填，则调用 record_price_reaction

    返回 (updated_events, stats)
    """
    stats: dict = {
        "processed": 0,
        "newly_complete": 0,
        "newly_partial": 0,
        "still_pending": 0,
        "narrative_reactions_recorded": 0,
        "price_history_size": len(price_history) if price_history else 0,
    }
    if not events:
        _mark_d04bf(stats)
        return [], stats

    if not price_history:
        # 没价格数据 → 原样返回
        out = [e.model_copy(deep=True) for e in events]
        stats["processed"] = len(out)
        stats["still_pending"] = len(out)
        _mark_d04bf(stats)
        return out, stats

    tolerance_sec = max(0, int(forward_fill_tolerance_min)) * 60
    ts_index = [int(p["ts"]) for p in price_history]

    out: list[EnrichedNewsEvent] = []
    for ev in events:
        new_ev = ev.model_copy(deep=True)
        stats["processed"] += 1

        status_before = new_ev.backfill_status
        publish_sec = int(new_ev.structured.ts or 0)
        if publish_sec <= 0 and new_ev.raw.publish_time > 0:
            publish_sec = int(new_ev.raw.publish_time // 1000)
        if publish_sec <= 0:
            # 无时间戳无法回填
            if status_before != "complete":
                stats["still_pending"] += 1
            out.append(new_ev)
            continue

        # 取 publish 时刻基准价
        if new_ev.price_at_publish is None:
            p_pub = _find_price_at(price_history, ts_index, publish_sec, tolerance_sec)
            if p_pub is not None:
                new_ev.price_at_publish = float(p_pub)

        base = new_ev.price_at_publish

        # 1h
        if new_ev.delta_pct_1h is None and now_ts - publish_sec >= _HOUR:
            p1 = _find_price_at(price_history, ts_index, publish_sec + _HOUR, tolerance_sec)
            if p1 is not None:
                new_ev.price_after_1h = float(p1)
                if base is not None:
                    new_ev.delta_pct_1h = _compute_delta_pct(base, p1)

        # 2h
        if new_ev.delta_pct_2h is None and now_ts - publish_sec >= 2 * _HOUR:
            p2 = _find_price_at(price_history, ts_index, publish_sec + 2 * _HOUR, tolerance_sec)
            if p2 is not None:
                new_ev.price_after_2h = float(p2)
                if base is not None:
                    new_ev.delta_pct_2h = _compute_delta_pct(base, p2)

        # 24h
        if new_ev.delta_pct_24h is None and now_ts - publish_sec >= _DAY:
            p24 = _find_price_at(price_history, ts_index, publish_sec + _DAY, tolerance_sec)
            if p24 is not None:
                new_ev.price_after_24h = float(p24)
                if base is not None:
                    new_ev.delta_pct_24h = _compute_delta_pct(base, p24)

        # 状态更新
        new_status = _resolve_status(new_ev, now_ts, publish_sec)
        new_ev.backfill_status = new_status
        if new_status in ("partial", "complete"):
            new_ev.backfilled_at = int(now_ts)

        if status_before != "complete" and new_status == "complete":
            stats["newly_complete"] += 1
        elif status_before == "pending" and new_status == "partial":
            stats["newly_partial"] += 1
        elif new_status == "pending":
            stats["still_pending"] += 1

        # 回写 NarrativeTracker（仅 1h 已填且 tracker 提供）
        if (
            narrative_tracker is not None
            and new_ev.delta_pct_1h is not None
            and status_before != "complete"
        ):
            try:
                reaction = NarrativePriceReaction(
                    event_id=new_ev.structured.event_id,
                    ts=publish_sec,
                    direction=new_ev.structured.direction,
                    impact_score=int(new_ev.structured.impact_score),
                    price_before=float(base or 0.0),
                    delta_pct_1h=new_ev.delta_pct_1h,
                    delta_pct_2h=new_ev.delta_pct_2h,
                    delta_pct_24h=new_ev.delta_pct_24h,
                    matched_direction=_check_direction_match(
                        new_ev.structured.direction, new_ev.delta_pct_1h
                    ),
                )
                recorded = bool(narrative_tracker.record_price_reaction(
                    new_ev.structured.event_id, reaction
                ))
                if recorded:
                    stats["narrative_reactions_recorded"] += 1
            except Exception as e:  # noqa: BLE001
                logger.debug(
                    "[D04BF] record_price_reaction failed event=%s: %s",
                    new_ev.structured.event_id, e,
                )

        out.append(new_ev)

    _mark_d04bf(stats)
    return out, stats


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 辅助
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _find_price_at(
    price_history: list[dict],
    ts_index: list[int],
    target_ts: int,
    tolerance_sec: int,
) -> Optional[float]:
    """在 price_history 中找最接近 target_ts 的价格（|Δt| ≤ tolerance_sec）。

    策略：bisect 二分定位，检查左右两个邻近点，取距离更近的。
    """
    if not price_history or not ts_index:
        return None

    idx = bisect.bisect_left(ts_index, target_ts)

    candidates: list[tuple[int, dict]] = []
    if idx < len(price_history):
        candidates.append((abs(ts_index[idx] - target_ts), price_history[idx]))
    if idx > 0:
        candidates.append((abs(ts_index[idx - 1] - target_ts), price_history[idx - 1]))

    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0])
    dt, best = candidates[0]
    if tolerance_sec > 0 and dt > tolerance_sec:
        return None
    try:
        p = float(best.get("price"))
    except (TypeError, ValueError):
        return None
    if p <= 0:
        return None
    return p


def _compute_delta_pct(base: float, target: float) -> float:
    """(target - base) / base * 100，base <= 0 返回 0"""
    try:
        b = float(base)
        t = float(target)
    except (TypeError, ValueError):
        return 0.0
    if b <= 0:
        return 0.0
    return round((t - b) / b * 100.0, 4)


def _resolve_status(ev: EnrichedNewsEvent, now_ts: int, publish_sec: int) -> str:
    """按已填档位和经过时间决定 status。

    规则：
      - 1h/2h/24h 档都已填 → complete
      - 事件已过 24h 且 24h 档未填 → partial（永远不会完整）
      - 事件未过 24h 且至少 1 档已填 → partial
      - 否则 → pending
    """
    age = now_ts - publish_sec
    has_1h = ev.delta_pct_1h is not None
    has_2h = ev.delta_pct_2h is not None
    has_24h = ev.delta_pct_24h is not None

    if has_1h and has_2h and has_24h:
        return "complete"
    # 事件已过 24 小时但某一档还是 None → 最多 partial
    if age >= _DAY:
        if has_1h or has_2h or has_24h:
            return "partial"
        return "pending"
    if has_1h or has_2h:
        return "partial"
    return "pending"


def _check_direction_match(direction: str, delta_pct_1h: Optional[float]) -> Optional[bool]:
    if delta_pct_1h is None:
        return None
    # ±0.2% 以内视为无显著反应 → 与方向判定无关
    if abs(delta_pct_1h) < 0.2:
        return None
    if direction == "bullish":
        return delta_pct_1h > 0
    if direction == "bearish":
        return delta_pct_1h < 0
    # neutral / potential_reversal → 不参与命中率统计
    return None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Decision Tracker
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _mark_d04bf(stats: dict) -> None:
    """PR-3 · decision_tracker 已下线，本函数保留壳。"""
    return
