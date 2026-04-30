"""Strategic AI（主决策官）REST API — 与 MAA 路由风格对齐。"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/strategic", tags=["strategic"])

_engine = None


def set_engine(engine) -> None:
    global _engine
    _engine = engine


def _require_engine():
    if _engine is None:
        raise HTTPException(status_code=503, detail="engine not ready")
    return _engine


def _get_state(coin: str):
    engine = _require_engine()
    state = engine._states.get(coin.upper())
    if state is None:
        raise HTTPException(status_code=404, detail=f"coin not supported: {coin}")
    return state


def _staleness_sec(ts: int | float | None) -> int:
    if not ts:
        return -1
    try:
        return max(0, int(time.time() - float(ts)))
    except (TypeError, ValueError):
        return -1


def _dump_report(report, *, slim: bool = False, include_prompt: bool = True) -> Optional[dict]:
    if report is None:
        return None
    d = report.model_dump()
    if slim or not include_prompt:
        d.pop("prompt_debug", None)
    return d


def _default_include_prompt() -> bool:
    try:
        return bool(_engine._settings.strategic.include_prompt_in_api)  # type: ignore[union-attr]
    except Exception:
        return True


@router.get("/report")
async def get_report(
    coin: str = Query("BTC"),
    slim: int = Query(0, ge=0, le=1),
    include_prompt: Optional[int] = Query(None, ge=0, le=1),
) -> dict[str, Any]:
    state = _get_state(coin)
    report = getattr(state, "strategic_report", None)
    if report is None:
        raise HTTPException(status_code=404, detail=f"no strategic report for {coin} yet")
    inc = bool(include_prompt) if include_prompt is not None else _default_include_prompt()
    dumped = _dump_report(report, slim=bool(slim), include_prompt=inc)
    assert dumped is not None
    dumped["stale_sec"] = _staleness_sec(getattr(report, "timestamp", None))
    return dumped


@router.get("/report/history")
async def get_report_history(
    coin: str = Query("BTC"),
    limit: int = Query(20, ge=1, le=200),
    slim: int = Query(1, ge=0, le=1),
) -> dict[str, Any]:
    state = _get_state(coin)
    hist = list(getattr(state, "strategic_history", []) or [])
    hist.reverse()
    hist = hist[:limit]
    items: list[dict] = []
    include_prompt = not bool(slim)
    for r in hist:
        d = _dump_report(r, slim=bool(slim), include_prompt=include_prompt)
        if d is not None:
            items.append(d)
    return {"coin": coin.upper(), "count": len(items), "items": items}


@router.get("/report/{coin}/{ts}")
async def get_report_detail(coin: str, ts: int) -> dict[str, Any]:
    state = _get_state(coin)
    hist = list(getattr(state, "strategic_history", []) or [])
    for r in reversed(hist):
        if int(getattr(r, "timestamp", 0) or 0) == int(ts):
            inc = _default_include_prompt()
            d = _dump_report(r, slim=False, include_prompt=inc)
            if d is None:
                break
            d["stale_sec"] = _staleness_sec(d.get("timestamp"))
            return d
    cur = getattr(state, "strategic_report", None)
    if cur is not None and int(getattr(cur, "timestamp", 0) or 0) == int(ts):
        inc = _default_include_prompt()
        d = _dump_report(cur, slim=False, include_prompt=inc)
        if d is not None:
            d["stale_sec"] = _staleness_sec(d.get("timestamp"))
            return d
    raise HTTPException(status_code=404, detail="report not found")


@router.post("/run")
async def run_once(coin: str = Query("BTC")) -> dict[str, Any]:
    engine = _require_engine()
    ccy = coin.upper()
    if ccy not in engine._settings.supported_coins:
        raise HTTPException(status_code=404, detail=f"coin not supported: {coin}")
    if not engine.strategic_available:
        raise HTTPException(status_code=503, detail="Strategic arbiter not available")
    try:
        await engine.fire_strategic_analysis(ccy)
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return {"coin": ccy, "status": "dispatched", "started_at": int(time.time())}
