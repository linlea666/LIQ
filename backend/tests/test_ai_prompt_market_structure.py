"""Commit 6 · AI prompt 市场结构集成回归测试

覆盖：
1. System prompt 包含 3 条新铁律（结构优先 / 价位血统 / 心理位禁令）
2. System prompt 的 L3 分级表提到 "1h 市场结构"
3. User prompt 在 §9i 正确渲染 market_structure 数据
4. User prompt 在 market_structure 缺失时不输出 §9i（降级安全）
5. §八自检的必选子项文本存在
6. 邮件 subject 在 ms_alignment=aligned/conflict 时加标签

这些测试是"幻觉防护硬门禁"的一部分 —— 如果 prompt 未来被人误改导致约束丢失，CI 会立即红。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ai.prompts import build_system_prompt, build_user_prompt
from notifications.email_alert import _build_subject
from notifications.signal_monitor import AlertEvent


# ─────────────────────────────────────────────────────────────
# System prompt 铁律 / 分级表检查
# ─────────────────────────────────────────────────────────────

def test_system_prompt_contains_structure_primacy_rule():
    """结构优先铁律必须存在 —— 这是 Commit 6 的核心约束。"""
    sp = build_system_prompt()
    assert "结构优先铁律" in sp
    assert "ms_bias" in sp
    assert "long_only" in sp or "short_only" in sp


def test_system_prompt_contains_price_lineage_rule():
    """价位血统铁律：每个价位必须能从 §9 找到来源。"""
    sp = build_system_prompt()
    assert "价位血统铁律" in sp
    assert "⚡AI推断" in sp
    # 数据来源示例须有（防止 prompt 改写后丢失具体指引）
    assert "清算簇" in sp
    assert "关键位" in sp


def test_system_prompt_contains_psychological_level_ban():
    """心理位禁令：$77,000 / $80,000 等不得凭空引用。"""
    sp = build_system_prompt()
    assert "心理位禁令" in sp
    assert "$77,000" in sp or "$80,000" in sp


def test_system_prompt_l3_mentions_market_structure():
    """L3 分级表必须明确提升 1h 市场结构到最高优先级。"""
    sp = build_system_prompt()
    assert "1h 市场结构" in sp
    assert "BOS/CHoCH" in sp


def test_system_prompt_key_level_bounce_quality_rules():
    """关键位段必须覆盖 bounce_quality / breakout_stage 消费规则。"""
    sp = build_system_prompt()
    assert "bounce_quality" in sp
    assert "proactive" in sp
    assert "passive" in sp
    assert "breakout_stage" in sp


def test_system_prompt_section_seven_word_floor():
    """§七场景推演的字数底线必须存在，防止 LLM 末尾敷衍。"""
    sp = build_system_prompt()
    assert "每个场景至少 30 字" in sp


def test_system_prompt_section_eight_mandatory_items():
    """§八自检必选子项：方向判断层级 + 结构对齐度。"""
    sp = build_system_prompt()
    assert "本次方向判断核心依据层级" in sp
    assert "1h 结构对齐度" in sp


# ─────────────────────────────────────────────────────────────
# User prompt §9i 渲染检查
# ─────────────────────────────────────────────────────────────

def _minimal_snapshot(extra: dict | None = None) -> dict:
    """构造最精简的 snapshot dict，只填 §9i 测试所需字段。"""
    snap = {
        "coin": "BTC",
        "price": 77_000.0,
        "high_24h": 78_000.0,
        "low_24h": 76_000.0,
    }
    if extra:
        snap.update(extra)
    return snap


def test_user_prompt_renders_section_9i_when_structure_present():
    """market_structure 有数据时必须出现 §9i 标题和核心字段。"""
    snap = _minimal_snapshot({
        "market_structure": {
            "direction": "bullish",
            "last_event": "BOS_up",
            "event_ts": 1_700_000_000,
            "operate_bias": "long_only",
            "confidence": 0.9,
            "structure_high": 77_400.0,
            "structure_low": 76_200.0,
            "summary": "1h 上升结构",
            "swing_highs": [{"ts": 1_699_990_000, "price": 77_400.0, "kind": "high"}],
            "swing_lows": [{"ts": 1_699_980_000, "price": 76_200.0, "kind": "low"}],
        }
    })
    up = build_user_prompt(snap)

    assert "### 9i." in up
    assert "1h 市场结构" in up
    assert "上升结构" in up
    assert "BOS↑" in up
    assert "仅顺势做多" in up
    assert "77,400" in up  # structure_high
    assert "76,200" in up  # structure_low


def test_user_prompt_skips_section_9i_when_structure_missing():
    """未提供 market_structure 时 §9i 整段不应出现，保持降级兼容。"""
    snap = _minimal_snapshot()
    up = build_user_prompt(snap)
    assert "### 9i." not in up


def test_user_prompt_renders_bos_down_correctly():
    """下降结构 + BOS_down 的场景渲染正确。"""
    snap = _minimal_snapshot({
        "market_structure": {
            "direction": "bearish",
            "last_event": "BOS_down",
            "operate_bias": "short_only",
            "confidence": 0.75,
            "structure_high": 78_500.0,
            "structure_low": 76_500.0,
            "swing_highs": [],
            "swing_lows": [],
        }
    })
    up = build_user_prompt(snap)
    assert "下降结构" in up
    assert "BOS↓" in up
    assert "仅顺势做空" in up


def test_user_prompt_high_confidence_appends_reverse_rule():
    """置信度≥60% 且方向明确时，附带逆势开仓的 L1 证据铁律提醒。"""
    snap = _minimal_snapshot({
        "market_structure": {
            "direction": "bullish",
            "last_event": "BOS_up",
            "operate_bias": "long_only",
            "confidence": 0.8,
            "structure_high": 77_400.0,
            "structure_low": 76_200.0,
            "swing_highs": [],
            "swing_lows": [],
        }
    })
    up = build_user_prompt(snap)
    assert "铁律提醒" in up
    assert "L1 级实盘资金背驰" in up


def test_user_prompt_low_confidence_skips_reverse_rule():
    """置信度 < 60% 时不附加铁律提醒，避免 AI 对不确定结构过度遵从。"""
    snap = _minimal_snapshot({
        "market_structure": {
            "direction": "bullish",
            "last_event": "BOS_up",
            "operate_bias": "long_only",
            "confidence": 0.4,  # 低置信度
            "structure_high": 77_400.0,
            "structure_low": 76_200.0,
            "swing_highs": [],
            "swing_lows": [],
        }
    })
    up = build_user_prompt(snap)
    assert "铁律提醒" not in up


def test_user_prompt_ranging_direction_not_forcing_reverse_rule():
    """震荡结构不应触发'必须顺势'铁律，即使置信度高。"""
    snap = _minimal_snapshot({
        "market_structure": {
            "direction": "ranging",
            "last_event": "",
            "operate_bias": "both_ok",
            "confidence": 0.9,
            "structure_high": 78_000.0,
            "structure_low": 76_000.0,
            "swing_highs": [],
            "swing_lows": [],
        }
    })
    up = build_user_prompt(snap)
    assert "铁律提醒" not in up
    assert "震荡结构" in up


# ─────────────────────────────────────────────────────────────
# 邮件 subject metadata 检查
# ─────────────────────────────────────────────────────────────

def _event(ms_alignment: str = "", ms_direction: str = "bullish") -> AlertEvent:
    return AlertEvent(
        coin="BTC",
        source="key_level",
        direction="long",
        signal_tier="A",
        price=77_000.0,
        action="snipe_long",
        ms_direction=ms_direction,
        ms_alignment=ms_alignment,
    )


def test_email_subject_aligned_adds_success_tag():
    subj = _build_subject(_event(ms_alignment="aligned"))
    assert "✓顺势" in subj
    assert "A级做多" in subj


def test_email_subject_conflict_adds_warning_tag():
    subj = _build_subject(_event(ms_alignment="conflict"))
    assert "⚠逆势" in subj


def test_email_subject_no_alignment_no_extra_tag():
    subj = _build_subject(_event(ms_alignment=""))
    # 既无 ✓ 也无 ⚠
    assert "顺势" not in subj
    assert "逆势" not in subj
    # 但基础信息仍完整
    assert "A级做多" in subj
    assert "BTC" in subj


def test_email_subject_neutral_alignment_no_extra_tag():
    subj = _build_subject(_event(ms_alignment="neutral"))
    assert "顺势" not in subj
    assert "逆势" not in subj
