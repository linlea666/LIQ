"""端到端数据链路测试：验证"多周期 MS + funding + OI + LS + Liq Map"这批
数据从 CoinState 真实流转到 AI prompt，字段名完全对齐，AI 能引用到位。

覆盖三层：
1. collect_extras(state)      ：Pydantic state → dict（字段对齐）
2. compact × 4                ：dict → AI-ready 精简结构
3. _build_user_prompt          ：精简结构 → prompt 字符串（包含关键线索）

关键断言：prompt 中必须含有
- market_structure.alignment
- funding 基点 + extreme_tag
- OI change_4h_pct（从 oi_history 现算）
- sentiment.divergence
- liq_fuel.asymmetry_note
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from types import SimpleNamespace
from collections import deque

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from api._ai_helpers import collect_extras
from ai.te_interpreter import (
    _build_user_prompt,
    _compact_flow_metrics,
    _compact_liq_fuel,
    _compact_market_structure,
    _compact_sentiment,
)
from models.market_structure import MarketStructure
from models.flow import (
    OIData,
    OISnapshot,
    FundingRateData,
    MultiFundingRateData,
    ExchangeFundingRate,
    LongShortRatioData,
    LongShortRatioExchange,
)
from models.liquidation import LiquidationMap, LiqCluster, VacuumZone


# ──────────────────────────────────────────────────
# 工厂：用真实 Pydantic 模型构造完整 state
# ──────────────────────────────────────────────────


def _full_state(price: float = 78000.0):
    """伪造一个"所有 9 项数据都齐"的 CoinState（用 SimpleNamespace 够用）。

    使用真实的 Pydantic 模型，保证字段名与生产路径一致。
    """
    now = int(time.time())
    now_ms = now * 1000

    # ── Market Structure × 3 周期 ──
    ms_1h = MarketStructure(
        coin="BTC", timeframe="1h",
        direction="bullish", last_event="BOS_up",
        event_ts=now_ms - 30 * 60 * 1000,
        structure_high=79500.0, structure_low=76000.0,
        operate_bias="long_only", confidence=0.78,
        summary="1h 上升结构，最近 BOS 向上 · 30m 前",
    )
    ms_1d = MarketStructure(
        coin="BTC", timeframe="1d",
        direction="bullish", last_event="CHoCH_up",
        event_ts=now_ms - 12 * 3600 * 1000,
        structure_high=80500.0, structure_low=70000.0,
        operate_bias="long_only", confidence=0.82,
        summary="日线反转向上",
    )
    ms_1w = MarketStructure(
        coin="BTC", timeframe="1w",
        direction="bullish", last_event="BOS_up",
        event_ts=now_ms - 4 * 86400 * 1000,
        structure_high=82000.0, structure_low=60000.0,
        operate_bias="long_only", confidence=0.85,
        summary="周线持续上升",
    )

    # ── Funding ──
    funding = FundingRateData(
        coin="BTC", ts=now,
        okx_rate=0.00012, binance_rate=0.00015,
        avg_rate=0.000135, oi_weighted_rate=0.00014,
        interpretation="偏多但未拥挤",
    )
    multi_funding = MultiFundingRateData(
        coin="BTC", ts=now,
        exchanges=[
            ExchangeFundingRate(exchange="OKX", current=0.00012, avg_7d=0.00008),
            ExchangeFundingRate(exchange="Binance", current=0.00015, avg_7d=0.00009),
            ExchangeFundingRate(exchange="Bybit", current=0.00013, avg_7d=0.00010),
        ],
        avg_current=0.00013, avg_7d=0.00009, oi_weighted=0.00014,
        interpretation="多头拥挤度中等",
    )

    # ── OI + history（48+ 条，覆盖 4h%） ──
    oi = OIData(
        coin="BTC", ts=now,
        current_usd=20_400_000_000,
        change_1h_pct=0.8, change_5m_pct=0.3, trend="up",
    )
    history: list = []
    for i in range(60):
        history.append(OISnapshot(
            coin="BTC", ts=now - (60 - i) * 300,
            oi=20_000_000_000 + i * (400_000_000 / 59),
            oi_usd=20_000_000_000 + i * (400_000_000 / 59),
        ))
    oi_history = deque(history, maxlen=720)

    # ── LS Ratio × 3 维度（散户 1.3 偏多 vs 大户 0.85 偏空 → retail_long_smart_short） ──
    ls_retail = LongShortRatioData(
        coin="BTC", ts=now, cycle="1h",
        exchanges=[
            LongShortRatioExchange(
                exchange="Binance", ratio=1.3, long_pct=56.5, short_pct=43.5,
            ),
        ],
        avg_ratio=1.3,
        interpretation="散户偏多",
    )
    ls_top_acct = LongShortRatioData(
        coin="BTC", ts=now, cycle="1h",
        exchanges=[
            LongShortRatioExchange(
                exchange="Binance", ratio=0.85, long_pct=45.9, short_pct=54.1,
            ),
        ],
        avg_ratio=0.85,
        interpretation="大户账户偏空",
    )
    ls_top_pos = LongShortRatioData(
        coin="BTC", ts=now, cycle="1h",
        exchanges=[
            LongShortRatioExchange(
                exchange="Binance", ratio=0.80, long_pct=44.4, short_pct=55.6,
            ),
        ],
        avg_ratio=0.80,
        interpretation="大户持仓偏空",
    )

    # ── Liquidation Map 1d（above 重 → above_heavy） ──
    liq_map = LiquidationMap(
        coin="BTC", ts=now, cycle="1d",
        leverage_groups=[],
        clusters_above=[
            LiqCluster(
                price_center=80000, price_from=79500, price_to=80500,
                total_usd=120_000_000, side="short",
                dominant_leverage="20x", distance_pct=2.56,
            ),
            LiqCluster(
                price_center=82000, price_from=81500, price_to=82500,
                total_usd=60_000_000, side="short",
                dominant_leverage="10x", distance_pct=5.13,
            ),
        ],
        clusters_below=[
            LiqCluster(
                price_center=76000, price_from=75500, price_to=76500,
                total_usd=30_000_000, side="long",
                dominant_leverage="20x", distance_pct=-2.56,
            ),
        ],
        vacuum_zones=[
            VacuumZone(price_from=80500, price_to=81400, midpoint=80950, note="quick slide"),
        ],
        imbalance_ratio=6.0,
    )

    return SimpleNamespace(
        market_structure=ms_1h,
        market_structure_1d=ms_1d,
        market_structure_1w=ms_1w,
        funding=funding,
        multi_funding=multi_funding,
        oi=oi,
        oi_history=oi_history,
        oi_change_24h_pct=2.5,
        ls_ratio=ls_retail,
        ls_ratio_top_account=ls_top_acct,
        ls_ratio_top_position=ls_top_pos,
        liq_maps={"1d": liq_map, "7d": liq_map},
    )


# ──────────────────────────────────────────────────
# Layer 1：collect_extras 能从 state 取到所有字段
# ──────────────────────────────────────────────────


def test_collect_extras_includes_all_nine_fields():
    """验证所有 9 个字段都被收集到 extras dict，且 key 名与 compact 期望一致。"""
    state = _full_state()
    extras = collect_extras(state)
    assert extras is not None
    # MS × 3
    assert "ms_1h" in extras and extras["ms_1h"]["direction"] == "bullish"
    assert "ms_1d" in extras and extras["ms_1d"]["last_event"] == "CHoCH_up"
    assert "ms_1w" in extras and extras["ms_1w"]["confidence"] == 0.85
    # funding
    assert "funding" in extras and extras["funding"]["oi_weighted_rate"] == 0.00014
    assert "multi_funding" in extras and len(extras["multi_funding"]["exchanges"]) == 3
    # OI
    assert "oi" in extras and extras["oi"]["current_usd"] == 20_400_000_000
    assert "oi_history" in extras and len(extras["oi_history"]) == 60
    assert extras["oi_history"][-1]["oi_usd"] > 20_000_000_000
    assert extras["oi_change_24h_pct"] == 2.5
    # LS × 3
    assert extras["ls_ratio"]["avg_ratio"] == 1.3
    assert extras["ls_top_account"]["avg_ratio"] == 0.85
    assert extras["ls_top_position"]["avg_ratio"] == 0.80
    # Liq
    assert "liq_map_1d" in extras
    assert extras["liq_map_1d"]["imbalance_ratio"] == 6.0
    assert len(extras["liq_map_1d"]["clusters_above"]) == 2


def test_collect_extras_handles_all_missing():
    """全缺字段时 collect_extras 返回 None，不抛异常。"""
    empty = SimpleNamespace()  # 无任何属性
    extras = collect_extras(empty)
    assert extras is None


def test_collect_extras_partial_missing_is_tolerant():
    """仅部分字段存在时，其他字段不会阻断收集。"""
    state = _full_state()
    state.funding = None
    state.ls_ratio_top_position = None
    extras = collect_extras(state)
    assert extras is not None
    assert "funding" not in extras
    assert "ls_top_position" not in extras
    # 其他仍在
    assert "ms_1h" in extras
    assert "oi" in extras


# ──────────────────────────────────────────────────
# Layer 2：compact × 4 正确产出 AI-ready 结构
# ──────────────────────────────────────────────────


def test_compact_market_structure_full_pipeline():
    state = _full_state()
    extras = collect_extras(state)
    out = _compact_market_structure(
        extras["ms_1h"], extras["ms_1d"], extras["ms_1w"], price=78000,
    )
    assert out is not None
    assert out["alignment"] == "aligned_up"
    assert out["1h"]["direction"] == "bullish"
    assert out["1d"]["last_event"] == "CHoCH_up"
    assert out["1h"]["event_age_min"] >= 29


def test_compact_flow_metrics_full_pipeline():
    state = _full_state()
    extras = collect_extras(state)
    out = _compact_flow_metrics(
        extras["funding"], extras["multi_funding"],
        extras["oi"], extras["oi_history"], extras["oi_change_24h_pct"],
    )
    assert out is not None
    # funding 基点 + extreme_tag
    assert abs(out["funding"]["oi_weighted_bp"] - 1.4) < 0.1
    assert out["funding"]["extreme_tag"] in (
        "neutral", "mild_bias_long", "mild_bias_short",
    )
    # multi_funding deviation = (0.00013 - 0.00009) * 10000 = 0.4 bp
    assert abs(out["multi_funding"]["deviation_bp"] - 0.4) < 0.1
    # OI 4h% 从 oi_history 现算：history 是线性递增，4h 内增幅约 1.35%
    assert "change_4h_pct" in out["oi"]
    assert out["oi"]["change_4h_pct"] > 0
    # 24h%
    assert out["oi"]["change_24h_pct"] == 2.5


def test_compact_sentiment_full_pipeline():
    state = _full_state()
    extras = collect_extras(state)
    out = _compact_sentiment(
        extras["ls_ratio"], extras["ls_top_account"], extras["ls_top_position"],
    )
    assert out is not None
    assert out["divergence"] == "retail_long_smart_short"
    assert out["retail"]["avg_ratio"] == 1.3
    assert out["top_account"]["avg_ratio"] == 0.85


def test_compact_liq_fuel_full_pipeline():
    state = _full_state()
    extras = collect_extras(state)
    out = _compact_liq_fuel(extras["liq_map_1d"], price=78000)
    assert out is not None
    assert out["asymmetry_note"] == "above_heavy"
    assert len(out["above"]) == 2
    assert len(out["vacuum_zones"]) == 1
    # 离价最近的 above 簇是 80000（distance_pct=2.56），保证排序
    assert out["above"][0]["price_center"] == 80000


# ──────────────────────────────────────────────────
# Layer 3：_build_user_prompt 真的把数据嵌入了 prompt
# ──────────────────────────────────────────────────


def _make_signal_min():
    return {
        "overall_state": "momentum_fading",
        "overall_direction": "down",
        "overall_action": "reduce",
        "overall_position_pct": 0.3,
        "consensus_level": "partial",
        "regime": "trend_down",
        "regime_vetoed": False,
        "overall_plain_cn": "规则候选：下跌衰竭",
        "overall_tip_cn": "减仓观察",
        "overall_reason_cn": "多周期动能减速",
        "data_quality": "ok",
        "missing_inputs": [],
        "tf_1h": {"tf": "1h", "direction": "down", "state": "momentum_fading",
                  "composite_score": -0.32, "momentum_score": -0.4,
                  "participation_score": -0.3, "exhaustion_score": -0.2,
                  "state_age_min": 60, "confirmed_ticks": 2,
                  "triggers": ["MACD 拐头"], "sub_scores": []},
        "tf_4h": {"tf": "4h", "direction": "down", "state": "momentum_fading",
                  "composite_score": -0.41, "momentum_score": -0.5,
                  "participation_score": -0.4, "exhaustion_score": -0.3,
                  "state_age_min": 240, "confirmed_ticks": 2,
                  "triggers": [], "sub_scores": []},
        "tf_1d": {"tf": "1d", "direction": "down", "state": "momentum_fading",
                  "composite_score": -0.28, "momentum_score": -0.3,
                  "participation_score": -0.2, "exhaustion_score": -0.4,
                  "state_age_min": 1440, "confirmed_ticks": 3,
                  "triggers": [], "sub_scores": []},
    }


def test_prompt_includes_all_four_data_blocks():
    """核心断言：prompt 必须含有所有 4 类扩展数据的关键标识。

    如果任一断言失败 = 某类数据没真正送到 AI 端。
    """
    state = _full_state()
    extras = collect_extras(state)

    compact_ms = _compact_market_structure(
        extras["ms_1h"], extras["ms_1d"], extras["ms_1w"], price=78000,
    )
    compact_flow = _compact_flow_metrics(
        extras["funding"], extras["multi_funding"],
        extras["oi"], extras["oi_history"], extras["oi_change_24h_pct"],
    )
    compact_sent = _compact_sentiment(
        extras["ls_ratio"], extras["ls_top_account"], extras["ls_top_position"],
    )
    compact_liq = _compact_liq_fuel(extras["liq_map_1d"], price=78000)

    prompt = _build_user_prompt(
        "BTC", _make_signal_min(), price=78000.0, atr=350.0,
        key_levels=None,
        market_structure=compact_ms,
        flow_metrics=compact_flow,
        sentiment=compact_sent,
        liq_fuel=compact_liq,
    )

    # ── 多周期 MS ──
    assert "market_structure" in prompt
    assert '"alignment": "aligned_up"' in prompt
    assert '"BOS_up"' in prompt or '"CHoCH_up"' in prompt

    # ── funding ──
    assert "flow_metrics" in prompt
    assert '"oi_weighted_bp"' in prompt
    assert '"extreme_tag"' in prompt

    # ── OI 多周期 ──
    assert '"change_5m_pct"' in prompt
    assert '"change_4h_pct"' in prompt  # 关键：从 oi_history 现算的字段真的送给了 AI
    assert '"change_24h_pct"' in prompt

    # ── sentiment 三维度 + divergence ──
    assert "sentiment" in prompt
    assert '"divergence": "retail_long_smart_short"' in prompt
    assert '"retail"' in prompt
    assert '"top_account"' in prompt
    assert '"top_position"' in prompt

    # ── liq_fuel ──
    assert "liq_fuel" in prompt
    assert '"asymmetry_note": "above_heavy"' in prompt
    assert '"vacuum_zones"' in prompt
    # 上方清算簇价位
    assert "80000" in prompt


def test_prompt_gracefully_handles_all_extras_none():
    """向后兼容：所有扩展数据为 None 时，prompt 仍可构造且含空 null 占位。"""
    prompt = _build_user_prompt(
        "BTC", _make_signal_min(), price=78000.0, atr=350.0,
        key_levels=None,
        market_structure=None, flow_metrics=None,
        sentiment=None, liq_fuel=None,
    )
    # 结构里有字段，只是值为 null
    assert '"market_structure": null' in prompt
    assert '"flow_metrics": null' in prompt
    assert '"sentiment": null' in prompt
    assert '"liq_fuel": null' in prompt
    # 不会引用这些字段的指引话术就不该出现（简单验证）
    assert "BTC" in prompt


def test_prompt_instructs_ai_to_use_new_data():
    """用户指引必须告诉 AI 如何使用这批新数据（否则 AI 会当可有可无忽略）。"""
    state = _full_state()
    extras = collect_extras(state)
    compact_ms = _compact_market_structure(
        extras["ms_1h"], extras["ms_1d"], extras["ms_1w"], price=78000,
    )
    prompt = _build_user_prompt(
        "BTC", _make_signal_min(), price=78000.0, atr=350.0,
        key_levels=None,
        market_structure=compact_ms, flow_metrics=None,
        sentiment=None, liq_fuel=None,
    )
    # prompt 里必须明确说 MS.alignment 是宏观硬锚
    assert "alignment" in prompt
    # divergence 作为强反转预警的指引
    assert "divergence" in prompt
