"""SMC / Smart Money Concepts REST API."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query

from models.smc import SMCSnapshot

logger = logging.getLogger(__name__)
probe_logger = logging.getLogger("nansen_flow_probe")

router = APIRouter(prefix="/api/smc", tags=["smc"])

NANSEN_FLOW_PROBE_TOKENS = {
    "WBTC": {
        "chain": "ethereum",
        "address": "0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599",
    },
    "WETH": {
        "chain": "ethereum",
        "address": "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
    },
}
NANSEN_FLOW_PROBE_LABELS = ("exchange", "smart_money", "whale")

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


def _compact_probe_row(row: dict[str, Any]) -> dict[str, Any]:
    preferred = [
        "date",
        "chain",
        "token_symbol",
        "symbol",
        "label",
        "inflow_usd",
        "outflow_usd",
        "net_flow_usd",
        "netflow_usd",
        "smart_trader_net_flow_usd",
        "top_pnl_net_flow_usd",
        "whale_net_flow_usd",
        "exchange_net_flow_usd",
        "inflow",
        "outflow",
        "net_flow",
        "count",
    ]
    out = {k: row.get(k) for k in preferred if k in row}
    if out:
        return out
    return {k: row[k] for k in list(row)[:12]}


def _probe_items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        data = payload.get("data", payload)
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]
        if isinstance(data, dict):
            return [data]
    return []


def _log_probe_result(title: str, rows: list[dict[str, Any]], last_error: str = "") -> dict[str, Any]:
    status = "OK" if rows else f"EMPTY {last_error}".strip()
    fields = sorted(rows[0].keys()) if rows else []
    sample = _compact_probe_row(rows[0]) if rows else {}
    probe_logger.info("probe result | title=%s status=%s rows=%s", title, status, len(rows))
    if rows:
        probe_logger.info("probe fields | title=%s fields=%s", title, ",".join(fields))
        probe_logger.info("probe sample | title=%s sample=%s", title, sample)
    return {
        "title": title,
        "status": status,
        "rows": len(rows),
        "fields": fields,
        "sample": sample,
    }


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


@router.post("/probe/nansen-flows")
async def probe_nansen_flows() -> dict[str, Any]:
    engine = _require_engine()
    source = getattr(engine, "_nansen", None)
    if source is None:
        probe_logger.error("probe skipped | reason=nansen_source_unavailable")
        raise HTTPException(status_code=503, detail="nansen source unavailable")

    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=7)
    probe_logger.info(
        "probe started | tokens=%s labels=%s from=%s to=%s",
        ",".join(NANSEN_FLOW_PROBE_TOKENS.keys()),
        ",".join(NANSEN_FLOW_PROBE_LABELS),
        start.isoformat(),
        today.isoformat(),
    )
    results: list[dict[str, Any]] = []
    for symbol, meta in NANSEN_FLOW_PROBE_TOKENS.items():
        chain = meta["chain"]
        address = meta["address"]
        probe_logger.info("probe endpoint | token=%s endpoint=tgm/flow-intelligence timeframe=1d", symbol)
        flow = await source.fetch_flow_intelligence(
            chain=chain,
            token_address=address,
            timeframe="1d",
        )
        results.append(_log_probe_result(f"{symbol} flow-intelligence", _probe_items(flow), source.last_error))

        for label in NANSEN_FLOW_PROBE_LABELS:
            probe_logger.info(
                "probe endpoint | token=%s endpoint=tgm/flows label=%s window=7d",
                symbol,
                label,
            )
            rows = await source.fetch_tgm_flows(
                chain=chain,
                token_address=address,
                label=label,
                from_date=start.isoformat(),
                to_date=today.isoformat(),
                per_page=20,
            )
            results.append(_log_probe_result(f"{symbol} tgm/flows label={label}", rows, source.last_error))

    probe_logger.info("probe finished | results=%d", len(results))
    return {
        "status": "ok",
        "from": start.isoformat(),
        "to": today.isoformat(),
        "results": results,
    }


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
