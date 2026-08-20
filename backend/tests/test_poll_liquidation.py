"""清算领域 poll 契约测试：验证 API JSON → CoinState 的映射正确性。

所有 fixture 严格按 Coinglass V4 真实 API 返回结构构造（已通过
`backend/data/liq_endpoint_dumps/*.json` 抓包验证），避免重蹈旧版字段名错位
导致 P0 数据死链路的覆辙。
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from polls.liquidation import (
    parse_liq_heatmap,
    parse_liquidation_map,
    poll_global_liq,
    poll_liq_heatmap,
    poll_liq_max_pain,
)


# ──────────────────────────────────────────────
# parse_liquidation_map · 真实 V4 双层嵌套 + by_exchange
# ──────────────────────────────────────────────

class TestParseLiquidationMap:
    """模拟 aggregated-map?symbol=BTC&range=1d 的真实结构。

    顶层 {code, data: {data:[{liqMapV2,instrument:{exName}}, ...], last_price}}
    """

    SAMPLE_REAL = {
        "code": "0",
        "data": {
            "data": [
                {
                    "liqMapV2": {
                        "70000": [[70000, 5_000_000, None, None]],
                        "80000": [[80000, 2_000_000, None, None]],
                    },
                    "instrument": {"exName": "Binance", "instrumentId": "BTCUSDT"},
                },
                {
                    "liqMapV2": {
                        "70000": [[70000, 1_500_000, None, None]],
                        "75000": [[75000, 3_000_000, None, None]],
                    },
                    "instrument": {"exName": "OKX", "instrumentId": "BTC-USDT-SWAP"},
                },
            ],
            "last_price": 74000,
        },
    }

    def test_real_nested_structure_basic(self):
        result = parse_liquidation_map(self.SAMPLE_REAL, "BTC", "1d", current_price=74000)
        assert result is not None
        assert result.coin == "BTC"
        assert result.cycle == "1d"
        grp = result.leverage_groups[0]
        assert grp.long_total_usd == 5_000_000 + 1_500_000   # 70000 跨所合并
        assert grp.short_total_usd == 2_000_000 + 3_000_000  # 75000 + 80000

    def test_below_above_split_real(self):
        result = parse_liquidation_map(self.SAMPLE_REAL, "BTC", "1d", current_price=74000)
        grp = result.leverage_groups[0]
        long_prices = sorted(b.price_from for b in grp.long_bands)
        short_prices = sorted(b.price_from for b in grp.short_bands)
        assert long_prices == [70000.0]
        assert short_prices == [75000.0, 80000.0]

    def test_by_exchange_retained(self):
        """关键回归：保留按交易所明细，未来支持高级筛选。"""
        result = parse_liquidation_map(self.SAMPLE_REAL, "BTC", "1d", current_price=74000)
        assert result.by_exchange is not None
        assert set(result.by_exchange.keys()) == {"Binance", "OKX"}
        assert result.by_exchange["Binance"]["70000"] == 5_000_000
        assert result.by_exchange["Binance"]["80000"] == 2_000_000
        assert result.by_exchange["OKX"]["70000"] == 1_500_000
        assert result.by_exchange["OKX"]["75000"] == 3_000_000

    def test_legacy_flat_data_compat(self):
        """旧路径：data 直接是 list（_request 解包后），仍应工作。"""
        flat = {"data": [{"liqMapV2": {"70000": [[70000, 5_000_000]]}}], "last_price": 74000}
        result = parse_liquidation_map(flat, "BTC", "1d", current_price=74000)
        assert result is not None
        assert result.leverage_groups[0].long_total_usd == 5_000_000

    def test_empty_data(self):
        assert parse_liquidation_map(None, "BTC", "1d") is None
        assert parse_liquidation_map({"data": []}, "BTC", "1d") is None
        assert parse_liquidation_map({"data": [], "last_price": 0}, "BTC", "1d") is None

    def test_decimal_prices(self):
        data = {
            "code": "0",
            "data": {
                "data": [{"liqMapV2": {"3456.78": [[3456.78, 100_000]]},
                          "instrument": {"exName": "Binance"}}],
                "last_price": 3000,
            },
        }
        result = parse_liquidation_map(data, "ETH", "1d", current_price=3000)
        assert result is not None

    def test_cached_source_observation_time_is_not_restamped(self):
        payload = {**self.SAMPLE_REAL, "_source_observed_at": 1_700_000_000}
        first = parse_liquidation_map(payload, "BTC", "1d", current_price=74000)
        second = parse_liquidation_map(payload, "BTC", "1d", current_price=74000)
        assert first is not None and second is not None
        assert first.ts == second.ts == 1_700_000_000

    def test_missing_source_observation_time_fails_closed(self):
        result = parse_liquidation_map(self.SAMPLE_REAL, "BTC", "1d", current_price=74000)
        assert result is not None
        assert result.ts == 0


# ──────────────────────────────────────────────
# parse_liq_heatmap · 真实 y_axis + 三元组结构
# ──────────────────────────────────────────────

class TestParseLiqHeatmap:
    """模拟 aggregated-heatmap/model1 的真实结构。"""

    SAMPLE_REAL = {
        "y_axis": [70000.0, 75000.0, 80000.0],   # 价格刻度
        "liquidation_leverage_data": [
            [1, 0, 1_000_000],   # t_idx=1, p_idx=0(70000), 瞬时投影=1M
            [1, 2, 2_000_000],   # t_idx=1, p_idx=2(80000), 瞬时投影=2M
            [2, 0, 500_000],     # 同价位另一切片：0.5M（应被 MAX 忽略）
            [3, 1, 3_000_000],   # 75000 价位 3M
        ],
        "price_candlesticks": [
            [1777240500, "78525.5", "78994.8", "78299.6", "78369.7", "100"],
            [1777240800, "78369.6", "78544.1", "78307", "78367.3", "100"],
        ],
        "update_time": 1777283472660,
    }

    def test_basic_aggregation(self):
        """同 price 多个切片应取 MAX（窗口内最大瞬时投影），不再累加。"""
        hm = parse_liq_heatmap(self.SAMPLE_REAL, "BTC", "24h")
        assert hm is not None
        assert hm.range == "24h"
        assert hm.model == 1
        prices = {p.price: p.value for p in hm.data}
        assert prices == {
            70000.0: 1_000_000,   # max(1M, 0.5M)
            75000.0: 3_000_000,
            80000.0: 2_000_000,
        }

    def test_ts_uses_last_candlestick(self):
        hm = parse_liq_heatmap(self.SAMPLE_REAL, "BTC", "24h")
        assert hm.data[0].ts == 1777240800   # 取 candlesticks 末根

    def test_invalid_indices_skipped(self):
        bad = {
            "y_axis": [70000.0, 75000.0],
            "liquidation_leverage_data": [
                [0, 99, 1_000_000],   # p_idx 越界
                [0, -1, 500_000],     # 负索引
                [0, 0, -100],         # 负 USD
                [0, 1, 200_000],      # 合法
            ],
            "price_candlesticks": [],
        }
        hm = parse_liq_heatmap(bad, "BTC", "24h")
        assert hm is not None
        assert {p.price: p.value for p in hm.data} == {75000.0: 200_000}

    def test_empty_returns_none(self):
        assert parse_liq_heatmap({}, "BTC", "24h") is None
        assert parse_liq_heatmap({"y_axis": []}, "BTC", "24h") is None
        assert parse_liq_heatmap(None, "BTC", "24h") is None


# ──────────────────────────────────────────────
# poll_liq_heatmap · key 必须为 "24h"/"7d"（不带 m1_ 前缀）
# ──────────────────────────────────────────────

class TestPollLiqHeatmap:

    @pytest.mark.asyncio
    async def test_writes_with_clean_key(self, cg, btc_state):
        cg.fetch_liquidation_aggregated_heatmap = AsyncMock(return_value={
            "y_axis": [70000.0, 80000.0],
            "liquidation_leverage_data": [[0, 0, 1_000_000], [0, 1, 2_000_000]],
            "price_candlesticks": [],
        })
        coin = type("CoinCfg", (), {"ccy": "BTC", "symbol_cg": "BTC"})()
        await poll_liq_heatmap(cg, coin, btc_state, ranges=("24h",))

        # 旧版本写入 "m1_24h" → P0 错位；本次必须是 "24h"
        assert "24h" in btc_state.liq_heatmaps
        assert "m1_24h" not in btc_state.liq_heatmaps
        hm = btc_state.liq_heatmaps["24h"]
        assert len(hm.data) == 2

    @pytest.mark.asyncio
    async def test_empty_does_not_overwrite(self, cg, btc_state):
        from models.liquidation import HeatmapData, HeatmapDataPoint
        btc_state.liq_heatmaps["24h"] = HeatmapData(
            coin="BTC", ts=1, model=1, range="24h",
            data=[HeatmapDataPoint(price=70000, value=1)],
        )
        cg.fetch_liquidation_aggregated_heatmap = AsyncMock(return_value=None)
        coin = type("CoinCfg", (), {"ccy": "BTC", "symbol_cg": "BTC"})()
        await poll_liq_heatmap(cg, coin, btc_state, ranges=("24h",))
        # 拉取失败时不应清掉上一周期的好数据
        assert btc_state.liq_heatmaps["24h"].data[0].price == 70000


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
# poll_liq_max_pain · 真实 4 字段 + supported_coins 过滤
# ──────────────────────────────────────────────

class TestPollLiqMaxPain:
    """真实 API 字段：long_max_pain_liq_level/price + short_max_pain_liq_level/price"""

    SAMPLE_REAL = [
        {
            "symbol": "BTC", "price": 77903.2,
            "long_max_pain_liq_level": 86_909_802.27,
            "long_max_pain_liq_price": 76963.86,
            "short_max_pain_liq_level": 86_909_802.27,
            "short_max_pain_liq_price": 78536.6,
        },
        {
            "symbol": "ETH", "price": 2322.19,
            "long_max_pain_liq_level": 36_421_810.65,
            "long_max_pain_liq_price": 2300.022,
            "short_max_pain_liq_level": 43_002_119.43,
            "short_max_pain_liq_price": 2345.056,
        },
        # 大量无关币种应被 supported_coins 过滤掉
        {
            "symbol": "XRP", "price": 1.41,
            "long_max_pain_liq_level": 1e6,
            "long_max_pain_liq_price": 1.40,
            "short_max_pain_liq_level": 2e6,
            "short_max_pain_liq_price": 1.44,
        },
    ]

    @pytest.mark.asyncio
    async def test_real_fields_extracted(self, cg, btc_state, states):
        cg.fetch_liquidation_max_pain = AsyncMock(return_value=self.SAMPLE_REAL)
        await poll_liq_max_pain(cg, ["BTC", "ETH"], states)

        pain_24h = btc_state.liq_max_pain.get("24h")
        assert pain_24h is not None
        # supported_coins 过滤后只剩 BTC + ETH，不含 XRP
        symbols = {it.symbol for it in pain_24h.items}
        assert symbols == {"BTC", "ETH"}

        btc_item = next(it for it in pain_24h.items if it.symbol == "BTC")
        assert btc_item.price == 77903.2
        assert btc_item.long_pain_price == 76963.86
        assert btc_item.long_pain_usd == 86_909_802.27
        assert btc_item.short_pain_price == 78536.6
        assert btc_item.short_pain_usd == 86_909_802.27

    @pytest.mark.asyncio
    async def test_no_legacy_fields(self, cg, btc_state, states):
        """旧字段名 long_liq_usd/short_liq_usd 已彻底删除，不应再被访问。"""
        cg.fetch_liquidation_max_pain = AsyncMock(return_value=self.SAMPLE_REAL)
        await poll_liq_max_pain(cg, ["BTC"], states)
        item = btc_state.liq_max_pain["24h"].items[0]
        assert not hasattr(item, "long_liq_usd")
        assert not hasattr(item, "short_liq_usd")

    @pytest.mark.asyncio
    async def test_pick_for_coin_isolates_per_coin(self, cg, states):
        """跨币种回归（保护 P1-E）：BTC/ETH state 共享同一 LiqMaxPainData 引用，
        但 _pick_max_pain_for_coin 必须只取出当前 coin 的 item，不能让 BTC
        socket payload 看到 ETH 痛点。
        """
        from engine import _pick_max_pain_for_coin
        cg.fetch_liquidation_max_pain = AsyncMock(return_value=self.SAMPLE_REAL)
        await poll_liq_max_pain(cg, ["BTC", "ETH"], states)

        btc_pain = states["BTC"].liq_max_pain["24h"]
        eth_pain = states["ETH"].liq_max_pain["24h"]
        assert btc_pain is eth_pain, "poll 层共享同一引用是预期实现（节省内存）"

        btc_item = _pick_max_pain_for_coin(btc_pain, "BTC")
        eth_item = _pick_max_pain_for_coin(eth_pain, "ETH")
        assert btc_item is not None and btc_item.symbol == "BTC"
        assert eth_item is not None and eth_item.symbol == "ETH"
        assert btc_item.long_pain_price != eth_item.long_pain_price

        # 不支持币种应返回 None
        assert _pick_max_pain_for_coin(btc_pain, "DOGE") is None
        assert _pick_max_pain_for_coin(None, "BTC") is None
