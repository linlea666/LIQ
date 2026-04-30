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
        # 反路径依赖硬提示必须存在（防止 AI 路径依赖"上轮 LONG 这轮也 LONG"）
        assert "禁止路径依赖" in text
        assert "仅作连贯性参考" in text
        assert "不是真理" in text


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

    def test_sweep_state_assessment_in_schema(self):
        """G-15 必修：sweep_state_assessment 必须在 schema 描述里 + 验证规则里出现。"""
        from ai.strategic_prompts import SYSTEM_PROMPT
        # schema 字段
        assert "sweep_state_assessment" in SYSTEM_PROMPT
        assert "nearest_upper_band" in SYSTEM_PROMPT
        assert "nearest_lower_band" in SYSTEM_PROMPT
        # 状态枚举（区分上下方场景）
        assert "swept_rejected" in SYSTEM_PROMPT
        assert "swept_accepted" in SYSTEM_PROMPT
        assert "swept_reclaimed" in SYSTEM_PROMPT
        assert "swept_failed" in SYSTEM_PROMPT
        # preferred_action 枚举
        assert "wait_for_sweep" in SYSTEM_PROMPT
        assert "wait_for_reclaim" in SYSTEM_PROMPT
        assert "limit_probe_ok" in SYSTEM_PROMPT
        # 验证规则：§8 有聚合带时必填
        assert "聚合带时" in SYSTEM_PROMPT and "sweep_state_assessment" in SYSTEM_PROMPT


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


class TestStopBandAggregationRendering:
    """GPT-3 必修：§8 渲染层把 N 个独立 cluster 聚合成"扫单磁铁带"。

    根因：上一版 §8 给 AI 1d/7d/30d × 8 = 24 个独立 100 美元 bins，AI 无法
    形成"带"的整体认知，反骑墙规则的"扫前/扫中/扫后"判断失去结构化依据。
    """

    def _snap_with_dense_clusters(self, current_price=76280.0, atr=400.0):
        from ai.snapshot import build_ai_snapshot
        from models.liquidation import LiqCluster
        from models.snapshot import LiquidationMapBlock

        snap = build_ai_snapshot(
            coin="BTC", price=current_price,
            high_24h=current_price + 1000, low_24h=current_price - 1000,
            atr=atr, market_temp_score=50, pin_risk_level="low",
        )
        # 模拟 GPT 反馈中真实场景：上方 76400/76500/76600/76700/76800 五个连续 bins
        snap.liq_map_block_1d = LiquidationMapBlock(
            cycle="1d", imbalance_ratio=1.13,
            clusters_above=[
                LiqCluster(
                    price_center=76400.0, price_from=76350.0, price_to=76450.0,
                    total_usd=85_000_000.0, side="short", distance_pct=0.16,
                    exchange_count=2,
                ),
                LiqCluster(
                    price_center=76500.0, price_from=76450.0, price_to=76550.0,
                    total_usd=72_000_000.0, side="short", distance_pct=0.29,
                    exchange_count=2,
                ),
                LiqCluster(
                    price_center=76800.0, price_from=76750.0, price_to=76850.0,
                    total_usd=242_000_000.0, side="short", distance_pct=0.68,
                    exchange_count=3,
                ),
            ],
            clusters_below=[
                LiqCluster(
                    price_center=75000.0, price_from=74950.0, price_to=75050.0,
                    total_usd=200_000_000.0, side="long", distance_pct=1.68,
                    exchange_count=3,
                ),
                LiqCluster(
                    price_center=75300.0, price_from=75250.0, price_to=75350.0,
                    total_usd=442_000_000.0, side="long", distance_pct=1.29,
                    exchange_count=3,
                ),
            ],
        )
        return snap

    def test_above_clusters_rendered_as_aggregated_band(self):
        """上方 3 个相邻 cluster 应聚合成一条带，并在文本里出现"聚合带"标签
        + peak / total / bins / 距离区间。"""
        from ai.strategic_prompts import build_user_prompt
        snap = self._snap_with_dense_clusters()
        text, _ = build_user_prompt(snap)

        # 聚合带视图存在
        assert "聚合带" in text
        # 阈值 = max(0.4×400, 0.4%×76280) = 305
        # 76400→76500 间距 100 ≤ 305 合并；76500→76800 间距 300 ≤ 305 合并 → 1 条带
        # peak 是 76800（USD 最大）
        assert "peak=$76,800" in text
        # total = 85M + 72M + 242M = 399M
        assert "total=399.00M" in text or "total=399M" in text
        # bins=3
        assert "bins=3" in text

    def test_below_clusters_aggregated_with_negative_distance(self):
        """下方多头止损带聚合距离为负（与 §6/§7 一致）。"""
        from ai.strategic_prompts import build_user_prompt
        snap = self._snap_with_dense_clusters()
        text, _ = build_user_prompt(snap)
        # 75000→75300 间距 300 ≤ 305 合并；_fmt_price 输出 `$75,000.00` 形式
        assert "[$75,000.00, $75,300.00]" in text
        # 距离负号（下方）
        for ln in text.splitlines():
            if "$75,000.00" in ln and "$75,300.00" in ln and "聚合带" not in ln:
                assert "-1." in ln, (
                    f"下方聚合带距离必须为负 | line={ln!r}"
                )
                break
        else:
            assert False, "未找到下方聚合带的渲染行"

    def test_top3_detail_preserved_for_audit(self):
        """聚合带后仍保留明细 top 3，便于 sweep_state_assessment 引用 + 审计。"""
        from ai.strategic_prompts import build_user_prompt
        snap = self._snap_with_dense_clusters()
        text, _ = build_user_prompt(snap)
        # "明细 top" 标签存在
        assert "明细 top" in text
        # 至少出现 1 条原 cluster 行（旧格式：side=`short`）
        assert "side=`short`" in text

    def test_no_atr_falls_back_to_legacy_render(self):
        """ATR=0 + last_price=0 极端场景退化到旧 cluster 列表（不聚合）。"""
        from ai.strategic_prompts import build_user_prompt
        from models.liquidation import LiqCluster
        from models.snapshot import LiquidationMapBlock

        from ai.snapshot import build_ai_snapshot
        snap = build_ai_snapshot(
            coin="BTC", price=0.0, high_24h=0.0, low_24h=0.0,
            atr=0.0, market_temp_score=50, pin_risk_level="low",
        )
        snap.liq_map_block_1d = LiquidationMapBlock(
            cycle="1d", imbalance_ratio=1.0,
            clusters_above=[
                LiqCluster(
                    price_center=76400.0, price_from=76350, price_to=76450,
                    total_usd=85_000_000, side="short", distance_pct=0.16,
                ),
            ],
        )
        text, _ = build_user_prompt(snap)
        # 不崩；side=`short` 仍然渲染（旧格式 fallback）
        assert "side=`short`" in text


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


