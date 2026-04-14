"""关键位生命周期追踪器：状态机 + 级联风险 + 信号生成

状态流转：
  IDLE → APPROACHING → TESTING → SWEPT/BOUNCED → (BROKEN → FLIPPED)
每个关键位来自 levels.py 的支撑/阻力列表 + range_signal 箱体边界，
由价格变化 + sweep 事件驱动状态转移。
"""

from __future__ import annotations

import logging
import time

from models.key_level import KeyLevel, KeyLevelSignal, KeyLevelSnapshot
from models.levels import LevelAnalysis, PriceLevel
from models.liquidation import LiquidationMap

logger = logging.getLogger(__name__)

_SIDE_CN = {"support": "支撑", "resistance": "阻力"}

_DEFAULT_CFG: dict = {
    "approach_pct": 2.0,
    "test_pct": 0.5,
    "bounce_pct": 1.0,
    "break_confirm_sec": 300,
    "break_depth_pct": 0.3,
    "flip_zone_pct": 0.5,
    "max_tracked_levels": 8,
    "level_expire_sec": 86400,
    "sweep_proximity_pct": 2.0,
    "cascade_weight_cap_m": 20.0,
    "cascade_norm": 50.0,
}


def update_key_levels(
    prev_levels: list[KeyLevel],
    current_price: float,
    levels: LevelAnalysis | None,
    liq_map: LiquidationMap | None,
    range_upper: float | None,
    range_lower: float | None,
    sweep_events_1h: list[dict],
    atr: float,
    cfg: dict | None = None,
) -> KeyLevelSnapshot:
    """更新关键位状态并生成信号。

    在 engine._recompute 中每次推送前调用，输入当前价格和最新数据，
    输出更新后的关键位列表和交易信号。
    """
    cfg = {**_DEFAULT_CFG, **(cfg or {})}
    now = int(time.time())

    # ── 1. 收集候选关键位 ──
    candidates = _collect_candidates(levels, range_upper, range_lower, current_price)

    # ── 2. 合并到已有追踪列表（按价格匹配，容差 0.2%）──
    tracked = _merge_levels(prev_levels, candidates, current_price, cfg, now)

    # ── 3. 状态机转移 ──
    for lv in tracked:
        _update_distance(lv, current_price)
        _transition(lv, current_price, atr, sweep_events_1h, cfg, now)

    # ── 4. 级联风险计算 ──
    if liq_map:
        for lv in tracked:
            _calc_cascade_risk(lv, liq_map, current_price, cfg)

    # ── 5. 生成信号 ──
    signals = []
    for lv in tracked:
        sig = _generate_signal(lv, current_price, atr, cfg)
        if sig:
            signals.append(sig)

    active_count = sum(1 for lv in tracked if lv.state != "idle")

    return KeyLevelSnapshot(
        ts=now,
        levels=tracked,
        signals=signals,
        active_count=active_count,
    )


# ── 候选位收集 ──

def _collect_candidates(
    levels: LevelAnalysis | None,
    range_upper: float | None,
    range_lower: float | None,
    current_price: float,
) -> list[KeyLevel]:
    """从 levels + range_signal 收集候选关键位。"""
    seen: set[int] = set()  # 价格 hash 去重（精度到整数）
    result: list[KeyLevel] = []

    if levels:
        for pl in levels.supports[:4]:
            key = int(pl.price)
            if key not in seen:
                seen.add(key)
                result.append(_from_price_level(pl))
        for pl in levels.resistances[:4]:
            key = int(pl.price)
            if key not in seen:
                seen.add(key)
                result.append(_from_price_level(pl))

    if range_upper and int(range_upper) not in seen:
        seen.add(int(range_upper))
        result.append(KeyLevel(
            price=range_upper, side="resistance",
            sources=["range_box_upper"], strength=2,
        ))
    if range_lower and int(range_lower) not in seen:
        seen.add(int(range_lower))
        result.append(KeyLevel(
            price=range_lower, side="support",
            sources=["range_box_lower"], strength=2,
        ))

    return result


def _from_price_level(pl: PriceLevel) -> KeyLevel:
    return KeyLevel(
        price=pl.price,
        side=pl.level_type,
        sources=pl.sources[:4],
        strength=min(5, max(1, len(pl.sources))),
    )


# ── 合并逻辑 ──

