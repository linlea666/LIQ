"""D14 · AI Trader Report Builder

职责：
  - 把现有 AIAnalysisResult + AISnapshot + 周边 context（geo/narrative/math_plan）
    组装成 AITraderReport
  - 让"AI 主模块"从简单的文本解析输出升级成**独立资深交易员**的结构化产物
  - 保留旧链路零破坏：AIAnalyzer.analyze() 仍输出 AIAnalysisResult，本 builder 只是
    在其之上叠加结构化层（适配器路径 · 九原则 §6 保守修改）

AIFactorMatrix 7 板块：
  A · 宏观联动 · DXY / Nasdaq / Gold / VIX / 10Y / Oil（来自 snapshot.macro_*）
  B · 资金流   · 稳定币 / ETF / 巨鲸 / 交易所净流（来自 snapshot.stablecoin / etf / whale）
  C · 衍生品   · OI / 资金费率 / 多空比 / 爆仓（snapshot.oi / funding / ls_ratio / liq）
  D · 技术面   · RSI / MACD / BOLL / MA / K 线形态（snapshot.rsi_14 / macd / ...）
  E · 新闻叙事 · 活跃主题 / flip-flop / priced-in（snapshot.active_narratives + news_brief）
  F · 地缘风险 · overall_level / escalation（snapshot.geo_overview）
  G · 双引擎共识 · math vs ai bias 对齐（external input）

对齐判定（agreement_with_math_engine）：
  agree    — math.direction == ai.bias 且 ai.bias != neutral
  caution  — 一方观望 / 分数差异显著
  disagree — 明确反向

落实日志锚点：D14 每次 build 上报 matrix_rows / plans_count / agreement
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

from models.ai_trader_report import (
    AIFactorMatrix,
    AIFactorRow,
    AIFactorSection,
    AIKeyLevelInterpretation,
    AINarrativeImpact,
    AITraderReport,
    AITradingPlan,
)
from models.common_enums import Confidence, Direction, SignalTier, TradingAction
from models.execution_plan import ExecutionPlan
from models.snapshot import AIAnalysisResult, AISnapshot

logger = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 公开入口
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def build_ai_trader_report(
    analysis: AIAnalysisResult,
    snapshot: AISnapshot,
    *,
    math_plan: Optional[ExecutionPlan] = None,
    model_name: str = "",
    latency_ms: int = 0,
    thinking_tokens: int = 0,
) -> AITraderReport:
    """组装 AI Trader Report。

    输入：
      analysis      — AIAnalyzer 的原始结构化产物
      snapshot      — 同批次的数据快照（供 FactorMatrix 用）
      math_plan     — 数学引擎输出（用于 G 板块共识评估）
      model_name    — 实际使用的 AI 模型名（deepseek-v4-flash / ...）
      latency_ms    — 本次 AI 调用耗时
      thinking_tokens — 思考 token 数（v4-flash 非思考模式恒为 0，保留兼容 R1）
    """
    # P1.7：优先采纳 AI 产出的 matrix_json（若存在且合法）
    ai_json = getattr(analysis, "ai_matrix_json", None) or None

    bias = _resolve_bias(analysis, ai_json)
    conviction = _resolve_conviction(analysis, bias, ai_json)

    # P1.8b-④ · 冲突熔断：AI JSON bias 与 markdown signal_summary.direction 严重矛盾
    # 硬性保底：立即把 bias 拉回 neutral，并下调 conviction（下游不得继续把 AI 当看多/看空用）
    internal_conflict = _detect_internal_conflict(analysis, ai_json)
    if internal_conflict:
        logger.warning(
            "[D14] internal conflict: json.bias=%s vs markdown.direction=%s (coin=%s)",
            (ai_json or {}).get("bias"),
            (analysis.signal_summary.direction if analysis.signal_summary else None),
            analysis.coin,
        )
        bias = "neutral"
        conviction = min(conviction, 40)

    # ── Key Level 解读 ──
    klvl = _build_key_level_interpretation(analysis, snapshot)

    # ── 交易计划（P1.8b-①：AI JSON 直出优先 → markdown entries → sniper → 观望） ──
    plans, plans_source = _build_trading_plans_v2(analysis, snapshot, math_plan, ai_json)

    # ── 叙事影响 ──
    narr_impact = _build_narrative_impact(snapshot)

    # ── 7 板块因子看盘表（先走规则生成，再用 AI JSON 覆写 direction/bias/summary） ──
    matrix = build_factor_matrix(
        snapshot=snapshot,
        ai_bias=bias,
        ai_conviction=conviction,
        math_plan=math_plan,
    )
    matrix_source = "rule_fallback"
    overlaid = 0
    if ai_json:
        try:
            matrix, overlaid = _apply_ai_matrix_overlay(matrix, ai_json)
            if overlaid > 0:
                matrix_source = "ai_json"
        except Exception:
            logger.warning(
                "[D14] matrix overlay failed, fallback to rule | coin=%s",
                analysis.coin, exc_info=True,
            )
            matrix_source = "rule_fallback"
            overlaid = 0
    # 冲突熔断优先标注（便于下游观测）
    if internal_conflict:
        matrix_source = "internal_conflict"

    # ── 双引擎对齐 ──
    agreement, agreement_notes = _judge_agreement(bias, conviction, math_plan)

    # ── 新闻 / 地缘摘要 ──
    news_summary = _summarize_news(snapshot)
    geo_summary = _summarize_geo(snapshot)

    # ── 风险提示（analysis.risk_warnings + AI JSON.key_risks + 强化规则） ──
    key_risks = list(analysis.risk_warnings or [])
    # P1.8b-③ · 追加 AI JSON 的 key_risks（去重）
    if ai_json:
        ai_risks = ai_json.get("key_risks") or []
        if isinstance(ai_risks, list):
            seen = {r.strip().lower() for r in key_risks if isinstance(r, str)}
            for item in ai_risks[:5]:
                if not isinstance(item, str):
                    continue
                s = item.strip()
                if not s:
                    continue
                if s.lower() in seen:
                    continue
                key_risks.append(s[:120])
                seen.add(s.lower())
    if snapshot.geo_overview:
        lvl = int(snapshot.geo_overview.get("overall_level", 0) or 0)
        if lvl >= 4:
            key_risks.insert(0, f"地缘风险等级 {lvl}/5 · 禁止新开仓")
    if snapshot.active_narratives:
        flip_themes = [
            t for t in snapshot.active_narratives
            if int(t.get("flip_flop_count_24h", 0) or 0) >= 2
        ]
        if flip_themes:
            names = ",".join(t.get("theme_id", "?") for t in flip_themes[:3])
            key_risks.append(f"反复叙事: {names} · 权重降档")

    # P1.8b-② · market_view_cn：若 markdown 空，用 AI JSON 的 matrix_summary_cn 兜底
    market_view = (analysis.market_overview or "").strip()
    if not market_view and ai_json:
        fallback = str(ai_json.get("matrix_summary_cn") or "").strip()
        if fallback:
            market_view = fallback[:240]

    report = AITraderReport(
        coin=analysis.coin,
        ts=int(analysis.ts or time.time()),
        price_at_analysis=float(analysis.price_at_analysis or snapshot.price or 0.0),
        model=model_name,
        thinking_tokens=int(thinking_tokens or 0),
        latency_ms=int(latency_ms or 0),
        market_view_cn=market_view,
        bias=bias,
        conviction=conviction,
        key_level_interpretation=klvl,
        trading_plans=plans,
        narrative_impact=narr_impact,
        news_impact_summary_cn=news_summary,
        geo_risk_assessment_cn=geo_summary,
        agreement_with_math_engine=agreement,
        agreement_notes_cn=agreement_notes,
        key_risks=key_risks,
        factor_matrix=matrix,
        scenario_analysis=list(analysis.scenario_analysis or []),
        raw_text=analysis.raw_text or "",
        user_prompt=analysis.user_prompt or "",
    )
    _mark_d14(
        report, math_plan,
        matrix_source=matrix_source,
        plans_source=plans_source,
    )
    # P1.8a · AI Quality Ledger 落一条
    _record_quality(
        report=report,
        analysis=analysis,
        ai_json=ai_json,
        matrix_source=matrix_source,
        plans_source=plans_source,
        overlay_fields=(overlaid if ai_json else 0),
    )
    return report


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Factor Matrix（7 板块）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def build_factor_matrix(
    snapshot: AISnapshot,
    *,
    ai_bias: Direction = "neutral",
    ai_conviction: int = 50,
    math_plan: Optional[ExecutionPlan] = None,
) -> AIFactorMatrix:
    sections = [
        _section_a_macro(snapshot),
        _section_b_capital_flow(snapshot),
        _section_c_derivatives(snapshot),
        _section_d_technical(snapshot),
        _section_e_news(snapshot),
        _section_f_geo(snapshot),
        _section_g_consensus(ai_bias, ai_conviction, math_plan),
    ]
    overall_bias, overall_conf = _aggregate_matrix_bias(sections)
    summary = _matrix_summary_line(overall_bias, overall_conf)
    return AIFactorMatrix(
        sections=sections,
        summary_line=summary,
        overall_bias=overall_bias,
        overall_confidence=overall_conf,
        analysis_ts=int(snapshot.ts or time.time()),
        price_at_analysis=float(snapshot.price or 0.0),
    )


def _section_a_macro(snapshot: AISnapshot) -> AIFactorSection:
    """构造宏观联动板块。

    从 AISnapshot 顶层散落字段（dxy / nasdaq / sp500 / gold / us_10y_yield）直接组装，
    不再依赖不存在的 `macro_items` / `market_index_items` 数组字段，避免走 fallback
    后由 AI 重复填充（导致 "数据未就绪" 占位与 AI 补丁行共存的 UI 矛盾）。
    """
    rows: list[AIFactorRow] = []

    # ── DXY：美元指数，与风险资产反向 ──
    dxy = getattr(snapshot, "dxy", None)
    dxy_chg = getattr(snapshot, "dxy_change_pct", None)
    if isinstance(dxy, (int, float)):
        d_dxy: Direction = "neutral"
        if isinstance(dxy_chg, (int, float)):
            if dxy_chg > 0.2:
                d_dxy = "bearish"   # DXY 涨 → 美元强 → 风险资产承压
            elif dxy_chg < -0.2:
                d_dxy = "bullish"
        rows.append(AIFactorRow(
            dimension="DXY(美元指数)",
            value_display=(
                f"{dxy:.2f} ({dxy_chg:+.2f}%)"
                if isinstance(dxy_chg, (int, float))
                else f"{dxy:.2f}"
            ),
            value_raw=float(dxy),
            signal=(
                "美元走强·风险承压" if d_dxy == "bearish"
                else "美元走弱·风险利好" if d_dxy == "bullish"
                else "持平"
            ),
            direction=d_dxy, resonance="medium", data_source_ref="§macro.dxy",
        ))

    # ── 美股指数：与 crypto 正相关 ──
    for name_cn, val_key, chg_key, src in (
        ("纳指", "nasdaq", "nasdaq_change_pct", "§macro.nasdaq"),
        ("标普500", "sp500", "sp500_change_pct", "§macro.sp500"),
    ):
        v = getattr(snapshot, val_key, None)
        c = getattr(snapshot, chg_key, None)
        if not isinstance(v, (int, float)):
            continue
        d_eq: Direction = "neutral"
        if isinstance(c, (int, float)):
            if c > 0.3:
                d_eq = "bullish"
            elif c < -0.3:
                d_eq = "bearish"
        rows.append(AIFactorRow(
            dimension=name_cn,
            value_display=(
                f"{v:,.2f} ({c:+.2f}%)"
                if isinstance(c, (int, float))
                else f"{v:,.2f}"
            ),
            value_raw=float(v),
            signal=_macro_signal_text(name_cn, c),
            direction=d_eq, resonance="medium", data_source_ref=src,
        ))

    # ── 黄金：与 crypto 相关性不固定，只展示不定方向 ──
    gold = getattr(snapshot, "gold", None)
    gold_chg = getattr(snapshot, "gold_change_pct", None)
    if isinstance(gold, (int, float)):
        rows.append(AIFactorRow(
            dimension="黄金",
            value_display=(
                f"${gold:,.2f} ({gold_chg:+.2f}%)"
                if isinstance(gold_chg, (int, float))
                else f"${gold:,.2f}"
            ),
            value_raw=float(gold),
            signal=_macro_signal_text("黄金", gold_chg),
            direction="neutral", resonance="low", data_source_ref="§macro.gold",
        ))

    # ── 美债 10Y 收益率：高利率环境压制风险资产 ──
    # 仅有绝对水平（无 change_pct），按阈值给定性判断
    y10 = getattr(snapshot, "us_10y_yield", None)
    if isinstance(y10, (int, float)):
        if y10 >= 4.5:
            d_y10: Direction = "bearish"
            sig_y10 = "高利率压制风险资产"
        elif y10 >= 3.5:
            d_y10 = "neutral"
            sig_y10 = "利率温和"
        else:
            d_y10 = "bullish"
            sig_y10 = "利率友好·流动性宽松"
        rows.append(AIFactorRow(
            dimension="美债 10Y 收益率",
            value_display=f"{y10:.2f}%",
            value_raw=float(y10),
            signal=sig_y10,
            direction=d_y10, resonance="medium", data_source_ref="§macro.bond",
        ))

    # ── 兼容老口径 macro_items / market_index_items（若未来迁移到数组） ──
    legacy_raw = (
        getattr(snapshot, "macro_items", None)
        or getattr(snapshot, "market_index_items", None)
        or []
    )
    for item in legacy_raw[:6]:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or item.get("key") or "")
        chg = item.get("change_pct")
        val = item.get("value") or item.get("price")
        d_leg: Direction = "neutral"
        if isinstance(chg, (int, float)):
            if chg > 0.3:
                d_leg = "bullish"
            elif chg < -0.3:
                d_leg = "bearish"
        rows.append(AIFactorRow(
            dimension=name or "macro",
            value_display=(
                f"{val} ({chg:+.2f}%)"
                if isinstance(chg, (int, float))
                else str(val or "-")
            ),
            value_raw=float(val) if isinstance(val, (int, float)) else None,
            signal=_macro_signal_text(name, chg),
            direction=d_leg, resonance="medium", data_source_ref="§macro",
        ))

    if not rows:
        rows.append(AIFactorRow(
            dimension="宏观指数", value_display="数据未就绪",
            signal="等待数据", direction="neutral", resonance="low",
            data_source_ref="§macro",
        ))
    bias = _majority_direction(rows)
    return AIFactorSection(
        section_id="A", section_name_cn="宏观联动", section_emoji="🌐",
        rows=rows,
        section_summary=_section_summary("宏观", bias, len(rows)),
        section_bias=bias,
    )


def _section_b_capital_flow(snapshot: AISnapshot) -> AIFactorSection:
    rows: list[AIFactorRow] = []

    # 稳定币市值
    sm = float(getattr(snapshot, "stablecoin_total_mcap", 0.0) or 0.0)
    chg7 = float(getattr(snapshot, "stablecoin_7d_change_pct", 0.0) or 0.0)
    if sm > 0:
        d: Direction = "bullish" if chg7 > 0.2 else ("bearish" if chg7 < -0.2 else "neutral")
        rows.append(AIFactorRow(
            dimension="稳定币总市值",
            value_display=f"${sm/1e9:,.2f}B ({chg7:+.2f}%/7d)",
            value_raw=sm, signal=f"7d 变化 {chg7:+.2f}%",
            direction=d, resonance="medium", data_source_ref="§9e",
        ))

    # ETF 净流入（复用 snapshot 里聚合）
    etf_trend = getattr(snapshot, "etf_trend_5d", "") or ""
    if etf_trend:
        d = "bullish" if "流入" in etf_trend else ("bearish" if "流出" in etf_trend else "neutral")
        rows.append(AIFactorRow(
            dimension="ETF 5d 净流", value_display=etf_trend,
            signal=etf_trend, direction=d, resonance="high", data_source_ref="§etf",
        ))

    # 巨鲸净方向
    whale_dir = getattr(snapshot, "whale_net_direction", "") or ""
    whale_net = float(getattr(snapshot, "whale_transfer_net_usd", 0.0) or 0.0)
    if whale_net:
        d = "bullish" if whale_net > 0 else "bearish"
        rows.append(AIFactorRow(
            dimension="巨鲸净转账 24h",
            value_display=f"${whale_net/1e6:,.1f}M {whale_dir}",
            value_raw=whale_net, signal=whale_dir or "净流观察",
            direction=d, resonance="medium", data_source_ref="§whale",
        ))

    if not rows:
        rows.append(AIFactorRow(
            dimension="资金流", value_display="数据未就绪",
            signal="等待数据", direction="neutral", resonance="low",
            data_source_ref="§capital",
        ))

    bias = _majority_direction(rows)
    return AIFactorSection(
        section_id="B", section_name_cn="资金流", section_emoji="💰",
        rows=rows,
        section_summary=_section_summary("资金", bias, len(rows)),
        section_bias=bias,
    )


def _section_c_derivatives(snapshot: AISnapshot) -> AIFactorSection:
    rows: list[AIFactorRow] = []

    oi_chg = float(getattr(snapshot, "oi_change_1h_pct", 0.0) or 0.0)
    oi_trend = getattr(snapshot, "oi_trend", "") or ""
    if oi_chg or oi_trend:
        d: Direction = "bullish" if oi_chg > 0.3 else ("bearish" if oi_chg < -0.3 else "neutral")
        rows.append(AIFactorRow(
            dimension="OI 1h 变化",
            value_display=f"{oi_chg:+.2f}% · {oi_trend}",
            value_raw=oi_chg, signal=oi_trend or f"{oi_chg:+.2f}%",
            direction=d, resonance="high", data_source_ref="§3",
        ))

    fr_bn = getattr(snapshot, "funding_rate_binance", None)
    fr_okx = getattr(snapshot, "funding_rate_okx", None)
    funds: list[float] = [x for x in (fr_bn, fr_okx) if isinstance(x, (int, float))]
    if funds:
        avg = sum(funds) / len(funds)
        d = "bearish" if avg > 0.02 else ("bullish" if avg < -0.01 else "neutral")
        rows.append(AIFactorRow(
            dimension="资金费率均值(bps)",
            value_display=f"{avg*10000:+.2f} bps · BN/OKX",
            value_raw=float(avg), signal="多头拥挤" if avg > 0.02 else ("空头拥挤" if avg < -0.01 else "中性"),
            direction=d, resonance="high", data_source_ref="§4",
        ))

    ls = getattr(snapshot, "ls_ratio_top_position", None)
    if isinstance(ls, (int, float)) and ls:
        d = "bearish" if ls > 1.3 else ("bullish" if ls < 0.77 else "neutral")
        rows.append(AIFactorRow(
            dimension="Top 持仓 L/S",
            value_display=f"{ls:.2f}",
            value_raw=float(ls), signal="多头拥挤" if d == "bearish" else ("空头拥挤" if d == "bullish" else "中性"),
            direction=d, resonance="medium", data_source_ref="§ls",
        ))

    liq24_long = float(getattr(snapshot, "recent_liq_24h_long_usd", 0.0) or 0.0)
    liq24_short = float(getattr(snapshot, "recent_liq_24h_short_usd", 0.0) or 0.0)
    if liq24_long + liq24_short > 0:
        d = "bullish" if liq24_short > liq24_long * 1.3 else (
            "bearish" if liq24_long > liq24_short * 1.3 else "neutral"
        )
        rows.append(AIFactorRow(
            dimension="24h 爆仓对比",
            value_display=f"多{liq24_long/1e6:,.1f}M vs 空{liq24_short/1e6:,.1f}M",
            value_raw=liq24_long - liq24_short,
            signal="空方清算更多" if d == "bullish" else ("多方清算更多" if d == "bearish" else "均衡"),
            direction=d, resonance="medium", data_source_ref="§1",
        ))

    if not rows:
        rows.append(AIFactorRow(
            dimension="衍生品", value_display="数据未就绪",
            signal="等待数据", direction="neutral", resonance="low",
            data_source_ref="§deriv",
        ))
    bias = _majority_direction(rows)
    return AIFactorSection(
        section_id="C", section_name_cn="衍生品结构", section_emoji="⚙️",
        rows=rows,
        section_summary=_section_summary("衍生品", bias, len(rows)),
        section_bias=bias,
    )


def _ms_row(tf_label: str, ms: dict | None) -> AIFactorRow | None:
    """把单个 TF 的市场结构字典转成一行 AIFactorRow（展示在技术面 §D）。

    - 仅 bullish/bearish 时 direction 非 neutral
    - 置信度高（≥0.6）→ resonance=high，低（<0.4）→ low，中间 medium
    - value_display 形如 "上升结构 · BOS↑ · 置信 82%"
    """
    if not ms:
        return None
    dir_raw = ms.get("direction") or ""
    if dir_raw == "bullish":
        direction: Direction = "bullish"
        dir_cn = "上升结构"
    elif dir_raw == "bearish":
        direction = "bearish"
        dir_cn = "下降结构"
    elif dir_raw == "ranging":
        direction = "neutral"
        dir_cn = "震荡结构"
    elif dir_raw == "transitioning":
        direction = "neutral"
        dir_cn = "结构转换中"
    else:
        return None

    conf = float(ms.get("confidence") or 0)
    event_cn_map = {
        "BOS_up": "BOS↑",
        "BOS_down": "BOS↓",
        "CHoCH_up": "CHoCH↑",
        "CHoCH_down": "CHoCH↓",
    }
    event = event_cn_map.get(ms.get("last_event") or "", "")
    bias_map = {
        "long_only": "仅做多",
        "short_only": "仅做空",
        "both_ok": "双向",
        "stand_aside": "观望",
    }
    bias_cn = bias_map.get(ms.get("operate_bias") or "", "")

    value_parts = [dir_cn]
    if event:
        value_parts.append(event)
    value_parts.append(f"置信 {conf * 100:.0f}%")
    value_display = " · ".join(value_parts)

    signal_parts = []
    if bias_cn:
        signal_parts.append(bias_cn)
    sh, sl = ms.get("structure_high"), ms.get("structure_low")
    if isinstance(sh, (int, float)) and isinstance(sl, (int, float)):
        signal_parts.append(f"区间 ${sl:,.0f}-${sh:,.0f}")
    signal = " · ".join(signal_parts) if signal_parts else "结构未激活"

    if conf >= 0.6:
        resonance: Any = "high"
    elif conf >= 0.4:
        resonance = "medium"
    else:
        resonance = "low"

    return AIFactorRow(
        dimension=f"{tf_label} 价格结构",
        value_display=value_display,
        value_raw=None,
        signal=signal,
        direction=direction,
        resonance=resonance,
        data_source_ref="§9i",
    )


def _section_d_technical(snapshot: AISnapshot) -> AIFactorSection:
    rows: list[AIFactorRow] = []

    # MTF 市场结构：1w → 1d → 1h（大周期先展示，引导 AI 自上而下）
    for tf_label, attr in (("1w", "market_structure_1w"),
                           ("1d", "market_structure_1d"),
                           ("1h", "market_structure")):
        ms_dict = getattr(snapshot, attr, None)
        row = _ms_row(tf_label, ms_dict)
        if row is not None:
            rows.append(row)

    rsi = getattr(snapshot, "rsi_14", None)
    if isinstance(rsi, (int, float)):
        if rsi >= 70:
            d: Direction = "bearish"
            sig = "超买"
        elif rsi <= 30:
            d = "bullish"
            sig = "超卖"
        else:
            d = "neutral"
            sig = "中性区"
        rows.append(AIFactorRow(
            dimension="RSI(14)", value_display=f"{rsi:.1f}",
            value_raw=float(rsi), signal=sig, direction=d,
            resonance="high", data_source_ref="§9a",
        ))

    macd = getattr(snapshot, "macd_data", None) or {}
    if isinstance(macd, dict) and macd:
        hist = macd.get("histogram") or macd.get("hist")
        if isinstance(hist, (int, float)):
            d = "bullish" if hist > 0 else "bearish"
            rows.append(AIFactorRow(
                dimension="MACD 柱", value_display=f"{hist:+.3f}",
                value_raw=float(hist), signal="多头动能" if hist > 0 else "空头动能",
                direction=d, resonance="medium", data_source_ref="§9a",
            ))

    price = float(snapshot.price or 0.0)
    for ma_key, ma_name in (("ma60_daily", "60d MA"), ("ma120_daily", "120d MA")):
        v = getattr(snapshot, ma_key, None)
        if isinstance(v, (int, float)) and v > 0 and price > 0:
            diff_pct = (price - v) / v * 100
            d = "bullish" if diff_pct > 1 else ("bearish" if diff_pct < -1 else "neutral")
            rows.append(AIFactorRow(
                dimension=ma_name,
                value_display=f"${v:,.0f} ({diff_pct:+.2f}%)",
                value_raw=float(v),
                signal=f"价格偏离 {diff_pct:+.2f}%",
                direction=d, resonance="medium", data_source_ref="§9b",
            ))

    kp = getattr(snapshot, "candlestick_pattern_name", "")
    kside = getattr(snapshot, "candlestick_pattern_side", "")
    if kp:
        d = "bullish" if kside == "support" else ("bearish" if kside == "resistance" else "neutral")
        rows.append(AIFactorRow(
            dimension="K 线形态",
            value_display=kp,
            signal=f"{kside or '中性'}: {kp}",
            direction=d, resonance="medium", data_source_ref="§9c",
        ))

    if not rows:
        rows.append(AIFactorRow(
            dimension="技术面", value_display="数据未就绪",
            signal="等待数据", direction="neutral", resonance="low",
            data_source_ref="§tech",
        ))
    bias = _majority_direction(rows)
    return AIFactorSection(
        section_id="D", section_name_cn="技术面", section_emoji="📈",
        rows=rows,
        section_summary=_section_summary("技术", bias, len(rows)),
        section_bias=bias,
    )


def _section_e_news(snapshot: AISnapshot) -> AIFactorSection:
    rows: list[AIFactorRow] = []
    themes = list(snapshot.active_narratives or [])
    for t in themes[:5]:
        tid = str(t.get("theme_id") or "?")
        name_cn = str(t.get("theme_name_cn") or "")
        bias_v = str(t.get("current_direction_bias") or "neutral")
        intensity = int(t.get("current_intensity") or 0)
        ff = int(t.get("flip_flop_count_24h") or 0)
        d: Direction = bias_v if bias_v in {"bullish", "bearish", "neutral", "potential_reversal"} else "neutral"  # type: ignore[assignment]
        tag = f" ⚠反复{ff}" if ff >= 2 else ""
        rows.append(AIFactorRow(
            dimension=f"{tid}",
            value_display=f"{name_cn or tid}{tag}",
            value_raw=float(intensity),
            signal=f"bias={bias_v} · int={intensity}/5",
            direction=d, resonance="high" if intensity >= 3 else "medium",
            data_source_ref="§13",
        ))
    if snapshot.news_brief_text:
        rows.append(AIFactorRow(
            dimension="Rolling Brief",
            value_display=f"v{snapshot.news_brief_version or 0}",
            signal=f"trigger={snapshot.news_brief_trigger or 'scheduled'}",
            direction="neutral", resonance="low", data_source_ref="§13",
        ))
    if not rows:
        rows.append(AIFactorRow(
            dimension="新闻叙事", value_display="24h 内无活跃叙事",
            signal="空窗期", direction="neutral", resonance="low",
            data_source_ref="§13",
        ))
    bias = _majority_direction(rows)
    return AIFactorSection(
        section_id="E", section_name_cn="新闻与叙事", section_emoji="📰",
        rows=rows,
        section_summary=_section_summary("新闻", bias, len(rows)),
        section_bias=bias,
    )


def _section_f_geo(snapshot: AISnapshot) -> AIFactorSection:
    rows: list[AIFactorRow] = []
    geo = snapshot.geo_overview or {}
    if geo:
        lvl = int(geo.get("overall_level", 0) or 0)
        label = str(geo.get("overall_label", "PEACE"))
        emoji = str(geo.get("overall_emoji", "🟢"))
        escalation_24h = int(geo.get("escalation_count_24h", 0) or 0)
        blackswan = bool(geo.get("has_blackswan_24h", False))
        direction: Direction = "bearish" if lvl >= 3 else ("neutral" if lvl >= 1 else "bullish")
        rows.append(AIFactorRow(
            dimension="地缘综合等级",
            value_display=f"{emoji} {label} · {lvl}/5",
            value_raw=float(lvl),
            signal=f"escalation24h={escalation_24h} blackswan={blackswan}",
            direction=direction, resonance="high" if lvl >= 3 else "medium",
            data_source_ref="§13",
        ))
        if geo.get("suggest_position_cap_pct") is not None:
            cap = geo.get("suggest_position_cap_pct")
            rows.append(AIFactorRow(
                dimension="建议仓位上限",
                value_display=f"{cap}%",
                value_raw=float(cap or 0),
                signal="SafetyGate 协同降仓",
                direction="bearish" if (cap or 100) < 60 else "neutral",
                resonance="medium", data_source_ref="§13",
            ))
    if not rows:
        rows.append(AIFactorRow(
            dimension="地缘风险", value_display="🟢 PEACE",
            signal="全局平稳", direction="neutral", resonance="low",
            data_source_ref="§13",
        ))
    bias = _majority_direction(rows)
    return AIFactorSection(
        section_id="F", section_name_cn="地缘风险", section_emoji="⚠️",
        rows=rows,
        section_summary=_section_summary("地缘", bias, len(rows)),
        section_bias=bias,
    )


def _section_g_consensus(
    ai_bias: Direction, ai_conviction: int, math_plan: Optional[ExecutionPlan]
) -> AIFactorSection:
    rows: list[AIFactorRow] = []
    if math_plan is None:
        rows.append(AIFactorRow(
            dimension="数学引擎状态", value_display="未就绪",
            signal="等待 ExecutionPlan", direction="neutral", resonance="low",
            data_source_ref="§math",
        ))
    else:
        math_dir = math_plan.direction
        math_score = float(math_plan.execution_score or 50.0)
        aligned = _directions_agree(math_dir, ai_bias)
        d: Direction = "bullish" if (aligned and ai_bias == "bullish") else (
            "bearish" if (aligned and ai_bias == "bearish") else "neutral"
        )
        rows.append(AIFactorRow(
            dimension="数学引擎",
            value_display=f"{math_plan.tier_hint}·{math_dir}·score{math_score:.0f}",
            value_raw=math_score,
            signal=math_plan.headline or math_plan.one_liner or "ExecutionPlan",
            direction=_direction_from_plan(math_dir),
            resonance="high", data_source_ref="§L4",
        ))
        rows.append(AIFactorRow(
            dimension="AI Trader",
            value_display=f"{ai_bias}·conviction{ai_conviction}",
            value_raw=float(ai_conviction),
            signal="同向" if aligned else ("反向" if _directions_conflict(math_dir, ai_bias) else "一方中性"),
            direction=ai_bias,
            resonance="high", data_source_ref="§L7",
        ))
    bias = _majority_direction(rows)
    return AIFactorSection(
        section_id="G", section_name_cn="双引擎共识", section_emoji="🎯",
        rows=rows,
        section_summary=_section_summary("共识", bias, len(rows)),
        section_bias=bias,
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# P1.7 · AI JSON 附录 overlay
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_VALID_DIRECTIONS = {"bullish", "bearish", "neutral", "potential_reversal"}
_VALID_SECTION_BIAS = {"bullish", "bearish", "neutral"}
_VALID_RESONANCE = {"high", "medium", "low"}


def _apply_ai_matrix_overlay(
    matrix: AIFactorMatrix, ai_json: dict
) -> tuple[AIFactorMatrix, int]:
    """用 AI JSON 附录覆写规则生成的 matrix。

    覆写范围（**只改方向/共振/文案**，不动 value_display / value_raw / data_source_ref）：
      - section_bias
      - section_summary
      - row.direction / row.resonance / row.signal（若 AI 给出更自然的中文）
      - matrix.overall_bias / summary_line（若 AI 给出）

    匹配策略：
      - section_id (A~G) 精确匹配
      - 行按 dimension 名称（严格 → 宽松：去空格/大小写/常见同义词）
      - AI 多给的行追加到该 section 末尾（标 `resonance=low` 提示非数据源）

    返回：
      (new_matrix, overlaid_count)  overlaid_count=被改动的字段总数；为 0 视为回退
    """
    overlaid = 0

    # 顶层 bias / summary_line
    top_bias = _norm_section_bias(ai_json.get("bias"))
    if top_bias and top_bias != matrix.overall_bias:
        matrix.overall_bias = top_bias  # type: ignore[assignment]
        overlaid += 1
    ai_summary = str(ai_json.get("matrix_summary_cn") or "").strip()
    if ai_summary:
        matrix.summary_line = ai_summary[:120]
        overlaid += 1

    # Sections
    ai_sections = ai_json.get("sections") or []
    if not isinstance(ai_sections, list):
        return matrix, overlaid

    # 按 id 建索引
    by_id: dict[str, dict] = {}
    for sec in ai_sections:
        if not isinstance(sec, dict):
            continue
        sid = str(sec.get("section_id") or "").strip().upper()
        if sid in {"A", "B", "C", "D", "E", "F", "G"}:
            by_id[sid] = sec

    for section in matrix.sections:
        ai_sec = by_id.get(section.section_id)
        if not ai_sec:
            continue

        sbias = _norm_section_bias(ai_sec.get("section_bias"))
        if sbias and sbias != section.section_bias:
            section.section_bias = sbias  # type: ignore[assignment]
            overlaid += 1

        summary = str(ai_sec.get("section_summary_cn") or "").strip()
        if summary:
            section.section_summary = summary[:140]
            overlaid += 1

        # 行级覆写（按 dimension 名称匹配；不匹配则保留原行）
        ai_rows = ai_sec.get("rows") or []
        if isinstance(ai_rows, list):
            ai_row_map = {
                _normalize_dim_key(r.get("dimension")): r
                for r in ai_rows
                if isinstance(r, dict) and r.get("dimension")
            }
            for row in section.rows:
                key = _normalize_dim_key(row.dimension)
                ai_row = ai_row_map.pop(key, None)
                if not ai_row:
                    continue
                ai_dir = _norm_direction(ai_row.get("direction"))
                if ai_dir and ai_dir != row.direction:
                    row.direction = ai_dir  # type: ignore[assignment]
                    overlaid += 1
                ai_res = str(ai_row.get("resonance") or "").strip().lower()
                if ai_res in _VALID_RESONANCE and ai_res != row.resonance:
                    row.resonance = ai_res  # type: ignore[assignment]
                    overlaid += 1
                ai_sig = str(ai_row.get("signal_cn") or ai_row.get("signal") or "").strip()
                if ai_sig:
                    row.signal = ai_sig[:80]
                    overlaid += 1

            # P1.8b-② · AI 多出来的行 → 追加（标注 §AI，AI 自定 direction/resonance）
            # 限制每节最多 5 条额外行（之前 3），防止爆版
            for _, extra in list(ai_row_map.items())[:5]:
                ai_dir = _norm_direction(extra.get("direction")) or "neutral"
                ai_res = str(extra.get("resonance") or "medium").strip().lower()
                if ai_res not in _VALID_RESONANCE:
                    ai_res = "medium"
                try:
                    section.rows.append(AIFactorRow(
                        dimension=str(extra.get("dimension") or "AI 推断")[:40],
                        value_display=str(extra.get("value_display") or "⚡AI")[:24],
                        signal=str(extra.get("signal_cn") or extra.get("signal") or "")[:80],
                        direction=ai_dir,  # type: ignore[arg-type]
                        resonance=ai_res,  # type: ignore[arg-type]
                        data_source_ref="§AI",
                    ))
                    overlaid += 1
                except Exception:
                    pass

    return matrix, overlaid


def _normalize_dim_key(name: Any) -> str:
    """维度名归一化（去空格/大小写/中英文括号），供 AI 行和规则行匹配"""
    s = str(name or "").strip().lower()
    for ch in (" ", "\t", "　", "(", ")", "（", "）", "·", "/"):
        s = s.replace(ch, "")
    return s


def _norm_direction(v: Any) -> Optional[str]:
    s = str(v or "").strip().lower()
    if s in _VALID_DIRECTIONS:
        return s
    if s in {"long", "多", "做多", "偏多", "看多"}:
        return "bullish"
    if s in {"short", "空", "做空", "偏空", "看空"}:
        return "bearish"
    if s in {"中性", "震荡", "观望", "wait"}:
        return "neutral"
    return None


def _norm_section_bias(v: Any) -> Optional[str]:
    d = _norm_direction(v)
    if d in _VALID_SECTION_BIAS:
        return d
    return None


def _detect_internal_conflict(
    analysis: AIAnalysisResult, ai_json: Optional[dict]
) -> bool:
    """P1.8b-④ · 严重内部矛盾检测

    当且仅当：AI JSON.bias 与 markdown signal_summary.direction 严重相反
      - json.bias ∈ {bullish} 且 md.direction ∈ {short, bearish, 看空, 做空}
      - json.bias ∈ {bearish} 且 md.direction ∈ {long, bullish, 看多, 做多}
    neutral / 观望 / 空值都不算冲突（不做误报）
    """
    if not ai_json or not isinstance(ai_json, dict):
        return False
    json_bias = _norm_direction(ai_json.get("bias"))
    if json_bias not in {"bullish", "bearish"}:
        return False
    md = analysis.signal_summary
    if md is None:
        return False
    md_dir = (md.direction or "").strip().lower()
    md_norm = _norm_direction(md_dir)
    if md_norm not in {"bullish", "bearish"}:
        return False
    return json_bias != md_norm


def _resolve_bias(analysis: AIAnalysisResult, ai_json: Optional[dict]) -> Direction:
    """AI JSON 的 bias 优先于 markdown 推断（但需通过合法性校验）"""
    if ai_json:
        d = _norm_direction(ai_json.get("bias"))
        if d in _VALID_DIRECTIONS:
            return d  # type: ignore[return-value]
    return _infer_bias(analysis)


def _resolve_conviction(
    analysis: AIAnalysisResult, bias: Direction, ai_json: Optional[dict]
) -> int:
    """AI JSON 的 conviction 优先，失败回退规则推断"""
    if ai_json:
        raw = ai_json.get("conviction")
        if isinstance(raw, (int, float)):
            c = int(raw)
            if 0 <= c <= 100:
                return c
    return _infer_conviction(analysis, bias)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 子组件
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _build_key_level_interpretation(
    analysis: AIAnalysisResult, snapshot: AISnapshot
) -> AIKeyLevelInterpretation:
    supports = [lvl for lvl in (analysis.key_levels or []) if str(lvl.get("type", "")).lower() == "support"]
    resists = [lvl for lvl in (analysis.key_levels or []) if str(lvl.get("type", "")).lower() == "resistance"]

    primary_s: Optional[float] = None
    s_reason = ""
    if supports:
        top_s = supports[0]
        try:
            primary_s = float(top_s.get("price") or 0.0) or None
        except Exception:
            primary_s = None
        s_reason = str(top_s.get("reason") or top_s.get("description") or "").strip()[:120]

    primary_r: Optional[float] = None
    r_reason = ""
    if resists:
        top_r = resists[0]
        try:
            primary_r = float(top_r.get("price") or 0.0) or None
        except Exception:
            primary_r = None
        r_reason = str(top_r.get("reason") or top_r.get("description") or "").strip()[:120]

    trap = ""
    for kv in analysis.risk_warnings or []:
        if any(k in kv for k in ("诱", "trap", "sweep", "假突破")):
            trap = kv[:120]
            break

    extras: list[dict] = []
    for lvl in (analysis.key_levels or [])[3:8]:
        extras.append({
            "price": lvl.get("price"),
            "side": lvl.get("type"),
            "reason": (lvl.get("reason") or lvl.get("description") or "")[:80],
        })

    return AIKeyLevelInterpretation(
        primary_support_price=primary_s,
        primary_support_reason=s_reason,
        primary_resistance_price=primary_r,
        primary_resistance_reason=r_reason,
        trap_warning=trap,
        extra_levels=extras,
    )


def _build_trading_plans_v2(
    analysis: AIAnalysisResult,
    snapshot: AISnapshot,
    math_plan: Optional[ExecutionPlan],
    ai_json: Optional[dict],
) -> tuple[list[AITradingPlan], str]:
    """P1.8b-① · 交易计划构造

    优先级：
      1. AI JSON 附录的 `trading_plans` 数组（权威源）
      2. markdown 解析出的 `trading_plan_entries`
      3. sniper_plans 回退
      4. 观望占位

    返回 (plans, source)：source ∈ {ai_json, markdown, sniper_fallback, wait_placeholder}
    """
    # ① AI JSON 直出
    if ai_json:
        ai_plans_raw = ai_json.get("trading_plans")
        if isinstance(ai_plans_raw, list) and ai_plans_raw:
            parsed = _parse_ai_json_plans(ai_plans_raw, math_plan)
            if parsed:
                return parsed, "ai_json"

    # ② markdown entries / sniper / 观望（复用旧逻辑）
    plans = _build_trading_plans(analysis, snapshot, math_plan)
    if not plans:
        return plans, "wait_placeholder"

    # 判定实际来源
    entries = list(analysis.trading_plan_entries or [])
    if entries:
        return plans, "markdown"
    if analysis.sniper_plans:
        return plans, "sniper_fallback"
    return plans, "wait_placeholder"


def _parse_ai_json_plans(
    raw_plans: list, math_plan: Optional[ExecutionPlan]
) -> list[AITradingPlan]:
    """把 AI JSON 的 trading_plans 数组转成 AITradingPlan 列表。

    校验原则（九原则 §8）：
      - direction 必须是 long / short / wait
      - wait 允许所有价格字段为 None
      - long/short 必须至少有 entry_low 且止损与方向一致（做空 SL>entry / 做多 SL<entry）
      - 价格字段类型不合法 → 该 plan 丢弃（不污染下游）
      - 保留 AI 提供的 conviction / tier_hint / position_pct（不再用规则硬编码）
    """
    out: list[AITradingPlan] = []
    for i, raw in enumerate(raw_plans[:3]):
        if not isinstance(raw, dict):
            continue
        action = _norm_action(raw.get("direction") or raw.get("action"))
        if action is None:
            continue

        entry_low = _safe_float(raw.get("entry_low") or raw.get("entry"))
        entry_high = _safe_float(raw.get("entry_high")) or entry_low
        stop_loss = _safe_float(raw.get("stop_loss") or raw.get("sl"))
        tp1 = _safe_float(raw.get("tp1"))
        tp2 = _safe_float(raw.get("tp2"))
        rr = _safe_float(raw.get("rr_ratio") or raw.get("rr"))

        # 止损方向合法性（做空 SL>entry，做多 SL<entry）
        if action == "long" and entry_low and stop_loss and stop_loss >= entry_low:
            continue
        if action == "short" and entry_low and stop_loss and stop_loss <= entry_low:
            continue
        # wait 要求价格字段都为空或可选
        if action != "wait" and entry_low is None:
            continue

        conviction_raw = raw.get("conviction")
        conviction = (
            int(conviction_raw) if isinstance(conviction_raw, (int, float))
            and 0 <= int(conviction_raw) <= 100 else (60 if i == 0 else 45)
        )
        tier_raw = str(raw.get("tier_hint") or "").strip().upper()
        tier: SignalTier = tier_raw if tier_raw in {"S", "A", "B", "C"} else "B"  # type: ignore[assignment]

        pos_raw = raw.get("position_suggestion_pct")
        position_pct = (
            float(pos_raw) if isinstance(pos_raw, (int, float))
            and 0 <= float(pos_raw) <= 100 else (30.0 if i == 0 else 20.0 if i == 1 else 10.0)
        )

        plan = AITradingPlan(
            priority=int(raw.get("priority") or (i + 1)),
            direction=action,
            entry_zone_low=entry_low,
            entry_zone_high=entry_high,
            trigger_condition=str(raw.get("trigger_condition") or "")[:120],
            stop_loss=stop_loss,
            tp1=tp1,
            tp2=tp2,
            rr_ratio=rr,
            position_suggestion_pct=position_pct,
            conviction=conviction,
            tier_hint=tier,
            invalidation=str(raw.get("invalidation") or "")[:120],
            reason=str(raw.get("reason") or "")[:160],
            aligned_with_math_engine=_plan_aligned_with_math(action, math_plan),
        )
        plan.alignment_note = _alignment_note(plan, math_plan)
        out.append(plan)

    # 按 priority 排序（稳定）
    out.sort(key=lambda p: p.priority)
    return out


def _norm_action(v: Any) -> Optional[TradingAction]:
    s = str(v or "").strip().lower()
    if s in {"long", "做多", "多", "bullish", "buy"}:
        return "long"
    if s in {"short", "做空", "空", "bearish", "sell"}:
        return "short"
    if s in {"wait", "观望", "待定", "neutral", "hold"}:
        return "wait"
    if s in {"avoid", "回避"}:
        return "avoid"
    return None


def _build_trading_plans(
    analysis: AIAnalysisResult,
    snapshot: AISnapshot,
    math_plan: Optional[ExecutionPlan],
) -> list[AITradingPlan]:
    plans: list[AITradingPlan] = []

    # 优先从 trading_plan_entries 提取（结构化）
    entries = list(analysis.trading_plan_entries or [])
    for i, e in enumerate(entries[:3]):
        dir_str = (e.direction or "").strip().lower()
        action: TradingAction = "long" if dir_str == "long" else ("short" if dir_str == "short" else "wait")
        plan = AITradingPlan(
            priority=i + 1,
            direction=action,
            entry_zone_low=_safe_float(e.entry),
            entry_zone_high=_safe_float(e.entry),
            trigger_condition=(e.logic or "")[:120],
            stop_loss=_safe_float(e.stop_loss),
            tp1=_safe_float(e.tp1),
            tp2=_safe_float(e.tp2),
            rr_ratio=_safe_float(e.rr),
            position_suggestion_pct=_infer_position_pct(e, i),
            conviction=_infer_plan_conviction(e, math_plan),
            tier_hint=_infer_tier(e, math_plan),
            reason=(e.logic or "")[:160],
            aligned_with_math_engine=_plan_aligned_with_math(action, math_plan),
        )
        plan.alignment_note = _alignment_note(plan, math_plan)
        plans.append(plan)

    # 若没有结构化 entries，尝试从 sniper_plans 回退
    if not plans and analysis.sniper_plans:
        for i, sp in enumerate(analysis.sniper_plans[:2]):
            action = "long" if (sp.direction or "").lower() == "long" else ("short" if (sp.direction or "").lower() == "short" else "wait")
            plan = AITradingPlan(
                priority=i + 1,
                direction=action,
                entry_zone_low=_safe_float(sp.entry),
                entry_zone_high=_safe_float(sp.entry),
                trigger_condition=(sp.logic or "")[:120],
                stop_loss=_safe_float(sp.stop_loss),
                tp1=_safe_float(sp.tp1),
                tp2=_safe_float(sp.tp2),
                rr_ratio=_safe_float(sp.rr),
                position_suggestion_pct=30.0 if i == 0 else 20.0,
                conviction=60 if i == 0 else 50,
                tier_hint="B",
                invalidation=sp.invalidation[:120] if sp.invalidation else "",
                reason=(sp.logic or "")[:160],
                aligned_with_math_engine=_plan_aligned_with_math(action, math_plan),
            )
            plan.alignment_note = _alignment_note(plan, math_plan)
            plans.append(plan)

    # 仍为空 → 构造观望占位计划
    if not plans:
        plans.append(AITradingPlan(
            priority=1, direction="wait",
            position_suggestion_pct=0.0,
            conviction=_infer_conviction(analysis, _infer_bias(analysis)),
            tier_hint="C",
            reason="无明确入场机会 · 继续观察",
        ))
    return plans


def _build_narrative_impact(snapshot: AISnapshot) -> list[AINarrativeImpact]:
    out: list[AINarrativeImpact] = []
    for t in (snapshot.active_narratives or [])[:5]:
        tid = str(t.get("theme_id") or "?")
        if not tid or tid == "?":
            continue
        intensity = int(t.get("current_intensity") or 0)
        ff = int(t.get("flip_flop_count_24h") or 0)
        bias_v = str(t.get("current_direction_bias") or "neutral")
        if ff >= 2:
            view = f"该主题 24h 内反复{ff}次 · 市场钝化，权重降档"
            weight = "low"
        elif intensity >= 4:
            view = f"{bias_v} · 强度 {intensity}/5，保持关注"
            weight = "high"
        elif intensity >= 2:
            view = f"{bias_v} · 中等强度 {intensity}/5"
            weight = "medium"
        else:
            view = f"强度弱 {intensity}/5"
            weight = "low"
        out.append(AINarrativeImpact(
            theme_id=tid,
            theme_name_cn=str(t.get("theme_name_cn") or ""),
            ai_view_cn=view,
            weight_on_current_plan=weight,  # type: ignore[arg-type]
        ))
    return out


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 对齐判定 / bias / 摘要
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _judge_agreement(
    ai_bias: Direction, ai_conviction: int, math_plan: Optional[ExecutionPlan]
) -> tuple[str, str]:
    """返回 (agreement, note)。agreement ∈ {agree, caution, disagree}"""
    if math_plan is None:
        return "caution", "数学引擎未就绪"
    math_dir = math_plan.direction
    math_score = float(math_plan.execution_score or 50.0)
    if _directions_conflict(math_dir, ai_bias):
        return "disagree", f"数学 {math_dir}·{math_score:.0f} / AI {ai_bias}·{ai_conviction} · 方向相反"
    if ai_bias == "neutral" or math_dir == "neutral":
        return "caution", f"一方观望（math {math_dir}·{math_score:.0f} / ai {ai_bias}·{ai_conviction}）"
    # 同向 · 分数差异
    if abs(math_score - ai_conviction) >= 25:
        return "caution", f"同向但信心差距大（{math_score:.0f} vs {ai_conviction}）"
    return "agree", f"两引擎同向 {ai_bias} · 分数接近（{math_score:.0f} vs {ai_conviction}）"


def _summarize_news(snapshot: AISnapshot) -> str:
    n = len(snapshot.active_narratives or [])
    ff = sum(1 for t in (snapshot.active_narratives or []) if int(t.get("flip_flop_count_24h") or 0) >= 2)
    if n == 0:
        return "24h 无活跃叙事 · 技术面主导"
    if ff:
        return f"活跃叙事 {n} 条 · 其中 {ff} 条反复拉扯（权重降档）"
    return f"活跃叙事 {n} 条 · 动能延续，参与市场方向形成"


def _summarize_geo(snapshot: AISnapshot) -> str:
    geo = snapshot.geo_overview or {}
    lvl = int(geo.get("overall_level", 0) or 0)
    label = str(geo.get("overall_label", "PEACE"))
    blackswan = bool(geo.get("has_blackswan_24h", False))
    if blackswan:
        return f"⚠ 24h 内出现地缘黑天鹅事件 · {label}/{lvl} · 禁开新仓"
    if lvl >= 4:
        return f"地缘高危 {label} · level {lvl}/5 · SafetyGate 协同"
    if lvl >= 2:
        return f"地缘预警 {label} · level {lvl}/5 · 降仓观察"
    return f"地缘环境 {label} · level {lvl}/5 · 无压制"


def _infer_bias(analysis: AIAnalysisResult) -> Direction:
    ss = analysis.signal_summary
    if ss is not None and ss.direction:
        d = ss.direction.strip().lower()
        if d in {"bullish", "bearish", "neutral", "potential_reversal"}:
            return d  # type: ignore[return-value]
        if "多" in d or "long" in d:
            return "bullish"
        if "空" in d or "short" in d:
            return "bearish"
    # 从交易计划方向推断
    plans = analysis.trading_plan_entries or []
    longs = sum(1 for e in plans if (e.direction or "").lower() == "long")
    shorts = sum(1 for e in plans if (e.direction or "").lower() == "short")
    if longs > shorts and longs > 0:
        return "bullish"
    if shorts > longs and shorts > 0:
        return "bearish"
    return "neutral"


def _infer_conviction(analysis: AIAnalysisResult, bias: Direction) -> int:
    if bias == "neutral":
        return 40
    ss = analysis.signal_summary
    if ss is not None:
        c = (ss.confidence or "").strip().lower()
        if c == "high":
            return 80
        if c == "medium":
            return 60
        if c == "low":
            return 40
    return 55


def _majority_direction(rows: list[AIFactorRow]) -> Direction:
    bull = sum(1 for r in rows if r.direction == "bullish")
    bear = sum(1 for r in rows if r.direction == "bearish")
    if bull > bear and bull > 0:
        return "bullish"
    if bear > bull and bear > 0:
        return "bearish"
    return "neutral"


def _aggregate_matrix_bias(sections: list[AIFactorSection]) -> tuple[Direction, Confidence]:
    bull = sum(1 for s in sections if s.section_bias == "bullish")
    bear = sum(1 for s in sections if s.section_bias == "bearish")
    total = max(1, len(sections))
    overall: Direction
    if bull > bear and bull >= 3:
        overall = "bullish"
    elif bear > bull and bear >= 3:
        overall = "bearish"
    else:
        overall = "neutral"
    dominant = max(bull, bear)
    ratio = dominant / total
    if ratio >= 0.6:
        conf: Confidence = "high"
    elif ratio >= 0.35:
        conf = "medium"
    else:
        conf = "low"
    return overall, conf


def _matrix_summary_line(bias: Direction, conf: Confidence) -> str:
    bias_cn = {"bullish": "看多", "bearish": "看空", "neutral": "中性", "potential_reversal": "潜在反转"}[bias]
    conf_cn = {"high": "强", "medium": "中", "low": "弱"}[conf]
    return f"{bias_cn}（{conf_cn}）"


def _section_summary(name: str, bias: Direction, n_rows: int) -> str:
    bias_cn = {"bullish": "偏多", "bearish": "偏空", "neutral": "中性", "potential_reversal": "反转迹象"}[bias]
    return f"{name}板块{bias_cn} · 覆盖 {n_rows} 项"


def _macro_signal_text(name: str, chg: Any) -> str:
    if not isinstance(chg, (int, float)):
        return "-"
    if abs(chg) < 0.1:
        return "持平"
    return f"{'+' if chg > 0 else ''}{chg:.2f}%"


def _directions_agree(math_dir: str, ai_bias: str) -> bool:
    """math_dir 来自 ExecutionPlan.direction（bullish/bearish/neutral/potential_reversal）
    或老口径 long/short。两种都兼容。"""
    if math_dir in {"bullish", "long"} and ai_bias == "bullish":
        return True
    if math_dir in {"bearish", "short"} and ai_bias == "bearish":
        return True
    return False


def _directions_conflict(math_dir: str, ai_bias: str) -> bool:
    if math_dir in {"bullish", "long"} and ai_bias == "bearish":
        return True
    if math_dir in {"bearish", "short"} and ai_bias == "bullish":
        return True
    return False


def _direction_from_plan(action: str) -> Direction:
    if action == "long":
        return "bullish"
    if action == "short":
        return "bearish"
    return "neutral"


def _plan_aligned_with_math(action: TradingAction, math_plan: Optional[ExecutionPlan]) -> bool:
    if math_plan is None:
        return True
    return action == math_plan.action or action == "wait"


def _alignment_note(plan: AITradingPlan, math_plan: Optional[ExecutionPlan]) -> str:
    if math_plan is None:
        return "数学引擎未就绪"
    if plan.direction != math_plan.action and plan.direction != "wait":
        return f"与数学引擎 {math_plan.action} 方向不一致 · 需谨慎"
    if plan.entry_zone_low and math_plan.entry_zone_low:
        diff = abs(plan.entry_zone_low - math_plan.entry_zone_low) / max(1.0, math_plan.entry_zone_low) * 100
        return f"同向 · 入场价差 {diff:.2f}%"
    return "方向一致"


def _infer_position_pct(entry, idx: int) -> float:
    # 主/备递减
    if idx == 0:
        return 40.0
    if idx == 1:
        return 25.0
    return 15.0


def _infer_plan_conviction(entry, math_plan: Optional[ExecutionPlan]) -> int:
    if math_plan is None:
        return 55
    return int(min(95.0, max(10.0, math_plan.execution_score * 0.9)))


def _infer_tier(entry, math_plan: Optional[ExecutionPlan]) -> SignalTier:
    if math_plan is not None:
        return math_plan.tier_hint
    return "C"


def _safe_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        f = float(v)
        return f if f != 0 else None
    except (TypeError, ValueError):
        return None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Decision Tracker
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _mark_d14(
    report: AITraderReport,
    math_plan: Optional[ExecutionPlan],
    *,
    matrix_source: str = "rule_fallback",
    plans_source: str = "markdown",
) -> None:
    try:
        from utils.decision_tracker import D, get_tracker
        sections = report.factor_matrix.sections if report.factor_matrix else []
        matrix_rows = sum(len(s.rows) for s in sections)
        tracker = get_tracker()
        tracker.mark(
            D.D14_AI_TRADER,
            status="ok" if report.bias != "neutral" or report.trading_plans else "warn",
            log=False,
            coin=report.coin,
            bias=report.bias,
            conviction=report.conviction,
            plans=len(report.trading_plans),
            matrix_sections=len(sections),
            matrix_rows=matrix_rows,
            matrix_source=matrix_source,
            plans_source=plans_source,
            agreement=report.agreement_with_math_engine,
            has_math_plan=math_plan is not None,
            latency_ms=report.latency_ms,
        )

        # D17 · 7 板块 AIFactorMatrix 独立上报
        # 注意：规则侧 build_factor_matrix 永远生成完整 A-G 7 板块（独立于 AI）
        # AI 的 sections[] 现为 optional，仅用于可选覆写（P1.3 降级策略 β）
        # 因此这里的完整性校验对象是"规则侧 matrix + AI overlay 后"的最终产物
        expected = {"A", "B", "C", "D", "E", "F", "G"}
        present = {s.section_id for s in sections if getattr(s, "section_id", None)}
        missing = sorted(expected - present)
        empty_sections = sum(1 for s in sections if not s.rows)
        # 成功标准：7 板块齐全 · 每板块 ≥1 行
        if not missing and empty_sections == 0 and len(sections) >= 7:
            d17_status = "ok"
        elif missing or len(sections) < 7:
            d17_status = "warn"
        else:
            d17_status = "warn"
        tracker.mark(
            D.D17_FACTOR_MATRIX,
            status=d17_status,
            log=False,
            coin=report.coin,
            sections=len(sections),
            total_rows=matrix_rows,
            missing_sections=",".join(missing) if missing else "",
            empty_sections=empty_sections,
            source=matrix_source,
        )
    except Exception:
        logger.debug("[D14/D17] mark failed", exc_info=True)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# P1.8a · AI Quality Ledger 对接
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _record_quality(
    *,
    report: AITraderReport,
    analysis: AIAnalysisResult,
    ai_json: Optional[dict],
    matrix_source: str,
    plans_source: str,
    overlay_fields: int,
) -> None:
    """每次 build_ai_trader_report 完成后落一条质量记录到 Ledger"""
    try:
        from processors.ai_quality_ledger import (
            AIQualityRecord, get_ai_quality_ledger,
        )

        # json_valid 判定
        # P1.3 · sections[] 降级策略 β：AI 可选填，缺失/空数组不再算 invalid
        # - 有核心字段（bias / trading_plans / matrix_summary_cn 任一）即认为 JSON 有效
        # - sections 若存在但非 list → malformed（真正的 schema 错误）
        # - 完全缺失或空数组 → 合法降级（规则侧规则引擎 build_factor_matrix 已兜底生成 7 板块）
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

        # bias_vs_text：AI JSON bias vs markdown signal_summary direction
        bias_vs_text = "unknown"
        if ai_json and analysis.signal_summary:
            json_bias = _norm_direction(ai_json.get("bias")) or ""
            md_dir = (analysis.signal_summary.direction or "").strip().lower()
            md_norm = _norm_direction(md_dir) or ""
            if not json_bias:
                bias_vs_text = "json_missing"
            elif not md_norm:
                bias_vs_text = "text_missing"
            elif json_bias == md_norm:
                bias_vs_text = "consistent"
            else:
                # 中性不算冲突
                if "neutral" in (json_bias, md_norm):
                    bias_vs_text = "consistent"
                else:
                    bias_vs_text = "conflict"
        elif ai_json:
            bias_vs_text = "text_missing"
        elif analysis.signal_summary:
            bias_vs_text = "json_missing"

        # AI plans count（走了 ai_json 路径则取 report.trading_plans 长度）
        ai_plans_count = len(report.trading_plans) if plans_source == "ai_json" else 0

        # AI 追加行数（factor_matrix 中 data_source_ref == §AI 的）
        ai_extra_rows = 0
        if report.factor_matrix:
            for sec in report.factor_matrix.sections:
                ai_extra_rows += sum(
                    1 for r in sec.rows if r.data_source_ref == "§AI"
                )

        rec = AIQualityRecord(
            ts=int(report.ts or time.time()),
            coin=report.coin,
            price_at_analysis=float(report.price_at_analysis or 0.0),
            matrix_source=matrix_source,
            plans_source=plans_source,
            json_valid=json_valid,
            json_invalid_reason=invalid_reason,
            overlay_fields=int(overlay_fields),
            ai_plans_count=ai_plans_count,
            ai_extra_rows=ai_extra_rows,
            bias_vs_text=bias_vs_text,
            final_bias=str(report.bias),
            final_conviction=int(report.conviction),
            math_agreement=str(report.agreement_with_math_engine or "no_math_plan"),
            latency_ms=int(report.latency_ms or 0),
            reasoning_tokens=int(report.thinking_tokens or 0),
            model=str(report.model or ""),
        )
        get_ai_quality_ledger().record(rec)
    except Exception:
        logger.debug("[D14.quality] record failed", exc_info=True)