# ────────────────────────────────────────────────────────────────────────────
# G-10：§5 OpportunityBoard 为空时拒绝原因渲染（必修组）
# ────────────────────────────────────────────────────────────────────────────


class TestOpportunityBoardEmptyDiagnostics:
    @staticmethod
    def _brain_with_zones(zones: list, last_price: float = 76_000.0,
                          atr: float = 400.0) -> "TradingBrainSnapshot":
        from models.trading_brain import (
            BrainContextChips,
            BrainDataQuality,
            TradingBrainSnapshot,
        )
        return TradingBrainSnapshot(
            coin="BTC",
            ts=1700000000,
            last_price=last_price,
            atr=atr,
            context=BrainContextChips(),
            zones=zones,
            data_quality=BrainDataQuality(),
            opportunities=[],
        )

    @staticmethod
    def _zone(*, support_trust=0.85, distance_pct=-1.0, data_confidence=0.85,
              dominant_role="spot_defense", price_mid=75_000.0):
        from models.trading_brain import BrainPriceZone, BrainZoneRoles
        return BrainPriceZone(
            zone_id=f"z_{int(price_mid)}",
            coin="BTC",
            price_low=price_mid * 0.998,
            price_high=price_mid * 1.002,
            price_mid=price_mid,
            distance_pct=distance_pct,
            roles=BrainZoneRoles(key_level=True, spot_supply_wall=True),
            dominant_role=dominant_role,
            dominant_label="测试区",
            support_trust=support_trust,
            resistance_trust=0.0,
            sweep_attractiveness=0.30,
            break_through_risk=0.20,
            data_confidence=data_confidence,
            evidence=["测试证据"],
        )

    def test_empty_zones_falls_back_to_legacy_message(self):
        """zones 空时旧文案保留，不调用诊断（避免 noisy 输出）。"""
        from ai.snapshot import build_ai_snapshot
        from ai.strategic_prompts import build_user_prompt

        brain = self._brain_with_zones([])
        snap = build_ai_snapshot(
            coin="BTC", price=76_000, high_24h=77_000, low_24h=75_000,
            atr=400, market_temp_score=50, pin_risk_level="low",
            trading_brain=brain,
        )
        text, _ = build_user_prompt(snap)
        # 旧文案路径
        assert "无 Opportunity 候选" in text
        # zones 空时不应出现"拒绝原因诊断"
        assert "拒绝原因诊断" not in text

    def test_low_trust_zone_renders_trust_too_low_reason(self):
        """zones 存在但全部 trust 不足 → §5 应渲染 trust_too_low 拒绝原因。"""
        from ai.snapshot import build_ai_snapshot
        from ai.strategic_prompts import build_user_prompt

        # support_trust=0.50 < 0.70 → trust_too_low
        # 至少加个上方 target zone 让 RR/targets 路径不是首要拒绝
        weak = self._zone(support_trust=0.50, distance_pct=-1.0, price_mid=75_000.0)
        target = self._zone(support_trust=0.40, distance_pct=2.0, price_mid=77_500.0)
        brain = self._brain_with_zones([weak, target])
        snap = build_ai_snapshot(
            coin="BTC", price=76_000, high_24h=77_000, low_24h=75_000,
            atr=400, market_temp_score=50, pin_risk_level="low",
            trading_brain=brain,
        )
        text, _ = build_user_prompt(snap)
        s5_start = text.index("## §5")
        s6_start = text.index("## §6")
        s5 = text[s5_start:s6_start]
        assert "拒绝原因诊断" in s5
        # 必须出现 trust_too_low（reason_code）和"信任分不足"（中文标签）
        assert "trust_too_low" in s5
        assert "信任分不足" in s5

    def test_distance_too_far_zone_renders_distance_reason(self):
        """zones 距离过远 → §5 出现 distance_too_far。"""
        from ai.snapshot import build_ai_snapshot
        from ai.strategic_prompts import build_user_prompt

        far = self._zone(distance_pct=-5.0, price_mid=72_000.0)
        brain = self._brain_with_zones([far])
        snap = build_ai_snapshot(
            coin="BTC", price=76_000, high_24h=77_000, low_24h=75_000,
            atr=400, market_temp_score=50, pin_risk_level="low",
            trading_brain=brain,
        )
        text, _ = build_user_prompt(snap)
        s5 = text[text.index("## §5"):text.index("## §6")]
        assert "distance_too_far" in s5
        # 中文标签必须出现，便于 AI 直接复述
        assert "距当前价过远" in s5

    def test_rejection_clusters_by_reason_with_count(self):
        """多 zone 同 reason 应聚类为一行（含命中次数）。"""
        from ai.snapshot import build_ai_snapshot
        from ai.strategic_prompts import build_user_prompt

        # 3 个全部 trust 不足的 zone
        zones = [
            self._zone(support_trust=0.50, distance_pct=-1.0, price_mid=75_000.0),
            self._zone(support_trust=0.55, distance_pct=-0.8, price_mid=75_200.0),
            self._zone(support_trust=0.45, distance_pct=-1.2, price_mid=74_800.0),
        ]
        brain = self._brain_with_zones(zones)
        snap = build_ai_snapshot(
            coin="BTC", price=76_000, high_24h=77_000, low_24h=75_000,
            atr=400, market_temp_score=50, pin_risk_level="low",
            trading_brain=brain,
        )
        text, _ = build_user_prompt(snap)
        s5 = text[text.index("## §5"):text.index("## §6")]
        # 至少 3 条 trust_too_low（3 zone × 2 setup_type 含 fake_break_reclaim_long）
        assert "trust_too_low" in s5
        assert "命中" in s5  # "命中 N 条" 字样
        assert "zones=" in s5  # zone 数量统计

    def test_rejection_diagnose_does_not_break_prompt_on_exception(self):
        """诊断函数任何异常都应被吞，不应让 build_user_prompt 整体崩溃。"""
        from unittest.mock import patch

        from ai.snapshot import build_ai_snapshot
        from ai.strategic_prompts import build_user_prompt

        zones = [self._zone(support_trust=0.50, price_mid=75_000.0)]
        brain = self._brain_with_zones(zones)
        snap = build_ai_snapshot(
            coin="BTC", price=76_000, high_24h=77_000, low_24h=75_000,
            atr=400, market_temp_score=50, pin_risk_level="low",
            trading_brain=brain,
        )
        with patch(
            "processors.opportunity_engine.diagnose_opportunities",
            side_effect=RuntimeError("boom"),
        ):
            text, _ = build_user_prompt(snap)
            # 异常吞掉后退化到旧文案
            assert "无 Opportunity 候选" in text


