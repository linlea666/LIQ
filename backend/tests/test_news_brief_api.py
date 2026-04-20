"""D09 · /api/news-brief/current 路由单测

不依赖 FastAPI TestClient（项目无现成基础设施），直接 await 路由 handler。
"""

from __future__ import annotations

import asyncio
import time

import pytest

from models.news_brief import NewsBrief, NewsBriefSection
from processors.news_brief import (
    reset_current_brief, set_current_brief, set_history_dir_for_tests,
)


@pytest.fixture(autouse=True)
def _reset(tmp_path):
    set_history_dir_for_tests(str(tmp_path))
    reset_current_brief()
    yield
    reset_current_brief()
    set_history_dir_for_tests(None)


def _call():
    from api.routes import news_brief_current
    return asyncio.run(news_brief_current())


def test_warming_up_when_no_brief_yet():
    """简报未生成（首启动）→ ready=False / warming_up。"""
    result = _call()
    assert result["ready"] is False
    assert result["status"] == "warming_up"
    assert "reason" in result


def test_circuit_break_status_when_no_events():
    """熔断态：前端应显示橙色"已熔断"徽章，不可渲染正文。"""
    set_current_brief(NewsBrief(
        version=1, updated_at=int(time.time()),
        tldr_cn="",
        sections=[NewsBriefSection(section_id="macro", section_title_cn="宏观", bullets=[])],
        based_on_events_count=0,
        model_used="skipped_no_events",
    ))
    result = _call()
    assert result["ready"] is True
    assert result["status"] == "circuit_break"
    assert "上游无新闻" in result["reason"]
    assert result["brief"]["based_on_events_count"] == 0


def test_ai_failed_status_from_fallback():
    """fallback：AI 调用失败，前端应显示红色"AI 故障"。"""
    set_current_brief(NewsBrief(
        version=3, updated_at=int(time.time()),
        tldr_cn="（AI 失败·保留上一版本）",
        sections=[NewsBriefSection(section_id="macro", section_title_cn="宏观", bullets=["旧"])],
        based_on_events_count=5,
        model_used="fallback",
    ))
    result = _call()
    assert result["ready"] is True
    assert result["status"] == "ai_failed"


def test_bootstrap_status_reports_first_round_pending():
    """冷启动 bootstrap 种子：状态 bootstrap，前端能识别"首轮生成中"。"""
    set_current_brief(NewsBrief(
        version=1, updated_at=int(time.time()),
        tldr_cn="首轮简报生成中·通常需 5-15 分钟",
        sections=[NewsBriefSection(section_id="macro", section_title_cn="宏观", bullets=[])],
        based_on_events_count=0,
        model_used="bootstrap",
    ))
    result = _call()
    assert result["ready"] is True
    assert result["status"] == "bootstrap"
    assert "首轮" in result["reason"]


def test_ok_status_with_real_content():
    """正常生成：前端应渲染全部正文。"""
    set_current_brief(NewsBrief(
        version=7, updated_at=int(time.time()),
        tldr_cn="BTC ETF 连续 3 日净流入，FOMC 政策预期升温",
        sections=[
            NewsBriefSection(
                section_id="macro", section_title_cn="宏观",
                bullets=["FOMC 决议将于周三公布", "ETF 净流入加速"],
            ),
            NewsBriefSection(
                section_id="regulatory", section_title_cn="监管",
                bullets=["SEC 暂无新动作"],
            ),
        ],
        based_on_events_count=8,
        model_used="deepseek-chat",
    ))
    result = _call()
    assert result["ready"] is True
    assert result["status"] == "ok"
    assert result["reason"] == ""
    assert result["brief"]["tldr_cn"].startswith("BTC ETF")
    assert len(result["brief"]["sections"]) == 2


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# /api/news-brief/history
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _call_history(**kwargs):
    from api.routes import news_brief_history
    return asyncio.run(news_brief_history(**kwargs))


def test_history_returns_persisted_briefs_in_version_order():
    from processors.news_brief import append_to_history
    now = int(time.time())
    for i in range(4):
        append_to_history(NewsBrief(
            version=i + 1, updated_at=now + i,
            tldr_cn=f"v{i+1}", based_on_events_count=i,
            model_used="deepseek-chat",
        ))
    result = _call_history(limit=30, since_ts=None)
    assert result["ready"] is True
    assert result["count"] == 4
    versions = [it["version"] for it in result["items"]]
    assert versions == [1, 2, 3, 4]


def test_history_since_ts_filters_newer_only():
    """since_ts 提供时只返回 updated_at > since_ts 的版本（供前端增量拉取）。"""
    from processors.news_brief import append_to_history
    now = int(time.time())
    for i in range(5):
        append_to_history(NewsBrief(
            version=i + 1, updated_at=now + i * 100,
            tldr_cn=f"v{i+1}", model_used="deepseek-chat",
        ))
    result = _call_history(limit=30, since_ts=now + 150)
    # now+0, now+100 不满足，应只剩 v3/v4/v5
    versions = [it["version"] for it in result["items"]]
    assert versions == [3, 4, 5]


def test_history_limit_keeps_latest():
    """limit 只保留末尾（最新）N 条。"""
    from processors.news_brief import append_to_history
    now = int(time.time())
    for i in range(10):
        append_to_history(NewsBrief(
            version=i + 1, updated_at=now + i,
            tldr_cn=f"v{i+1}", model_used="deepseek-chat",
        ))
    result = _call_history(limit=3, since_ts=None)
    versions = [it["version"] for it in result["items"]]
    assert versions == [8, 9, 10]


def test_history_empty_when_no_data():
    result = _call_history(limit=30, since_ts=None)
    assert result == {"ready": True, "count": 0, "items": []}
