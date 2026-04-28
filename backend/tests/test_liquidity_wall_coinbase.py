"""测试 Phase C：Coinbase 现货 → 流动性墙引擎集成。

覆盖：
  _augment_zones_with_coinbase
    - 价位完全匹配 → coinbase_spot_confluence=True
    - 容差内匹配（USD/USDT spread 5bp 之内）
    - USD 量级低于阈值（< wall_min × 0.30）→ 不算共振
    - num_orders < 3 → 不算共振（spoof 嫌疑）
    - 侧匹配：bid zone 只看 Coinbase bids
    - 多档累加
    - 距离过远 → 不匹配
    - 空 frame → 安全 noop
    - None frame → 安全 noop
  _compute_trust_score
    - coinbase_spot_confluence 单独 +0.10
    - 与 dual_source 叠加（不替换）
    - 与 has_spot_confluence 叠加
    - clamp 1.0
    - 关闭加分（cfg=0）
  build_liquidity_wall_outputs end-to-end
    - state.coinbase_orderbook 注入 → walls 含 coinbase_spot_confluence
    - 仍向后兼容：state 无 coinbase_orderbook → zone.coinbase_spot_confluence=False
  AI snapshot
    - _build_liquidity_wall_block 把 coinbase_confluence 字段写入 wall_dict
"""

from __future__ import annotations

from collections import deque
from types import SimpleNamespace

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
    _compute_trust_score,
    build_liquidity_wall_outputs,
)


# ──────────────────────────────────────────────────────────────────────
# 工具
# ──────────────────────────────────────────────────────────────────────
def _make_zone(
    side: str = "bid",
    price_low: float = 99_990.0,
    price_high: float = 100_010.0,
    peak_price: float = 100_000.0,
    current_usd: float = 5_000_000.0,
) -> WallZone:
    return WallZone(
        side=side,
        price_low=price_low,
        price_high=price_high,
        price_mid=(price_low + price_high) / 2,
        peak_price=peak_price,
        distance_pct=0.0,
        current_usd=current_usd,
        max_usd_1h=current_usd,
        avg_usd_1h=current_usd,
        bin_count=3,
        seen_count=12,
        visible_minutes=60.0,
        persistence_score=1.0,
    )


def _make_cb_frame(
    bids: list[tuple[float, float, int]] | None = None,
    asks: list[tuple[float, float, int]] | None = None,
) -> CoinbaseOrderbookFrame:
    return CoinbaseOrderbookFrame(
        coin="BTC",
        product_id="BTC-USD",
        ts_sec=1714303200,
        api_ts_iso="2026-04-28T17:44:20Z",
        sequence=1,
        bids=[CoinbaseBookLevel(price=p, size=s, num_orders=n)
              for p, s, n in (bids or [])],
        asks=[CoinbaseBookLevel(price=p, size=s, num_orders=n)
              for p, s, n in (asks or [])],
        bid_count=len(bids or []),
        ask_count=len(asks or []),
    )


# ──────────────────────────────────────────────────────────────────────
# _augment_zones_with_coinbase
# ──────────────────────────────────────────────────────────────────────
def test_coinbase_augment_exact_match_marks_confluence():
    """zone 价区 [99990, 100010] 含 BTC-USD 大单，多笔聚集 → 共振。"""
    zone = _make_zone(side="bid")
    frame = _make_cb_frame(bids=[
        (99_995.0, 5.0, 4),     # 5 BTC × $99,995 ≈ $500k，4 笔
        (100_005.0, 10.0, 5),   # 10 BTC × $100,005 ≈ $1M，5 笔
    ])
    cfg = dict(ENGINE_DEFAULTS)
    _augment_zones_with_coinbase([zone], frame, cfg)

    assert zone.coinbase_spot_confluence is True
    assert zone.coinbase_spot_usd > 1_400_000  # 累加约 $1.5M
    assert zone.coinbase_num_orders == 9       # 4 + 5


def test_coinbase_augment_within_tolerance_5bp():
    """USD/USDT spread 模拟：Coinbase 价位偏移 5bp（10bp tolerance 内应匹配）。"""
    zone = _make_zone(side="ask", price_low=100_000.0, price_high=100_100.0,
                      peak_price=100_050.0)
    frame = _make_cb_frame(asks=[
        # tol = max(100050 × 0.001, 5.0) = 100.05 → 上界 100200.05
        (100_150.0, 5.0, 5),  # 价格在 zone 上界 100100 + 容差 100 内 → 匹配
    ])
    cfg = dict(ENGINE_DEFAULTS)
    _augment_zones_with_coinbase([zone], frame, cfg)
    assert zone.coinbase_spot_confluence is True
    assert zone.coinbase_num_orders == 5


