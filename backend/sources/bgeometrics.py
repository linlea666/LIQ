"""BGeometrics（bitcoin-data.com）链上指标源 · 仅供 Bottom Model 模块。

免费档硬限 **8 次/小时、15 次/天**，返回近 4 年日级历史（T-1 更新）。
配额极其珍贵，因此本源的设计纪律：

1. 双滑动窗口限流（hour/day）在客户端强制执行——超限直接返回 None，
   绝不发出必然 429 的请求（429 也计入服务端配额）。
2. 不做自动重试（重试同样消耗配额）；失败由采集器按日期戳判断次日补拉。
3. fail-open：任何失败只影响对应指标缺失，绝不抛出到调用方。

响应格式（实测 2026-08）：
    GET /v1/{endpoint}       → [{"d": "YYYY-MM-DD", "unixTs": 1660262400, "<驼峰指标名>": 0.18}, ...]
    GET /v1/{endpoint}/last  → {"d": ..., "unixTs": ..., "<驼峰指标名>": ...}
指标值字段名各端点不同（mvrvZscore / sthMvrv / realizedLoss ...），
解析时取除 d/unixTs 外唯一的数值字段，避免逐端点硬编码字段名。
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import deque
from typing import Any, Optional

from config.settings import BGeometricsSourceConfig
from sources.base import DataSource

logger = logging.getLogger(__name__)

_META_KEYS = frozenset({"d", "unixTs"})


def parse_bg_rows(rows: Any) -> list[tuple[str, float]]:
    """把 BGeometrics 日级数组解析为 [(day, value), ...]（day 为 YYYY-MM-DD）。

    值字段取除 d/unixTs 外唯一的数值字段；无法解析的行静默跳过。
    """
    if not isinstance(rows, list):
        return []
    out: list[tuple[str, float]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        day = row.get("d")
        if not isinstance(day, str) or len(day) != 10:
            continue
        value: Optional[float] = None
        for key, raw in row.items():
            if key in _META_KEYS:
                continue
            try:
                value = float(raw)
            except (TypeError, ValueError):
                continue
            break
        if value is None:
            continue
        out.append((day, value))
    out.sort(key=lambda item: item[0])
    return out


class _DualWindowLimiter:
    """严格滚动 1h/24h 双窗口限额；到限即拒绝（不排队）。"""

    def __init__(self, hourly_limit: int, daily_limit: int):
        self._hourly_limit = max(1, int(hourly_limit))
        self._daily_limit = max(1, int(daily_limit))
        self._events: deque[float] = deque()

    def _prune(self, now: float) -> None:
        while self._events and now - self._events[0] >= 86400.0:
            self._events.popleft()

    def try_acquire(self) -> bool:
        now = time.monotonic()
        self._prune(now)
        recent_hour = sum(1 for ts in self._events if now - ts < 3600.0)
        if recent_hour >= self._hourly_limit or len(self._events) >= self._daily_limit:
            return False
        self._events.append(now)
        return True

    def snapshot(self) -> dict[str, int]:
        now = time.monotonic()
        self._prune(now)
        recent_hour = sum(1 for ts in self._events if now - ts < 3600.0)
        return {
            "hourly_used": recent_hour,
            "hourly_limit": self._hourly_limit,
            "daily_used": len(self._events),
            "daily_limit": self._daily_limit,
        }


class BGeometricsSource(DataSource):
    def __init__(self, cfg: BGeometricsSourceConfig):
        super().__init__("bgeometrics", cfg.timeout_sec, max_retries=1)
        self._base_url = cfg.base_url.rstrip("/")
        self._api_key = (os.getenv(cfg.api_key_env, "") or "").strip()
        self._limiter = _DualWindowLimiter(cfg.hourly_limit, cfg.daily_limit)
        self._fetch_lock = asyncio.Lock()
        self.last_error = ""

    def get_poll_interval(self) -> int:
        return 86400

    async def fetch(self, coin) -> None:
        return None

    def quota_snapshot(self) -> dict[str, int]:
        return self._limiter.snapshot()

    async def fetch_metric_series(self, endpoint: str) -> Optional[list[tuple[str, float]]]:
        """拉取一个指标的完整可用历史（近 4 年日级）。

        返回升序 [(day, value), ...]；配额耗尽或请求失败返回 None。
        """
        raw = await self._request(f"/v1/{endpoint.strip('/')}")
        if raw is None:
            return None
        parsed = parse_bg_rows(raw)
        if not parsed:
            self.last_error = f"{endpoint}: empty_or_unparsable"
            logger.warning("BGeometrics %s returned unparsable payload", endpoint)
            return None
        return parsed

    async def _request(self, path: str) -> Optional[Any]:
        # 限流检查与请求发出之间用锁串行化，避免并发调用双双通过检查。
        async with self._fetch_lock:
            if not self._limiter.try_acquire():
                self.last_error = "quota_exhausted"
                logger.warning(
                    "BGeometrics quota exhausted, refusing request | path=%s quota=%s",
                    path, self._limiter.snapshot(),
                )
                return None
            headers = {"User-Agent": "LIQ-bottom-model/1.0"}
            if self._api_key:
                headers["Authorization"] = f"Bearer {self._api_key}"
            url = f"{self._base_url}{path}"
            started = time.monotonic()
            try:
                session = await self.get_session()
                async with session.get(url, headers=headers) as resp:
                    if resp.status == 429:
                        self._mark_failure()
                        self.last_error = "http_429"
                        logger.warning("BGeometrics 429 rate limited | path=%s", path)
                        return None
                    resp.raise_for_status()
                    data = await resp.json(content_type=None)
                self._mark_success((time.monotonic() - started) * 1000)
                self.last_error = ""
                return data
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._mark_failure()
                self.last_error = f"{type(exc).__name__}: {exc}"
                logger.warning("BGeometrics request failed | path=%s err=%s", path, self.last_error)
                return None


def create_bgeometrics_source(cfg: BGeometricsSourceConfig) -> Optional[BGeometricsSource]:
    return BGeometricsSource(cfg) if cfg.enabled else None
