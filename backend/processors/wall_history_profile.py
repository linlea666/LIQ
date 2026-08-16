"""P4：墙历史画像 —— 归档反哺运行时（history_presence_7d / history_consumed_ratio）。

数据流：
    liquidity_wall_archiver 归档（.jsonl / .jsonl.gz）
        → refresh_profile()（engine 后台协程周期调用，thread pool 内执行）
        → 进程内画像缓存 {coin: {price_bucket_key: profile}}
        → attach_history_profile()（liquidity_wall_engine 每帧查表附加到 zone）

设计约束（遵守大脑只读铁律）：
    - 两个字段只做展示 + AI prompt 输入，首版不进 trust/SR 任何主评分公式
    - 读盘只发生在 refresh_profile（低频后台）；attach 是纯内存 dict 查表

内存防御（OOM 事故复盘后的硬约束）：
    - 逐行流式解析，只提取 (价格桶, 小时桶, 事件类型) 极简信息，
      任何时刻不持有整文件文本 / dict 列表 —— 内存 O(价位带数)
    - 单日文件解压后体积 > _MAX_DAY_UNCOMPRESSED_BYTES（80MB）直接跳过并告警：
      这类文件是 P0 归档瘦身前的旧 6.5s 高频存量（~230MB/天），一次性读入
      曾把 2G 容器打爆（OOM exit 137）

画像 key（ATR 无关的固定比例价格桶）：
    - wall_zone_id 的桶宽依赖生成当时的 ATR，7 天内 ATR 漂移会让同一物理
      价位分裂成多个 id → 查表大面积 miss。
    - 改用对数比例桶：idx = floor(ln(price) / ln(1.002))，桶宽恒为价格的
      0.2%，跨 ATR / 跨天稳定。attach 时查 idx±1 三桶取 presence 最高者，
      吸收桶边界抖动。
    - 不改 wall_zone_id 本身（归档连续性与事件关联不动）。
"""
from __future__ import annotations

import gzip
import json
import logging
import math
import os
import struct
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

logger = logging.getLogger(__name__)

# 与 archiver 一致：归档按北京时区日切
_TZ_CN = timezone(timedelta(hours=8))

_DEFAULT_HISTORY_ROOT = Path(__file__).resolve().parent.parent / "data" / "liquidity_wall_history"

DEFAULT_WINDOW_DAYS = 7

# 单日文件解压后体积上限；超过视为旧高频存量，跳过不读
_MAX_DAY_UNCOMPRESSED_BYTES = 80 * 1024 * 1024

# 对数价格桶比例（0.2%/桶）
_BUCKET_LOG_STEP = math.log(1.002)

# 事件去重粒度（引擎在条件持续期间每帧重复发同一事件）
_EVENT_DEDUP_BUCKET_SEC = 600

# coin → bucket_key → {"presence_7d": float, "consumed_ratio": Optional[float]}
_PROFILE_CACHE: dict[str, dict[str, dict[str, Any]]] = {}
# coin → {"updated_ts": int, "zones": int, "window_days": int, "skipped_files": int}
_PROFILE_META: dict[str, dict[str, Any]] = {}
_LOCK = threading.Lock()


# ──────────────────────────────────────────────────────────────────────
# 价格桶 key
# ──────────────────────────────────────────────────────────────────────

def _bucket_idx(price: float) -> Optional[int]:
    if price is None or price <= 0:
        return None
    return int(math.log(price) / _BUCKET_LOG_STEP)


def _bucket_key(side: str, price: float) -> Optional[str]:
    idx = _bucket_idx(price)
    if idx is None:
        return None
    side_norm = "ask" if side == "ask" else "bid"
    return f"{side_norm}|{idx}"


# ──────────────────────────────────────────────────────────────────────
# 流式读档
# ──────────────────────────────────────────────────────────────────────

def _uncompressed_size(path: Path) -> int:
    """估算文件解压后体积：gz 读尾部 ISIZE（mod 2^32，对 <4GB 文件准确）。"""
    try:
        if path.suffix == ".gz":
            with open(path, "rb") as f:
                f.seek(-4, os.SEEK_END)
                return struct.unpack("<I", f.read(4))[0]
        return path.stat().st_size
    except (OSError, struct.error):
        return 0


def _iter_day_lines(
    coin_dir: Path, day: str, skip_counter: list[int],
) -> Iterator[str]:
    """逐行流式读一天的归档（.jsonl.gz 与 .jsonl 可能并存，都读）。

    超大文件（旧高频存量）单文件跳过并累加 skip_counter[0]，
    不影响同日另一格式文件。
    """
    for path in (coin_dir / f"{day}.jsonl.gz", coin_dir / f"{day}.jsonl"):
        if not path.is_file():
            continue
        size = _uncompressed_size(path)
        if size > _MAX_DAY_UNCOMPRESSED_BYTES:
            skip_counter[0] += 1
            logger.warning(
                "[wall_history_profile] skip oversized archive | %s "
                "uncompressed=%.0fMB > %.0fMB（旧高频存量，30 天后自然滚出）",
                path.name, size / 1048576,
                _MAX_DAY_UNCOMPRESSED_BYTES / 1048576,
            )
            continue
        try:
            if path.suffix == ".gz":
                with gzip.open(path, "rt", encoding="utf-8") as f:
                    yield from f
            else:
                with path.open("r", encoding="utf-8") as f:
                    yield from f
        except OSError:
            logger.debug("[wall_history_profile] read failed: %s", path,
                         exc_info=True)


