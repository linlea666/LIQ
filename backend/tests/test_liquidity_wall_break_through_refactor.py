"""W2-T2 单元测试：break_through_risk 重构。

覆盖三个核心改动：
  1. persistence 单调项移除 — 新墙不被误判
     - 旧：persistence < 0.3 单调 +0.20（新墙 / 暖机墙误判为高打穿风险）
     - 新：thinning AND persistence < 0.3 → +0.10 复合（仅"变薄+短期"才加分）
  2. 流动性失衡分新增 — 对侧供给远大于本侧时加分
     - same/opp ratio < 0.3 → 1.0；< 0.5 → 0.5；线性映射
     - 现货优先 → 合约 fallback
  3. active_attack_score 优先用 zone 字段（W2-T1 主流程已写入）
     - zone.active_attack_score > 0 → 直接用，不重算
     - == 0 → fallback 重算（向后兼容旧测试）
"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from models.orderbook_pressure import (
    PositionCrowdingSnapshot,
    SweepTarget,
    WallZone,
)
from processors.liquidity_wall_engine import (
    ENGINE_DEFAULTS,
    _compute_break_through_risk,
    _liquidity_imbalance_score,
)


def _make_zone(
    side: str = "bid",
    *,
    current_usd: float = 2_000_000,
    max_usd_1h: float = 2_000_000,
    persistence: float = 0.5,
    active_attack: float = 0.0,
) -> WallZone:
    return WallZone(
        side=side, price_low=76050, price_high=76080,
        price_mid=76065, peak_price=76065, distance_pct=-0.30,
        current_usd=current_usd, max_usd_1h=max_usd_1h, avg_usd_1h=max_usd_1h,
        bin_count=3, seen_count=10, visible_minutes=40,
        persistence_score=persistence,
        active_attack_score=active_attack,
    )


# ─────────────────────────────────────────────────────────────────
# 1. persistence 单调项移除（核心修复）
# ─────────────────────────────────────────────────────────────────

class TestPersistenceMonotonicityFix:
    def test_short_persistence_alone_no_longer_penalty(self):
        """W2-T2 核心修复：persistence < 0.3 但厚度稳定（无 thinning）→ 不加分。
           旧逻辑会因 persistence 单调项 +0.20，导致刚出现的稳定强墙被误判。"""
        # current == max → 无 thinning
        zone = _make_zone(current_usd=2_000_000, max_usd_1h=2_000_000,
                          persistence=0.05)  # 短期但厚度稳定
        risk = _compute_break_through_risk(zone, None, None, ENGINE_DEFAULTS)
        # 旧逻辑：0.20（persistence < 0.3 单调）→ 现在 0.0
        assert risk == 0.0

    def test_thinning_alone_adds_only_thinning_weight(self):
        """thinning 但 persistence ≥ 0.3 → 仅 +0.25（去掉短期复合）"""
        zone = _make_zone(current_usd=500_000, max_usd_1h=2_000_000,
                          persistence=0.5)  # thinning + 中等持续
        risk = _compute_break_through_risk(zone, None, None, ENGINE_DEFAULTS)
        assert risk == pytest.approx(0.25, abs=0.01)

    def test_thinning_AND_short_compound_adds_extra(self):
        """thinning AND persistence < 0.3 → +0.25 + 0.10 = 0.35"""
        zone = _make_zone(current_usd=500_000, max_usd_1h=2_000_000,
                          persistence=0.05)
        risk = _compute_break_through_risk(zone, None, None, ENGINE_DEFAULTS)
        assert risk == pytest.approx(0.35, abs=0.01)

    def test_new_strong_wall_not_misclassified_as_high_risk(self):
        """W2-T2 关键不变量：新出现的强墙（厚度大 + 持续短）不应被判为高打穿风险。
           旧：仅 persistence < 0.3 就 +0.20，可能让新墙误进 break_through_risk ≥ 0.6
           现：仅有 persistence 短不够；必须配合 thinning 才扣分"""
        zone = _make_zone(
            current_usd=10_000_000,  # 厚度大
            max_usd_1h=10_000_000,    # 不变薄
            persistence=0.10,          # 刚出现
        )
        risk = _compute_break_through_risk(zone, None, None, ENGINE_DEFAULTS)
        assert risk < 0.30, "新出现的稳定厚墙不应被判为高打穿风险"


# ─────────────────────────────────────────────────────────────────
# 2. 流动性失衡分（新增）
# ─────────────────────────────────────────────────────────────────

class TestLiquidityImbalanceScore:
    def _make_history(self, bids_usd: float, asks_usd: float):
        return [SimpleNamespace(
            aggregated_bids_usd=bids_usd,
            aggregated_asks_usd=asks_usd,
        )]

    def test_no_history_returns_zero(self):
        zone = _make_zone(side="bid")
        score = _liquidity_imbalance_score(zone, None, None)
        assert score == 0.0

    def test_balanced_supply_returns_zero(self):
        """bids = asks → ratio = 1.0 → 无失衡 → 0"""
        zone = _make_zone(side="bid")
        history = self._make_history(1e9, 1e9)
        assert _liquidity_imbalance_score(zone, history, None) == 0.0

    def test_bid_wall_with_heavy_ask_supply_max_score(self):
        """bid 墙下 + 卖盘是买盘的 4x → ratio = 0.25 ≤ 0.3 → 1.0"""
        zone = _make_zone(side="bid")
        history = self._make_history(bids_usd=2.5e8, asks_usd=1e9)
        score = _liquidity_imbalance_score(zone, history, None)
        assert score == 1.0

    def test_ask_wall_with_heavy_bid_supply_max_score(self):
        """ask 墙上 + 买盘是卖盘的 4x → 同等失衡（侧反过来）"""
        zone = _make_zone(side="ask")
        history = self._make_history(bids_usd=1e9, asks_usd=2.5e8)
        score = _liquidity_imbalance_score(zone, history, None)
        assert score == 1.0

    def test_partial_imbalance_linear_mapping(self):
        """ratio = 0.5（对侧 2x） → 0.5/0.7 ≈ 0.714"""
        zone = _make_zone(side="bid")
        history = self._make_history(bids_usd=5e8, asks_usd=1e9)
        score = _liquidity_imbalance_score(zone, history, None)
        # (1 - 0.5) / 0.7 ≈ 0.714
        assert score == pytest.approx(0.714, abs=0.01)

    def test_spot_history_priority_over_futures(self):
        """现货失衡优先；现货无数据时 fallback 合约。"""
        zone = _make_zone(side="bid")
        # 现货：失衡（ratio=0.3）
        spot_history = self._make_history(bids_usd=3e8, asks_usd=1e9)
        # 合约：平衡（ratio=1.0，应被忽略）
        futures_history = self._make_history(bids_usd=1e9, asks_usd=1e9)
        score = _liquidity_imbalance_score(zone, futures_history, spot_history)
        # 1.0（现货优先）
        assert score == 1.0

    def test_imbalance_added_to_break_through_risk(self):
        """失衡分在 _compute_break_through_risk 中作为独立加分项贡献 0.10。"""
        zone = _make_zone(side="bid", current_usd=2_000_000, max_usd_1h=2_000_000,
                          persistence=0.5)
        history_balanced = self._make_history(1e9, 1e9)
        history_imbalanced = self._make_history(2e8, 1e9)  # bids=200M < asks=1B → ratio=0.2

        risk_balanced = _compute_break_through_risk(
            zone, None, None, ENGINE_DEFAULTS,
            ask_bids_history=history_balanced,
        )
        risk_imbalanced = _compute_break_through_risk(
            zone, None, None, ENGINE_DEFAULTS,
            ask_bids_history=history_imbalanced,
        )
        # 平衡 → 0；失衡（ratio=0.2 ≤ 0.3）→ 1.0 × 0.10 = +0.10
        assert risk_balanced == 0.0
        assert risk_imbalanced == pytest.approx(0.10, abs=0.01)


# ─────────────────────────────────────────────────────────────────
# 3. active_attack 优先用 zone 字段（性能 + 一致性）
# ─────────────────────────────────────────────────────────────────

class TestActiveAttackFromZoneField:
    def test_zone_field_used_when_set(self):
        """zone.active_attack_score 已有值 → 直接用，不重算（不依赖 taker_flow）。"""
        zone = _make_zone(active_attack=0.8)  # 已写入字段
        # 不传 taker_flow / cvd_spot；旧实现会因为没传而算出 0
        risk = _compute_break_through_risk(zone, None, None, ENGINE_DEFAULTS)
        # 0 + 0.10 × 0.8 = 0.08
        assert risk == pytest.approx(0.08, abs=0.01)

    def test_zone_field_zero_falls_back_to_recompute(self):
        """zone.active_attack_score == 0 → fallback 重算（向后兼容旧测试调用方）。
           旧测试直接传 taker_flow / cvd_spot 给 _compute_break_through_risk，必须仍生效。"""
        zone = _make_zone(active_attack=0.0)
        taker = SimpleNamespace(buy_volume_usd=1_000_000, sell_volume_usd=4_000_000)
        cvd = SimpleNamespace(trend_1h="strong_down")
        risk = _compute_break_through_risk(
            zone, None, None, ENGINE_DEFAULTS,
            taker_flow=taker, cvd_spot=cvd,
        )
        # active_attack 重算：taker 同向 0.40 + cvd 0.30 = 0.70
        # break_through_risk += 0.10 × 0.70 = 0.07
        assert risk == pytest.approx(0.07, abs=0.02)


# ─────────────────────────────────────────────────────────────────
# 4. 端到端：W2-T2 重构后总分上限 = 1.0
# ─────────────────────────────────────────────────────────────────

class TestRefactoredCapAtOne:
    def test_max_total_score_caps_at_one(self):
        """所有因子触发：thinning 0.25 + thinning AND short 0.10 + magnet 0.20
           + vacuum 0.15 + crowding 0.10 + active 0.10 + 失衡 0.10 = 1.00 ✓"""
        zone = _make_zone(
            side="bid",
            current_usd=500_000, max_usd_1h=2_000_000,  # thinning ratio=0.25
            persistence=0.05,                             # short
            active_attack=1.0,                            # max
        )
        sweep = SweepTarget(direction="below", magnet_price=99_900,
                             magnet_amount_usd=4e6,
                             distance_pct=-0.1, vacuum_gap_pct=0.6)
        crowding = PositionCrowdingSnapshot(long_crowding_risk=0.7)
        history = [SimpleNamespace(
            aggregated_bids_usd=2e8, aggregated_asks_usd=1e9,  # ratio=0.2 → 1.0
        )]
        risk = _compute_break_through_risk(
            zone, crowding, sweep, ENGINE_DEFAULTS,
            ask_bids_history=history,
        )
        assert risk == pytest.approx(1.0, abs=0.01)

    def test_realistic_dangerous_zone(self):
        """现实危险场景：墙正在变薄 + 多头拥挤 + magnet 邻近。"""
        zone = _make_zone(
            side="bid",
            current_usd=800_000, max_usd_1h=2_000_000,  # thinning 0.40
            persistence=0.5,                             # 不算短期
            active_attack=0.5,                           # 中度攻击
        )
        sweep = SweepTarget(direction="below", magnet_price=99_900,
                             magnet_amount_usd=4e6,
                             distance_pct=-0.3, vacuum_gap_pct=0.7)
        crowding = PositionCrowdingSnapshot(long_crowding_risk=0.7)
        risk = _compute_break_through_risk(zone, crowding, sweep, ENGINE_DEFAULTS)
        # thinning 0.25 + magnet 0.20 + vacuum 0.15 + crowding 0.10 + active 0.05 = 0.75
        assert risk == pytest.approx(0.75, abs=0.02)
