"""P0.1 回归：Ahr999 单一事实源

背景：生产 AI 输出里同一份报告的 §9c 和 §9e 出现两个不同 Ahr999 值
（BBX 0.6247 "适合定投" vs Coinglass 0.4407 "适合抄底"），让 AI 在
同一次推理里得出相反结论。根因是 `build_ai_snapshot` 把 `mi.ahr999`
（BBX 源）作为 snapshot.ahr999 的优先值，而 §9e 渲染时从 cycle_position
直接取 Coinglass 值 → 两边不同源。

本测试锁定修复后的性质：
1. snapshot.ahr999 优先级：Coinglass (cycle_position.ahr999_value) > BBX (mi.ahr999)
2. Coinglass 缺失时 BBX 值作为 fallback
3. 两源都缺失/为 0 时为 None
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ai.snapshot import build_ai_snapshot
from models.flow import CyclePositionData, MarketIndexData


def _make_mi(ahr: float | None) -> MarketIndexData:
    return MarketIndexData(ts=0, ahr999=ahr)


def _make_cycle(ahr_val: float | None) -> CyclePositionData:
    return CyclePositionData(ts=0, cps=3.0, cps_label="公允区", ahr999_value=ahr_val)


def _resolve_ahr999(mi_ahr: float | None, cycle_ahr: float | None) -> float | None:
    """精简复用 snapshot 里的优先级规则（保留相同逻辑）。"""
    snap = build_ai_snapshot(
        coin="BTC",
        price=75000.0, high_24h=76000.0, low_24h=74000.0,
        liq_map=None, cvd_contract=None, cvd_spot=None,
        oi=None, funding=None, basis=None, orderbook=None,
        liq_stats=None, vp=None, atr=0.0,
        market_temp_score=50.0, pin_risk_level="low",
        market_index=_make_mi(mi_ahr),
        cycle_position=_make_cycle(cycle_ahr),
    ).model_dump()
    return snap["ahr999"]


def test_ahr999_prefers_coinglass_over_bbx():
    """P0.1 · 两源都有时优先取 Coinglass（与 §9e CPS 同源）。"""
    assert _resolve_ahr999(0.6247, 0.4407) == 0.4407, (
        "两源都有时必须优先 Coinglass（§9e 贡献分同源）"
    )


def test_ahr999_falls_back_to_bbx_when_coinglass_missing():
    """P0.1 · Coinglass 缺失时退回 BBX 值。"""
    assert _resolve_ahr999(0.6247, None) == 0.6247, "Coinglass 缺失时必须 fallback 到 BBX"


def test_ahr999_none_when_both_missing():
    """P0.1 · 两源都无时为 None。"""
    assert _resolve_ahr999(None, None) is None


def test_ahr999_treats_zero_as_missing():
    """P0.1 · 源值 <=0 视为缺失（防上游异常返回 0）。"""
    assert _resolve_ahr999(0.0, 0.0) is None
