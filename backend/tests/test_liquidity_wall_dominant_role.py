"""W3-T2 单元测试：dominant_role 主导角色分类。

覆盖优先级互斥：
  1. dual_battleground（SR ≥ 0.6 AND SA ≥ 0.6，最高优先级）
  2. institutional_footprint（coinbase_max_single ≥ 1M，与前端 ★ 严格对齐）
  3. support_resistance_strong（SR ≥ 0.7 且 SA < 0.4）
  4. magnet_strong（SA ≥ 0.6 且 SR < 0.5）
  5. spoof_suspect（trust < 0.55 且 removal_risk ≥ 0.6）
  6. transient（persistence < 0.3 且不变薄）
  7. ordinary（默认 fallback）

并验证：
  - 互斥性：每个 zone 只返回一个 role
  - 优先级：高优先级先匹配（如 dual_battleground 即使同时满足
    institutional_footprint 也仅返 dual_battleground）
  - 边界：阈值临界值正确处理
  - 主流程注入：build_liquidity_wall_outputs 后 zone.dominant_role 已写入
"""
from __future__ import annotations

import os
import sys
from collections import deque
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from models.orderbook_pressure import (
    DepthBin,
    OrderbookDepthSnapshot,
    OrderbookPressureSnapshot,
    WallZone,
)
from processors.liquidity_wall_engine import (
    ENGINE_DEFAULTS,
    _classify_dominant_role,
    build_liquidity_wall_outputs,
)


def _make_zone(
    *,
    sr: float = 0.0,
    sa: float = 0.0,
    trust: float = 0.5,
    removal_risk: float = 0.0,
    persistence: float = 0.5,
    coinbase_max_single: float = 0.0,
    current_usd: float = 2_000_000,
    max_usd_1h: float = 2_000_000,
) -> WallZone:
    return WallZone(
        side="bid",
        price_low=76050, price_high=76080,
        price_mid=76065, peak_price=76065, distance_pct=-0.30,
        current_usd=current_usd, max_usd_1h=max_usd_1h, avg_usd_1h=max_usd_1h,
        bin_count=3, seen_count=10, visible_minutes=40,
        persistence_score=persistence,
        trust_score=trust,
        wall_removal_risk=removal_risk,
        support_resistance_trust_score=sr,
        sweep_attractiveness_score=sa,
        coinbase_max_single_order_usd=coinbase_max_single,
    )


# ─────────────────────────────────────────────────────────────────
# 1. 各角色独立场景
# ─────────────────────────────────────────────────────────────────

class TestEachRoleClassified:
    def test_ordinary_default(self):
        """无任何特征 → ordinary。"""
        zone = _make_zone()
        assert _classify_dominant_role(zone) == "ordinary"

    def test_dual_battleground(self):
        """SR ≥ 0.6 AND SA ≥ 0.6 → dual_battleground"""
        zone = _make_zone(sr=0.7, sa=0.65)
        assert _classify_dominant_role(zone) == "dual_battleground"

    def test_institutional_footprint(self):
        """coinbase_max_single ≥ 1M → institutional_footprint
        （W4-T1 阶段 P1-A：阈值从 100k 提升到 1M，与前端 ★ 对齐）"""
        zone = _make_zone(coinbase_max_single=1_500_000)
        assert _classify_dominant_role(zone) == "institutional_footprint"

    def test_support_resistance_strong(self):
        """SR ≥ 0.7 且 SA < 0.4 → support_resistance_strong"""
        zone = _make_zone(sr=0.85, sa=0.2)
        assert _classify_dominant_role(zone) == "support_resistance_strong"

    def test_magnet_strong(self):
        """SA ≥ 0.6 且 SR < 0.5 → magnet_strong"""
        zone = _make_zone(sr=0.30, sa=0.75)
        assert _classify_dominant_role(zone) == "magnet_strong"

    def test_spoof_suspect(self):
        """trust < 0.55 且 removal_risk ≥ 0.6 → spoof_suspect"""
        zone = _make_zone(trust=0.50, removal_risk=0.70)
        assert _classify_dominant_role(zone) == "spoof_suspect"

    def test_transient(self):
        """persistence < 0.3 且不变薄（current/max ≥ 0.7）→ transient"""
        zone = _make_zone(persistence=0.10, current_usd=2_000_000, max_usd_1h=2_000_000)
        assert _classify_dominant_role(zone) == "transient"

    def test_transient_thinning_fallback_to_ordinary(self):
        """persistence 短 + 在变薄 → 不算 transient（变薄已由 break_through_risk 覆盖）"""
        zone = _make_zone(persistence=0.10, current_usd=400_000, max_usd_1h=2_000_000)
        # ratio = 0.20 < 0.7 → 不算 transient
        assert _classify_dominant_role(zone) == "ordinary"


# ─────────────────────────────────────────────────────────────────
# 2. 优先级互斥
# ─────────────────────────────────────────────────────────────────

