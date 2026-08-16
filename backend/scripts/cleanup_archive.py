#!/usr/bin/env python3
"""归档目录磁盘清理脚本（运维独立工具，不在引擎主路径运行）。

适用场景：
    - 生产服务器磁盘水位逼近 80%，archiver 已在自我保护跳过写入
    - 需要立即按保留期清掉旧 jsonl，腾空间 + 让落盘恢复

覆盖目录（按各 archiver 的 _KEEP_DAYS 默认值，与代码常量保持一致）：
    1. data/liquidity_wall_history/{COIN}/{YYYYMMDD}.jsonl  ← 默认 14 天
    2. data/sweep_watch/{COIN}/{YYYYMMDD}.jsonl            ← 7 天

不会动：
    - data/liq_endpoint_dumps/*.json   测试 fixture（API 抓包）
    - data/news_ledger.json            新闻去重账本
    - data/roll/                       仓位状态

用法：
    # 在仓库根（或 backend/）下运行
    python3 backend/scripts/cleanup_archive.py            # dry-run 预览
    python3 backend/scripts/cleanup_archive.py --apply    # 实际删除

    # 自定义保留天数（覆盖默认）
    python3 backend/scripts/cleanup_archive.py --apply --wall-days 14 --sweep-days 3

    # 存量一次性压缩：把非当日的 .jsonl 压缩为 .jsonl.gz（archiver 上线
    # 压缩逻辑前积累的大文件用这个清一次；引擎读取方已兼容 gz）
    python3 backend/scripts/cleanup_archive.py --apply --compress

不进 cron 的原因：archiver 内置 _gc_if_due() 每小时 GC，运行中进程已自动维护。
本脚本针对的是"长期累积 / 进程未运行 / 手动一次性大清理"场景。
"""
from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable

_TZ_CN = timezone(timedelta(hours=8))

_REPO_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
)
_DATA_ROOT = os.path.join(_REPO_ROOT, "data")

def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


# 与 backend/processors/{liquidity_wall_archiver,sweep_watch_archiver}.py 的
# _KEEP_DAYS 常量保持同步。改动那边时记得同步这里默认值。
_DEFAULT_WALL_KEEP_DAYS = _env_int("LIQUIDITY_WALL_KEEP_DAYS", 14)
_DEFAULT_SWEEP_KEEP_DAYS = 7


@dataclass
class CleanupResult:
    target: str
    keep_days: int
    scanned_files: int
    rotated_files: int
    rotated_bytes: int

    @property
    def rotated_mb(self) -> float:
        return self.rotated_bytes / (1024 * 1024)


def _iter_dated_jsonl(root: str) -> Iterable[tuple[str, str]]:
    """枚举 root/{COIN}/{YYYYMMDD}.jsonl[.gz]，产出 (full_path, day_key)。"""
    if not os.path.isdir(root):
        return
    for coin in sorted(os.listdir(root)):
        sub = os.path.join(root, coin)
        if not os.path.isdir(sub):
            continue
        for name in sorted(os.listdir(sub)):
            if name.endswith(".jsonl.gz"):
                day_key = name[: -len(".jsonl.gz")]
            elif name.endswith(".jsonl"):
                day_key = name[: -len(".jsonl")]
            else:
                continue
            if len(day_key) != 8 or not day_key.isdigit():
                continue
            yield os.path.join(sub, name), day_key


def _cleanup_dir(target: str, keep_days: int, apply: bool) -> CleanupResult:
    cutoff = (datetime.now(tz=_TZ_CN) - timedelta(days=keep_days)).strftime("%Y%m%d")
    scanned = 0
    rotated = 0
    rotated_bytes = 0
    for path, day_key in _iter_dated_jsonl(target):
        scanned += 1
        if day_key >= cutoff:
            continue
        try:
            size = os.path.getsize(path)
        except OSError:
            size = 0
        if apply:
            try:
                os.remove(path)
            except OSError as e:
                print(f"  [WARN] 无法删除 {path}: {e}", file=sys.stderr)
                continue
        rotated += 1
        rotated_bytes += size
    return CleanupResult(
        target=target,
        keep_days=keep_days,
        scanned_files=scanned,
        rotated_files=rotated,
        rotated_bytes=rotated_bytes,
    )


