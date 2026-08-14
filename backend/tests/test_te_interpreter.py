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
    _SYSTEM_PROMPT,
    TEInterpreter,
    _bucket,
    _collect_allowed_prices,
    _compact_flow_metrics,
    _compact_key_levels,
    _compact_liq_fuel,
    _compact_market_structure,
    _compact_sentiment,
    _extract_json,
    _extras_fingerprint_buckets,
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
        model="deepseek-v4-flash",
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


# ──────────────────────────────────────────────────
# Phase 3 · Market Structure / Flow / Sentiment / Liq Fuel
# ──────────────────────────────────────────────────


def _make_ms(direction="bullish", event="BOS_up", tf="1h", high=80000, low=75000):
    return {
        "timeframe": tf,
        "direction": direction,
        "last_event": event,
        "event_ts": int(time.time() * 1000) - 30 * 60 * 1000,  # 30min ago
        "structure_high": high,
        "structure_low": low,
        "operate_bias": "long_only" if direction == "bullish" else "short_only",
        "confidence": 0.75,
        "summary": f"{tf} {direction}",
    }


def test_compact_market_structure_aligned_up():
    ms_1h = _make_ms("bullish", "BOS_up", "1h")
    ms_1d = _make_ms("bullish", "CHoCH_up", "1d")
    ms_1w = _make_ms("bullish", "BOS_up", "1w")
    out = _compact_market_structure(ms_1h, ms_1d, ms_1w, price=78000)
    assert out is not None
    assert out["alignment"] == "aligned_up"
    assert out["1h"]["direction"] == "bullish"
    assert out["1d"]["last_event"] == "CHoCH_up"
    assert out["1h"]["event_age_min"] is not None
    assert out["1h"]["event_age_min"] >= 29  # 30min 左右


def test_compact_market_structure_only_1h():
    ms_1h = _make_ms("bullish", "BOS_up", "1h")
    out = _compact_market_structure(ms_1h, None, None, price=78000)
    assert out is not None
    # 只有 1h，另外两个周期为 None
    assert out["1h"] is not None
    assert out["1d"] is None
    assert out["1w"] is None
    # 单周期不足以判断 alignment
    assert out["alignment"] == "insufficient"


def test_compact_flow_metrics_oi_4h_calc():
    """oi_history 足够 48 条（5m 粒度 × 48 = 4h）→ 现算 4h%。"""
    now_ts = int(time.time())
    # 48 条：价从 1B 涨到 1.02B（+2%）
    history = []
    for i in range(48):
        history.append({
            "ts": now_ts - (48 - i) * 300,
            "oi_usd": 1_000_000_000 + i * (20_000_000 / 47),
        })
    history.append({"ts": now_ts, "oi_usd": 1_020_000_000})
    funding = {"oi_weighted_rate": 0.0005}  # +5bp
    out = _compact_flow_metrics(funding, None, None, history, None)
    assert out is not None
    assert "change_4h_pct" in out["oi"]
    # 1e9 → 1.02e9 = +2.0%
    assert abs(out["oi"]["change_4h_pct"] - 2.0) < 0.5


def test_compact_flow_metrics_oi_history_too_short():
    """不足 48 条 → 不算 4h%，不报错。"""
    history = [{"ts": 0, "oi_usd": 1e9}] * 10
    funding = {"oi_weighted_rate": 0.0005}
    out = _compact_flow_metrics(funding, None, None, history, None)
    assert out is not None
    assert "change_4h_pct" not in (out.get("oi") or {})


def test_compact_sentiment_retail_long_smart_short():
    """散户 1.3（偏多）+ 大户 0.8（偏空）→ divergence = retail_long_smart_short。"""
    retail = {"avg_ratio": 1.3, "exchanges": [], "cycle": "1h"}
    top_acct = {"avg_ratio": 0.85, "exchanges": [], "cycle": "1h"}
    top_pos = {"avg_ratio": 0.80, "exchanges": [], "cycle": "1h"}
    out = _compact_sentiment(retail, top_acct, top_pos)
    assert out is not None
    assert out["divergence"] == "retail_long_smart_short"


def test_compact_liq_fuel_above_heavy():
    """上方清算总额 > 下方 → asymmetry_note = above_heavy。"""
    liq = {
        "clusters_above": [
            {"price_center": 80000, "price_from": 79500, "price_to": 80500,
             "distance_pct": 2.0, "total_usd": 100e6, "side": "short", "dominant_leverage": "20x"},
            {"price_center": 82000, "price_from": 81500, "price_to": 82500,
             "distance_pct": 5.0, "total_usd": 50e6, "side": "short", "dominant_leverage": "10x"},
        ],
        "clusters_below": [
            {"price_center": 76000, "price_from": 75500, "price_to": 76500,
             "distance_pct": -2.0, "total_usd": 30e6, "side": "long", "dominant_leverage": "20x"},
        ],
        "imbalance_ratio": 5.0,  # 150/30
        "vacuum_zones": [
            {"price_from": 80500, "price_to": 81500, "midpoint": 81000, "note": "gap"},
        ],
    }
    out = _compact_liq_fuel(liq, price=78000)
    assert out is not None
    assert out["asymmetry_note"] == "above_heavy"
    assert len(out["above"]) == 2
    assert len(out["below"]) == 1


