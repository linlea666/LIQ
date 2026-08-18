"""OpportunityStateMachine 单元测试：状态转换 + 边界 + 历史。"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from models.orderbook_pressure import WallEvent
from models.trading_brain import (
    BrainContextChips,
    SetupEntryStyle,
    SetupRiskPlan,
    SetupState,
    SetupTarget,
    TradeSetupCandidate,
)
from processors.opportunity_state_machine import (
    StateTickContext,
    advance_setup_state,
)


def _make_long_setup(
    *, state_name="forming", since_ts=0, entry=(99_000.0, 99_200.0),
    soft=98_900.0, hard=98_700.0,
) -> TradeSetupCandidate:
    return TradeSetupCandidate(
        setup_id="BTC_support_limit_probe_test",
        coin="BTC",
        zone_id="z1",
        setup_type="support_limit_probe",
        direction="long",
        entry_styles=[
            SetupEntryStyle(style="aggressive", entry_zone=entry, requires=[], risk_note=""),
        ],
        risk_plan=SetupRiskPlan(soft_invalidation=soft, hard_stop=hard),
        targets=[SetupTarget(price=101_000.0, type="key_level", rr=2.5)],
        asymmetry_score=0.7,
        opportunity_score=0.6,
        data_confidence=0.85,
        state=SetupState(name=state_name, since_ts=since_ts or int(time.time())),
        cancel_conditions=[],
        evidence=[],
    )


def _make_short_setup(**kw) -> TradeSetupCandidate:
    s = _make_long_setup(**kw)
    s.direction = "short"
    s.setup_type = "resistance_limit_probe"
    s.risk_plan = SetupRiskPlan(soft_invalidation=99_300.0, hard_stop=99_500.0)
    s.entry_styles[0].entry_zone = (99_000.0, 99_200.0)
    s.state = SetupState(name="forming", since_ts=int(time.time()))
    return s


def test_long_hard_stop_invalidates():
    s = _make_long_setup(state_name="triggered", since_ts=int(time.time()) - 100)
    tick = StateTickContext(last_price=98_600.0, now_sec=int(time.time()),
                            wall_events=[])
    new = advance_setup_state(s, tick)
    assert new.name == "invalidated"


def test_short_hard_stop_invalidates_via_break_up():
    s = _make_short_setup()
    s.state = SetupState(name="triggered", since_ts=int(time.time()) - 50)
    tick = StateTickContext(last_price=99_600.0, now_sec=int(time.time()),
                            wall_events=[])
    new = advance_setup_state(s, tick)
    assert new.name == "invalidated"


def test_forming_to_waiting_when_price_near_zone():
    s = _make_long_setup(state_name="forming")
    tick = StateTickContext(last_price=99_300.0, now_sec=int(time.time()),
                            wall_events=[])
    new = advance_setup_state(s, tick)
    assert new.name == "waiting_for_trigger"


def test_waiting_to_triggered_when_price_in_zone():
    s = _make_long_setup(state_name="waiting_for_trigger")
    tick = StateTickContext(last_price=99_100.0, now_sec=int(time.time()),
                            wall_events=[])
    new = advance_setup_state(s, tick)
    assert new.name == "triggered"


def test_triggered_to_confirmation_pending_on_reload():
    s = _make_long_setup(state_name="triggered")
    now = int(time.time())
    evts = [WallEvent(ts_sec=now - 60, side="bid", price_mid=99_080.0,
                       event_type="wall_reloaded")]
    tick = StateTickContext(last_price=99_100.0, now_sec=now, wall_events=evts)
    new = advance_setup_state(s, tick)
    assert new.name == "confirmation_pending"


def test_confirmation_pending_to_confirmed_with_strengthen():
    s = _make_long_setup(state_name="confirmation_pending")
    now = int(time.time())
    evts = [
        WallEvent(ts_sec=now - 30, side="bid", price_mid=99_100.0,
                  event_type="wall_strengthened"),
    ]
    tick = StateTickContext(last_price=99_120.0, now_sec=now, wall_events=evts)
    new = advance_setup_state(s, tick)
    assert new.name == "confirmed"


def test_regime_flip_cancels_long_setup():
    s = _make_long_setup(state_name="waiting_for_trigger")
    tick = StateTickContext(
        last_price=99_300.0, now_sec=int(time.time()),
        wall_events=[],
        ctx=BrainContextChips(regime="trend_down"),
    )
    new = advance_setup_state(s, tick)
    assert new.name == "cancelled"


def test_missed_when_long_waiting_too_long_and_far():
    s = _make_long_setup(state_name="waiting_for_trigger",
                         since_ts=int(time.time()) - 60 * 35)
    tick = StateTickContext(last_price=105_000.0, now_sec=int(time.time()),
                            wall_events=[])
    new = advance_setup_state(s, tick)
    assert new.name == "missed"


def test_state_transition_writes_history():
    s = _make_long_setup(state_name="forming")
    tick = StateTickContext(last_price=99_300.0, now_sec=int(time.time()),
                            wall_events=[])
    new = advance_setup_state(s, tick)
    assert len(new.history) == 1
    item = new.history[-1]
    assert item["from"] == "forming"
    assert item["to"] == "waiting_for_trigger"


def test_terminal_states_enter_cooldown():
    now = int(time.time())
    s = _make_long_setup(state_name="invalidated", since_ts=now - 60)
    tick = StateTickContext(last_price=98_800.0, now_sec=now, wall_events=[])
    new = advance_setup_state(s, tick)
    assert new.name == "cooldown"


def test_cooldown_freezes():
    """cooldown 状态不再被任何 tick 推进。"""
    now = int(time.time())
    s = _make_long_setup(state_name="cooldown", since_ts=now - 100)
    tick = StateTickContext(last_price=99_100.0, now_sec=now, wall_events=[])
    new = advance_setup_state(s, tick)
    assert new.name == "cooldown"


# ─────────────────────────────────────────────────────────────────────
# P0 修复：fake_break 的 neutral 方向必须按 setup_type 推导风险方向
# ─────────────────────────────────────────────────────────────────────
def _make_fake_break_long_setup(*, state_name="waiting_for_trigger") -> TradeSetupCandidate:
    s = _make_long_setup(state_name=state_name)
    s.setup_type = "fake_break_reclaim_long"
    s.direction = "neutral"
    return s


def _make_fake_break_short_setup(*, state_name="waiting_for_trigger") -> TradeSetupCandidate:
    s = _make_short_setup()
    s.setup_type = "fake_break_reclaim_short"
    s.direction = "neutral"
    s.state = SetupState(name=state_name, since_ts=int(time.time()))
    return s


def test_fake_break_long_hard_stop_invalidates():
    """neutral 方向的 fake_break_reclaim_long 穿硬止损必须 invalidated。"""
    s = _make_fake_break_long_setup(state_name="triggered")
    tick = StateTickContext(last_price=98_600.0, now_sec=int(time.time()),
                            wall_events=[])
    new = advance_setup_state(s, tick)
    assert new.name == "invalidated"


def test_fake_break_short_hard_stop_invalidates():
    s = _make_fake_break_short_setup(state_name="triggered")
    tick = StateTickContext(last_price=99_600.0, now_sec=int(time.time()),
                            wall_events=[])
    new = advance_setup_state(s, tick)
    assert new.name == "invalidated"


def test_fake_break_long_regime_flip_cancels():
    """neutral fake_break_reclaim_long 在 trend_down 下必须 cancelled。"""
    s = _make_fake_break_long_setup(state_name="waiting_for_trigger")
    tick = StateTickContext(
        last_price=99_300.0, now_sec=int(time.time()),
        wall_events=[],
        ctx=BrainContextChips(regime="trend_down"),
    )
    new = advance_setup_state(s, tick)
    assert new.name == "cancelled"


def test_fake_break_long_not_invalidated_above_hard_stop():
    """价格未穿硬止损时保持原状态推进逻辑。"""
    s = _make_fake_break_long_setup(state_name="waiting_for_trigger")
    tick = StateTickContext(last_price=99_100.0, now_sec=int(time.time()),
                            wall_events=[])
    new = advance_setup_state(s, tick)
    assert new.name == "triggered"
