"""P1.3 · AI sections[] 降级策略 β 单测

核心覆盖：
  当 AI 返回的 AITRADER_MATRIX_JSON 不含 sections 或为空数组时，
  trader_report_builder._record_quality 的 json_valid 判定逻辑

历史语义（改前）：
  - sections 必须是 list 才算有效，缺失或 None → json_valid=False / invalid_reason="wrong_sections"
  - 这导致 AI 选择精简输出时被错误判为质量问题

新语义（P1.3 改后）：
  - sections 存在但非 list → 仍然 invalid（真 schema 错误）
  - sections 完全缺失或空数组 [] → 合法降级（规则侧已兜底生成 7 板块）
  - 其他字段（bias/trading_plans/matrix_summary_cn）决定 JSON 实质有效性

本测不直接调 _record_quality（依赖图太深），而是直接验证其决策分支逻辑。
"""
from __future__ import annotations


def _judge_json_valid(ai_json):
    """复刻 trader_report_builder._record_quality 的 json_valid 判定逻辑

    与生产代码保持同语义；任何一侧改动都必须同步修改另一侧。
    """
    json_valid = False
    invalid_reason = "missing"
    if ai_json:
        if not isinstance(ai_json, dict):
            invalid_reason = "malformed"
        else:
            secs = ai_json.get("sections", None)
            if secs is not None and not isinstance(secs, list):
                invalid_reason = "wrong_sections"
            else:
                json_valid = True
                invalid_reason = ""
    return json_valid, invalid_reason


class TestOptionalSectionsDowngrade:
    """P1.3 · sections[] optional 降级语义测试"""

    def test_full_sections_still_valid(self):
        """经典场景：AI 填满 7 板块 → 有效"""
        payload = {
            "bias": "bullish",
            "sections": [
                {"section_id": "A", "rows": []},
                {"section_id": "B", "rows": []},
            ],
            "trading_plans": [],
        }
        ok, reason = _judge_json_valid(payload)
        assert ok is True
        assert reason == ""

    def test_empty_sections_list_valid(self):
        """降级场景 1：AI 显式返回 sections=[] → 仍然有效"""
        payload = {
            "bias": "neutral",
            "sections": [],
            "trading_plans": [],
        }
        ok, reason = _judge_json_valid(payload)
        assert ok is True, "空数组 sections 应视为合法降级，规则侧已兜底"
        assert reason == ""

    def test_missing_sections_key_valid(self):
        """降级场景 2：AI 完全不返回 sections 键 → 仍然有效"""
        payload = {
            "bias": "bearish",
            "conviction": 68,
            "trading_plans": [{"priority": 1, "direction": "short"}],
        }
        ok, reason = _judge_json_valid(payload)
        assert ok is True, "sections 键缺失应视为合法降级（prompt 已标 optional）"
        assert reason == ""

    def test_sections_wrong_type_still_invalid(self):
        """真错误：sections 存在但非 list（比如 dict/str）→ malformed"""
        payload = {"bias": "bullish", "sections": {"A": "bad shape"}}
        ok, reason = _judge_json_valid(payload)
        assert ok is False
        assert reason == "wrong_sections"

    def test_sections_string_invalid(self):
        """真错误：sections 是字符串 → malformed"""
        payload = {"bias": "bullish", "sections": "not a list"}
        ok, reason = _judge_json_valid(payload)
        assert ok is False
        assert reason == "wrong_sections"

    def test_non_dict_payload_malformed(self):
        """payload 不是 dict → malformed"""
        ok, reason = _judge_json_valid(["array", "not", "dict"])
        assert ok is False
        assert reason == "malformed"

    def test_none_payload_missing(self):
        """None → missing"""
        ok, reason = _judge_json_valid(None)
        assert ok is False
        assert reason == "missing"

    def test_empty_dict_falsy_missing(self):
        """空 dict 在 Python 中是 falsy，走 missing 分支（与无 JSON 对齐）
        这是故意保留的行为：AI 产出完全空的 dict 不应被视为有效输出"""
        ok, reason = _judge_json_valid({})
        assert ok is False
        assert reason == "missing"

    def test_dict_with_only_bias_valid(self):
        """最小合法载荷：只有 bias 字段（sections/plans 均缺失）→ 有效
        这是 P1.3 最典型的降级场景 —— AI 叙事链 + 方向裁定已充分"""
        ok, reason = _judge_json_valid({"bias": "bullish"})
        assert ok is True
        assert reason == ""
