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
NANSEN_FLOW_PROBE_PER_PAGE = 200

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
        "price_usd",
        "token_amount",
        "value_usd",
        "holders_count",
        "total_inflows_cex",
        "total_outflows_cex",
        "total_inflows_dex",
        "total_outflows_dex",
        "total_inflows_count",
        "total_outflows_count",
        "inflow",
        "outflow",
        "net_flow",
        "count",
    ]
    out = {k: row.get(k) for k in preferred if k in row}
    if out:
        return out
    return {k: row[k] for k in list(row)[:12]}


def _probe_num(value: Any) -> float:
    if isinstance(value, bool) or value is None:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _probe_sorted_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(rows, key=lambda r: str(r.get("date") or r.get("time") or ""))


def _probe_net_from_signed_or_positive_parts(in_value: float, out_value: float) -> float:
    # Nansen currently returns total_outflows_* as negative deltas. Keep this
    # tolerant in case another endpoint returns positive outflow magnitudes.
    if out_value < 0:
        return in_value + out_value
    return in_value - out_value


def _probe_flow_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows or not any("total_inflows_cex" in r or "total_outflows_cex" in r for r in rows):
        return {}
    ordered = _probe_sorted_rows(rows)
    latest = ordered[-1]
    latest_price = _probe_num(latest.get("price_usd"))
    cex_in = sum(_probe_num(r.get("total_inflows_cex")) for r in ordered)
    cex_out = sum(_probe_num(r.get("total_outflows_cex")) for r in ordered)
    dex_in = sum(_probe_num(r.get("total_inflows_dex")) for r in ordered)
    dex_out = sum(_probe_num(r.get("total_outflows_dex")) for r in ordered)
    cex_net = _probe_net_from_signed_or_positive_parts(cex_in, cex_out)
    dex_net = _probe_net_from_signed_or_positive_parts(dex_in, dex_out)
    summary: dict[str, Any] = {
        "from": ordered[0].get("date"),
        "to": latest.get("date"),
        "rows": len(ordered),
        "latest_price_usd": latest_price,
        "cex_in_token": cex_in,
        "cex_out_token_abs": abs(cex_out),
        "cex_out_token_raw": cex_out,
        "cex_net_token": cex_net,
        "dex_in_token": dex_in,
        "dex_out_token_abs": abs(dex_out),
        "dex_out_token_raw": dex_out,
        "dex_net_token": dex_net,
    }
    if latest_price > 0:
        summary["cex_net_usd_approx"] = cex_net * latest_price
        summary["dex_net_usd_approx"] = dex_net * latest_price
    return summary


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
    ordered = _probe_sorted_rows(rows) if rows else []
    sample = _compact_probe_row(ordered[-1]) if ordered else {}
    summary = _probe_flow_summary(rows)
    probe_logger.info("probe result | title=%s status=%s rows=%s", title, status, len(rows))
    if rows:
        probe_logger.info("probe fields | title=%s fields=%s", title, ",".join(fields))
        probe_logger.info("probe sample | title=%s sample=%s", title, sample)
        if summary:
            probe_logger.info("probe flow-summary | title=%s summary=%s", title, summary)
    return {
        "title": title,
        "status": status,
        "rows": len(rows),
        "fields": fields,
        "sample": sample,
        "summary": summary,
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
                per_page=NANSEN_FLOW_PROBE_PER_PAGE,
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
