"""联合风险预警只读 API。所有状态推进只发生在后台固定 tick。"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query

router = APIRouter(prefix="/api/market-risk", tags=["market-risk"])
_service: Optional[Any] = None


def set_service(service: Any) -> None:
    global _service
    _service = service


def _require_service() -> Any:
    if _service is None:
        raise HTTPException(503, "Market risk service not ready")
    return _service


def _require_enabled_service() -> Any:
    service = _require_service()
    if not service.config.enabled:
        raise HTTPException(503, "Market risk feature is disabled")
    return service


@router.get("/health")
async def get_market_risk_health():
    service = _require_service()
    payload = service.health().model_dump()
    payload["effective_config"] = service.config.effective_dict()
    payload["config_source"] = "config.yaml"
    return payload


@router.get("/ready")
async def get_market_risk_ready():
    service = _require_service()
    return service.ready().model_dump()


@router.get("/{coin}/history")
async def get_market_risk_history(
    coin: str,
    from_ts: Optional[int] = Query(None, alias="from"),
    to_ts: Optional[int] = Query(None, alias="to"),
    limit: int = Query(2_000, ge=1, le=10_000),
    order: str = Query("asc", pattern="^(asc|desc)$"),
    cursor: Optional[int] = Query(None),
):
    service = _require_enabled_service()
    coin_u = coin.upper()
    if coin_u not in service.config.coins:
        raise HTTPException(400, f"Market risk not enabled for {coin_u}")
    items = service.store.history(
        coin_u, from_ts, to_ts, limit, order=order, cursor=cursor,
    )
    return {
        "coin": coin_u,
        "from": from_ts,
        "to": to_ts,
        "order": order,
        "cursor": cursor,
        "next_cursor": (
            int(items[-1].get("decision_time") or 0) if len(items) == limit else None
        ),
        "items": items,
    }


@router.get("/{coin}/intelligence")
async def get_market_risk_intelligence(coin: str):
    service = _require_enabled_service()
    coin_u = coin.upper()
    if coin_u not in service.config.coins:
        raise HTTPException(400, f"Market risk not enabled for {coin_u}")
    intelligence = service.intelligence(coin_u)
    if intelligence is None:
        raise HTTPException(503, f"Market risk warming for {coin_u}")
    return intelligence.model_dump()


@router.get("/{coin}/incidents/{incident_id}")
async def get_market_risk_incident(coin: str, incident_id: str):
    service = _require_enabled_service()
    result = service.store.incident(incident_id)
    if result is None or result["snapshot"].get("coin") != coin.upper():
        raise HTTPException(404, "Incident not found")
    return result


@router.get("/{coin}")
async def get_market_risk(coin: str):
    service = _require_enabled_service()
    coin_u = coin.upper()
    if coin_u not in service.config.coins:
        raise HTTPException(400, f"Market risk not enabled for {coin_u}")
    snapshot = service.latest(coin_u)
    if snapshot is None:
        raise HTTPException(503, f"Market risk warming for {coin_u}")
    return snapshot.model_dump()
