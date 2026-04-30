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
# 数据契约修复回归（funding 双源 / IV 量纲 / put_call OI / coinbase 溢价）
# ────────────────────────────────────────────────────────────────────────────


def _make_snap_for_render_checks(**kwargs):
    """构造能触发 §7/§11/§12 全部渲染分支的最小 snapshot。"""
    from ai.snapshot import build_ai_snapshot
    return build_ai_snapshot(
        coin="BTC", price=65000, high_24h=66000, low_24h=64000,
        atr=500, market_temp_score=50, pin_risk_level="low",
        **kwargs,
    )


class TestFundingPercentileSingleSource:
    """§7 不再渲染 funding 30d 百分位（避免与 §9 双源刻度混淆）。

    根因记录：cg.funding_percentile_30d 是 0-1 浮点，ffu.percentile_30d 是
    1-100 整数；同名"30d 百分位"在同一份 prompt 出现两个值（如 0 vs 68），
    AI 在 evidence_matrix 里多次抄错。修复策略：§7 删除该行，§9 单点输出。
    """

    def test_section7_no_longer_shows_funding_percentile(self):
        from ai.strategic_prompts import build_user_prompt
        from models.orderbook_pressure import PositionCrowdingSnapshot

        snap = _make_snap_for_render_checks()
        snap.crowding_global = PositionCrowdingSnapshot(
            oi_delta_1h_pct=0.5,
            oi_delta_24h_pct=-1.2,
            funding_now_pct=0.001,
            funding_percentile_30d=0.0,  # 0-1 刻度的 0
        )

        text, _ = build_user_prompt(snap)

        # 确认 §7 仍渲染 funding_now_pct
        assert "Funding now=" in text
        # 但 §7 不再含 "30d 百分位"——避免 cg(0-1) / ffu(0-100) 同名冲突
        # 用 markdown header 锚定章节边界（避免内容里的 §N 字符串引用提前匹配）
        sec7_start = text.index("## §7")
        sec8_start = text.index("## §8")
        sec7_block = text[sec7_start:sec8_start]
        assert "Funding 百分位" not in sec7_block, (
            "§7 子段标题不应再宣称 Funding 百分位"
        )
        assert "30d 百分位" not in sec7_block, (
            "§7 不应再渲染 30d 百分位（与 §9 ffu.percentile_30d 双源冲突）"
        )

    def test_section9_keeps_funding_percentile_30d(self):
        """§9 仍保留 ffu.percentile_30d（1-100 刻度）作为唯一真值源。"""
        from ai.strategic_prompts import build_user_prompt
        from models.market_action import FundingSnapshot, MarketActionFacts

        facts = MarketActionFacts(
            coin="BTC", timestamp=1700000000,
            funding=FundingSnapshot(
                avg_current=0.0001, percentile_30d=68, percentile_7d=71,
            ),
        )
        snap = _make_snap_for_render_checks(market_action_facts=facts)
        text, _ = build_user_prompt(snap)

        sec9_start = text.index("## §9")
        sec10_start = text.index("## §10")
        sec9_block = text[sec9_start:sec10_start]
        assert "30d 百分位=68" in sec9_block
        assert "7d 百分位=71" in sec9_block


class TestOptionFieldRenderHardening:
    """§12 期权字段渲染防御：IV 量纲 / put_call_oi 缺失保护。"""

    def test_btc_iv_normalized_when_decimal_scale(self):
        """上游 BBX 历史返回过 0-1 小数（如 0.38 = 38%）；
        必须 ×100 后显示，避免出现 "BTC IV=0.38%" 让 AI 误判极低波动率。"""
        from ai.strategic_prompts import build_user_prompt

        snap = _make_snap_for_render_checks(btc_implied_vol=0.38)
        text, _ = build_user_prompt(snap)

        assert "BTC IV=38.00%" in text
        assert "BTC IV=0.38%" not in text

    def test_btc_iv_passthrough_when_percent_scale(self):
        """若上游已是百分比刻度（如 38），原样显示。"""
        from ai.strategic_prompts import build_user_prompt

        snap = _make_snap_for_render_checks(btc_implied_vol=38.0)
        text, _ = build_user_prompt(snap)

        assert "BTC IV=38.00%" in text

    def test_put_call_oi_zero_skipped(self):
        """put/call OI 比例为 0 = 上游数据缺失，不应作为"持仓平衡"渲染。"""
        from ai.strategic_prompts import build_user_prompt

        snap = _make_snap_for_render_checks(btc_put_call_oi=0.0)
        text, _ = build_user_prompt(snap)

        assert "put/call OI" not in text

    def test_put_call_oi_positive_renders(self):
        """正常 P/C 比例（> 0）正常显示。"""
        from ai.strategic_prompts import build_user_prompt

        snap = _make_snap_for_render_checks(btc_put_call_oi=1.25)
        text, _ = build_user_prompt(snap)

        assert "put/call OI=1.25" in text


