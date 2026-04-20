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
