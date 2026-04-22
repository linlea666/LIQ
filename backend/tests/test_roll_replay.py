"""processors/roll_replay.py 单元测试"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from models.roll_position import RollEvent, UserPosition  # noqa: E402
from processors.roll_replay import (  # noqa: E402
    FOLLOW_WINDOW_SEC,
    compute_replay_stats,
)


def _make_position(
    coin: str = "BTC",
    side: str = "long",
    entry_price: float = 100.0,
    initial_margin: float = 100.0,
    events: list[RollEvent] | None = None,
    status: str = "closed",
    closed_at: int | None = 10_000,
) -> UserPosition:
    return UserPosition(
        id="pos-1",
        coin=coin,
        side=side,  # type: ignore[arg-type]
        margin_mode="isolated",
        leverage=10,
        entry_price=entry_price,
        position_size=1.0,
        position_value_usd=entry_price * 1.0,
        margin_used_usd=initial_margin,
        total_account_usd=10_000.0,
        status=status,  # type: ignore[arg-type]
        plan_id="plan-1",
        created_at=1_000,
        updated_at=10_000,
        closed_at=closed_at,
        events=events or [],
        note="",
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 基础计数
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_counts_empty_events():
    pos = _make_position()
    stats = compute_replay_stats(pos)
    assert stats.total_events == 0
    assert stats.adds == 0
    assert stats.follow_rate_overall is None
    assert stats.override_rate is None


def test_counts_basic_lifecycle():
    events = [
        RollEvent(ts=1000, kind="init", price=100, margin_delta_usd=100, size_delta=1.0,
                  avg_price_after=100, leverage_after=10, liq_price_after=90),
        RollEvent(ts=2000, kind="alert_add", system_action="add", system_confidence=75.0),
        RollEvent(ts=2060, kind="add", price=105, margin_delta_usd=50, size_delta=0.5,
                  avg_price_after=101.67, leverage_after=11),
        RollEvent(ts=3000, kind="alert_reduce", system_action="reduce", system_confidence=65.0),
        RollEvent(ts=3090, kind="reduce", price=110, margin_delta_usd=-30, size_delta=-0.3,
                  avg_price_after=101.67, leverage_after=9),
        RollEvent(ts=4000, kind="user_override_add", price=108, margin_delta_usd=80,
                  size_delta=0.5, avg_price_after=103.5, leverage_after=12, user_override=True),
        RollEvent(ts=5000, kind="gate_blocked", reason="gate A failed"),
        RollEvent(ts=9000, kind="close_manual", price=120, margin_delta_usd=-200,
                  size_delta=-1.7, avg_price_after=103.5, leverage_after=0),
    ]
    pos = _make_position(events=events)
    stats = compute_replay_stats(pos)

    assert stats.total_events == len(events)
    assert stats.counts_by_kind["add"] == 1
    assert stats.counts_by_kind["reduce"] == 1
    assert stats.counts_by_kind["user_override_add"] == 1
    assert stats.counts_by_kind["gate_blocked"] == 1
    assert stats.adds == 2                # add + user_override_add
    assert stats.reduces == 1
    assert stats.closes == 1
    assert stats.overrides == 1
    assert stats.gate_blocks == 1
    assert stats.final_close_kind == "close_manual"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 覆盖率
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_follow_rate_alert_add_followed_in_window():
    events = [
        RollEvent(ts=1000, kind="alert_add"),
        RollEvent(ts=1300, kind="add", price=100, size_delta=0.5, avg_price_after=100, leverage_after=10),
    ]
    stats = compute_replay_stats(_make_position(events=events))
    assert stats.follow_rate_add == 1.0
    assert stats.avg_follow_delay_sec == 300.0


def test_follow_rate_alert_add_ignored_after_window():
    events = [
        RollEvent(ts=1000, kind="alert_add"),
        # 32 min later > FOLLOW_WINDOW_SEC
        RollEvent(ts=1000 + FOLLOW_WINDOW_SEC + 60, kind="add",
                  price=100, size_delta=0.5, avg_price_after=100, leverage_after=10),
    ]
    stats = compute_replay_stats(_make_position(events=events))
    assert stats.follow_rate_add == 0.0


def test_follow_rate_override_counts_as_followed_for_add():
    events = [
        RollEvent(ts=1000, kind="alert_add"),
        RollEvent(ts=1200, kind="user_override_add", price=100, size_delta=0.5,
                  avg_price_after=100, leverage_after=10, user_override=True),
    ]
    stats = compute_replay_stats(_make_position(events=events))
    assert stats.follow_rate_add == 1.0


def test_follow_rate_greedy_pairing_no_double_count():
    events = [
        RollEvent(ts=1000, kind="alert_add"),
        RollEvent(ts=1100, kind="alert_add"),
        # 只有一次真实加仓 —— 第一个 alert 配对，第二个 alert 仍然 unmatched
        RollEvent(ts=1200, kind="add", price=100, size_delta=0.5,
                  avg_price_after=100, leverage_after=10),
    ]
    stats = compute_replay_stats(_make_position(events=events))
    assert stats.alerts_by_action["alert_add"] == 2
    assert stats.follow_rate_add == 0.5


def test_follow_rate_mixed_alert_types():
    events = [
        RollEvent(ts=1000, kind="alert_reduce"),
        RollEvent(ts=1200, kind="reduce", price=105, size_delta=-0.3,
                  avg_price_after=100, leverage_after=9),
        RollEvent(ts=2000, kind="alert_move_sl"),
        RollEvent(ts=2300, kind="sl_move", price=108, size_delta=0,
                  avg_price_after=100, leverage_after=9),
        RollEvent(ts=3000, kind="alert_close"),
        # close 没跟上 → 不算
    ]
    stats = compute_replay_stats(_make_position(events=events))
    assert stats.follow_rate_reduce == 1.0
    assert stats.follow_rate_move_sl == 1.0
    assert stats.follow_rate_close == 0.0
    assert abs((stats.follow_rate_overall or 0) - (2 / 3)) < 1e-6


def test_override_rate():
    events = [
        RollEvent(ts=1000, kind="add", price=100, size_delta=0.5,
                  avg_price_after=100, leverage_after=10),
        RollEvent(ts=2000, kind="add", price=100, size_delta=0.5,
                  avg_price_after=100, leverage_after=10),
        RollEvent(ts=3000, kind="user_override_add", price=100, size_delta=0.5,
                  avg_price_after=100, leverage_after=10, user_override=True),
    ]
    stats = compute_replay_stats(_make_position(events=events))
    assert stats.adds == 3
    assert stats.overrides == 1
    assert abs(stats.override_rate - 1 / 3) < 1e-6


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# P&L
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_pnl_long_full_close_profit():
    # 100 → 120, size=1.0, long，收益 = 20
    events = [
        RollEvent(ts=1000, kind="init", price=100, margin_delta_usd=100, size_delta=1.0,
                  avg_price_after=100, leverage_after=10),
        RollEvent(ts=5000, kind="close_manual", price=120, margin_delta_usd=-100,
                  size_delta=-1.0, avg_price_after=100, leverage_after=0),
    ]
    stats = compute_replay_stats(_make_position(events=events))
    assert abs(stats.realized_pnl_usd - 20.0) < 1e-6
    assert stats.final_close_kind == "close_manual"


def test_pnl_short_full_close_profit():
    # short @ 100，close @ 80，size=1，收益 = 20
    events = [
        RollEvent(ts=1000, kind="init", price=100, margin_delta_usd=100, size_delta=1.0,
                  avg_price_after=100, leverage_after=10),
        RollEvent(ts=5000, kind="close_manual", price=80, margin_delta_usd=-100,
                  size_delta=-1.0, avg_price_after=100, leverage_after=0),
    ]
    pos = _make_position(side="short", events=events)
    stats = compute_replay_stats(pos)
    assert abs(stats.realized_pnl_usd - 20.0) < 1e-6


def test_pnl_long_partial_reduce_then_close():
    # 100 → reduce@110 with 0.5 → 10；then close@120 with 0.5 → 10；total 20
    events = [
        RollEvent(ts=1000, kind="init", price=100, margin_delta_usd=100, size_delta=1.0,
                  avg_price_after=100, leverage_after=10),
        RollEvent(ts=3000, kind="reduce", price=110, margin_delta_usd=-50,
                  size_delta=-0.5, avg_price_after=100, leverage_after=10),
        RollEvent(ts=5000, kind="close_manual", price=120, margin_delta_usd=-50,
                  size_delta=-0.5, avg_price_after=100, leverage_after=0),
    ]
    # reduce leg: 0.5 × (110 - 100) = 5；close leg: 0.5 × (120 - 100) = 10；total 15
    stats = compute_replay_stats(_make_position(events=events))
    assert abs(stats.realized_pnl_usd - 15.0) < 1e-6


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 时间范围
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_duration_from_first_event_to_closed_at():
    events = [
        RollEvent(ts=1000, kind="init", price=100, margin_delta_usd=100, size_delta=1.0,
                  avg_price_after=100, leverage_after=10),
        RollEvent(ts=8000, kind="close_manual", price=105, margin_delta_usd=-100,
                  size_delta=-1.0, avg_price_after=100, leverage_after=0),
    ]
    pos = _make_position(events=events, closed_at=8000)
    stats = compute_replay_stats(pos)
    assert stats.opened_at == 1000
    assert stats.closed_at == 8000
    assert stats.duration_sec == 7000
