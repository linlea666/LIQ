"""Coinbase Exchange 原生现货订单簿数据源（Phase C）。

为何独立而不走 Coinglass：
    Coinglass 的 /api/spot/orderbook/* 端点对 Coinbase 完全 400/500（probe 验证）。
    Coinbase Exchange 公开 REST 不需要 auth、限速宽松（10/s = 600/min），直接拉是
    最低成本路径。本源仅用于 Liquidity Wall Engine 的 Coinbase 公开现货验证维度，
    不推断挂单人身份、ETF 行为或机构意图。

定位：
    - 不替代 Coinglass：聚合 ask-bids、热力图、Binance lifecycle 仍走 Coinglass
    - 唯一用途：在 WallZone 上叠加 Coinbase USD 厚度 → coinbase_spot_confluence
    - 速率上限远高于实际用量（4 币 × 1/90s ≈ 0.04 req/s vs limit 10/s）

设计与 base.DataSource 风格一致：
    - 继承 DataSource（复用 health 跟踪 / session 复用）
    - aiohttp.ClientSession(trust_env=False) 防 macOS SOCKS 代理污染
    - 内置 FixedIntervalLimiter（默认 60/min ≈ 1/s，留 10× 余量）
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

import aiohttp

from config.settings import CoinConfig
from models.coinbase_orderbook import CoinbaseBookLevel, CoinbaseOrderbookFrame
from sources.base import DataSource

logger = logging.getLogger(__name__)


class _SimpleRateLimiter:
    """固定间隔限流器（与 coinglass.FixedIntervalLimiter 同语义，独立实例不共用）。

    Coinbase 公开 limit 10 req/s = 600/min。我们设 60/min（1s 间隔）已留 10× 余量。
    避免依赖 sources.coinglass.FixedIntervalLimiter（防止职责耦合）。
    """

    def __init__(self, rate_per_min: int = 60):
        self._min_interval = 60.0 / max(rate_per_min, 1)
        self._last_request = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self):
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_request
            if elapsed < self._min_interval:
                await asyncio.sleep(self._min_interval - elapsed)
            self._last_request = time.monotonic()


class CoinbaseNativeSource(DataSource):
    """Coinbase Exchange 公开 REST 客户端（仅 orderbook，免 auth）。"""

    DEFAULT_BASE_URL = "https://api.exchange.coinbase.com"

    def __init__(self, base_url: str = DEFAULT_BASE_URL, timeout_sec: int = 15,
                 rate_per_min: int = 60):
        super().__init__(name="coinbase_native", timeout_sec=timeout_sec, max_retries=2)
        self._base_url = base_url.rstrip("/")
        self._limiter = _SimpleRateLimiter(rate_per_min)

    async def get_session(self) -> aiohttp.ClientSession:
        """复用 base.get_session，但显式 trust_env=False 防 macOS SOCKS 代理。"""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                trust_env=False,
                timeout=aiohttp.ClientTimeout(total=self.timeout_sec),
            )
        return self._session

    def get_poll_interval(self) -> int:
        return 90

    async def fetch(self, coin: CoinConfig) -> Any:
        return None

    async def fetch_orderbook(self, product_id: str,
                              level: int = 2) -> Optional[dict]:
        """拉 Coinbase Exchange 原生 orderbook。

        Args:
            product_id: 如 "BTC-USD"
            level: 1=top1, 2=full aggregated (default), 3=full unaggregated
                   level=2 已能给完整聚合深度（22k+ 档），不需要 level=3。

        Returns:
            原始响应 dict，含 keys: bids, asks, sequence, time, auction_mode, auction
            失败返回 None（HTTP/JSON/超时全部归零，不抛异常给上层）。
        """
        if not product_id:
            return None
        await self._limiter.acquire()

        url = f"{self._base_url}/products/{product_id}/book"
        params = {"level": str(level)}

        session = await self.get_session()
        t0 = time.time()

        # W1-T1：所有出口统一上报（best-effort）
        def _record_metric(latency_ms: float, ok: bool) -> None:
            try:
                from processors.liquidity_wall_metrics import get_metrics
                get_metrics().record_coinbase_call(latency_ms, ok)
            except Exception:
                pass

        try:
            async with session.get(url, params=params) as resp:
                latency = (time.time() - t0) * 1000
                if resp.status == 429:
                    logger.warning("Coinbase 429 rate limited | product=%s", product_id)
                    self._mark_failure()
                    _record_metric(latency, ok=False)
                    await asyncio.sleep(5)
                    return None
                if resp.status == 404:
                    # product 不存在（如 SUI-USD 不存在）→ 静默返回 None，由调用方决定
                    logger.warning("Coinbase product not found | product=%s", product_id)
                    self._mark_failure()
                    _record_metric(latency, ok=False)
                    return None
                resp.raise_for_status()
                data = await resp.json()
                self._mark_success(latency)
                _record_metric(latency, ok=True)
                return data
        except asyncio.TimeoutError:
            self._mark_failure()
            _record_metric((time.time() - t0) * 1000, ok=False)
            logger.warning("Coinbase timeout | product=%s timeout=%ss",
                           product_id, self.timeout_sec)
            return None
        except aiohttp.ClientResponseError as e:
            self._mark_failure()
            _record_metric((time.time() - t0) * 1000, ok=False)
            logger.error("Coinbase HTTP %d | product=%s | %s", e.status, product_id, str(e))
            return None
        except Exception as e:
            self._mark_failure()
            _record_metric((time.time() - t0) * 1000, ok=False)
            logger.error("Coinbase request failed | product=%s | %s",
                         product_id, str(e), exc_info=True)
            return None


def parse_orderbook_frame(coin: str, product_id: str,
                          raw: dict) -> Optional[CoinbaseOrderbookFrame]:
    """解析 Coinbase 原始响应 → CoinbaseOrderbookFrame。

    解析失败返回 None；遇到部分异常档位（无法转 float / num_orders 非数字）跳过该档。

    单测覆盖：
      - bids/asks 升序（Coinbase 默认 best-first，这里改为升序与项目其他模型对齐）
      - num_orders 缺失时默认 1
      - 字符串价格/数量转 float
    """
    if not isinstance(raw, dict):
        return None

    bids_raw = raw.get("bids")
    asks_raw = raw.get("asks")
    if not isinstance(bids_raw, list) or not isinstance(asks_raw, list):
        return None

    def _parse_levels(items: list) -> list[CoinbaseBookLevel]:
        out: list[CoinbaseBookLevel] = []
        for entry in items:
            if not isinstance(entry, (list, tuple)) or len(entry) < 2:
                continue
            try:
                price = float(entry[0])
                size = float(entry[1])
                num_orders = int(entry[2]) if len(entry) >= 3 else 1
            except (TypeError, ValueError):
                continue
            if price <= 0 or size <= 0:
                continue
            out.append(CoinbaseBookLevel(
                price=price, size=size, num_orders=max(num_orders, 1),
            ))
        out.sort(key=lambda b: b.price)
        return out

    bids = _parse_levels(bids_raw)
    asks = _parse_levels(asks_raw)
    if not bids and not asks:
        return None

    seq_raw = raw.get("sequence")
    sequence: Optional[int] = None
    try:
        sequence = int(seq_raw) if seq_raw is not None else None
    except (TypeError, ValueError):
        sequence = None

    return CoinbaseOrderbookFrame(
        coin=coin,
        product_id=product_id,
        ts_sec=int(time.time()),
        api_ts_iso=str(raw.get("time", "")),
        sequence=sequence,
        bids=bids,
        asks=asks,
        bid_count=len(bids),
        ask_count=len(asks),
    )


def create_coinbase_native_source() -> CoinbaseNativeSource:
    """工厂方法：默认参数下创建 Coinbase 原生源。"""
    return CoinbaseNativeSource()