def _compress_dir(target: str, apply: bool) -> CleanupResult:
    """把非当日的 .jsonl 压缩为 .jsonl.gz（gz 已存在时追加 gzip member）。"""
    import gzip

    today_key = datetime.now(tz=_TZ_CN).strftime("%Y%m%d")
    scanned = 0
    rotated = 0
    rotated_bytes = 0
    for path, day_key in _iter_dated_jsonl(target):
        if not path.endswith(".jsonl"):
            continue
        scanned += 1
        if day_key >= today_key:
            continue  # 当日文件保持明文（引擎仍在追加写）
        try:
            size = os.path.getsize(path)
        except OSError:
            size = 0
        if apply:
            try:
                with open(path, "rb") as src:
                    data = src.read()
                with open(path + ".gz", "ab") as dst:
                    dst.write(gzip.compress(data))
                os.remove(path)
            except OSError as e:
                print(f"  [WARN] 无法压缩 {path}: {e}", file=sys.stderr)
                continue
        rotated += 1
        rotated_bytes += size
    return CleanupResult(
        target=target,
        keep_days=0,
        scanned_files=scanned,
        rotated_files=rotated,
        rotated_bytes=rotated_bytes,
    )


def _print_result(r: CleanupResult, apply: bool) -> None:
    verb = "已删除" if apply else "将删除"
    rel = os.path.relpath(r.target, _REPO_ROOT)
    print(
        f"  {rel:<40s} keep={r.keep_days:2d}d  "
        f"扫描 {r.scanned_files:5d} 个  {verb} {r.rotated_files:5d} 个 "
        f"({r.rotated_mb:8.2f} MB)"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="实际删除（默认 dry-run 预览）")
    parser.add_argument(
        "--wall-days", type=int, default=_DEFAULT_WALL_KEEP_DAYS,
        help=f"liquidity_wall_history 保留天数（默认 {_DEFAULT_WALL_KEEP_DAYS}）",
    )
    parser.add_argument(
        "--sweep-days", type=int, default=_DEFAULT_SWEEP_KEEP_DAYS,
        help=f"sweep_watch 保留天数（默认 {_DEFAULT_SWEEP_KEEP_DAYS}）",
    )
    parser.add_argument(
        "--data-root", default=_DATA_ROOT,
        help=f"data 根路径（默认 {_DATA_ROOT}）",
    )
    parser.add_argument(
        "--compress", action="store_true",
        help="附加动作：把非当日的 .jsonl 压缩为 .jsonl.gz（存量一次性瘦身）",
    )
    args = parser.parse_args()

    targets = [
        (os.path.join(args.data_root, "liquidity_wall_history"), args.wall_days),
        (os.path.join(args.data_root, "sweep_watch"), args.sweep_days),
    ]

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"\n[{mode}] 归档目录清理 | data_root={args.data_root}")
    print("─" * 80)

    total_files = 0
    total_bytes = 0
    for target, keep_days in targets:
        r = _cleanup_dir(target, keep_days, apply=args.apply)
        _print_result(r, apply=args.apply)
        total_files += r.rotated_files
        total_bytes += r.rotated_bytes

    if args.compress:
        verb = "已压缩" if args.apply else "将压缩"
        print(f"\n[{mode}] 存量压缩（非当日 .jsonl → .jsonl.gz）")
        print("─" * 80)
        for target, _ in targets:
            r = _compress_dir(target, apply=args.apply)
            rel = os.path.relpath(r.target, _REPO_ROOT)
            print(
                f"  {rel:<40s} 扫描 {r.scanned_files:5d} 个  "
                f"{verb} {r.rotated_files:5d} 个 ({r.rotated_mb:8.2f} MB 原始)"
            )

    print("─" * 80)
    total_mb = total_bytes / (1024 * 1024)
    summary = "已释放" if args.apply else "可释放"
    print(f"  汇总：{summary} {total_files} 个文件，约 {total_mb:.2f} MB")
    if not args.apply:
        print("  ⚠️ 这只是预览。加 --apply 才会真删。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
