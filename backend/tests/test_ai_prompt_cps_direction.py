"""CPS 刻度方向回归测试

背景：生产 AI 输出里观察到对同一条 CPS=1 出现三种矛盾解读
（"极端底部" / "逆周期做空" / "顺周期做多"），根因是 prompt 只说
"1.0/10 → 溢价区"，没解释 CPS 是反向刻度（高=底部便宜、低=顶部贵）。

本测试确保：
1. system prompt 有清晰的刻度方向铁律
2. user prompt 每次渲染 CPS 都带刻度说明 + 当前档位解读
3. 不同档位映射到不同 cps_intent 文案
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ai.prompts import build_system_prompt, build_user_prompt


def _make_snapshot_with_cps(cps: float, label: str) -> dict:
    """构造含 CPS 字段的最小 snapshot（只为触发 §9e 渲染分支）。"""
    return {
        "coin": "BTC",
        "price": 77000.0,
        "high_24h": 78000.0,
        "low_24h": 76000.0,
        "cycle_position": {
            "cps": cps,
            "cps_label": label,
            "mvrv_z_score": None,
            "ahr999_value": None,
        },
    }


# ──────────────────────────────────────────────
# System prompt 刻度方向铁律
# ──────────────────────────────────────────────

def test_system_prompt_contains_cps_direction_rule():
    """system prompt 必须明确说明 CPS 是反向刻度。"""
    sp = build_system_prompt()
    assert "刻度方向铁律" in sp
    assert "反向刻度" in sp
    # 关键语义：高=底部便宜、低=顶部贵
    assert "数值越" in sp and "底部" in sp
    # 明确禁止把 1/2/3 误读为底部
    assert "严禁" in sp


def test_system_prompt_lists_all_cps_tier_mappings():
    """档位映射必须完整，避免 AI 猜阈值。"""
    sp = build_system_prompt()
    assert "≥8=周期底部区" in sp
    assert "溢价区" in sp
    assert "顶部区" in sp


# ──────────────────────────────────────────────
# User prompt 行内刻度说明
# ──────────────────────────────────────────────

def test_user_prompt_cps_renders_direction_hint_for_low_cps():
    """CPS=1.0 → 溢价区：prompt 必须带刻度说明行，避免被误读为底部。"""
    snapshot = _make_snapshot_with_cps(1.0, "溢价区")
    up = build_user_prompt(snapshot)
    assert "周期评分(CPS): 1.0/10" in up
    assert "溢价区" in up
    # 刻度方向说明必须存在
    assert "10=周期底部区" in up
    assert "0=顶部区" in up
    # 当前档位解读必须映射为"偏空"类语义
    assert "偏空" in up or "谨慎" in up


def test_user_prompt_cps_renders_direction_hint_for_high_cps():
    """CPS=9.0 → 周期底部区：当前解读应为偏多。"""
    snapshot = _make_snapshot_with_cps(9.0, "周期底部区")
    up = build_user_prompt(snapshot)
    assert "周期评分(CPS): 9.0/10" in up
    assert "周期底部区" in up
    assert "偏多" in up
    assert "便宜" in up


def test_user_prompt_cps_mid_tier_shows_neutral():
    """CPS=3.5 → 公允区：当前解读应为中性。"""
    snapshot = _make_snapshot_with_cps(3.5, "公允区")
    up = build_user_prompt(snapshot)
    assert "周期评分(CPS): 3.5/10" in up
    assert "中性" in up


def test_user_prompt_cps_extreme_top_warns_strongly():
    """CPS=0.3 → 顶部区：当前解读必须警示严禁追多。"""
    snapshot = _make_snapshot_with_cps(0.3, "顶部区")
    up = build_user_prompt(snapshot)
    assert "周期评分(CPS): 0.3/10" in up
    assert "禁止追多" in up or "强空" in up
