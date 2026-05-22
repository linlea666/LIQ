"""Nansen API source for the SMC confirmation layer.

Only low/medium frequency endpoints are wrapped here.  The source never reads
an API key from YAML and never logs request headers.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from typing import Any, Optional

from config.settings import NansenSourceConfig, get_settings
from sources.base import DataSource, CoinConfig

logger = logging.getLogger(__name__)


class _FixedSpacingLimiter:
    def __init__(self, rate_per_min: int = 30):
        rate = max(1, int(rate_per_min or 30))
        self._min_interval = 60.0 / rate
        self._last_request = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            wait = self._min_interval - (now - self._last_request)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request = time.monotonic()


class NansenSource(DataSource):
    """Small Nansen REST client with TTL caching and safe degradation."""

    def __init__(
        self,
        cfg: NansenSourceConfig,
        api_key: str,
    ):
        super().__init__(name="nansen", timeout_sec=cfg.timeout_sec, max_retries=1)
        self._base_url = cfg.base_url.rstrip("/")
        self._api_key = api_key
        self._limiter = _FixedSpacingLimiter(cfg.rate_limit_per_min)
        self._cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self.last_error: str = ""

    def get_poll_interval(self) -> int:
        return 900

    async def fetch(self, coin: CoinConfig) -> Any:
        return await self.fetch_perp_screener(coin.ccy)

    @staticmethod
    def _items(payload: Optional[dict[str, Any]]) -> list[dict[str, Any]]:
        if not payload:
            return []
        data = payload.get("data", payload)
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        if isinstance(data, dict):
            for key in ("items", "rows", "result", "tokens"):
                value = data.get(key)
                if isinstance(value, list):
                    return [item for item in value if isinstance(item, dict)]
            return [data]
        return []

    @staticmethod
    def _cache_key(path: str, body: dict[str, Any]) -> str:
        raw = f"{path}:{json.dumps(body, sort_keys=True, separators=(',', ':'))}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    async def _post(
        self,
        path: str,
        body: dict[str, Any],
        *,
        cache_ttl: int,
    ) -> Optional[dict[str, Any]]:
        if not self._api_key:
            self.last_error = "missing_api_key"
            return None

        ck = self._cache_key(path, body)
        cached = self._cache.get(ck)
        if cached and cached[0] > time.time():
            return cached[1]

        await self._limiter.acquire()

        url = f"{self._base_url}/api/v1/{path.lstrip('/')}"
        headers = {
            "Content-Type": "application/json",
            "apiKey": self._api_key,
        }
        session = await self.get_session()
        t0 = time.time()
        try:
            async with session.post(url, json=body, headers=headers) as resp:
                latency = (time.time() - t0) * 1000
                if resp.status in (401, 403, 429):
                    self.last_error = f"http_{resp.status}"
                    self._mark_failure()
                    return None
                if resp.status >= 400:
                    self.last_error = f"http_{resp.status}"
                    self._mark_failure()
                    return None
                payload = await resp.json()
                self.last_error = ""
                self._mark_success(latency)
                if cache_ttl > 0:
                    self._cache[ck] = (time.time() + cache_ttl, payload)
                return payload
        except Exception as exc:
            self.last_error = exc.__class__.__name__
            self._mark_failure()
            logger.debug("Nansen request failed | path=%s err=%s", path, self.last_error)
            return None

    async def fetch_perp_screener(self, symbol: str) -> Optional[dict[str, Any]]:
        """Fetch smart-money perp rows and select the requested BTC/ETH symbol."""
        sym = symbol.upper()
        body = {
            "timeframe": "24h",
            "order_by": [
                {"field": "smart_money_volume", "direction": "DESC"},
            ],
            "pagination": {"page": 1, "per_page": 100},
        }
        payload = await self._post("perp-screener", body, cache_ttl=900)
        rows = self._items(payload)
        if not rows:
            return None
        for row in rows:
            token = str(row.get("token_symbol") or row.get("symbol") or "").upper()
            if token == sym:
                return row
        return rows[0] if sym in {"BTC", "ETH"} else None

    async def fetch_flow_intelligence(
        self,
        *,
        chain: str,
        token_address: str,
        timeframe: str = "1d",
    ) -> Optional[dict[str, Any]]:
        body = {
            "chain": chain,
            "token_address": token_address,
            "timeframe": timeframe,
        }
        payload = await self._post("tgm/flow-intelligence", body, cache_ttl=3600)
        rows = self._items(payload)
        return rows[0] if rows else None

    async def fetch_smart_money_netflow(
        self,
        chains: list[str],
        *,
        per_page: int = 50,
    ) -> list[dict[str, Any]]:
        body = {
            "chains": chains,
            "pagination": {"page": 1, "per_page": per_page},
        }
        payload = await self._post("smart-money/netflow", body, cache_ttl=14400)
        return self._items(payload)

    async def fetch_token_screener(
        self,
        chains: list[str],
        *,
        timeframe: str = "24h",
        per_page: int = 50,
    ) -> list[dict[str, Any]]:
        body = {
            "chains": chains,
            "timeframe": timeframe,
            "filters": {
                "only_smart_money": True,
                "token_age_days": {"min": 1, "max": 365},
            },
            "order_by": [
                {"field": "buy_volume", "direction": "DESC"},
            ],
            "pagination": {"page": 1, "per_page": per_page},
        }
        payload = await self._post("token-screener", body, cache_ttl=14400)
        return self._items(payload)


def create_nansen_source() -> Optional[NansenSource]:
    settings = get_settings()
    cfg = settings.nansen
    if not cfg.enabled:
        return None
    api_key = os.getenv(cfg.api_key_env, "")
    if not api_key:
        logger.info("Nansen source disabled: env key not set")
        return None
    return NansenSource(cfg=cfg, api_key=api_key)
