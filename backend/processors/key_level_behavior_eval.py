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

# 模块版本（M3.1 引入 · 用于 BehaviorEval.behavior_eval_version）
BEHAVIOR_EVAL_VERSION = "1.1"  # 1.0 = M2.5 / 1.1 = M3.1（互斥校准 + 元信息）


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# M3.1 健康监控（模块级 in-memory，向 /api/key-levels/behavior-eval-health 暴露）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 设计纪律：
#   - 仅记录运行健康度（成功/失败/平均耗时），不缓存业务数据
#   - 进程内单例（重启即清零，正常）；如需跨进程聚合，由上层 Prometheus 抓
#   - reset_health_stats() 仅供测试 / 健康自检调用
_HEALTH_STATS: dict = {
    "total_calls": 0,            # evaluate_behavior 调用总次数
    "success_calls": 0,          # 主入口正常返回（含 lv 级失败）
    "input_skip_calls": 0,       # 因 atr<=0 / price<=0 跳过的调用
    "level_eval_total": 0,       # 单 level 评估总数
    "level_eval_success": 0,     # 单 level 评估成功数
    "level_eval_error": 0,       # 单 level 评估失败数（异常被吞）
    "last_error_msg": "",
    "last_error_ts": 0,
    "total_latency_ms": 0.0,     # evaluate_behavior 累计耗时
    "module_version": BEHAVIOR_EVAL_VERSION,
}


def get_health_stats() -> dict:
    """对外只读快照（GET /api/key-levels/behavior-eval-health 用）。"""
    s = dict(_HEALTH_STATS)
    if s["total_calls"] > 0:
        s["avg_latency_ms"] = round(s["total_latency_ms"] / s["total_calls"], 3)
    else:
        s["avg_latency_ms"] = 0.0
    if s["level_eval_total"] > 0:
        s["error_rate"] = round(s["level_eval_error"] / s["level_eval_total"], 4)
    else:
        s["error_rate"] = 0.0
    return s


def reset_health_stats() -> None:
    """重置健康统计（测试场景使用，生产场景不触发）。"""
    _HEALTH_STATS.update({
        "total_calls": 0, "success_calls": 0, "input_skip_calls": 0,
        "level_eval_total": 0, "level_eval_success": 0, "level_eval_error": 0,
        "last_error_msg": "", "last_error_ts": 0, "total_latency_ms": 0.0,
    })


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
    # V3-M4 P1-3：未拿到次根 K 收阳确认时，capitulation_bottom_score 的最高上限
    # （超过此值的会被 cap 到此值；意味着"候选观察"而非"高置信确认"）
    "cap_secondary_confirm_cap": 0.50,
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

    t_start = time.perf_counter()
    _HEALTH_STATS["total_calls"] += 1

    if snapshot.atr <= 0 or snapshot.current_price <= 0:
        # 数据不足：给所有 level 标记"未评估"，便于回测 / 前端区分（M3.1）
        _HEALTH_STATS["input_skip_calls"] += 1
        missing = []
        if snapshot.atr <= 0:
            missing.append("atr")
        if snapshot.current_price <= 0:
            missing.append("current_price")
        for lv in snapshot.levels:
            lv.behavior = BehaviorEval(
                behavior_state="pending",
                evaluated_at=now,
                behavior_eval_available=False,
                behavior_eval_version=BEHAVIOR_EVAL_VERSION,
                input_quality="missing",
                missing_inputs=list(missing),
            )
        _HEALTH_STATS["total_latency_ms"] += (time.perf_counter() - t_start) * 1000.0
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

    # 标记关键输入缺失情况（部分维度可降级，但完整性要透明）
    snap_input_quality, snap_missing = _assess_input_quality(
        candles_15m=candles_15m, candles_1h=candles_1h, cvd=cvd,
    )

    for lv in snapshot.levels:
        _HEALTH_STATS["level_eval_total"] += 1
        try:
            beh = _evaluate_one_level(lv, snapshot, ctx, cfg, now)
            # M3.1 元信息（成功路径）
            beh.behavior_eval_available = True
            beh.behavior_eval_version = BEHAVIOR_EVAL_VERSION
            beh.input_quality = snap_input_quality
            beh.missing_inputs = list(snap_missing)
            # M3.1 互斥校准（selloff vs capitulation）
            _calibrate_mutual_exclusion(beh)
            lv.behavior = beh
            _HEALTH_STATS["level_eval_success"] += 1
        except Exception as exc:
            logger.exception(
                "behavior_eval failed for level price=%.4f side=%s state=%s",
                lv.price, lv.side, lv.state,
            )
            _HEALTH_STATS["level_eval_error"] += 1
            err_msg = repr(exc)[:120]
            _HEALTH_STATS["last_error_msg"] = err_msg
            _HEALTH_STATS["last_error_ts"] = now
            lv.behavior = BehaviorEval(
                behavior_state="pending",
                evaluated_at=now,
                behavior_eval_available=False,
                behavior_eval_version=BEHAVIOR_EVAL_VERSION,
                input_quality=snap_input_quality,
                missing_inputs=list(snap_missing),
                evaluator_error=err_msg,
            )

    _HEALTH_STATS["success_calls"] += 1
    _HEALTH_STATS["total_latency_ms"] += (time.perf_counter() - t_start) * 1000.0


