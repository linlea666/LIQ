"""关键位生命周期追踪器 V2：ATR 自适应 + 量价突破确认 + 智能 R:R

状态流转：
  IDLE → APPROACHING → TESTING → SWEPT/BOUNCED → (BROKEN → FAKE_BREAK | FLIPPED)

V2 改进：
  - 所有阈值根据 ATR / price 动态缩放
  - 突破确认同时检查成交量放大 + OI 变化方向
  - TP 指向对侧最近关键位或 VP POC（而非固定 ATR 倍数）
  - 信号过期：swept/bounced 超过 4H 自动降级
  - 冲突解决：同时多空 A 级信号时参考温度计

防扫损增强（本轮）：
  1. fake_break 状态：broken 后若 15m close 回到原 level 未破侧，切到 fake_break
     并产出反向 A 级 snipe 信号
  2. breakout_stage 驱动：broken 分支按 stage 分流 wait / B / A
  3. bounce_quality=passive 降级：缩量反弹降一档
  4. Z · MTF 1h 一致性：1h 与 level 方向同/异向加/扣分
  5. V · CVD 背离确认：CVD 方向与信号方向同 / 异给加 / 减分
  6. 置信度透明化：每张信号带 confirmations / signal_kind / score 0-100
"""

from __future__ import annotations

import logging
import time

from models.flow import CVDData
from models.key_level import (
    CascadeComponents,
    KeyLevelSignal,
    KeyLevelSnapshotV2,
    KeyLevelV2,
    LifecycleEvent,
)
from models.liquidation import LiquidationMap
from models.market import CandleData
from models.orderbook_pressure import OrderbookPressureSnapshot, WallZone
from processors.candlestick_patterns import PatternResult, detect_reversal_pattern
from processors.level_discovery import fmt_usd_cn

logger = logging.getLogger(__name__)

_SIDE_CN = {"support": "支撑", "resistance": "阻力"}

_DEFAULT_CFG: dict = {
    "approach_pct": 2.0,
    "test_pct": 0.5,
    "bounce_pct": 1.0,
    "break_confirm_sec": 300,
    "break_depth_pct": 0.3,
    # P1-1 · 收盘价确认开关：testing/swept → broken 必须等一根已收盘 15m
    # bar 的 close 真正突破过 level ± break_depth_pct。开启后可过滤掉
    # tick 扫动 + 秒级假突破，从根本消灭"拔毛行情"误触发。
    # 配置为 False 时行为与 V2 原版一致（只看 tick price），便于灰度。
    "break_require_closed_bar": True,
    "flip_zone_pct": 0.5,
    "level_expire_sec": 86400,
    "sweep_proximity_pct": 2.0,
    "cascade_weight_cap_m": 20.0,
    # cascade_norm 作为 risk_score 的归一化分母：值越大 → cascade_risk 越小
    # 经验校准：120.0 下多数 BTC 关键位的 cascade_risk 落在 0-0.6 区间（健康范围）
    # 用户可通过 config.yaml 覆盖（历史值 600 也有效，会进一步降低 risk 数值）
    "cascade_norm": 120.0,
    "signal_expire_sec": 14400,  # 4H
    # ── Commit 4：质量标注（博主方法论）──
    "bounce_vol_proactive_mult": 1.5,   # 主动吸筹的量能倍率（>= 近20根均量的 1.5×）
    "bounce_vol_passive_mult": 0.8,     # 被动触发的量能倍率（< 近20根均量的 0.8×）
    "breakout_stage1_max_sec": 900,     # stage1 窗口：破位 15m 内
    "breakout_retest_max_sec": 5400,    # stage2 窗口：破位后 90m 内出现回踩
    "breakout_retest_atr_mult": 0.5,    # 回踩判定：距 level ±0.5×ATR
    "breakout_confirm_atr_mult": 0.3,   # stage3 确认：反向推进 ≥ 0.3×ATR
    "breakout_expire_sec": 21600,       # 6h 后 stage 重置为 0
    # ── Scalp 日内极小止损档参数（仅 S/A 级关键位 + 15m 影线确认时生效）──
    "scalp_max_distance_pct": 0.8,    # 关键位与现价最大偏离
    "scalp_sl_min_pct": 0.2,          # 止损下限（价格百分比）
    "scalp_sl_max_atr_mult": 0.5,     # 止损上限（ATR 倍数）
    "scalp_tp_min_atr_mult": 0.5,     # TP 距 price 的最小 ATR 倍数（传给 _find_opposite_target）
    "scalp_min_rr": 1.5,              # 最小 R:R
    "scalp_max_cascade": 0.5,         # 级联风险上限
    "scalp_min_pattern_strength": 0.6, # 15m 反转形态最小强度（pin bar=0.85 / engulf=0.80 / doji=0.50）
    "scalp_signal_expire_sec": 1800,  # 关键位进入可 scalp 状态后的最长窗口（30 分钟）
    # ── 假突破反转（fake_break）──
    "fake_break_expire_sec": 7200,    # fake_break 持续超过 2h 无后续 → 回 idle
}


# 评分映射：base(confidence) + 每项 confirmation + 惩罚每条 warning
_BASE_SCORE = {"A": 80, "B": 60, "C": 40}
_CONFIRMATION_BONUS = 4   # 每项 +4
_CONFIRMATION_CAP = 5     # 最多计 5 项（+20 封顶）
_WARNING_PENALTY = 3      # 每条 warning -3


def _compute_score(sig: KeyLevelSignal) -> int:
    """透明的置信度评分公式：base + 确认项加分 - warning 扣分，clamp [0,100]。"""
    base = _BASE_SCORE.get(sig.confidence, 40)
    bonus = min(_CONFIRMATION_CAP, len(sig.confirmations)) * _CONFIRMATION_BONUS
    penalty = len(sig.warnings) * _WARNING_PENALTY
    return max(0, min(100, base + bonus - penalty))


def _finalize_signal(sig: KeyLevelSignal | None, kind: str) -> KeyLevelSignal | None:
    """给信号加 signal_kind 和 score。confirmations 由各分支自行填入。"""
    if sig is None:
        return None
    if not sig.signal_kind:
        sig.signal_kind = kind
    sig.score = _compute_score(sig)
    return sig


