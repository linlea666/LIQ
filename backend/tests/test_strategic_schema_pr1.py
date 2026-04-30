"""PR-1 · Strategic AI Schema + 数据层 回归测试

覆盖三个核心保证：
  1. **新 schema 正确性** —— common_prompt_debug / strategic_report / LiquidationMapBlock
     默认值合法、字段完整、序列化往返一致。
  2. **MAA 持久化向后兼容** —— `market_action.PromptDebug` re-export 后，
     旧 `market_action_history.json` 反序列化路径无破坏。
  3. **AISnapshot 增量装配** —— 新增字段默认 None / 空 list；
     当传入 trading_brain / market_action_facts 时正确透传强类型。
"""

from __future__ import annotations

import time

import pytest
from pydantic import ValidationError


# ────────────────────────────────────────────────────────────────────────────
# 1. PromptDebug 通用模型 + market_action re-export 兼容性
# ────────────────────────────────────────────────────────────────────────────

class TestPromptDebugReexport:
    def test_common_prompt_debug_basic(self):
        from models.common_prompt_debug import PromptDebug, PromptSection

        section = PromptSection(anchor="§1", title="数据透明度", level=2)
        debug = PromptDebug(
            system="你是交易员",
            user="当前价 65000",
            chars=10,
            sections=[section],
            model="deepseek-v4-flash",
            tokens_prompt=120,
            tokens_completion=80,
            latency_ms=1200,
            generated_at=int(time.time()),
            ai_raw_response='{"decision":"WAIT"}',
            parse_ok=True,
        )
        assert debug.system == "你是交易员"
        assert debug.sections[0].anchor == "§1"
        assert debug.sections[0].level == 2
        assert debug.tokens_reasoning is None  # 默认 None（v4-flash 非思考模式）

    def test_market_action_prompt_debug_is_same_class(self):
        """`from models.market_action import PromptDebug` 必须解析到通用版同一对象。"""
        from models.common_prompt_debug import PromptDebug as CommonPromptDebug
        from models.market_action import PromptDebug as MAPromptDebug

        assert MAPromptDebug is CommonPromptDebug, (
            "MAA 应该 re-export 通用版，否则旧 history 反序列化路径会断裂"
        )

    def test_market_action_history_legacy_payload_loads(self):
        """模拟旧 `market_action_history.json` 中的 PromptDebug 字典——
        新代码必须能原样反序列化（pydantic 按字段名匹配）。"""
        from models.market_action import MarketActionReport, PromptDebug

        legacy_payload = {
            "system": "old system",
            "user": "old user",
            "chars": 100,
            "sections": [{"anchor": "§1", "title": "旧标题", "level": 2}],
            "model": "deepseek-r1-0528",
            "tokens_prompt": 200,
            "tokens_completion": 150,
            "tokens_reasoning": 800,
            "latency_ms": 5000,
            "generated_at": 1700000000,
            "ai_raw_response": "...",
            "ai_reasoning_content": "<think>历史 CoT 原文</think>",
            "parse_ok": True,
            "parse_error": None,
        }
        debug = PromptDebug.model_validate(legacy_payload)
        assert debug.tokens_reasoning == 800
        assert debug.ai_reasoning_content.startswith("<think>")

        # 模拟旧 history 文件结构：MarketActionReport 必填字段 + prompt_debug 子 dict
        report_payload = {
            "coin": "BTC",
            "timestamp": 1700000000,
            "market_conclusion": "测试结论",
            "scenario": "range_bound",
            "market_phase": "transition",
            "confidence": 50,
            "prompt_debug": legacy_payload,
        }
        report = MarketActionReport.model_validate(report_payload)
        assert report.prompt_debug is not None
        assert report.prompt_debug.model == "deepseek-r1-0528"
        assert report.prompt_debug.ai_reasoning_content.startswith("<think>")


