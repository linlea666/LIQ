#!/usr/bin/env python3
"""Bottom Model 历史回填脚本（独立调研工具，不进生产链路）。

用法：
    cd backend
    # 全量回填（Coinglass + BGeometrics + Yahoo；已新鲜的指标自动跳过）
    python3 scripts/backfill_bottom_model.py

    # 只回填某些源（BGeometrics 免费档 8/h、15/day，额度紧张时分批跑）
    python3 scripts/backfill_bottom_model.py --sources coinglass,yahoo_cme
    python3 scripts/backfill_bottom_model.py --sources bgeometrics

    # 强制重拉（无视日期戳去重；BGeometrics 慎用）
    python3 scripts/backfill_bottom_model.py --force

说明：
- Coinglass 指标端点单次返回全历史，回填 = 正常采集一轮即可。
- BGeometrics 共 7 个端点，一轮 7 次外呼，在 8/h 限内可一次跑完；
  若当天其余额度已被消耗，失败端点次日自动补拉。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import get_settings  # noqa: E402
from processors.bottom_model.collector import BottomModelCollector  # noqa: E402
from sources.bgeometrics import create_bgeometrics_source  # noqa: E402
from sources.coinglass import create_coinglass_source  # noqa: E402
from sources.yahoo_cme import create_yahoo_cme_source  # noqa: E402
from storage.bottom_model_store import BottomModelStore  # noqa: E402

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("backfill_bottom_model")

_VALID_SOURCES = {"coinglass", "bgeometrics", "yahoo_cme"}


async def main() -> int:
    parser = argparse.ArgumentParser(description="Bottom Model 历史回填")
    parser.add_argument("--sources", default="",
                        help="逗号分隔源过滤：coinglass,bgeometrics,yahoo_cme（默认全部）")
    parser.add_argument("--force", action="store_true",
                        help="无视日期戳去重强制重拉（BGeometrics 慎用）")
    parser.add_argument("--spacing", type=float, default=None,
                        help="Coinglass 请求间隔秒（默认取配置）")
    args = parser.parse_args()

    only_sources = None
    if args.sources.strip():
        only_sources = {s.strip() for s in args.sources.split(",") if s.strip()}
        invalid = only_sources - _VALID_SOURCES
        if invalid:
            parser.error(f"未知源：{sorted(invalid)}；可选 {sorted(_VALID_SOURCES)}")

    settings = get_settings()
    store = BottomModelStore(settings.bottom_model.data_dir)
    cg = create_coinglass_source()
    bg = create_bgeometrics_source(settings.bgeometrics)
    yahoo = create_yahoo_cme_source(settings.yahoo_cme)
    spacing = args.spacing if args.spacing is not None \
        else settings.bottom_model.coinglass_spacing_sec

    collector = BottomModelCollector(
        store, cg, bgeometrics=bg, yahoo_cme=yahoo,
        coinglass_spacing_sec=spacing,
    )

    try:
        summary = await collector.run_once(force=args.force, only_sources=only_sources)
    finally:
        await cg.close()
        if bg is not None:
            await bg.close()
        if yahoo is not None:
            await yahoo.close()

    print("\n===== 采集结果 =====")
    for key, item in summary["specs"].items():
        print(f"  {key:<22} {item['status']:<8} "
              f"{item.get('rows', '')} {item.get('error', '')}")
    print(f"\nfetched={summary['fetched']} fresh={summary['skipped_fresh']} "
          f"failed={summary['failed']} elapsed={summary['elapsed_sec']}s")

    print("\n===== 序列覆盖 =====")
    coverage = store.coverage()
    for metric in sorted(coverage):
        info = coverage[metric]
        print(f"  {metric:<24} {info['first_day']} → {info['last_day']}  "
              f"({info['count']} 天)")
    if bg is not None:
        print(f"\nBGeometrics 配额：{bg.quota_snapshot()}")
    store.close()
    return 0 if summary["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
