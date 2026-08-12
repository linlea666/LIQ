"""Yahoo Finance CME 比特币期货（BTC=F）周线量价源 · 仅供 Bottom Model。

用途：CME 恐慌周量子信号（合规市场恐慌换手的历史百分位）。
BTC=F 为前月标准合约（5 BTC/张）连续序列，即 TradingView BTC1! 同源，
不含 Micro/期权/现货报价合约——只是代理指标，权重与语义由因子层控制。

非官方公开接口，随时可能变更：任何失败 fail-open 返回 None，
仅导致该子信号缺失，绝不影响其他因子。

实测（2026-08）：/v8/finance/chart/BTC=F?interval=1wk&range=10y
返回 2017-12（CME 上市）至今约 450+ 周；最后一根为进行中的当前周，必须剔除。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional
from urllib.parse import quote

from config.settings import YahooCMESourceConfig
from sources.base import DataSource

logger = logging.getLogger(__name__)

_WEEK_SEC = 7 * 86400


def parse_weekly_chart(payload: Any, now_ts: Optional[int] = None) -> list[dict[str, Any]]:
    """解析 Yahoo chart 响应为已收盘完整周的列表（升序）。

    返回 [{"week_start_ts": int, "close": float, "volume": float}, ...]；
    进行中的当前周（week_start + 7d > now）与缺值周被剔除。
    """
    now = int(now_ts if now_ts is not None else time.time())
    try:
        result = payload["chart"]["result"][0]
        timestamps = result["timestamp"]
        quote_block = result["indicators"]["quote"][0]
        closes = quote_block["close"]
        volumes = quote_block["volume"]
    except (KeyError, IndexError, TypeError):
        return []
    rows: list[dict[str, Any]] = []
    for ts, close, volume in zip(timestamps, closes, volumes):
        if close is None or volume is None:
            continue
        try:
            ts_int = int(ts)
        except (TypeError, ValueError):
            continue
        if ts_int + _WEEK_SEC > now:
            continue  # 进行中的周，量价未定
        rows.append({
            "week_start_ts": ts_int,
            "close": float(close),
            "volume": float(volume),
        })
    rows.sort(key=lambda item: item["week_start_ts"])
    return rows


class YahooCMESource(DataSource):
    def __init__(self, cfg: YahooCMESourceConfig):
        super().__init__("yahoo_cme", cfg.timeout_sec, max_retries=1)
        self._base_url = cfg.base_url.rstrip("/")
        self._symbol = cfg.symbol
        self.last_error = ""

    def get_poll_interval(self) -> int:
        return 86400

    async def fetch(self, coin) -> None:
        return None

    async def fetch_weekly_history(self, range_: str = "10y") -> Optional[list[dict[str, Any]]]:
        """拉取 BTC=F 周线量价（仅已收盘完整周，升序）。失败返回 None。"""
        url = (
            f"{self._base_url}/v8/finance/chart/{quote(self._symbol)}"
            f"?interval=1wk&range={range_}"
        )
        started = time.monotonic()
        try:
            session = await self.get_session()
            headers = {"User-Agent": "Mozilla/5.0 (LIQ-bottom-model)"}
            async with session.get(url, headers=headers) as resp:
                resp.raise_for_status()
                payload = await resp.json(content_type=None)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._mark_failure()
            self.last_error = f"{type(exc).__name__}: {exc}"
            logger.warning("YahooCME fetch failed | err=%s", self.last_error)
            return None
        rows = parse_weekly_chart(payload)
        if not rows:
            self._mark_failure()
            self.last_error = "empty_or_unparsable"
            logger.warning("YahooCME returned unparsable payload")
            return None
        self._mark_success((time.monotonic() - started) * 1000)
        self.last_error = ""
        return rows


def create_yahoo_cme_source(cfg: YahooCMESourceConfig) -> Optional[YahooCMESource]:
    return YahooCMESource(cfg) if cfg.enabled else None
