#!/usr/bin/env python3
"""审计 8/19–20 法证里程碑；缺数据即失败，不生成或补写任何信号。"""
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _risk_rows(path: Path) -> list[dict[str, Any]]:
    connection = sqlite3.connect(path)
    try:
        rows = connection.execute(
            "SELECT payload FROM snapshots WHERE coin='BTC' ORDER BY decision_time"
        ).fetchall()
        return [json.loads(row[0]) for row in rows]
    finally:
        connection.close()


def _matches(row: dict[str, Any], milestone: dict[str, Any]) -> bool:
    if milestone.get("stage") and row.get("stage") != milestone["stage"]:
        return False
    if milestone.get("spot_confirmed") is True and row.get("spot_confirmed") is not True:
        return False
    signal = milestone.get("research_signal")
    if signal and signal not in (row.get("research_signals") or []):
        return False
    if milestone.get("event") == "new_episode" and not row.get("episode_id"):
        return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument(
        "--fixture", type=Path,
        default=Path(__file__).parents[1] / "config" / "forensic_2026-08-19_20.json",
    )
    parser.add_argument("--external-events", type=Path)
    args = parser.parse_args()
    fixture = _load_json(args.fixture)
    risk = _risk_rows(args.db)
    external = _load_json(args.external_events) if args.external_events else []
    failures: list[str] = []
    results: list[dict[str, Any]] = []
    for milestone in fixture["milestones"]:
        target = int(datetime.fromisoformat(milestone["local_time"]).timestamp())
        tolerance = int(milestone.get("tolerance_sec", 0))
        if milestone["channel"] == "market_risk":
            candidates = [
                row for row in risk
                if abs(int(row.get("decision_time") or 0) - target) <= tolerance
                and _matches(row, milestone)
            ]
        else:
            candidates = [
                row for row in external
                if row.get("channel") == milestone["channel"]
                and abs(int(row.get("decision_time") or 0) - target) <= tolerance
                and all(
                    row.get(key) == milestone[key]
                    for key in ("state", "event") if key in milestone
                )
            ]
        passed = bool(candidates)
        results.append({"id": milestone["id"], "passed": passed})
        if not passed:
            failures.append(milestone["id"])
    print(json.dumps({
        "fixture_version": fixture["fixture_version"],
        "excluded_from_oos": fixture["excluded_from_oos"],
        "passed": not failures, "failures": failures, "results": results,
        "note": "missing milestones are failures; this tool never fabricates signals",
    }, ensure_ascii=False, indent=2))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
