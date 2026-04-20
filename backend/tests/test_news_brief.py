"""D09 news_brief 单测（mock analyzer）"""

from __future__ import annotations

import asyncio
import json
import os
import time

import pytest

from models.geo_risk import GeoRiskOverview
from models.narrative import NarrativeTheme
from models.news_brief import NewsBrief, NewsBriefSection
from models.news_event import (
    AssetImpact, EnrichedNewsEvent, MarketEventSignal, RawNewsItem,
)
from processors.news_brief import (
    generate_brief, get_current_brief, reset_current_brief, set_current_brief,
    _normalize_sections, _parse_brief_response, _extract_json_object,
    _diff_briefs, _bullet_lines, _derive_d09_status,
    set_history_dir_for_tests,
)


@pytest.fixture(autouse=True)
def _reset(tmp_path):
    # 把 history 文件重定向到 pytest tmp 目录，避免污染 backend/data/
    set_history_dir_for_tests(str(tmp_path))
    reset_current_brief()
    yield
    reset_current_brief()
    set_history_dir_for_tests(None)


# ── 测试辅助 ──

class MockAnalyzer:
    def __init__(self, responses=None, raise_on_call=0):
        self.responses = list(responses or [])
        self.calls = []
        self.raise_on_call = raise_on_call
        self._count = 0

    async def call_chat(self, *, system_prompt, user_prompt, temperature=0.2, max_tokens=1500):
        self._count += 1
        self.calls.append({"system": system_prompt, "user": user_prompt, "max_tokens": max_tokens})
        if self.raise_on_call and self._count == self.raise_on_call:
            raise RuntimeError("mock brief failure")
        text = self.responses.pop(0) if self.responses else "{}"
        return text, {"tokens": 400, "latency_ms": 80, "model": "deepseek-chat"}


def _enriched(
    event_id: str = "e1",
    ts: int = None,
    direction: str = "bullish",
    tier: str = "normal",
    theme: str = "Fed_Rate_Policy",
    impact: int = 3,
    summary: str = "利好",
) -> EnrichedNewsEvent:
    ts = ts or int(time.time())
    return EnrichedNewsEvent(
        raw=RawNewsItem(
            source_type="okx",
            source_author="BlockBeats",
            source_reliability=0.9,
            external_id=event_id,
            publish_time=ts * 1000,
            fetch_time=ts,
            title=summary,
            content="...",
            lang="zh",
        ),
        structured=MarketEventSignal(
            event_id=event_id,
            ts=ts,
            expires_at=ts + 86400,
            target="BTC",
            direction=direction,
            first_order_impact=summary,
            impact_score=impact,
            confidence=0.8,
            horizon="short",
            narrative_theme=theme,
            risk_type="macro_economic",
            tier=tier,
            summary_cn=summary,
            impact_on_assets=[AssetImpact(asset="BTC", direction=direction, magnitude="medium")],
        ),
    )


def _theme(theme_id: str = "Fed_Rate_Policy", name: str = "Fed 政策") -> NarrativeTheme:
    now = int(time.time())
    return NarrativeTheme(
        theme_id=theme_id,
        theme_name_cn=name,
        category="macro_policy",
        first_seen_ts=now - 3600,
        last_seen_ts=now - 100,
        event_count_24h=3,
        flip_flop_count_24h=1,
        current_direction_bias="bullish",
        current_intensity=3,
        trend="active",
    )


def _geo_overview(level: int = 2) -> GeoRiskOverview:
    return GeoRiskOverview(
        ts=int(time.time()),
        overall_level=level,
        overall_label="TENSION" if level == 2 else "PEACE",
        overall_emoji="🟠" if level == 2 else "🟢",
        overall_summary_cn="全球风险中等",
        active_themes=[],
    )


def _brief_json(**overrides) -> str:
    obj = {
        "tldr_cn": "宏观偏多，地缘中等",
        "sections": [
            {"section_id": "macro", "section_title_cn": "宏观", "bullets": ["美联储降息预期升温", "CPI 温和"]},
            {"section_id": "regulatory", "section_title_cn": "监管", "bullets": ["SEC 批准新 ETF"]},
            {"section_id": "onchain", "section_title_cn": "链上", "bullets": []},
            {"section_id": "risk", "section_title_cn": "风险", "bullets": ["中东局势紧张"]},
        ],
        "tracked_themes": [
            {
                "theme_id": "Fed_Rate_Policy",
                "theme_name_cn": "Fed 政策",
                "current_stance_cn": "偏多",
                "flip_flop_count_24h": 1,
                "relevance_score": 0.8,
            }
        ],
    }
    obj.update(overrides)
    return json.dumps(obj, ensure_ascii=False)


