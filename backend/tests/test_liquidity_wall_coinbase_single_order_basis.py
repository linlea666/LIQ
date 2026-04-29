"""W2-T4 单元测试：Coinbase 单笔大单分支 + USD/USDT 基差。

覆盖：
  Coinbase max_single_order_usd 字段：
    - 单 level 高 USD + num_orders=1 → max_single 等于该 level USD
    - 单 level 高 USD + num_orders=10 → max_single = USD/10（散户 cluster）
    - 多个 level 取最大单笔
    - 仅 overlap >= 0.5 的 level 参与统计（边缘 level 不污染）
    - num_orders=0 视为 1 笔（避免 div-by-zero）

  SR 加分（W4-T1 阶段 1.1 阶梯化升级）：
    - coinbase_max_single_order_usd ≥ 100k → SR +0.03 (大额订单)
    - coinbase_max_single_order_usd ≥ 1M  → SR +0.10 (机构级，与前端 ★ 对齐)
    - < 100k → SR 不加分（散户级别）
    - 取最高匹配档加分一次，不叠加

  USD/USDT basis：
    - coinbase_mid > ticker_last → 正值（USD 比 USDT 贵）
    - coinbase_mid < ticker_last → 负值
    - coinbase_frame=None → None
    - ticker_last 缺失 → None
    - bids/asks 空 → None
    - 异常 best_ask < best_bid → None
"""
from __future__ import annotations

import os
import sys
from collections import deque
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from models.coinbase_orderbook import CoinbaseBookLevel, CoinbaseOrderbookFrame
from models.orderbook_pressure import (
    DepthBin,
    OrderbookDepthSnapshot,
    OrderbookPressureSnapshot,
    WallZone,
)
from processors.liquidity_wall_engine import (
    ENGINE_DEFAULTS,
    _augment_zones_with_coinbase,
    _compute_support_resistance_trust_score,
    _compute_usd_usdt_basis_pct,
    build_liquidity_wall_outputs,
)


def _make_zone(side: str = "bid", peak: float = 76050.0) -> WallZone:
    return WallZone(
        side=side, price_low=peak - 25, price_high=peak + 25,
        price_mid=peak, peak_price=peak, distance_pct=-0.30,
        current_usd=2_000_000, max_usd_1h=2_000_000, avg_usd_1h=2_000_000,
        bin_count=3, seen_count=10, visible_minutes=40, persistence_score=0.5,
    )


def _make_cb_frame(
    coin: str = "BTC",
    bids: list[tuple[float, float, int]] = None,
    asks: list[tuple[float, float, int]] = None,
    ts_sec: int = 1_700_000_000,
) -> CoinbaseOrderbookFrame:
    bids = bids or []
    asks = asks or []
    return CoinbaseOrderbookFrame(
        coin=coin, product_id=f"{coin}-USD", ts_sec=ts_sec,
        bids=[CoinbaseBookLevel(price=p, size=s, num_orders=n) for p, s, n in bids],
        asks=[CoinbaseBookLevel(price=p, size=s, num_orders=n) for p, s, n in asks],
        bid_count=len(bids), ask_count=len(asks),
    )


# ─────────────────────────────────────────────────────────────────
# 1. Coinbase max_single_order_usd 字段
# ─────────────────────────────────────────────────────────────────

