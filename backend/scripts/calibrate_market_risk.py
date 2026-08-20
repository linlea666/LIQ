#!/usr/bin/env python3
"""从 purged training fold JSONL 生成冻结校准产物；不做生产准入。"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from processors.market_risk_backtest import fit_training_calibration  # noqa: E402


def _json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"line {line_number} must be a JSON object")
        rows.append(value)
    return rows


def _epoch(value: str) -> int:
    return int(datetime.fromisoformat(value).timestamp())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--training-jsonl", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--min-samples", type=int, default=50)
    parser.add_argument("--force", action="store_true")
    parser.add_argument(
        "--forensic-fixture", type=Path,
        default=BACKEND_ROOT / "config" / "forensic_2026-08-19_20.json",
    )
    args = parser.parse_args()
    if args.output.exists() and not args.force:
        raise SystemExit(f"refusing to overwrite {args.output}; pass --force explicitly")
    forensic = _json(args.forensic_fixture)
    if forensic.get("excluded_from_oos") is not True or not forensic.get("excluded_range"):
        raise SystemExit("forensic fixture must declare excluded_from_oos and excluded_range")
    excluded = forensic["excluded_range"]
    artifact = fit_training_calibration(
        _jsonl(args.training_jsonl), min_samples=args.min_samples,
        forbidden_intervals=[(_epoch(excluded["from"]), _epoch(excluded["to"]))],
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text(
        json.dumps(artifact.model_dump(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, args.output)
    print(json.dumps({
        "output": str(args.output),
        "calibration_version": artifact.calibration_version,
        "admitted_for_production": artifact.admitted_for_production,
        "training_window": artifact.training_window,
        "note": "artifact is frozen but remains shadow-only until OOS gates pass",
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
