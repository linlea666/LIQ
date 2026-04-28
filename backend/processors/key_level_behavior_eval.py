"""V3 关键位行为评估层（Behavior Validation Layer · 2026-04-M1）。

═══════════════════════════════════════════════════════════════
设计目标
═══════════════════════════════════════════════════════════════

旧 state machine（key_level_tracker_v2._transition）回答：
    "事件是否发生？"（broken / bounced / flipped 几何门）

本模块回答：
    "这次事件多可信？"（连续 0-1 分数 + 多因子）

两者职责互补，不竞争 state 决定权。
本模块是关键位的"行为侧标签"，对外只读 KeyLevelV2，仅写入 lv.behavior。

═══════════════════════════════════════════════════════════════
设计纪律（违反即视为 bug）
═══════════════════════════════════════════════════════════════

1. 不写入 lv.state / lv.final_score / lv.strength_tier / lv.cascade_risk
2. 不修改 lv.explain_chips（避免覆盖 V3 已有解释；本模块解释独立放在 lv.behavior.explain_chips）
3. 输入仅读 KeyLevelV2 + 行情数据（candles/cvd/oi/taker），不依赖 footprint/funding（M1 最小依赖）
4. M1 阶段为纯观测：不参与 signal 生成、不进 AI prompt
5. 调用顺序：必须在 _transition / _assess_bounce_quality / _assess_breakout_stage / _calc_cascade_risk 之后调用
   （依赖 lv.state、lv.bounce_quality、lv.breakout_stage、lv.cascade_risk 已就绪）

═══════════════════════════════════════════════════════════════
6 个分数定义
═══════════════════════════════════════════════════════════════

breakout_validity         真突破质量（state ∈ {broken, flipped} 时填充）
retest_quality            回踩质量（state ∈ {bounced, flipped} 时填充）
selloff_continuation_risk 放量破位延续风险（state == broken & side == support）
capitulation_bottom_score 恐慌出清候选分（state == broken & side == support）
flip_confirmation         翻转确认度（state == flipped）
false_break_risk          假突破风险（state ∈ {testing, broken}；state == fake_break 直接 = 1.0）

公式选型理念：
    每个分数都用"4-5 个 0-1 因子的算术平均（或加权平均）"，避免乘法导致的零吞噬。
    因子选择优先复用 V3 已有字段（cascade_risk / vacuum_gap_pct / next_magnet_price /
    bounce_quality / breakout_stage / lowest_wick），避免重复造轮子。

═══════════════════════════════════════════════════════════════
behavior_state 派生（9 选 1）
═══════════════════════════════════════════════════════════════

state == fake_break                                              → failed_breakout
state == flipped & flip_confirmation >= 0.65                     → confirmed_flip
state == flipped & flip_confirmation <  0.40                     → wait_for_second_test
state == broken  & breakout_validity  >= 0.65                    → true_breakout
state == broken  & false_break_risk   >= 0.60                    → failed_breakout
state == broken  & support & selloff_continuation_risk >= 0.65   → heavy_volume_breakdown
state == broken  & support & capitulation_bottom_score >= 0.65   → capitulation_flush
state == bounced & retest_quality     >= 0.55                    → healthy_retest
state == testing & breakout_validity  >= 0.55                    → pending_breakout
其它                                                              → pending
"""

from __future__ import annotations

import logging
import math
import time
from typing import Optional

from models.flow import CVDData
from models.key_level import BehaviorEval, KeyLevelSnapshotV2, KeyLevelV2
from models.market import CandleData