# ──────────────────────────────────────────────────────────────────────
# 刷新与查表
# ──────────────────────────────────────────────────────────────────────

def refresh_profile(
    coin: str,
    *,
    history_root: Optional[str] = None,
    window_days: int = DEFAULT_WINDOW_DAYS,
    now: Optional[int] = None,
) -> int:
    """重算一个币的画像缓存（同步、流式读档，请放 thread pool）。

    返回缓存的价位带数；归档缺失/为空时返回 0 并保留旧缓存
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

    # bucket_key → set[小时桶]；bucket_key → set[10min 事件桶]
    presence: dict[str, set[int]] = {}
    consumed: dict[str, set[int]] = {}
    removed: dict[str, set[int]] = {}
    lines_seen = 0
    skip_counter = [0]

    for day in days:
        for raw in _iter_day_lines(coin_dir, day, skip_counter):
            s = raw.strip()
            if not s:
                continue
            try:
                snap = json.loads(s)
            except (json.JSONDecodeError, ValueError):
                continue
            if not isinstance(snap, dict):
                continue
            ts = int(snap.get("ts", 0) or 0)
            if not (cutoff_ts <= ts <= now_ts):
                continue
            lines_seen += 1
            hour = ts // 3600
            for wall_key in ("walls_above", "walls_below"):
                for w in snap.get(wall_key, []) or []:
                    if not isinstance(w, dict):
                        continue
                    price = float(w.get("price_mid") or w.get("peak_price") or 0)
                    key = _bucket_key(str(w.get("side", "")), price)
                    if key:
                        presence.setdefault(key, set()).add(hour)
            for ev in snap.get("wall_events", []) or []:
                if not isinstance(ev, dict):
                    continue
                etype = str(ev.get("event_type", ""))
                if etype not in ("wall_consumed", "wall_removed",
                                 "wall_consumed_and_removed"):
                    continue
                ev_ts = int(ev.get("ts", 0) or 0)
                key = _bucket_key(str(ev.get("side", "")),
                                  float(ev.get("price_mid", 0) or 0))
                if not key or ev_ts <= 0:
                    continue
                bucket = ev_ts // _EVENT_DEDUP_BUCKET_SEC
                if etype in ("wall_consumed", "wall_consumed_and_removed"):
                    consumed.setdefault(key, set()).add(bucket)
                if etype in ("wall_removed", "wall_consumed_and_removed"):
                    removed.setdefault(key, set()).add(bucket)

    skipped_files = skip_counter[0]
    if lines_seen == 0:
        return 0

    window_hours = float(window_days * 24)
    table: dict[str, dict[str, Any]] = {}
    for key, hours in presence.items():
        c = len(consumed.get(key, ()))
        r = len(removed.get(key, ()))
        table[key] = {
            "presence_7d": round(min(len(hours) / window_hours, 1.0), 3),
            "consumed_ratio": round(c / (c + r), 3) if (c + r) > 0 else None,
        }

    with _LOCK:
        _PROFILE_CACHE[coin] = table
        _PROFILE_META[coin] = {
            "updated_ts": now_ts,
            "zones": len(table),
            "window_days": window_days,
            "skipped_files": skipped_files,
        }
    logger.info(
        "[wall_history_profile] refreshed | coin=%s buckets=%d frames=%d skipped_files=%d",
        coin, len(table), lines_seen, skipped_files,
    )
    return len(table)


def attach_history_profile(coin: str, zones: list[Any]) -> None:
    """把缓存画像按价格桶附加到本帧 zone（纯内存查表，热路径安全）。

    查 idx±1 三桶取 presence 最高者，吸收桶边界抖动。
    缓存未就绪（启动初期 / 无归档）时字段保持 None，不影响任何评分。
    """
    with _LOCK:
        table = _PROFILE_CACHE.get(coin.upper())
    if not table:
        return
    for z in zones:
        # 归档摘要只有 price_mid（无 peak_price），两侧统一用 price_mid 定桶
        price = float(getattr(z, "price_mid", 0) or getattr(z, "peak_price", 0) or 0)
        idx = _bucket_idx(price)
        if idx is None:
            continue
        side = "ask" if getattr(z, "side", "") == "ask" else "bid"
        best: Optional[dict[str, Any]] = None
        for i in (idx, idx - 1, idx + 1):
            prof = table.get(f"{side}|{i}")
            if prof is not None and (
                best is None or prof["presence_7d"] > best["presence_7d"]
            ):
                best = prof
        if best is None:
            continue
        z.history_presence_7d = best["presence_7d"]
        z.history_consumed_ratio = best["consumed_ratio"]


def get_profile_meta(coin: str) -> Optional[dict[str, Any]]:
    with _LOCK:
        meta = _PROFILE_META.get(coin.upper())
        return dict(meta) if meta else None


def reset_for_test() -> None:
    with _LOCK:
        _PROFILE_CACHE.clear()
        _PROFILE_META.clear()
