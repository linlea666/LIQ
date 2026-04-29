"""W2-T1 单元测试：trust_score 拆分（raw + components + SR/SA）。

覆盖：
  trust_breakdown：
    - 各因子加分明细记录到 components
    - components["total"] 与 final 一致
    - 既有 _compute_trust_score（向后兼容包装）仍返回 float
  SR (support_resistance_trust_score)：
    - 单源合约（spot_only base 0.30）
    - dual_source + 持续 + coinbase 共振 → 高 SR
    - 高 wall_removal_risk 拖低 SR（即使 trust_score 高）
    - wall_consumed_confidence ≥ 0.6 给硬证据加分
  SA (sweep_attractiveness_score)：
    - 同向 crowding（bid wall × long_crowding；ask wall × short_crowding）
    - magnet 邻近 + 真空跨度
    - active_attack_score 加分
    - thinning（current/max < 0.3）
  SR vs SA 正交性：可以同时高（"双向博弈热点"）
  active_attack_score 主流程写入 zone（不只是 break_through_risk 中间量）
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
    PositionCrowdingSnapshot,
    SweepTarget,
    WallZone,
)
from processors.liquidity_wall_engine import (
    ENGINE_DEFAULTS,
    _compute_support_resistance_trust_score,
    _compute_sweep_attractiveness_score,
    _compute_trust_breakdown,
    _compute_trust_score,
    build_liquidity_wall_outputs,
)


# ─────────────────────────────────────────────────────────────────
# 工具
# ─────────────────────────────────────────────────────────────────

def _make_zone(
    side: str = "bid",
    *,
    dual_source: bool = False,
    has_spot_confluence: bool = False,
    coinbase_confluence: bool = False,
    persistence: float = 0.5,
    consumed_conf: float = 0.0,
    removal_risk: float = 0.0,
    active_attack: float = 0.0,
    current_usd: float = 2_000_000.0,
    max_usd_1h: float = 2_000_000.0,
    sweep_target: SweepTarget | None = None,
    exchange_count: int = 1,
    coinbase_max_single: float = 0.0,
) -> WallZone:
    return WallZone(
        side=side,
        price_low=76050.0, price_high=76080.0,
        price_mid=76065.0, peak_price=76065.0, distance_pct=-0.30,
        current_usd=current_usd, max_usd_1h=max_usd_1h, avg_usd_1h=max_usd_1h,
        bin_count=3,
        seen_count=10, visible_minutes=40, persistence_score=persistence,
        dual_source=dual_source, has_spot_confluence=has_spot_confluence,
        coinbase_spot_confluence=coinbase_confluence,
        coinbase_max_single_order_usd=coinbase_max_single,
        exchange_count=exchange_count,
        wall_consumed_confidence=consumed_conf,
        wall_removal_risk=removal_risk,
        active_attack_score=active_attack,
        sweep_target=sweep_target,
    )


# ─────────────────────────────────────────────────────────────────
# 1. trust breakdown
# ─────────────────────────────────────────────────────────────────

class TestTrustBreakdown:
    def test_base_only_zone(self):
        zone = _make_zone()
        final, comps = _compute_trust_breakdown(zone, ENGINE_DEFAULTS)
        # base 0.50 → final 0.50
        assert final == 0.5
        assert comps["base"] == 0.5
        assert comps["total"] == 0.5
        # 没有任何加分因子 → components 只含 base + total
        assert set(comps.keys()) == {"base", "total"}

    def test_dual_source_records_component(self):
        zone = _make_zone(dual_source=True)
        final, comps = _compute_trust_breakdown(zone, ENGINE_DEFAULTS)
        # 0.50 + 0.30 = 0.80
        assert final == 0.8
        assert comps["dual_source"] == 0.30
        assert comps["base"] == 0.5

    def test_full_combination_components_sum_to_total(self):
        """所有加分因子都触发：base + dual + spot + multi + coinbase + persistent
           = 0.50 + 0.30 + 0.15 + 0.10 + 0.10 + 0.10 = 1.25 → clamp 1.0"""
        sweep = None
        zone = _make_zone(
            dual_source=True, has_spot_confluence=True, coinbase_confluence=True,
            persistence=0.95, exchange_count=3,
        )
        final, comps = _compute_trust_breakdown(zone, ENGINE_DEFAULTS)
        assert final == 1.0
        # components 各因子应记录原始未 clamp 的贡献
        assert comps["dual_source"] == 0.30
        assert comps["spot_confluence"] == 0.15
        assert comps["multi_exchange"] == 0.10
        assert comps["coinbase_confluence"] == 0.10
        assert comps["persistent"] == 0.10
        # 总和（before clamp）= 1.25，但 total 是 final（clamp 后）
        assert comps["total"] == 1.0

    def test_compute_trust_score_backward_compat_returns_float(self):
        """_compute_trust_score 向后兼容包装：仍只返回 float，与既有调用方语义一致。"""
        zone = _make_zone(dual_source=True)
        result = _compute_trust_score(zone, ENGINE_DEFAULTS)
        assert isinstance(result, float)
        assert result == 0.8


# ─────────────────────────────────────────────────────────────────
# 2. SR (support_resistance_trust_score)
# ─────────────────────────────────────────────────────────────────

class TestSupportResistanceTrust:
    def test_single_source_low_sr_baseline(self):
        """仅合约单源、无加分 → SR = 0.30 base。"""
        zone = _make_zone()
        sr = _compute_support_resistance_trust_score(zone, ENGINE_DEFAULTS)
        assert sr == 0.30

    def test_dual_source_plus_persistence_high_sr(self):
        """dual_source + persistence ≥ 0.7 → 0.30 + 0.30 + 0.10 = 0.70。"""
        zone = _make_zone(dual_source=True, persistence=0.85)
        sr = _compute_support_resistance_trust_score(zone, ENGINE_DEFAULTS)
        assert sr == pytest.approx(0.70, abs=0.01)

    def test_full_evidence_high_sr(self):
        """全部因子触发 + consumed_confidence 硬证据，无 removal_risk：
           0.30 + 0.30 + 0.15 + 0.10 + 0.10 + 0.10 = 1.05 → clamp 1.0"""
        zone = _make_zone(
            dual_source=True, has_spot_confluence=True, coinbase_confluence=True,
            persistence=0.85, consumed_conf=0.7,
        )
        sr = _compute_support_resistance_trust_score(zone, ENGINE_DEFAULTS)
        assert sr == 1.0

    def test_high_removal_risk_lowers_sr_below_trust(self):
        """同一个高 trust 墙，wall_removal_risk 高 → SR 显著低于 trust。
           dual_source + has_spot + coinbase + persistence = 0.95 SR 加分；
           wall_removal_risk = 0.8 × 0.30 = 0.24 减分；
           SR = 0.95 - 0.24 = 0.71"""
        zone = _make_zone(
            dual_source=True, has_spot_confluence=True, coinbase_confluence=True,
            persistence=0.85, removal_risk=0.8,
        )
        # 对照 trust：完整因子 → 1.0
        trust = _compute_trust_score(zone, ENGINE_DEFAULTS)
        sr = _compute_support_resistance_trust_score(zone, ENGINE_DEFAULTS)
        assert trust == 1.0
        # SR 加分：0.30 + 0.30 + 0.15 + 0.10 + 0.10 = 0.95；扣 0.24 → 0.71
        assert sr == pytest.approx(0.71, abs=0.01)
        # 关键不变量：高 trust + 高 removal_risk 时 SR 必显著低于 trust
        assert sr < trust - 0.20

    def test_consumed_confidence_adds_to_sr(self):
        """已被验证承接过买卖盘（consumed_conf ≥ 0.6）→ +0.10 硬证据。"""
        zone_no_consumed = _make_zone(dual_source=True, persistence=0.85)
        zone_consumed = _make_zone(dual_source=True, persistence=0.85, consumed_conf=0.65)
        sr_no = _compute_support_resistance_trust_score(zone_no_consumed, ENGINE_DEFAULTS)
        sr_yes = _compute_support_resistance_trust_score(zone_consumed, ENGINE_DEFAULTS)
        assert sr_yes - sr_no == pytest.approx(0.10, abs=0.001)

    def test_sr_clamped_to_zero_when_excessive_removal_risk(self):
        """极端：base 0.30 - 0.30×1.0 = 0.0（clamp 不下溢）。"""
        zone = _make_zone(removal_risk=1.0)
        sr = _compute_support_resistance_trust_score(zone, ENGINE_DEFAULTS)
        assert sr >= 0.0


class TestSRLargeSingleOrderLadder:
    """W4-T1 阶段 1.1：单档大单阶梯加分（取最高匹配档，不叠加）。

    阈值与前端 SpotOrderBookPanel ★ (1M) 严格对齐。
    旧逻辑 100k 一刀切 +0.05 → 100k 单和 4464 万单完全等同。
    """

    def test_below_100k_no_bonus(self):
        """单档 < 100k → 不加分。"""
        zone = _make_zone(coinbase_max_single=80_000)
        sr = _compute_support_resistance_trust_score(zone, ENGINE_DEFAULTS)
        # 仅 base 0.30
        assert sr == pytest.approx(0.30, abs=0.01)

    def test_100k_tier_adds_003(self):
        """≥ 100k 大额订单 → +0.03。"""
        zone = _make_zone(coinbase_max_single=200_000)
        sr = _compute_support_resistance_trust_score(zone, ENGINE_DEFAULTS)
        assert sr == pytest.approx(0.33, abs=0.01)

    def test_500k_tier_adds_006(self):
        """≥ 500k 中型机构挂单 → +0.06。"""
        zone = _make_zone(coinbase_max_single=600_000)
        sr = _compute_support_resistance_trust_score(zone, ENGINE_DEFAULTS)
        assert sr == pytest.approx(0.36, abs=0.01)

    def test_1m_tier_adds_010_aligned_with_frontend_star(self):
        """≥ 1M 机构级（与前端 ★ 阈值对齐）→ +0.10。"""
        zone = _make_zone(coinbase_max_single=2_000_000)
        sr = _compute_support_resistance_trust_score(zone, ENGINE_DEFAULTS)
        assert sr == pytest.approx(0.40, abs=0.01)

    def test_5m_tier_adds_013_capped(self):
        """≥ 5M 大型机构（封顶，避免单维度主导）→ +0.13。"""
        zone = _make_zone(coinbase_max_single=10_000_000)
        sr = _compute_support_resistance_trust_score(zone, ENGINE_DEFAULTS)
        assert sr == pytest.approx(0.43, abs=0.01)

    def test_ladder_takes_highest_match_not_cumulative(self):
        """6M 单档应只取 5M 档 (+0.13)，不叠加 100k+500k+1M+5M。"""
        zone = _make_zone(coinbase_max_single=6_000_000)
        sr = _compute_support_resistance_trust_score(zone, ENGINE_DEFAULTS)
        # base 0.30 + 0.13 = 0.43，而不是 0.30 + (0.03+0.06+0.10+0.13) = 0.62
        assert sr == pytest.approx(0.43, abs=0.01)
        assert sr < 0.50

    def test_lone_institutional_order_meaningfully_lifts_sr(self):
        """关键回归：1M 机构级单档 vs 100k 大额订单，SR 必有可感知差异。

        旧逻辑两者都 +0.05 完全等同 → 用户在 ZoneDetailCard 看不出"机构级 footprint"。
        新逻辑 1M=+0.10 vs 100k=+0.03，差距 0.07（可感知）。
        """
        zone_100k = _make_zone(coinbase_max_single=200_000)
        zone_1m = _make_zone(coinbase_max_single=2_000_000)
        sr_100k = _compute_support_resistance_trust_score(zone_100k, ENGINE_DEFAULTS)
        sr_1m = _compute_support_resistance_trust_score(zone_1m, ENGINE_DEFAULTS)
        assert sr_1m - sr_100k == pytest.approx(0.07, abs=0.01)

    def test_ladder_disabled_when_cfg_overrides_to_zero(self):
        """配置可关闭阶梯（回归向后兼容）。"""
        cfg = dict(ENGINE_DEFAULTS)
        cfg["sr_bonus_large_single_100k"] = 0.0
        cfg["sr_bonus_large_single_500k"] = 0.0
        cfg["sr_bonus_large_single_1m"] = 0.0
        cfg["sr_bonus_large_single_5m"] = 0.0
        zone = _make_zone(coinbase_max_single=10_000_000)
        sr = _compute_support_resistance_trust_score(zone, cfg)
        assert sr == pytest.approx(0.30, abs=0.01)


# ─────────────────────────────────────────────────────────────────
# 3. SA (sweep_attractiveness_score)
# ─────────────────────────────────────────────────────────────────

class TestSweepAttractiveness:
    def test_no_factors_yields_zero(self):
        """单纯墙、无 crowding / magnet / active_attack / removal / thinning → SA=0。"""
        zone = _make_zone()
        sa = _compute_sweep_attractiveness_score(zone, None, 76300.0, ENGINE_DEFAULTS)
        assert sa == 0.0

    def test_long_crowding_attracts_bid_wall_sweep(self):
        """bid wall + long_crowding=0.8 → 0.25 × 0.8 = 0.20"""
        zone = _make_zone(side="bid")
        crowding = PositionCrowdingSnapshot(long_crowding_risk=0.8, short_crowding_risk=0.0)
        sa = _compute_sweep_attractiveness_score(zone, crowding, 76300.0, ENGINE_DEFAULTS)
        assert sa == pytest.approx(0.20, abs=0.01)

    def test_short_crowding_attracts_ask_wall_sweep(self):
        """ask wall + short_crowding=0.8 → 0.20；且 long_crowding 不影响 ask wall。"""
        zone = _make_zone(side="ask")
        crowding = PositionCrowdingSnapshot(short_crowding_risk=0.8, long_crowding_risk=0.0)
        sa = _compute_sweep_attractiveness_score(zone, crowding, 76300.0, ENGINE_DEFAULTS)
        assert sa == pytest.approx(0.20, abs=0.01)

    def test_magnet_proximity_adds(self):
        """magnet 距 < 0.5% + vacuum_gap_pct ≥ 1% → +0.15 + 0.05 = 0.20。
           last_price=76300, magnet=76270 (距离 30/76300 ≈ 0.039% < 0.5%)。"""
        sweep = SweepTarget(
            direction="below", magnet_price=76270.0,
            magnet_amount_usd=4_000_000.0,
            distance_pct=-0.04, vacuum_gap_pct=1.2,
        )
        zone = _make_zone(side="bid", sweep_target=sweep)
        sa = _compute_sweep_attractiveness_score(zone, None, 76300.0, ENGINE_DEFAULTS)
        assert sa == pytest.approx(0.20, abs=0.01)

    def test_active_attack_score_contributes(self):
        """active_attack=0.8 → 0.20×0.8 = 0.16"""
        zone = _make_zone(active_attack=0.8)
        sa = _compute_sweep_attractiveness_score(zone, None, 76300.0, ENGINE_DEFAULTS)
        assert sa == pytest.approx(0.16, abs=0.01)

    def test_thinning_adds(self):
        """current/max < 0.3 → +0.10。"""
        zone = _make_zone(current_usd=400_000, max_usd_1h=2_000_000)  # ratio = 0.20
        sa = _compute_sweep_attractiveness_score(zone, None, 76300.0, ENGINE_DEFAULTS)
        assert sa == pytest.approx(0.10, abs=0.01)

    def test_high_sa_combination(self):
        """组合：crowding + magnet + active + removal + thinning。"""
        sweep = SweepTarget(
            direction="below", magnet_price=76270.0,
            magnet_amount_usd=4_000_000.0,
            distance_pct=-0.04, vacuum_gap_pct=1.5,
        )
        zone = _make_zone(
            side="bid",
            sweep_target=sweep,
            active_attack=0.7,
            removal_risk=0.5,
            current_usd=400_000, max_usd_1h=2_000_000,
        )
        crowding = PositionCrowdingSnapshot(long_crowding_risk=0.8, short_crowding_risk=0.0)
        sa = _compute_sweep_attractiveness_score(zone, crowding, 76300.0, ENGINE_DEFAULTS)
        # 0.25×0.8 + 0.15 + 0.05 + 0.20×0.7 + 0.15×0.5 + 0.10 = 0.20+0.20+0.14+0.075+0.10 = 0.715
        assert sa >= 0.65


# ─────────────────────────────────────────────────────────────────
# 4. SR / SA 正交性：可以同时高
# ─────────────────────────────────────────────────────────────────

class TestSRandSAOrthogonality:
    def test_high_trust_wall_can_still_be_high_sa_magnet(self):
        """W2-T1 核心洞察：dual_source + spot_confluence 高 SR 墙，
           若同时位于多头拥挤 + magnet 邻近 + active_attack 区域 → SA 也高。
           前端/AI 应同时看 SR 和 SA，不能仅凭 trust 判断"会反弹"。"""
        sweep = SweepTarget(
            direction="below", magnet_price=76060.0,
            magnet_amount_usd=4_000_000.0,
            distance_pct=-0.32, vacuum_gap_pct=1.0,
        )
        zone = _make_zone(
            side="bid",
            dual_source=True, has_spot_confluence=True,
            persistence=0.85,
            sweep_target=sweep,
            active_attack=0.7,
            consumed_conf=0.7,
        )
        crowding = PositionCrowdingSnapshot(
            long_crowding_risk=0.85, short_crowding_risk=0.10,
        )
        # last_price 76300 → magnet 76060 距离 ≈ 0.31% < 0.5%
        sr = _compute_support_resistance_trust_score(zone, ENGINE_DEFAULTS)
        sa = _compute_sweep_attractiveness_score(zone, crowding, 76300.0, ENGINE_DEFAULTS)
        # 关键不变量：双向博弈热点 — SR 与 SA 同时 ≥ 0.5
        assert sr >= 0.65
        assert sa >= 0.50

    def test_high_sr_low_sa_clean_support(self):
        """干净的强支撑：SR 高，SA 低。"""
        zone = _make_zone(
            side="bid",
            dual_source=True, has_spot_confluence=True, coinbase_confluence=True,
            persistence=0.85, consumed_conf=0.7,
        )
        sr = _compute_support_resistance_trust_score(zone, ENGINE_DEFAULTS)
        # 没 magnet / active_attack / crowding / removal_risk → SA 低
        sa = _compute_sweep_attractiveness_score(zone, None, 76300.0, ENGINE_DEFAULTS)
        assert sr >= 0.85
        assert sa <= 0.10

    def test_low_sr_high_sa_pure_magnet(self):
        """纯磁铁墙：单源 + 高 removal_risk + 厚度衰减 + 拥挤 + magnet 邻近。
           SR 低（容易消失），SA 高（吸引扫单）。"""
        sweep = SweepTarget(
            direction="below", magnet_price=76060.0,
            magnet_amount_usd=4_000_000.0,
            distance_pct=-0.32, vacuum_gap_pct=1.5,
        )
        zone = _make_zone(
            side="bid",
            removal_risk=0.7,
            sweep_target=sweep,
            active_attack=0.6,
            current_usd=400_000, max_usd_1h=2_000_000,  # thinning
        )
        crowding = PositionCrowdingSnapshot(long_crowding_risk=0.8, short_crowding_risk=0.0)
        sr = _compute_support_resistance_trust_score(zone, ENGINE_DEFAULTS)
        sa = _compute_sweep_attractiveness_score(zone, crowding, 76300.0, ENGINE_DEFAULTS)
        assert sr <= 0.20  # 0.30 base - 0.21 removal
        assert sa >= 0.55


# ─────────────────────────────────────────────────────────────────
# 5. 主流程端到端：active_attack_score / SR / SA 写入 zone
# ─────────────────────────────────────────────────────────────────

class TestMainEntryWritesNewFields:
    def _make_state(self, ts_base: int = 1_700_000_000) -> SimpleNamespace:
        # 最简端到端 state，只为了让 build_liquidity_wall_outputs 不崩
        history = []
        for i in range(8):
            bids = [DepthBin(price=p, quantity=q, usd_value=p*q)
                    for p, q in [(76045, 6), (76050, 60), (76055, 4)]]
            asks = [DepthBin(price=p, quantity=q, usd_value=p*q)
                    for p, q in [(76145, 4), (76150, 60), (76155, 4)]]
            history.append(OrderbookDepthSnapshot(
                coin="BTC", exchange="Binance", symbol="BTCUSDT",
                ts_sec=ts_base + i * 300,
                bids=bids, asks=asks,
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

    def test_main_entry_writes_active_attack_and_sr_sa(self):
        from models.orderbook_pressure import OrderbookPressureSnapshot
        state = self._make_state()
        base_snap = OrderbookPressureSnapshot(
            coin="BTC", ts_sec=1_700_000_000, last_price=76100.0,
            atr=200.0, walls=[],
        )
        out = build_liquidity_wall_outputs(state, base_snap, dict(ENGINE_DEFAULTS), now=1_700_002_400)
        all_zones = out.walls_above + out.walls_below
        # 主流程已运行：每个 zone 必须有 W2-T1 新字段（默认 0 也 OK，关键是不抛异常）
        for z in all_zones:
            # active_attack_score 必须是 0-1 浮点数（已写入字段）
            assert 0.0 <= z.active_attack_score <= 1.0
            # SR / SA 必须是 0-1 浮点数
            assert 0.0 <= z.support_resistance_trust_score <= 1.0
            assert 0.0 <= z.sweep_attractiveness_score <= 1.0
            # raw_trust_score 与 trust_score 一致（W2-T1 当前等价；W3+ 可能差异化）
            assert z.raw_trust_score == z.trust_score
            # trust_components 必须是 dict 且含 base / total
            assert isinstance(z.trust_components, dict)
            assert "base" in z.trust_components
            assert "total" in z.trust_components
            assert z.trust_components["total"] == z.trust_score
