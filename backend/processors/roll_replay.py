"""滚仓复盘统计 —— 从事件流计算一次持仓的回放摘要。

模块职责：
    输入：UserPosition（含持久化事件）或 (position, events) 二元组
    输出：ReplayStats Pydantic 对象，供 /roll/replay/[id] 展示

设计原则：
    - 纯函数：不读文件，不改变状态，不依赖引擎/服务
    - 覆盖率基于事件流内嵌的 kind/ts/user_override 字段推导
    - P&L 基于 event.price + avg_price 快照，不依赖外部行情

覆盖率匹配窗口：
    alert_X 发生后，若在 FOLLOW_WINDOW_SEC 内出现匹配的用户动作，则记为"已覆盖"。
    匹配关系：
        alert_add      → add / user_override_add
        alert_reduce   → reduce
        alert_close    → close_manual / close_sl_hit / close_tp_hit
        alert_move_sl  → sl_move

若同一 alert 后有多条候选用户事件，采用"先到先配对，已配对不再匹配其他 alert"。
"""

from __future__ import annotations

import statistics
from typing import Iterable, Optional

from models.roll_position import ReplayStats, RollEvent, UserPosition


# 覆盖率匹配窗口（秒）。30 分钟覆盖绝大多数用户反应时间，不会被跨夜噪声污染。
FOLLOW_WINDOW_SEC = 30 * 60

# alert → 期望的用户动作集合
_FOLLOW_MAP: dict[str, tuple[str, ...]] = {
    "alert_add": ("add", "user_override_add"),
    "alert_reduce": ("reduce",),
    "alert_close": ("close_manual", "close_sl_hit", "close_tp_hit"),
    "alert_move_sl": ("sl_move",),
}

# 用户"最终动作"类：统计 adds/reduces 等
_USER_ADD_KINDS = {"add", "user_override_add"}
_USER_REDUCE_KINDS = {"reduce"}
_USER_CLOSE_KINDS = {"close_manual", "close_sl_hit", "close_tp_hit"}
_USER_SL_KINDS = {"sl_move"}


