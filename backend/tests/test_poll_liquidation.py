"""
清算领域 poll 契约测试：验证 API JSON → CoinState 的映射正确性。
"""
from __future__ import annotations

import pytest
from unittest.mock import MagicMock

from polls.liquidation import (
    parse_liquidation_map,
    poll_global_liq,
    poll_liq_history,
    poll_liq_max_pain,
)


# ──────────────────────────────────────────────
# parse_liquidation_map
# ──────────────────────────────────────────────

class TestParseLiquidationMap:

    SAMPLE_RESPONSE = {
        "data": [
            {
                "liqMapV2": {
                    "70000": [[70000, 5000000]],
                    "75000": [[75000, 3000000]],
                    "80000": [[80000, 2000000]],
                }
            }
        ],
        "last_price": 74000,
    }

    def test_basic_parsing(self):
        result = parse_liquidation_map(self.SAMPLE_RESPONSE, "BTC", "1d", current_price=74000)
        assert result is not None
        assert result.coin == "BTC"
        assert result.cycle == "1d"
        assert len(result.leverage_groups) == 1
        grp = result.leverage_groups[0]
        assert grp.long_total_usd == 5000000
        assert grp.short_total_usd == 5000000

    def test_below_above_split(self):
        result = parse_liquidation_map(self.SAMPLE_RESPONSE, "BTC", "1d", current_price=74000)
        grp = result.leverage_groups[0]
        long_prices = [b.price_from for b in grp.long_bands]
        short_prices = [b.price_from for b in grp.short_bands]
        assert all(p <= 74000 for p in long_prices)
        assert all(p > 74000 for p in short_prices)

    def test_empty_data(self):
        assert parse_liquidation_map(None, "BTC", "1d") is None
        assert parse_liquidation_map({"data": []}, "BTC", "1d") is None
        assert parse_liquidation_map({"data": [], "last_price": 0}, "BTC", "1d") is None

    def test_list_format(self):
        data = [{"liqMapV2": {"90000": [[90000, 1000000]]}}]
        result = parse_liquidation_map(data, "ETH", "7d", current_price=3500)
        assert result is not None
        assert result.coin == "ETH"

    def test_decimal_prices(self):
        data = {"data": [{"liqMapV2": {"3456.78": [[3456.78, 100000]]}}], "last_price": 3000}
        result = parse_liquidation_map(data, "ETH", "1d", current_price=3000)
        assert result is not None


# ──────────────────────────────────────────────
# poll_global_liq
# ──────────────────────────────────────────────

class TestPollGlobalLiq:

    SAMPLE_24H = [
        {"exchange": "All", "liquidation_usd": 500e6,
         "longLiquidation_usd": 200e6, "shortLiquidation_usd": 300e6},
        {"exchange": "Binance", "liquidation_usd": 300e6,
         "longLiquidation_usd": 120e6, "shortLiquidation_usd": 180e6},
    ]

    SAMPLE_1H = [
        {"exchange": "All", "liquidation_usd": 10e6,
         "longLiquidation_usd": 3e6, "shortLiquidation_usd": 7e6},
    ]

    @pytest.mark.asyncio
    async def test_uses_all_row_not_sum(self, cg, btc_state, states):
        """必须使用 All 汇总行，不能逐所累加"""
        cg.fetch_liquidation_exchange_list = AsyncMock(side_effect=[self.SAMPLE_24H, self.SAMPLE_1H])
        await poll_global_liq(cg, ["BTC"], states)

        gliq = btc_state.global_liq
        assert gliq is not None
        assert gliq.long_24h_usd == 200e6
        assert gliq.short_24h_usd == 300e6
        assert gliq.long_1h_usd == 3e6
        assert gliq.short_1h_usd == 7e6
        assert gliq.ratio_24h == round(200e6 / 300e6, 2)

    @pytest.mark.asyncio
    async def test_empty_response(self, cg, btc_state, states):
        cg.fetch_liquidation_exchange_list = AsyncMock(return_value=None)
        await poll_global_liq(cg, ["BTC"], states)
        assert btc_state.global_liq is None or btc_state.global_liq.long_24h_usd == 0


# ──────────────────────────────────────────────
# poll_liq_max_pain
# ──────────────────────────────────────────────

class TestPollLiqMaxPain:

    SAMPLE = [
        {"symbol": "BTC", "price": 74000, "longLiqUsd": 100e6, "shortLiqUsd": 80e6},
        {"symbol": "ETH", "price": 3200, "longLiqUsd": 50e6, "shortLiqUsd": 40e6},
    ]

    @pytest.mark.asyncio
    async def test_writes_to_all_supported_coins(self, cg, btc_state, states):
        cg.fetch_liquidation_max_pain = AsyncMock(return_value=self.SAMPLE)
        await poll_liq_max_pain(cg, ["BTC"], states)
        assert btc_state.liq_max_pain is not None
        assert len(btc_state.liq_max_pain.items) >= 1


from unittest.mock import AsyncMock