def _merge_levels(
    prev: list[KeyLevel],
    candidates: list[KeyLevel],
    current_price: float,
    cfg: dict,
    now: int,
) -> list[KeyLevel]:
    """将候选位合并到已有追踪列表，保留活跃状态。"""
    max_levels = cfg["max_tracked_levels"]
    expire_sec = cfg["level_expire_sec"]
    merge_tol = 0.002  # 0.2% 价格容差

    # 以已有追踪列表为基础
    result: list[KeyLevel] = []
    matched_cand: set[int] = set()

    for lv in prev:
        # 清除过期的 idle 位
        if lv.state == "idle" and (now - lv.state_ts) > expire_sec:
            continue
        # 在候选中找匹配的，更新 sources/strength
        for i, cand in enumerate(candidates):
            if i in matched_cand:
                continue
            if abs(lv.price - cand.price) / max(lv.price, 1) < merge_tol:
                lv.sources = list(set(lv.sources + cand.sources))[:6]
                lv.strength = min(5, max(1, len(lv.sources)))
                matched_cand.add(i)
                break
        result.append(lv)

    # 加入未匹配的新候选
    for i, cand in enumerate(candidates):
        if i not in matched_cand:
            cand.state_ts = now
            result.append(cand)

    # 按距当前价排序，截断到 max_levels
    for lv in result:
        _update_distance(lv, current_price)

    # 优先保留非 idle 的，然后按距离近排序
    result.sort(key=lambda lv: (0 if lv.state != "idle" else 1, abs(lv.distance_pct)))
    return result[:max_levels]


def _update_distance(lv: KeyLevel, price: float):
    if price > 0:
        lv.distance_pct = round((lv.price - price) / price * 100, 3)


# ── 状态机转移 ──

def _transition(
    lv: KeyLevel,
    price: float,
    atr: float,
    sweep_events: list[dict],
    cfg: dict,
    now: int,
):
    """驱动单个关键位的状态转移。"""
    dist_abs = abs(lv.distance_pct)
    approach_pct = cfg["approach_pct"]
    test_pct = cfg["test_pct"]
    bounce_pct = cfg["bounce_pct"]
    break_depth_pct = cfg["break_depth_pct"]
    break_confirm_sec = cfg["break_confirm_sec"]
    flip_zone_pct = cfg["flip_zone_pct"]

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
        # 更新测试期间的极值
        if is_support:
            if lv.lowest_wick is None or price < lv.lowest_wick:
                lv.lowest_wick = price
        else:
            if lv.lowest_wick is None or price > lv.lowest_wick:
                lv.lowest_wick = price

        # 检查 sweep（需价格邻近）
        swept = _check_sweep(lv, sweep_events, cfg.get("sweep_proximity_pct", 2.0))
        if swept:
            lv.sweep_usd = swept
            _set_state(lv, "swept", now)
            return

        # 检查突破
        if _is_broken(lv, price, break_depth_pct):
            if lv.break_start_ts == 0:
                lv.break_start_ts = now
            elif (now - lv.break_start_ts) >= break_confirm_sec:
                _set_state(lv, "broken", now)
                return
        else:
            lv.break_start_ts = 0

        # 检查反弹
        if dist_abs > bounce_pct:
            price_on_safe_side = (
                (is_support and price > lv.price) or
                (not is_support and price < lv.price)
            )
            if price_on_safe_side:
                _set_state(lv, "bounced", now)

    elif lv.state == "swept":
        # 扫取后：如果价格反弹远离 → 维持 swept；如果深度突破 → broken
        if _is_broken(lv, price, break_depth_pct):
            if lv.break_start_ts == 0:
                lv.break_start_ts = now
            elif (now - lv.break_start_ts) >= break_confirm_sec:
                _set_state(lv, "broken", now)
        else:
            lv.break_start_ts = 0
        # swept 后价格大幅远离（>bounce_pct 且在安全侧）→ 回到 bounced
        if dist_abs > bounce_pct:
            safe = (is_support and price > lv.price) or (not is_support and price < lv.price)
            if safe:
                _set_state(lv, "bounced", now)

    elif lv.state == "bounced":
        # 反弹后可能重新测试
        if dist_abs <= test_pct:
            _set_state(lv, "testing", now)
            lv.test_count += 1
        elif dist_abs > approach_pct * 2:
            _set_state(lv, "idle", now)

    elif lv.state == "broken":
        # 突破后检测回踩（经典 S/R 翻转）
        # 支撑跌破后：价格从下方接近该位（回踩阻力）→ flipped
        # 阻力突破后：价格从上方接近该位（回踩支撑）→ flipped
        if dist_abs <= flip_zone_pct:
            on_broken_side = (
                (is_support and price < lv.price) or
                (not is_support and price > lv.price)
            )
            if on_broken_side:
                _set_state(lv, "flipped", now)
                lv.side = "resistance" if is_support else "support"

    elif lv.state == "flipped":
        # 翻转确认后如果价格远离，回到 idle
        if dist_abs > approach_pct * 2:
            _set_state(lv, "idle", now)


