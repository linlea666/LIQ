"""D11 GeoRiskTracker + _derive_level + _label_for_level 单测"""

from __future__ import annotations

import time

import pytest

from models.news_event import MarketEventSignal
from processors.geo_risk_tracker import (
    GeoRiskTracker, _DEFAULT_TEMPLATES, _derive_level, _label_for_level,
    _severity_of, get_geo_risk_tracker, reset_geo_risk_tracker,
)


@pytest.fixture(autouse=True)
def _reset():
    reset_geo_risk_tracker()
    yield
    reset_geo_risk_tracker()


def _ev(
    *,
    summary: str = "",
    impact: int = 3,
    direction: str = "bearish",
    theme: str = "Middle_East_Iran",
    risk_type: str = "geopolitical",
    flip: bool = False,
    event_id: str = "",
) -> MarketEventSignal:
    return MarketEventSignal(
        event_id=event_id or f"e_{time.time_ns()}",
        ts=int(time.time()),
        target="macro",
        direction=direction,
        first_order_impact=summary,
        impact_score=impact,
        confidence=0.7,
        horizon="short",
        narrative_theme=theme,
        risk_type=risk_type,
        summary_cn=summary,
        flip_flop_warning=flip,
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 辅助纯函数
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_label_for_level_full_table():
    expected = [
        (0, "PEACE"), (1, "WATCHING"), (2, "TENSION"),
        (3, "CRISIS"), (4, "ESCALATION"), (5, "WAR"),
    ]
    for lvl, lbl in expected:
        assert _label_for_level(lvl)[0] == lbl


def test_severity_of():
    assert _severity_of(2, 3) == "escalation"
    assert _severity_of(3, 2) == "de-escalation"
    assert _severity_of(3, 3) == "stable"


def test_derive_level_absolute_keyword_overrides_current():
    ev = _ev(summary="某国宣布全面战争", impact=5)
    lvl = _derive_level(ev, _DEFAULT_TEMPLATES, current_level=1)
    assert lvl == 5


def test_derive_level_delta_keyword_adds_to_current():
    # 升级关键词
    ev = _ev(summary="冲突升级", impact=2)
    lvl = _derive_level(ev, _DEFAULT_TEMPLATES, current_level=2)
    assert lvl >= 3


def test_derive_level_ceasefire_reduces():
    ev = _ev(summary="ceasefire agreement reached", impact=2, direction="bullish")
    lvl = _derive_level(ev, _DEFAULT_TEMPLATES, current_level=3)
    assert lvl <= 2


def test_derive_level_impact_score_raises_floor():
    # 无关键词但 impact 极强
    ev = _ev(summary="某事件影响巨大", impact=5)
    lvl = _derive_level(ev, _DEFAULT_TEMPLATES, current_level=0)
    assert lvl >= 3


def test_derive_level_flip_flop_discount():
    # 同一强升级事件 + flip_flop → 降档
    no_flip = _derive_level(_ev(summary="冲突升级", impact=3, flip=False), _DEFAULT_TEMPLATES, current_level=2)
    with_flip = _derive_level(_ev(summary="冲突升级", impact=3, flip=True), _DEFAULT_TEMPLATES, current_level=2)
    assert with_flip <= no_flip


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ingest 基本流程
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_ingest_non_geopolitical_returns_none():
    tr = GeoRiskTracker()
    out = tr.ingest(_ev(risk_type="regulatory"))
    assert out is None
    assert tr.get_state("Middle_East_Iran") is None


def test_ingest_escalation_creates_event():
    tr = GeoRiskTracker()
    ev = tr.ingest(_ev(summary="外交召回大使", impact=2))   # absolute 2 TENSION
    assert ev is not None
    assert ev.severity == "escalation"
    assert ev.level_before == 0
    assert ev.level_after == 2
    s = tr.get_state("Middle_East_Iran")
    assert s.current_level == 2
    assert s.level_label == "TENSION"


def test_ingest_same_level_no_event():
    tr = GeoRiskTracker()
    tr.ingest(_ev(summary="召回大使外交 ", impact=2))
    ev2 = tr.ingest(_ev(summary="再次外交抗议 ", impact=2))
    # 同为 TENSION（2），预期无 event 返回
    assert ev2 is None


def test_ingest_flip_flop_counts():
    tr = GeoRiskTracker()
    # 升级 0 → 3
    tr.ingest(_ev(summary="sanctions announced 制裁", impact=3))
    # 降级 3 → 1 (ceasefire -1 * 2 = 1)
    tr.ingest(_ev(summary="ceasefire", impact=3, direction="bullish"))
    # 再升级 1 → 4 (airstrike 绝对=4)
    tr.ingest(_ev(summary="airstrike reported 空袭", impact=4))
    s = tr.get_state("Middle_East_Iran")
    assert s.flip_flop_count_24h >= 2
    assert s.flip_flop_warning is True


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Overview
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_overview_overall_is_max_level():
    tr = GeoRiskTracker()
    tr.ingest(_ev(summary="外交抗议", theme="Russia_Ukraine", impact=1))          # level 2
    tr.ingest(_ev(summary="airstrike reported 空袭", theme="Middle_East_Iran", impact=4))  # level 4
    ov = tr.get_overview()
    assert ov.overall_level == 4
    assert ov.overall_label == "ESCALATION"
    assert ov.suggest_safety_gate_block is True
    assert ov.suggest_position_cap_pct == 15.0
    assert len(ov.active_themes) == 2


def test_overview_peace_by_default():
    tr = GeoRiskTracker()
    ov = tr.get_overview()
    assert ov.overall_level == 0
    assert ov.overall_label == "PEACE"
    assert ov.suggest_safety_gate_block is False
    assert ov.suggest_position_cap_pct is None


def test_overview_24h_escalation_count():
    tr = GeoRiskTracker()
    tr.ingest(_ev(summary="sanctions 制裁", impact=3))       # 0 → 3 escal
    tr.ingest(_ev(summary="ceasefire 和谈", impact=2,
                   direction="bullish"))                      # 3 → 1 de-escal
    ov = tr.get_overview()
    assert ov.escalation_count_24h == 1
    assert ov.de_escalation_count_24h == 1


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Decay
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_decay_drops_one_level_after_24h():
    tr = GeoRiskTracker()
    tr.ingest(_ev(summary="制裁 sanctions", impact=3))  # level 3
    s = tr.get_state("Middle_East_Iran")
    s.latest_event_ts = int(time.time()) - 25 * 3600
    changed = tr.decay()
    assert changed == 1
    s2 = tr.get_state("Middle_East_Iran")
    assert s2.current_level == 2


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 单例
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_singleton_identity():
    a = get_geo_risk_tracker()
    b = get_geo_risk_tracker()
    assert a is b