# ── 单测 ──

def test_generate_full_brief_first_time():
    events = [_enriched("e1"), _enriched("e2", direction="bearish", summary="利空")]
    themes = [_theme()]
    geo = _geo_overview(level=2)
    analyzer = MockAnalyzer(responses=[_brief_json()])
    brief = asyncio.run(generate_brief(
        events_24h=events, themes=themes, geo_overview=geo,
        prev_brief=None, analyzer=analyzer, trigger="scheduled",
    ))
    assert brief.version == 1
    assert brief.based_on_events_count == 2
    assert brief.tldr_cn == "宏观偏多，地缘中等"
    assert len(brief.sections) == 4
    macro = next(s for s in brief.sections if s.section_id == "macro")
    assert len(macro.bullets) == 2
    assert brief.char_count > 0
    assert brief.token_estimate > 0
    assert brief.update_trigger == "scheduled"
    assert brief.diff_from_prev_version == ""  # 无 prev → 空


def test_blackswan_forces_full_rewrite():
    """有 prev_brief 但 trigger=blackswan 时 → 走 full prompt"""
    prev = NewsBrief(
        version=3, updated_at=int(time.time()) - 600,
        tldr_cn="旧简报",
        sections=_normalize_sections(None),
        based_on_events_count=5,
    )
    set_current_brief(prev)
    analyzer = MockAnalyzer(responses=[_brief_json(tldr_cn="新简报·黑天鹅")])
    brief = asyncio.run(generate_brief(
        events_24h=[_enriched("e99", tier="blackswan", direction="bearish")],
        themes=[_theme()],
        geo_overview=_geo_overview(level=4),
        prev_brief=prev,
        analyzer=analyzer,
        trigger="blackswan",
    ))
    assert brief.version == 4
    assert brief.update_trigger == "blackswan"
    assert brief.tldr_cn == "新简报·黑天鹅"
    # 调用的 prompt 不应含 "旧简报" 相关 incremental 特征
    user_prompt = analyzer.calls[0]["user"]
    assert "过去 24h 结构化事件" in user_prompt  # full prompt 标记


def test_incremental_selects_only_new_events():
    now = int(time.time())
    prev = NewsBrief(
        version=2, updated_at=now - 1000, tldr_cn="旧",
        sections=_normalize_sections(None), based_on_events_count=2,
    )
    # 老事件 (< prev.updated_at) + 新事件 (> prev.updated_at)
    old_ev = _enriched("e_old", ts=now - 2000)
    new_ev = _enriched("e_new", ts=now - 500)
    analyzer = MockAnalyzer(responses=[_brief_json(tldr_cn="增量合并")])
    brief = asyncio.run(generate_brief(
        events_24h=[old_ev, new_ev],
        themes=[_theme()], geo_overview=_geo_overview(),
        prev_brief=prev, analyzer=analyzer, trigger="scheduled",
    ))
    assert brief.version == 3
    assert brief.update_trigger == "scheduled"
    # 增量 prompt 标志
    user_prompt = analyzer.calls[0]["user"]
    assert "旧简报" in user_prompt or "【旧简报" in user_prompt
    assert "e_new" in user_prompt
    # old_ev 不应出现在新增事件部分
    assert user_prompt.count("e_old") == 0


def test_diff_generated_when_prev_exists():
    now = int(time.time())
    prev = NewsBrief(
        version=1, updated_at=now - 1000, tldr_cn="旧 tldr",
        sections=[
            NewsBriefSection(section_id="macro", section_title_cn="宏观", bullets=["旧条目"]),
            NewsBriefSection(section_id="regulatory", section_title_cn="监管", bullets=[]),
            NewsBriefSection(section_id="onchain", section_title_cn="链上", bullets=[]),
            NewsBriefSection(section_id="risk", section_title_cn="风险", bullets=[]),
        ],
        based_on_events_count=1,
    )
    analyzer = MockAnalyzer(responses=[_brief_json(tldr_cn="全新 tldr")])
    brief = asyncio.run(generate_brief(
        events_24h=[_enriched("e1")],
        themes=[_theme()], geo_overview=_geo_overview(),
        prev_brief=prev, analyzer=analyzer, trigger="scheduled",
    ))
    assert brief.diff_from_prev_version != ""
    # diff 应包含 tldr 变化
    assert "旧 tldr" in brief.diff_from_prev_version or "全新 tldr" in brief.diff_from_prev_version


