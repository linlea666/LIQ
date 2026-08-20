"""BTC 情报室原生现货深度：Binance、OKX、Coinbase 独立快照。"""
from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any, Optional

from config.settings import CoinConfig
from sources.binance_futures import BinanceFuturesSource
from sources.coinbase_native import CoinbaseNativeSource

if TYPE_CHECKING:
    from engine import CoinState


def _depth(levels: Any, *, mid: float, side: str, width_pct: float = 1.0) -> float:
    total = 0.0
    for row in levels if isinstance(levels, list) else []:
        try:
            price, quantity = float(row[0]), float(row[1])
        except (TypeError, ValueError, IndexError):
            continue
        distance = (mid - price) / mid * 100 if side == "bid" else (price - mid) / mid * 100
        if 0 <= distance <= width_pct:
            total += price * quantity
    return total


def _summary(source: str, bids: Any, asks: Any, observed_at: int, source_time: int = 0) -> Optional[dict[str, Any]]:
    try:
        best_bid = max(float(row[0]) for row in bids)
        best_ask = min(float(row[0]) for row in asks)
    except (TypeError, ValueError, IndexError, StopIteration):
        return None
    if best_bid <= 0 or best_ask <= best_bid:
        return None
    mid = (best_bid + best_ask) / 2
    return {
        "source": source, "as_of": int(source_time or observed_at),
        "observed_at": observed_at, "mid": mid,
        "bid_depth_1pct_usd": _depth(bids, mid=mid, side="bid"),
        "ask_depth_1pct_usd": _depth(asks, mid=mid, side="ask"),
        "spread_bps": (best_ask - best_bid) / mid * 10_000,
    }


async def poll_native_liquidity(
    bn: BinanceFuturesSource, cb: CoinbaseNativeSource,
    coin: CoinConfig, state: "CoinState",
) -> None:
    if coin.ccy != "BTC":
        return
    session = await bn.get_session()

    async def binance() -> Optional[dict[str, Any]]:
        async with session.get(
            "https://api.binance.com/api/v3/depth",
            params={"symbol": "BTCUSDT", "limit": 1000},
        ) as response:
            if response.status != 200:
                return None
            payload = await response.json()
            now = int(time.time())
            return _summary("binance_spot_depth", payload.get("bids"), payload.get("asks"), now)

    async def okx() -> Optional[dict[str, Any]]:
        async with session.get(
            "https://www.okx.com/api/v5/market/books",
            params={"instId": "BTC-USDT", "sz": 400},
        ) as response:
            if response.status != 200:
                return None
            payload = await response.json()
            item = next(iter(payload.get("data") or []), {})
            now = int(time.time())
            source_time = int(float(item.get("ts", 0) or 0) / 1000)
            return _summary("okx_spot_depth", item.get("bids"), item.get("asks"), now, source_time)

    async def coinbase() -> Optional[dict[str, Any]]:
        payload = await cb.fetch_orderbook("BTC-USD", level=2)
        now = int(time.time())
        return _summary(
            "coinbase_spot_depth", (payload or {}).get("bids"),
            (payload or {}).get("asks"), now,
        ) if payload else None

    results = await asyncio.gather(binance(), okx(), coinbase(), return_exceptions=True)
    exchanges = {
        item["source"]: item for item in results if isinstance(item, dict)
    }
    state.native_liquidity = {
        "ts": int(time.time()), "available_count": len(exchanges),
        "required_count": 2, "exchanges": exchanges,
        "continuity": "snapshot_only",
    }
