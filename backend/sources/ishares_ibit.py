"""iShares IBIT 官方日度持仓。

该来源描述基金披露的日终持仓、份额和资产，不代表贝莱德在观测时刻刚刚
通过交易所买入或卖出 BTC。首次观测时间由系统单独保存，用于 PIT 审计。
"""
from __future__ import annotations

import csv
import time
from datetime import datetime, timezone
from io import StringIO
from typing import Any, Optional

from sources.base import DataSource


class ISharesIBITSource(DataSource):
    HOLDINGS_URL = (
        "https://www.ishares.com/us/products/333011/"
        "ishares-bitcoin-trust-etf/latest-holdings.csv"
    )

    def __init__(self, timeout_sec: int = 20):
        super().__init__(name="ishares_ibit_official", timeout_sec=timeout_sec, max_retries=1)

    def get_poll_interval(self) -> int:
        return 21_600

    async def fetch(self, coin: Any) -> Optional[dict[str, Any]]:
        if str(getattr(coin, "ccy", "BTC")).upper() != "BTC":
            return None
        return await self.fetch_holdings()

    async def fetch_holdings(self) -> Optional[dict[str, Any]]:
        session = await self.get_session()
        started = time.time()
        try:
            async with session.get(
                self.HOLDINGS_URL,
                headers={"User-Agent": "LIQ-market-risk/1.0"},
            ) as response:
                if response.status != 200:
                    self._mark_failure()
                    return None
                parsed = self.parse_holdings(await response.text(encoding="utf-8-sig"))
                if parsed is None:
                    self._mark_failure()
                    return None
                parsed["observed_at"] = int(time.time())
                parsed["source"] = "ishares_ibit_official"
                self._mark_success((time.time() - started) * 1000)
                return parsed
        except Exception:
            self._mark_failure()
            return None

    @staticmethod
    def _number(value: str) -> Optional[float]:
        cleaned = (value or "").replace(",", "").replace("$", "").strip()
        try:
            return float(cleaned)
        except (TypeError, ValueError):
            return None

    @classmethod
    def parse_holdings(cls, raw_csv: str) -> Optional[dict[str, Any]]:
        rows = list(csv.reader(StringIO(raw_csv or "")))
        if not rows or "iShares Bitcoin Trust" not in (rows[0][0] if rows[0] else ""):
            return None
        as_of = ""
        shares_outstanding: Optional[float] = None
        header_index = -1
        for index, row in enumerate(rows):
            if not row:
                continue
            label = row[0].strip()
            if label == "Fund Holdings as of" and len(row) > 1:
                as_of = row[1].strip()
            elif label == "Shares Outstanding" and len(row) > 1:
                shares_outstanding = cls._number(row[1])
            elif label == "Ticker":
                header_index = index
                break
        if not as_of or shares_outstanding is None or header_index < 0:
            return None
        headers = [item.strip() for item in rows[header_index]]
        btc_row: Optional[dict[str, str]] = None
        for row in rows[header_index + 1:]:
            if row and row[0].strip().upper() == "BTC":
                padded = row + [""] * max(0, len(headers) - len(row))
                btc_row = dict(zip(headers, padded))
                break
        if btc_row is None:
            return None
        bitcoin_quantity = cls._number(btc_row.get("Quantity", ""))
        market_value_usd = cls._number(btc_row.get("Market Value", ""))
        if bitcoin_quantity is None or market_value_usd is None:
            return None
        try:
            as_of_day = datetime.strptime(as_of, "%b %d, %Y").replace(tzinfo=timezone.utc)
        except ValueError:
            return None
        return {
            "fund": "IBIT",
            "as_of": as_of_day.date().isoformat(),
            "as_of_ts": int(as_of_day.timestamp()),
            "shares_outstanding": shares_outstanding,
            "bitcoin_quantity": bitcoin_quantity,
            "bitcoin_market_value_usd": market_value_usd,
        }
