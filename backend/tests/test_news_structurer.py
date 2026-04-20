"""D08 Layer 2 news_structurer 单测（mock analyzer）"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from models.news_event import RawNewsItem
from models.narrative import NarrativeTheme
from models.geo_risk import GeoRiskState
from processors.news_structurer import (
    structure_news_layer2, _fallback_rule_infer, _extract_json_array,
    _split_into_batches, _dict_to_signal,
)


# ── 测试辅助 ──

class MockAnalyzer:
    """可控的 mock analyzer"""

    def __init__(self, *, responses=None, raise_on_call: int = 0):
        self.responses = list(responses or [])
        self.calls = []
        self.raise_on_call = raise_on_call  # 0=从不 raise；N=第N次(1-based)抛
        self._count = 0

    async def call_chat(self, *, system_prompt, user_prompt, temperature=0.2, max_tokens=2000):
        self._count += 1
        self.calls.append({
            "system_prompt": system_prompt,
            "user_prompt": user_prompt,
            "temperature": temperature,
            "max_tokens": max_tokens,
        })
        if self.raise_on_call and self._count == self.raise_on_call:
            raise RuntimeError("mock failure")
        if self.responses:
            text = self.responses.pop(0)
        else:
            text = "[]"
        return text, {"tokens": 100, "latency_ms": 50, "model": "deepseek-chat"}


def _raw(eid: str, title: str = "Test title", content: str = "Test content", tier_hint: str = "normal") -> RawNewsItem:
    return RawNewsItem(
        source_type="okx",
        source_author="Blockbeats",
        source_reliability=0.9,
        external_id=eid,
        publish_time=int(time.time() * 1000),
        fetch_time=int(time.time()),
        title=title,
        content=content,
        lang="zh",
    )


def _fake_ai_obj(eid: str, *, direction="bullish", impact=3, theme="Fed_Rate_Policy", risk_type="macro_economic", tier="normal", flip_flop=False) -> dict:
    return {
        "event_id": eid,
        "target": "BTC",
        "direction": direction,
        "first_order_impact": "直接影响",
        "second_order_impact": "",
        "impact_score": impact,
        "confidence": 0.8,
        "source_credibility": 0.9,
        "horizon": "short",
        "narrative_theme": theme,
        "narrative_stage": "continuing",
        "flip_flop_warning": flip_flop,
        "already_priced_in_pct": 10.0,
        "risk_type": risk_type,
        "impact_on_assets": [{"asset": "BTC", "direction": "bullish", "magnitude": "medium"}],
        "rationale_cn": "因为利好",
        "summary_cn": "利好摘要",
        "trading_insight": "逢低做多",
        "tier": tier,
    }


# ── 单测 ──

def test_structure_empty_input():
    res = asyncio.run(structure_news_layer2(
        items=[], tier_map={},
        active_narratives={}, geo_states={},
        current_btc_price=70000.0,
        analyzer=MockAnalyzer(),
    ))
    assert res == []


def test_structure_single_item_parses_correctly():
    item = _raw("e1", title="美联储降息")
    ai_json = json.dumps([_fake_ai_obj("e1", direction="bullish", impact=3)])
    res = asyncio.run(structure_news_layer2(
        items=[item], tier_map={"e1": "major"},
        active_narratives={}, geo_states={},
        current_btc_price=70000.0,
        analyzer=MockAnalyzer(responses=[ai_json]),
    ))
    assert len(res) == 1
    sig = res[0]
    assert sig.event_id == "e1"
    assert sig.direction == "bullish"
    assert sig.impact_score == 3
    assert sig.narrative_theme == "Fed_Rate_Policy"
    assert sig.processed_by == "ai"
    assert sig.model_used == "deepseek-chat"
    assert len(sig.impact_on_assets) == 1
    assert sig.impact_on_assets[0].asset == "BTC"


def test_structure_handles_markdown_fence():
    item = _raw("e1")
    ai_json = "```json\n" + json.dumps([_fake_ai_obj("e1")]) + "\n```"
    res = asyncio.run(structure_news_layer2(
        items=[item], tier_map={"e1": "normal"},
        active_narratives={}, geo_states={},
        current_btc_price=70000.0,
        analyzer=MockAnalyzer(responses=[ai_json]),
    ))
    assert len(res) == 1
    assert res[0].event_id == "e1"


def test_structure_fallback_on_ai_failure():
    item = _raw("e1", title="Breaking: bitcoin hack halt")
    res = asyncio.run(structure_news_layer2(
        items=[item], tier_map={"e1": "major"},
        active_narratives={}, geo_states={},
        current_btc_price=70000.0,
        analyzer=MockAnalyzer(raise_on_call=1),
    ))
    assert len(res) == 1
    assert res[0].processed_by == "rule"
    assert res[0].tier == "major"
    assert res[0].impact_score < 0  # 标题含 hack/halt 触发负向


def test_structure_missing_event_id_uses_fallback():
    """AI 返回不包含某 event_id → 该条走 fallback"""
    items = [_raw("e1"), _raw("e2")]
    ai_json = json.dumps([_fake_ai_obj("e1")])  # 缺 e2
    res = asyncio.run(structure_news_layer2(
        items=items, tier_map={"e1": "normal", "e2": "minor"},
        active_narratives={}, geo_states={},
        current_btc_price=70000.0,
        analyzer=MockAnalyzer(responses=[ai_json]),
        batch_size=2,  # 强制单批
    ))
    assert len(res) == 2
    assert res[0].event_id == "e1" and res[0].processed_by == "ai"
    assert res[1].event_id == "e2" and res[1].processed_by == "rule"


def test_structure_clamps_invalid_fields():
    item = _raw("e1")
    bad_obj = {
        "event_id": "e1",
        "target": "BTC",
        "direction": "invalid_dir",  # 非法
        "impact_score": 99,           # 超界
        "confidence": 2.0,            # 超界
        "already_priced_in_pct": -50, # 负值
        "horizon": "wrong",           # 非法
        "narrative_stage": "wtf",
        "risk_type": "unknown",
        "tier": "hyperblackswan",
    }
    res = asyncio.run(structure_news_layer2(
        items=[item], tier_map={"e1": "normal"},
        active_narratives={}, geo_states={},
        current_btc_price=70000.0,
        analyzer=MockAnalyzer(responses=[json.dumps([bad_obj])]),
    ))
    assert len(res) == 1
    sig = res[0]
    assert sig.direction == "neutral"
    assert -5 <= sig.impact_score <= 5
    assert 0.0 <= sig.confidence <= 1.0
    assert sig.already_priced_in_pct >= 0
    assert sig.horizon == "short"
    assert sig.narrative_stage == "new"
    assert sig.risk_type == "none"
    assert sig.tier == "normal"


def test_flip_flop_backfill_from_narrative_tracker_count():
    """若 AI 没标 flip_flop_warning，但 theme 有 flip_flop_count_24h>=1 且方向相反 → 补上"""
    item = _raw("e1")
    # AI 判定 bearish，但 theme bias=bullish 且 24h 有 1 次反复
    ai_json = json.dumps([_fake_ai_obj("e1", direction="bearish", theme="Middle_East_Iran", flip_flop=False)])
    theme = NarrativeTheme(
        theme_id="Middle_East_Iran",
        theme_name_cn="美伊",
        current_direction_bias="bullish",
        flip_flop_count_24h=1,
        event_count_24h=3,
        first_seen_ts=int(time.time()) - 3600,
        last_seen_ts=int(time.time()) - 100,
    )
    res = asyncio.run(structure_news_layer2(
        items=[item], tier_map={"e1": "major"},
        active_narratives={"Middle_East_Iran": theme}, geo_states={},
        current_btc_price=70000.0,
        analyzer=MockAnalyzer(responses=[ai_json]),
    ))
    assert res[0].flip_flop_warning is True


def test_split_into_batches_by_tier():
    from config.settings import get_settings
    news_cfg = get_settings().ai.news_agent

    items = [_raw(f"e{i}") for i in range(10)]
    tier_map = {
        "e0": "blackswan",
        "e1": "major", "e2": "major", "e3": "major", "e4": "major",
        "e5": "normal", "e6": "normal", "e7": "normal", "e8": "normal", "e9": "normal",
    }
    batches = _split_into_batches(items, tier_map, news_cfg)
    # blackswan 默认 1/批 → 1 批
    # major 默认 3/批 → 4 条 / 3 = 2 批（3+1）
    # normal 默认 5/批 → 5 条 / 5 = 1 批
    assert len(batches) == 1 + 2 + 1
    # 第一批应是 blackswan（tier bucket 顺序）
    assert all(tier_map[it.external_id] == "blackswan" for it in batches[0])


def test_extract_json_array_various_formats():
    assert _extract_json_array("[]") == []
    assert _extract_json_array('[{"a":1}]') == [{"a": 1}]
    assert _extract_json_array('```json\n[{"b":2}]\n```') == [{"b": 2}]
    assert _extract_json_array("noise [1,2,3] trailing") == [1, 2, 3]
    assert _extract_json_array('{"events":[{"x":1}]}') == [{"x": 1}]
    assert _extract_json_array("not a json") == []
    assert _extract_json_array("") == []


def test_fallback_rule_infer_blackswan_is_bearish():
    item = _raw("e1", title="极端事件")
    sig = _fallback_rule_infer(item, "blackswan")
    assert sig.direction == "bearish"
    assert sig.impact_score == -4
    assert sig.risk_type == "black_swan"
    assert sig.tier == "blackswan"


def test_fallback_rule_infer_major_war_title_geopolitical():
    item = _raw("e1", title="中东战争升级", content="...")
    sig = _fallback_rule_infer(item, "major")
    assert sig.risk_type == "geopolitical"


def test_fallback_rule_infer_normal_neutral():
    item = _raw("e1", title="行业讨论")
    sig = _fallback_rule_infer(item, "normal")
    assert sig.direction == "neutral"
    assert sig.impact_score == 0
