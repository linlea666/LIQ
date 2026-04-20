"""D08 Layer 1 规则过滤单测"""

from __future__ import annotations

import time

import pytest

from models.news_event import RawNewsItem
from processors.news_filter import FilterStats, filter_news_layer1


def _mk(**kw) -> RawNewsItem:
    base = dict(
        source_type="okx",
        source_author="OKX",
        source_reliability=0.85,
        external_id=f"id_{time.time_ns()}",
        publish_time=int(time.time() * 1000),
        title="某 ETF 通过 SEC 审批",
        content="这是一条正常的新闻内容。",
        view_count=5000,
    )
    base.update(kw)
    return RawNewsItem(**base)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 黑名单
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_blacklist_ad_dropped():
    items = [_mk(title="扫码领取福利 VIP 群", content="加群一起赚", source_reliability=0.5, view_count=10)]
    kept, _, stats = filter_news_layer1(items)
    assert kept == []
    assert stats.dropped_by_blacklist == 1


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 白名单
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_whitelist_keyword_forces_keep_low_heat():
    items = [_mk(title="BTC 突发消息", content="内容", view_count=1, source_reliability=0.5)]
    kept, tier_map, _ = filter_news_layer1(items)
    assert len(kept) == 1
    assert kept[0].external_id in tier_map


def test_whitelist_author_forces_keep():
    items = [_mk(source_author="BlockBeats", title="低热低长度标题", content="短", view_count=0, source_reliability=0.3)]
    kept, _, _ = filter_news_layer1(items)
    assert len(kept) == 1


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 热度门槛
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_low_heat_low_reliability_dropped():
    items = [_mk(title="某主流币行情播报", content="内容", view_count=10, source_reliability=0.5)]
    kept, _, stats = filter_news_layer1(items)
    assert kept == []
    assert stats.dropped_by_heat == 1


def test_low_heat_but_high_reliability_kept_if_relevant():
    items = [
        _mk(
            title="SEC 声明影响深远",
            content="详细文本",
            view_count=10,
            source_reliability=0.9,  # 高可信源免除热度门槛
        )
    ]
    kept, _, _ = filter_news_layer1(items)
    assert len(kept) == 1


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 标题长度 & 去重
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_title_too_short_dropped():
    items = [_mk(title="喵", content="内容")]
    kept, _, stats = filter_news_layer1(items)
    assert kept == []
    assert stats.dropped_by_length == 1


def test_title_dedupe_same_batch():
    items = [
        _mk(external_id="a", title="美联储主席发表讲话"),
        _mk(external_id="b", title="美联储主席发表讲话  "),  # 仅多了空格
    ]
    kept, _, _ = filter_news_layer1(items)
    assert len(kept) == 1


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# tier 分档
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_tier_blackswan_on_war_keyword():
    items = [_mk(external_id="bs", title="某国全面战争爆发", content="详细")]
    _, tier_map, stats = filter_news_layer1(items)
    assert tier_map["bs"] == "blackswan"
    assert stats.blackswan == 1


def test_tier_major_on_fomc():
    items = [_mk(external_id="mj", title="FOMC 会议纪要公布")]
    _, tier_map, stats = filter_news_layer1(items)
    assert tier_map["mj"] == "major"
    assert stats.major == 1


def test_tier_geopolitical_forced_major():
    items = [_mk(external_id="geo", title="某国与邻国外交 ceasefire 信号", content="继续观察")]
    _, tier_map, _ = filter_news_layer1(items)
    assert tier_map["geo"] == "major"


def test_tier_minor_for_low_heat_market_report():
    items = [_mk(
        external_id="m1",
        title="BTC 行情解读：K线分析",
        content="基础教程扫盲",
        view_count=200,
        source_reliability=0.6,
    )]
    kept, tier_map, _ = filter_news_layer1(items)
    # 含 BTC 白名单 → 保留，但命中 minor_keywords（行情解读/K线/教程）
    assert len(kept) == 1
    assert tier_map["m1"] == "minor"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 非加密相关
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_non_crypto_dropped():
    items = [_mk(title="娱乐圈八卦新闻", content="无关紧要", view_count=50, source_reliability=0.5)]
    kept, _, stats = filter_news_layer1(items)
    assert kept == []
    # 低热度 + 非相关，应该走 heat 或 non_crypto，统计项 any 命中即可
    assert (stats.dropped_by_heat + stats.dropped_by_length) >= 1


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# stats 汇总
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_stats_pass_rate_and_top_drop_reasons():
    items = [
        _mk(external_id="k1", title="FOMC 会议纪要"),             # keep
        _mk(external_id="d1", title="扫码领取福利 群", view_count=10, source_reliability=0.5),  # blacklist
        _mk(external_id="d2", title="扫码入群", view_count=10, source_reliability=0.5),         # blacklist
    ]
    kept, _, stats = filter_news_layer1(items)
    assert len(kept) == 1
    assert stats.input_count == 3
    assert stats.kept_count == 1
    assert 0.0 < stats.pass_rate < 1.0
    assert stats.top_drop_reasons  # non-empty
    assert stats.top_drop_reasons[0][0].startswith("blacklist")
