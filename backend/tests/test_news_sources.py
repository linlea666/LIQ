"""D07 新闻源 + registry 单测

覆盖：
  - OkxTimelineSource._normalize / _heat_score / build_url
  - registry register / get_all / clear / fetch_all（用 mock source）
  - deduplicate_cross_source 规则
  - NewsSource.filter_new 去重
"""

from __future__ import annotations

import asyncio
import time
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from models.news_event import RawNewsItem
from sources.news.base import NewsSource
from sources.news.okx import OkxTimelineSource, create_industry_source
from sources.news.registry import (
    clear, deduplicate_cross_source, fetch_all, get_all, register,
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Fixtures
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.fixture(autouse=True)
def _clear_registry():
    clear()
    yield
    clear()


class _FakeSource(NewsSource):
    source_type = "fake"

    def __init__(self, name: str, items: list[RawNewsItem], *, raises: bool = False, reliability: float = 0.8):
        super().__init__(name=name)
        self.source_author = name
        self.source_reliability = reliability
        self._items = items
        self._raises = raises

    async def fetch(self, since_ts: Optional[int] = None) -> list[RawNewsItem]:
        if self._raises:
            raise RuntimeError("boom")
        if since_ts is None:
            return list(self._items)
        return [it for it in self._items if (it.publish_time // 1000) >= since_ts]


def _mk_raw(**kw) -> RawNewsItem:
    base = dict(
        source_type="okx",
        source_author="okx",
        source_reliability=0.85,
        external_id="id_1",
        publish_time=int(time.time() * 1000),
        title="默认标题",
        content="默认内容",
    )
    base.update(kw)
    return RawNewsItem(**base)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# OkxTimelineSource 基本字段
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_okx_build_url():
    s = OkxTimelineSource(name="x", query_name="123", size=50)
    assert "queryName=123" in s.build_url()
    assert "size=50" in s.build_url()
    assert "type=3" in s.build_url()


def test_okx_normalize_basic():
    s = create_industry_source()
    raw = {
        "contentId": "C123",
        "publishTime": 1_700_000_000_000,
        "titleNew": "美联储降息预期升温",
        "contentCnShort": "FOMC 会议前夕",
        "contentEnShort": "Fed may cut rates.",
        "viewCount": "10000",
        "likeCount": 50,
        "commentCount": 5,
        "cashTagList": ["BTC", {"tag": "ETH"}],
        "shareUrl": "https://x/1",
    }
    item = s._normalize(raw)
    assert item is not None
    assert item.external_id == "C123"
    assert item.publish_time == 1_700_000_000_000
    assert item.title == "美联储降息预期升温"
    assert item.content == "FOMC 会议前夕"
    assert item.lang == "zh"
    assert "BTC" in item.raw_tags
    assert "ETH" in item.raw_tags
    assert 0.0 < item.heat_score <= 1.0


def test_okx_normalize_missing_id_returns_none():
    s = create_industry_source()
    assert s._normalize({"publishTime": 1}) is None


def test_okx_normalize_seconds_publish_time_promoted_to_ms():
    s = create_industry_source()
    # 10 位秒级时间戳
    item = s._normalize({"contentId": "X", "publishTime": 1_700_000_000, "titleNew": "t"})
    assert item is not None
    assert item.publish_time == 1_700_000_000_000  # 被补成毫秒


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 响应外壳兼容（OKX 2026-04 改版 data→dict.contentDataList）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _make_fake_session(payload: dict):
    """构造一个最小可用的 aiohttp.ClientSession mock，返回固定 payload。"""
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = AsyncMock(return_value=payload)
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)

    session = MagicMock()
    session.get = MagicMock(return_value=resp)
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    return session


def test_okx_fetch_parses_new_shell_data_contentDataList():
    """OKX 新结构：{"code":"0","data":{"contentDataList":[...]}}，必须解析出条目。"""
    payload = {
        "code": "0",
        "data": {
            "contentDataList": [
                {
                    "contentId": "N1",
                    "publishTime": 1_700_000_000_000,
                    "titleNew": "BTC 暴涨",
                    "contentCnShort": "...",
                    "viewCount": 100,
                },
                {
                    "contentId": "N2",
                    "publishTime": 1_700_000_060_000,
                    "titleNew": "ETH 反弹",
                    "contentCnShort": "...",
                    "viewCount": 50,
                },
            ],
            "nextCursor": "xx",
        },
    }
    s = create_industry_source()
    with patch("sources.news.okx.aiohttp.ClientSession", return_value=_make_fake_session(payload)):
        items = asyncio.run(s.fetch())
    ids = [it.external_id for it in items]
    assert ids == ["N1", "N2"]


def test_okx_fetch_parses_legacy_shell_data_list():
    """旧结构：{"data":[...]} 仍须兼容。"""
    payload = {
        "code": "0",
        "data": [
            {"contentId": "L1", "publishTime": 1_700_000_000_000, "titleNew": "t"},
        ],
    }
    s = create_industry_source()
    with patch("sources.news.okx.aiohttp.ClientSession", return_value=_make_fake_session(payload)):
        items = asyncio.run(s.fetch())
    assert [it.external_id for it in items] == ["L1"]


def test_okx_fetch_unknown_shell_returns_empty_without_raise():
    """未知外壳：不抛异常、返回 0 条、仍打一条 warning 便于后续发现。"""
    payload = {"code": "0", "data": "<- str surprised us"}
    s = create_industry_source()
    with patch("sources.news.okx.aiohttp.ClientSession", return_value=_make_fake_session(payload)):
        items = asyncio.run(s.fetch())
    assert items == []


def test_heat_score_bounds():
    assert OkxTimelineSource._heat_score(0, 0, 0) == 0.0
    lo = OkxTimelineSource._heat_score(10, 0, 0)
    mid = OkxTimelineSource._heat_score(10_000, 100, 10)
    hi = OkxTimelineSource._heat_score(1_000_000, 10_000, 1_000)
    assert 0.0 <= lo < mid < hi <= 1.0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Registry 聚合 fetch_all
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_registry_register_and_get_all():
    register("a", _FakeSource("a", []))
    register("b", _FakeSource("b", []))
    assert {s.name for s in get_all()} == {"a", "b"}


def test_fetch_all_empty_registry():
    items = asyncio.run(fetch_all())
    assert items == []


def test_fetch_all_single_source_with_dedupe():
    items = [
        _mk_raw(external_id="1", title="T1", publish_time=1_000),
        _mk_raw(external_id="2", title="T2", publish_time=2_000),
        _mk_raw(external_id="2", title="T2", publish_time=2_000),  # 源内重复
    ]
    register("one", _FakeSource("one", items))
    out = asyncio.run(fetch_all())
    # 源内去重 + 跨源去重（标题分钟级）
    assert len(out) == 2
    ids = {it.external_id for it in out}
    assert ids == {"1", "2"}


def test_fetch_all_continues_on_source_failure():
    register("bad", _FakeSource("bad", [], raises=True))
    register("ok", _FakeSource("ok", [_mk_raw(external_id="k", title="K")]))
    out = asyncio.run(fetch_all())
    assert len(out) == 1
    assert out[0].external_id == "k"


def test_dedupe_cross_source_prefers_higher_reliability():
    low = _mk_raw(
        external_id="l", title="同一新闻", source_author="low",
        source_reliability=0.6, publish_time=1_700_000_000_000,
    )
    high = _mk_raw(
        external_id="h", title="同一新闻 ", source_author="high",
        source_reliability=0.9, publish_time=1_700_000_000_000 + 5_000,  # 同分钟
    )
    unique, dropped = deduplicate_cross_source([low, high])
    assert dropped == 1
    assert len(unique) == 1
    assert unique[0].external_id == "h"  # 高可信度保留


def test_dedupe_cross_source_keeps_non_overlapping_minutes():
    a = _mk_raw(external_id="a", title="T", publish_time=1_700_000_000_000)
    b = _mk_raw(external_id="b", title="T", publish_time=1_700_000_000_000 + 120_000)  # +2min
    unique, dropped = deduplicate_cross_source([a, b])
    assert dropped == 0
    assert len(unique) == 2


def test_news_source_filter_new_dedupe():
    src = _FakeSource("x", [])
    items = [
        _mk_raw(external_id="1"),
        _mk_raw(external_id="2"),
        _mk_raw(external_id="1"),  # 已见
    ]
    fresh = src.filter_new(items)
    assert len(fresh) == 2
    fresh2 = src.filter_new([_mk_raw(external_id="1"), _mk_raw(external_id="3")])
    assert [it.external_id for it in fresh2] == ["3"]
