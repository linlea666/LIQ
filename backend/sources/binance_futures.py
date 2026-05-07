"""Binance USD-M Futures 公共数据源（第一档迁移：ticker/klines/basis）。"""

from __future__ import annotations

import asyncio
import logging
import json
import time
from collections.abc import AsyncIterator
from typing import Any, Optional

import aiohttp

# WS 读超时：aiohttp heartbeat=30s 之外再加一层应用级 read timeout。
# 触发场景：TCP 半开（pong 写不出 / 收不到 close 帧）时，async for 会无限阻塞，
# 历史曾出现 52min 静默 + ClientConnectionResetError。45s 留足容错（>= heartbeat 30s + 余量）。
_WS_READ_TIMEOUT_SEC = 45

from sources.base import DataSource

logger = logging.getLogger(__name__)


class BinanceFuturesSource(DataSource):
    """轻量 Binance Futures 客户端，仅封装本次迁移所需接口。"""

    def __init__(
        self,
        base_url: str,
        timeout_sec: int = 10,
        ws_url: str = "wss://fstream.binance.com/ws/!ticker@arr",
    ):
        super().__init__(name="binance_futures", timeout_sec=timeout_sec, max_retries=2)
        self._base_url = base_url.rstrip("/")
        self._ws_url = ws_url

    def get_poll_interval(self) -> int:
        return 1

    async def fetch(self, coin) -> Any:
        return None

    async def _request(self, path: str, params: Optional[dict] = None) -> Optional[Any]:
        session = await self.get_session()
        url = f"{self._base_url}{path}"
        t0 = time.time()
        try:
            async with session.get(url, params=params) as resp:
                resp.raise_for_status()
                data = await resp.json()
                self._mark_success((time.time() - t0) * 1000)
                return data
        except aiohttp.ClientResponseError:
            self._mark_failure()
            logger.error("Binance HTTP error | path=%s", path, exc_info=True)
            return None
        except Exception:
            self._mark_failure()
            logger.error("Binance request failed | path=%s", path, exc_info=True)
            return None

    async def fetch_tickers_24h(self) -> Optional[list]:
        """全量 24h ticker（含高低、涨跌、成交额）。"""
        data = await self._request("/fapi/v1/ticker/24hr")
        return data if isinstance(data, list) else None

    async def fetch_klines(
        self, symbol: str, interval: str = "1h", limit: int = 200,
        start_time: Optional[int] = None, end_time: Optional[int] = None,
    ) -> Optional[list]:
        """K线数组格式：[openTime, open, high, low, close, volume, closeTime, ...]
        start_time/end_time: 毫秒时间戳（可选，用于历史回查）。
        """
        params: dict = {"symbol": symbol, "interval": interval, "limit": limit}
        if start_time is not None:
            params["startTime"] = start_time
        if end_time is not None:
            params["endTime"] = end_time
        data = await self._request("/fapi/v1/klines", params)
        return data if isinstance(data, list) else None

    async def fetch_premium_index(self, symbol: str) -> Optional[dict]:
        """返回 markPrice / indexPrice，用于本地 basis 计算。"""
        data = await self._request("/fapi/v1/premiumIndex", {"symbol": symbol})
        return data if isinstance(data, dict) else None

    async def stream_tickers(
        self, watched_symbols: set[str] | None = None,
    ) -> AsyncIterator[list[dict]]:
        """订阅 Binance !ticker@arr 实时流，产出简化后的 ticker 事件列表。

        健壮性：
            - aiohttp 的 heartbeat=30 每 30s 发 ping，对端无响应才认连接死
            - 额外加 _WS_READ_TIMEOUT_SEC 应用级读超时，覆盖 TCP 半开场景：
              `async for msg in ws` 在半开连接下不会抛错也不会返回，只是
              永远阻塞——这是历史"52min 静默 + ClientConnectionResetError"
              的根因。read 超时后主动 break，由调用方触发重连
        """
        session = await self.get_session()
        try:
            async with session.ws_connect(self._ws_url, heartbeat=30) as ws:
                logger.info("Binance WS connected | url=%s", self._ws_url)
                while True:
                    try:
                        msg = await asyncio.wait_for(
                            ws.receive(), timeout=_WS_READ_TIMEOUT_SEC,
                        )
                    except asyncio.TimeoutError:
                        self._mark_failure()
                        logger.warning(
                            "Binance WS receive timeout (%ds) | forcing reconnect",
                            _WS_READ_TIMEOUT_SEC,
                        )
                        break
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        payload = json.loads(msg.data)
                        rows = payload.get("data") if isinstance(payload, dict) else payload
                        if not isinstance(rows, list):
                            continue
                        events = []
                        for item in rows:
                            if not isinstance(item, dict):
                                continue
                            symbol = str(item.get("s", item.get("symbol", "")))
                            if not symbol:
                                continue
                            if watched_symbols and symbol not in watched_symbols:
                                continue
                            events.append(item)
                        if events:
                            self._mark_success()
                            yield events
                    elif msg.type in (
                        aiohttp.WSMsgType.CLOSED,
                        aiohttp.WSMsgType.CLOSING,
                        aiohttp.WSMsgType.ERROR,
                    ):
                        self._mark_failure()
                        break
        except Exception:
            self._mark_failure()
            logger.warning("Binance WS stream failed", exc_info=True)
            raise


def create_binance_source() -> BinanceFuturesSource:
    from config.settings import get_settings

    cfg = get_settings().binance
    return BinanceFuturesSource(
        base_url=cfg.base_url,
        timeout_sec=cfg.timeout_sec,
        ws_url=cfg.ws_url,
    )
