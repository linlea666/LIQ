"""币安接口 HTTP 客户端。

职责边界：只负责"把请求安全地发出去并拿回结构化结果"，
不负责归档策略、不负责业务解析——那些由采集器决定，
避免客户端变成什么都管的上帝对象。

内建能力：
  - 请求前向调度器申请配额（三重约束在那边实现）。
  - 指数退避重试；429 单独走更长的退避并联动全局降速。
  - 同类错误聚合：连续失败只发一条 API_DEGRADED，恢复时补 API_RECOVERED，
    否则接口挂 10 分钟会产生几百条一模一样的告警把真正有价值的信息淹没。
  - 只记录端点/状态码/耗时/响应大小/哈希/条目数，**绝不把完整响应写日志**。
"""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import logging
import random
import time
from dataclasses import dataclass
from typing import Any

import aiohttp

from ..obs.events import EventType, Severity, bus
from ..obs.metrics import metrics
from ..scheduler import RequestScheduler
from .endpoints import DEFAULT_HEADERS, Endpoint
from .parsers import SchemaDrift, check_envelope

logger = logging.getLogger("radar.client")


@dataclass
class FetchResult:
    """一次成功请求的结果。"""

    endpoint: Endpoint
    chain_id: str | None
    data: Any                       # 已通过信封校验的 data 部分
    raw_text: str                   # 原始响应文本（供按策略归档）
    http_status: int
    latency_ms: int
    response_hash: str
    observed_at: int
    item_count: int = 0
    anomaly: str = ""               # 非空表示解析层发现异常，应强制归档

    @property
    def payload_size(self) -> int:
        return len(self.raw_text)

    def compressed_payload(self, *, max_bytes: int = 2_000_000) -> bytes | None:
        """gzip 压缩后的原始响应，供归档。超大响应直接放弃归档。"""
        if len(self.raw_text) > max_bytes:
            return None
        return gzip.compress(self.raw_text.encode("utf-8"), compresslevel=6)


class FetchError(Exception):
    """请求最终失败（已用尽重试）。"""

    def __init__(self, message: str, *, status: int = 0, rate_limited: bool = False) -> None:
        super().__init__(message)
        self.status = status
        self.rate_limited = rate_limited


