"""Bottom Model SQLite WAL 存储：日级指标序列、采集账本与每日评分快照。

存储纪律（继承 2026-08 内存治理教训）：
- 全部数据有界：series 上限由上游历史深度决定（~20 指标 × ~6000 天 ≈ 数 MB），
  snapshots 按保留天数裁剪；无分钟级归档。
- 采集账本 fetch_log 按 (metric, day) 记录成功日，是 BGeometrics 15 次/天
  配额保护的核心：同一指标同一天成功后绝不重复外呼，重启也不例外。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from typing import Any, Optional


class BottomModelStore:
    def __init__(self, data_dir: str = "data/bottom_model"):
        os.makedirs(data_dir, exist_ok=True)
        self.path = os.path.join(data_dir, "bottom_model.sqlite3")
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._conn:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS series (
                    metric TEXT NOT NULL, day TEXT NOT NULL, value REAL NOT NULL,
                    PRIMARY KEY (metric, day)
                );
                CREATE INDEX IF NOT EXISTS idx_series_metric_day ON series(metric, day DESC);
                CREATE TABLE IF NOT EXISTS fetch_log (
                    metric TEXT PRIMARY KEY,
                    last_success_day TEXT NOT NULL DEFAULT '',
                    last_attempt_ts INTEGER NOT NULL DEFAULT 0,
                    last_ok INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT '',
                    last_rows INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS snapshots (
                    day TEXT PRIMARY KEY, ts INTEGER NOT NULL, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS schema_meta (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL
                );
                INSERT OR REPLACE INTO schema_meta(key,value) VALUES('schema_version','1');
                """
            )

    # ── 指标序列 ──

    def upsert_series(self, metric: str, rows: list[tuple[str, float]]) -> int:
        """幂等写入 [(day, value), ...]；返回写入行数。非法行静默跳过。"""
        clean: list[tuple[str, str, float]] = []
        for day, value in rows:
            if not isinstance(day, str) or len(day) != 10:
                continue
            try:
                clean.append((metric, day, float(value)))
            except (TypeError, ValueError):
                continue
        if not clean:
            return 0
        with self._lock, self._conn:
            self._conn.executemany(
                """INSERT INTO series(metric,day,value) VALUES(?,?,?)
                   ON CONFLICT(metric,day) DO UPDATE SET value=excluded.value""",
                clean,
            )
        return len(clean)

    def series(self, metric: str, limit: Optional[int] = None) -> list[tuple[str, float]]:
        """升序返回指标序列；limit 只截取最近 N 天。"""
        with self._lock:
            if limit is not None:
                rows = self._conn.execute(
                    "SELECT day,value FROM series WHERE metric=? ORDER BY day DESC LIMIT ?",
                    (metric, max(1, int(limit))),
                ).fetchall()
                rows = list(reversed(rows))
            else:
                rows = self._conn.execute(
                    "SELECT day,value FROM series WHERE metric=? ORDER BY day ASC",
                    (metric,),
                ).fetchall()
        return [(row["day"], float(row["value"])) for row in rows]

    def latest_day(self, metric: str) -> Optional[str]:
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(day) AS d FROM series WHERE metric=?", (metric,)
            ).fetchone()
        return row["d"] if row and row["d"] else None

    def coverage(self) -> dict[str, dict[str, Any]]:
        """每指标的覆盖概况（health / 证据包用）。"""
        with self._lock:
            rows = self._conn.execute(
                """SELECT metric, MIN(day) AS first_day, MAX(day) AS last_day,
                          COUNT(*) AS n FROM series GROUP BY metric"""
            ).fetchall()
        return {
            row["metric"]: {
                "first_day": row["first_day"],
                "last_day": row["last_day"],
                "count": int(row["n"]),
            }
            for row in rows
        }

    # ── 采集账本 ──

    def record_fetch(self, metric: str, ok: bool, error: str = "",
                     rows: int = 0, success_day: str = "") -> None:
        now = int(time.time())
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO fetch_log(metric,last_success_day,last_attempt_ts,last_ok,last_error,last_rows)
                   VALUES(?,?,?,?,?,?)
                   ON CONFLICT(metric) DO UPDATE SET
                     last_attempt_ts=excluded.last_attempt_ts,
                     last_ok=excluded.last_ok,
                     last_error=excluded.last_error,
                     last_rows=excluded.last_rows,
                     last_success_day=CASE
                       WHEN excluded.last_ok=1 AND excluded.last_success_day > fetch_log.last_success_day
                       THEN excluded.last_success_day ELSE fetch_log.last_success_day END""",
                (metric, success_day if ok else "", now, int(ok), error[:500], int(rows)),
            )

    def fetch_log(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM fetch_log").fetchall()
        return {
            row["metric"]: {
                "last_success_day": row["last_success_day"],
                "last_attempt_ts": int(row["last_attempt_ts"]),
                "last_ok": bool(row["last_ok"]),
                "last_error": row["last_error"],
                "last_rows": int(row["last_rows"]),
            }
            for row in rows
        }

    def last_success_day(self, metric: str) -> str:
        with self._lock:
            row = self._conn.execute(
                "SELECT last_success_day FROM fetch_log WHERE metric=?", (metric,)
            ).fetchone()
        return row["last_success_day"] if row else ""

    # ── 每日评分快照 ──

    def save_snapshot(self, day: str, payload: dict[str, Any]) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO snapshots(day,ts,payload) VALUES(?,?,?)
                   ON CONFLICT(day) DO UPDATE SET ts=excluded.ts,payload=excluded.payload""",
                (day, int(time.time()), json.dumps(payload, ensure_ascii=False)),
            )

    def latest_snapshot(self) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT payload FROM snapshots ORDER BY day DESC LIMIT 1"
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    def snapshot_history(self, limit: int = 400) -> list[dict[str, Any]]:
        """升序返回最近 N 天快照。"""
        limit = max(1, min(int(limit), 2000))
        with self._lock:
            rows = self._conn.execute(
                "SELECT payload FROM snapshots ORDER BY day DESC LIMIT ?", (limit,)
            ).fetchall()
        return [json.loads(row["payload"]) for row in reversed(rows)]

    def prune(self, snapshot_retention_days: int = 800) -> None:
        cutoff_ts = time.time() - max(90, snapshot_retention_days) * 86400
        cutoff_day = time.strftime("%Y-%m-%d", time.gmtime(cutoff_ts))
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM snapshots WHERE day<?", (cutoff_day,))

    def close(self) -> None:
        with self._lock:
            self._conn.close()
