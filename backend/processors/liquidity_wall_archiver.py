"""流动性墙引擎历史落盘器（W1-T2）。

回应审计 P1-2：
    1-2 周观察期到期后必须能用真实数据校准 trust / break_through / active_attack
    权重；零落盘 = 凭感觉调参。本文件提供轻量摘要落盘（不含原始 bins）。

设计要点（与既有 processors/snapshot_archiver.py 范式对齐）：
    - 同步 IO + threading.RLock，主路径用 run_in_executor 推到 thread pool
    - 当日文件为纯 JSONL，便于 `jq / rg / pandas.read_json(lines=True)` 直读；
      历史日文件在 GC 时 gzip 压缩为 .jsonl.gz（读取方需兼容两种后缀）
    - 按币/按天分文件：data/liquidity_wall_history/{COIN}/{YYYYMMDD}.jsonl[.gz]
    - 写入节流（2026-08 P0 瘦身）：engine._recompute 每次重算都会调用 append
      （生产实测 ~6.5s/次 → 244MB/天/币）。改为按币最小写入间隔 60s +
      内容哈希去重（状态未变时最长 300s 心跳一帧），预期 <30MB/天/币，
      gzip 后 <5MB/天/币
    - 默认 30 天 GC（可用 LIQUIDITY_WALL_KEEP_DAYS 调整；压缩后 30 天成本低，
      且 W4 后验需要 2-4 周样本窗口）
    - 磁盘水位保护：> 80% 跳过落盘 + 日志告警，30s 抑制再检测
    - 写入失败全部 best-effort（吞异常），绝不影响主轮询

落盘 schema（向前兼容 W1-T4 / W2-T1 字段）：
    {
      "ts": 1714327200,
      "coin": "BTC",
      "last_price": 76234.5,
      "atr": 312.0,
      "data_quality": "ok",                 # warming/partial/stale/missing
      "walls_above": [<wall_summary>, ...], # 卖墙
      "walls_below": [<wall_summary>, ...], # 买墙
      "wall_events": [<event_summary>, ...],
      "crowding_global": {...},
      "top_sweep_targets": [...],
    }

每个 <wall_summary> 含（W1-T4/W2-T1 后会被填充更多字段）：
    wall_zone_id (W1-T4)、source、price_mid/low/high、current_usd、max_usd_1h、
    trust_score、raw_trust_score (W2-T1)、trust_components (W2-T1)、
    support_resistance_trust_score (W2-T1)、sweep_attractiveness_score (W2-T1)、
    break_through_risk、active_attack_score、coinbase_spot_confluence、
    coinbase_num_orders、persistence_min、source_age_sec
"""
from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
import shutil
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ── 常量 ────────────────────────────────────────────────────────────────

_DEFAULT_ROOT = os.path.abspath(
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "data",
        "liquidity_wall_history",
    )
)
_TZ_CN = timezone(timedelta(hours=8))

# 磁盘水位保护
def _env_int(name: str, default: int, min_value: int, max_value: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "[liquidity_wall_archiver] invalid env %s=%r, fallback=%s",
            name, raw, default,
        )
        return default
    return max(min_value, min(max_value, value))