def _assess_input_quality(
    *,
    candles_15m: list[CandleData] | None,
    candles_1h: list[CandleData] | None,
    cvd: CVDData | None,
) -> tuple[str, list[str]]:
    """评估输入数据完整度（M3.1）。

    返回 ("ok"|"partial"|"missing", missing_inputs 列表)
      - ok：candles_15m 充足 + cvd 存在
      - partial：candles_15m 充足但缺 cvd / 或 candles_15m 不足但有兜底
      - missing：candles_15m 完全缺失（评估几乎不可信）

    注意：本函数仅做"输入侧元信息"标注，不影响实际评估流程。
    """
    missing: list[str] = []
    if not candles_15m or len(candles_15m) < 5:
        missing.append("candles_15m")
    if not candles_1h or len(candles_1h) < 3:
        missing.append("candles_1h")
    if cvd is None:
        missing.append("cvd")

    if "candles_15m" in missing:
        return "missing", missing
    if missing:
        return "partial", missing
    return "ok", missing


def _calibrate_mutual_exclusion(beh: BehaviorEval) -> None:
    """selloff_continuation_risk 与 capitulation_bottom_score 互斥校准（M3.1）。

    痛点（GPT 审查指出）：
      二者都在 broken & support 状态下计算，但语义对立：
        - selloff_continuation_risk 高 = "继续下行风险大"（看跌延续）
        - capitulation_bottom_score 高 = "可能恐慌见底"（看多反转候选）
      若两者同时 ≥ 0.65，下游 AI / 前端会读到矛盾信号。

    校准规则：
      - 仅当两个分数都很高（≥ 0.50）时介入
      - 让"较弱方"按差值衰减（弱者 *= 1 - 强者 × 0.4），保持总体可读性
      - 触发时在 explain_chips 增加白话备注（仅当 chips 还有空位）

    保守设计：
      - 不直接归零（保留两者各自数值供专家观察）
      - 互斥不影响 behavior_state（state 已经在 _evaluate_one_level 决定）
    """
    a = beh.selloff_continuation_risk
    b = beh.capitulation_bottom_score
    threshold = 0.50

    if a < threshold or b < threshold:
        return  # 至少一个低于阈值，不冲突

    if a >= b:
        beh.capitulation_bottom_score = round(b * (1.0 - a * 0.4), 4)
        winner = "selloff"
    else:
        beh.selloff_continuation_risk = round(a * (1.0 - b * 0.4), 4)
        winner = "capitulation"

    if len(beh.explain_chips) < _MAX_CHIPS:
        if winner == "selloff":
            beh.explain_chips.append("⚠ 卖压延续主导，弱化恐慌见底分")
        else:
            beh.explain_chips.append("⚠ 恐慌见底主导，弱化卖压延续分")


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

    # ── M2.5 · V2 双轨影子字段（不影响生产链路，仅记录） ──
    # 即使在 idle / approaching 等 M1 不评估 6 分的 state，也要计算 V2 影子，
    # 给前端"V1 vs V2 对比"和 M3 回测提供完整数据。
    e.bounce_quality_enhanced = compute_bounce_quality_v2(lv, ctx, cfg)
    e.breakout_stage_enhanced = compute_breakout_stage_v2(lv, snapshot.atr, ctx, cfg, now)
    e.fake_break_strength = assess_fake_break_strength(lv, ctx)
    e.dynamic_break_depth_pct = compute_dynamic_break_depth_pct(snapshot.atr, snapshot.current_price, cfg)

    # ── M2.5 · 冲突预警（state vs behavior_state 严重不一致时记入） ──
    # V3-M4 P1-4：reasons + severities 一一对应，severity ∈ low/medium/high
    contradictions, severities = _detect_contradictions(lv, e, cfg)
    e.contradiction_with_state = contradictions
    e.contradiction_severities = severities

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

    V3-M4 P1-3：二次确认门槛（GPT 审查采纳）：
      若「次根 K（最近一根已收盘 15m K）非阳线」（close <= open）→ score cap 在 0.50。
      逻辑：单根长下影只是"可能见底"的候选信号；只有下一根继续收阳才算市场确认。
      没有这个门槛会让"下跌中途长下影插针"被直接判 capitulation_flush，
      诱导 AI 给"做多入场"建议（已在 prompt §9g.1 显式禁止，但底层分数也要兜住）。
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
    score = _clamp01(score)

    # V3-M4 P1-3：二次确认门槛 — 次根 K 必须收阳才允许进入"high confidence"区
    # 检测的是"最近一根已收盘 K"（candles[-2]，因 candles[-1] 可能未收盘），
    # close > open 视为收阳；缺数据时按"未确认"处理（保守 cap）
    cap_threshold = float(cfg.get("cap_secondary_confirm_cap", 0.50))
    candles = ctx.get("candles_15m") or []
    secondary_confirmed = False
    if len(candles) >= 2:
        last_closed = candles[-2]
        if last_closed and last_closed.close > last_closed.open:
            secondary_confirmed = True

    if not secondary_confirmed and score > cap_threshold:
        return cap_threshold
    return score


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


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# M2.5 · V2 双轨增强函数（旧 4 个 tracker 函数的影子升级版）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# 设计纪律：
#   1. 这些函数只读 KeyLevelV2 + ctx，输出写入 lv.behavior 影子字段
#   2. 旧 _assess_bounce_quality / _assess_breakout_stage / _fake_break_reclaim
#      / _is_broken 在 tracker 中保持不变，生产链路 0 影响
#   3. M3 回测时分桶对比"V1 vs V2"准确率，数据驱动决定何时切换
#   4. 函数签名独立可单测；不依赖 tracker 内部状态
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def compute_bounce_quality_v2(lv: KeyLevelV2, ctx: dict, cfg: dict) -> float:
    """V2 反弹质量（0-1 连续）：替代旧死阈值 1.5x / 0.8x。

    旧 `_assess_bounce_quality` 的问题：
      - 死阈值 vol ≥ 1.5×均量 / vol < 0.8×均量 → BTC/ETH/SOL 共用，
        高波动期把正常反弹打成 proactive，盘整期把弱反弹打成 passive
    V2 改进：
      - 用 vol_zscore_recent + panic_volume_percentile 自适应基线
      - 输出 0-1 连续（前端可呈现进度条；M3 回测可分桶）

    返回：
      0.0 → 极弱（缩量到地量）
      0.3 → 偏弱
      0.5 → 中性
      0.7 → 偏强（放量但未极端）
      1.0 → 极强（高 z-score + 高 percentile）

    仅 lv.state == "bounced" 时输出 ≠ 0；其它状态返回 0.0。
    """
    if lv.state != "bounced":
        return 0.0

    last = ctx.get("last_bar_15m")
    if not last:
        return 0.0
    is_support = lv.side == "support"

    # 方向必须一致：支撑反弹应是阳线（close > open）
    direction_ok = (last.close > last.open) if is_support else (last.close < last.open)
    if not direction_ok:
        return 0.0  # 方向不对，谈不上"主动反弹"

    # 基础分：vol_ratio_recent 0-2 → 0-1
    vr = float(ctx.get("vol_ratio_recent", 0.0))
    base = min(vr / 2.0, 1.0) if vr > 0 else 0.0

    # 加成：z-score 方向（≥ 1.5 σ 算极端放量）
    z = float(ctx.get("vol_zscore_recent", 0.0))
    if z >= 2.0:
        base = min(base + 0.15, 1.0)
    elif z >= 1.0:
        base = min(base + 0.08, 1.0)
    elif z <= -1.0:
        base = max(base - 0.10, 0.0)

    # 加成：percentile 极端
    p = float(ctx.get("panic_volume_percentile", 0.0))
    if p >= 0.90:
        base = min(base + 0.10, 1.0)

    # 实体强度（非十字星）
    body_ratio = float(ctx.get("last_body_ratio", 0.0))
    if body_ratio >= 0.5:
        base = min(base + 0.05, 1.0)
    elif body_ratio < 0.2:
        base = max(base - 0.10, 0.0)  # 十字星反弹不可信

    return _clamp01(base)


