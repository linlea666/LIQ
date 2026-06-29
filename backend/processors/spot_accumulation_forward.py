"""基于线上完整事实快照的M/A前向影子验证。

本模块不回填历史订单簿或Footprint，不执行交易，也不输出伪造的全栈回测结论。
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Iterable, Optional


DAY_SECONDS = 86_400
FORWARD_DAYS = (7, 30, 90)
TERMINAL_STATUSES = {"invalidated", "expired", "skipped", "filled"}
M_METRICS = {
    "etf_flow_5d_usd", "exchange_balance_7d_pct", "spot_netflow_24h_usd",
    "stablecoin_change_7d_pct", "coinbase_premium",
}
A_METRICS = {
    "spot_cvd_delta_1h", "spot_taker_delta_1h", "footprint_absorption",
    "persistent_spot_wall", "coinbase_confluence", "key_level_reclaimed",
}


def _timestamp(record: dict[str, Any]) -> int:
    try:
        return int(record.get("timestamp") or 0)
    except (TypeError, ValueError, OverflowError):
        return 0


def _price(record: dict[str, Any]) -> Optional[float]:
    facts = record.get("facts")
    facts = facts if isinstance(facts, dict) else {}
    value = facts.get("price", record.get("price"))
    try:
        price = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return price if price > 0 else None


def _future_price(
    records: list[dict[str, Any]],
    target_ts: int,
    max_gap_seconds: int,
) -> Optional[float]:
    for record in records:
        ts = _timestamp(record)
        if ts < target_ts:
            continue
        if ts - target_ts > max_gap_seconds:
            return None
        return _price(record)
    return None


def _ma_coverage(facts: dict[str, Any]) -> tuple[float, bool]:
    metric_facts = facts.get("metric_facts") or {}
    metric_facts = metric_facts if isinstance(metric_facts, dict) else {}
    included = sum(
        bool((metric_facts.get(name) or {}).get("included_in_score"))
        for name in M_METRICS | A_METRICS
    )
    data_quality = facts.get("data_quality") or {}
    data_quality = data_quality if isinstance(data_quality, dict) else {}
    layer_quality = data_quality.get("layer_quality") or {}
    layer_quality = layer_quality if isinstance(layer_quality, dict) else {}
    ready = bool(
        (layer_quality.get("capital_flow") or {}).get("passed")
        and (layer_quality.get("acceptance") or {}).get("passed")
    )
    return included / len(M_METRICS | A_METRICS), ready


def build_forward_report(
    raw_records: Iterable[dict[str, Any]],
    *,
    max_target_gap_seconds: int = 2 * DAY_SECONDS,
) -> dict[str, Any]:
    """统计机会后7/30/90日收益、后续回撤、失效原因和M/A覆盖率。"""
    records = sorted(
        (
            record for record in raw_records
            if isinstance(record, dict) and _timestamp(record) > 0 and _price(record)
        ),
        key=_timestamp,
    )
    full_records = [
        record for record in records
        if record.get("archive_schema_version") == 2
        and record.get("record_type") == "spot_accumulation_full_fact_snapshot"
    ]
    legacy_count = len(records) - len(full_records)
    first_entries: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    for record in full_records:
        opportunities = record.get("opportunities") or []
        opportunities = opportunities if isinstance(opportunities, list) else []
        for opportunity in opportunities:
            if not isinstance(opportunity, dict):
                continue
            opportunity_id = str(opportunity.get("opportunity_id") or "")
            if (
                opportunity_id
                and opportunity.get("status") in {"eligible", "accepted"}
                and opportunity_id not in first_entries
            ):
                first_entries[opportunity_id] = (record, opportunity)

    outcomes: list[dict[str, Any]] = []
    invalidation_counts: Counter[str] = Counter()
    for opportunity_id, (entry_record, opportunity) in first_entries.items():
        entry_ts = _timestamp(entry_record)
        entry_price = _price(entry_record)
        if entry_price is None:
            continue
        after = [record for record in full_records if _timestamp(record) >= entry_ts]
        forward_returns: dict[str, Optional[float]] = {}
        for days in FORWARD_DAYS:
            price = _future_price(
                after, entry_ts + days * DAY_SECONDS, max_target_gap_seconds,
            )
            forward_returns[f"return_{days}d_pct"] = (
                round((price - entry_price) / entry_price * 100, 4)
                if price is not None else None
            )
        horizon = entry_ts + 90 * DAY_SECONDS
        observed_prices = [
            price for record in after
            if _timestamp(record) <= horizon and (price := _price(record)) is not None
        ]
        max_drawdown = (
            round((min(observed_prices) - entry_price) / entry_price * 100, 4)
            if observed_prices else None
        )
        terminal_status: Optional[str] = None
        terminal_reason: Optional[str] = None
        for record in after:
            opportunities = record.get("opportunities") or []
            opportunities = opportunities if isinstance(opportunities, list) else []
            current = next(
                (
                    item for item in opportunities
                    if isinstance(item, dict) and item.get("opportunity_id") == opportunity_id
                ),
                None,
            )
            if current and current.get("status") in TERMINAL_STATUSES:
                terminal_status = str(current["status"])
                reasons = current.get("blocked_by") or record.get("blocking_reasons") or []
                terminal_reason = str(reasons[0]) if reasons else terminal_status
                if terminal_status in {"invalidated", "expired"}:
                    invalidation_counts[terminal_reason] += 1
                break
        entry_facts = entry_record.get("facts") or {}
        entry_facts = entry_facts if isinstance(entry_facts, dict) else {}
        ma_coverage, ma_ready = _ma_coverage(entry_facts)
        last_observed_ts = _timestamp(after[-1]) if after else entry_ts
        outcomes.append({
            "opportunity_id": opportunity_id,
            "stage": opportunity.get("stage"),
            "policy_version": opportunity.get("policy_version", entry_record.get("policy_version")),
            "entry_ts": entry_ts,
            "entry_price": entry_price,
            **forward_returns,
            "max_drawdown_90d_pct": max_drawdown,
            "observed_days": round(max(0, last_observed_ts - entry_ts) / DAY_SECONDS, 2),
            "terminal_status": terminal_status,
            "terminal_reason": terminal_reason,
            "ma_coverage_ratio": round(ma_coverage, 4),
            "ma_layers_ready": ma_ready,
        })

    horizon_stats: dict[str, dict[str, Optional[float] | int]] = {}
    for days in FORWARD_DAYS:
        key = f"return_{days}d_pct"
        values = [float(item[key]) for item in outcomes if item[key] is not None]
        horizon_stats[f"{days}d"] = {
            "sample_count": len(values),
            "average_return_pct": round(sum(values) / len(values), 4) if values else None,
        }
    return {
        "label": "M/A前向验证",
        "capability": "capital_acceptance_forward_shadow_validation",
        "disclaimer": "订单簿与Footprint历史有限；本报告是前向影子验证，不是全栈历史回测。",
        "record_count": len(full_records),
        "legacy_record_count": legacy_count,
        "opportunity_count": len(outcomes),
        "ma_layers_ready_rate": (
            round(sum(bool(item["ma_layers_ready"]) for item in outcomes) / len(outcomes), 4)
            if outcomes else 0.0
        ),
        "average_ma_coverage_ratio": (
            round(sum(float(item["ma_coverage_ratio"]) for item in outcomes) / len(outcomes), 4)
            if outcomes else 0.0
        ),
        "horizons": horizon_stats,
        "invalidation_reasons": dict(invalidation_counts),
        "outcomes": outcomes,
    }
