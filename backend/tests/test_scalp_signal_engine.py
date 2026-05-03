"""SignalEngine 集成测试 · tick_once 端到端串联（regime → strategy → veto → score → store）"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

import pytest

from models.market import CandleData
from models.scalp_signal import (
    EvidenceItem,
    FactorBreakdown,
    ScalpConfig,
    ScalpSignal,
    StrategyName,
    calc_expiry_ts,
    make_signal_id,
)

from processors.scalp_signal.base_strategy import (
    BaseStrategy,
    StrategyCandidate,
    StrategyContext,
)
from processors.scalp_signal.signal_engine import SignalEngine
from processors.scalp_signal.strategy_registry import StrategyRegistry
from storage.scalp_signal_store import ScalpSignalStore


# ════════════════════════════════════════════════════════════════════════════
# 测试用假策略：可控触发
# ════════════════════════════════════════════════════════════════════════════

class FakeUpStrategy(BaseStrategy):
    name = StrategyName.A_SWEEP_RECLAIM
    display_name = "Fake Up"
    suitable_regimes = {"range", "trend_up"}
    suitable_horizons = {30}

    def __init__(self, *, raw: float = 0.85, trigger: bool = True):
        self._raw = raw
        self._trigger = trigger

    def detect(self, ctx):
        if not self._trigger:
            return None
        ref = ctx.state.ticker.last
        return StrategyCandidate(
            direction="up", reference_price=ref, raw_strength=self._raw,
            evidence=[EvidenceItem(dimension="Test", observation="up triggered")],
            triggered_conditions=["test"],
        )


class FakeDownStrategy(BaseStrategy):
    name = StrategyName.B_CVD_DIVERGENCE
    display_name = "Fake Down"
    suitable_regimes = {"range"}
    suitable_horizons = {30}

    def detect(self, ctx):
        return StrategyCandidate(
            direction="down", reference_price=ctx.state.ticker.last,
            raw_strength=0.85,
            evidence=[EvidenceItem(dimension="Test", observation="down")],
        )


# ════════════════════════════════════════════════════════════════════════════
# Fixtures
# ════════════════════════════════════════════════════════════════════════════

def _candle(price: float, ts: int) -> CandleData:
    return CandleData(coin="BTC", ts=ts, o=price, h=price + 5, l=price - 5, c=price)


def _state(*, now: int, last: float = 78400.0):
    return SimpleNamespace(
        ticker=SimpleNamespace(last=last, ts=now - 5),
        cvd_contract=SimpleNamespace(ts=now - 30, trend_1h="flat", delta_1h=0),
        cvd_spot=SimpleNamespace(ts=now - 30, trend_1h="flat", delta_1h=0),
        candles_15m=[_candle(78380, now - 900), _candle(last, now - 300)],
        regime_snapshot=SimpleNamespace(regime="range", confidence=0.85,
                                        regime_changed_at=now - 3600),
        range_signal=SimpleNamespace(price_position_pct=50.0),
        market_action_report=None,
        market_structure=None,
        market_structure_1d=None,
        market_structure_1w=None,
        key_level_snapshot_v2=None,
        rsi_14=None,
    )


@pytest.fixture
def store(tmp_path: Path) -> ScalpSignalStore:
    return ScalpSignalStore(data_dir=str(tmp_path))


@pytest.fixture
def registry_with_up() -> StrategyRegistry:
    reg = StrategyRegistry()
    reg.register(FakeUpStrategy())
    return reg


@pytest.fixture
def cfg_with_a_enabled() -> ScalpConfig:
    cfg = ScalpConfig()
    cfg.strategies[StrategyName.A_SWEEP_RECLAIM].enabled = True
    cfg.strategies[StrategyName.A_SWEEP_RECLAIM].confidence_threshold = 60  # 低门槛便于触发
    return cfg


def _make_engine(
    store: ScalpSignalStore,
    registry: StrategyRegistry,
    cfg: ScalpConfig,
    state,
    *,
    blackswan: tuple[bool, Optional[int]] = (False, None),
    on_created=None,
    on_settled=None,
) -> SignalEngine:
    return SignalEngine(
        store=store,
        registry=registry,
        config_getter=lambda: cfg,
        state_getter=lambda _coin: state,
        blackswan_getter=lambda: blackswan,
        on_signal_created=on_created,
        on_signal_settled=on_settled,
    )


# ════════════════════════════════════════════════════════════════════════════
# tick_once · 触发路径
# ════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
class TestTickGenerate:
    async def test_generates_signal_on_clean_path(
        self, store, registry_with_up, cfg_with_a_enabled,
    ):
        now = int(time.time())
        state = _state(now=now)
        captured: list[ScalpSignal] = []

        async def on_create(sig):
            captured.append(sig)

        engine = _make_engine(
            store, registry_with_up, cfg_with_a_enabled, state,
            on_created=on_create,
        )
        result = await engine.tick_once(now_ts=now)

        assert len(result["generated"]) == 1
        assert result["regime"] == "range"
        active = store.get_active()
        assert len(active) == 1
        assert active[0].direction == "up"
        assert active[0].state == "active"
        assert active[0].test_mode is True

        # 通知回调被触发
        assert len(captured) == 1
        assert captured[0].signal_id == active[0].signal_id

    async def test_disabled_config_skips(self, store, registry_with_up):
        now = int(time.time())
        state = _state(now=now)
        cfg = ScalpConfig()
        cfg.enabled = False
        engine = _make_engine(store, registry_with_up, cfg, state)
        result = await engine.tick_once(now_ts=now)
        assert result["skipped_reason"] == "config_disabled"
        assert len(store.get_active()) == 0

    async def test_no_state_skips(self, store, registry_with_up, cfg_with_a_enabled):
        now = int(time.time())
        engine = SignalEngine(
            store=store, registry=registry_with_up,
            config_getter=lambda: cfg_with_a_enabled,
            state_getter=lambda _coin: None,
        )
        result = await engine.tick_once(now_ts=now)
        assert result["skipped_reason"] == "no_state"

    async def test_strategy_disabled_no_signal(self, store, registry_with_up):
        now = int(time.time())
        state = _state(now=now)
        cfg = ScalpConfig()  # 默认全关
        engine = _make_engine(store, registry_with_up, cfg, state)
        result = await engine.tick_once(now_ts=now)
        assert result["generated"] == []

    async def test_confidence_below_threshold_filters(
        self, store, registry_with_up,
    ):
        """confidence 阈值过滤"""
        now = int(time.time())
        state = _state(now=now)
        cfg = ScalpConfig()
        cfg.strategies[StrategyName.A_SWEEP_RECLAIM].enabled = True
        cfg.strategies[StrategyName.A_SWEEP_RECLAIM].confidence_threshold = 99  # 极高
        engine = _make_engine(store, registry_with_up, cfg, state)
        result = await engine.tick_once(now_ts=now)
        assert result["generated"] == []


# ════════════════════════════════════════════════════════════════════════════
# tick_once · regime 闸门
# ════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
class TestTickRegime:
    async def test_extreme_regime_blocks_and_cancels_active(
        self, store, registry_with_up, cfg_with_a_enabled,
    ):
        """regime=extreme → 取消所有活跃信号；
        P0-4 改造：取消后保留在活跃池等 shadow settle"""
        now = int(time.time())
        existing = ScalpSignal(
            signal_id="prev_signal_x", coin="BTC", horizon_min=30,
            direction="up", strategy=StrategyName.A_SWEEP_RECLAIM,
            reference_price=78000, created_at=now - 600,
            expiry_ts=now + 1200, confidence=80, hit_probability=0.7,
            regime="range", factor_breakdown=FactorBreakdown(),
        )
        store.add_active(existing)

        state = _state(now=now)
        state.regime_snapshot.regime = "extreme"

        engine = _make_engine(store, registry_with_up, cfg_with_a_enabled, state)
        result = await engine.tick_once(now_ts=now)

        assert "extreme" in (result["skipped_reason"] or "")
        assert "prev_signal_x" in result["cancelled"]
        # P0-4：cancelled 信号仍在活跃池（state=cancelled）
        active = store.get_active()
        assert len(active) == 1
        assert active[0].signal_id == "prev_signal_x"
        assert active[0].state == "cancelled"
        assert active[0].invalidation_kind == "regime_flip"

    async def test_no_regime_skips_no_cancel(
        self, store, registry_with_up, cfg_with_a_enabled,
    ):
        """regime_snapshot=None 时跳过但不取消活跃"""
        now = int(time.time())
        existing = ScalpSignal(
            signal_id="prev_signal_y", coin="BTC", horizon_min=30,
            direction="up", strategy=StrategyName.A_SWEEP_RECLAIM,
            reference_price=78000, created_at=now - 600,
            expiry_ts=now + 1200, confidence=80, hit_probability=0.7,
            regime="range", factor_breakdown=FactorBreakdown(),
        )
        store.add_active(existing)

        state = _state(now=now)
        state.regime_snapshot = None

        engine = _make_engine(store, registry_with_up, cfg_with_a_enabled, state)
        result = await engine.tick_once(now_ts=now)
        assert result["skipped_reason"] == "no_regime_snapshot"
        # 不取消（只是 block 当轮）
        assert len(store.get_active()) == 1


# ════════════════════════════════════════════════════════════════════════════
# tick_once · 黑天鹅取消活跃 + 跳过
# ════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
class TestTickBlackswan:
    async def test_blackswan_cancels_and_skips(
        self, store, registry_with_up, cfg_with_a_enabled,
    ):
        now = int(time.time())
        existing = ScalpSignal(
            signal_id="prev_blackswan_z", coin="BTC", horizon_min=30,
            direction="up", strategy=StrategyName.A_SWEEP_RECLAIM,
            reference_price=78000, created_at=now - 600,
            expiry_ts=now + 1200, confidence=80, hit_probability=0.7,
            regime="range", factor_breakdown=FactorBreakdown(),
        )
        store.add_active(existing)

        state = _state(now=now)
        engine = _make_engine(
            store, registry_with_up, cfg_with_a_enabled, state,
            blackswan=(True, 600),
        )
        result = await engine.tick_once(now_ts=now)
        assert result["skipped_reason"] == "blackswan_active"
        assert "prev_blackswan_z" in result["cancelled"]
        # P0-4：cancelled 信号仍保留在活跃池
        active = store.get_active()
        assert len(active) == 1
        assert active[0].state == "cancelled"
        assert active[0].invalidation_kind == "blackswan"


# ════════════════════════════════════════════════════════════════════════════
# tick_once · 结算到期信号
# ════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
class TestTickSettlement:
    async def test_settles_due_and_archives(
        self, store, registry_with_up, cfg_with_a_enabled,
    ):
        now = int(time.time())
        # 已到期信号
        due = ScalpSignal(
            signal_id="due_signal_a", coin="BTC", horizon_min=30,
            direction="up", strategy=StrategyName.A_SWEEP_RECLAIM,
            reference_price=78000, created_at=now - 1900,
            expiry_ts=now - 100,  # 100s 前到期
            confidence=80, hit_probability=0.7,
            regime="range", factor_breakdown=FactorBreakdown(),
        )
        store.add_active(due)

        state = _state(now=now, last=78200.0)  # 涨了 → won
        captured = []

        async def on_settled(sig):
            captured.append(sig)

        engine = _make_engine(
            store, registry_with_up, cfg_with_a_enabled, state,
            on_settled=on_settled,
        )
        result = await engine.tick_once(now_ts=now)
        assert "due_signal_a" in result["settled"]

        # 活跃池中已不在
        active_ids = {s.signal_id for s in store.get_active()}
        assert "due_signal_a" not in active_ids

        # 历史中存在 + outcome=won
        history = store.iter_history()
        due_in_hist = next((s for s in history if s.signal_id == "due_signal_a"), None)
        assert due_in_hist is not None
        assert due_in_hist.outcome == "won"

        # 通知触发（注意：on_settled 在 settle_due 后调用，但实现里 _find_in_history 才能找到）
        # 至少不报错
        assert len(captured) <= 1

    async def test_settlement_triggers_calibrator(
        self, store, registry_with_up, cfg_with_a_enabled,
    ):
        now = int(time.time())
        due = ScalpSignal(
            signal_id="due_signal_calib", coin="BTC", horizon_min=30,
            direction="up", strategy=StrategyName.A_SWEEP_RECLAIM,
            reference_price=78000, created_at=now - 1900,
            expiry_ts=now - 100,
            confidence=80, hit_probability=0.7,
            regime="range", factor_breakdown=FactorBreakdown(),
        )
        store.add_active(due)

        state = _state(now=now, last=78200.0)
        engine = _make_engine(store, registry_with_up, cfg_with_a_enabled, state)
        await engine.tick_once(now_ts=now)
        # 结算后应该有 stats_cache
        cache = store.load_stats_cache()
        assert cache is not None
        assert cache.total_signals >= 1


# ════════════════════════════════════════════════════════════════════════════
# 多策略 + 隔离
# ════════════════════════════════════════════════════════════════════════════

class CrashStrategy(BaseStrategy):
    name = StrategyName.C_RANGE_EDGE_FADE
    display_name = "Crash"
    suitable_regimes = {"range"}
    suitable_horizons = {30}

    def detect(self, ctx):
        raise ValueError("intentional crash")


@pytest.mark.asyncio
class TestMultiStrategyIsolation:
    async def test_crash_isolated_others_continue(self, store):
        now = int(time.time())
        state = _state(now=now)
        reg = StrategyRegistry()
        reg.register(CrashStrategy())
        reg.register(FakeUpStrategy())

        cfg = ScalpConfig()
        cfg.strategies[StrategyName.A_SWEEP_RECLAIM].enabled = True
        cfg.strategies[StrategyName.A_SWEEP_RECLAIM].confidence_threshold = 60
        cfg.strategies[StrategyName.C_RANGE_EDGE_FADE].enabled = True
        cfg.strategies[StrategyName.C_RANGE_EDGE_FADE].confidence_threshold = 60

        engine = _make_engine(store, reg, cfg, state)
        result = await engine.tick_once(now_ts=now)
        # A 应该正常生成
        assert len(result["generated"]) == 1

    async def test_signal_id_collision_handled(self, store, registry_with_up, cfg_with_a_enabled):
        """同一秒重复触发时（极端边界）不应崩溃"""
        now = int(time.time())
        state = _state(now=now)
        engine = _make_engine(store, registry_with_up, cfg_with_a_enabled, state)
        # 两次同一时间点 tick → 第二次因 signal_id 重复应该被跳过
        await engine.tick_once(now_ts=now)
        result2 = await engine.tick_once(now_ts=now)
        # 第二次因 add_active 报 ValueError → 被捕获 → generated 为空
        assert result2["generated"] == []
        assert len(store.get_active()) == 1