def test_analyzer_failure_uses_fallback():
    prev = NewsBrief(
        version=5, updated_at=int(time.time()) - 100, tldr_cn="保留旧",
        sections=_normalize_sections(None), based_on_events_count=3,
        model_used="prev_model",
    )
    analyzer = MockAnalyzer(responses=[], raise_on_call=1)
    brief = asyncio.run(generate_brief(
        events_24h=[_enriched("e1")],
        themes=[_theme()], geo_overview=_geo_overview(),
        prev_brief=prev, analyzer=analyzer, trigger="scheduled",
    ))
    # 版本仍然推进
    assert brief.version == 6
    assert "AI 失败" in brief.tldr_cn
    assert brief.model_used == "fallback"


def test_first_time_analyzer_failure_returns_placeholder():
    analyzer = MockAnalyzer(responses=[], raise_on_call=1)
    brief = asyncio.run(generate_brief(
        events_24h=[_enriched("e1")],
        themes=[], geo_overview=_geo_overview(),
        prev_brief=None, analyzer=analyzer, trigger="scheduled",
    ))
    assert brief.version == 1
    assert brief.model_used == "fallback"
    assert "首次生成失败" in brief.tldr_cn


def test_sections_always_4_in_order():
    # AI 只返回 2 个板块 → 其余自动补全为空
    partial_json = json.dumps({
        "tldr_cn": "test",
        "sections": [
            {"section_id": "macro", "section_title_cn": "宏观", "bullets": ["x"]},
            {"section_id": "risk", "section_title_cn": "风险", "bullets": ["y"]},
        ],
        "tracked_themes": [],
    })
    analyzer = MockAnalyzer(responses=[partial_json])
    brief = asyncio.run(generate_brief(
        events_24h=[_enriched("e1")],
        themes=[], geo_overview=_geo_overview(),
        prev_brief=None, analyzer=analyzer, trigger="scheduled",
    ))
    assert [s.section_id for s in brief.sections] == ["macro", "regulatory", "onchain", "risk"]
    assert len(next(s for s in brief.sections if s.section_id == "macro").bullets) == 1
    assert len(next(s for s in brief.sections if s.section_id == "regulatory").bullets) == 0


def test_extract_json_object_robust():
    assert _extract_json_object('{"a":1}') == {"a": 1}
    assert _extract_json_object('```json\n{"b":2}\n```') == {"b": 2}
    assert _extract_json_object('random text {"c":3} trail') == {"c": 3}
    assert _extract_json_object("not json at all") == {}
    assert _extract_json_object("") == {}


def test_parse_brief_response_clamps_relevance():
    raw = json.dumps({
        "tldr_cn": "",
        "sections": [],
        "tracked_themes": [
            {"theme_id": "x", "relevance_score": 5.0},  # 超界
            {"theme_id": "y", "relevance_score": -1.0},
        ],
    })
    b = _parse_brief_response(raw, base_events_count=0, trigger="scheduled")
    scores = {t.theme_id: t.relevance_score for t in b.tracked_themes}
    assert scores["x"] == 1.0
    assert scores["y"] == 0.0


def test_current_brief_singleton():
    assert get_current_brief() is None
    b = NewsBrief(version=1, tldr_cn="x", updated_at=int(time.time()))
    set_current_brief(b)
    got = get_current_brief()
    assert got is not None and got.tldr_cn == "x"


