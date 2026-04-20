"""L5 SafetyGate 5 道护栏单测（D03）"""

from __future__ import annotations

import time

import pytest

from models.geo_risk import GeoRiskOverview
from processors.safety_gate import (
    _g1_extreme_volatility, _g3_liquidation_chaos,
    _g4_api_degradation, _g5_blackswan_geo,
    aggregate_to_result, evaluate_safety_gates,
)


class TestG1ExtremeVolatility:
    def test_percentile_block(self):
        status, reason = _g1_extreme_volatility(atr_pct_percentile=96)
        assert status == "block"
        assert "极端" in reason

    def test_percentile_warn(self):
        status, _ = _g1_extreme_volatility(atr_pct_percentile=87)
        assert status == "warn"

    def test_hard_atr_block(self):
        # ATR% = 7% > 6% → block
        status, _ = _g1_extreme_volatility(price=100_000, atr_14=7_000)
        assert status == "block"

    def test_hard_atr_warn(self):
        status, _ = _g1_extreme_volatility(price=100_000, atr_14=4_500)
        assert status == "warn"

    def test_hour_change_block(self):
        status, _ = _g1_extreme_volatility(hour_change_pct=-6.2)
        assert status == "block"

    def test_normal_pass(self):
        status, _ = _g1_extreme_volatility(price=100_000, atr_14=2_000)
        assert status == "pass"


class TestG3LiquidationChaos:
    def test_ratio_block(self):
        status, _ = _g3_liquidation_chaos(
            liq_24h_total_usd=3e9, liq_7d_avg_usd=500e6,
        )
        assert status == "block"

    def test_ratio_warn(self):
        status, _ = _g3_liquidation_chaos(
            liq_24h_total_usd=2e9, liq_7d_avg_usd=500e6,
        )
        assert status == "warn"

    def test_hard_block(self):
        status, _ = _g3_liquidation_chaos(liq_24h_total_usd=6e9)
        assert status == "block"

    def test_normal_pass(self):
        status, _ = _g3_liquidation_chaos(liq_24h_total_usd=300e6)
        assert status == "pass"

    def test_empty_input(self):
        status, _ = _g3_liquidation_chaos()
        assert status == "pass"


class TestG4ApiDegradation:
    def test_single_source_stale_warn(self):
        now = int(time.time())
        health = [{
            "name": "coinglass", "status": "disconnected",
            "last_success_ts": now - 7200,
        }]
        status, _ = _g4_api_degradation(health)
        assert status == "warn"

    def test_multi_source_block(self):
        now = int(time.time())
        health = [
            {"name": "coinglass", "status": "disconnected", "last_success_ts": now - 7200},
            {"name": "binance_futures", "status": "disconnected", "last_success_ts": now - 7200},
        ]
        status, _ = _g4_api_degradation(health)
        assert status == "block"

    def test_healthy_pass(self):
        now = int(time.time())
        health = [
            {"name": "coinglass", "status": "connected", "last_success_ts": now - 30},
        ]
        status, _ = _g4_api_degradation(health)
        assert status == "pass"

    def test_empty_pass(self):
        status, _ = _g4_api_degradation(None)
        assert status == "pass"


class TestG5BlackswanGeo:
    def test_war_block(self):
        geo = GeoRiskOverview(ts=int(time.time()), overall_level=5)
        status, _ = _g5_blackswan_geo(geo)
        assert status == "block"

    def test_escalation_block(self):
        geo = GeoRiskOverview(ts=int(time.time()), overall_level=4)
        status, _ = _g5_blackswan_geo(geo)
        assert status == "block"

    def test_crisis_warn(self):
        geo = GeoRiskOverview(ts=int(time.time()), overall_level=3)
        status, _ = _g5_blackswan_geo(geo)
        assert status == "warn"

    def test_peace_pass(self):
        geo = GeoRiskOverview(ts=int(time.time()), overall_level=0)
        status, _ = _g5_blackswan_geo(geo)
        assert status == "pass"

    def test_blackswan_override(self):
        geo = GeoRiskOverview(ts=int(time.time()), overall_level=1, has_blackswan_24h=True)
        status, _ = _g5_blackswan_geo(geo)
        assert status == "block"

    def test_none_pass(self):
        status, _ = _g5_blackswan_geo(None)
        assert status == "pass"


class TestAggregation:
    def test_all_pass_not_triggered(self):
        res = aggregate_to_result({
            "g1": ("pass", ""), "g2": ("pass", ""), "g3": ("pass", ""),
            "g4": ("pass", ""), "g5": ("pass", ""),
        })
        assert res.triggered is False
        assert res.block_reason == ""

    def test_first_block_wins(self):
        res = aggregate_to_result({
            "g1": ("warn", "w1"), "g2": ("block", "b2"), "g3": ("pass", ""),
            "g4": ("block", "b4"), "g5": ("pass", ""),
        })
        assert res.triggered is True
        # block_reason 应取第一个 block（dict 插入顺序：g2 先于 g4）
        assert res.block_reason == "b2"
        assert "b2" in res.warnings
        assert "w1" in res.warnings


class TestEvaluateEndToEnd:
    def test_normal_case_not_triggered(self):
        res = evaluate_safety_gates(
            coin="BTC", price=100_000, atr_14=2_000,
            liq_24h_total_usd=300e6,
        )
        assert res.triggered is False
        assert res.g1_extreme_vol == "pass"
        assert res.g5_blackswan == "pass"

    def test_extreme_vol_blocks(self):
        res = evaluate_safety_gates(
            coin="BTC", price=100_000, atr_14=7_000,
        )
        assert res.triggered is True
        assert res.g1_extreme_vol == "block"

    def test_geo_blocks_independently(self):
        geo = GeoRiskOverview(ts=int(time.time()), overall_level=5)
        res = evaluate_safety_gates(
            coin="BTC", price=100_000, atr_14=2_000,
            geo_overview=geo,
        )
        assert res.triggered is True
        assert res.g5_blackswan == "block"
        # G1 仍然 pass
        assert res.g1_extreme_vol == "pass"
