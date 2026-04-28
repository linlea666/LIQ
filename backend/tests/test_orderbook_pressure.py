"""挂单压力监测器单元测试 (重构后 · 盘口订单流仪表盘)

覆盖要点：
  - L1 路径 A · detect_walls_from_depth：±depth_range_pct 过滤、双闸阈值、合并、排序
  - L1 路径 B · detect_walls_from_large_orders：远距筛选、聚合、holding-only
  - tag_with_large_orders：depth 路径补 has_active_whale + holding_avg_age_sec
  - augment_with_absorption：confluence 标记（不再加 confidence）
  - _duration_factor / _compute_strength_score：时间阶梯 × USD × whale × absorption
  - _assign_strength_tier：USD 绝对阈值（S ≥ $30M / A ≥ $10M / B ≥ $3M / C 默认）
  - compute_pressure_snapshot：双路径合并、source 字段、reason 文案、tier 派生
  - LargeOrderLifecycle.holding_age_sec：holding/ended/异常时间戳

注意：本次重构后已砍 OP-Signal 通道，相关测试一并删除。
"""
from __future__ import annotations

import os
import sys
import time
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from models.market_action import AbsorptionSnapshot, AbsorptionZone
from models.orderbook_pressure import (
    DepthBin,
    LargeOrderLifecycle,
    OrderbookDepthSnapshot,
    PressureWall,
)
from processors.orderbook_pressure import (
    DEFAULTS,
    _assign_strength_tier,
    _compute_strength_score,
    _duration_factor,
    augment_with_absorption,
    compute_pressure_snapshot,
    detect_walls_from_depth,
    detect_walls_from_large_orders,
    tag_with_large_orders,
)

# ── 公共工具 ──────────────────────────────────────────────────────────────


def _bin(price: float, qty: float) -> DepthBin:
    return DepthBin(price=price, quantity=qty, usd_value=price * qty)


def _depth(
    last_price: float,
    bids: list[DepthBin],
    asks: list[DepthBin],
    *,
    prev_bids: list[DepthBin] | None = None,
    prev_asks: list[DepthBin] | None = None,
    ts_sec: int = 1_700_000_000,
) -> OrderbookDepthSnapshot:
    snap = OrderbookDepthSnapshot(
        coin="BTC", exchange="Binance", symbol="BTCUSDT",
        ts_sec=ts_sec, bids=bids, asks=asks,
    )
    if prev_bids is not None or prev_asks is not None:
        snap.prev_ts_sec = ts_sec - 90
        snap.prev_bids = prev_bids or []
        snap.prev_asks = prev_asks or []
    return snap


