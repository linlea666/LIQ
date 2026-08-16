#!/usr/bin/env python3
"""W4-T1 流动性墙后验报告 CLI。

读取 W1-T2 落盘的 90 天历史 wall_zone 数据，拉对应窗口的 Binance K 线，
计算 trust/SR/SA/break_through/wall_consumed/dominant_role 等评分的事后命中率，
输出 JSON + Markdown 双格式报告。

设计原则（按 dev-constraints）：
    - 只读：不修改任何引擎逻辑、不动阈值、不重新打分
    - IO 集中：所有 IO 在本文件，纯函数与判定逻辑在
      processors/liquidity_wall_postmortem.py
    - 失败保守：缺数据 → 报告里显式标 insufficient_data；不抛异常打断流程

用法：
    cd backend
    python3 scripts/liquidity_wall_postmortem.py --coin BTC --window 7d
    python3 scripts/liquidity_wall_postmortem.py --coin ETH --window 14d \\
        --output /tmp/eth_postmortem
    python3 scripts/liquidity_wall_postmortem.py --coin BTC --start 20260420 \\
        --end 20260427

CLI 输出（默认）：
    backend/data/liquidity_wall_postmortem/{COIN}/{YYYY-MM-DD}/report.json
    backend/data/liquidity_wall_postmortem/{COIN}/{YYYY-MM-DD}/report.md
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

# 让脚本可以从任意位置运行（cd backend 或 cd repo root 都行）
_BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from processors.liquidity_wall_postmortem import (  # noqa: E402
    KlinePoint,
    PostmortemReport,
    build_report,
    parse_archived_snapshots,
    parse_binance_klines,
    report_to_dict,
    report_to_markdown,
)

logger = logging.getLogger("liquidity_wall_postmortem")

_DEFAULT_HISTORY_ROOT = _BACKEND_ROOT / "data" / "liquidity_wall_history"
_DEFAULT_OUTPUT_ROOT = _BACKEND_ROOT / "data" / "liquidity_wall_postmortem"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# CLI 参数
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="W4-T1 流动性墙后验报告（事后命中率统计）",
    )
    p.add_argument("--coin", required=True,
                   help="币种代码，如 BTC / ETH / SOL")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--window", default="7d",
                   help="时间窗口（结尾，默认 7d；支持 1d/3d/7d/14d/30d）")
    g.add_argument("--start", help="起始日期（YYYYMMDD），与 --end 配对")
    p.add_argument("--end", help="结束日期（YYYYMMDD），与 --start 配对")
    p.add_argument("--history-root", default=str(_DEFAULT_HISTORY_ROOT),
                   help="W1-T2 归档目录（默认 backend/data/liquidity_wall_history）")
    p.add_argument("--output", default=None,
                   help="报告输出根目录（默认 backend/data/liquidity_wall_postmortem）")
    p.add_argument("--kline-interval", default="5m",
                   help="K 线间隔（默认 5m，与 wall 落盘频率匹配）")
    p.add_argument("--no-klines", action="store_true",
                   help="不拉 K 线（调试用，所有 outcome 都将是 insufficient_data）")
    p.add_argument("--verbose", action="store_true", help="详细日志")
    return p.parse_args(argv)


def _parse_window(window: str) -> int:
    """支持 1d / 3d / 7d / 14d / 30d 等。返回秒数。"""
    m = re.fullmatch(r"(\d+)d", window.strip().lower())
    if not m:
        raise ValueError(f"unsupported window: {window!r} (expected like '7d')")
    return int(m.group(1)) * 86400


def _resolve_window(args: argparse.Namespace) -> tuple[int, int]:
    """返回 (start_ts_sec, end_ts_sec)。"""
    if args.start and args.end:
        start_dt = datetime.strptime(args.start, "%Y%m%d").replace(tzinfo=timezone.utc)
        end_dt = datetime.strptime(args.end, "%Y%m%d").replace(tzinfo=timezone.utc)
        # end 是当日结束 → +1day
        end_dt = end_dt + timedelta(days=1)
        return int(start_dt.timestamp()), int(end_dt.timestamp())
    if args.start or args.end:
        raise ValueError("--start/--end must be provided together")
    span_sec = _parse_window(args.window)
    end_ts = int(datetime.now(timezone.utc).timestamp())
    return end_ts - span_sec, end_ts


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# IO：归档读取
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def load_jsonl_files(
    history_root: Path, coin: str, start_ts: int, end_ts: int,
) -> list[dict]:
    """读 W1-T2 归档目录里 [start_ts, end_ts] 覆盖的所有日 JSONL。

    日期边界：归档命名 YYYYMMDD（北京时区）。为了避免漏帧，
    向两侧各扩 1 天读取，再按 ts 在内存中精确过滤。
    """
    coin_dir = history_root / coin.upper()
    if not coin_dir.is_dir():
        logger.warning("history dir not found: %s", coin_dir)
        return []

    # 按 UTC 起止生成候选日期（多覆盖 1 天保险）
    start_dt = datetime.fromtimestamp(start_ts, tz=timezone.utc) - timedelta(days=1)
    end_dt = datetime.fromtimestamp(end_ts, tz=timezone.utc) + timedelta(days=1)
    days: list[str] = []
    cur = start_dt
    while cur <= end_dt:
        days.append(cur.strftime("%Y%m%d"))
        cur += timedelta(days=1)

    snapshots: list[dict] = []
    files_read = 0
    for day in days:
        # 同一天可能同时存在 .jsonl.gz（历史压缩）与 .jsonl（边界补写），都读
        candidates = [coin_dir / f"{day}.jsonl.gz", coin_dir / f"{day}.jsonl"]
        for path in candidates:
            if not path.is_file():
                continue
            try:
                if path.suffix == ".gz":
                    import gzip
                    with gzip.open(path, "rt", encoding="utf-8") as f:
                        lines = f.readlines()
                else:
                    with path.open("r", encoding="utf-8") as f:
                        lines = f.readlines()
                parsed = parse_archived_snapshots(lines)
                files_read += 1
                for p in parsed:
                    ts = int(p.get("ts", 0) or 0)
                    if start_ts <= ts <= end_ts:
                        snapshots.append(p)
            except OSError:
                logger.exception("failed to read %s", path)
                continue
    logger.info("loaded %d snapshots from %d file(s) under %s",
                len(snapshots), files_read, coin_dir)
    return snapshots


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# IO：K 线拉取（复用 binance_futures.fetch_klines）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _interval_to_ms(interval: str) -> int:
    m = re.fullmatch(r"(\d+)([smhd])", interval.strip().lower())
    if not m:
        raise ValueError(f"unsupported kline interval: {interval!r}")
    n = int(m.group(1))
    unit_ms = {"s": 1000, "m": 60_000, "h": 3_600_000, "d": 86_400_000}
    return n * unit_ms[m.group(2)]


async def fetch_klines_window(
    coin: str, start_ts: int, end_ts: int, interval: str = "5m",
) -> list[KlinePoint]:
    """复用 sources.binance_futures.create_binance_source。

    Binance 单次最多 1500 根，因此分页拉取直到覆盖整个窗口。
    """
    from sources.binance_futures import create_binance_source

    symbol = f"{coin.upper()}USDT"
    src = create_binance_source()
    interval_ms = _interval_to_ms(interval)
    all_raw: list[list] = []

    cur_ms = start_ts * 1000
    end_ms = end_ts * 1000
    page_limit = 1500

    try:
        while cur_ms < end_ms:
            page_end = min(cur_ms + page_limit * interval_ms, end_ms)
            raw = await src.fetch_klines(
                symbol=symbol, interval=interval, limit=page_limit,
                start_time=cur_ms, end_time=page_end,
            )
            if not raw:
                logger.warning("fetch_klines returned empty | symbol=%s window=[%d,%d]",
                               symbol, cur_ms, page_end)
                break
            all_raw.extend(raw)
            # 防止死循环（每页严格前进）
            try:
                last_open = int(raw[-1][0])
            except (TypeError, ValueError, IndexError):
                break
            next_ms = last_open + interval_ms
            if next_ms <= cur_ms:
                break
            cur_ms = next_ms
            if len(raw) < page_limit:
                break
    finally:
        try:
            await src.close()
        except Exception:  # noqa: BLE001
            pass

    klines = parse_binance_klines(all_raw)
    logger.info("fetched %d klines for %s %s [%s → %s]",
                len(klines), symbol, interval,
                datetime.fromtimestamp(start_ts, tz=timezone.utc).isoformat(),
                datetime.fromtimestamp(end_ts, tz=timezone.utc).isoformat())
    return klines


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# IO：报告输出
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def write_report(
    report: PostmortemReport, output_dir: Path,
) -> tuple[Path, Path]:
    """写 JSON + Markdown 到输出目录，返回 (json_path, md_path)。"""
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "report.json"
    md_path = output_dir / "report.md"

    json_payload = report_to_dict(report)
    json_path.write_text(
        json.dumps(json_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    md_path.write_text(report_to_markdown(report), encoding="utf-8")
    return json_path, md_path


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Main
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def main_async(args: argparse.Namespace) -> int:
    coin = args.coin.upper()
    history_root = Path(args.history_root)
    output_root = Path(args.output) if args.output else _DEFAULT_OUTPUT_ROOT

    start_ts, end_ts = _resolve_window(args)
    logger.info("window: [%s → %s] (%d sec)",
                datetime.fromtimestamp(start_ts, tz=timezone.utc).isoformat(),
                datetime.fromtimestamp(end_ts, tz=timezone.utc).isoformat(),
                end_ts - start_ts)

    snapshots = load_jsonl_files(history_root, coin, start_ts, end_ts)
    if not snapshots:
        logger.warning(
            "no snapshots in window — make sure %s has been collecting "
            "(W1-T2 archiver enabled)", history_root,
        )

    if args.no_klines or not snapshots:
        klines: list[KlinePoint] = []
    else:
        try:
            klines = await fetch_klines_window(
                coin, start_ts, end_ts, interval=args.kline_interval,
            )
        except Exception:  # noqa: BLE001
            logger.exception("fetch_klines failed; continuing with empty klines")
            klines = []

    now_ts = int(datetime.now(timezone.utc).timestamp())
    report = build_report(coin, snapshots, klines, now_ts=now_ts)

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    output_dir = output_root / coin / today
    json_path, md_path = write_report(report, output_dir)

    logger.info("✓ report written: %s", json_path)
    logger.info("✓ report written: %s", md_path)

    # 终端简要总结
    print()
    print(f"=== {coin} 流动性墙后验摘要 ===")
    print(f"窗口 snapshot 帧：{report.total_snapshots}")
    print(f"wall 记录：{report.total_zone_records}　|　事件：{report.total_events}")
    if report.consumed_stats.evaluated > 0:
        print(f"wall_consumed_confidence ≥ 0.6 命中率：{report.consumed_stats.hit_rate:.1%}"
              f"（n={report.consumed_stats.evaluated}）")
    if report.break_through_stats.evaluated > 0:
        print(f"break_through_risk ≥ 0.6 命中率：{report.break_through_stats.hit_rate:.1%}"
              f"（n={report.break_through_stats.evaluated}）")
    if report.removal_stats.evaluated > 0:
        print(f"wall_removal_risk ≥ 0.6 命中率：{report.removal_stats.hit_rate:.1%}"
              f"（n={report.removal_stats.evaluated}）")
    print(f"详细报告：{md_path}")
    print()

    return 0


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    try:
        return asyncio.run(main_async(args))
    except KeyboardInterrupt:
        logger.warning("interrupted by user")
        return 130


if __name__ == "__main__":
    sys.exit(main())