def run_tracker_v2(
    snapshot: KeyLevelSnapshotV2,
    liq_map: LiquidationMap | None,
    sweep_events_1h: list[dict],
    taker_buy_vol: float = 0,
    taker_sell_vol: float = 0,
    oi_change_pct_1h: float = 0,
    *,
    liq_map_7d: LiquidationMap | None = None,  # V3-P2-10：cascade magnet 7d 回退源
    temperature_score: float = 50,
    candles_4h: list[CandleData] | None = None,
    candles_15m: list[CandleData] | None = None,
    candles_1h: list[CandleData] | None = None,
    cvd: CVDData | None = None,
    pressure_snapshot: OrderbookPressureSnapshot | None = None,
    cfg: dict | None = None,
) -> KeyLevelSnapshotV2:
    """在 confluence_scoring 产出的快照基础上，运行状态机 + 信号生成。

    candles_1h · cvd · pressure_snapshot 均为可选（防御式编程，测试与旧调用无痛兼容）：
      - candles_1h 缺失     → MTF 确认跳过（不加减分）
      - cvd 缺失           → CVD 确认跳过（不加减分）
      - pressure_snapshot 缺失 → 挂单压力共振/警告跳过（不加减分）
    """
    cfg = {**_DEFAULT_CFG, **(cfg or {})}
    now = int(time.time())
    price = snapshot.current_price
    atr = snapshot.atr

    if price <= 0 or atr <= 0:
        return snapshot

    atr_factor = _calc_atr_factor(atr, price)

    # 状态机转移
    for lv in snapshot.levels:
        _update_distance(lv, price)
        _transition(lv, price, atr, atr_factor, sweep_events_1h,
                    taker_buy_vol, taker_sell_vol, oi_change_pct_1h,
                    candles_15m, cfg, now)

    # 级联风险（V3-P2-10：1d 簇为空时回退到 7d 计算 magnet）
    if liq_map or liq_map_7d:
        for lv in snapshot.levels:
            _calc_cascade_risk(
                lv, liq_map, price, cfg,
                liq_map_7d=liq_map_7d,
            )

    # ── Commit 4：质量标注（在信号生成前，让信号也能读到 stage/quality）──
    for lv in snapshot.levels:
        lv.bounce_quality = _assess_bounce_quality(lv, candles_15m, cfg)
        lv.breakout_stage = _assess_breakout_stage(lv, atr, candles_15m, cfg, now)

    # 信号生成
    signals: list[KeyLevelSignal] = []
    opposite_levels = snapshot.levels
    for lv in snapshot.levels:
        sig = _generate_signal(
            lv, price, atr, opposite_levels, temperature_score,
            candles_4h, cfg, now,
            candles_1h=candles_1h, cvd=cvd,
        )
        if sig:
            signals.append(sig)
        # ── Scalp 日内极小止损档：仅 S/A 级 + 15m 影线确认时叠加产出，与上方信号并存 ──
        scalp = _generate_scalp_signal(lv, price, atr, opposite_levels, candles_15m, cfg, now, cvd=cvd)
        if scalp:
            signals.append(scalp)

    # ── 挂单压力共振/警告（OP snapshot 已在本轮 _recompute 前置阶段算好）──
    # 行为：对每条 KL 信号叠加 OP confirmation/warning，必要时降级；不影响信号本身的产出。
    if pressure_snapshot is not None and signals:
        _apply_pressure_alignment(signals, pressure_snapshot, atr)

    # 冲突解决：同时有多空 A 级时，按温度计方向保留主信号（涵盖 snipe/flip/scalp）
    _LONG_ACTIONS = ("snipe_long", "flip_long", "scalp_long")
    _SHORT_ACTIONS = ("snipe_short", "flip_short", "scalp_short")
    a_signals = [s for s in signals if s.confidence == "A"]
    if len(a_signals) >= 2:
        has_long = any(s.action in _LONG_ACTIONS for s in a_signals)
        has_short = any(s.action in _SHORT_ACTIONS for s in a_signals)
        if has_long and has_short:
            # 逆向思维：市场过冷(<40)优先做多，过热(>=40)优先做空
            prefer_long = temperature_score < 40
            for s in a_signals:
                is_long = s.action in _LONG_ACTIONS
                if (prefer_long and not is_long) or (not prefer_long and is_long):
                    s.confidence = "B"
                    s.warnings.append("存在反向A级信号，已降级为B级")

    snapshot.signals = signals
    snapshot.active_count = sum(1 for lv in snapshot.levels if lv.state != "idle")

    # ── D05 · cascade_risk 修复落实追踪 ──
    _report_cascade_health(snapshot, cfg)

    # ── M4 · 行为评估层（V3 行为验证引擎，纯观测，零信号影响）──
    # 严格只读 lv，仅写入 lv.behavior。失败被内部捕获不会影响主流程。
    # 详见 key_level_behavior_eval 模块顶部"设计纪律"。
    try:
        from processors.key_level_behavior_eval import evaluate_behavior
        evaluate_behavior(
            snapshot,
            candles_15m=candles_15m,
            candles_1h=candles_1h,
            cvd=cvd,
            oi_change_pct_1h=oi_change_pct_1h,
            taker_buy_vol=taker_buy_vol,
            taker_sell_vol=taker_sell_vol,
            cfg=cfg,
            now=now,
        )
    except Exception:  # noqa: BLE001 — 行为层完全独立，绝不影响信号产出
        import logging
        logging.getLogger(__name__).exception("evaluate_behavior failed (non-fatal)")

    return snapshot


def _report_cascade_health(snapshot: KeyLevelSnapshotV2, cfg: dict) -> None:
    """D05：采样当前 tracker 产出中 A 级信号的 cascade_risk 分布并上报。

    目的：
      修复前（双重计数）：几乎所有 A 级信号 cascade_risk>60% 概率 >60%
      修复后（移除 barrier 贡献）：同一数据下 A 级信号占比会降低，
                                    且剩下的 A 级 cascade>60% 占比应显著下降
    失败不影响主流程（tracker 内部已兜底）。
    """
    try:
        from utils.decision_tracker import D, get_tracker

        a_signals = [s for s in snapshot.signals if s.confidence == "A"]
        a_total = len(a_signals)
        a_cascade_gt60 = 0
        if a_total > 0:
            # 信号本身不带 cascade_risk，需从 levels 反查
            price_to_lv = {lv.price: lv for lv in snapshot.levels}
            for s in a_signals:
                lv = price_to_lv.get(s.level_price)
                if lv and lv.cascade_risk >= 0.6:
                    a_cascade_gt60 += 1

        barrier_max = max((lv.barrier_score for lv in snapshot.levels), default=0.0)
        cfg_cascade_norm = float(cfg.get("cascade_norm", 120.0))
        user_override = cfg_cascade_norm != 120.0

        # 关键位 snapshot 每 2-5s 一次，log=False 避免刷屏；
        # runtime 状态仍可通过 /api/decisions/summary 或 D1-D17 看板查询
        get_tracker().mark(
            D.D05_CASCADE_FIX,
            status="ok",
            log=False,
            cfg_cascade_norm=cfg_cascade_norm,
            user_override=user_override,
            a_tier_count=a_total,
            a_tier_cascade_gt60=a_cascade_gt60,
            a_tier_cascade_gt60_pct=(a_cascade_gt60 / a_total) if a_total else 0.0,
            barrier_max=barrier_max,
        )
    except Exception as e:  # noqa: BLE001 — tracker 不影响主流程
        logger.debug("D05 cascade health report failed: %s", e)


def _calc_atr_factor(atr: float, price: float) -> float:
    """ATR 自适应因子：高波动放宽阈值，低波动收窄。"""
    if price <= 0:
        return 1.0
    atr_pct = atr / price * 100
    return max(0.5, min(2.0, atr_pct / 2.0))


def _update_distance(lv: KeyLevelV2, price: float):
    if price > 0:
        lv.distance_pct = round((lv.price - price) / price * 100, 3)


def _transition(
    lv: KeyLevelV2,
    price: float,
    atr: float,
    atr_factor: float,
    sweep_events: list[dict],
    taker_buy: float,
    taker_sell: float,
    oi_change_pct: float,
    candles_15m: list[CandleData] | None,
    cfg: dict,
    now: int,
):
    dist_abs = abs(lv.distance_pct)
    approach_pct = cfg["approach_pct"] * atr_factor
    test_pct = cfg["test_pct"] * atr_factor
    bounce_pct = cfg["bounce_pct"] * atr_factor
    break_depth_pct = cfg["break_depth_pct"] * atr_factor
    break_confirm_sec = cfg["break_confirm_sec"]
    require_closed = bool(cfg.get("break_require_closed_bar", True))
    flip_zone_pct = cfg["flip_zone_pct"] * atr_factor

    is_support = lv.side == "support"

    if lv.state == "idle":
        if dist_abs <= approach_pct:
            _set_state(lv, "approaching", now)

    elif lv.state == "approaching":
        if dist_abs > approach_pct * 1.5:
            _set_state(lv, "idle", now)
        elif dist_abs <= test_pct:
            _set_state(lv, "testing", now)
            lv.test_count += 1
            lv.lowest_wick = price

    elif lv.state == "testing":
        if is_support:
            if lv.lowest_wick is None or price < lv.lowest_wick:
                lv.lowest_wick = price
        else:
            if lv.lowest_wick is None or price > lv.lowest_wick:
                lv.lowest_wick = price

        swept = _check_sweep(lv, sweep_events, cfg.get("sweep_proximity_pct", 2.0))
        if swept:
            lv.sweep_usd = swept
            _set_state(lv, "swept", now)
            return

        # 量价突破确认：时间 + 成交量放大 + OI 变化方向 + (P1-1) 已收盘 bar 穿透
        if _is_broken(lv, price, break_depth_pct):
            if lv.break_start_ts == 0:
                lv.break_start_ts = now
            else:
                time_ok = (now - lv.break_start_ts) >= break_confirm_sec
                vol_ok = _volume_confirms_break(is_support, taker_buy, taker_sell)
                oi_ok = _oi_confirms_break(oi_change_pct)
                closed_ok = _closed_bar_confirms_break(
                    candles_15m, lv, is_support, break_depth_pct, require_closed,
                )
                if time_ok and vol_ok and oi_ok and closed_ok:
                    _set_state(lv, "broken", now)
                    return
        else:
            lv.break_start_ts = 0

        if dist_abs > bounce_pct:
            safe = (is_support and price > lv.price) or (not is_support and price < lv.price)
            if safe:
                _set_state(lv, "bounced", now)

    elif lv.state == "swept":
        if _is_broken(lv, price, break_depth_pct):
            if lv.break_start_ts == 0:
                lv.break_start_ts = now
            elif (now - lv.break_start_ts) >= break_confirm_sec:
                vol_ok = _volume_confirms_break(is_support, taker_buy, taker_sell)
                oi_ok = _oi_confirms_break(oi_change_pct)
                closed_ok = _closed_bar_confirms_break(
                    candles_15m, lv, is_support, break_depth_pct, require_closed,
                )
                if vol_ok and oi_ok and closed_ok:
                    _set_state(lv, "broken", now)
        else:
            lv.break_start_ts = 0

        if dist_abs > bounce_pct:
            safe = (is_support and price > lv.price) or (not is_support and price < lv.price)
            if safe:
                _set_state(lv, "bounced", now)

    elif lv.state == "bounced":
        if dist_abs <= test_pct:
            _set_state(lv, "testing", now)
            lv.test_count += 1
        elif dist_abs > approach_pct * 2:
            _set_state(lv, "idle", now)

    elif lv.state == "broken":
        # 先判"假突破回收"：最新已收盘 15m bar close 回到原 level 未破侧
        # 成立 → 状态降级为 fake_break（产出反向 A 级信号）
        # 注：用户主动打开 break_require_closed_bar 时才启用该检测；关闭则维持 V2 原行为
        if cfg.get("break_require_closed_bar", True) and _fake_break_reclaim(
            candles_15m, lv, is_support
        ):
            _set_state(lv, "fake_break", now)
            lv.fake_break_count += 1
            return

        if dist_abs <= flip_zone_pct:
            on_broken_side = (
                (is_support and price < lv.price) or
                (not is_support and price > lv.price)
            )
            if on_broken_side:
                _set_state(lv, "flipped", now)
                lv.side = "resistance" if is_support else "support"

    elif lv.state == "flipped":
        if dist_abs > approach_pct * 2:
            _set_state(lv, "idle", now)

    elif lv.state == "fake_break":
        # fake_break 持续 > fake_break_expire_sec 且价格远离 level → 回 idle
        age = now - lv.state_ts
        expire = cfg.get("fake_break_expire_sec", 7200)
        if age > expire and dist_abs > approach_pct * 2:
            _set_state(lv, "idle", now)
        # 若价格又真正把 level 破了（新一轮破位），重置回 testing 让正常流程再判
        elif _is_broken(lv, price, break_depth_pct):
            _set_state(lv, "testing", now)
            lv.break_start_ts = 0


