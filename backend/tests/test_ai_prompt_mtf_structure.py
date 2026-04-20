"""§9i MTF 扩展专项单测

覆盖：
1. 三 TF 同向共振 → "三周期同向共振" + "高胜率窗口"
2. 日周同向 + 1h 相反 → "日周X头 vs 1h Y头" + "回调/反弹"
3. 日周冲突 (1w ≠ 1d) → "周线与日线方向相反" + "结构转换期"
4. 仅 1h 可用（降级兼容原有路径）→ ℹ MTF 提示
5. `_render_ms_block` 渲染单 TF 正确
6. `_mtf_alignment_line` 输入 4 种组合返回符合预期
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ai.prompts import _mtf_alignment_line, _render_ms_block, build_user_prompt


def _ms(direction: str, conf: float = 0.8, last_event: str = "") -> dict:
    return {
        "direction": direction,
        "last_event": last_event,
        "operate_bias": (
            "long_only" if direction == "bullish"
            else "short_only" if direction == "bearish"
            else "both_ok"
        ),
        "confidence": conf,
        "structure_high": 80_000.0,
        "structure_low": 70_000.0,
        "swing_highs": [],
        "swing_lows": [],
    }


def _snap_with_mtf(ms_1w, ms_1d, ms_1h) -> dict:
    s = {"coin": "BTC", "price": 77_000.0, "high_24h": 78_000.0, "low_24h": 76_000.0}
    if ms_1w is not None:
        s["market_structure_1w"] = ms_1w
    if ms_1d is not None:
        s["market_structure_1d"] = ms_1d
    if ms_1h is not None:
        s["market_structure"] = ms_1h
    return s


# ═══════════════════════════════════════════════════════════════
# _mtf_alignment_line 单元测试（纯函数，4 组合）
# ═══════════════════════════════════════════════════════════════

class TestMtfAlignmentLine:
    def test_three_tf_bullish_resonance(self):
        line = _mtf_alignment_line(_ms("bullish"), _ms("bullish"), _ms("bullish"))
        assert line is not None
        assert "三周期同向共振" in line
        assert "高胜率窗口" in line
        assert "全 多" in line

    def test_three_tf_bearish_resonance(self):
        line = _mtf_alignment_line(_ms("bearish"), _ms("bearish"), _ms("bearish"))
        assert line is not None
        assert "三周期同向共振" in line
        assert "全 空" in line

    def test_big_frames_up_1h_down(self):
        line = _mtf_alignment_line(_ms("bullish"), _ms("bullish"), _ms("bearish"))
        assert line is not None
        assert "日周多头 vs 1h 空头" in line
        assert "回调" in line or "反弹" in line

    def test_weekly_daily_conflict(self):
        line = _mtf_alignment_line(_ms("bullish"), _ms("bearish"), _ms("bullish"))
        assert line is not None
        assert "周线与日线方向相反" in line
        assert "结构转换期" in line

    def test_low_confidence_treated_as_unclear(self):
        """任一 TF 置信度 < 0.5 被视为 unclear，不会触发共振判定。"""
        line = _mtf_alignment_line(
            _ms("bullish", conf=0.3), _ms("bullish"), _ms("bullish"),
        )
        assert line is not None
        assert "三周期同向共振" not in line
        assert "未形成明确共振" in line

    def test_all_ranging_unclear(self):
        line = _mtf_alignment_line(
            _ms("ranging"), _ms("ranging"), _ms("ranging"),
        )
        assert line is not None
        assert "未形成明确共振" in line

    def test_only_1h_falls_back_to_unclear(self):
        line = _mtf_alignment_line(None, None, _ms("bullish"))
        assert line is not None
        assert "未形成明确共振" in line

    def test_all_missing_returns_none(self):
        assert _mtf_alignment_line(None, None, None) is None


# ═══════════════════════════════════════════════════════════════
# _render_ms_block 单元测试
# ═══════════════════════════════════════════════════════════════

class TestRenderMsBlock:
    def test_verbose_includes_swing_and_summary(self):
        ms = _ms("bullish", conf=0.9, last_event="BOS_up")
        ms["swing_highs"] = [{"ts": 1, "price": 80000, "kind": "high"}]
        ms["swing_lows"] = [{"ts": 2, "price": 70000, "kind": "low"}]
        ms["summary"] = "1d 上升结构形成"
        out = _render_ms_block("1d", ms, verbose=True)

        body = "\n".join(out)
        assert "[1d]" in body
        assert "上升结构" in body
        assert "BOS↑" in body
        assert "最近 swing" in body
        assert "结构要点: 1d 上升结构形成" in body

    def test_non_verbose_omits_swing(self):
        ms = _ms("bearish", conf=0.7, last_event="BOS_down")
        ms["swing_highs"] = [{"ts": 1, "price": 80000, "kind": "high"}]
        ms["swing_lows"] = [{"ts": 2, "price": 70000, "kind": "low"}]
        ms["summary"] = "周线下行"
        out = _render_ms_block("1w", ms, verbose=False)

        body = "\n".join(out)
        assert "[1w]" in body
        assert "下降结构" in body
        assert "最近 swing" not in body
        assert "结构要点" not in body

    def test_empty_ms_returns_empty_list(self):
        assert _render_ms_block("1h", {}, verbose=True) == []
        assert _render_ms_block("1h", None, verbose=True) == []  # type: ignore[arg-type]


# ═══════════════════════════════════════════════════════════════
# build_user_prompt 级联集成
# ═══════════════════════════════════════════════════════════════

class TestBuildUserPromptMtf:
    def test_renders_three_rows_when_all_present(self):
        snap = _snap_with_mtf(
            _ms("bullish"), _ms("bullish"), _ms("bullish", last_event="BOS_up"),
        )
        up = build_user_prompt(snap)
        assert "### 9i." in up
        # 三行都在
        assert "[1w]" in up
        assert "[1d]" in up
        assert "[1h]" in up
        # 共振判定
        assert "三周期同向共振" in up

    def test_backwards_compatible_with_only_1h(self):
        """只给 1h 时必须降级为"仅单 TF"模式，不崩。"""
        snap = _snap_with_mtf(None, None, _ms("bullish", last_event="BOS_up"))
        up = build_user_prompt(snap)
        assert "### 9i." in up
        assert "[1h]" in up
        # 无 1w/1d 行
        assert "[1w]" not in up
        assert "[1d]" not in up
        # 有 MTF 提示行
        assert "未形成明确共振" in up or "MTF" in up

    def test_ms_1d_only_still_renders(self):
        """极端情况：只有 1d 数据（1h 未算出），仍应渲染 §9i。"""
        snap = _snap_with_mtf(None, _ms("bearish"), None)
        up = build_user_prompt(snap)
        assert "### 9i." in up
        assert "[1d]" in up

    def test_section_skipped_when_all_missing(self):
        snap = _snap_with_mtf(None, None, None)
        up = build_user_prompt(snap)
        assert "### 9i." not in up

    def test_mtf_conflict_renders_warning(self):
        """1w 多 1d 空 → 冲突提示。"""
        snap = _snap_with_mtf(_ms("bullish"), _ms("bearish"), _ms("bullish"))
        up = build_user_prompt(snap)
        assert "周线与日线方向相反" in up
        assert "结构转换期" in up