class TestCoinbaseMaxSingleOrder:
    def test_single_level_one_order_full_usd(self):
        """单 level 1 笔订单 → max_single = level.usd_value。"""
        zone = _make_zone(side="bid", peak=76050)
        # 1 笔 5 BTC × 76050 = 380_250 USD（孤立机构大单）
        frame = _make_cb_frame(bids=[(76050, 5.0, 1)])
        _augment_zones_with_coinbase([zone], frame, ENGINE_DEFAULTS)
        assert zone.coinbase_max_single_order_usd == pytest.approx(380_250, abs=10)

    def test_single_level_many_orders_per_order_usd(self):
        """单 level 10 笔订单 → max_single = USD / 10（散户 cluster）。"""
        zone = _make_zone(side="bid", peak=76050)
        # 10 笔合计 5 BTC × 76050 = 380_250；平均单笔 38_025（散户级）
        frame = _make_cb_frame(bids=[(76050, 5.0, 10)])
        _augment_zones_with_coinbase([zone], frame, ENGINE_DEFAULTS)
        assert zone.coinbase_max_single_order_usd == pytest.approx(38_025, abs=10)

    def test_max_across_multiple_levels(self):
        """多 level 取最大单笔（机构区可能在 zone 内某个 level）。"""
        zone = _make_zone(side="bid", peak=76050)
        # 三个 level 都在 zone 内；最大单笔 = 第二个 level 的 200_000
        frame = _make_cb_frame(bids=[
            (76040, 1.0, 5),    # 76040 / 5 ≈ 15_208 per order
            (76050, 5.0, 2),    # 380250 / 2 = 190_125 per order ← max
            (76060, 0.5, 1),    # 38_030 per order
        ])
        _augment_zones_with_coinbase([zone], frame, ENGINE_DEFAULTS)
        assert zone.coinbase_max_single_order_usd == pytest.approx(190_125, abs=10)

    def test_zero_num_orders_treated_as_one(self):
        """num_orders=0（异常数据）→ 视为 1 笔，避免 div-by-zero。"""
        zone = _make_zone(side="bid", peak=76050)
        frame = _make_cb_frame(bids=[(76050, 5.0, 0)])
        _augment_zones_with_coinbase([zone], frame, ENGINE_DEFAULTS)
        # 视为 1 笔 → 完整 380_250 USD
        assert zone.coinbase_max_single_order_usd == pytest.approx(380_250, abs=10)

    def test_no_coinbase_data_keeps_zero(self):
        """coinbase_frame=None → 字段保持 0。"""
        zone = _make_zone(side="bid", peak=76050)
        _augment_zones_with_coinbase([zone], None, ENGINE_DEFAULTS)
        assert zone.coinbase_max_single_order_usd == 0.0

    def test_only_main_overlap_levels_count(self):
        """边缘 level（overlap < 0.5）不参与单笔统计，避免边缘漂移污染。
           但深度共振统计（cb_usd）仍按比例累加。"""
        zone = _make_zone(side="bid", peak=76050)  # 范围 [76025, 76075]，tol 后扩展
        # 76050 是 zone 中心 level → overlap >= 0.5 → 计入单笔
        # 76200 是远离 zone 的 level → overlap = 0 → 完全不计入
        frame = _make_cb_frame(bids=[
            (76050, 1.0, 1),    # 中心：76050 USD per order
            (76200, 100.0, 1),  # 远 level：完全不在 zone 内，不计入
        ])
        _augment_zones_with_coinbase([zone], frame, ENGINE_DEFAULTS)
        # 远 level 完全在 zone 外（overlap=0）→ 单笔统计仅看中心 76050
        assert zone.coinbase_max_single_order_usd == pytest.approx(76_050, abs=10)


# ─────────────────────────────────────────────────────────────────
# 2. SR 机构 footprint 加分
# ─────────────────────────────────────────────────────────────────

class TestSRInstitutionalFootprint:
    def test_100k_tier_adds_003_to_sr(self):
        """≥ 100k USD/笔 → SR +0.03（W4-T1 阶段 1.1 阶梯化：大额订单档）。"""
        zone_no_inst = _make_zone()
        zone_no_inst.coinbase_max_single_order_usd = 50_000  # 散户级
        zone_inst = _make_zone()
        zone_inst.coinbase_max_single_order_usd = 150_000

        sr_no = _compute_support_resistance_trust_score(zone_no_inst, ENGINE_DEFAULTS)
        sr_yes = _compute_support_resistance_trust_score(zone_inst, ENGINE_DEFAULTS)
        assert sr_yes - sr_no == pytest.approx(0.03, abs=0.001)

    def test_1m_institutional_tier_adds_010_to_sr(self):
        """≥ 1M USD/笔 → SR +0.10（W4-T1 阶段 1.1 阶梯化：机构级，与前端 ★ 对齐）。

        旧逻辑 100k 一刀切 +0.05 → 100k 单和 4464 万单完全等同。
        新阶梯让"机构级"信号在 ZoneDetailCard 的 support_trust 上有可感知差异。
        """
        zone_no_inst = _make_zone()
        zone_no_inst.coinbase_max_single_order_usd = 50_000
        zone_inst = _make_zone()
        zone_inst.coinbase_max_single_order_usd = 2_000_000

        sr_no = _compute_support_resistance_trust_score(zone_no_inst, ENGINE_DEFAULTS)
        sr_yes = _compute_support_resistance_trust_score(zone_inst, ENGINE_DEFAULTS)
        assert sr_yes - sr_no == pytest.approx(0.10, abs=0.001)

    def test_below_100k_no_bonus(self):
        """< 100k USD/笔 → 不加分。"""
        zone = _make_zone()
        zone.coinbase_max_single_order_usd = 99_999
        sr = _compute_support_resistance_trust_score(zone, ENGINE_DEFAULTS)
        # 仅 base 0.30
        assert sr == pytest.approx(0.30, abs=0.001)


# ─────────────────────────────────────────────────────────────────
# 3. USD/USDT basis
# ─────────────────────────────────────────────────────────────────

