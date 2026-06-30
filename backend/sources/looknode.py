"""Looknode BTC exchange transfer flow source.

The public endpoints expose daily BTC inflow/outflow for seven major exchanges.
This source is deliberately independent from the CoinGlass limiter and keeps a
six-hour in-memory cache because the upstream series updates only once per day.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

from config.settings import LooknodeSourceConfig
from sources.base import DataSource

logger = logging.getLogger(__name__)


class LooknodeExchangeFlowSource(DataSource):
    def __init__(self, cfg: LooknodeSourceConfig):
        super().__init__("looknode_exchange_flow", cfg.timeout_sec, max_retries=2)
        self._base_url = cfg.base_url.rstrip("/")
        self._cache_ttl_sec = cfg.cache_ttl_sec
        self._cache: Optional[dict[str, Any]] = None
        self._cache_expires_at = 0.0
        self._fetch_lock = asyncio.Lock()
        self.last_error = ""

    def get_poll_interval(self) -> int:
        return self._cache_ttl_sec

    async def fetch(self, coin) -> Optional[dict[str, Any]]:
        if str(getattr(coin, "ccy", "BTC")).upper() != "BTC":
            return None
        return await self.fetch_exchange_flows()

    @staticmethod
    def _validate_envelope(payload: Any, label: str) -> list[dict[str, Any]]:
        if not isinstance(payload, dict) or payload.get("code") != 100:
            raise ValueError(f"{label} response code is not 100")
        rows = payload.get("data")
        if not isinstance(rows, list) or not rows:
            raise ValueError(f"{label} data is empty")
        return [row for row in rows if isinstance(row, dict)]

    async def _fetch_pair_once(self) -> dict[str, Any]:
        session = await self.get_session()

        async def get(path: str) -> dict[str, Any]:
            async with session.get(f"{self._base_url}{path}", params={"ex": "all"}) as response:
                response.raise_for_status()
                payload = await response.json()
                if not isinstance(payload, dict):
                    raise ValueError(f"unexpected {path} response")
                return payload

        started = time.monotonic()
        inflow_payload, outflow_payload = await asyncio.gather(
            get("/api/exFlowIn2"), get("/api/exFlowOut2"),
        )
        inflow = self._validate_envelope(inflow_payload, "inflow")
        outflow = self._validate_envelope(outflow_payload, "outflow")
        fetched_at = int(time.time())
        self._mark_success((time.monotonic() - started) * 1000)
        self.last_error = ""
        return {"inflow": inflow, "outflow": outflow, "fetched_at": fetched_at}

    async def fetch_exchange_flows(self) -> Optional[dict[str, Any]]:
        now = time.monotonic()
        if self._cache is not None and now < self._cache_expires_at:
            return self._cache
        async with self._fetch_lock:
            now = time.monotonic()
            if self._cache is not None and now < self._cache_expires_at:
                return self._cache
            for attempt in range(2):
                try:
                    result = await self._fetch_pair_once()
                    self._cache = result
                    self._cache_expires_at = time.monotonic() + self._cache_ttl_sec
                    logger.info(
                        "Looknode exchange flow ready | inflow=%d outflow=%d",
                        len(result["inflow"]), len(result["outflow"]),
                    )
                    return result
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._mark_failure()
                    self.last_error = f"{type(exc).__name__}: {exc}"
                    if attempt == 0:
                        await asyncio.sleep(1)
                    else:
                        logger.warning("Looknode exchange flow failed | error=%s", self.last_error)
            return self._cache


def create_looknode_source(cfg: LooknodeSourceConfig) -> Optional[LooknodeExchangeFlowSource]:
    return LooknodeExchangeFlowSource(cfg) if cfg.enabled else None
