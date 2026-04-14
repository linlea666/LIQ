"""
衍生品 poll 契约测试：验证 ticker、OI、funding、LS ratio 的 API JSON → CoinState 映射。
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock

from config.settings import CoinConfig
from polls.derivatives import poll_oi, poll_funding_all, poll_ls_ratio, poll_basis


def _make_coin(ccy="BTC", symbol_cg="BTC", symbol_pair="BTCUSDT", exchange="Binance"):
    c = MagicMock(spec=CoinConfig)
    c.ccy = ccy
    c.symbol_cg = symbol_cg
    c.symbol_cg_pair = symbol_pair
    c.exchange_primary = exchange
    return c


# ──────────────────────────────────────────────
# poll_oi
# ──────────────────────────────────────────────

class TestPollOI:

    SAMPLE = [
        {"time": 1700000000, "close": 40_000_000_000},
        {"time": 1700000300, "close": 40_200_000_000},
        {"time": 1700000600, "close": 40_500_000_000},
    ]

    @pytest.mark.asyncio
    async def test_oi_trend_calculated(self, cg, btc_state):
        cg.fetch_oi_aggregated_history = AsyncMock(return_value=self.SAMPLE)
        coin = _make_coin()
        await poll_oi(cg, coin, btc_state)
        assert btc_state.oi is not None
        assert btc_state.oi.current_usd == 40_500_000_000
        assert btc_state.oi.trend in ("stable", "surging", "declining")


# ──────────────────────────────────────────────
# poll_funding_all
# ──────────────────────────────────────────────

class TestPollFundingAll:

    SAMPLE = [
        {
            "symbol": "BTC",
            "stablecoin_margin_list": [
                {"exchange": "Binance", "funding_rate": 0.0001},
                {"exchange": "OKX", "funding_rate": 0.00015},
            ],
        },
    ]

    @pytest.mark.asyncio
    async def test_funding_parsed(self, cg, btc_state, states):
        cg.fetch_fr_exchange_list = AsyncMock(return_value=self.SAMPLE)
        get_coin = MagicMock(side_effect=lambda c: _make_coin(c, c))
        from processors.percentile import PercentileTracker
        pt = PercentileTracker()
        logged = set()
        await poll_funding_all(cg, states, ["BTC"], get_coin, pt, logged)

        assert btc_state.funding is not None
        assert btc_state.multi_funding is not None
        assert len(btc_state.multi_funding.exchanges) == 2
        assert btc_state.funding.binance_rate == 0.0001
        assert btc_state.funding.okx_rate == 0.00015


# ──────────────────────────────────────────────
# poll_ls_ratio
# ──────────────────────────────────────────────

class TestPollLSRatio:

    SAMPLE_GLOBAL = [
        {
            "global_account_long_percent": 55.0,
            "global_account_short_percent": 45.0,
            "global_account_long_short_ratio": 1.22,
        },
    ]

    @pytest.mark.asyncio
    async def test_ls_ratio_parsed(self, cg, btc_state):
        cg.fetch_global_ls_ratio_history = AsyncMock(return_value=self.SAMPLE_GLOBAL)
        cg.fetch_top_ls_account_ratio_history = AsyncMock(return_value=None)
        cg.fetch_top_ls_position_ratio_history = AsyncMock(return_value=None)
        coin = _make_coin()
        logged = set()
        await poll_ls_ratio(cg, coin, btc_state, logged)

        assert btc_state.ls_ratio is not None
        assert btc_state.ls_ratio.avg_ratio == 1.22


# ──────────────────────────────────────────────
# poll_basis
# ──────────────────────────────────────────────

class TestPollBasis:

    SAMPLE = [
        {"time": 1700000000, "close_basis": 0.15, "annualized_basis": 5.5},
    ]

    @pytest.mark.asyncio
    async def test_basis_parsed(self, cg, btc_state):
        cg.fetch_basis_history = AsyncMock(return_value=self.SAMPLE)
        coin = _make_coin()
        await poll_basis(cg, coin, btc_state)

        assert btc_state.basis is not None
        assert btc_state.basis.basis_pct == 0.15
