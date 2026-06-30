"""BTC 趋势模块 SQLite WAL 存储、事件账本与持久化邮件 outbox。"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from typing import Any, Optional

from models.trend_monitor import TrendEvent, TrendMachineContext, TrendSnapshot


class TrendStore:
    def __init__(self, data_dir: str = "data/trend"):
        os.makedirs(data_dir, exist_ok=True)
        self.path = os.path.join(data_dir, "trend.sqlite3")
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._conn:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS snapshots (
                    coin TEXT NOT NULL, closed_5m_ts INTEGER NOT NULL,
                    algorithm_version TEXT NOT NULL, ts INTEGER NOT NULL,
                    state TEXT NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY (coin, closed_5m_ts, algorithm_version)
                );
                CREATE INDEX IF NOT EXISTS idx_snapshots_ts ON snapshots(ts DESC);
                CREATE TABLE IF NOT EXISTS wallet_state (
                    coin TEXT PRIMARY KEY, ts INTEGER NOT NULL, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS ai_reviews (
                    coin TEXT NOT NULL, closed_5m_ts INTEGER NOT NULL,
                    verdict TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '', ts INTEGER NOT NULL,
                    PRIMARY KEY (coin, closed_5m_ts)
                );
                CREATE TABLE IF NOT EXISTS quality_history (
                    coin TEXT NOT NULL, closed_5m_ts INTEGER NOT NULL,
                    valid INTEGER NOT NULL, reason TEXT NOT NULL, ts INTEGER NOT NULL,
                    PRIMARY KEY (coin, closed_5m_ts)
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL,
                    event_type TEXT NOT NULL, severity TEXT NOT NULL,
                    dedup_key TEXT NOT NULL UNIQUE, payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts DESC);
                CREATE TABLE IF NOT EXISTS outbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, dedup_key TEXT NOT NULL UNIQUE,
                    subject TEXT NOT NULL, html TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0, next_attempt_ts INTEGER NOT NULL,
                    created_ts INTEGER NOT NULL, sent_ts INTEGER, last_error TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_outbox_due ON outbox(status, next_attempt_ts);
                CREATE TABLE IF NOT EXISTS flow_observations (
                    market TEXT NOT NULL, window TEXT NOT NULL, closed_ts INTEGER NOT NULL,
                    net_usd REAL NOT NULL, total_usd REAL NOT NULL,
                    PRIMARY KEY (market, window, closed_ts)
                );
                CREATE INDEX IF NOT EXISTS idx_flow_obs ON flow_observations(market, window, closed_ts DESC);
                CREATE TABLE IF NOT EXISTS machine_state (
                    coin TEXT PRIMARY KEY, ts INTEGER NOT NULL, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS schema_meta (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL
                );
                INSERT OR REPLACE INTO schema_meta(key,value) VALUES('schema_version','3');
                CREATE TABLE IF NOT EXISTS source_availability (
                    source TEXT NOT NULL, closed_ts INTEGER NOT NULL, available INTEGER NOT NULL,
                    PRIMARY KEY(source,closed_ts)
                );
                """
            )
            review_columns = {
                row[1] for row in self._conn.execute("PRAGMA table_info(ai_reviews)").fetchall()
            }
            if "reason" not in review_columns:
                self._conn.execute(
                    "ALTER TABLE ai_reviews ADD COLUMN reason TEXT NOT NULL DEFAULT ''"
                )

    def save_snapshot(
        self, snapshot: TrendSnapshot,
        machine_context: Optional[TrendMachineContext] = None,
    ) -> None:
        with self._lock, self._conn:
            self._write_snapshot(snapshot, machine_context)

    def _write_snapshot(
        self, snapshot: TrendSnapshot,
        machine_context: Optional[TrendMachineContext],
    ) -> None:
        # 钱包 400 日 × 多交易所曲线只保留一份 latest，避免每个5m快照重复写入。
        wallet_payload = snapshot.wallet_flow.model_dump_json()
        compact = snapshot.model_copy(deep=True)
        compact.wallet_flow.chart = []
        compact.wallet_flow.exchange_charts = {}
        payload = compact.model_dump_json()
        self._conn.execute(
            """INSERT INTO wallet_state(coin,ts,payload) VALUES(?,?,?)
               ON CONFLICT(coin) DO UPDATE SET ts=excluded.ts,payload=excluded.payload""",
            (snapshot.coin, snapshot.ts, wallet_payload),
        )
        self._conn.execute(
            """INSERT INTO snapshots(coin, closed_5m_ts, algorithm_version, ts, state, payload)
               VALUES(?,?,?,?,?,?)
               ON CONFLICT(coin,closed_5m_ts,algorithm_version) DO UPDATE SET
                 ts=excluded.ts,state=excluded.state,payload=excluded.payload""",
            (snapshot.coin, snapshot.closed_5m_ts, snapshot.algorithm_version,
             snapshot.ts, snapshot.state, payload),
        )
        self._conn.execute(
            """INSERT OR REPLACE INTO ai_reviews(coin,closed_5m_ts,verdict,reason,ts)
               VALUES(?,?,?,?,?)""",
            (snapshot.coin, snapshot.closed_5m_ts, snapshot.ai_review,
             snapshot.ai_review_reason, snapshot.ts),
        )
        self._conn.execute(
            """INSERT OR REPLACE INTO quality_history(coin,closed_5m_ts,valid,reason,ts)
               VALUES(?,?,?,?,?)""",
            (snapshot.coin, snapshot.closed_5m_ts, int(snapshot.data_quality.valid),
             snapshot.data_quality.reason, snapshot.ts),
        )
        if machine_context is not None:
            self._conn.execute(
                """INSERT INTO machine_state(coin,ts,payload) VALUES('BTC',?,?)
                   ON CONFLICT(coin) DO UPDATE SET ts=excluded.ts,payload=excluded.payload""",
                (snapshot.ts, machine_context.model_dump_json()),
            )

    def commit_evaluation(
        self, snapshot: TrendSnapshot, machine_context: TrendMachineContext,
        event_emails: list[tuple[TrendEvent, Optional[str], Optional[str]]],
    ) -> list[TrendEvent]:
        """快照、状态机、事件与Outbox同事务提交。"""
        inserted: list[TrendEvent] = []
        now = int(time.time())
        with self._lock, self._conn:
            self._write_snapshot(snapshot, machine_context)
            for event, subject, html in event_emails:
                cur = self._conn.execute(
                    "INSERT OR IGNORE INTO events(ts,event_type,severity,dedup_key,payload) VALUES(?,?,?,?,?)",
                    (event.ts, event.event_type, event.severity, event.dedup_key,
                     event.model_dump_json(exclude={"id"})),
                )
                if cur.rowcount <= 0:
                    continue
                inserted.append(event)
                if subject is not None and html is not None:
                    self._conn.execute(
                        """INSERT OR IGNORE INTO outbox
                           (dedup_key,subject,html,status,attempts,next_attempt_ts,created_ts)
                           VALUES(?,?,?,'pending',0,?,?)""",
                        (event.dedup_key, subject, html, now, now),
                    )
        return inserted

    def save_machine_context(self, context: TrendMachineContext, ts: Optional[int] = None) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO machine_state(coin,ts,payload) VALUES('BTC',?,?)
                   ON CONFLICT(coin) DO UPDATE SET ts=excluded.ts,payload=excluded.payload""",
                (ts or int(time.time()), context.model_dump_json()),
            )

    def load_machine_context(self) -> Optional[TrendMachineContext]:
        with self._lock:
            row = self._conn.execute(
                "SELECT payload FROM machine_state WHERE coin='BTC'"
            ).fetchone()
        return TrendMachineContext.model_validate_json(row["payload"]) if row else None

    def latest_snapshot(self) -> Optional[TrendSnapshot]:
        with self._lock:
            row = self._conn.execute(
                "SELECT payload FROM snapshots ORDER BY ts DESC LIMIT 1"
            ).fetchone()
            wallet_row = self._conn.execute(
                "SELECT payload FROM wallet_state WHERE coin='BTC'"
            ).fetchone()
        if not row:
            return None
        snapshot = TrendSnapshot.model_validate_json(row["payload"])
        if wallet_row:
            from models.trend_monitor import WalletFlowSnapshot
            snapshot.wallet_flow = WalletFlowSnapshot.model_validate_json(wallet_row["payload"])
        return snapshot

    def history(self, limit: int = 200) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 2000))
        with self._lock:
            rows = self._conn.execute(
                "SELECT payload FROM snapshots ORDER BY ts DESC LIMIT ?", (limit,)
            ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def add_event(self, event: TrendEvent) -> bool:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO events(ts,event_type,severity,dedup_key,payload) VALUES(?,?,?,?,?)",
                (event.ts, event.event_type, event.severity, event.dedup_key,
                 event.model_dump_json(exclude={"id"})),
            )
            return cur.rowcount > 0

    def persist_event_and_email(
        self, event: TrendEvent, subject: Optional[str] = None, html: Optional[str] = None,
    ) -> bool:
        """事件与可选邮件同事务提交，避免事件已去重但邮件永久丢失。"""
        now = int(time.time())
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO events(ts,event_type,severity,dedup_key,payload) VALUES(?,?,?,?,?)",
                (event.ts, event.event_type, event.severity, event.dedup_key,
                 event.model_dump_json(exclude={"id"})),
            )
            if cur.rowcount <= 0:
                return False
            if subject is not None and html is not None:
                self._conn.execute(
                    """INSERT OR IGNORE INTO outbox
                       (dedup_key,subject,html,status,attempts,next_attempt_ts,created_ts)
                       VALUES(?,?,?,'pending',0,?,?)""",
                    (event.dedup_key, subject, html, now, now),
                )
            return True

    def events(self, limit: int = 100) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 1000))
        with self._lock:
            rows = self._conn.execute(
                "SELECT id,payload FROM events ORDER BY ts DESC LIMIT ?", (limit,)
            ).fetchall()
        result = []
        for row in rows:
            item = json.loads(row["payload"])
            item["id"] = row["id"]
            result.append(item)
        return result

    def enqueue_email(self, dedup_key: str, subject: str, html: str) -> bool:
        now = int(time.time())
        with self._lock, self._conn:
            cur = self._conn.execute(
                """INSERT OR IGNORE INTO outbox
                   (dedup_key,subject,html,status,attempts,next_attempt_ts,created_ts)
                   VALUES(?,?,?,'pending',0,?,?)""",
                (dedup_key, subject, html, now, now),
            )
            return cur.rowcount > 0

    def due_outbox(self, limit: int = 10) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT * FROM outbox WHERE status='pending' AND next_attempt_ts<=?
                   ORDER BY created_ts ASC LIMIT ?""",
                (int(time.time()), max(1, min(limit, 50))),
            ).fetchall()
        return [dict(row) for row in rows]

    def claim_due_outbox(self, limit: int = 10, lease_sec: int = 120) -> list[dict[str, Any]]:
        """原子领取待发邮件；进程崩溃后租约到期可恢复。"""
        now = int(time.time())
        with self._lock, self._conn:
            self._conn.execute(
                """UPDATE outbox SET status='pending'
                   WHERE status='sending' AND next_attempt_ts<=?""", (now,),
            )
            rows = self._conn.execute(
                """SELECT * FROM outbox WHERE status='pending' AND next_attempt_ts<=?
                   ORDER BY created_ts ASC LIMIT ?""",
                (now, max(1, min(limit, 50))),
            ).fetchall()
            ids = [int(row["id"]) for row in rows]
            if ids:
                placeholders = ",".join("?" for _ in ids)
                self._conn.execute(
                    f"UPDATE outbox SET status='sending',next_attempt_ts=? WHERE id IN ({placeholders})",
                    (now + max(30, lease_sec), *ids),
                )
        claimed = [dict(row) for row in rows]
        for item in claimed:
            item["status"] = "sending"
        return claimed

    def mark_outbox_sent(self, item_id: int) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE outbox SET status='sent',sent_ts=?,last_error=NULL WHERE id=?",
                (int(time.time()), item_id),
            )

    def mark_outbox_failed(self, item_id: int, attempts: int, error: str) -> None:
        attempts = max(1, attempts)
        delay = min(3600, 30 * (2 ** min(attempts - 1, 7)))
        status = "dead" if attempts >= 10 else "pending"
        with self._lock, self._conn:
            self._conn.execute(
                """UPDATE outbox SET status=?,attempts=?,next_attempt_ts=?,last_error=? WHERE id=?""",
                (status, attempts, int(time.time()) + delay, error[:500], item_id),
            )

    def record_flow(self, market: str, window: str, closed_ts: int,
                    net_usd: float, total_usd: float) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT OR REPLACE INTO flow_observations
                   (market,window,closed_ts,net_usd,total_usd) VALUES(?,?,?,?,?)""",
                (market, window, closed_ts, net_usd, total_usd),
            )

    def record_flows(self, market: str, window: str, rows: list[tuple[int, float, float]]) -> None:
        if not rows:
            return
        with self._lock, self._conn:
            self._conn.executemany(
                """INSERT OR REPLACE INTO flow_observations
                   (market,window,closed_ts,net_usd,total_usd) VALUES(?,?,?,?,?)""",
                [(market, window, ts, net, total) for ts, net, total in rows],
            )

    def flow_count(self, market: str, window: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM flow_observations WHERE market=? AND window=?",
                (market, window),
            ).fetchone()
        return int(row["n"] if row else 0)

    def flow_abs_percentile(self, market: str, window: str, value: float,
                            lookback_days: int, min_samples: int = 30) -> Optional[float]:
        cutoff = int(time.time()) - lookback_days * 86400
        with self._lock:
            rows = self._conn.execute(
                """SELECT ABS(net_usd) AS value FROM flow_observations
                   WHERE market=? AND window=? AND closed_ts>=?""",
                (market, window, cutoff),
            ).fetchall()
        values = sorted(float(row["value"]) for row in rows)
        if len(values) < min_samples:
            return None
        return 100.0 * sum(v <= abs(value) for v in values) / len(values)

    def prune(self, snapshot_retention_days: int = 400) -> None:
        now = int(time.time())
        snapshot_cutoff = now - max(30, snapshot_retention_days) * 86400
        flow_cutoff = now - 400 * 86400
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM snapshots WHERE ts<?", (snapshot_cutoff,))
            self._conn.execute("DELETE FROM ai_reviews WHERE ts<?", (snapshot_cutoff,))
            self._conn.execute("DELETE FROM quality_history WHERE ts<?", (snapshot_cutoff,))
            self._conn.execute("DELETE FROM flow_observations WHERE closed_ts<?", (flow_cutoff,))
            self._conn.execute("DELETE FROM source_availability WHERE closed_ts<?", (flow_cutoff,))
            self._conn.execute(
                "DELETE FROM outbox WHERE status='sent' AND sent_ts IS NOT NULL AND sent_ts<?",
                (now - 180 * 86400,),
            )

    def record_source_availability(self, source: str, closed_ts: int, available: bool) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO source_availability(source,closed_ts,available) VALUES(?,?,?)",
                (source, closed_ts, int(available)),
            )

    def source_availability_pct(self, source: str, days: int = 14) -> Optional[float]:
        cutoff = int(time.time()) - max(1, days) * 86400
        with self._lock:
            row = self._conn.execute(
                """SELECT COUNT(*) AS n, SUM(available) AS ok FROM source_availability
                   WHERE source=? AND closed_ts>=?""", (source, cutoff),
            ).fetchone()
        if not row or int(row["n"] or 0) == 0:
            return None
        return round(100.0 * int(row["ok"] or 0) / int(row["n"]), 2)

    def audit_stats(self, days: int = 7) -> dict[str, Any]:
        cutoff = int(time.time()) - max(1, days) * 86400
        with self._lock:
            quality = self._conn.execute(
                """SELECT COUNT(*) AS n, SUM(valid) AS ok FROM quality_history WHERE ts>=?""",
                (cutoff,),
            ).fetchone()
            verdicts = self._conn.execute(
                """SELECT verdict,COUNT(*) AS n FROM ai_reviews WHERE ts>=? GROUP BY verdict""",
                (cutoff,),
            ).fetchall()
            outbox = self._conn.execute(
                "SELECT status,COUNT(*) AS n FROM outbox GROUP BY status"
            ).fetchall()
        total = int(quality["n"] or 0) if quality else 0
        valid = int(quality["ok"] or 0) if quality else 0
        return {
            "window_days": days,
            "quality_samples": total,
            "quality_valid_pct": round(100.0 * valid / total, 2) if total else None,
            "ai_verdicts": {row["verdict"]: int(row["n"]) for row in verdicts},
            "outbox": {row["status"]: int(row["n"]) for row in outbox},
        }

    def close(self) -> None:
        with self._lock:
            self._conn.close()
