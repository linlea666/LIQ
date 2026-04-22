"""滚仓风险数学层 (processors/roll_risk.py) 单元测试

覆盖要点（对齐 Step 1 自检清单）：
  1. 浮盈 / 有效杠杆 / 加权均价 / 持币量 基础公式
  2. 爆仓价估算 —— long/short × isolated/cross × 边界（极端浮亏/零保证金）
  3. 加仓量 4 种模式（passive/pyramid/layered/fixed）
  4. 加仓后指标模拟
  5. 三道闸门检查 + 烈度阈值覆盖
  6. 二分法求解 —— 全量通过 / 缩量 / 缩到 0（三种情形）
  7. 止损上移规则（单向移动 + 保本/ATR 追踪）
  8. 置信度映射到烈度
"""

from __future__ import annotations

import pytest

from models.roll_position import (
    AddMode,
    ConfidenceThresholds,
    RollEvent,
    RollPlan,
    SafetyGates,
    UserPosition,
)
from processors.roll_risk import (
    DEFAULT_MAINTENANCE_MARGIN_RATIO,
    IdealAddContext,
    abs_distance_pct,
    bars_since_last_add_in_atr,
    binary_search_safe_margin,
    check_gates,
    compute_ideal_add_margin,
    compute_trail_sl,
    count_add_events,
    distance_pct,
    effective_leverage,
    estimate_liq_price,
    intensity_from_score,
    simulate_after_add,
    size_from_margin,
    unrealized_pnl_pct,
    unrealized_pnl_usd,
    weighted_avg_entry,
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 辅助：构造测试用 Position / Plan
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _make_position(
    side="short",
    margin_mode="isolated",
    leverage=10,
    entry=79000.0,
    size=None,
    margin=100.0,
    total_account=1000.0,
    events=None,
    stop_loss=None,
):
    if size is None:
        size = margin * leverage / entry
    return UserPosition(
        id="pos-test",
        coin="BTC",
        side=side,
        margin_mode=margin_mode,
        leverage=leverage,
        entry_price=entry,
        position_size=size,
        position_value_usd=size * entry,
        margin_used_usd=margin,
        total_account_usd=total_account,
        stop_loss=stop_loss,
        plan_id="plan-test",
        created_at=0,
        updated_at=0,
        events=events or [],
    )


def _make_plan(add_mode: AddMode = "passive_deleveraging", **kwargs):
    defaults = dict(
        id="plan-test",
        position_id="pos-test",
        template_id="fatzhai",
        add_mode=add_mode,
        created_at=0,
        gates=SafetyGates(),
        thresholds=ConfidenceThresholds(),
    )
    defaults.update(kwargs)
    return RollPlan(**defaults)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 基础指标
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestUnrealizedPnL:
    def test_long_profit(self):
        # long 10 coins @ 100，现价 120，浮盈 200
        pnl = unrealized_pnl_usd("long", 100.0, 120.0, 10.0)
        assert pnl == pytest.approx(200.0)

    def test_long_loss(self):
        pnl = unrealized_pnl_usd("long", 100.0, 80.0, 10.0)
        assert pnl == pytest.approx(-200.0)

    def test_short_profit(self):
        # short 10 coins @ 100，现价 80，浮盈 200
        pnl = unrealized_pnl_usd("short", 100.0, 80.0, 10.0)
        assert pnl == pytest.approx(200.0)

    def test_short_loss(self):
        pnl = unrealized_pnl_usd("short", 100.0, 120.0, 10.0)
        assert pnl == pytest.approx(-200.0)

    def test_zero_size(self):
        assert unrealized_pnl_usd("long", 100.0, 120.0, 0.0) == 0.0

    def test_zero_price(self):
        assert unrealized_pnl_usd("long", 0.0, 120.0, 10.0) == 0.0


class TestUnrealizedPnLPct:
    def test_short_10x_price_drop_5pct(self):
        # short 10x，价格跌 5% → 盈利 50% 保证金（杠杆放大）
        pct = unrealized_pnl_pct("short", 79000.0, 75050.0, 10)
        assert pct == pytest.approx(50.0, rel=1e-3)
        pct = unrealized_pnl_pct("short", 100.0, 95.0, 10)
        assert pct == pytest.approx(50.0)

    def test_long_10x_price_up_3pct(self):
        pct = unrealized_pnl_pct("long", 100.0, 103.0, 10)
        assert pct == pytest.approx(30.0)

    def test_zero_leverage_safe(self):
        assert unrealized_pnl_pct("long", 100.0, 110.0, 0) == 0.0


class TestEffectiveLeverage:
    def test_no_pnl(self):
        # 10 coins × 100 = 1000 notional, 100 margin → 10x
        lev = effective_leverage(10.0, 100.0, 100.0, 0.0)
        assert lev == pytest.approx(10.0)

    def test_profit_reduces_leverage(self):
        # short 10 coins @ 100，现价 90，浮盈 100，名义 = 10×90 = 900
        # 保证金+浮盈 = 200，有效杠杆 = 900/200 = 4.5
        lev = effective_leverage(10.0, 90.0, 100.0, 100.0)
        assert lev == pytest.approx(4.5)

    def test_extreme_loss_returns_sentinel(self):
        # 浮亏超过保证金：分母 <= 0 → 返回 999
        lev = effective_leverage(10.0, 50.0, 100.0, -150.0)
        assert lev == 999.0


class TestWeightedAvg:
    def test_basic(self):
        # 10 @ 100 + 10 @ 200 → 均价 150
        avg = weighted_avg_entry(10.0, 100.0, 10.0, 200.0)
        assert avg == pytest.approx(150.0)

    def test_zero_add(self):
        avg = weighted_avg_entry(10.0, 100.0, 0.0, 200.0)
        assert avg == pytest.approx(100.0)

    def test_zero_total(self):
        assert weighted_avg_entry(0.0, 100.0, 0.0, 200.0) == 0.0


class TestSizeFromMargin:
    def test_basic(self):
        # 100 USD × 10x / 100 USD/coin = 10 coins
        assert size_from_margin(100.0, 10, 100.0) == pytest.approx(10.0)

    def test_zero_price(self):
        assert size_from_margin(100.0, 10, 0.0) == 0.0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 爆仓价
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestEstimateLiqPrice:
    def test_long_isolated_10x(self):
        # long 10x @ 100, mmr=0.005
        # liq = 100 × (1 - 0.1) / (1 - 0.005) ≈ 90.45
        liq = estimate_liq_price("long", "isolated", 100.0, 10, 1.0, 10.0)
        assert liq == pytest.approx(90.452, rel=1e-3)

    def test_short_isolated_10x(self):
        # short 10x @ 100, mmr=0.005
        # liq = 100 × (1 + 0.1) / (1 + 0.005) ≈ 109.45
        liq = estimate_liq_price("short", "isolated", 100.0, 10, 1.0, 10.0)
        assert liq == pytest.approx(109.452, rel=1e-3)

    def test_short_isolated_10x_btc(self):
        # short 10x @ 79000
        # 理论爆仓 ≈ 86844
        liq = estimate_liq_price("short", "isolated", 79000.0, 10, 0.01266, 100.0)
        assert liq is not None
        assert 85000 < liq < 88000

    def test_zero_inputs_returns_none(self):
        assert estimate_liq_price("long", "isolated", 0.0, 10, 1.0, 10.0) is None
        assert estimate_liq_price("long", "isolated", 100.0, 0, 1.0, 10.0) is None
        assert estimate_liq_price("long", "isolated", 100.0, 10, 0.0, 10.0) is None
        assert estimate_liq_price("long", "isolated", 100.0, 10, 1.0, 0.0) is None

    def test_cross_without_account(self):
        # cross 模式未传账户总额 → None
        liq = estimate_liq_price("long", "cross", 100.0, 10, 1.0, 10.0)
        assert liq is None

    def test_cross_with_account(self):
        # cross 模式下简化估算：保守偏远
        liq = estimate_liq_price(
            "long", "cross", 100.0, 10, 1.0, 10.0, total_account_usd=100.0
        )
        assert liq is not None
        assert liq >= 0.0


class TestDistancePct:
    def test_positive(self):
        assert distance_pct(100.0, 105.0) == pytest.approx(5.0)

    def test_negative(self):
        assert distance_pct(100.0, 95.0) == pytest.approx(-5.0)

    def test_zero_from(self):
        assert distance_pct(0.0, 100.0) == 0.0

    def test_abs_symmetric(self):
        assert abs_distance_pct(100.0, 95.0) == pytest.approx(5.0)
        assert abs_distance_pct(100.0, 105.0) == pytest.approx(5.0)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 加仓量计算（4 种模式）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestComputeIdealAddMargin:
    def test_passive_deleveraging(self):
        # short @79000 100U margin 10x, 现价 75000
        # 浮盈 = 0.01266 × 4000 = 50.6
        pos = _make_position()
        plan = _make_plan("passive_deleveraging")
        ctx = IdealAddContext(pos, plan, 75000.0, past_add_count=0)
        m = compute_ideal_add_margin(ctx)
        assert 50 < m < 52

    def test_passive_no_profit_returns_zero(self):
        pos = _make_position()
        plan = _make_plan("passive_deleveraging")
        ctx = IdealAddContext(pos, plan, 79500.0, past_add_count=0)  # 逆向
        m = compute_ideal_add_margin(ctx)
        assert m == 0.0

    def test_pyramid_decay_first_add(self):
        # first_add_ratio=0.5, 初始 margin=100 → 首次加 50
        pos = _make_position(events=[
            RollEvent(ts=0, kind="init", price=79000.0, margin_delta_usd=100.0,
                      avg_price_after=79000.0, leverage_after=10.0,
                      liq_price_after=86844.0, reason="init"),
        ])
        plan = _make_plan("pyramid_decay", pyramid_decay_ratio=0.6)
        ctx = IdealAddContext(pos, plan, 75000.0, past_add_count=0, first_add_ratio=0.5)
        m = compute_ideal_add_margin(ctx)
        assert m == pytest.approx(50.0)

    def test_pyramid_decay_second_add(self):
        # 第二次加：50 × 0.6 = 30
        pos = _make_position(events=[
            RollEvent(ts=0, kind="init", price=79000.0, margin_delta_usd=100.0,
                      avg_price_after=79000.0, leverage_after=10.0,
                      liq_price_after=86844.0, reason="init"),
        ])
        plan = _make_plan("pyramid_decay", pyramid_decay_ratio=0.6)
        ctx = IdealAddContext(pos, plan, 75000.0, past_add_count=1, first_add_ratio=0.5)
        m = compute_ideal_add_margin(ctx)
        assert m == pytest.approx(30.0, rel=1e-3)

    def test_layered_independent(self):
        # account=1000, pct=0.1 → 100
        pos = _make_position(total_account=1000.0)
        plan = _make_plan("layered_independent", layered_pct_of_account=0.1)
        ctx = IdealAddContext(pos, plan, 75000.0, past_add_count=3)  # 次数不影响
        m = compute_ideal_add_margin(ctx)
        assert m == pytest.approx(100.0)

    def test_fixed_ratio(self):
        # margin=100, ratio=0.3 → 30
        pos = _make_position(margin=100.0)
        plan = _make_plan("fixed_ratio", fixed_ratio_of_position=0.3)
        ctx = IdealAddContext(pos, plan, 75000.0, past_add_count=0)
        m = compute_ideal_add_margin(ctx)
        assert m == pytest.approx(30.0)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 加仓后指标模拟（核心 —— 用户关心的"加仓拉近均价"问题）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestSimulateAfterAdd:
    def test_short_add_pulls_avg_closer_to_price(self):
        """用户案例：short @79000 100U 10x，现价 75000，加仓 100U 10x。

        预期：
          - 加仓前均价 79000，距现价 +5.3%（现价比均价低 5.3%）
          - 加仓后均价约 76953，距现价约 +2.6%
        """
        pos = _make_position()  # short @79000, size=0.01266, margin=100
        before = simulate_after_add(pos, 0, 75000.0)
        after = simulate_after_add(pos, 100.0, 75000.0)

        # 加仓前
        assert before.avg_price == pytest.approx(79000.0)
        # 加仓后均价向现价靠拢
        assert 76500 < after.avg_price < 77200
        # 距离压缩（前约 5.3%，后约 2.6%）
        assert abs(before.distance_to_price_pct) > 5.0
        assert abs(after.distance_to_price_pct) < 3.0

    def test_passive_deleveraging_keeps_avg_further(self):
        """passive 模式（m_add≈50）相比无脑等量加仓（m_add=100）均价压缩更轻。"""
        pos = _make_position()
        add_50 = simulate_after_add(pos, 50.0, 75000.0)
        add_100 = simulate_after_add(pos, 100.0, 75000.0)
        # 加得少 → 均价离现价更远（压缩少）
        assert abs(add_50.distance_to_price_pct) > abs(add_100.distance_to_price_pct)

    def test_after_add_leverage_goes_up(self):
        """加仓后有效杠杆（含浮盈）上升。"""
        pos = _make_position()
        before = simulate_after_add(pos, 0, 75000.0)
        after = simulate_after_add(pos, 100.0, 75000.0)
        # 加仓前有效杠杆 ≈ 6.3x（浮盈拉低），加仓后拉到更高
        assert before.effective_leverage < after.effective_leverage

    def test_long_position_symmetric(self):
        """long 持仓上涨后加仓 —— 对称性验证。"""
        pos = _make_position(side="long", entry=50000.0)
        after = simulate_after_add(pos, 100.0, 55000.0)
        # 加仓后均价应在 50000 和 55000 之间，偏 50000 一侧（因为老仓仓位大）
        assert 50000 < after.avg_price < 55000


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 闸门检查
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestCheckGates:
    def test_all_pass(self):
        # 离现价远 + 杠杆低 + 爆仓远
        pos = _make_position()
        metrics = simulate_after_add(pos, 50.0, 75000.0)
        gates = SafetyGates(
            min_avg_distance_pct=2.0,
            min_liq_distance_pct=8.0,
            max_eff_leverage=15.0,
        )
        result = check_gates(metrics, gates, intensity="full")
        assert result.all_pass is True
        assert result.failing_reasons() == []

    def test_gate_a_fail_small_intensity_stricter(self):
        """small 烈度下 min_avg_distance_pct 强制升到 5%。"""
        pos = _make_position()
        metrics = simulate_after_add(pos, 100.0, 75000.0)  # 均价压到约 2.6%
        gates = SafetyGates(
            min_avg_distance_pct=2.0,  # 模板说 2%
            min_liq_distance_pct=5.0,
            max_eff_leverage=20.0,
        )
        # full 档：2% < 2.6% → 过
        r_full = check_gates(metrics, gates, intensity="full")
        assert r_full.gate_a_pass is True
        # small 档：5% > 2.6% → 不过（烈度覆盖）
        r_small = check_gates(metrics, gates, intensity="small")
        assert r_small.gate_a_pass is False

    def test_gate_c_fail(self):
        pos = _make_position()
        metrics = simulate_after_add(pos, 500.0, 75000.0)  # 大量加仓 → 杠杆飙升
        gates = SafetyGates(
            min_avg_distance_pct=1.0,
            min_liq_distance_pct=1.0,
            max_eff_leverage=5.0,  # 设得很低
        )
        result = check_gates(metrics, gates, intensity="full")
        assert result.gate_c_pass is False


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 二分法求解
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestBinarySearchSafeMargin:
    def test_ideal_passes_all_gates(self):
        """理论量就已经满足所有闸门 → 不缩量。"""
        pos = _make_position()
        gates = SafetyGates(
            min_avg_distance_pct=2.0,
            min_liq_distance_pct=5.0,
            max_eff_leverage=15.0,
            min_add_margin_usd=5.0,
        )
        result = binary_search_safe_margin(pos, 50.0, 75000.0, gates, intensity="full")
        assert result.accepted is True
        assert result.final_margin_usd == pytest.approx(50.0)
        assert result.shrink_reason == ""

    def test_shrink_by_gate_a(self):
        """理论量闸门 A 不过 → 缩量。"""
        pos = _make_position()
        gates = SafetyGates(
            min_avg_distance_pct=3.5,  # 要求 3.5%（100U 加仓会压到 2.6%，不过）
            min_liq_distance_pct=5.0,
            max_eff_leverage=15.0,
            min_add_margin_usd=5.0,
        )
        result = binary_search_safe_margin(pos, 100.0, 75000.0, gates, intensity="full")
        assert result.accepted is True
        assert 0 < result.final_margin_usd < 100.0
        assert "闸门A" in result.shrink_reason
        # 缩后应该恰好过关
        assert result.gates.all_pass is True

    def test_shrink_to_zero_rejects(self):
        """闸门过严，连 0 都无法过 → 拒绝。"""
        pos = _make_position()
        # 设一个 0 加仓时闸门 C 都不过的场景：
        # 当前有效杠杆 ≈ 6.3x（现价75000），设 max_eff_leverage=5 → C 不过
        gates = SafetyGates(
            min_avg_distance_pct=1.0,
            min_liq_distance_pct=1.0,
            max_eff_leverage=5.0,
            min_add_margin_usd=5.0,
        )
        result = binary_search_safe_margin(pos, 100.0, 75000.0, gates, intensity="full")
        assert result.accepted is False
        assert result.final_margin_usd == 0.0

    def test_below_min_margin_rejects(self):
        """缩到的量低于 min_add_margin_usd → 拒绝。"""
        pos = _make_position()
        gates = SafetyGates(
            min_avg_distance_pct=5.3,  # 要求极紧：必须压不动均价
            min_liq_distance_pct=1.0,
            max_eff_leverage=20.0,
            min_add_margin_usd=10.0,
        )
        # 初始均价距现价 5.3%，任何加仓都会压到 <5.3%
        result = binary_search_safe_margin(pos, 100.0, 75000.0, gates, intensity="full")
        assert result.accepted is False

    def test_zero_ideal(self):
        pos = _make_position()
        gates = SafetyGates()
        result = binary_search_safe_margin(pos, 0.0, 75000.0, gates)
        assert result.accepted is False
        assert result.final_margin_usd == 0.0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 止损上移
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestComputeTrailSl:
    def _pos_with_init_and_one_add(self, side="short"):
        events = [
            RollEvent(ts=100, kind="init", price=79000.0,
                      margin_delta_usd=100.0, avg_price_after=79000.0,
                      leverage_after=10.0, reason="init"),
            RollEvent(ts=200, kind="add", price=75000.0,
                      margin_delta_usd=50.0, avg_price_after=77000.0,
                      leverage_after=10.0, reason="add#1"),
        ]
        return _make_position(side=side, events=events, stop_loss=81000.0 if side == "short" else None)

    def test_not_yet_trigger(self):
        """past_add_count < trail_sl_after_add_n → 不建议移动。"""
        pos = self._pos_with_init_and_one_add()
        plan = _make_plan(trail_sl_after_add_n=2)
        new_sl = compute_trail_sl(pos, 75000.0, atr=500.0, past_add_count=1, plan=plan)
        assert new_sl is None

    def test_move_to_breakeven_after_first_add(self):
        """第 1 次加仓后建议挪到保本（initial entry）。"""
        pos = self._pos_with_init_and_one_add(side="short")
        plan = _make_plan(trail_sl_after_add_n=1)
        new_sl = compute_trail_sl(pos, 75000.0, atr=500.0, past_add_count=1, plan=plan)
        assert new_sl == pytest.approx(79000.0)

    def test_only_moves_in_favorable_direction_short(self):
        """short 持仓止损只能下移，不能上移。"""
        pos = self._pos_with_init_and_one_add(side="short")
        pos.stop_loss = 78000.0  # 已经比保本 79000 更紧
        plan = _make_plan(trail_sl_after_add_n=1)
        new_sl = compute_trail_sl(pos, 75000.0, atr=500.0, past_add_count=1, plan=plan)
        # 保本 79000 比现止损 78000 更松 → 不建议移
        assert new_sl is None

    def test_only_moves_in_favorable_direction_long(self):
        events = [
            RollEvent(ts=100, kind="init", price=50000.0,
                      margin_delta_usd=100.0, avg_price_after=50000.0,
                      leverage_after=10.0, reason="init"),
            RollEvent(ts=200, kind="add", price=55000.0,
                      margin_delta_usd=50.0, avg_price_after=52000.0,
                      leverage_after=10.0, reason="add#1"),
        ]
        pos = _make_position(side="long", entry=52000.0, events=events, stop_loss=51000.0)
        plan = _make_plan(trail_sl_after_add_n=1)
        new_sl = compute_trail_sl(pos, 55000.0, atr=500.0, past_add_count=1, plan=plan)
        # 保本 50000 比现止损 51000 更松 → 不移
        assert new_sl is None

    def test_deep_roll_uses_atr_offset(self):
        """第 2 次加仓后应基于上次加仓价 ± ATR。"""
        events = [
            RollEvent(ts=100, kind="init", price=79000.0,
                      margin_delta_usd=100.0, avg_price_after=79000.0,
                      leverage_after=10.0, reason="init"),
            RollEvent(ts=200, kind="add", price=75000.0,
                      margin_delta_usd=50.0, avg_price_after=77000.0,
                      leverage_after=10.0, reason="add#1"),
            RollEvent(ts=300, kind="add", price=72000.0,
                      margin_delta_usd=30.0, avg_price_after=75500.0,
                      leverage_after=10.0, reason="add#2"),
        ]
        pos = _make_position(side="short", events=events, stop_loss=79000.0)
        plan = _make_plan(trail_sl_after_add_n=1, trail_sl_atr_mult=1.5)
        # short 持仓：止损 = 上次加仓价 + 1.5 × ATR = 72000 + 750 = 72750
        new_sl = compute_trail_sl(pos, 71000.0, atr=500.0, past_add_count=2, plan=plan)
        assert new_sl == pytest.approx(72750.0, rel=1e-3)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 置信度 → 烈度 映射
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestIntensityFromScore:
    def test_thresholds_default(self):
        # 默认：full=80 / half=55 / small=40
        plan = _make_plan()
        assert intensity_from_score(85, plan) == "full"
        assert intensity_from_score(80, plan) == "full"
        assert intensity_from_score(60, plan) == "half"
        assert intensity_from_score(55, plan) == "half"
        assert intensity_from_score(45, plan) == "small"
        assert intensity_from_score(40, plan) == "small"
        assert intensity_from_score(35, plan) == "reject"
        assert intensity_from_score(-10, plan) == "reject"

    def test_custom_thresholds(self):
        plan = _make_plan(thresholds=ConfidenceThresholds(
            full_add=70, half_add=50, small_add=30,
            full_reduce=60, half_reduce=40,
        ))
        assert intensity_from_score(70, plan) == "full"
        assert intensity_from_score(50, plan) == "half"
        assert intensity_from_score(30, plan) == "small"
        assert intensity_from_score(29, plan) == "reject"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 事件计数 & ATR 距离辅助
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestEventHelpers:
    def test_count_add_events(self):
        events = [
            RollEvent(ts=1, kind="init", price=100),
            RollEvent(ts=2, kind="add", price=95),
            RollEvent(ts=3, kind="add", price=90),
            RollEvent(ts=4, kind="user_override_add", price=88),  # 不计入
            RollEvent(ts=5, kind="reduce", price=93),
        ]
        pos = _make_position(events=events)
        assert count_add_events(pos) == 2

    def test_bars_since_last_add_no_add(self):
        pos = _make_position()
        dist = bars_since_last_add_in_atr(pos, 75000.0, 500.0)
        assert dist == float("inf")

    def test_bars_since_last_add(self):
        events = [
            RollEvent(ts=1, kind="init", price=79000),
            RollEvent(ts=2, kind="add", price=75000),
        ]
        pos = _make_position(events=events)
        # 现价 73000，上次加仓 75000 → 距离 2000；ATR 500 → 4 ATR
        dist = bars_since_last_add_in_atr(pos, 73000.0, 500.0)
        assert dist == pytest.approx(4.0)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 端到端数值校验：完整重现用户案例
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestUserCaseEndToEnd:
    """重现用户关心的案例：short @79000 100U 10x，现价 75000。

    验证 passive vs 无脑等量加仓的差异，这是方案里用户最关心的数学正确性。
    """

    def _setup(self):
        pos = _make_position(
            side="short", margin_mode="isolated", leverage=10,
            entry=79000.0, margin=100.0, total_account=1000.0,
        )
        return pos

    def test_passive_add_is_safer_than_equal_add(self):
        pos = self._setup()
        plan_passive = _make_plan("passive_deleveraging")
        plan_fixed = _make_plan("fixed_ratio", fixed_ratio_of_position=1.0)  # 无脑等量

        ctx_p = IdealAddContext(pos, plan_passive, 75000.0, past_add_count=0)
        ctx_f = IdealAddContext(pos, plan_fixed, 75000.0, past_add_count=0)

        m_passive = compute_ideal_add_margin(ctx_p)
        m_fixed = compute_ideal_add_margin(ctx_f)

        # passive（约 50）< fixed（100）
        assert m_passive < m_fixed

        # 模拟加仓后，passive 的均价距现价应该更远
        sim_passive = simulate_after_add(pos, m_passive, 75000.0)
        sim_fixed = simulate_after_add(pos, m_fixed, 75000.0)

        assert abs(sim_passive.distance_to_price_pct) > abs(sim_fixed.distance_to_price_pct)

    def test_gates_block_aggressive_fixed_ratio(self):
        """用 fixed_ratio=1.0（无脑翻倍）在默认闸门下必然被缩量。"""
        pos = self._setup()
        plan = _make_plan("fixed_ratio", fixed_ratio_of_position=1.0)
        ctx = IdealAddContext(pos, plan, 75000.0, past_add_count=0)
        ideal = compute_ideal_add_margin(ctx)
        # 默认闸门：min_avg_distance_pct=2.5
        # 翻倍加仓会把均价压到 ~2.6%，刚好过 2.5
        # 我们用 small 烈度（要求 5%）验证一定被缩
        result = binary_search_safe_margin(
            pos, ideal, 75000.0, SafetyGates(), intensity="small"
        )
        assert result.final_margin_usd < ideal
