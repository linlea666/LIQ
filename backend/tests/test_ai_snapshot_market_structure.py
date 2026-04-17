"""Commit 6 · AI snapshot 注入市场结构的回归测试

覆盖：
1. build_ai_snapshot 接受 market_structure 参数，序列化到 AISnapshot.market_structure
2. market_structure=None 时字段为 None（防御性），不影响其他字段
3. model_dump 的形状与下游 prompt 消费者一致（direction / last_event / operate_bias /
   confidence / structure_high / structure_low / swing_highs / swing_lows / summary）
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ai.snapshot import build_ai_snapshot
from models.market_structure import MarketStructure, SwingPoint


def _make_base_kwargs():
    """构造 build_ai_snapshot 的最小必填参数，其他默认 None/0。"""
    return dict(
        coin="BTC",
        price=77_000.0,
        high_24h=78_000.0,
        low_24h=76_000.0,
        liq_map=None,
        cvd_contract=None,
        cvd_spot=None,
        oi=None,
        funding=None,
        basis=None,
        orderbook=None,
        liq_stats=None,
        vp=None,
        atr=500.0,
        market_temp_score=50.0,
        pin_risk_level="low",
    )


def test_snapshot_injects_market_structure_when_present():
    """当 market_structure 参数提供时，AISnapshot.market_structure 必须是同构 dict。"""
    ms = MarketStructure(
        timeframe="1h",
        direction="bullish",
        last_event="BOS_up",
        event_ts=1_700_000_000,
        event_price=77_400.0,
        structure_high=77_400.0,
        structure_low=76_200.0,
        operate_bias="long_only",
        confidence=0.85,
        summary="1h 上升结构，最近 BOS↑ · 2h 前",
        swing_highs=[SwingPoint(ts=1_699_990_000, price=77_400.0, kind="high")],
        swing_lows=[SwingPoint(ts=1_699_980_000, price=76_200.0, kind="low")],
    )

    snap = build_ai_snapshot(**_make_base_kwargs(), market_structure=ms)

    assert snap.market_structure is not None, "market_structure 应该被写入 snapshot"
    dumped = snap.market_structure
    assert dumped["direction"] == "bullish"
    assert dumped["last_event"] == "BOS_up"
    assert dumped["operate_bias"] == "long_only"
    assert dumped["confidence"] == 0.85
    assert dumped["structure_high"] == 77_400.0
    assert dumped["structure_low"] == 76_200.0
    assert dumped["summary"].startswith("1h 上升结构")
    assert isinstance(dumped["swing_highs"], list)
    assert isinstance(dumped["swing_lows"], list)
    assert dumped["swing_highs"][0]["price"] == 77_400.0


def test_snapshot_market_structure_defaults_to_none():
    """未传入 market_structure 时 snapshot.market_structure == None，其他字段正常。"""
    snap = build_ai_snapshot(**_make_base_kwargs())

    assert snap.market_structure is None
    # 不应影响基础字段
    assert snap.coin == "BTC"
    assert snap.price == 77_000.0


def test_snapshot_preserves_ranging_structure_with_low_confidence():
    """震荡结构 + 低置信度也必须完整序列化，避免 prompt 消费 KeyError。"""
    ms = MarketStructure(
        timeframe="1h",
        direction="ranging",
        last_event="",
        operate_bias="stand_aside",
        confidence=0.2,
        structure_high=78_000.0,
        structure_low=76_000.0,
    )

    snap = build_ai_snapshot(**_make_base_kwargs(), market_structure=ms)

    assert snap.market_structure is not None
    dumped = snap.market_structure
    assert dumped["direction"] == "ranging"
    assert dumped["operate_bias"] == "stand_aside"
    assert dumped["confidence"] == 0.2
    assert dumped["last_event"] == ""  # 允许空事件
