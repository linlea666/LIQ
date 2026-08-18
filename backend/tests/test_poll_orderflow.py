"""
订单流 poll 契约测试：CVD、大单、订单簿。
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock

from config.settings import CoinConfig
from polls.orderflow import poll_cvd, poll_large_orders, poll_orderbook_depth, calc_cvd_trend


def _make_coin(ccy="BTC", symbol="BTC", pair="BTCUSDT", exchange="Binance"):
    c = MagicMock(spec=CoinConfig)
    c.ccy = ccy
    c.symbol_cg = symbol
    c.symbol_cg_pair = pair
    c.exchange_primary = exchange
    return c


# ──────────────────────────────────────────────
# poll_large_orders
# ──────────────────────────────────────────────

class TestPollLargeOrders:

    # Coinglass /large-limit-order(-history) 约定（2026-08-18 生产实测定案）：
    #   order_side=1 → ask（卖单 / 上方阻力）
    #   order_side=2 → bid（买单 / 下方支撑）
    # 实测方法：期货+现货共 450+ 条 holding 大单对照现价，side=1 全部在
    # 现价上方、side=2 全部在现价下方。此前按错误"实测"翻转成 1=bid，
    # 导致全链路 bid/ask 反向（UI 卖墙低于买墙、pressure 大单被全量误滤）。
    SAMPLE = [
        {"price": 74500, "order_side": "2", "order_size_usd": 5000000},  # bid（下方买单）
        {"price": 74800, "order_side": "1", "order_size_usd": 3000000},  # ask（上方卖单）
    ]

    @pytest.mark.asyncio
    async def test_side_mapping(self, cg, btc_state):
        """order_side 1=ask(卖), 2=bid(买)。"""
        cg.fetch_large_orders = AsyncMock(return_value=self.SAMPLE)
        # history 端点也会被 poll 调用（双轨合并），mock 成空列表避免真实请求
        cg.fetch_large_orders_history = AsyncMock(return_value=[])
        coin = _make_coin()
        await poll_large_orders(cg, coin, btc_state)

        assert btc_state.large_orders is not None
        orders = btc_state.large_orders.orders
        assert len(orders) == 2
        bid = [o for o in orders if o.side == "bid"]
        ask = [o for o in orders if o.side == "ask"]
        assert len(bid) == 1
        assert bid[0].price == 74500
        assert len(ask) == 1
        assert ask[0].price == 74800


# ──────────────────────────────────────────────
# poll_orderbook_depth
# ──────────────────────────────────────────────

class TestPollOrderbookDepth:

    SAMPLE = [
        {"aggregated_bids_usd": 500_000_000, "aggregated_asks_usd": 600_000_000, "time": 1700000000},
    ]

    @pytest.mark.asyncio
    async def test_orderbook_parsed(self, cg, btc_state):
        cg.fetch_orderbook_aggregated_ask_bids = AsyncMock(return_value=self.SAMPLE)
        coin = _make_coin()
        await poll_orderbook_depth(cg, coin, btc_state)

        assert btc_state.orderbook is not None
        assert btc_state.orderbook.bid_total_usd == 500_000_000
        assert btc_state.orderbook.ask_total_usd == 600_000_000


# ──────────────────────────────────────────────
# calc_cvd_trend
# ──────────────────────────────────────────────

class TestCalcCVDTrend:

    @staticmethod
    def _make_points(values):
        from models.flow import CVDPoint
        pts = []
        cvd = 0
        for i, v in enumerate(values):
            buy = max(v, 0)
            sell = max(-v, 0)
            cvd += v
            pts.append(CVDPoint(ts=i, buy_vol=buy, sell_vol=sell, delta=v, cvd=cvd))
        return pts

    def test_rising(self):
        pts = self._make_points([100] * 15)
        trend, _ = calc_cvd_trend(pts)
        assert trend == "rising"

    def test_declining(self):
        pts = self._make_points([-100] * 15)
        trend, _ = calc_cvd_trend(pts)
        assert trend == "declining"

    def test_short_series(self):
        from models.flow import CVDPoint
        trend, _ = calc_cvd_trend([CVDPoint(ts=0, buy_vol=1, sell_vol=0, delta=1, cvd=1)])
        assert trend == "flat"


@pytest.mark.asyncio
async def test_poll_cvd_normalizes_dedupes_and_sets_as_of(cg, btc_state):
    base = 1_700_000_000
    contract = [
        {
            "time": (base + i * 300) * 1000,
            "agg_taker_buy_vol": 200 + i,
            "agg_taker_sell_vol": 100,
            "cum_vol_delta": 100 + i,
        }
        for i in range(12)
    ]
    contract.insert(4, dict(contract[3]))
    spot = [dict(item) for item in reversed(contract)]
    cg.fetch_aggregated_cvd_history = AsyncMock(return_value=contract)
    cg.fetch_spot_aggregated_cvd = AsyncMock(return_value=spot)

    await poll_cvd(cg, _make_coin(), btc_state)

    assert btc_state.cvd_contract is not None
    assert btc_state.cvd_spot is not None
    assert len(btc_state.cvd_contract.series) == 12
    assert btc_state.cvd_contract.ts == base + 11 * 300
    assert btc_state.cvd_spot.ts == base + 11 * 300
    assert [p.ts for p in btc_state.cvd_spot.series] == sorted(
        p.ts for p in btc_state.cvd_spot.series
    )
