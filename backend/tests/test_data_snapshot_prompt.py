"""跨模型对照用「纯数据切片」生成器单测（2026-04-22 新增）

背景：
  用户希望把同一份行情快照分别喂给 Claude / Gemini / GPT-4 / Kimi 等多个
  AI 模型做独立方向判断，对比谁的预测准确率更高。
  我方 system_prompt / user_prompt 都含有"8 维投票框架/裁决铁律/输出格式"
  等偏置信息，直接复用会污染对照实验，必须新造一份剥净的数据切片。

本测覆盖：
  1. **剥离验证**：切片中不得出现 §9k/§10/§11 整段 + 头尾规则指令
  2. **保留验证**：核心数据板块（§1/§2/§3/§9d/§9e/§9f/§9g/§9i/§9j/§13）必须完整
  3. **中性任务说明**：末尾附加的"请独立给出"文本存在且不含"8 维/铁律"等规则词
  4. **空 snapshot 兜底**：即使 snapshot 极简也不应抛异常

所有断言使用 substring 精确匹配，避免因 prompts.py 措辞小改动误报。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ai.prompts import build_data_snapshot_prompt, build_user_prompt


# ── 构造一份"尽可能覆盖所有板块"的最小 snapshot ──
def _full_snapshot() -> dict:
    return {
        "coin": "BTC",
        "price": 86000.0,
        "high_24h": 87500,
        "low_24h": 85200,
        # §1 清算
        "liq_imbalance_ratio": 1.35,
        "liq_clusters_above": [
            {"price_from": 87000, "price_to": 87200, "total_usd": 1.2e8,
             "dominant_leverage": "25", "distance_pct": 1.2},
        ],
        "liq_clusters_below": [
            {"price_from": 85500, "price_to": 85700, "total_usd": 8.0e7,
             "dominant_leverage": "25", "distance_pct": 0.6},
        ],
        "vacuum_zones": [],
        "liq_sweep_above_usd_1h": 0,
        "liq_sweep_below_usd_1h": 0,
        # §2 CVD
        "cvd_contract_trend": "rising",
        "cvd_contract_delta_1h": 3.2e6,
        "cvd_spot_trend": "neutral",
        "cvd_spot_delta_1h": -1.1e6,
        "cvd_divergence": "无",
        # §3 持仓
        "oi_current_usd": 65e9,
        "oi_change_24h_pct": 2.3,
        "oi_change_1h_pct": 0.1,
        "oi_change_5m_pct": 0.02,
        "oi_trend": "平稳上升",
        # §4 资金费率
        "funding_exchanges": [
            {"exchange": "Binance", "current": 0.0001, "avg_7d": 0.00008},
        ],
        "funding_rate_okx": 0.0001,
        "funding_rate_binance": 0.0001,
        "funding_interpretation": "多头温和付费",
        # §5 多空比
        "ls_ratio": 2.1,
        "ls_ratio_long_pct": 67.7,
        "ls_ratio_short_pct": 32.3,
        "ls_ratio_change_24h": 0.05,
        # §6 订单簿
        "orderbook_bid_total_usd": 3.4e8,
        "orderbook_ask_total_usd": 3.2e8,
        "orderbook_spread_pct": 0.01,
        # §8 成交分布
        "volume_profile_poc": 86100,
        "value_area_low": 85500,
        "value_area_high": 86800,
        "vwap": 86050,
        "atr_14": 520,
        # §9d 加密情绪（触发 §9d / §9e 块，便于覆盖）
        "fear_greed_index": 55,
        "btc_dominance": 58.2,
        # §9e 周期位置
        "cycle_position": {
            "cps": 62.5, "cps_label": "中期", "mvrv_z": 1.2,
            "ahr999_value": 1.02, "ahr999_band": "持有",
        },
        # §9f 箱体
        "range_signal": {
            "has_box": True, "box_upper": 87500, "box_lower": 84500,
            "box_state": "mature", "box_quality": 7, "box_age_hours": 48,
            "breakout_probability": 0.35, "breakout_direction_bias": "up",
            "breakout_reason": "量缩 + BB squeeze",
        },
        # §9g 关键位
        "key_levels": {
            "active_count": 8,
            "structure_summary": "箱体上沿附近，上方阻力密集",
            "levels": [
                {"price": 87000, "strength_tier": "S", "side": "resistance",
                 "state": "approaching", "confluence_score": 82, "source_count": 4,
                 "distance_pct": 1.2, "test_count": 2, "sweep_usd": 0,
                 "cascade_risk": 0.0, "sources": ["liq_cluster", "poc", "ma60_daily"],
                 # M2.5 · 行为观测层（V3 双轨）→ 触发 §9g.1 渲染
                 "behavior": {
                     "behavior_state": "flip_pending",
                     "state_confidence": 0.55,
                     "breakout_validity": 0.62,
                     "retest_quality": 0.0,
                     "selloff_continuation_risk": 0.0,
                     "capitulation_bottom_score": 0.0,
                     "flip_confirmation": 0.45,
                     "false_break_risk": 0.20,
                     "explain_chips": ["突破质量良好", "等待回踩验证"],
                     "components_used": ["breakout_validity", "flip_confirmation"],
                     "evaluated_at": 1713763800,
                     "bounce_quality_enhanced": 0.0,
                     "breakout_stage_enhanced": 1,
                     "fake_break_strength": 0.0,
                     "dynamic_break_depth_pct": 0.45,
                     "contradiction_with_state": [],
                 }},
            ],
            "signals": [],
        },
        # §9h 净持仓
        "net_position_trend": "上升(多头增仓)",
        "net_position_latest": 3200.0,
        "net_position_change_24h": 240.0,
        # §9i 市场结构
        "market_structure": {
            "direction": "bullish", "confidence": 0.65,
            "bias": "long_only", "recent_events": ["BOS_up"],
        },
        # §9j 动能衰竭
        "trend_exhaustion": {
            "overall_state": "healthy_continuation",
            "overall_direction": "up",
            "consensus_level": "partial",
            "overall_action": "continuation",
            "data_quality": "ok",
            "overall_plain_cn": "温和上涨延续中，无衰竭信号",
        },
        # §9k direction_vote（要被剥掉）
        "direction_vote": {
            "consensus_score": 0.56,
            "dominant_direction": "bullish",
            "consensus_level": "strong_consensus",
            "summary_cn": "规则 8 维倾向做多",
            "votes": [
                {"key": "structure", "name_cn": "结构",
                 "direction": "bullish", "strength": 0.7,
                 "weight": 0.18, "note": "HH+HL"},
            ],
            "top_bullish": ["structure"],
            "top_bearish": [],
        },
        # §10 规则引擎（要被剥掉）
        "market_temperature": 62,
        "pin_risk_level": "MEDIUM",
        "rule_supports": [{"price": 85500, "sources": ["vp", "poc"]}],
        "rule_resistances": [{"price": 87000, "sources": ["liq"]}],
        # §11 sniper/ladder（要被剥掉）
        "sniper_entries": [
            {"direction": "long", "entry_price": 85700, "stop_loss": 85000,
             "take_profit_1": 86500, "take_profit_2": 87800,
             "rr_ratio_1": 1.1, "rr_ratio_2": 3.0, "logic": ["POC 共振"]},
        ],
        "ladder_plans": [],
        # §13 新闻
        "news_brief_text": '{"summary": "BTC 维持高位震荡"}',
        "news_brief_version": 12,
        "news_brief_trigger": "scheduled",
        "news_brief_updated_at": 1713763800,
        "active_narratives": [
            {"theme_id": "etf_flow", "theme_name_cn": "ETF 流",
             "current_direction_bias": "bullish", "current_intensity": 3,
             "flip_flop_count_24h": 0},
        ],
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 1. 剥离验证（drop）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestStripInstructional:
    """切片中不应含我方规则侧结论、指令、输出格式要求。"""

    def test_drop_section_9k(self):
        out = build_data_snapshot_prompt(_full_snapshot())
        assert "### 9k" not in out
        assert "规则 8 维共识" not in out
        assert "top_bullish=" not in out
        assert "使用原则:" not in out

    def test_drop_section_10(self):
        out = build_data_snapshot_prompt(_full_snapshot())
        assert "### 10" not in out
        assert "规则引擎预计算" not in out

    def test_drop_section_11(self):
        out = build_data_snapshot_prompt(_full_snapshot())
        # §11 主标题及其全部子段都要剥
        assert "### 11" not in out
        assert "引擎交易方案" not in out
        assert "11a. 短线档方案" not in out
        assert "11b. 中远线档方案" not in out

    def test_drop_engine_constraint_header(self):
        out = build_data_snapshot_prompt(_full_snapshot())
        assert "【引擎约束】" not in out

    def test_drop_instruction_footer(self):
        out = build_data_snapshot_prompt(_full_snapshot())
        assert "请基于以上数据输出" not in out
        assert "必须包含八个章节" not in out
        # 末尾「重点：1) ... 2) ...」整行剥除
        assert not any(
            ln.startswith("重点：") for ln in out.split("\n")
        ), "末尾 `重点：` 指令行应被剥除"

    def test_drop_news_usage_rule(self):
        """§13 新闻情报段末尾『- 【使用规则】』指令行必须剥除。"""
        out = build_data_snapshot_prompt(_full_snapshot())
        assert "【使用规则】" not in out, (
            "新闻段的「使用规则」是输出指令，不是数据，需要剥离"
        )

    def test_no_output_format_keywords(self):
        """切片不应残留我方 8 维/AI 终审员 / JSON 合约等关键词（预设偏置）。"""
        out = build_data_snapshot_prompt(_full_snapshot())
        banned = [
            "AITRADER_MATRIX_JSON",
            "8 维投票",
            "AI 终审员",
            "推翻规则",
            "输出格式契约",
            "必须输出",
            "section_bias",
            "sections[]",
        ]
        for word in banned:
            assert word not in out, f"污染词 `{word}` 不应出现在数据切片"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 2. 保留验证（keep）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestKeepDataSections:
    """核心数据板块必须完整保留，供跨模型分析使用。"""

    def test_keep_price_and_24h_range(self):
        out = build_data_snapshot_prompt(_full_snapshot())
        assert "当前价格" in out
        assert "$86,000" in out
        assert "24h最高" in out
        assert "24h最低" in out

    def test_keep_section_1_liquidation(self):
        out = build_data_snapshot_prompt(_full_snapshot())
        assert "### 1. 清算地图数据" in out
        assert "多空失衡比" in out
        assert "上方清算密集区" in out

    def test_keep_section_2_cvd(self):
        out = build_data_snapshot_prompt(_full_snapshot())
        assert "### 2. 资金流数据" in out
        assert "合约CVD趋势" in out

    def test_keep_section_3_oi(self):
        out = build_data_snapshot_prompt(_full_snapshot())
        assert "### 3. 持仓与杠杆" in out
        assert "OI总量" in out

    def test_keep_section_5_ls_ratio(self):
        out = build_data_snapshot_prompt(_full_snapshot())
        assert "### 5. 多空比" in out
        assert "全局账户多空比" in out

    def test_keep_section_6_orderbook(self):
        out = build_data_snapshot_prompt(_full_snapshot())
        assert "### 6. 订单簿聚合深度" in out
        assert "买卖力差" in out

    def test_keep_section_9g_key_levels(self):
        """关键位板块（state machine 产出的纯数据表）必须保留。"""
        out = build_data_snapshot_prompt(_full_snapshot())
        assert "### 9g. 关键位状态机" in out
        assert "共振分" in out  # 表头

    def test_keep_section_9i_market_structure(self):
        out = build_data_snapshot_prompt(_full_snapshot())
        assert "### 9i" in out and "市场结构" in out

    def test_keep_section_9j_trend_exhaustion(self):
        out = build_data_snapshot_prompt(_full_snapshot())
        assert "### 9j" in out and "动能衰竭" in out

    def test_keep_section_13_news(self):
        out = build_data_snapshot_prompt(_full_snapshot())
        assert "### 13. 新闻情报" in out
        assert "Rolling Brief" in out

    def test_keep_field_semantic_notes(self):
        """『字段语义/数据说明』等客观解释属于『让其他 AI 读得懂』的必需注解。"""
        out = build_data_snapshot_prompt(_full_snapshot())
        # §9g 的字段语义说明（反弹质量 / 突破阶段 预期空值解释）
        assert "字段语义" in out or "数据说明" in out

    def test_keep_anomaly_warnings(self):
        """⚠ 数据异常告警（如极端 spread）属于数据质量标注，不是方向判断规则。"""
        snap = _full_snapshot()
        snap["orderbook_spread_pct"] = 41.76  # P0.8 HIGH-1 场景
        out = build_data_snapshot_prompt(snap)
        assert "极端异常值" in out


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 2.5 §9g.1 行为观测层（V3 · M2.5 双轨）渲染验证
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestKeyLevelBehaviorSection:
    """§9g.1 是 V3 关键位行为观测层在数据切片中的呈现。

    设计纪律：
      - 同份切片同时含 §9g（V2 状态机）+ §9g.1（V2.5 双轨观测）
      - §9g.1 仅在存在显著 behavior 时渲染（pending+无冲突→ 不渲染）
      - 冲突预警必须以 ⚠ 提示，但**不**让其他 AI 推翻 9g
    """

    def test_renders_when_behavior_state_active(self):
        """含 flip_pending 的 level → 9g.1 应出现 + 含 behavior_state 中文。"""
        out = build_data_snapshot_prompt(_full_snapshot())
        assert "### 9g.1" in out
        assert "翻转待确认" in out  # flip_pending 中文
        assert "conf=" in out  # 置信度展示

    def test_renders_significant_scores(self):
        """≥ 0.4 的分数应展示；< 0.4 的不展示（避免噪声）。"""
        out = build_data_snapshot_prompt(_full_snapshot())
        assert "突破:0.62" in out  # >= 0.4
        assert "翻转:0.45" in out  # >= 0.4
        # false_break_risk=0.20 < 0.4 → 不应展示
        seg_91 = out.split("### 9g.1", 1)[1].split("### 9g2", 1)[0] \
            if "### 9g2" in out else out.split("### 9g.1", 1)[1]
        assert "假破:0.20" not in seg_91

    def test_section_91_disclaimer_does_not_override_9g(self):
        """§9g.1 必须明确为辅助层，不得让 AI 单独反驳 9g。"""
        out = build_data_snapshot_prompt(_full_snapshot())
        seg_91 = out.split("### 9g.1", 1)[1]
        assert "第二意见" in seg_91 or "辅助" in seg_91

    def test_skip_when_pending_and_no_contradiction(self):
        """pending state + 无冲突 → 该 level 不出现在 §9g.1（无信号）。"""
        snap = _full_snapshot()
        snap["key_levels"]["levels"][0]["behavior"]["behavior_state"] = "pending"
        snap["key_levels"]["levels"][0]["behavior"]["contradiction_with_state"] = []
        out = build_data_snapshot_prompt(snap)
        # 仅一个 level，pending 且无冲突 → §9g.1 整段不渲染
        assert "### 9g.1" not in out

    def test_renders_when_pending_but_has_contradiction(self):
        """pending state + 有冲突 → 仍要展示该 level（让 AI 看到风险）。"""
        snap = _full_snapshot()
        snap["key_levels"]["levels"][0]["behavior"]["behavior_state"] = "pending"
        snap["key_levels"]["levels"][0]["behavior"]["contradiction_with_state"] = [
            "支撑接触但破位延续风险高"
        ]
        out = build_data_snapshot_prompt(snap)
        assert "### 9g.1" in out
        assert "⚠" in out
        assert "破位延续风险" in out

    def test_dual_track_v1_v2_comparison_renders(self):
        """V1 bounce_quality + V2 enhanced 都存在 → 双轨对照应展示。"""
        snap = _full_snapshot()
        lv = snap["key_levels"]["levels"][0]
        lv["bounce_quality"] = "proactive"
        lv["behavior"]["bounce_quality_enhanced"] = 0.42
        lv["behavior"]["behavior_state"] = "weak_bounce"
        out = build_data_snapshot_prompt(snap)
        assert "V1反弹=主动" in out
        assert "V2=0.42" in out


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 3. 末尾中性任务说明
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestNeutralTaskFooter:
    def test_has_neutral_task_description(self):
        out = build_data_snapshot_prompt(_full_snapshot())
        assert "分析任务" in out
        assert "请基于以上数据独立给出" in out

    def test_task_not_prescriptive(self):
        """任务说明中不得含『8 维/铁律/必须输出格式』等规则词。"""
        out = build_data_snapshot_prompt(_full_snapshot())
        tail = out.split("## 分析任务", 1)[-1]
        banned_in_tail = [
            "必须包含八",
            "8 维",
            "AITRADER_MATRIX",
            "铁律",
            "禁止",
        ]
        for word in banned_in_tail:
            assert word not in tail, (
                f"任务说明应保持中性，不得含规则词 `{word}`"
            )

    def test_has_stripping_disclaimer(self):
        """告知其他 AI『本切片已剥除我方规则侧结论』，避免误以为系统断供。"""
        out = build_data_snapshot_prompt(_full_snapshot())
        assert "已剥除" in out
        assert "规则" in out  # 描述剥除了哪些内容


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 4. 鲁棒性
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestRobustness:
    def test_minimal_snapshot_no_crash(self):
        """极简 snapshot 也不应抛异常。"""
        out = build_data_snapshot_prompt({"coin": "BTC", "price": 0})
        assert "BTC" in out
        assert "## 分析任务" in out

    def test_shorter_than_user_prompt(self):
        """切片必须比 user_prompt 短（因为剥除了 §9k/§10/§11 等）。"""
        snap = _full_snapshot()
        data_only = build_data_snapshot_prompt(snap)
        full = build_user_prompt(snap)
        assert len(data_only) < len(full), (
            f"切片 {len(data_only)} chars 应短于原 user_prompt {len(full)} chars"
        )

    def test_idempotent(self):
        """同一 snapshot 重复生成结果一致。"""
        snap = _full_snapshot()
        a = build_data_snapshot_prompt(snap)
        b = build_data_snapshot_prompt(snap)
        assert a == b