class BinanceClient:
    """所有对外请求的唯一出口。"""

    def __init__(self, settings: Any, scheduler: RequestScheduler) -> None:
        cfg = settings.scheduler
        self._timeout = float(cfg.get("request_timeout_sec", 12))
        self._max_retries = int(cfg.get("max_retries", 2))
        self._backoff_base = float(cfg.get("retry_backoff_base_sec", 2.0))
        self._scheduler = scheduler
        self._session: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        # 连接池刻意开得很小：我们只访问单一主机且 rpm 约 72，
        # 大连接池只会白占内存（容器上限 512MB）。
        connector = aiohttp.TCPConnector(
            limit=8,
            limit_per_host=8,
            ttl_dns_cache=300,
            enable_cleanup_closed=True,
        )
        self._session = aiohttp.ClientSession(
            connector=connector,
            timeout=aiohttp.ClientTimeout(total=self._timeout),
            headers=DEFAULT_HEADERS,
        )

    async def stop(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None
            # aiohttp 需要一点时间关闭底层连接
            await asyncio.sleep(0.1)

    async def fetch(
        self,
        endpoint: Endpoint,
        *,
        chain_id: str | None = None,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        tier: str | None = None,
        budget_timeout_sec: float = 120.0,
    ) -> FetchResult:
        """发起一次请求。失败时抛 FetchError（已完成重试与事件上报）。"""
        if self._session is None:
            raise FetchError("HTTP 会话未初始化")

        tier_name = tier or endpoint.tier
        agg_key = f"{endpoint.name}:{chain_id or '-'}"

        if not await self._scheduler.acquire(tier_name, timeout_sec=budget_timeout_sec):
            # 拿不到配额不算接口故障，不进错误聚合，只计数
            metrics.incr("requests_skipped_no_budget")
            raise FetchError(f"{endpoint.name} 等待配额超时（层 {tier_name}）")

        last_error: FetchError | None = None
        for attempt in range(self._max_retries + 1):
            started = time.perf_counter()
            try:
                result = await self._attempt(endpoint, chain_id, params, body)
            except FetchError as exc:
                latency_ms = (time.perf_counter() - started) * 1000
                metrics.record_request(
                    endpoint.name, ok=False, latency_ms=latency_ms,
                    rate_limited=exc.rate_limited, error=str(exc),
                )
                last_error = exc
                if exc.rate_limited:
                    self._scheduler.record_rate_limit()
                if attempt >= self._max_retries:
                    break
                await asyncio.sleep(self._backoff_delay(attempt, exc.rate_limited))
                continue

            latency_ms = (time.perf_counter() - started) * 1000
            metrics.record_request(endpoint.name, ok=True, latency_ms=latency_ms)
            recovered, failures, elapsed = bus.errors.record_success(agg_key)
            if recovered:
                bus.emit(
                    EventType.API_RECOVERED,
                    module="collector",
                    chain_id=chain_id,
                    summary=f"{endpoint.name} 恢复，降级持续 {elapsed:.0f}s，期间失败 {failures} 次",
                    duration_ms=int(elapsed * 1000),
                    payload={
                        "endpoint": endpoint.name,
                        "total_failures": failures,
                        "duration_sec": round(elapsed, 1),
                    },
                )
            return result

        assert last_error is not None
        self._report_failure(endpoint, chain_id, agg_key, last_error)
        raise last_error

    def _backoff_delay(self, attempt: int, rate_limited: bool) -> float:
        """指数退避 + 抖动。429 用更激进的退避。"""
        base = self._backoff_base * (4.0 if rate_limited else 1.0)
        delay = base * (2 ** attempt)
        return min(60.0, delay * random.uniform(0.7, 1.3))

    def _report_failure(self, endpoint: Endpoint, chain_id: str | None,
                        agg_key: str, error: FetchError) -> None:
        should_emit, failures, elapsed = bus.errors.record_failure(agg_key)
        if not should_emit:
            # 已在降级窗口内，仅计数不再刷事件
            logger.debug("%s 持续失败（累计 %d 次）", agg_key, failures)
            return

        if error.rate_limited:
            bus.emit(
                EventType.API_RATE_LIMITED,
                module="collector",
                chain_id=chain_id,
                summary=f"{endpoint.name} 被限流（HTTP {error.status}）",
                payload={"endpoint": endpoint.name, "occurrences": failures},
            )
            return

        event_type = EventType.API_DEGRADED if failures > 1 else EventType.API_REQUEST_FAILED
        bus.emit(
            event_type,
            module="collector",
            chain_id=chain_id,
            summary=f"{endpoint.name} 请求失败：{error}",
            severity=Severity.ERROR if failures > 3 else None,
            payload={
                "endpoint": endpoint.name,
                "status": error.status,
                "occurrences": failures,
                "degraded_sec": round(elapsed, 1),
                "error": str(error)[:300],
            },
        )

    async def _attempt(
        self,
        endpoint: Endpoint,
        chain_id: str | None,
        params: dict[str, Any] | None,
        body: dict[str, Any] | None,
    ) -> FetchResult:
        assert self._session is not None
        observed_at = int(time.time() * 1000)
        started = time.perf_counter()

        try:
            if endpoint.method == "POST":
                ctx = self._session.post(endpoint.url, json=body or {})
            else:
                ctx = self._session.get(endpoint.url, params=self._stringify(params))
            async with ctx as response:
                status = response.status
                text = await response.text()
        except asyncio.TimeoutError as exc:
            raise FetchError(f"超时 {self._timeout}s") from exc
        except aiohttp.ClientError as exc:
            raise FetchError(f"{type(exc).__name__}: {exc}") from exc

        latency_ms = int((time.perf_counter() - started) * 1000)

        if status == 429 or status == 418:
            raise FetchError(f"HTTP {status} 限流", status=status, rate_limited=True)
        if status >= 400:
            # 只截取响应片段用于诊断，避免把整个响应写进日志/事件
            raise FetchError(f"HTTP {status}: {text[:200]}", status=status)

        try:
            payload = json.loads(text)
        except ValueError as exc:
            raise FetchError(f"响应非 JSON: {text[:120]}", status=status) from exc

        anomaly = ""
        try:
            data = check_envelope(payload)
        except SchemaDrift as exc:
            # 业务码错误（如参数非法）当作请求失败，但要留下 drift 线索
            raise FetchError(f"业务错误: {exc}", status=status) from exc

        item_count = len(data) if isinstance(data, list) else 0

        return FetchResult(
            endpoint=endpoint,
            chain_id=chain_id,
            data=data,
            raw_text=text,
            http_status=status,
            latency_ms=latency_ms,
            response_hash=hashlib.sha256(text.encode("utf-8")).hexdigest()[:16],
            observed_at=observed_at,
            item_count=item_count,
            anomaly=anomaly,
        )

    @staticmethod
    def _stringify(params: dict[str, Any] | None) -> dict[str, str] | None:
        """aiohttp 的 params 不接受 int/bool，统一转字符串并丢弃 None。"""
        if not params:
            return None
        return {k: str(v) for k, v in params.items() if v is not None}
