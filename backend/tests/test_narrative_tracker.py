"""D10 NarrativeTracker + detect_flip_flop 单测"""

from __future__ import annotations

import time

import pytest

from models.news_event import MarketEventSignal
from processors.narrative_tracker import (
    NarrativeTracker, detect_flip_flop, get_narrative_tracker,
    reset_narrative_tracker,
)


@pytest.fixture(autouse=True)
def _reset():
    reset_narrative_tracker()
    yield
    reset_narrative_tracker()


def _ev(
    theme: str = "Middle_East_Iran",
    direction: str = "bearish",
    impact: int = 3,
    *,
    event_id: str = "",
    risk_type: str = "geopolitical",
    summary: str = "",
) -> MarketEventSignal:
    return MarketEventSignal(
        event_id=event_id or f"e_{time.time_ns()}",
        ts=int(time.time()),
        target="macro",
        direction=direction,
        impact_score=impact,
        confidence=0.7,
        horizon="short",
        narrative_theme=theme,
        risk_type=risk_type,
        summary_cn=summary,
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 单例
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_singleton_identity():
    a = get_narrative_tracker()
    b = get_narrative_tracker()
    assert a is b


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ingest 基础
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_ingest_creates_theme():
    tr = NarrativeTracker()
    theme = tr.ingest(_ev(theme="Fed_Rate", direction="bullish", impact=2))
    assert theme.theme_id == "Fed_Rate"
    assert theme.active is True
    assert theme.event_count_24h == 1
    assert theme.latest_event_direction == "bullish"
    assert theme.current_direction_bias == "bullish"
    assert theme.current_intensity >= 1


def test_ingest_multiple_events_increments_counts():
    tr = NarrativeTracker()
    for _ in range(3):
        tr.ingest(_ev(theme="Fed_Rate", direction="bullish", impact=2))
    t = tr.get("Fed_Rate")
    assert t is not None
    assert t.event_count_24h == 3
    assert t.trend == "active"  # >=3 → active


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# flip-flop 检测（ingest 内部）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_flip_flop_increments_on_opposite_direction():
    tr = NarrativeTracker()
    tr.ingest(_ev(direction="bearish"))
    tr.ingest(_ev(direction="bullish"))   # flip 1
    tr.ingest(_ev(direction="bearish"))   # flip 2
    t = tr.get("Middle_East_Iran")
    assert t is not None
    assert t.flip_flop_count_24h >= 2
    assert t.flip_flop_count_7d >= 2


def test_neutral_does_not_count_as_flip():
    tr = NarrativeTracker()
    tr.ingest(_ev(direction="bearish"))
    tr.ingest(_ev(direction="neutral"))
    tr.ingest(_ev(direction="neutral"))
    t = tr.get("Middle_East_Iran")
    assert t.flip_flop_count_24h == 0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# detect_flip_flop 独立函数
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_detect_flip_flop_basic():
    # bullish / bearish / bullish / bearish → 3 switches → True
    assert detect_flip_flop("x", "bearish", ["bullish", "bearish", "bullish"]) is True


def test_detect_flip_flop_not_enough_switches():
    assert detect_flip_flop("x", "bullish", ["bullish", "bullish"]) is False


def test_detect_flip_flop_neutral_ignored():
    assert detect_flip_flop("x", "bearish", ["bullish", "neutral", "neutral"]) is False  # 仅 1 switch


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 查询
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_get_active_sorted_by_intensity():
    tr = NarrativeTracker()
    for _ in range(3):
        tr.ingest(_ev(theme="A", direction="bearish", impact=5))
    tr.ingest(_ev(theme="B", direction="bullish", impact=1))
    active = tr.get_active(limit=5)
    assert active[0].theme_id == "A"
    assert active[-1].theme_id == "B"


def test_get_recent_direction_returns_last_5():
    tr = NarrativeTracker()
    for d in ["bearish", "bullish", "neutral", "bearish", "bullish", "bearish", "bullish"]:
        tr.ingest(_ev(direction=d))
    recent = tr.get_recent_direction_by_theme("Middle_East_Iran")
    assert len(recent) == 5
    assert recent[-1] == "bullish"


def test_get_active_briefs_shape():
    tr = NarrativeTracker()
    tr.ingest(_ev(theme="A", direction="bearish", impact=3, summary="测试摘要"))
    briefs = tr.get_active_briefs()
    assert len(briefs) == 1
    assert briefs[0].theme_id == "A"
    assert briefs[0].current_direction_bias == "bearish"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 状态机
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_decay_marks_inactive_after_window():
    tr = NarrativeTracker()
    tr.ingest(_ev(theme="Old"))
    t = tr.get("Old")
    t.last_seen_ts = int(time.time()) - 72 * 3600  # 故意调老
    changed = tr.decay()
    assert changed >= 1
    assert t.active is False
    assert t.trend in {"fading", "dormant"}


def test_stats_summary():
    tr = NarrativeTracker()
    tr.ingest(_ev(theme="A", direction="bearish"))
    tr.ingest(_ev(theme="A", direction="bullish"))
    s = tr.stats()
    assert s["total"] == 1
    assert s["active_count"] == 1
    assert s["flip_flop_themes_count"] == 1


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 容量
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_max_themes_evicts_oldest():
    tr = NarrativeTracker(max_themes=2)
    tr.ingest(_ev(theme="A", event_id="ea"))
    time.sleep(0.01)
    tr.ingest(_ev(theme="B", event_id="eb"))
    time.sleep(0.01)
    tr.ingest(_ev(theme="C", event_id="ec"))
    ids = set(tr.get_all_ids())
    assert "A" not in ids  # 最旧的被挤掉
    assert {"B", "C"}.issubset(ids)
