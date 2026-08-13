#!/usr/bin/env python3
"""离线生成 BTC Bottom Model 完整数学审计 JSON/Markdown。"""

from __future__ import annotations

import argparse
import json
import os
import sys

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

from processors.bottom_model.audit import run_mathematical_audit  # noqa: E402
from storage.bottom_model_store import BottomModelStore  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default=os.path.join(BACKEND, "data/bottom_model"))
    parser.add_argument("--output-dir", default=os.path.join(BACKEND, "data/bottom_model/audits"))
    parser.add_argument("--as-of")
    parser.add_argument(
        "--provenance-json",
        help="可选：冻结数据来源和只读核验事实的 JSON 文件",
    )
    args = parser.parse_args()
    provenance = None
    if args.provenance_json:
        with open(args.provenance_json, encoding="utf-8") as handle:
            provenance = json.load(handle)
    store = BottomModelStore(args.data_dir)
    try:
        payload, markdown = run_mathematical_audit(
            store, args.as_of, source_provenance=provenance,
        )
    finally:
        store.close()
    os.makedirs(args.output_dir, exist_ok=True)
    stem = os.path.join(args.output_dir, payload["audit_id"])
    with open(stem + ".json", "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, allow_nan=False)
    with open(stem + ".md", "w", encoding="utf-8") as handle:
        handle.write(markdown)
    print(json.dumps({"audit_id": payload["audit_id"], "status": payload["status"], "json": stem + ".json", "markdown": stem + ".md"}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