def _env_float(name: str, default: float, min_value: float, max_value: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = float(raw)
    except ValueError:
        logger.warning(
            "[liquidity_wall_archiver] invalid env %s=%r, fallback=%s",
            name, raw, default,
        )
        return default
    return max(min_value, min(max_value, value))


_KEEP_DAYS = _env_int("LIQUIDITY_WALL_KEEP_DAYS", 30, 3, 90)
_DISK_HIGH_WATERMARK = _env_float("LIQUIDITY_WALL_DISK_HIGH_WATERMARK", 0.80, 0.50, 0.98)
_DISK_CHECK_INTERVAL_SEC = 30         # 高水位后 30s 内不再检测

# 写入节流：最小写入间隔（同币两次落盘的最短间隔）与去重心跳
# （内容哈希未变时最长隔多久仍强制写一帧，保证时间轴连续性供后验使用）
_MIN_WRITE_INTERVAL_SEC = _env_int("LIQUIDITY_WALL_MIN_WRITE_INTERVAL_SEC", 60, 0, 600)
_DEDUP_HEARTBEAT_SEC = _env_int("LIQUIDITY_WALL_DEDUP_HEARTBEAT_SEC", 300, 60, 3600)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Archiver
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class LiquidityWallArchiver:
    """流动性墙快照落盘器（process-singleton，线程安全）。

    职责：
      - 接收 OrderbookPressureSnapshot（pydantic model）+ source_age dict
      - 提炼摘要（轻量 schema，不含原始 bins）
      - 按 coin/day 写入 JSONL（同步 + RLock）
      - GC（每小时触发一次，默认 14 天，可由环境变量调整）
      - 磁盘高水位时跳过 + 日志告警

    使用：
      archiver = get_archiver()
      archiver.append(snapshot, source_age={...})  # 同步快路径，< 1ms
      # 或 async 路径：
      await loop.run_in_executor(None, archiver.append, snapshot, source_age)
    """

    def __init__(
        self,
        root: str = _DEFAULT_ROOT,
        keep_days: int = _KEEP_DAYS,
        min_write_interval_sec: int = _MIN_WRITE_INTERVAL_SEC,
        dedup_heartbeat_sec: int = _DEDUP_HEARTBEAT_SEC,
    ) -> None:
        self._lock = threading.RLock()
        self._root = root
        self._keep_days = keep_days
        self._min_write_interval_sec = max(0, int(min_write_interval_sec))
        self._dedup_heartbeat_sec = max(
            self._min_write_interval_sec, int(dedup_heartbeat_sec),
        )
        os.makedirs(self._root, exist_ok=True)
        self._last_gc_ts = 0
        # 磁盘水位状态
        self._disk_full_until_ts: float = 0.0      # 高水位抑制到何时
        self._dropped_count: int = 0               # 高水位 + queue 满计数（运维用）
        self._success_count: int = 0
        self._error_count: int = 0
        # 写入节流状态（按币）：上次落盘墙钟秒 / 上次内容哈希
        self._last_write_ts: dict[str, float] = {}
        self._last_content_hash: dict[str, str] = {}
        self._throttled_count: int = 0             # 因间隔/去重被跳过的帧数（运维用）

    # ── 写入 ───────────────────────────────────────────────────────────

    def append(
        self,
        snapshot: Any,
        source_age: Optional[dict[str, int]] = None,
    ) -> bool:
        """提炼并落盘一帧 OrderbookPressureSnapshot。

        参数：
            snapshot: OrderbookPressureSnapshot 实例（pydantic model）
            source_age: dict[endpoint, age_sec]，从 metrics 取，作为冗余审计
        返回：
            True = 落盘成功；False = 跳过（snapshot 无效 / 高水位 / 异常）
        """
        if snapshot is None:
            return False

        # 磁盘水位保护
        if self._is_disk_high():
            self._gc_if_due(force=True)
            self._dropped_count += 1
            return False

        try:
            row = self._build_row(snapshot, source_age or {})
            if row is None:
                return False
            coin = str(row["coin"])
            now = time.time()

            with self._lock:
                # ── 写入节流（P0 瘦身）─────────────────────────────
                # 1) 最小间隔：同币两次落盘间隔 < min_interval → 直接跳过
                # 2) 去重：间隔够了但内容哈希未变 → 仍跳过，
                #    直到超过 heartbeat 强制写一帧（保证时间轴连续）
                last_ts = self._last_write_ts.get(coin, 0.0)
                elapsed = now - last_ts
                if elapsed < self._min_write_interval_sec:
                    self._throttled_count += 1
                    return False
                content_hash = _content_hash(row)
                if (
                    content_hash == self._last_content_hash.get(coin)
                    and elapsed < self._dedup_heartbeat_sec
                ):
                    self._throttled_count += 1
                    return False

                day_key = _day_key(int(row["ts"]))
                path = self._path_for(coin, day_key)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
                self._last_write_ts[coin] = now
                self._last_content_hash[coin] = content_hash
                self._success_count += 1
            self._gc_if_due()
            return True
        except Exception as e:
            self._error_count += 1
            logger.debug("[liquidity_wall_archiver] append failed: %s", e, exc_info=True)
            return False

    # ── 读取（后验脚本用，不在主路径调用）───────────────────────────────

    def list_days(self, coin: Optional[str] = None) -> list[str]:
        """列出已有数据的日期（YYYYMMDD）。

        coin=None 返回所有币的并集；指定 coin 返回该币的日期列表。
        """
        days: set[str] = set()
        try:
            if coin:
                d = os.path.join(self._root, coin.upper())
                if os.path.isdir(d):
                    for f in os.listdir(d):
                        key = _day_key_from_filename(f)
                        if key:
                            days.add(key)
            else:
                if not os.path.isdir(self._root):
                    return []
                for c in os.listdir(self._root):
                    sub = os.path.join(self._root, c)
                    if not os.path.isdir(sub):
                        continue
                    for f in os.listdir(sub):
                        key = _day_key_from_filename(f)
                        if key:
                            days.add(key)
        except OSError:
            return []
        return sorted(days)

    def stats(self) -> dict:
        """运维用统计。"""
        with self._lock:
            return {
                "success_count": self._success_count,
                "error_count": self._error_count,
                "dropped_count": self._dropped_count,
                "throttled_count": self._throttled_count,
                "disk_high_until_ts": int(self._disk_full_until_ts),
                "keep_days": self._keep_days,
                "min_write_interval_sec": self._min_write_interval_sec,
                "dedup_heartbeat_sec": self._dedup_heartbeat_sec,
                "disk_high_watermark": _DISK_HIGH_WATERMARK,
                "root": self._root,
            }

    def reset_for_testing(self) -> None:
        """仅测试用：清空目录 + 计数。"""
        with self._lock:
            if os.path.isdir(self._root):
                for c in os.listdir(self._root):
                    sub = os.path.join(self._root, c)
                    if os.path.isdir(sub):
                        for f in os.listdir(sub):
                            try:
                                os.remove(os.path.join(sub, f))
                            except OSError:
                                pass
                        try:
                            os.rmdir(sub)
                        except OSError:
                            pass
            self._last_gc_ts = 0
            self._disk_full_until_ts = 0.0
            self._dropped_count = 0
            self._success_count = 0
            self._error_count = 0
            self._throttled_count = 0
            self._last_write_ts.clear()
            self._last_content_hash.clear()

    # ── 内部 ───────────────────────────────────────────────────────────

    def _path_for(self, coin: str, day: str) -> str:
        return os.path.join(self._root, coin.upper(), f"{day}.jsonl")

    def _is_disk_high(self) -> bool:
        """检查根目录所在分区水位；高位时返回 True 并打印告警（30s 抑制）。"""
        now = time.time()
        if now < self._disk_full_until_ts:
            return True
        try:
            usage = shutil.disk_usage(self._root)
            ratio = (usage.total - usage.free) / max(usage.total, 1)
            if ratio >= _DISK_HIGH_WATERMARK:
                self._disk_full_until_ts = now + _DISK_CHECK_INTERVAL_SEC
                logger.warning(
                    "[liquidity_wall_archiver] disk high | usage=%.1f%% root=%s | "
                    "skipping writes for %ds",
                    ratio * 100, self._root, _DISK_CHECK_INTERVAL_SEC,
                )
                return True
        except Exception:
            # 水位检查失败时不阻塞落盘（退化为不保护）
            return False
        return False

    def _gc_if_due(self, force: bool = False) -> None:
        """每小时检查一次：1) 压缩历史日 .jsonl → .jsonl.gz；2) 删除超期文件。"""
        now = int(time.time())
        if not force and now - self._last_gc_ts < 3600:
            return
        self._last_gc_ts = now
        try:
            today_key = datetime.now(_TZ_CN).strftime("%Y%m%d")
            cutoff_dt = datetime.now(_TZ_CN) - timedelta(days=self._keep_days)
            cutoff_key = cutoff_dt.strftime("%Y%m%d")
            for c in os.listdir(self._root):
                sub = os.path.join(self._root, c)
                if not os.path.isdir(sub):
                    continue
                for f in os.listdir(sub):
                    day_key = _day_key_from_filename(f)
                    if not day_key:
                        continue
                    path = os.path.join(sub, f)
                    if day_key < cutoff_key:
                        try:
                            os.remove(path)
                            logger.info(
                                "[liquidity_wall_archiver] rotated out | coin=%s day=%s",
                                c, day_key,
                            )
                        except OSError:
                            pass
                        continue
                    # 历史日的未压缩文件 → gzip（当日文件保持明文可 jq/rg 直读）
                    if f.endswith(".jsonl") and day_key < today_key:
                        self._compress_day_file(path)
        except Exception:
            logger.debug("[liquidity_wall_archiver] gc failed", exc_info=True)

    def _compress_day_file(self, path: str) -> None:
        """把历史日 .jsonl 压缩为 .jsonl.gz（追加 gzip member，兼容既有 gz）。

        gzip 格式允许多 member 串接，Python gzip 读取时自动拼接——因此若
        午夜边界竞态导致 gz 已存在后又出现同日 .jsonl，追加写入依然安全。
        持有 self._lock，避免与 append 并发写同一文件。
        """
        gz_path = path + ".gz"
        try:
            with self._lock:
                if not os.path.isfile(path):
                    return
                with open(path, "rb") as src:
                    data = src.read()
                with open(gz_path, "ab") as dst:
                    dst.write(gzip.compress(data))
                os.remove(path)
            logger.info(
                "[liquidity_wall_archiver] compressed | %s (%.1f MB → %.1f MB)",
                os.path.basename(path),
                len(data) / 1048576,
                os.path.getsize(gz_path) / 1048576,
            )
        except Exception:
            logger.debug(
                "[liquidity_wall_archiver] compress failed: %s", path, exc_info=True,
            )

    def _build_row(self, snapshot: Any, source_age: dict[str, int]) -> Optional[dict]:
        """提炼 OrderbookPressureSnapshot → 落盘 dict（轻量 schema）。"""
        coin = (getattr(snapshot, "coin", None) or "").upper()
        ts_sec = int(getattr(snapshot, "ts_sec", 0) or 0)
        if not coin or ts_sec <= 0:
            return None

        last_price = float(getattr(snapshot, "last_price", 0) or 0)
        atr = getattr(snapshot, "atr", None)
        data_quality = getattr(snapshot, "data_quality", "") or ""

        walls_above_raw = list(getattr(snapshot, "walls_above", None) or [])
        walls_below_raw = list(getattr(snapshot, "walls_below", None) or [])
        events_raw = list(getattr(snapshot, "wall_events", None) or [])
        crowding = getattr(snapshot, "crowding_global", None)
        sweep_targets_raw = list(getattr(snapshot, "top_sweep_targets", None) or [])

        return {
            "ts": ts_sec,
            "coin": coin,
            "last_price": round(last_price, 6) if last_price else 0.0,
            "atr": round(float(atr), 4) if atr else None,
            "data_quality": data_quality,
            "walls_above": [_summarize_wall(w) for w in walls_above_raw],
            "walls_below": [_summarize_wall(w) for w in walls_below_raw],
            "wall_events": [_summarize_event(e) for e in events_raw],
            "crowding_global": _summarize_crowding(crowding),
            "top_sweep_targets": [_summarize_sweep(s) for s in sweep_targets_raw],
            "source_age": dict(source_age) if source_age else {},
        }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 提炼工具
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _summarize_wall(z: Any) -> dict:
    """WallZone → 落盘摘要。

    向前兼容：W1-T4 / W2-T1 引入的字段（wall_zone_id / raw_trust_score /
    trust_components / SR/SA）通过 getattr 默认值兼容。落地后自动填充。
    """
    out = {
        "wall_zone_id": getattr(z, "wall_zone_id", "") or "",
        "side": getattr(z, "side", ""),
        "source": getattr(z, "source", ""),
        "dual_source": bool(getattr(z, "dual_source", False)),
        "price_mid": _round(getattr(z, "price_mid", 0), 4),
        "price_low": _round(getattr(z, "price_low", 0), 4),
        "price_high": _round(getattr(z, "price_high", 0), 4),
        "current_usd": _round(getattr(z, "current_usd", 0), 0),
        "max_usd_1h": _round(getattr(z, "max_usd_1h", 0), 0),
        "trust_score": _round(getattr(z, "trust_score", 0), 3),
        "raw_trust_score": _round(getattr(z, "raw_trust_score", 0), 3),
        "trust_components": dict(getattr(z, "trust_components", {}) or {}),
        "support_resistance_trust_score":
            _round(getattr(z, "support_resistance_trust_score", 0), 3),
        "sweep_attractiveness_score":
            _round(getattr(z, "sweep_attractiveness_score", 0), 3),
        "break_through_risk": _round(getattr(z, "break_through_risk", 0), 3),
        "active_attack_score": _round(getattr(z, "active_attack_score", 0), 3),
        "wall_removal_risk": _round(getattr(z, "wall_removal_risk", 0), 3),
        "wall_consumed_confidence": _round(getattr(z, "wall_consumed_confidence", 0), 3),
        "persistence_score": _round(getattr(z, "persistence_score", 0), 3),
        "persistence_min": _round(getattr(z, "visible_minutes", 0), 1),
        "exchange_count": int(getattr(z, "exchange_count", 0) or 0),
        "has_spot_confluence": bool(getattr(z, "has_spot_confluence", False)),
        "spot_current_usd": _round(getattr(z, "spot_current_usd", 0), 0),
        "coinbase_spot_confluence": bool(getattr(z, "coinbase_spot_confluence", False)),
        "coinbase_spot_usd": _round(getattr(z, "coinbase_spot_usd", 0), 0),
        "coinbase_num_orders": int(getattr(z, "coinbase_num_orders", 0) or 0),
        "trend": getattr(z, "trend", ""),
    }
    next_magnet = getattr(z, "next_magnet_price", None)
    if next_magnet is not None:
        out["next_magnet_price"] = _round(next_magnet, 4)
    sweep = getattr(z, "sweep_target", None)
    if sweep is not None:
        out["vacuum_gap_pct"] = _round(getattr(sweep, "vacuum_gap_pct", 0), 2)
    return out


def _summarize_event(e: Any) -> dict:
    return {
        "wall_zone_id": getattr(e, "wall_zone_id", "") or "",
        "event_type": getattr(e, "event_type", ""),
        "side": getattr(e, "side", ""),
        "price_mid": _round(getattr(e, "price_mid", 0), 4),
        "ts": int(getattr(e, "ts_sec", 0) or 0),
        "size_before_usd": _opt_round(getattr(e, "size_before_usd", None), 0),
        "size_after_usd": _opt_round(getattr(e, "size_after_usd", None), 0),
        "executed_usd_value": _opt_round(getattr(e, "executed_usd_value", None), 0),
        "confidence": _round(getattr(e, "confidence", 0), 3),
    }


def _summarize_crowding(cg: Any) -> Optional[dict]:
    if cg is None:
        return None
    return {
        "oi_delta_1h_pct": _opt_round(getattr(cg, "oi_delta_1h_pct", None), 4),
        "oi_delta_24h_pct": _opt_round(getattr(cg, "oi_delta_24h_pct", None), 4),
        "oi_margin_split": getattr(cg, "oi_margin_split", ""),
        "funding_now_pct": _opt_round(getattr(cg, "funding_now_pct", None), 6),
        "funding_avg_8h_pct": _opt_round(getattr(cg, "funding_avg_8h_pct", None), 6),
        "funding_percentile_30d": _opt_round(getattr(cg, "funding_percentile_30d", None), 3),
        "top_position_ls_ratio": _opt_round(getattr(cg, "top_position_ls_ratio", None), 3),
        "global_account_ls_ratio": _opt_round(getattr(cg, "global_account_ls_ratio", None), 3),
        "inferred_position_state": getattr(cg, "inferred_position_state", ""),
        "long_crowding_risk": _round(getattr(cg, "long_crowding_risk", 0), 3),
        "short_crowding_risk": _round(getattr(cg, "short_crowding_risk", 0), 3),
    }


def _summarize_sweep(s: Any) -> dict:
    return {
        "magnet_price": _round(getattr(s, "magnet_price", 0), 4),
        "direction": getattr(s, "direction", ""),
        "magnet_amount_usd": _round(getattr(s, "magnet_amount_usd", 0), 0),
        "vacuum_gap_pct": _round(getattr(s, "vacuum_gap_pct", 0), 2),
        "distance_pct": _round(getattr(s, "distance_pct", 0), 3),
    }


def _round(v: Any, ndigits: int) -> float:
    try:
        return round(float(v or 0), ndigits)
    except (TypeError, ValueError):
        return 0.0


def _opt_round(v: Any, ndigits: int) -> Optional[float]:
    if v is None:
        return None
    try:
        return round(float(v), ndigits)
    except (TypeError, ValueError):
        return None


def _day_key(ts: int) -> str:
    dt = datetime.fromtimestamp(int(ts or time.time()), tz=_TZ_CN)
    return dt.strftime("%Y%m%d")


def _day_key_from_filename(name: str) -> Optional[str]:
    """从归档文件名提取 YYYYMMDD；非归档文件返回 None。"""
    if name.endswith(".jsonl.gz"):
        key = name[: -len(".jsonl.gz")]
    elif name.endswith(".jsonl"):
        key = name[: -len(".jsonl")]
    else:
        return None
    return key if len(key) == 8 and key.isdigit() else None


# 内容去重时忽略的顶层字段：每帧必变（时间戳/源龄）或与墙状态无关的行情噪声。
_HASH_EXCLUDE_KEYS = frozenset({"ts", "source_age", "last_price", "atr"})


def _content_hash(row: dict) -> str:
    """对落盘行做稳定哈希（排除必变字段），用于「状态未变不重复写」判定。"""
    payload = {k: v for k, v in row.items() if k not in _HASH_EXCLUDE_KEYS}
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Process-level singleton
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_instance: Optional[LiquidityWallArchiver] = None
_instance_lock = threading.Lock()


def get_archiver() -> LiquidityWallArchiver:
    """获取全局 archiver 实例（首次调用时懒初始化）。"""
    global _instance
    if _instance is None:
        with _instance_lock:
            if _instance is None:
                _instance = LiquidityWallArchiver()
    return _instance


def configure_archiver(keep_days: Optional[int] = None) -> LiquidityWallArchiver:
    """启动时按配置调整全局实例（engine.start 调用一次）。

    优先级：环境变量 LIQUIDITY_WALL_KEEP_DAYS > yaml retention.liquidity_wall_days。
    env 未设置且传入 keep_days 时以后者生效（clamp 3-90，与 env 口径一致）。
    """
    archiver = get_archiver()
    if keep_days is not None and not os.getenv("LIQUIDITY_WALL_KEEP_DAYS"):
        with archiver._lock:
            archiver._keep_days = max(3, min(90, int(keep_days)))
    return archiver


def reset_archiver_for_test(
    root: Optional[str] = None, keep_days: int = _KEEP_DAYS
) -> LiquidityWallArchiver:
    """仅测试用：重置全局实例，可指定独立 root。"""
    global _instance
    with _instance_lock:
        _instance = LiquidityWallArchiver(
            root=root or _DEFAULT_ROOT, keep_days=keep_days,
        )
    return _instance
