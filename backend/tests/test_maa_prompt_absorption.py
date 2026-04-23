"""MAA Prompt · Orderbook → Absorption 替换回归

验证：
1. SYSTEM_PROMPT 维度白名单已删除 Orderbook、加入 Absorption
2. USER_PROMPT §3 不再渲染 Orderbook 章节
3. USER_PROMPT 在 Footprint 之后渲染 Absorption 章节
4. 空 absorption 时有明确"无显著吸收带"降级文案
5. 有 absorption 时显示 Support / Resistance 带 + 元数据
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ai.market_action_prompts import SYSTEM_PROMPT, build_user_prompt
from models.market_action import (
    AbsorptionSnapshot,
    AbsorptionZone,
    MarketActionFacts,
    PriceSnapshot,
)


# ─────────────────────────────────────────────────────────────────────────────
# 1. SYSTEM_PROMPT 维度白名单
# ─────────────────────────────────────────────────────────────────────────────

def test_system_prompt_dimension_whitelist_updated():
    # 白名单行（列举 12 个维度）
    assert "`Absorption`" in SYSTEM_PROMPT
    assert "| `Options`" in SYSTEM_PROMPT
    # Orderbook 不应出现在白名单正文（Schema JSON 那行也应该不含）
    # 允许在"说明注释"里提 Orderbook（说明它被移除了）
    # 精确校验：两个关键位置
    assert "Taker|Absorption|Options" in SYSTEM_PROMPT, "schema 白名单未更新"
    assert "| `Taker` | `Absorption` | `Options`" in SYSTEM_PROMPT, "Evidence 白名单未更新"


def test_system_prompt_mentions_absorption_fields():
    assert "absorption.zones_support" in SYSTEM_PROMPT
    assert "taker_volume_usd" in SYSTEM_PROMPT
    assert "delta_pct_abs_avg" in SYSTEM_PROMPT
    assert "bar_count" in SYSTEM_PROMPT
    assert "age_hours" in SYSTEM_PROMPT


# ─────────────────────────────────────────────────────────────────────────────
# 2. USER_PROMPT §3 不再有 Orderbook 章节
# ─────────────────────────────────────────────────────────────────────────────

def test_user_prompt_no_orderbook_section():
    facts = MarketActionFacts(
        coin="BTC", timestamp=1_700_000_000,
        price=PriceSnapshot(last=78_000),
    )
    prompt, sections = build_user_prompt(facts)
    # §3 title 不含 Orderbook
    anchors = [s.title for s in sections]
    s3_title = next((t for t in anchors if "A 级关键区分" in t), "")
    assert "Orderbook" not in s3_title
    assert "Absorption" in s3_title
    # 正文里 §3 区域不应该有"盘口挂单失衡度"子章节标题
    assert "### Orderbook" not in prompt
    assert "盘口挂单失衡度" not in prompt
    # book_imbalance_pct 字段不再渲染
    assert "book_imbalance_pct" not in prompt


# ─────────────────────────────────────────────────────────────────────────────
# 3. 空 absorption 降级
# ─────────────────────────────────────────────────────────────────────────────

def test_user_prompt_empty_absorption_fallback():
    facts = MarketActionFacts(
        coin="BTC", timestamp=1_700_000_000,
        price=PriceSnapshot(last=78_000),
    )
    prompt, _ = build_user_prompt(facts)
    assert "### Absorption" in prompt
    assert "无显著吸收带" in prompt


# ─────────────────────────────────────────────────────────────────────────────
# 4. 有 absorption 的完整渲染
# ─────────────────────────────────────────────────────────────────────────────

def test_user_prompt_renders_absorption_zones():
    facts = MarketActionFacts(
        coin="BTC", timestamp=1_700_000_000,
        price=PriceSnapshot(last=78_000),
        absorption=AbsorptionSnapshot(
            zones_support=[
                AbsorptionZone(
                    price=76_500, side="support",
                    taker_volume_usd=12_000_000, delta_pct_abs_avg=0.05,
                    bar_count=2, age_hours=1.5, source="contract",
                ),
            ],
            zones_resistance=[
                AbsorptionZone(
                    price=79_500, side="resistance",
                    taker_volume_usd=8_000_000, delta_pct_abs_avg=0.12,
                    bar_count=1, age_hours=0.5, source="contract",
                ),
            ],
            total_zone_count=2, window_hours=3.0, lookback_bars=3, fallback_used=False,
        ),
    )
    prompt, _ = build_user_prompt(facts)
    # 标题 + 窗口 meta
    assert "### Absorption" in prompt
    assert "覆盖窗口：近 3.0h" in prompt or "覆盖窗口：近 3h" in prompt
    assert "3 根 1h bar" in prompt
    assert "放宽兜底：否" in prompt
    # Support 带
    assert "Support 带" in prompt
    assert "76,500" in prompt or "76500" in prompt
    assert "12.00M" in prompt
    # Resistance 带
    assert "Resistance 带" in prompt
    assert "79,500" in prompt or "79500" in prompt
    assert "8.00M" in prompt


def test_fallback_used_shows_in_prompt():
    facts = MarketActionFacts(
        coin="BTC", timestamp=1_700_000_000,
        price=PriceSnapshot(last=78_000),
        absorption=AbsorptionSnapshot(
            zones_support=[
                AbsorptionZone(
                    price=77_000, side="support",
                    taker_volume_usd=5_000_000, delta_pct_abs_avg=0.25,
                    bar_count=1, age_hours=0.2, source="contract",
                ),
            ],
            total_zone_count=1, window_hours=3.0, lookback_bars=3, fallback_used=True,
        ),
    )
    prompt, _ = build_user_prompt(facts)
    assert "放宽兜底：是" in prompt
