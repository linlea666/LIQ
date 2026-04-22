"""P0.3 回归：订单簿买卖力差公式 + 多空比变化量单位标注

# 背景
生产 AI §二市场格局总览里出现"买盘 $3.1亿 / 卖盘 $3.2亿，却给 买卖力差 +2.73%"
这种明显错误。根因双重：
1. §6 展示层把 `orderbook_spread_pct`（买一/卖一价差=流动性紧致度）
   直接标为"买卖力差"，而真实买卖力差应为 (bid-ask)/(bid+ask)%。
2. §5 多空比"24h 变化: +0.0273" 是 ratio 数值差（非百分比），AI 错把
   这个数字串到买卖力差里变成 "+2.73%"。

# 锁定
- §6 买卖力差必须按 (bid-ask)/(bid+ask)*100 计算，与 spread 分开展示。
- §5 ls_ratio 24h 变化必须显式标注"比值差, 非百分比"。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ai.prompts import build_user_prompt


BASE_SNAPSHOT = {
    "coin": "BTC",
    "price": 75000.0,
    "high_24h": 76000.0,
    "low_24h": 74000.0,
}


def _render(extra: dict) -> str:
    snap = {**BASE_SNAPSHOT, **extra}
    return build_user_prompt(snap)


def test_bid_ask_skew_correctly_negative_when_ask_heavier():
    """P0.3 · 卖盘更重时，买卖力差必须为负（实况 $3.1B 买 / $3.2B 卖）。"""
    out = _render({
        "orderbook_bid_total_usd": 3.1e9,
        "orderbook_ask_total_usd": 3.2e9,
        "orderbook_spread_pct": 0.002,
    })
    # (3.1-3.2)/(3.1+3.2)*100 ≈ -1.587%
    assert "买卖力差 -1.59%" in out or "买卖力差 -1.58%" in out, (
        f"$3.1B / $3.2B 应得 ≈ -1.59% 买卖力差，未在输出内:\n{out[out.find('### 6.'):out.find('### 6.')+500]}"
    )
    # 断开 spread 展示，与买卖力差字面区分
    assert "盘口价差 spread:" in out
    assert "非买卖力对比" in out


def test_bid_ask_skew_positive_when_bid_heavier():
    out = _render({
        "orderbook_bid_total_usd": 4.0e9,
        "orderbook_ask_total_usd": 3.0e9,
        "orderbook_spread_pct": 0.005,
    })
    # (4-3)/(4+3)*100 ≈ 14.29%
    assert "买卖力差 +14.29%" in out


def test_bid_ask_skew_zero_when_equal():
    out = _render({
        "orderbook_bid_total_usd": 5.0e9,
        "orderbook_ask_total_usd": 5.0e9,
        "orderbook_spread_pct": 0.001,
    })
    assert "买卖力差 +0.00%" in out


def test_ls_ratio_change_explicitly_labeled_as_ratio_diff():
    """P0.3 · 多空比 24h 变化必须显式标注'比值差'，防被 AI 读成'+2.73%'。"""
    out = _render({
        "ls_ratio": 2.23,
        "ls_ratio_long_pct": 69.0,
        "ls_ratio_short_pct": 31.0,
        "ls_ratio_change_24h": 0.0273,
        "ls_ratio_interpretation": "多头主导",
    })
    assert "24h 比值差 +0.0273" in out, f"未见显式'比值差'标注:\n{out[out.find('### 5.'):out.find('### 5.')+400]}"
    assert "非买卖力差" in out


def test_ls_ratio_change_includes_equivalent_pct_for_disambiguation():
    """P0.3 · 有基值时附带等效环比百分比，帮 AI 判断规模（0.0273 / 2.23 ≈ +1.22%）。"""
    out = _render({
        "ls_ratio": 2.23,
        "ls_ratio_long_pct": 69.0,
        "ls_ratio_short_pct": 31.0,
        "ls_ratio_change_24h": 0.0273,
        "ls_ratio_interpretation": "多头主导",
    })
    assert "+1.22%" in out or "+1.23%" in out, f"未见等效环比百分比:\n{out[out.find('### 5.'):out.find('### 5.')+400]}"
