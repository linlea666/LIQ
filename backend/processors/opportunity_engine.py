"""高盈亏比观察区候选引擎（OpportunityEngine · MVP）。

定位
====
从 BrainPriceZone 派生 TradeSetupCandidate（"低成本试错"观察区）。
**只输出观察区，不输出交易指令**：

- 后端字段使用 long/short/neutral 描述方向（结构需要）；
- 前端 UI 必须转译为「做多观察 / 做空观察 / 等待」措辞；
- 任何下单、仓位、概率预测均不在本模块产出。

铁律（与 V3 关键位 / 流动性墙引擎保持一致）
========================================
1. 不重新计算 KL 评分（不动 final_score/strength_tier/cascade_risk）。
2. 不修改 WallZone 评分字段。
3. break_through_risk 仍称"打穿风险评分"，不当概率。
4. 严筛门槛 + |distance| ≤ 1.5%，避免噪声。
5. 数据 stale / 不齐 → 降权 + cancel_conditions 追加 "数据 stale"。

MVP 支持的 setup 类型（3+1 = 4）
==============================
- support_limit_probe       ：防守位限价试错（做多观察）
- resistance_limit_probe    ：阻力位限价试错（做空观察）
- fake_break_reclaim_long   ：扫破支撑后收回（做多观察，前置态=等待）
- fake_break_reclaim_short  ：扫破阻力后收回（做空观察，前置态=等待）
"""
from __future__ import annotations

import hashlib
import time
from typing import Optional

from models.trading_brain import (
    BrainContextChips,
    BrainDataQuality,
    BrainPriceZone,
    SetupEntryStyle,
    SetupRiskPlan,
    SetupState,
    SetupTarget,
    TradeSetupCandidate,
)


# ── 严筛门槛（GPT 第 15 节翻译为代码常量；保守起见不暴露给配置层）─────────
_MIN_SUPPORT_TRUST = 0.70
_MIN_RESISTANCE_TRUST = 0.70
_MIN_RR_T1 = 2.0
_MIN_DATA_CONFIDENCE = 0.75
_MAX_DISTANCE_PCT = 1.5

# ── 失效结构（按 ATR 计算）────────────────────────────────────────────
_SOFT_BUFFER_ATR = 0.30
_HARD_BUFFER_ATR = 0.80


def _make_setup_id(coin: str, zone_id: str, setup_type: str) -> str:
    digest = hashlib.sha1(f"{coin}|{zone_id}|{setup_type}".encode()).hexdigest()[:10]
    return f"{coin.upper()}_{setup_type}_{digest}"


def _round_price(p: float) -> float:
    return round(p, 4)


def _safe_atr(atr: float, last_price: float) -> float:
    """ATR 兜底：避免 ATR=0 导致止损零距离。"""
    if atr and atr > 0:
        return atr
    return max(last_price * 0.003, 1e-6)


def _stale_or_missing(dq: Optional[BrainDataQuality]) -> bool:
    if dq is None:
        return True
    if dq.is_partial_ready:
        return True
    if dq.stale_sources or dq.missing_sources:
        return True
    return False


def _regime_blocks_long(ctx: Optional[BrainContextChips]) -> bool:
    if ctx is None:
        return False
    r = (ctx.regime or "").lower()
    return r in ("trend_down", "down_trend", "bearish_trend")


def _regime_blocks_short(ctx: Optional[BrainContextChips]) -> bool:
    if ctx is None:
        return False
    r = (ctx.regime or "").lower()
    return r in ("trend_up", "up_trend", "bullish_trend")


