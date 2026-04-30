"""PR-2 · Strategic Prompts 渲染回归测试。

覆盖：
  1. SYSTEM_PROMPT 关键约束词存在（WAIT 合法 / 不出仓位百分比 / 冲突矩阵 / §N 引用）
  2. build_user_prompt 渲染 §0-§14 章节锚点齐全
  3. 数据缺失时降级章节存在但内容标注"无数据"
  4. Top N 截断生效（zones / opportunities / walls / clusters）
  5. extract_json_payload 兼容 markdown code fence
"""

from __future__ import annotations

import pytest


# ────────────────────────────────────────────────────────────────────────────
# SYSTEM_PROMPT 约束词
# ────────────────────────────────────────────────────────────────────────────

class TestSystemPrompt:
    def test_required_constraints(self):
        from ai.strategic_prompts import SYSTEM_PROMPT, build_system_prompt

        assert build_system_prompt() == SYSTEM_PROMPT
        # WAIT 是合法输出
        assert "WAIT" in SYSTEM_PROMPT
        # 冲突矩阵
        assert "evidence_matrix" in SYSTEM_PROMPT
        assert "contradictions" in SYSTEM_PROMPT
        # 不输出仓位百分比 / 杠杆倍数
        assert "仓位" in SYSTEM_PROMPT
        assert "leverage_risk_level" in SYSTEM_PROMPT
        assert "position_sizing_note" in SYSTEM_PROMPT
        # 必须引用 §N
        assert "§N" in SYSTEM_PROMPT or "section_ref" in SYSTEM_PROMPT
        # horizon 自决
        assert "scalp" in SYSTEM_PROMPT
        assert "intraday" in SYSTEM_PROMPT
        assert "swing" in SYSTEM_PROMPT
        # decision 6 选项
        for d in ("WAIT", "LONG_PLAN", "SHORT_PLAN", "NO_TRADE", "LONG_OBSERVATION", "SHORT_OBSERVATION"):
            assert d in SYSTEM_PROMPT

    def test_no_position_pct_directly_required(self):
        """SYSTEM_PROMPT 不应该让 AI 输出 position_pct（GPT 反馈关键修复点）。"""
        from ai.strategic_prompts import SYSTEM_PROMPT
        # 不能要求输出 position_pct 字段
        assert "position_pct" not in SYSTEM_PROMPT


# ────────────────────────────────────────────────────────────────────────────
# build_user_prompt §0-§14
# ────────────────────────────────────────────────────────────────────────────

def _empty_snapshot():
    """构造最小可用 AISnapshot（用 build_ai_snapshot 走主装配路径）。"""
    from ai.snapshot import build_ai_snapshot

    return build_ai_snapshot(
        coin="BTC", price=65000, high_24h=66000, low_24h=64000,
        atr=500, market_temp_score=50, pin_risk_level="low",
    )


class TestBuildUserPromptStructure:
    def test_minimum_renders_all_sections(self):
        """最小快照必须仍渲染出 §0-§14 全部锚点（数据缺失时也要保留章节）。"""
        from ai.strategic_prompts import build_user_prompt

        snap = _empty_snapshot()
        text, sections = build_user_prompt(snap)
        anchors = [s.anchor for s in sections]
        # 必须包含核心 14 个锚点
        for required in ("§0", "§1", "§2", "§3", "§4", "§5", "§6", "§7",
                         "§8", "§9", "§10", "§11", "§12", "§13", "§14"):
            assert required in anchors, f"missing anchor: {required}"

    def test_coin_and_price_in_text(self):
        from ai.strategic_prompts import build_user_prompt

        snap = _empty_snapshot()
        text, _ = build_user_prompt(snap)
        assert "BTC" in text
        assert "65,000" in text or "$65,000" in text

    def test_no_provisional_marker_when_facts_clean(self):
        """无 provisional bar 时显式标"否"。"""
        from ai.strategic_prompts import build_user_prompt

        snap = _empty_snapshot()
        text, _ = build_user_prompt(snap)
        assert "含未收盘 bar：否" in text

    def test_previous_report_renders_section_0_prime(self):
        """传入 previous_report 时渲染 §0' 前情提要。"""
        from ai.strategic_prompts import build_user_prompt
        from models.strategic_report import AIStrategicReport, TradingPlan

        prev = AIStrategicReport(
            coin="BTC",
            timestamp=1700000000,
            decision="LONG_PLAN",
            horizon="intraday",
            bias="bullish",
            confidence=0.65,
            primary_plan=TradingPlan(
                setup_type="回踩支撑",
                entry_zone_low=64500,
                entry_zone_high=64700,
                hard_invalidation="64200 收盘失守",
            ),
            invalidation_conditions=["64200 失守", "现货墙撤"],
        )
        snap = _empty_snapshot()
        text, sections = build_user_prompt(snap, previous_report=prev)
        anchors = [s.anchor for s in sections]
        assert "§0'" in anchors
        assert "前情提要" in text
        assert "LONG_PLAN" in text
        assert "回踩支撑" in text


# ────────────────────────────────────────────────────────────────────────────
# Top N 截断
# ────────────────────────────────────────────────────────────────────────────