class TestCoinbasePremiumNearZero:
    """Coinbase 溢价极小值不应渲染为 "-0.00"（看起来像 bug）。"""

    def test_near_zero_premium_renders_approx(self):
        from ai.strategic_prompts import build_user_prompt

        snap = _make_snap_for_render_checks()
        snap.coinbase_premium = -0.001  # |x| < 0.01

        text, _ = build_user_prompt(snap)

        assert "Coinbase 溢价=≈0" in text
        assert "-0.00" not in text  # 旧渲染瑕疵

    def test_meaningful_premium_renders_value(self):
        from ai.strategic_prompts import build_user_prompt

        snap = _make_snap_for_render_checks()
        snap.coinbase_premium = -1.25

        text, _ = build_user_prompt(snap)

        assert "Coinbase 溢价=-1.25" in text


# ────────────────────────────────────────────────────────────────────────────
# extract_json_payload 复用
# ────────────────────────────────────────────────────────────────────────────

# ────────────────────────────────────────────────────────────────────────────
# GTP feedback 必采项（PR-6）：硬规则 + §8 语义 + 距离符号 + 新闻简报智能截断
# ────────────────────────────────────────────────────────────────────────────


class TestSystemPromptHardRulesV2:
    """GTP 反馈必采的 5 条硬规则必须全部进入 system prompt。

    根因：上一版 prompt 让 AI 把"上方空头止损带"当成"强阻力"——这是市场微观
    结构基础语义错误，必须在 system prompt 一级锁死，而不是依赖渲染层提示。
    """

    def test_anti_fence_sitting_rule_present(self):
        """反骑墙规则：≥4 项条件齐备时禁止只输出 WAIT。"""
        from ai.strategic_prompts import SYSTEM_PROMPT
        assert "反骑墙" in SYSTEM_PROMPT
        assert "条件化" in SYSTEM_PROMPT
        assert "0.3 ATR" in SYSTEM_PROMPT
        # 主决策 WAIT 时仍必须给 alternative_plan
        assert "alternative_plan" in SYSTEM_PROMPT
        assert "不允许" in SYSTEM_PROMPT and "null" in SYSTEM_PROMPT

    def test_liquidation_band_semantic_rule_present(self):
        """清算/止损带语义硬规则：止损带 ≠ 支撑/阻力。"""
        from ai.strategic_prompts import SYSTEM_PROMPT
        assert "清算/止损带语义硬规则" in SYSTEM_PROMPT
        # 关键反例
        assert "不是阻力" in SYSTEM_PROMPT
        assert "不是支撑" in SYSTEM_PROMPT
        # 正确语义术语
        assert "轧空磁铁" in SYSTEM_PROMPT
        assert "扫多磁铁" in SYSTEM_PROMPT
        # 真阻力/支撑指引
        assert "现货卖墙" in SYSTEM_PROMPT
        assert "现货买墙" in SYSTEM_PROMPT

    def test_high_break_risk_rule_present(self):
        """高争夺区处理规则：support_trust 高 + break_risk 高 → 不许直接限价试错。"""
        from ai.strategic_prompts import SYSTEM_PROMPT
        assert "高争夺" in SYSTEM_PROMPT or "高争夺区" in SYSTEM_PROMPT
        assert "break_through_risk" in SYSTEM_PROMPT
        # 等待扫后反应
        assert "扫后反应" in SYSTEM_PROMPT or "扫后" in SYSTEM_PROMPT

    def test_hard_stop_threshold_loosened(self):
        """NO_TRADE 阈值放宽：仅当 ≥2 个核心源缺失时才触发 hard_stop_triggered。"""
        from ai.strategic_prompts import SYSTEM_PROMPT
        # 核心阈值表述
        assert "≥2 个" in SYSTEM_PROMPT or "≥2 个核心源" in SYSTEM_PROMPT
        # 数据级阻断 vs 交易止损消歧
        assert "数据级阻断" in SYSTEM_PROMPT

    def test_markdown_rule_no_longer_self_contradicts(self):
        """旧版"禁止 Markdown / emoji / 任何 JSON 之外的文字" 与"返回 ```json 代码块"
        互冲——删除冲突表述，明确"JSON 内部禁止 Markdown 格式"。"""
        from ai.strategic_prompts import SYSTEM_PROMPT
        # 仍保留 ```json 代码块要求（下游 extract_json_payload 兼容）
        assert "```json" in SYSTEM_PROMPT
        # 但不再写"禁止 Markdown / emoji / 任何 JSON 之外的文字"这种全局禁令
        # （它会让 AI 困惑：到底要不要 ```json 围栏？）
        assert "JSON 内部禁止 Markdown" in SYSTEM_PROMPT

    def test_subjective_probability_disambiguation(self):
        """probability_pct 明确标注为相对情景权重而非统计概率。"""
        from ai.strategic_prompts import SYSTEM_PROMPT
        assert "相对情景权重" in SYSTEM_PROMPT
        assert "非统计概率" in SYSTEM_PROMPT or "不是统计概率" in SYSTEM_PROMPT

    def test_market_phase_anti_hallucination(self):
        """market_phase / cycle_position 必须基于 §3+§6+§11 共同判断，证据不足填
        insufficient_evidence；防止 AI 幻觉。"""
        from ai.strategic_prompts import SYSTEM_PROMPT
        assert "insufficient_evidence" in SYSTEM_PROMPT
        assert "禁止幻觉" in SYSTEM_PROMPT or "不要靠想象" in SYSTEM_PROMPT


