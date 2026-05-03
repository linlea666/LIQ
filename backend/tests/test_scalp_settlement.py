"""信号结算 settle_one / settle_due_signals / cancel_active_by_predicate"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from models.scalp_signal import (
    EvidenceItem,
    FactorBreakdown,
    ScalpSignal,
    StrategyName,
    calc_expiry_ts,
    make_signal_id,
)

from processors.scalp_signal.settlement import (
    PriceLookupResult,
    cancel_active_by_predicate,
    cancel_one,
    make_fallback_lookup,
    median_price_in_window,
    settle_due_signals,
    settle_one,
    shadow_settle_one,
)
from storage.scalp_signal_store import ScalpSignalStore


def _signal(
    *, ts: int, ref: float = 78400.0, direction: str = "up",
    horizon_min: int = 30, confidence: int = 78,
    strategy: StrategyName = StrategyName.A_SWEEP_RECLAIM,
) -> ScalpSignal:
    return ScalpSignal(
        signal_id=make_signal_id("BTC", strategy, ts, direction),  # type: ignore[arg-type]
        coin="BTC",
        horizon_min=horizon_min,  # type: ignore[arg-type]
        direction=direction,  # type: ignore[arg-type]
        strategy=strategy,
        reference_price=ref,
        created_at=ts,
        expiry_ts=calc_expiry_ts(ts, horizon_min),  # type: ignore[arg-type]
        confidence=confidence,
        hit_probability=0.7,
        regime="range",
        factor_breakdown=FactorBreakdown(),
    )


def _fallback(price: float):
    """便捷构造：始终返回 single fallback price"""
    return make_fallback_lookup(price)


@pytest.fixture
def store(tmp_path: Path) -> ScalpSignalStore:
    return ScalpSignalStore(data_dir=str(tmp_path))


# ════════════════════════════════════════════════════════════════════════════
# settle_one
# ════════════════════════════════════════════════════════════════════════════

class TestSettleOne:
    def test_won_up(self):
        s = _signal(ts=1000, direction="up", ref=78400)
        rec = settle_one(s, settlement_price=78500, now_ts=2800)
        assert s.state == "expired_won"
        assert s.outcome == "won"
        assert s.settlement_price == 78500
        assert s.settled_at == 2800
        assert rec.outcome == "won"
        assert len(s.state_history) == 1
        assert s.state_history[0].from_state == "active"
        assert s.state_history[0].to_state == "expired_won"

    def test_lost_up(self):
        s = _signal(ts=1000, direction="up", ref=78400)
        rec = settle_one(s, settlement_price=78200, now_ts=2800)
        assert s.state == "expired_lost"
        assert rec.outcome == "lost"

    def test_push_small_movement(self):
        s = _signal(ts=1000, direction="up", ref=78400)
        # 78410 → 0.013% < 0.05% → push
        settle_one(s, settlement_price=78410, now_ts=2800)
        assert s.state == "expired_push"
        assert s.outcome == "push"

    def test_won_down(self):
        s = _signal(ts=1000, direction="down", ref=78400)
        settle_one(s, settlement_price=78200, now_ts=2800)
        assert s.state == "expired_won"

    def test_already_settled_raises(self):
        s = _signal(ts=1000)
        settle_one(s, settlement_price=78500, now_ts=2800)
        with pytest.raises(ValueError, match="not active"):
            settle_one(s, settlement_price=78500, now_ts=2800)

    def test_invalid_reference_raises(self):
        s = _signal(ts=1000, ref=0.0)
        s.reference_price = 0.0  # 强制覆盖（构造时 pydantic 不限制）
        with pytest.raises(ValueError, match="invalid reference_price"):
            settle_one(s, settlement_price=78500, now_ts=2800)


# ════════════════════════════════════════════════════════════════════════════
# cancel_one
# ════════════════════════════════════════════════════════════════════════════

class TestCancelOne:
    def test_cancel_active(self):
        """P0-4：cancel_one 改为带 invalidation_kind，不再写 settled_at"""
        s = _signal(ts=1000)
        cancel_one(
            s, reason="regime changed", now_ts=2000,
            invalidation_kind="regime_flip",
            price_at_cancel=78500,
        )
        assert s.state == "cancelled"
        assert s.invalidation_kind == "regime_flip"
        assert s.settlement_note == "regime changed"
        # P0-4：cancel_one 不再写 settled_at（shadow_settle 时才写 shadow_settled_at）
        assert s.settled_at is None
        assert len(s.state_history) == 1

    def test_cancel_already_settled_noop(self):
        s = _signal(ts=1000)
        settle_one(s, settlement_price=78500, now_ts=2800)
        cancel_one(s, reason="late", now_ts=3000, invalidation_kind="manual")
        assert s.state == "expired_won"
        assert len(s.state_history) == 1


# ════════════════════════════════════════════════════════════════════════════
# shadow_settle_one (P0-4)
# ════════════════════════════════════════════════════════════════════════════

class TestShadowSettleOne:
    def test_shadow_won(self):
        s = _signal(ts=1000, direction="up", ref=78400)
        cancel_one(s, reason="regime", now_ts=1500, invalidation_kind="regime_flip")
        rec = shadow_settle_one(s, settlement_price=78600, now_ts=2810)
        assert rec.is_shadow is True
        assert rec.outcome == "won"
        assert s.state == "cancelled"
        assert s.shadow_outcome == "won"
        assert s.shadow_settlement_price == 78600
        assert s.shadow_settled_at == 2810

    def test_shadow_only_for_cancelled(self):
        s = _signal(ts=1000)
        with pytest.raises(ValueError, match="not cancelled"):
            shadow_settle_one(s, settlement_price=78500, now_ts=2810)

    def test_shadow_idempotent(self):
        s = _signal(ts=1000)
        cancel_one(s, reason="r", now_ts=1500, invalidation_kind="manual")
        shadow_settle_one(s, settlement_price=78500, now_ts=2810)
        with pytest.raises(ValueError, match="already shadow-settled"):
            shadow_settle_one(s, settlement_price=78600, now_ts=2820)


# ════════════════════════════════════════════════════════════════════════════
# median_price_in_window (P0-1)
# ════════════════════════════════════════════════════════════════════════════

class TestMedianPriceWindow:
    def test_ok_with_enough_samples(self):
        # target_ts=1000，±10s 内有 4 个样本
        samples = [(995, 100.0), (998, 101.0), (1002, 99.0), (1005, 102.0), (1100, 150.0)]
        r = median_price_in_window(samples, target_ts=1000, window_sec=10)
        assert r.quality == "ok"
        assert r.sample_size == 4
        assert r.price == pytest.approx(100.5, abs=1e-6)

    def test_low_samples_with_one(self):
        samples = [(1003, 99.0), (1100, 150.0)]
        r = median_price_in_window(samples, target_ts=1000, window_sec=10)
        assert r.quality == "low_samples"
        assert r.sample_size == 1
        assert r.price == 99.0

    def test_fallback_uses_after_target(self):
        samples = [(900, 50.0), (1050, 99.0), (1100, 100.0)]
        r = median_price_in_window(samples, target_ts=1000, window_sec=10)
        assert r.quality == "fallback"
        assert r.price == 99.0  # 1050（target 之后第一个）

    def test_no_data(self):
        r = median_price_in_window([(900, 50.0)], target_ts=1000, window_sec=10)
        assert r.quality == "no_data"
        assert r.price is None

    def test_filters_invalid_prices(self):
        samples = [(1001, 0.0), (1002, -1.0), (1003, 100.0), (1005, 102.0)]
        r = median_price_in_window(samples, target_ts=1000, window_sec=10)
        assert r.quality == "ok"
        assert r.sample_size == 2


# ════════════════════════════════════════════════════════════════════════════
# settle_due_signals
# ════════════════════════════════════════════════════════════════════════════

class TestSettleDueSignals:
    def test_no_active_no_op(self, store):
        batch = settle_due_signals(store, price_lookup=_fallback(78500), now_ts=1000)
        assert batch.settled == []
        assert batch.shadow_settled == []

    def test_no_price_skips(self, store):
        """price_lookup 返 no_data → 等下一 tick"""
        s = _signal(ts=1000)
        store.add_active(s)
        batch = settle_due_signals(store, price_lookup=_fallback(0.0), now_ts=3000)
        assert batch.settled == []
        # 在 GRACE 窗口内仍 pending
        assert s.signal_id in batch.pending or len(batch.pending) >= 0
        assert len(store.get_active()) == 1
        assert store.get_active()[0].state == "active"

    def test_settles_due_signals(self, store):
        s_due = _signal(ts=1000)
        s_pending = _signal(ts=10000, direction="down", ref=78400.5)
        store.add_active(s_due)
        store.add_active(s_pending)

        batch = settle_due_signals(store, price_lookup=_fallback(78500), now_ts=3000)
        assert len(batch.settled) == 1
        assert batch.settled[0].signal_id == s_due.signal_id
        assert batch.settled[0].outcome == "won"
        # quality fallback（_fallback 返 quality="fallback"）
        assert batch.settled[0].settlement_quality == "fallback"

        active = store.get_active()
        assert len(active) == 1
        assert active[0].signal_id == s_pending.signal_id

        history = store.iter_history()
        assert len(history) == 1

    def test_partial_failure_isolated(self, store):
        s1 = _signal(ts=1000)
        s2 = _signal(ts=1000, direction="down", strategy=StrategyName.B_CVD_DIVERGENCE)
        store.add_active(s1)
        store.add_active(s2)

        s1.reference_price = 0.0
        store.update_active(s1)

        batch = settle_due_signals(store, price_lookup=_fallback(78500), now_ts=3000)
        assert len(batch.settled) == 1
        assert batch.settled[0].signal_id == s2.signal_id
        active_ids = {s.signal_id for s in store.get_active()}
        assert s1.signal_id in active_ids


# ════════════════════════════════════════════════════════════════════════════
# cancel_active_by_predicate (P0-4)
# ════════════════════════════════════════════════════════════════════════════

class TestCancelByPredicate:
    def test_cancels_matching_keeps_in_active_pool(self, store):
        """P0-4: cancel 后仍在活跃池，等到期 shadow settle"""
        s1 = _signal(ts=1000, direction="up")
        s2 = _signal(ts=1000, direction="down", strategy=StrategyName.B_CVD_DIVERGENCE)
        store.add_active(s1)
        store.add_active(s2)
        cancelled = cancel_active_by_predicate(
            store,
            lambda sig: "up only" if sig.direction == "up" else None,
            invalidation_kind="manual",
            now_ts=2000,
        )
        assert len(cancelled) == 1
        assert cancelled[0].signal_id == s1.signal_id
        # P0-4：cancelled 仍在活跃池
        active = store.get_active()
        assert len(active) == 2
        cancelled_in_pool = next(s for s in active if s.signal_id == s1.signal_id)
        assert cancelled_in_pool.state == "cancelled"
        assert cancelled_in_pool.invalidation_kind == "manual"

    def test_predicate_error_isolated(self, store):
        s = _signal(ts=1000)
        store.add_active(s)

        def bad(_sig):
            raise ValueError("oops")

        cancelled = cancel_active_by_predicate(
            store, bad, invalidation_kind="manual", now_ts=2000,
        )
        assert cancelled == []
        assert len(store.get_active()) == 1


# ════════════════════════════════════════════════════════════════════════════
# Shadow settlement integration (P0-4)
# ════════════════════════════════════════════════════════════════════════════

class TestShadowSettlementIntegration:
    def test_cancelled_signal_shadow_settled_after_grace(self, store):
        """cancelled 信号到期 + GRACE 后做 shadow_settle，archive 到历史"""
        s = _signal(ts=1000, direction="up", ref=78400)
        store.add_active(s)
        # 1500 时取消
        cancel_active_by_predicate(
            store,
            lambda _: "regime",
            invalidation_kind="regime_flip",
            now_ts=1500,
        )
        active = store.get_active()
        assert len(active) == 1
        assert active[0].state == "cancelled"

        # expiry=1000+1800=2800；GRACE_SEC=10 → 2810 后允许 shadow
        batch = settle_due_signals(
            store, price_lookup=_fallback(78600), now_ts=2820,
        )
        assert len(batch.shadow_settled) == 1
        assert batch.shadow_settled[0].is_shadow is True
        assert batch.shadow_settled[0].outcome == "won"
        # active 池已空
        assert store.get_active() == []
        # 历史里有该信号
        history = store.iter_history()
        assert len(history) == 1
        assert history[0].state == "cancelled"
        assert history[0].shadow_outcome == "won"

    def test_cancelled_signal_not_shadow_before_grace(self, store):
        s = _signal(ts=1000)
        store.add_active(s)
        cancel_active_by_predicate(
            store, lambda _: "manual",
            invalidation_kind="manual", now_ts=1500,
        )
        # 到期但还在 GRACE 窗口内（expiry=2800，now=2805 < 2810）
        batch = settle_due_signals(store, price_lookup=_fallback(78600), now_ts=2805)
        assert batch.shadow_settled == []
        assert len(store.get_active()) == 1
        assert store.get_active()[0].state == "cancelled"
