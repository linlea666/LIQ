"""D12 NewsChatAnalyzer 基础单测（不走网络）"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ai.news_analyzer import (
    NewsChatAnalyzer, create_news_chat_analyzer, get_news_chat_analyzer,
    reset_news_chat_analyzer,
)


@pytest.fixture(autouse=True)
def _reset_singleton():
    reset_news_chat_analyzer()
    yield
    reset_news_chat_analyzer()


def test_init_without_key_not_available(monkeypatch):
    """无 key 时 analyzer.available=False, call_chat 会 raise"""
    a = NewsChatAnalyzer(api_key="")
    assert a.available is False
    assert a.model == "deepseek-chat"
    # metrics 初始为 0
    m = a.snapshot_metrics()
    assert m["chat_calls"] == 0
    assert m["available"] is False


def test_init_with_explicit_key():
    a = NewsChatAnalyzer(api_key="sk-test", api_base="https://api.deepseek.com")
    assert a.available is True
    assert a.model == "deepseek-chat"


def test_call_chat_without_client_raises():
    a = NewsChatAnalyzer(api_key="")
    with pytest.raises(RuntimeError):
        asyncio.run(a.call_chat(system_prompt="s", user_prompt="u"))


def test_call_chat_success_updates_metrics():
    a = NewsChatAnalyzer(api_key="sk-test", max_retries=1)

    fake_resp = MagicMock()
    fake_resp.choices = [MagicMock(message=MagicMock(content='[{"event_id":"e1"}]'))]
    fake_resp.usage = MagicMock(prompt_tokens=100, completion_tokens=50)

    a._client = MagicMock()
    a._client.chat.completions.create = AsyncMock(return_value=fake_resp)

    text, meta = asyncio.run(a.call_chat(system_prompt="sys", user_prompt="u"))
    assert '"event_id":"e1"' in text
    assert meta["tokens"] == 150
    assert meta["prompt_tokens"] == 100
    assert meta["completion_tokens"] == 50
    assert meta["model"] == "deepseek-chat"

    m = a.snapshot_metrics()
    assert m["chat_calls"] == 1
    assert m["total_tokens"] == 150
    assert m["fail_streak"] == 0


def test_call_chat_retries_then_fails():
    a = NewsChatAnalyzer(api_key="sk-test", max_retries=2)

    a._client = MagicMock()
    a._client.chat.completions.create = AsyncMock(side_effect=RuntimeError("boom"))

    with pytest.raises(RuntimeError):
        asyncio.run(a.call_chat(system_prompt="s", user_prompt="u"))

    m = a.snapshot_metrics()
    assert m["fail_streak"] == 1
    assert "boom" in m["last_error"]
    # 重试次数：client.create 应被调用 2 次
    assert a._client.chat.completions.create.call_count == 2


def test_call_chat_retries_then_success():
    a = NewsChatAnalyzer(api_key="sk-test", max_retries=2)

    fake_resp = MagicMock()
    fake_resp.choices = [MagicMock(message=MagicMock(content="ok"))]
    fake_resp.usage = MagicMock(prompt_tokens=10, completion_tokens=5)

    calls = {"n": 0}

    async def _side(*a_, **kw_):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("first fail")
        return fake_resp

    a._client = MagicMock()
    a._client.chat.completions.create = AsyncMock(side_effect=_side)

    text, meta = asyncio.run(a.call_chat(system_prompt="s", user_prompt="u"))
    assert text == "ok"
    assert meta["tokens"] == 15
    m = a.snapshot_metrics()
    assert m["chat_calls"] == 1
    assert m["fail_streak"] == 0


def test_singleton_reuse():
    # 无 key 下也可构造
    a1 = get_news_chat_analyzer()
    a2 = get_news_chat_analyzer()
    assert a1 is a2


def test_create_multiple_instances_independent():
    a1 = create_news_chat_analyzer(api_key="k1", model="m1")
    a2 = create_news_chat_analyzer(api_key="k2", model="m2")
    assert a1 is not a2
    assert a1.model == "m1"
    assert a2.model == "m2"


def test_metrics_avg_latency():
    a = NewsChatAnalyzer(api_key="sk-test", max_retries=1)
    a._tally_success(pt=10, ct=5, latency_ms=200)
    a._tally_success(pt=10, ct=5, latency_ms=400)
    m = a.snapshot_metrics()
    assert m["chat_calls"] == 2
    assert m["avg_latency_ms"] == 300
    assert m["total_tokens"] == 30