class TestLiqClusterRenderingSemantics:
    """§8 渲染层防御：(1) 距离符号根据 side 动态决定，(2) 标题写明语义。"""

    def _snap_with_liq_block(self, current_price=76280.0):
        """构造含 1d LiquidationMapBlock 的最小 snapshot。"""
        from ai.snapshot import build_ai_snapshot
        from models.liquidation import LiqCluster
        from models.snapshot import LiquidationMapBlock

        snap = build_ai_snapshot(
            coin="BTC", price=current_price,
            high_24h=current_price + 1000, low_24h=current_price - 1000,
            atr=500, market_temp_score=50, pin_risk_level="low",
        )
        # LiqCluster.distance_pct 在生产代码里恒为正（liquidation.py 同时给 above/below
        # 都赋绝对值），渲染层应根据 side 还原符号
        snap.liq_map_block_1d = LiquidationMapBlock(
            cycle="1d",
            imbalance_ratio=1.13,
            clusters_above=[
                LiqCluster(
                    price_center=76800.0, price_from=76700.0, price_to=76900.0,
                    total_usd=242_280_000.0, side="short", distance_pct=0.68,
                    exchange_count=3,
                ),
            ],
            clusters_below=[
                LiqCluster(
                    price_center=75300.0, price_from=75200.0, price_to=75400.0,
                    total_usd=442_000_000.0, side="long", distance_pct=1.29,
                    exchange_count=3,
                ),
            ],
        )
        return snap

    def test_above_short_cluster_distance_is_positive(self):
        """side=short（上方空头止损带）→ 距离正号。"""
        from ai.strategic_prompts import build_user_prompt
        snap = self._snap_with_liq_block()
        text, _ = build_user_prompt(snap)
        assert "$76,800" in text
        # 上方应为正号
        assert "(+0.68%)" in text

    def test_below_long_cluster_distance_is_negative(self):
        """side=long（下方多头止损带）→ 距离负号（GPT-2 必采修复）。

        根因：liquidation.py:42-43 给 clusters_below 的 distance_pct 也赋绝对值
        （而不是带负号），导致 prompt 把 $75,300 渲染成 +1.29% 让 AI 误以为
        在当前价上方。修复策略：渲染层根据 side 还原符号。
        """
        from ai.strategic_prompts import build_user_prompt
        snap = self._snap_with_liq_block()
        text, _ = build_user_prompt(snap)
        assert "$75,300" in text
        # 下方必须显示负号
        assert "(-1.29%)" in text
        # 旧 bug：不能再出现"下方价位 + 正号"
        # 提取 $75,300 那一行做精确断言
        for ln in text.splitlines():
            if "$75,300" in ln:
                assert "+1.29%" not in ln, (
                    f"下方多头止损带不应显示正号 | line={ln!r}"
                )

    def test_section8_title_semantics(self):
        """§8 标题/语义提示明确写"不是阻力 / 不是支撑"，防止 AI 在 evidence 里
        把空头止损带当成强阻力（GPT-1 必采修复）。"""
        from ai.strategic_prompts import build_user_prompt
        snap = self._snap_with_liq_block()
        text, _ = build_user_prompt(snap)
        # 旧标题已被替换
        assert "轧空磁铁" in text
        assert "扫多磁铁" in text
        # 旧标题里还在保留的"上方空头止损"和"下方多头止损"是合理的（用作描述），
        # 但同一行必须紧跟"**不是阻力**" / "**不是支撑**"否定语
        assert "不是阻力" in text
        assert "不是支撑" in text


