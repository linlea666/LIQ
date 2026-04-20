"""Unit tests · signal_bus.adapt_news_event / adapt_geo_risk_event（P1.2b）"""

from __future__ import annotations

import time

import pytest

from models.geo_risk import GeoRiskEvent
from models.news_event import AssetImpact, MarketEventSignal
from processors.signal_bus import adapt_geo_risk_event, adapt_news_event


def _ev(**kw) -> MarketEventSignal:
    defaults = {
        "event_id": "e1",
        "ts": int(time.time()),
        "direction": "bullish",
        "tier": "major",
        "impact_score": 3,
        "confidence": 0.8,
        "source_credibility": 0.8,
        "risk_type": "macro_economic",
        "narrative_theme": "Fed_Rate_Policy",
        "summary_cn": "美联储降息 25bp",
        "impact_on_assets": [AssetImpact(asset="BTC", direction="bullish", magnitude="high")],
    }
    defaults.update(kw)
    return MarketEventSignal(**defaults)


def _ge(**kw) -> GeoRiskEvent:
    defaults = {
        "event_id": "g1",
        "theme_id": "Middle_East_Iran",
        "ts": int(time.time()),
        "level_before": 2,
        "level_after": 4,
        "severity": "escalation",
        "estimated_direction": "bearish",
        "estimated_crypto_reaction_pct": -4.0,
        "confidence": 0.85,
        "is_blackswan": False,
    }
    defaults.update(kw)
    return GeoRiskEvent(**defaults)


class TestAdaptNewsEvent:
    def test_basic_news_produces_wait_action_signal(self):
        cs_list = adapt_news_event(_ev())
        assert len(cs_list) == 1
        cs = cs_list[0]
        # 保守策略：永不生成交易 action
        assert cs.action == "wait"
        assert cs.source == "news_event.ai"
        assert cs.source_id == "e1"
        assert cs.direction == "bullish"
        # major + confidence 0.8 → B 档
        assert cs.confidence == "B"
        assert cs.score > 0
        assert cs.provenance.get("coin") == "BTC"
        assert cs.provenance.get("narrative_theme") == "Fed_Rate_Policy"

    def test_none_input_returns_empty_list(self):
        assert adapt_news_event(None) == []

    def test_neutral_non_blackswan_dropped(self):
        cs_list = adapt_news_event(_ev(direction="neutral", tier="normal"))
        assert cs_list == []

    def test_minor_weak_impact_dropped(self):
        cs_list = adapt_news_event(_ev(tier="minor", impact_score=1))
        assert cs_list == []

    def test_blackswan_maps_to_a_confidence_and_high_score(self):
        cs_list = adapt_news_event(_ev(tier="blackswan", impact_score=5, confidence=0.9))
        assert len(cs_list) == 1
        cs = cs_list[0]
        assert cs.confidence == "A"
        assert cs.score >= 60  # base 100 * 0.9

    def test_flip_flop_applies_weight_discount(self):
        # 非黑天鹅的 flip-flop → score 打 0.7 折
        base = adapt_news_event(_ev(tier="major", impact_score=4, confidence=1.0))[0].score
        flipped = adapt_news_event(_ev(tier="major", impact_score=4, confidence=1.0, flip_flop_warning=True))[0]
        assert flipped.score < base
        assert any("反复" in w for w in flipped.warnings)

    def test_blackswan_flip_flop_not_discounted(self):
        bs = adapt_news_event(_ev(tier="blackswan", impact_score=5, confidence=1.0))[0]
        bs_flip = adapt_news_event(_ev(tier="blackswan", impact_score=5, confidence=1.0, flip_flop_warning=True))[0]
        # 黑天鹅仍会保留 base >= 70 并乘 confidence=1.0
        assert bs.score == bs_flip.score

    def test_target_coins_override_impact_on_assets(self):
        cs_list = adapt_news_event(_ev(), target_coins=["BTC", "ETH"])
        coins = {cs.provenance["coin"] for cs in cs_list}
        assert coins == {"BTC", "ETH"}

    def test_eth_only_impact_routes_to_eth(self):
        ev = _ev(impact_on_assets=[AssetImpact(asset="ETH", direction="bullish", magnitude="high")])
        cs_list = adapt_news_event(ev)
        assert len(cs_list) == 1
        assert cs_list[0].provenance["coin"] == "ETH"


class TestAdaptGeoRiskEvent:
    def test_basic_geo_produces_wait_bearish_signal(self):
        cs_list = adapt_geo_risk_event(_ge())
        assert len(cs_list) == 1
        cs = cs_list[0]
        assert cs.action == "wait"
        assert cs.source == "geo_risk.tracker"
        assert cs.direction == "bearish"
        assert cs.confidence == "A"  # escalation 或 level_after >= 4
        assert cs.provenance.get("theme_id") == "Middle_East_Iran"

    def test_none_input_returns_empty(self):
        assert adapt_geo_risk_event(None) == []

    def test_no_level_change_non_blackswan_dropped(self):
        ge = _ge(level_before=3, level_after=3, severity="stable")
        assert adapt_geo_risk_event(ge) == []

    def test_blackswan_produces_warnings_and_high_score(self):
        ge = _ge(level_before=0, level_after=5, severity="escalation", is_blackswan=True)
        cs_list = adapt_geo_risk_event(ge)
        assert len(cs_list) == 1
        cs = cs_list[0]
        assert any("黑天鹅" in w for w in cs.warnings)
        assert cs.score >= 70  # base max 85 * confidence

    def test_de_escalation_flips_direction_to_bullish(self):
        ge = _ge(
            level_before=3, level_after=1,
            severity="de-escalation",
            estimated_direction="neutral",
        )
        cs = adapt_geo_risk_event(ge)[0]
        assert cs.direction == "bullish"

    def test_flip_flop_discounts_score(self):
        a = adapt_geo_risk_event(_ge(confidence=1.0))[0]
        b = adapt_geo_risk_event(_ge(confidence=1.0, flip_flop_warning=True))[0]
        assert b.score < a.score
        assert any("反复" in w for w in b.warnings)

    def test_level_4_adds_safetygate_warning(self):
        cs = adapt_geo_risk_event(_ge(level_after=4))[0]
        assert any("SafetyGate" in w for w in cs.warnings)

    def test_multi_coin_output(self):
        cs_list = adapt_geo_risk_event(_ge(), target_coins=["BTC", "ETH"])
        assert len(cs_list) == 2
        coins = {cs.provenance["coin"] for cs in cs_list}
        assert coins == {"BTC", "ETH"}
