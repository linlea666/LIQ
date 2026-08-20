"""W4-T1 阶段 4 · Sweep Watch 引擎（双向止损扫单观察 + 完整 trace 日志）。

设计：
  - 输入：BrainPriceZone 聚合层 + BrainEvent 市场事件 + ctx / atr / now_sec
  - 输出：BrainSweepWatch（below + above 双侧 SweepWatchSide + trace_log）
  - 不修改 BrainPriceZone，不引入新数据源（用户原则 5）
  - 全部派生计算 + 每个步骤注入 trace 记录

5 态机（全部用现有数据派生）：
  - waiting           : 距 > 1.5%
  - approaching       : 距 ≤ 1.5%，最近 5min 无 wall_consumed
  - in_sweep          : 最近 5min 有 wall_consumed OR 价格已穿破区间
  - swept_reclaiming  : in_sweep 后价格已回到区间内
  - swept_continuing  : in_sweep 后未收回，CVD 同向继续

3 派生分公式（below 侧用 support_*；above 侧用 resistance_*）：
  reversal_potential =
      0.40 × strength + 0.20 × (1 - fragility) + 0.20 × data_confidence
    + 0.20 × cvd_against_score
  continuation_risk =
      0.35 × break_through_risk + 0.25 × sweep_attractiveness
    + 0.20 × cvd_alignment_score + 0.10 × (1 - data_confidence) + 0.10 × fragility
  sweep_attractiveness：直接复用 zone.sweep_attractiveness

可观测性：每次调用产出 8-12 条 SweepWatchTraceEntry 供前端展示 + jsonl 后验。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from models.sweep_watch import (
    BrainSweepWatch,
    SweepPhase,
    SweepSide,
    SweepWatchSide,
    SweepWatchTraceEntry,
)
from models.trading_brain import (
    BrainContextChips,
    BrainEvent,
    BrainPriceZone,
)
from processors.cvd import normalize_trend


# ─────────────────────────────────────────────────────────────────────
# 常量（保守起见不暴露给配置层；如需调整在此集中修改）
# ─────────────────────────────────────────────────────────────────────
_APPROACHING_THRESHOLD_PCT = 1.5
"""approaching 阈值：距现价 ≤ 1.5% 视为接近止损带。"""

_IN_SWEEP_WINDOW_SEC = 300
"""in_sweep 窗口：最近 N 秒内有 wall_consumed 事件视为正在扫。"""

_STRONG_ROLES_FOR_REPRESENTATIVE: frozenset[str] = frozenset(
    {"spot_defense", "contested", "liquidation_magnet", "futures_target"}
)
"""选代表 zone 的强角色范围（与 PriceAxisMap 强角色保持一致）。"""


# ─────────────────────────────────────────────────────────────────────
# trace 工具
# ─────────────────────────────────────────────────────────────────────
def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class _TraceRecorder:
    """trace 收集器：每个 step 的 inputs/output/rule 结构化落地。"""

    def __init__(self, side: SweepSide):
        self.side = side
        self.entries: list[SweepWatchTraceEntry] = []
        self._ts = _now_iso()

    def emit(
        self,
        step: str,
        *,
        inputs: Optional[dict] = None,
        output=None,
        rule_hit: Optional[str] = None,
        notes: str = "",
    ) -> None:
        self.entries.append(SweepWatchTraceEntry(
            ts_iso=self._ts,
            side=self.side,
            step=step,
            inputs=inputs or {},
            output=output,
            rule_hit=rule_hit,
            notes=notes,
        ))


# ─────────────────────────────────────────────────────────────────────
# CVD 对齐打分（below 侧 = 关心是否还在跌；above 侧 = 关心是否还在涨）
# ─────────────────────────────────────────────────────────────────────
def _trend_score_falling(trend: str) -> float:
    """trend 描述跌势的强度 [0-1]：1 = 强跌；0 = 强涨。

    枚举值（rising/declining/flat 及历史别名）统一走 normalize_trend，
    避免枚举断裂（生产端输出 declining，此前本地只认 falling 导致
    CVD 项恒为 0.5）；仅对中文描述串保留关键词兜底。
    """
    normalized = normalize_trend(trend)
    if normalized == "up":
        return 0.0
    if normalized == "down":
        return 1.0
    if normalized == "flat":
        return 0.5
    t = (trend or "").lower()
    if any(k in t for k in ("rising", "上涨", "净买", "buy")):
        return 0.0
    if any(k in t for k in ("falling", "下跌", "净卖", "sell")):
        return 1.0
    return 0.5  # 持平 / 未知


def _cvd_alignment_score(side: SweepSide, ctx: Optional[BrainContextChips]) -> float:
    """CVD 是否同向破位（高 → 延续风险加大）。

    below 侧（向下扫破）：CVD 越下跌 → 越同向 → 1
    above 侧（向上突破）：CVD 越上涨 → 越同向 → 1
    """
    if ctx is None:
        return 0.5
    fut = _trend_score_falling(ctx.cvd_contract_trend or "")
    spot = _trend_score_falling(ctx.cvd_spot_trend or "")
    falling_score = 0.5 * fut + 0.5 * spot  # 跌势综合分
    if side == "below":
        return falling_score
    # above 侧：上涨同向 = 1 - falling_score
    return 1.0 - falling_score


def _cvd_against_score(side: SweepSide, ctx: Optional[BrainContextChips]) -> float:
    """CVD 是否反向（高 → 反转潜力加大）。与 alignment 互补。"""
    return 1.0 - _cvd_alignment_score(side, ctx)


# ─────────────────────────────────────────────────────────────────────
# 代表 zone 选择
# ─────────────────────────────────────────────────────────────────────
def _select_representative(
    side: SweepSide,
    zones: list[BrainPriceZone],
    trace: _TraceRecorder,
    previous: Optional[SweepWatchSide] = None,
) -> Optional[BrainPriceZone]:
    """选该侧的代表 zone（桶式排序）。

    缺口 1 修复（P1-A）：原实现只按 |distance_pct| 升序，会让"次近但更招扫"的
    zone 被距离更近但 SA 很低的 zone 屏蔽。修复后采用三桶排序：

      桶 0  |d| ≤ 1.5%（approaching 范围）：SA 优先 → 距离次之
            理由：用户最关心"近端最招扫的那条带"；同距离段内 SA 才决定先后。
      桶 1  1.5% < |d| ≤ 3.0%：距离优先 → SA 次之
            理由：中距离仍以"哪个先被扫到"为主，SA 仅作 tie-breaker。
      桶 2  |d| > 3.0%：距离优先 → SA 次之
            同桶 1，远端始终保持"近的赢"。

    保留"代表 = 当前最相关的那条带"语义：远端 SA 极高的 zone 不会抢走近端
    桶 0 的代表位（避免代表区跳到 ≥ 3% 之外失去过程态意义）。
    """
    if previous is not None and previous.sweep_phase != "waiting":
        retained = next(
            (z for z in zones if z.zone_id == previous.representative_zone_id), None,
        )
        if retained is not None:
            trace.emit(
                step="select_representative",
                inputs={
                    "side": side,
                    "previous_zone_id": previous.representative_zone_id,
                    "previous_phase": previous.sweep_phase,
                },
                output={
                    "zone_id": retained.zone_id,
                    "distance_pct": round(retained.distance_pct, 3),
                },
                rule_hit="retain_stable_zone_identity",
                notes="过程态未结束，跨过现价后仍保留原 zone 身份",
            )
            return retained

    if side == "below":
        candidates = [z for z in zones if z.distance_pct < 0]
    else:
        candidates = [z for z in zones if z.distance_pct > 0]
    strong = [z for z in candidates if z.dominant_role in _STRONG_ROLES_FOR_REPRESENTATIVE]

    def _bucket_sort_key(z: BrainPriceZone) -> tuple[int, float, float]:
        d = abs(z.distance_pct)
        sa = z.sweep_attractiveness or 0.0
        if d <= _APPROACHING_THRESHOLD_PCT:
            return (0, -sa, d)
        if d <= 3.0:
            return (1, d, -sa)
        return (2, d, -sa)

    strong.sort(key=_bucket_sort_key)
    repr_zone = strong[0] if strong else None

    if repr_zone is None:
        rule_hit = "no_strong_zone"
        notes = "该侧无强角色 zone，将返回 None（前端隐藏该栏）"
    else:
        d_abs = abs(repr_zone.distance_pct)
        if d_abs <= _APPROACHING_THRESHOLD_PCT:
            rule_hit = "near_bucket_high_attractiveness"
            notes = (
                f"近桶（|d|≤{_APPROACHING_THRESHOLD_PCT}%）：SA 优先 → 命中 {repr_zone.zone_id}"
                f" (SA={repr_zone.sweep_attractiveness:.2f})"
            )
        elif d_abs <= 3.0:
            rule_hit = "mid_bucket_closest_distance"
            notes = f"中桶（1.5%<|d|≤3%）：距离优先 → 命中 {repr_zone.zone_id}"
        else:
            rule_hit = "far_bucket_closest_distance"
            notes = f"远桶（|d|>3%）：距离优先 → 命中 {repr_zone.zone_id}"

    trace.emit(
        step="select_representative",
        inputs={
            "side": side,
            "candidate_count": len(candidates),
            "strong_role_count": len(strong),
            "strong_role_filter": sorted(_STRONG_ROLES_FOR_REPRESENTATIVE),
            "approaching_threshold_pct": _APPROACHING_THRESHOLD_PCT,
            "near_bucket_priority": "sweep_attractiveness DESC, distance ASC",
            "far_bucket_priority": "distance ASC, sweep_attractiveness DESC",
        },
        output=(
            {
                "zone_id": repr_zone.zone_id,
                "dominant_role": repr_zone.dominant_role,
                "distance_pct": round(repr_zone.distance_pct, 3),
                "sweep_attractiveness": round(repr_zone.sweep_attractiveness, 3),
                "label": repr_zone.dominant_label,
            }
            if repr_zone
            else None
        ),
        rule_hit=rule_hit,
        notes=notes,
    )
    return repr_zone


# ─────────────────────────────────────────────────────────────────────
# 5 态机
# ─────────────────────────────────────────────────────────────────────
def _is_recent_wall_consumed(
    zone_ids: list[str], events: list[BrainEvent], now_sec: int,
) -> tuple[bool, Optional[BrainEvent]]:
    """zone 是否在最近 N 秒内被 wall_consumed。"""
    zone_id_set = set(zone_ids or [])
    for ev in events:
        if ev.zone_id and ev.zone_id in zone_id_set:
            if (now_sec - ev.ts) <= _IN_SWEEP_WINDOW_SEC and "消耗" in ev.message:
                return True, ev
    return False, None


def _is_price_pierced(
    side: SweepSide, zone: BrainPriceZone, last_price: float,
) -> bool:
    """价格是否穿破 zone 区间。"""
    if side == "below":
        return last_price < zone.price_low
    return last_price > zone.price_high


def _is_price_reclaimed(zone: BrainPriceZone, last_price: float) -> bool:
    """价格是否已回到 zone 区间之内。"""
    return zone.price_low <= last_price <= zone.price_high


def _decide_phase(
    side: SweepSide,
    zone: BrainPriceZone,
    last_price: float,
    events: list[BrainEvent],
    ctx: Optional[BrainContextChips],
    now_sec: int,
    trace: _TraceRecorder,
    previous: Optional[SweepWatchSide] = None,
) -> SweepPhase:
    distance_abs = abs(zone.distance_pct)
    consumed, ev = _is_recent_wall_consumed(zone.wall_zone_ids, events, now_sec)
    pierced = _is_price_pierced(side, zone, last_price)
    reclaimed = _is_price_reclaimed(zone, last_price)
    cvd_alignment = _cvd_alignment_score(side, ctx)

    # 5 态机判定（与文档语义严格对齐）：
    #   - 触发态（consumed or pierced） + reclaimed     → swept_reclaiming
    #   - 触发态 + CVD 同向（alignment ≥ 0.6）          → swept_continuing
    #   - 触发态 + CVD 中性/反向                        → in_sweep（等结构反应；不预判延续）
    #   - 非触发态 + 距离 ≤ 1.5%                        → approaching
    #   - 否则                                           → waiting
    was_triggered = bool(
        previous and previous.sweep_phase in {
            "in_sweep", "swept_reclaiming", "swept_continuing",
        }
    )
    in_sweep_trigger = consumed or pierced or was_triggered
    if in_sweep_trigger:
        if reclaimed:
            phase: SweepPhase = "swept_reclaiming"
            rule = "swept_with_reclaim"
        elif cvd_alignment >= 0.6:
            phase = "swept_continuing"
            rule = "swept_with_cvd_alignment_high"
        else:
            # 已扫但未收回 + CVD 中性/反向：仍归 in_sweep（保守，等结构表达）
            phase = "in_sweep"
            rule = "in_sweep_no_strong_continuation"
    elif distance_abs <= _APPROACHING_THRESHOLD_PCT:
        phase = "approaching"
        rule = "distance_within_1_5pct"
    else:
        phase = "waiting"
        rule = "distance_far"

    trace.emit(
        step="phase_decision",
        inputs={
            "distance_pct": round(zone.distance_pct, 3),
            "approaching_threshold_pct": _APPROACHING_THRESHOLD_PCT,
            "wall_consumed_within_5min": consumed,
            "consumed_event_zone_id": ev.zone_id if ev else None,
            # 时钟回滚 / 异步事件可能让 ev.ts > now_sec，clamp 到 0 避免 trace 显示负值
            "consumed_event_age_sec": max(0, now_sec - ev.ts) if ev else None,
            "price_pierced": pierced,
            "price_reclaimed": reclaimed,
            "previous_phase": previous.sweep_phase if previous else None,
            "cvd_alignment_score": round(cvd_alignment, 3),
        },
        output=phase,
        rule_hit=rule,
        notes=f"5 态机判定 → {phase}",
    )
    return phase


# ─────────────────────────────────────────────────────────────────────
# 3 派生分
# ─────────────────────────────────────────────────────────────────────
def _strength_fragility_for_side(
    side: SweepSide, zone: BrainPriceZone,
) -> tuple[float, float]:
    """below 侧用 support_*；above 侧用 resistance_*。"""
    if side == "below":
        return zone.support_strength, zone.support_fragility
    return zone.resistance_strength, zone.resistance_fragility


def _calc_reversal_potential(
    side: SweepSide,
    zone: BrainPriceZone,
    ctx: Optional[BrainContextChips],
    trace: _TraceRecorder,
) -> float:
    """反转潜力 [0-1]：扫单后价格能反弹的可能性。"""
    strength, fragility = _strength_fragility_for_side(side, zone)
    cvd_against = _cvd_against_score(side, ctx)
    contrib_strength = 0.40 * strength
    contrib_fragility = 0.20 * (1.0 - fragility)
    contrib_data = 0.20 * zone.data_confidence
    contrib_cvd = 0.20 * cvd_against
    rp = contrib_strength + contrib_fragility + contrib_data + contrib_cvd
    rp = max(0.0, min(1.0, rp))
    trace.emit(
        step="reversal_potential",
        inputs={
            "strength": round(strength, 3),
            "fragility": round(fragility, 3),
            "data_confidence": round(zone.data_confidence, 3),
            "cvd_against_score": round(cvd_against, 3),
            "weights": {
                "strength": 0.40,
                "1_minus_fragility": 0.20,
                "data_confidence": 0.20,
                "cvd_against": 0.20,
            },
            "contributions": {
                "strength": round(contrib_strength, 3),
                "fragility_inv": round(contrib_fragility, 3),
                "data_confidence": round(contrib_data, 3),
                "cvd_against": round(contrib_cvd, 3),
            },
        },
        output=round(rp, 3),
        rule_hit=None,
        notes=(
            f"反转潜力 = 0.40×{strength:.2f} + 0.20×{1 - fragility:.2f} + "
            f"0.20×{zone.data_confidence:.2f} + 0.20×{cvd_against:.2f} = {rp:.2f}"
        ),
    )
    return round(rp, 3)


def _calc_continuation_risk(
    side: SweepSide,
    zone: BrainPriceZone,
    ctx: Optional[BrainContextChips],
    trace: _TraceRecorder,
) -> float:
    """延续风险 [0-1]：扫破后继续级联的可能性。"""
    strength, fragility = _strength_fragility_for_side(side, zone)
    cvd_alignment = _cvd_alignment_score(side, ctx)
    contrib_btr = 0.35 * zone.break_through_risk
    contrib_sa = 0.25 * zone.sweep_attractiveness
    contrib_cvd = 0.20 * cvd_alignment
    contrib_data = 0.10 * (1.0 - zone.data_confidence)
    contrib_frag = 0.10 * fragility
    cr = contrib_btr + contrib_sa + contrib_cvd + contrib_data + contrib_frag
    cr = max(0.0, min(1.0, cr))
    trace.emit(
        step="continuation_risk",
        inputs={
            "break_through_risk": round(zone.break_through_risk, 3),
            "sweep_attractiveness": round(zone.sweep_attractiveness, 3),
            "cvd_alignment_score": round(cvd_alignment, 3),
            "data_confidence": round(zone.data_confidence, 3),
            "fragility": round(fragility, 3),
            "weights": {
                "break_through_risk": 0.35,
                "sweep_attractiveness": 0.25,
                "cvd_alignment": 0.20,
                "1_minus_data_confidence": 0.10,
                "fragility": 0.10,
            },
            "contributions": {
                "break_through_risk": round(contrib_btr, 3),
                "sweep_attractiveness": round(contrib_sa, 3),
                "cvd_alignment": round(contrib_cvd, 3),
                "data_inv": round(contrib_data, 3),
                "fragility": round(contrib_frag, 3),
            },
        },
        output=round(cr, 3),
        rule_hit=None,
        notes=(
            f"延续风险 = 0.35×{zone.break_through_risk:.2f} + "
            f"0.25×{zone.sweep_attractiveness:.2f} + 0.20×{cvd_alignment:.2f} + "
            f"0.10×{1 - zone.data_confidence:.2f} + 0.10×{fragility:.2f} = {cr:.2f}"
        ),
    )
    return round(cr, 3)


# ─────────────────────────────────────────────────────────────────────
# 触发观察 / 失效条件（基于 phase 模板化）
# ─────────────────────────────────────────────────────────────────────
def _build_triggers_invalidations(
    side: SweepSide,
    phase: SweepPhase,
    zone: BrainPriceZone,
    trace: _TraceRecorder,
) -> tuple[list[str], list[str]]:
    triggers: list[str] = []
    invalidations: list[str] = []

    band_lo = round(zone.price_low, 2)
    band_hi = round(zone.price_high, 2)

    if side == "below":
        if phase == "waiting":
            # 缺口 5：waiting 距 > 1.5%，关注的是"会不会接近"，不是"扫到怎样"
            triggers = [
                f"价格是否向 {band_lo}-{band_hi} 区间持续靠近",
                "下方清算磁铁(下) 距离是否缩短（顶栏磁铁）",
                "现货 CVD 是否持续走弱（顶栏现货 CVD 创新低）",
            ]
            invalidations = [
                "下方清算磁铁完全失守（穿到磁铁之下，扫单目标转移）",
                "现货 CVD 持续走强 + OI 1H 转负（下扫动机消失）",
            ]
        elif phase == "approaching":
            triggers = [
                f"价格跌破 {band_lo} 后 5min 内是否快速收回",
                "现货买墙是否未撤（关注 ZoneDetailCard 双源 / Coinbase 共振 chip）",
                "现货 CVD 是否衰竭（顶栏现货 CVD 不再创新低）",
            ]
            invalidations = [
                f"无法收回 {band_lo}（持续 ≥ 15min 仍在区间外）",
                "下方清算磁铁继续增强（顶栏磁铁(下) 距离缩短）",
            ]
        elif phase == "in_sweep":
            triggers = [
                "等待结构反应：5min 内能否收回区间",
                "观察现货墙是否被一次性吃穿（衡量买盘吸收力）",
                "active_attack_score 是否快速回落",
            ]
            invalidations = [
                "5min 后仍在区间外 + 现货买墙撤出",
                "合约 CVD 继续净卖出加速",
            ]
        elif phase == "swept_reclaiming":
            triggers = [
                f"价格站稳 {band_lo}-{band_hi} 区间内 ≥ 10min",
                "支撑信任 chip 出现 ★ 单笔大额 / 双源 / Coinbase 共振",
                "现货 CVD 衰竭信号确认（不再创新低）",
            ]
            invalidations = [
                "再次跌破 band_lo 且更深（结构二次失败）",
                "现货买墙在收回过程中撤出",
            ]
        else:  # swept_continuing
            triggers = [
                "等下一个支撑 zone 形成（关注 PriceAxisMap 下方强角色）",
                "等 CVD 衰竭信号（合约 CVD 由净卖出转持平）",
                "等 OI 释放（顶栏 OI 1H 由正转负）",
            ]
            invalidations = [
                "下方清算磁铁完全失守（价格穿到磁铁之下）",
                "无新支撑 zone 形成 → 进入下一级搜寻",
            ]
    else:  # above
        if phase == "waiting":
            # 缺口 5：waiting 距 > 1.5%，关注的是"会不会接近"，不是"突破怎样"
            triggers = [
                f"价格是否向 {band_lo}-{band_hi} 区间持续靠近",
                "上方清算磁铁(上) 距离是否缩短（顶栏磁铁）",
                "现货 CVD 是否持续走强（顶栏现货 CVD 创新高）",
            ]
            invalidations = [
                "上方清算磁铁完全突破（穿到磁铁之上，扫单目标转移）",
                "现货 CVD 持续走弱 + OI 1H 转负（上扫动机消失）",
            ]
        elif phase == "approaching":
            triggers = [
                f"价格突破 {band_hi} 后 5min 内是否快速跌回",
                "现货卖墙是否未撤（关注 ZoneDetailCard 双源 / Coinbase 共振 chip）",
                "现货 CVD 是否衰竭（顶栏现货 CVD 不再创新高）",
            ]
            invalidations = [
                f"突破后站稳 {band_hi} 之上（持续 ≥ 15min）",
                "上方清算磁铁继续增强（顶栏磁铁(上) 距离缩短）",
            ]
        elif phase == "in_sweep":
            triggers = [
                "等待结构反应：5min 内能否跌回区间",
                "观察现货墙是否被一次性吃穿（衡量卖盘吸收力）",
                "active_attack_score 是否快速回落",
            ]
            invalidations = [
                "5min 后仍在区间之上 + 现货卖墙撤出",
                "合约 CVD 继续净买入加速",
            ]
        elif phase == "swept_reclaiming":
            triggers = [
                f"价格站稳 {band_lo}-{band_hi} 区间内 ≥ 10min",
                "阻力信任 chip 出现 ★ 单笔大额 / 双源 / Coinbase 共振",
                "现货 CVD 衰竭信号确认（不再创新高）",
            ]
            invalidations = [
                "再次突破 band_hi 且更高（结构二次失败）",
                "现货卖墙在跌回过程中撤出",
            ]
        else:  # swept_continuing
            triggers = [
                "等下一个阻力 zone 形成（关注 PriceAxisMap 上方强角色）",
                "等 CVD 衰竭信号（合约 CVD 由净买入转持平）",
                "等 OI 释放（顶栏 OI 1H 由正转负）",
            ]
            invalidations = [
                "上方清算磁铁完全突破（价格升到磁铁之上）",
                "无新阻力 zone 形成 → 进入下一级搜寻",
            ]

    triggers = triggers[:3]
    invalidations = invalidations[:2]
    trace.emit(
        step="triggers_invalidations",
        inputs={"side": side, "phase": phase},
        output={
            "triggers_count": len(triggers),
            "invalidations_count": len(invalidations),
        },
        rule_hit=f"template_{side}_{phase}",
        notes=f"按 ({side}, {phase}) 选模板，输出 ≤3 触发 + ≤2 失效",
    )
    return triggers, invalidations


# ─────────────────────────────────────────────────────────────────────
# 单侧 build
# ─────────────────────────────────────────────────────────────────────
def _build_side(
    side: SweepSide,
    zones: list[BrainPriceZone],
    last_price: float,
    events: list[BrainEvent],
    ctx: Optional[BrainContextChips],
    now_sec: int,
    previous: Optional[SweepWatchSide] = None,
) -> tuple[Optional[SweepWatchSide], list[SweepWatchTraceEntry]]:
    trace = _TraceRecorder(side=side)
    repr_zone = _select_representative(side, zones, trace, previous)
    if repr_zone is None:
        return None, trace.entries

    phase = _decide_phase(
        side, repr_zone, last_price, events, ctx, now_sec, trace, previous,
    )
    sweep_attractiveness = round(repr_zone.sweep_attractiveness, 3)
    trace.emit(
        step="sweep_attractiveness",
        inputs={"zone_id": repr_zone.zone_id},
        output=sweep_attractiveness,
        rule_hit="reuse_zone_field",
        notes="直接复用 BrainPriceZone.sweep_attractiveness（不重打分）",
    )
    reversal = _calc_reversal_potential(side, repr_zone, ctx, trace)
    continuation = _calc_continuation_risk(side, repr_zone, ctx, trace)
    triggers, invalidations = _build_triggers_invalidations(side, phase, repr_zone, trace)

    label = "下方多头止损带" if side == "below" else "上方空头止损带"
    side_obj = SweepWatchSide(
        direction=side,
        label=label,
        representative_zone_id=repr_zone.zone_id,
        representative_zone_label=repr_zone.dominant_label or "",
        price_band=(round(repr_zone.price_low, 2), round(repr_zone.price_high, 2)),
        distance_pct=round(repr_zone.distance_pct, 3),
        sweep_phase=phase,
        sweep_attractiveness=sweep_attractiveness,
        reversal_potential=reversal,
        continuation_risk=continuation,
        triggers=triggers,
        invalidations=invalidations,
    )
    return side_obj, trace.entries


# ─────────────────────────────────────────────────────────────────────
# 主入口
# ─────────────────────────────────────────────────────────────────────
def build_sweep_watch(
    *,
    coin: str,
    last_price: float,
    zones: list[BrainPriceZone],
    events: list[BrainEvent],
    ctx: Optional[BrainContextChips] = None,
    now_sec: int,
    previous: Optional[BrainSweepWatch] = None,
) -> BrainSweepWatch:
    """构建双侧 SweepWatch 主聚合对象 + trace 日志。

    输入完全来自 BrainSnapshot 已聚合的 zones + events + ctx，不读底层 WallZone。
    """
    below_obj, below_trace = _build_side(
        "below", zones, last_price, events, ctx, now_sec,
        previous.below if previous else None,
    )
    above_obj, above_trace = _build_side(
        "above", zones, last_price, events, ctx, now_sec,
        previous.above if previous else None,
    )
    return BrainSweepWatch(
        coin=coin,
        last_price=last_price,
        ts_iso=_now_iso(),
        below=below_obj,
        above=above_obj,
        trace_log=[*below_trace, *above_trace],
    )