logger = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 默认配置（与 _DEFAULT_CFG 风格一致；可由调用方覆盖）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_DEFAULT_BEHAVIOR_CFG = {
    # 预计算窗口
    "vol_baseline_bars": 20,         # 量能基准回看根数（15m）
    "panic_baseline_bars": 50,       # 恐慌量分位基准回看根数
    "panic_percentile": 0.95,        # 恐慌量分位（≥ 95%）
    # breakout_validity 因子权重（5 因子均权）
    "bv_weight_stage": 0.25,
    "bv_weight_closed": 0.20,
    "bv_weight_volume": 0.20,
    "bv_weight_body": 0.15,
    "bv_weight_flow": 0.20,
    # retest_quality 因子权重（4 因子均权）
    "rq_weight_quality_label": 0.30,
    "rq_weight_pullback_depth": 0.25,
    "rq_weight_volume_contraction": 0.25,
    "rq_weight_held_level": 0.20,
    # selloff_continuation_risk 因子权重
    "sc_weight_breakout_validity": 0.25,
    "sc_weight_cascade": 0.30,
    "sc_weight_vacuum": 0.20,
    "sc_weight_cvd": 0.15,
    "sc_weight_close_near_low": 0.10,
    # capitulation 触发门槛（panic_volume_percentile 必须达标才计算其他子项）
    "cap_panic_gate": 0.85,
    "cap_weight_panic": 0.30,
    "cap_weight_wick_reclaim": 0.25,
    "cap_weight_cvd_divergence": 0.20,
    "cap_weight_oi_drop": 0.15,
    "cap_weight_funding_reset": 0.10,
    # flip_confirmation 因子权重
    "fc_weight_breakout_validity": 0.30,
    "fc_weight_retest_quality": 0.30,
    "fc_weight_stage3": 0.20,
    "fc_weight_bounce_count": 0.20,
    # behavior_state 阈值
    "th_confirmed_flip": 0.65,
    "th_wait_second_test": 0.40,
    "th_true_breakout": 0.65,
    "th_failed_breakout": 0.60,
    "th_heavy_breakdown": 0.65,
    "th_capitulation": 0.65,
    "th_healthy_retest": 0.55,
    "th_pending_breakout": 0.55,
}