def test_coinbase_augment_low_usd_no_confluence():
    """USD 量级 < wall_min × 0.30 → 不算共振，但仍记录 coinbase_spot_usd。"""
    zone = _make_zone()
    cfg = dict(ENGINE_DEFAULTS)
    # wall_min=500k，30% = 150k 门槛；构造 50k 的小堆
    frame = _make_cb_frame(bids=[(100_000.0, 0.5, 5)])  # $50k
    _augment_zones_with_coinbase([zone], frame, cfg)

    assert zone.coinbase_spot_confluence is False
    assert zone.coinbase_spot_usd == 50_000.0          # 仍写入字段
    assert zone.coinbase_num_orders == 5


def test_coinbase_augment_low_num_orders_no_confluence():
    """USD 量级合格但 num_orders < 3 → 单大单 spoof 嫌疑，不算共振。"""
    zone = _make_zone()
    cfg = dict(ENGINE_DEFAULTS)
    # 1 笔订单 × 大 USD —— 典型的"鲸鱼挂单可能撤单"
    frame = _make_cb_frame(bids=[(100_000.0, 20.0, 1)])  # $2M / 1 笔
    _augment_zones_with_coinbase([zone], frame, cfg)

    assert zone.coinbase_spot_confluence is False
    assert zone.coinbase_num_orders == 1
    assert zone.coinbase_spot_usd > 1_000_000          # USD 通过但 num_orders 拦截


def test_coinbase_augment_side_match_bid_only_uses_bids():
    """bid zone 只看 Coinbase bids，asks 同价位也不参与。"""
    zone = _make_zone(side="bid")
    frame = _make_cb_frame(
        bids=[(99_999.0, 0.1, 1)],                    # bid 太小
        asks=[(100_001.0, 100.0, 10)],                # ask 巨大但被忽略
    )
    cfg = dict(ENGINE_DEFAULTS)
    _augment_zones_with_coinbase([zone], frame, cfg)

    assert zone.coinbase_spot_confluence is False
    assert zone.coinbase_num_orders == 1               # 只数了 bids


def test_coinbase_augment_distance_exceeds_tolerance_no_match():
    """Coinbase 价位远超容差 → 不计入。"""
    zone = _make_zone(side="bid", price_low=99_990.0, price_high=100_010.0,
                      peak_price=100_000.0)
    frame = _make_cb_frame(bids=[
        (95_000.0, 100.0, 10),                        # 距离 -5%，超出容差
    ])
    cfg = dict(ENGINE_DEFAULTS)
    _augment_zones_with_coinbase([zone], frame, cfg)
    assert zone.coinbase_spot_confluence is False
    assert zone.coinbase_num_orders == 0
    assert zone.coinbase_spot_usd == 0.0


def test_coinbase_augment_empty_frame_safe_noop():
    zone = _make_zone()
    frame = _make_cb_frame(bids=[], asks=[])
    cfg = dict(ENGINE_DEFAULTS)
    _augment_zones_with_coinbase([zone], frame, cfg)
    assert zone.coinbase_spot_confluence is False
    assert zone.coinbase_num_orders == 0


def test_coinbase_augment_none_frame_safe_noop():
    zone = _make_zone()
    cfg = dict(ENGINE_DEFAULTS)
    _augment_zones_with_coinbase([zone], None, cfg)
    assert zone.coinbase_spot_confluence is False


def test_coinbase_augment_multiple_zones_independent():
    """多个 zone 各自独立计算，互不影响。"""
    z_bid = _make_zone(side="bid", price_low=99_990, price_high=100_010,
                       peak_price=100_000)
    z_ask = _make_zone(side="ask", price_low=110_000, price_high=110_100,
                       peak_price=110_050)
    frame = _make_cb_frame(
        bids=[(100_000.0, 5.0, 4)],                   # 命中 z_bid
        asks=[(110_050.0, 10.0, 5)],                  # 命中 z_ask
    )
    cfg = dict(ENGINE_DEFAULTS)
    _augment_zones_with_coinbase([z_bid, z_ask], frame, cfg)

    assert z_bid.coinbase_spot_confluence is True
    assert z_ask.coinbase_spot_confluence is True
    assert z_bid.coinbase_num_orders == 4
    assert z_ask.coinbase_num_orders == 5


# ──────────────────────────────────────────────────────────────────────
# _compute_trust_score
# ──────────────────────────────────────────────────────────────────────
def test_trust_score_coinbase_alone_adds_010():
    """仅 coinbase_spot_confluence=True，其他都关 → base 0.50 + 0.10 = 0.60。"""
    z = _make_zone()
    z.coinbase_spot_confluence = True
    z.dual_source = False
    z.has_spot_confluence = False
    z.exchange_count = 1
    z.persistence_score = 0.0
    cfg = dict(ENGINE_DEFAULTS)
    score = _compute_trust_score(z, cfg)
    assert score == pytest.approx(0.60, abs=1e-6)


