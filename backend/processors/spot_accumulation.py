"""BTC 现货动态抄底确定性评分与机会生成。"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from typing import Optional

from models.spot_accumulation import (
    EvidenceScore,
    SpotAccumulationConfig,
    SpotAccumulationFacts,
    SpotAccumulationRuntimeState,
    SpotOpportunity,
)


def _clamp(value: float) -> float:
    return max(0.0, min(100.0, value))


def _linear_low(value: Optional[float], best: float, worst: float, neutral: float = 50.0) -> float:
    if value is None:
        return neutral
    if best == worst:
        return neutral
    return _clamp((worst - value) / (worst - best) * 100.0)


def _linear_high(value: Optional[float], worst: float, best: float, neutral: float = 50.0) -> float:
    if value is None:
        return neutral
    if best == worst:
        return neutral
    return _clamp((value - worst) / (best - worst) * 100.0)


def _mean_present(values: list[Optional[float]]) -> float:
    present = [float(value) for value in values if value is not None]
    return sum(present) / len(present) if present else 0.0


def score_facts(facts: SpotAccumulationFacts) -> EvidenceScore:
    v = facts.valuation_inputs
    valuation = _mean_present([
        _linear_high(facts.drawdown_pct, 25.0, 75.0),
        _linear_low(v.get("mvrv"), 0.75, 3.0),
        _linear_low(v.get("ahr999"), 0.35, 1.5),
        _linear_low(v.get("price_vs_200w"), 0.85, 2.5),
        _linear_low(v.get("price_vs_sth"), 0.70, 1.30),
        _linear_low(v.get("nupl"), -0.20, 0.65),
        _linear_low(v.get("reserve_risk"), 0.0005, 0.01),
        _linear_low(v.get("puell"), 0.45, 2.5),
        _linear_low(v.get("sth_sopr"), 0.94, 1.08),
        _linear_low(v.get("sth_supply_change_30d_pct"), -10.0, 10.0),
    ])

    m = facts.capital_inputs
    capital = _mean_present([
        _linear_high(m.get("etf_flow_5d_usd"), -1_500_000_000.0, 1_500_000_000.0),
        _linear_low(m.get("exchange_balance_7d_pct"), -5.0, 5.0),
        _linear_high(m.get("spot_netflow_24h_usd"), -1_000_000_000.0, 1_000_000_000.0),
        _linear_high(m.get("stablecoin_change_7d_pct"), -2.0, 2.0),
        _linear_high(m.get("coinbase_premium"), -0.002, 0.002),
    ])

    a = facts.acceptance_inputs
    binary = lambda name: 100.0 if a.get(name) is True else 0.0 if a.get(name) is False else None
    acceptance = _mean_present([
        _linear_high(_as_float(a.get("spot_cvd_delta_1h")), -100_000_000.0, 100_000_000.0),
        _linear_high(_as_float(a.get("spot_taker_delta_1h")), -100_000_000.0, 100_000_000.0),
        binary("footprint_absorption"),
        binary("persistent_spot_wall"),
        binary("coinbase_confluence"),
        binary("key_level_reclaimed"),
    ])
    return EvidenceScore(
        valuation=round(valuation, 2),
        capital_flow=round(capital, 2),
        acceptance=round(acceptance, 2),
    )


def _as_float(value: object) -> Optional[float]:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class _StageRule:
    stage: str
    bucket: str
    min_v: float
    min_m: float
    min_a: float


CORE_RULES = (
    _StageRule("insurance", "core", 55.0, 40.0, 65.0),
    _StageRule("value_1", "core", 65.0, 45.0, 60.0),
    _StageRule("deep_value", "core", 75.0, 45.0, 60.0),
    _StageRule("capitulation", "core", 80.0, 0.0, 65.0),
    _StageRule("bottom_confirmed", "core", 60.0, 65.0, 75.0),
)


def build_opportunities(
    facts: SpotAccumulationFacts,
    runtime: SpotAccumulationRuntimeState,
    available_cash: dict[str, float],
    reserved: dict[str, float],
    *,
    daily_atr_pct: float = 5.0,
    capitulation_confirmed: bool = False,
    weekly_reclaim_confirmed: bool = False,
    stage_allocations: Optional[dict[str, float]] = None,
) -> list[SpotOpportunity]:
    now = int(time.time())
    scores = facts.scores
    output: list[SpotOpportunity] = []
    allocations = stage_allocations or SpotAccumulationConfig().core_stage_allocations()
    existing_by_stage = {
        item.stage: item for item in runtime.opportunities.values()
        if item.stage in {rule.stage for rule in CORE_RULES}
        and item.status in ("eligible", "accepted", "filled", "skipped")
    }
    prior_complete = True
    for rule in CORE_RULES:
        amount = float(allocations[rule.stage])
        existing = existing_by_stage.get(rule.stage)
        if existing and existing.status in {"filled", "skipped"}:
            continue
        if existing and existing.status in {"eligible", "accepted"}:
            prior_complete = False
            continue
        blocked: list[str] = list(facts.hard_vetoes)
        if not prior_complete:
            blocked.append("需先完成或跳过前一核心档位")
        if not facts.data_quality.can_open_new_opportunity:
            blocked.append("数据完整度或新鲜度不足")
        if scores.valuation < rule.min_v:
            blocked.append(f"V<{rule.min_v:.0f}")
        if scores.capital_flow < rule.min_m:
            blocked.append(f"M<{rule.min_m:.0f}")
        if scores.acceptance < rule.min_a:
            blocked.append(f"A<{rule.min_a:.0f}")
        if rule.stage == "capitulation" and not capitulation_confirmed:
            blocked.append("尚未完成出清后承接确认")
        if rule.stage == "bottom_confirmed" and not weekly_reclaim_confirmed:
            blocked.append("周线结构尚未确认收回")
        if rule.stage in {"value_1", "deep_value", "capitulation"} and runtime.last_filled_price:
            gap = abs(facts.price - runtime.last_filled_price) / runtime.last_filled_price * 100
            if facts.price >= runtime.last_filled_price or gap < max(5.0, 1.5 * daily_atr_pct):
                blocked.append("与上一笔成交价差不足")
        spendable = max(0.0, available_cash.get(rule.bucket, 0.0) - reserved.get(rule.bucket, 0.0))
        if spendable + 0.01 < amount:
            blocked.append("对应预算不足")

        status = "eligible" if not blocked else "observing"
        if status != "eligible":
            prior_complete = False
        else:
            # 同一时刻只允许一个核心档位释放；后续档位保持观察。
            prior_complete = False
        tolerance = max(0.01, daily_atr_pct / 100.0 * 0.35)
        low = facts.price * (1.0 - tolerance)
        high = facts.price * (1.0 + tolerance)
        oid_raw = f"BTC|{rule.stage}|{round(facts.price / max(1.0, facts.price * 0.02))}"
        oid = hashlib.sha1(oid_raw.encode()).hexdigest()[:16]
        output.append(SpotOpportunity(
            opportunity_id=oid,
            stage=rule.stage,  # type: ignore[arg-type]
            bucket=rule.bucket,  # type: ignore[arg-type]
            allocation_usdt=amount,
            reserved_usdt=amount if status == "eligible" else 0.0,
            status=status,
            price_zone_low=round(low, 2),
            price_zone_high=round(high, 2),
            trigger_price=facts.price,
            scores=scores,
            reasons=list(facts.evidence),
            blocked_by=blocked,
            created_at=now,
            updated_at=now,
            expires_at=now + 7 * 86400,
        ))
    return output


def build_tail_opportunities(
    facts: SpotAccumulationFacts,
    runtime: SpotAccumulationRuntimeState,
    available_cash: float,
    reserved_cash: float,
    *,
    capitulation_confirmed: bool,
    weekly_reclaim_confirmed: bool,
    daily_atr_pct: float = 5.0,
    tranche_usdt: Optional[float] = None,
) -> list[SpotOpportunity]:
    """尾部极端和右侧纠错共用同一预算，严格三档串行且模式互斥。"""
    tranche_usdt = tranche_usdt or SpotAccumulationConfig().tail_tranche_usdt
    extreme_ok = facts.scores.valuation >= 90 and facts.scores.acceptance >= 65 and capitulation_confirmed
    catch_up_ok = (
        facts.scores.valuation >= 60
        and facts.scores.capital_flow >= 65
        and facts.scores.acceptance >= 75
        and weekly_reclaim_confirmed
    )
    desired_mode = runtime.tail_mode
    if desired_mode is None:
        desired_mode = "extreme" if extreme_ok else "catch_up" if catch_up_ok else None
    if desired_mode is None:
        return []
    stage = "tail_extreme" if desired_mode == "extreme" else "tail_catch_up"
    condition_ok = extreme_ok if desired_mode == "extreme" else catch_up_ok
    historical = sorted(
        [item for item in runtime.opportunities.values() if item.stage == stage],
        key=lambda item: item.created_at,
    )
    complete_count = sum(item.status in {"filled", "skipped"} for item in historical)
    active = next((item for item in historical if item.status in {"eligible", "accepted"}), None)
    if active is not None or complete_count >= 3:
        return []
    tranche = complete_count + 1
    blocked = list(facts.hard_vetoes)
    if not facts.data_quality.can_open_new_opportunity:
        blocked.append("数据完整度或新鲜度不足")
    if not condition_ok:
        blocked.append("尾部模式确认条件未满足")
    completed = [item for item in historical if item.status in {"filled", "skipped"}]
    if completed:
        previous = completed[-1]
        if desired_mode == "catch_up" and int(time.time()) - previous.updated_at < 7 * 86400:
            blocked.append("右侧纠错档位需至少间隔7天")
        if desired_mode == "extreme":
            gap_required = max(5.0, 1.5 * daily_atr_pct)
            gap = (previous.trigger_price - facts.price) / previous.trigger_price * 100
            if facts.price >= previous.trigger_price or gap < gap_required:
                blocked.append("极端尾部下一档价格间距不足")
    if available_cash - reserved_cash < tranche_usdt - 0.01:
        blocked.append("尾部预算不足")
    status = "eligible" if not blocked else "observing"
    tolerance = max(0.01, daily_atr_pct / 100.0 * 0.35)
    now = int(time.time())
    oid = hashlib.sha1(f"BTC|{stage}|{tranche}".encode()).hexdigest()[:16]
    return [SpotOpportunity(
        opportunity_id=oid,
        stage=stage,  # type: ignore[arg-type]
        bucket="tail",
        allocation_usdt=tranche_usdt,
        reserved_usdt=tranche_usdt if status == "eligible" else 0,
        status=status,
        price_zone_low=round(facts.price * (1 - tolerance), 2),
        price_zone_high=round(facts.price * (1 + tolerance), 2),
        trigger_price=facts.price,
        scores=facts.scores,
        reasons=[f"{desired_mode}模式第{tranche}/3档"] + list(facts.evidence),
        blocked_by=blocked,
        created_at=now,
        updated_at=now,
        expires_at=now + 7 * 86400,
    )]


def build_swing_opportunity(
    facts: SpotAccumulationFacts,
    runtime: SpotAccumulationRuntimeState,
    available_cash: float,
    reserved_cash: float,
    *,
    support_price: Optional[float],
    stop_price: Optional[float],
    target_price: Optional[float],
    has_open_position: bool,
    max_loss_usdt: Optional[float] = None,
    min_rr: float = 2.0,
) -> list[SpotOpportunity]:
    max_loss_usdt = max_loss_usdt or SpotAccumulationConfig().max_swing_loss_usdt
    if has_open_position:
        return []
    if any(item.stage == "swing" and item.status in {"eligible", "accepted"}
           for item in runtime.opportunities.values()):
        return []
    blocked = list(facts.hard_vetoes)
    if not facts.data_quality.can_open_new_opportunity:
        blocked.append("数据完整度或新鲜度不足")
    if facts.scores.acceptance < 70:
        blocked.append("A<70")
    if not support_price or not stop_price or not target_price:
        blocked.append("缺少结构支撑、止损或目标")
        stop_pct = rr = 0.0
    else:
        stop_pct = (facts.price - stop_price) / facts.price
        reward_pct = (target_price - facts.price) / facts.price
        rr = reward_pct / stop_pct if stop_pct > 0 else 0.0
        if stop_price >= facts.price or support_price > facts.price:
            blocked.append("当前价格未处于支撑观察区")
        elif (facts.price - support_price) / facts.price * 100 > 3:
            blocked.append("距离支撑超过3%")
        if rr < min_rr:
            blocked.append(f"预期盈亏比{rr:.2f}<{min_rr:.2f}")
    allocation = min(max(0.0, available_cash - reserved_cash),
                     max_loss_usdt / stop_pct if stop_pct > 0 else 0.0)
    if allocation <= 0:
        blocked.append("波段预算不足")
        allocation = 1.0
    status = "eligible" if not blocked else "observing"
    seq = sum(item.stage == "swing" and item.status == "filled"
              for item in runtime.opportunities.values()) + 1
    oid = hashlib.sha1(f"BTC|swing|{seq}|{round(support_price or facts.price)}".encode()).hexdigest()[:16]
    now = int(time.time())
    return [SpotOpportunity(
        opportunity_id=oid,
        stage="swing",
        bucket="swing",
        allocation_usdt=round(allocation, 2),
        reserved_usdt=round(allocation, 2) if status == "eligible" else 0,
        status=status,
        price_zone_low=max(1.0, round((support_price or facts.price) * 0.99, 2)),
        price_zone_high=round((support_price or facts.price) * 1.01, 2),
        trigger_price=facts.price,
        scores=facts.scores,
        reasons=list(facts.evidence),
        blocked_by=blocked,
        created_at=now,
        updated_at=now,
        expires_at=now + 3 * 86400,
        structural_stop=stop_price,
        target_price=target_price,
        expected_rr=round(rr, 2) if rr else None,
    )]