# 时间框架到秒数的缩放系数（基线为 1H）
_TF_SCALE_SECONDS = {
    "15m": 0.25,
    "30m": 0.5,
    "1H":  1.0, "1h": 1.0,
    "2H":  2.0, "2h": 2.0,
    "4H":  4.0, "4h": 4.0,
    "12H": 12.0, "12h": 12.0,
    "1D":  24.0, "1d": 24.0,
    "3D":  72.0, "3d": 72.0,
    "1W":  168.0, "1w": 168.0,
}


def compute_breakout_stage_v2(
    lv: KeyLevelV2, atr: float, ctx: dict, cfg: dict, now: int,
) -> int:
    """V2 突破阶段（0/1/2/3）：替代旧固定时间窗。

    旧 `_assess_breakout_stage` 的问题：
      - 固定时间窗 stage1=900s / stage2=5400s（1.5h）
      - 1D/1W 级别永远走不到 stage 3（24h+ 才回踩 → 早过期）
      - 1D/1W 关键位 confidence 永远偏低，强位拿不到 A 级信号
    V2 改进：
      - 时间窗按 lv.timeframe 自适应缩放：
        * 15m：stage1=225s / stage2=1350s（0.25 倍）
        * 1H： stage1=900s / stage2=5400s （1 倍 = 旧默认）
        * 1D： stage1=21600s / stage2=129600s（24 倍 = 6h / 36h）
        * 1W： stage1=151200s / stage2=907200s（168 倍 ≈ 1.75d / 10.5d）

    严格只读：基于 lv.state / lv.state_ts / candles_15m，不修改 lv。
    """
    if lv.state not in ("broken", "flipped"):
        return 0
    if atr <= 0:
        return 0

    age = now - (lv.state_ts or 0)
    if age <= 0:
        return 0

    candles_15m = ctx.get("candles_15m") or []

    # 自适应缩放
    tf = (lv.timeframe or "1H").strip()
    scale = _TF_SCALE_SECONDS.get(tf, 1.0)
    base_stage1 = int(cfg.get("breakout_stage1_max_sec", 900))
    base_stage2 = int(cfg.get("breakout_retest_max_sec", 5400))
    base_expire = int(cfg.get("breakout_expire_sec", 21600))
    stage1_max = int(base_stage1 * scale)
    stage2_max = int(base_stage2 * scale)
    expire = int(base_expire * scale)

    if age > expire:
        return 0
    if age < stage1_max:
        return 1
    if not candles_15m:
        return 1

    # stage 2：是否出现回踩
    retest_tol = atr * float(cfg.get("breakout_retest_atr_mult", 0.5))
    confirm_atr = atr * float(cfg.get("breakout_confirm_atr_mult", 0.3))
    is_support = lv.side == "support"

    retest_idx = -1
    for idx, bar in enumerate(candles_15m):
        bar_age = now - bar.ts
        if bar_age <= 0 or bar_age > stage2_max:
            continue
        if bar.ts < (lv.state_ts or 0):
            continue
        if (
            abs(bar.high - lv.price) <= retest_tol
            or abs(bar.low - lv.price) <= retest_tol
        ):
            retest_idx = idx
            break

    if retest_idx == -1:
        return 1
    if retest_idx + 1 >= len(candles_15m):
        return 2

    # stage 3：回踩后的下一根 bar 反向继续推进 ≥ confirm_atr
    follow = candles_15m[retest_idx + 1]
    if is_support:
        if follow.close <= lv.price - confirm_atr:
            return 3
    else:
        if follow.close >= lv.price + confirm_atr:
            return 3
    return 2


