"""
衍生品 poll 契约测试：验证 ticker、OI、funding、LS ratio 的 API JSON → CoinState 映射。
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

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

    CG_SAMPLE = [
        {
            "symbol": "BTC",
            "stablecoin_margin_list": [
                {"exchange": "Binance", "funding_rate": 0.00009},
                {"exchange": "OKX", "funding_rate": 0.00014},
            ],
        },
    ]

    @pytest.mark.asyncio
    async def test_funding_primary_official_only_two_exchanges(
        self, cg, btc_state, states,
    ):
        """官方主源成功 → exchanges 仅保留 Binance + OKX 两条（方案 B），
        不调用 Coinglass。"""
        cg.fetch_fr_exchange_list = AsyncMock(return_value=self.CG_SAMPLE)
        get_coin = MagicMock(side_effect=lambda c: _make_coin(c, c))
        from processors.percentile import PercentileTracker
        pt = PercentileTracker()

        with patch(
            "polls.derivatives.fetch_official_funding_pair",
            new=AsyncMock(return_value=(0.0001, 0.00015)),
        ):
            await poll_funding_all(cg, states, ["BTC"], get_coin, pt, set())

        # 官方拿到 → Coinglass 不触发（fallback 没跑）
        cg.fetch_fr_exchange_list.assert_not_called()

        assert btc_state.funding is not None
        assert btc_state.multi_funding is not None
        # 方案 B：只保留 Binance + OKX 两条
        assert len(btc_state.multi_funding.exchanges) == 2
        names = sorted(ex.exchange for ex in btc_state.multi_funding.exchanges)
        assert names == ["Binance", "OKX"]
        # 数值来自官方，不是 Coinglass
        assert btc_state.funding.binance_rate == 0.0001
        assert btc_state.funding.okx_rate == 0.00015
        # avg = (0.0001 + 0.00015) / 2 = 0.000125
        assert btc_state.funding.avg_rate == pytest.approx(0.000125, abs=1e-7)

    @pytest.mark.asyncio
    async def test_funding_threshold_triggers_long_crowded(
        self, cg, btc_state, states,
    ):
        """avg > 0.0005 → 多头拥挤（阈值保留原值 0.0005）"""
        get_coin = MagicMock(side_effect=lambda c: _make_coin(c, c))
        from processors.percentile import PercentileTracker
        pt = PercentileTracker()

        # 两家都 0.001 → avg 0.001 > 0.0005
        with patch(
            "polls.derivatives.fetch_official_funding_pair",
            new=AsyncMock(return_value=(0.001, 0.001)),
        ):
            await poll_funding_all(cg, states, ["BTC"], get_coin, pt, set())

        assert btc_state.multi_funding.interpretation == "多头拥挤"

    @pytest.mark.asyncio
    async def test_funding_official_failure_fallbacks_to_coinglass(
        self, cg, btc_state, states,
    ):
        """官方两家都 None → 启动 Coinglass fallback，从中提取 Binance/OKX 条目。"""
        cg.fetch_fr_exchange_list = AsyncMock(return_value=self.CG_SAMPLE)
        get_coin = MagicMock(side_effect=lambda c: _make_coin(c, c))
        from processors.percentile import PercentileTracker
        pt = PercentileTracker()

        with patch(
            "polls.derivatives.fetch_official_funding_pair",
            new=AsyncMock(return_value=(None, None)),
        ):
            await poll_funding_all(cg, states, ["BTC"], get_coin, pt, set())

        # 官方都挂 → Coinglass 被调用
        cg.fetch_fr_exchange_list.assert_awaited_once()

        assert btc_state.funding is not None
        # Coinglass fallback 提供的 binance / okx
        assert btc_state.funding.binance_rate == pytest.approx(0.00009)
        assert btc_state.funding.okx_rate == pytest.approx(0.00014)
        # 仍然只保留 Binance + OKX 两条（方案 B）
        names = sorted(ex.exchange for ex in btc_state.multi_funding.exchanges)
        assert names == ["Binance", "OKX"]

    @pytest.mark.asyncio
    async def test_funding_partial_official_okx_only(
        self, cg, btc_state, states,
    ):
        """Binance 挂 / OKX ok → 不走 fallback，仅单家也能工作。"""
        cg.fetch_fr_exchange_list = AsyncMock(return_value=self.CG_SAMPLE)
        get_coin = MagicMock(side_effect=lambda c: _make_coin(c, c))
        from processors.percentile import PercentileTracker
        pt = PercentileTracker()

        with patch(
            "polls.derivatives.fetch_official_funding_pair",
            new=AsyncMock(return_value=(None, 0.00012)),
        ):
            await poll_funding_all(cg, states, ["BTC"], get_coin, pt, set())

        # 任一官方拿到 → 不触发 fallback
        cg.fetch_fr_exchange_list.assert_not_called()
        assert btc_state.funding.binance_rate is None
        assert btc_state.funding.okx_rate == 0.00012
        assert btc_state.funding.avg_rate == pytest.approx(0.00012, abs=1e-7)
        assert len(btc_state.multi_funding.exchanges) == 1


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

    SAMPLE = {"markPrice": "65000", "indexPrice": "64900"}

    @pytest.mark.asyncio
    async def test_basis_parsed(self, cg, btc_state):
        bn = MagicMock()
        bn.fetch_premium_index = AsyncMock(return_value=self.SAMPLE)
        coin = _make_coin()
        await poll_basis(cg, coin, btc_state, bn=bn)

        assert btc_state.basis is not None
        assert btc_state.basis.basis_pct == pytest.approx(round((65000 - 64900) / 64900 * 100, 4), abs=1e-4)
