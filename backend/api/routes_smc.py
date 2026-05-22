"""SMC / Smart Money Concepts REST API."""

from __future__ import annotations

import logging
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query

from models.smc import SMCSnapshot

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/smc", tags=["smc"])

_engine = None


def set_engine(engine) -> None:
    global _engine
    _engine = engine


def _require_engine():
    if _engine is None:
        raise HTTPException(status_code=503, detail="engine not ready")
    return _engine


def _state(coin: str):
    engine = _require_engine()
    c = coin.upper()
    st = engine._states.get(c)
    if st is None:
        raise HTTPException(status_code=404, detail=f"coin not supported: {c}")
    return st


@router.get("/market-breadth")
async def get_market_breadth() -> dict[str, Any]:
    engine = _require_engine()
    breadth = engine.get_smc_market_breadth()
    logger.info(
        "SMC market-breadth served | status=%s score=%.2f",
        breadth.status,
        breadth.breadth_score,
    )
    return breadth.model_dump()


@router.get("/{coin}", response_model=SMCSnapshot)
async def get_smc_snapshot(
    coin: str,
    horizon: Literal["intraday", "swing"] = Query("intraday"),
) -> SMCSnapshot:
    _state(coin)
    engine = _require_engine()
    snap = engine.get_smc_snapshot(coin.upper(), horizon=horizon)
    if snap is None:
        raise HTTPException(status_code=503, detail=f"SMC snapshot unavailable: {coin}")
    logger.info(
        "SMC snapshot served | coin=%s horizon=%s observation=%s state=%s confidence=%d dq=%s nansen=%s",
        snap.coin,
        snap.horizon,
        snap.observation,
        snap.setup_state,
        snap.confidence,
        snap.data_quality.status,
        snap.smart_money.status,
    )
    return snap


@router.get("/{coin}/facts")
async def get_smc_facts(
    coin: str,
    horizon: Literal["intraday", "swing"] = Query("intraday"),
) -> dict[str, Any]:
    _state(coin)
    engine = _require_engine()
    facts = engine.get_smc_facts(coin.upper(), horizon=horizon)
    logger.info(
        "SMC facts served | coin=%s horizon=%s fields=%d",
        coin.upper(),
        horizon,
        len(facts.get("field_map", [])),
    )
    return facts