def assess_fake_break_strength(lv: KeyLevelV2, ctx: dict) -> float:
    """V2 假突破回收强度（0-1 连续）：替代旧布尔事件。

    旧 `_fake_break_reclaim` 的问题：
      - 只看 close 是否回到未破侧 → 长下影回收 vs 实体震荡回收混淆
      - 长下影 = 真假突破反转更可信；实体 = 弱震荡不一定反转
    V2 改进：
      - 长下影 / 长上影占比 → 越长越好
      - 是否两根连续站回未破侧（双重确认）
      - 输出 0-1，让 signal_builder 能分档使用

    仅 lv.state == "fake_break" 或 lv.state == "broken" 时输出 ≠ 0；
    其它状态返回 0.0。
    """
    if lv.state not in ("fake_break", "broken"):
        return 0.0
    candles = ctx.get("candles_15m") or []
    if len(candles) < 2 or lv.price <= 0:
        return 0.0
    is_support = lv.side == "support"

    # 取最近一根已收盘 bar
    last_closed = candles[-2]
    if last_closed.close <= 0:
        return 0.0
    # 必须 close 已回到未破侧
    if is_support:
        if last_closed.close < lv.price:
            return 0.0
        wick_ratio = float(ctx.get("last_wick_lower_ratio", 0.0))  # 支撑：长下影
    else:
        if last_closed.close > lv.price:
            return 0.0
        wick_ratio = float(ctx.get("last_wick_upper_ratio", 0.0))  # 阻力：长上影

    # 基础分：影线占比
    if wick_ratio >= 0.6:
        base = 0.85
    elif wick_ratio >= 0.4:
        base = 0.65
    elif wick_ratio >= 0.25:
        base = 0.45
    else:
        base = 0.25  # 实体收回，弱信号

    # 加成：连续 2 根站回（last + prev_closed）
    if len(candles) >= 3:
        prev_closed = candles[-3]
        if is_support and prev_closed.close >= lv.price:
            base = min(base + 0.10, 1.0)
        elif (not is_support) and prev_closed.close <= lv.price:
            base = min(base + 0.10, 1.0)

    # 加成：fake_break_count 历史多次（防守强）
    fbc = lv.fake_break_count or 0
    if fbc >= 2:
        base = min(base + 0.05, 1.0)

    return _clamp01(base)