def _set_state(lv: KeyLevel, new_state: str, now: int):
    lv.prev_state = lv.state
    lv.state = new_state
    lv.state_ts = now
    lv.break_start_ts = 0


def _is_broken(lv: KeyLevel, price: float, depth_pct: float) -> bool:
    """价格是否穿透了关键位（超过 depth_pct）"""
    if lv.price <= 0:
        return False
    if lv.side == "support":
        return price < lv.price * (1 - depth_pct / 100)
    else:
        return price > lv.price * (1 + depth_pct / 100)


def _check_sweep(lv: KeyLevel, sweep_events: list[dict], proximity_pct: float = 2.0) -> float:
    """检查最近 1h sweep 事件是否覆盖了该关键位（需价格邻近）。"""
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


# ── 级联风险计算 ──

def _calc_cascade_risk(lv: KeyLevel, liq_map: LiquidationMap, current_price: float, cfg: dict):
    """计算如果该关键位被突破后的级联穿透风险。"""
    if lv.side == "support":
        clusters = [c for c in liq_map.clusters_below if c.price_center < lv.price]
        clusters.sort(key=lambda c: c.price_center, reverse=True)
    else:
        clusters = [c for c in liq_map.clusters_above if c.price_center > lv.price]
        clusters.sort(key=lambda c: c.price_center)

    if not clusters:
        lv.cascade_risk = 0
        lv.cascade_layers = 0
        lv.cascade_total_usd = 0
        return

    lv.cascade_layers = len(clusters)
    lv.cascade_total_usd = sum(c.total_usd for c in clusters)

    weight_cap = cfg.get("cascade_weight_cap_m", 20.0)
    norm = cfg.get("cascade_norm", 50.0)

    risk_score = 0.0
    prev_price = lv.price
    for c in clusters[:5]:
        gap_pct = abs(c.price_center - prev_price) / max(current_price, 1) * 100
        gap_pct = max(gap_pct, 0.1)
        weight = min(c.total_usd / 1e6, weight_cap)
        risk_score += weight / gap_pct
        prev_price = c.price_center

    lv.cascade_risk = round(min(1.0, risk_score / norm), 2)


# ── 信号生成 ──