def test_max_chars_shrink_pass_triggers_second_call():
    """生成的 brief 字符数超过 max_chars 时应触发第二次 shrink 调用"""
    # 生成一个巨大的首轮 brief（100 条 bullets）
    huge_bullets = [f"要点{i}" * 20 for i in range(100)]  # 故意超大
    big_json = json.dumps({
        "tldr_cn": "t",
        "sections": [
            {"section_id": "macro", "section_title_cn": "宏观", "bullets": huge_bullets},
            {"section_id": "regulatory", "section_title_cn": "监管", "bullets": []},
            {"section_id": "onchain", "section_title_cn": "链上", "bullets": []},
            {"section_id": "risk", "section_title_cn": "风险", "bullets": []},
        ],
        "tracked_themes": [],
    })
    small_json = _brief_json(tldr_cn="已精简")

    analyzer = MockAnalyzer(responses=[big_json, small_json])
    brief = asyncio.run(generate_brief(
        events_24h=[_enriched("e1")],
        themes=[], geo_overview=_geo_overview(),
        prev_brief=None, analyzer=analyzer, trigger="scheduled",
        max_chars=800,
    ))
    # 两次调用都发生了
    assert len(analyzer.calls) == 2
    # 第二次 shrink 后结果应远小于第一次
    assert brief.tldr_cn == "已精简"


def test_generate_brief_circuit_break_when_events_empty():
    """P0-3 · events=0 时必须熔断：绝不调 analyzer，绝不生成虚构内容。"""
    analyzer = MockAnalyzer(responses=[_brief_json(tldr_cn="不应出现的文本")])
    brief = asyncio.run(generate_brief(
        events_24h=[],
        themes=[_theme()],
        geo_overview=_geo_overview(level=3),
        prev_brief=None,
        analyzer=analyzer,
        trigger="scheduled",
    ))
    # analyzer 不应被调用
    assert len(analyzer.calls) == 0
    # brief 是空占位，based_on_events_count=0，tldr 留空
    assert brief.based_on_events_count == 0
    assert brief.tldr_cn == ""
    assert brief.model_used == "skipped_no_events"
    assert all(len(s.bullets) == 0 for s in brief.sections)


def test_collect_news_context_skips_inject_when_events_zero():
    """P0-3 · _collect_news_context 必须在 based_on_events=0 时 不 注入 brief_text。"""
    from ai.snapshot import _collect_news_context

    reset_current_brief()
    brief = NewsBrief(
        version=3, updated_at=int(time.time()), tldr_cn="本应不被注入",
        sections=[NewsBriefSection(section_id="macro", section_title_cn="宏观", bullets=["x"])],
        based_on_events_count=0,
        model_used="skipped_no_events",
    )
    set_current_brief(brief)
    try:
        ctx = _collect_news_context()
        # 绝不把 tldr / sections 传给主 prompt
        assert ctx["news_brief_text"] == ""
        # 但元数据保留（前端可显示"本轮无事件"）
        assert ctx["news_brief_version"] == 3
    finally:
        reset_current_brief()


def test_collect_news_context_injects_when_events_present():
    """P0-3 · 对照组：有事件支撑时 brief_text 必须正常注入。"""
    from ai.snapshot import _collect_news_context

    reset_current_brief()
    brief = NewsBrief(
        version=5, updated_at=int(time.time()), tldr_cn="有真实事件",
        sections=[NewsBriefSection(section_id="macro", section_title_cn="宏观", bullets=["x"])],
        based_on_events_count=4,
        model_used="deepseek-chat",
    )
    set_current_brief(brief)
    try:
        ctx = _collect_news_context()
        assert ctx["news_brief_text"] != ""
        assert "有真实事件" in ctx["news_brief_text"]
        assert ctx["news_brief_version"] == 5
    finally:
        reset_current_brief()


def test_generate_brief_circuit_break_preserves_version_chain():
    """P0-3 · events=0 熔断不破坏 version 单调递增（防止前端卡死）。"""
    prev = NewsBrief(
        version=7, updated_at=int(time.time()) - 3600, tldr_cn="old",
        sections=[NewsBriefSection(section_id="macro", section_title_cn="宏观", bullets=["old bullet"])],
        based_on_events_count=3,
    )
    analyzer = MockAnalyzer(responses=[])
    brief = asyncio.run(generate_brief(
        events_24h=[],
        themes=[], geo_overview=_geo_overview(level=0),
        prev_brief=prev, analyzer=analyzer, trigger="scheduled",
    ))
    assert brief.version == 8
    assert brief.based_on_events_count == 0
    assert brief.model_used == "skipped_no_events"
    assert len(analyzer.calls) == 0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# D09 status 语义（P0-3 后续修复：熔断态 ≠ 故障态）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_d09_status_skipped_no_events_is_ok_not_warn():
    """熔断是健康的保护行为，不应挂 warn（否则上游长期空窗会告警疲劳）。"""
    brief = NewsBrief(
        version=1, updated_at=int(time.time()),
        tldr_cn="",
        sections=_normalize_sections(None),
        based_on_events_count=0,
        model_used="skipped_no_events",
    )
    status, note = _derive_d09_status(brief, bullet_total=0)
    assert status == "ok"
    assert note == "upstream_empty"


