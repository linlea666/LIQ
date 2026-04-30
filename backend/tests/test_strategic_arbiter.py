"""PR-2 · Strategic Arbiter 回归测试。

覆盖：
  1. _coerce_* helpers 容忍式映射（合法 / 非法 / 异类型 / 越界）
  2. _payload_to_report 一致性约束（hard_stop → NO_TRADE / NO_TRADE 清空 plan /
     LONG_PLAN 缺 plan → OBSERVATION）
  3. _fallback_report：AI key 未配置时降级
  4. StrategicArbiter.analyze 全链路 mock：成功 / parse 失败 / API 重试耗尽
"""

from __future__ import annotations

import json

import pytest


# ────────────────────────────────────────────────────────────────────────────
# Helpers · 构造最小 AISnapshot
# ────────────────────────────────────────────────────────────────────────────

def _empty_snapshot(coin: str = "BTC", price: float = 65000):
    from ai.snapshot import build_ai_snapshot

    return build_ai_snapshot(
        coin=coin, price=price, high_24h=price * 1.02, low_24h=price * 0.98,
        atr=500, market_temp_score=50, pin_risk_level="low",
    )


# ────────────────────────────────────────────────────────────────────────────
# Coerce helpers
# ────────────────────────────────────────────────────────────────────────────

class TestCoerceHelpers:
    def test_decision_valid(self):
        from ai.strategic_arbiter import _coerce_decision
        assert _coerce_decision("LONG_PLAN") == "LONG_PLAN"
        assert _coerce_decision(" wait ") == "WAIT"
        assert _coerce_decision("long_plan") == "LONG_PLAN"

    def test_decision_invalid_fallback(self):
        from ai.strategic_arbiter import _coerce_decision
        assert _coerce_decision(None) == "WAIT"
        assert _coerce_decision("UNKNOWN") == "WAIT"
        assert _coerce_decision(123) == "WAIT"

    def test_horizon_valid(self):
        from ai.strategic_arbiter import _coerce_horizon
        assert _coerce_horizon("scalp") == "scalp"
        assert _coerce_horizon("SWING") == "swing"

    def test_horizon_invalid_fallback(self):
        from ai.strategic_arbiter import _coerce_horizon
        assert _coerce_horizon("longterm") == "intraday"

    def test_confidence_normalization(self):
        """confidence 接受 0-1（标准）或 0-100 整数（兼容 AI 走偏的情况）。"""
        from ai.strategic_arbiter import _coerce_confidence
        assert _coerce_confidence(0.65) == pytest.approx(0.65)
        assert _coerce_confidence(65) == pytest.approx(0.65)
        assert _coerce_confidence(150) == pytest.approx(1.0)
        assert _coerce_confidence(-0.1) == 0.0
        assert _coerce_confidence(None) == 0.0
        assert _coerce_confidence("not number") == 0.0

    def test_str_list_truncate(self):
        from ai.strategic_arbiter import _coerce_str_list
        out = _coerce_str_list(["a", "b", "c", "d"], max_len=2)
        assert out == ["a", "b"]
        out2 = _coerce_str_list(["x" * 300], max_len=5, item_max=10)
        assert len(out2[0]) == 10

    def test_evidence_list(self):
        from ai.strategic_arbiter import _coerce_evidence_list
        raw = [
            {"section_ref": "§7", "observation": "下方双源墙",
             "inference": "买盘强承接", "supports": "main", "weight": "high"},
            {"observation": ""},  # 空 observation 跳过
            {"section_ref": "§8", "observation": "上方磁铁",
             "supports": "contrarian", "weight": "medium"},
        ]
        out = _coerce_evidence_list(raw)
        assert len(out) == 2
        assert out[0].section_ref == "§7"
        assert out[0].supports == "main"
        assert out[1].supports == "contrarian"

    def test_trading_plan_returns_none_when_missing_required(self):
        from ai.strategic_arbiter import _coerce_trading_plan
        # 缺 setup_type
        assert _coerce_trading_plan({
            "entry_zone_low": 64500, "entry_zone_high": 64700,
            "hard_invalidation": "失守",
        }) is None
        # 缺 entry_zone
        assert _coerce_trading_plan({
            "setup_type": "回踩", "hard_invalidation": "失守",
        }) is None
        # 缺 hard_invalidation
        assert _coerce_trading_plan({
            "setup_type": "回踩", "entry_zone_low": 64500, "entry_zone_high": 64700,
        }) is None

    def test_trading_plan_swap_inverted_zone(self):
        """entry_zone_low > entry_zone_high 时自动 swap。"""
        from ai.strategic_arbiter import _coerce_trading_plan
        plan = _coerce_trading_plan({
            "setup_type": "回踩",
            "entry_zone_low": 65000,
            "entry_zone_high": 64500,
            "hard_invalidation": "64200 失守",
        })
        assert plan is not None
        assert plan.entry_zone_low == 64500
        assert plan.entry_zone_high == 65000


