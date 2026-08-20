"""有界高频事件 Parquet 存储。

单写线程 + 非阻塞有界队列；任何溢出或 writer 故障都产生 gap marker，调用方
据此 fail-closed。SQLite 只保留特征/事件，不承载逐 tick。
"""
from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)


class RawEventStore:
    def __init__(
        self,
        data_dir: str,
        queue_max: int = 20_000,
        batch_size: int = 2_000,
        *,
        allowed_coins: tuple[str, ...] = ("BTC",),
        segment_sec: int = 300,
        max_total_bytes: int = 50 * 1024**3,
        min_free_bytes: int = 10 * 1024**3,
        min_free_inodes: int = 200_000,
    ) -> None:
        self.data_dir = data_dir
        self.queue_max = max(1_000, int(queue_max))
        self.batch_size = max(100, int(batch_size))
        self.allowed_coins = {str(coin).upper() for coin in allowed_coins}
        self.segment_sec = max(60, int(segment_sec))
        self.max_total_bytes = max(1024**3, int(max_total_bytes))
        self.min_free_bytes = max(1024**3, int(min_free_bytes))
        self.min_free_inodes = max(10_000, int(min_free_inodes))
        self._queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=self.queue_max)
        self._storage_lock = threading.RLock()
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._written = 0
        self._files_written = 0
        self._bytes_written = 0
        self._dropped = 0
        self._last_flush_at = 0
        self._last_error = ""
        self._gaps: deque[dict[str, Any]] = deque(maxlen=500)
        self._started_at = int(time.time())
        self._resource_cache: tuple[int, dict[str, Any]] = (0, {})
        self._pyarrow_available = False
        try:
            import pyarrow  # noqa: F401
            import pyarrow.parquet  # noqa: F401
            self._pyarrow_available = True
        except ImportError:
            self._last_error = "pyarrow_unavailable"

    def start(self) -> None:
        if self._running:
            return
        os.makedirs(self.data_dir, exist_ok=True)
        if not self._pyarrow_available:
            self._record_gap("system", "ALL", "pyarrow_unavailable")
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._worker, name="market-risk-raw-writer", daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=15)
            self._thread = None

    def append(self, event: dict[str, Any]) -> bool:
        if not self._running:
            return False
        coin = str(event.get("coin") or "").upper()
        if coin not in self.allowed_coins:
            return False
        event_time = int(event.get("event_time") or 0)
        decision_time = int(
            event.get("decision_time") or event.get("observed_at") or time.time()
        )
        if event_time <= 0 or decision_time <= 0 or event_time > decision_time:
            self._dropped += 1
            self._record_gap(
                str(event.get("market") or "unknown"), coin or "UNKNOWN",
                "raw_event_pit_violation",
            )
            return False
        try:
            self._queue.put_nowait(dict(event))
            return True
        except queue.Full:
            self._dropped += 1
            self._record_gap(
                str(event.get("market") or "unknown"),
                str(event.get("coin") or "UNKNOWN"),
                "raw_event_queue_overflow",
            )
            return False

    def _record_gap(self, market: str, coin: str, reason: str) -> None:
        marker = {
            "source_id": f"raw_event_store:{market}",
            "market": market, "coin": coin.upper(), "reason": reason,
            "observed_at": int(time.time()), "queue_size": self._queue.qsize(),
        }
        if self._gaps and all(
            self._gaps[-1].get(key) == marker.get(key)
            for key in ("market", "coin", "reason")
        ) and marker["observed_at"] - int(self._gaps[-1]["observed_at"]) < 60:
            return
        self._gaps.append(marker)
        try:
            gap_dir = os.path.join(self.data_dir, "gap_markers")
            os.makedirs(gap_dir, exist_ok=True)
            day = datetime.fromtimestamp(marker["observed_at"], timezone.utc).strftime("%Y-%m-%d")
            with open(os.path.join(gap_dir, f"{day}.jsonl"), "a", encoding="utf-8") as handle:
                handle.write(json.dumps(marker, separators=(",", ":")) + "\n")
        except OSError:
            logger.exception("raw event gap marker persist failed")

    def recent_gap(self, coin: str, market: str, within_sec: int = 300) -> Optional[dict[str, Any]]:
        cutoff = int(time.time()) - max(1, int(within_sec))
        return next((
            marker for marker in reversed(self._gaps)
            if marker["observed_at"] >= cutoff
            and marker["coin"] in {coin.upper(), "ALL"}
            and marker["market"] in {market, "system"}
        ), None)

    def health(self) -> dict[str, Any]:
        resources = self._resource_stats()
        uptime = max(1, int(time.time()) - self._started_at)
        files_per_day = round(self._files_written * 86_400 / uptime, 1) if uptime >= 300 else 0.0
        bytes_per_day = round(self._bytes_written * 86_400 / uptime, 1) if uptime >= 300 else 0.0
        return {
            "enabled": True,
            "running": self._running,
            "format": "parquet-zstd" if self._pyarrow_available else "unavailable",
            "queue_size": self._queue.qsize(), "queue_max": self.queue_max,
            "written": self._written, "dropped": self._dropped,
            "last_flush_at": self._last_flush_at, "last_error": self._last_error,
            "last_gap_marker": self._gaps[-1] if self._gaps else None,
            "allowed_coins": sorted(self.allowed_coins),
            "segment_sec": self.segment_sec,
            "file_count": resources["file_count"],
            "total_bytes": resources["total_bytes"],
            "free_bytes": resources["free_bytes"],
            "free_inodes": resources["free_inodes"],
            "projected_files_per_day": files_per_day,
            "projected_bytes_per_day": bytes_per_day,
            "projected_90d_files": round(files_per_day * 90, 1),
            "projected_90d_bytes": round(bytes_per_day * 90, 1),
            "resource_admissible": self._resource_admissible(resources),
            "limits": {
                "max_total_bytes": self.max_total_bytes,
                "min_free_bytes": self.min_free_bytes,
                "min_free_inodes": self.min_free_inodes,
            },
            "retention": {"raw_hours": 48, "aggregate_days": 90},
        }

    def _worker(self) -> None:
        # 以 event_time 固定切成 5 分钟原子段。一个段只写一次 raw/1s/5s，
        # 因而文件数量由时间决定，不再由队列 timeout 或行情速率决定。
        segments: dict[int, list[dict[str, Any]]] = {}
        while self._running or not self._queue.empty() or segments:
            try:
                item = self._queue.get(timeout=0.1 if not self._running else 1.0)
                event_time = int(item.get("event_time") or 0)
                segment = event_time - event_time % self.segment_sec
                segments.setdefault(segment, []).append(item)
            except queue.Empty:
                pass
            if not self._running:
                while True:
                    try:
                        tail = self._queue.get_nowait()
                    except queue.Empty:
                        break
                    tail_time = int(tail.get("event_time") or 0)
                    tail_segment = tail_time - tail_time % self.segment_sec
                    segments.setdefault(tail_segment, []).append(tail)
            now = int(time.time())
            closed = [
                segment for segment in segments
                if not self._running or segment + self.segment_sec + 2 <= now
            ]
            for segment in sorted(closed):
                batch = segments.pop(segment)
                try:
                    resources = self._resource_stats(force=True)
                    if not self._resource_admissible(resources):
                        raise OSError("raw_event_resource_limit")
                    self._write_batch(batch)
                    self._written += len(batch)
                    self._last_flush_at = int(time.time())
                    if self._written % 100_000 < len(batch):
                        self.prune()
                except Exception as exc:  # noqa: BLE001
                    self._last_error = f"{type(exc).__name__}: {exc}"
                    logger.exception("raw event parquet writer failed")
                    markets = {
                        (str(row.get("market") or "unknown"), str(row.get("coin") or "UNKNOWN"))
                        for row in batch
                    }
                    for market, coin in markets:
                        self._record_gap(market, coin, "raw_event_writer_failure")

    def _partition_path(self, layer: str, row: dict[str, Any]) -> str:
        ts = int(row.get("event_time") or time.time())
        dt = datetime.fromtimestamp(ts, timezone.utc)
        return os.path.join(
            self.data_dir, layer,
            f"date={dt:%Y-%m-%d}", f"hour={dt:%H}",
            f"market={row.get('market', 'unknown')}",
            f"coin={row.get('coin', 'UNKNOWN')}",
        )

    def _write_rows(
        self, layer: str, rows: list[dict[str, Any]], *, segment_start: int,
    ) -> None:
        import pyarrow as pa
        import pyarrow.parquet as pq

        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            grouped.setdefault(self._partition_path(layer, row), []).append(row)
        for directory, part in grouped.items():
            with self._storage_lock:
                os.makedirs(directory, exist_ok=True)
                # 同一进程同一段只有一个文件；重启落在同一段时保留独立 restart part，
                # 读取端按 bucket 再聚合，避免覆盖已经成功提交的数据。
                suffix = f"{os.getpid()}-{self._started_at}"
                path = os.path.join(directory, f"segment-{segment_start}-{suffix}.parquet")
                temp_path = path + ".tmp"
                existed = os.path.exists(path)
                previous_size = os.path.getsize(path) if existed else 0
                write_rows = list(part)
                if existed:
                    existing = pq.ParquetFile(path).read().to_pylist()
                    if layer == "raw":
                        deduplicated: dict[tuple[Any, ...], dict[str, Any]] = {}
                        for item in [*existing, *part]:
                            sequence = item.get("source_sequence")
                            key = (
                                item.get("coin"), item.get("market"), sequence,
                            ) if sequence is not None else (
                                json.dumps(item, sort_keys=True, separators=(",", ":"), default=str),
                            )
                            deduplicated[key] = item
                        write_rows = sorted(
                            deduplicated.values(),
                            key=lambda item: (
                                int(item.get("event_time") or 0),
                                str(item.get("source_sequence") or ""),
                            ),
                        )
                    else:
                        merged: dict[int, dict[str, Any]] = {}
                        for item in [*existing, *part]:
                            ts = int(item.get("event_time") or 0)
                            bucket = merged.setdefault(ts, {
                                **item, "aggressor_buy_quote": 0.0,
                                "aggressor_sell_quote": 0.0, "trade_count": 0,
                                "first_sequence": None, "last_sequence": None,
                            })
                            bucket["aggressor_buy_quote"] += float(item.get("aggressor_buy_quote") or 0)
                            bucket["aggressor_sell_quote"] += float(item.get("aggressor_sell_quote") or 0)
                            bucket["trade_count"] += int(item.get("trade_count") or 0)
                            first = item.get("first_sequence")
                            last = item.get("last_sequence")
                            if first is not None:
                                bucket["first_sequence"] = first if bucket["first_sequence"] is None else min(bucket["first_sequence"], first)
                            if last is not None:
                                bucket["last_sequence"] = last if bucket["last_sequence"] is None else max(bucket["last_sequence"], last)
                        write_rows = [merged[key] for key in sorted(merged)]
                pq.write_table(pa.Table.from_pylist(write_rows), temp_path, compression="zstd")
                os.replace(temp_path, path)
                if not existed:
                    self._files_written += 1
                try:
                    self._bytes_written += max(0, os.path.getsize(path) - previous_size)
                except OSError:
                    pass

    def _write_batch(
        self, rows: list[dict[str, Any]], *, segment_start: Optional[int] = None,
    ) -> None:
        if not rows:
            return
        segment = int(segment_start if segment_start is not None else rows[0]["event_time"])
        segment -= segment % self.segment_sec
        rows = self._filter_new_raw_rows(rows, segment)
        if not rows:
            return
        self._write_rows("raw", rows, segment_start=segment)
        for seconds, layer in ((1, "aggregate_1s"), (5, "aggregate_5s")):
            buckets: dict[tuple[str, str, int], dict[str, Any]] = {}
            for row in rows:
                ts = int(row.get("event_time") or 0)
                bucket_ts = ts - ts % seconds
                key = (str(row.get("coin")), str(row.get("market")), bucket_ts)
                bucket = buckets.setdefault(key, {
                    "coin": key[0], "market": key[1], "event_time": bucket_ts,
                    "window_sec": seconds, "aggressor_buy_quote": 0.0,
                    "aggressor_sell_quote": 0.0, "trade_count": 0,
                    "first_sequence": None, "last_sequence": None,
                })
                side = str(row.get("aggressor_side") or "")
                quote = float(row.get("quote_notional") or 0)
                if side == "buy":
                    bucket["aggressor_buy_quote"] += quote
                elif side == "sell":
                    bucket["aggressor_sell_quote"] += quote
                bucket["trade_count"] += 1
                sequence = row.get("source_sequence")
                if sequence is not None:
                    bucket["first_sequence"] = sequence if bucket["first_sequence"] is None else min(bucket["first_sequence"], sequence)
                    bucket["last_sequence"] = sequence if bucket["last_sequence"] is None else max(bucket["last_sequence"], sequence)
            self._write_rows(layer, list(buckets.values()), segment_start=segment)

    def _filter_new_raw_rows(
        self, rows: list[dict[str, Any]], segment_start: int,
    ) -> list[dict[str, Any]]:
        """跨延迟批次和重启按交易序列去重，聚合层只消费真正的新事件。"""
        import pyarrow.parquet as pq

        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            grouped.setdefault(self._partition_path("raw", row), []).append(row)
        accepted: list[dict[str, Any]] = []
        for directory, candidates in grouped.items():
            seen: set[tuple[Any, ...]] = set()
            if os.path.isdir(directory):
                prefix = f"segment-{segment_start}-"
                for name in os.listdir(directory):
                    if not (name.startswith(prefix) and name.endswith(".parquet")):
                        continue
                    try:
                        existing = pq.ParquetFile(os.path.join(directory, name)).read().to_pylist()
                    except Exception:
                        self._record_gap(
                            str(candidates[0].get("market") or "unknown"),
                            str(candidates[0].get("coin") or "UNKNOWN"),
                            "raw_segment_dedup_read_failure",
                        )
                        raise
                    seen.update(self._raw_identity(item) for item in existing)
            for item in candidates:
                identity = self._raw_identity(item)
                if identity in seen:
                    continue
                seen.add(identity)
                accepted.append(item)
        return accepted

    @staticmethod
    def _raw_identity(item: dict[str, Any]) -> tuple[Any, ...]:
        sequence = item.get("source_sequence")
        if sequence is not None:
            return (
                str(item.get("coin") or "").upper(),
                str(item.get("market") or ""),
                int(item.get("event_time") or 0), sequence,
            )
        return (
            json.dumps(item, sort_keys=True, separators=(",", ":"), default=str),
        )

    def read_aggregates(
        self, layer: str, coin: str, market: str, from_ts: int, to_ts: int,
    ) -> list[dict[str, Any]]:
        """读取并按 bucket 再聚合；兼容重启时同一时间段产生多个原子 part。"""
        if layer not in {"aggregate_1s", "aggregate_5s"}:
            raise ValueError("layer must be aggregate_1s or aggregate_5s")
        import pyarrow.parquet as pq

        grouped: dict[int, dict[str, Any]] = {}
        root = os.path.join(self.data_dir, layer)
        if not os.path.isdir(root):
            return []
        for directory, _, names in os.walk(root):
            if f"market={market}" not in directory or f"coin={coin.upper()}" not in directory:
                continue
            for name in names:
                if not name.endswith(".parquet"):
                    continue
                for row in pq.ParquetFile(os.path.join(directory, name)).read().to_pylist():
                    ts = int(row.get("event_time") or 0)
                    if ts < int(from_ts) or ts > int(to_ts):
                        continue
                    bucket = grouped.setdefault(ts, {
                        **row,
                        "aggressor_buy_quote": 0.0,
                        "aggressor_sell_quote": 0.0,
                        "trade_count": 0,
                        "first_sequence": None,
                        "last_sequence": None,
                    })
                    bucket["aggressor_buy_quote"] += float(row.get("aggressor_buy_quote") or 0)
                    bucket["aggressor_sell_quote"] += float(row.get("aggressor_sell_quote") or 0)
                    bucket["trade_count"] += int(row.get("trade_count") or 0)
                    first = row.get("first_sequence")
                    last = row.get("last_sequence")
                    if first is not None:
                        bucket["first_sequence"] = first if bucket["first_sequence"] is None else min(bucket["first_sequence"], first)
                    if last is not None:
                        bucket["last_sequence"] = last if bucket["last_sequence"] is None else max(bucket["last_sequence"], last)
        return [grouped[key] for key in sorted(grouped)]

    def _resource_stats(self, *, force: bool = False) -> dict[str, Any]:
        now = int(time.time())
        cached_at, cached = self._resource_cache
        if cached and not force and now - cached_at < 60:
            return dict(cached)
        file_count = 0
        total_bytes = 0
        if os.path.isdir(self.data_dir):
            for directory, _, names in os.walk(self.data_dir):
                for name in names:
                    if not name.endswith(".parquet"):
                        continue
                    file_count += 1
                    try:
                        total_bytes += os.path.getsize(os.path.join(directory, name))
                    except OSError:
                        pass
        probe = self.data_dir if os.path.exists(self.data_dir) else os.path.dirname(self.data_dir)
        os.makedirs(probe, exist_ok=True)
        stat = os.statvfs(probe)
        result = {
            "file_count": file_count,
            "total_bytes": total_bytes,
            "free_bytes": int(stat.f_bavail * stat.f_frsize),
            "free_inodes": int(stat.f_favail),
        }
        self._resource_cache = (now, result)
        return dict(result)

    def _resource_admissible(self, stats: dict[str, Any]) -> bool:
        return bool(
            int(stats.get("total_bytes", 0)) < self.max_total_bytes
            and int(stats.get("free_bytes", 0)) >= self.min_free_bytes
            and int(stats.get("free_inodes", 0)) >= self.min_free_inodes
        )

    def prune(self) -> None:
        self.compact_closed_hours()
        now = time.time()
        cutoffs = {"raw": now - 48 * 3600, "aggregate_1s": now - 90 * 86_400, "aggregate_5s": now - 90 * 86_400}
        for layer, cutoff in cutoffs.items():
            root = os.path.join(self.data_dir, layer)
            if not os.path.isdir(root):
                continue
            for date_name in os.listdir(root):
                if not date_name.startswith("date="):
                    continue
                try:
                    day_ts = datetime.strptime(date_name[5:], "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()
                except ValueError:
                    continue
                # 整日过期才删，避免删除尚在保留窗内的小时。
                if day_ts + 86_400 >= cutoff:
                    continue
                import shutil
                shutil.rmtree(os.path.join(root, date_name), ignore_errors=True)

    def compact_closed_hours(self, now: Optional[int] = None) -> int:
        """把已闭合小时的 1s/5s 段压成单文件；raw 保持 5m 段便于审计恢复。"""
        if not self._pyarrow_available:
            return 0
        import pyarrow as pa
        import pyarrow.parquet as pq

        current = int(time.time() if now is None else now)
        compacted = 0
        for layer in ("aggregate_1s", "aggregate_5s"):
            root = os.path.join(self.data_dir, layer)
            if not os.path.isdir(root):
                continue
            for directory, _, names in os.walk(root):
                files = [
                    os.path.join(directory, name) for name in names
                    if name.endswith(".parquet")
                ]
                if len(files) <= 1:
                    continue
                sample = os.path.basename(files[0])
                try:
                    date_name = next(part[5:] for part in directory.split(os.sep) if part.startswith("date="))
                    hour_name = next(part[5:] for part in directory.split(os.sep) if part.startswith("hour="))
                    hour_start = int(datetime.strptime(
                        f"{date_name} {hour_name}", "%Y-%m-%d %H",
                    ).replace(tzinfo=timezone.utc).timestamp())
                except (StopIteration, ValueError):
                    continue
                if hour_start + 3600 + self.segment_sec > current:
                    continue
                buckets: dict[int, dict[str, Any]] = {}
                try:
                    with self._storage_lock:
                        for path in files:
                            for row in pq.ParquetFile(path).read().to_pylist():
                                ts = int(row.get("event_time") or 0)
                                bucket = buckets.setdefault(ts, {
                                    **row, "aggressor_buy_quote": 0.0,
                                    "aggressor_sell_quote": 0.0, "trade_count": 0,
                                    "first_sequence": None, "last_sequence": None,
                                })
                                bucket["aggressor_buy_quote"] += float(row.get("aggressor_buy_quote") or 0)
                                bucket["aggressor_sell_quote"] += float(row.get("aggressor_sell_quote") or 0)
                                bucket["trade_count"] += int(row.get("trade_count") or 0)
                                first = row.get("first_sequence")
                                last = row.get("last_sequence")
                                if first is not None:
                                    bucket["first_sequence"] = first if bucket["first_sequence"] is None else min(bucket["first_sequence"], first)
                                if last is not None:
                                    bucket["last_sequence"] = last if bucket["last_sequence"] is None else max(bucket["last_sequence"], last)
                        target = os.path.join(directory, f"hour-{hour_start}-compacted.parquet")
                        temporary = target + ".tmp"
                        pq.write_table(
                            pa.Table.from_pylist([buckets[key] for key in sorted(buckets)]),
                            temporary, compression="zstd",
                        )
                        os.replace(temporary, target)
                        for path in files:
                            if path != target:
                                os.remove(path)
                    compacted += 1
                except Exception:
                    self._last_error = f"hour_compaction_failed:{layer}:{sample}"
                    logger.exception("raw event hourly compaction failed | directory=%s", directory)
        if compacted:
            self._resource_cache = (0, {})
        return compacted


_instance: Optional[RawEventStore] = None


def set_raw_event_store(store: Optional[RawEventStore]) -> None:
    global _instance
    _instance = store


def get_raw_event_store() -> Optional[RawEventStore]:
    return _instance