# 限制 explain_chips 数量（避免前端 UI 拥挤）
_MAX_CHIPS = 4


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 主入口
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def evaluate_behavior(
    snapshot: KeyLevelSnapshotV2,
    *,
    candles_15m: list[CandleData] | None = None,
    candles_1h: list[CandleData] | None = None,
    cvd: CVDData | None = None,
    oi_change_pct_1h: float = 0.0,
    taker_buy_vol: float = 0.0,
    taker_sell_vol: float = 0.0,
    cfg: dict | None = None,
    now: int = 0,
) -> None:
    """主入口：在 state machine 之后就地写入 lv.behavior。

    严格只读 lv（除 lv.behavior 字段），不修改 state / final_score / explain_chips。
    任何异常单 level 隔离，不影响其他 level（pending state + 空分数）。
    """
    cfg = {**_DEFAULT_BEHAVIOR_CFG, **(cfg or {})}
    if now == 0:
        now = int(time.time())

    if snapshot.atr <= 0 or snapshot.current_price <= 0:
        # 数据不足，给所有 level 安插 pending 占位（前端可见状态）
        for lv in snapshot.levels:
            lv.behavior = BehaviorEval(behavior_state="pending", evaluated_at=now)
        return

    # 预计算（snapshot 级共享）
    ctx = _prepare_context(
        candles_15m=candles_15m,
        candles_1h=candles_1h,
        cvd=cvd,
        oi_change_pct_1h=oi_change_pct_1h,
        taker_buy_vol=taker_buy_vol,
        taker_sell_vol=taker_sell_vol,
        atr=snapshot.atr,
        cfg=cfg,
    )

    for lv in snapshot.levels:
        try:
            lv.behavior = _evaluate_one_level(lv, snapshot, ctx, cfg, now)
        except Exception:
            logger.exception(
                "behavior_eval failed for level price=%.4f side=%s state=%s",
                lv.price, lv.side, lv.state,
            )
            lv.behavior = BehaviorEval(behavior_state="pending", evaluated_at=now)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 上下文预计算（snapshot 级共享，所有 level 复用）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _prepare_context(
    *,
    candles_15m: list[CandleData] | None,
    candles_1h: list[CandleData] | None,
    cvd: CVDData | None,
    oi_change_pct_1h: float,
    taker_buy_vol: float,
    taker_sell_vol: float,
    atr: float,
    cfg: dict,
) -> dict:
    """计算所有 level 共享的市场上下文指标。

    输出 dict 结构（None 表示数据不足）：
      atr, last_close, last_bar_15m, prev_bar_15m,
      vol_baseline_15m, vol_zscore_recent, vol_ratio_recent,
      panic_volume_percentile, last_wick_lower_ratio, last_wick_upper_ratio,
      cvd_trend, has_cvd_divergence,
      oi_change_pct_1h, taker_buy_vol, taker_sell_vol, flow_alignment,
      candles_15m, candles_1h
    """
    ctx: dict = {
        "atr": atr,
        "candles_15m": candles_15m,
        "candles_1h": candles_1h,
        "oi_change_pct_1h": oi_change_pct_1h,
        "taker_buy_vol": taker_buy_vol,
        "taker_sell_vol": taker_sell_vol,
        "cvd_trend": (cvd.trend_1h if cvd else "") or "",
        "has_cvd_divergence": bool(cvd.has_divergence) if cvd else False,
    }

    # 量能基线（15m 近 N 根）
    vol_baseline_bars = int(cfg.get("vol_baseline_bars", 20))
    panic_bars = int(cfg.get("panic_baseline_bars", 50))

    if candles_15m and len(candles_15m) >= 2:
        ctx["last_bar_15m"] = candles_15m[-1]
        ctx["prev_bar_15m"] = candles_15m[-2]
        ctx["last_close"] = candles_15m[-1].close

        # 最近 vol_baseline_bars 根（不含当前未收盘）的均量
        ref = candles_15m[-(vol_baseline_bars + 1):-1] if len(candles_15m) > vol_baseline_bars else candles_15m[:-1]
        vols = [c.vol for c in ref if c.vol > 0]
        if vols:
            avg_vol = sum(vols) / len(vols)
            ctx["vol_baseline_15m"] = avg_vol
            cur_vol = candles_15m[-1].vol
            ctx["vol_ratio_recent"] = cur_vol / avg_vol if avg_vol > 0 else 0.0
            # z-score（粗略：(x - mean) / std）
            std = math.sqrt(sum((v - avg_vol) ** 2 for v in vols) / len(vols)) if len(vols) > 1 else 0.0
            ctx["vol_zscore_recent"] = (cur_vol - avg_vol) / std if std > 0 else 0.0
        else:
            ctx["vol_baseline_15m"] = 0.0
            ctx["vol_ratio_recent"] = 0.0
            ctx["vol_zscore_recent"] = 0.0

        # 恐慌量分位（≥ 第 panic_percentile 分位 = panic_volume_percentile 高）
        panic_ref = candles_15m[-panic_bars:-1] if len(candles_15m) > panic_bars else candles_15m[:-1]
        panic_vols = sorted([c.vol for c in panic_ref if c.vol > 0])
        if panic_vols:
            cur_vol = candles_15m[-1].vol
            count_below = sum(1 for v in panic_vols if v < cur_vol)
            ctx["panic_volume_percentile"] = count_below / len(panic_vols)
        else:
            ctx["panic_volume_percentile"] = 0.0

        # 最近一根的下/上影线占比（用于 wick_reclaim / pin_bar）
        last = candles_15m[-1]
        rng = max(last.high - last.low, 1e-9)
        body_low = min(last.open, last.close)
        body_high = max(last.open, last.close)
        ctx["last_wick_lower_ratio"] = max(body_low - last.low, 0.0) / rng
        ctx["last_wick_upper_ratio"] = max(last.high - body_high, 0.0) / rng
        ctx["last_body_ratio"] = (body_high - body_low) / rng
    else:
        ctx["last_bar_15m"] = None
        ctx["prev_bar_15m"] = None
        ctx["last_close"] = 0.0
        ctx["vol_baseline_15m"] = 0.0
        ctx["vol_ratio_recent"] = 0.0
        ctx["vol_zscore_recent"] = 0.0
        ctx["panic_volume_percentile"] = 0.0
        ctx["last_wick_lower_ratio"] = 0.0
        ctx["last_wick_upper_ratio"] = 0.0
        ctx["last_body_ratio"] = 0.0

    # 主动买卖力量对齐方向（taker 主导方向）
    total_taker = taker_buy_vol + taker_sell_vol
    if total_taker > 0:
        buy_share = taker_buy_vol / total_taker
        ctx["taker_buy_share"] = buy_share
        # taker_dominant: "buy" / "sell" / "neutral"
        if buy_share >= 0.6:
            ctx["taker_dominant"] = "buy"
        elif buy_share <= 0.4:
            ctx["taker_dominant"] = "sell"
        else:
            ctx["taker_dominant"] = "neutral"
    else:
        ctx["taker_buy_share"] = 0.5
        ctx["taker_dominant"] = "neutral"

    return ctx


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 单 level 主流程
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _evaluate_one_level(
    lv: KeyLevelV2,
    snapshot: KeyLevelSnapshotV2,
    ctx: dict,
    cfg: dict,
    now: int,
) -> BehaviorEval:
    """计算单个 level 的 6 个分数 + 综合状态。"""
    e = BehaviorEval(evaluated_at=now)
    used: list[str] = []

    state = lv.state
    is_support = lv.side == "support"

    # ── 1. breakout_validity（broken / flipped / testing 时计算）──
    if state in ("broken", "flipped", "testing"):
        e.breakout_validity = _calc_breakout_validity(lv, ctx, cfg)
        used.append("breakout_validity")

    # ── 2. retest_quality（bounced / flipped 时计算）──
    if state in ("bounced", "flipped"):
        e.retest_quality = _calc_retest_quality(lv, ctx, cfg)
        used.append("retest_quality")

    # ── 3. selloff_continuation_risk（broken & support 时计算）──
    if state == "broken" and is_support:
        e.selloff_continuation_risk = _calc_selloff_continuation(lv, snapshot, ctx, cfg, e.breakout_validity)
        used.append("selloff_continuation_risk")

    # ── 4. capitulation_bottom_score（broken & support 且伴随极端放量+长下影时计算）──
    if state == "broken" and is_support:
        e.capitulation_bottom_score = _calc_capitulation(lv, ctx, cfg)
        used.append("capitulation_bottom_score")

    # ── 5. flip_confirmation（flipped 时计算）──
    if state == "flipped":
        e.flip_confirmation = _calc_flip_confirmation(lv, ctx, cfg, e.breakout_validity, e.retest_quality)
        used.append("flip_confirmation")

    # ── 6. false_break_risk（testing / broken / fake_break 时计算）──
    if state == "fake_break":
        e.false_break_risk = 1.0  # 事件已成立
        used.append("false_break_risk")
    elif state in ("testing", "broken"):
        e.false_break_risk = _calc_false_break_risk(lv, ctx, cfg, e.breakout_validity)
        used.append("false_break_risk")

    # ── 派生 behavior_state + state_confidence + chips ──
    e.behavior_state = _derive_behavior_state(lv, e, cfg)
    e.state_confidence = _derive_state_confidence(lv, e)
    e.explain_chips = _build_chips(lv, e, ctx)
    e.components_used = used

    return e


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 子分数：breakout_validity（突破有效性）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _calc_breakout_validity(lv: KeyLevelV2, ctx: dict, cfg: dict) -> float:
    """5 因子加权：阶段 + 收盘 + 量能 + 实体 + 流量对齐。

    返回 [0, 1]。每个因子缺失时给 0.5 中性值（不强加偏置）。
    """
    is_support = lv.side == "support"

    # f1: breakout_stage（已是 0/1/2/3）→ 归一化到 0~1
    stage = lv.breakout_stage or 0
    f_stage = min(stage / 3.0, 1.0)

    # f2: 收盘是否确认（用最近一根已收盘 15m bar 的 close 与 lv.price 对比）
    f_closed = _close_acceptance_score(lv, ctx)

    # f3: 量能扩张（vol_ratio_recent → 越高越好，clamp 到 [0, 2] 并归一）
    vr = float(ctx.get("vol_ratio_recent", 0.0))
    f_volume = min(vr / 2.0, 1.0) if vr > 0 else 0.5

    # f4: K 线实体强度（实体占整个 range 的比例）
    body = float(ctx.get("last_body_ratio", 0.0))
    f_body = min(body * 1.4, 1.0)  # 体感：实体 ≥ 70% 即接近满分

    # f5: 流量方向对齐（突破方向 与 taker / cvd 一致性）
    f_flow = _flow_alignment_score(is_support, ctx)

    weights = {
        "stage": float(cfg.get("bv_weight_stage", 0.25)),
        "closed": float(cfg.get("bv_weight_closed", 0.20)),
        "volume": float(cfg.get("bv_weight_volume", 0.20)),
        "body": float(cfg.get("bv_weight_body", 0.15)),
        "flow": float(cfg.get("bv_weight_flow", 0.20)),
    }
    score = (
        weights["stage"] * f_stage
        + weights["closed"] * f_closed
        + weights["volume"] * f_volume
        + weights["body"] * f_body
        + weights["flow"] * f_flow
    )
    return _clamp01(score)