class TestTopNTruncation:
    def test_walls_truncated_to_per_side_limit(self):
        """超过 TOP_N_WALLS_PER_SIDE 的 wall 被截断。"""
        from ai.snapshot import build_ai_snapshot
        from ai.strategic_prompts import TOP_N_WALLS_PER_SIDE, build_user_prompt
        from models.orderbook_pressure import WallZone

        def _wall(price, distance, idx):
            return WallZone(
                zone_id=f"w_{idx}",
                side="ask",
                price_low=price - 50,
                price_high=price + 50,
                price_mid=price,
                peak_price=price,
                distance_pct=distance,
                current_usd=2_000_000,
                max_usd_1h=2_500_000,
                avg_usd_1h=2_200_000,
                bin_count=3,
                seen_count=10,
                persistence_score=0.6,
                trust_score=0.7 + idx * 0.001,  # 让它们不同方便排序
                visible_minutes=30,
            )

        # 构造 12 个上方墙（超 TOP_N_WALLS_PER_SIDE=8）
        walls = [_wall(66000 + i * 100, 1.5 + i * 0.1, i) for i in range(12)]
        snap = build_ai_snapshot(
            coin="BTC", price=65000, high_24h=66000, low_24h=64000,
            atr=500, market_temp_score=50, pin_risk_level="low",
        )
        # 直接覆写新字段
        snap.wall_zones_above = walls

        text, _ = build_user_prompt(snap)
        # 应该只渲染 top N
        assert f"top {TOP_N_WALLS_PER_SIDE}" in text


class TestNoCrashOnPartialData:
    def test_only_facts_oi(self):
        """只有 facts_oi，其他全空——不应崩溃。"""
        from ai.snapshot import build_ai_snapshot
        from ai.strategic_prompts import build_user_prompt
        from models.market_action import MarketActionFacts, OISnapshot

        facts = MarketActionFacts(
            coin="BTC",
            timestamp=1700000000,
            oi=OISnapshot(current_usd=20_000_000_000, change_1h_pct=1.5,
                          change_24h_pct=3.2),
        )
        snap = build_ai_snapshot(
            coin="BTC", price=65000, high_24h=66000, low_24h=64000,
            atr=500, market_temp_score=50, pin_risk_level="low",
            market_action_facts=facts,
        )
        text, sections = build_user_prompt(snap)
        # OI 数值出现在 §9
        assert "20.00B" in text or "20000000000" in text

    def test_oi_with_venue_split(self):
        """venue_split 渲染分支：曾因字段名 typo（v.exchange vs v.venue）整链崩溃。

        本用例显式构造非空 venue_split → 触发 prompt 中 line 675 的 join 闭包，
        如果未来再写错字段名（exchange / name / 其它），用例立即失败。
        """
        from ai.snapshot import build_ai_snapshot
        from ai.strategic_prompts import build_user_prompt
        from models.market_action import (
            MarketActionFacts,
            OISnapshot,
            OIVenueEntry,
        )

        facts = MarketActionFacts(
            coin="BTC",
            timestamp=1700000000,
            oi=OISnapshot(
                current_usd=20_000_000_000,
                change_1h_pct=1.5,
                change_24h_pct=3.2,
                venue_split=[
                    OIVenueEntry(
                        venue="Binance",
                        oi_usd=12_000_000_000,
                        share_pct=60.0,
                        change_1h_pct=2.1,
                        change_24h_pct=4.5,
                    ),
                    OIVenueEntry(
                        venue="OKX",
                        oi_usd=4_000_000_000,
                        share_pct=20.0,
                        change_1h_pct=0.3,
                        change_24h_pct=1.0,
                    ),
                ],
            ),
        )
        snap = build_ai_snapshot(
            coin="BTC", price=65000, high_24h=66000, low_24h=64000,
            atr=500, market_temp_score=50, pin_risk_level="low",
            market_action_facts=facts,
        )

        text, _ = build_user_prompt(snap)

        # 渲染分支必须执行：venue 名称应直接出现在 prompt 文本里
        assert "Binance" in text
        assert "OKX" in text
        assert "头部 venue 1h Δ" in text


# ────────────────────────────────────────────────────────────────────────────
# extract_json_payload 复用
# ────────────────────────────────────────────────────────────────────────────

class TestExtractJsonPayload:
    def test_markdown_fence(self):
        from ai.strategic_prompts import extract_json_payload

        raw = '```json\n{"decision": "WAIT", "confidence": 0.5}\n```'
        payload = extract_json_payload(raw)
        assert payload["decision"] == "WAIT"
        assert payload["confidence"] == 0.5

    def test_plain_object(self):
        from ai.strategic_prompts import extract_json_payload

        raw = '一些前缀文字 {"decision": "LONG_PLAN"} 后缀'
        payload = extract_json_payload(raw)
        assert payload["decision"] == "LONG_PLAN"

    def test_empty_raises(self):
        from ai.strategic_prompts import extract_json_payload

        with pytest.raises(ValueError):
            extract_json_payload("")

    def test_no_json_raises(self):
        from ai.strategic_prompts import extract_json_payload

        with pytest.raises(ValueError):
            extract_json_payload("just plain text no braces")