# ────────────────────────────────────────────────────────────────────────────
# 一致性约束（_payload_to_report 核心）
# ────────────────────────────────────────────────────────────────────────────

class TestPayloadConsistency:
    def test_hard_stop_forces_no_trade(self):
        """hard_stop_triggered=true 即使 AI 给 LONG_PLAN，也强制 NO_TRADE。"""
        from ai.strategic_arbiter import _payload_to_report
        from models.common_prompt_debug import PromptDebug

        snap = _empty_snapshot()
        debug = PromptDebug(system="", user="", chars=0, model="test")
        payload = {
            "decision": "LONG_PLAN",
            "primary_plan": {
                "setup_type": "回踩",
                "entry_zone_low": 64500,
                "entry_zone_high": 64700,
                "hard_invalidation": "64200 失守",
            },
            "data_self_check": {"hard_stop_triggered": True},
        }
        report = _payload_to_report(payload, snap, debug)
        assert report.decision == "NO_TRADE"
        assert report.primary_plan is None  # NO_TRADE 强制清空 plan

    def test_no_trade_clears_plans(self):
        from ai.strategic_arbiter import _payload_to_report
        from models.common_prompt_debug import PromptDebug

        snap = _empty_snapshot()
        debug = PromptDebug(system="", user="", chars=0, model="test")
        payload = {
            "decision": "NO_TRADE",
            "primary_plan": {
                "setup_type": "回踩",
                "entry_zone_low": 64500,
                "entry_zone_high": 64700,
                "hard_invalidation": "64200 失守",
            },
        }
        report = _payload_to_report(payload, snap, debug)
        assert report.decision == "NO_TRADE"
        assert report.primary_plan is None
        assert report.alternative_plan is None

    def test_long_plan_without_plan_degrades_to_observation(self):
        """LONG_PLAN 但 primary_plan 缺失关键字段 → 降级为 LONG_OBSERVATION。"""
        from ai.strategic_arbiter import _payload_to_report
        from models.common_prompt_debug import PromptDebug

        snap = _empty_snapshot()
        debug = PromptDebug(system="", user="", chars=0, model="test")
        payload = {
            "decision": "LONG_PLAN",
            # primary_plan 缺 hard_invalidation → _coerce_trading_plan 返回 None
            "primary_plan": {
                "setup_type": "回踩",
                "entry_zone_low": 64500,
                "entry_zone_high": 64700,
            },
        }
        report = _payload_to_report(payload, snap, debug)
        assert report.decision == "LONG_OBSERVATION"
        assert report.primary_plan is None

    def test_short_plan_without_plan_degrades(self):
        from ai.strategic_arbiter import _payload_to_report
        from models.common_prompt_debug import PromptDebug

        snap = _empty_snapshot()
        debug = PromptDebug(system="", user="", chars=0, model="test")
        report = _payload_to_report(
            {"decision": "SHORT_PLAN"}, snap, debug,
        )
        assert report.decision == "SHORT_OBSERVATION"

    def test_full_payload_roundtrip(self):
        """完整合法 payload 完整保留所有字段。"""
        from ai.strategic_arbiter import _payload_to_report
        from models.common_prompt_debug import PromptDebug

        snap = _empty_snapshot()
        debug = PromptDebug(system="", user="", chars=0, model="test")
        payload = {
            "decision": "LONG_PLAN",
            "horizon": "intraday",
            "bias": "bullish",
            "confidence": 0.65,
            "confidence_rationale": "结构强但 OI 拥挤偏高",
            "market_phase": "趋势确认",
            "cycle_position": "中期",
            "current_zone_assessment": {
                "zone_id": "z1",
                "role": "spot_defense",
                "nearest_critical_above_pct": 0.45,
                "nearest_critical_below_pct": 0.30,
                "key_conflict": "现货墙强 vs 下方清算磁铁近",
            },
            "structure_analysis": "§4 当前 zone 为 spot_defense",
            "flow_analysis": "§9 OI 上升 + CVD 多头",
            "macro_context": "§11 宏观偏中性",
            "primary_plan": {
                "setup_type": "回踩支撑",
                "entry_zone_low": 64500,
                "entry_zone_high": 64700,
                "trigger_conditions": ["现货墙未撤", "CVD 5m 转正"],
                "soft_invalidation": "60min 内不回到入场区",
                "hard_invalidation": "64200 收盘失守",
                "targets": [
                    {"price": 66000, "reason": "上方 7d 清算簇", "rr": 2.5},
                ],
                "cancel_conditions": ["现货墙双源被吃 50%"],
                "risk_unit": "按 1R 计算",
                "leverage_risk_level": "medium",
                "position_sizing_note": "若 hard_inv 距离 1.2% 且 1R/笔，仓位 ≈ R / 1.2%",
            },
            "alternative_scenario": {
                "description": "若现货墙被吃 → 转下行",
                "probability_pct": 30,
                "trigger": "现货墙双源被吃 50%",
            },
            "evidence_matrix": {
                "long_evidence": [
                    {"section_ref": "§7", "observation": "下方双源墙",
                     "inference": "买盘承接强", "supports": "main", "weight": "high"},
                ],
                "short_evidence": [
                    {"section_ref": "§8", "observation": "下方磁铁近",
                     "supports": "contrarian", "weight": "medium"},
                ],
                "wait_evidence": [],
                "contradictions": ["现货墙强 vs 清算磁铁近"],
            },
            "invalidation_conditions": ["64200 失守", "现货墙撤"],
            "data_self_check": {
                "missing": [],
                "stale": [],
                "provisional": ["price.recent_bars_1h"],
                "hard_stop_triggered": False,
                "confidence_penalty_reason": "1h bar 未收盘 → 降权 5%",
            },
            "macro_modifier_note": "宏观偏中性 → 无修正",
            "data_quality": "ok",
        }
        report = _payload_to_report(payload, snap, debug)
        assert report.decision == "LONG_PLAN"
        assert report.confidence == pytest.approx(0.65)
        assert report.primary_plan is not None
        assert report.primary_plan.targets[0].rr == 2.5
        assert len(report.evidence_matrix.long_evidence) == 1
        assert report.evidence_matrix.contradictions == ["现货墙强 vs 清算磁铁近"]
        assert report.alternative_scenario.probability_pct == 30
        assert report.data_self_check.provisional == ["price.recent_bars_1h"]


