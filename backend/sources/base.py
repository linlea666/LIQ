"""数据源基类：定义标准接口、重试逻辑、健康状态跟踪"""

from __future__ import annotations

import logging
import time
import traceback
from abc import ABC, abstractmethod
from typing import Any, Optional

import aiohttp

from config.settings import CoinConfig
from models.snapshot import SourceHealth

logger = logging.getLogger(__name__)


class DataSource(ABC):
    """所有数据源的基类"""

    def __init__(self, name: str, timeout_sec: int = 10, max_retries: int = 3):
        self.name = name
        self.timeout_sec = timeout_sec
        self.max_retries = max_retries
        self._error_count = 0
        self._last_success_ts = 0
        self._last_latency_ms: float = 0
        self._session: Optional[aiohttp.ClientSession] = None

    async def get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout_sec)
            )
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    @abstractmethod
    async def fetch(self, coin: CoinConfig) -> Any:
        ...

    @abstractmethod
    def get_poll_interval(self) -> int:
        ...

    def _mark_success(self, latency_ms: float = 0):
        self._error_count = 0
        self._last_success_ts = int(time.time())
        if latency_ms > 0:
            self._last_latency_ms = latency_ms

    def _mark_failure(self):
        self._error_count += 1

    def health(self) -> SourceHealth:
        if self._error_count == 0 and self._last_success_ts > 0:
            status = "connected"
        elif self._error_count < 5:
            status = "degraded"
        else:
            status = "disconnected"
        return SourceHealth(
            name=self.name,
            status=status,
            latency_ms=self._last_latency_ms,
            last_success_ts=self._last_success_ts,
            error_count=self._error_count,
        )

    async def fetch_with_retry(self, coin: CoinConfig) -> Optional[Any]:
        """带指数退避重试的 fetch 包装"""
        for attempt in range(1, self.max_retries + 1):
            t0 = time.time()
            try:
                result = await self.fetch(coin)
                elapsed_ms = (time.time() - t0) * 1000
                self._last_latency_ms = elapsed_ms
                self._last_success_ts = int(time.time())
                self._error_count = 0
                logger.info(
                    "%s fetch OK | coin=%s | latency=%.0fms",
                    self.name, coin.ccy, elapsed_ms,
                )
                return result
            except Exception as e:
                elapsed_ms = (time.time() - t0) * 1000
                self._error_count += 1
                wait = 2 ** attempt
                logger.warning(
                    "%s fetch FAIL | coin=%s | attempt=%d/%d | latency=%.0fms | err=%s",
                    self.name, coin.ccy, attempt, self.max_retries, elapsed_ms, str(e),
                )
                if attempt < self.max_retries:
                    import asyncio
                    await asyncio.sleep(wait)
                else:
                    logger.error(
                        "%s fetch EXHAUSTED | coin=%s | errors=%d\n%s",
                        self.name, coin.ccy, self._error_count, traceback.format_exc(),
                    )
        return None

    async def _get_json(self, url: str, method: str = "GET",
                        json_body: Optional[dict] = None,
                        headers: Optional[dict] = None) -> dict:
        """通用 HTTP JSON 请求"""
        session = await self.get_session()
        hdrs = {"Content-Type": "application/json"}
        if headers:
            hdrs.update(headers)

        if method.upper() == "POST":
            async with session.post(url, json=json_body, headers=hdrs) as resp:
                resp.raise_for_status()
                return await resp.json()
        else:
            async with session.get(url, headers=hdrs) as resp:
                resp.raise_for_status()
                return await resp.json()