def compute_replay_stats(
    position: UserPosition,
    events: Optional[Iterable[RollEvent]] = None,
) -> ReplayStats:
    """计算单个持仓的复盘统计。

    Args:
        position: 目标持仓（提供 id/coin/side/status 等元信息）
        events:   事件流。None 时回落到 position.events（已内嵌的）
    """
    evs = list(events) if events is not None else list(position.events)
    evs.sort(key=lambda e: e.ts)

    # 基础计数
    counts_by_kind: dict[str, int] = {}
    for e in evs:
        counts_by_kind[e.kind] = counts_by_kind.get(e.kind, 0) + 1

    alerts_by_action = {
        "alert_add": counts_by_kind.get("alert_add", 0),
        "alert_reduce": counts_by_kind.get("alert_reduce", 0),
        "alert_close": counts_by_kind.get("alert_close", 0),
        "alert_move_sl": counts_by_kind.get("alert_move_sl", 0),
    }

    adds = sum(counts_by_kind.get(k, 0) for k in _USER_ADD_KINDS)
    reduces = sum(counts_by_kind.get(k, 0) for k in _USER_REDUCE_KINDS)
    sl_moves = sum(counts_by_kind.get(k, 0) for k in _USER_SL_KINDS)
    closes = sum(counts_by_kind.get(k, 0) for k in _USER_CLOSE_KINDS)
    overrides = counts_by_kind.get("user_override_add", 0)
    gate_blocks = counts_by_kind.get("gate_blocked", 0)

    # 覆盖率：alert → 用户动作匹配
    follow_stats = _match_alerts(evs)

    # 覆盖率汇总
    follow_rate_overall: Optional[float] = None
    total_alerts = sum(alerts_by_action.values())
    if total_alerts > 0:
        matched_total = sum(follow_stats["matched"].values())
        follow_rate_overall = matched_total / total_alerts

    avg_follow_delay_sec = (
        statistics.fmean(follow_stats["delays"])
        if follow_stats["delays"]
        else None
    )

    # 覆盖率
    def _rate(alert_kind: str) -> Optional[float]:
        total = alerts_by_action[alert_kind]
        if total <= 0:
            return None
        return follow_stats["matched"].get(alert_kind, 0) / total

    follow_rate_add = _rate("alert_add")
    follow_rate_reduce = _rate("alert_reduce")
    follow_rate_close = _rate("alert_close")
    follow_rate_move_sl = _rate("alert_move_sl")

    # 覆盖率（用户覆盖加仓比例）
    override_rate: Optional[float] = None
    if adds > 0:
        override_rate = overrides / adds

    # P&L 与峰值
    pnl_usd, peak_margin, peak_lev = _realized_pnl(position, evs)
    initial_margin = position.margin_used_usd if position.status == "active" else _first_margin(evs)
    realized_pnl_pct = (pnl_usd / initial_margin * 100.0) if initial_margin > 0 else 0.0

    # 关仓信息
    final_close_kind = None
    for e in reversed(evs):
        if e.kind in _USER_CLOSE_KINDS:
            final_close_kind = e.kind
            break

    # 时间段
    opened_at = evs[0].ts if evs else position.created_at
    closed_at = position.closed_at
    if closed_at is None and final_close_kind:
        # 未及时更新 closed_at（例如 replay 时传入 active 位置 + 纯事件流）
        for e in reversed(evs):
            if e.kind in _USER_CLOSE_KINDS:
                closed_at = e.ts
                break

    if closed_at:
        duration_sec = max(0, int(closed_at - opened_at))
    else:
        # 未平仓：用最后事件 or 当前时间回退为 0（UI 自己决定是否展示）
        duration_sec = max(0, int(evs[-1].ts - opened_at)) if evs else 0

    return ReplayStats(
        position_id=position.id,
        plan_id=position.plan_id,
        coin=position.coin,
        side=position.side,
        status=position.status,
        opened_at=opened_at,
        closed_at=closed_at,
        duration_sec=duration_sec,
        total_events=len(evs),
        counts_by_kind=counts_by_kind,
        adds=adds,
        reduces=reduces,
        sl_moves=sl_moves,
        closes=closes,
        overrides=overrides,
        gate_blocks=gate_blocks,
        alerts_by_action=alerts_by_action,
        follow_rate_add=follow_rate_add,
        follow_rate_reduce=follow_rate_reduce,
        follow_rate_close=follow_rate_close,
        follow_rate_move_sl=follow_rate_move_sl,
        follow_rate_overall=follow_rate_overall,
        avg_follow_delay_sec=avg_follow_delay_sec,
        override_rate=override_rate,
        realized_pnl_usd=pnl_usd,
        realized_pnl_pct=realized_pnl_pct,
        max_margin_used_usd=peak_margin,
        peak_effective_leverage=peak_lev,
        final_close_kind=final_close_kind,
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 辅助
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _match_alerts(events: list[RollEvent]) -> dict:
    """贪心匹配 alert_X → 最近的合法用户事件（同 alert 窗口内 + 未被先前 alert 占用）。

    Returns:
        {"matched": {alert_kind: int}, "delays": [seconds...]}
    """
    matched: dict[str, int] = {k: 0 for k in _FOLLOW_MAP}
    delays: list[float] = []

    used_user_idx: set[int] = set()

    for ai, alert in enumerate(events):
        expect = _FOLLOW_MAP.get(alert.kind)
        if not expect:
            continue
        for ui in range(ai + 1, len(events)):
            if ui in used_user_idx:
                continue
            cand = events[ui]
            delay = cand.ts - alert.ts
            if delay > FOLLOW_WINDOW_SEC:
                break
            if cand.kind in expect:
                matched[alert.kind] += 1
                delays.append(float(delay))
                used_user_idx.add(ui)
                break

    return {"matched": matched, "delays": delays}


def _realized_pnl(
    position: UserPosition,
    events: list[RollEvent],
) -> tuple[float, float, float]:
    """从事件流重建已实现 P&L、峰值保证金、峰值有效杠杆。

    P&L 口径：
        - 以 size 视角：每次 reduce/close，按 |size_delta| × (event.price - avg_before) × side_dir
        - avg_before 取上一条有 avg_price_after 的事件；若首事件，取 position.entry_price
        - size_delta 对 long 来说：add>0 / reduce<0；short 相反
          → 绝对值 × 方向系数 保证"多头 price 上涨为正、空头 price 下跌为正"
    """
    side_dir = 1.0 if position.side == "long" else -1.0

    pnl = 0.0
    peak_margin = 0.0
    peak_lev = 0.0
    avg_before = position.entry_price

    for e in events:
        if e.leverage_after > peak_lev:
            peak_lev = e.leverage_after

        # reduce/close/sl_hit/tp_hit 都按平仓处理
        if e.kind in _USER_REDUCE_KINDS | _USER_CLOSE_KINDS:
            closed_size = abs(e.size_delta)
            if closed_size > 0:
                pnl += closed_size * (e.price - avg_before) * side_dir
        else:
            # 非平仓事件：更新 avg_before（add / user_override_add 会拉高平均价）
            if e.avg_price_after > 0 and e.kind in _USER_ADD_KINDS | {"init"}:
                avg_before = e.avg_price_after

        # margin 快照估算（margin_used = position_value / leverage）
        # leverage_after 为 0 时跳过（例如 alert 事件）
        if e.leverage_after > 0 and e.avg_price_after > 0:
            # 这里无法严格还原 size 序列；用 position.position_size 作为上限估计
            # 实际峰值由 UserPosition.margin_used_usd 持续追踪更准确，这里仅用于 UI 近似
            margin_est = (abs(e.size_delta) * e.price) / max(e.leverage_after, 0.01)
            if margin_est > peak_margin:
                peak_margin = margin_est

    # 最终兜底：如果 events 没能推出任何 margin（例如只含 alert），使用当前 margin_used
    if peak_margin == 0.0:
        peak_margin = position.margin_used_usd

    return pnl, peak_margin, peak_lev


def _first_margin(events: list[RollEvent]) -> float:
    """从事件流找到初始保证金（init 事件）。找不到则返回 0。"""
    for e in events:
        if e.kind == "init" and e.margin_delta_usd > 0:
            return e.margin_delta_usd
    return 0.0
