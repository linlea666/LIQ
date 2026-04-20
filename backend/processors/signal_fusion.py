"""双引擎融合层 · L7.5 Signal Fusion（真实实现）

职责：
  - 接收数学引擎 ExecutionPlan + AI 引擎 AITraderReport
  - 判断共识等级 (strong / agree / math_lead / ai_lead / conflict / both_wait)
  - 产出 FinalDecision（对外主视图）

共识规则：
  strong    — 两引擎同向 + math.final_score >= 75 且 ai.conviction >= 75
  agree     — 两引擎同向（数学 bias 与 AI bias 一致且均非观望态）
  math_lead — AI 观望 (bias=neutral 或 action=wait) 且数学引擎有明确方向
  ai_lead   — 数学引擎观望 但 AI 有明确方向
  conflict  — 两引擎反向
  both_wait — 两引擎都在观望（P0-1 · 严禁被旧逻辑误判为 agree → execute）

分歧处理：
  - conflict  → recommended_action = reduce_size 或 wait
  - both_wait → recommended_action = wait（强制 0 仓位，不派发任何参数）
  - 历史上此类分歧谁胜率高 → divergence_summary 展示（来自 backtest 闭环）
  - 若 safety_gate 已触发 → final action 强制 avoid

落实日志锚点：
  - D.D15_FUSION：每次 fuse 上报 consensus / final_score / math_score / ai_score
  - 特殊：conflict 额外 status=warn
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from models.ai_trader_report import AITraderReport, AITradingPlan
from models.execution_plan import ExecutionPlan
from models.fused_decision import DivergenceStats, EngineBrief, FinalDecision
from models.geo_risk import GeoRiskOverview

logger = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 公开入口
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def fuse_decisions(
    coin: str,
    math_plan: ExecutionPlan,
    ai_report: AITraderReport,
    *,
    geo_overview: Optional[GeoRiskOverview] = None,
    divergence_stats: Optional[list[DivergenceStats]] = None,
    active_themes_count: int = 0,
) -> FinalDecision:
    """融合两引擎输出。

    返回 FinalDecision（含 consensus + 双引擎 brief + 最终动作）。
    """
    math_brief = _math_brief(math_plan)
    ai_brief = _ai_brief(ai_report)

    consensus = _classify_consensus(math_brief, ai_brief)
    final_score = _compute_final_score(math_brief, ai_brief, consensus)
    stars = _consensus_stars(consensus, abs(math_brief.score - ai_brief.score))

    entry_params = _merge_entry_params(math_plan, ai_report, consensus)
    divergence = _match_divergence_stats(consensus, math_brief, ai_brief, divergence_stats)

    action, pos_pct, final_direction = _derive_action(
        math_brief, ai_brief, consensus, math_plan, ai_report,
    )
    traffic_light = _traffic_light(final_score, consensus, math_plan.safety_gates.triggered)
    headline, one_liner = _compose_headline(final_score, consensus, action, final_direction)

    # 新闻 / 地缘快速展示
    geo_lvl = 0
    geo_label = "PEACE"
    has_blackswan = False
    if geo_overview is not None:
        geo_lvl = int(getattr(geo_overview, "overall_level", 0) or 0)
        geo_label = str(getattr(geo_overview, "overall_label", "PEACE") or "PEACE")
        has_blackswan = bool(getattr(geo_overview, "has_blackswan_24h", False))

    decision = FinalDecision(
        coin=coin,
        ts=int(ai_report.ts or math_plan.ts or time.time()),
        current_price=float(math_plan.current_price or ai_report.price_at_analysis or 0.0),
        final_score=final_score,
        traffic_light=traffic_light,
        headline=headline,
        one_liner=one_liner,
        consensus_level=consensus,  # type: ignore[arg-type]
        consensus_stars=stars,
        consensus_summary_cn=_consensus_summary_cn(consensus, math_brief, ai_brief),
        math_brief=math_brief,
        ai_brief=ai_brief,
        divergence_summary_cn=(_divergence_summary_cn(consensus, math_brief, ai_brief, divergence)
                               if consensus == "conflict" else ""),
        historical_divergence=divergence if consensus == "conflict" else None,
        recommended_action=action,
        recommended_position_pct=pos_pct,
        entry_zone_low=entry_params.get("entry_zone_low"),
        entry_zone_high=entry_params.get("entry_zone_high"),
        stop_loss=entry_params.get("stop_loss"),
        tp1=entry_params.get("tp1"),
        tp2=entry_params.get("tp2"),
        rr_ratio=entry_params.get("rr_ratio"),
        underlying_math_plan_ref=_math_plan_fingerprint(math_plan),
        underlying_ai_plan_priority=(ai_report.trading_plans[0].priority
                                     if ai_report.trading_plans else 1),
        active_themes_count=int(active_themes_count or 0),
        geo_risk_overall_level=geo_lvl,
        geo_risk_label=geo_label,
        has_blackswan_warning=has_blackswan,
        safety_gate_triggered=bool(math_plan.safety_gates.triggered),
        safety_gate_reason=str(math_plan.safety_gates.block_reason or ""),
        expires_at=math_plan.expires_at,
    )

    _mark_d15(decision, math_brief, ai_brief, consensus)
    return decision


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 引擎简报
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _math_brief(plan: ExecutionPlan) -> EngineBrief:
    summary_parts = []
    if plan.tier_hint:
        summary_parts.append(f"{plan.tier_hint}级")
    if plan.action != "wait":
        summary_parts.append(f"{plan.action}")
    if plan.entry_zone_low:
        summary_parts.append(f"@{plan.entry_zone_low:.0f}")
    if plan.position_size_pct:
        summary_parts.append(f"仓{plan.position_size_pct:.0f}%")
    summary = "·".join(summary_parts) or "math·wait"

    return EngineBrief(
        engine_name="math",
        score=float(plan.execution_score or 50.0),
        bias=_map_direction_to_bias(plan.direction),
        action=plan.action,
        entry_hint=plan.entry_zone_low,
        stop_loss_hint=plan.stop_loss,
        tp1_hint=plan.tp1,
        rr_ratio=plan.rr_ratio,
        position_pct=float(plan.position_size_pct or 0.0),
        summary_cn=summary,
    )


def _ai_brief(report: AITraderReport) -> EngineBrief:
    primary: Optional[AITradingPlan] = None
    if report.trading_plans:
        primary = next((p for p in report.trading_plans if p.priority == 1),
                       report.trading_plans[0])

    if primary is None:
        return EngineBrief(
            engine_name="ai",
            score=float(report.conviction or 50),
            bias=report.bias,
            action="wait",
            position_pct=0.0,
            summary_cn=f"AI {report.bias}·conviction{report.conviction}",
        )

    parts = [f"{primary.tier_hint}级", primary.direction]
    if primary.entry_zone_low:
        parts.append(f"@{primary.entry_zone_low:.0f}")
    if primary.position_suggestion_pct:
        parts.append(f"仓{primary.position_suggestion_pct:.0f}%")
    summary = "·".join(parts)

    return EngineBrief(
        engine_name="ai",
        score=float(report.conviction or primary.conviction or 50),
        bias=report.bias,
        action=primary.direction,
        entry_hint=primary.entry_zone_low,
        stop_loss_hint=primary.stop_loss,
        tp1_hint=primary.tp1,
        rr_ratio=primary.rr_ratio,
        position_pct=float(primary.position_suggestion_pct or 0.0),
        summary_cn=summary,
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 共识判定
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _classify_consensus(math_brief: EngineBrief, ai_brief: EngineBrief) -> str:
    """P0-1 · 共识判定修正。

    旧逻辑只看 bias，导致 (math.bias=neutral,action=wait) + (ai.bias=neutral,action=wait)
    被归入 "agree" → 下游 _derive_action 按 agree 分支强制 execute 开仓。
    修正：任一方 bias=neutral 或 action=wait 都视为 "观望"：
      - 两方观望 → both_wait（新引入，下游强制 wait）
      - 一方观望 + 另一方有方向 → *_lead（按有方向的一侧定向，下游 reduce_size）
      - 两方都有明确方向且反向 → conflict
      - 两方都有明确方向且同向 → agree / strong
    """
    m_bias, a_bias = math_brief.bias, ai_brief.bias
    m_act, a_act = math_brief.action, ai_brief.action

    if _is_conflict(m_bias, a_bias):
        return "conflict"

    m_waiting = (m_bias == "neutral") or (m_act == "wait")
    a_waiting = (a_bias == "neutral") or (a_act == "wait")

    if m_waiting and a_waiting:
        return "both_wait"
    if m_waiting:
        return "ai_lead"
    if a_waiting:
        return "math_lead"

    # 两方都有明确方向且同向
    if math_brief.score >= 75.0 and ai_brief.score >= 75.0:
        return "strong"
    return "agree"


def _consensus_stars(consensus: str, score_delta: float) -> int:
    if consensus == "strong":
        return 5 if score_delta <= 10 else 4
    if consensus == "agree":
        return 4 if score_delta <= 15 else 3
    if consensus in {"math_lead", "ai_lead"}:
        return 3
    if consensus == "conflict":
        return 1
    if consensus == "both_wait":
        return 2  # 非交易态，但也不是分歧，给 2 星
    return 2


def _compute_final_score(
    math_brief: EngineBrief,
    ai_brief: EngineBrief,
    consensus: str,
    math_weight: float = 0.55,
) -> float:
    m = float(math_brief.score)
    a = float(ai_brief.score)
    if consensus == "strong":
        base = max(m, a)
    elif consensus == "agree":
        base = m * math_weight + a * (1 - math_weight)
    elif consensus == "math_lead":
        base = m * 0.85
    elif consensus == "ai_lead":
        base = a * 0.85
    elif consensus == "conflict":
        base = min(m, a) - 10.0
    elif consensus == "both_wait":
        # 两方观望：用较小值，避免虚高诱导用户
        base = min(m, a)
    else:
        base = (m + a) / 2.0
    return max(0.0, min(100.0, base))


def _merge_entry_params(
    math_plan: ExecutionPlan,
    ai_report: AITraderReport,
    consensus: str,
) -> dict:
    """合并入场/止损/止盈参数。

    - agree / strong：严格交集（更保守的入场 + 更严止损）
    - math_lead：取数学引擎
    - ai_lead：取 AI 主计划
    - conflict：全部 None
    """
    empty = {"entry_zone_low": None, "entry_zone_high": None,
             "stop_loss": None, "tp1": None, "tp2": None, "rr_ratio": None}

    if consensus in {"conflict", "both_wait"}:
        return empty

    ai_primary: Optional[AITradingPlan] = None
    if ai_report.trading_plans:
        ai_primary = next((p for p in ai_report.trading_plans if p.priority == 1),
                          ai_report.trading_plans[0])

    if consensus == "math_lead":
        return {
            "entry_zone_low": math_plan.entry_zone_low,
            "entry_zone_high": math_plan.entry_zone_high,
            "stop_loss": math_plan.stop_loss,
            "tp1": math_plan.tp1,
            "tp2": math_plan.tp2,
            "rr_ratio": math_plan.rr_ratio,
        }

    if consensus == "ai_lead":
        if ai_primary is None:
            return empty
        return {
            "entry_zone_low": ai_primary.entry_zone_low,
            "entry_zone_high": ai_primary.entry_zone_high,
            "stop_loss": ai_primary.stop_loss,
            "tp1": ai_primary.tp1,
            "tp2": ai_primary.tp2,
            "rr_ratio": ai_primary.rr_ratio,
        }

    # strong / agree · 两方同向 · 取交集（最保守）
    if ai_primary is None:
        return {
            "entry_zone_low": math_plan.entry_zone_low,
            "entry_zone_high": math_plan.entry_zone_high,
            "stop_loss": math_plan.stop_loss,
            "tp1": math_plan.tp1,
            "tp2": math_plan.tp2,
            "rr_ratio": math_plan.rr_ratio,
        }

    direction = math_plan.direction  # long / short / neutral
    m_low, m_high = math_plan.entry_zone_low, math_plan.entry_zone_high
    a_low, a_high = ai_primary.entry_zone_low, ai_primary.entry_zone_high
    entry_low, entry_high = _merge_entry_zone(m_low, m_high, a_low, a_high, direction)

    # 止损取"更严"：long 取 max（更接近价格），short 取 min
    stop_loss = _pick_stricter_stop(math_plan.stop_loss, ai_primary.stop_loss, direction)
    # TP 取"更保守"（更接近价格）
    tp1 = _pick_closer_tp(math_plan.tp1, ai_primary.tp1, direction)
    tp2 = _pick_closer_tp(math_plan.tp2, ai_primary.tp2, direction)
    rr = _pick_min(math_plan.rr_ratio, ai_primary.rr_ratio)

    return {
        "entry_zone_low": entry_low,
        "entry_zone_high": entry_high,
        "stop_loss": stop_loss,
        "tp1": tp1,
        "tp2": tp2,
        "rr_ratio": rr,
    }


def _match_divergence_stats(
    consensus: str,
    math_brief: EngineBrief,
    ai_brief: EngineBrief,
    history: Optional[list[DivergenceStats]],
) -> Optional[DivergenceStats]:
    if consensus != "conflict" or not history:
        return None
    key = _divergence_type(math_brief, ai_brief)
    # 精确匹配优先
    for d in history:
        if d.divergence_type == key:
            return d
    # 粗匹配：长/短方向一致
    for d in history:
        if (math_brief.action in d.divergence_type and
                ai_brief.action in d.divergence_type):
            return d
    return None


def _compose_headline(
    final_score: float, consensus: str, action: str, direction: str,
) -> tuple[str, str]:
    dir_cn = {"bullish": "做多", "bearish": "做空", "neutral": "观望",
              "long": "做多", "short": "做空", "wait": "观望",
              "avoid": "回避", "potential_reversal": "潜在反转"}.get(direction, direction)

    if consensus == "strong":
        headline = f"🟢 双引擎强共识{dir_cn} · 分数 {final_score:.0f}"
        one = f"两引擎同向 + 分数均 ≥75 · 高确定性"
    elif consensus == "agree":
        headline = f"🟢 两引擎一致{dir_cn} · 分数 {final_score:.0f}"
        one = f"方向一致，建议按融合参数执行"
    elif consensus == "math_lead":
        headline = f"🟡 数学引擎主导{dir_cn} · AI 观望"
        one = f"AI 信心不足，减半仓位执行"
    elif consensus == "ai_lead":
        headline = f"🟡 AI 主导{dir_cn} · 数学引擎观望"
        one = f"数学信号弱，按 AI 方案小仓位参与"
    elif consensus == "conflict":
        headline = f"🟠 双引擎分歧 · 建议{'减仓' if action == 'reduce_size' else '等待'}"
        one = f"方向相反 · {'降仓对冲' if action == 'reduce_size' else '等待明朗'}"
    elif consensus == "both_wait":
        headline = f"⚪ 双引擎均观望 · 分数 {final_score:.0f}"
        one = "两引擎都无明确方向 · 不建议开仓"
    else:
        headline = f"⚪ 观望 · 分数 {final_score:.0f}"
        one = "信号不足"
    return headline, one


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 动作推导
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _derive_action(
    math_brief: EngineBrief,
    ai_brief: EngineBrief,
    consensus: str,
    math_plan: ExecutionPlan,
    ai_report: AITraderReport,
) -> tuple[str, float, str]:
    """返回 (recommended_action, position_pct, final_direction)"""
    # SafetyGate 强制覆盖
    if math_plan.safety_gates.triggered and any(
        status == "block" for status in [
            math_plan.safety_gates.g1_extreme_vol,
            math_plan.safety_gates.g2_macro_event,
            math_plan.safety_gates.g3_liq_chaos,
            math_plan.safety_gates.g4_api_degrade,
            math_plan.safety_gates.g5_blackswan,
        ]
    ):
        return "avoid", 0.0, "neutral"

    ai_primary_pos = 0.0
    if ai_report.trading_plans:
        primary = next((p for p in ai_report.trading_plans if p.priority == 1),
                       ai_report.trading_plans[0])
        ai_primary_pos = float(primary.position_suggestion_pct or 0.0)
    math_pos = float(math_plan.position_size_pct or 0.0)

    if consensus == "strong":
        action = "execute"
        pos = min(math_pos if math_pos > 0 else 30.0,
                  ai_primary_pos if ai_primary_pos > 0 else 30.0) or 35.0
        direction = math_brief.bias
    elif consensus == "agree":
        action = "execute"
        # 平均 × 0.8
        avg = ((math_pos or 25.0) + (ai_primary_pos or 25.0)) / 2.0
        pos = max(10.0, avg * 0.8)
        direction = math_brief.bias
    elif consensus == "math_lead":
        action = "reduce_size"
        pos = max(5.0, (math_pos or 20.0) * 0.5)
        direction = math_brief.bias
    elif consensus == "ai_lead":
        action = "reduce_size"
        pos = max(5.0, (ai_primary_pos or 20.0) * 0.5)
        direction = ai_brief.bias
    elif consensus == "conflict":
        # 分歧：分数差距大 → 跟强方减仓；否则 wait
        delta = abs(math_brief.score - ai_brief.score)
        if delta >= 25:
            action = "reduce_size"
            pos = 10.0
            direction = math_brief.bias if math_brief.score > ai_brief.score else ai_brief.bias
        else:
            action = "wait"
            pos = 0.0
            direction = "neutral"
    elif consensus == "both_wait":
        # P0-1 · 两引擎均观望时强制 wait，绝不派发仓位
        action = "wait"
        pos = 0.0
        direction = "neutral"
    else:
        action = "wait"
        pos = 0.0
        direction = "neutral"

    # SafetyGate warn 但未 block → 折半
    if math_plan.safety_gates.triggered and action == "execute":
        pos *= 0.6
        action = "reduce_size"

    return action, min(100.0, max(0.0, pos)), direction


def _traffic_light(final_score: float, consensus: str, safety_triggered: bool) -> str:
    if safety_triggered:
        return "red"
    if consensus == "conflict":
        return "orange"
    if final_score >= 75:
        return "green"
    if final_score >= 55:
        return "yellow"
    if final_score >= 40:
        return "orange"
    return "red"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 内部工具
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _map_direction_to_bias(direction: str) -> str:
    """ExecutionPlan.direction ∈ {bullish,bearish,neutral,potential_reversal}
    保持不变即可（EngineBrief.bias 的 Direction 枚举一致）"""
    if direction in {"bullish", "bearish", "neutral", "potential_reversal"}:
        return direction
    if direction == "long":
        return "bullish"
    if direction == "short":
        return "bearish"
    return "neutral"


def _is_conflict(m: str, a: str) -> bool:
    return (m == "bullish" and a == "bearish") or (m == "bearish" and a == "bullish")


def _merge_entry_zone(
    m_low: Optional[float], m_high: Optional[float],
    a_low: Optional[float], a_high: Optional[float],
    direction: str,
) -> tuple[Optional[float], Optional[float]]:
    """交集：long 取更高的 low（更保守进场），short 取更低的 low。"""
    lows = [v for v in (m_low, a_low) if isinstance(v, (int, float)) and v > 0]
    highs = [v for v in (m_high, a_high) if isinstance(v, (int, float)) and v > 0]
    if not lows or not highs:
        return (m_low or a_low, m_high or a_high)
    if direction == "bullish" or direction == "long":
        low = max(lows)
        high = min(highs)
    else:
        low = min(lows)
        high = max(highs)
    if low > high:
        low, high = high, low
    return low, high


def _pick_stricter_stop(m: Optional[float], a: Optional[float], direction: str) -> Optional[float]:
    vals = [v for v in (m, a) if isinstance(v, (int, float)) and v > 0]
    if not vals:
        return None
    if direction in {"bullish", "long"}:
        return max(vals)  # 更接近价格
    if direction in {"bearish", "short"}:
        return min(vals)
    return vals[0]


def _pick_closer_tp(m: Optional[float], a: Optional[float], direction: str) -> Optional[float]:
    vals = [v for v in (m, a) if isinstance(v, (int, float)) and v > 0]
    if not vals:
        return None
    if direction in {"bullish", "long"}:
        return min(vals)
    if direction in {"bearish", "short"}:
        return max(vals)
    return vals[0]


def _pick_min(m: Optional[float], a: Optional[float]) -> Optional[float]:
    vals = [v for v in (m, a) if isinstance(v, (int, float)) and v > 0]
    return min(vals) if vals else None


def _divergence_type(math_brief: EngineBrief, ai_brief: EngineBrief) -> str:
    return f"math_{math_brief.action}_ai_{ai_brief.action}"


def _math_plan_fingerprint(plan: ExecutionPlan) -> str:
    return f"{plan.tier_hint}·{plan.action}·s{plan.execution_score:.0f}·t{plan.ts}"


def _consensus_summary_cn(
    consensus: str, math_brief: EngineBrief, ai_brief: EngineBrief,
) -> str:
    m_score = math_brief.score
    a_score = ai_brief.score
    if consensus == "strong":
        return f"两引擎强共识 · 方向一致 · 分数均 ≥75（{m_score:.0f}/{a_score:.0f}）"
    if consensus == "agree":
        return f"方向一致 · 分数接近（{m_score:.0f}/{a_score:.0f}）"
    if consensus == "math_lead":
        return f"数学引擎 {math_brief.bias}·{m_score:.0f} / AI 观望 · 建议减半仓位"
    if consensus == "ai_lead":
        return f"AI {ai_brief.bias}·{a_score:.0f} / 数学观望 · 小仓位参与"
    if consensus == "conflict":
        return f"分歧 · 数学 {math_brief.bias}·{m_score:.0f} vs AI {ai_brief.bias}·{a_score:.0f}"
    if consensus == "both_wait":
        return f"两引擎均无方向 · 数学 {m_score:.0f} / AI {a_score:.0f} · 建议观望"
    return f"math {m_score:.0f} / ai {a_score:.0f}"


def _divergence_summary_cn(
    consensus: str,
    math_brief: EngineBrief,
    ai_brief: EngineBrief,
    divergence: Optional[DivergenceStats],
) -> str:
    base = (f"数学{math_brief.bias} vs AI{ai_brief.bias} · "
            f"分数 {math_brief.score:.0f}/{ai_brief.score:.0f}")
    if divergence is None:
        return base + " · 历史样本不足 · 默认降仓或等待"
    winner = "数学引擎" if divergence.math_win_rate >= divergence.ai_win_rate else "AI"
    return f"{base} · 历史样本 n={divergence.sample_size} · {winner}胜率更高"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Decision Tracker
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _mark_d15(
    decision: FinalDecision,
    math_brief: EngineBrief,
    ai_brief: EngineBrief,
    consensus: str,
) -> None:
    try:
        from utils.decision_tracker import D, get_tracker
        status = "warn" if consensus == "conflict" else "ok"
        get_tracker().mark(
            D.D15_FUSION,
            status=status,
            log=False,
            coin=decision.coin,
            consensus=consensus,
            stars=decision.consensus_stars,
            final_score=round(decision.final_score, 2),
            math_score=round(math_brief.score, 2),
            ai_score=round(ai_brief.score, 2),
            math_bias=math_brief.bias,
            ai_bias=ai_brief.bias,
            action=decision.recommended_action,
            position_pct=round(decision.recommended_position_pct, 2),
            safety_triggered=decision.safety_gate_triggered,
        )
    except Exception:
        logger.debug("[D15] mark failed", exc_info=True)
