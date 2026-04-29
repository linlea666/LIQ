"""extract_json_payload / _extract_json 容错回归测试。

根因记录（Phase A0）
====================
线上 deepseek 曾在 MAA `analyst_reasoning` 长字符串中夹带未转义控制字符
（0x00–0x1f），触发 json.loads "Invalid control character at: line N column M"
解析失败 → MAA 整轮报告归零（conf=0、bias=wait）。

修复策略
========
两处 LLM JSON 解析入口（market_action_prompts.extract_json_payload 与
te_interpreter._extract_json）统一改为 json.loads(block, strict=False)；
仅放宽字符串内字面控制字符，结构校验不变。

本文件锁住该行为，避免后续误回退。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def test_maa_extract_tolerates_control_char_in_string():
    """模拟 deepseek 在字符串内夹带 0x0b（垂直制表）+ 0x01 这种控制字符。"""
    from ai.market_action_prompts import extract_json_payload
    raw = (
        '```json\n'
        '{\n'
        '  "market_conclusion": "BTC \x0b动能衰竭\x01等待方向",\n'
        '  "scenario": "range_bound",\n'
        '  "confidence": 65\n'
        '}\n'
        '```'
    )
    payload = extract_json_payload(raw)
    assert payload["scenario"] == "range_bound"
    assert payload["confidence"] == 65
    assert "动能衰竭" in payload["market_conclusion"]


def test_maa_extract_tolerates_literal_newline_inside_string():
    """字符串内未转义的字面 \n（json 标准也不允许）应被容忍。"""
    from ai.market_action_prompts import extract_json_payload
    raw = '{"market_conclusion": "line1\nline2", "scenario": "range_bound"}'
    payload = extract_json_payload(raw)
    assert payload["scenario"] == "range_bound"
    assert "\n" in payload["market_conclusion"]


def test_maa_extract_still_rejects_non_dict_root():
    """strict=False 不应让根类型校验放松。"""
    from ai.market_action_prompts import extract_json_payload
    import pytest
    with pytest.raises(ValueError):
        extract_json_payload('[1, 2, 3]')


def test_te_extract_tolerates_control_char_in_string():
    """te_interpreter._extract_json 同步口径。"""
    from ai.te_interpreter import _extract_json
    raw = '{"direction_tested": "support\x0btested", "trade_direction": "long"}'
    payload = _extract_json(raw)
    assert payload is not None
    assert "tested" in payload["direction_tested"]
    assert payload["trade_direction"] == "long"


def test_te_extract_returns_none_on_garbage():
    from ai.te_interpreter import _extract_json
    assert _extract_json("not json at all") is None
    assert _extract_json("") is None
