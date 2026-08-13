"""BTC 熊市底部证据与验证模型 REST API（只读 + 手动触发）。"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import PlainTextResponse

router = APIRouter(prefix="/api/bottom-model", tags=["bottom-model"])
_service = None


def set_service(service) -> None:
    global _service
    _service = service


def _require_service(allow_disabled: bool = False):
    if _service is None:
        raise HTTPException(status_code=503, detail="Bottom model unavailable")
    if not allow_disabled and not _service.enabled:
        raise HTTPException(status_code=503, detail="Bottom model disabled")
    return _service


@router.get("/snapshot")
async def get_snapshot():
    service = _require_service()
    snapshot = service.latest()
    if snapshot is None:
        raise HTTPException(status_code=503, detail="Bottom model snapshot not ready")
    return snapshot


@router.get("/history")
async def get_history(limit: int = Query(400, ge=1, le=2000)):
    service = _require_service()
    return {"items": service.history(limit)}


@router.get("/evidence-pack", response_class=PlainTextResponse)
async def get_evidence_pack(detail: Literal["full", "compact"] = Query("full")):
    service = _require_service()
    snapshot = service.latest()
    if snapshot is None:
        raise HTTPException(status_code=503, detail="Bottom model snapshot not ready")
    from processors.bottom_model.evidence_pack import build_evidence_pack
    return build_evidence_pack(snapshot, service.store, detail=detail)


@router.get("/health")
async def get_health():
    service = _require_service(allow_disabled=True)
    return service.health()


@router.get("/audit/latest")
async def get_latest_audit():
    service = _require_service()
    audit = service.latest_audit()
    if audit is None:
        raise HTTPException(status_code=404, detail="Mathematical audit not available")
    return audit


@router.get("/audit/{audit_id}")
async def get_audit(audit_id: str):
    service = _require_service()
    audit = service.audit(audit_id)
    if audit is None:
        raise HTTPException(status_code=404, detail="Mathematical audit not found")
    return audit


@router.post("/run")
async def trigger_run(force: bool = Query(False)):
    service = _require_service()
    return service.trigger_run(force=force)
