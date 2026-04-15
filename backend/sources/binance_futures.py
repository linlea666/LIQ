"""Binance USD-M Futures 公共数据源（第一档迁移：ticker/klines/basis）。"""

from __future__ import annotations

import logging
from typing import Any, Optional

import aiohttp

from sources.base import DataSource

logger = logging.getLogger(__name__)


class BinanceFuturesSource(DataSource):
    """轻量 Binance Futures 客户端，仅封装本次迁移所需接口。"""

    def __init__(self, base_url: str, timeout_sec: int = 10):
        super().__init__(name="binance_futures", timeout_sec=timeout_sec, max_retries=2)
        self._base_url = base_url.rstrip("/")

    def get_poll_interval(self) -> int:
        return 1

    async def fetch(self, coin) -> Any:
        return None

    async def _request(self, path: str, params: Optional[dict] = None) -> Optional[Any]:
        session = await self.get_session()
        url = f"{self._base_url}{path}"
        try:
            async with session.get(url, params=params) as resp:
                resp.raise_for_status()
                return await resp.json()
        except aiohttp.ClientResponseError:
            logger.error("Binance HTTP error | path=%s", path, exc_info=True)
            return None
        except Exception:
            logger.error("Binance request failed | path=%s", path, exc_info=True)
            return None

    async def fetch_tickers_24h(self) -> Optional[list]:
        """全量 24h ticker（含高低、涨跌、成交额）。"""
        data = await self._request("/fapi/v1/ticker/24hr")
        return data if isinstance(data, list) else None

    async def fetch_klines(self, symbol: str, interval: str = "1h", limit: int = 200) -> Optional[list]:
        """K线数组格式：[openTime, open, high, low, close, volume, closeTime, ...]"""
        data = await self._request("/fapi/v1/klines", {
            "symbol": symbol,
            "interval": interval,
            "limit": limit,
        })
        return data if isinstance(data, list) else None

    async def fetch_premium_index(self, symbol: str) -> Optional[dict]:
        """返回 markPrice / indexPrice，用于本地 basis 计算。"""
        data = await self._request("/fapi/v1/premiumIndex", {"symbol": symbol})
        return data if isinstance(data, dict) else None


def create_binance_source() -> BinanceFuturesSource:
    from config.settings import get_settings

    cfg = get_settings().binance
    return BinanceFuturesSource(
        base_url=cfg.base_url,
        timeout_sec=cfg.timeout_sec,
    )
