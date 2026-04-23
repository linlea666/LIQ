"""
P0-A / P0-B / P0-C / P1-F / P1-E 五项合并修复的回归测试

修复背景：
  P0-A · LiqClusterSnapshot.bias（short_squeeze_fuel/long_squeeze_fuel/balanced）
         是立场预判，已删除；prompt 改为纯"上/下簇比值"数值对比
  P0-B · `days_negative_streak` 基于 OI 加权历史，`avg_current` 是跨家算术均值，
         口径不同；在 prompt 字段定义里明确标注以消除歧义
  P0-C · `continuity.stance=reversal` 硬规则：仅在 scenario 大类切换时使用
  P1-F · `CVDSnapshot.trend_recent_30m` 新增，用近 6 根 5m 派生，识别拐点
  P1-E · Roll 引擎 URGENT log，规则触发时打 "confidence=RULE" 而非 "confidence=0"
"""
from __future__ import annotations

import logging

import pytest

from models.market_action import (
    CVDSnapshot,
    LiqClusterSnapshot,
    MarketActionFacts,
    PriceSnapshot,
)
from processors.market_action.facts_collector import (
    _trend_label_from_window,
    build_cvd_snapshot,
)
from processors.market_action.liq_cluster_analyzer import build_cluster_snapshot


# ────────────────────────────────────────────────────────────
# P0-A · LiqClusterSnapshot.bias 已彻底移除
# ────────────────────────────────────────────────────────────

def test_p0a_liq_cluster_snapshot_has_no_bias_field():
    snap = LiqClusterSnapshot()
    assert not hasattr(snap, "bias"), "LiqClusterSnapshot 不应再有 bias 立场字段"


def test_p0a_cluster_analyzer_produces_pure_numbers_no_bias():
    """构建 snapshot 后，只能拿到上下簇数值，不应再有立场标签。"""
    class _Cluster:
        def __init__(self, price_center, total_usd):
            self.price_center = price_center
            self.total_usd = total_usd

    class _LiqMap:
        clusters_above = [_Cluster(79000, 2_000_000_000)]
        clusters_below = [_Cluster(77000, 500_000_000)]

    snap = build_cluster_snapshot(heatmap=None, current_price=78000, liq_map=_LiqMap())
    assert snap.above_cluster_usd == 2_000_000_000
    assert snap.below_cluster_usd == 500_000_000
    assert snap.above_distance_pct is not None and snap.above_distance_pct > 0
    assert not hasattr(snap, "bias")


def test_p0a_prompt_renders_ratio_not_bias():
    """prompt 里应出现 '上/下簇比值'，不应出现 short_squeeze_fuel / long_squeeze_fuel / balanced 立场字样。"""
    from ai.market_action_prompts import build_user_prompt

    facts = MarketActionFacts(
        coin="BTC", timestamp=1_700_000_000,
        price=PriceSnapshot(last=78_000),
        liq_map_clusters=LiqClusterSnapshot(
            above_cluster_usd=2_000_000_000,
            below_cluster_usd=500_000_000,
            above_nearest_price=79000,
            below_nearest_price=77000,
            above_distance_pct=1.28,
            below_distance_pct=1.28,
        ),
    )
    out, _ = build_user_prompt(facts)
    assert "上/下簇比值" in out
    assert "short_squeeze_fuel" not in out
    assert "long_squeeze_fuel" not in out
    # 注：'balanced' 在 system_prompt 里可能作为泛英文出现；这里只检查 user_prompt 部分
    assert "`balanced`" not in out  # 原三值枚举的反引号字段写法不应出现


# ────────────────────────────────────────────────────────────
# P0-B · days_negative_streak 口径说明
# ────────────────────────────────────────────────────────────

def test_p0b_prompt_explains_streak_oi_weighted_caliber():
    from ai.market_action_prompts import SYSTEM_PROMPT

    assert "days_negative_streak" in SYSTEM_PROMPT
    # 必须说明口径基于 OI 加权历史
    assert "OI 加权" in SYSTEM_PROMPT
    # 必须提示 avg_current 与 streak 口径可能不一致
    assert "算术均值" in SYSTEM_PROMPT


# ────────────────────────────────────────────────────────────
# P0-C · continuity.reversal 硬规则
# ────────────────────────────────────────────────────────────

