"""短线预测合约信号 · REST API + Socket.IO 推送

端点：
  GET    /api/scalp/signals/active             当前活跃信号列表
  GET    /api/scalp/signals/history            历史结算（带 limit/strategy/coin 过滤）
  GET    /api/scalp/signals/{signal_id}        单信号详情（活跃 + 历史合并查找）
  GET    /api/scalp/config                     当前配置
  PATCH  /api/scalp/config                     更新配置（运行时热更新）
  GET    /api/scalp/stats                      策略统计快照
  GET    /api/scalp/calibration                calibration 曲线数据
  POST   /api/scalp/signals/{signal_id}/cancel 手动取消活跃信号

Socket.IO 推送（沿用 api/ws.py 的 sio 单例）：
  scalp_signal_created   新信号生成时广播（payload = ScalpSignal.model_dump）
  scalp_signal_settled   信号结算时广播
  scalp_signal_cancelled 信号被取消时广播

注入方式（参考其他 routes 模块）：
  main.py 启动时调用 set_components(store, engine, calibrator)
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

from fastapi import APIRouter, Body, HTTPException, Query
from pydantic import BaseModel, Field

from models.scalp_signal import (
    GlobalStats,
    CalibrationCurve,
    ScalpConfig,
    ScalpSignal,
    StrategyName,
)

from processors.scalp_signal.calibrator import Calibrator
from processors.scalp_signal.settlement import cancel_one
from processors.scalp_signal.signal_engine import SignalEngine
from storage.scalp_signal_store import ScalpSignalStore

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/scalp", tags=["scalp"])


# ── 全局依赖（main.py 启动时注入）──────────────────────────

_store: Optional[ScalpSignalStore] = None
_engine: Optional[SignalEngine] = None
_calibrator: Optional[Calibrator] = None


def set_components(
    *,
    store: ScalpSignalStore,
    engine: SignalEngine,
    calibrator: Calibrator,
) -> None:
    global _store, _engine, _calibrator
    _store = store
    _engine = engine
    _calibrator = calibrator
    logger.info("scalp routes components injected")


def _require_store() -> ScalpSignalStore:
    if _store is None:
        raise HTTPException(status_code=503, detail="scalp engine not ready")
    return _store


def _require_calibrator() -> Calibrator:
    if _calibrator is None:
        raise HTTPException(status_code=503, detail="scalp calibrator not ready")
    return _calibrator


# ── PATCH /api/scalp/config 的请求 schema ───────────────────

class StrategyConfigPatch(BaseModel):
    """单策略配置补丁（所有字段可选，None 表示不修改）"""
    enabled: Optional[bool] = None
    confidence_threshold: Optional[int] = Field(default=None, ge=50, le=100)
    cooldown_min: Optional[int] = Field(default=None, ge=1, le=600)
    notes: Optional[str] = None


class NotificationConfigPatch(BaseModel):
    browser_enabled: Optional[bool] = None
    browser_min_confidence: Optional[int] = Field(default=None, ge=0, le=100)
    email_enabled: Optional[bool] = None
    email_min_confidence: Optional[int] = Field(default=None, ge=0, le=100)


class ScalpConfigPatch(BaseModel):
    """整体配置补丁"""
    enabled: Optional[bool] = None
    coin: Optional[str] = None
    horizon_min: Optional[int] = Field(default=None, ge=10, le=60)
    strategies: Optional[dict[StrategyName, StrategyConfigPatch]] = None
    notification: Optional[NotificationConfigPatch] = None


# ════════════════════════════════════════════════════════════════════════════
# Signals
# ════════════════════════════════════════════════════════════════════════════

@router.get("/signals/active")
async def list_active_signals() -> dict:
    """当前活跃（未到期）信号列表"""
    store = _require_store()
    signals = store.get_active()
    return {
        "count": len(signals),
        "signals": [s.model_dump(mode="json") for s in signals],
        "ts": int(time.time()),
    }


@router.get("/signals/history")
async def list_history_signals(
    limit: int = Query(default=100, ge=1, le=1000),
    strategy: Optional[StrategyName] = None,
    coin: Optional[str] = None,
    horizon_min: Optional[int] = None,
    since_ts: Optional[int] = None,
) -> dict:
    """历史结算信号 · 倒序（最新在前）

    - strategy/coin/horizon_min: 过滤条件（horizon_min 仅允许 10/30/60）
    - since_ts: 仅返回 created_at >= since_ts 的（增量）
    """
    if horizon_min is not None and horizon_min not in (10, 30, 60):
        raise HTTPException(status_code=400, detail="horizon_min must be 10/30/60")
    store = _require_store()
    signals = store.iter_history(
        limit=limit, strategy=strategy, coin=coin,
        horizon_min=horizon_min, since_ts=since_ts,
    )
    return {
        "count": len(signals),
        "signals": [s.model_dump(mode="json") for s in signals],
        "ts": int(time.time()),
    }


@router.get("/signals/{signal_id}")
async def get_signal(signal_id: str) -> dict:
    """单信号详情 · 优先查活跃池，未命中查历史"""
    store = _require_store()
    sig = store.get_active_by_id(signal_id)
    if sig is None:
        # 历史中查找（限制扫描量级避免 hot path 慢）
        for s in store.iter_history(limit=2000):
            if s.signal_id == signal_id:
                sig = s
                break
    if sig is None:
        raise HTTPException(status_code=404, detail=f"signal not found: {signal_id}")
    return sig.model_dump(mode="json")


@router.post("/signals/{signal_id}/cancel")
async def cancel_signal(
    signal_id: str,
    reason: str = Body(default="manual cancel by user", embed=True),
) -> dict:
    """手动取消活跃信号 · 仅 active 态可取消

    P0-4 行为变更：
      - 取消后信号 **不立即归档**，保留在活跃池
      - SignalEngine 主循环会在 expiry_ts + GRACE 后做 shadow settlement → archive
      - 这样能持续追踪"如果不取消会怎样"以衡量取消触发器有效性
    """
    store = _require_store()
    sig = store.get_active_by_id(signal_id)
    if sig is None:
        raise HTTPException(status_code=404, detail="signal not active")
    if sig.state != "active":
        raise HTTPException(status_code=400, detail=f"signal not in active state: {sig.state}")

    cancel_one(
        sig, reason=reason, now_ts=int(time.time()),
        invalidation_kind="manual",
    )
    try:
        store.update_active(sig)
    except Exception as e:  # noqa: BLE001
        logger.error("scalp cancel persist failed | id=%s err=%s", signal_id, e)
        raise HTTPException(status_code=500, detail="failed to persist cancellation")

    try:
        from api.ws import sio
        await sio.emit("scalp_signal_cancelled", sig.model_dump(mode="json"))
    except Exception as e:  # noqa: BLE001
        logger.debug("scalp WS emit cancelled failed: %s", e)

    return {"ok": True, "signal": sig.model_dump(mode="json")}


# ════════════════════════════════════════════════════════════════════════════
# Config
# ════════════════════════════════════════════════════════════════════════════

@router.get("/config")
async def get_config() -> dict:
    """当前完整配置"""
    store = _require_store()
    cfg = store.load_config()
    return cfg.model_dump(mode="json")


@router.patch("/config")
async def patch_config(patch: ScalpConfigPatch) -> dict:
    """运行时热更新配置 · 仅传入要改的字段（None 表示不变）

    可改字段：
      - enabled / coin / horizon_min
      - strategies[name].enabled / confidence_threshold / cooldown_min / notes
      - notification.* 各字段
    """
    store = _require_store()
    cfg = store.load_config()

    # 顶层
    if patch.enabled is not None:
        cfg.enabled = patch.enabled
    if patch.coin is not None:
        cfg.coin = patch.coin.upper()
    if patch.horizon_min is not None:
        if patch.horizon_min not in (10, 30, 60):
            raise HTTPException(status_code=400, detail="horizon_min must be 10/30/60")
        cfg.horizon_min = patch.horizon_min  # type: ignore[assignment]

    # 各策略
    if patch.strategies:
        for name, sub in patch.strategies.items():
            if name not in cfg.strategies:
                continue
            sc = cfg.strategies[name]
            if sub.enabled is not None:
                sc.enabled = sub.enabled
            if sub.confidence_threshold is not None:
                sc.confidence_threshold = sub.confidence_threshold
            if sub.cooldown_min is not None:
                sc.cooldown_min = sub.cooldown_min
            if sub.notes is not None:
                sc.notes = sub.notes

    # 通知
    if patch.notification:
        n = cfg.notification
        if patch.notification.browser_enabled is not None:
            n.browser_enabled = patch.notification.browser_enabled
        if patch.notification.browser_min_confidence is not None:
            n.browser_min_confidence = patch.notification.browser_min_confidence
        if patch.notification.email_enabled is not None:
            n.email_enabled = patch.notification.email_enabled
        if patch.notification.email_min_confidence is not None:
            n.email_min_confidence = patch.notification.email_min_confidence

    store.save_config(cfg)
    logger.info(
        "scalp config patched | enabled=%s coin=%s horizon=%d strategies_enabled=%s",
        cfg.enabled, cfg.coin, cfg.horizon_min,
        [n.value for n, sc in cfg.strategies.items() if sc.enabled],
    )
    return cfg.model_dump(mode="json")


# ════════════════════════════════════════════════════════════════════════════
# Stats / Calibration
# ════════════════════════════════════════════════════════════════════════════

@router.get("/stats")
async def get_stats(
    recompute: bool = Query(default=False, description="忽略缓存重新计算"),
) -> dict:
    """全局统计 · 默认从缓存读，缓存不存在时实时计算"""
    store = _require_store()
    cal = _require_calibrator()

    if recompute:
        stats = cal.recompute_stats()
    else:
        cached = store.load_stats_cache()
        stats = cached if cached is not None else cal.recompute_stats()

    return stats.model_dump(mode="json")


@router.get("/calibration")
async def get_calibration(
    recompute: bool = Query(default=False),
) -> dict:
    """Calibration 曲线（预测置信度 vs 实际命中率）"""
    store = _require_store()
    cal = _require_calibrator()

    if recompute:
        curve = cal.recompute_calibration()
    else:
        cached = store.load_calibration_cache()
        curve = cached if cached is not None else cal.recompute_calibration()

    return curve.model_dump(mode="json")


# ════════════════════════════════════════════════════════════════════════════
# Socket.IO 推送（由 main.py 在创建 SignalEngine 时注入回调）
# ════════════════════════════════════════════════════════════════════════════

async def push_signal_created(signal: ScalpSignal) -> None:
    """新信号生成 · WS 广播 + 发邮件（条件）"""
    try:
        from api.ws import sio
        await sio.emit("scalp_signal_created", signal.model_dump(mode="json"))
    except Exception as e:  # noqa: BLE001
        logger.debug("scalp WS emit created failed: %s", e)

    # 邮件（按 confidence 阈值）
    try:
        store = _store
        if store is None:
            return
        cfg = store.load_config()
        if not cfg.notification.email_enabled:
            return
        if signal.confidence < cfg.notification.email_min_confidence:
            return
        from config.settings import get_settings
        from notifications.email_scalp import send_scalp_signal_email
        email_cfg = get_settings().notifications.email
        await send_scalp_signal_email(
            signal, email_cfg,
            test_mode_subject_prefix=cfg.notification.test_mode_subject_prefix,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("scalp email dispatch failed: %s", e)


async def push_signal_settled(signal: ScalpSignal) -> None:
    """信号结算 · WS 广播（不发邮件，避免刷屏）"""
    try:
        from api.ws import sio
        await sio.emit("scalp_signal_settled", signal.model_dump(mode="json"))
    except Exception as e:  # noqa: BLE001
        logger.debug("scalp WS emit settled failed: %s", e)
