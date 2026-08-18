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


def _linear_low(value: Optional[float], best: float, worst: float,
                neutral: float = 50.0) -> Optional[float]:
    if value is None:
        return None
    if best == worst:
        return neutral
    return _clamp((worst - value) / (worst - best) * 100.0)


def _linear_high(value: Optional[float], worst: float, best: float,
                 neutral: float = 50.0) -> Optional[float]:
    if value is None:
        return None
    if best == worst:
        return neutral
    return _clamp((value - worst) / (best - worst) * 100.0)


def _mean_present(values: list[Optional[float]]) -> float:
    present = [float(value) for value in values if value is not None]
    return sum(present) / len(present) if present else 0.0


def _metric_score(
    facts: SpotAccumulationFacts,
    name: str,
    value: Optional[float],
    scorer,
) -> Optional[float]:
    metric = facts.metric_facts.get(name)
    if metric is not None and not metric.included_in_score:
        metric.score = None
        return None
    score = scorer(value)
    if metric is not None:
        metric.score = round(score, 2) if score is not None else None
    return score


def score_facts(facts: SpotAccumulationFacts) -> EvidenceScore:
    v = facts.valuation_inputs
    valuation = _mean_present([
        _metric_score(facts, "drawdown_pct", facts.drawdown_pct,
                      lambda value: _linear_high(value, 25.0, 75.0)),
        _metric_score(facts, "mvrv", v.get("mvrv"),
                      lambda value: _linear_low(value, 0.75, 3.0)),
        _metric_score(facts, "ahr999", v.get("ahr999"),
                      lambda value: _linear_low(value, 0.35, 1.5)),
        _metric_score(facts, "price_vs_200w", v.get("price_vs_200w"),
                      lambda value: _linear_low(value, 0.85, 2.5)),
        _metric_score(facts, "price_vs_sth", v.get("price_vs_sth"),
                      lambda value: _linear_low(value, 0.70, 1.30)),
        _metric_score(facts, "nupl", v.get("nupl"),
                      lambda value: _linear_low(value, -0.20, 0.65)),
        _metric_score(facts, "reserve_risk", v.get("reserve_risk"),
                      lambda value: _linear_low(value, 0.0005, 0.01)),
        _metric_score(facts, "puell", v.get("puell"),
                      lambda value: _linear_low(value, 0.45, 2.5)),
        _metric_score(facts, "sth_sopr", v.get("sth_sopr"),
                      lambda value: _linear_low(value, 0.94, 1.08)),
        _metric_score(facts, "sth_supply_change_30d_pct", v.get("sth_supply_change_30d_pct"),
                      lambda value: _linear_low(value, -10.0, 10.0)),
    ])

    m = facts.capital_inputs
    capital = _mean_present([
        _metric_score(facts, "etf_flow_5d_usd", m.get("etf_flow_5d_usd"),
                      lambda value: _linear_high(value, -1_500_000_000.0, 1_500_000_000.0)),
        _metric_score(facts, "exchange_balance_7d_pct", m.get("exchange_balance_7d_pct"),
                      lambda value: _linear_low(value, -5.0, 5.0)),
        _metric_score(facts, "spot_netflow_24h_usd", m.get("spot_netflow_24h_usd"),
                      lambda value: _linear_high(value, -1_000_000_000.0, 1_000_000_000.0)),
        _metric_score(facts, "stablecoin_change_7d_pct", m.get("stablecoin_change_7d_pct"),
                      lambda value: _linear_high(value, -2.0, 2.0)),
        _metric_score(facts, "coinbase_premium", m.get("coinbase_premium"),
                      lambda value: _linear_high(value, -0.002, 0.002)),
    ])

    a = facts.acceptance_inputs
    binary = lambda name: 100.0 if a.get(name) is True else 0.0 if a.get(name) is False else None
    acceptance = _mean_present([
        _metric_score(facts, "spot_cvd_delta_1h", _as_float(a.get("spot_cvd_delta_1h")),
                      lambda value: _linear_high(value, -100_000_000.0, 100_000_000.0)),
        _metric_score(facts, "spot_taker_delta_1h", _as_float(a.get("spot_taker_delta_1h")),
                      lambda value: _linear_high(value, -100_000_000.0, 100_000_000.0)),
        _metric_score(facts, "footprint_absorption", _as_float(binary("footprint_absorption")),
                      lambda value: value),
        _metric_score(facts, "persistent_spot_wall", _as_float(binary("persistent_spot_wall")),
                      lambda value: value),
        _metric_score(facts, "coinbase_confluence", _as_float(binary("coinbase_confluence")),
                      lambda value: value),
        _metric_score(facts, "key_level_reclaimed", _as_float(binary("key_level_reclaimed")),
                      lambda value: value),
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
    """核心档位定义。V/M/A 门槛统一由 SpotAccumulationConfig.core_thresholds
    提供，此处不再内嵌副本（历史上曾内嵌导致与 config 漂移误导排查）。"""
    stage: str
    bucket: str


CORE_RULES = (
    _StageRule("insurance", "core"),
    _StageRule("value_1", "core"),
    _StageRule("deep_value", "core"),
    _StageRule("capitulation", "core"),
    _StageRule("bottom_confirmed", "core"),
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
    config: Optional[SpotAccumulationConfig] = None,
) -> list[SpotOpportunity]:
    """按最深满足档位创建累计批次；批次内仅开放第一个未完成子档。"""
    now = int(time.time())
    scores = facts.scores
    config = config or SpotAccumulationConfig()
    allocations = stage_allocations or config.core_stage_allocations()
    core_stages = [rule.stage for rule in CORE_RULES]
    unresolved_batch = any(
        item.stage in core_stages and item.batch_id
        and item.policy_version == config.policy_version
        and item.status in {"observing", "eligible", "accepted"}
        for item in runtime.opportunities.values()
    )
    if unresolved_batch:
        return []

    filled_by_stage = {
        stage: sum(
            item.filled_usdt for item in runtime.opportunities.values()
            if item.stage == stage
        )
        for stage in core_stages
    }
    skipped = {
        item.stage for item in runtime.opportunities.values()
        if item.policy_version == config.policy_version and item.status == "skipped"
    }
    blockers_by_stage: dict[str, list[str]] = {}
    deepest_index: Optional[int] = None
    for rule in CORE_RULES:
        thresholds = config.core_thresholds[rule.stage]
        blocked: list[str] = list(facts.hard_vetoes)
        if not facts.data_quality.can_open_new_opportunity:
            blocked.append("数据完整度或新鲜度不足")
        if scores.valuation < thresholds["v"]:
            blocked.append(f"V<{thresholds['v']:.0f}")
        if scores.capital_flow < thresholds["m"]:
            blocked.append(f"M<{thresholds['m']:.0f}")
        if scores.acceptance < thresholds["a"]:
            blocked.append(f"A<{thresholds['a']:.0f}")
        if rule.stage == "capitulation" and not capitulation_confirmed:
            blocked.append("尚未完成出清后承接确认")
        if rule.stage == "bottom_confirmed" and not weekly_reclaim_confirmed:
            blocked.append("周线结构尚未确认收回")
        if rule.stage in {"value_1", "deep_value", "capitulation"} and runtime.last_filled_price:
            gap = abs(facts.price - runtime.last_filled_price) / runtime.last_filled_price * 100
            required = max(config.min_price_gap_ratio * 100, config.atr_gap_multiplier * daily_atr_pct)
            if facts.price >= runtime.last_filled_price or gap < required:
                blocked.append("与上一笔成交价差不足")
        blockers_by_stage[rule.stage] = blocked
        if not blocked:
            deepest_index = core_stages.index(rule.stage)

    if deepest_index is None:
        output: list[SpotOpportunity] = []
        for rule in CORE_RULES:
            remaining = max(0.0, float(allocations[rule.stage]) - filled_by_stage[rule.stage])
            if remaining <= 0.01 or rule.stage in skipped:
                continue
            tolerance = max(0.01, daily_atr_pct / 100.0 * 0.35)
            oid = hashlib.sha1(
                f"BTC|observe|{config.policy_version}|{rule.stage}".encode()
            ).hexdigest()[:16]
            output.append(SpotOpportunity(
                opportunity_id=oid,
                stage=rule.stage,  # type: ignore[arg-type]
                bucket="core",
                allocation_usdt=remaining,
                status="observing",
                price_zone_low=round(facts.price * (1 - tolerance), 2),
                price_zone_high=round(facts.price * (1 + tolerance), 2),
                trigger_price=facts.price,
                scores=scores,
                reasons=list(facts.evidence),
                blocked_by=blockers_by_stage[rule.stage],
                created_at=now,
                updated_at=now,
                expires_at=now + 7 * 86400,
                policy_version=config.policy_version,
            ))
        return output

    pending = [
        stage for stage in core_stages[:deepest_index + 1]
        if stage not in skipped and allocations[stage] - filled_by_stage[stage] > 0.01
    ]
    if not pending:
        return []
    runtime.creation_sequence += 1
    creation = runtime.creation_sequence
    deepest = core_stages[deepest_index]
    batch_id = hashlib.sha1(
        f"BTC|core|p{config.policy_version}|b{creation}|{deepest}".encode()
    ).hexdigest()[:16]
    output = []
    for index, stage in enumerate(pending, 1):
        amount = max(0.0, float(allocations[stage]) - filled_by_stage[stage])
        spendable = max(0.0, available_cash.get("core", 0.0) - reserved.get("core", 0.0))
        blocked = [] if index == 1 else ["等待同批次前序子档完成或跳过"]
        if index == 1 and spendable + 0.01 < amount:
            blocked.append("对应预算不足")
        # capitulation 的"出清后承接确认"是该档的语义前提，即使作为更深档
        # 触发批次中的前序补齐档，也不得绕过；未确认时保持 observing，
        # 用户可通过"跳过"放行后续档位。
        if index == 1 and stage == "capitulation" and not capitulation_confirmed:
            blocked.append("尚未完成出清后承接确认")
        status = "eligible" if not blocked else "observing"
        tolerance = max(0.01, daily_atr_pct / 100.0 * 0.35)
        low = facts.price * (1.0 - tolerance)
        high = facts.price * (1.0 + tolerance)
        oid_raw = f"BTC|p{config.policy_version}|{batch_id}|{stage}|{creation}"
        oid = hashlib.sha1(oid_raw.encode()).hexdigest()[:16]
        output.append(SpotOpportunity(
            opportunity_id=oid,
            stage=stage,  # type: ignore[arg-type]
            bucket="core",
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
            policy_version=config.policy_version,
            batch_id=batch_id,
            batch_sequence=index,
            creation_sequence=creation,
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
    config: Optional[SpotAccumulationConfig] = None,
) -> list[SpotOpportunity]:
    """尾部极端和右侧纠错共用同一预算，严格三档串行且模式互斥。"""
    config = config or SpotAccumulationConfig()
    tranche_usdt = tranche_usdt or config.tail_tranche_usdt
    extreme_ok = (
        facts.scores.valuation >= config.tail_extreme_v
        and facts.scores.acceptance >= config.tail_extreme_a
        and capitulation_confirmed
    )
    catch_up_ok = (
        facts.scores.valuation >= config.tail_catch_up_v
        and facts.scores.capital_flow >= config.tail_catch_up_m
        and facts.scores.acceptance >= config.tail_catch_up_a
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
            gap_required = max(
                config.min_price_gap_ratio * 100,
                config.atr_gap_multiplier * daily_atr_pct,
            )
            gap = (previous.trigger_price - facts.price) / previous.trigger_price * 100
            if facts.price >= previous.trigger_price or gap < gap_required:
                blocked.append("极端尾部下一档价格间距不足")
    if available_cash - reserved_cash < tranche_usdt - 0.01:
        blocked.append("尾部预算不足")
    status = "eligible" if not blocked else "observing"
    tolerance = max(0.01, daily_atr_pct / 100.0 * 0.35)
    now = int(time.time())
    if status == "eligible":
        runtime.creation_sequence += 1
        creation = runtime.creation_sequence
    else:
        creation = 0
    oid = hashlib.sha1(
        f"BTC|p{config.policy_version}|{stage}|{tranche}|{creation}".encode()
    ).hexdigest()[:16]
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
        policy_version=config.policy_version,
        batch_id=f"tail-{config.policy_version}-{stage}",
        batch_sequence=tranche,
        creation_sequence=creation,
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
    config: Optional[SpotAccumulationConfig] = None,
) -> list[SpotOpportunity]:
    config = config or SpotAccumulationConfig()
    max_loss_usdt = max_loss_usdt or SpotAccumulationConfig().max_swing_loss_usdt
    if has_open_position:
        return []
    if any(item.stage == "swing" and item.status in {"eligible", "accepted"}
           for item in runtime.opportunities.values()):
        return []
    blocked = list(facts.hard_vetoes)
    if not facts.data_quality.can_open_new_opportunity:
        blocked.append("数据完整度或新鲜度不足")
    if facts.scores.acceptance < config.min_swing_acceptance:
        blocked.append(f"A<{config.min_swing_acceptance:.0f}")
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
    intended = max_loss_usdt / stop_pct if stop_pct > 0 else 0.0
    spendable = max(0.0, available_cash - reserved_cash)
    allocation = min(spendable, intended)
    if allocation <= 0:
        if intended > 0:
            # 展示目标额度与真实缺口，而非 1U 伪额度误导用户
            blocked.append(f"波段预算不足（需 {intended:.0f} U，缺口 {intended - spendable:.0f} U）")
            allocation = intended
        else:
            blocked.append("波段预算不足")
            allocation = 1.0  # 无法推算目标额度（缺结构位）时的占位下限
    status = "eligible" if not blocked else "observing"
    seq = sum(item.stage == "swing" and item.status == "filled"
              for item in runtime.opportunities.values()) + 1
    if status == "eligible":
        runtime.creation_sequence += 1
        creation = runtime.creation_sequence
    else:
        creation = 0
    oid_source = (
        f"BTC|p{config.policy_version}|swing|{seq}|{creation}"
        if status == "eligible"
        else f"BTC|p{config.policy_version}|swing|observing"
    )
    oid = hashlib.sha1(oid_source.encode()).hexdigest()[:16]
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
        policy_version=config.policy_version,
        creation_sequence=creation,
    )]
