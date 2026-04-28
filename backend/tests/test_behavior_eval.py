"""V3 行为评估层（key_level_behavior_eval）单元测试。

覆盖：
1. evaluate_behavior 主入口在各种 state 下的字段填充正确性
2. 6 个子分数的方向性（高质量场景 vs 低质量场景输出对比单调）
3. behavior_state 派生规则（9 选 1 的边界）
4. state_confidence 与对应分数的一致性
5. explain_chips 长度限制 + 不为空
6. 异常隔离：单 level 计算失败不影响其他 level
7. 严格只读纪律：调用前后 lv.state / lv.final_score / lv.cascade_risk 不被修改
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from models.flow import CVDData
from models.key_level import (
    BehaviorEval,
    KeyLevelSnapshotV2,
    KeyLevelV2,
)
from models.market import CandleData
from processors.key_level_behavior_eval import (
    _DEFAULT_BEHAVIOR_CFG,
    _calc_breakout_validity,
    _calc_capitulation,
    _calc_false_break_risk,
    _calc_flip_confirmation,
    _calc_retest_quality,
    _calc_selloff_continuation,
    _clamp01,
    _derive_behavior_state,
    _detect_contradictions,
    _prepare_context,
    assess_fake_break_strength,
    compute_bounce_quality_v2,
    compute_breakout_stage_v2,
    compute_dynamic_break_depth_pct,
    evaluate_behavior,
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 测试辅助
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _candles(n: int = 30, base: float = 100_000.0, base_vol: float = 100.0,
             *, last_close: float | None = None, last_vol: float | None = None,
             last_low: float | None = None, last_high: float | None = None,
             last_open: float | None = None) -> list[CandleData]:
    """生成 n 根中性 K 线，并允许覆盖最后一根。"""
    bars: list[CandleData] = []
    base_ts = int(time.time()) - n * 900
    for i in range(n):
        bars.append(
            CandleData(
                coin="BTC",
                ts=base_ts + i * 900,
                o=base, h=base + 50, l=base - 50, c=base + 10,
                vol=base_vol,
            )
        )
    if last_close is not None or last_vol is not None or last_low is not None \
            or last_high is not None or last_open is not None:
        last = bars[-1]
        bars[-1] = CandleData(
            coin="BTC",
            ts=last.ts,
            o=last_open if last_open is not None else last.open,
            h=last_high if last_high is not None else last.high,
            l=last_low if last_low is not None else last.low,
            c=last_close if last_close is not None else last.close,
            vol=last_vol if last_vol is not None else last.vol,
        )
    return bars


def _snapshot(levels: list[KeyLevelV2], price: float = 100_000.0, atr: float = 500.0) -> KeyLevelSnapshotV2:
    return KeyLevelSnapshotV2(
        ts=int(time.time()), current_price=price, atr=atr,
        levels=levels, signals=[],
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 主入口：基础占位 + 异常隔离
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestEvaluateBehaviorEntry:
    def test_writes_behavior_to_every_level(self):
        levels = [
            KeyLevelV2(price=100_000, side="support", state="idle"),
            KeyLevelV2(price=101_500, side="resistance", state="approaching",
                       state_ts=int(time.time()) - 60),
        ]
        snap = _snapshot(levels)
        evaluate_behavior(snap, candles_15m=_candles())
        for lv in snap.levels:
            assert lv.behavior is not None
            assert isinstance(lv.behavior, BehaviorEval)
            assert lv.behavior.evaluated_at > 0

    def test_pending_when_atr_zero(self):
        levels = [KeyLevelV2(price=100_000, side="support", state="broken",
                             state_ts=int(time.time()) - 60)]
        snap = _snapshot(levels, atr=0.0)
        evaluate_behavior(snap, candles_15m=_candles())
        assert snap.levels[0].behavior.behavior_state == "pending"
        assert snap.levels[0].behavior.breakout_validity == 0.0

    def test_does_not_modify_state_or_score(self):
        """关键纪律：行为层不修改任何已有字段。"""
        lv = KeyLevelV2(
            price=100_000, side="support", state="broken",
            state_ts=int(time.time()) - 60,
            final_score=72.5, strength_tier="A", cascade_risk=0.45,
            bounce_quality="proactive", breakout_stage=2,
        )
        snap = _snapshot([lv])
        evaluate_behavior(snap, candles_15m=_candles())
        # 旧字段一字不变
        assert lv.state == "broken"
        assert lv.final_score == 72.5
        assert lv.strength_tier == "A"
        assert lv.cascade_risk == 0.45
        assert lv.bounce_quality == "proactive"
        assert lv.breakout_stage == 2
        # 新字段被填充
        assert lv.behavior is not None

    def test_isolates_single_level_failure(self, monkeypatch):
        """一个 level 的评估失败，不影响其他 level。"""
        from processors import key_level_behavior_eval as bem

        original = bem._evaluate_one_level
        call_count = {"n": 0}

        def fail_first(lv, *args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("simulated")
            return original(lv, *args, **kwargs)

        monkeypatch.setattr(bem, "_evaluate_one_level", fail_first)

        levels = [
            KeyLevelV2(price=100_000, side="support", state="testing",
                       state_ts=int(time.time()) - 60),
            KeyLevelV2(price=101_000, side="resistance", state="approaching",
                       state_ts=int(time.time()) - 60),
        ]
        snap = _snapshot(levels)
        evaluate_behavior(snap, candles_15m=_candles())
        # level[0] 失败 → pending；level[1] 正常
        assert snap.levels[0].behavior.behavior_state == "pending"
        assert snap.levels[1].behavior is not None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# breakout_validity（向下方向单调性）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestBreakoutValidity:
    def _make_ctx(self, candles, atr=500):
        return _prepare_context(
            candles_15m=candles, candles_1h=None, cvd=None,
            oi_change_pct_1h=0.0, taker_buy_vol=0, taker_sell_vol=0,
            atr=atr, cfg=_DEFAULT_BEHAVIOR_CFG,
        )

    def test_high_quality_breakout(self):
        """支撑被突破：放量 + 收盘远离 + 大实体 + sell 主导 → 高分。"""
        candles = _candles(
            n=30, base=100_000, base_vol=100,
            last_close=99_300,        # 站到下方
            last_open=99_900,         # 实体大
            last_low=99_300,
            last_high=99_950,
            last_vol=300,             # 3x 放量
        )
        # 倒数第二根也站下方（已收盘确认）
        candles[-2] = CandleData(
            coin="BTC", ts=candles[-2].ts,
            o=99_900, h=99_950, l=99_400, c=99_500, vol=200,
        )
        cvd = CVDData(coin="BTC", inst_type="CONTRACTS", series=[],
                      trend_1h="falling")
        ctx = _prepare_context(
            candles_15m=candles, candles_1h=None, cvd=cvd,
            oi_change_pct_1h=0.0, taker_buy_vol=10, taker_sell_vol=90,
            atr=500, cfg=_DEFAULT_BEHAVIOR_CFG,
        )
        lv = KeyLevelV2(price=100_000, side="support", state="broken",
                        state_ts=int(time.time()) - 60, breakout_stage=3)
        score = _calc_breakout_validity(lv, ctx, _DEFAULT_BEHAVIOR_CFG)
        assert score >= 0.65

    def test_low_quality_breakout(self):
        """支撑被刺破：缩量 + 收盘还在原区间 + 小实体 → 低分。"""
        candles = _candles(
            n=30, base=100_000, base_vol=100,
            last_close=99_950,        # 收回原区间
            last_open=99_980,
            last_low=99_500,
            last_high=100_050,
            last_vol=50,              # 缩量
        )
        ctx = self._make_ctx(candles)
        lv = KeyLevelV2(price=100_000, side="support", state="broken",
                        state_ts=int(time.time()) - 60, breakout_stage=1)
        score = _calc_breakout_validity(lv, ctx, _DEFAULT_BEHAVIOR_CFG)
        assert score <= 0.50

    def test_validity_in_range(self):
        candles = _candles()
        ctx = self._make_ctx(candles)
        lv = KeyLevelV2(price=100_000, side="resistance", state="broken",
                        state_ts=int(time.time()) - 60)
        score = _calc_breakout_validity(lv, ctx, _DEFAULT_BEHAVIOR_CFG)
        assert 0.0 <= score <= 1.0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# retest_quality（缩量回踩 vs 放量回踩）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestRetestQuality:
    def test_healthy_retest_high(self):
        candles = _candles(n=30, base=100_000, base_vol=100, last_vol=60)  # 缩量
        ctx = _prepare_context(
            candles_15m=candles, candles_1h=None, cvd=None,
            oi_change_pct_1h=0.0, taker_buy_vol=0, taker_sell_vol=0,
            atr=500, cfg=_DEFAULT_BEHAVIOR_CFG,
        )
        lv = KeyLevelV2(
            price=100_000, side="support", state="bounced",
            state_ts=int(time.time()) - 60,
            bounce_quality="proactive",
            lowest_wick=99_900,  # 几乎没回踩
        )
        score = _calc_retest_quality(lv, ctx, _DEFAULT_BEHAVIOR_CFG)
        assert score >= 0.55

    def test_unhealthy_retest_low(self):
        candles = _candles(n=30, base=100_000, base_vol=100, last_vol=200)  # 放量
        ctx = _prepare_context(
            candles_15m=candles, candles_1h=None, cvd=None,
            oi_change_pct_1h=0.0, taker_buy_vol=0, taker_sell_vol=0,
            atr=500, cfg=_DEFAULT_BEHAVIOR_CFG,
        )
        lv = KeyLevelV2(
            price=100_000, side="support", state="bounced",
            state_ts=int(time.time()) - 60,
            bounce_quality="passive",
            lowest_wick=99_200,  # 深度回踩
        )
        score = _calc_retest_quality(lv, ctx, _DEFAULT_BEHAVIOR_CFG)
        assert score <= 0.50


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# selloff_continuation_risk（破位延续）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestSelloffContinuationRisk:
    def test_high_risk_breakdown(self):
        """放量破位 + 大真空 + 高 cascade + CVD 持续走弱 → 高风险。"""
        candles = _candles(
            n=30, base=100_000, base_vol=100,
            last_close=99_300, last_low=99_300, last_high=99_700, last_vol=400,
        )
        cvd = CVDData(coin="BTC", inst_type="CONTRACTS", series=[],
                      trend_1h="falling")
        ctx = _prepare_context(
            candles_15m=candles, candles_1h=None, cvd=cvd,
            oi_change_pct_1h=0.0, taker_buy_vol=20, taker_sell_vol=80,
            atr=500, cfg=_DEFAULT_BEHAVIOR_CFG,
        )
        lv = KeyLevelV2(
            price=100_000, side="support", state="broken",
            state_ts=int(time.time()) - 60,
            cascade_risk=0.75, vacuum_gap_pct=2.5,
            next_magnet_price=97_000,
        )
        score = _calc_selloff_continuation(
            lv, _snapshot([lv], price=99_300), ctx, _DEFAULT_BEHAVIOR_CFG, breakout_validity=0.8,
        )
        assert score >= 0.65

    def test_low_risk_breakdown(self):
        """没有 cascade，没有真空，CVD 中性 → 低风险。"""
        candles = _candles(n=30)
        ctx = _prepare_context(
            candles_15m=candles, candles_1h=None, cvd=None,
            oi_change_pct_1h=0.0, taker_buy_vol=50, taker_sell_vol=50,
            atr=500, cfg=_DEFAULT_BEHAVIOR_CFG,
        )
        lv = KeyLevelV2(
            price=100_000, side="support", state="broken",
            state_ts=int(time.time()) - 60,
            cascade_risk=0.05, vacuum_gap_pct=0.3,
        )
        score = _calc_selloff_continuation(
            lv, _snapshot([lv]), ctx, _DEFAULT_BEHAVIOR_CFG, breakout_validity=0.3,
        )
        assert score <= 0.55


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# capitulation_bottom_score（恐慌出清）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestCapitulation:
    def test_below_panic_gate_zero(self):
        """量能不到 panic gate → 直接 0.0。

        构造：历史 50 根均量 300，最后一根 vol=110 → percentile=0（当前是历史最低之一）
        """
        candles = _candles(n=60, base=100_000, base_vol=300, last_vol=110)
        ctx = _prepare_context(
            candles_15m=candles, candles_1h=None, cvd=None,
            oi_change_pct_1h=0.0, taker_buy_vol=0, taker_sell_vol=0,
            atr=500, cfg=_DEFAULT_BEHAVIOR_CFG,
        )
        lv = KeyLevelV2(price=100_000, side="support", state="broken")
        # 验证 panic_percentile 没过 gate（0.85）
        assert ctx["panic_volume_percentile"] < 0.85
        score = _calc_capitulation(lv, ctx, _DEFAULT_BEHAVIOR_CFG)
        assert score == 0.0

    def test_above_panic_gate_with_long_wick(self):
        """量能极端 + 长下影 + CVD 背离 + OI 大跌 → 高分。"""
        # 50 根历史均量 100；最后一根放量到 800（远超 95 分位）
        candles = _candles(n=60, base=100_000, base_vol=100)
        candles[-1] = CandleData(
            coin="BTC", ts=candles[-1].ts,
            o=99_900, h=100_000, l=98_500, c=99_800, vol=800,
        )
        # 长下影：rng=1500, body=99_900~99_800=100，下影=99_900-98_500=1400 → 占 93%
        cvd = CVDData(coin="BTC", inst_type="CONTRACTS", series=[],
                      trend_1h="falling", has_divergence=True)
        ctx = _prepare_context(
            candles_15m=candles, candles_1h=None, cvd=cvd,
            oi_change_pct_1h=-2.5, taker_buy_vol=50, taker_sell_vol=50,
            atr=500, cfg=_DEFAULT_BEHAVIOR_CFG,
        )
        lv = KeyLevelV2(price=100_000, side="support", state="broken")
        score = _calc_capitulation(lv, ctx, _DEFAULT_BEHAVIOR_CFG)
        assert score >= 0.65


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# flip_confirmation（翻转确认）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestFlipConfirmation:
    def test_high_when_full_confirmation(self):
        ctx = _prepare_context(
            candles_15m=_candles(), candles_1h=None, cvd=None,
            oi_change_pct_1h=0.0, taker_buy_vol=0, taker_sell_vol=0,
            atr=500, cfg=_DEFAULT_BEHAVIOR_CFG,
        )
        lv = KeyLevelV2(
            price=100_000, side="support", state="flipped",
            breakout_stage=3, bounce_count=2,
        )
        score = _calc_flip_confirmation(
            lv, ctx, _DEFAULT_BEHAVIOR_CFG,
            breakout_validity=0.85, retest_quality=0.75,
        )
        assert score >= 0.7

    def test_low_when_no_market_confirmation(self):
        ctx = _prepare_context(
            candles_15m=_candles(), candles_1h=None, cvd=None,
            oi_change_pct_1h=0.0, taker_buy_vol=0, taker_sell_vol=0,
            atr=500, cfg=_DEFAULT_BEHAVIOR_CFG,
        )
        lv = KeyLevelV2(
            price=100_000, side="support", state="flipped",
            breakout_stage=1, bounce_count=0,
        )
        score = _calc_flip_confirmation(
            lv, ctx, _DEFAULT_BEHAVIOR_CFG,
            breakout_validity=0.3, retest_quality=0.2,
        )
        assert score <= 0.45


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# false_break_risk（假突破风险）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestFalseBreakRisk:
    def test_fake_break_state_full_risk(self):
        """state == fake_break 由 evaluate_behavior 主流程直接赋 1.0。"""
        levels = [KeyLevelV2(
            price=100_000, side="support", state="fake_break",
            state_ts=int(time.time()) - 60,
        )]
        snap = _snapshot(levels)
        evaluate_behavior(snap, candles_15m=_candles())
        assert snap.levels[0].behavior.false_break_risk == 1.0

    def test_broken_with_low_validity_high_risk(self):
        """broken + breakout_validity 极低 → false_break_risk 高。"""
        ctx = _prepare_context(
            candles_15m=_candles(), candles_1h=None, cvd=None,
            oi_change_pct_1h=0.0, taker_buy_vol=0, taker_sell_vol=0,
            atr=500, cfg=_DEFAULT_BEHAVIOR_CFG,
        )
        lv = KeyLevelV2(price=100_000, side="support", state="broken",
                        state_ts=int(time.time()) - 60)
        risk = _calc_false_break_risk(lv, ctx, _DEFAULT_BEHAVIOR_CFG, breakout_validity=0.15)
        assert risk >= 0.7


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# behavior_state 派生（关键边界）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestBehaviorStateDerivation:
    def test_fake_break_maps_to_failed_breakout(self):
        lv = KeyLevelV2(price=100_000, side="support", state="fake_break")
        e = BehaviorEval(false_break_risk=1.0)
        assert _derive_behavior_state(lv, e, _DEFAULT_BEHAVIOR_CFG) == "failed_breakout"

    def test_flipped_high_confirmation(self):
        lv = KeyLevelV2(price=100_000, side="support", state="flipped")
        e = BehaviorEval(flip_confirmation=0.8)
        assert _derive_behavior_state(lv, e, _DEFAULT_BEHAVIOR_CFG) == "confirmed_flip"

    def test_flipped_low_confirmation(self):
        lv = KeyLevelV2(price=100_000, side="support", state="flipped")
        e = BehaviorEval(flip_confirmation=0.25)
        assert _derive_behavior_state(lv, e, _DEFAULT_BEHAVIOR_CFG) == "wait_for_second_test"

    def test_broken_capitulation_priority_over_breakdown(self):
        """capitulation_bottom_score 高时优先级 > heavy_volume_breakdown。"""
        lv = KeyLevelV2(price=100_000, side="support", state="broken")
        e = BehaviorEval(
            capitulation_bottom_score=0.7,
            selloff_continuation_risk=0.7,
        )
        assert _derive_behavior_state(lv, e, _DEFAULT_BEHAVIOR_CFG) == "capitulation_flush"

    def test_broken_failed_takes_precedence_over_true_breakout(self):
        """false_break_risk 高时优先 failed_breakout。"""
        lv = KeyLevelV2(price=100_000, side="support", state="broken")
        e = BehaviorEval(
            breakout_validity=0.7,
            false_break_risk=0.7,
        )
        assert _derive_behavior_state(lv, e, _DEFAULT_BEHAVIOR_CFG) == "failed_breakout"

    def test_broken_high_validity_true_breakout(self):
        lv = KeyLevelV2(price=100_000, side="resistance", state="broken")
        e = BehaviorEval(breakout_validity=0.8, false_break_risk=0.1)
        assert _derive_behavior_state(lv, e, _DEFAULT_BEHAVIOR_CFG) == "true_breakout"

    def test_bounced_high_quality(self):
        lv = KeyLevelV2(price=100_000, side="support", state="bounced")
        e = BehaviorEval(retest_quality=0.7)
        assert _derive_behavior_state(lv, e, _DEFAULT_BEHAVIOR_CFG) == "healthy_retest"

    def test_testing_high_validity_pending_breakout(self):
        lv = KeyLevelV2(price=100_000, side="support", state="testing")
        e = BehaviorEval(breakout_validity=0.65)
        assert _derive_behavior_state(lv, e, _DEFAULT_BEHAVIOR_CFG) == "pending_breakout"

    def test_idle_returns_pending(self):
        lv = KeyLevelV2(price=100_000, side="support", state="idle")
        e = BehaviorEval()
        assert _derive_behavior_state(lv, e, _DEFAULT_BEHAVIOR_CFG) == "pending"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# state_confidence 与 chips
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestStateConfidenceAndChips:
    def test_chips_max_count(self):
        lv = KeyLevelV2(
            price=100_000, side="support", state="broken",
            state_ts=int(time.time()) - 60,
            cascade_risk=0.8, vacuum_gap_pct=2.5,
        )
        snap = _snapshot([lv])
        evaluate_behavior(snap, candles_15m=_candles(last_vol=300))
        chips = snap.levels[0].behavior.explain_chips
        assert len(chips) <= 4
        assert all(isinstance(c, str) for c in chips)

    def test_state_confidence_for_broken(self):
        lv = KeyLevelV2(
            price=100_000, side="resistance", state="broken",
            state_ts=int(time.time()) - 60, breakout_stage=3,
        )
        snap = _snapshot([lv])
        evaluate_behavior(
            snap, candles_15m=_candles(last_vol=200),
            taker_buy_vol=80, taker_sell_vol=20,
        )
        b = snap.levels[0].behavior
        # state == broken → state_confidence == breakout_validity
        assert abs(b.state_confidence - b.breakout_validity) < 1e-6

    def test_state_confidence_for_fake_break_inverted(self):
        lv = KeyLevelV2(
            price=100_000, side="support", state="fake_break",
            state_ts=int(time.time()) - 60,
        )
        snap = _snapshot([lv])
        evaluate_behavior(snap, candles_15m=_candles())
        b = snap.levels[0].behavior
        # fake_break → state_confidence = 1 - false_break_risk = 0
        assert b.false_break_risk == 1.0
        assert b.state_confidence == 0.0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 工具函数
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestClamp01:
    def test_clamp_negative(self):
        assert _clamp01(-0.5) == 0.0

    def test_clamp_over_one(self):
        assert _clamp01(1.5) == 1.0

    def test_clamp_nan(self):
        assert _clamp01(float("nan")) == 0.0

    def test_clamp_normal(self):
        assert _clamp01(0.42) == 0.42


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 与 run_tracker_v2 集成回归
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# M2.5 · V2 双轨增强函数（影子字段）测试
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestBounceQualityV2:
    def _ctx(self, candles, atr=500):
        return _prepare_context(
            candles_15m=candles, candles_1h=None, cvd=None,
            oi_change_pct_1h=0.0, taker_buy_vol=0, taker_sell_vol=0,
            atr=atr, cfg=_DEFAULT_BEHAVIOR_CFG,
        )

    def test_zero_when_not_bounced(self):
        """非 bounced state → 严格返回 0.0（不污染影子）。"""
        for state in ("idle", "approaching", "testing", "broken", "flipped", "swept"):
            lv = KeyLevelV2(price=100_000, side="support", state=state)
            score = compute_bounce_quality_v2(lv, self._ctx(_candles()), _DEFAULT_BEHAVIOR_CFG)
            assert score == 0.0, f"state={state} 应该返回 0.0，得到 {score}"

    def test_high_quality_proactive(self):
        """支撑反弹 + 阳线 + 高 z-score + 大实体 → 高分。"""
        # 历史均量 100，最后一根放量到 300（z-score 大），阳线
        candles = _candles(n=30, base=100_000, base_vol=100,
                           last_open=99_900, last_close=100_300, last_high=100_350,
                           last_low=99_900, last_vol=300)
        ctx = self._ctx(candles)
        lv = KeyLevelV2(price=100_000, side="support", state="bounced")
        score = compute_bounce_quality_v2(lv, ctx, _DEFAULT_BEHAVIOR_CFG)
        assert score >= 0.65

    def test_low_quality_passive(self):
        """支撑反弹 + 缩量 → 低分。"""
        candles = _candles(n=30, base=100_000, base_vol=200,
                           last_open=99_950, last_close=100_050, last_high=100_080,
                           last_low=99_940, last_vol=60)  # 0.3x
        ctx = self._ctx(candles)
        lv = KeyLevelV2(price=100_000, side="support", state="bounced")
        score = compute_bounce_quality_v2(lv, ctx, _DEFAULT_BEHAVIOR_CFG)
        assert score <= 0.40

    def test_wrong_direction_returns_zero(self):
        """支撑反弹但收阴线 → 方向不对，返回 0.0。"""
        candles = _candles(n=30, base=100_000, base_vol=100,
                           last_open=100_100, last_close=99_950, last_vol=300)
        ctx = self._ctx(candles)
        lv = KeyLevelV2(price=100_000, side="support", state="bounced")
        score = compute_bounce_quality_v2(lv, ctx, _DEFAULT_BEHAVIOR_CFG)
        assert score == 0.0


class TestBreakoutStageV2:
    def _ctx(self, candles):
        return _prepare_context(
            candles_15m=candles, candles_1h=None, cvd=None,
            oi_change_pct_1h=0.0, taker_buy_vol=0, taker_sell_vol=0,
            atr=500, cfg=_DEFAULT_BEHAVIOR_CFG,
        )

    def test_zero_when_not_broken_or_flipped(self):
        for state in ("idle", "approaching", "testing", "bounced", "swept", "fake_break"):
            lv = KeyLevelV2(price=100_000, side="support", state=state,
                            state_ts=int(time.time()) - 100)
            stage = compute_breakout_stage_v2(
                lv, atr=500, ctx=self._ctx(_candles()),
                cfg=_DEFAULT_BEHAVIOR_CFG, now=int(time.time()),
            )
            assert stage == 0

    def test_1d_timeframe_extends_window(self):
        """1D 关键位破位 5 小时（旧固定窗已过期）→ V2 仍返回 stage 1。"""
        now = int(time.time())
        lv = KeyLevelV2(
            price=100_000, side="support", state="broken",
            state_ts=now - 5 * 3600,  # 5 小时前
            timeframe="1D",
        )
        stage = compute_breakout_stage_v2(
            lv, atr=500, ctx=self._ctx(_candles()),
            cfg=_DEFAULT_BEHAVIOR_CFG, now=now,
        )
        # 旧固定窗 stage1=900s（15min）→ 5h 早过期
        # 新自适应窗 stage1=900*24=21600s（6h）→ 5h 内仍是 stage 1
        assert stage == 1

    def test_15m_timeframe_compresses_window(self):
        """15m 关键位破位 5 分钟 → V2 缩放后仍是 stage 1。"""
        now = int(time.time())
        lv = KeyLevelV2(
            price=100_000, side="support", state="broken",
            state_ts=now - 5 * 60,
            timeframe="15m",
        )
        stage = compute_breakout_stage_v2(
            lv, atr=500, ctx=self._ctx(_candles()),
            cfg=_DEFAULT_BEHAVIOR_CFG, now=now,
        )
        # 15m: stage1=900*0.25=225s，5min=300s → 已超 stage 1，进入 stage 2 判定
        # 但 candles 没有触达 level 的回踩 → 返回 stage 1
        assert stage in (1, 2)


class TestFakeBreakStrength:
    def _ctx(self, candles):
        return _prepare_context(
            candles_15m=candles, candles_1h=None, cvd=None,
            oi_change_pct_1h=0.0, taker_buy_vol=0, taker_sell_vol=0,
            atr=500, cfg=_DEFAULT_BEHAVIOR_CFG,
        )

    def test_zero_when_not_relevant_state(self):
        for state in ("idle", "bounced", "flipped", "swept", "testing"):
            lv = KeyLevelV2(price=100_000, side="support", state=state)
            assert assess_fake_break_strength(lv, self._ctx(_candles())) == 0.0

    def test_long_lower_wick_high_strength(self):
        """支撑 fake_break + 长下影回收 → 高分。"""
        # 倒数第二根（已收盘）：长下影 + close ≥ price
        candles = _candles(n=30, base=100_000, base_vol=100)
        candles[-2] = CandleData(
            coin="BTC", ts=candles[-2].ts,
            o=100_050, h=100_100, l=99_300, c=100_080, vol=200,
        )
        # 当前根的 last_wick_lower_ratio 由 prepare_context 算出
        # 我们手动构造让 prepare_context 看到长下影
        candles[-1] = CandleData(
            coin="BTC", ts=candles[-1].ts,
            o=100_080, h=100_120, l=99_400, c=100_100, vol=150,
        )
        lv = KeyLevelV2(price=100_000, side="support", state="fake_break",
                        fake_break_count=1)
        score = assess_fake_break_strength(lv, self._ctx(candles))
        assert score >= 0.55

    def test_close_below_price_returns_zero(self):
        """支撑 fake_break 但 last_closed.close < price → 0（未真正回收）。"""
        candles = _candles(n=30, base=100_000)
        candles[-2] = CandleData(
            coin="BTC", ts=candles[-2].ts,
            o=99_900, h=99_950, l=99_500, c=99_700, vol=100,
        )
        lv = KeyLevelV2(price=100_000, side="support", state="fake_break")
        score = assess_fake_break_strength(lv, self._ctx(candles))
        assert score == 0.0


class TestDynamicBreakDepthPct:
    def test_returns_max_of_cfg_and_atr_pct(self):
        # ATR=500, price=100_000 → ATR%=0.5%, k=0.3 → 0.15%
        # cfg=0.3% → max(0.3, 0.15)=0.3
        depth = compute_dynamic_break_depth_pct(atr=500, price=100_000, cfg=_DEFAULT_BEHAVIOR_CFG)
        assert abs(depth - 0.3) < 1e-6

    def test_high_volatility_uses_atr(self):
        # ATR=2000, price=100_000 → ATR%=2%, k=0.3 → 0.6%
        # cfg=0.3% → max(0.3, 0.6)=0.6
        depth = compute_dynamic_break_depth_pct(atr=2000, price=100_000, cfg=_DEFAULT_BEHAVIOR_CFG)
        assert abs(depth - 0.6) < 1e-6

    def test_zero_inputs_return_cfg(self):
        depth = compute_dynamic_break_depth_pct(atr=0, price=100_000, cfg=_DEFAULT_BEHAVIOR_CFG)
        assert abs(depth - 0.3) < 1e-6


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# M2.5 · 冲突预警（state vs behavior 不一致）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestContradictionDetection:
    def test_broken_with_low_validity_and_high_false_risk(self):
        lv = KeyLevelV2(price=100_000, side="support", state="broken")
        e = BehaviorEval(breakout_validity=0.20, false_break_risk=0.75)
        out = _detect_contradictions(lv, e, _DEFAULT_BEHAVIOR_CFG)
        assert any("可能假破" in c for c in out)

    def test_broken_low_validity_only(self):
        lv = KeyLevelV2(price=100_000, side="resistance", state="broken")
        e = BehaviorEval(breakout_validity=0.15, false_break_risk=0.40)
        out = _detect_contradictions(lv, e, _DEFAULT_BEHAVIOR_CFG)
        assert any("假突破" in c for c in out)

    def test_bounced_with_unhealthy_retest(self):
        lv = KeyLevelV2(price=100_000, side="support", state="bounced")
        e = BehaviorEval(retest_quality=0.20)
        out = _detect_contradictions(lv, e, _DEFAULT_BEHAVIOR_CFG)
        assert any("不健康" in c for c in out)

    def test_flipped_unconfirmed(self):
        lv = KeyLevelV2(price=100_000, side="support", state="flipped")
        e = BehaviorEval(flip_confirmation=0.20)
        out = _detect_contradictions(lv, e, _DEFAULT_BEHAVIOR_CFG)
        assert any("市场未确认" in c for c in out)

    def test_v1_v2_divergence_proactive_to_passive(self):
        """V1 主动 vs V2 被动（背离 → 提示）。"""
        lv = KeyLevelV2(price=100_000, side="support", state="bounced",
                        bounce_quality="proactive")
        e = BehaviorEval(bounce_quality_enhanced=0.20, retest_quality=0.50)
        out = _detect_contradictions(lv, e, _DEFAULT_BEHAVIOR_CFG)
        assert any("V1 标记主动" in c for c in out)

    def test_no_contradiction_when_aligned(self):
        """state == bounced + 高 retest_quality + V1 V2 一致 → 无冲突。"""
        lv = KeyLevelV2(price=100_000, side="support", state="bounced",
                        bounce_quality="proactive")
        e = BehaviorEval(retest_quality=0.75, bounce_quality_enhanced=0.80)
        out = _detect_contradictions(lv, e, _DEFAULT_BEHAVIOR_CFG)
        assert out == []


class TestEvalWritesV2Fields:
    def test_evaluate_behavior_populates_v2_shadow_fields(self):
        """主入口必须写入所有 V2 影子字段（即使值为 0 也是显式赋值）。"""
        lv = KeyLevelV2(price=100_000, side="support", state="bounced",
                        state_ts=int(time.time()) - 60,
                        bounce_quality="proactive", breakout_stage=0)
        snap = _snapshot([lv])
        evaluate_behavior(snap, candles_15m=_candles(last_vol=200))
        b = snap.levels[0].behavior
        assert b is not None
        # V2 影子字段都是浮点数 / 整数，不是 None
        assert isinstance(b.bounce_quality_enhanced, float)
        assert isinstance(b.breakout_stage_enhanced, int)
        assert isinstance(b.fake_break_strength, float)
        assert isinstance(b.dynamic_break_depth_pct, float)
        # contradiction_with_state 必须是 list（即使空）
        assert isinstance(b.contradiction_with_state, list)


class TestIntegrationWithTracker:
    def test_run_tracker_v2_writes_behavior(self):
        """run_tracker_v2 末尾应自动调用 evaluate_behavior 并写入每个 level。"""
        from processors.key_level_tracker_v2 import run_tracker_v2

        lv = KeyLevelV2(price=100_000, side="support", state="testing",
                        state_ts=int(time.time()) - 100, test_count=1)
        snap = _snapshot([lv], price=100_100, atr=500)
        snap = run_tracker_v2(snap, liq_map=None, sweep_events_1h=[])
        assert snap.levels[0].behavior is not None
        # 状态机本来要把它变成 bounced
        assert snap.levels[0].state in ("bounced", "testing")
        # behavior_state 至少是合法标签之一
        valid_states = {
            "pending", "pending_breakout", "true_breakout",
            "healthy_retest", "failed_breakout", "heavy_volume_breakdown",
            "capitulation_flush", "confirmed_flip", "wait_for_second_test",
        }
        assert snap.levels[0].behavior.behavior_state in valid_states

    def test_tracker_does_not_break_existing_signal_flow(self):
        """关键回归：行为层接入后，原 signal 链路完全不变。"""
        from processors.key_level_tracker_v2 import run_tracker_v2

        lv = KeyLevelV2(price=100_000, side="support", state="bounced",
                        state_ts=int(time.time()) - 60, strength_tier="A")
        res = KeyLevelV2(price=102_000, side="resistance")
        snap = _snapshot([lv, res], price=100_500, atr=500)
        snap = run_tracker_v2(snap, liq_map=None, sweep_events_1h=[])
        assert isinstance(snap.signals, list)
        assert snap.active_count >= 1


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# M3.1 新增：互斥校准 + 元信息字段 + 健康监控
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

from processors.key_level_behavior_eval import (  # noqa: E402
    BEHAVIOR_EVAL_VERSION,
    _calibrate_mutual_exclusion,
    evaluate_behavior,
    get_health_stats,
    reset_health_stats,
)


class TestMutualExclusionCalibration:
    """selloff_continuation_risk vs capitulation_bottom_score 互斥校准。"""

    def test_no_conflict_no_change(self):
        """两个分都低于阈值 → 不介入。"""
        beh = BehaviorEval(
            selloff_continuation_risk=0.30,
            capitulation_bottom_score=0.20,
        )
        _calibrate_mutual_exclusion(beh)
        assert beh.selloff_continuation_risk == 0.30
        assert beh.capitulation_bottom_score == 0.20
        assert beh.explain_chips == []

    def test_selloff_wins_over_capitulation(self):
        """selloff 0.80 > capitulation 0.60 → capitulation 衰减、加 chip。"""
        beh = BehaviorEval(
            selloff_continuation_risk=0.80,
            capitulation_bottom_score=0.60,
        )
        _calibrate_mutual_exclusion(beh)
        assert beh.selloff_continuation_risk == 0.80  # 强者不动
        # 弱者按 1 - 0.80*0.4 = 0.68 → 0.60*0.68 = 0.408
        assert abs(beh.capitulation_bottom_score - 0.408) < 1e-3
        assert any("卖压延续主导" in c for c in beh.explain_chips)

    def test_capitulation_wins_over_selloff(self):
        beh = BehaviorEval(
            selloff_continuation_risk=0.55,
            capitulation_bottom_score=0.85,
        )
        _calibrate_mutual_exclusion(beh)
        assert beh.capitulation_bottom_score == 0.85
        # selloff *= 1 - 0.85*0.4 = 0.66 → 0.55*0.66 = 0.363
        assert abs(beh.selloff_continuation_risk - 0.363) < 1e-3
        assert any("恐慌见底主导" in c for c in beh.explain_chips)

    def test_one_below_threshold_no_change(self):
        """一个低于 0.50 → 不视为冲突，不校准。"""
        beh = BehaviorEval(
            selloff_continuation_risk=0.85,
            capitulation_bottom_score=0.40,
        )
        _calibrate_mutual_exclusion(beh)
        assert beh.capitulation_bottom_score == 0.40
        assert beh.explain_chips == []


class TestBehaviorEvalMetaFields:
    """M3.1：BehaviorEval 元信息字段（available / version / quality / missing / error）。"""

    def test_default_values_for_new_eval(self):
        """新建空 BehaviorEval → 默认值符合预期。"""
        beh = BehaviorEval()
        assert beh.behavior_eval_available is True
        assert beh.behavior_eval_version == "1.0"
        assert beh.input_quality == "ok"
        assert beh.missing_inputs == []
        assert beh.evaluator_error == ""

    def test_unavailable_when_atr_missing(self):
        """atr<=0 → behavior_eval_available=False，missing 列出 'atr'。"""
        reset_health_stats()
        lv = KeyLevelV2(price=100_000, side="support", state="bounced")
        snap = KeyLevelSnapshotV2(ts=int(time.time()), current_price=100_500,
                                   atr=0.0, levels=[lv])
        evaluate_behavior(snap, candles_15m=_candles(), candles_1h=_candles(),
                          cvd=None)
        b = lv.behavior
        assert b is not None
        assert b.behavior_eval_available is False
        assert "atr" in b.missing_inputs
        assert b.input_quality == "missing"
        assert b.behavior_eval_version == BEHAVIOR_EVAL_VERSION

    def test_partial_quality_when_cvd_missing(self):
        """candles_15m 充足但缺 cvd → input_quality=partial，仍可评估成功。"""
        reset_health_stats()
        lv = KeyLevelV2(price=100_000, side="support", state="bounced",
                        state_ts=int(time.time()) - 60)
        snap = _snapshot([lv], price=100_500, atr=500)
        evaluate_behavior(snap, candles_15m=_candles(), candles_1h=_candles(),
                          cvd=None)
        b = lv.behavior
        assert b is not None
        assert b.behavior_eval_available is True  # 仍成功
        assert b.input_quality == "partial"
        assert "cvd" in b.missing_inputs


class TestBehaviorHealthStats:
    """M3.1：模块级健康监控。"""

    def test_reset_clears_counters(self):
        reset_health_stats()
        s = get_health_stats()
        assert s["total_calls"] == 0
        assert s["level_eval_total"] == 0
        assert s["error_rate"] == 0.0
        assert s["module_version"] == BEHAVIOR_EVAL_VERSION

    def test_success_increments_counters(self):
        reset_health_stats()
        lv = KeyLevelV2(price=100_000, side="support", state="bounced",
                        state_ts=int(time.time()) - 60)
        snap = _snapshot([lv], price=100_500, atr=500)
        evaluate_behavior(snap, candles_15m=_candles(), candles_1h=_candles())
        s = get_health_stats()
        assert s["total_calls"] == 1
        assert s["success_calls"] == 1
        assert s["level_eval_total"] == 1
        assert s["level_eval_success"] == 1
        assert s["level_eval_error"] == 0
        assert s["avg_latency_ms"] >= 0.0  # 可能极小但非负

    def test_input_skip_counts_separately(self):
        reset_health_stats()
        lv = KeyLevelV2(price=100_000, side="support", state="bounced")
        snap = KeyLevelSnapshotV2(ts=int(time.time()), current_price=100_500,
                                   atr=0.0, levels=[lv])
        evaluate_behavior(snap, candles_15m=_candles())
        s = get_health_stats()
        assert s["input_skip_calls"] == 1
        # input_skip 阶段不进入 level_eval_total（atr<=0 直接跳过 level 循环）
        assert s["level_eval_total"] == 0