def _assess_bounce_quality(
    lv: KeyLevelV2,
    candles_15m: list[CandleData] | None,
    cfg: dict,
) -> str:
    """评估反弹质量（博主方法论：主动吸筹 vs 被动触发）。

    仅在 state == "bounced" 时有意义，其它状态返回 ""。

    proactive：反弹那根 15m bar 放量 ≥ 近 20 根均量 × 1.5，且方向一致
               (支撑=阳线 close>open / 阻力=阴线 close<open)
    passive  ：反弹 bar 缩量 < 近 20 根均量 × 0.8
    ""       ：中间态 / 数据不足 / 方向不一致
    """
    if lv.state != "bounced":
        return ""
    if not candles_15m or len(candles_15m) < 21:
        return ""

    recent = candles_15m[-1]
    ref = candles_15m[-21:-1]  # 前 20 根做基准
    avg_vol = sum(c.vol for c in ref) / 20.0
    if avg_vol <= 0:
        return ""

    ratio = recent.vol / avg_vol
    is_support = lv.side == "support"
    direction_ok = (
        (recent.close > recent.open) if is_support else (recent.close < recent.open)
    )
    if not direction_ok:
        return ""

    if ratio >= cfg.get("bounce_vol_proactive_mult", 1.5):
        return "proactive"
    if ratio < cfg.get("bounce_vol_passive_mult", 0.8):
        return "passive"
    return ""


def _assess_breakout_stage(
    lv: KeyLevelV2,
    atr: float,
    candles_15m: list[CandleData] | None,
    cfg: dict,
    now: int,
) -> int:
    """评估突破三步确认进度（博主方法论：破位→回踩→确认）。

    仅在 state in ("broken", "flipped") 时有意义，其它状态返回 0。

    stage 1：破位 15 分钟内
    stage 2：stage1 之后 90 分钟内，出现一根 bar 的高/低触达 level ±0.5×ATR（回踩）
    stage 3：回踩后下一根 bar 反向继续推进 ≥ 0.3×ATR（确认延续）
    stage 0：非破位状态 / 超过 6h 过期
    """
    if lv.state not in ("broken", "flipped"):
        return 0
    if atr <= 0:
        return 0

    age = now - lv.state_ts
    if age <= 0 or age > cfg.get("breakout_expire_sec", 21600):
        return 0

    if age < cfg.get("breakout_stage1_max_sec", 900):
        return 1

    if not candles_15m:
        return 1

    retest_tol = atr * cfg.get("breakout_retest_atr_mult", 0.5)
    retest_max_sec = cfg.get("breakout_retest_max_sec", 5400)
    confirm_atr = atr * cfg.get("breakout_confirm_atr_mult", 0.3)
    is_support = lv.side == "support"

    retest_idx = -1
    for idx, bar in enumerate(candles_15m):
        bar_age = now - bar.ts
        if bar_age <= 0 or bar_age > retest_max_sec:
            continue
        if bar.ts < lv.state_ts:
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

    follow = candles_15m[retest_idx + 1]
    if is_support:
        if follow.close <= lv.price - confirm_atr:
            return 3
    else:
        if follow.close >= lv.price + confirm_atr:
            return 3
    return 2


def _set_state(lv: KeyLevelV2, new_state: str, now: int):
    # Phase 2：一次"新反弹事件"才累加 bounce_count（testing→bounced 而非 bounced→bounced）
    if new_state == "bounced" and lv.state != "bounced":
        lv.bounce_count += 1

    # M3 · R9: 记录关键状态变化为 lifecycle event（仅当确实变化时）
    # 仅追踪有交易语义的状态转移（避免 idle↔approaching 噪声）
    _LIFECYCLE_TRACKED_STATES = {"testing", "swept", "bounced", "broken", "fake_break", "flipped"}
    _STATE_TO_EVENT = {
        "testing": "tested",
        "swept": "tested",
        "bounced": "reacted",
        "broken": "broken",
        "fake_break": "fake_break",
        "flipped": "flipped",
    }
    if new_state in _LIFECYCLE_TRACKED_STATES and new_state != lv.state:
        evt_type = _STATE_TO_EVENT.get(new_state, new_state)
        side_cn = {"support": "支撑", "resistance": "阻力"}.get(lv.side, lv.side)
        detail_map = {
            "tested": f"进入测试 · {side_cn} ${lv.price:.2f}",
            "reacted": f"反弹生效 · {side_cn} ${lv.price:.2f}（第{lv.bounce_count}次）",
            "broken": f"被有效突破 · {side_cn} ${lv.price:.2f}",
            "fake_break": f"假突破后重夺 · {side_cn} ${lv.price:.2f}",
            "flipped": f"角色翻转 · {side_cn} ${lv.price:.2f}",
        }
        lv.lifecycle_events.append(LifecycleEvent(
            ts=now,
            event_type=evt_type,
            detail=detail_map.get(evt_type, f"{lv.state} → {new_state}"),
            score_before=lv.final_score,
            score_after=lv.final_score,
            tier_before=lv.strength_tier,
            tier_after=lv.strength_tier,
            state_before=lv.state,
            state_after=new_state,
            layer="tracker",
        ))
        # 限长 20 条
        if len(lv.lifecycle_events) > 20:
            lv.lifecycle_events = lv.lifecycle_events[-20:]

    lv.prev_state = lv.state
    lv.state = new_state
    lv.state_ts = now
    lv.break_start_ts = 0


def _is_broken(lv: KeyLevelV2, price: float, depth_pct: float) -> bool:
    if lv.price <= 0:
        return False
    if lv.side == "support":
        return price < lv.price * (1 - depth_pct / 100)
    else:
        return price > lv.price * (1 + depth_pct / 100)


def _closed_bar_confirms_break(
    candles: list[CandleData] | None,
    lv: KeyLevelV2,
    is_support: bool,
    depth_pct: float,
    require_closed: bool,
) -> bool:
    """P1-1 · 检查最近一根**已收盘** 15m bar 的 close 是否真的穿透 level。

    语义：
      - `require_closed=False`：老行为，直接放行（不做收盘确认）
      - `require_closed=True` 但 candles 数据不足：保守放行（避免数据缺失时卡死状态机）
      - 正常情况下取 `candles[-2]`（通常 `[-1]` 是当前未收盘 bar），检查其 close
        是否满足破位阈值；满足才允许升 broken
    """
    if not require_closed:
        return True
    if not candles or len(candles) < 2:
        # 数据不足 → 保守放行；避免数据降级时整条状态机不可用
        return True
    if lv.price <= 0:
        return False

    # candles[-1] 一般是当前未收盘 bar，必须排除；用 candles[-2] 做闭口确认
    last_closed = candles[-2]
    close = getattr(last_closed, "close", None)
    if close is None or close <= 0:
        return False

    if is_support:
        return close < lv.price * (1 - depth_pct / 100)
    return close > lv.price * (1 + depth_pct / 100)