def _generate_signal(
    lv: KeyLevel,
    price: float,
    atr: float,
    cfg: dict,
) -> KeyLevelSignal | None:
    """根据关键位状态生成交易信号。"""
    if lv.state == "idle":
        return None
    if atr <= 0:
        return None

    is_support = lv.side == "support"
    side_cn = _SIDE_CN.get(lv.side, lv.side)
    base = KeyLevelSignal(
        level_price=lv.price,
        side=lv.side,
        state=lv.state,
        action="wait_approach",
        reason="",
    )

    if lv.state == "approaching":
        base.action = "wait_approach"
        direction = "做多" if is_support else "做空"
        base.reason = f"价格正在接近{side_cn}位${lv.price:,.0f}，准备关注{direction}机会"
        base.confidence = "C"
        return base

    if lv.state == "testing":
        base.action = "wait_sweep"
        wick_info = ""
        if lv.lowest_wick is not None and abs(lv.lowest_wick - lv.price) > atr * 0.05:
            wick_info = f"，已触及${lv.lowest_wick:,.0f}"
        base.reason = f"价格正在测试{side_cn}位${lv.price:,.0f}{wick_info}，等待流动性扫取确认后入场"
        base.confidence = "C"
        if lv.cascade_risk > 0.7:
            base.warnings.append(f"级联风险{lv.cascade_risk:.0%}，突破后可能瀑布")
        return base

    if lv.state == "swept":
        if is_support:
            entry = lv.price + atr * 0.15
            sl = lv.price - atr * 1.5
            tp1 = price + atr * 3 if price > 0 else entry + atr * 3
            base.action = "snipe_long"
            base.reason = (
                f"{side_cn}${lv.price:,.0f}下方流动性已被扫取(${lv.sweep_usd/1e6:.1f}M)，"
                f"空头弹药耗尽 → A级做多"
            )
        else:
            entry = lv.price - atr * 0.15
            sl = lv.price + atr * 1.5
            tp1 = price - atr * 3 if price > 0 else entry - atr * 3
            base.action = "snipe_short"
            base.reason = (
                f"{side_cn}${lv.price:,.0f}上方流动性已被扫取(${lv.sweep_usd/1e6:.1f}M)，"
                f"多头弹药耗尽 → A级做空"
            )
        base.confidence = "A"
        base.entry_price = round(entry, 2)
        base.stop_loss = round(sl, 2)
        base.tp1 = round(tp1, 2)
        risk = abs(entry - sl)
        reward = abs(tp1 - entry)
        base.rr_ratio = round(reward / risk, 1) if risk > 0 else 0
        if lv.cascade_risk > 0.5:
            base.warnings.append(f"注意级联风险{lv.cascade_risk:.0%}，建议减仓")
        return base

    if lv.state == "bounced":
        has_sweep = lv.sweep_usd > 0
        if is_support:
            entry = lv.price + atr * 0.2
            sl = lv.price - atr * 1.2
            tp1 = price + atr * 2.5 if price > 0 else entry + atr * 2.5
            base.action = "snipe_long"
            if has_sweep:
                base.reason = (
                    f"{side_cn}${lv.price:,.0f}流动性扫取后反弹确认"
                    f"(${lv.sweep_usd/1e6:.1f}M) → A级做多"
                )
            else:
                base.reason = f"{side_cn}${lv.price:,.0f}反弹确认(第{lv.test_count}次测试) → B级做多"
        else:
            entry = lv.price - atr * 0.2
            sl = lv.price + atr * 1.2
            tp1 = price - atr * 2.5 if price > 0 else entry - atr * 2.5
            base.action = "snipe_short"
            if has_sweep:
                base.reason = (
                    f"{side_cn}${lv.price:,.0f}流动性扫取后受阻确认"
                    f"(${lv.sweep_usd/1e6:.1f}M) → A级做空"
                )
            else:
                base.reason = f"{side_cn}${lv.price:,.0f}受阻确认(第{lv.test_count}次测试) → B级做空"
        base.confidence = "A" if has_sweep else "B"
        base.entry_price = round(entry, 2)
        base.stop_loss = round(sl, 2)
        base.tp1 = round(tp1, 2)
        risk = abs(entry - sl)
        reward = abs(tp1 - entry)
        base.rr_ratio = round(reward / risk, 1) if risk > 0 else 0
        return base

    if lv.state == "broken":
        base.action = "wait_approach"
        base.reason = f"{side_cn}${lv.price:,.0f}已被突破，等待回踩确认S/R翻转"
        base.confidence = "C"
        base.warnings.append("突破后不要追单，等回踩")
        return base

    if lv.state == "flipped":
        if lv.side == "resistance":
            entry = lv.price - atr * 0.1
            sl = lv.price + atr * 1.2
            tp1 = entry - atr * 3
            base.action = "flip_short"
            base.reason = (
                f"原支撑${lv.price:,.0f}已翻转为阻力，价格回踩被拒 → A级翻转做空"
            )
        else:
            entry = lv.price + atr * 0.1
            sl = lv.price - atr * 1.2
            tp1 = entry + atr * 3
            base.action = "flip_long"
            base.reason = (
                f"原阻力${lv.price:,.0f}已翻转为支撑，价格回踩获撑 → A级翻转做多"
            )
        base.confidence = "A"
        base.entry_price = round(entry, 2)
        base.stop_loss = round(sl, 2)
        base.tp1 = round(tp1, 2)
        risk = abs(entry - sl)
        reward = abs(tp1 - entry)
        base.rr_ratio = round(reward / risk, 1) if risk > 0 else 0
        return base

    return None
