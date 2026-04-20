"""Phase 2 · TE AI Interpreter 单元测试

覆盖：
    1. 指纹 _fingerprint：判定性字段变化 → 指纹变；毛刺（时间/age）不变 → 指纹同
    2. _extract_json：支持纯 JSON / ```json 包裹 / 尾随文本
    3. _parse_ai_json：字段白名单 + 越界 confidence clamp + 非 list traps 兼容
    4. 缓存：首次调用落缓存；再次同指纹直接 cache_hit；force=True 绕过
    5. AI 未配置时返回 error 兜底
    6. Shadow log 写入 / stats
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ai.te_interpreter import (
    TEInterpreter,
    _collect_allowed_prices,
    _compact_key_levels,
    _extract_json,
    _fingerprint,
    _parse_ai_json,
)
from models.te_interpretation import TEAIInterpretation
from monitoring import te_ai_log as ai_log_mod


# ──────────────────────────────────────────────────
# 数据工厂
# ──────────────────────────────────────────────────

def _make_signal(
    state: str = "momentum_fading",
    direction: str = "down",
    regime: str = "trend_down",
    vetoed: bool = False,
    consensus: str = "partial",
    tf1h_comp: float = -0.32,
    tf4h_comp: float = -0.41,
    tf1d_comp: float = -0.28,
) -> dict:
    return {
        "coin": "BTC",
        "overall_state": state,
        "overall_direction": direction,
        "overall_action": "reduce",
        "overall_position_pct": 0.3,
        "consensus_level": consensus,
        "regime": regime,
        "regime_vetoed": vetoed,
        "overall_plain_cn": "下跌已接近衰竭",
        "overall_tip_cn": "减仓观察",
        "overall_reason_cn": "多周期动能减速",
        "data_quality": "ok",
        "missing_inputs": [],
        "tf_1h": {
            "tf": "1h",
            "direction": direction,
            "state": state,
            "composite_score": tf1h_comp,
            "momentum_score": -0.4,
            "participation_score": -0.3,
            "exhaustion_score": -0.2,
            "state_age_min": 60,
            "confirmed_ticks": 2,
            "triggers": ["MACD 拐头"],
            "sub_scores": [
                {"key": "m1", "name": "MACD 二阶", "score": -0.6, "note": "hist 减速"},
                {"key": "p1", "name": "CVD 动能", "score": 0.3, "note": "资金在跟"},
            ],
        },
        "tf_4h": {
            "tf": "4h",
            "direction": direction,
            "state": state,
            "composite_score": tf4h_comp,
            "momentum_score": -0.5,
            "participation_score": -0.4,
            "exhaustion_score": -0.3,
            "state_age_min": 240,
            "confirmed_ticks": 2,
            "triggers": [],
            "sub_scores": [],
        },
        "tf_1d": {
            "tf": "1d",
            "direction": direction,
            "state": state,
            "composite_score": tf1d_comp,
            "momentum_score": -0.3,
            "participation_score": -0.2,
            "exhaustion_score": -0.4,
            "state_age_min": 1440,
            "confirmed_ticks": 3,
            "triggers": [],
            "sub_scores": [],
        },
    }


# ──────────────────────────────────────────────────
# 1. 指纹测试
# ──────────────────────────────────────────────────

def test_fingerprint_stable_on_noise():
    s1 = _make_signal()
    s2 = _make_signal()
    # 增加一个不在指纹白名单里的毛刺字段
    s2["ts_noise"] = 12345
    assert _fingerprint("BTC", s1) == _fingerprint("BTC", s2)


def test_fingerprint_changes_on_state():
    s1 = _make_signal(state="momentum_fading")
    s2 = _make_signal(state="exhaustion_warn")
    assert _fingerprint("BTC", s1) != _fingerprint("BTC", s2)


def test_fingerprint_changes_on_composite_rounded():
    s1 = _make_signal(tf1h_comp=-0.32)
    s2 = _make_signal(tf1h_comp=-0.65)  # 四舍 1 位：-0.3 vs -0.7
    assert _fingerprint("BTC", s1) != _fingerprint("BTC", s2)


def test_fingerprint_stable_on_small_composite_noise():
    """四舍 1 位后相同的 composite 不应改变指纹（去毛刺）。"""
    s1 = _make_signal(tf1h_comp=-0.30)
    s2 = _make_signal(tf1h_comp=-0.34)  # round(-0.30, 1) == round(-0.34, 1) == -0.3
    assert _fingerprint("BTC", s1) == _fingerprint("BTC", s2)


# ──────────────────────────────────────────────────
# 2. JSON 抽取
# ──────────────────────────────────────────────────

def test_extract_plain_json():
    txt = '{"a": 1, "b": "hello"}'
    assert _extract_json(txt) == {"a": 1, "b": "hello"}


def test_extract_with_markdown_fence():
    txt = "```json\n{\"a\": 1}\n```"
    assert _extract_json(txt) == {"a": 1}


def test_extract_with_fence_no_lang():
    txt = "```\n{\"a\": 1}\n```"
    assert _extract_json(txt) == {"a": 1}


def test_extract_with_prefix_and_suffix():
    txt = "Here is my answer:\n{\"a\": 1}\n\nCheers!"
    assert _extract_json(txt) == {"a": 1}


def test_extract_trailing_comma_tolerated():
    txt = '{"a": 1, "b": [1,2,3,],}'
    assert _extract_json(txt) == {"a": 1, "b": [1, 2, 3]}


def test_extract_returns_none_on_garbage():
    assert _extract_json("no json here") is None


# ──────────────────────────────────────────────────
# 3. 字段解析白名单
# ──────────────────────────────────────────────────

def test_parse_valid_full():
    raw = json.dumps({
        "summary_cn": "下跌接近尾声",
        "scenario": "reversal_early",
        "conflict_resolution": "1H 已翻多但日线仍空",
        "traps": ["别追空", "假突破"],
        "triggers_to_watch": ["等 4H composite 翻正"],
        "action_suggestion": "观望",
        "confidence": 0.72,
        "alignment_with_rules": "partial_disagree",
        "alignment_reason": "时机不一致",
    })
    parsed, err = _parse_ai_json(raw, "")
    assert err is None
    assert parsed["summary_cn"] == "下跌接近尾声"
    assert parsed["scenario"] == "reversal_early"
    assert len(parsed["traps"]) == 2
    assert parsed["confidence"] == 0.72
    assert parsed["alignment_with_rules"] == "partial_disagree"


def test_parse_unknown_scenario_falls_to_unclear():
    raw = json.dumps({"scenario": "wild_guess", "confidence": 0.5})
    parsed, err = _parse_ai_json(raw, "")
    assert err is None
    assert parsed["scenario"] == "unclear"


def test_parse_unknown_alignment_falls_to_insufficient():
    raw = json.dumps({"alignment_with_rules": "maybe"})
    parsed, err = _parse_ai_json(raw, "")
    assert parsed["alignment_with_rules"] == "insufficient"


def test_parse_confidence_clamped():
    raw = json.dumps({"confidence": 5.0})
    parsed, _ = _parse_ai_json(raw, "")
    assert parsed["confidence"] == 1.0
    raw2 = json.dumps({"confidence": -2.0})
    parsed2, _ = _parse_ai_json(raw2, "")
    assert parsed2["confidence"] == 0.0


def test_parse_non_list_traps_coerced():
    raw = json.dumps({"traps": "单条陷阱"})
    parsed, _ = _parse_ai_json(raw, "")
    assert parsed["traps"] == ["单条陷阱"]


def test_parse_error_on_unparseable():
    parsed, err = _parse_ai_json("garbage text", "")
    assert err is not None
    assert parsed == {}


# ──────────────────────────────────────────────────
# 4. 缓存 + 异步调用
# ──────────────────────────────────────────────────

class _FakeChoice:
    def __init__(self, content: str, reasoning: str = ""):
        self.message = MagicMock()
        self.message.content = content
        self.message.reasoning_content = reasoning


class _FakeUsage:
    def __init__(self):
        self.prompt_tokens = 100
        self.completion_tokens = 50
        self.completion_tokens_details = MagicMock(reasoning_tokens=30)


class _FakeResponse:
    def __init__(self, content: str, reasoning: str = ""):
        self.choices = [_FakeChoice(content, reasoning)]
        self.usage = _FakeUsage()


def _fake_ai_content() -> str:
    return json.dumps({
        "summary_cn": "1H 已翻多但大级别仍空",
        "scenario": "reversal_early",
        "conflict_resolution": "1H 多信号强，4H 仍看空",
        "traps": ["追空被夹"],
        "triggers_to_watch": ["等 4H composite 翻正"],
        "action_suggestion": "空单减仓",
        "confidence": 0.72,
        "alignment_with_rules": "partial_disagree",
        "alignment_reason": "方向一致但 AI 更谨慎",
    })


@pytest.mark.asyncio
async def test_interpret_caches_and_force():
    interp = TEInterpreter()
    # 手工塞 client
    mock_client = MagicMock()
    mock_client.chat.completions.create = AsyncMock(
        return_value=_FakeResponse(_fake_ai_content(), reasoning="思考过程：..."),
    )
    interp._client = mock_client

    sig = _make_signal()

    # 首次调用 → 实际调用 AI
    r1 = await interp.interpret("BTC", sig, price=72000, atr=800)
    assert r1.error is None
    assert r1.cache_hit is False
    assert r1.summary_cn == "1H 已翻多但大级别仍空"
    assert r1.confidence == 0.72
    assert r1.reasoning == "思考过程：..."
    assert mock_client.chat.completions.create.await_count == 1

    # 第二次相同指纹 → 命中缓存，不再调用
    r2 = await interp.interpret("BTC", sig, price=72000, atr=800)
    assert r2.cache_hit is True
    assert r2.summary_cn == r1.summary_cn
    assert mock_client.chat.completions.create.await_count == 1

    # force=True 绕过缓存
    r3 = await interp.interpret("BTC", sig, price=72000, atr=800, force=True)
    assert r3.cache_hit is False
    assert mock_client.chat.completions.create.await_count == 2


@pytest.mark.asyncio
async def test_interpret_returns_error_when_no_client():
    interp = TEInterpreter()
    interp._client = None
    sig = _make_signal()
    r = await interp.interpret("BTC", sig, price=72000)
    assert r.error is not None
    assert "未配置" in r.error or "API" in r.error
    assert r.alignment_with_rules == "insufficient"


@pytest.mark.asyncio
async def test_interpret_parse_failure_returns_error():
    interp = TEInterpreter()
    mock_client = MagicMock()
    mock_client.chat.completions.create = AsyncMock(
        return_value=_FakeResponse("not json at all"),
    )
    interp._client = mock_client
    sig = _make_signal()
    r = await interp.interpret("BTC", sig, price=72000)
    assert r.error is not None
    assert r.alignment_with_rules == "insufficient"
    # 不应入缓存
    r2 = await interp.interpret("BTC", sig, price=72000)
    assert mock_client.chat.completions.create.await_count == 2


# ──────────────────────────────────────────────────
# 5. Shadow log 写入
# ──────────────────────────────────────────────────

def test_shadow_log_writes_main_and_thinking(tmp_path, monkeypatch):
    monkeypatch.setattr(ai_log_mod, "_repo_root", lambda: str(tmp_path))

    result = TEAIInterpretation(
        coin="BTC",
        ts=int(time.time()),
        signal_fingerprint="abc123",
        model="deepseek-reasoner",
        cache_hit=False,
        latency_ms=12000,
        tokens_in=300,
        tokens_out=150,
        reasoning_tokens=200,
        summary_cn="测试结论",
        scenario="reversal_early",
        conflict_resolution="A 冲突 B",
        traps=["陷阱1"],
        triggers_to_watch=["等 X"],
        action_suggestion="观望",
        confidence=0.65,
        alignment_with_rules="partial_disagree",
        alignment_reason="时机存疑",
        reasoning="long reasoning chain here...",
    )
    sig = _make_signal()

    ai_log_mod.log_interpretation(result, sig, price=72000)

    day_root = Path(ai_log_mod.ai_log_root())
    days = list(day_root.iterdir())
    assert len(days) == 1
    files = list(days[0].iterdir())
    names = sorted(f.name for f in files)
    assert "BTC.jsonl" in names
    assert "BTC.thinking.jsonl" in names

    # 主记录可被解析
    main_line = (days[0] / "BTC.jsonl").read_text(encoding="utf-8").strip()
    main = json.loads(main_line)
    assert main["coin"] == "BTC"
    assert main["ai"]["summary_cn"] == "测试结论"
    assert main["rules_snapshot"]["overall_state"] == "momentum_fading"

    # reasoning 独立文件
    think_line = (days[0] / "BTC.thinking.jsonl").read_text(encoding="utf-8").strip()
    think = json.loads(think_line)
    assert think["reasoning"].startswith("long reasoning")

    # stats 统计
    stats = ai_log_mod.stats()
    assert stats["days"] == 1
    assert stats["total_records"] == 1


@pytest.mark.asyncio
async def test_public_helpers_for_async_polling():
    """routes.py 依赖这几个 public 方法做任务异步模式，确保不被重构破坏。"""
    interp = TEInterpreter()
    sig = _make_signal()

    fp1 = interp.compute_fingerprint("BTC", sig)
    fp2 = interp.compute_fingerprint("BTC", sig)
    assert fp1 == fp2 and len(fp1) == 16

    # 空缓存 / 空 inflight
    assert interp.peek_cache(fp1) is None
    assert interp.is_inflight(fp1) is False

    # 手动塞一条缓存，peek_cache 能拿到且标 cache_hit
    from models.te_interpretation import TEAIInterpretation as TIModel
    from ai.te_interpreter import _CacheEntry
    dummy = TIModel(
        coin="BTC", ts=int(time.time()), signal_fingerprint=fp1,
        summary_cn="test",
    )
    interp._cache[fp1] = _CacheEntry(result=dummy, expires_at=time.time() + 60)
    got = interp.peek_cache(fp1)
    assert got is not None
    assert got.cache_hit is True
    assert got.summary_cn == "test"


# ──────────────────────────────────────────────────
# 6. Phase 2 · key_levels 压缩 + 新字段 + 白名单
# ──────────────────────────────────────────────────

def _make_kl_snapshot() -> dict:
    """模拟 KeyLevelSnapshotV2.model_dump() 的精简版。"""
    return {
        "ts": int(time.time()),
        "current_price": 76109.0,
        "atr": 600.0,
        "levels": [
            {
                "price": 76948.21, "side": "resistance", "strength_tier": "S",
                "distance_pct": 1.10, "state": "idle",
                "sources": ["liq_cluster", "swing_high"],
                "source_count": 3, "historical_validity": 0.72, "bounce_count": 3,
                "pattern_detected": "", "final_score": 88.5,
                "note": "牛市支撑带上沿", "timeframe": "4H",
                "category": "strong_resistance",
            },
            {
                "price": 74973.0, "side": "support", "strength_tier": "S",
                "distance_pct": -1.49, "state": "idle",
                "sources": ["liq_cluster", "psych_level"],
                "source_count": 2, "historical_validity": 0.0, "bounce_count": 0,
                "pattern_detected": "", "final_score": 82.0,
                "note": "清算密集区 + 心理关口", "timeframe": "1D",
                "category": "strong_support",
            },
            {
                "price": 74577.23, "side": "support", "strength_tier": "S",
                "distance_pct": -2.01, "state": "idle",
                "sources": ["liq_cluster", "ema200"],
                "source_count": 2, "historical_validity": 0.3, "bounce_count": 1,
                "pattern_detected": "", "final_score": 80.0,
                "note": "清算密集区", "timeframe": "1D",
                "category": "strong_support",
            },
            {
                "price": 73500.0, "side": "support", "strength_tier": "B",  # 非 S/A 应被过滤
                "distance_pct": -3.43, "state": "idle",
                "sources": ["fib_0618"], "source_count": 1,
                "historical_validity": 0.0, "bounce_count": 0,
                "pattern_detected": "", "final_score": 55.0,
                "note": "弱", "timeframe": "1D",
                "category": "moderate_support",
            },
        ],
        "bull_bear_line": {
            "current_regime": "bull",
            "regime_reason": "价在 SMA200d 上方 + 云层上方",
            "sma200d": 70300.0,
            "ichimoku_cloud_top": 70569.32,
            "ichimoku_cloud_bottom": 69555.50,
        },
        "breakout_zone": {
            "bb_squeeze": True,
            "squeeze_direction": "up",
            "bb_upper": 76500.0,
            "bb_lower": 75200.0,
            "note": "BBW 压缩",
        },
        "daily_strong_support": "$74,577.23",
        "daily_strong_resistance": "$78,207.01",
        "weekly_strong_support": "$69,555.50",
        "weekly_strong_resistance": None,
        "structure_summary": "价在牛市支撑带下方",
        "nearest_strong_support": 74973.0,
        "nearest_strong_resistance": 76948.21,
    }


def test_compact_key_levels_keeps_only_s_a_tier():
    kl = _make_kl_snapshot()
    compact = _compact_key_levels(kl, current_price=76109.0)
    assert compact is not None
    # B 级应被过滤
    all_prices = {lv["price"] for lv in compact["strong_resistances"] + compact["strong_supports"]}
    assert 73500.0 not in all_prices
    # S 级保留
    assert 76948.21 in all_prices
    assert 74973.0 in all_prices
    # 按距离排序（最近的在前）
    assert compact["strong_supports"][0]["price"] == 74973.0
    # 最多 3 档
    assert len(compact["strong_supports"]) <= 3
    # 牛熊分界线透传
    assert compact["bull_bear_line"]["regime"] == "bull"
    # 挤压带保留
    assert compact["breakout_zone"]["direction"] == "up"
    # 多周期级位字符串透传
    assert compact["daily_strong_resistance"] == "$78,207.01"


def test_compact_key_levels_returns_none_on_empty():
    assert _compact_key_levels(None, 76109.0) is None
    # 空结构也能容错
    empty = _compact_key_levels({}, 76109.0)
    assert empty is not None
    assert empty["strong_resistances"] == []
    assert empty["strong_supports"] == []


def test_collect_allowed_prices_includes_string_levels():
    kl = _compact_key_levels(_make_kl_snapshot(), 76109.0)
    prices = _collect_allowed_prices(kl)
    # S 级现价
    assert 76948.21 in prices
    # 字符串形式的多周期级位也应被解析
    assert 74577.23 in prices
    assert 78207.01 in prices
    assert 69555.5 in prices


def test_parse_level_projection_valid_price_accepted():
    kl = _compact_key_levels(_make_kl_snapshot(), 76109.0)
    raw = json.dumps({
        "summary_cn": "多头动能足",
        "level_projection": {
            "target_level": 76948.21,  # 合法价位
            "direction_tested": "resistance",
            "break_likelihood": "likely",
            "break_conviction": 0.68,
            "reasoning_cn": "动能 + 共振",
            "if_break_cn": "目标 $78,207.01",
            "if_fail_cn": "回测 $74,973.00",
        },
    })
    parsed, err = _parse_ai_json(raw, "", key_levels=kl)
    assert err is None
    lp = parsed["level_projection"]
    assert lp["target_level"] == 76948.21
    assert lp["direction_tested"] == "resistance"
    assert lp["break_likelihood"] == "likely"
    assert 0.67 <= lp["break_conviction"] <= 0.69


def test_parse_level_projection_fake_price_degrades():
    """AI 编造一个 key_levels 里没有的价位 → 强制降级为 none/insufficient。"""
    kl = _compact_key_levels(_make_kl_snapshot(), 76109.0)
    raw = json.dumps({
        "summary_cn": "测试",
        "level_projection": {
            "target_level": 76800.0,  # 不在白名单里
            "direction_tested": "resistance",
            "break_likelihood": "very_likely",
            "break_conviction": 0.9,
        },
    })
    parsed, _ = _parse_ai_json(raw, "", key_levels=kl)
    lp = parsed["level_projection"]
    assert lp["target_level"] is None
    assert lp["direction_tested"] == "none"
    assert lp["break_likelihood"] == "insufficient"


def test_parse_trend_assessment_and_trade_bias():
    raw = json.dumps({
        "summary_cn": "上涨趋势健康",
        "trend_assessment": {
            "primary_trend": "uptrend",
            "momentum_quality": "fuel_adequate",
            "momentum_direction": "stable",
            "health_summary_cn": "上涨趋势，动能足",
            "evidence_cn": "RSI=65.5 + 价距 EMA20 +3.4σ + FVG 多1空0",
        },
        "trade_bias": {
            "direction": "long",
            "strength": "probe",
            "entry_zone_cn": "76200-76500 回踩",
            "invalidation_cn": "跌破 74973（S 级支撑）",
            "timeframe_cn": "4-12 小时",
            "why_cn": "多周期共振 + 挤压向上",
        },
        "independent_view": "OI 与 CVD 未完全同步，疑有诱空",
    })
    parsed, _ = _parse_ai_json(raw, "", key_levels=None)
    ta = parsed["trend_assessment"]
    assert ta["primary_trend"] == "uptrend"
    assert ta["momentum_quality"] == "fuel_adequate"
    assert "RSI=65.5" in ta["evidence_cn"]
    tb = parsed["trade_bias"]
    assert tb["direction"] == "long"
    assert tb["strength"] == "probe"
    assert "74973" in tb["invalidation_cn"]
    assert "诱空" in parsed["independent_view"]


def test_parse_neutral_alignment_accepted():
    """neutral 是新增合法对齐值，应正常通过。"""
    raw = json.dumps({
        "summary_cn": "独立观察",
        "alignment_with_rules": "neutral",
        "alignment_reason": "既不支持也不反对规则",
    })
    parsed, _ = _parse_ai_json(raw, "", key_levels=None)
    assert parsed["alignment_with_rules"] == "neutral"


def test_parse_unknown_trend_fields_fallback_to_defaults():
    raw = json.dumps({
        "trend_assessment": {
            "primary_trend": "wild",   # 非法
            "momentum_quality": "yolo",  # 非法
            "momentum_direction": "turbo",  # 非法
        },
    })
    parsed, _ = _parse_ai_json(raw, "", key_levels=None)
    ta = parsed["trend_assessment"]
    assert ta["primary_trend"] == "transition"
    assert ta["momentum_quality"] == "unclear"
    assert ta["momentum_direction"] == "unclear"


def test_fingerprint_includes_key_levels():
    """key_levels 变化（S/A 级位价格集合）应改变指纹，避免缓存错位。"""
    s = _make_signal()
    kl1 = _make_kl_snapshot()
    kl2 = _make_kl_snapshot()
    # 改动一个 S 级位的价格（>100 美元）
    kl2["levels"][0]["price"] = 77500.0
    fp1 = _fingerprint("BTC", s, kl1)
    fp2 = _fingerprint("BTC", s, kl2)
    assert fp1 != fp2
    # 无 key_levels 和有 key_levels 也应不同
    fp_no_kl = _fingerprint("BTC", s, None)
    assert fp_no_kl != fp1


def test_shadow_log_skips_thinking_when_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(ai_log_mod, "_repo_root", lambda: str(tmp_path))
    result = TEAIInterpretation(
        coin="ETH",
        ts=int(time.time()),
        signal_fingerprint="def",
        model="deepseek-chat",
        summary_cn="无思考",
        reasoning="",
    )
    ai_log_mod.log_interpretation(result, _make_signal(), price=3500)
    day_root = Path(ai_log_mod.ai_log_root())
    days = list(day_root.iterdir())
    files = [f.name for f in days[0].iterdir()]
    assert "ETH.jsonl" in files
    assert "ETH.thinking.jsonl" not in files