def _fake_break_reclaim(
    candles: list[CandleData] | None,
    lv: KeyLevelV2,
    is_support: bool,
) -> bool:
    """假突破回收检测：level 已处于 broken，但最新一根**已收盘** 15m bar 的 close
    重新回到了 level 未破侧。

    - support：broken 代表 close 跌穿，若下一根已收盘 close 重新 ≥ level.price → 假破
    - resistance：反之亦然

    用 candles[-2] 保证闭口（candles[-1] 通常是未收盘 bar）。
    数据不足返回 False（不误判为假破）。
    """
    if not candles or len(candles) < 2 or lv.price <= 0:
        return False
    last_closed = candles[-2]
    close = getattr(last_closed, "close", None)
    if close is None or close <= 0:
        return False
    if is_support:
        return close >= lv.price
    return close <= lv.price


# ─── Z · MTF 1h 一致性 ────────────────────────────────────────────────────────

def _mtf_1h_bias(candles_1h: list[CandleData] | None) -> str:
    """粗估 1h 方向偏向：
      - 取最近 4 根 1h 的 close 做 EMA 近似判断
      - return 'up' | 'down' | 'flat' | 'unknown'
    数据不足返回 unknown（调用方跳过 MTF 判定，不加减分）。
    """
    if not candles_1h or len(candles_1h) < 4:
        return "unknown"
    closes = [getattr(c, "close", None) for c in candles_1h[-6:]]
    closes = [c for c in closes if c and c > 0]
    if len(closes) < 3:
        return "unknown"
    head = closes[0]
    tail = closes[-1]
    if head <= 0:
        return "unknown"
    change_pct = (tail - head) / head * 100
    if change_pct >= 0.6:
        return "up"
    if change_pct <= -0.6:
        return "down"
    return "flat"


def _mtf_aligned_with_long(bias: str) -> tuple[bool, bool]:
    """返回 (aligned, diverged)，flat / unknown 时两者都 False（不加减分）。"""
    if bias == "up":
        return True, False
    if bias == "down":
        return False, True
    return False, False


def _mtf_aligned_with_short(bias: str) -> tuple[bool, bool]:
    if bias == "down":
        return True, False
    if bias == "up":
        return False, True
    return False, False


# ─── V · CVD 背离 / 一致性确认 ───────────────────────────────────────────────

def _cvd_trend_value(cvd: CVDData | None) -> str:
    """拿 CVD 1h 趋势，简化为 'up' / 'down' / 'flat' / 'unknown'。"""
    if not cvd:
        return "unknown"
    trend = (cvd.trend_1h or "").strip().lower()
    if trend in {"up", "rising", "positive", "bullish"}:
        return "up"
    if trend in {"down", "falling", "negative", "bearish"}:
        return "down"
    # 没有显式 trend 就用 delta_1h 兜底
    delta = cvd.delta_1h or 0
    if delta > 0:
        return "up"
    if delta < 0:
        return "down"
    return "flat"


def _cvd_aligned_with_long(cvd: CVDData | None) -> tuple[bool, bool]:
    """做多：CVD up → aligned；CVD down → diverged；flat/unknown → 不加减分。"""
    trend = _cvd_trend_value(cvd)
    if trend == "up":
        return True, False
    if trend == "down":
        return False, True
    return False, False


def _cvd_aligned_with_short(cvd: CVDData | None) -> tuple[bool, bool]:
    trend = _cvd_trend_value(cvd)
    if trend == "down":
        return True, False
    if trend == "up":
        return False, True
    return False, False


def _volume_confirms_break(is_support: bool, taker_buy: float, taker_sell: float) -> bool:
    """成交量确认：突破方向的主动成交量 > 对手方。"""
    if taker_buy <= 0 and taker_sell <= 0:
        return True
    if is_support:
        return taker_sell > taker_buy * 0.8
    else:
        return taker_buy > taker_sell * 0.8


def _oi_confirms_break(oi_change_pct: float) -> bool:
    """OI 确认：OI 未大幅下降即视为通过。

    OI 增加 → 新仓位入场推动突破（真突破）
    OI 持平 → 不确定，放行
    OI 大幅下降(< -3%) → 平仓推动穿越，很可能是假突破
    """
    if oi_change_pct == 0:
        return True
    return oi_change_pct > -3.0


def _check_sweep(lv: KeyLevelV2, sweep_events: list[dict], proximity_pct: float) -> float:
    total = 0.0
    tol = lv.price * proximity_pct / 100
    for ev in sweep_events:
        ev_side = ev.get("side", "")
        ev_usd = ev.get("usd", 0)
        if ev_usd <= 0:
            continue
        ev_from = ev.get("price_from", 0)
        ev_to = ev.get("price_to", 0)
        if ev_from > 0 and ev_to > 0:
            if ev_to < lv.price - tol or ev_from > lv.price + tol:
                continue
        if lv.side == "support" and ev_side == "below":
            total += ev_usd
        elif lv.side == "resistance" and ev_side == "above":
            total += ev_usd
    return total