# ────────────────────────────────────────────────────────────────────────────
# 2. AIStrategicReport schema 完整性
# ────────────────────────────────────────────────────────────────────────────

class TestAIStrategicReport:
    def test_default_minimal_instance(self):
        """最少字段构造（仅 coin/timestamp）必须合法，便于解析失败兜底。"""
        from models.strategic_report import AIStrategicReport

        r = AIStrategicReport(coin="BTC", timestamp=1700000000)
        assert r.decision == "WAIT"
        assert r.horizon == "intraday"
        assert r.bias == "neutral"
        assert r.confidence == 0.0
        assert r.evidence_matrix.long_evidence == []
        assert r.data_self_check.hard_stop_triggered is False
        assert r.primary_plan is None

    def test_confidence_range_validation(self):
        """confidence 必须在 [0, 1]——保证 prompt 约束被 schema 拦住。"""
        from models.strategic_report import AIStrategicReport

        with pytest.raises(ValidationError):
            AIStrategicReport(coin="BTC", timestamp=1, confidence=1.5)
        with pytest.raises(ValidationError):
            AIStrategicReport(coin="BTC", timestamp=1, confidence=-0.1)

    def test_full_plan_roundtrip(self):
        """主计划 + 备选场景 + 冲突矩阵 完整序列化往返。"""
        from models.strategic_report import (
            AIStrategicReport,
            AlternativeScenario,
            CurrentZoneAssessment,
            DataSelfCheck,
            Evidence,
            EvidenceMatrix,
            Target,
            TradingPlan,
        )

        plan = TradingPlan(
            setup_type="扫单反转",
            entry_zone_low=64500,
            entry_zone_high=64700,
            trigger_conditions=["现货墙未撤", "CVD 5m 转正"],
            soft_invalidation="60min 内未回到入场区上沿",
            hard_invalidation="64200 收盘失守",
            targets=[
                Target(price=66000, reason="上方 7d 清算簇 + Coinbase 强阻力", rr=2.5),
                Target(price=67500, reason="日线压力位汇聚", rr=4.0),
            ],
            cancel_conditions=["现货墙被吃 50% 以上"],
            risk_unit="按 1R 风险计算",
            leverage_risk_level="medium",
            position_sizing_note="若 hard_invalidation 距离 1.2% 且账户允许 1R/笔，仓位 ≈ R / 1.2%",
        )
        evidence = EvidenceMatrix(
            long_evidence=[
                Evidence(section_ref="§7", observation="下方 64600 有双源墙",
                         inference="买盘承接强", supports="main", weight="high"),
            ],
            short_evidence=[
                Evidence(section_ref="§8", observation="下方 0.8% 处巨型清算磁铁",
                         inference="可能引发扫单", supports="contrarian", weight="medium"),
            ],
            wait_evidence=[],
            contradictions=["现货墙强支撑 vs 下方清算磁铁近"],
        )
        report = AIStrategicReport(
            coin="BTC",
            timestamp=1700000000,
            decision="LONG_PLAN",
            horizon="intraday",
            bias="bullish",
            confidence=0.62,
            confidence_rationale="结构强但 OI 拥挤度偏高",
            current_zone_assessment=CurrentZoneAssessment(
                zone_id="zone_64500_spot_defense",
                role="spot_defense",
                nearest_critical_above_pct=0.45,
                nearest_critical_below_pct=0.30,
                key_conflict="现货墙强支撑 vs 下方 0.8% 处巨型清算磁铁",
            ),
            structure_analysis="§4 当前 zone 为 spot_defense ...",
            flow_analysis="§9 OI/CVD 共振多 ...",
            macro_context="§11 宏观偏中性 ...",
            primary_plan=plan,
            alternative_scenario=AlternativeScenario(
                description="若现货墙被吃 → 转为下行延续",
                probability_pct=30,
                trigger="现货墙双源被吃 50% 以上",
            ),
            evidence_matrix=evidence,
            invalidation_conditions=["64200 收盘失守", "现货墙撤单"],
            data_self_check=DataSelfCheck(
                missing=[],
                stale=[],
                provisional=["price.recent_bars_1h"],
                hard_stop_triggered=False,
                confidence_penalty_reason="latest 1h 未收盘 → 降权 5%",
            ),
            macro_modifier_note="宏观偏中性 → 无需修正",
            data_quality="ok",
        )

        dumped = report.model_dump()
        from models.strategic_report import AIStrategicReport as ReportCls
        restored = ReportCls.model_validate(dumped)
        assert restored.decision == "LONG_PLAN"
        assert restored.primary_plan.targets[0].rr == 2.5
        assert restored.evidence_matrix.contradictions == [
            "现货墙强支撑 vs 下方清算磁铁近"
        ]