# ────────────────────────────────────────────────────────────────────────────
# G-11：§10 Footprint / Absorption 解读规则与强度标注（必修组）
# ────────────────────────────────────────────────────────────────────────────


class TestFootprintAbsorptionInterpretation:
    def test_section_10_renders_interpretation_block(self):
        """§10 顶部必须包含「如何解读」段，让 AI 知道 footprint/absorption 语义。"""
        from ai.strategic_prompts import build_user_prompt

        snap = _empty_snapshot()
        text, _ = build_user_prompt(snap)
        s10 = text[text.index("## §10"):text.index("## §11")]
        # 标题加强语义
        assert "验证 §6/§7 的真伪" in s10
        # 解读段
        assert "如何解读 §10" in s10
        # 三个语义块都必须存在
        assert "Footprint imbalance" in s10
        assert "Absorption（吸收）" in s10
        assert "方向语义" in s10
        # 共振判定（与 §6/§7 的桥接）
        assert "共振判定" in s10
        # 关键反语义：zones_support = 多方吃货
        assert "多方在该价位吃货" in s10
        assert "空方在该价位反手压盘" in s10

    def test_footprint_strength_label_for_one_sided(self):
        """ratio≥999 → one-sided + 极强标签（同时回归 PR-5 之前就存在的"读错字段"bug）。"""
        from ai.snapshot import build_ai_snapshot
        from ai.strategic_prompts import build_user_prompt
        from models.market_action import (
            FootprintBarStats,
            FootprintSnapshot,
            MarketActionFacts,
        )

        fp = FootprintSnapshot(
            contract_latest=FootprintBarStats(
                ts=1700000000,
                total_buy_usd=5_000_000.0,
                total_sell_usd=2_000.0,
                delta_usd=4_998_000.0,
                delta_pct=0.99,
                bar_closed=True,
                top_imbalance_zones=[
                    {
                        "price": 75_500.0,
                        "buy": 5_000_000.0,
                        "sell": 2_000.0,
                        "ratio": 999.0,
                        "side": "stacked_buy",
                    }
                ],
            ),
        )
        facts = MarketActionFacts(
            coin="BTC", timestamp=1700000000, footprint=fp,
        )
        snap = build_ai_snapshot(
            coin="BTC", price=76_000, high_24h=77_000, low_24h=75_000,
            atr=400, market_temp_score=50, pin_risk_level="low",
            market_action_facts=facts,
        )
        text, _ = build_user_prompt(snap)
        s10 = text[text.index("## §10"):text.index("## §11")]
        assert "one-sided" in s10
        assert "极强" in s10
        # bar_closed 必须从 contract_latest 正确读出（旧 bug：从顶层永远 None）
        assert "bar_closed: contract=`True`" in s10

    def test_footprint_strength_label_for_medium(self):
        """3.0 ≤ ratio < 5.0 → 中标签。"""
        from ai.snapshot import build_ai_snapshot
        from ai.strategic_prompts import build_user_prompt
        from models.market_action import (
            FootprintBarStats,
            FootprintSnapshot,
            MarketActionFacts,
        )

        fp = FootprintSnapshot(
            spot_latest=FootprintBarStats(
                ts=1700000000,
                total_buy_usd=300_000.0,
                total_sell_usd=80_000.0,
                delta_usd=220_000.0,
                delta_pct=0.58,
                bar_closed=True,
                top_imbalance_zones=[
                    {
                        "price": 75_500.0,
                        "buy": 300_000.0,
                        "sell": 80_000.0,
                        "ratio": 3.5,
                        "side": "buy",
                    }
                ],
            ),
        )
        facts = MarketActionFacts(
            coin="BTC", timestamp=1700000000, footprint=fp,
        )
        snap = build_ai_snapshot(
            coin="BTC", price=76_000, high_24h=77_000, low_24h=75_000,
            atr=400, market_temp_score=50, pin_risk_level="low",
            market_action_facts=facts,
        )
        text, _ = build_user_prompt(snap)
        s10 = text[text.index("## §10"):text.index("## §11")]
        assert "3.5x" in s10
        assert " · 中" in s10
        # 通过 spot_latest 渲染 → 必须出现"现货 top imbalance zones"
        assert "现货 top imbalance zones" in s10

    def test_absorption_strength_strong_label(self):
        """vol≥5M + |delta|<0.05 + bar_count≥3 → 强吸收标签。"""
        from ai.snapshot import build_ai_snapshot
        from ai.strategic_prompts import build_user_prompt
        from models.market_action import (
            AbsorptionSnapshot,
            AbsorptionZone,
            MarketActionFacts,
        )

        absp = AbsorptionSnapshot(
            zones_support=[
                AbsorptionZone(
                    price=75_000.0,
                    side="support",
                    taker_volume_usd=8_000_000.0,
                    delta_pct_abs_avg=0.03,
                    bar_count=4,
                    age_hours=1.5,
                ),
            ],
            zones_resistance=[],
        )
        facts = MarketActionFacts(
            coin="BTC", timestamp=1700000000, absorption=absp,
        )
        snap = build_ai_snapshot(
            coin="BTC", price=76_000, high_24h=77_000, low_24h=75_000,
            atr=400, market_temp_score=50, pin_risk_level="low",
            market_action_facts=facts,
        )
        text, _ = build_user_prompt(snap)
        s10 = text[text.index("## §10"):text.index("## §11")]
        assert "Support 带" in s10
        assert "强" in s10  # 必须出现强标签
        # 同时确保改动没破坏既有数值渲染
        assert "$75,000" in s10
        assert "$8.00M" in s10 or "$8,000,000" in s10  # vol


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
