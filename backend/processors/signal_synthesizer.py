"""数学引擎 · L4 Signal Synthesizer

职责：
  - 接收 CandidateSignal 集合 + Regime + 新闻/地缘上下文
  - 聚合同向信号（corroboration bonus）
  - 按 ExecutionScoreBreakdown 逐项打分
  - 产出 ExecutionPlan（数学引擎对外主输出）

打分公式（全部可解释）：
  final_score = base(50)
              + tier_bonus         (S→+20, A→+15, B→+8, C→0)
              + confidence_bonus   (高置信 +8 / 中 +3 / 低 0)
              + regime_alignment   (Regime.action_weights 映射)
              + corroboration_bonus (同向源数：2→+5, 3→+10, 4+→+15)
              - cascade_penalty    (cascade_risk * 权重)
              + rr_bonus           (rr>=3→+8, rr>=2→+4, rr>=1.5→+2)
              + backtest_bonus     (历史胜率>55%→+X)
              + event_factor       (强利好/利空) — P1 启用
              + geo_risk_factor    (GeoRisk level 2→-5, 3→-10, 4+→-15)
              + safety_gate_delta  (通常负)
  clip 到 0-100

落实日志锚点：
  - D.D02_DUAL_ENGINE：每次 synthesize 成功 status=ok, final_score, action
  - D.D15_FUSION 由 signal_fusion 单独上报

复用决策：
  - 不改 KeyLevelSignal / RangeSignalData
  - confluence_score / cascade_risk 从 CandidateSignal.provenance 里取
  - 安全护栏 delegated 到 safety_gate.evaluate_safety_gates
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from models.candidate_signal import CandidateSignal
from models.execution_plan import (
    ExecutionPlan, ExecutionScoreBreakdown, SafetyGateResult,
)
from models.geo_risk import GeoRiskOverview
from models.news_event import MarketEventSignal
from models.regime import RegimeSnapshot
from models.snapshot import AISnapshot, BacktestStats

logger = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 常量
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_TIER_BONUS = {"S": 20.0, "A": 15.0, "B": 8.0, "C": 0.0}
_TIER_VOTE_WEIGHT = {"S": 4.0, "A": 3.0, "B": 2.0, "C": 1.0}

# source 投票权重（不同来源可靠性不同）
_SOURCE_WEIGHT = {
    "tracker_v2.": 1.2,
    "range_signal.": 0.8,
    "levels.": 1.0,
    "news_event.": 0.8,
    "geo_risk.": 1.0,
}


def _source_weight(source: str) -> float:
    for prefix, w in _SOURCE_WEIGHT.items():
        if source.startswith(prefix):
            return w
    return 1.0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 主入口
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def synthesize(
    coin: str,
    candidates: list[CandidateSignal],
    regime: RegimeSnapshot,
    *,
    current_price: float,
    snapshot: Optional[AISnapshot] = None,
    geo_overview: Optional[GeoRiskOverview] = None,
    active_news: Optional[list[MarketEventSignal]] = None,
    backtest_hint: Optional[BacktestStats] = None,
    safety_gates: Optional[SafetyGateResult] = None,
) -> ExecutionPlan:
    """合成数学引擎的最终 ExecutionPlan。

    参数说明：
      candidates    — 本轮所有候选信号（通常来自 SignalBus.query）
      regime        — L2 产出，决定权重切换
      current_price — 必填（即使 snapshot 为 None 也可正常运行）
      snapshot      — AI 快照（可选；P0 阶段大多数 recompute 不构造）
      geo_overview  — 地缘风险（P1 接入）
      active_news   — 24h 活跃新闻（P1 接入）
      backtest_hint — 该 action+tier 组合的历史胜率
      safety_gates  — 上游计算好的护栏结果；未传则内部保持 pass（L5 独立计算）
    """
    now = int(time.time())

    if current_price is None or current_price <= 0:
        current_price = 0.0

    # 1. 过滤过期信号
    valid: list[CandidateSignal] = []
    for s in candidates or []:
        if s.expires_at and s.expires_at < now:
            continue
        valid.append(s)

    # 2. 投票选主方向
    primary_direction, aligned = _vote_primary_direction(valid)

    # 3. 聚合价格参数
    price_params = _aggregate_price_params(aligned)

    # 4. 选代表 action / tier
    chosen_action, chosen_tier, chosen_anchor = _pick_representative(aligned, primary_direction)

    # 5. 安全护栏
    safety = safety_gates or SafetyGateResult()

    # 6. 逐项打分
    breakdown = _score_breakdown(
        tier=chosen_tier,
        direction=primary_direction,
        aligned=aligned,
        regime=regime,
        chosen_action=chosen_action,
        price_params=price_params,
        geo=geo_overview,
        news=active_news,
        backtest=backtest_hint,
        safety=safety,
    )

    # 7. 红绿灯
    light = _traffic_light_for(breakdown.final_score, safety.triggered)

    # 8. Plan 装配
    plan = ExecutionPlan(
        coin=coin.upper(),
        ts=now,
        current_price=float(current_price),
        regime=regime.regime,
        regime_confidence=float(regime.confidence or 0.0),
        execution_score=breakdown.final_score,
        traffic_light=light,  # type: ignore[arg-type]
        action=chosen_action,  # type: ignore[arg-type]
        direction=primary_direction,  # type: ignore[arg-type]
        tier_hint=chosen_tier,  # type: ignore[arg-type]
        entry_zone_low=price_params.get("entry_low"),
        entry_zone_high=price_params.get("entry_high"),
        stop_loss=price_params.get("stop_loss"),
        tp1=price_params.get("tp1"),
        tp2=price_params.get("tp2"),
        rr_ratio=price_params.get("rr_ratio"),
        position_size_pct=_suggest_position_pct(breakdown.final_score, safety, regime),
        expires_at=now + 3600,
        breakdown=breakdown,
        safety_gates=safety,
        corroborating_sources=[s.source for s in aligned],
        contributing_signals=[s.model_copy(deep=True) for s in aligned],
        historical_win_rate=(
            float(backtest_hint.win_rate) if backtest_hint and backtest_hint.win_rate else None
        ),
        historical_sample_size=(
            int(backtest_hint.total_signals) if backtest_hint else 0
        ),
    )

    # 9. headline / one_liner
    headline, one_liner = _compose_headline(plan, aligned)
    plan.headline = headline
    plan.one_liner = one_liner

    # 10. D02 上报
    try:
        from utils.decision_tracker import D, get_tracker
        get_tracker().mark(
            D.D02_DUAL_ENGINE,
            status="ok" if primary_direction != "neutral" or chosen_action == "wait" else "warn",
            log=False,
            coin=coin,
            math_plan_ok=True,
            ai_report_ok=False,
            candidates_in=len(valid),
            aligned_count=len(aligned),
            action=chosen_action,
            final_score=round(breakdown.final_score, 2),
            traffic_light=light,
            safety_triggered=bool(safety.triggered),
        )
    except Exception:  # noqa: BLE001
        logger.debug("[D02] synthesize tracker mark failed", exc_info=True)

    return plan


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 内部辅助
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _vote_primary_direction(
    candidates: list[CandidateSignal],
) -> tuple[str, list[CandidateSignal]]:
    """加权投票选主方向。

    tier × source_weight 加权，多/空/中性分别累积：
      - 返回 (direction, aligned_signals)
      - aligned_signals 仅含主方向或中性（neutral）信号
      - 无候选时返回 ("neutral", [])
    """
    if not candidates:
        return "neutral", []

    bullish = 0.0
    bearish = 0.0
    neutrals: list[CandidateSignal] = []
    bull_list: list[CandidateSignal] = []
    bear_list: list[CandidateSignal] = []

    for s in candidates:
        tw = _TIER_VOTE_WEIGHT.get(str(s.confidence), 1.0)
        sw = _source_weight(s.source)
        w = tw * sw
        if s.direction == "bullish":
            bullish += w
            bull_list.append(s)
        elif s.direction == "bearish":
            bearish += w
            bear_list.append(s)
        else:
            neutrals.append(s)

    if bullish > bearish and bullish > 0:
        return "bullish", bull_list + neutrals
    if bearish > bullish and bearish > 0:
        return "bearish", bear_list + neutrals
    return "neutral", neutrals or (bull_list + bear_list)


def _pick_representative(
    aligned: list[CandidateSignal], direction: str,
) -> tuple[str, str, float]:
    """从 aligned 中选出代表性 action / tier / anchor。"""
    if not aligned:
        return "wait", "C", 0.0

    # 优先按 tier 降序（S→A→B→C），再按 score 降序
    def _key(s: CandidateSignal) -> tuple[float, float]:
        return (_TIER_VOTE_WEIGHT.get(str(s.confidence), 1.0), float(s.score or 0))

    sorted_aligned = sorted(aligned, key=_key, reverse=True)

    # 如果主方向是中性，整体偏 wait
    if direction == "neutral":
        top = sorted_aligned[0]
        return "wait", str(top.confidence), float(top.anchor_price or 0.0)

    # 跳过纯 wait 信号（它们只是标记还没触发）
    directional = [s for s in sorted_aligned if s.action in ("long", "short")]
    if directional:
        top = directional[0]
    else:
        top = sorted_aligned[0]

    return str(top.action), str(top.confidence), float(top.anchor_price or 0.0)


def _aggregate_price_params(aligned: list[CandidateSignal]) -> dict:
    """同向信号的 entry/SL/TP 聚合。"""
    params: dict = {
        "entry_low": None,
        "entry_high": None,
        "stop_loss": None,
        "tp1": None,
        "tp2": None,
        "rr_ratio": None,
    }
    if not aligned:
        return params

    entries = [s.entry_price for s in aligned if s.entry_price]
    if not entries:
        # 兜底用 anchor_price
        entries = [s.anchor_price for s in aligned if s.anchor_price]
    if entries:
        params["entry_low"] = min(entries)
        params["entry_high"] = max(entries)

    # 最严格 SL：做多取最低 SL，做空取最高 SL；这里简单用多数方向
    longs = [s for s in aligned if s.action == "long"]
    shorts = [s for s in aligned if s.action == "short"]
    if longs:
        sls = [s.stop_loss for s in longs if s.stop_loss]
        if sls:
            params["stop_loss"] = min(sls)
    elif shorts:
        sls = [s.stop_loss for s in shorts if s.stop_loss]
        if sls:
            params["stop_loss"] = max(sls)

    # tp：取置信最高的那一条
    best = max(aligned, key=lambda s: _TIER_VOTE_WEIGHT.get(str(s.confidence), 1.0))
    params["tp1"] = best.tp1
    params["tp2"] = best.tp2

    # rr：优先用聚合后重算；否则取 best.rr_ratio
    try:
        entry = (
            (params["entry_low"] + params["entry_high"]) / 2.0
            if params["entry_low"] and params["entry_high"]
            else (best.entry_price or best.anchor_price)
        )
        sl = params["stop_loss"]
        tp = params["tp1"]
        if entry and sl and tp and entry != sl:
            rr = abs(tp - entry) / abs(entry - sl)
            params["rr_ratio"] = round(rr, 2)
    except Exception:  # noqa: BLE001
        pass

    if params["rr_ratio"] is None and best.rr_ratio:
        params["rr_ratio"] = best.rr_ratio

    return params


def _score_breakdown(
    *,
    tier: str,
    direction: str,
    aligned: list[CandidateSignal],
    regime: RegimeSnapshot,
    chosen_action: str,
    price_params: dict,
    geo: Optional[GeoRiskOverview],
    news: Optional[list[MarketEventSignal]],
    backtest: Optional[BacktestStats],
    safety: SafetyGateResult,
) -> ExecutionScoreBreakdown:
    """计算 ExecutionScoreBreakdown 每一项"""
    b = ExecutionScoreBreakdown()
    b.base = 50.0

    # tier bonus
    b.tier_bonus = _TIER_BONUS.get(tier, 0.0)

    # confidence bonus（用最强一条的 confidence 做代表，S 额外再 +3，A +2, B +1）
    # 这里 tier 已用过；为避免双计，给 confidence_bonus 一个"样本数质量"含义：
    high_conf_count = sum(1 for s in aligned if s.confidence in ("S", "A"))
    if high_conf_count >= 2:
        b.confidence_bonus = 6.0
    elif high_conf_count == 1:
        b.confidence_bonus = 3.0
    else:
        b.confidence_bonus = 0.0

    # regime alignment
    b.regime_alignment = _regime_alignment_score(regime, chosen_action, direction)

    # corroboration（同向不同 source 家族的条数）
    source_families = set()
    for s in aligned:
        # 同族不重复加分：同 tracker_v2.* 算同一族
        if s.source.startswith("tracker_v2."):
            source_families.add("tracker_v2")
        elif s.source.startswith("news_event."):
            source_families.add("news_event")
        elif s.source.startswith("range_signal."):
            source_families.add("range_signal")
        elif s.source.startswith("levels."):
            source_families.add("levels")
        else:
            source_families.add(s.source.split(".")[0])
    fam_n = len(source_families)
    if fam_n >= 4:
        b.corroboration_bonus = 15.0
    elif fam_n == 3:
        b.corroboration_bonus = 10.0
    elif fam_n == 2:
        b.corroboration_bonus = 5.0
    else:
        b.corroboration_bonus = 0.0

    # cascade penalty
    #   从 aligned 里取最大 cascade_risk（provenance 字段），权重 -20 最大
    max_cascade = 0.0
    for s in aligned:
        cr = float(s.provenance.get("cascade_risk", 0.0) or 0.0)
        max_cascade = max(max_cascade, cr)
    # cascade_risk 是 0-1（V2 已归一化）
    b.cascade_penalty = -round(min(max_cascade, 1.0) * 20.0, 2)

    # rr bonus
    rr = price_params.get("rr_ratio") or 0
    if rr >= 3:
        b.rr_bonus = 8.0
    elif rr >= 2:
        b.rr_bonus = 4.0
    elif rr >= 1.5:
        b.rr_bonus = 2.0
    else:
        b.rr_bonus = 0.0

    # backtest
    if backtest and backtest.total_signals >= 10:
        wr = float(backtest.win_rate or 0)
        if wr >= 0.65:
            b.backtest_bonus = 10.0
        elif wr >= 0.55:
            b.backtest_bonus = 5.0
        elif wr <= 0.35:
            b.backtest_bonus = -10.0
        elif wr <= 0.45:
            b.backtest_bonus = -5.0

    # event_factor（P1 启用）
    if news:
        # 简化：取 impact_score 绝对值最大一条；方向与 direction 对齐 → 加分
        try:
            top_event = max(news, key=lambda e: abs(float(getattr(e, "impact_score", 0) or 0)))
            imp = float(getattr(top_event, "impact_score", 0) or 0)
            event_dir = str(getattr(top_event, "direction", "") or "").lower()
            aligned_bonus = 1.0 if (
                (direction == "bullish" and event_dir == "bullish") or
                (direction == "bearish" and event_dir == "bearish")
            ) else (-1.0 if event_dir in ("bullish", "bearish") and direction != "neutral" else 0.0)
            b.event_factor = round(min(max(imp * aligned_bonus, -12.0), 8.0), 2)
        except Exception:  # noqa: BLE001
            b.event_factor = 0.0

    # geo risk factor
    if geo is not None:
        try:
            lv = int(getattr(geo, "overall_level", 0) or 0)
            if lv >= 4:
                b.geo_risk_factor = -15.0
            elif lv == 3:
                b.geo_risk_factor = -10.0
            elif lv == 2:
                b.geo_risk_factor = -5.0
            else:
                b.geo_risk_factor = 0.0
        except Exception:  # noqa: BLE001
            b.geo_risk_factor = 0.0

    # safety_gate_delta
    if safety.triggered:
        # 任一 block → -40；仅 warn → -10
        has_block = any(
            getattr(safety, f, "pass") == "block"
            for f in ("g1_extreme_vol", "g2_macro_event", "g3_liq_chaos", "g4_api_degrade", "g5_blackswan")
        )
        b.safety_gate_delta = -40.0 if has_block else -10.0

    # final
    total = (
        b.base + b.tier_bonus + b.confidence_bonus + b.regime_alignment
        + b.corroboration_bonus + b.cascade_penalty + b.rr_bonus
        + b.backtest_bonus + b.event_factor + b.geo_risk_factor
        + b.safety_gate_delta
    )
    b.final_score = round(max(0.0, min(100.0, total)), 2)
    return b


def _regime_alignment_score(
    regime: RegimeSnapshot, action: str, direction: str,
) -> float:
    """把 regime.action_weights 映射到 -15 ~ +15 的分数。

    映射基准：
      - regime 推荐该 action: weight=1.3 → +10；1.2 → +7；1.1 → +3
      - regime 抑制该 action: weight=0.7 → -5；0.5 → -10；<=0.3 → -15
      - 无建议（weight≈1）: 0
    """
    if not regime or not regime.action_weights:
        return 0.0
    # 组合 key：snipe_{direction}, flip_{direction}, scalp_{direction}
    if action == "long":
        key_candidates = ["snipe_long", "flip_long", "scalp_long"]
    elif action == "short":
        key_candidates = ["snipe_short", "flip_short", "scalp_short"]
    else:
        return 0.0

    # 取最大值（表示有至少一种策略是鼓励的）
    weights = [regime.action_weights.get(k, 1.0) for k in key_candidates]
    if not weights:
        return 0.0
    w = max(weights)
    if w >= 1.3:
        return 10.0
    if w >= 1.2:
        return 7.0
    if w >= 1.1:
        return 3.0
    if w >= 0.9:
        return 0.0
    if w >= 0.7:
        return -5.0
    if w >= 0.4:
        return -10.0
    return -15.0


def _traffic_light_for(score: float, safety_triggered: bool) -> str:
    """分数 → 红绿灯颜色"""
    if safety_triggered:
        # 即便 triggered 但只有 warn，若分数仍高，最多降到 orange
        return "orange" if score >= 55 else "red"
    if score >= 75:
        return "green"
    if score >= 60:
        return "yellow"
    if score >= 45:
        return "orange"
    return "red"


def _suggest_position_pct(
    score: float, safety: SafetyGateResult, regime: RegimeSnapshot,
) -> float:
    """基于 score + safety + regime 推荐仓位百分比 0-100"""
    if safety.triggered and any(
        getattr(safety, f, "pass") == "block"
        for f in ("g1_extreme_vol", "g2_macro_event", "g3_liq_chaos", "g4_api_degrade", "g5_blackswan")
    ):
        return 0.0

    base = 0.0
    if score >= 80:
        base = 50.0
    elif score >= 70:
        base = 35.0
    elif score >= 60:
        base = 20.0
    elif score >= 50:
        base = 10.0
    else:
        base = 0.0

    # regime 压制
    if regime and regime.regime in ("extreme", "high_vol_chop"):
        base *= 0.3
    elif regime and regime.regime == "squeeze":
        base *= 0.5

    # safety warn → 减半
    if safety.triggered:
        base *= 0.5

    return round(min(max(base, 0.0), 60.0), 1)


def _compose_headline(
    plan: ExecutionPlan, aligned: list[CandidateSignal],
) -> tuple[str, str]:
    """组装 headline + one_liner 中文描述"""
    action_cn = {
        "long": "做多", "short": "做空", "wait": "观望", "avoid": "回避",
    }.get(plan.action, plan.action)

    direction_cn = {
        "bullish": "多头", "bearish": "空头", "neutral": "中性",
        "potential_reversal": "反向风险",
    }.get(plan.direction, plan.direction)

    regime_cn = {
        "trend_up": "上升趋势", "trend_down": "下降趋势",
        "range": "箱体震荡", "squeeze": "蓄力收敛",
        "high_vol_chop": "高波无序", "extreme": "极端波动",
    }.get(plan.regime, plan.regime)

    entry_txt = ""
    if plan.entry_zone_low and plan.entry_zone_high:
        if abs(plan.entry_zone_high - plan.entry_zone_low) < 1e-6:
            entry_txt = f" @ {plan.entry_zone_low:g}"
        else:
            entry_txt = f" @ {plan.entry_zone_low:g}~{plan.entry_zone_high:g}"

    pos_txt = (
        f"，仓位 {plan.position_size_pct:g}%"
        if plan.position_size_pct and plan.position_size_pct > 0 else ""
    )
    headline = f"[{plan.tier_hint}] {action_cn} · {regime_cn}{entry_txt}{pos_txt}"

    # one_liner：列几个贡献源 + RR + 分数
    fam = []
    seen = set()
    for s in aligned[:4]:
        prefix = s.source.split(".")[0]
        if prefix not in seen:
            seen.add(prefix)
            fam.append(prefix)
    srcs = "/".join(fam) if fam else "无"
    rr_txt = f" · RR {plan.rr_ratio}" if plan.rr_ratio else ""
    safety_txt = " · ⚠ 护栏触发" if plan.safety_gates.triggered else ""
    one_liner = (
        f"{direction_cn}·score={plan.execution_score:g}·来源[{srcs}]·"
        f"{regime_cn}{rr_txt}{safety_txt}"
    )

    return headline, one_liner
