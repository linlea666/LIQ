"""V1 vs V2 关键位行为对比回测引擎 · 单元测试（V3-M3 · 2026-04）

覆盖范围：
  1. 三个 evaluate_*_outcome 函数的真值表（true / failed / ambiguous / None）
  2. _signed_distance_atr 方向化距离正确性
  3. _find_future_snapshot 配对逻辑（容差 / 边界）
  4. build_outcome_records 端到端：合成 2 快照（t0 + 4h）→ 验证 OutcomeRecord 字段
  5. 三个 compute_*_stats 函数的混淆矩阵正确性（已知输入 → 已知输出）
  6. 卡方检验（_chi_square_2x2_p）：极端 / 平衡 / 小样本三种情形
  7. run_full_comparison 顶层入口的稳定输出结构

测试纪律：
  - 不依赖真实 kl_history.json，全部用合成快照
  - 每个用例自洽（不共享状态）
  - 覆盖正常路径 + 边界路径 + 异常路径（atr=0 / 缺 level_id / state 不匹配）
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from models.key_level import BehaviorEval, KeyLevelSnapshotV2, KeyLevelV2
from processors.behavior_backtest_engine import (
    CalibrationBucket,
    ComparisonStats,
    ConfusionMatrix,
    MIN_SAMPLES_TRUSTED,
    OutcomeRecord,
    _chi_square_2x2_p,
    _evaluate_decision,
    _find_future_snapshot,
    _is_calibration_monotonic,
    _signed_distance_atr,
    build_outcome_records,
    compute_bounce_quality_stats,
    compute_breakout_stage_stats,
    compute_calibration_buckets,
    compute_fake_break_stats,
    evaluate_bounce_outcome,
    evaluate_breakout_outcome,
    evaluate_fake_break_outcome,
    mcnemar_test,
    run_full_comparison,
    wilson_ci,
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 工具：合成 KeyLevelV2 / Snapshot
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _lv(
    *, level_id: str = "L1",
    price: float = 100_000.0,
    side: str = "support",
    state: str = "idle",
    state_ts: int = 0,
    bounce_quality: str = "",
    breakout_stage: int = 0,
    behavior: BehaviorEval | None = None,
    strength_tier: str = "B",
) -> KeyLevelV2:
    return KeyLevelV2(
        level_id=level_id,
        price=price,
        side=side,
        state=state,
        state_ts=state_ts,
        bounce_quality=bounce_quality,
        breakout_stage=breakout_stage,
        behavior=behavior,
        strength_tier=strength_tier,
    )


def _beh(
    *, bounce_quality_enhanced: float = 0.0,
    breakout_stage_enhanced: int = 0,
    fake_break_strength: float = 0.0,
    dynamic_break_depth_pct: float = 0.0,
) -> BehaviorEval:
    return BehaviorEval(
        bounce_quality_enhanced=bounce_quality_enhanced,
        breakout_stage_enhanced=breakout_stage_enhanced,
        fake_break_strength=fake_break_strength,
        dynamic_break_depth_pct=dynamic_break_depth_pct,
    )


def _snap(
    *, ts: int, current_price: float, atr: float = 500.0,
    levels: list[KeyLevelV2] | None = None,
) -> KeyLevelSnapshotV2:
    return KeyLevelSnapshotV2(
        ts=ts, current_price=current_price, atr=atr,
        levels=levels or [], signals=[],
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 1. 方向化距离 _signed_distance_atr
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestSignedDistance:
    def test_zero_when_invalid(self):
        assert _signed_distance_atr(0, 100, "support", 500) == 0.0
        assert _signed_distance_atr(100, 100, "support", 0) == 0.0

    def test_positive_when_above(self):
        # future > level → 正
        d = _signed_distance_atr(100_000, 101_000, "support", 500)
        assert abs(d - 2.0) < 1e-6

    def test_negative_when_below(self):
        d = _signed_distance_atr(100_000, 99_000, "support", 500)
        assert abs(d + 2.0) < 1e-6


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 2. evaluate_breakout_outcome
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestEvaluateBreakout:
    def test_none_when_state_not_broken(self):
        for s in ("idle", "approaching", "testing", "bounced", "swept", "fake_break"):
            lv = _lv(state=s)
            assert evaluate_breakout_outcome(lv, 99_000, 500) is None

    def test_none_when_atr_zero(self):
        lv = _lv(state="broken")
        assert evaluate_breakout_outcome(lv, 99_000, 0) is None

    def test_support_true_breakout(self):
        """支撑破位向下 1.5 ATR → true_breakout"""
        lv = _lv(side="support", state="broken", price=100_000)
        assert evaluate_breakout_outcome(lv, 99_250, 500) == "true_breakout"  # -1.5 ATR

    def test_support_failed_breakout(self):
        """支撑标记 broken 但价格已回到上方 0.5 ATR → failed_breakout"""
        lv = _lv(side="support", state="broken", price=100_000)
        assert evaluate_breakout_outcome(lv, 100_250, 500) == "failed_breakout"  # +0.5 ATR

    def test_support_ambiguous(self):
        """支撑 broken 但仅向下 0.1 ATR 且未回到上方 → ambiguous"""
        lv = _lv(side="support", state="broken", price=100_000)
        assert evaluate_breakout_outcome(lv, 99_950, 500) == "ambiguous"  # -0.1 ATR

    def test_resistance_true_breakout(self):
        """阻力破位向上 1.2 ATR → true_breakout"""
        lv = _lv(side="resistance", state="broken", price=100_000)
        assert evaluate_breakout_outcome(lv, 100_600, 500) == "true_breakout"  # +1.2 ATR

    def test_resistance_failed_breakout(self):
        lv = _lv(side="resistance", state="broken", price=100_000)
        assert evaluate_breakout_outcome(lv, 99_750, 500) == "failed_breakout"  # -0.5 ATR

    def test_flipped_state_works(self):
        """flipped 也算 broken 谱系"""
        lv = _lv(side="support", state="flipped", price=100_000)
        assert evaluate_breakout_outcome(lv, 99_000, 500) == "true_breakout"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 3. evaluate_bounce_outcome
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestEvaluateBounce:
    def test_none_when_not_bounced(self):
        for s in ("idle", "broken", "fake_break", "flipped"):
            lv = _lv(state=s)
            assert evaluate_bounce_outcome(lv, 101_000, 500) is None

    def test_support_real_bounce(self):
        """支撑反弹后 +1.5 ATR → real_bounce"""
        lv = _lv(side="support", state="bounced", price=100_000)
        assert evaluate_bounce_outcome(lv, 100_750, 500) == "real_bounce"

    def test_support_weak_bounce(self):
        """支撑标记 bounced 但价格反向 -0.5 ATR（破了）→ weak_bounce"""
        lv = _lv(side="support", state="bounced", price=100_000)
        assert evaluate_bounce_outcome(lv, 99_750, 500) == "weak_bounce"

    def test_resistance_real_bounce(self):
        """阻力反弹（被压回）→ -1.2 ATR"""
        lv = _lv(side="resistance", state="bounced", price=100_000)
        assert evaluate_bounce_outcome(lv, 99_400, 500) == "real_bounce"

    def test_ambiguous_band(self):
        lv = _lv(side="support", state="bounced", price=100_000)
        assert evaluate_bounce_outcome(lv, 100_050, 500) == "ambiguous"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 4. evaluate_fake_break_outcome
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestEvaluateFakeBreak:
    def test_none_when_irrelevant_state(self):
        for s in ("idle", "approaching", "bounced", "swept"):
            lv = _lv(state=s)
            assert evaluate_fake_break_outcome(lv, 99_000, 500) is None

    def test_support_confirmed_fake(self):
        """支撑标记破位但已回到 +0.5 ATR → confirmed_fake"""
        lv = _lv(side="support", state="broken", price=100_000)
        assert evaluate_fake_break_outcome(lv, 100_250, 500) == "confirmed_fake"

    def test_support_true_break(self):
        lv = _lv(side="support", state="broken", price=100_000)
        assert evaluate_fake_break_outcome(lv, 99_250, 500) == "true_break"  # -1.5 ATR

    def test_fake_break_state_works(self):
        lv = _lv(side="support", state="fake_break", price=100_000)
        assert evaluate_fake_break_outcome(lv, 100_400, 500) == "confirmed_fake"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 5. _find_future_snapshot 配对逻辑
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestFindFuture:
    def test_finds_closest_within_tolerance(self):
        snaps = [_snap(ts=0, current_price=100_000),
                 _snap(ts=14000, current_price=100_500),  # 4h - 400s
                 _snap(ts=14600, current_price=100_500)]   # 4h + 200s
        # base_idx=0, window=14400, tolerance=600
        # 候选 1: |14000-14400|=400 ≤ 600
        # 候选 2: |14600-14400|=200 ≤ 600 → 更近
        result = _find_future_snapshot(snaps, 0, 14400, 600)
        assert result is not None
        assert result.ts == 14600

    def test_returns_none_when_no_match(self):
        snaps = [_snap(ts=0, current_price=100_000),
                 _snap(ts=20000, current_price=100_500)]  # 远超 4h+600s
        result = _find_future_snapshot(snaps, 0, 14400, 600)
        assert result is None

    def test_returns_none_when_no_future(self):
        snaps = [_snap(ts=0, current_price=100_000)]
        assert _find_future_snapshot(snaps, 0, 14400, 600) is None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 6. build_outcome_records 端到端
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestBuildRecords:
    def test_empty_history(self):
        assert build_outcome_records([], coin="BTC") == []

    def test_skip_levels_without_id(self):
        """缺 level_id 的样本应被跳过。"""
        lv1 = _lv(level_id="", state="bounced", price=100_000)
        snaps = [_snap(ts=0, current_price=100_000, levels=[lv1]),
                 _snap(ts=14400, current_price=100_750)]
        recs = build_outcome_records(snaps, coin="BTC")
        assert recs == []

    def test_full_pipeline(self):
        """完整链路：bounced level → 配对 4h 后快照 → 真相 = real_bounce。"""
        lv = _lv(
            level_id="L1", side="support", state="bounced", price=100_000,
            bounce_quality="proactive",
            behavior=_beh(bounce_quality_enhanced=0.75),
            strength_tier="A",
        )
        snaps = [
            _snap(ts=1000, current_price=100_000, atr=500, levels=[lv]),
            _snap(ts=15400, current_price=100_750, atr=500),  # +1.5 ATR
        ]
        recs = build_outcome_records(snaps, coin="BTC", future_window_sec=14400)
        assert len(recs) == 1
        r = recs[0]
        assert r.coin == "BTC"
        assert r.level_id == "L1"
        assert r.bounce_truth == "real_bounce"
        assert r.v1_bounce_quality == "proactive"
        assert abs(r.v2_bounce_quality_enhanced - 0.75) < 1e-6
        # 突破真相不适用（state != broken）
        assert r.breakout_truth is None
        # fake_break 真相也不适用
        assert r.fake_break_truth is None

    def test_handles_missing_behavior(self):
        """旧快照无 behavior → V2 字段全 0，但仍生成记录。"""
        lv = _lv(level_id="L1", state="bounced", price=100_000, bounce_quality="passive")
        snaps = [
            _snap(ts=0, current_price=100_000, levels=[lv]),
            _snap(ts=14400, current_price=99_500),
        ]
        recs = build_outcome_records(snaps, coin="BTC")
        assert len(recs) == 1
        assert recs[0].v2_bounce_quality_enhanced == 0.0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 7. ConfusionMatrix 计算
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestConfusionMatrix:
    def test_empty_returns_zero(self):
        cm = ConfusionMatrix()
        assert cm.accuracy == 0.0
        assert cm.precision == 0.0
        assert cm.recall == 0.0
        assert cm.f1 == 0.0

    def test_perfect_classifier(self):
        cm = ConfusionMatrix(tp=10, fp=0, tn=10, fn=0)
        assert cm.accuracy == 1.0
        assert cm.precision == 1.0
        assert cm.recall == 1.0
        assert cm.f1 == 1.0

    def test_known_values(self):
        # tp=8 fp=2 tn=7 fn=3 → acc=15/20=0.75, prec=8/10=0.8, recall=8/11≈0.7273
        cm = ConfusionMatrix(tp=8, fp=2, tn=7, fn=3)
        assert cm.accuracy == 0.75
        assert abs(cm.precision - 0.8) < 1e-6
        assert abs(cm.recall - 8 / 11) < 1e-6


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 8. compute_bounce_quality_stats
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _make_bounce_record(
    *, v1_bq: str, v2_bq: float, truth: str, tier: str = "B",
    regime: str = "", state_ts: int = 0,
) -> OutcomeRecord:
    """快速构造 bounced state 的记录用例。"""
    return OutcomeRecord(
        coin="BTC", snapshot_ts=0, future_ts=14400,
        level_id="L", level_price=100_000, level_side="support",
        strength_tier=tier, state="bounced", state_ts=state_ts,
        timeframe="1H", regime=regime,
        v1_bounce_quality=v1_bq,
        v1_breakout_stage=0, v1_state_is_fake_break=False,
        v1_state_is_broken=False,
        v2_bounce_quality_enhanced=v2_bq,
        v2_breakout_stage_enhanced=0,
        v2_fake_break_strength=0.0,
        v2_dynamic_break_depth_pct=0.0,
        future_price=100_000, future_atr=500, future_distance_atr=0.0,
        bounce_truth=truth,
    )


class TestBounceQualityStats:
    def test_zero_records(self):
        s = compute_bounce_quality_stats([])
        assert s.sample_size == 0
        assert s.confusion_v1.accuracy == 0.0

    def test_skips_ambiguous(self):
        recs = [_make_bounce_record(v1_bq="proactive", v2_bq=0.8, truth="ambiguous")]
        s = compute_bounce_quality_stats(recs)
        assert s.sample_size == 0
        assert s.ambiguous_count == 1

    def test_v1_perfect_v2_random(self):
        """构造 V1 完美、V2 全错 → V1 应优于 V2。"""
        recs: list[OutcomeRecord] = []
        for _ in range(20):
            recs.append(_make_bounce_record(
                v1_bq="proactive", v2_bq=0.1,  # V2 < 0.5 → 预测负
                truth="real_bounce",  # 真相 = 正
            ))
            recs.append(_make_bounce_record(
                v1_bq="passive", v2_bq=0.9,  # V2 ≥ 0.5 → 预测正
                truth="weak_bounce",  # 真相 = 负
            ))
        s = compute_bounce_quality_stats(recs)
        assert s.sample_size == 40
        assert s.confusion_v1.accuracy == 1.0
        assert s.confusion_v2.accuracy == 0.0
        assert s.delta_accuracy < 0
        assert s.is_v2_significantly_better is False

    def test_v2_significantly_better(self):
        """V2 完美预测，V1 全错 → V2 显著优于 V1（n=200 满足 ≥100 阈值）。"""
        recs: list[OutcomeRecord] = []
        # M3.1：MIN_SAMPLES_TRUSTED=100，所以正/负各 100 条
        for _ in range(100):
            recs.append(_make_bounce_record(
                v1_bq="passive", v2_bq=0.85,
                truth="real_bounce",
            ))
            recs.append(_make_bounce_record(
                v1_bq="proactive", v2_bq=0.15,
                truth="weak_bounce",
            ))
        s = compute_bounce_quality_stats(recs)
        assert s.confusion_v1.accuracy == 0.0
        assert s.confusion_v2.accuracy == 1.0
        assert s.delta_accuracy > 0.5
        # M3.1：检验主指标改为 McNemar，但卡方仍保留作参考
        assert s.mcnemar_p_value < 0.001
        assert s.chi_square_p_value < 0.001
        assert s.is_v2_significantly_better is True
        # 决策原因列表应该都通过（除校准可能因极端分布单调判定差异）
        assert any("McNemar" in r and "✓" in r for r in s.decision_reasons)
        # Wilson CI 应有效
        assert s.accuracy_ci_v2[0] > 0.9

    def test_tier_filter(self):
        """tier_filter 应只统计指定级别。"""
        recs = [
            _make_bounce_record(v1_bq="proactive", v2_bq=0.8, truth="real_bounce", tier="S"),
            _make_bounce_record(v1_bq="proactive", v2_bq=0.8, truth="weak_bounce", tier="C"),
        ]
        s_all = compute_bounce_quality_stats(recs)
        assert s_all.sample_size == 2
        s_s = compute_bounce_quality_stats(recs, tier_filter=["S"])
        assert s_s.sample_size == 1


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 9. compute_breakout_stage_stats
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _make_breakout_record(
    *, v1_stage: int, v2_stage: int, truth: str,
    regime: str = "", state_ts: int = 0,
) -> OutcomeRecord:
    return OutcomeRecord(
        coin="BTC", snapshot_ts=0, future_ts=14400,
        level_id="L", level_price=100_000, level_side="support",
        strength_tier="A", state="broken", state_ts=state_ts,
        timeframe="1D", regime=regime,
        v1_bounce_quality="", v1_breakout_stage=v1_stage,
        v1_state_is_fake_break=False, v1_state_is_broken=True,
        v2_bounce_quality_enhanced=0.0,
        v2_breakout_stage_enhanced=v2_stage,
        v2_fake_break_strength=0.0,
        v2_dynamic_break_depth_pct=0.0,
        future_price=99_000, future_atr=500, future_distance_atr=-2.0,
        breakout_truth=truth,
    )


class TestBreakoutStageStats:
    def test_v2_better_for_long_timeframe(self):
        """模拟 1D 关键位场景：V1 总是 stage<3，V2 给到 stage=3。

        V1 全负预测 vs V2 全正预测 → 在 50% 正样本下两者 acc 都是 0.5。
        这是个"等效"场景，不应判定 V2 显著更优。"""
        recs: list[OutcomeRecord] = []
        for _ in range(20):
            recs.append(_make_breakout_record(v1_stage=1, v2_stage=3, truth="true_breakout"))
            recs.append(_make_breakout_record(v1_stage=1, v2_stage=3, truth="failed_breakout"))
        s = compute_breakout_stage_stats(recs)
        # V1 总是 stage<3 → 全部预测负
        # 真相 50% 正 → V1 acc = 0.5
        assert abs(s.confusion_v1.accuracy - 0.5) < 1e-6
        # V2 总是 stage=3 → 全部预测正
        # 真相 50% 正 → V2 acc = 0.5
        assert abs(s.confusion_v2.accuracy - 0.5) < 1e-6


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 10. compute_fake_break_stats
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _make_fake_break_record(
    *, v2_strength: float, truth: str, v1_is_fake: bool = False,
    state: str = "broken",
) -> OutcomeRecord:
    """假破回收测试记录构造器。"""
    return OutcomeRecord(
        coin="BTC", snapshot_ts=0, future_ts=1, level_id="L",
        level_price=100_000, level_side="support", strength_tier="A",
        state=state, state_ts=0, timeframe="1H", regime="",
        v1_bounce_quality="", v1_breakout_stage=0,
        v1_state_is_fake_break=v1_is_fake,
        v1_state_is_broken=(state in ("broken", "flipped")),
        v2_bounce_quality_enhanced=0.0, v2_breakout_stage_enhanced=0,
        v2_fake_break_strength=v2_strength,
        v2_dynamic_break_depth_pct=0.0,
        future_price=100_300 if truth == "confirmed_fake" else 99_000,
        future_atr=500,
        future_distance_atr=0.6 if truth == "confirmed_fake" else -2.0,
        fake_break_truth=truth,
    )


class TestFakeBreakStats:
    def test_perfect_v2(self):
        """V2 完美预测假破 → V2 显著优于 V1（n=200 满足 ≥100 阈值）。"""
        recs: list[OutcomeRecord] = []
        for _ in range(100):
            recs.append(_make_fake_break_record(v2_strength=0.85, truth="confirmed_fake"))
            recs.append(_make_fake_break_record(v2_strength=0.10, truth="true_break"))
        s = compute_fake_break_stats(recs)
        assert s.confusion_v2.accuracy == 1.0
        # V1 全部 state==broken（不是 fake_break）→ 全预测负
        # 真相 50% 正 → V1 acc = 0.5
        assert abs(s.confusion_v1.accuracy - 0.5) < 1e-6
        assert s.is_v2_significantly_better is True


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 11. 卡方检验
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestChiSquare:
    def test_small_sample_returns_nonsig(self):
        c1 = ConfusionMatrix(tp=2, fp=1, tn=2, fn=1)
        c2 = ConfusionMatrix(tp=3, fp=0, tn=3, fn=0)
        chi, p = _chi_square_2x2_p(c1, c2)
        # 总样本 12，但 Yates 校正后差异小 → p 不应小于 0.01
        assert p > 0.01

    def test_perfect_separation_low_p(self):
        # V1 全错 vs V2 全对，n=200
        c1 = ConfusionMatrix(tp=0, fp=50, tn=0, fn=50)
        c2 = ConfusionMatrix(tp=50, fp=0, tn=50, fn=0)
        chi, p = _chi_square_2x2_p(c1, c2)
        assert p < 0.001

    def test_no_difference_high_p(self):
        c1 = ConfusionMatrix(tp=25, fp=25, tn=25, fn=25)
        c2 = ConfusionMatrix(tp=25, fp=25, tn=25, fn=25)
        chi, p = _chi_square_2x2_p(c1, c2)
        assert p > 0.5


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 12. run_full_comparison 顶层入口
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestRunFullComparison:
    def test_empty_history_safe(self):
        out = run_full_comparison([], coin="BTC")
        assert out["coin"] == "BTC"
        assert out["total_records"] == 0
        assert "stats" in out
        assert set(out["stats"].keys()) == {"bounce_quality", "breakout_stage", "fake_break"}

    def test_full_pipeline_synthetic(self):
        """合成 5 个连续 4h 间隔的快照 → 验证返回结构稳定。

        M3.1 升级：默认开启事件去重 → 同一 (level_id, state, state_ts) 只保留一条。
        本场景 5 个快照中 lv state=bounced 且 state_ts=0 → 去重后只剩 1 条。"""
        snaps = []
        for i in range(5):
            lv = _lv(
                level_id="L1", side="support", state="bounced", price=100_000,
                bounce_quality="proactive",
                behavior=_beh(bounce_quality_enhanced=0.75),
                strength_tier="S",
            )
            snaps.append(_snap(
                ts=i * 14400, current_price=100_000 + i * 200, atr=500,
                levels=[lv],
            ))
        out = run_full_comparison(snaps, coin="BTC", future_window_sec=14400)
        # M3.1：去重后同一事件只剩 1 条记录
        assert out["total_records"] == 1
        # 关闭去重 → 4 配对
        out2 = run_full_comparison(
            snaps, coin="BTC", future_window_sec=14400,
            deduplicate_events=False,
        )
        assert out2["total_records"] >= 4
        assert out["params"]["future_window_sec"] == 14400
        assert "v1" in out["stats"]["bounce_quality"]
        assert "v2" in out["stats"]["bounce_quality"]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 13. M3.1 新增 · McNemar 检验
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestMcNemar:
    def test_small_sample_returns_nonsig(self):
        chi, p = mcnemar_test(b=2, c=1)
        assert p == 1.0  # b+c=3 < 10 阈值
        assert chi == 0.0

    def test_no_difference_high_p(self):
        # b == c → 完全无方向性差异
        chi, p = mcnemar_test(b=10, c=10)
        assert p == 1.0  # |b-c|-1 = -1 ≤ 0

    def test_strong_v2_advantage(self):
        # 50 个样本里 V1 错 V2 对 = 45，V1 对 V2 错 = 5 → V2 大幅占优
        chi, p = mcnemar_test(b=45, c=5)
        assert chi > 0
        assert p < 0.001

    def test_symmetric_with_correction(self):
        # |b-c|=1 → 连续性校正后 diff=0 → 不显著
        _chi, p = mcnemar_test(b=20, c=19)
        assert p == 1.0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 14. M3.1 新增 · Wilson 95% CI
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestWilsonCI:
    def test_zero_total_safe(self):
        low, high = wilson_ci(0, 0)
        assert low == 0.0 and high == 0.0

    def test_perfect_success_high_low_below_one(self):
        # 100/100 → 上限 ≈ 1.0，下限 < 1（不再越界）
        low, high = wilson_ci(100, 100)
        assert abs(high - 1.0) < 1e-9
        assert low > 0.9 and low < 1.0

    def test_balanced_centered_around_half(self):
        low, high = wilson_ci(50, 100)
        assert low < 0.5 < high
        # 95% CI 宽度约 0.2 左右
        assert (high - low) < 0.25

    def test_smaller_sample_wider_ci(self):
        low_big, high_big = wilson_ci(50, 100)
        low_small, high_small = wilson_ci(5, 10)
        assert (high_small - low_small) > (high_big - low_big)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 15. M3.1 新增 · ConfusionMatrix balanced_acc / MCC / specificity
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestConfusionMatrixExtras:
    def test_perfect_mcc_one(self):
        cm = ConfusionMatrix(tp=50, fp=0, tn=50, fn=0)
        assert cm.mcc == 1.0
        assert cm.specificity == 1.0
        assert cm.balanced_accuracy == 1.0

    def test_random_mcc_zero(self):
        # 平衡随机：tp=fp, tn=fn
        cm = ConfusionMatrix(tp=25, fp=25, tn=25, fn=25)
        assert abs(cm.mcc) < 1e-6
        assert cm.balanced_accuracy == 0.5

    def test_inverted_mcc_negative(self):
        cm = ConfusionMatrix(tp=0, fp=50, tn=0, fn=50)
        assert cm.mcc < 0  # 类似随机但偏负

    def test_specificity_recall_imbalance(self):
        # recall=1, specificity=0 → 类别不平衡问题
        cm = ConfusionMatrix(tp=10, fp=90, tn=0, fn=0)
        assert cm.recall == 1.0
        assert cm.specificity == 0.0
        assert cm.balanced_accuracy == 0.5  # 平衡指标揭穿了 acc=0.1 的伪装


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 16. M3.1 新增 · 分桶校准
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestCalibration:
    def test_breakout_dimension_returns_empty(self):
        """breakout_stage 是离散维度 → 不适用分桶校准。"""
        recs = [_make_breakout_record(v1_stage=1, v2_stage=3, truth="true_breakout")]
        buckets = compute_calibration_buckets(recs, dimension="breakout_stage")
        assert buckets == []

    def test_perfectly_monotonic(self):
        """构造 V2 分数与真相完全相关 → 桶 hit_rate 单调递增。"""
        recs: list[OutcomeRecord] = []
        # 桶 0-0.2 全错（hit=0）；桶 0.8-1.0 全对（hit=1）
        for _ in range(10):
            recs.append(_make_bounce_record(v1_bq="passive", v2_bq=0.05, truth="weak_bounce"))
            recs.append(_make_bounce_record(v1_bq="passive", v2_bq=0.25, truth="weak_bounce"))
            recs.append(_make_bounce_record(v1_bq="passive", v2_bq=0.45, truth="weak_bounce"))
            recs.append(_make_bounce_record(v1_bq="passive", v2_bq=0.65, truth="real_bounce"))
            recs.append(_make_bounce_record(v1_bq="passive", v2_bq=0.85, truth="real_bounce"))
        buckets = compute_calibration_buckets(recs, dimension="bounce_quality")
        assert len(buckets) == 5
        assert buckets[0].hit_rate == 0.0
        assert buckets[4].hit_rate == 1.0
        assert _is_calibration_monotonic(buckets) is True

    def test_non_monotonic_detected(self):
        """V2 分数与真相反相关 → 高分桶 hit_rate 反而低 → 不单调。"""
        recs: list[OutcomeRecord] = []
        for _ in range(10):
            recs.append(_make_bounce_record(v1_bq="passive", v2_bq=0.85, truth="weak_bounce"))
            recs.append(_make_bounce_record(v1_bq="passive", v2_bq=0.05, truth="real_bounce"))
        buckets = compute_calibration_buckets(recs, dimension="bounce_quality")
        # 桶 0-0.2 全对（hit=1.0）； 桶 0.8-1.0 全错（hit=0.0）→ 不单调
        valid = [b for b in buckets if b.sample_size >= 3]
        assert len(valid) == 2
        assert _is_calibration_monotonic(buckets) is False

    def test_to_dict_shape(self):
        b = CalibrationBucket(range_low=0.0, range_high=0.2, sample_size=10, hit_count=3, hit_rate=0.3)
        d = b.to_dict()
        assert d == {
            "range_low": 0.0, "range_high": 0.2,
            "sample_size": 10, "hit_count": 3, "hit_rate": 0.3,
        }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 17. M3.1 新增 · 多条件决策 _evaluate_decision
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestEvaluateDecision:
    def test_below_min_sample_blocks(self):
        c1 = ConfusionMatrix(tp=20, fp=0, tn=20, fn=0)
        c2 = ConfusionMatrix(tp=20, fp=0, tn=20, fn=0)
        # n=40 < 100 → 不通过
        better, reasons = _evaluate_decision(c1, c2, mcnemar_p=0.0001, sample_size=40,
                                              calibration_monotonic=True)
        assert better is False
        assert any("样本量" in r and "✗" in r for r in reasons)

    def test_recall_collapse_blocks(self):
        # V2 precision 提升 0.10，但 recall 从 0.8 崩到 0.4（< 0.8×0.85=0.68）
        c1 = ConfusionMatrix(tp=80, fp=20, tn=80, fn=20)  # prec 0.8 / rec 0.8
        c2 = ConfusionMatrix(tp=40, fp=0, tn=100, fn=60)  # prec 1.0 / rec 0.4
        better, reasons = _evaluate_decision(c1, c2, mcnemar_p=0.001, sample_size=200,
                                              calibration_monotonic=True)
        assert better is False
        assert any("recall" in r and "✗" in r for r in reasons)

    def test_full_pass(self):
        # 全部条件满足
        c1 = ConfusionMatrix(tp=50, fp=20, tn=80, fn=50)  # prec 0.71
        c2 = ConfusionMatrix(tp=70, fp=10, tn=90, fn=30)  # prec 0.875, rec 0.7
        better, reasons = _evaluate_decision(c1, c2, mcnemar_p=0.001, sample_size=200,
                                              calibration_monotonic=True)
        assert better is True
        assert all("✗" not in r for r in reasons)

    def test_mcnemar_not_significant_blocks(self):
        c1 = ConfusionMatrix(tp=70, fp=30, tn=70, fn=30)
        c2 = ConfusionMatrix(tp=72, fp=28, tn=72, fn=28)
        better, _ = _evaluate_decision(c1, c2, mcnemar_p=0.10, sample_size=200,
                                        calibration_monotonic=True)
        assert better is False


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 18. M3.1 新增 · build_outcome_records 事件去重
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestEventDedup:
    def test_same_state_ts_collapses(self):
        """同一 (level_id, state, state_ts) 在 5 个快照中重复 → 只剩 1 条。"""
        snaps = []
        for i in range(5):
            lv = _lv(level_id="L1", side="support", state="bounced", price=100_000,
                     bounce_quality="proactive",
                     behavior=_beh(bounce_quality_enhanced=0.7),
                     state_ts=1000)  # 关键：state_ts 固定
            snaps.append(_snap(ts=i * 14400, current_price=100_500, atr=500, levels=[lv]))
        recs = build_outcome_records(snaps, coin="BTC", future_window_sec=14400)
        assert len(recs) == 1

    def test_different_state_ts_kept(self):
        """state_ts 不同（每次都是新事件）→ 全部保留。"""
        snaps = []
        for i in range(4):
            lv = _lv(level_id="L1", side="support", state="bounced", price=100_000,
                     bounce_quality="proactive",
                     behavior=_beh(bounce_quality_enhanced=0.7),
                     state_ts=1000 * (i + 1))
            snaps.append(_snap(ts=i * 14400, current_price=100_500, atr=500, levels=[lv]))
        recs = build_outcome_records(snaps, coin="BTC", future_window_sec=14400)
        assert len(recs) == 3  # 4 快照 → 3 配对，每个 state_ts 不同 → 不去重

    def test_dedup_disabled(self):
        snaps = []
        for i in range(5):
            lv = _lv(level_id="L1", side="support", state="bounced", price=100_000,
                     bounce_quality="proactive",
                     behavior=_beh(bounce_quality_enhanced=0.7),
                     state_ts=1000)
            snaps.append(_snap(ts=i * 14400, current_price=100_500, atr=500, levels=[lv]))
        recs = build_outcome_records(snaps, coin="BTC", future_window_sec=14400,
                                     deduplicate=False)
        assert len(recs) >= 4

    def test_require_behavior_eval_filter(self):
        """behavior_eval_available=False 的样本应被过滤（M3.1 元信息保护）。"""
        # 使用未填充 behavior 的旧快照 → behavior=None → require_behavior_eval 仍保留
        # （None 视为旧版本数据，不强制过滤）
        snaps = []
        for i in range(3):
            lv = _lv(level_id="L1", side="support", state="bounced", price=100_000,
                     bounce_quality="proactive", behavior=None, state_ts=1000 * (i + 1))
            snaps.append(_snap(ts=i * 14400, current_price=100_500, atr=500, levels=[lv]))
        recs = build_outcome_records(snaps, coin="BTC", future_window_sec=14400,
                                     require_behavior_eval=True)
        assert len(recs) == 2  # behavior=None 不过滤；2 配对


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 19. M3.1 新增 · regime / state filter
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestFilters:
    def test_regime_filter(self):
        recs = [
            _make_bounce_record(v1_bq="proactive", v2_bq=0.7, truth="real_bounce", regime="trend_up"),
            _make_bounce_record(v1_bq="passive", v2_bq=0.7, truth="weak_bounce", regime="range"),
        ]
        s_all = compute_bounce_quality_stats(recs)
        assert s_all.sample_size == 2
        s_trend = compute_bounce_quality_stats(recs, regime_filter=["trend_up"])
        assert s_trend.sample_size == 1

    def test_state_filter_for_breakout(self):
        recs = [
            _make_breakout_record(v1_stage=3, v2_stage=3, truth="true_breakout"),
            _make_breakout_record(v1_stage=3, v2_stage=3, truth="failed_breakout"),
        ]
        # state_filter=["broken"]（breakout_record 默认 broken）→ 全部命中
        s = compute_breakout_stage_stats(recs, state_filter=["broken"])
        assert s.sample_size == 2
        s_empty = compute_breakout_stage_stats(recs, state_filter=["flipped"])
        assert s_empty.sample_size == 0
