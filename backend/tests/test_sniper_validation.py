"""P0.5 回归：KL 信号桥接到 sniper 时的几何 + R:R 质量门

背景：旧版 `_merge_kl_signals_into_sniper` 不做几何/RR 校验，只要
KeyLevelSignal 含 entry/sl/tp1 就直接桥接为 SniperEntry 推入候选池。
主函数 `_calc_sniper_entries` 却有完整质量门 → 不对称，导致：
- TP1 离 entry 太近（rr1<1.5）的 KL 信号进入 prompt
- tp1/tp2 顺序反了、sl 方向反了的信号直接塞给 AI

本测试锁定新校验器 `_validate_sniper_entry` 与 `_merge_kl_signals_into_sniper`
的统一质量门。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from models.key_level import KeyLevelSignal
from models.levels import SniperEntry
from processors.levels import (
    _merge_kl_signals_into_sniper,
    _validate_sniper_entry,
)


def _mk_long(entry: float, sl: float, tp1: float, tp2: float, rr1: float, rr2: float) -> SniperEntry:
    return SniperEntry(
        direction="long",
        entry_price=entry, stop_loss=sl, take_profit_1=tp1, take_profit_2=tp2,
        rr_ratio_1=rr1, rr_ratio_2=rr2,
        risk_usd_per_unit=abs(entry - sl),
        cluster_usd=0, logic=[],
    )


def _mk_short(entry: float, sl: float, tp1: float, tp2: float, rr1: float, rr2: float) -> SniperEntry:
    return SniperEntry(
        direction="short",
        entry_price=entry, stop_loss=sl, take_profit_1=tp1, take_profit_2=tp2,
        rr_ratio_1=rr1, rr_ratio_2=rr2,
        risk_usd_per_unit=abs(entry - sl),
        cluster_usd=0, logic=[],
    )


# ─── 几何校验 ───
def test_valid_long_passes():
    e = _mk_long(100, 95, 110, 120, 2.0, 4.0)
    ok, why = _validate_sniper_entry(e, min_rr=2.5)
    assert ok and why == ""


def test_long_with_sl_above_entry_rejected():
    e = _mk_long(100, 105, 110, 120, 2.0, 4.0)
    ok, why = _validate_sniper_entry(e, min_rr=2.5)
    assert not ok
    assert "long 几何" in why


def test_long_with_tp2_below_tp1_rejected():
    e = _mk_long(100, 95, 120, 110, 4.0, 2.0)
    ok, why = _validate_sniper_entry(e, min_rr=2.5)
    assert not ok
    assert "long 几何" in why


def test_valid_short_passes():
    e = _mk_short(100, 105, 90, 80, 2.0, 4.0)
    ok, why = _validate_sniper_entry(e, min_rr=2.5)
    assert ok


def test_short_with_sl_below_entry_rejected():
    e = _mk_short(100, 95, 90, 80, 2.0, 4.0)
    ok, _ = _validate_sniper_entry(e, min_rr=2.5)
    assert not ok


# ─── R:R 校验 ───
def test_rr1_below_1_5_rejected():
    e = _mk_long(100, 95, 101, 120, 0.2, 4.0)
    ok, why = _validate_sniper_entry(e, min_rr=2.5)
    assert not ok and "rr1" in why


def test_rr2_below_min_rr_rejected():
    e = _mk_long(100, 95, 110, 115, 2.0, 2.0)
    ok, why = _validate_sniper_entry(e, min_rr=2.5)
    assert not ok and "rr2" in why


# ─── 桥接端到端 ───
def _mk_sig(entry: float, sl: float, tp1: float, tp2: float | None, action: str = "snipe_long",
            confidence: str = "A") -> KeyLevelSignal:
    return KeyLevelSignal(
        level_id="L-test",
        level_price=entry,
        side="support",
        state="BOUNCED",
        confidence=confidence,
        action=action,
        entry_price=entry,
        stop_loss=sl,
        tp1=tp1,
        tp2=tp2,
        reason="test",
    )


def test_kl_bridge_drops_bad_rr1():
    """rr1 < 1.5 的 KL 信号不应进入 sniper 候选池。"""
    bad = _mk_sig(entry=100, sl=95, tp1=101, tp2=110)  # rr1=0.2 远<1.5
    out = _merge_kl_signals_into_sniper([], [bad], current_price=100)
    assert len(out) == 0, "rr1 过低的 KL 信号必须被过滤"


def test_kl_bridge_drops_inverted_tp_order():
    """tp2 < tp1 in long 的 KL 信号不应进入候选池（几何不合法）。"""
    bad = _mk_sig(entry=100, sl=95, tp1=120, tp2=110)
    out = _merge_kl_signals_into_sniper([], [bad], current_price=100)
    assert len(out) == 0


def test_kl_bridge_accepts_good_signal():
    """合法且高质量的 KL 信号正常桥接进候选池。"""
    good = _mk_sig(entry=100, sl=95, tp1=110, tp2=125)  # rr1=2, rr2=5
    out = _merge_kl_signals_into_sniper([], [good], current_price=100)
    assert len(out) == 1
    assert out[0].rr_ratio_1 >= 1.5
    assert out[0].rr_ratio_2 >= 2.5
