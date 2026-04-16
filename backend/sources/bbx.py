"""BBX 市场指数数据源：单次 POST 获取 465+ 个宏观/链上/衍生品指标。

无请求频率限制，内置 TTL 缓存避免过度请求。
替代 Coinglass 的 fear_greed、btc_dominance、coinbase_premium 等低频宏观接口，
同时激活 DXY、纳指、黄金、MVRV、波动率等此前无数据源的指标。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

import aiohttp

logger = logging.getLogger(__name__)

_BBX_URL = "https://bbx.com/api/pc?module=v1/market/index"


class BBXSource:
    """BBX 市场指数聚合数据源。

    一次 POST 返回全部指标，按 key 建索引缓存。
    cache_ttl 控制两次实际 HTTP 请求之间的最小间隔（秒）。
    """

    def __init__(self, cache_ttl: int = 120, timeout_sec: int = 15):
        self._cache: dict[str, dict[str, Any]] = {}
        self._cache_ttl = cache_ttl
        self._timeout_sec = timeout_sec
        self._last_fetch_ts: float = 0
        self._session: Optional[aiohttp.ClientSession] = None
        self._error_count = 0
        self._last_success_ts: float = 0

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self._timeout_sec),
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def fetch_all(self) -> dict[str, dict[str, Any]]:
        """获取全部指标并缓存。缓存未过期时直接返回。"""
        now = time.time()
        if self._cache and (now - self._last_fetch_ts) < self._cache_ttl:
            return self._cache

        try:
            session = await self._get_session()
            body = {"open_time": int(now), "lan": "zh-CN"}
            async with session.post(_BBX_URL, json=body) as resp:
                resp.raise_for_status()
                data = await resp.json()

            items = data.get("data", [])
            if not isinstance(items, list):
                logger.warning("BBX unexpected response type: %s", type(items).__name__)
                return self._cache

            self._cache = {}
            for item in items:
                key = item.get("key")
                if key:
                    self._cache[key] = item

            self._last_fetch_ts = now
            self._error_count = 0
            self._last_success_ts = now
            logger.info("BBX fetch OK | %d indices cached", len(self._cache))
        except Exception:
            self._error_count += 1
            logger.warning("BBX fetch failed (err_count=%d)", self._error_count, exc_info=True)

        return self._cache

    def get_float(self, key: str) -> Optional[float]:
        """获取指定 key 的 last 值（float），无数据返回 None。"""
        item = self._cache.get(key)
        if not item:
            return None
        raw = item.get("last")
        if raw is None or raw == "" or raw == "-":
            return None
        try:
            return float(raw)
        except (ValueError, TypeError):
            return None

    def get_change(self, key: str) -> Optional[float]:
        """获取指定 key 的绝对变化量。"""
        item = self._cache.get(key)
        if not item:
            return None
        raw = item.get("change")
        if raw is None or raw == "" or raw == "-":
            return None
        try:
            return float(raw)
        except (ValueError, TypeError):
            return None

    def get_change_pct(self, key: str) -> Optional[float]:
        """从 last 和 change 推算变化百分比。"""
        last = self.get_float(key)
        change = self.get_change(key)
        if last is None or change is None:
            return None
        prev = last - change
        if prev == 0:
            return None
        return round(change / prev * 100, 2)

    def health(self) -> dict:
        return {
            "name": "bbx",
            "status": "connected" if self._error_count == 0 and self._last_success_ts > 0 else (
                "degraded" if self._error_count < 5 else "disconnected"
            ),
            "cached_indices": len(self._cache),
            "last_success_ts": int(self._last_success_ts),
            "error_count": self._error_count,
        }
