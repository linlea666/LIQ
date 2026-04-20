"""P1-4 · Prompt 模式切换单测（LIQ_PROMPT_MODE 开关）

原则 6：heuristic 模式只在 strict 输出前追加一段提示，core 原文必须 100% 保留，
        便于一键回退。
"""

from __future__ import annotations

import os

import pytest

from ai.prompts import (
    _HEURISTIC_PREFIX,
    _build_strict_system_prompt,
    _get_prompt_mode,
    build_system_prompt,
)


@pytest.fixture(autouse=True)
def _restore_env():
    original = os.environ.get("LIQ_PROMPT_MODE")
    try:
        yield
    finally:
        if original is None:
            os.environ.pop("LIQ_PROMPT_MODE", None)
        else:
            os.environ["LIQ_PROMPT_MODE"] = original


def test_default_mode_is_strict():
    """未设置环境变量时默认 strict。"""
    os.environ.pop("LIQ_PROMPT_MODE", None)
    assert _get_prompt_mode() == "strict"
    prompt = build_system_prompt()
    core = _build_strict_system_prompt()
    assert prompt == core
    assert "【Prompt 模式：heuristic" not in prompt
    # 铁律必须完整保留
    assert "铁律" in prompt
    assert "必须" in prompt


@pytest.mark.parametrize("mode", ["heuristic", "soft", "coach", "HEURISTIC", "  Coach "])
def test_heuristic_mode_prepends_soft_prefix(mode: str):
    """任意合法 heuristic 别名都应生效，strict 正文 100% 保留。"""
    os.environ["LIQ_PROMPT_MODE"] = mode
    assert _get_prompt_mode() == "heuristic"
    prompt = build_system_prompt()
    core = _build_strict_system_prompt()
    assert prompt.startswith(_HEURISTIC_PREFIX)
    assert core in prompt
    # 前缀明确告诉 AI 输出格式契约仍是强制的（避免 JSON 解析失败）
    assert "AITRADER_MATRIX_JSON" in prompt


def test_invalid_mode_falls_back_to_strict(caplog):
    """非法值不应中断运行，回退至 strict 并打 warning。"""
    os.environ["LIQ_PROMPT_MODE"] = "unknown_value"
    with caplog.at_level("WARNING", logger="ai.prompts"):
        mode = _get_prompt_mode()
    assert mode == "strict"
    prompt = build_system_prompt()
    assert not prompt.startswith(_HEURISTIC_PREFIX)


def test_mode_switch_does_not_change_rr_placeholder():
    """动态 R:R 下限在两个模式下都必须被注入（避免前缀引入解析错位）。"""
    os.environ["LIQ_PROMPT_MODE"] = "strict"
    strict = build_system_prompt()
    os.environ["LIQ_PROMPT_MODE"] = "heuristic"
    heur = build_system_prompt()

    # 两份都必须注入 min_sniper_rr 数字（不再有 "1:{min_rr:.1f}" 占位符）
    assert "{min_rr" not in strict
    assert "{min_rr" not in heur
    # heuristic 正文部分长度 = strict 长度
    assert len(heur) == len(strict) + len(_HEURISTIC_PREFIX)
