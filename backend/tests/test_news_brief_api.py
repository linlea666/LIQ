"""D09 · /api/news-brief/current 路由单测

不依赖 FastAPI TestClient（项目无现成基础设施），直接 await 路由 handler。
"""

from __future__ import annotations

import asyncio
import time

import pytest

from models.news_brief import NewsBrief, NewsBriefSection
from processors.news_brief import reset_current_brief, set_current_brief


@pytest.fixture(autouse=True)
def _reset():
    reset_current_brief()
    yield
    reset_current_brief()


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
