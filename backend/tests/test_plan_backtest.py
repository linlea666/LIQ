"""D04 · PlanBacktestStore 单元测试

覆盖：
  - 过滤非 long/short 或非 green/yellow plan → 不记录
  - 新 trade 去重（同 coin+direction+entry_mid 60min 内只记一次）
  - tick：触发 → 结算 tp1 / sl / expired
  - get_stats 样本数与 win_rate 计算
  - 持久化 round-trip
"""
from __future__ import annotations

import os
import tempfile
import time

import pytest

from models.execution_plan import (
    ExecutionPlan,
    ExecutionScoreBreakdown,
    SafetyGateResult as EPSafetyGateResult,  # if model differs, fallback
)


# ─── fixtures ──────────────────────────────────────────────────────────

@pytest.fixture
def tmp_store(tmp_path):
    """隔离的 PlanBacktestStore（独立 data_file）"""
    from processors.plan_backtest import PlanBacktestStore

    f = tmp_path / "plan_backtest.json"
    return PlanBacktestStore(data_file=str(f))


def _mk_plan(
    *,
    coin="BTC",
    action="long",
    light="green",
    entry_low=100_000.0,
    entry_high=100_200.0,
    sl=99_000.0,
    tp1=102_000.0,
    tier="A",
    rr=2.0,
):
    from models.execution_plan import (
        ExecutionPlan, ExecutionScoreBreakdown, SafetyGateResult,
    )
    return ExecutionPlan(
        coin=coin,
        ts=int(time.time()),
        current_price=(entry_low + entry_high) / 2,
        regime="trend_up",
        regime_confidence=0.8,
        execution_score=70.0,
        traffic_light=light,
        headline="test",
        one_liner="test plan",
        action=action,
        direction="bullish" if action == "long" else "bearish",
        tier_hint=tier,
        entry_zone_low=entry_low,
        entry_zone_high=entry_high,
        stop_loss=sl,
        tp1=tp1,
        tp2=None,
        rr_ratio=rr,
        position_size_pct=30.0,
        expires_at=int(time.time()) + 3600,
        breakdown=ExecutionScoreBreakdown(
            base=50, tier_bonus=10, confidence_bonus=5, regime_alignment=5,
            corroboration_bonus=0, cascade_penalty=0, rr_bonus=5, backtest_bonus=0,
            event_factor=0, geo_risk_factor=0, safety_gate_delta=0, final_score=70,
        ),
        safety_gates=SafetyGateResult(
            g1_extreme_vol="pass",
            g2_macro_event="pass",
            g3_liq_chaos="pass",
            g4_api_degrade="pass",
            g5_blackswan="pass",
            triggered=False,
            block_reason="",
            warnings=[],
        ),
        corroborating_sources=["tracker_v2.bounced"],
        contributing_signals=[],
        historical_win_rate=None,
        historical_sample_size=0,
    )


# ─── 过滤逻辑 ──────────────────────────────────────────────────────────

class TestFilter:
    def test_skip_wait_action(self, tmp_store):
        plan = _mk_plan(action="wait")
        tmp_store.track(plan, current_price=100_100)
        assert tmp_store.snapshot_dict() == {}

    def test_skip_red_light(self, tmp_store):
        plan = _mk_plan(light="red")
        tmp_store.track(plan, current_price=100_100)
        assert tmp_store.snapshot_dict() == {}

    def test_skip_missing_entry_zone(self, tmp_store):
        plan = _mk_plan()
        plan.entry_zone_low = None
        tmp_store.track(plan, current_price=100_100)
        assert tmp_store.snapshot_dict() == {}


# ─── 去重 ─────────────────────────────────────────────────────────────

class TestDedup:
    def test_same_entry_only_once(self, tmp_store):
        plan = _mk_plan()
        for _ in range(5):
            tmp_store.track(plan, current_price=100_500)
        trades = tmp_store.snapshot_dict()["BTC"]
        assert len(trades) == 1

    def test_different_direction_two_trades(self, tmp_store):
        plan_long = _mk_plan(action="long", entry_low=100_000, entry_high=100_200)
        plan_short = _mk_plan(
            action="short",
            entry_low=100_000, entry_high=100_200,
            sl=101_000, tp1=98_000,
        )
        tmp_store.track(plan_long, current_price=100_100)
        tmp_store.track(plan_short, current_price=100_100)
        trades = tmp_store.snapshot_dict()["BTC"]
        assert len(trades) == 2


# ─── tick / 触发 / 结算 ────────────────────────────────────────────────

class TestTick:
    def test_long_triggers_then_tp1(self, tmp_store):
        plan = _mk_plan(action="long", entry_low=100_000, entry_high=100_200,
                        sl=99_000, tp1=102_000)
        tmp_store.track(plan, current_price=100_100)        # 立即触发
        tmp_store.track(plan, current_price=102_300)        # 命中 tp1
        t = tmp_store.snapshot_dict()["BTC"][0]
        assert t["outcome"] == "tp1"
        assert t["triggered_ts"] is not None

    def test_long_triggers_then_sl(self, tmp_store):
        plan = _mk_plan(action="long", entry_low=100_000, entry_high=100_200,
                        sl=99_000, tp1=102_000)
        tmp_store.track(plan, current_price=100_100)
        tmp_store.track(plan, current_price=98_500)
        t = tmp_store.snapshot_dict()["BTC"][0]
        assert t["outcome"] == "sl"

    def test_short_tp1(self, tmp_store):
        plan = _mk_plan(action="short", entry_low=100_000, entry_high=100_200,
                        sl=101_000, tp1=98_000)
        tmp_store.track(plan, current_price=100_100)
        tmp_store.track(plan, current_price=97_500)
        t = tmp_store.snapshot_dict()["BTC"][0]
        assert t["outcome"] == "tp1"


# ─── stats ─────────────────────────────────────────────────────────────

class TestStats:
    def test_win_rate_calculation(self, tmp_store):
        # 2 个 plan：一个 tp1，一个 sl
        p1 = _mk_plan(action="long", entry_low=100_000, entry_high=100_100, sl=99_000, tp1=102_000)
        tmp_store.track(p1, current_price=100_050)
        tmp_store.track(p1, current_price=102_500)

        p2 = _mk_plan(action="long", entry_low=90_000, entry_high=90_100, sl=89_000, tp1=92_000)
        tmp_store.track(p2, current_price=90_050)
        tmp_store.track(p2, current_price=88_500)

        stats = tmp_store.get_stats("BTC")
        assert stats.total_signals == 2
        assert stats.tp1_hit == 1
        assert stats.sl_hit == 1
        assert abs(stats.win_rate - 0.5) < 1e-6


# ─── 持久化 ─────────────────────────────────────────────────────────────

class TestPersistence:
    def test_roundtrip(self, tmp_path):
        from processors.plan_backtest import PlanBacktestStore
        f = tmp_path / "pb.json"

        s1 = PlanBacktestStore(data_file=str(f))
        plan = _mk_plan()
        s1.track(plan, current_price=100_050)
        s1._persist_to_disk()
        assert f.exists()

        s2 = PlanBacktestStore(data_file=str(f))
        trades = s2.snapshot_dict()["BTC"]
        assert len(trades) == 1
        assert trades[0]["direction"] == "long"
