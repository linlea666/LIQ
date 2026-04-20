"""D14 AI Trader Report Builder 单测

覆盖：
  - FactorMatrix 7 板块是否全部生成
  - 交易计划映射（trading_plan_entries → AITradingPlan）
  - key_level_interpretation 从 AIAnalysisResult.key_levels 抽取
  - 双引擎对齐判定（agree / caution / disagree）
  - 观望占位（无 entries + 无 sniper_plans）
  - 数据降级：math_plan=None / 无 snapshot 字段
"""

from __future__ import annotations

import time

import pytest

from ai.trader_report_builder import (
    _judge_agreement, build_ai_trader_report, build_factor_matrix,
)


# P1.8a · 隔离 AI Quality Ledger 的测试副作用
@pytest.fixture(autouse=True)
def _isolate_ai_quality_ledger(tmp_path, monkeypatch):
    from processors import ai_quality_ledger
    file = str(tmp_path / "ai_quality.json")
    ledger = ai_quality_ledger.AIQualityLedger(data_file=file)
    monkeypatch.setattr(ai_quality_ledger, "_instance", ledger)
    yield
from models.ai_trader_report import AITraderReport
from models.execution_plan import (
    ExecutionPlan, ExecutionScoreBreakdown, SafetyGateResult,
)
from models.snapshot import (
    AIAnalysisResult, AISnapshot, SignalSummary, TradingPlanEntry,
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _mk_snapshot(**overrides) -> AISnapshot:
    base = dict(
        coin="BTC",
        price=72500.0,
        ts=int(time.time()),
        high_24h=73000.0,
        low_24h=71500.0,
        rsi_14=62.0,
        macd_data={"histogram": 0.5},
        ma60_daily=70000.0,
        ma120_daily=68000.0,
        candlestick_pattern_name="锤子线",
        candlestick_pattern_side="support",
        stablecoin_total_mcap=150_000_000_000.0,
        stablecoin_7d_change_pct=0.35,
        whale_net_direction="in",
        whale_transfer_net_usd=120_000_000.0,
        etf_trend_5d="连续 5 日净流入",
        oi_change_1h_pct=0.4,
        oi_trend="温和上升",
        funding_rate_binance=0.005,
        funding_rate_okx=0.006,
        ls_ratio_top_position=1.15,
        recent_liq_24h_long_usd=40_000_000.0,
        recent_liq_24h_short_usd=80_000_000.0,
        active_narratives=[
            {
                "theme_id": "ETF_inflow",
                "theme_name_cn": "ETF 资金流入",
                "current_direction_bias": "bullish",
                "current_intensity": 4,
                "flip_flop_count_24h": 0,
            },
            {
                "theme_id": "Middle_East_Iran",
                "theme_name_cn": "中东局势",
                "current_direction_bias": "bearish",
                "current_intensity": 3,
                "flip_flop_count_24h": 3,
            },
        ],
        geo_overview={
            "overall_level": 2,
            "overall_label": "LOW",
            "overall_emoji": "🟡",
            "escalation_count_24h": 1,
            "has_blackswan_24h": False,
            "suggest_position_cap_pct": 60,
        },
        news_brief_text="{\"v\":1}",
        news_brief_version=1,
        news_brief_trigger="scheduled",
    )
    base.update(overrides)
    # AISnapshot 有很多必填字段；本 builder 仅读取一小撮，故用 model_construct 绕过校验。
    return AISnapshot.model_construct(**base)


def _mk_analysis(
    *,
    direction: str = "bullish",
    confidence: str = "medium",
    entries: list[TradingPlanEntry] | None = None,
    key_levels: list[dict] | None = None,
) -> AIAnalysisResult:
    if entries is None:
        entries = [
            TradingPlanEntry(
                tier="short", direction="long", entry=72300.0,
                stop_loss=71400.0, tp1=73800.0, tp2=75000.0, rr=2.3,
                logic="关键支撑反弹 + RSI 回升",
            ),
        ]
    if key_levels is None:
        key_levels = [
            {"price": 71800, "type": "support", "reason": "日线 demand zone"},
            {"price": 73500, "type": "resistance", "reason": "近期高点"},
        ]
    return AIAnalysisResult(
        coin="BTC",
        ts=int(time.time()),
        price_at_analysis=72500.0,
        signal_summary=SignalSummary(
            direction=direction, confidence=confidence, reason="test", raw_line="",
        ),
        market_overview="短期偏多，关键位附近试空无效。",
        key_levels=key_levels,
        stop_loss_suggestion={"price": 71400, "reason": "结构低点下方"},
        entry_zones=[{"low": 72100, "high": 72400, "reason": "回踩区间"}],
        trading_plan_entries=entries,
        risk_warnings=["注意诱多 sweep"],
        scenario_analysis=[],
        raw_text="raw",
        user_prompt="",
    )


def _mk_math_plan(
    *,
    direction: str = "bullish",
    action: str = "long",
    score: float = 70.0,
    position_pct: float = 35.0,
    safety_triggered: bool = False,
) -> ExecutionPlan:
    return ExecutionPlan(
        coin="BTC",
        ts=int(time.time()),
        current_price=72500.0,
        regime="trend_up",
        regime_confidence=0.7,
        execution_score=score,
        traffic_light="green",
        headline=f"{action} @72200",
        action=action,  # type: ignore[arg-type]
        direction=direction,  # type: ignore[arg-type]
        tier_hint="A",
        entry_zone_low=72100.0,
        entry_zone_high=72400.0,
        stop_loss=71500.0,
        tp1=73800.0,
        tp2=75200.0,
        rr_ratio=2.3,
        position_size_pct=position_pct,
        breakdown=ExecutionScoreBreakdown(final_score=score),
        safety_gates=SafetyGateResult(triggered=safety_triggered),
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Tests
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestFactorMatrix:
    def test_seven_sections_all_present(self):
        snap = _mk_snapshot()
        matrix = build_factor_matrix(snap, ai_bias="bullish", ai_conviction=70, math_plan=_mk_math_plan())
        assert len(matrix.sections) == 7
        ids = [s.section_id for s in matrix.sections]
        assert ids == ["A", "B", "C", "D", "E", "F", "G"]
        for s in matrix.sections:
            assert s.rows, f"section {s.section_id} must have at least one row"

    def test_overall_bias_aggregation_bullish(self):
        snap = _mk_snapshot()
        matrix = build_factor_matrix(snap, ai_bias="bullish", ai_conviction=80, math_plan=_mk_math_plan())
        # 构造的数据整体偏多（ETF 流入 + 爆仓空方多 + RSI 62 中性 + MA 趋势 + funding 不极端）
        assert matrix.overall_bias in {"bullish", "neutral"}
        assert matrix.summary_line  # non-empty
        assert matrix.overall_confidence in {"low", "medium", "high"}

    def test_technical_section_detects_oversold_when_rsi_low(self):
        snap = _mk_snapshot(rsi_14=22.0)
        matrix = build_factor_matrix(snap, ai_bias="neutral", ai_conviction=50, math_plan=None)
        d_section = next(s for s in matrix.sections if s.section_id == "D")
        rsi_row = next(r for r in d_section.rows if r.dimension == "RSI(14)")
        assert rsi_row.direction == "bullish"
        assert rsi_row.signal == "超卖"

    def test_derivatives_detects_crowded_long(self):
        # 只保留资金费率 + 多空比两维，避免 OI/爆仓方向中和
        snap = _mk_snapshot(
            funding_rate_binance=0.03, funding_rate_okx=0.04, ls_ratio_top_position=1.5,
            oi_change_1h_pct=0.0, oi_trend="",
            recent_liq_24h_long_usd=0.0, recent_liq_24h_short_usd=0.0,
        )
        matrix = build_factor_matrix(snap, ai_bias="neutral", ai_conviction=50, math_plan=None)
        c_section = next(s for s in matrix.sections if s.section_id == "C")
        # 资金费率 + 多空比都偏多 → 板块偏空（拥挤警示）
        assert c_section.section_bias == "bearish"

    def test_geo_high_level_shows_bearish(self):
        snap = _mk_snapshot(geo_overview={
            "overall_level": 4, "overall_label": "HIGH", "overall_emoji": "🟠",
            "escalation_count_24h": 3, "has_blackswan_24h": False,
        })
        matrix = build_factor_matrix(snap, ai_bias="neutral", ai_conviction=50, math_plan=None)
        f_section = next(s for s in matrix.sections if s.section_id == "F")
        assert f_section.section_bias == "bearish"

    def test_consensus_section_marks_both_engines(self):
        snap = _mk_snapshot()
        math_plan = _mk_math_plan(direction="bullish", action="long", score=72)
        matrix = build_factor_matrix(snap, ai_bias="bullish", ai_conviction=70, math_plan=math_plan)
        g_section = next(s for s in matrix.sections if s.section_id == "G")
        dims = [r.dimension for r in g_section.rows]
        assert "数学引擎" in dims
        assert "AI Trader" in dims

    def test_consensus_section_without_math_plan(self):
        snap = _mk_snapshot()
        matrix = build_factor_matrix(snap, ai_bias="bullish", ai_conviction=60, math_plan=None)
        g_section = next(s for s in matrix.sections if s.section_id == "G")
        # 仅占位一行 "未就绪"
        assert any("未就绪" in r.value_display for r in g_section.rows)


class TestAgreementJudge:
    def test_agree_same_direction_close_score(self):
        math = _mk_math_plan(direction="bullish", action="long", score=75)
        agr, note = _judge_agreement("bullish", 72, math)
        assert agr == "agree"
        assert "同向" in note

    def test_disagree_opposite_direction(self):
        math = _mk_math_plan(direction="bearish", action="short", score=70)
        agr, note = _judge_agreement("bullish", 72, math)
        assert agr == "disagree"
        assert "相反" in note

    def test_caution_one_side_neutral(self):
        math = _mk_math_plan(direction="neutral", action="wait", score=50)
        agr, _note = _judge_agreement("bullish", 70, math)
        assert agr == "caution"

    def test_caution_same_dir_but_score_gap_large(self):
        math = _mk_math_plan(direction="bullish", action="long", score=85)
        agr, _note = _judge_agreement("bullish", 40, math)
        assert agr == "caution"

    def test_caution_when_no_math_plan(self):
        agr, _note = _judge_agreement("bullish", 60, None)
        assert agr == "caution"


class TestBuildFullReport:
    def test_full_report_with_trading_plans(self):
        snap = _mk_snapshot()
        analysis = _mk_analysis(direction="bullish", confidence="high")
        math = _mk_math_plan(direction="bullish", action="long", score=75)
        report = build_ai_trader_report(
            analysis, snap, math_plan=math, model_name="deepseek-reasoner",
            latency_ms=1800, thinking_tokens=500,
        )
        assert isinstance(report, AITraderReport)
        assert report.coin == "BTC"
        assert report.bias == "bullish"
        assert report.conviction >= 70
        assert report.model == "deepseek-reasoner"
        assert report.latency_ms == 1800
        assert report.thinking_tokens == 500
        assert report.factor_matrix is not None
        assert len(report.factor_matrix.sections) == 7
        assert len(report.trading_plans) >= 1
        p0 = report.trading_plans[0]
        assert p0.direction == "long"
        assert p0.priority == 1
        assert p0.entry_zone_low == 72300.0
        assert p0.stop_loss == 71400.0
        assert report.agreement_with_math_engine == "agree"
        assert report.news_impact_summary_cn
        assert report.geo_risk_assessment_cn
        # key level 解读
        assert report.key_level_interpretation.primary_support_price == 71800
        assert report.key_level_interpretation.primary_resistance_price == 73500

    def test_wait_placeholder_when_no_entries(self):
        snap = _mk_snapshot()
        analysis = _mk_analysis(direction="neutral", confidence="low", entries=[])
        report = build_ai_trader_report(analysis, snap, math_plan=None)
        assert len(report.trading_plans) == 1
        assert report.trading_plans[0].direction == "wait"
        assert report.bias == "neutral"

    def test_risk_warnings_enriched_by_flip_flop(self):
        snap = _mk_snapshot()  # Middle_East_Iran flip_flop=3
        analysis = _mk_analysis()
        report = build_ai_trader_report(analysis, snap, math_plan=_mk_math_plan())
        joined = " ".join(report.key_risks)
        assert "反复叙事" in joined
        assert "Middle_East_Iran" in joined

    def test_risk_warnings_enriched_by_high_geo_level(self):
        snap = _mk_snapshot(geo_overview={
            "overall_level": 4, "overall_label": "HIGH", "overall_emoji": "🟠",
            "escalation_count_24h": 2, "has_blackswan_24h": False,
        })
        analysis = _mk_analysis()
        report = build_ai_trader_report(analysis, snap, math_plan=_mk_math_plan())
        assert any("地缘风险等级 4" in r for r in report.key_risks)

    def test_narrative_impact_weights_by_flip_flop(self):
        snap = _mk_snapshot()
        analysis = _mk_analysis()
        report = build_ai_trader_report(analysis, snap, math_plan=_mk_math_plan())
        impacts = {ni.theme_id: ni for ni in report.narrative_impact}
        # 反复的叙事被降档
        assert impacts["Middle_East_Iran"].weight_on_current_plan == "low"
        # 强度 4 的保持 high
        assert impacts["ETF_inflow"].weight_on_current_plan == "high"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# P1.7 · AI JSON 附录 overlay
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

from ai.trader_report_builder import (
    _apply_ai_matrix_overlay, _norm_direction, _norm_section_bias,
    _normalize_dim_key, _resolve_bias, _resolve_conviction,
)


def _make_matrix(snap):
    return build_factor_matrix(snapshot=snap, ai_bias="neutral", ai_conviction=50, math_plan=None)


class TestOverlayHelpers:
    def test_norm_direction_en(self):
        assert _norm_direction("bullish") == "bullish"
        assert _norm_direction("BEARISH") == "bearish"
        assert _norm_direction("neutral") == "neutral"

    def test_norm_direction_zh(self):
        assert _norm_direction("做多") == "bullish"
        assert _norm_direction("偏空") == "bearish"
        assert _norm_direction("中性") == "neutral"

    def test_norm_direction_invalid(self):
        assert _norm_direction("garbage") is None
        assert _norm_direction(None) is None
        assert _norm_direction("") is None

    def test_norm_section_bias_excludes_potential_reversal(self):
        assert _norm_section_bias("potential_reversal") is None  # section 不允许此值
        assert _norm_section_bias("bullish") == "bullish"

    def test_normalize_dim_key_strips_spaces_and_case(self):
        assert _normalize_dim_key("DXY") == "dxy"
        assert _normalize_dim_key("资金费率 ") == "资金费率"
        assert _normalize_dim_key("OI/多空比") == "oi多空比"


class TestResolveBiasConviction:
    def test_ai_json_bias_wins(self):
        analysis = _mk_analysis(direction="neutral", confidence="low")
        ai = {"bias": "bullish"}
        assert _resolve_bias(analysis, ai) == "bullish"

    def test_invalid_ai_bias_falls_back(self):
        analysis = _mk_analysis(direction="bearish", confidence="medium")
        ai = {"bias": "yolo"}
        assert _resolve_bias(analysis, ai) == "bearish"

    def test_no_ai_json_uses_rule(self):
        analysis = _mk_analysis(direction="bullish", confidence="high")
        assert _resolve_bias(analysis, None) == "bullish"

    def test_ai_conviction_in_range(self):
        analysis = _mk_analysis(confidence="low")
        assert _resolve_conviction(analysis, "bullish", {"conviction": 78}) == 78

    def test_ai_conviction_out_of_range_falls_back(self):
        analysis = _mk_analysis(confidence="high")
        # 原规则 high → 80
        assert _resolve_conviction(analysis, "bullish", {"conviction": 150}) == 80

    def test_ai_conviction_non_numeric_falls_back(self):
        analysis = _mk_analysis(confidence="medium")
        assert _resolve_conviction(analysis, "bullish", {"conviction": "high"}) == 60


class TestApplyAIMatrixOverlay:
    def test_empty_json_returns_zero(self):
        snap = _mk_snapshot()
        mx = _make_matrix(snap)
        _, n = _apply_ai_matrix_overlay(mx, {})
        assert n == 0

    def test_overlay_overwrites_section_bias(self):
        snap = _mk_snapshot()
        mx = _make_matrix(snap)
        # 原始 A 板块可能 neutral；强制改成 bullish
        ai = {
            "bias": "bullish",
            "matrix_summary_cn": "看多（中）· DXY 走弱 + ETF 连续净流入",
            "sections": [
                {
                    "section_id": "A",
                    "section_bias": "bullish",
                    "section_summary_cn": "DXY 回落叠加纳指走强 · risk-on 共振",
                    "rows": [],
                },
            ],
        }
        mx2, n = _apply_ai_matrix_overlay(mx, ai)
        assert n >= 2  # 至少 section_bias + section_summary
        assert mx2.overall_bias == "bullish"
        assert "看多" in mx2.summary_line
        sec_a = next(s for s in mx2.sections if s.section_id == "A")
        assert sec_a.section_bias == "bullish"
        assert "DXY" in sec_a.section_summary

    def test_overlay_rewrites_row_direction_and_signal(self):
        snap = _mk_snapshot()
        mx = _make_matrix(snap)
        sec_c = next(s for s in mx.sections if s.section_id == "C")
        assert sec_c.rows, "C 板块应有至少一行"
        first_dim = sec_c.rows[0].dimension

        ai = {
            "sections": [
                {
                    "section_id": "C",
                    "rows": [
                        {
                            "dimension": first_dim,
                            "direction": "bearish",
                            "resonance": "high",
                            "signal_cn": "异常多头拥挤 · 轧多风险陡增",
                        },
                    ],
                },
            ],
        }
        mx2, n = _apply_ai_matrix_overlay(mx, ai)
        assert n >= 2  # 至少 direction + signal 被改（resonance 若原本就 high 不计）
        sec_c2 = next(s for s in mx2.sections if s.section_id == "C")
        row = next(r for r in sec_c2.rows if r.dimension == first_dim)
        assert row.direction == "bearish"
        assert row.resonance == "high"
        assert "轧多" in row.signal

    def test_overlay_invalid_direction_skipped(self):
        snap = _mk_snapshot()
        mx = _make_matrix(snap)
        sec_c = next(s for s in mx.sections if s.section_id == "C")
        first_dim = sec_c.rows[0].dimension
        orig_dir = sec_c.rows[0].direction

        ai = {
            "sections": [{
                "section_id": "C",
                "rows": [{"dimension": first_dim, "direction": "rocket"}],
            }],
        }
        mx2, _ = _apply_ai_matrix_overlay(mx, ai)
        sec_c2 = next(s for s in mx2.sections if s.section_id == "C")
        assert sec_c2.rows[0].direction == orig_dir  # 无效值被忽略

    def test_overlay_extra_rows_appended(self):
        snap = _mk_snapshot()
        mx = _make_matrix(snap)
        sec_a = next(s for s in mx.sections if s.section_id == "A")
        orig_count = len(sec_a.rows)
        ai = {
            "sections": [{
                "section_id": "A",
                "rows": [
                    {
                        "dimension": "AI 独立发现维度",
                        "direction": "bullish",
                        "resonance": "medium",
                        "signal_cn": "隐含维度：利率拐点",
                    },
                ],
            }],
        }
        mx2, _ = _apply_ai_matrix_overlay(mx, ai)
        sec_a2 = next(s for s in mx2.sections if s.section_id == "A")
        assert len(sec_a2.rows) == orig_count + 1
        appended = sec_a2.rows[-1]
        assert appended.dimension == "AI 独立发现维度"
        assert appended.data_source_ref == "§AI"
        assert appended.value_display == "⚡AI"

    def test_overlay_malformed_sections_graceful(self):
        snap = _mk_snapshot()
        mx = _make_matrix(snap)
        # sections 不是 list
        mx2, n = _apply_ai_matrix_overlay(mx, {"sections": "nope"})
        assert isinstance(mx2, type(mx))  # 不崩


class TestBuildReportWithAIJson:
    def test_ai_json_drives_bias_and_matrix(self):
        snap = _mk_snapshot()
        analysis = _mk_analysis(direction="neutral", confidence="low")
        analysis.ai_matrix_json = {
            "bias": "bullish",
            "conviction": 72,
            "matrix_summary_cn": "看多（中）· 短期顺势",
            "sections": [
                {
                    "section_id": "A",
                    "section_bias": "bullish",
                    "section_summary_cn": "DXY 走弱配合纳指上行",
                    "rows": [],
                },
            ],
        }
        report = build_ai_trader_report(analysis, snap, math_plan=_mk_math_plan())
        assert report.bias == "bullish"
        assert report.conviction == 72
        assert report.factor_matrix is not None
        assert report.factor_matrix.overall_bias == "bullish"
        sec_a = next(s for s in report.factor_matrix.sections if s.section_id == "A")
        assert sec_a.section_bias == "bullish"

    def test_no_ai_json_preserves_rule_path(self):
        snap = _mk_snapshot()
        analysis = _mk_analysis(direction="bullish", confidence="medium")
        assert analysis.ai_matrix_json is None
        report = build_ai_trader_report(analysis, snap, math_plan=_mk_math_plan())
        # 走规则路径仍然能正常产出
        assert report.bias == "bullish"
        assert report.conviction == 60
        assert report.factor_matrix is not None
        assert len(report.factor_matrix.sections) == 7

    def test_ai_json_empty_dict_treated_as_missing(self):
        snap = _mk_snapshot()
        analysis = _mk_analysis(direction="bullish", confidence="medium")
        analysis.ai_matrix_json = {}  # 空 dict 不应覆盖 bias
        report = build_ai_trader_report(analysis, snap, math_plan=_mk_math_plan())
        assert report.bias == "bullish"
        assert report.conviction == 60


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# P1.7 · AITRADER_MATRIX_JSON 块提取（analyzer 层）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

from ai.analyzer import _extract_matrix_json


class TestExtractMatrixJson:
    def test_happy_path(self):
        raw = """## 一、市场格局总览
偏多 中等信心

```AITRADER_MATRIX_JSON
{"bias": "bullish", "conviction": 70, "sections": [{"section_id": "A"}]}
```
"""
        p = _extract_matrix_json(raw)
        assert p is not None
        assert p["bias"] == "bullish"
        assert p["conviction"] == 70

    def test_case_insensitive_tag(self):
        raw = "```aitrader_matrix_json\n{\"bias\":\"bearish\"}\n```"
        p = _extract_matrix_json(raw)
        assert p is not None
        assert p["bias"] == "bearish"

    def test_fallback_to_json_block_with_sections(self):
        raw = "```json\n{\"sections\": [{\"section_id\":\"A\"}]}\n```"
        p = _extract_matrix_json(raw)
        assert p is not None
        assert "sections" in p

    def test_fallback_json_without_sections_rejected(self):
        raw = "```json\n{\"foo\": 1}\n```"
        # 必须含 sections 才视为 matrix
        assert _extract_matrix_json(raw) is None

    def test_malformed_json_returns_none(self):
        raw = "```AITRADER_MATRIX_JSON\n{bias: bullish, }\n```"
        assert _extract_matrix_json(raw) is None

    def test_no_block_returns_none(self):
        assert _extract_matrix_json("plain markdown no blocks") is None
        assert _extract_matrix_json("") is None

    def test_trailing_comma_tolerated(self):
        raw = """```AITRADER_MATRIX_JSON
{"bias": "bullish", "sections": [], }
```"""
        p = _extract_matrix_json(raw)
        assert p is not None
        assert p["bias"] == "bullish"

    def test_prefers_matrix_tag_over_json(self):
        raw = """```json
{"bias":"bearish","sections":[]}
```
```AITRADER_MATRIX_JSON
{"bias":"bullish","sections":[]}
```"""
        p = _extract_matrix_json(raw)
        assert p is not None
        assert p["bias"] == "bullish"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# P1.8b-① · AI JSON 直出 trading_plans
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

from ai.trader_report_builder import (
    _build_trading_plans_v2, _norm_action, _parse_ai_json_plans,
)


class TestNormAction:
    def test_long_variants(self):
        assert _norm_action("long") == "long"
        assert _norm_action("做多") == "long"
        assert _norm_action("BULLISH") == "long"

    def test_short_variants(self):
        assert _norm_action("short") == "short"
        assert _norm_action("做空") == "short"

    def test_wait(self):
        assert _norm_action("wait") == "wait"
        assert _norm_action("观望") == "wait"

    def test_invalid(self):
        assert _norm_action("rocket") is None
        assert _norm_action(None) is None


class TestParseAIJsonPlans:
    def test_full_long_plan(self):
        raw = [{
            "priority": 1, "direction": "long",
            "entry_low": 72300, "entry_high": 72450,
            "stop_loss": 71400, "tp1": 73800, "tp2": 75200,
            "rr_ratio": 2.4, "conviction": 72, "tier_hint": "A",
            "position_suggestion_pct": 30,
            "trigger_condition": "回踩支撑",
            "invalidation": "跌破 71400",
            "reason": "DXY 走弱 + ETF 流入",
        }]
        out = _parse_ai_json_plans(raw, None)
        assert len(out) == 1
        p = out[0]
        assert p.direction == "long"
        assert p.entry_zone_low == 72300
        assert p.stop_loss == 71400
        assert p.tp2 == 75200
        assert p.conviction == 72
        assert p.tier_hint == "A"
        assert p.position_suggestion_pct == 30
        assert "DXY" in p.reason

    def test_short_plan_sl_below_entry_rejected(self):
        # 做空 SL 必须 > entry，这条 SL 71400 < entry 72300 不合法，应丢弃
        raw = [{
            "priority": 1, "direction": "short",
            "entry_low": 72300, "stop_loss": 71400,
        }]
        out = _parse_ai_json_plans(raw, None)
        assert out == []

    def test_long_plan_sl_above_entry_rejected(self):
        raw = [{
            "priority": 1, "direction": "long",
            "entry_low": 72000, "stop_loss": 73000,
        }]
        out = _parse_ai_json_plans(raw, None)
        assert out == []

    def test_wait_plan_no_entry_required(self):
        raw = [{
            "priority": 1, "direction": "wait",
            "reason": "等关键位测试",
        }]
        out = _parse_ai_json_plans(raw, None)
        assert len(out) == 1
        assert out[0].direction == "wait"
        assert out[0].entry_zone_low is None

    def test_long_without_entry_rejected(self):
        raw = [{"priority": 1, "direction": "long", "reason": "凭空"}]
        out = _parse_ai_json_plans(raw, None)
        assert out == []

    def test_invalid_direction_skipped(self):
        raw = [
            {"direction": "rocket", "entry_low": 72000},
            {"direction": "long", "entry_low": 72000, "stop_loss": 71000},
        ]
        out = _parse_ai_json_plans(raw, None)
        assert len(out) == 1
        assert out[0].direction == "long"

    def test_priority_sort_stable(self):
        raw = [
            {"priority": 2, "direction": "wait"},
            {"priority": 1, "direction": "long", "entry_low": 72000, "stop_loss": 71000},
        ]
        out = _parse_ai_json_plans(raw, None)
        assert out[0].priority == 1
        assert out[1].priority == 2

    def test_max_3_plans(self):
        raw = [
            {"priority": i, "direction": "wait"} for i in range(1, 10)
        ]
        out = _parse_ai_json_plans(raw, None)
        assert len(out) <= 3

    def test_conviction_out_of_range_fallback(self):
        raw = [{"direction": "wait", "conviction": 999}]
        out = _parse_ai_json_plans(raw, None)
        assert len(out) == 1
        # priority=1 → 默认 60
        assert out[0].conviction == 60

    def test_tier_hint_invalid_falls_back_to_b(self):
        raw = [{"direction": "wait", "tier_hint": "Z"}]
        out = _parse_ai_json_plans(raw, None)
        assert out[0].tier_hint == "B"


class TestBuildTradingPlansV2:
    def test_ai_json_wins_over_markdown(self):
        snap = _mk_snapshot()
        analysis = _mk_analysis(direction="bullish", confidence="high")
        analysis.ai_matrix_json = {
            "trading_plans": [{
                "priority": 1, "direction": "short",
                "entry_low": 72500, "stop_loss": 73500,
                "tp1": 71000, "tp2": 69500, "rr_ratio": 2.6,
                "reason": "AI 独立判断",
            }],
        }
        plans, source = _build_trading_plans_v2(
            analysis, snap, None, analysis.ai_matrix_json,
        )
        assert source == "ai_json"
        assert len(plans) == 1
        assert plans[0].direction == "short"
        assert plans[0].stop_loss == 73500

    def test_no_ai_json_falls_back_to_markdown(self):
        snap = _mk_snapshot()
        analysis = _mk_analysis()  # entries 含一条 long
        plans, source = _build_trading_plans_v2(analysis, snap, None, None)
        assert source == "markdown"
        assert len(plans) == 1
        assert plans[0].direction == "long"

    def test_ai_json_all_invalid_falls_back(self):
        snap = _mk_snapshot()
        analysis = _mk_analysis()
        # AI JSON 给出但全部非法（做空却没 entry）
        plans, source = _build_trading_plans_v2(
            analysis, snap, None,
            {"trading_plans": [{"direction": "short"}]},
        )
        # 应回退到 markdown
        assert source == "markdown"

    def test_ai_json_empty_array_falls_back(self):
        snap = _mk_snapshot()
        analysis = _mk_analysis()
        plans, source = _build_trading_plans_v2(
            analysis, snap, None, {"trading_plans": []},
        )
        assert source == "markdown"

    def test_full_report_uses_ai_json_plans(self):
        """端到端：markdown 与 AI JSON 方向一致时，AI JSON plans/bias 生效"""
        snap = _mk_snapshot()
        analysis = _mk_analysis(direction="bearish")  # 与 AI JSON 一致，避免熔断
        analysis.ai_matrix_json = {
            "bias": "bearish",
            "conviction": 65,
            "trading_plans": [{
                "priority": 1, "direction": "short",
                "entry_low": 72500, "stop_loss": 73200,
                "tp1": 71000, "tp2": 69500, "rr_ratio": 3.0,
                "conviction": 68, "tier_hint": "A",
                "position_suggestion_pct": 25,
                "reason": "AI 独立发现顶部形态",
            }],
        }
        report = build_ai_trader_report(analysis, snap, math_plan=_mk_math_plan())
        assert report.bias == "bearish"  # AI bias 生效
        assert len(report.trading_plans) == 1
        p = report.trading_plans[0]
        assert p.direction == "short"
        assert p.conviction == 68  # AI 给的，不是规则推断的
        assert p.tier_hint == "A"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# P1.8b · 放开信任 + 冲突熔断
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

from ai.trader_report_builder import _detect_internal_conflict


class TestDetectInternalConflict:
    def test_opposite_direction_is_conflict(self):
        analysis = _mk_analysis(direction="bullish")
        assert _detect_internal_conflict(analysis, {"bias": "bearish"}) is True
        analysis = _mk_analysis(direction="bearish")
        assert _detect_internal_conflict(analysis, {"bias": "bullish"}) is True

    def test_same_direction_no_conflict(self):
        analysis = _mk_analysis(direction="bullish")
        assert _detect_internal_conflict(analysis, {"bias": "bullish"}) is False

    def test_neutral_no_conflict(self):
        analysis = _mk_analysis(direction="bullish")
        assert _detect_internal_conflict(analysis, {"bias": "neutral"}) is False
        analysis2 = _mk_analysis(direction="neutral")
        assert _detect_internal_conflict(analysis2, {"bias": "bullish"}) is False

    def test_empty_json_no_conflict(self):
        analysis = _mk_analysis(direction="bullish")
        assert _detect_internal_conflict(analysis, None) is False
        assert _detect_internal_conflict(analysis, {}) is False


class TestCircuitBreakerEndToEnd:
    def test_conflict_forces_neutral_and_low_conviction(self):
        snap = _mk_snapshot()
        analysis = _mk_analysis(direction="bullish", confidence="high")
        analysis.ai_matrix_json = {
            "bias": "bearish",
            "conviction": 80,
            "sections": [],
        }
        report = build_ai_trader_report(analysis, snap, math_plan=_mk_math_plan())
        assert report.bias == "neutral"
        assert report.conviction <= 40

    def test_conflict_marks_matrix_source(self):
        """冲突熔断后 ledger 应记录 matrix_source=internal_conflict"""
        from processors.ai_quality_ledger import get_ai_quality_ledger
        snap = _mk_snapshot()
        analysis = _mk_analysis(direction="bullish")
        analysis.ai_matrix_json = {"bias": "bearish", "sections": []}
        build_ai_trader_report(analysis, snap, math_plan=_mk_math_plan())
        recent = get_ai_quality_ledger().get_recent("BTC", limit=1)
        assert len(recent) == 1
        assert recent[0]["matrix_source"] == "internal_conflict"
        assert recent[0]["bias_vs_text"] == "conflict"


class TestAIRiskAppending:
    def test_ai_key_risks_appended(self):
        snap = _mk_snapshot()
        analysis = _mk_analysis(direction="bullish")
        analysis.ai_matrix_json = {
            "bias": "bullish",
            "sections": [],
            "key_risks": [
                "71,800 簇密集，止损 71,400 可能滑点",
                "11:30 非农数据高波动",
            ],
        }
        report = build_ai_trader_report(analysis, snap, math_plan=_mk_math_plan())
        joined = " | ".join(report.key_risks)
        assert "71,800" in joined or "71800" in joined
        assert "非农" in joined

    def test_duplicate_risks_dedup(self):
        snap = _mk_snapshot()
        analysis = _mk_analysis(direction="bullish")
        analysis.risk_warnings = ["关注 ETH 解锁"]
        analysis.ai_matrix_json = {
            "bias": "bullish",
            "sections": [],
            "key_risks": ["关注 ETH 解锁", "新增：ETF 净流出"],
        }
        report = build_ai_trader_report(analysis, snap, math_plan=_mk_math_plan())
        # "关注 ETH 解锁" 只出现一次
        hits = [r for r in report.key_risks if "ETH 解锁" in r]
        assert len(hits) == 1

    def test_non_list_ai_risks_ignored(self):
        snap = _mk_snapshot()
        analysis = _mk_analysis(direction="bullish")
        analysis.ai_matrix_json = {
            "bias": "bullish",
            "sections": [],
            "key_risks": "单条字符串",  # 故意非法
        }
        report = build_ai_trader_report(analysis, snap, math_plan=_mk_math_plan())
        # 不应抛错，且 key_risks 不会包含该字符串
        assert "单条字符串" not in " ".join(report.key_risks)


class TestMarketViewFallback:
    def test_markdown_overview_wins(self):
        snap = _mk_snapshot()
        analysis = _mk_analysis(direction="bullish")
        analysis.market_overview = "主线叙事是 ETF 持续流入"
        analysis.ai_matrix_json = {
            "bias": "bullish",
            "matrix_summary_cn": "不该采纳的 summary",
            "sections": [],
        }
        report = build_ai_trader_report(analysis, snap, math_plan=_mk_math_plan())
        assert "ETF" in report.market_view_cn
        assert "不该采纳" not in report.market_view_cn

    def test_empty_markdown_uses_ai_summary(self):
        snap = _mk_snapshot()
        analysis = _mk_analysis(direction="bullish")
        analysis.market_overview = ""
        analysis.ai_matrix_json = {
            "bias": "bullish",
            "matrix_summary_cn": "AI 结论：看多（中）· DXY 回落 + ETF 流入",
            "sections": [],
        }
        report = build_ai_trader_report(analysis, snap, math_plan=_mk_math_plan())
        assert "DXY" in report.market_view_cn

    def test_no_ai_json_keeps_empty_when_md_empty(self):
        snap = _mk_snapshot()
        analysis = _mk_analysis(direction="bullish")
        analysis.market_overview = ""
        report = build_ai_trader_report(analysis, snap, math_plan=_mk_math_plan())
        assert report.market_view_cn == ""


class TestExtraRowsAccepted:
    def test_ai_extra_row_keeps_ai_resonance(self):
        """AI 追加行的 resonance 应沿用 AI 指定值（不再强制 low）"""
        snap = _mk_snapshot()
        analysis = _mk_analysis(direction="bullish")
        analysis.ai_matrix_json = {
            "bias": "bullish",
            "sections": [
                {
                    "section_id": "D",
                    "section_bias": "bullish",
                    "section_summary_cn": "技术面偏多",
                    "rows": [
                        {
                            "dimension": "1D RSI 回升",
                            "signal_cn": "RSI 58 → 62，趋势回补",
                            "direction": "bullish",
                            "resonance": "high",
                        },
                    ],
                },
            ],
        }
        report = build_ai_trader_report(analysis, snap, math_plan=_mk_math_plan())
        sec_d = next(
            s for s in report.factor_matrix.sections if s.section_id == "D"
        )
        ai_rows = [r for r in sec_d.rows if r.data_source_ref == "§AI"]
        assert len(ai_rows) >= 1
        assert ai_rows[0].resonance == "high"
