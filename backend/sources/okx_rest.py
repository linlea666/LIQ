"""OKX REST API 数据源：资金费率、OI、Taker Volume(CVD原料)、K线、标记/指数价"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

from config.settings import CoinConfig, get_settings
from models.flow import CVDPoint, FundingRateData, OISnapshot
from models.market import CandleData, OrderBookLevel, OrderBookSnapshot
from sources.base import DataSource

logger = logging.getLogger(__name__)


class OKXRestSource(DataSource):
    """OKX REST API 公开数据"""

    def __init__(self):
        cfg = get_settings().okx
        super().__init__(name="okx_rest", timeout_sec=cfg.timeout_sec)
        self._base = cfg.rest_base_url
        self._intervals = cfg.poll_intervals

    def get_poll_interval(self) -> int:
        return min(self._intervals.values())

    async def fetch(self, coin: CoinConfig) -> dict[str, Any]:
        """拉取全部 REST 数据，返回按类型分键的 dict"""
        return {}

    # ── 订单簿 (REST 替代 WS books50-l2-tbt) ─────────────────

    async def fetch_orderbook(self, coin: CoinConfig, size: int = 50) -> Optional[OrderBookSnapshot]:
        """获取订单簿快照，格式与 WS books50-l2-tbt 一致"""
        url = f"{self._base}/market/books?instId={coin.symbol_okx_swap}&sz={size}"
        try:
            t0 = time.time()
            data = await self._get_json(url)
            self._mark_success((time.time() - t0) * 1000)
            if data.get("code") != "0" or not data.get("data"):
                logger.warning("OKX orderbook empty | coin=%s", coin.ccy)
                return None
            book = data["data"][0]
            asks = [
                OrderBookLevel(
                    price=float(a[0]), size=float(a[1]),
                    order_count=int(a[3]) if len(a) > 3 else 0,
                )
                for a in book.get("asks", [])
            ]
            bids = [
                OrderBookLevel(
                    price=float(b[0]), size=float(b[1]),
                    order_count=int(b[3]) if len(b) > 3 else 0,
                )
                for b in book.get("bids", [])
            ]
            return OrderBookSnapshot(
                coin=coin.ccy,
                ts=int(book.get("ts", 0)),
                asks=asks,
                bids=bids,
                source="okx",
            )
        except Exception:
            self._mark_failure()
            logger.error("OKX orderbook failed | coin=%s", coin.ccy, exc_info=True)
            return None

    # ── 资金费率 ────────────────────────────────────────────

    async def fetch_funding_rate(self, coin: CoinConfig) -> Optional[FundingRateData]:
        url = f"{self._base}/public/funding-rate?instId={coin.symbol_okx_swap}"
        try:
            t0 = time.time()
            data = await self._get_json(url)
            self._mark_success((time.time() - t0) * 1000)
            if data.get("code") != "0" or not data.get("data"):
                logger.warning("OKX funding-rate empty | coin=%s", coin.ccy)
                return None
            item = data["data"][0]
            rate = float(item["fundingRate"])
            next_ts = int(item.get("nextFundingTime", 0))

            if abs(rate) > 0.0005:
                interp = "多头拥挤" if rate > 0 else "空头拥挤"
            elif abs(rate) > 0.0001:
                interp = "多头略挤" if rate > 0 else "空头略挤"
            else:
                interp = "中性"

            return FundingRateData(
                coin=coin.ccy,
                ts=int(time.time()),
                okx_rate=rate,
                avg_rate=rate,
                next_funding_ts=next_ts,
                interpretation=interp,
            )
        except Exception:
            self._mark_failure()
            logger.error("OKX funding-rate failed | coin=%s", coin.ccy, exc_info=True)
            return None

    # ── OI 未平仓 ──────────────────────────────────────────

    async def fetch_oi(self, coin: CoinConfig) -> Optional[OISnapshot]:
        url = f"{self._base}/public/open-interest?instType=SWAP&instId={coin.symbol_okx_swap}"
        try:
            t0 = time.time()
            data = await self._get_json(url)
            self._mark_success((time.time() - t0) * 1000)
            if data.get("code") != "0" or not data.get("data"):
                return None
            item = data["data"][0]
            return OISnapshot(
                coin=coin.ccy,
                ts=int(item["ts"]),
                oi=float(item["oi"]),
                oi_usd=float(item.get("oiUsd", 0)),
                source="okx",
            )
        except Exception:
            self._mark_failure()
            logger.error("OKX open-interest failed | coin=%s", coin.ccy, exc_info=True)
            return None

    # ── Taker Volume (CVD 原料) ─────────────────────────────

    async def fetch_taker_volume(self, coin: CoinConfig, inst_type: str = "CONTRACTS") -> list[CVDPoint]:
        url = (
            f"{self._base}/rubik/stat/taker-volume"
            f"?ccy={coin.ccy}&instType={inst_type}&period=5m"
        )
        try:
            t0 = time.time()
            data = await self._get_json(url)
            self._mark_success((time.time() - t0) * 1000)
            if data.get("code") != "0" or not data.get("data"):
                logger.warning("OKX taker-volume empty | coin=%s type=%s", coin.ccy, inst_type)
                return []

            raw_points = data["data"]
            raw_points.sort(key=lambda x: int(x[0]))

            cvd = 0.0
            result: list[CVDPoint] = []
            for row in raw_points:
                ts = int(row[0])
                buy_vol = float(row[1])
                sell_vol = float(row[2])
                delta = buy_vol - sell_vol
                cvd += delta
                result.append(CVDPoint(
                    ts=ts, buy_vol=buy_vol, sell_vol=sell_vol,
                    delta=delta, cvd=cvd,
                ))
            return result
        except Exception:
            self._mark_failure()
            logger.error("OKX taker-volume failed | coin=%s type=%s", coin.ccy, inst_type, exc_info=True)
            return []

    # ── K线 ────────────────────────────────────────────────

    async def fetch_candles(self, coin: CoinConfig, bar: str = "1H",
                            limit: int = 100) -> list[CandleData]:
        url = (
            f"{self._base}/market/candles"
            f"?instId={coin.symbol_okx_swap}&bar={bar}&limit={limit}"
        )
        try:
            t0 = time.time()
            data = await self._get_json(url)
            self._mark_success((time.time() - t0) * 1000)
            if data.get("code") != "0" or not data.get("data"):
                return []
            candles: list[CandleData] = []
            for row in data["data"]:
                candles.append(CandleData(
                    coin=coin.ccy,
                    ts=int(row[0]),
                    o=float(row[1]),
                    h=float(row[2]),
                    l=float(row[3]),
                    c=float(row[4]),
                    vol=float(row[5]),
                    vol_ccy=float(row[6]),
                ))
            candles.sort(key=lambda c: c.ts)
            return candles
        except Exception:
            self._mark_failure()
            logger.error("OKX candles failed | coin=%s bar=%s", coin.ccy, bar, exc_info=True)
            return []

    # ── 标记价 + 指数价 ─────────────────────────────────────

    async def fetch_mark_price(self, coin: CoinConfig) -> Optional[float]:
        url = f"{self._base}/public/mark-price?instType=SWAP&instId={coin.symbol_okx_swap}"
        try:
            t0 = time.time()
            data = await self._get_json(url)
            self._mark_success((time.time() - t0) * 1000)
            if data.get("code") == "0" and data.get("data"):
                return float(data["data"][0]["markPx"])
        except Exception:
            self._mark_failure()
            logger.error("OKX mark-price failed | coin=%s", coin.ccy, exc_info=True)
        return None

    async def fetch_index_price(self, coin: CoinConfig) -> Optional[float]:
        url = f"{self._base}/market/index-tickers?instId={coin.symbol_okx_spot}"
        try:
            t0 = time.time()
            data = await self._get_json(url)
            self._mark_success((time.time() - t0) * 1000)
            if data.get("code") == "0" and data.get("data"):
                return float(data["data"][0]["idxPx"])
        except Exception:
            self._mark_failure()
            logger.error("OKX index-price failed | coin=%s", coin.ccy, exc_info=True)
        return None

    # ── 爆仓数据 (REST 补充) ────────────────────────────────

    async def fetch_liquidations(self, coin: CoinConfig) -> list[dict]:
        url = (
            f"{self._base}/public/liquidation-orders"
            f"?instType=SWAP&instFamily={coin.inst_family}&state=filled&limit=100"
        )
        try:
            data = await self._get_json(url)
            if data.get("code") != "0" or not data.get("data"):
                return []
            events = []
            for batch in data["data"]:
                for detail in batch.get("details", []):
                    events.append({
                        "coin": coin.ccy,
                        "ts": int(detail.get("ts", 0)),
                        "side": detail.get("posSide", ""),
                        "price": float(detail.get("bkPx", 0)),
                        "size": float(detail.get("sz", 0)),
                    })
            return events
        except Exception:
            logger.error("OKX liquidations failed | coin=%s", coin.ccy, exc_info=True)
            return []

    # ── Ticker ──────────────────────────────────────────────

    async def fetch_ticker(self, coin: CoinConfig) -> Optional[dict]:
        url = f"{self._base}/market/ticker?instId={coin.symbol_okx_swap}"
        try:
            data = await self._get_json(url)
            if data.get("code") == "0" and data.get("data"):
                t = data["data"][0]
                last = float(t["last"])
                open24 = float(t.get("open24h", last))
                return {
                    "coin": coin.ccy,
                    "ts": int(t["ts"]),
                    "last": last,
                    "high_24h": float(t.get("high24h", last)),
                    "low_24h": float(t.get("low24h", last)),
                    "vol_24h": float(t.get("vol24h", 0)),
                    "change_24h": last - open24,
                    "change_pct_24h": ((last - open24) / open24 * 100) if open24 else 0,
                }
        except Exception:
            logger.error("OKX ticker failed | coin=%s", coin.ccy, exc_info=True)
        return None


def create_okx_rest_source() -> OKXRestSource:
    return OKXRestSource()
