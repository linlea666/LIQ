"""关键位生命周期追踪器 V2：ATR 自适应 + 量价突破确认 + 智能 R:R

状态流转（与 V1 相同）：
  IDLE → APPROACHING → TESTING → SWEPT/BOUNCED → (BROKEN → FLIPPED)

V2 改进：
  - 所有阈值根据 ATR / price 动态缩放
  - 突破确认同时检查成交量放大 + OI 变化方向
  - TP 指向对侧最近关键位或 VP POC（而非固定 ATR 倍数）
  - 信号过期：swept/bounced 超过 4H 自动降级
  - 冲突解决：同时多空 A 级信号时参考温度计
"""

from __future__ import annotations

import logging
import time

from models.key_level import KeyLevelSignal, KeyLevelSnapshotV2, KeyLevelV2
from models.liquidation import LiquidationMap
from models.market import CandleData
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
    "flip_zone_pct": 0.5,
    "level_expire_sec": 86400,
    "sweep_proximity_pct": 2.0,
    "cascade_weight_cap_m": 20.0,
    "cascade_norm": 50.0,
    "signal_expire_sec": 14400,  # 4H
}


def run_tracker_v2(
    snapshot: KeyLevelSnapshotV2,
    liq_map: LiquidationMap | None,
    sweep_events_1h: list[dict],
    taker_buy_vol: float = 0,
    taker_sell_vol: float = 0,
    oi_change_pct_1h: float = 0,
    temperature_score: float = 50,
    candles_4h: list[CandleData] | None = None,
    cfg: dict | None = None,
) -> KeyLevelSnapshotV2:
    """在 confluence_scoring 产出的快照基础上，运行状态机 + 信号生成。"""
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
                    taker_buy_vol, taker_sell_vol, oi_change_pct_1h, cfg, now)

    # 级联风险
    if liq_map:
        for lv in snapshot.levels:
            _calc_cascade_risk(lv, liq_map, price, cfg)

    # 信号生成
    signals: list[KeyLevelSignal] = []
    opposite_levels = snapshot.levels
    for lv in snapshot.levels:
        sig = _generate_signal(lv, price, atr, opposite_levels, temperature_score, candles_4h, cfg, now)
        if sig:
            signals.append(sig)

    # 冲突解决：同时有多空 A 级时，按温度计方向保留主信号
    a_signals = [s for s in signals if s.confidence == "A"]
    if len(a_signals) >= 2:
        has_long = any(s.action in ("snipe_long", "flip_long") for s in a_signals)
        has_short = any(s.action in ("snipe_short", "flip_short") for s in a_signals)
        if has_long and has_short:
            # 逆向思维：市场过冷(<40)优先做多，过热(>=40)优先做空
            prefer_long = temperature_score < 40
            for s in a_signals:
                is_long = s.action in ("snipe_long", "flip_long")
                if (prefer_long and not is_long) or (not prefer_long and is_long):
                    s.confidence = "B"
                    s.warnings.append("存在反向A级信号，已降级为B级")

    snapshot.signals = signals
    snapshot.active_count = sum(1 for lv in snapshot.levels if lv.state != "idle")
    return snapshot


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
    cfg: dict,
    now: int,
):
    dist_abs = abs(lv.distance_pct)
    approach_pct = cfg["approach_pct"] * atr_factor
    test_pct = cfg["test_pct"] * atr_factor
    bounce_pct = cfg["bounce_pct"] * atr_factor
    break_depth_pct = cfg["break_depth_pct"] * atr_factor
    break_confirm_sec = cfg["break_confirm_sec"]
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

        # 量价突破确认：时间 + 成交量放大 + OI 变化方向
        if _is_broken(lv, price, break_depth_pct):
            if lv.break_start_ts == 0:
                lv.break_start_ts = now
            else:
                time_ok = (now - lv.break_start_ts) >= break_confirm_sec
                vol_ok = _volume_confirms_break(is_support, taker_buy, taker_sell)
                oi_ok = _oi_confirms_break(oi_change_pct)
                if time_ok and vol_ok and oi_ok:
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
                if vol_ok and oi_ok:
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


def _set_state(lv: KeyLevelV2, new_state: str, now: int):
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


def _calc_cascade_risk(lv: KeyLevelV2, liq_map: LiquidationMap, price: float, cfg: dict):
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

    lv.cascade_layers = min(len(clusters), 5)
    lv.cascade_total_usd = sum(c.total_usd for c in clusters[:5])

    weight_cap = cfg.get("cascade_weight_cap_m", 20.0)
    norm = cfg.get("cascade_norm", 200.0)
    risk_score = 0.0
    prev_price = lv.price
    for c in clusters[:5]:
        gap_pct = abs(c.price_center - prev_price) / max(price, 1) * 100
        gap_pct = max(gap_pct, 0.2)
        weight = min(c.total_usd / 1e6, weight_cap)
        risk_score += weight / gap_pct
        prev_price = c.price_center

    dist_from_price = abs(lv.price - price) / max(price, 1) * 100
    dist_decay = 1.0 / (1.0 + dist_from_price / 3.0)
    lv.cascade_risk = round(min(1.0, risk_score / norm * dist_decay), 2)


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
) -> KeyLevelSignal | None:
    if atr <= 0:
        return None

    if lv.state == "idle":
        if lv.strength_tier in ("S", "A") and abs(lv.distance_pct) <= 15:
            is_support = lv.side == "support"
            direction = "做多" if is_support else "做空"
            return KeyLevelSignal(
                level_price=lv.price, side=lv.side, state=lv.state,
                action="wait_approach",
                confidence="C",
                reason=(
                    f"前瞻观察: {lv.strength_tier}级"
                    f"{_SIDE_CN.get(lv.side, lv.side)}${lv.price:,.0f}"
                    f"({lv.source_count}维共振, 距{abs(lv.distance_pct):.1f}%), "
                    f"价格接近时关注{direction}机会"
                ),
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
        return base

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
        return base

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
        return base

    if lv.state == "bounced":
        has_sweep = lv.sweep_usd > 0
        pattern = detect_reversal_pattern(candles_4h, lv.side)
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
        return base

    if lv.state == "broken":
        base.action = "wait_approach"
        base.reason = f"{side_cn}${lv.price:,.0f}已被突破，等待回踩确认S/R翻转"
        base.confidence = "C"
        base.warnings.append("突破后不要追单，等回踩")
        return base

    if lv.state == "flipped":
        pattern = detect_reversal_pattern(candles_4h, lv.side)
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
        return base

    return None


def _find_opposite_target(
    lv: KeyLevelV2,
    price: float,
    all_levels: list[KeyLevelV2],
    atr: float,
) -> float:
    """智能 TP：指向最近的对侧关键位，而非固定 ATR 倍数。"""
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
            min_rr_target = price + atr * 2
            return max(target, min_rr_target)
        else:
            min_rr_target = price - atr * 2
            return min(target, min_rr_target)

    return fallback
