"""CFTC 官方 CME Bitcoin Futures-Only COT 周报。

该来源只提供慢周期机构背景，永不进入联合风险事件评分。报告日与系统首次
观测时间分开保存，避免把周二持仓错误地当成周二即可获得的数据。
"""
from __future__ import annotations

import html
import re
import time
from datetime import datetime, timezone
from typing import Any, Optional

from sources.base import DataSource


class CFTCBitcoinCOTSource(DataSource):
    REPORT_URL = "https://www.cftc.gov/dea/futures/deacmesf.htm"
    MARKET_MARKER = "BITCOIN - CHICAGO MERCANTILE EXCHANGE"
    MARKET_CODE = "133741"

    def __init__(self, timeout_sec: int = 20):
        super().__init__(name="cftc_cme_bitcoin_cot", timeout_sec=timeout_sec, max_retries=1)

    def get_poll_interval(self) -> int:
        return 14_400

    async def fetch(self, coin: Any) -> Optional[dict[str, Any]]:
        if str(getattr(coin, "ccy", "BTC")).upper() != "BTC":
            return None
        return await self.fetch_bitcoin_report()

    async def fetch_bitcoin_report(self) -> Optional[dict[str, Any]]:
        session = await self.get_session()
        started = time.time()
        try:
            async with session.get(self.REPORT_URL) as response:
                if response.status != 200:
                    self._mark_failure(f"http_{response.status}", response.status)
                    return None
                parsed = self.parse_report(await response.text())
                if parsed is None:
                    self._mark_failure("report_parse_failed")
                    return None
                parsed["observed_at"] = int(time.time())
                parsed["source"] = "cftc_official_cme_futures_only"
                self._mark_success((time.time() - started) * 1000, response.status)
                return parsed
        except Exception as exc:
            self._mark_failure(type(exc).__name__)
            return None

    @classmethod
    def parse_report(cls, raw_html: str) -> Optional[dict[str, Any]]:
        text = html.unescape(re.sub(r"<[^>]+>", " ", raw_html or ""))
        text = re.sub(r"\s+", " ", text).strip()
        start = text.find(cls.MARKET_MARKER)
        if start < 0:
            return None
        block = text[start:]
        next_market = block.find(cls.MARKET_MARKER, len(cls.MARKET_MARKER))
        if next_market > 0:
            block = block[:next_market]
        if f"Code-{cls.MARKET_CODE}" not in block:
            return None
        date_match = re.search(r"POSITIONS AS OF\s+(\d{2}/\d{2}/\d{2})", block)
        oi_match = re.search(r"OPEN INTEREST:\s*([\d,]+)", block)
        commitments_match = re.search(r"COMMITMENTS\s+(.+?)\s+CHANGES FROM", block)
        changes_match = re.search(
            r"CHANGES FROM\s+\d{2}/\d{2}/\d{2}[^:]*:\s*[\-\d,]+\)\s+(.+?)\s+PERCENT OF OPEN INTEREST",
            block,
        )
        if not date_match or not oi_match or not commitments_match:
            return None

        def numbers(value: str) -> list[int]:
            return [int(item.replace(",", "")) for item in re.findall(r"-?[\d,]+", value)]

        positions = numbers(commitments_match.group(1))[:9]
        if len(positions) != 9:
            return None
        changes = numbers(changes_match.group(1))[:9] if changes_match else []
        report_day = datetime.strptime(date_match.group(1), "%m/%d/%y").replace(tzinfo=timezone.utc)
        labels = (
            "noncommercial_long", "noncommercial_short", "noncommercial_spreads",
            "commercial_long", "commercial_short", "total_long", "total_short",
            "nonreportable_long", "nonreportable_short",
        )
        payload: dict[str, Any] = {
            "market_code": cls.MARKET_CODE,
            "report_date": report_day.date().isoformat(),
            "report_as_of": int(report_day.timestamp()),
            "open_interest_contracts": int(oi_match.group(1).replace(",", "")),
            "positions": dict(zip(labels, positions)),
            "noncommercial_net": positions[0] - positions[1],
        }
        if len(changes) == 9:
            payload["changes"] = dict(zip(labels, changes))
            payload["noncommercial_net_change"] = changes[0] - changes[1]
        return payload
