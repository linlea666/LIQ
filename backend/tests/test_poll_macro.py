"""
宏观/情报 poll 契约测试：ETF、CB Premium 趋势、稳定币。
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock

from polls.macro import (
    poll_etf_flow,
    poll_coinbase_premium,
    calc_cb_premium_trend,
    calc_stablecoin_change,
)


# ──────────────────────────────────────────────
# calc_cb_premium_trend  (阈值 0.001)
# ──────────────────────────────────────────────

class TestCBPremiumTrend:

    def _make_cb(self, premiums):
        from models.macro import CoinbasePremiumData, CoinbasePremiumPoint
        pts = [CoinbasePremiumPoint(ts=i, premium=p, price=74000) for i, p in enumerate(premiums)]
        return CoinbasePremiumData(ts=0, current_premium=premiums[-1], history=pts)

    def test_strong_buy(self):
        cb = self._make_cb([0.005, 0.006, 0.004, 0.005, 0.007, 0.006])
        assert calc_cb_premium_trend(cb) == "机构买入偏强"

    def test_strong_sell(self):
        cb = self._make_cb([-0.005, -0.006, -0.004, -0.005, -0.007, -0.006])
        assert calc_cb_premium_trend(cb) == "机构卖出偏强"

    def test_neutral(self):
        cb = self._make_cb([0.0001, -0.0002, 0.0003, -0.0001, 0.0002, 0.0001])
        assert calc_cb_premium_trend(cb) == "中性"

    def test_old_threshold_would_miss(self):
        """旧阈值 0.05 会把 0.002（0.2%溢价）判为中性，新阈值 0.001 正确识别为偏强"""
        cb = self._make_cb([0.002] * 6)
        assert calc_cb_premium_trend(cb) == "机构买入偏强"


# ──────────────────────────────────────────────
# calc_stablecoin_change
# ──────────────────────────────────────────────

class TestStablecoinChange:

    def test_increase(self):
        from models.macro import StablecoinMcapData, StablecoinMcapPoint
        sc = StablecoinMcapData(
            ts=0,
            current_total_mcap=200_000_000_000,
            history=[
                StablecoinMcapPoint(ts=0, total_mcap=190_000_000_000),
                StablecoinMcapPoint(ts=1, total_mcap=200_000_000_000),
            ],
        )
        pct = calc_stablecoin_change(sc)
        assert pct > 0
        assert abs(pct - 5.26) < 0.1

    def test_none_input(self):
        assert calc_stablecoin_change(None) == 0


# ──────────────────────────────────────────────
# poll_etf_flow
# ──────────────────────────────────────────────

class TestPollEtfFlow:

    SAMPLE_BTC_ETF = [
        {"timestamp": 1713000000000, "totalNetFlow": 50_000_000},
        {"timestamp": 1713086400000, "totalNetFlow": -20_000_000},
        {"timestamp": 1713172800000, "totalNetFlow": 30_000_000},
    ]

    @pytest.mark.asyncio
    async def test_etf_flow_parsed(self, cg, btc_state, states):
        cg.fetch_btc_etf_flow_history = AsyncMock(return_value=self.SAMPLE_BTC_ETF)
        cg.fetch_eth_etf_flow_history = AsyncMock(return_value=None)
        await poll_etf_flow(cg, states, ["BTC"])

        assert btc_state.etf_flow is not None
        assert len(btc_state.etf_flow.recent_days) == 3


# ──────────────────────────────────────────────
# poll_coinbase_premium
# ──────────────────────────────────────────────

class TestPollCBPremium:

    SAMPLE = [
        {"time": 1700000000, "premium_rate": 0.002, "coinbase_price": 74000},
        {"time": 1700000300, "premium_rate": 0.003, "coinbase_price": 74100},
    ]

    @pytest.mark.asyncio
    async def test_premium_stored(self, cg, btc_state, states):
        cg.fetch_coinbase_premium = AsyncMock(return_value=self.SAMPLE)
        await poll_coinbase_premium(cg, states, ["BTC"])

        assert btc_state.coinbase_premium is not None
        assert btc_state.coinbase_premium.current_premium == 0.003
        assert len(btc_state.coinbase_premium.history) == 2
