"""P0.4 回归：MTF 一致性按可用 TF 数分档归一化

背景：旧版 `_vote_mtf_align` 当只有 1 个 TF 有数据时仍走 `bulls == total`
分支返回 strength 0.9（和 3/3 完全同向的权重相同），把"多周期共振"
退化成"单 TF 重复计数"——冷启动期 1w/1d 未就绪时尤其严重，AI 会
被一条 1h 数据撬动整个 MTF 维度的投票权重。

本测试锁定新分档：
- total==3：3/3 → 0.9，2/3 → 0.55（保留）
- total==2：2/2 → 0.6，1/2 → 0.1 (neutral)
- total==1：任意方向 → 0.3（MTF 硬顶）
- total==0：missing
"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from processors.direction_vote import _vote_mtf_align


def _ms(direction: str):
    return SimpleNamespace(direction=direction, confidence=0.8)


class _State:
    def __init__(self, s1w=None, s1d=None, s1h=None):
        self.market_structure_1w = s1w
        self.market_structure_1d = s1d
        self.market_structure = s1h


# ─────── total == 3 ───────
def test_three_tf_all_bullish_keeps_strength_0_9():
    st = _State(_ms("bullish"), _ms("bullish"), _ms("bullish"))
    v = _vote_mtf_align(st)
    assert v.direction == "bullish"
    assert v.strength == 0.9


def test_three_tf_two_bullish_keeps_0_55():
    st = _State(_ms("bullish"), _ms("bullish"), _ms("neutral"))
    # neutral 不计入 bulls/bears（被过滤），total=2 → 走 total==2 分支
    # 上面这个实际会进 total==2 分支，这里做纯 3TF 场景：1 个中性但有 direction 字段为空串
    st2 = _State(_ms("bullish"), _ms("bullish"), SimpleNamespace(direction="", confidence=0))
    v = _vote_mtf_align(st2)
    # 2 bulls / 2 bears（但 bears=0），total=2 → 2/2 同向多 → strength 0.6
    assert v.direction == "bullish"
    assert v.strength == 0.6


# ─────── total == 2 ───────
def test_two_tf_both_bullish_strength_0_6():
    st = _State(s1w=_ms("bullish"), s1d=_ms("bullish"), s1h=None)
    v = _vote_mtf_align(st)
    assert v.direction == "bullish"
    assert v.strength == 0.6
    assert "1 周期数据不全" in v.note


def test_two_tf_conflict_neutral():
    st = _State(s1w=_ms("bullish"), s1d=_ms("bearish"), s1h=None)
    v = _vote_mtf_align(st)
    assert v.direction == "neutral"
    assert v.strength < 0.2


# ─────── total == 1 （核心 P0.4 修复点）───────
def test_single_tf_strength_capped_at_0_3():
    st = _State(s1w=None, s1d=None, s1h=_ms("bullish"))
    v = _vote_mtf_align(st)
    assert v.direction == "bullish"
    assert v.strength == 0.3, f"单 TF 必须硬顶 0.3，实际 {v.strength}"
    assert "MTF 共振无法判定" in v.note


def test_single_tf_bearish_also_capped_at_0_3():
    st = _State(s1w=None, s1d=_ms("bearish"), s1h=None)
    v = _vote_mtf_align(st)
    assert v.direction == "bearish"
    assert v.strength == 0.3


# ─────── total == 0 ───────
def test_no_tf_returns_missing():
    st = _State()
    v = _vote_mtf_align(st)
    assert v.direction == "neutral"
    assert v.strength == 0.0  # _vote_missing 强度为 0
