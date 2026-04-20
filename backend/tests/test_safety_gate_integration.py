"""P0-h · SafetyGate 与 engine.get_source_health() 形状兼容集成测试

engine.get_source_health() 返回的是 list[dict]（见 engine.py L1787+），
其中有 SourceHealth.model_dump() 形式的 {name, status, last_success_ts, ...}
和自定义 dict（如 coinglass_daily_usage / market_readiness，名字不在
_KEY_SOURCES 中应被 G4 忽略）。

这个测试保证 G4 能正确处理这种混合 list。
"""
from __future__ import annotations

import time

from processors.safety_gate import evaluate_safety_gates, _g4_api_degradation


def _engine_shape(
    cg_status: str = "connected",
    bn_status: str = "connected",
    bbx_status: str = "connected",
    cg_last: int | None = None,
) -> list[dict]:
    now = int(time.time())
    cg_last_ts = cg_last if cg_last is not None else now
    return [
        {"name": "coinglass", "status": cg_status, "latency_ms": 120.0,
         "last_success_ts": cg_last_ts, "error_count": 0},
        {"name": "binance_futures", "status": bn_status, "latency_ms": 80.0,
         "last_success_ts": now, "error_count": 0},
        {"name": "coinglass_daily_usage", "status": "connected",
         "daily_requests": 100, "daily_limit": 1000, "usage_pct": 10.0,
         "latency_ms": 0},
        {"name": "bbx", "status": bbx_status,
         "cached_indices": 3, "last_success_ts": now, "error_count": 0},
        {"name": "market_readiness", "status": "connected",
         "coins": [{"coin": "BTC", "ticker_ready": True}]},
    ]


class TestG4WithEngineShape:
    def test_all_healthy_pass(self):
        sh = _engine_shape()
        status, reason = _g4_api_degradation(sh)
        assert status == "pass"

    def test_one_key_source_disconnected_stale_warn(self):
        sh = _engine_shape(cg_status="disconnected", cg_last=int(time.time()) - 7200)
        status, reason = _g4_api_degradation(sh)
        assert status == "warn"
        assert "coinglass" in reason

    def test_two_disconnected_block(self):
        now = int(time.time())
        sh = _engine_shape(
            cg_status="disconnected",
            cg_last=now - 7200,
            bn_status="disconnected",
        )
        # 修正 binance last_success_ts → 同样过期
        for s in sh:
            if s.get("name") == "binance_futures":
                s["last_success_ts"] = now - 7200
        status, reason = _g4_api_degradation(sh)
        assert status == "block"
        assert "coinglass" in reason and "binance_futures" in reason

    def test_ignored_custom_dicts(self):
        """coinglass_daily_usage / market_readiness 这些非 KEY_SOURCES 即便异常也不应触发 G4"""
        sh = _engine_shape()
        for s in sh:
            if s.get("name") in ("coinglass_daily_usage", "market_readiness"):
                s["status"] = "disconnected"
                s["last_success_ts"] = 0
        status, _ = _g4_api_degradation(sh)
        assert status == "pass"


class TestEvaluateEndToEnd:
    def test_pass_with_healthy_engine_sources(self):
        sh = _engine_shape()
        res = evaluate_safety_gates(
            coin="BTC",
            price=72000.0,
            atr_14=900.0,
            source_health=sh,
        )
        assert res.g4_api_degrade == "pass"
        assert not res.triggered

    def test_block_when_multiple_sources_down(self):
        now = int(time.time())
        sh = _engine_shape(
            cg_status="disconnected",
            cg_last=now - 7200,
            bn_status="disconnected",
        )
        for s in sh:
            if s.get("name") == "binance_futures":
                s["last_success_ts"] = now - 7200
        res = evaluate_safety_gates(
            coin="BTC",
            price=72000.0,
            atr_14=900.0,
            source_health=sh,
        )
        assert res.g4_api_degrade == "block"
        assert res.triggered is True
        assert "coinglass" in res.block_reason