def compute_dynamic_break_depth_pct(atr: float, price: float, cfg: dict) -> float:
    """V2 动态破位阈值（百分比）：替代旧固定 0.3%。

    旧 `_is_broken` 用固定 `cfg["break_depth_pct"] = 0.3%` 的问题：
      - ATR 0.5%（盘整）：0.3% 偏松 → 噪声波动也判破
      - ATR 2%（高波动）：0.3% 偏严 → 实破还在 testing
    V2 改进：
      - depth_pct = max(cfg_pct, k × ATR%)，k 默认 0.3
      - 让破位事件触发率在不同 regime 下公平

    注：这是"建议值"，仅写入 lv.behavior.dynamic_break_depth_pct 用于展示与回测；
    旧 _is_broken 在 tracker 中**保持读 cfg["break_depth_pct"]**，不变。
    """
    cfg_pct = float(cfg.get("break_depth_pct", 0.3))
    if atr <= 0 or price <= 0:
        return cfg_pct
    atr_pct = atr / price * 100
    k = float(cfg.get("dynamic_break_depth_atr_mult", 0.3))
    return max(cfg_pct, k * atr_pct)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# M2.5 · 冲突预警派生（state vs behavior 严重不一致 → 写入 contradiction_with_state）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _detect_contradictions(
    lv: KeyLevelV2, e: BehaviorEval, cfg: dict,
) -> tuple[list[str], list[str]]:
    """检查旧 state 与新 behavior 是否极端不一致；返回 (reasons, severities)。

    设计纪律：
      - 仅"两者方向相反"才记入，避免噪声（轻微差异不算冲突）
      - 不写入 lv.contradiction_reasons（保持主路径清洁，那个用来记 V3 评分扣分原因）
      - 仅前端 / AI 可见，不影响信号生成
      - severities 与 reasons 同长，one-to-one 对应（low/medium/high）

    检测规则（V3-M4 P1-4 升级，含 GPT 审查补的 3 条强对立规则）：
      原 6 条（保留）：
      1. state==broken & breakout_validity<0.30 & false_break_risk≥0.65 [med]
         → "形态破位但量价未确认（可能假破）"
      2. state==broken & breakout_validity<0.30 [med]
         → "突破质量极弱，警惕假突破"
      3. state==bounced & retest_quality<0.30 [low]
         → "反弹生效但量能/深度不健康"
      4. state==flipped & flip_confirmation<0.30 [med]
         → "几何翻转但市场未确认"
      5. state ∈ {testing,bounced} & support & selloff_continuation_risk≥0.65 [high]
         → "支撑接触但破位延续风险高"
      6. V1/V2 bounce_quality 双轨方向背离 [low]

      新增 3 条（V3-M4 P1-4）：
      7. state==broken & support & capitulation_bottom_score≥0.70 [high]
         → "V1 判破位下行，V2 看到恐慌见底候选（方向严重对立）"
      8. state==fake_break & breakout_validity≥0.70 [high]
         → "V1 判假突破，V2 看真突破质量（方向严重对立）"
      9. strength_tier∈{S,A} & support & selloff_continuation_risk≥0.70 [high]
         → "强支撑（{tier}级）面临高破位延续风险"
    """
    out: list[str] = []
    sev: list[str] = []
    state = lv.state
    is_support = lv.side == "support"

    def _add(reason: str, severity: str) -> None:
        out.append(reason)
        sev.append(severity)

    # 规则 1-2：state=broken 但行为分极弱
    if state == "broken":
        if e.breakout_validity < 0.30 and e.false_break_risk >= 0.65:
            _add("形态破位但量价未确认（可能假破）", "medium")
        elif e.breakout_validity < 0.30:
            _add("突破质量极弱，警惕假突破", "medium")

    # 规则 3：state=bounced 但回踩质量差
    if state == "bounced" and e.retest_quality < 0.30:
        _add("反弹生效但量能/深度不健康", "low")

    # 规则 4：state=flipped 但翻转未确认
    if state == "flipped" and e.flip_confirmation < 0.30:
        _add("几何翻转但市场未确认", "medium")

    # 规则 5：testing/bounced 中支撑面临高延续下行
    if state in ("testing", "bounced") and is_support \
            and e.selloff_continuation_risk >= 0.65:
        _add("支撑接触但破位延续风险高", "high")

    # 规则 6：V1 vs V2 bounce_quality 双轨背离
    bq_v1 = lv.bounce_quality or ""
    bq_v2 = e.bounce_quality_enhanced
    if bq_v1 == "proactive" and bq_v2 < 0.35:
        _add("V1 标记主动反弹，V2 评估为被动（建议关注）", "low")
    elif bq_v1 == "passive" and bq_v2 > 0.65:
        _add("V1 标记被动反弹，V2 评估为主动（建议关注）", "low")

    # ── V3-M4 P1-4 新增 3 条强对立规则 ──
    # 规则 7：V1 判破位下行 vs V2 看见底候选（方向严重对立 → high）
    if state == "broken" and is_support and e.capitulation_bottom_score >= 0.70:
        _add("V1 判破位下行，V2 看到恐慌见底候选（方向严重对立）", "high")

    # 规则 8：V1 判假突破 vs V2 看真突破质量（方向严重对立 → high）
    if state == "fake_break" and e.breakout_validity >= 0.70:
        _add("V1 判假突破，V2 看真突破质量（方向严重对立）", "high")

    # 规则 9：强支撑（S/A 级）面临高破位延续风险（罕见 → high）
    if (lv.strength_tier or "") in ("S", "A") and is_support \
            and e.selloff_continuation_risk >= 0.70:
        _add(f"强支撑（{lv.strength_tier}级）面临高破位延续风险", "high")

    return out, sev
