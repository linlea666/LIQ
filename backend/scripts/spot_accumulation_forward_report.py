#!/usr/bin/env python3
"""从月度完整事实快照生成M/A前向影子验证报告。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from processors.spot_accumulation_forward import build_forward_report  # noqa: E402
from storage.spot_accumulation_store import SpotAccumulationStore  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--output", default="")
    args = parser.parse_args()
    records = SpotAccumulationStore(args.data_dir).load_facts_snapshots()
    report = build_forward_report(records)
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(text + "\n", encoding="utf-8")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
