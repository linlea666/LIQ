"""routes_scalp · REST API 路由测试 · 直接 await handler（项目惯例）"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from api import routes_scalp
from models.scalp_signal import (
    FactorBreakdown,
    ScalpSignal,
    StrategyName,
    calc_expiry_ts,
    make_signal_id,
)
from processors.scalp_signal.calibrator import Calibrator
from storage.scalp_signal_store import ScalpSignalStore


def _make_signal(
    *, ts: int, confidence: int = 78, direction: str = "up",
    strategy: StrategyName = StrategyName.A_SWEEP_RECLAIM,
    state: str = "active",
) -> ScalpSignal:
    s = ScalpSignal(
        signal_id=make_signal_id("BTC", strategy, ts, direction),  # type: ignore[arg-type]
        coin="BTC", horizon_min=30,
        direction=direction,  # type: ignore[arg-type]
        strategy=strategy,
        reference_price=78400.0, created_at=ts,
        expiry_ts=calc_expiry_ts(ts, 30),
        confidence=confidence, hit_probability=0.7, regime="range",
        factor_breakdown=FactorBreakdown(),
        state=state,  # type: ignore[arg-type]
    )
    if state in ("expired_won", "expired_lost", "expired_push"):
        s.outcome = (
            "won" if state == "expired_won"
            else "lost" if state == "expired_lost"
            else "push"
        )
        s.settlement_price = 78500.0 if s.outcome == "won" else 78300.0
        s.settled_at = ts + 1800
    return s


@pytest.fixture(autouse=True)
def _reset_components():
    """每个测试前后重置全局组件，避免污染"""
    yield
    routes_scalp._store = None
    routes_scalp._engine = None
    routes_scalp._calibrator = None


@pytest.fixture
def store(tmp_path: Path) -> ScalpSignalStore:
    return ScalpSignalStore(data_dir=str(tmp_path))


@pytest.fixture
def calibrator(store) -> Calibrator:
    return Calibrator(store)


@pytest.fixture
def injected(store, calibrator):
    """注入组件（engine 用 mock）"""
    routes_scalp.set_components(
        store=store, engine=MagicMock(), calibrator=calibrator,
    )
    return store, calibrator


# ════════════════════════════════════════════════════════════════════════════
# 503 守门
# ════════════════════════════════════════════════════════════════════════════

class TestNotReady:
    def test_active_503_when_no_store(self):
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(routes_scalp.list_active_signals())
        assert exc_info.value.status_code == 503

    def test_stats_503_when_no_calibrator(self):
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(routes_scalp.get_stats())
        assert exc_info.value.status_code == 503


# ════════════════════════════════════════════════════════════════════════════
# Active signals
# ════════════════════════════════════════════════════════════════════════════

class TestActiveSignals:
    def test_empty_active(self, injected):
        result = asyncio.run(routes_scalp.list_active_signals())
        assert result["count"] == 0
        assert result["signals"] == []

    def test_returns_active_signals(self, injected):
        store, _ = injected
        s1 = _make_signal(ts=int(time.time()), direction="up")
        s2 = _make_signal(
            ts=int(time.time()) + 1, direction="down",
            strategy=StrategyName.B_CVD_DIVERGENCE,
        )
        store.add_active(s1)
        store.add_active(s2)
        result = asyncio.run(routes_scalp.list_active_signals())
        assert result["count"] == 2
        ids = {sig["signal_id"] for sig in result["signals"]}
        assert ids == {s1.signal_id, s2.signal_id}


# ════════════════════════════════════════════════════════════════════════════
# History signals
# ════════════════════════════════════════════════════════════════════════════

class TestHistorySignals:
    def test_history_returns_archived(self, injected):
        store, _ = injected
        s = _make_signal(ts=int(time.time()) - 1800, state="expired_won")
        store.archive_signal(s)
        result = asyncio.run(routes_scalp.list_history_signals(limit=100))
        assert result["count"] == 1
        assert result["signals"][0]["signal_id"] == s.signal_id
        assert result["signals"][0]["outcome"] == "won"

    def test_filter_by_strategy(self, injected):
        store, _ = injected
        s_a = _make_signal(
            ts=int(time.time()) - 1800, state="expired_won",
            strategy=StrategyName.A_SWEEP_RECLAIM,
        )
        s_b = _make_signal(
            ts=int(time.time()) - 1700, state="expired_lost",
            strategy=StrategyName.B_CVD_DIVERGENCE,
        )
        store.archive_signal(s_a)
        store.archive_signal(s_b)
        result = asyncio.run(routes_scalp.list_history_signals(
            limit=100, strategy=StrategyName.A_SWEEP_RECLAIM,
        ))
        assert result["count"] == 1
        assert result["signals"][0]["strategy"] == "A_sweep_reclaim"


# ════════════════════════════════════════════════════════════════════════════
# Get signal by id
# ════════════════════════════════════════════════════════════════════════════

class TestGetSignal:
    def test_finds_active(self, injected):
        store, _ = injected
        s = _make_signal(ts=int(time.time()))
        store.add_active(s)
        result = asyncio.run(routes_scalp.get_signal(s.signal_id))
        assert result["signal_id"] == s.signal_id
        assert result["state"] == "active"

    def test_finds_in_history(self, injected):
        store, _ = injected
        s = _make_signal(ts=int(time.time()) - 1800, state="expired_won")
        store.archive_signal(s)
        result = asyncio.run(routes_scalp.get_signal(s.signal_id))
        assert result["signal_id"] == s.signal_id
        assert result["outcome"] == "won"

    def test_404_when_not_found(self, injected):
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(routes_scalp.get_signal("not_exist"))
        assert exc_info.value.status_code == 404


# ════════════════════════════════════════════════════════════════════════════
# Cancel signal
# ════════════════════════════════════════════════════════════════════════════

class TestCancelSignal:
    def test_cancel_active_signal_keeps_in_pool(self, injected):
        """P0-4：cancel 后保留在活跃池，等到期 shadow settle 才归档"""
        store, _ = injected
        s = _make_signal(ts=int(time.time()))
        store.add_active(s)
        result = asyncio.run(routes_scalp.cancel_signal(
            s.signal_id, reason="user manual",
        ))
        assert result["ok"] is True
        assert result["signal"]["state"] == "cancelled"
        assert result["signal"]["invalidation_kind"] == "manual"
        # P0-4：cancelled 信号仍在活跃池
        active = store.get_active()
        assert len(active) == 1
        assert active[0].signal_id == s.signal_id
        assert active[0].state == "cancelled"
        # 历史里还没有
        history = store.iter_history()
        assert not any(h.signal_id == s.signal_id for h in history)

    def test_cancel_404_when_not_active(self, injected):
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(routes_scalp.cancel_signal("not_active"))
        assert exc_info.value.status_code == 404


# ════════════════════════════════════════════════════════════════════════════
# Config
# ════════════════════════════════════════════════════════════════════════════

class TestConfig:
    def test_get_config_returns_defaults(self, injected):
        result = asyncio.run(routes_scalp.get_config())
        assert result["coin"] == "BTC"
        assert result["horizon_min"] == 30
        assert result["test_mode"] is True
        # 默认全关
        for sc in result["strategies"].values():
            assert sc["enabled"] is False

    def test_patch_enables_strategy(self, injected):
        from api.routes_scalp import ScalpConfigPatch, StrategyConfigPatch
        patch = ScalpConfigPatch(
            strategies={
                StrategyName.A_SWEEP_RECLAIM: StrategyConfigPatch(
                    enabled=True, confidence_threshold=82,
                ),
            },
        )
        result = asyncio.run(routes_scalp.patch_config(patch))
        a = result["strategies"]["A_sweep_reclaim"]
        assert a["enabled"] is True
        assert a["confidence_threshold"] == 82

    def test_patch_partial_keeps_others(self, injected):
        from api.routes_scalp import ScalpConfigPatch, StrategyConfigPatch
        # 第一次 patch
        asyncio.run(routes_scalp.patch_config(ScalpConfigPatch(
            strategies={
                StrategyName.A_SWEEP_RECLAIM: StrategyConfigPatch(enabled=True),
            },
        )))
        # 第二次只改 cooldown
        asyncio.run(routes_scalp.patch_config(ScalpConfigPatch(
            strategies={
                StrategyName.A_SWEEP_RECLAIM: StrategyConfigPatch(cooldown_min=120),
            },
        )))
        # enabled 应保持 True
        result = asyncio.run(routes_scalp.get_config())
        a = result["strategies"]["A_sweep_reclaim"]
        assert a["enabled"] is True
        assert a["cooldown_min"] == 120

    def test_patch_invalid_horizon_400(self, injected):
        from fastapi import HTTPException
        from api.routes_scalp import ScalpConfigPatch
        # horizon_min=20 不是合法值
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(routes_scalp.patch_config(ScalpConfigPatch(horizon_min=20)))
        # FastAPI 之外的校验：我们写 400 但 Pydantic 已经 ge=10 le=60 会 422
        # 实际由 horizon_min not in (10/30/60) 抛 400
        # 注意 Pydantic ge=10 le=60 只允许 10-60，但 20/40/50 会通过 ge/le 校验
        # 这里 20 通过 ge=10 le=60 → 进入 handler → 抛 400
        assert exc_info.value.status_code == 400

    def test_patch_notification(self, injected):
        from api.routes_scalp import (
            ScalpConfigPatch, NotificationConfigPatch,
        )
        result = asyncio.run(routes_scalp.patch_config(ScalpConfigPatch(
            notification=NotificationConfigPatch(
                email_enabled=False, email_min_confidence=90,
            ),
        )))
        assert result["notification"]["email_enabled"] is False
        assert result["notification"]["email_min_confidence"] == 90


# ════════════════════════════════════════════════════════════════════════════
# Stats / Calibration
# ════════════════════════════════════════════════════════════════════════════

class TestStatsAndCalibration:
    def test_stats_empty(self, injected):
        result = asyncio.run(routes_scalp.get_stats())
        assert result["total_signals"] == 0
        assert result["by_strategy"] == []

    def test_stats_with_history(self, injected):
        store, _ = injected
        for outcome_state in ["expired_won", "expired_won", "expired_lost"]:
            s = _make_signal(
                ts=int(time.time()) - 86400, state=outcome_state,
            )
            store.archive_signal(s)
        result = asyncio.run(routes_scalp.get_stats(recompute=True))
        assert result["total_signals"] == 3
        assert result["total_won"] == 2
        assert result["total_lost"] == 1

    def test_calibration_empty(self, injected):
        result = asyncio.run(routes_scalp.get_calibration())
        assert result["sample_size_total"] == 0