# ────────────────────────────────────────────────────────────────────────────
# Fallback
# ────────────────────────────────────────────────────────────────────────────

class TestFallback:
    def test_fallback_no_client(self):
        """_fallback_report 在 AI 不可用时构造合法降级报告。"""
        from ai.strategic_arbiter import _fallback_report

        snap = _empty_snapshot()
        report = _fallback_report(snap, reason="AI key not configured")
        assert report.decision == "NO_TRADE"
        assert report.data_self_check.hard_stop_triggered is True
        assert report.data_quality == "insufficient"
        assert "AI key" in report.confidence_rationale


# ────────────────────────────────────────────────────────────────────────────
# Arbiter.analyze · mock LLM client
# ────────────────────────────────────────────────────────────────────────────

class _MockChoice:
    def __init__(self, content: str, reasoning: str = ""):
        class _Msg:
            pass
        self.message = _Msg()
        self.message.content = content
        self.message.reasoning_content = reasoning


class _MockUsage:
    prompt_tokens = 1500
    completion_tokens = 800

    def __init__(self):
        class _D:
            reasoning_tokens = 0
        self.completion_tokens_details = _D()


class _MockResponse:
    def __init__(self, content: str):
        self.choices = [_MockChoice(content)]
        self.usage = _MockUsage()