# ────────────────────────────────────────────────────────────────────────────
# 3. AISnapshot 新字段 + LiquidationMapBlock + build_ai_snapshot 增量装配
# ────────────────────────────────────────────────────────────────────────────

class TestAISnapshotPR1Fields:
    def test_aisnapshot_default_new_fields(self):
        """AISnapshot 不传新字段时，所有 PR-1 字段必须有合法默认值。

        注：PR-5 起 AISnapshot 为 Strategic 专用瘦身模型；`build_ai_snapshot` 仅
        暴露 keyword-only 扩展参数，最小调用仅需价格带来字段 + atr/temp/pin。
        """
        from ai.snapshot import build_ai_snapshot

        s = build_ai_snapshot(
            coin="BTC", price=65000, high_24h=66000, low_24h=64000,
            atr=500, market_temp_score=50, pin_risk_level="low",
        )
        # 强类型对象默认 None
        assert s.trading_brain is None
        assert s.key_level_snapshot is None
        assert s.liq_map_block_1d is None
        assert s.liq_map_block_7d is None
        assert s.liq_map_block_30d is None
        assert s.crowding_global is None
        assert s.usd_usdt_basis_pct is None
        # facts 13 个全部 None
        assert s.facts_oi is None
        assert s.facts_funding is None
        assert s.facts_cvd_contract is None
        assert s.facts_options is None
        # list 字段默认空
        assert s.wall_zones_above == []
        assert s.wall_zones_below == []
        assert s.wall_events_v2 == []
        assert s.facts_missing == []
        assert s.facts_provisional_fields == []
        assert s.facts_sources_used == []
        # 标量
        assert s.facts_data_quality == ""
        assert s.facts_has_provisional_bars is False

    def test_liquidation_map_block_basic(self):
        """LiquidationMapBlock 默认实例 + 强类型 LiqCluster 正确收纳。"""
        from models.liquidation import LiqCluster
        from models.snapshot import LiquidationMapBlock

        block = LiquidationMapBlock(cycle="1d")
        assert block.cycle == "1d"
        assert block.clusters_above == []
        assert block.imbalance_ratio == 0.0
        assert block.max_pain is None

        cluster = LiqCluster(
            price_center=66000,
            price_from=65900,
            price_to=66100,
            total_usd=1_500_000,
            side="short",
            distance_pct=1.5,
            exchange_count=3,
        )
        block_with_data = LiquidationMapBlock(
            cycle="7d",
            clusters_above=[cluster],
            imbalance_ratio=1.3,
            by_exchange_summary=[
                {"exchange": "Binance", "total_usd": 800_000, "share_pct": 53.3},
            ],
        )
        assert block_with_data.clusters_above[0].exchange_count == 3
        assert block_with_data.by_exchange_summary[0]["share_pct"] == 53.3

    def test_build_liq_map_block_helper(self):
        """`_build_liq_map_block` 输入 LiquidationMap → 输出强类型 Block。

        关键覆盖：
          - by_exchange 嵌套结构（{exName: {price_str: usd}}）正确聚合
          - max_pain 仅 1d 注入，长窗口为 None
          - liq_map=None 时返回 None
        """
        from ai.snapshot import _build_liq_map_block
        from models.liquidation import LiqCluster, LiqLeverageGroup, LiquidationMap, LiqMaxPainItem

        liq_map = LiquidationMap(
            coin="BTC",
            ts=1700000000,
            cycle="1d",
            leverage_groups=[
                LiqLeverageGroup(leverage="10"),
            ],
            clusters_above=[
                LiqCluster(
                    price_center=66000, price_from=65900, price_to=66100,
                    total_usd=2_000_000, side="short", distance_pct=1.5,
                    exchange_count=2,
                ),
            ],
            clusters_below=[
                LiqCluster(
                    price_center=64000, price_from=63900, price_to=64100,
                    total_usd=1_500_000, side="long", distance_pct=-1.5,
                    exchange_count=1,
                ),
            ],
            imbalance_ratio=1.33,
            by_exchange={
                "Binance": {"66000": 1_200_000, "64000": 800_000},
                "OKX": {"66000": 800_000, "64000": 700_000},
                "Bybit": {"66000": 100_000},
            },
        )
        max_pain = LiqMaxPainItem(
            symbol="BTC",
            long_pain_price=63500,
            long_pain_usd=900_000,
            short_pain_price=66500,
            short_pain_usd=1_100_000,
        )

        block_1d = _build_liq_map_block(liq_map, "1d", max_pain=max_pain)
        assert block_1d is not None
        assert block_1d.cycle == "1d"
        assert len(block_1d.clusters_above) == 1
        assert block_1d.imbalance_ratio == pytest.approx(1.33)
        assert block_1d.max_pain is max_pain

        ex_names = [item["exchange"] for item in block_1d.by_exchange_summary]
        assert ex_names[0] == "Binance"
        assert ex_names[:3] == ["Binance", "OKX", "Bybit"]
        # share_pct 总和应近似 100%
        share_sum = sum(item["share_pct"] for item in block_1d.by_exchange_summary)
        assert share_sum == pytest.approx(100.0, abs=0.5)

        block_7d = _build_liq_map_block(liq_map, "7d")  # 长窗口不传 max_pain
        assert block_7d.max_pain is None

        assert _build_liq_map_block(None, "30d") is None

    def test_build_ai_snapshot_passes_through_facts_and_brain(self):
        """`build_ai_snapshot` 接收 trading_brain + facts 时强类型透传。"""
        from ai.snapshot import build_ai_snapshot
        from models.market_action import (
            FactsDataMeta,
            FundingSnapshot,
            MarketActionFacts,
            OISnapshot,
        )
        from models.trading_brain import TradingBrainSnapshot

        brain = TradingBrainSnapshot(coin="BTC", ts=1700000000, last_price=65000.0)
        facts = MarketActionFacts(
            coin="BTC",
            timestamp=1700000000,
            oi=OISnapshot(current_usd=20_000_000_000),
            funding=FundingSnapshot(avg_current=0.005),
            data_quality="ok",
            missing=["basis"],
            data_meta=FactsDataMeta(
                generated_at=1700000000,
                has_provisional_bars=True,
                sources_used=["coinglass", "binance"],
                provisional_fields=["price.recent_bars_1h"],
            ),
        )

        snap = build_ai_snapshot(
            coin="BTC",
            price=65000,
            high_24h=66000,
            low_24h=64000,
            atr=500,
            market_temp_score=50,
            pin_risk_level="low",
            trading_brain=brain,
            market_action_facts=facts,
        )

        assert snap.trading_brain is brain
        assert snap.facts_oi is facts.oi
        assert snap.facts_funding is facts.funding
        assert snap.facts_data_quality == "ok"
        assert snap.facts_missing == ["basis"]
        assert snap.facts_has_provisional_bars is True
        assert snap.facts_provisional_fields == ["price.recent_bars_1h"]
        assert snap.facts_sources_used == ["coinglass", "binance"]