def test_d09_status_fallback_is_warn_because_ai_actually_failed():
    """AI 调用失败走 fallback 才是真正需要关注的 warn。"""
    brief = NewsBrief(
        version=2, updated_at=int(time.time()),
        tldr_cn="（AI 失败·保留上一版本）",
        sections=_normalize_sections(None),
        based_on_events_count=3,
        model_used="fallback",
    )
    status, note = _derive_d09_status(brief, bullet_total=0)
    assert status == "warn"
    assert note == "ai_call_failed"


def test_d09_status_normal_content_is_ok():
    """正常生成：有 bullets 或 tldr → ok / 空 note。"""
    brief = NewsBrief(
        version=3, updated_at=int(time.time()),
        tldr_cn="正常简报",
        sections=[NewsBriefSection(
            section_id="macro", section_title_cn="宏观", bullets=["FOMC 将于本周召开"],
        )],
        based_on_events_count=5,
        model_used="deepseek-chat",
    )
    status, note = _derive_d09_status(brief, bullet_total=1)
    assert status == "ok"
    assert note == ""


def test_d09_status_unexpected_empty_is_warn():
    """既非熔断也非 fallback，但内容为空 → warn，note 标注 unexpected。"""
    brief = NewsBrief(
        version=4, updated_at=int(time.time()),
        tldr_cn="",
        sections=_normalize_sections(None),
        based_on_events_count=10,
        model_used="deepseek-chat",  # 非熔断也非 fallback
    )
    status, note = _derive_d09_status(brief, bullet_total=0)
    assert status == "warn"
    assert note == "unexpected_empty"


def test_bullet_lines_diff():
    b1 = NewsBrief(
        version=1, updated_at=0, tldr_cn="A",
        sections=[NewsBriefSection(section_id="macro", section_title_cn="宏观", bullets=["x", "y"])],
    )
    b2 = NewsBrief(
        version=2, updated_at=0, tldr_cn="B",
        sections=[NewsBriefSection(section_id="macro", section_title_cn="宏观", bullets=["x", "z"])],
    )
    lines1 = _bullet_lines(b1)
    lines2 = _bullet_lines(b2)
    assert lines1 != lines2
    d = _diff_briefs(b1, b2)
    assert d != ""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Bootstrap seed（解决"启动 15 分钟预热中"体验）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_ensure_bootstrap_brief_cold_start_seeds_v1():
    """冷启动：无 current 无历史 → 写入 bootstrap v1，tldr 明示首轮生成中。"""
    from processors.news_brief import ensure_bootstrap_brief
    reset_current_brief()
    seed = ensure_bootstrap_brief()
    assert seed.version == 1
    assert seed.model_used == "bootstrap"
    assert "首轮" in seed.tldr_cn
    # 再次调用应幂等返回同一 current，不重新发种子
    seed2 = ensure_bootstrap_brief()
    assert seed2 is seed or (seed2.model_used == "bootstrap" and seed2.version == 1)


def test_ensure_bootstrap_does_not_pollute_history(tmp_path):
    """bootstrap 种子不落历史文件（避免每次重启都污染一条种子）。"""
    from processors.news_brief import (
        append_to_history, ensure_bootstrap_brief, load_history,
    )
    reset_current_brief()
    ensure_bootstrap_brief()
    assert load_history() == []
    # 但真正的 v1（非 bootstrap）写入时应落历史
    real = NewsBrief(
        version=2, updated_at=int(time.time()),
        tldr_cn="真·简报", based_on_events_count=5, model_used="deepseek-chat",
    )
    append_to_history(real)
    hist = load_history()
    assert len(hist) == 1 and hist[0].version == 2