# ── targets：从其它 zones 找 T1/T2/T3 ─────────────────────────────────
def _select_targets_for_long(
    *, zone: BrainPriceZone, all_zones: list[BrainPriceZone], hard_stop: float,
) -> list[SetupTarget]:
    above = [z for z in all_zones if z.price_mid > zone.price_high]
    above.sort(key=lambda z: z.price_mid)
    out: list[SetupTarget] = []
    seen: set[str] = set()
    for z in above:
        if z.zone_id in seen:
            continue
        seen.add(z.zone_id)
        risk = max(zone.price_mid - hard_stop, 1e-9)
        reward = z.price_mid - zone.price_mid
        rr = round(reward / risk, 2)
        if rr <= 0:
            continue
        ttype = (
            "spot_wall" if z.dominant_role == "spot_defense"
            else "short_liq_magnet" if z.dominant_role in ("liquidation_magnet", "futures_target")
            else "key_level"
        )
        out.append(SetupTarget(
            price=_round_price(z.price_mid),
            type=ttype,
            rr=rr,
            note=z.dominant_label,
        ))
        if len(out) >= 3:
            break
    return out


def _select_targets_for_short(
    *, zone: BrainPriceZone, all_zones: list[BrainPriceZone], hard_stop: float,
) -> list[SetupTarget]:
    below = [z for z in all_zones if z.price_mid < zone.price_low]
    below.sort(key=lambda z: -z.price_mid)
    out: list[SetupTarget] = []
    seen: set[str] = set()
    for z in below:
        if z.zone_id in seen:
            continue
        seen.add(z.zone_id)
        risk = max(hard_stop - zone.price_mid, 1e-9)
        reward = zone.price_mid - z.price_mid
        rr = round(reward / risk, 2)
        if rr <= 0:
            continue
        ttype = (
            "spot_wall" if z.dominant_role == "spot_defense"
            else "long_liq_magnet" if z.dominant_role in ("liquidation_magnet", "futures_target")
            else "key_level"
        )
        out.append(SetupTarget(
            price=_round_price(z.price_mid),
            type=ttype,
            rr=rr,
            note=z.dominant_label,
        ))
        if len(out) >= 3:
            break
    return out


# ── scores ────────────────────────────────────────────────────────────
def _asymmetry_score(
    *, zone: BrainPriceZone, last_price: float, hard_stop: float,
    targets: list[SetupTarget], data_confidence: float, direction: str,
) -> float:
    """asymmetry = rr_score × invalidation_clarity × entry_proximity ×
                   target_quality × liquidity_path × data_confidence
    （0–1，越高越值得低成本试错；UI 显示「不对称评分」）。
    """
    if not targets:
        return 0.0
    rr_top = max(t.rr for t in targets)
    rr_score = min(1.0, rr_top / 6.0)

    risk = abs(zone.price_mid - hard_stop)
    invalidation_clarity = min(1.0, risk / max(zone.price_mid * 0.005, 1e-9))
    invalidation_clarity = min(1.0, max(0.3, invalidation_clarity))

    proximity = 1.0 - min(abs(zone.distance_pct) / _MAX_DISTANCE_PCT, 1.0)
    proximity = max(0.2, proximity)

    quality_per_t = []
    for t in targets:
        if t.type in ("short_liq_magnet", "long_liq_magnet"):
            quality_per_t.append(0.8)
        elif t.type == "spot_wall":
            quality_per_t.append(0.9)
        elif t.type == "key_level":
            quality_per_t.append(0.75)
        else:
            quality_per_t.append(0.6)
    target_quality = sum(quality_per_t) / len(quality_per_t)

    liq_path = 1.0 - min(zone.break_through_risk * 0.5, 0.5)

    raw = (
        rr_score * invalidation_clarity * proximity * target_quality
        * liq_path * max(0.4, data_confidence)
    )
    return round(min(1.0, raw), 3)


def _opportunity_score(
    *, asymmetry: float, confirmation: float, key_level_quality: float,
    liquidity_path: float, regime_fit: float, data_confidence: float,
    execution_risk_penalty: float,
) -> float:
    raw = (
        0.30 * asymmetry
        + 0.20 * confirmation
        + 0.15 * key_level_quality
        + 0.15 * liquidity_path
        + 0.10 * regime_fit
        + 0.10 * data_confidence
        - execution_risk_penalty
    )
    return round(max(0.0, min(1.0, raw)), 3)


