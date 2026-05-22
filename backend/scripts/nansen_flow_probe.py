#!/usr/bin/env python3
"""Probe low-frequency Nansen flow endpoints for SMC planning.

Reads NANSEN_API_KEY from the environment and never prints it.  The script is
intended for one-off field discovery before wiring new flow signals into SMC.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.settings import NansenSourceConfig  # noqa: E402
from sources.nansen import NansenSource  # noqa: E402


TOKENS = {
    "WBTC": {
        "chain": "ethereum",
        "address": "0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599",
    },
    "WETH": {
        "chain": "ethereum",
        "address": "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
    },
}

LABELS = ("exchange", "smart_money", "whale")

LOG_FORMAT = "[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s"
LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"
logger = logging.getLogger("nansen_flow_probe")


def _items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        data = payload.get("data", payload)
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]
        if isinstance(data, dict):
            return [data]
    return []


def _compact(row: dict[str, Any]) -> dict[str, Any]:
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


def _num(value: Any) -> float:
    if isinstance(value, bool) or value is None:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _sorted_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(rows, key=lambda r: str(r.get("date") or r.get("time") or ""))


def _flow_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows or not any("total_inflows_cex" in r or "total_outflows_cex" in r for r in rows):
        return {}
    ordered = _sorted_rows(rows)
    latest = ordered[-1]
    latest_price = _num(latest.get("price_usd"))
    cex_in = sum(_num(r.get("total_inflows_cex")) for r in ordered)
    cex_out = sum(_num(r.get("total_outflows_cex")) for r in ordered)
    dex_in = sum(_num(r.get("total_inflows_dex")) for r in ordered)
    dex_out = sum(_num(r.get("total_outflows_dex")) for r in ordered)
    cex_net = cex_in - cex_out
    dex_net = dex_in - dex_out
    summary: dict[str, Any] = {
        "from": ordered[0].get("date"),
        "to": latest.get("date"),
        "rows": len(ordered),
        "latest_price_usd": latest_price,
        "cex_in_token": cex_in,
        "cex_out_token": cex_out,
        "cex_net_token": cex_net,
        "dex_in_token": dex_in,
        "dex_out_token": dex_out,
        "dex_net_token": dex_net,
    }
    if latest_price > 0:
        summary["cex_net_usd_approx"] = cex_net * latest_price
        summary["dex_net_usd_approx"] = dex_net * latest_price
    return summary


def _print_rows(title: str, rows: list[dict[str, Any]], last_error: str = "") -> None:
    status = "OK" if rows else f"EMPTY {last_error}".strip()
    logger.info("probe result | title=%s status=%s rows=%s", title, status, len(rows))
    if rows:
        ordered = _sorted_rows(rows)
        sample = ordered[-1]
        summary = _flow_summary(rows)
        logger.info("probe fields | title=%s fields=%s", title, ",".join(sorted(rows[0].keys())))
        logger.info(
            "probe sample | title=%s sample=%s",
            title,
            json.dumps(_compact(sample), ensure_ascii=False, sort_keys=True),
        )
        if summary:
            logger.info(
                "probe flow-summary | title=%s summary=%s",
                title,
                json.dumps(summary, ensure_ascii=False, sort_keys=True),
            )


async def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format=LOG_FORMAT,
        datefmt=LOG_DATEFMT,
        stream=sys.stdout,
    )
    api_key = os.getenv("NANSEN_API_KEY", "").strip()
    if not api_key:
        logger.error("NANSEN_API_KEY is not set")
        return 2

    source = NansenSource(NansenSourceConfig(), api_key=api_key)
    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=7)
    logger.info(
        "probe started | tokens=%s labels=%s from=%s to=%s",
        ",".join(TOKENS.keys()),
        ",".join(LABELS),
        start.isoformat(),
        today.isoformat(),
    )
    try:
        for symbol, meta in TOKENS.items():
            chain = meta["chain"]
            address = meta["address"]
            logger.info("probe endpoint | token=%s endpoint=tgm/flow-intelligence timeframe=1d", symbol)
            flow = await source.fetch_flow_intelligence(
                chain=chain,
                token_address=address,
                timeframe="1d",
            )
            _print_rows(
                f"{symbol} flow-intelligence",
                _items(flow),
                source.last_error,
            )

            for label in LABELS:
                logger.info(
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
                _print_rows(f"{symbol} tgm/flows label={label}", rows, source.last_error)
    finally:
        await source.close()
    logger.info("probe finished")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