class TestUSDUSDTBasis:
    def test_positive_basis_when_usd_premium(self):
        """coinbase_mid > ticker_last → 正基差（USD 比 USDT 贵）。"""
        # coinbase mid = (76095 + 76105) / 2 = 76100；ticker = 76050
        # basis = (76100 - 76050) / 76050 × 100 ≈ 0.0658%
        frame = _make_cb_frame(bids=[(76095, 1.0, 1)], asks=[(76105, 1.0, 1)])
        basis = _compute_usd_usdt_basis_pct(frame, ticker_last=76050.0)
        assert basis is not None and basis == pytest.approx(0.0658, abs=0.001)

    def test_negative_basis_when_usd_discount(self):
        """coinbase_mid < ticker_last → 负基差。"""
        frame = _make_cb_frame(bids=[(76020, 1.0, 1)], asks=[(76030, 1.0, 1)])
        basis = _compute_usd_usdt_basis_pct(frame, ticker_last=76100.0)
        # mid=76025；(76025-76100)/76100 × 100 ≈ -0.0986
        assert basis is not None and basis < 0
        assert basis == pytest.approx(-0.0986, abs=0.001)

    def test_none_frame_returns_none(self):
        assert _compute_usd_usdt_basis_pct(None, ticker_last=76100.0) is None

    def test_none_ticker_returns_none(self):
        frame = _make_cb_frame(bids=[(76095, 1.0, 1)], asks=[(76105, 1.0, 1)])
        assert _compute_usd_usdt_basis_pct(frame, ticker_last=None) is None
        assert _compute_usd_usdt_basis_pct(frame, ticker_last=0.0) is None
        assert _compute_usd_usdt_basis_pct(frame, ticker_last=-1.0) is None

    def test_empty_book_returns_none(self):
        frame = _make_cb_frame(bids=[], asks=[])
        assert _compute_usd_usdt_basis_pct(frame, ticker_last=76100.0) is None

    def test_only_one_side_returns_none(self):
        """缺 bids 或 asks → None（无法算 mid）。"""
        frame = _make_cb_frame(bids=[(76095, 1.0, 1)], asks=[])
        assert _compute_usd_usdt_basis_pct(frame, ticker_last=76100.0) is None

    def test_inverted_book_returns_none(self):
        """异常：best_ask < best_bid → None（数据损坏，不强行算）。"""
        frame = _make_cb_frame(bids=[(76200, 1.0, 1)], asks=[(76050, 1.0, 1)])
        assert _compute_usd_usdt_basis_pct(frame, ticker_last=76100.0) is None


# ─────────────────────────────────────────────────────────────────
# 4. 端到端：主流程写入 basis 字段
# ─────────────────────────────────────────────────────────────────

class TestEndToEndBasisIntegration:
    def _make_state(self) -> SimpleNamespace:
        ts_base = 1_700_000_000
        history = []
        for i in range(8):
            bids = [DepthBin(price=p, quantity=q, usd_value=p*q)
                    for p, q in [(76045, 6), (76050, 60), (76055, 4)]]
            asks = [DepthBin(price=p, quantity=q, usd_value=p*q)
                    for p, q in [(76145, 4), (76150, 60), (76155, 4)]]
            history.append(OrderbookDepthSnapshot(
                coin="BTC", exchange="Binance", symbol="BTCUSDT",
                ts_sec=ts_base + i * 300, bids=bids, asks=asks,
            ))
        return SimpleNamespace(
            coin="BTC",
            ticker=SimpleNamespace(last=76100.0),
            atr=200.0,
            orderbook_depth_history=deque(history),
            spot_orderbook_depth_history=deque(),
            large_orders_history=[],
            spot_large_orders_history=[],
            taker_flow=None, cvd_spot=None,
            aggregated_ask_bids_history=[],
            spot_aggregated_ask_bids_history=[],
            coinbase_orderbook=_make_cb_frame(
                bids=[(76095, 1.0, 1)], asks=[(76110, 1.0, 1)],
            ),
            liq_max_pain=None,
            oi_exchange_rank=None, multi_funding=None, ls_ratio=None,
        )

    def test_main_entry_outputs_basis(self):
        state = self._make_state()
        base_snap = OrderbookPressureSnapshot(
            coin="BTC", ts_sec=1_700_000_000, last_price=76100.0,
            atr=200.0, walls=[],
        )
        out = build_liquidity_wall_outputs(state, base_snap, dict(ENGINE_DEFAULTS), now=1_700_002_400)
        # coinbase mid = (76095 + 76110) / 2 = 76102.5
        # basis = (76102.5 - 76100) / 76100 × 100 = 0.00328%
        assert out.usd_usdt_basis_pct is not None
        assert out.usd_usdt_basis_pct == pytest.approx(0.00328, abs=0.001)

    def test_main_entry_no_coinbase_basis_none(self):
        state = self._make_state()
        state.coinbase_orderbook = None
        base_snap = OrderbookPressureSnapshot(
            coin="BTC", ts_sec=1_700_000_000, last_price=76100.0,
            atr=200.0, walls=[],
        )
        out = build_liquidity_wall_outputs(state, base_snap, dict(ENGINE_DEFAULTS), now=1_700_002_400)
        assert out.usd_usdt_basis_pct is None
