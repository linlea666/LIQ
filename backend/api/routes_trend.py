"""BTC 原生趋势与资金流只读 API。"""

from __future__ import annotations

from typing import Literal, Optional

from fastapi import APIRouter, HTTPException, Query

router = APIRouter(prefix="/api/trend", tags=["btc-trend-monitor"])
_service = None


def set_service(service) -> None:
    global _service
    _service = service


def _require_btc(coin: str, *, allow_disabled: bool = False):
    if coin.upper() != "BTC":
        raise HTTPException(status_code=404, detail="Trend monitor is BTC-only")
    if _service is None:
        raise HTTPException(status_code=503, detail="Trend monitor unavailable")
    if not allow_disabled and not _service.enabled:
        raise HTTPException(status_code=503, detail="Trend monitor disabled")
    return _service


@router.get("/hyperliquid-whale-distributions")
async def get_hyperliquid_whale_distributions():
    service = _require_btc("BTC")
    return service.hyperliquid_whale_distributions().model_dump(mode="json")


@router.get("/{coin}")
async def get_trend(coin: str):
    service = _require_btc(coin)
    snapshot = service.latest()
    if snapshot is None:
        raise HTTPException(status_code=503, detail="Trend snapshot not ready")
    return snapshot.model_dump(mode="json")


@router.get("/{coin}/history")
async def get_trend_history(coin: str, limit: int = Query(200, ge=1, le=2000)):
    service = _require_btc(coin)
    return {"coin": "BTC", "items": service.store.history(limit)}


@router.get("/{coin}/flow-history")
async def get_closed_flow_history(
    coin: str,
    window: Literal["1h", "4h", "24h"] = Query("1h"),
    limit: Optional[int] = Query(None, ge=1, le=168),
):
    service = _require_btc(coin)
    defaults = {"1h": 24, "4h": 18, "24h": 7}
    maximums = {"1h": 168, "4h": 42, "24h": 7}
    selected_limit = defaults[window] if limit is None else limit
    if selected_limit > maximums[window]:
        raise HTTPException(
            status_code=422,
            detail=f"{window} flow history limit must be <= {maximums[window]}",
        )
    return service.flow_history(window, selected_limit).model_dump(mode="json")


@router.get("/{coin}/events")
async def get_trend_events(coin: str, limit: int = Query(100, ge=1, le=1000)):
    service = _require_btc(coin)
    return {"coin": "BTC", "items": service.store.events(limit)}


@router.get("/{coin}/health")
async def get_trend_health(coin: str):
    service = _require_btc(coin, allow_disabled=True)
    snapshot = service.latest()
    return {
        "coin": "BTC",
        "ready": snapshot is not None,
        "enabled": service.enabled,
        "running": service.running,
        "data_quality": snapshot.data_quality.model_dump() if snapshot else None,
        "source_diagnostics": service.source_diagnostics(),
        "audit_stats": service.store.audit_stats(7),
    }