def _close_acceptance_score(lv: KeyLevelV2, ctx: dict) -> float:
    """最近一根已收盘 15m bar 是否站稳关键位（破侧）。

    支撑被突破：close 应 < lv.price（破在下方）
    阻力被突破：close 应 > lv.price（破在上方）
    """
    candles = ctx.get("candles_15m") or []
    if len(candles) < 2:
        return 0.5
    last_closed = candles[-2]  # 倒数第二根才是已收盘
    if not last_closed or last_closed.close <= 0 or lv.price <= 0:
        return 0.5
    is_support = lv.side == "support"
    if is_support:
        # 站在破侧（下方）= 突破成立
        gap_pct = (lv.price - last_closed.close) / lv.price * 100
    else:
        gap_pct = (last_closed.close - lv.price) / lv.price * 100
    if gap_pct <= 0:
        return 0.3  # 还在原区间
    # gap ≥ 0.5% 视为充分站稳
    return _clamp01(0.5 + gap_pct / 1.0)  # 0.5 → 0.5+0.5*1.0=1.0


def _flow_alignment_score(is_support: bool, ctx: dict) -> float:
    """流量方向（taker / cvd）与突破方向是否一致。

    支撑被突破（向下）：taker_dominant=sell + cvd_trend=falling/declining 加分
    阻力被突破（向上）：taker_dominant=buy + cvd_trend=rising 加分
    """
    score = 0.5
    taker_dominant = ctx.get("taker_dominant", "neutral")
    cvd_trend = (ctx.get("cvd_trend") or "").lower()

    expected_taker = "sell" if is_support else "buy"
    expected_cvd_keywords = ("fall", "declin", "down", "weaken") if is_support else ("ris", "up", "strong")

    if taker_dominant == expected_taker:
        score += 0.25
    elif taker_dominant != "neutral":
        score -= 0.15

    if cvd_trend and any(k in cvd_trend for k in expected_cvd_keywords):
        score += 0.25
    elif cvd_trend:
        score -= 0.15

    return _clamp01(score)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 子分数：retest_quality（回踩质量）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _calc_retest_quality(lv: KeyLevelV2, ctx: dict, cfg: dict) -> float:
    """4 因子加权：质量标签 + 回踩深度 + 量能收缩 + 持守关键位。

    复用 V3 已有 lv.bounce_quality（proactive/passive/""）。
    回踩深度从 lv.lowest_wick 派生。
    """
    # f1: bounce_quality 映射
    bq = lv.bounce_quality or ""
    if bq == "proactive":
        f_label = 1.0
    elif bq == "passive":
        f_label = 0.2
    else:
        f_label = 0.5

    # f2: 回踩深度（lowest_wick 距 lv.price 越近越好；ATR 归一）
    f_pullback = _pullback_depth_score(lv, ctx)

    # f3: 量能收缩（健康回踩 = 量能小于近 20 根均量）
    vr = float(ctx.get("vol_ratio_recent", 1.0))
    if vr <= 0.7:
        f_volume = 1.0
    elif vr <= 1.0:
        f_volume = 0.7
    elif vr <= 1.3:
        f_volume = 0.4
    else:
        f_volume = 0.1  # 回踩放量 = 不健康

    # f4: 是否守住关键位（distance 还在合理范围 + 价格未跌破破侧）
    f_held = _held_level_score(lv, ctx)

    weights = {
        "label": float(cfg.get("rq_weight_quality_label", 0.30)),
        "pullback": float(cfg.get("rq_weight_pullback_depth", 0.25)),
        "volume": float(cfg.get("rq_weight_volume_contraction", 0.25)),
        "held": float(cfg.get("rq_weight_held_level", 0.20)),
    }
    score = (
        weights["label"] * f_label
        + weights["pullback"] * f_pullback
        + weights["volume"] * f_volume
        + weights["held"] * f_held
    )
    return _clamp01(score)


