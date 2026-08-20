"""Deribit BTC 期权官方实时概览解析。"""
from __future__ import annotations

import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from models.options import OptionInfoData
from sources.deribit_options import DeribitOptionsSource

if TYPE_CHECKING:
    from engine import CoinState


def _instrument(raw: str) -> tuple[int, float, str] | None:
    parts = raw.split("-")
    if len(parts) != 4 or parts[3] not in {"C", "P"}:
        return None
    try:
        expiry = int(datetime.strptime(parts[1].upper(), "%d%b%y").replace(tzinfo=timezone.utc).timestamp())
        strike = float(parts[2])
    except (TypeError, ValueError):
        return None
    return expiry, strike, parts[3]


async def poll_deribit_options(source: DeribitOptionsSource, state: "CoinState") -> None:
    rows = await source.fetch_book_summaries("BTC")
    if not rows:
        return
    now = int(time.time())
    parsed: list[dict[str, Any]] = []
    underlying = 0.0
    for row in rows:
        instrument = _instrument(str(row.get("instrument_name") or ""))
        if instrument is None or instrument[0] <= now:
            continue
        try:
            oi = max(0.0, float(row.get("open_interest", 0) or 0))
            volume = max(0.0, float(row.get("volume", 0) or 0))
            iv = float(row["mark_iv"]) if row.get("mark_iv") is not None else None
            underlying = max(underlying, float(row.get("underlying_price", 0) or 0))
        except (TypeError, ValueError):
            continue
        parsed.append({"expiry": instrument[0], "strike": instrument[1], "type": instrument[2], "oi": oi, "volume": volume, "iv": iv})
    if not parsed or underlying <= 0:
        return
    call_oi = sum(item["oi"] for item in parsed if item["type"] == "C")
    put_oi = sum(item["oi"] for item in parsed if item["type"] == "P")
    call_volume = sum(item["volume"] for item in parsed if item["type"] == "C")
    put_volume = sum(item["volume"] for item in parsed if item["type"] == "P")
    by_expiry: dict[int, list[dict[str, Any]]] = defaultdict(list)
    by_strike: dict[float, float] = defaultdict(float)
    for item in parsed:
        by_expiry[item["expiry"]].append(item)
        by_strike[item["strike"]] += item["oi"]
    term_structure = []
    for expiry, items in sorted(by_expiry.items())[:8]:
        at_money = sorted(items, key=lambda item: abs(item["strike"] - underlying))[:4]
        ivs = [float(item["iv"]) for item in at_money if item["iv"] is not None]
        term_structure.append({
            "expiry": expiry, "atm_iv": sum(ivs) / len(ivs) if ivs else None,
            "open_interest": sum(item["oi"] for item in items),
        })
    atm_ivs = [item["atm_iv"] for item in term_structure if item["atm_iv"] is not None]
    total_oi = call_oi + put_oi
    nearest_oi = term_structure[0]["open_interest"] if term_structure else 0.0
    state.option_info = OptionInfoData(
        symbol="BTC", ts=now, known_at=now,
        total_oi_usd=total_oi * underlying,
        total_vol_24h_usd=(call_volume + put_volume) * underlying,
        put_call_oi_ratio=put_oi / call_oi if call_oi > 0 else 0.0,
        put_call_vol_ratio=put_volume / call_volume if call_volume > 0 else 0.0,
        iv_atm=atm_ivs[0] if atm_ivs else None,
        # Book summary 无 delta；不得用价外距离伪装 25D skew。
        iv_skew=None,
        term_structure=term_structure,
        strike_clusters=[
            {"strike": strike, "open_interest": oi}
            for strike, oi in sorted(by_strike.items(), key=lambda item: item[1], reverse=True)[:10]
        ],
        expiry_concentration=nearest_oi / total_oi if total_oi > 0 else None,
        gex_status="unavailable", source="deribit_official",
    )