# ── cancel conditions（每个 setup 类型一份基础模板）──────────────────
_LONG_CANCEL_TEMPLATE = [
    "现货买墙撤出",
    "Coinbase 现货买墙消失",
    "合约买墙被持续吃掉且不重挂",
    "下方多头清算磁铁明显增厚或更靠近",
    "合约 CVD 持续下行超 30 分钟",
    "OI 上升 + 价格下跌（空头开仓增强）",
    "Funding 多头拥挤恶化",
    "Regime 切换为 trend_down",
    "数据 stale 或主源缺失",
]

_SHORT_CANCEL_TEMPLATE = [
    "现货卖墙撤出",
    "Coinbase 现货卖墙消失",
    "合约卖墙被持续吃掉且不重挂",
    "上方空头清算磁铁明显增厚或更靠近",
    "合约 CVD 持续上行超 30 分钟",
    "OI 上升 + 价格上涨（多头开仓增强）",
    "Funding 空头拥挤恶化",
    "Regime 切换为 trend_up",
    "数据 stale 或主源缺失",
]


# ── 各 setup 构造函数 ────────────────────────────────────────────────
def _build_support_limit_probe(
    *, zone: BrainPriceZone, all_zones: list[BrainPriceZone],
    last_price: float, atr: float, ctx: Optional[BrainContextChips],
    dq: Optional[BrainDataQuality],
) -> Optional[TradeSetupCandidate]:
    if zone.dominant_role not in ("spot_defense", "contested"):
        return None
    if zone.support_trust < _MIN_SUPPORT_TRUST:
        return None
    if abs(zone.distance_pct) > _MAX_DISTANCE_PCT:
        return None
    if zone.data_confidence < _MIN_DATA_CONFIDENCE:
        return None
    if _regime_blocks_long(ctx):
        return None

    a = _safe_atr(atr, last_price)
    soft = _round_price(zone.price_low - _SOFT_BUFFER_ATR * a)
    hard = _round_price(zone.price_low - _HARD_BUFFER_ATR * a)

    targets = _select_targets_for_long(
        zone=zone, all_zones=all_zones, hard_stop=hard,
    )
    if not targets or max(t.rr for t in targets) < _MIN_RR_T1:
        return None

    aggressive = SetupEntryStyle(
        style="aggressive",
        entry_zone=(_round_price(zone.price_low), _round_price(zone.price_high)),
        requires=["现价距入场区 ≤ 0.5×ATR", "active_attack 评分不高（<0.6）"],
        risk_note="入场点位好，但容易被瞬时扫到 hard_stop",
    )
    conservative = SetupEntryStyle(
        style="conservative",
        entry_zone=(
            _round_price(soft),
            _round_price(zone.price_low),
        ),
        requires=[
            "支撑被刺破后 5min 内收回",
            "现货 CVD 不再创新低",
            "现货买墙重挂",
        ],
        risk_note="确认更强但可能错过快速反弹",
    )

    asym = _asymmetry_score(
        zone=zone, last_price=last_price, hard_stop=hard,
        targets=targets, data_confidence=zone.data_confidence,
        direction="long",
    )
    conf_score = 0.4 if zone.dominant_role == "spot_defense" else 0.55
    opp = _opportunity_score(
        asymmetry=asym,
        confirmation=conf_score,
        key_level_quality=min(1.0, zone.support_trust),
        liquidity_path=1.0 - min(zone.break_through_risk * 0.5, 0.5),
        regime_fit=0.6 if not ctx else (0.8 if (ctx.regime or "").lower() in ("range", "consolidation") else 0.5),
        data_confidence=zone.data_confidence,
        execution_risk_penalty=0.10 if _stale_or_missing(dq) else 0.0,
    )

    cancel = list(_LONG_CANCEL_TEMPLATE)
    if _stale_or_missing(dq):
        cancel.insert(0, "[已触发] 数据未就绪/部分源 stale，已降权")

    pending_reason = "等待价格回到入场区或扫破后收回"

    return TradeSetupCandidate(
        setup_id=_make_setup_id(zone.coin, zone.zone_id, "support_limit_probe"),
        coin=zone.coin,
        zone_id=zone.zone_id,
        setup_type="support_limit_probe",
        direction="long",
        entry_styles=[aggressive, conservative],
        risk_plan=SetupRiskPlan(
            soft_invalidation=soft,
            hard_stop=hard,
            structural_invalidation=f"15m 收盘跌破 {hard} 且未收回",
            stop_logic=[
                "跌破软失效但快速收回不立即判失败",
                "跌破硬止损且无法收回判结构失败",
                "现货买墙撤出则取消观察",
            ],
        ),
        targets=targets,
        asymmetry_score=asym,
        opportunity_score=opp,
        data_confidence=zone.data_confidence,
        state=SetupState(
            name="forming" if abs(zone.distance_pct) > 0.5 else "waiting_for_trigger",
            since_ts=int(time.time()),
            pending_reason=pending_reason,
        ),
        cancel_conditions=cancel,
        evidence=zone.evidence[:6],
        notes=[zone.dominant_label],
    )