def _pullback_depth_score(lv: KeyLevelV2, ctx: dict) -> float:
    """回踩深度评分：lowest_wick 离 lv.price 越近越好。"""
    if lv.lowest_wick is None or lv.lowest_wick <= 0 or lv.price <= 0:
        return 0.5
    atr = float(ctx.get("atr", 0.0))
    if atr <= 0:
        return 0.5
    is_support = lv.side == "support"
    # 支撑：lowest_wick 应 ≥ lv.price - α×ATR；α 越小越好
    if is_support:
        gap = lv.price - lv.lowest_wick
    else:
        # 阻力翻支撑场景：highest_wick 才合理；这里近似用 lowest_wick 代理
        gap = lv.price - lv.lowest_wick
    if gap <= 0:
        return 0.9  # 没有破位
    alpha = gap / atr
    if alpha <= 0.3:
        return 1.0
    if alpha <= 0.6:
        return 0.7
    if alpha <= 1.0:
        return 0.4
    return 0.1


def _held_level_score(lv: KeyLevelV2, ctx: dict) -> float:
    """是否守住关键位：last_close 应在未破侧。"""
    last_close = float(ctx.get("last_close", 0.0))
    if last_close <= 0 or lv.price <= 0:
        return 0.5
    is_support = lv.side == "support"
    if is_support:
        # 应在 lv.price 之上
        return 1.0 if last_close >= lv.price else 0.2
    else:
        return 1.0 if last_close <= lv.price else 0.2


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 子分数：selloff_continuation_risk（放量破位延续风险）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _calc_selloff_continuation(
    lv: KeyLevelV2,
    snapshot: KeyLevelSnapshotV2,
    ctx: dict,
    cfg: dict,
    breakout_validity: float,
) -> float:
    """5 因子加权：突破有效性 + cascade + 真空 + CVD + 收盘靠近低位。

    复用 V3 已有 lv.cascade_risk / lv.vacuum_gap_pct / lv.next_magnet_price。
    """
    # f1: 突破有效性（沿用同 level 的 breakout_validity）
    f_bv = breakout_validity

    # f2: cascade_risk（已是 0-1）
    f_cascade = _clamp01(float(lv.cascade_risk or 0.0))

    # f3: vacuum_gap_pct（V3 字段：当前位到下一磁铁的真空跨度，越大越易急速穿越）
    vg = float(lv.vacuum_gap_pct or 0.0)
    if vg >= 3.0:
        f_vacuum = 1.0
    elif vg >= 1.5:
        f_vacuum = 0.7
    elif vg >= 0.8:
        f_vacuum = 0.5
    else:
        f_vacuum = 0.2

    # f4: CVD 持续走弱
    cvd_trend = (ctx.get("cvd_trend") or "").lower()
    if any(k in cvd_trend for k in ("fall", "declin", "weaken")):
        f_cvd = 1.0
    elif "neutral" in cvd_trend or not cvd_trend:
        f_cvd = 0.5
    else:
        f_cvd = 0.2

    # f5: 收盘靠近低位（last bar 的 close 接近 low）
    f_close_near_low = _close_near_low_score(ctx)

    weights = {
        "bv": float(cfg.get("sc_weight_breakout_validity", 0.25)),
        "cascade": float(cfg.get("sc_weight_cascade", 0.30)),
        "vacuum": float(cfg.get("sc_weight_vacuum", 0.20)),
        "cvd": float(cfg.get("sc_weight_cvd", 0.15)),
        "close": float(cfg.get("sc_weight_close_near_low", 0.10)),
    }
    score = (
        weights["bv"] * f_bv
        + weights["cascade"] * f_cascade
        + weights["vacuum"] * f_vacuum
        + weights["cvd"] * f_cvd
        + weights["close"] * f_close_near_low
    )
    return _clamp01(score)