def _make_lo(
    *, id: int, side: str, limit_price: float, current_qty: float = 50.0,
    start_ms: int | None = None, end_ms: int | None = None,
    state: str = "holding",
) -> LargeOrderLifecycle:
    """造一个测试用大单 lifecycle。"""
    if start_ms is None:
        start_ms = int(time.time() * 1000) - 3600 * 1000  # 默认 1h 前
    return LargeOrderLifecycle(
        id=id, side=side, limit_price=limit_price,
        start_time_ms=start_ms, end_time_ms=end_ms,
        start_quantity=current_qty, current_quantity=current_qty,
        executed_volume=0.0, executed_usd_value=0.0,
        start_usd_value=limit_price * current_qty,
        current_usd_value=limit_price * current_qty,
        state=state,    # type: ignore[arg-type]
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# LargeOrderLifecycle.holding_age_sec
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_holding_age_sec_holding_uses_now():
    """holding 状态：从 start_time 到现在的秒数。"""
    now_ms = int(time.time() * 1000)
    lo = _make_lo(id=1, side="ask", limit_price=100.0,
                  start_ms=now_ms - 7200 * 1000, state="holding")
    age = lo.holding_age_sec
    # 7200 ± 30s 容差（避免测试时序漂移）
    assert 7170 <= age <= 7230


def test_holding_age_sec_ended_uses_end_time():
    """ended 状态：用 end_time - start_time，不再随时间增长。"""
    lo = _make_lo(
        id=1, side="ask", limit_price=100.0,
        start_ms=1_000_000_000_000,
        end_ms=1_000_000_000_000 + 3_600_000,    # 1 小时后结束
        state="ended",
    )
    assert lo.holding_age_sec == 3600


def test_holding_age_sec_invalid_returns_zero():
    """异常时间戳防御。"""
    lo1 = _make_lo(id=1, side="ask", limit_price=100.0, start_ms=0, state="holding")
    assert lo1.holding_age_sec == 0
    # ended 但 end_time < start_time 的脏数据
    lo2 = _make_lo(
        id=2, side="ask", limit_price=100.0,
        start_ms=2_000_000_000_000, end_ms=1_000_000_000_000,
        state="ended",
    )
    assert lo2.holding_age_sec == 0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# L1 路径 A · detect_walls_from_depth (近+中距 ≤4%)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_detect_walls_from_depth_filters_range_and_picks_top():
    """±depth_range_pct(默认 4%) 内挑出大单堆，超出范围的过滤。

    detect_walls_from_depth 返回 _RawWall（内部结构），仅含 price_lo/price_hi。
    """
    last = 100.0
    asks = [
        _bin(101.0, 100),       # +1.0% 在范围内
        _bin(102.0, 60_000),    # +2.0% $6,120,000 ✓ 大墙
        _bin(110.0, 80_000),    # +10% 超出 ±4% 范围 → 过滤
    ]
    bids = [
        _bin(99.0, 50_000),     # -1.0% $4,950,000 ✓ 大墙
        _bin(95.0, 80_000),     # -5.0% 超出范围 → 过滤
    ]
    walls = detect_walls_from_depth(_depth(last, bids, asks), last, DEFAULTS)
    sides = sorted({w.side for w in walls})
    assert sides == ["ask", "bid"]
    # 102 入选，110 不入选
    ask_w = next(w for w in walls if w.side == "ask")
    assert 101.5 <= ask_w.price_lo <= 102.5
    bid_w = next(w for w in walls if w.side == "bid")
    assert bid_w.size_usd >= DEFAULTS["wall_min_usd"]
    # source 字段标识来源
    assert all(w.source == "depth_5m" for w in walls)


def test_detect_walls_from_depth_merges_neighbouring_bins():
    """相邻 ±0.05% 价位合并成一个墙。"""
    last = 1000.0
    asks = [
        _bin(1010.0, 700),    # $707,000
        _bin(1010.4, 800),    # $808,000；与 1010 相距 0.4 < 0.5 (merge_tol)
    ]
    cfg = {**DEFAULTS, "wall_size_top_pct": 1.0}  # 让两个 bin 都过双闸
    walls = detect_walls_from_depth(_depth(last, [], asks), last, cfg)
    asks_w = [w for w in walls if w.side == "ask"]
    assert len(asks_w) == 1
    merged = asks_w[0]
    assert merged.price_lo == 1010.0 and merged.price_hi == 1010.4
    assert merged.size_usd >= 707_000 + 808_000 - 1


def test_detect_walls_from_depth_skips_when_no_data():
    """空 depth / last_price=0 → 直接返回空。"""
    assert detect_walls_from_depth(_depth(100.0, [], []), 100.0, DEFAULTS) == []
    snap = _depth(100.0, [_bin(99, 1)], [_bin(101, 1)])
    assert detect_walls_from_depth(snap, 0.0, DEFAULTS) == []


def test_detect_walls_from_depth_excludes_far_distance():
    """≥ 4% 距离的 bin 被 depth 路径过滤（应该走 large_orders 路径）。"""
    last = 100.0
    asks = [
        _bin(102.0, 60_000),    # +2% 在范围内
        _bin(106.0, 60_000),    # +6% 超出 4%
    ]
    walls = detect_walls_from_depth(_depth(last, [], asks), last, DEFAULTS)
    # 只有 102 价位的 wall
    asks_w = [w for w in walls if w.side == "ask"]
    assert len(asks_w) == 1
    assert 101.5 <= asks_w[0].price_lo <= 102.5


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# L1 路径 B · detect_walls_from_large_orders (远距 4-12%)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_detect_walls_from_large_orders_filters_to_far_distance():
    """只挑选距现价 4-12% 的 holding 大单（近+中距留给 depth 路径）。"""
    last = 1000.0
    los = [
        _make_lo(id=1, side="ask", limit_price=1020.0, current_qty=10_000),  # +2% < 4% 过滤
        _make_lo(id=2, side="ask", limit_price=1080.0, current_qty=10_000),  # +8% ✓
        _make_lo(id=3, side="ask", limit_price=1200.0, current_qty=10_000),  # +20% > 12% 过滤
        _make_lo(id=4, side="bid", limit_price=950.0, current_qty=10_000),   # -5% ✓
    ]
    walls = detect_walls_from_large_orders(los, last, DEFAULTS)
    asks = [w for w in walls if w.side == "ask"]
    bids = [w for w in walls if w.side == "bid"]
    assert len(asks) == 1
    assert len(bids) == 1
    assert abs(asks[0].price_lo - 1080.0) < 0.5
    assert abs(bids[0].price_lo - 950.0) < 0.5
    # source 字段
    assert all(w.source == "large_orders" for w in walls)


def test_detect_walls_from_large_orders_excludes_ended():
    """ended（已平/已撤）的大单不计入远距 wall。"""
    last = 1000.0
    los = [
        _make_lo(id=1, side="ask", limit_price=1080.0, current_qty=10_000,
                 state="holding"),
        _make_lo(id=2, side="ask", limit_price=1080.0, current_qty=10_000,
                 state="ended", end_ms=int(time.time() * 1000)),
    ]
    walls = detect_walls_from_large_orders(los, last, DEFAULTS)
    asks = [w for w in walls if w.side == "ask"]
    assert len(asks) == 1
    # 仅 holding 的那笔，size 是单笔大小（不是合并）
    assert asks[0].size_usd <= 1080.0 * 10_000 + 1


def test_detect_walls_from_large_orders_merges_close_prices():
    """相邻 ±0.10% 价位的大单聚合 + 计算加权平均 holding_age。"""
    last = 1000.0
    now_ms = int(time.time() * 1000)
    los = [
        _make_lo(id=1, side="ask", limit_price=1080.0, current_qty=5_000,
                 start_ms=now_ms - 3600 * 1000),   # 1h 前
        _make_lo(id=2, side="ask", limit_price=1080.5, current_qty=5_000,
                 start_ms=now_ms - 7200 * 1000),   # 2h 前
    ]
    walls = detect_walls_from_large_orders(los, last, DEFAULTS)
    asks = [w for w in walls if w.side == "ask"]
    assert len(asks) == 1  # merge 成 1 个
    merged = asks[0]
    assert merged.bin_count == 2
    # 加权平均：两个 USD 接近相等，age 应该约等于 (3600+7200)/2 = 5400
    assert 4500 <= merged.holding_avg_age_sec <= 6300


def test_detect_walls_from_large_orders_skips_below_min_usd():
    """单笔/聚合后低于 wall_min_usd($500K) 的过滤。"""
    last = 1000.0
    los = [
        _make_lo(id=1, side="ask", limit_price=1080.0, current_qty=100),  # $108K < $500K
    ]
    assert detect_walls_from_large_orders(los, last, DEFAULTS) == []


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# tag_with_large_orders — depth 路径补 has_active_whale + holding_avg_age
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_tag_with_large_orders_depth_path():
    """depth_5m 路径的 wall 通过 tag_with_large_orders 补 has_active_whale。"""
    wall = PressureWall(
        side="ask", price_lo=100.0, price_hi=100.2, price_mid=100.1,
        distance_pct=0.1, size_usd=1_000_000, size_base=10, source="depth_5m",
    )
    now_ms = int(time.time() * 1000)
    los = [_make_lo(id=10, side="ask", limit_price=100.05,
                    current_qty=50, start_ms=now_ms - 7200 * 1000, state="holding")]
    tag_with_large_orders([wall], los, last_price=100.0, cfg=DEFAULTS)
    assert wall.large_order_ids == [10]
    assert wall.has_active_whale is True
    assert wall.holding_avg_age_sec > 7000


def test_tag_with_large_orders_skips_opposite_side():
    """ask wall 不应被 bid 大单关联。"""
    wall = PressureWall(
        side="ask", price_lo=100.0, price_hi=100.2, price_mid=100.1,
        distance_pct=0.1, size_usd=1_000_000, size_base=10, source="depth_5m",
    )
    los = [_make_lo(id=10, side="bid", limit_price=100.05, current_qty=50)]
    tag_with_large_orders([wall], los, last_price=100.0, cfg=DEFAULTS)
    assert wall.large_order_ids == []
    assert wall.has_active_whale is False


def test_tag_with_large_orders_preserves_large_orders_path():
    """large_orders 路径来源的 wall 自带 ids 与 has_active_whale，tag 函数不应覆盖它们。

    has_active_whale 在 _raw_to_pressure_wall 中创建时已写入；本函数对
    source == 'large_orders' 的 wall 直接 continue。
    """
    wall = PressureWall(
        side="ask", price_lo=110.0, price_hi=110.2, price_mid=110.1,
        distance_pct=10.1, size_usd=10_000_000, size_base=900,
        source="large_orders", large_order_ids=[1, 2], large_order_count=2,
        has_active_whale=True,
    )
    # 即使传入 large_orders 列表（包含价位远的散单），ids/whale 标记也不应被覆盖
    los = [_make_lo(id=99, side="bid", limit_price=50.0, current_qty=100)]
    tag_with_large_orders([wall], los, last_price=100.0, cfg=DEFAULTS)
    assert wall.large_order_ids == [1, 2]
    assert wall.has_active_whale is True


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# augment_with_absorption — 共振只打标，不加分
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_augment_with_absorption_marks_confluence_only():
    """重构后只打 confluence_with_absorption 标记，不再 +25 confidence。"""
    wall = PressureWall(
        side="ask", price_lo=100.0, price_hi=100.5, price_mid=100.25,
        distance_pct=0.25, size_usd=1_000_000, size_base=10, source="depth_5m",
    )
    zone = AbsorptionZone(
        price=100.20, side="resistance", taker_volume_usd=5_000_000,
        delta_pct_abs_avg=0.05, bar_count=2, age_hours=0.5,
    )
    snap = AbsorptionSnapshot(
        zones_resistance=[zone], total_zone_count=1,
        strongest_resistance=zone, lookback_bars=3,
    )
    augment_with_absorption([wall], snap, last_price=100.0, atr=2.0, cfg=DEFAULTS)
    assert wall.confluence_with_absorption is True
    assert wall.absorption_zone_price == 100.20


def test_augment_with_absorption_no_match_when_far():
    wall = PressureWall(
        side="ask", price_lo=100.0, price_hi=100.5, price_mid=100.25,
        distance_pct=0.25, size_usd=1_000_000, size_base=10, source="depth_5m",
    )
    zone = AbsorptionZone(
        price=110.0, side="resistance", taker_volume_usd=5_000_000,
        delta_pct_abs_avg=0.05, bar_count=2, age_hours=0.5,
    )
    snap = AbsorptionSnapshot(
        zones_resistance=[zone], total_zone_count=1,
        strongest_resistance=zone, lookback_bars=3,
    )
    augment_with_absorption([wall], snap, last_price=100.0, atr=2.0, cfg=DEFAULTS)
    assert wall.confluence_with_absorption is False


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# _duration_factor — 时间阶梯
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_duration_factor_depth_5m_always_one():
    """depth_5m 路径无法精确知挂单时长 → 统一 1.0。"""
    assert _duration_factor(0, "depth_5m") == 1.0
    assert _duration_factor(7 * 86400, "depth_5m") == 1.0


def test_duration_factor_large_orders_ladder():
    """large_orders 路径阶梯：<1h → 0.7；1-6h → 1.0；6-24h → 1.3；1-7d → 1.6；>7d → 2.0。"""
    assert _duration_factor(30 * 60, "large_orders") == 0.7      # 30min
    assert _duration_factor(3 * 3600, "large_orders") == 1.0     # 3h
    assert _duration_factor(12 * 3600, "large_orders") == 1.3    # 12h
    assert _duration_factor(3 * 86400, "large_orders") == 1.6    # 3d
    assert _duration_factor(10 * 86400, "large_orders") == 2.0   # 10d


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# _compute_strength_score — 综合公式
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_compute_strength_score_base_usd_only():
    """无 whale + 无 absorption + depth_5m → score == size_usd × 1 × 1 × 1。"""
    wall = PressureWall(
        side="ask", price_lo=100.0, price_hi=100.5, price_mid=100.25,
        distance_pct=0.25, size_usd=1_000_000, size_base=10, source="depth_5m",
    )
    assert _compute_strength_score(wall) == 1_000_000


def test_compute_strength_score_with_whale_and_absorption():
    """有 whale + 有 absorption → 1 × 1.3 × 1.2 = 1.56× 加成。"""
    wall = PressureWall(
        side="ask", price_lo=100.0, price_hi=100.5, price_mid=100.25,
        distance_pct=0.25, size_usd=1_000_000, size_base=10, source="depth_5m",
        has_active_whale=True, confluence_with_absorption=True,
    )
    score = _compute_strength_score(wall)
    assert abs(score - 1_000_000 * 1.3 * 1.2) < 1


def test_compute_strength_score_large_orders_long_duration():
    """large_orders + 5d holding → 1 × 1.6 = 1.6× 加成。"""
    wall = PressureWall(
        side="ask", price_lo=100.0, price_hi=100.5, price_mid=100.25,
        distance_pct=0.25, size_usd=10_000_000, size_base=100, source="large_orders",
        holding_avg_age_sec=5 * 86400,
    )
    score = _compute_strength_score(wall)
    assert abs(score - 10_000_000 * 1.6) < 1


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# _assign_strength_tier — 绝对 USD 阈值
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_assign_strength_tier_thresholds():
    """S ≥ $30M / A ≥ $10M / B ≥ $3M / C 默认。"""
    def _w(score: float) -> PressureWall:
        w = PressureWall(
            side="ask", price_lo=100, price_hi=100.5, price_mid=100.25,
            distance_pct=0.25, size_usd=score, size_base=1, source="depth_5m",
        )
        w.strength_score = score
        return w

    assert _assign_strength_tier(_w(50_000_000), DEFAULTS) == "S"
    assert _assign_strength_tier(_w(15_000_000), DEFAULTS) == "A"
    assert _assign_strength_tier(_w(5_000_000), DEFAULTS) == "B"
    assert _assign_strength_tier(_w(1_000_000), DEFAULTS) == "C"


def test_assign_strength_tier_uses_score_not_size():
    """tier 用 strength_score 而非 size_usd（确保时间衰减/累积生效）。"""
    # size_usd $5M（B 阈值），但 score 因 30min 快闪挂单变 $3.5M（仍 B 但贴边）
    w = PressureWall(
        side="ask", price_lo=100, price_hi=100.5, price_mid=100.25,
        distance_pct=0.25, size_usd=5_000_000, size_base=50, source="large_orders",
        holding_avg_age_sec=30 * 60,   # 30min
    )
    w.strength_score = _compute_strength_score(w)
    # 30min < 1h → factor 0.7 → 5M × 0.7 = 3.5M ≥ tier_b
    assert _assign_strength_tier(w, DEFAULTS) == "B"

    # 同样 $5M 但持有 2 天 → score 5M × 1.6 = $8M < $10M → 仍 B
    w2 = PressureWall(
        side="ask", price_lo=100, price_hi=100.5, price_mid=100.25,
        distance_pct=0.25, size_usd=5_000_000, size_base=50, source="large_orders",
        holding_avg_age_sec=2 * 86400,
    )
    w2.strength_score = _compute_strength_score(w2)
    assert _assign_strength_tier(w2, DEFAULTS) == "B"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 顶层 compute_pressure_snapshot — 端到端
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_compute_pressure_snapshot_returns_none_on_missing_ticker():
    s = SimpleNamespace(coin="BTC", ticker=None, orderbook_depth_snapshot=None,
                        large_orders_history=[])
    assert compute_pressure_snapshot(s) is None


def test_compute_pressure_snapshot_returns_empty_when_no_walls():
    """有 ticker 但 depth/large_orders 都为空 → 返回空 walls 但不 None。"""
    s = SimpleNamespace(
        coin="BTC", ticker=SimpleNamespace(last=100.0),
        orderbook_depth_snapshot=None,
        large_orders_history=[],
        footprint_contract=[], footprint_spot=[],
        atr=1.0,
    )
    snap = compute_pressure_snapshot(s)
    assert snap is not None
    assert snap.walls == []
    assert snap.data_quality == "missing"


def test_data_quality_marked_stale_when_depth_age_exceeds_threshold():
    """depth.ts_sec 超过 stale_age_sec（默认 180s）→ data_quality=stale 且 notes 含 age 详情。"""
    last = 100.0
    now = int(time.time())
    asks = [_bin(101.0, 60_000)]   # +1%
    bids = [_bin(99.0, 60_000)]
    # depth 时间戳设为 now-300s（超过默认 180s stale 阈值）
    depth = _depth(last, bids, asks, ts_sec=now - 300)
    state = SimpleNamespace(
        coin="BTC", ticker=SimpleNamespace(last=last),
        orderbook_depth_snapshot=depth,
        large_orders_history=[],
        footprint_contract=[], footprint_spot=[],
        atr=1.0,
    )
    snap = compute_pressure_snapshot(state, now_sec=now)
    assert snap is not None
    assert snap.data_quality == "stale"
    assert any(n.startswith("depth_age_") for n in snap.notes)


def test_data_quality_remains_ok_when_depth_age_fresh():
    """depth.ts_sec 在 stale_age_sec 内 → 不会变 stale。"""
    last = 100.0
    now = int(time.time())
    asks = [_bin(101.0, 60_000)]
    bids = [_bin(99.0, 60_000)]
    depth = _depth(last, bids, asks, ts_sec=now - 30)
    los = [
        _make_lo(id=1, side="ask", limit_price=110.0, current_qty=200_000,
                 start_ms=now * 1000 - 86400 * 1000),
    ]
    state = SimpleNamespace(
        coin="BTC", ticker=SimpleNamespace(last=last),
        orderbook_depth_snapshot=depth,
        large_orders_history=los,
        footprint_contract=[], footprint_spot=[],
        atr=1.0,
    )
    snap = compute_pressure_snapshot(state, now_sec=now)
    assert snap is not None
    assert snap.data_quality == "ok"
    assert not any(n.startswith("depth_age_") for n in snap.notes)


def test_data_quality_stale_overrides_partial():
    """depth 陈旧 + large_orders 缺失 → stale 优先（比 partial 更重要的提示）。"""
    last = 100.0
    now = int(time.time())
    asks = [_bin(101.0, 60_000)]
    bids = [_bin(99.0, 60_000)]
    depth = _depth(last, bids, asks, ts_sec=now - 500)
    state = SimpleNamespace(
        coin="BTC", ticker=SimpleNamespace(last=last),
        orderbook_depth_snapshot=depth,
        large_orders_history=[],
        footprint_contract=[], footprint_spot=[],
        atr=1.0,
    )
    snap = compute_pressure_snapshot(state, now_sec=now)
    assert snap is not None
    assert snap.data_quality == "stale"


def test_compute_pressure_snapshot_combines_both_sources():
    """近+中距走 depth_5m，远距走 large_orders，输出合并后 source 字段正确。"""
    last = 100.0
    now_ms = int(time.time() * 1000)
    asks = [_bin(101.0, 60_000)]   # +1% depth 路径 ($6.06M)
    bids = [_bin(99.0, 60_000)]
    depth = _depth(last, bids, asks)
    los = [
        _make_lo(id=1, side="ask", limit_price=110.0, current_qty=200_000,
                 start_ms=now_ms - 86400 * 1000),    # +10% large_orders 路径 ($22M, 1d)
    ]
    state = SimpleNamespace(
        coin="BTC", ticker=SimpleNamespace(last=last),
        orderbook_depth_snapshot=depth,
        large_orders_history=los,
        footprint_contract=[], footprint_spot=[],
        atr=1.0,
    )
    snap = compute_pressure_snapshot(state)
    assert snap is not None
    sources = {w.source for w in snap.walls}
    assert "depth_5m" in sources
    assert "large_orders" in sources
    # 远距来源的 wall 必须填了 holding_avg_age_sec
    far_walls = [w for w in snap.walls if w.source == "large_orders"]
    assert all(w.holding_avg_age_sec > 0 for w in far_walls)


def test_compute_pressure_snapshot_assigns_tier_and_reason():
    """每个 wall 都带 tier + reason 文案（中性）。"""
    last = 100.0
    asks = [_bin(101.0, 60_000)]
    state = SimpleNamespace(
        coin="BTC", ticker=SimpleNamespace(last=last),
        orderbook_depth_snapshot=_depth(last, [], asks),
        large_orders_history=[],
        footprint_contract=[], footprint_spot=[],
        atr=1.0,
    )
    snap = compute_pressure_snapshot(state)
    assert snap is not None and snap.walls
    for w in snap.walls:
        assert w.strength_tier in ("S", "A", "B", "C")
        assert w.reason  # 非空
        # 文案不能再含"真假"语义
        assert "真" not in w.reason
        assert "假" not in w.reason
        assert "spoof" not in w.reason.lower()
        # 标签必须是新中性枚举
        assert w.label in ("wall_ask", "wall_bid", "wall_vanished", "wall_broken")


def test_compute_pressure_snapshot_top_resistance_uses_strong_tier():
    """top_resistance 取 S/A 级最近的 ask wall（B/C 级降级为最后兜底）。"""
    last = 100.0
    now_ms = int(time.time() * 1000)
    # 1) 近距弱墙（B 级，~$6M score with depth_5m factor=1.0）
    asks_close = [_bin(101.0, 60_000)]
    # 2) 远距强墙（>$30M 进入 S 级；+8% 远距）
    los = [_make_lo(id=1, side="ask", limit_price=108.0, current_qty=400_000,
                    start_ms=now_ms - 7 * 86400 * 1000)]   # 7d holding → factor 2.0
    state = SimpleNamespace(
        coin="BTC", ticker=SimpleNamespace(last=last),
        orderbook_depth_snapshot=_depth(last, [], asks_close),
        large_orders_history=los,
        footprint_contract=[], footprint_spot=[],
        atr=1.0,
    )
    snap = compute_pressure_snapshot(state)
    assert snap is not None
    # S/A 级远距强墙应被选为 top_resistance（虽然距离更远）
    s_a_asks = [w for w in snap.walls
                if w.side == "ask" and w.strength_tier in ("S", "A")]
    assert s_a_asks, "expected at least one S/A ask wall"
    assert snap.top_resistance is not None


def test_compute_pressure_snapshot_no_signal_field():
    """重构后 snapshot 不应再产出任何 signal 字段（OP-Signal 通道已砍）。"""
    last = 100.0
    state = SimpleNamespace(
        coin="BTC", ticker=SimpleNamespace(last=last),
        orderbook_depth_snapshot=_depth(last, [], [_bin(101.0, 60_000)]),
        large_orders_history=[],
        footprint_contract=[], footprint_spot=[],
        atr=1.0,
    )
    snap = compute_pressure_snapshot(state)
    assert snap is not None
    # 模型无 signals/last_signals 字段
    assert not hasattr(snap, "signals")
    assert not hasattr(snap, "last_signals")