def _calc_cascade_risk(
    lv: KeyLevelV2,
    liq_map: LiquidationMap | None,
    price: float,
    cfg: dict,
    *,
    liq_map_7d: LiquidationMap | None = None,
):
    """V3-P2-10：当 24h liq_map 没有方向上的簇时，回退到 7d 计算 magnet。

    设计：
      - 1d/24h 是首选（最新鲜，方向最准）
      - 但 1d 簇有时为空（清算清淡 / 周末 / 数据短暂缺失）
      - 此时若 7d 有相同方向的簇，回退使用之，避免 next_magnet_price=None
      - 关键标识：当走 7d 回退时，cascade_components 不会假装满分（仍然按真实数据算）
    """
    def _pick_clusters(lm: LiquidationMap | None) -> list:
        if lm is None:
            return []
        if lv.side == "support":
            cs = [c for c in lm.clusters_below if c.price_center < lv.price]
            cs.sort(key=lambda c: c.price_center, reverse=True)
        else:
            cs = [c for c in lm.clusters_above if c.price_center > lv.price]
            cs.sort(key=lambda c: c.price_center)
        return cs

    clusters = _pick_clusters(liq_map)
    if not clusters and liq_map_7d is not None:
        clusters = _pick_clusters(liq_map_7d)

    if not clusters:
        lv.cascade_risk = 0
        lv.cascade_layers = 0
        lv.cascade_total_usd = 0
        # M1: 无 cascade 也清空 magnet/vacuum
        lv.next_magnet_price = None
        lv.vacuum_gap_pct = 0.0
        # M2: 4 子分清空
        lv.cascade_components = CascadeComponents()
        return

    lv.cascade_layers = min(len(clusters), 5)
    lv.cascade_total_usd = sum(c.total_usd for c in clusters[:5])

    # ── M1：破位后的下一个磁铁价位 + 真空跨度 ──────────────────
    # 设计：破位后价格会被最近的清算簇磁吸；该簇就是 next_magnet_price
    # vacuum_gap_pct = 当前 level.price → next_magnet_price 之间的价格间距 %
    nearest = clusters[0]
    lv.next_magnet_price = round(nearest.price_center, 2)
    lv.vacuum_gap_pct = round(
        abs(lv.price - nearest.price_center) / max(price, 1) * 100, 2,
    )

    # ── M2 + V3-P1-1：cascade 4 子分 + 真聚合 cascade_risk ──────────
    # 历史背景（D05 修复 → 2025-04 V3 验收 P1-1）：
    #   旧公式 cascade_risk = risk_score / norm * dist_decay 与 4 子分
    #   并行计算，互不一致。模型注释承诺"加权和"但代码未真聚合 → 伪落地。
    #   本次（P1-1）改为 cascade_risk 由 4 子分加权聚合得出，让模型注释、
    #   UI 展示、AI 解释三者完全对账。
    #
    # 4 子分（均归一到 0-1）：
    #   count_score    : 簇数量（≤5 上限避免双重计数 usd）
    #   usd_score      : 累计 USD 规模
    #   velocity_score : 真空跨度倒推的破位加速度
    #   leverage_score : 簇内最大 leverage 强度（杠杆主导风险）
    count_score = min(1.0, len(clusters) / 5.0)
    usd_score = min(1.0, lv.cascade_total_usd / 200_000_000)  # 200M USD 满分
    # velocity_score: 真空跨度越紧凑，破位越急速
    # vacuum_gap_pct ≤ 0.5% → 1.0；≥ 5% → 0.2；中间线性
    if lv.vacuum_gap_pct <= 0.5:
        velocity_score = 1.0
    elif lv.vacuum_gap_pct >= 5.0:
        velocity_score = 0.2
    else:
        velocity_score = 1.0 - (lv.vacuum_gap_pct - 0.5) / 4.5 * 0.8
    max_leverage_intensity = 0.0
    for c in clusters[:5]:
        li = float(getattr(c, "leverage_intensity", 0.0) or 0.0)
        if li > max_leverage_intensity:
            max_leverage_intensity = li
    leverage_score = min(1.0, max_leverage_intensity / 0.7)  # 0.7+ 算满分

    lv.cascade_components = CascadeComponents(
        count_score=round(count_score, 3),
        usd_score=round(usd_score, 3),
        velocity_score=round(velocity_score, 3),
        leverage_score=round(leverage_score, 3),
    )

    # 加权聚合：USD 权重最高（绝对压力），velocity 次之（破位加速），
    # leverage 反映杠杆主导，count 已被 usd 部分代理故权重最低
    weighted = (
        0.35 * usd_score
        + 0.30 * velocity_score
        + 0.20 * leverage_score
        + 0.15 * count_score
    )
    dist_from_price = abs(lv.price - price) / max(price, 1) * 100
    dist_decay = 1.0 / (1.0 + dist_from_price / 3.0)
    lv.cascade_risk = round(min(1.0, weighted * dist_decay), 2)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 智能信号生成
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _generate_signal(
    lv: KeyLevelV2,
    price: float,
    atr: float,
    all_levels: list[KeyLevelV2],
    temperature: float,
    candles_4h: list[CandleData] | None,
    cfg: dict,
    now: int,
    candles_1h: list[CandleData] | None = None,
    cvd: CVDData | None = None,
) -> KeyLevelSignal | None:
    if atr <= 0:
        return None

    # MTF / CVD 偏向预计算（整张信号通用）
    mtf_bias = _mtf_1h_bias(candles_1h)

    if lv.state == "idle":
        if lv.strength_tier in ("S", "A") and abs(lv.distance_pct) <= 15:
            is_support = lv.side == "support"
            direction = "做多" if is_support else "做空"
            return _finalize_signal(
                KeyLevelSignal(
                    level_price=lv.price, side=lv.side, state=lv.state,
                    action="wait_approach",
                    confidence="C",
                    reason=(
                        f"前瞻观察: {lv.strength_tier}级"
                        f"{_SIDE_CN.get(lv.side, lv.side)}${lv.price:,.0f}"
                        f"({lv.source_count}维共振, 距{abs(lv.distance_pct):.1f}%), "
                        f"价格接近时关注{direction}机会"
                    ),
                ),
                "wait_approach",
            )
        return None

    # 信号过期检查
    signal_expire = cfg.get("signal_expire_sec", 14400)
    if lv.state in ("swept", "bounced") and (now - lv.state_ts) > signal_expire:
        return None

    is_support = lv.side == "support"
    side_cn = _SIDE_CN.get(lv.side, lv.side)

    base = KeyLevelSignal(
        level_price=lv.price, side=lv.side, state=lv.state,
        action="wait_approach", reason="",
    )

    if lv.state == "approaching":
        base.action = "wait_approach"
        direction = "做多" if is_support else "做空"
        base.reason = f"价格正在接近{side_cn}位${lv.price:,.0f}({lv.source_count}维共振)，准备关注{direction}机会"
        if lv.strength_tier in ("S", "A"):
            base.confidence = "B"
            base.reason = (
                f"价格正在接近{lv.strength_tier}级{side_cn}位${lv.price:,.0f}"
                f"({lv.source_count}维共振，距离{abs(lv.distance_pct):.1f}%)，"
                f"建议提前准备{direction}策略"
            )
        else:
            base.confidence = "C"
        return _finalize_signal(base, "wait_approach")

    if lv.state == "testing":
        base.action = "wait_sweep"
        if lv.strength_tier in ("S", "A"):
            base.confidence = "B"
            base.reason = (
                f"价格正在测试{lv.strength_tier}级{side_cn}位${lv.price:,.0f}"
                f"(已测试{lv.test_count}次)，等待流动性扫取确认后入场"
            )
        else:
            base.confidence = "C"
            base.reason = f"价格正在测试{side_cn}位${lv.price:,.0f}，等待流动性扫取确认后入场"
        if lv.cascade_risk > 0.7:
            base.warnings.append(f"级联风险{lv.cascade_risk:.0%}，突破后可能瀑布")
        return _finalize_signal(base, "wait_sweep")

    if lv.state == "swept":
        tp1_price = _find_opposite_target(lv, price, all_levels, atr)
        if is_support:
            entry = lv.price + atr * 0.15
            sl = lv.price - atr * 1.5
            base.action = "snipe_long"
            base.reason = (
                f"{side_cn}${lv.price:,.0f}下方流动性已被扫取({fmt_usd_cn(lv.sweep_usd)})，"
                f"空头弹药耗尽 → A级做多"
            )
        else:
            entry = lv.price - atr * 0.15
            sl = lv.price + atr * 1.5
            base.action = "snipe_short"
            base.reason = (
                f"{side_cn}${lv.price:,.0f}上方流动性已被扫取({fmt_usd_cn(lv.sweep_usd)})，"
                f"多头弹药耗尽 → A级做空"
            )
        base.confidence = "A"
        base.entry_price = round(entry, 2)
        base.stop_loss = round(sl, 2)
        base.tp1 = round(tp1_price, 2)
        risk = abs(entry - sl)
        reward = abs(tp1_price - entry)
        base.rr_ratio = round(reward / risk, 1) if risk > 0 else 0
        if lv.cascade_risk > 0.5:
            base.warnings.append(f"注意级联风险{lv.cascade_risk:.0%}，建议减仓")
        base.confirmations.append("sweep_taken")
        _apply_mtf_cvd(base, long=is_support, mtf_bias=mtf_bias, cvd=cvd)
        return _finalize_signal(base, "snipe_sweep")

    if lv.state == "bounced":
        has_sweep = lv.sweep_usd > 0
        pattern = detect_reversal_pattern(candles_4h, lv.side)
        if pattern.found:
            lv.pattern_detected = pattern.name
            lv.pattern_strength = round(pattern.strength, 2)
        tp1_price = _find_opposite_target(lv, price, all_levels, atr)
        direction = "做多" if is_support else "做空"

        if is_support:
            entry = lv.price + atr * 0.2
            sl = lv.price - atr * 1.2
            base.action = "snipe_long"
        else:
            entry = lv.price - atr * 0.2
            sl = lv.price + atr * 1.2
            base.action = "snipe_short"

        if has_sweep and pattern.found:
            base.confidence = "A"
            base.reason = (
                f"{side_cn}${lv.price:,.0f}流动性扫取+{pattern.name}双重确认"
                f" → A级{direction}"
            )
        elif has_sweep:
            base.confidence = "A"
            cn = "反弹确认" if is_support else "受阻确认"
            base.reason = f"{side_cn}${lv.price:,.0f}流动性扫取后{cn} → A级{direction}"
        elif pattern.found:
            base.confidence = "A"
            base.reason = (
                f"{side_cn}${lv.price:,.0f}{pattern.name}反转确认"
                f"(第{lv.test_count}次测试) → A级{direction}"
            )
        else:
            base.confidence = "B"
            cn = "反弹确认" if is_support else "受阻确认"
            base.reason = f"{side_cn}${lv.price:,.0f}{cn}(第{lv.test_count}次测试) → B级{direction}"

        base.entry_price = round(entry, 2)
        base.stop_loss = round(sl, 2)
        base.tp1 = round(tp1_price, 2)
        risk = abs(entry - sl)
        reward = abs(tp1_price - entry)
        base.rr_ratio = round(reward / risk, 1) if risk > 0 else 0

        # 收集确认项
        if has_sweep:
            base.confirmations.append("sweep_taken")
        if pattern.found:
            base.confirmations.append(f"pattern_{pattern.name}")
        # bounce_quality 降级：缩量反弹 = 被动，降一档 + 加 warning
        if lv.bounce_quality == "proactive":
            base.confirmations.append("volume_proactive")
        elif lv.bounce_quality == "passive":
            base.warnings.append("缩量反弹(被动触发)，容易二次回抽")
            if base.confidence == "A":
                base.confidence = "B"
            elif base.confidence == "B":
                base.confidence = "C"

        _apply_mtf_cvd(base, long=is_support, mtf_bias=mtf_bias, cvd=cvd)
        return _finalize_signal(base, "snipe_bounce")

    if lv.state == "broken":
        # breakout_stage 驱动置信度：未收盘 / 未回踩 → 观望；回踩中 → B；确认后 → A
        stage = lv.breakout_stage or 1
        tp1_price = _find_opposite_target(lv, price, all_levels, atr)
        direction = "做空" if is_support else "做多"  # 破位后反向：支撑破 = 做空

        if stage <= 1:
            base.action = "wait_approach"
            base.reason = (
                f"{side_cn}${lv.price:,.0f}刚突破（Stage 1·未收盘确认），"
                f"等待回踩确认再入场"
            )
            base.confidence = "C"
            base.warnings.append("突破刚刚发生，容易反向假破，不追单")
            return _finalize_signal(base, "breakout_observing")

        if stage == 2:
            if is_support:
                entry = lv.price - atr * 0.3  # 已破支撑，回踩到 level 下方做空
                sl = lv.price + atr * 0.8
                base.action = "snipe_short"
            else:
                entry = lv.price + atr * 0.3
                sl = lv.price - atr * 0.8
                base.action = "snipe_long"
            base.confidence = "B"
            base.reason = (
                f"{side_cn}${lv.price:,.0f}破位回踩中(Stage 2)，"
                f"等待回踩确认做{direction}"
            )
            base.entry_price = round(entry, 2)
            base.stop_loss = round(sl, 2)
            base.tp1 = round(tp1_price, 2)
            risk = abs(entry - sl)
            reward = abs(tp1_price - entry)
            base.rr_ratio = round(reward / risk, 1) if risk > 0 else 0
            base.confirmations.append("retest_in_progress")
            _apply_mtf_cvd(
                base, long=not is_support, mtf_bias=mtf_bias, cvd=cvd,
            )
            return _finalize_signal(base, "breakout_retest")

        # stage >= 3 确认成功
        if is_support:
            entry = lv.price - atr * 0.15
            sl = lv.price + atr * 0.8
            base.action = "snipe_short"
        else:
            entry = lv.price + atr * 0.15
            sl = lv.price - atr * 0.8
            base.action = "snipe_long"
        base.confidence = "A"
        base.reason = (
            f"{side_cn}${lv.price:,.0f}破位三步确认完成(Stage 3) → A级{direction}"
        )
        base.entry_price = round(entry, 2)
        base.stop_loss = round(sl, 2)
        base.tp1 = round(tp1_price, 2)
        risk = abs(entry - sl)
        reward = abs(tp1_price - entry)
        base.rr_ratio = round(reward / risk, 1) if risk > 0 else 0
        base.confirmations.append("retest_done")
        base.confirmations.append("continuation")
        _apply_mtf_cvd(base, long=not is_support, mtf_bias=mtf_bias, cvd=cvd)
        return _finalize_signal(base, "breakout_continuation")

    if lv.state == "fake_break":
        # 假突破反转：原 support 被 close 跌穿后又回收，给反向 A 级 snipe_long
        tp1_price = _find_opposite_target(lv, price, all_levels, atr)
        if is_support:
            entry = lv.price + atr * 0.1
            sl = lv.price - atr * 1.0  # 止损给到原破位深度，防再次真破
            base.action = "snipe_long"
            base.reason = (
                f"{side_cn}${lv.price:,.0f}假突破回收，"
                f"破位陷阱 → A级做多（level 二次确认强守）"
            )
        else:
            entry = lv.price - atr * 0.1
            sl = lv.price + atr * 1.0
            base.action = "snipe_short"
            base.reason = (
                f"{side_cn}${lv.price:,.0f}假突破回收，"
                f"破位陷阱 → A级做空（level 二次确认强压）"
            )
        base.confidence = "A"
        base.entry_price = round(entry, 2)
        base.stop_loss = round(sl, 2)
        base.tp1 = round(tp1_price, 2)
        risk = abs(entry - sl)
        reward = abs(tp1_price - entry)
        base.rr_ratio = round(reward / risk, 1) if risk > 0 else 0
        base.confirmations.append("fake_break_reclaim")
        base.confirmations.append("closed_bar")
        if lv.fake_break_count >= 2:
            base.confirmations.append("multi_fake_break")
        _apply_mtf_cvd(base, long=is_support, mtf_bias=mtf_bias, cvd=cvd)
        return _finalize_signal(base, "fake_break_reversal")

    if lv.state == "flipped":
        pattern = detect_reversal_pattern(candles_4h, lv.side)
        if pattern.found:
            lv.pattern_detected = pattern.name
            lv.pattern_strength = round(pattern.strength, 2)
        tp1_price = _find_opposite_target(lv, price, all_levels, atr)
        pat_tag = f"+{pattern.name}" if pattern.found else ""
        if lv.side == "resistance":
            entry = lv.price - atr * 0.1
            sl = lv.price + atr * 1.2
            base.action = "flip_short"
            base.reason = (
                f"原支撑${lv.price:,.0f}已翻转为阻力，回踩被拒{pat_tag}"
                f" → A级翻转做空"
            )
        else:
            entry = lv.price + atr * 0.1
            sl = lv.price - atr * 1.2
            base.action = "flip_long"
            base.reason = (
                f"原阻力${lv.price:,.0f}已翻转为支撑，回踩获撑{pat_tag}"
                f" → A级翻转做多"
            )
        base.confidence = "A"
        base.entry_price = round(entry, 2)
        base.stop_loss = round(sl, 2)
        base.tp1 = round(tp1_price, 2)
        risk = abs(entry - sl)
        reward = abs(tp1_price - entry)
        base.rr_ratio = round(reward / risk, 1) if risk > 0 else 0
        if pattern.found:
            base.confirmations.append(f"pattern_{pattern.name}")
        base.confirmations.append("flip_retest")
        long_side = lv.side == "support"  # 翻成 support → 做多
        _apply_mtf_cvd(base, long=long_side, mtf_bias=mtf_bias, cvd=cvd)
        return _finalize_signal(base, "flip_retest")

    return None