class _MockClient:
    """异步 OpenAI 客户端 mock。"""

    def __init__(self, *, response_content: str = "", raise_exc: Exception | None = None):
        self._content = response_content
        self._raise = raise_exc
        self.calls = 0

        class _Chat:
            class _Completions:
                async def create(_self, **kwargs):
                    self.calls += 1
                    if self._raise is not None:
                        raise self._raise
                    return _MockResponse(self._content)

            completions = _Completions()

        self.chat = _Chat()


class TestArbiterAnalyze:
    @pytest.mark.asyncio
    async def test_analyze_success(self, monkeypatch):
        from ai.strategic_arbiter import StrategicArbiter

        snap = _empty_snapshot()
        payload = {
            "decision": "WAIT",
            "horizon": "intraday",
            "bias": "neutral",
            "confidence": 0.5,
            "no_trade_conditions": ["数据冲突未明朗"],
            "evidence_matrix": {
                "wait_evidence": [
                    {"section_ref": "§13", "observation": "多空均势",
                     "supports": "neutral", "weight": "medium"},
                ],
                "contradictions": ["现货墙强 vs 清算磁铁近"],
            },
            "data_quality": "ok",
        }
        raw_resp = f"```json\n{json.dumps(payload)}\n```"

        arbiter = StrategicArbiter()
        # 强制注入 mock client（实际生产环境通过 settings.ai.api_key 触发）
        arbiter._client = _MockClient(response_content=raw_resp)
        arbiter._max_retries = 2

        report = await arbiter.analyze(snap)
        assert report.decision == "WAIT"
        assert report.confidence == pytest.approx(0.5)
        assert report.data_quality == "ok"
        assert report.prompt_debug is not None
        assert report.prompt_debug.parse_ok is True
        assert report.prompt_debug.tokens_prompt == 1500

    @pytest.mark.asyncio
    async def test_analyze_parse_failure(self):
        from ai.strategic_arbiter import StrategicArbiter

        snap = _empty_snapshot()
        arbiter = StrategicArbiter()
        # AI 返回完全不是 JSON
        arbiter._client = _MockClient(response_content="<<<garbled response>>>")
        arbiter._max_retries = 1

        report = await arbiter.analyze(snap)
        assert report.decision == "NO_TRADE"
        assert report.data_self_check.hard_stop_triggered is True
        assert report.prompt_debug is not None
        assert report.prompt_debug.parse_ok is False
        assert "json_extract" in (report.prompt_debug.parse_error or "")

    @pytest.mark.asyncio
    async def test_analyze_api_retry_exhausted(self):
        from ai.strategic_arbiter import StrategicArbiter

        snap = _empty_snapshot()
        arbiter = StrategicArbiter()
        arbiter._client = _MockClient(raise_exc=RuntimeError("network down"))
        arbiter._max_retries = 2

        report = await arbiter.analyze(snap)
        assert report.decision == "NO_TRADE"
        assert report.prompt_debug is not None
        assert report.prompt_debug.parse_ok is False
        assert "api_error" in (report.prompt_debug.parse_error or "")
        # 验证重试次数
        assert arbiter._client.calls == 2

    @pytest.mark.asyncio
    async def test_analyze_no_client_returns_fallback(self):
        from ai.strategic_arbiter import StrategicArbiter

        snap = _empty_snapshot()
        arbiter = StrategicArbiter()
        arbiter._client = None  # 模拟 api_key 未配置

        report = await arbiter.analyze(snap)
        assert report.decision == "NO_TRADE"
        assert report.prompt_debug is not None
        assert report.prompt_debug.parse_error == "ai_client_unavailable"