def test_trust_score_coinbase_plus_dual_source():
    """coinbase + dual_source + persistent → 0.50 + 0.30 + 0.10 + 0.10 = 1.00。"""
    z = _make_zone()
    z.dual_source = True
    z.coinbase_spot_confluence = True
    z.persistence_score = 0.80   # > 0.70 触发 persistent 加分
    cfg = dict(ENGINE_DEFAULTS)
    score = _compute_trust_score(z, cfg)
    assert score == pytest.approx(1.00, abs=1e-6)


def test_trust_score_all_dimensions_clamped_to_1():
    """全部维度都 True → 1.05 → clamp 1.0。"""
    z = _make_zone()
    z.dual_source = True
    z.has_spot_confluence = True
    z.coinbase_spot_confluence = True
    z.exchange_count = 3   # 触发 multi_exchange
    z.persistence_score = 1.0
    cfg = dict(ENGINE_DEFAULTS)
    score = _compute_trust_score(z, cfg)
    assert score == pytest.approx(1.0, abs=1e-6)


def test_trust_score_coinbase_disabled_via_cfg():
    """trust_bonus_coinbase_confluence=0.0 → coinbase 共振不加分。"""
    z = _make_zone()
    z.coinbase_spot_confluence = True
    z.dual_source = False
    z.persistence_score = 0.0
    cfg = dict(ENGINE_DEFAULTS)
    cfg["trust_bonus_coinbase_confluence"] = 0.0
    score = _compute_trust_score(z, cfg)
    assert score == pytest.approx(0.50, abs=1e-6)


def test_trust_score_coinbase_only_max_065_without_dual():
    """coinbase + has_spot_confluence + persistent，无 dual_source → 0.50 + 0.10 + 0.15 + 0.10 = 0.85。"""
    z = _make_zone()
    z.dual_source = False
    z.has_spot_confluence = True
    z.coinbase_spot_confluence = True
    z.persistence_score = 0.80
    cfg = dict(ENGINE_DEFAULTS)
    score = _compute_trust_score(z, cfg)
    assert score == pytest.approx(0.85, abs=1e-6)


# ──────────────────────────────────────────────────────────────────────
# build_liquidity_wall_outputs end-to-end
# ──────────────────────────────────────────────────────────────────────
def _make_frame(ts: int, bids: list[tuple[float, float]],
                asks: list[tuple[float, float]]) -> OrderbookDepthSnapshot:
    return OrderbookDepthSnapshot(
        coin="BTC", exchange="Binance", symbol="BTCUSDT",
        ts_sec=ts,
        bids=[DepthBin(price=p, quantity=q, usd_value=p * q) for p, q in bids],
        asks=[DepthBin(price=p, quantity=q, usd_value=p * q) for p, q in asks],
    )


def test_build_outputs_with_coinbase_orderbook_marks_confluence():
    """状态机 + Coinbase frame 注入 → 输出 zone 含 coinbase_spot_confluence=True。"""
    last_price = 100_000.0
    # 12 帧持续可见的合约买墙在 99990-100010
    history = []
    for i in range(12):
        history.append(_make_frame(
            ts=1_000_000 + i * 300,
            bids=[(99_995.0 + j * 5, 80.0) for j in range(3)],  # 3 个 bin × 80 BTC ≈ $24M
            asks=[],
        ))

    coinbase_frame = _make_cb_frame(bids=[
        (99_998.0, 5.0, 4),    # ~$500k × 4 笔
        (100_005.0, 10.0, 5),  # ~$1M × 5 笔
    ])

    state = SimpleNamespace(
        coin="BTC",
        ticker=SimpleNamespace(last=last_price),
        atr=200.0,
        orderbook_depth_history=deque(history, maxlen=12),
        spot_orderbook_depth_history=deque(),
        large_orders_history=[],
        spot_large_orders_history=[],
        coinbase_orderbook=coinbase_frame,
        oi_exchange_rank=None,
        multi_funding=None,
        funding_history_8h=None,
        ls_ratio=None,
        top_position_ratio=None,
        taker_flow=None,
        cvd_spot=None,
        liq_max_pain={},
        liq_summary=None,
        candles_4h=[],
    )
    base = OrderbookPressureSnapshot(
        coin="BTC", ts_sec=2_000_000, last_price=last_price, atr=200.0,
    )
    cfg = {**ENGINE_DEFAULTS, "wall_min_usd": 500_000, "seed_min_usd": 500_000}
    out = build_liquidity_wall_outputs(state, base, cfg, now=2_000_000)

    # 找下方买墙
    bid_zones = [z for z in out.walls_below if z.price_low <= 100_000 <= z.price_high
                 or (z.price_low <= 100_010 and z.price_high >= 99_990)]
    assert bid_zones, "至少有一个买墙覆盖 ~$100k"
    matched = [z for z in bid_zones if z.coinbase_spot_confluence]
    assert matched, "Coinbase 共振墙应被识别"
    z = matched[0]
    assert z.coinbase_num_orders == 9  # 4 + 5
    assert z.coinbase_spot_usd > 1_000_000
    # trust_score 比无 Coinbase 时高 0.10（base 0.50 + dual 0/spot 0/persistent 0.10 + coinbase 0.10）
    assert z.trust_score >= 0.60


