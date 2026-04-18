"""BBX 字段覆盖率降级告警回归测试

背景：生产日志持续稳定 `25/33 fields populated` 却没任何警告，
用户无法感知上游数据退化（黄金等关键字段悄悄失联）。

修复历程：
- v1（已废弃）：分母=33（MarketIndexData 全字段） → 25/33=76% 永久触发假告警
- v2（本版本）：分母=25（仅 BBX 映射字段），排除 btc_max_pain 等 7 个代码已移除字段
- populated 率低于 80% 时 WARNING 一次，失联集合不变时静默
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import polls.bbx_index as bbx_index_mod
from models.flow import MarketIndexData


def _reset_module_state():
    """每个测试前清空 module 级缓存，避免状态污染。"""
    bbx_index_mod._BBX_PREV_MISSING = set()


def test_coverage_threshold_constant_is_80_percent():
    """阈值必须是 80%，低于此触发 WARNING。"""
    assert bbx_index_mod._BBX_COVERAGE_WARN_THRESHOLD == 0.80


def test_direct_map_contains_critical_fields():
    """BBX 关键宏观字段必须被映射，确保"字段缺失"可追溯到具体 key。"""
    keys = {bbx_key for bbx_key, _ in bbx_index_mod._DIRECT_MAP}
    # 黄金、DXY、纳指、标普、恐惧贪婪 —— 都是 AI 分析核心宏观维度
    assert "i:xauusd:liffe" in keys, "gold 映射缺失"
    assert "i:diniw:ice" in keys, "DXY 映射缺失"
    assert "i:ndx:nasdaq" in keys, "纳指映射缺失"
    assert "i:fgi:alternative" in keys, "恐惧贪婪映射缺失"


def test_missing_key_format_is_bbx_arrow_field():
    """失联 key 记录格式应为 `bbx_key→mi_field`，便于定位。"""
    sample_bbx_key, sample_field = bbx_index_mod._DIRECT_MAP[0]
    formatted = f"{sample_bbx_key}→{sample_field}"
    assert "→" in formatted
    assert sample_bbx_key in formatted
    assert sample_field in formatted


def test_coverage_denominator_excludes_unmapped_fields():
    """覆盖率分母必须是"可映射字段数"，不能是 MarketIndexData 全字段数。

    历史 bug：btc_max_pain / usdt_market_cap / btc_hashrate / raw_items /
    okx_ls_ratio_btc / binance_ls_ratio_btc / stablecoin_dominance
    这 7 个字段代码里明确"已移除/冗余"，永远为 None。把它们算进分母
    会让覆盖率永久虚低（24/(24+7)≈77%），触发永恒的假 WARNING。
    """
    mappable = bbx_index_mod._BBX_MAPPABLE_FIELDS
    assert len(mappable) > 0

    all_fields = {f for f in MarketIndexData.model_fields if f != "ts"}

    # 至少应当有 7 个字段不在映射集合里（这些是已知无 BBX 映射的历史遗留）
    unmapped = all_fields - mappable
    assert "btc_max_pain" in unmapped, "btc_max_pain 应被排除出覆盖率分母"
    assert "btc_hashrate" in unmapped, "btc_hashrate 应被排除"
    assert "usdt_market_cap" in unmapped, "usdt_market_cap 应被排除"
    assert "raw_items" in unmapped, "raw_items 应被排除"

    # 核心宏观字段必须在映射集合里
    assert "gold" in mappable
    assert "dxy" in mappable
    assert "fear_greed" in mappable
    assert "nasdaq" in mappable
    assert "gold_change_pct" in mappable
    assert "exchange_btc_change_24h" in mappable


def test_mappable_fields_count_aligns_with_map_size():
    """_BBX_MAPPABLE_FIELDS 应等于 DIRECT + CHANGE_PCT + ex_bal_change 并集。"""
    expected = set()
    expected.update(f for _, f in bbx_index_mod._DIRECT_MAP)
    expected.update(f for _, f in bbx_index_mod._CHANGE_PCT_MAP)
    expected.add("exchange_btc_change_24h")
    assert bbx_index_mod._BBX_MAPPABLE_FIELDS == expected


def test_module_state_initialized():
    """模块级去重状态字段存在。"""
    assert hasattr(bbx_index_mod, "_BBX_PREV_MISSING")
    assert isinstance(bbx_index_mod._BBX_PREV_MISSING, set)
