"""BTC现货动态抄底：快照、配置、机会决策和手工成交账本。"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ai.spot_accumulation_explainer import SpotAccumulationExplainer
from processors.spot_accumulation_service import SpotAccumulationService
from storage.spot_accumulation_store import SpotIdempotencyConflict, SpotStorageCorruption

router = APIRouter(prefix="/api/spot-accumulation", tags=["spot-accumulation"])

_service: Optional[SpotAccumulationService] = None
_explainer: Optional[SpotAccumulationExplainer] = None


def set_components(service: SpotAccumulationService, explainer: SpotAccumulationExplainer) -> None:
    global _service, _explainer
    _service = service
    _explainer = explainer


def _require_service() -> SpotAccumulationService:
    if _service is None:
        raise HTTPException(503, "spot accumulation service not ready")
    return _service


def _btc(coin: str) -> None:
    if coin.upper() != "BTC":
        raise HTTPException(400, "首版仅支持 BTC")


class ConfigMutation(BaseModel):
    model_config = {"extra": "forbid"}

    expected_policy_version: Optional[int] = Field(default=None, ge=1)
    preview_hash: Optional[str] = Field(default=None, min_length=16, max_length=128)
    initial_capital_usdt: Optional[float] = Field(default=None, gt=0)
    core_ratio: Optional[float] = Field(default=None, ge=0, le=1)
    swing_ratio: Optional[float] = Field(default=None, ge=0, le=1)
    tail_ratio: Optional[float] = Field(default=None, ge=0, le=1)
    insurance_ratio: Optional[float] = Field(default=None, ge=0, le=1)
    core_stage_ratios: Optional[dict[str, float]] = None
    core_thresholds: Optional[dict[str, dict[str, float]]] = None
    tail_extreme_v: Optional[float] = Field(default=None, ge=0, le=100)
    tail_extreme_a: Optional[float] = Field(default=None, ge=0, le=100)
    tail_catch_up_v: Optional[float] = Field(default=None, ge=0, le=100)
    tail_catch_up_m: Optional[float] = Field(default=None, ge=0, le=100)
    tail_catch_up_a: Optional[float] = Field(default=None, ge=0, le=100)
    min_price_gap_ratio: Optional[float] = Field(default=None, ge=0, le=1)
    atr_gap_multiplier: Optional[float] = Field(default=None, ge=0)
    acceptance_grace_seconds: Optional[int] = Field(default=None, ge=60, le=86_400)
    weekly_reclaim_weeks: Optional[int] = Field(default=None, ge=1, le=8)
    max_swing_loss_ratio: Optional[float] = Field(default=None, gt=0, le=1)
    min_swing_rr: Optional[float] = Field(default=None, gt=0)
    cycle_ath_override: Optional[float] = Field(default=None, gt=0)
    email_notifications: Optional[bool] = None
    ai_explanation_enabled: Optional[bool] = None

    def patch_payload(self) -> dict:
        return self.model_dump(
            exclude_unset=True,
            exclude={"expected_policy_version", "preview_hash"},
        )


class FillRequest(BaseModel):
    client_event_id: str = Field(min_length=1, max_length=100)
    side: str
    bucket: str
    quantity_btc: float = Field(gt=0)
    price_usdt: float = Field(gt=0)
    fee_usdt: float = Field(default=0, ge=0)
    executed_at: Optional[int] = None
    opportunity_id: Optional[str] = None
    note: str = Field(default="", max_length=500)


class ReversalRequest(BaseModel):
    client_event_id: str = Field(min_length=1, max_length=100)
    note: str = Field(default="", max_length=500)


class OpportunityDecisionRequest(BaseModel):
    decision: str


@router.get("/{coin}/snapshot")
async def get_snapshot(coin: str):
    _btc(coin)
    snapshot = _require_service().get_snapshot()
    if snapshot is None:
        raise HTTPException(503, {
            "message": "BTC现货抄底行情尚未就绪",
            "health": _require_service().health(),
        })
    return snapshot.model_dump(mode="json")


@router.get("/{coin}/health")
async def get_health(coin: str):
    _btc(coin)
    return _require_service().health()


@router.get("/config")
async def get_config():
    return _require_service().config.public_dump()


@router.post("/config/preview")
async def preview_config(request: ConfigMutation):
    service = _require_service()
    if (
        request.expected_policy_version is not None
        and request.expected_policy_version != service.config.policy_version
    ):
        raise HTTPException(409, "策略版本已变化，请重新加载配置")
    try:
        return service.preview_config(request.patch_payload())
    except (SpotStorageCorruption, ValueError) as exc:
        return {"errors": [str(exc)], "preview_hash": None}


@router.patch("/config")
async def patch_config(request: ConfigMutation):
    if request.expected_policy_version is None or not request.preview_hash:
        raise HTTPException(400, "保存配置必须提供expected_policy_version和preview_hash")
    try:
        return _require_service().update_config(
            request.patch_payload(),
            expected_policy_version=request.expected_policy_version,
            preview_hash=request.preview_hash,
        ).public_dump()
    except (SpotStorageCorruption, SpotIdempotencyConflict) as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/{coin}/ledger")
async def get_ledger(coin: str):
    _btc(coin)
    service = _require_service()
    try:
        portfolio = service.store.build_portfolio(service.config)
    except ValueError as exc:
        raise HTTPException(409, f"账本不一致：{exc}") from exc
    return {
        "events": [event.model_dump(mode="json") for event in service.get_events()],
        "portfolio": portfolio.model_dump(mode="json"),
    }


@router.post("/{coin}/fills")
async def create_fill(coin: str, request: FillRequest):
    _btc(coin)
    try:
        event = _require_service().record_fill(request.model_dump(exclude_none=True))
        return event.model_dump(mode="json")
    except (SpotIdempotencyConflict, SpotStorageCorruption) as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/{coin}/fills/{event_id}/reverse")
async def reverse_fill(coin: str, event_id: str, request: ReversalRequest):
    _btc(coin)
    try:
        event = _require_service().reverse_fill(
            event_id, request.client_event_id, request.note,
        )
        return event.model_dump(mode="json")
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except (SpotIdempotencyConflict, SpotStorageCorruption) as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/{coin}/opportunities/{opportunity_id}/decision")
async def decide_opportunity(coin: str, opportunity_id: str, request: OpportunityDecisionRequest):
    _btc(coin)
    try:
        item = _require_service().decide_opportunity(opportunity_id, request.decision)
        return item.model_dump(mode="json")
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except SpotStorageCorruption as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/{coin}/explain")
async def explain_snapshot(coin: str):
    _btc(coin)
    service = _require_service()
    snapshot = service.get_snapshot()
    if snapshot is None:
        raise HTTPException(503, "BTC行情尚未就绪")
    if not service.config.ai_explanation_enabled:
        raise HTTPException(409, "AI解释已关闭")
    explainer = _explainer or SpotAccumulationExplainer()
    text = await explainer.explain(snapshot)
    service.set_ai_explanation(text)
    return {"explanation": text, "available": explainer.available}
