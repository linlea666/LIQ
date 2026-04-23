"""滚仓引擎主体 (processors/roll_position_engine.py) + 前瞻扫描 (processors/roll_forward.py)
单元测试。

覆盖要点：
  1. Phase 0  数据健康 / safety_gate=block
  2. Phase 1  离场：止损击穿 / 爆仓临近 / 结构+衰竭双确认
  3. Phase 2  减仓：full / half / 不触发
  4. Phase 3  止损移动：保本 / 不覆盖
  5. Phase 4  加仓先验：浮盈不够 / 次数满 / 间距不足
  6. Phase 4  confidence scoring + 烈度映射（raw 对齐）
  7. Phase 4  稳定器 3 分钟滞后
  8. Phase 4  regime=extreme 硬红线
  9. Phase 4  闸门缩量（accepted/rejected）
 10. Phase 5  前瞻窗口 + 频控

风格：一个 test 聚焦一个行为；构造最小 MarketContext 仅填充相关字段。
"""

from __future__ import annotations

import pytest

from models.roll_position import (
    ConfidenceThresholds,
    RollEvent,
    RollPlan,
    SafetyGates,
    UserPosition,
)
from processors.roll_forward import ForwardScanner
from processors.roll_position_engine import (
    IntensityStabilizer,
    KeyLevelRef,
    MarketContext,
    compute_add_confidence,
    compute_reduce_confidence,
    evaluate,
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 构造工具
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _pos(
    side="short",
    entry=79000.0,
    leverage=10,
    margin=100.0,
    size=None,
    stop_loss=None,
    events=None,
    total_account=1000.0,
):
    if size is None:
        size = margin * leverage / entry
    return UserPosition(
        id="pos-test",
        coin="BTC",
        side=side,
        margin_mode="isolated",
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


def _plan(
    add_mode="pyramid_decay",
    **kwargs,
):
    defaults = dict(
        id="plan-test",
        position_id="pos-test",
        template_id="pyramid",
        add_mode=add_mode,
        created_at=0,
        gates=SafetyGates(),
        thresholds=ConfidenceThresholds(),
        reduce_signals=[
            "cvd_bear_div", "cvd_bull_div", "exhaustion_warn",
            "fake_break", "structure_choch_against", "reversal_pattern",
        ],
        add_triggers=["structure_breakout_retest", "key_level_bounce"],
    )
    defaults.update(kwargs)
    return RollPlan(**defaults)


def _ctx(
    ts=1_000_000,
    price=75000.0,
    atr=500.0,
    **kwargs,
):
    return MarketContext(ts=ts, current_price=price, atr=atr, **kwargs)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Phase 0 · 数据健康 / 系统护栏
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestPhase0:
    def test_insufficient_data_holds(self):
        pos = _pos()
        plan = _plan()
        ctx = _ctx(data_quality="insufficient", missing_inputs=["te_state", "regime"])
        sig = evaluate(pos, plan, ctx)
        assert sig.action == "hold"
        assert sig.urgency == "info"
        assert "数据不足" in sig.headline_cn
        assert sig.data_quality == "insufficient"
        # C1：Phase 0 中止时必须声明所有下游 phase 被跳过
        assert set(sig.skipped_phases) == {"exit", "reduce", "trail_sl", "add"}

    def test_safety_gate_block_urgent_hold(self):
        pos = _pos()
        plan = _plan()
        ctx = _ctx(safety_gate="block", safety_gate_reason="黑天鹅事件")
        sig = evaluate(pos, plan, ctx)
        assert sig.action == "hold"
        assert sig.urgency == "urgent"
        assert "黑天鹅事件" in sig.detail_cn
        assert set(sig.skipped_phases) == {"exit", "reduce", "trail_sl", "add"}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Phase 1 · 离场
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestPhase1Exit:
    def test_stop_loss_hit_short(self):
        # short @79000 止损 81000，现价 81500 → 击穿
        pos = _pos(side="short", stop_loss=81000.0)
        plan = _plan()
        ctx = _ctx(price=81500.0)
        sig = evaluate(pos, plan, ctx)
        assert sig.action == "close"
        assert sig.urgency == "urgent"
        assert "止损" in sig.headline_cn

    def test_stop_loss_hit_long(self):
        pos = _pos(side="long", entry=50000.0, stop_loss=49000.0)
        plan = _plan()
        ctx = _ctx(price=48500.0)
        sig = evaluate(pos, plan, ctx)
        assert sig.action == "close"

    def test_liq_emergency(self):
        # short @79000 isolated 10x → 爆仓约 86844，现价 83000 → 距爆仓 ~4.6% < 5%
        pos = _pos(side="short", entry=79000.0, leverage=10)
        plan = _plan()
        ctx = _ctx(price=83000.0)
        sig = evaluate(pos, plan, ctx)
        assert sig.action == "close"
        assert "爆仓" in sig.headline_cn
        # C1：Phase 1 命中 → reduce/trail_sl/add 被跳过（exit 本身不算跳过）
        assert set(sig.skipped_phases) == {"reduce", "trail_sl", "add"}

    def test_structure_choch_plus_exhaustion_confirmed(self):
        pos = _pos()
        plan = _plan()
        ctx = _ctx(
            ms_last_event_4h="CHoCH",
            ms_last_event_side_4h="long",       # 反向（我们持 short）
            te_overall_state="exhaustion_confirmed",
        )
        sig = evaluate(pos, plan, ctx)
        assert sig.action == "close"
        assert "结构反转" in sig.headline_cn


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Phase 1.5 · 爆仓预警软减仓（C3）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestPhase15LiqWarning:
    """short @79000 isolated 10x → liq ≈ 86468
    距爆仓：
      - 83000 → ≈ 4.18% < 5% → Phase 1 close
      - 81800 → ≈ 5.71% ∈ [5, 10) → Phase 1.5 半量减仓
      - 78500 → ≈ 10.15% > 10% → 1.5 不触发
    """

    def test_warning_triggers_half_reduce(self):
        pos = _pos(side="short", entry=79000.0, leverage=10)
        plan = _plan()
        ctx = _ctx(price=81800.0)
        sig = evaluate(pos, plan, ctx,
                       liq_emergency_pct=5.0, liq_warning_pct=10.0)
        assert sig.action == "reduce"
        assert sig.urgency == "attention"
        # 半量（step_size × 0.5）
        assert sig.reduce_pct == pytest.approx(plan.reduce_step_size_pct * 0.5)
        # supporting 必含 liq_warning
        assert any(s.source == "liq_warning" for s in sig.supporting)
        # 跳过下游
        assert set(sig.skipped_phases) == {"reduce", "trail_sl", "add"}

    def test_outside_warning_zone_does_not_trigger(self):
        pos = _pos(side="short", entry=79000.0, leverage=10)
        plan = _plan()
        # 距爆仓 ≈ 10.15% > warning 阈值 10 → 1.5 不触发，继续走下游
        ctx = _ctx(price=78500.0)
        sig = evaluate(pos, plan, ctx,
                       liq_emergency_pct=5.0, liq_warning_pct=10.0)
        assert sig.action != "reduce" or not any(
            s.source == "liq_warning" for s in sig.supporting
        )

    def test_emergency_still_wins_over_warning(self):
        pos = _pos(side="short", entry=79000.0, leverage=10)
        plan = _plan()
        # 距爆仓 < 5% 仍走 Phase 1 close，不降级为 1.5 reduce
        ctx = _ctx(price=83000.0)
        sig = evaluate(pos, plan, ctx,
                       liq_emergency_pct=5.0, liq_warning_pct=10.0)
        assert sig.action == "close"

    def test_warning_disabled_when_threshold_not_strictly_greater(self):
        # liq_warning_pct <= liq_emergency_pct → 1.5 禁用
        pos = _pos(side="short", entry=79000.0, leverage=10)
        plan = _plan()
        ctx = _ctx(price=81800.0)   # 在原本的预警区
        sig = evaluate(pos, plan, ctx,
                       liq_emergency_pct=5.0, liq_warning_pct=5.0)
        # 禁用后该仓不应被 1.5 推到 reduce
        assert not (
            sig.action == "reduce"
            and any(s.source == "liq_warning" for s in sig.supporting)
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Phase 2 · 减仓
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestPhase2Reduce:
    def test_full_reduce_triggered(self):
        # 多个减仓信号累加 → score ≥ 60
        pos = _pos(side="short")
        plan = _plan(reduce_signals=[
            "cvd_bull_div", "fake_break", "structure_choch_against",
        ])
        ctx = _ctx(
            cvd_divergence="bull_div",
            nearest_level=KeyLevelRef(price=75000.0, kind="support",
                                      state="fake_break", confluence_score=70),
            ms_last_event_4h="CHoCH",
            ms_last_event_side_4h="long",
        )
        sig = evaluate(pos, plan, ctx)
        assert sig.action == "reduce"
        assert sig.reduce_pct == pytest.approx(plan.reduce_step_size_pct)
        assert sig.reduce_confidence >= 60
        # C1：Phase 2 full 命中 → trail_sl/add 被跳过
        assert set(sig.skipped_phases) == {"trail_sl", "add"}

    def test_half_reduce_triggered(self):
        # 单一中等信号 → 40 ≤ score < 60
        pos = _pos(side="short")
        plan = _plan(reduce_signals=["cvd_bull_div", "exhaustion_warn"])
        ctx = _ctx(
            cvd_divergence="bull_div",  # 20
            te_overall_state="exhaustion_warn",  # 25
        )
        sig = evaluate(pos, plan, ctx)
        # 20+25=45，在 [40, 60) → 半量减仓
        assert sig.action == "reduce"
        assert sig.reduce_pct == pytest.approx(plan.reduce_step_size_pct * 0.5)

    def test_no_reduce_below_threshold(self):
        pos = _pos(side="short")
        plan = _plan(reduce_signals=["cvd_bull_div"])
        ctx = _ctx(cvd_divergence="bull_div")  # 只有 20 分
        sig = evaluate(pos, plan, ctx)
        assert sig.action != "reduce"

    def test_compute_reduce_confidence_direct(self):
        pos = _pos(side="short")
        plan = _plan(reduce_signals=["cvd_bull_div", "fake_break"])
        ctx = _ctx(
            cvd_divergence="bull_div",
            nearest_level=KeyLevelRef(price=75000, kind="support",
                                      state="fake_break", confluence_score=60),
        )
        r = compute_reduce_confidence(pos, plan, ctx)
        assert r.score > 0
        assert "cvd_bull_div" in r.breakdown
        assert "fake_break" in r.breakdown


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Phase 3 · 止损上移
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestPhase3MoveSL:
    def test_move_to_breakeven_after_first_add(self):
        events = [
            RollEvent(ts=100, kind="init", price=79000,
                      margin_delta_usd=100, avg_price_after=79000,
                      leverage_after=10, reason="init"),
            RollEvent(ts=200, kind="add", price=75000,
                      margin_delta_usd=50, avg_price_after=77000,
                      leverage_after=10, reason="add#1"),
        ]
        pos = _pos(side="short", events=events, stop_loss=81000.0)
        plan = _plan(trail_sl_after_add_n=1, min_profit_pct_to_add=100.0)  # 防止进入 phase4 加仓
        ctx = _ctx(price=74000.0)
        sig = evaluate(pos, plan, ctx)
        assert sig.action == "move_sl"
        assert sig.suggested_new_sl == pytest.approx(79000.0)
        assert sig.sl_move_reason == "breakeven"

    def test_no_sl_move_when_already_tighter(self):
        events = [
            RollEvent(ts=100, kind="init", price=79000, margin_delta_usd=100,
                      avg_price_after=79000, leverage_after=10, reason="init"),
            RollEvent(ts=200, kind="add", price=75000, margin_delta_usd=50,
                      avg_price_after=77000, leverage_after=10, reason="add#1"),
        ]
        pos = _pos(side="short", events=events, stop_loss=76000.0)  # 已经很紧
        plan = _plan(trail_sl_after_add_n=1, min_profit_pct_to_add=100.0)
        ctx = _ctx(price=74000.0)
        sig = evaluate(pos, plan, ctx)
        # 止损不应移动（保本 79000 比现止损 76000 松）
        assert sig.action != "move_sl"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Phase 4 · 加仓先验
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestPhase4Preconditions:
    def test_profit_not_enough(self):
        pos = _pos(side="short", entry=79000, leverage=10)
        plan = _plan(min_profit_pct_to_add=5.0)  # 要求价格走 5%
        ctx = _ctx(price=78000.0)  # 只走了 1.27%
        sig = evaluate(pos, plan, ctx)
        assert sig.action == "hold"
        assert "浮盈" in sig.headline_cn

    def test_max_add_times_reached(self):
        events = [
            RollEvent(ts=1, kind="init", price=79000),
            RollEvent(ts=2, kind="add", price=77000),
            RollEvent(ts=3, kind="add", price=75000),
            RollEvent(ts=4, kind="add", price=73000),
        ]
        # short 止损 73500：> 现价(72000) 不触发止损；比深层追踪建议(73750) 更紧 → Phase 3 不触发
        pos = _pos(side="short", events=events, stop_loss=73500.0)
        plan = _plan(max_add_times=3, min_profit_pct_to_add=0.1,
                     trail_sl_after_add_n=1, trail_sl_atr_mult=1.5)
        ctx = _ctx(price=72000.0)
        sig = evaluate(pos, plan, ctx)
        assert sig.action == "hold"
        assert "最大加仓次数" in sig.headline_cn

    def test_bar_distance_insufficient(self):
        events = [
            RollEvent(ts=1, kind="init", price=79000),
            RollEvent(ts=2, kind="add", price=75100),
        ]
        pos = _pos(side="short", events=events, stop_loss=78000.0)
        plan = _plan(min_profit_pct_to_add=0.1,
                     gates=SafetyGates(min_add_bar_distance_atr=2.0))
        ctx = _ctx(price=75000.0, atr=500.0)
        # 上次加仓 75100，现价 75000 → 距离 100 / 500 = 0.2 ATR < 2.0
        sig = evaluate(pos, plan, ctx)
        assert sig.action == "hold"
        assert "间距" in sig.headline_cn


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Phase 4 · confidence scoring
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestAddConfidence:
    def test_trend_aligned_gets_high_score(self):
        """空单遇上 trend_down + ms_bearish + te_momentum + level_bounced → 应拿满分堆。"""
        pos = _pos(side="short")
        ctx = _ctx(
            regime="trend_down",                          # +25
            ms_direction_4h="bearish",                    # +20
            ms_last_event_4h="BOS", ms_last_event_side_4h="short",  # +10
            ms_direction_1h="bearish",                    # +8
            te_overall_state="momentum_continuation",     # +15
            nearest_level=KeyLevelRef(
                price=75500, kind="resistance", state="bounced",
                confluence_score=75, distance_pct=0.7,
            ),                                             # +15 +5
            reversal_pattern="pin_bar_resistance",        # +10
            cvd_divergence="bear_div",                    # +10
            squeeze_state="released_down",                # +10
        )
        r = compute_add_confidence(pos, ctx)
        # 至少超过 full_add 阈值 80
        assert r.score >= 80
        assert r.hard_block is False

    def test_regime_extreme_hard_blocks(self):
        pos = _pos(side="short")
        ctx = _ctx(regime="extreme")
        r = compute_add_confidence(pos, ctx)
        assert r.hard_block is True
        assert r.score <= -100

    def test_opposing_trend_blocks(self):
        """空单遇上 trend_up → 大幅扣分。"""
        pos = _pos(side="short")
        ctx = _ctx(regime="trend_up", ms_direction_4h="bullish")
        r = compute_add_confidence(pos, ctx)
        assert r.score < 0

    def test_range_regime_negative(self):
        pos = _pos(side="short")
        ctx = _ctx(regime="range")
        r = compute_add_confidence(pos, ctx)
        assert r.score < 0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Phase 4 · 稳定器 3 分钟滞后
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestIntensityStabilizer:
    def test_no_change_when_same_intensity(self):
        s = IntensityStabilizer(stabilization_sec=180)
        out = s.observe("p1", "reject", ts=0)
        assert out == "reject"
        out = s.observe("p1", "reject", ts=10)
        assert out == "reject"

    def test_requires_continuous_hold(self):
        s = IntensityStabilizer(stabilization_sec=180)
        # 从 reject 开始
        assert s.observe("p1", "reject", ts=0) == "reject"
        # 首次看到 full → 不切换
        assert s.observe("p1", "full", ts=60) == "reject"
        # 60s 后持续 full → 仍不切换（总计不足 180s）
        assert s.observe("p1", "full", ts=120) == "reject"
        # 再过 120s → 累计 180s，切换成功
        assert s.observe("p1", "full", ts=240) == "full"

    def test_pending_interrupt_restarts_timer(self):
        s = IntensityStabilizer(stabilization_sec=180)
        assert s.observe("p1", "reject", ts=0) == "reject"
        # full 持续 100s 后又看到 half → 重置计时器
        assert s.observe("p1", "full", ts=60) == "reject"
        assert s.observe("p1", "half", ts=100) == "reject"
        assert s.observe("p1", "half", ts=200) == "reject"
        assert s.observe("p1", "half", ts=280) == "half"  # 从 100s 起累计 180s

    def test_independent_positions(self):
        s = IntensityStabilizer(stabilization_sec=180)
        s.observe("p1", "full", ts=60)
        s.observe("p2", "full", ts=60)
        # p1 在 240s 切换
        assert s.observe("p1", "full", ts=240) == "full"
        # p2 仍在原值（p1 的切换不影响 p2 —— 但它们都在 60s 开始 pending，240s 应同步切换）
        assert s.observe("p2", "full", ts=240) == "full"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Phase 4 · 集成：高分 + 稳定器 → 加仓建议
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestPhase4AddRecommendation:
    def _strong_ctx(self, ts=1_000_000, price=75000):
        return _ctx(
            ts=ts, price=price,
            regime="trend_down",
            ms_direction_4h="bearish",
            ms_last_event_4h="BOS", ms_last_event_side_4h="short",
            ms_direction_1h="bearish",
            te_overall_state="momentum_continuation",
            nearest_level=KeyLevelRef(
                price=75500, kind="resistance", state="bounced",
                confluence_score=75, distance_pct=0.7,
            ),
            reversal_pattern="pin_bar_resistance",
            cvd_divergence="bear_div",
        )

    def test_first_pass_waits_for_stabilization(self):
        """首次看到高分 → 稳定器要求 3 分钟，故首次不应切换。"""
        pos = _pos(side="short")
        plan = _plan("pyramid_decay", min_profit_pct_to_add=0.1,
                     pyramid_decay_ratio=0.6)
        pos.events = [RollEvent(ts=0, kind="init", price=79000,
                                 margin_delta_usd=100, avg_price_after=79000,
                                 leverage_after=10, reason="init")]
        stab = IntensityStabilizer(stabilization_sec=180)

        sig = evaluate(pos, plan, self._strong_ctx(ts=1_000_000), stabilizer=stab)
        assert sig.action == "hold"
        assert sig.add_intensity == "reject"
        assert "稳态" in sig.headline_cn or "未达" in sig.headline_cn

    def test_stabilized_emits_add_signal(self):
        pos = _pos(side="short")
        plan = _plan("pyramid_decay", min_profit_pct_to_add=0.1,
                     pyramid_decay_ratio=0.6)
        pos.events = [RollEvent(ts=0, kind="init", price=79000,
                                 margin_delta_usd=100, avg_price_after=79000,
                                 leverage_after=10, reason="init")]
        stab = IntensityStabilizer(stabilization_sec=180)

        # 连续 4 次（间隔 60s）高分 → 累计 180s 切换
        evaluate(pos, plan, self._strong_ctx(ts=0), stabilizer=stab)
        evaluate(pos, plan, self._strong_ctx(ts=60), stabilizer=stab)
        evaluate(pos, plan, self._strong_ctx(ts=120), stabilizer=stab)
        sig = evaluate(pos, plan, self._strong_ctx(ts=180), stabilizer=stab)

        assert sig.action == "add"
        assert sig.add_intensity in ("full", "half", "small")
        assert sig.add_preview is not None
        assert sig.add_preview.final_margin_usd > 0
        # 加仓后均价应该靠近现价
        assert sig.add_preview.after.avg_price < pos.entry_price
        # before / after 字段完整
        assert sig.add_preview.before.avg_price == pos.entry_price
        assert sig.add_preview.gates.gate_a_pass

    def test_regime_extreme_rejects_immediately(self):
        pos = _pos(side="short")
        pos.events = [RollEvent(ts=0, kind="init", price=79000,
                                 margin_delta_usd=100, avg_price_after=79000,
                                 leverage_after=10, reason="init")]
        plan = _plan(min_profit_pct_to_add=0.1)
        stab = IntensityStabilizer(stabilization_sec=1)  # 即使稳定器很松也应立即拒

        ctx = _ctx(price=75000, regime="extreme")
        sig = evaluate(pos, plan, ctx, stabilizer=stab)
        assert sig.action == "hold"
        assert sig.add_intensity == "reject"
        assert "硬红线" in sig.headline_cn or "极端" in sig.headline_cn

    def test_gate_blocked_when_passive_too_aggressive(self):
        """fixed_ratio=1.0 + small 烈度 + 严闸门 → 被缩到低于 min_add → 拒绝加仓。"""
        pos = _pos(side="short")
        pos.events = [RollEvent(ts=0, kind="init", price=79000,
                                 margin_delta_usd=100, avg_price_after=79000,
                                 leverage_after=10, reason="init")]
        # 弱信号 → 稳定后是 small 档
        plan = _plan(
            "fixed_ratio",
            fixed_ratio_of_position=1.0,
            min_profit_pct_to_add=0.1,
            gates=SafetyGates(
                min_avg_distance_pct=5.5,  # 极严
                min_liq_distance_pct=5.0,
                max_eff_leverage=20.0,
                min_add_margin_usd=80.0,   # 最小加仓量 80
            ),
            thresholds=ConfidenceThresholds(
                full_add=80, half_add=55, small_add=40,
                full_reduce=60, half_reduce=40,
            ),
        )
        stab = IntensityStabilizer(stabilization_sec=1)
        # 弱 ctx：只有 regime_trend + ms_align
        ctx = _ctx(price=75000, regime="trend_down", ms_direction_4h="bearish")
        # 两次调用让稳定器稳态
        evaluate(pos, plan, ctx, stabilizer=stab)
        import dataclasses
        ctx2 = dataclasses.replace(ctx, ts=ctx.ts + 10)
        sig = evaluate(pos, plan, ctx2, stabilizer=stab)
        # 要么 gate_blocked，要么 reject
        assert sig.action == "hold"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Phase 5 · 前瞻扫描 + 频控
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestForwardScanner:
    def test_squeeze_release_emits(self):
        scanner = ForwardScanner(default_cooldown_sec=300)
        ctx = _ctx(ts=1000, squeeze_state="pending")
        windows = scanner.scan("p1", ctx)
        kinds = {w.kind for w in windows}
        assert "squeeze_release_imminent" in kinds

    def test_key_level_approaching_emits(self):
        scanner = ForwardScanner()
        ctx = _ctx(nearest_level=KeyLevelRef(
            price=75200, kind="support", state="approaching",
            confluence_score=60, distance_pct=1.0,
        ))
        windows = scanner.scan("p1", ctx)
        kinds = {w.kind for w in windows}
        assert "key_level_approaching" in kinds

    def test_far_level_does_not_emit(self):
        scanner = ForwardScanner()
        ctx = _ctx(nearest_level=KeyLevelRef(
            price=75200, kind="support", state="approaching",
            confluence_score=60, distance_pct=3.0,  # 超过 1.5% 阈值
        ))
        windows = scanner.scan("p1", ctx)
        assert all(w.kind != "key_level_approaching" for w in windows)

    def test_exhaustion_warn_emits(self):
        scanner = ForwardScanner()
        ctx = _ctx(te_overall_state="exhaustion_warn", te_overall_score=-0.6)
        windows = scanner.scan("p1", ctx)
        assert any(w.kind == "exhaustion_early_hint" for w in windows)

    def test_structure_transitioning_emits(self):
        scanner = ForwardScanner()
        ctx = _ctx(ms_direction_4h="transitioning")
        windows = scanner.scan("p1", ctx)
        assert any(w.kind == "structure_pending_confirm" for w in windows)

    def test_cooldown_suppresses_repeat(self):
        scanner = ForwardScanner(default_cooldown_sec=300)
        ctx = _ctx(ts=1000, squeeze_state="pending")
        w1 = scanner.scan("p1", ctx)
        assert any(w.kind == "squeeze_release_imminent" for w in w1)

        ctx2 = _ctx(ts=1100, squeeze_state="pending")  # 100s 后
        w2 = scanner.scan("p1", ctx2)
        # 频控内 → 不应再 emit 相同 kind
        assert not any(w.kind == "squeeze_release_imminent" for w in w2)

    def test_cooldown_resets_after_expire(self):
        scanner = ForwardScanner(default_cooldown_sec=300)
        ctx1 = _ctx(ts=1000, squeeze_state="pending")
        scanner.scan("p1", ctx1)

        ctx2 = _ctx(ts=1400, squeeze_state="pending")  # 超过 300s
        w = scanner.scan("p1", ctx2)
        assert any(w.kind == "squeeze_release_imminent" for w in w)

    def test_cooldown_per_position(self):
        scanner = ForwardScanner(default_cooldown_sec=300)
        ctx = _ctx(ts=1000, squeeze_state="pending")
        scanner.scan("p1", ctx)
        # 不同 position 不共享频控
        w = scanner.scan("p2", ctx)
        assert any(wd.kind == "squeeze_release_imminent" for wd in w)

    def test_evaluate_integrates_scanner(self):
        pos = _pos(side="short", stop_loss=81000.0)
        plan = _plan(min_profit_pct_to_add=100.0)  # 防止进 phase 4
        scanner = ForwardScanner()
        ctx = _ctx(price=75000.0, squeeze_state="pending")
        sig = evaluate(pos, plan, ctx, forward_scanner=scanner)
        # forward_windows 应该被填充
        assert len(sig.forward_windows) >= 1


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# RollSignal 完整性
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestRollSignalCompleteness:
    def test_hold_signal_has_all_required_fields(self):
        pos = _pos()
        plan = _plan(min_profit_pct_to_add=100.0)
        ctx = _ctx()
        sig = evaluate(pos, plan, ctx)
        assert sig.position_id == pos.id
        assert sig.plan_id == plan.id
        assert sig.ts == ctx.ts
        assert sig.coin == pos.coin
        assert sig.current_price == ctx.current_price
        assert sig.action == "hold"
        assert isinstance(sig.supporting, list)
        assert isinstance(sig.blocking, list)
        assert sig.headline_cn

    def test_serializable(self):
        """RollSignal 可完整序列化（WS 推送前提）。"""
        pos = _pos()
        plan = _plan(min_profit_pct_to_add=100.0)
        ctx = _ctx()
        sig = evaluate(pos, plan, ctx)
        dumped = sig.model_dump()
        assert dumped["action"] == "hold"
        assert dumped["position_id"] == pos.id