def test_ensure_bootstrap_restores_from_history_on_restart():
    """重启场景：磁盘有历史 → 直接把最新一版恢复为 current，不发种子。"""
    from processors.news_brief import (
        append_to_history, ensure_bootstrap_brief,
    )
    now = int(time.time())
    append_to_history(NewsBrief(
        version=10, updated_at=now - 300,
        tldr_cn="旧版", based_on_events_count=3, model_used="deepseek-chat",
    ))
    reset_current_brief()
    restored = ensure_bootstrap_brief()
    assert restored.version == 10
    assert restored.model_used == "deepseek-chat"
    assert restored.tldr_cn == "旧版"


def test_d09_status_bootstrap_is_ok_not_warn():
    """bootstrap 属于"启动种子"，应 ok，避免启动 15 分钟内 D09 挂 warn/pending。"""
    brief = NewsBrief(
        version=1, updated_at=int(time.time()), tldr_cn="首轮简报生成中",
        sections=_normalize_sections(None),
        based_on_events_count=0, model_used="bootstrap",
    )
    status, note = _derive_d09_status(brief, bullet_total=0)
    assert status == "ok"
    assert note == "bootstrap_pending"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 历史持久化（append / load / dedupe / truncate）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_history_append_and_load_roundtrip():
    from processors.news_brief import append_to_history, load_history
    now = int(time.time())
    for i in range(3):
        append_to_history(NewsBrief(
            version=i + 1, updated_at=now + i,
            tldr_cn=f"v{i+1}",
            sections=[NewsBriefSection(section_id="macro", section_title_cn="宏观", bullets=[f"bullet {i+1}"])],
            based_on_events_count=i,
            model_used="deepseek-chat",
        ))
    hist = load_history()
    assert [b.version for b in hist] == [1, 2, 3]
    assert hist[-1].tldr_cn == "v3"
    assert hist[-1].sections[0].bullets == ["bullet 3"]


def test_history_dedupe_same_version_and_ts():
    """同一 (version, updated_at) 不重复写入（防止重启反复 append）。"""
    from processors.news_brief import append_to_history, load_history
    ts = int(time.time())
    b = NewsBrief(version=5, updated_at=ts, tldr_cn="x", model_used="deepseek-chat")
    append_to_history(b)
    append_to_history(b)
    append_to_history(b)
    assert len(load_history()) == 1


def test_history_truncates_to_max_capacity():
    """超出 MAX_HISTORY 按末尾截断，保留最新。"""
    from processors.news_brief import MAX_HISTORY, append_to_history, load_history
    now = int(time.time())
    for i in range(MAX_HISTORY + 20):
        append_to_history(NewsBrief(
            version=i + 1, updated_at=now + i,
            tldr_cn=f"v{i+1}", model_used="deepseek-chat",
        ))
    hist = load_history()
    assert len(hist) == MAX_HISTORY
    assert hist[0].version == 21  # 被截掉的是前 20 条
    assert hist[-1].version == MAX_HISTORY + 20


def test_load_history_with_limit():
    from processors.news_brief import append_to_history, load_history
    now = int(time.time())
    for i in range(10):
        append_to_history(NewsBrief(
            version=i + 1, updated_at=now + i,
            tldr_cn=f"v{i+1}", model_used="deepseek-chat",
        ))
    recent3 = load_history(limit=3)
    assert [b.version for b in recent3] == [8, 9, 10]


def test_set_current_brief_auto_persists():
    """默认 persist=True → set_current_brief 自动 append 到历史。"""
    from processors.news_brief import load_history
    reset_current_brief()
    b = NewsBrief(
        version=100, updated_at=int(time.time()),
        tldr_cn="auto-persist test", model_used="deepseek-chat",
    )
    set_current_brief(b)
    hist = load_history()
    assert len(hist) == 1 and hist[0].version == 100


def test_set_current_brief_skips_persist_when_flag_false():
    """persist=False 场景（bootstrap/单测）不落磁盘。"""
    from processors.news_brief import load_history
    reset_current_brief()
    b = NewsBrief(version=200, updated_at=int(time.time()), model_used="bootstrap")
    set_current_brief(b, persist=False)
    assert load_history() == []


def test_history_corrupted_file_returns_empty(tmp_path):
    """历史文件损坏（非 JSON / 非 list）时 load_history 返回空而不是抛错。"""
    from processors.news_brief import load_history, _resolve_history_path
    path = _resolve_history_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("{not a list}")
    assert load_history() == []
