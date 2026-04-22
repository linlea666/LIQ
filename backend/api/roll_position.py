"""滚仓模块 REST API

职责：HTTP 层 ↔ processors.roll_service.RollService 的转换，不含业务逻辑。

路由设计（全部位于 /api/roll 前缀下）：
  · 持仓
      GET    /positions                    列表
      POST   /positions                    创建
      GET    /positions/{id}               详情（含事件、最新信号）
      DELETE /positions/{id}               硬删除
      GET    /positions/{id}/events        历史事件流
      GET    /positions/{id}/signal        最新缓存信号（引擎周期性计算）
      POST   /positions/{id}/execute       执行一次事件（add/reduce/close/move_sl）
      POST   /positions/{id}/override      用户手动覆盖加仓
  · 模板
      GET    /templates
      POST   /templates                    派生自定义模板
      PUT    /templates/{id}               更新自定义
      DELETE /templates/{id}
  · 设置
      GET    /settings
      PUT    /settings

所有写操作立即落盘（service 内部 persist_store），响应成功后前端可刷新列表。
"""

from __future__ import annotations

import logging
from typing import Any, Literal, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from models.roll_position import (
    AddMode,
    AddTrigger,
    ConfidenceThresholds,
    MarginMode,
    ReduceSignal,
    RollGlobalSettings,
    RollPlan,
    RollTemplate,
    SafetyGates,
    Side,
    UserPosition,
)
from processors.roll_replay import compute_replay_stats
from processors.roll_service import RollService, RollServiceError
from processors.roll_templates import (
    TemplateValidationError,
    delete_template,
    derive_template,
    save_templates,
    update_template,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/roll")

_service: Optional[RollService] = None


def set_service(service: RollService) -> None:
    """由 main.py 启动时注入 RollService 实例。"""
    global _service
    _service = service


def _require_service() -> RollService:
    if _service is None:
        raise HTTPException(503, "RollService 未初始化")
    return _service


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 请求体 / 响应体 DTO
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class CreatePositionReq(BaseModel):
    coin: str
    side: Side
    margin_mode: MarginMode
    leverage: int = Field(..., ge=1, le=125)
    entry_price: float = Field(..., gt=0)
    margin_usd: float = Field(..., gt=0)
    template_id: str
    name: str = ""
    note: str = ""
    stop_loss: Optional[float] = None
    # 可选：覆盖部分 plan 字段（overrides 会透传给 plan_from_template）
    plan_overrides: Optional[dict[str, Any]] = None


class PositionWithPlanResp(BaseModel):
    position: UserPosition
    plan: RollPlan
    latest_signal: Optional[dict] = None


class ExecuteEventReq(BaseModel):
    kind: Literal["add", "reduce", "close", "move_sl"]
    price: float = Field(..., gt=0)
    # add 专用
    margin_delta_usd: Optional[float] = None
    # reduce 专用
    reduce_pct: Optional[float] = None
    # close 专用
    close_kind: Optional[Literal["close_manual", "close_sl_hit", "close_tp_hit"]] = "close_manual"
    # move_sl 专用
    new_sl: Optional[float] = None
    # 通用
    reason: str = ""
    # add 事件可选：关联的系统信号（便于覆盖率统计）
    system_confidence: float = 0.0
    system_action: str = ""


class OverrideAddReq(BaseModel):
    price: float = Field(..., gt=0)
    margin_delta_usd: float = Field(..., gt=0)
    reason: str = ""
    system_confidence: float = 0.0
    system_action: str = ""


class DeriveTemplateReq(BaseModel):
    source_id: str
    new_id: str
    new_name: str


class UpdateTemplateReq(BaseModel):
    patch: dict[str, Any]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 持仓相关路由
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@router.get("/positions")
def list_positions(status: Optional[Literal["active", "closed", "all"]] = "active"):
    """列出持仓（默认仅 active）。"""
    svc = _require_service()
    positions = list(svc.store.positions.values())
    if status == "active":
        positions = [p for p in positions if p.status == "active"]
    elif status == "closed":
        positions = [p for p in positions if p.status == "closed"]

    items: list[dict[str, Any]] = []
    for p in positions:
        plan = svc.store.get_plan(p.plan_id)
        latest = svc.last_signals.get(p.id)
        items.append({
            "position": p.model_dump(),
            "plan": plan.model_dump() if plan else None,
            "latest_signal": latest.model_dump() if latest else None,
        })
    return {"items": items, "count": len(items)}


@router.post("/positions")
def create_position(req: CreatePositionReq):
    """创建新持仓 + 从模板派生计划。"""
    svc = _require_service()
    try:
        pos, plan = svc.create_position(
            coin=req.coin,
            side=req.side,
            margin_mode=req.margin_mode,
            leverage=req.leverage,
            entry_price=req.entry_price,
            margin_usd=req.margin_usd,
            total_account_usd=svc.settings.total_account_usd,
            template_id=req.template_id,
            name=req.name,
            note=req.note,
            stop_loss=req.stop_loss,
            plan_overrides=req.plan_overrides,
        )
    except RollServiceError as e:
        raise HTTPException(400, str(e)) from e
    return {
        "position": pos.model_dump(),
        "plan": plan.model_dump(),
    }


@router.get("/positions/{position_id}")
def get_position(position_id: str):
    svc = _require_service()
    pos = svc.store.get_position(position_id)
    if pos is None:
        raise HTTPException(404, f"持仓不存在: {position_id}")
    plan = svc.store.get_plan(pos.plan_id)
    latest = svc.last_signals.get(position_id)
    return {
        "position": pos.model_dump(),
        "plan": plan.model_dump() if plan else None,
        "latest_signal": latest.model_dump() if latest else None,
    }


@router.delete("/positions/{position_id}")
def delete_position(position_id: str):
    svc = _require_service()
    try:
        svc.delete_position(position_id)
    except RollServiceError as e:
        raise HTTPException(404, str(e)) from e
    return {"ok": True, "deleted": position_id}


@router.get("/positions/{position_id}/events")
def list_position_events(position_id: str, limit: int = 500):
    svc = _require_service()
    pos = svc.store.get_position(position_id)
    if pos is None:
        raise HTTPException(404, f"持仓不存在: {position_id}")
    events = pos.events[-limit:] if limit > 0 else pos.events
    return {
        "position_id": position_id,
        "count": len(events),
        "events": [e.model_dump() for e in events],
    }


@router.get("/positions/{position_id}/signal")
def get_latest_signal(position_id: str):
    svc = _require_service()
    if position_id not in svc.store.positions:
        raise HTTPException(404, f"持仓不存在: {position_id}")
    signal = svc.last_signals.get(position_id)
    if signal is None:
        raise HTTPException(404, "尚未评估（引擎下一周期会生成）")
    return signal.model_dump()


@router.post("/positions/{position_id}/execute")
def execute_event(position_id: str, req: ExecuteEventReq):
    """用户确认执行一次事件：add / reduce / close / move_sl。

    字段校验按 kind 分派；服务层会更新 position 状态并落盘事件。
    """
    svc = _require_service()
    try:
        if req.kind == "add":
            if req.margin_delta_usd is None or req.margin_delta_usd <= 0:
                raise HTTPException(400, "add 事件必须提供 margin_delta_usd > 0")
            pos = svc.execute_add(
                position_id=position_id,
                margin_delta_usd=req.margin_delta_usd,
                price=req.price,
                reason=req.reason,
                system_confidence=req.system_confidence,
                system_action=req.system_action,
                user_override=False,
            )
        elif req.kind == "reduce":
            if req.reduce_pct is None or not (0 < req.reduce_pct <= 1):
                raise HTTPException(400, "reduce 事件必须提供 reduce_pct ∈ (0, 1]")
            pos = svc.execute_reduce(
                position_id=position_id,
                reduce_pct=req.reduce_pct,
                price=req.price,
                reason=req.reason,
            )
        elif req.kind == "close":
            pos = svc.execute_close(
                position_id=position_id,
                price=req.price,
                reason=req.reason,
                kind=req.close_kind or "close_manual",
            )
        elif req.kind == "move_sl":
            if req.new_sl is None:
                raise HTTPException(400, "move_sl 事件必须提供 new_sl")
            pos = svc.execute_move_sl(
                position_id=position_id,
                new_sl=req.new_sl,
                price=req.price,
                reason=req.reason,
            )
        else:
            raise HTTPException(400, f"未知事件类型: {req.kind}")
    except RollServiceError as e:
        raise HTTPException(400, str(e)) from e

    plan = svc.store.get_plan(pos.plan_id)
    return {
        "position": pos.model_dump(),
        "plan": plan.model_dump() if plan else None,
    }


@router.get("/positions/{position_id}/replay")
def get_replay(position_id: str):
    """复盘统计：事件流 + 覆盖率/P&L 等聚合指标。

    对 active 持仓也可调用（反映"至今"的统计）。
    """
    svc = _require_service()
    pos = svc.store.get_position(position_id)
    if pos is None:
        raise HTTPException(404, f"持仓不存在: {position_id}")
    plan = svc.store.get_plan(pos.plan_id)
    stats = compute_replay_stats(pos)
    return {
        "position": pos.model_dump(),
        "plan": plan.model_dump() if plan else None,
        "events": [e.model_dump() for e in pos.events],
        "stats": stats.model_dump(),
    }


@router.post("/positions/{position_id}/override")
def execute_override_add(position_id: str, req: OverrideAddReq):
    """用户手动覆盖加仓（系统原本不推荐或被闸门拦截时）。

    与普通 execute(add) 区别：
      - 事件 kind = user_override_add
      - 触发覆盖次数统计（未来接入行为熔断）
    """
    svc = _require_service()
    try:
        pos = svc.execute_add(
            position_id=position_id,
            margin_delta_usd=req.margin_delta_usd,
            price=req.price,
            reason=req.reason or "user_override",
            system_confidence=req.system_confidence,
            system_action=req.system_action,
            user_override=True,
        )
    except RollServiceError as e:
        raise HTTPException(400, str(e)) from e
    return {"position": pos.model_dump()}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 模板相关路由
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@router.get("/templates")
def list_templates():
    svc = _require_service()
    return {
        "items": [t.model_dump() for t in svc.templates],
        "count": len(svc.templates),
    }


@router.post("/templates")
def derive_custom_template(req: DeriveTemplateReq):
    """从现有模板派生一份自定义模板（id 必须以 custom: 开头）。"""
    svc = _require_service()
    try:
        new_tpl = derive_template(
            svc.templates,
            source_id=req.source_id,
            new_id=req.new_id,
            new_name=req.new_name,
        )
        svc.templates.append(new_tpl)
        save_templates(svc.data_dir, svc.templates)
    except TemplateValidationError as e:
        raise HTTPException(400, str(e)) from e
    return new_tpl.model_dump()


@router.put("/templates/{template_id}")
def update_custom_template(template_id: str, req: UpdateTemplateReq):
    svc = _require_service()
    try:
        updated = update_template(svc.templates, template_id, req.patch)
        save_templates(svc.data_dir, svc.templates)
    except TemplateValidationError as e:
        raise HTTPException(400, str(e)) from e
    return updated.model_dump()


@router.delete("/templates/{template_id}")
def delete_custom_template(template_id: str):
    svc = _require_service()
    try:
        delete_template(svc.templates, template_id)
        save_templates(svc.data_dir, svc.templates)
    except TemplateValidationError as e:
        raise HTTPException(400, str(e)) from e
    return {"ok": True, "deleted": template_id}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 全局设置路由
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@router.get("/settings")
def get_global_settings():
    svc = _require_service()
    return svc.settings.model_dump()


@router.put("/settings")
def update_global_settings(patch: dict[str, Any]):
    svc = _require_service()

    try:
        merged = svc.settings.model_copy(update=patch)
        # 走一次 Pydantic 校验（触发 Field 约束）
        RollGlobalSettings.model_validate(merged.model_dump())
    except Exception as e:   # noqa: BLE001
        raise HTTPException(400, f"设置非法: {e}") from e

    # 边界校验：静默时段合法性
    if merged.quiet_start_utc < 0 or merged.quiet_start_utc > 23:
        raise HTTPException(400, "quiet_start_utc 必须 ∈ [0, 23]")
    if merged.quiet_end_utc < 0 or merged.quiet_end_utc > 23:
        raise HTTPException(400, "quiet_end_utc 必须 ∈ [0, 23]")

    import time as _t
    merged.updated_at = int(_t.time())
    svc.settings = merged
    svc.persist_settings()

    # 同步前瞻扫描器冷却时长
    svc.forward_scanner.default_cooldown_sec = merged.forward_alert_cooldown_min * 60

    return svc.settings.model_dump()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 辅助：让 TS 类型定义器也能拿到枚举值（前端 codegen 可用）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@router.get("/enums")
def list_enums():
    """返回前端可能用到的枚举集合，便于 UI 固定选项。"""
    from typing import get_args

    return {
        "sides": list(get_args(Side)),
        "margin_modes": list(get_args(MarginMode)),
        "add_modes": list(get_args(AddMode)),
        "add_triggers": list(get_args(AddTrigger)),
        "reduce_signals": list(get_args(ReduceSignal)),
        "safety_gates_defaults": SafetyGates().model_dump(),
        "thresholds_defaults": ConfidenceThresholds().model_dump(),
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 明确导出：RollTemplate 类型（提示 mypy / 前端 OpenAPI 生成）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
__all__ = ["router", "set_service", "RollTemplate", "RollPlan", "UserPosition"]
