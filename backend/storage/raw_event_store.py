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
import uuid
from collections import deque
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)


class RawEventStore:
    def __init__(self, data_dir: str, queue_max: int = 20_000, batch_size: int = 2_000) -> None:
        self.data_dir = data_dir
        self.queue_max = max(1_000, int(queue_max))
        self.batch_size = max(100, int(batch_size))
        self._queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=self.queue_max)
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._written = 0
        self._dropped = 0
        self._last_flush_at = 0
        self._last_error = ""
        self._gaps: deque[dict[str, Any]] = deque(maxlen=500)
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
        return {
            "enabled": True,
            "running": self._running,
            "format": "parquet-zstd" if self._pyarrow_available else "unavailable",
            "queue_size": self._queue.qsize(), "queue_max": self.queue_max,
            "written": self._written, "dropped": self._dropped,
            "last_flush_at": self._last_flush_at, "last_error": self._last_error,
            "last_gap_marker": self._gaps[-1] if self._gaps else None,
            "retention": {"raw_hours": 48, "aggregate_days": 90, "feature_days": 400},
        }

    def _worker(self) -> None:
        pending: list[dict[str, Any]] = []
        # stop() 后仍须冲掉已出队、但尚未达到 batch_size 的尾批。
        while self._running or not self._queue.empty() or pending:
            timed_out = False
            try:
                item = self._queue.get(timeout=0.1 if not self._running else 1.0)
                pending.append(item)
            except queue.Empty:
                timed_out = True
            if len(pending) < self.batch_size and self._running and pending and not timed_out:
                continue
            if not pending:
                continue
            batch, pending = pending, []
            try:
                self._write_batch(batch)
                self._written += len(batch)
                self._last_flush_at = int(time.time())
                if self._written % 100_000 < len(batch):
                    self.prune()
            except Exception as exc:  # noqa: BLE001
                self._last_error = f"{type(exc).__name__}: {exc}"
                logger.exception("raw event parquet writer failed")
                markets = {(str(row.get("market") or "unknown"), str(row.get("coin") or "UNKNOWN")) for row in batch}
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

    def _write_rows(self, layer: str, rows: list[dict[str, Any]]) -> None:
        import pyarrow as pa
        import pyarrow.parquet as pq

        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            grouped.setdefault(self._partition_path(layer, row), []).append(row)
        for directory, part in grouped.items():
            os.makedirs(directory, exist_ok=True)
            path = os.path.join(directory, f"part-{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}.parquet")
            pq.write_table(pa.Table.from_pylist(part), path, compression="zstd")

    def _write_batch(self, rows: list[dict[str, Any]]) -> None:
        self._write_rows("raw", rows)
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
            self._write_rows(layer, list(buckets.values()))

    def prune(self) -> None:
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


_instance: Optional[RawEventStore] = None


def set_raw_event_store(store: Optional[RawEventStore]) -> None:
    global _instance
    _instance = store


def get_raw_event_store() -> Optional[RawEventStore]:
    return _instance
