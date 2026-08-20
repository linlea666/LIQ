#!/usr/bin/env python3
"""从 PIT 合格物化快照构建可复现研究集；非法/冻结快照永不入集。"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any


def _value(evidence: list[dict[str, Any]], name: str, key: str, default: float = 0.0) -> float:
    item = next((row for row in evidence if row.get("name") == name), None)
    try:
        return float((item or {}).get("values", {}).get(key, default))
    except (TypeError, ValueError):
        return default


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sqlite", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--from-ts", type=int, default=0)
    parser.add_argument("--to-ts", type=int, default=2**63 - 1)
    args = parser.parse_args()
    conn = sqlite3.connect(args.sqlite)
    rows = conn.execute(
        """SELECT payload FROM snapshots WHERE decision_time>=? AND decision_time<=?
           ORDER BY decision_time ASC""",
        (args.from_ts, args.to_ts),
    ).fetchall()
    conn.close()
    materialized: list[dict[str, Any]] = []
    rejected = 0
    for (raw,) in rows:
        snapshot = json.loads(raw)
        if (
            snapshot.get("valid_for_calibration") is not True
            or snapshot.get("quality_layer") != "normal"
            or snapshot.get("pit_violations")
        ):
            rejected += 1
            continue
        evidence = list(snapshot.get("evidence") or [])
        spot = next((item for item in evidence if item.get("causal_root") == "spot_demand"), {})
        materialized.append({
            "decision_time": int(snapshot["decision_time"]),
            "spot_taker_imbalance": float(spot.get("values", {}).get("imbalance", 0.0)),
            "spot_quote_usd": float(spot.get("values", {}).get("aggressor_buy_quote", 0.0)) + float(spot.get("values", {}).get("aggressor_sell_quote", 0.0)),
            "oi_change_1h_pct": _value(evidence, "standardized_oi_with_perp_flow", "oi_change_1h_pct"),
            "funding_rate": _value(evidence, "predicted_funding_crowding", "predicted_rate_observed"),
            "liquidation_1h_usd": max(
                _value(evidence, "realized_liquidation_flow_1h", "long_executed_notional_usd"),
                _value(evidence, "realized_liquidation_flow_1h", "short_executed_notional_usd"),
            ),
            "liquidation_density_usd": max(
                (_value([item], item.get("name", ""), "estimated_density_usd") for item in evidence if str(item.get("name", "")).startswith("estimated_density_")),
                default=0.0,
            ),
            "wall_attack": max(
                (_value([item], "wall_attack_or_removal", "active_attack_score") for item in evidence),
                default=0.0,
            ),
            "price_move_5m_pct": _value(evidence, "closed_5m_price_feedback", "price_change_5m_pct"),
            "full_engine_confidence": max((float(item.get("confidence", 0.0)) for item in evidence if item.get("role") == "scoring"), default=0.0),
        })
    encoded_lines = [json.dumps(row, sort_keys=True, separators=(",", ":")) for row in materialized]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("\n".join(encoded_lines) + ("\n" if encoded_lines else ""), encoding="utf-8")
    digest = hashlib.sha256("\n".join(encoded_lines).encode()).hexdigest()
    print(json.dumps({
        "output": str(args.output), "rows": len(materialized), "rejected": rejected,
        "dataset_hash": digest,
        "warning": "outcomes and all-source raw PIT replay must be joined before production admission",
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
