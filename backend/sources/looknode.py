"""LookNode 链上周期数据源：200W SMA / MVRV Z / STH Cost / Ahr999 / Pi Cycle / CVDD / BTC Price

数据全部为 BTC 日频，通过本地 JSON 文件缓存避免高频请求被封。
分批串行请求（可配间隔），单个失败不阻塞，fallback 上次缓存。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any, Optional

import aiohttp

from config.settings import get_settings
from models.flow import OnchainCycleData
from sources.base import CoinConfig, DataSource

logger = logging.getLogger(__name__)

_BACKEND_DIR = Path(__file__).resolve().parent.parent

_ENDPOINTS: dict[str, str] = {
    "btc_price": "/BTCPrice",
    "sma_200w": "/twoHanWeekSMA",
    "mvrv_z": "/MVRVZ",
    "sth_cost": "/sthCostPrice",
    "ahr999": "/Ahr999",
    "pi_cycle": "/getPiCircle",
    "cvdd": "/CVDD",
}


class LookNodeSource(DataSource):
    """LookNode 链上指标数据源（分批请求 + 本地 JSON 缓存）"""

    def __init__(self):
        cfg = get_settings().looknode
        super().__init__(name="looknode", timeout_sec=cfg.timeout_sec, max_retries=2)
        self._base_url = cfg.base_url
        self._gap_sec = cfg.request_gap_sec
        self._cache_dir = _BACKEND_DIR / cfg.cache_dir
        self._cache_ttl = cfg.cache_ttl_sec
        self._poll_interval = cfg.poll_interval_sec
        self._cache_dir.mkdir(parents=True, exist_ok=True)

    def get_poll_interval(self) -> int:
        return self._poll_interval

    async def fetch(self, coin: CoinConfig) -> Any:
        return await self.fetch_all()

    async def fetch_all(self, force: bool = False) -> Optional[OnchainCycleData]:
        """分批拉取所有 LookNode 指标。

        - force=True 忽略缓存 TTL 强制刷新
        - 单个 API 失败 fallback 上次缓存
        - 请求间隔 gap_sec 防封
        """
        results: dict[str, Optional[list[dict]]] = {}

        group_main = ["btc_price", "sma_200w", "mvrv_z", "sth_cost", "ahr999", "pi_cycle"]
        for key in group_main:
            results[key] = await self._fetch_single(key, force=force)
            if results[key] is not None:
                await asyncio.sleep(self._gap_sec)

        results["cvdd"] = await self._fetch_single("cvdd", force=force, timeout_override=20)

        return self._assemble(results)

    # ── 单指标拉取 ──

    async def _fetch_single(
        self,
        key: str,
        force: bool = False,
        timeout_override: int = 0,
    ) -> Optional[list[dict]]:
        if not force:
            cached = self._read_cache(key)
            if cached is not None:
                return cached

        url = self._base_url + _ENDPOINTS[key]
        try:
            session = await self.get_session()
            req_timeout = aiohttp.ClientTimeout(total=timeout_override or self.timeout_sec)
            async with session.get(url, timeout=req_timeout) as resp:
                resp.raise_for_status()
                raw = await resp.json(content_type=None)

            rows = raw.get("data", [])
            if not rows:
                logger.warning("LookNode %s returned empty data", key)
                return self._read_cache(key, ignore_ttl=True)

            keep = 20 if key == "btc_price" else 3
            to_cache = rows[-keep:]
            self._write_cache(key, to_cache)
            self._mark_success()
            logger.info("LookNode %s OK | rows=%d cached=%d", key, len(rows), len(to_cache))
            return to_cache

        except Exception as e:
            self._mark_failure()
            logger.warning("LookNode %s fetch failed: %s", key, e)
            fallback = self._read_cache(key, ignore_ttl=True)
            if fallback:
                logger.info("LookNode %s using stale cache", key)
            return fallback

    # ── 本地 JSON 缓存 ──

    def _cache_path(self, key: str) -> Path:
        return self._cache_dir / f"{key}.json"

    def _read_cache(self, key: str, ignore_ttl: bool = False) -> Optional[list[dict]]:
        path = self._cache_path(key)
        if not path.exists():
            return None
        try:
            with open(path, "r") as f:
                cached = json.load(f)
            fetched_at = cached.get("fetched_at", 0)
            if not ignore_ttl and time.time() - fetched_at > self._cache_ttl:
                return None
            return cached.get("data")
        except Exception:
            return None

    def _write_cache(self, key: str, data: list[dict]) -> None:
        try:
            with open(self._cache_path(key), "w") as f:
                json.dump({"fetched_at": int(time.time()), "data": data}, f)
        except Exception as e:
            logger.warning("LookNode cache write failed for %s: %s", key, e)

    # ── 组装 OnchainCycleData ──

    def _assemble(self, results: dict[str, Optional[list[dict]]]) -> Optional[OnchainCycleData]:
        if not any(results.values()):
            return None

        data = OnchainCycleData(ts=int(time.time()))

        sma_rows = results.get("sma_200w")
        if sma_rows:
            data.sma_200w = _safe_float(sma_rows[-1].get("v"))

        mvrv_rows = results.get("mvrv_z")
        if mvrv_rows:
            last = mvrv_rows[-1]
            data.mvrv_z = _safe_float(last.get("v3"))
            data.mvrv_market_cap = _safe_float(last.get("v1"))
            data.mvrv_realized_cap = _safe_float(last.get("v2"))

        sth_rows = results.get("sth_cost")
        if sth_rows:
            last = sth_rows[-1]
            data.sth_cost_1d = _safe_float(last.get("v1"))
            data.sth_cost_1w = _safe_float(last.get("v2"))
            data.sth_cost_1m = _safe_float(last.get("v3"))
            data.sth_cost_3m = _safe_float(last.get("v4"))

        ahr_rows = results.get("ahr999")
        if ahr_rows:
            data.ahr999 = _safe_float(ahr_rows[-1].get("v"))

        pi_rows = results.get("pi_cycle")
        if pi_rows:
            last = pi_rows[-1]
            data.pi_111dma_x2 = _safe_float(last.get("v1"))
            data.pi_350dma = _safe_float(last.get("v2"))

        cvdd_rows = results.get("cvdd")
        if cvdd_rows:
            data.cvdd = _safe_float(cvdd_rows[-1].get("v"))

        price_rows = results.get("btc_price")
        if price_rows:
            data.btc_daily_prices = [
                p for r in price_rows
                if (p := _safe_float(r.get("v"))) is not None and p > 0
            ]

        return data


def _safe_float(v: Any, default: Optional[float] = None) -> Optional[float]:
    if v is None:
        return default
    try:
        return float(v)
    except (ValueError, TypeError):
        return default


def create_looknode_source() -> LookNodeSource:
    return LookNodeSource()
