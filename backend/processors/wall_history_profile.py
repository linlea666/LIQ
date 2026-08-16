"""P4：墙历史画像 —— 归档反哺运行时（history_presence_7d / history_consumed_ratio）。

数据流：
    liquidity_wall_archiver 归档（.jsonl / .jsonl.gz）
        → refresh_profile()（engine 后台协程每 6h 调一次，thread pool 内执行）
        → 进程内画像缓存 {coin: {wall_zone_id: profile}}
        → attach_history_profile()（liquidity_wall_engine 每帧查表附加到 zone）

设计约束（遵守大脑只读铁律）：
    - 两个字段只做展示 + AI prompt 输入，首版不进 trust/SR 任何主评分公式
    - 复用 liquidity_wall_postmortem 的纯函数解析/聚合（提取复用），
      不重复实现归档解析
    - 读盘只发生在 refresh_profile（低频后台）；attach 是纯内存 dict 查表
"""
from __future__ import annotations

import gzip
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from processors.liquidity_wall_postmortem import (
    compute_zone_band_profiles,
    extract_event_records,
    extract_zone_records,
    parse_archived_snapshots,
)

logger = logging.getLogger(__name__)

# 与 archiver 一致：归档按北京时区日切
_TZ_CN = timezone(timedelta(hours=8))

_DEFAULT_HISTORY_ROOT = Path(__file__).resolve().parent.parent / "data" / "liquidity_wall_history"

DEFAULT_WINDOW_DAYS = 7

# coin → wall_zone_id → {"presence_7d": float, "consumed_ratio": Optional[float]}
_PROFILE_CACHE: dict[str, dict[str, dict[str, Any]]] = {}
# coin → {"updated_ts": int, "zones": int, "window_days": int}
_PROFILE_META: dict[str, dict[str, Any]] = {}
_LOCK = threading.Lock()


def _read_day_lines(coin_dir: Path, day: str) -> list[str]:
    """读一天的归档行（.jsonl.gz 与 .jsonl 可能并存，都读）。"""
    lines: list[str] = []
    for path in (coin_dir / f"{day}.jsonl.gz", coin_dir / f"{day}.jsonl"):
        if not path.is_file():
            continue
        try:
            if path.suffix == ".gz":
                with gzip.open(path, "rt", encoding="utf-8") as f:
                    lines.extend(f.readlines())
            else:
                with path.open("r", encoding="utf-8") as f:
                    lines.extend(f.readlines())
        except OSError:
            logger.debug("[wall_history_profile] read failed: %s", path,
                         exc_info=True)
    return lines


def refresh_profile(
    coin: str,
    *,
    history_root: Optional[str] = None,
    window_days: int = DEFAULT_WINDOW_DAYS,
    now: Optional[int] = None,
) -> int:
    """重算一个币的画像缓存（同步、可能读几十 MB 归档，请放 thread pool）。

    返回缓存的 zone 数；归档缺失/为空时返回 0 并保留旧缓存
    （避免一次读盘失败把已有画像清掉）。
    """
    coin = coin.upper()
    root = Path(history_root) if history_root else _DEFAULT_HISTORY_ROOT
    coin_dir = root / coin
    now_ts = int(now if now is not None else time.time())

    if not coin_dir.is_dir():
        return 0

    # 日期键按北京时区生成，窗口两侧各多读 1 天防边界漏帧
    end_dt = datetime.fromtimestamp(now_ts, tz=_TZ_CN)
    days = [
        (end_dt - timedelta(days=i)).strftime("%Y%m%d")
        for i in range(window_days + 1, -1, -1)
    ]
    cutoff_ts = now_ts - window_days * 86400

    lines: list[str] = []
    for day in days:
        lines.extend(_read_day_lines(coin_dir, day))
    if not lines:
        return 0

    snapshots = [
        s for s in parse_archived_snapshots(lines)
        if cutoff_ts <= int(s.get("ts", 0) or 0) <= now_ts
    ]
    if not snapshots:
        return 0

    zone_records = extract_zone_records(snapshots)
    event_records = extract_event_records(snapshots)
    # 固定 7 天分母（不足 7 天的归档出现率按 7 天算，诚实反映数据量）
    profiles = compute_zone_band_profiles(
        zone_records, event_records, klines=[],
        window_hours=window_days * 24.0, now_ts=now_ts,
    )

    table: dict[str, dict[str, Any]] = {
        p.wall_zone_id: {
            "presence_7d": p.presence_ratio,
            "consumed_ratio": p.consumed_ratio,
        }
        for p in profiles
    }
    with _LOCK:
        _PROFILE_CACHE[coin] = table
        _PROFILE_META[coin] = {
            "updated_ts": now_ts,
            "zones": len(table),
            "window_days": window_days,
        }
    logger.info(
        "[wall_history_profile] refreshed | coin=%s zones=%d snapshots=%d",
        coin, len(table), len(snapshots),
    )
    return len(table)


def attach_history_profile(coin: str, zones: list[Any]) -> None:
    """把缓存画像按 wall_zone_id 附加到本帧 zone（纯内存查表，热路径安全）。

    缓存未就绪（启动初期 / 无归档）时字段保持 None，不影响任何评分。
    """
    with _LOCK:
        table = _PROFILE_CACHE.get(coin.upper())
    if not table:
        return
    for z in zones:
        prof = table.get(getattr(z, "wall_zone_id", "") or "")
        if prof is None:
            continue
        z.history_presence_7d = prof["presence_7d"]
        z.history_consumed_ratio = prof["consumed_ratio"]


def get_profile_meta(coin: str) -> Optional[dict[str, Any]]:
    with _LOCK:
        meta = _PROFILE_META.get(coin.upper())
        return dict(meta) if meta else None


def reset_for_test() -> None:
    with _LOCK:
        _PROFILE_CACHE.clear()
        _PROFILE_META.clear()
