"""P0.7 回归：新闻叙事活跃主题的 bias vs intensity 语义解耦展示

背景：NarrativeTheme 里有两个不同语义的字段：
  - current_direction_bias：该叙事对 **BTC 价格** 的方向判断（bullish/bearish/neutral）
  - current_intensity：叙事影响强度（0-5），与价格方向解耦
prompt 旧渲染 `bias=xxx intensity=n/5` 裸写没解释，观察到 AI 会把强度当方向
权重或把叙事情感（e.g. 美伊冲突的"负面色彩"）直接当作 BTC 看多/看空信号。

本测试锁定展示层消歧：
1. 渲染字段名从 `bias=` 改为 `btc_price_bias=`（自解释）
2. 附加"对 BTC 价格方向"括号注释
3. intensity 附"与方向解耦"说明
4. 结尾追加"字段术语"使用规则行
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ai.prompts import build_user_prompt


def _snap_with_narr(narr: list[dict]) -> dict:
    return {
        "coin": "BTC",
        "price": 75000.0,
        "high_24h": 76000,
        "low_24h": 74000,
        "news_brief_text": "[brief 文本 placeholder]",
        "active_narratives": narr,
    }


def test_bias_field_renamed_to_btc_price_bias():
    out = build_user_prompt(_snap_with_narr([
        {
            "theme_id": "Middle_East_Iran", "theme_name_cn": "中东-伊朗局势",
            "current_direction_bias": "bearish",
            "current_intensity": 4,
            "flip_flop_count_24h": 0,
        },
    ]))
    assert "btc_price_bias=bearish" in out
    assert "对 BTC 价格方向的判断" in out


def test_intensity_explicitly_decoupled_from_direction():
    out = build_user_prompt(_snap_with_narr([
        {
            "theme_id": "Fed_Policy", "theme_name_cn": "美联储政策",
            "current_direction_bias": "neutral",
            "current_intensity": 3,
            "flip_flop_count_24h": 0,
        },
    ]))
    assert "intensity=3/5" in out
    assert "叙事影响强度，与方向解耦" in out


def test_terminology_footnote_emitted_when_themes_present():
    out = build_user_prompt(_snap_with_narr([
        {
            "theme_id": "Regulation", "theme_name_cn": "监管打压",
            "current_direction_bias": "bearish",
            "current_intensity": 5,
            "flip_flop_count_24h": 1,
        },
    ]))
    assert "【字段术语】" in out
    assert "解耦" in out
    assert "高强度利空 ≠ 高强度看多" in out