def _build_resistance_limit_probe(
    *, zone: BrainPriceZone, all_zones: list[BrainPriceZone],
    last_price: float, atr: float, ctx: Optional[BrainContextChips],
    dq: Optional[BrainDataQuality],
) -> Optional[TradeSetupCandidate]:
    if zone.dominant_role not in ("spot_defense", "contested"):
        return None
    if zone.resistance_trust < _MIN_RESISTANCE_TRUST:
        return None
    if abs(zone.distance_pct) > _MAX_DISTANCE_PCT:
        return None
    if zone.data_confidence < _MIN_DATA_CONFIDENCE:
        return None
    if _regime_blocks_short(ctx):
        return None

    a = _safe_atr(atr, last_price)
    soft = _round_price(zone.price_high + _SOFT_BUFFER_ATR * a)
    hard = _round_price(zone.price_high + _HARD_BUFFER_ATR * a)

    targets = _select_targets_for_short(
        zone=zone, all_zones=all_zones, hard_stop=hard,
    )
    if not targets or max(t.rr for t in targets) < _MIN_RR_T1:
        return None

    aggressive = SetupEntryStyle(
        style="aggressive",
        entry_zone=(_round_price(zone.price_low), _round_price(zone.price_high)),
        requires=["现价距入场区 ≤ 0.5×ATR", "active_attack 评分不高（<0.6）"],
        risk_note="入场点位好，但容易被瞬时插针扫到 hard_stop",
    )
    conservative = SetupEntryStyle(
        style="conservative",
        entry_zone=(
            _round_price(zone.price_high),
            _round_price(soft),
        ),
        requires=[
            "阻力被刺破后 5min 内收回",
            "现货 CVD 不再创新高",
            "现货卖墙重挂",
        ],
        risk_note="确认更强但可能错过快速回落",
    )

    asym = _asymmetry_score(
        zone=zone, last_price=last_price, hard_stop=hard,
        targets=targets, data_confidence=zone.data_confidence,
        direction="short",
    )
    conf_score = 0.4 if zone.dominant_role == "spot_defense" else 0.55
    opp = _opportunity_score(
        asymmetry=asym,
        confirmation=conf_score,
        key_level_quality=min(1.0, zone.resistance_trust),
        liquidity_path=1.0 - min(zone.break_through_risk * 0.5, 0.5),
        regime_fit=0.6 if not ctx else (0.8 if (ctx.regime or "").lower() in ("range", "consolidation") else 0.5),
        data_confidence=zone.data_confidence,
        execution_risk_penalty=0.10 if _stale_or_missing(dq) else 0.0,
    )

    cancel = list(_SHORT_CANCEL_TEMPLATE)
    if _stale_or_missing(dq):
        cancel.insert(0, "[已触发] 数据未就绪/部分源 stale，已降权")

    return TradeSetupCandidate(
        setup_id=_make_setup_id(zone.coin, zone.zone_id, "resistance_limit_probe"),
        coin=zone.coin,
        zone_id=zone.zone_id,
        setup_type="resistance_limit_probe",
        direction="short",
        entry_styles=[aggressive, conservative],
        risk_plan=SetupRiskPlan(
            soft_invalidation=soft,
            hard_stop=hard,
            structural_invalidation=f"15m 收盘突破 {hard} 且未回落",
            stop_logic=[
                "突破软失效但快速回落不立即判失败",
                "突破硬止损且无法回落判结构失败",
                "现货卖墙撤出则取消观察",
            ],
        ),
        targets=targets,
        asymmetry_score=asym,
        opportunity_score=opp,
        data_confidence=zone.data_confidence,
        state=SetupState(
            name="forming" if abs(zone.distance_pct) > 0.5 else "waiting_for_trigger",
            since_ts=int(time.time()),
            pending_reason="等待价格回到入场区或扫破后回落",
        ),
        cancel_conditions=cancel,
        evidence=zone.evidence[:6],
        notes=[zone.dominant_label],
    )


