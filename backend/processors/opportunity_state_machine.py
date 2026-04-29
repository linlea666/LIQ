"""TradeSetupCandidate 事件驱动状态机（只读消费 wall_events / KL 状态）。

铁律
====
- 只读消费已有引擎事件，不修改 WallZone / KeyLevelV2 / 评分字段
- 不输出交易指令；状态机只负责描述「观察区现在到哪一步了」

状态转换图（MVP 简化版）
======================
    forming
       │ price 接近入场区 / regime 适配
       ▼
    waiting_for_trigger
       │ 价格落入入场区
       ▼ ────────────────────────────┐
    triggered                          │ cancel_conditions 触发
       │                               │ → cancelled
       │ 软失效快速收回 / 墙重挂 / CVD 不背离
       ▼                               │
    confirmation_pending               │
       │ 确认达标                       │
       ▼                               │
    confirmed ─────────────────────────┤
                                       │
    硬止损被穿 → invalidated            │
    一段时间未触发 + 远离 → missed       │
    invalidated/missed → cooldown      ┘

实现思路
========
- 输入：当前 setup（status name = forming/waiting/triggered/...）+
  最新 last_price / wall_events 切片 / KL state（possible signals）+ 当前 dq
- 输出：新的 SetupState（name + since_ts + history 追加 + pending_reason）
- 不直接修改输入 setup 对象；返回新 SetupState 由调用方写回

使用方式
========
    from processors.opportunity_state_machine import advance_setup_state
    new_state = advance_setup_state(setup, ctx)
    setup.state = new_state

被 trading_brain_builder 在 build_opportunities 之后调用一次，
让首屏即看到准确的状态分布；后续靠 events 推动持续刷新。
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

from models.orderbook_pressure import WallEvent
from models.trading_brain import (
    BrainContextChips,
    BrainDataQuality,
    SetupState,
    SetupStateName,
    TradeSetupCandidate,
)


@dataclass
class StateTickContext:
    """状态机 tick 输入。"""
    last_price: float
    now_sec: int
    wall_events: list[WallEvent]
    """最近窗口 wall_events（建议传 30 分钟切片，已按 ts 升序）。"""
    ctx: Optional[BrainContextChips] = None
    dq: Optional[BrainDataQuality] = None


_HISTORY_LIMIT = 5
_MISSED_AGE_SEC = 60 * 30  # 30 分钟未触发 + 远离 → missed
_COOLDOWN_AGE_SEC = 60 * 30  # invalidated/cancelled 后 30 分钟内为 cooldown


def _push_history(
    state: SetupState, *, src: SetupStateName, dst: SetupStateName, reason: str, ts: int,
) -> list[dict]:
    item = {"ts": ts, "from": src, "to": dst, "reason": reason}
    new_hist = (list(state.history) + [item])[-_HISTORY_LIMIT:]
    return new_hist


def _new_state(
    *, prev: SetupState, dst: SetupStateName, reason: str, ts: int,
    pending: str = "",
) -> SetupState:
    if dst == prev.name:
        # 名字未变；只刷新 pending_reason，不计入历史
        return SetupState(
            name=prev.name,
            since_ts=prev.since_ts or ts,
            pending_reason=pending or prev.pending_reason,
            history=list(prev.history),
        )
    return SetupState(
        name=dst,
        since_ts=ts,
        pending_reason=pending,
        history=_push_history(prev, src=prev.name, dst=dst, reason=reason, ts=ts),
    )


def _events_in_zone(
    events: list[WallEvent], *, lo: float, hi: float, after_ts: int = 0,
) -> list[WallEvent]:
    out: list[WallEvent] = []
    for e in events or []:
        if e.ts_sec < after_ts:
            continue
        if e.price_mid >= lo and e.price_mid <= hi:
            out.append(e)
    return out


def _has_recent_consume(events: list[WallEvent]) -> bool:
    for e in events:
        et = str(e.event_type)
        if et in ("wall_consumed", "wall_consumed_and_removed"):
            return True
    return False


def _has_recent_reload_or_strengthen(events: list[WallEvent]) -> bool:
    for e in events:
        et = str(e.event_type)
        if et in ("wall_reloaded", "wall_strengthened", "wall_appeared"):
            return True
    return False


def _has_recent_remove_or_weaken(events: list[WallEvent]) -> bool:
    for e in events:
        et = str(e.event_type)
        if et in ("wall_removed", "wall_weakened"):
            return True
    return False


def _regime_blocks(setup: TradeSetupCandidate, ctx: Optional[BrainContextChips]) -> bool:
    if ctx is None:
        return False
    r = (ctx.regime or "").lower()
    if setup.direction == "long" and r in ("trend_down", "down_trend", "bearish_trend"):
        return True
    if setup.direction == "short" and r in ("trend_up", "up_trend", "bullish_trend"):
        return True
    return False


def advance_setup_state(
    setup: TradeSetupCandidate,
    tick: StateTickContext,
) -> SetupState:
    """根据 tick 推进 setup 状态；返回新的 SetupState（不原地修改）。

    优先级：
        1. 硬止损被穿 → invalidated
        2. cancel 触发（regime 反转 / 数据 stale 长期 / 墙撤出 / 失效结构）→ cancelled
        3. forming → waiting_for_trigger（价格接近）
        4. waiting → triggered（价格落入入场区）
        5. triggered → confirmation_pending（事件信号支持）
        6. confirmation_pending → confirmed（持续证据）
        7. waiting/triggered 老化 → missed
        8. invalidated/cancelled/missed 30min 内 → cooldown
    """
    prev = setup.state
    now = int(tick.now_sec or time.time())
    last = float(tick.last_price)
    rp = setup.risk_plan

    # 1. 硬止损被穿
    if setup.direction == "long" and last <= rp.hard_stop:
        return _new_state(prev=prev, dst="invalidated",
                           reason="价格跌破硬止损", ts=now)
    if setup.direction == "short" and last >= rp.hard_stop:
        return _new_state(prev=prev, dst="invalidated",
                           reason="价格突破硬止损", ts=now)

    # 2. regime 反转 → cancelled
    if _regime_blocks(setup, tick.ctx):
        return _new_state(prev=prev, dst="cancelled",
                           reason="Regime 切换至反向趋势", ts=now)

    # 已是终态 → cooldown 计时
    if prev.name in ("invalidated", "cancelled", "missed"):
        if now - (prev.since_ts or now) >= _COOLDOWN_AGE_SEC:
            return prev
        return _new_state(prev=prev, dst="cooldown",
                           reason="进入冷却窗口", ts=now)
    if prev.name == "cooldown":
        return prev  # 不再变更
    if prev.name == "confirmed":
        return prev

    # zone 区间事件切片
    primary_entry = setup.entry_styles[0].entry_zone if setup.entry_styles else (rp.soft_invalidation, rp.soft_invalidation)
    z_lo = min(primary_entry[0], primary_entry[1])
    z_hi = max(primary_entry[0], primary_entry[1])
    recent_events = _events_in_zone(
        tick.wall_events, lo=z_lo, hi=z_hi, after_ts=now - 1800,
    )

    # 3 & 4. price 状态推进
    in_zone = z_lo <= last <= z_hi
    near_zone = (
        not in_zone
        and abs(last - (z_lo + z_hi) / 2) <= max((z_hi - z_lo) * 1.5, 1.0)
    )

    # 4b. waiting/forming → triggered
    if in_zone and prev.name in ("forming", "waiting_for_trigger"):
        return _new_state(prev=prev, dst="triggered",
                           reason="价格落入入场区",
                           pending="等待吸收/墙重挂/CVD 不背离 等确认", ts=now)

    # 5. triggered → confirmation_pending
    if prev.name == "triggered":
        if _has_recent_reload_or_strengthen(recent_events):
            return _new_state(prev=prev, dst="confirmation_pending",
                               reason="同价区出现重挂/增厚事件",
                               pending="等待持续吸收 + 二次回踩缩量", ts=now)
        if _has_recent_remove_or_weaken(recent_events) and _has_recent_consume(recent_events):
            return _new_state(prev=prev, dst="cancelled",
                               reason="墙被消耗 + 撤单组合，结构告破", ts=now)

    # 6. confirmation_pending → confirmed
    if prev.name == "confirmation_pending":
        if _has_recent_reload_or_strengthen(recent_events) and not _has_recent_remove_or_weaken(recent_events):
            return _new_state(prev=prev, dst="confirmed",
                               reason="重挂/增厚连续 + 无撤离信号", ts=now)

    # 3. forming → waiting
    if prev.name == "forming" and (in_zone or near_zone):
        return _new_state(prev=prev, dst="waiting_for_trigger",
                           reason="价格接近入场区",
                           pending="等待价格进入入场区或扫破后收回", ts=now)

    # 7. waiting/forming 老化 → missed（远离且超龄）
    if prev.name in ("waiting_for_trigger", "forming"):
        age = now - (prev.since_ts or now)
        if age >= _MISSED_AGE_SEC and not in_zone and not near_zone:
            return _new_state(prev=prev, dst="missed",
                               reason="入场区长期未被触达且远离", ts=now)

    return prev


def advance_all(
    setups: list[TradeSetupCandidate],
    tick: StateTickContext,
) -> list[TradeSetupCandidate]:
    """便捷批处理：原地写回新的 state。"""
    for s in setups:
        s.state = advance_setup_state(s, tick)
    return setups
