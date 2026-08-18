"""现货抄底的小白视图：只读规划，不改变机会、预留或账本。"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Optional

from models.spot_accumulation import (
    SpotAccumulationConfig,
    SpotAccumulationFacts,
    SpotConditionalLadderItem,
    SpotDecisionSummary,
    SpotLadderProjection,
    SpotOpportunity,
    SpotPortfolio,
    SpotSupportMapItem,
)
from processors.trading_brain_builder import (
    build_price_zones,
    build_spot_book,
    merge_tolerance,
)
from storage.spot_accumulation_store import SpotLedgerExecutionSummary


CORE_STAGES = ("insurance", "value_1", "deep_value", "capitulation", "bottom_confirmed")
STAGE_LABELS = {
    "insurance": "踏空保险",
    "value_1": "价值一档",
    "deep_value": "深度价值",
    "capitulation": "恐慌出清",
    "bottom_confirmed": "底部确认",
}


@dataclass
class _Anchor:
    price_low: float
    price_high: float
    source: str
    label: str
    trust: Optional[float] = None
    evidence: list[str] = field(default_factory=list)

    @property
    def mid(self) -> float:
        return (self.price_low + self.price_high) / 2.0


def _fresh(facts: SpotAccumulationFacts, metric: str) -> bool:
    fact = facts.metric_facts.get(metric)
    return bool(fact and fact.freshness == "fresh" and fact.parse_status == "ok")


def _market_inputs(state: Any) -> tuple[Any, Any, Any, float]:
    kl = getattr(state, "key_level_snapshot_v2", None)
    op = getattr(state, "orderbook_pressure_snapshot", None)
    maps = getattr(state, "liq_maps", None) or {}
    liq = maps.get("1d") or maps.get("7d") or maps.get("30d")
    atr = float(getattr(state, "atr", 0.0) or 0.0)
    return kl, op, liq, atr


def _matching_zone(zones: list[Any], price: float, tolerance: float) -> Optional[Any]:
    matches = [
        zone for zone in zones
        if zone.price_low - tolerance <= price <= zone.price_high + tolerance
    ]
    return min(matches, key=lambda zone: abs(zone.price_mid - price)) if matches else None


def build_support_map(
    state: Any,
    facts: SpotAccumulationFacts,
    *,
    now: int,
    absorption: Any = None,
) -> tuple[list[SpotSupportMapItem], list[Any]]:
    """返回近场承接地图和未截断 PriceZone；不重新计算任何旧评分。"""
    kl, op, liq, atr = _market_inputs(state)
    zones = build_price_zones(
        coin="BTC", last_price=facts.price, atr=atr, kl=kl, op=op, liq=liq, max_zones=0,
    )
    book = build_spot_book(op)
    absorption_support = [
        zone for zone in (getattr(absorption, "zones_support", None) or [])
        if zone.source == "spot"
    ]
    tolerance = merge_tolerance(facts.price, atr)
    orderbook_fresh = _fresh(facts, "persistent_spot_wall")
    footprint_fresh = _fresh(facts, "footprint_absorption")
    orderbook_ts = int(facts.source_timestamps.get("orderbook_pressure", 0) or 0)
    footprint_ts = int(facts.source_timestamps.get("footprint_spot", 0) or 0)
    rows: list[SpotSupportMapItem] = []

    for item in list(book.bids if book else []):
        if (
            item.side != "bid"
            or item.spot_usd <= 0
            or item.price >= facts.price
            or not -5 <= item.distance_pct < 0
        ):
            continue
        zone = _matching_zone(zones, item.price, tolerance)
        role = str(getattr(zone, "dominant_role", item.dominant_role) or "other")
        fragility = float(getattr(zone, "support_fragility", 0.0) or 0.0)
        trust = float(getattr(zone, "support_trust", item.trust_score) or 0.0)
        if role == "contested" or fragility >= 0.6:
            label = "争夺区·可能被扫"
        elif role == "spot_defense":
            label = "现货防守区"
        else:
            label = "现货挂单观察区"
        evidence = list(getattr(zone, "evidence", None) or [])[:4]
        rows.append(SpotSupportMapItem(
            support_id=item.wall_zone_id or hashlib.sha1(f"wall|{item.price}".encode()).hexdigest()[:16],
            price_low=float(getattr(zone, "price_low", item.price) or item.price),
            price_high=float(getattr(zone, "price_high", item.price) or item.price),
            price_mid=float(getattr(zone, "price_mid", item.price) or item.price),
            distance_pct=float(item.distance_pct),
            binance_spot_usd=float(item.binance_spot_usd),
            coinbase_spot_usd=float(item.coinbase_spot_usd),
            spot_wall_usd=float(item.spot_usd),
            persistence_1h=float(item.persistence_score),
            persistence_8h=float(item.persistence_score_8h),
            max_usd_1h=float(item.max_usd_1h),
            max_usd_8h=float(item.max_usd_8h),
            support_trust=trust,
            support_strength=float(getattr(zone, "support_strength", trust) or 0.0),
            support_fragility=fragility,
            dominant_role=role,
            label=label,
            wall_source_timestamp=orderbook_ts,
            wall_fresh=orderbook_fresh,
            source_timestamp=orderbook_ts,
            is_fresh=orderbook_fresh,
            # contested 一并纳入锚区资格：行本身源自现货 bid 墙，contested
            # 只是同价位另有合约兴趣，对抄底承接判断仍有效（此前仅
            # spot_defense 导致 trust=1.0 的百万级现货买墙被整体排除）。
            anchor_eligible=bool(
                orderbook_fresh and trust >= 0.70
                and role in ("spot_defense", "contested")
            ),
            evidence=evidence,
        ))

    matched_absorption: set[int] = set()
    for index, candidate in enumerate(absorption_support):
        matches = [
            row for row in rows
            if abs(candidate.price - row.price_mid) <= tolerance
        ]
        if not matches:
            continue
        selected = min(
            matches,
            key=lambda row: (
                abs(candidate.price - row.price_mid),
                -row.support_trust,
                row.support_id,
            ),
        )
        matched_absorption.add(index)
        selected.absorption_usd = round(
            selected.absorption_usd + candidate.taker_volume_usd, 2,
        )
        selected.absorption_bar_count += candidate.bar_count
        selected.absorption_age_hours = (
            candidate.age_hours
            if selected.absorption_age_hours is None
            else min(selected.absorption_age_hours, candidate.age_hours)
        )
        selected.absorption_source_timestamp = footprint_ts
        selected.absorption_fresh = footprint_fresh
        selected.source_timestamp = max(selected.wall_source_timestamp, footprint_ts)
        selected.is_fresh = selected.wall_fresh and footprint_fresh

    for index, candidate in enumerate(absorption_support):
        if index in matched_absorption or not 0 <= (facts.price - candidate.price) / facts.price * 100 <= 5:
            continue
        rows.append(SpotSupportMapItem(
            support_id=hashlib.sha1(f"absorption|{candidate.price}".encode()).hexdigest()[:16],
            price_low=candidate.price,
            price_high=candidate.price,
            price_mid=candidate.price,
            distance_pct=(candidate.price - facts.price) / facts.price * 100,
            absorption_usd=candidate.taker_volume_usd,
            absorption_bar_count=candidate.bar_count,
            absorption_age_hours=candidate.age_hours,
            dominant_role="spot_absorption",
            label="已成交现货吸收",
            absorption_source_timestamp=footprint_ts,
            absorption_fresh=footprint_fresh,
            source_timestamp=footprint_ts,
            is_fresh=footprint_fresh,
            anchor_eligible=False,
            evidence=["Footprint真实成交吸收，不等同于可撤销挂单"],
        ))
    rows.sort(key=lambda row: (
        -row.support_trust,
        -(row.absorption_usd if row.absorption_fresh else 0.0),
        abs(row.distance_pct),
    ))
    return rows, zones


def _planning_anchors(state: Any, facts: SpotAccumulationFacts, zones: list[Any]) -> list[_Anchor]:
    anchors: list[_Anchor] = []
    key_fresh = _fresh(facts, "key_level_reclaimed")
    wall_fresh = _fresh(facts, "persistent_spot_wall")
    for zone in zones:
        if zone.price_mid >= facts.price or zone.support_trust < 0.70:
            continue
        if zone.dominant_role not in {"spot_defense", "key_level_only"}:
            continue
        source_fresh = bool(
            (getattr(zone.roles, "key_level", False) and key_fresh)
            or (getattr(zone.roles, "spot_supply_wall", False) and wall_fresh)
        )
        if not source_fresh:
            continue
        anchors.append(_Anchor(
            price_low=zone.price_low,
            price_high=zone.price_high,
            source="trading_brain_price_zone",
            label=zone.dominant_label or "高可信结构支撑",
            trust=zone.support_trust,
            evidence=list(zone.evidence)[:4],
        ))

    cycle = getattr(state, "cycle_position", None)
    for attr, metric, label in (
        ("sth_cost_1d", "price_vs_sth", "短期持有者成本"),
        ("sma_200w", "price_vs_200w", "200周均线"),
    ):
        raw = getattr(cycle, attr, None)
        try:
            price = float(raw)
        except (TypeError, ValueError):
            continue
        if price > 0 and price < facts.price and _fresh(facts, metric):
            anchors.append(_Anchor(
                price_low=price,
                price_high=price,
                source=f"cycle_position.{attr}",
                label=label,
                evidence=[f"新鲜的{label}估值锚"],
            ))

    tolerance = merge_tolerance(facts.price, float(getattr(state, "atr", 0.0) or 0.0))
    merged: list[_Anchor] = []
    for anchor in sorted(anchors, key=lambda item: item.mid, reverse=True):
        existing = next((item for item in merged if abs(item.mid - anchor.mid) <= tolerance), None)
        if existing is None:
            merged.append(anchor)
            continue
        existing.price_low = min(existing.price_low, anchor.price_low)
        existing.price_high = max(existing.price_high, anchor.price_high)
        existing.label = f"{existing.label} + {anchor.label}"
        existing.evidence = list(dict.fromkeys(existing.evidence + anchor.evidence))[:6]
        if anchor.trust is not None:
            existing.trust = max(existing.trust or 0.0, anchor.trust)
    return sorted(merged, key=lambda item: item.mid, reverse=True)


def _stage_blockers(
    stage: str,
    config: SpotAccumulationConfig,
    facts: SpotAccumulationFacts,
    opportunity: Optional[SpotOpportunity],
) -> list[str]:
    thresholds = config.core_thresholds[stage]
    gaps: list[str] = []
    for label, current, needed in (
        ("便宜程度V", facts.scores.valuation, thresholds["v"]),
        ("资金进场M", facts.scores.capital_flow, thresholds["m"]),
        ("现货承接A", facts.scores.acceptance, thresholds["a"]),
    ):
        if current + 1e-9 < needed:
            gaps.append(f"{label} 当前{current:.0f}，需要{needed:.0f}（差{needed-current:.0f}）")
    if opportunity:
        gaps.extend(opportunity.blocked_by)
    gaps.extend(facts.hard_vetoes)
    return list(dict.fromkeys(gaps))[:5]


def build_conditional_ladder(
    state: Any,
    config: SpotAccumulationConfig,
    facts: SpotAccumulationFacts,
    portfolio: SpotPortfolio,
    opportunities: list[SpotOpportunity],
    zones: list[Any],
    execution_summary: SpotLedgerExecutionSummary,
    *,
    capitulation_confirmed: bool = False,
    weekly_reclaim_confirmed: bool = False,
    valuation_bands: Optional[dict[str, Any]] = None,
) -> list[SpotConditionalLadderItem]:
    anchors = _planning_anchors(state, facts, zones)
    bands = valuation_bands or {}
    allocations = config.core_stage_allocations()
    projected_btc = portfolio.total_btc
    projected_cost = portfolio.total_cost_basis_usdt
    projected_core_cash = portfolio.buckets["core"].cash_usdt
    projected_total_cash = portfolio.total_cash_usdt
    rows: list[SpotConditionalLadderItem] = []

    for index, stage in enumerate(CORE_STAGES):
        stage_items = [item for item in opportunities if item.stage == stage]
        execution = execution_summary.stages.get(stage)
        filled = min(allocations[stage], execution.spent_usdt if execution else 0.0)
        remaining = max(0.0, allocations[stage] - filled)
        current = sorted(
            (item for item in stage_items if item.policy_version == config.policy_version),
            key=lambda item: (
                {"accepted": 0, "eligible": 1, "observing": 2}.get(item.status, 3),
                -item.updated_at,
            ),
        )
        opportunity = current[0] if current else None
        pricing_mode = "event_driven" if stage in {"capitulation", "bottom_confirmed"} else "price_ladder"
        anchor = anchors[index] if pricing_mode == "price_ladder" and index < len(anchors) else None
        event_ready = (
            capitulation_confirmed if stage == "capitulation"
            else weekly_reclaim_confirmed if stage == "bottom_confirmed"
            else False
        )
        actionable = bool(opportunity and opportunity.status in {"eligible", "accepted"})
        if remaining <= 0.01:
            status = "filled"
        elif opportunity and opportunity.status == "accepted":
            status = "accepted"
        elif opportunity and opportunity.status == "eligible":
            status = "eligible"
        elif filled > 0:
            status = "partial"
        elif pricing_mode == "event_driven" and not event_ready:
            status = "waiting_event"
        elif pricing_mode == "event_driven" and event_ready and opportunity:
            status = "conditional"
        elif pricing_mode == "event_driven":
            status = "waiting_event"
        elif anchor:
            status = "conditional"
        else:
            status = "waiting_anchor"

        if remaining <= 0.01:
            low = high = None
            source = "ledger"
            label = "历史成交已完成"
            trust = None
        elif actionable and opportunity:
            low, high = opportunity.price_zone_low, opportunity.price_zone_high
            source = "active_opportunity"
            label = "策略已触发价格区"
            trust = anchor.trust if anchor else None
        elif pricing_mode == "event_driven" and event_ready and opportunity:
            low, high = opportunity.price_zone_low, opportunity.price_zone_high
            source = "event_confirmed_opportunity"
            label = "事件已确认，等待批次前序完成"
            trust = None
        elif anchor:
            low, high = anchor.price_low, anchor.price_high
            source, label, trust = anchor.source, anchor.label, anchor.trust
        else:
            low = high = None
            source = label = ""
            trust = None
        band = bands.get(stage)
        band_price = getattr(band, "band_price", None) if band else None
        # 无结构锚 / 事件档无机会时，用估值带价兜底作参考价——
        # 让 deep_value/capitulation 也有可规划的价格（此前恒为空）。
        # 带价是进带上限：已在带内（带价高于现价）时按现价封顶，
        # 推演不得假设以高于现价的价格成交。
        if low is None and band_price:
            low = high = min(band_price, facts.price)
            source = "valuation_band"
            label = getattr(band, "note", "") or "估值带参考价"
        mid = (low + high) / 2 if low is not None and high is not None else None
        planned = min(remaining, max(0.0, projected_core_cash))
        shortfall = max(0.0, remaining - planned)
        estimated = planned / mid if mid and planned > 0 else None
        if estimated is not None and planned > 0:
            projected_btc += estimated
            projected_cost += planned
            projected_core_cash = max(0.0, projected_core_cash - planned)
            projected_total_cash = max(0.0, projected_total_cash - planned)
        average = projected_cost / projected_btc if projected_btc > 0 else None
        blockers = _stage_blockers(stage, config, facts, opportunity)
        if shortfall > 0.01:
            blockers.append(f"核心分桶资金缺口 {shortfall:.2f} U")
            if execution_summary.unassigned_core_buy_usdt > 0.01:
                blockers.append(
                    f"策略外核心买入已占用 {execution_summary.unassigned_core_buy_usdt:.2f} U"
                )
        invalidation = []
        if pricing_mode == "event_driven" and not event_ready:
            invalidation.append(
                "等待出清后现货承接确认" if stage == "capitulation"
                else "等待闭合周线结构收回"
            )
        elif anchor is None and not actionable and not (event_ready and opportunity):
            invalidation.append("暂无新鲜且可信的下方结构位")
        if not facts.data_quality.can_open_new_opportunity:
            invalidation.append("数据质量不足，条件价仅作观察")
        rows.append(SpotConditionalLadderItem(
            stage=stage,  # type: ignore[arg-type]
            target_usdt=round(allocations[stage], 2),
            filled_usdt=round(filled, 2),
            remaining_usdt=round(remaining, 2),
            planned_usdt=round(planned, 2),
            cash_shortfall_usdt=round(shortfall, 2),
            status=status,  # type: ignore[arg-type]
            pricing_mode=pricing_mode,  # type: ignore[arg-type]
            is_actionable=actionable,
            opportunity_id=opportunity.opportunity_id if actionable and opportunity else None,
            reference_price_low=round(low, 2) if low is not None else None,
            reference_price_high=round(high, 2) if high is not None else None,
            reference_price_mid=round(mid, 2) if mid is not None else None,
            anchor_source=source,
            anchor_label=label,
            support_trust=trust,
            blockers=blockers,
            invalidation_reasons=invalidation,
            historical_quantity_btc=execution.quantity_btc if execution else 0.0,
            historical_average_price=execution.average_price if execution and execution.quantity_btc else None,
            estimated_btc=estimated,
            projected_total_btc=projected_btc,
            projected_average_cost=average,
            projected_cash_remaining=round(projected_total_cash, 2),
            projected_core_cash_remaining=round(projected_core_cash, 2),
            projected_total_cash_remaining=round(projected_total_cash, 2),
            valuation_band_price=band_price,
            valuation_band_mode=getattr(band, "mode", "none") if band else "none",
            valuation_band_in=getattr(band, "in_band", None) if band else None,
            valuation_band_note=getattr(band, "note", "") if band else "",
        ))
    return rows


def build_ladder_projection(
    ladder: list[SpotConditionalLadderItem],
    portfolio: SpotPortfolio,
    price: float,
) -> Optional[SpotLadderProjection]:
    """阶梯推演汇总（纯展示层，复用阶梯逐行已算好的累计投影）。

    口径 = 已成交持仓 + 剩余阶梯按各档参考价全部成交；对照基线 =
    同额资金现在按现价一次性买入，量化价格纪律带来的 BTC 数量差。
    """
    if not ladder or price <= 0:
        return None
    planned_spend = sum(
        row.planned_usdt for row in ladder if row.estimated_btc
    )
    no_price = [
        row.stage for row in ladder
        if row.remaining_usdt > 0.01 and row.reference_price_mid is None
    ]
    last = ladder[-1]
    baseline_btc = portfolio.total_btc + planned_spend / price
    baseline_cost = portfolio.total_cost_basis_usdt + planned_spend
    baseline_avg = baseline_cost / baseline_btc if baseline_btc > 0 else None
    projected_btc = last.projected_total_btc
    advantage = (
        (projected_btc - baseline_btc) / baseline_btc * 100.0
        if projected_btc is not None and baseline_btc > 0 else None
    )
    notes = [
        "条件推演：各档需价格到达参考价才成交，不到带/锚的资金保留为现金",
    ]
    if no_price:
        notes.append(
            "以下档位参考价数据缺席，暂无法推演："
            + "、".join(STAGE_LABELS.get(s, s) for s in no_price)
        )
    return SpotLadderProjection(
        current_btc=portfolio.total_btc,
        current_average_cost=(
            portfolio.total_cost_basis_usdt / portfolio.total_btc
            if portfolio.total_btc > 0 else None
        ),
        planned_spend_usdt=round(planned_spend, 2),
        projected_total_btc=projected_btc,
        projected_average_cost=last.projected_average_cost,
        baseline_price=price,
        baseline_total_btc=baseline_btc if baseline_btc > 0 else None,
        baseline_average_cost=baseline_avg,
        btc_advantage_pct=round(advantage, 2) if advantage is not None else None,
        stages_without_price=no_price,
        notes=notes,
    )


def build_decision_summary(
    facts: SpotAccumulationFacts,
    ladder: list[SpotConditionalLadderItem],
    opportunities: list[SpotOpportunity],
    *,
    now: int,
) -> SpotDecisionSummary:
    accepted = next((item for item in opportunities if item.status == "accepted"), None)
    eligible = next((item for item in opportunities if item.status == "eligible"), None)
    active = accepted or eligible
    if active:
        remaining = max(0.0, active.allocation_usdt - active.filled_usdt)
        mid = (active.price_zone_low + active.price_zone_high) / 2
        return SpotDecisionSummary(
            state="accepted" if accepted else "eligible",
            headline="宽限期等待手工成交" if accepted else "现在可手工买入",
            detail=f"{STAGE_LABELS.get(active.stage, active.stage)}已通过规则，金额不得超过建议上限",
            opportunity_id=active.opportunity_id,
            stage=active.stage,
            bucket=active.bucket,
            amount_usdt=round(remaining, 2),
            price_low=active.price_zone_low,
            price_high=active.price_zone_high,
            estimated_btc=remaining / mid if mid > 0 else None,
            blockers=[],
            grace_expires_at=active.grace_expires_at,
            updated_at=now,
        )
    if not ladder:
        return SpotDecisionSummary(
            state="blocked",
            headline="当前不买",
            detail="条件阶梯暂时不可用，核心机会与账本仍正常",
            updated_at=now,
        )
    pending = next((row for row in ladder if row.remaining_usdt > 0.01), None)
    if pending is None:
        return SpotDecisionSummary(
            state="complete", headline="核心计划已完成", detail="五档核心目标金额均已记录成交", updated_at=now,
        )
    state = "conditional" if pending.reference_price_mid else "blocked"
    return SpotDecisionSummary(
        state=state,
        headline="当前不买",
        detail=(
            f"下一档为{STAGE_LABELS.get(pending.stage, pending.stage)}，计划 {pending.remaining_usdt:.0f} U；"
            + ("参考价仅是动态条件计划，尚未获得买入授权" if pending.reference_price_mid else "尚无可靠价格锚")
        ),
        stage=pending.stage,
        amount_usdt=pending.remaining_usdt,
        price_low=pending.reference_price_low,
        price_high=pending.reference_price_high,
        estimated_btc=pending.estimated_btc,
        blockers=pending.blockers[:3],
        updated_at=now,
    )
