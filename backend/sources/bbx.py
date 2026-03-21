"""BBX 清算地图数据源"""

from __future__ import annotations

import logging
from typing import Any

from config.settings import CoinConfig, get_settings
from models.liquidation import LiqBand, LiqLeverageGroup, LiquidationMap
from sources.base import DataSource

logger = logging.getLogger(__name__)


class BBXLiquidationSource(DataSource):
    """从 bbx.com 拉取清算热力图数据"""

    def __init__(self):
        cfg = get_settings().bbx
        super().__init__(name="bbx_liquidation", timeout_sec=cfg.timeout_sec)
        self._base_url = cfg.base_url
        self._module = cfg.module
        self._poll_interval = cfg.poll_interval_sec
        self._cycles = cfg.cycles

    def get_poll_interval(self) -> int:
        return self._poll_interval

    async def fetch(self, coin: CoinConfig) -> dict[str, LiquidationMap]:
        """拉取所有周期的清算地图，返回 {cycle: LiquidationMap}"""
        results: dict[str, LiquidationMap] = {}
        for cycle in self._cycles:
            liq_map = await self._fetch_cycle(coin, cycle)
            if liq_map:
                results[cycle] = liq_map
        return results

    async def _fetch_cycle(self, coin: CoinConfig, cycle: str) -> LiquidationMap | None:
        url = f"{self._base_url}?module={self._module}"
        body = {
            "symbol": coin.symbol_bbx,
            "cycle": cycle,
            "multiple": "",
            "lan": "zh",
        }
        try:
            data = await self._get_json(url, method="POST", json_body=body)
        except Exception:
            logger.error("BBX API request failed | coin=%s cycle=%s", coin.ccy, cycle, exc_info=True)
            return None

        if not data.get("success"):
            logger.warning("BBX API returned success=false | coin=%s cycle=%s", coin.ccy, cycle)
            return None

        raw = data.get("data", {})
        ts = raw.get("timestamp", 0)
        if isinstance(ts, str):
            ts = int(ts) if ts.isdigit() else 0

        leverage_groups: list[LiqLeverageGroup] = []
        for lev in ("10", "25", "50", "100"):
            group_raw = raw.get(lev)
            if not group_raw or not isinstance(group_raw, dict):
                continue

            short_bands = [
                LiqBand(price_from=b[0], price_to=b[1], turnover_usd=b[2])
                for b in group_raw.get("short", [])
            ]
            long_bands = [
                LiqBand(price_from=b[0], price_to=b[1], turnover_usd=b[2])
                for b in group_raw.get("long", [])
            ]

            leverage_groups.append(LiqLeverageGroup(
                leverage=lev,
                short_bands=short_bands,
                long_bands=long_bands,
                short_total_usd=sum(b.turnover_usd for b in short_bands),
                long_total_usd=sum(b.turnover_usd for b in long_bands),
            ))

        logger.debug(
            "BBX parsed | coin=%s cycle=%s leverages=%d",
            coin.ccy, cycle, len(leverage_groups),
        )

        return LiquidationMap(
            coin=coin.ccy,
            ts=ts,
            cycle=cycle,
            leverage_groups=leverage_groups,
        )


def create_bbx_source() -> BBXLiquidationSource:
    return BBXLiquidationSource()