def _close_near_low_score(ctx: dict) -> float:
    """最近一根 close 离 low 越近，下行延续风险越大。"""
    last = ctx.get("last_bar_15m")
    if not last:
        return 0.5
    rng = last.high - last.low
    if rng <= 0:
        return 0.5
    # close 占 range 中位置：0 = 收在 low（最弱），1 = 收在 high（最强）
    pos = (last.close - last.low) / rng
    return _clamp01(1.0 - pos)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 子分数：capitulation_bottom_score（恐慌出清候选）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _calc_capitulation(lv: KeyLevelV2, ctx: dict, cfg: dict) -> float:
    """门槛触发式：必须先达到 panic_volume_percentile gate 才计算其他子项。

    高分严格要求：极端放量 + 长下影 + CVD 背离 + OI 释放。
    """
    panic_pct = float(ctx.get("panic_volume_percentile", 0.0))
    gate = float(cfg.get("cap_panic_gate", 0.85))

    if panic_pct < gate:
        # 未触发恐慌量门槛 → 直接返回低分（不参与累加）
        return 0.0

    # f1: 极端放量分位（已 ≥ gate，把 gate→1 的部分映射到 0.7→1.0）
    f_panic = 0.7 + (panic_pct - gate) / max(1.0 - gate, 1e-3) * 0.3
    f_panic = _clamp01(f_panic)

    # f2: 长下影（wick_lower_ratio ≥ 0.5 → 接近满分）
    wick = float(ctx.get("last_wick_lower_ratio", 0.0))
    if wick >= 0.6:
        f_wick = 1.0
    elif wick >= 0.4:
        f_wick = 0.7
    elif wick >= 0.25:
        f_wick = 0.4
    else:
        f_wick = 0.1

    # f3: CVD 背离
    f_cvd = 1.0 if ctx.get("has_cvd_divergence", False) else 0.4

    # f4: OI 大幅下降（多头清算释放，oi_change_pct_1h ≤ -1.0%）
    oi_change = float(ctx.get("oi_change_pct_1h", 0.0))
    if oi_change <= -2.0:
        f_oi = 1.0
    elif oi_change <= -1.0:
        f_oi = 0.7
    elif oi_change <= 0:
        f_oi = 0.4
    else:
        f_oi = 0.1  # OI 反而上升 → 不是清算释放

    # f5: funding 重置（M1 不接入 funding，给中性 0.5）
    f_funding = 0.5

    weights = {
        "panic": float(cfg.get("cap_weight_panic", 0.30)),
        "wick": float(cfg.get("cap_weight_wick_reclaim", 0.25)),
        "cvd": float(cfg.get("cap_weight_cvd_divergence", 0.20)),
        "oi": float(cfg.get("cap_weight_oi_drop", 0.15)),
        "funding": float(cfg.get("cap_weight_funding_reset", 0.10)),
    }
    score = (
        weights["panic"] * f_panic
        + weights["wick"] * f_wick
        + weights["cvd"] * f_cvd
        + weights["oi"] * f_oi
        + weights["funding"] * f_funding
    )
    return _clamp01(score)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 子分数：flip_confirmation（翻转确认度）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _calc_flip_confirmation(
    lv: KeyLevelV2,
    ctx: dict,
    cfg: dict,
    breakout_validity: float,
    retest_quality: float,
) -> float:
    """4 因子加权：突破有效 + 回踩有效 + stage3 确认 + 反弹次数。"""
    f_bv = breakout_validity
    f_rq = retest_quality

    # f3: stage 3 是否到达
    stage = lv.breakout_stage or 0
    f_stage3 = 1.0 if stage >= 3 else (0.5 if stage == 2 else 0.2)

    # f4: 在新 side 反弹次数（bounce_count > 0 = 有市场确认）
    bc = lv.bounce_count or 0
    if bc >= 2:
        f_bounce = 1.0
    elif bc == 1:
        f_bounce = 0.7
    else:
        f_bounce = 0.3

    weights = {
        "bv": float(cfg.get("fc_weight_breakout_validity", 0.30)),
        "rq": float(cfg.get("fc_weight_retest_quality", 0.30)),
        "stage": float(cfg.get("fc_weight_stage3", 0.20)),
        "bounce": float(cfg.get("fc_weight_bounce_count", 0.20)),
    }
    score = (
        weights["bv"] * f_bv
        + weights["rq"] * f_rq
        + weights["stage"] * f_stage3
        + weights["bounce"] * f_bounce
    )
    return _clamp01(score)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 子分数：false_break_risk（假突破风险预警）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _calc_false_break_risk(
    lv: KeyLevelV2,
    ctx: dict,
    cfg: dict,
    breakout_validity: float,
) -> float:
    """与事件级 fake_break 互补：在 fake_break 状态成立前提供预警分数。

    state == fake_break  → 1.0（事件成立，由 evaluate 主流程直接赋值）
    state == broken      → 1 - breakout_validity（突破弱即预警）
    state == testing     → 由量能 + 流量 + 影线综合判定
    """
    if lv.state == "broken":
        return _clamp01(1.0 - breakout_validity)

    # state == testing：早期预警
    is_support = lv.side == "support"
    score = 0.0

    # 影线方向不利（支撑下方有长下影 + 价格反弹 = 假破嫌疑）
    wick_lower = float(ctx.get("last_wick_lower_ratio", 0.0))
    wick_upper = float(ctx.get("last_wick_upper_ratio", 0.0))
    if is_support and wick_lower >= 0.4:
        score += 0.30
    if not is_support and wick_upper >= 0.4:
        score += 0.30

    # 量能未扩张
    vr = float(ctx.get("vol_ratio_recent", 1.0))
    if vr < 0.8:
        score += 0.20

    # 流量与突破方向反向
    taker_dominant = ctx.get("taker_dominant", "neutral")
    expected = "sell" if is_support else "buy"
    if taker_dominant != "neutral" and taker_dominant != expected:
        score += 0.20

    return _clamp01(score)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 派生：behavior_state / state_confidence / chips
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _derive_behavior_state(lv: KeyLevelV2, e: BehaviorEval, cfg: dict) -> str:
    """从 6 个分数派生综合 behavior_state（9 选 1）。"""
    state = lv.state
    is_support = lv.side == "support"

    th_confirmed = float(cfg.get("th_confirmed_flip", 0.65))
    th_wait = float(cfg.get("th_wait_second_test", 0.40))
    th_true = float(cfg.get("th_true_breakout", 0.65))
    th_failed = float(cfg.get("th_failed_breakout", 0.60))
    th_heavy = float(cfg.get("th_heavy_breakdown", 0.65))
    th_cap = float(cfg.get("th_capitulation", 0.65))
    th_healthy = float(cfg.get("th_healthy_retest", 0.55))
    th_pending = float(cfg.get("th_pending_breakout", 0.55))

    if state == "fake_break":
        return "failed_breakout"

    if state == "flipped":
        if e.flip_confirmation >= th_confirmed:
            return "confirmed_flip"
        if e.flip_confirmation < th_wait:
            return "wait_for_second_test"
        return "pending"

    if state == "broken":
        # 优先级：capitulation > heavy_volume_breakdown > failed_breakout > true_breakout
        if is_support and e.capitulation_bottom_score >= th_cap:
            return "capitulation_flush"
        if e.false_break_risk >= th_failed:
            return "failed_breakout"
        if is_support and e.selloff_continuation_risk >= th_heavy:
            return "heavy_volume_breakdown"
        if e.breakout_validity >= th_true:
            return "true_breakout"
        return "pending"

    if state == "bounced":
        if e.retest_quality >= th_healthy:
            return "healthy_retest"
        return "pending"

    if state == "testing":
        if e.breakout_validity >= th_pending:
            return "pending_breakout"
        return "pending"

    return "pending"