_LONG_ACTIONS_OP = frozenset({"snipe_long", "flip_long", "scalp_long"})
_SHORT_ACTIONS_OP = frozenset({"snipe_short", "flip_short", "scalp_short"})


def _apply_pressure_alignment(
    signals: list[KeyLevelSignal],
    pressure_snapshot: OrderbookPressureSnapshot,
    atr: float,
) -> None:
    """挂单压力监测器对 KL 信号的 confirmation/warning 叠加（M3 桥接）。

    设计演进：
      - 早期（2026-04 重构）：仅 S/A 级 PressureWall 共振 → ob_strong_bid/ask
      - M3 桥接（当前）：读 wall_zones / wall_events / break_through_risk /
        sweep_target / vacuum_gap_pct，产出更精细的多档 chip + 风险 warning
      - W3-T4-a（2026-04）：删除旧 ob_strong_* 路径——它仅看 strength_tier（厚度档），
        无法识别"厚但低 trust 的 spoof 嫌疑墙"，与新 6 类 chip（按 trust/源/Coinbase
        细分）口径冲突且冗余。删除后这类墙不再贡献 chip，避免误导 AI/前端。

    所有改动**仅追加** confirmations / warnings，不动 final_score /
    strength_tier / cascade_risk（V3 铁律）。

    输出（按优先级互斥 + Coinbase 叠加）：
      A. confirmations（key 化，前端 CONFIRMATION_LABELS 映射）：
         - ob_dual_source_bid/ask     双源高可信墙（dual_source=True）
         - ob_spot_only_bid/ask       仅现货墙（source="spot_only"）
         - ob_spot_confluence_bid/ask 现货大单共振（has_spot_confluence=True）
         - ob_trusted_bid/ask         较可信合约墙（trust_score >= 0.65）
         - ob_coinbase_bid/ask        ⭐ W3-T1：Coinbase 现货共振（机构资金独立验证维度）
                                       叠加而非互斥 — 双源墙 + Coinbase 同价区共振时同时出现
         - ob_wall_strengthened       最近 30min 该价位墙增厚事件

      B. warnings（中文短句，前端原样渲染）：
         - "该位墙刚被吃 Nmin 前"      (wall_consumed @ 同价位 30min 内)
         - "该位墙刚撤单 Nmin 前"      (wall_removed @ 同价位 30min 内)
         - "打穿风险评分 0.XX；下/上方磁铁 $Y" (break_through_risk >= 0.6 + sweep_target)
         - "真空跨度 X%（无缓冲）"     (sweep_target.vacuum_gap_pct >= 0.5)
         - "仅合约挂单 + 撤单风险评分 0.XX" (trust_score < 0.55 且 wall_removal_risk >= 0.6)

      W3-T3 措辞口径：
         风险评分一律以 0.XX 浮点形式展示（与 trust_score/confidence 同口径），
         不再使用 X% 表达，从根本上消除被误读为"统计概率"的路径。
         "真空跨度" 仍保留 % 单位 — 它是真实的价格区间百分比（非评分）。

    匹配规则：
      - 同价位 ≤ 0.5 × ATR（atr 缺失时 fallback 0.3% 价格）
      - 做多信号 ↔ bid 墙；做空信号 ↔ ask 墙
      - 多 zone 命中取 trust_score 最高一个产 chip，避免 chip 泛滥
    """
    same_tol = max(atr * 0.5, 1e-9) if atr > 0 else 0.0

    # ── 数据预备：wall_zones + wall_events（W3-T4-a：移除旧 PressureWall 路径）──
    zones_above = pressure_snapshot.walls_above or []
    zones_below = pressure_snapshot.walls_below or []
    events = pressure_snapshot.wall_events or []
    snap_ts = pressure_snapshot.ts_sec or 0

    if not (zones_above or zones_below or events):
        return

    for sig in signals:
        long_side = sig.action in _LONG_ACTIONS_OP
        short_side = sig.action in _SHORT_ACTIONS_OP
        if not (long_side or short_side):
            continue

        lvl_price = sig.level_price
        local_same = same_tol if same_tol > 0 else lvl_price * 0.003

        # ── wall_zones 同价位 → 多档 chip + 风险 warning ──
        if long_side:
            matched_zones = [z for z in zones_below
                             if abs(z.price_mid - lvl_price) <= local_same]
            side_label = "bid"
        else:
            matched_zones = [z for z in zones_above
                             if abs(z.price_mid - lvl_price) <= local_same]
            side_label = "ask"

        if matched_zones:
            best_zone = max(matched_zones, key=lambda z: z.trust_score)
            _append_zone_trust_chip(sig, best_zone, side_label)
            _append_zone_risk_warnings(sig, best_zone, long_side)

        # ── wall_events：最近 30min 同价位 + 同侧 ──
        ev_strengthened_added = False
        for ev in events:
            if abs(ev.price_mid - lvl_price) > local_same:
                continue
            ev_age_sec = max(0, snap_ts - ev.ts_sec) if snap_ts else 0
            if ev_age_sec > 1800:
                continue
            ev_side_match = (long_side and ev.side == "bid") or \
                            (short_side and ev.side == "ask")
            if not ev_side_match:
                continue
            ev_min = max(1, ev_age_sec // 60)
            if ev.event_type == "wall_consumed":
                sig.warnings.append(f"该位墙刚被吃 {ev_min}min 前")
            elif ev.event_type == "wall_removed":
                sig.warnings.append(f"该位墙刚撤单 {ev_min}min 前")
            elif ev.event_type == "wall_strengthened" and not ev_strengthened_added:
                sig.confirmations.append("ob_wall_strengthened")
                ev_strengthened_added = True

        sig.score = _compute_score(sig)


def _append_zone_trust_chip(
    sig: KeyLevelSignal, zone: WallZone, side_label: str
) -> None:
    """根据 zone 信任档位在 sig.confirmations 加 chip key。

    互斥优先级（从高到低）：
      双源 > 仅现货 > 现货共振 > 可信合约 > （普通合约不加 chip）

    W3-T4-a：trust_score < 0.65 且非现货/双源的墙不再加 chip——这类"厚但低 trust"
    的墙过去由旧 ob_strong_* 路径覆盖，但实证发现它包含较多合约 spoof 嫌疑墙，
    给 AI 加 chip 反而是噪声。如需观察这类墙，请直接查看 §8d wall 列表。

    W3-T1 叠加（与互斥优先级正交）：
      - ob_coinbase_<side>：当 coinbase_spot_confluence=True 时**额外**追加，
        机构资金独立验证维度（Binance/OKX 系之外的 Coinbase 现货共振）。
        与上述任一互斥 chip 可同时出现，前端按"叠加证据"展示。
    """
    if zone.dual_source:
        sig.confirmations.append(f"ob_dual_source_{side_label}")
    elif zone.source == "spot_only":
        sig.confirmations.append(f"ob_spot_only_{side_label}")
    elif zone.has_spot_confluence:
        sig.confirmations.append(f"ob_spot_confluence_{side_label}")
    elif zone.trust_score >= 0.65:
        sig.confirmations.append(f"ob_trusted_{side_label}")

    # W3-T1：Coinbase 现货共振叠加 chip（机构资金独立验证）
    if getattr(zone, "coinbase_spot_confluence", False):
        sig.confirmations.append(f"ob_coinbase_{side_label}")


def _append_zone_risk_warnings(
    sig: KeyLevelSignal, zone: WallZone, long_side: bool
) -> None:
    """根据 zone 风险因子追加中文 warning 字符串。

    规则（独立判定，可同时多条）：
      - 仅合约 + 高撤单：trust_score < 0.55 且 wall_removal_risk >= 0.6
      - 打穿高风险：break_through_risk >= 0.6（带磁铁价位）
      - 真空跨度大：sweep_target.vacuum_gap_pct >= 0.5
    """
    if zone.trust_score < 0.55 and zone.wall_removal_risk >= 0.6:
        sig.warnings.append(
            f"仅合约挂单+撤单风险评分{zone.wall_removal_risk:.2f}"
        )

    if zone.break_through_risk >= 0.6:
        magnet = zone.next_magnet_price
        if magnet is None and zone.sweep_target:
            magnet = zone.sweep_target.magnet_price
        if magnet:
            direction = "下方" if long_side else "上方"
            magnet_str = _format_magnet_price(magnet)
            sig.warnings.append(
                f"打穿风险评分{zone.break_through_risk:.2f}；{direction}磁铁{magnet_str}"
            )
        else:
            sig.warnings.append(
                f"打穿风险评分{zone.break_through_risk:.2f}"
            )

    sweep = zone.sweep_target
    if sweep and sweep.vacuum_gap_pct >= 0.5:
        sig.warnings.append(f"真空跨度{sweep.vacuum_gap_pct:.1f}%（无缓冲）")


def _format_magnet_price(price: float) -> str:
    """磁铁价位中文友好格式化。"""
    if price >= 1000:
        return f"${price:,.0f}"
    if price >= 10:
        return f"${price:.2f}"
    return f"${price:.4f}"


def _apply_mtf_cvd(
    sig: KeyLevelSignal,
    long: bool,
    mtf_bias: str,
    cvd: CVDData | None,
) -> None:
    """给信号叠加 Z · MTF 1h 一致性 + V · CVD 一致性确认。

    行为：
      - aligned：confirmations 追加 mtf_aligned / cvd_aligned（score 会 +4）
      - diverged：warnings 追加说明（score -3），并且若原 A 级 → 降为 B，
        让高频扫损场景（信号方向与高阶方向相反）自动降档

    设计原则：
      - 数据缺失（unknown/flat）时不加减分，防止测试 / 数据降级误伤
      - 同时被 Z 和 V 共同判背离时，最多只降 1 档（避免从 A 直接跳 C 过激）
    """
    mtf_aligned, mtf_diverged = (
        _mtf_aligned_with_long(mtf_bias) if long else _mtf_aligned_with_short(mtf_bias)
    )
    cvd_aligned, cvd_diverged = (
        _cvd_aligned_with_long(cvd) if long else _cvd_aligned_with_short(cvd)
    )

    if mtf_aligned:
        sig.confirmations.append("mtf_aligned")
    if cvd_aligned:
        sig.confirmations.append("cvd_aligned")

    degraded = False
    if mtf_diverged:
        sig.warnings.append("1h 级别方向不一致(MTF 背离)")
        degraded = True
    if cvd_diverged:
        sig.warnings.append("CVD 方向与信号相反(资金流背离)")
        degraded = True

    if degraded and sig.confidence == "A":
        sig.confidence = "B"


def _generate_scalp_signal(
    lv: KeyLevelV2,
    price: float,
    atr: float,
    all_levels: list[KeyLevelV2],
    candles_15m: list[CandleData] | None,
    cfg: dict,
    now: int,
    cvd: CVDData | None = None,
) -> KeyLevelSignal | None:
    """日内极小止损档（scalp）信号生成器。

    核心逻辑：贴近 S/A 级关键位 + 15m K 线拒绝影线（pin bar / engulfing） + 极小止损
    (≤ max(0.2%, 0.4×ATR)，上限 0.5×ATR) → 高 R:R 日内机会。

    与 snipe/flip 信号**并存而非替代**：snipe 面向中线(SL ~1.5 ATR)，scalp 面向
    日内(SL <0.5 ATR)，两者时间尺度和仓位管理不同。

    触发条件全部满足才产出：
      1. 关键位 strength_tier ∈ {S, A}
      2. state ∈ {testing, swept, bounced, flipped}（idle/approaching/broken 跳过）
      3. 距 state_ts 在 scalp_signal_expire_sec 以内（默认 30 分钟窗口）
      4. |distance_pct| ≤ scalp_max_distance_pct
      5. cascade_risk < scalp_max_cascade
      6. 15m K 线反转形态 strength ≥ scalp_min_pattern_strength
      7. 最终 R:R ≥ scalp_min_rr

    无 15m K 线数据、ATR 异常、不符合条件 → 返回 None。
    """
    # 可观测性：S/A 级 level 才打调试日志（避免刷屏），记录每道过滤的命中原因
    # 目的：排障时能直接回答"为什么这根 S 级阻力没产出 scalp"，而不是人工反推阈值表。
    is_candidate = lv.strength_tier in ("S", "A")

    def _skip(reason: str) -> None:
        if is_candidate and logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "[scalp-skip] %s@%s tier=%s state=%s dist=%.3f%% cascade=%.2f reason=%s",
                lv.side, f"{lv.price:.2f}", lv.strength_tier, lv.state,
                lv.distance_pct or 0, lv.cascade_risk or 0, reason,
            )

    if atr <= 0 or price <= 0:
        _skip("atr_or_price_nonpositive")
        return None
    if lv.strength_tier not in ("S", "A"):
        return None  # 非候选静默，不打日志
    if lv.state not in ("testing", "swept", "bounced", "flipped"):
        _skip(f"state={lv.state}_not_eligible")
        return None

    # 时间过期：scalp 是日内策略，状态进入超过 scalp_signal_expire_sec 即失效
    # （防止关键位长期停留在 testing 状态仍反复产出 scalp 信号）
    scalp_expire = cfg.get("scalp_signal_expire_sec", 1800)
    if lv.state_ts > 0 and (now - lv.state_ts) > scalp_expire:
        _skip(f"expired_age={now - lv.state_ts}s>{scalp_expire}s")
        return None

    max_distance = cfg.get("scalp_max_distance_pct", 0.8)
    if abs(lv.distance_pct) > max_distance:
        _skip(f"distance={abs(lv.distance_pct):.3f}%>max_{max_distance}%")
        return None

    max_cascade = cfg.get("scalp_max_cascade", 0.5)
    if (lv.cascade_risk or 0) >= max_cascade:
        _skip(f"cascade={lv.cascade_risk:.2f}>=max_{max_cascade}")
        return None

    if not candles_15m or len(candles_15m) < 2:
        _skip("no_15m_candles")
        return None

    pattern = detect_reversal_pattern(candles_15m, lv.side)
    min_strength = cfg.get("scalp_min_pattern_strength", 0.6)
    if not pattern.found or pattern.strength < min_strength:
        _skip(
            f"pattern_weak found={pattern.found} "
            f"strength={pattern.strength:.2f}<min_{min_strength}"
        )
        return None

    is_support = lv.side == "support"
    side_cn = _SIDE_CN.get(lv.side, lv.side)

    # 止损宽度：max(0.2% of price, 0.4×ATR)，上限 0.5×ATR（极小）
    sl_min_pct = cfg.get("scalp_sl_min_pct", 0.2)
    sl_max_atr_mult = cfg.get("scalp_sl_max_atr_mult", 0.5)
    sl_width = max(lv.price * sl_min_pct / 100, atr * 0.4)
    sl_width = min(sl_width, atr * sl_max_atr_mult)
    if sl_width <= 0:
        return None

    # 入场紧贴关键位（0.1×ATR 缓冲，避免成交价正好在关键位失败）
    entry_buffer = min(atr * 0.1, lv.price * 0.0015)
    if is_support:
        entry = lv.price + entry_buffer
        sl = lv.price - sl_width
    else:
        entry = lv.price - entry_buffer
        sl = lv.price + sl_width

    # TP：复用对侧关键位扫描，但传更小的 ATR 下限（0.5×ATR）
    # scalp 的 SL 被限制在 ≤0.5×ATR，若沿用默认 2×ATR 下限会让 R:R 最低恒为 4，
    # 反而让 scalp_min_rr 阈值失效；传 0.5 允许 TP1 贴近最近对侧 level。
    tp_atr_mult = cfg.get("scalp_tp_min_atr_mult", 0.5)
    tp1_price = _find_opposite_target(lv, price, all_levels, atr, min_rr_atr_mult=tp_atr_mult)

    risk = abs(entry - sl)
    reward = abs(tp1_price - entry)
    if risk <= 0:
        _skip("risk_nonpositive")
        return None
    rr = reward / risk

    min_rr = cfg.get("scalp_min_rr", 1.5)
    if rr < min_rr:
        _skip(f"rr={rr:.2f}<min_{min_rr} (tp={tp1_price:.2f} entry={entry:.2f} sl={sl:.2f})")
        return None

    # 置信度：S 级 + 强形态(>=0.8) → A，其余 → B
    confidence = "A" if (lv.strength_tier == "S" and pattern.strength >= 0.8) else "B"
    direction = "做多" if is_support else "做空"
    action = "scalp_long" if is_support else "scalp_short"

    sig = KeyLevelSignal(
        level_price=lv.price,
        side=lv.side,
        state=lv.state,
        action=action,
        confidence=confidence,
        entry_price=round(entry, 2),
        stop_loss=round(sl, 2),
        tp1=round(tp1_price, 2),
        rr_ratio=round(rr, 2),
        reason=(
            f"⚡日内: {lv.strength_tier}级{side_cn}${lv.price:,.0f}"
            f" + 15m{pattern.name}({pattern.strength:.2f})"
            f" → 极小止损{direction}(SL≈${risk:.1f}, 约{risk/price*100:.2f}%)"
        ),
    )
    if lv.cascade_risk and lv.cascade_risk > 0.3:
        sig.warnings.append(f"级联风险{lv.cascade_risk:.0%}，务必硬止损")

    # 填确认项 + MTF/CVD 叠加（scalp 也用同样的一致性确认）
    sig.confirmations.append(f"pattern_{pattern.name}")
    sig.confirmations.append("closed_bar")
    _apply_mtf_cvd(sig, long=is_support, mtf_bias=_mtf_1h_bias(None), cvd=cvd)
    # 注：scalp 不强依赖 1h MTF（日内尺度），故不显式传 candles_1h；
    #    仅 CVD 会起作用
    return _finalize_signal(sig, "scalp")