# ──────────────────────────────────────────────────
# Phase 3 · 指纹桶化
# ──────────────────────────────────────────────────


def test_fingerprint_bucketing_crosses_boundary():
    """OI 5m% 从 +0.15 到 +0.25（跨 0.2 桶边界）→ 指纹变。"""
    s = _make_signal()
    # 桶大小 0.2：0.15→ bucket 0.2，0.25→ bucket 0.2 也可能… 我们选跨 0 线的：
    # 0.05 → bucket 0.0；0.25 → bucket 0.2
    extras1 = {"oi": {"change_5m_pct": 0.05, "change_1h_pct": 0.1, "trend": "up"}}
    extras2 = {"oi": {"change_5m_pct": 0.25, "change_1h_pct": 0.1, "trend": "up"}}
    assert _fingerprint("BTC", s, None, extras1) != _fingerprint("BTC", s, None, extras2)


def test_fingerprint_bucketing_small_noise_stable():
    """OI 5m% 从 0.15 到 0.18（同一 0.2 桶）→ 指纹不变（抗噪）。"""
    s = _make_signal()
    extras1 = {"oi": {"change_5m_pct": 0.15, "change_1h_pct": 0.1, "trend": "up"}}
    extras2 = {"oi": {"change_5m_pct": 0.18, "change_1h_pct": 0.1, "trend": "up"}}
    assert _fingerprint("BTC", s, None, extras1) == _fingerprint("BTC", s, None, extras2)


def test_fingerprint_stable_without_extras():
    """向后兼容：extras_dict=None 或缺省，指纹与旧行为一致（不受 extras 干扰）。"""
    s = _make_signal()
    fp_no_extras_new = _fingerprint("BTC", s, None, None)
    fp_no_extras_old = _fingerprint("BTC", s, None)  # 使用默认 None
    assert fp_no_extras_new == fp_no_extras_old


def test_bucket_basic():
    assert _bucket(None, 0.2) is None
    assert _bucket("nan_garbage", 0.2) is None
    assert _bucket(0.15, 0.2) == 0.2
    assert _bucket(0.05, 0.2) == 0.0
    assert _bucket(-0.15, 0.2) == -0.2


def test_extras_fingerprint_buckets_empty():
    """None 或 {} 输入 → 返回 {}，不会污染指纹。"""
    assert _extras_fingerprint_buckets(None) == {}
    assert _extras_fingerprint_buckets({}) == {}


def test_compact_flow_oi_4h_uses_timestamp_aligned_baseline():
    from ai.te_interpreter import _compact_flow_metrics

    base = 1_700_000_000
    history = [
        {"ts": base + i * 300, "oi_usd": 100.0 + i}
        for i in range(49)
    ]
    out = _compact_flow_metrics(
        None, None, {"current_usd": 148.0}, history, None,
    )
    assert out is not None
    assert out["oi"]["change_4h_pct"] == 48.0


def test_compact_flow_oi_4h_does_not_widen_missing_window():
    from ai.te_interpreter import _compact_flow_metrics

    base = 1_700_000_000
    history = [
        {"ts": base + i * 300, "oi_usd": 100.0 + i}
        for i in range(49)
        if i != 0
    ]
    out = _compact_flow_metrics(
        None, None, {"current_usd": 148.0}, history, None,
    )
    assert out is not None
    assert "change_4h_pct" not in out["oi"]


def test_shadow_log_includes_new_fields(tmp_path, monkeypatch):
    """回归：补全后的 log_interpretation 必须包含 4 个新字段。"""
    from models.te_interpretation import (
        TEAIInterpretation as _TEResult,
        TradeBias, TrendAssessment, LevelProjection,
    )
    monkeypatch.setattr(ai_log_mod, "_repo_root", lambda: str(tmp_path))
    result = _TEResult(
        coin="BTC",
        ts=int(time.time()),
        signal_fingerprint="log_test_fp",
        model="deepseek-v4-flash",
        summary_cn="test",
        trend_assessment=TrendAssessment(primary_trend="uptrend", momentum_quality="fuel_full"),
        level_projection=LevelProjection(target_level=78000, break_likelihood="likely"),
        trade_bias=TradeBias(direction="long", strength="standard"),
        independent_view="独立观察一句",
    )
    ai_log_mod.log_interpretation(result, _make_signal(), price=77500)
    day_root = Path(ai_log_mod.ai_log_root())
    files = sorted(next(day_root.iterdir()).iterdir())
    # 只校验主 jsonl，不关心 thinking
    jsonl = next(p for p in files if p.name == "BTC.jsonl")
    lines = jsonl.read_text(encoding="utf-8").strip().splitlines()
    obj = json.loads(lines[-1])
    assert obj["ai"]["trend_assessment"]["primary_trend"] == "uptrend"
    assert obj["ai"]["level_projection"]["break_likelihood"] == "likely"
    assert obj["ai"]["trade_bias"]["direction"] == "long"
    assert obj["ai"]["independent_view"] == "独立观察一句"