class TestNewsBriefStructuredRendering:
    """§11 新闻简报智能截断（GPT-13）：解析 JSON 后输出 tldr + sections.bullets[:N]，
    避免上一版"硬截 500 字符 + 中间断裂"的丑陋渲染。"""

    def test_json_brief_extracts_tldr_and_bullets(self):
        from ai.snapshot import build_ai_snapshot
        from ai.strategic_prompts import build_user_prompt

        import json

        brief_payload = {
            "version": 7,
            "tldr_cn": "中东冲突升级 + ETF 资金流出 + 降息预期推迟，整体偏空。",
            "sections": [
                {
                    "section_id": "regulatory",
                    "title_cn": "监管",
                    "bullets": [
                        "韩国交易所要求 KYC 升级",
                        "美国 SEC 推迟 ETF 决议",
                    ],
                },
                {
                    "section_id": "macro",
                    "title_cn": "宏观",
                    "bullets": [
                        "降息预期推迟到 Q2",
                    ],
                },
            ],
            "diff_from_prev_version": "新增中东冲突影响",
        }
        snap = build_ai_snapshot(
            coin="BTC", price=65000, high_24h=66000, low_24h=64000,
            atr=500, market_temp_score=50, pin_risk_level="low",
        )
        snap.news_brief_text = json.dumps(brief_payload, ensure_ascii=False)
        snap.news_brief_version = 7

        text, _ = build_user_prompt(snap)

        assert "新闻简报（v7）" in text
        # tldr 出现
        assert "中东冲突升级" in text
        # section 标题被提取
        assert "[监管]" in text
        assert "[宏观]" in text
        # bullets 提取（每节最多 2 条）
        assert "韩国交易所要求 KYC 升级" in text
        assert "降息预期推迟到 Q2" in text
        assert "版本差异" in text
        # 关键：不再原样输出整段 JSON
        assert '{"version":7' not in text
        assert '"sections":[' not in text

    def test_freeform_brief_falls_back_to_truncate(self):
        """非 JSON 字符串（旧 free-form 文本）→ fallback 到原硬截断逻辑，
        保证向后兼容。"""
        from ai.snapshot import build_ai_snapshot
        from ai.strategic_prompts import build_user_prompt

        snap = build_ai_snapshot(
            coin="BTC", price=65000, high_24h=66000, low_24h=64000,
            atr=500, market_temp_score=50, pin_risk_level="low",
        )
        long_text = "宏观摘要：" + ("ABC " * 200)  # 远超 500 字符
        snap.news_brief_text = long_text
        snap.news_brief_version = 3

        text, _ = build_user_prompt(snap)

        assert "新闻简报（v3）" in text
        # fallback：500 字符 + "…"
        assert "宏观摘要" in text
        assert "…" in text


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