def _find_opposite_target(
    lv: KeyLevelV2,
    price: float,
    all_levels: list[KeyLevelV2],
    atr: float,
    min_rr_atr_mult: float = 2.0,
) -> float:
    """智能 TP：指向最近的对侧关键位，而非固定 ATR 倍数。

    min_rr_atr_mult：TP 距离 price 的最小 ATR 倍数兜底。
      - snipe/flip 默认 2.0（中线档期望足够 R:R 空间）
      - scalp 应传 0.5（日内档贴近最近对侧 level，不强行抬高 TP；
        否则 min_rr 过滤阈值形同虚设，且 TP1 常态无法达到）
    """
    is_support = lv.side == "support"
    fallback = price + atr * 3 if is_support else price - atr * 3

    if is_support:
        targets = [l for l in all_levels
                   if l.side == "resistance" and l.price > price and l.price != lv.price]
        targets.sort(key=lambda l: l.price)
    else:
        targets = [l for l in all_levels
                   if l.side == "support" and l.price < price and l.price != lv.price]
        targets.sort(key=lambda l: -l.price)

    if targets:
        target = targets[0].price
        if is_support:
            min_rr_target = price + atr * min_rr_atr_mult
            return max(target, min_rr_target)
        else:
            min_rr_target = price - atr * min_rr_atr_mult
            return min(target, min_rr_target)

    return fallback
