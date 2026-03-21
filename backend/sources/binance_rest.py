"""Binance REST API 数据源：资金费率、OI、Taker买卖比、爆仓"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

from config.settings import CoinConfig, get_settings
from models.flow import FundingRateData, OISnapshot
from sources.base import DataSource

logger = logging.getLogger(__name__)


class BinanceRestSource(DataSource):
    """Binance Futures 公开 REST API"""

    def __init__(self):
        cfg = get_settings().binance
        super().__init__(name="binance_rest", timeout_sec=cfg.timeout_sec)
        self._base = cfg.rest_base_url
        self._enabled = cfg.enabled
        self._intervals = cfg.poll_intervals

    def get_poll_interval(self) -> int:
        return min(self._intervals.values()) if self._intervals else 10

    async def fetch(self, coin: CoinConfig) -> dict[str, Any]:
        return {}

    async def fetch_funding_rate(self, coin: CoinConfig) -> Optional[float]:
        """获取 Binance 当前资金费率"""
        if not self._enabled:
            return None
        url = f"{self._base}/fapi/v1/premiumIndex?symbol={coin.symbol_binance}"
        try:
            data = await self._get_json(url)
            if isinstance(data, dict) and "lastFundingRate" in data:
                return float(data["lastFundingRate"])
            if isinstance(data, dict) and data.get("code"):
                logger.warning(
                    "Binance funding-rate blocked | coin=%s msg=%s",
                    coin.ccy, data.get("msg", ""),
                )
            return None
        except Exception:
            logger.error("Binance funding-rate failed | coin=%s", coin.ccy, exc_info=True)
            return None

    async def fetch_oi(self, coin: CoinConfig) -> Optional[OISnapshot]:
        if not self._enabled:
            return None
        url = f"{self._base}/fapi/v1/openInterest?symbol={coin.symbol_binance}"
        try:
            data = await self._get_json(url)
            if isinstance(data, dict) and "openInterest" in data:
                oi = float(data["openInterest"])
                return OISnapshot(
                    coin=coin.ccy,
                    ts=int(data.get("time", time.time() * 1000)),
                    oi=oi,
                    oi_usd=0,
                    source="binance",
                )
            return None
        except Exception:
            logger.error("Binance OI failed | coin=%s", coin.ccy, exc_info=True)
            return None

    async def fetch_taker_ratio(self, coin: CoinConfig, limit: int = 30) -> list[dict]:
        """Taker买卖比历史"""
        if not self._enabled:
            return []
        url = (
            f"{self._base}/futures/data/takerlongshortRatio"
            f"?symbol={coin.symbol_binance}&period=5m&limit={limit}"
        )
        try:
            data = await self._get_json(url)
            if isinstance(data, list):
                return data
            return []
        except Exception:
            logger.error("Binance taker-ratio failed | coin=%s", coin.ccy, exc_info=True)
            return []

    async def fetch_force_orders(self, coin: CoinConfig, limit: int = 50) -> list[dict]:
        """最近爆仓订单"""
        if not self._enabled:
            return []
        url = (
            f"{self._base}/fapi/v1/allForceOrders"
            f"?symbol={coin.symbol_binance}&limit={limit}"
        )
        try:
            data = await self._get_json(url)
            if isinstance(data, list):
                return data
            return []
        except Exception:
            logger.error("Binance force-orders failed | coin=%s", coin.ccy, exc_info=True)
            return []

    async def fetch_depth(self, coin: CoinConfig, limit: int = 20) -> Optional[dict]:
        if not self._enabled:
            return None
        url = f"{self._base}/fapi/v1/depth?symbol={coin.symbol_binance}&limit={limit}"
        try:
            data = await self._get_json(url)
            if isinstance(data, dict) and "bids" in data:
                return data
            return None
        except Exception:
            logger.error("Binance depth failed | coin=%s", coin.ccy, exc_info=True)
            return None


def create_binance_rest_source() -> BinanceRestSource:
    return BinanceRestSource()