def _derive_state_confidence(lv: KeyLevelV2, e: BehaviorEval) -> float:
    """旧 state 的可信度。"""
    state = lv.state
    if state == "broken":
        return e.breakout_validity
    if state == "bounced":
        return e.retest_quality
    if state == "flipped":
        return e.flip_confirmation
    if state == "fake_break":
        return _clamp01(1.0 - e.false_break_risk)
    if state == "testing":
        return e.breakout_validity * 0.5  # testing 还没真破，信心折半
    return 0.0


def _build_chips(lv: KeyLevelV2, e: BehaviorEval, ctx: dict) -> list[str]:
    """前端 chip：精简白话，最多 _MAX_CHIPS 个。

    chips 选择优先级：
        1. behavior_state 主标签（必有）
        2. 1-2 个支撑性子因子描述（量能/影线/cvd 等）
    """
    chips: list[str] = []
    state_label = _BEHAVIOR_STATE_LABEL.get(e.behavior_state, "")
    if state_label:
        chips.append(state_label)

    # 量能描述
    vr = float(ctx.get("vol_ratio_recent", 0.0))
    if e.behavior_state in ("true_breakout", "pending_breakout") and vr >= 1.5:
        chips.append("放量站稳")
    elif e.behavior_state == "healthy_retest" and vr <= 0.8:
        chips.append("缩量回踩")
    elif e.behavior_state == "heavy_volume_breakdown" and vr >= 1.5:
        chips.append("放量破位")

    # 影线描述
    wl = float(ctx.get("last_wick_lower_ratio", 0.0))
    if e.behavior_state == "capitulation_flush" and wl >= 0.4:
        chips.append("长下影收回")

    # 流量背离
    if e.behavior_state in ("failed_breakout",) and ctx.get("has_cvd_divergence"):
        chips.append("CVD 背离")

    # vacuum 警示
    if e.behavior_state == "heavy_volume_breakdown" and (lv.vacuum_gap_pct or 0.0) >= 1.5:
        chips.append("下方真空")

    return chips[:_MAX_CHIPS]


_BEHAVIOR_STATE_LABEL = {
    "true_breakout": "真突破",
    "healthy_retest": "健康回踩",
    "failed_breakout": "假突破/失败突破",
    "heavy_volume_breakdown": "放量破位",
    "capitulation_flush": "恐慌出清候选",
    "confirmed_flip": "翻转确认",
    "wait_for_second_test": "等二次确认",
    "pending_breakout": "突破逼近",
    "pending": "",
}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 工具函数
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _clamp01(x: float) -> float:
    """钳位到 [0, 1]，处理 NaN / 负值 / 极大值。"""
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return 0.0
    if x < 0:
        return 0.0
    if x > 1:
        return 1.0
    return float(x)