def _build_fake_break_reclaim(
    *, zone: BrainPriceZone, all_zones: list[BrainPriceZone],
    last_price: float, atr: float, ctx: Optional[BrainContextChips],
    dq: Optional[BrainDataQuality], side: str,
) -> Optional[TradeSetupCandidate]:
    """扫破后收回；前置态固定为 forming/waiting，方向 long/short 由 side 决定。

    与 limit_probe 的差异：
      - 入场区位于 zone 的"远侧"（支撑下方/阻力上方）
      - 必须等扫破 + 收回 + CVD 不再恶化 才进入 triggered
      - 默认 direction=neutral 表示等待，UI 显示「等待」
    """
    if side == "long":
        if zone.dominant_role not in ("spot_defense", "contested"):
            return None
        if zone.support_trust < _MIN_SUPPORT_TRUST - 0.05:
            return None
        if _regime_blocks_long(ctx):
            return None
    else:
        if zone.dominant_role not in ("spot_defense", "contested"):
            return None
        if zone.resistance_trust < _MIN_RESISTANCE_TRUST - 0.05:
            return None
        if _regime_blocks_short(ctx):
            return None

    if abs(zone.distance_pct) > _MAX_DISTANCE_PCT:
        return None
    if zone.data_confidence < _MIN_DATA_CONFIDENCE - 0.05:
        return None

    a = _safe_atr(atr, last_price)
    if side == "long":
        soft = _round_price(zone.price_low - _SOFT_BUFFER_ATR * a)
        hard = _round_price(zone.price_low - (_HARD_BUFFER_ATR + 0.4) * a)
        targets = _select_targets_for_long(
            zone=zone, all_zones=all_zones, hard_stop=hard,
        )
    else:
        soft = _round_price(zone.price_high + _SOFT_BUFFER_ATR * a)
        hard = _round_price(zone.price_high + (_HARD_BUFFER_ATR + 0.4) * a)
        targets = _select_targets_for_short(
            zone=zone, all_zones=all_zones, hard_stop=hard,
        )
    if not targets or max(t.rr for t in targets) < _MIN_RR_T1:
        return None

    if side == "long":
        entry = SetupEntryStyle(
            style="conservative",
            entry_zone=(_round_price(soft), _round_price(zone.price_low)),
            requires=[
                "价格扫破支撑后 5min 内收回",
                "现货 CVD 不再创新低",
                "现货买墙重挂或合约买墙被快速重建",
            ],
            risk_note="必须确认收回，否则取消",
        )
        cancel = list(_LONG_CANCEL_TEMPLATE)
        cancel.insert(0, "支撑被扫后 15min 内未收回")
    else:
        entry = SetupEntryStyle(
            style="conservative",
            entry_zone=(_round_price(zone.price_high), _round_price(soft)),
            requires=[
                "价格扫破阻力后 5min 内回落",
                "现货 CVD 不再创新高",
                "现货卖墙重挂或合约卖墙被快速重建",
            ],
            risk_note="必须确认回落，否则取消",
        )
        cancel = list(_SHORT_CANCEL_TEMPLATE)
        cancel.insert(0, "阻力被扫后 15min 内未回落")

    if _stale_or_missing(dq):
        cancel.insert(0, "[已触发] 数据未就绪/部分源 stale，已降权")

    asym = _asymmetry_score(
        zone=zone, last_price=last_price, hard_stop=hard,
        targets=targets, data_confidence=zone.data_confidence,
        direction=side,
    )
    opp = _opportunity_score(
        asymmetry=asym,
        confirmation=0.30,
        key_level_quality=min(1.0, zone.support_trust if side == "long" else zone.resistance_trust),
        liquidity_path=1.0 - min(zone.break_through_risk * 0.5, 0.5),
        regime_fit=0.6 if not ctx else (0.85 if (ctx.regime or "").lower() in ("range", "consolidation") else 0.5),
        data_confidence=zone.data_confidence,
        execution_risk_penalty=0.05 if _stale_or_missing(dq) else 0.0,
    )

    setup_type = "fake_break_reclaim_long" if side == "long" else "fake_break_reclaim_short"
    return TradeSetupCandidate(
        setup_id=_make_setup_id(zone.coin, zone.zone_id, setup_type),
        coin=zone.coin,
        zone_id=zone.zone_id,
        setup_type=setup_type,  # type: ignore[arg-type]
        direction="neutral",
        entry_styles=[entry],
        risk_plan=SetupRiskPlan(
            soft_invalidation=soft,
            hard_stop=hard,
            structural_invalidation=(
                f"15m 收盘{'跌破' if side == 'long' else '突破'} {hard} 且未{'收回' if side == 'long' else '回落'}"
            ),
            stop_logic=[
                "等待扫破 + 收回，缺一不可",
                "扫破后 15min 未确认即取消观察",
                "Regime 反转直接取消",
            ],
        ),
        targets=targets,
        asymmetry_score=asym,
        opportunity_score=opp,
        data_confidence=zone.data_confidence,
        state=SetupState(
            name="forming",
            since_ts=int(time.time()),
            pending_reason=(
                "等待价格扫破支撑/阻力后快速收回（前置态=等待）"
            ),
        ),
        cancel_conditions=cancel,
        evidence=zone.evidence[:6],
        notes=[zone.dominant_label, "扫破收回观察区"],
    )


def build_opportunities(
    *,
    zones: list[BrainPriceZone],
    last_price: float,
    atr: float,
    ctx: Optional[BrainContextChips] = None,
    dq: Optional[BrainDataQuality] = None,
    max_opps: int = 8,
) -> list[TradeSetupCandidate]:
    """从 zones 派生候选；按 opportunity_score 排序，截 max_opps。"""
    if not zones or last_price <= 0:
        return []
    out: list[TradeSetupCandidate] = []
    seen: set[str] = set()
    for z in zones:
        for builder, kw in (
            (_build_support_limit_probe, {}),
            (_build_resistance_limit_probe, {}),
            (_build_fake_break_reclaim, {"side": "long"}),
            (_build_fake_break_reclaim, {"side": "short"}),
        ):
            try:
                cand = builder(  # type: ignore[arg-type]
                    zone=z, all_zones=zones,
                    last_price=last_price, atr=atr,
                    ctx=ctx, dq=dq, **kw,
                )
            except Exception:
                cand = None
            if cand is None:
                continue
            if cand.setup_id in seen:
                continue
            seen.add(cand.setup_id)
            out.append(cand)

    out.sort(key=lambda c: (-c.opportunity_score, abs(c.asymmetry_score - 1)))
    return out[:max_opps]
