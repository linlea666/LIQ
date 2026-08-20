#!/usr/bin/env python3
"""把冻结校准产物晋级为生产产物；禁止只翻 admitted 布尔值。"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from models.market_risk import CalibrationArtifact  # noqa: E402
from processors.market_risk_backtest import admission_gates  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--frozen-artifact", required=True, type=Path)
    parser.add_argument("--admission-report", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    if args.output.exists() and not args.force:
        raise SystemExit(f"refusing to overwrite {args.output}; pass --force explicitly")
    artifact = CalibrationArtifact.model_validate_json(
        args.frozen_artifact.read_text(encoding="utf-8")
    )
    report_bytes = args.admission_report.read_bytes()
    report = json.loads(report_bytes)
    required = {
        "warning_tp", "warning_total", "critical_tp", "critical_total",
        "recall_hits", "qualifying_events", "false_warning_per_day",
        "false_critical_per_14d", "core_coverage", "incremental_delta_ci",
        "dataset_hash", "code_hash", "config_hash", "all_required_sources_pit",
        "walk_forward_months",
    }
    missing = sorted(required - set(report))
    if missing:
        raise SystemExit("admission report missing: " + ", ".join(missing))
    if report["all_required_sources_pit"] is not True:
        raise SystemExit("all required sources must have PIT-complete replay")
    if float(report["walk_forward_months"]) < 12:
        raise SystemExit("walk-forward history must cover at least 12 months")
    gates = admission_gates(
        warning_tp=int(report["warning_tp"]), warning_total=int(report["warning_total"]),
        critical_tp=int(report["critical_tp"]), critical_total=int(report["critical_total"]),
        recall_hits=int(report["recall_hits"]), qualifying_events=int(report["qualifying_events"]),
        false_warning_per_day=float(report["false_warning_per_day"]),
        false_critical_per_14d=float(report["false_critical_per_14d"]),
        core_coverage=float(report["core_coverage"]),
        incremental_delta_ci=tuple(report["incremental_delta_ci"]),
    )
    if not gates["admitted"]:
        raise SystemExit("admission gates failed: " + json.dumps(gates, ensure_ascii=False))
    promoted = artifact.model_copy(update={
        "status": "production_admitted", "admitted_for_production": True,
        "dataset_hash": str(report["dataset_hash"]),
        "code_hash": str(report["code_hash"]),
        "config_hash": str(report["config_hash"]),
        "admission_report_hash": hashlib.sha256(report_bytes).hexdigest(),
        "admission_metrics": {**report, "gates": gates},
    })
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text(
        json.dumps(promoted.model_dump(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, args.output)
    print(json.dumps({
        "output": str(args.output), "status": promoted.status,
        "admission_report_hash": promoted.admission_report_hash,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