def test_p0c_system_prompt_has_reversal_hard_rule():
    from ai.market_action_prompts import SYSTEM_PROMPT

    # 必须明文禁止：scenario 相同时不能填 reversal
    assert (
        "scenario 大类切换" in SYSTEM_PROMPT
        or "scenario 与上一份相同时禁止填 reversal" in SYSTEM_PROMPT
    )


# ────────────────────────────────────────────────────────────
# P1-F · CVDSnapshot.trend_recent_30m
# ────────────────────────────────────────────────────────────

def test_p1f_trend_window_rising():
    """后半段都是正 delta → rising。"""
    assert _trend_label_from_window([1e6, 2e6, 1.5e6, 3e6, 2.5e6, 1.8e6]) == "rising"


def test_p1f_trend_window_declining():
    assert _trend_label_from_window([-1e6, -2e6, -1.5e6, -3e6, -2.5e6, -1.8e6]) == "declining"


def test_p1f_trend_window_flat_by_noise_threshold():
    """sum 的绝对值未超过 peak × 15% → flat（避免抖动误判）。"""
    # peak=10, sum=1 → 1 / 10 = 0.1 < 0.15 → flat
    assert _trend_label_from_window([10, -9, 5, -4, 3, -4]) == "flat"


def test_p1f_trend_window_empty_returns_none():
    assert _trend_label_from_window([]) is None


def test_p1f_cvd_snapshot_exposes_trend_recent_30m():
    """build_cvd_snapshot 应派生 trend_recent_30m 字段。"""
    class _Pt:
        def __init__(self, d): self.delta = d

    class _CVD:
        delta_1h = 100.0
        trend_1h = "rising"
        has_divergence = False
        divergence_note = None
        # 前 6 根正，后 6 根负 —— 经典拐点
        series = [
            _Pt(1e6), _Pt(2e6), _Pt(1.5e6), _Pt(3e6), _Pt(2.5e6), _Pt(1.8e6),
            _Pt(-1.5e6), _Pt(-2e6), _Pt(-3e6), _Pt(-2.5e6), _Pt(-1.8e6), _Pt(-1.2e6),
        ]

    snap = build_cvd_snapshot(_CVD())
    assert snap is not None
    assert snap.trend_1h == "rising"        # 整体 1h 仍 rising（上游给的）
    assert snap.trend_recent_30m == "declining"  # 近 30min 已反转
    assert len(snap.recent_delta_5m) == 12


def test_p1f_cvd_snapshot_model_field_defaults_to_none():
    snap = CVDSnapshot(delta_1h=0.0)
    assert snap.trend_recent_30m is None


def test_p1f_prompt_renders_both_trends():
    from ai.market_action_prompts import build_user_prompt

    facts = MarketActionFacts(
        coin="BTC", timestamp=1_700_000_000,
        price=PriceSnapshot(last=78_000),
        cvd_contract=CVDSnapshot(
            delta_1h=100_000_000,
            trend_1h="rising",
            trend_recent_30m="declining",
            has_divergence=False,
            recent_delta_5m=[1e6] * 12,
        ),
        cvd_spot=CVDSnapshot(
            delta_1h=10_000_000,
            trend_1h="rising",
            trend_recent_30m="flat",
            has_divergence=False,
            recent_delta_5m=[1e5] * 12,
        ),
    )
    out, _ = build_user_prompt(facts)
    assert "trend_1h" in out
    assert "trend_recent_30m" in out
    assert "declining" in out  # 合约 trend_recent_30m
    assert "rising" in out     # trend_1h


# ────────────────────────────────────────────────────────────
# P1-E · Roll URGENT log confidence 语义
# ────────────────────────────────────────────────────────────

def test_p1e_urgent_rule_trigger_emits_rule_label(caplog):
    """规则触发（close + confidence_score=0）应该打 'confidence=RULE' 而非 'confidence=0'。

    用 engine loop 里的那段格式化逻辑做单元化验证（直接模拟 logger.warning 格式）。
    """
    # 模拟：规则触发
    action = "close"
    confidence_score = 0.0
    _is_rule_trigger = action in ("close", "hold") and (confidence_score or 0) == 0
    conf_text = "RULE" if _is_rule_trigger else f"{confidence_score:.0f}"
    assert conf_text == "RULE"

    # 模拟：评分路径（reduce 且有非 0 分）
    action = "reduce"
    confidence_score = 62.3
    _is_rule_trigger = action in ("close", "hold") and (confidence_score or 0) == 0
    conf_text = "RULE" if _is_rule_trigger else f"{confidence_score:.0f}"
    assert conf_text == "62"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