def test_build_outputs_without_coinbase_orderbook_unchanged():
    """state.coinbase_orderbook=None → 全部 zone.coinbase_spot_confluence=False。"""
    last_price = 100_000.0
    history = []
    for i in range(12):
        history.append(_make_frame(
            ts=1_000_000 + i * 300,
            bids=[(99_995.0 + j * 5, 80.0) for j in range(3)],
            asks=[],
        ))

    state = SimpleNamespace(
        coin="BTC",
        ticker=SimpleNamespace(last=last_price),
        atr=200.0,
        orderbook_depth_history=deque(history, maxlen=12),
        spot_orderbook_depth_history=deque(),
        large_orders_history=[],
        spot_large_orders_history=[],
        coinbase_orderbook=None,
        oi_exchange_rank=None,
        multi_funding=None,
        funding_history_8h=None,
        ls_ratio=None,
        top_position_ratio=None,
        taker_flow=None,
        cvd_spot=None,
        liq_max_pain={},
        liq_summary=None,
        candles_4h=[],
    )
    base = OrderbookPressureSnapshot(
        coin="BTC", ts_sec=2_000_000, last_price=last_price, atr=200.0,
    )
    cfg = {**ENGINE_DEFAULTS, "wall_min_usd": 500_000, "seed_min_usd": 500_000}
    out = build_liquidity_wall_outputs(state, base, cfg, now=2_000_000)

    for z in out.walls_below + out.walls_above:
        assert z.coinbase_spot_confluence is False
        assert z.coinbase_spot_usd == 0.0
        assert z.coinbase_num_orders == 0


# ──────────────────────────────────────────────────────────────────────
# AI snapshot _build_liquidity_wall_block
# ──────────────────────────────────────────────────────────────────────
def test_ai_snapshot_includes_coinbase_fields():
    """墙含 coinbase_spot_confluence → AI dict 含 coinbase_confluence/usd/num_orders。"""
    from ai.snapshot import _build_liquidity_wall_block

    z = _make_zone(side="bid")
    z.dual_source = True            # 让墙通过严格筛选
    z.trust_score = 0.90
    z.distance_pct = -0.5
    z.coinbase_spot_confluence = True
    z.coinbase_spot_usd = 1_500_000.0
    z.coinbase_num_orders = 7

    pressure = OrderbookPressureSnapshot(
        coin="BTC", ts_sec=2_000_000, last_price=100_000.0, atr=200.0,
        walls_above=[],
        walls_below=[z],
    )
    out = _build_liquidity_wall_block(pressure, last_price=100_000.0)

    assert len(out["walls"]) == 1
    w = out["walls"][0]
    assert w.get("coinbase_confluence") is True
    assert w.get("coinbase_spot_usd") == 1_500_000
    assert w.get("coinbase_num_orders") == 7


def test_ai_snapshot_skips_coinbase_fields_when_no_confluence():
    """墙 coinbase_spot_confluence=False → AI dict 不带 coinbase_* keys（保持紧凑）。"""
    from ai.snapshot import _build_liquidity_wall_block

    z = _make_zone(side="ask")
    z.dual_source = True
    z.trust_score = 0.90
    z.coinbase_spot_confluence = False
    z.coinbase_spot_usd = 50_000.0          # 有数据但未 confluence
    z.coinbase_num_orders = 1

    pressure = OrderbookPressureSnapshot(
        coin="BTC", ts_sec=2_000_000, last_price=100_000.0, atr=200.0,
        walls_above=[z],
        walls_below=[],
    )
    out = _build_liquidity_wall_block(pressure, last_price=100_000.0)

    assert len(out["walls"]) == 1
    w = out["walls"][0]
    assert "coinbase_confluence" not in w   # 节省 prompt token
    assert "coinbase_spot_usd" not in w
    assert "coinbase_num_orders" not in w