def test_shadow_log_skips_thinking_when_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(ai_log_mod, "_repo_root", lambda: str(tmp_path))
    result = TEAIInterpretation(
        coin="ETH",
        ts=int(time.time()),
        signal_fingerprint="def",
        model="deepseek-v4-flash",
        summary_cn="无思考",
        reasoning="",
    )
    ai_log_mod.log_interpretation(result, _make_signal(), price=3500)
    day_root = Path(ai_log_mod.ai_log_root())
    days = list(day_root.iterdir())
    files = [f.name for f in days[0].iterdir()]
    assert "ETH.jsonl" in files
    assert "ETH.thinking.jsonl" not in files


# ──────────────────────────────────────────────────
# Prompt 质量回归（防止指引被误删 / 降级）
# ──────────────────────────────────────────────────

def test_system_prompt_liq_fuel_has_symmetric_guidance():
    """P0：liq_fuel 对称语义 + 两种磁吸理论必须在 prompt 里显式说明。

    实际事故：AI 在同一次输出里把 below_heavy 解读成 3 种互斥方向
    （上移磁吸 / 可能反弹 / 限制下跌），根因就是 prompt 只写了
    above_heavy 一个方向的语义。此测试锁定修复不被回退。
    """
    assert "below_heavy" in _SYSTEM_PROMPT, "below_heavy 语义必须显式出现"
    assert "above_heavy" in _SYSTEM_PROMPT
    assert "反身性扫流动性" in _SYSTEM_PROMPT, "两种磁吸理论的区分必须在"
    assert "不得双向脚踩" in _SYSTEM_PROMPT or "不得在 independent_view" in _SYSTEM_PROMPT, (
        "必须有禁止自相矛盾的硬约束"
    )


def test_system_prompt_alignment_has_neutral_guard():
    """P1a：trade_bias=neutral 时禁止 strong_disagree 的硬约束必须在。"""
    assert "trade_bias.direction" in _SYSTEM_PROMPT
    assert "禁止" in _SYSTEM_PROMPT and "strong_disagree" in _SYSTEM_PROMPT
    # 更硬的定位：必须同时出现 neutral/avoid 和 strong_disagree 禁用关系
    guard_block = _SYSTEM_PROMPT.split("硬约束")[1] if "硬约束" in _SYSTEM_PROMPT else ""
    assert "neutral" in guard_block and "strong_disagree" in guard_block, (
        "neutral+strong_disagree 的禁用组合必须在硬约束段"
    )


def test_system_prompt_confidence_has_downgrade_triggers():
    """P1b：自我矛盾 / neutral / 多解读时 confidence 封顶触发器。"""
    assert "降档触发" in _SYSTEM_PROMPT, "confidence 降档段必须存在"
    # 三条触发器关键字
    assert "不得 > 0.55" in _SYSTEM_PROMPT
    assert "不得 > 0.5" in _SYSTEM_PROMPT
    assert "不得 > 0.45" in _SYSTEM_PROMPT


def test_system_prompt_has_flat_direction_guidance():
    """direction=flat 场景：规则引擎把 sub.score 强制归 0 时，AI 必须知道
    改去读 note 原文而不是被 score=0 误导为"证据不足"。

    注意：此段只约束「认知层」——告诉 AI 怎么读数据；不约束「决策层」——
    最终方向 / alignment / trade_bias 完全由 AI 自主判断（若 note + 五类扩展数据
    真的指向明确方向，AI 应当敢给 strong_disagree）。
    """
    # 认知层三要素必须在
    assert 'tf.*.direction == "flat"' in _SYSTEM_PROMPT or "direction==flat" in _SYSTEM_PROMPT
    assert "score 强制归 0" in _SYSTEM_PROMPT or "sub.score 强制归 0" in _SYSTEM_PROMPT
    assert "只看 sub.note" in _SYSTEM_PROMPT or "请忽略 sub.score" in _SYSTEM_PROMPT
    # 五类扩展数据的权重提示必须在
    assert "key_levels" in _SYSTEM_PROMPT and "market_structure" in _SYSTEM_PROMPT
    assert "不受 flat 影响" in _SYSTEM_PROMPT or "权重调高" in _SYSTEM_PROMPT
    # 明确声明"认知层而非决策层"，避免未来 PR 又把方向限制塞回来
    flat_block_start = _SYSTEM_PROMPT.find("direction == \"flat\"")
    if flat_block_start == -1:
        flat_block_start = _SYSTEM_PROMPT.find("direction==flat")
    flat_block = _SYSTEM_PROMPT[flat_block_start : flat_block_start + 1500]
    assert "独立判断" in flat_block or "自主判断" in flat_block, (
        "flat 段必须明确'决策由 AI 自主判断'，不得加硬性方向限制"
    )
