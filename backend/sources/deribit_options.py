"""Deribit 官方公开期权概览；不推断 dealer 仓位或伪精确 GEX。"""
from __future__ import annotations

import time
from typing import Any, Optional

from sources.base import DataSource


class DeribitOptionsSource(DataSource):
    def __init__(self, base_url: str = "https://www.deribit.com", timeout_sec: int = 15):
        super().__init__(name="deribit_options", timeout_sec=timeout_sec, max_retries=2)
        self._base_url = base_url.rstrip("/")

    def get_poll_interval(self) -> int:
        return 300

    async def fetch(self, coin: Any) -> Any:
        return await self.fetch_book_summaries(getattr(coin, "ccy", "BTC"))

    async def fetch_book_summaries(self, currency: str = "BTC") -> Optional[list[dict[str, Any]]]:
        session = await self.get_session()
        started = time.time()
        try:
            async with session.get(
                f"{self._base_url}/api/v2/public/get_book_summary_by_currency",
                params={"currency": currency.upper(), "kind": "option"},
            ) as response:
                if response.status != 200:
                    self._mark_failure(f"http_{response.status}", response.status)
                    return None
                payload = await response.json()
                rows = payload.get("result") if isinstance(payload, dict) else None
                if not isinstance(rows, list):
                    self._mark_failure("invalid_result_shape")
                    return None
                self._mark_success((time.time() - started) * 1000, response.status)
                return [row for row in rows if isinstance(row, dict)]
        except Exception as exc:
            self._mark_failure(type(exc).__name__)
            return None