class TestPriorityOrdering:
    def test_dual_battleground_wins_over_institutional(self):
        """同时满足 dual_battleground 和 institutional → 仅 dual_battleground"""
        zone = _make_zone(sr=0.7, sa=0.7, coinbase_max_single=2_000_000)
        assert _classify_dominant_role(zone) == "dual_battleground"

    def test_institutional_wins_over_sr_strong(self):
        """SR ≥ 0.7 + SA < 0.4 但 coinbase_max_single ≥ 1M → institutional 优先
           （机构布局信号比"反弹可信"更具体）"""
        zone = _make_zone(sr=0.85, sa=0.2, coinbase_max_single=1_500_000)
        assert _classify_dominant_role(zone) == "institutional_footprint"

    def test_sr_strong_wins_over_magnet_when_sa_low(self):
        """SR ≥ 0.7 + SA < 0.4 → support_resistance_strong（不会进 magnet 分支）"""
        zone = _make_zone(sr=0.85, sa=0.30)
        assert _classify_dominant_role(zone) == "support_resistance_strong"

    def test_magnet_wins_over_spoof_when_sa_high(self):
        """SA 高 + SR 低 + trust 低 + removal 高 → magnet_strong 优先（顺序 4 > 5）"""
        zone = _make_zone(sr=0.20, sa=0.70, trust=0.50, removal_risk=0.70)
        assert _classify_dominant_role(zone) == "magnet_strong"

    def test_spoof_wins_over_transient(self):
        """trust < 0.55 + removal ≥ 0.6 + persistence < 0.3 + 不变薄 → spoof_suspect"""
        zone = _make_zone(
            trust=0.50, removal_risk=0.65, persistence=0.10,
            current_usd=2_000_000, max_usd_1h=2_000_000,
        )
        assert _classify_dominant_role(zone) == "spoof_suspect"


# ─────────────────────────────────────────────────────────────────
# 3. 边界值
# ─────────────────────────────────────────────────────────────────

class TestBoundaries:
    def test_dual_battleground_exact_threshold(self):
        """SR=0.6 AND SA=0.6（边界）→ dual_battleground"""
        zone = _make_zone(sr=0.6, sa=0.6)
        assert _classify_dominant_role(zone) == "dual_battleground"

    def test_just_below_dual_threshold(self):
        """SR=0.59 AND SA=0.6 → 不进 dual_battleground"""
        zone = _make_zone(sr=0.59, sa=0.60)
        # SR < 0.7 不进 sr_strong；SA ≥ 0.6 但 SR ≥ 0.5 不进 magnet → ordinary
        assert _classify_dominant_role(zone) == "ordinary"

    def test_institutional_exact_1m(self):
        """coinbase_max_single = 1_000_000 边界 → institutional_footprint
        （W4-T1 阶段 P1-A：阈值从 100k 提升到 1M，与前端 ★ 严格对齐）"""
        zone = _make_zone(coinbase_max_single=1_000_000)
        assert _classify_dominant_role(zone) == "institutional_footprint"

    def test_institutional_just_below_1m(self):
        """coinbase_max_single = 999_999 → 不算（避免标签语义稀释）"""
        zone = _make_zone(coinbase_max_single=999_999)
        assert _classify_dominant_role(zone) == "ordinary"

    def test_institutional_at_old_100k_no_longer_triggers(self):
        """W4-T1 阶段 P1-A 回归保障：100k 单档不再被误标为 institutional_footprint。
        修复用户截图实测 bug：CB 单档 44.6 万被误标 institutional_footprint，
        但前端无 ★（前端阈值 1M），导致标签语义稀释。"""
        zone = _make_zone(coinbase_max_single=446_000)  # 截图实测值
        assert _classify_dominant_role(zone) == "ordinary"
        zone2 = _make_zone(coinbase_max_single=100_000)
        assert _classify_dominant_role(zone2) == "ordinary"

    def test_sr_strong_sa_exactly_04(self):
        """SR=0.7 且 SA=0.4 → 不进 sr_strong（SA < 0.4 是严格 <）"""
        zone = _make_zone(sr=0.70, sa=0.40)
        # SR ≥ 0.7 + SA = 0.4 → 不进 sr_strong；不 dual（SA < 0.6）→ ordinary
        assert _classify_dominant_role(zone) == "ordinary"

    def test_magnet_sr_exactly_05(self):
        """SA ≥ 0.6 且 SR=0.5 → 不进 magnet（SR < 0.5 是严格 <）"""
        zone = _make_zone(sr=0.50, sa=0.65)
        # SR=0.5 < 0.6 不 dual；SR=0.5 不 < 0.5 不 magnet → ordinary
        assert _classify_dominant_role(zone) == "ordinary"


# ─────────────────────────────────────────────────────────────────
# 4. 主流程端到端：build_liquidity_wall_outputs 后 dominant_role 已写入
# ─────────────────────────────────────────────────────────────────

class TestMainEntryWritesDominantRole:
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
            coinbase_orderbook=None,
            liq_max_pain=None,
            oi_exchange_rank=None, multi_funding=None, ls_ratio=None,
        )

    def test_main_entry_writes_dominant_role(self):
        state = self._make_state()
        base_snap = OrderbookPressureSnapshot(
            coin="BTC", ts_sec=1_700_000_000, last_price=76100.0,
            atr=200.0, walls=[],
        )
        out = build_liquidity_wall_outputs(state, base_snap, dict(ENGINE_DEFAULTS), now=1_700_002_400)
        all_zones = out.walls_above + out.walls_below
        valid_roles = {
            "dual_battleground", "institutional_footprint", "support_resistance_strong",
            "magnet_strong", "spoof_suspect", "transient", "ordinary",
        }
        for z in all_zones:
            assert z.dominant_role in valid_roles
