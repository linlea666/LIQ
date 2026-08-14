"""Bottom Model SQLite WAL 存储：日级指标序列、采集账本与每日评分快照。

存储纪律（继承 2026-08 内存治理教训）：
- 全部数据有界：series 上限由上游历史深度决定（~20 指标 × ~6000 天 ≈ 数 MB），
  snapshots 按保留天数裁剪；无分钟级归档。
- 采集账本 fetch_log 按 (metric, day) 记录成功日，是 BGeometrics 15 次/天
  配额保护的核心：同一指标同一天成功后绝不重复外呼，重启也不例外。
"""

from __future__ import annotations

import json
import hashlib
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone
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
                CREATE TABLE IF NOT EXISTS replay (
                    day TEXT PRIMARY KEY,
                    algorithm_version TEXT NOT NULL,
                    stress REAL,
                    confirmation REAL,
                    quadrant TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS schema_meta (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS observations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    metric TEXT NOT NULL,
                    observation_day TEXT NOT NULL,
                    value REAL NOT NULL,
                    observation_ts INTEGER NOT NULL,
                    period_end_ts INTEGER NOT NULL,
                    available_at INTEGER NOT NULL,
                    ingested_at INTEGER NOT NULL,
                    is_final INTEGER NOT NULL,
                    source TEXT NOT NULL,
                    unit TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    payload_hash TEXT NOT NULL,
                    schema_hash TEXT NOT NULL,
                    quality_flag TEXT NOT NULL,
                    UNIQUE(metric, observation_day, source, revision)
                );
                CREATE INDEX IF NOT EXISTS idx_observations_metric_day
                    ON observations(metric, observation_day, revision DESC);
                CREATE INDEX IF NOT EXISTS idx_observations_available
                    ON observations(metric, available_at, is_final);
                CREATE TABLE IF NOT EXISTS quarantine (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    metric TEXT NOT NULL,
                    observation_day TEXT NOT NULL,
                    value REAL,
                    source TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    ingested_at INTEGER NOT NULL,
                    payload TEXT NOT NULL DEFAULT '{}'
                );
                CREATE TABLE IF NOT EXISTS replay_v2 (
                    algorithm_version TEXT NOT NULL,
                    data_policy_id TEXT NOT NULL,
                    dataset_id TEXT NOT NULL,
                    day TEXT NOT NULL,
                    stress REAL,
                    confirmation REAL,
                    quadrant TEXT NOT NULL DEFAULT '',
                    feature_payload TEXT NOT NULL DEFAULT '{}',
                    PRIMARY KEY (algorithm_version, data_policy_id, dataset_id, day)
                );
                CREATE TABLE IF NOT EXISTS model_runs (
                    run_id TEXT PRIMARY KEY,
                    model_id TEXT NOT NULL,
                    data_policy_id TEXT NOT NULL,
                    dataset_id TEXT NOT NULL,
                    decision_as_of TEXT NOT NULL,
                    quality_status TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audits (
                    audit_id TEXT PRIMARY KEY,
                    model_id TEXT NOT NULL,
                    data_policy_id TEXT NOT NULL,
                    dataset_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    markdown TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS bottom_email_outbox (
                    alert_id TEXT PRIMARY KEY,
                    model_id TEXT NOT NULL,
                    data_policy_id TEXT NOT NULL,
                    day TEXT NOT NULL,
                    state TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(
                        status IN ('PENDING','SENT','FAILED','SUPPRESSED','EXPIRED')
                    ),
                    subject TEXT NOT NULL,
                    html_body TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    last_attempt_at INTEGER NOT NULL DEFAULT 0,
                    sent_at INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT '',
                    UNIQUE(model_id,data_policy_id,day,state)
                );
                CREATE INDEX IF NOT EXISTS idx_bottom_email_due
                    ON bottom_email_outbox(status,next_attempt_at);
                CREATE INDEX IF NOT EXISTS idx_bottom_email_day
                    ON bottom_email_outbox(day);
                INSERT OR REPLACE INTO schema_meta(key,value) VALUES('schema_version','5');
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

    # ── append-only 点时观测 ──

    @staticmethod
    def _day_ts(day: str) -> int:
        return int(datetime.strptime(day, "%Y-%m-%d").replace(
            tzinfo=timezone.utc,
        ).timestamp())

    def append_observations(
        self,
        metric: str,
        rows: list[tuple[str, float]],
        *,
        source: str,
        cadence: str,
        unit: str,
        publication_lag_sec: int = 0,
        quality_flag: str = "PIT_APPROX",
        ingested_at: Optional[int] = None,
    ) -> int:
        """追加发生变化的观测版本；相同值重拉不制造重复 revision。"""
        ingested = int(ingested_at or time.time())
        period_sec = 7 * 86400 if cadence == "weekly" else 86400
        schema_hash = hashlib.sha256(
            f"{metric}|{source}|{cadence}|{unit}|v1".encode(),
        ).hexdigest()
        inserted = 0
        with self._lock, self._conn:
            for day, raw_value in rows:
                try:
                    value = float(raw_value)
                    observation_ts = self._day_ts(day)
                except (TypeError, ValueError):
                    continue
                if value != value or value in (float("inf"), float("-inf")):
                    continue
                previous = self._conn.execute(
                    """SELECT value,revision FROM observations
                       WHERE metric=? AND observation_day=? AND source=?
                       ORDER BY revision DESC LIMIT 1""",
                    (metric, day, source),
                ).fetchone()
                if previous is not None and float(previous["value"]) == value:
                    continue
                revision = int(previous["revision"]) + 1 if previous is not None else 1
                period_end = observation_ts + period_sec
                # 回填/修订不能冒充在历史周期结束时已经可得；在没有上游真实
                # vintage 的情况下，最早可得时间只能保守取本次实际摄取时间。
                available_at = max(
                    period_end + max(0, int(publication_lag_sec)), ingested,
                )
                payload_hash = hashlib.sha256(
                    f"{metric}|{day}|{value:.17g}|{source}".encode(),
                ).hexdigest()
                self._conn.execute(
                    """INSERT INTO observations(
                         metric,observation_day,value,observation_ts,period_end_ts,
                         available_at,ingested_at,is_final,source,unit,revision,
                         payload_hash,schema_hash,quality_flag
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        metric, day, value, observation_ts, period_end,
                        available_at, ingested, int(period_end <= ingested), source,
                        unit, revision, payload_hash, schema_hash, quality_flag,
                    ),
                )
                inserted += 1
        return inserted

    def observation_meta(self, metric: str, *,
                         as_of_day: Optional[str] = None) -> Optional[dict[str, Any]]:
        where = "metric=?"
        params: tuple[Any, ...] = (metric,)
        if as_of_day is not None:
            where += " AND observation_day<=?"
            params += (as_of_day,)
        with self._lock:
            row = self._conn.execute(
                f"""SELECT * FROM observations WHERE {where}
                    ORDER BY observation_day DESC,revision DESC LIMIT 1""",
                params,
            ).fetchone()
        return dict(row) if row is not None else None

    def quarantine_rows(
        self, metric: str, rows: list[tuple[str, float]], *, source: str,
        reason: str, payload: Optional[dict[str, Any]] = None,
    ) -> int:
        now = int(time.time())
        clean = [
            (metric, day, float(value), source, reason, now,
             json.dumps(payload or {}, ensure_ascii=False))
            for day, value in rows if isinstance(day, str)
        ]
        if not clean:
            return 0
        with self._lock, self._conn:
            self._conn.executemany(
                """INSERT INTO quarantine(
                     metric,observation_day,value,source,reason,ingested_at,payload
                   ) VALUES(?,?,?,?,?,?,?)""",
                clean,
            )
        return len(clean)

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
        self.save_snapshot_with_bottom_alert(day, payload)

    def save_snapshot_with_bottom_alert(
        self,
        day: str,
        payload: dict[str, Any],
        alert: Optional[dict[str, Any]] = None,
    ) -> bool:
        """同事务保存有效快照与可选邮件事件；返回事件是否首次入队。"""
        inserted = False
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO snapshots(day,ts,payload) VALUES(?,?,?)
                   ON CONFLICT(day) DO UPDATE SET ts=excluded.ts,payload=excluded.payload""",
                (day, int(time.time()), json.dumps(payload, ensure_ascii=False)),
            )
            if alert is not None:
                now = int(alert.get("created_at") or time.time())
                cur = self._conn.execute(
                    """INSERT OR IGNORE INTO bottom_email_outbox(
                         alert_id,model_id,data_policy_id,day,state,status,subject,
                         html_body,attempts,next_attempt_at,expires_at,created_at,
                         updated_at,last_attempt_at,sent_at,last_error
                       ) VALUES(?,?,?,?,?,?,?,?,0,?,?,?,?,0,0,'')""",
                    (
                        str(alert["alert_id"]), str(alert["model_id"]),
                        str(alert["data_policy_id"]), day,
                        str(alert.get("state") or "confirmed_recovery"),
                        str(alert.get("status") or "PENDING"),
                        str(alert["subject"]), str(alert["html_body"]),
                        int(alert.get("next_attempt_at") or now),
                        int(alert.get("expires_at") or now + 86400), now, now,
                    ),
                )
                inserted = cur.rowcount > 0
        return inserted

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

    def previous_ok_snapshot(
        self,
        *,
        model_id: str,
        data_policy_id: str,
        before_day: str,
    ) -> Optional[dict[str, Any]]:
        """返回同模型/政策下更早的最近 OK 快照；无历史时明确返回 None。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT payload FROM snapshots WHERE day<? ORDER BY day DESC",
                (before_day,),
            ).fetchall()
        for row in rows:
            try:
                payload = json.loads(row["payload"])
            except (TypeError, json.JSONDecodeError):
                continue
            if (
                payload.get("model_id") == model_id
                and payload.get("data_policy_id") == data_policy_id
                and payload.get("quality_status") == "OK"
            ):
                return payload
        return None

    # ── 底部确认邮件 outbox ──

    def claim_due_bottom_alerts(
        self,
        *,
        now: Optional[int] = None,
        limit: int = 10,
        lease_sec: int = 120,
    ) -> list[dict[str, Any]]:
        """领取到期项并延后 next_attempt，避免同进程并发重复发送。"""
        current = int(time.time() if now is None else now)
        self.expire_bottom_alerts(now=current)
        with self._lock, self._conn:
            rows = self._conn.execute(
                """SELECT * FROM bottom_email_outbox
                   WHERE status='PENDING' AND next_attempt_at<=? AND expires_at>?
                   ORDER BY created_at ASC LIMIT ?""",
                (current, current, max(1, min(int(limit), 50))),
            ).fetchall()
            ids = [str(row["alert_id"]) for row in rows]
            if ids:
                placeholders = ",".join("?" for _ in ids)
                self._conn.execute(
                    f"""UPDATE bottom_email_outbox SET next_attempt_at=?,updated_at=?
                        WHERE alert_id IN ({placeholders})""",
                    (current + max(30, int(lease_sec)), current, *ids),
                )
        return [dict(row) for row in rows]

    def mark_bottom_alert_sent(self, alert_id: str, *, now: Optional[int] = None) -> None:
        current = int(time.time() if now is None else now)
        with self._lock, self._conn:
            self._conn.execute(
                """UPDATE bottom_email_outbox
                   SET status='SENT',attempts=attempts+1,last_attempt_at=?,sent_at=?,
                       updated_at=?,last_error=''
                   WHERE alert_id=? AND status='PENDING'""",
                (current, current, current, alert_id),
            )

    def mark_bottom_alert_failed(
        self,
        alert_id: str,
        error: str,
        *,
        now: Optional[int] = None,
    ) -> None:
        current = int(time.time() if now is None else now)
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT attempts FROM bottom_email_outbox WHERE alert_id=? AND status='PENDING'",
                (alert_id,),
            ).fetchone()
            if row is None:
                return
            attempts = int(row["attempts"]) + 1
            if attempts >= 4:
                status, next_attempt = "FAILED", current
            else:
                retry_delays = {1: 15 * 60, 2: 60 * 60, 3: 3 * 60 * 60}
                status = "PENDING"
                next_attempt = current + retry_delays[attempts]
            self._conn.execute(
                """UPDATE bottom_email_outbox
                   SET status=?,attempts=?,next_attempt_at=?,last_attempt_at=?,
                       updated_at=?,last_error=? WHERE alert_id=?""",
                (status, attempts, next_attempt, current, current, error[:500], alert_id),
            )

    def defer_bottom_alert(
        self,
        alert_id: str,
        error: str,
        *,
        now: Optional[int] = None,
        delay_sec: int = 600,
    ) -> None:
        """配置暂缺时延后发送，但不消耗 SMTP 尝试次数。"""
        current = int(time.time() if now is None else now)
        with self._lock, self._conn:
            self._conn.execute(
                """UPDATE bottom_email_outbox SET next_attempt_at=?,updated_at=?,last_error=?
                   WHERE alert_id=? AND status='PENDING'""",
                (current + max(60, int(delay_sec)), current, error[:500], alert_id),
            )

    def suppress_bottom_alert(self, alert_id: str, *, now: Optional[int] = None) -> None:
        current = int(time.time() if now is None else now)
        with self._lock, self._conn:
            self._conn.execute(
                """UPDATE bottom_email_outbox
                   SET status='SUPPRESSED',updated_at=?,last_error=''
                   WHERE alert_id=? AND status='PENDING'""",
                (current, alert_id),
            )

    def expire_bottom_alerts(self, *, now: Optional[int] = None) -> int:
        current = int(time.time() if now is None else now)
        with self._lock, self._conn:
            cur = self._conn.execute(
                """UPDATE bottom_email_outbox SET status='EXPIRED',updated_at=?
                   WHERE status='PENDING' AND expires_at<=?""",
                (current, current),
            )
        return int(cur.rowcount)

    def bottom_alert_health(self) -> dict[str, Any]:
        with self._lock:
            counts = self._conn.execute(
                "SELECT status,COUNT(*) AS n FROM bottom_email_outbox GROUP BY status"
            ).fetchall()
            last_sent = self._conn.execute(
                "SELECT MAX(sent_at) AS ts FROM bottom_email_outbox WHERE status='SENT'"
            ).fetchone()
            latest_error = self._conn.execute(
                """SELECT last_error FROM bottom_email_outbox
                   WHERE status IN ('PENDING','FAILED') AND last_error!=''
                   ORDER BY updated_at DESC LIMIT 1"""
            ).fetchone()
        by_status = {str(row["status"]): int(row["n"]) for row in counts}
        return {
            "pending": by_status.get("PENDING", 0),
            "failed": by_status.get("FAILED", 0),
            "sent": by_status.get("SENT", 0),
            "suppressed": by_status.get("SUPPRESSED", 0),
            "expired": by_status.get("EXPIRED", 0),
            "last_sent_at": int(last_sent["ts"]) if last_sent and last_sent["ts"] else None,
            "last_error": str(latest_error["last_error"]) if latest_error else "",
        }

    # ── 历史回放（base rate 层）──

    def replay_rows(self, algorithm_version: str, *, data_policy_id: str = "",
                    dataset_id: str = "") -> list[dict[str, Any]]:
        """升序返回指定算法版本的回放点；版本不符的行不返回。

        day 是主键，每天只保留最近算过的那个版本——算法版本变化时旧行被整表
        覆盖，读取按版本过滤，于是版本切换自然触发一次全量重算。全表仅数百行、
        重算数秒，不值得为多版本共存引入复合主键。
        """
        if data_policy_id and dataset_id:
            with self._lock:
                rows = self._conn.execute(
                    """SELECT day,stress,confirmation,quadrant,feature_payload
                       FROM replay_v2 WHERE algorithm_version=? AND data_policy_id=?
                         AND dataset_id=? ORDER BY day ASC""",
                    (algorithm_version, data_policy_id, dataset_id),
                ).fetchall()
            return [
                {
                    "day": row["day"],
                    "stress": None if row["stress"] is None else float(row["stress"]),
                    "confirmation": None if row["confirmation"] is None else float(row["confirmation"]),
                    "quadrant": row["quadrant"],
                    "features": json.loads(row["feature_payload"] or "{}"),
                }
                for row in rows
            ]
        with self._lock:
            rows = self._conn.execute(
                """SELECT day,stress,confirmation,quadrant FROM replay
                   WHERE algorithm_version=? ORDER BY day ASC""",
                (algorithm_version,),
            ).fetchall()
        return [
            {
                "day": row["day"],
                "stress": None if row["stress"] is None else float(row["stress"]),
                "confirmation": (
                    None if row["confirmation"] is None else float(row["confirmation"])
                ),
                "quadrant": row["quadrant"],
            }
            for row in rows
        ]

    def upsert_replay(self, algorithm_version: str,
                      rows: list[dict[str, Any]], *, data_policy_id: str = "",
                      dataset_id: str = "") -> int:
        if not rows:
            return 0
        if data_policy_id and dataset_id:
            payload_v2 = [
                (
                    algorithm_version, data_policy_id, dataset_id, row["day"],
                    row.get("stress"), row.get("confirmation"),
                    row.get("quadrant") or "",
                    json.dumps(row.get("features") or {}, ensure_ascii=False),
                )
                for row in rows if isinstance(row.get("day"), str)
            ]
            with self._lock, self._conn:
                self._conn.executemany(
                    """INSERT INTO replay_v2(
                         algorithm_version,data_policy_id,dataset_id,day,stress,
                         confirmation,quadrant,feature_payload
                       ) VALUES(?,?,?,?,?,?,?,?)
                       ON CONFLICT(algorithm_version,data_policy_id,dataset_id,day)
                       DO UPDATE SET stress=excluded.stress,
                         confirmation=excluded.confirmation,
                         quadrant=excluded.quadrant,
                         feature_payload=excluded.feature_payload""",
                    payload_v2,
                )
            return len(payload_v2)
        payload = [
            (row["day"], algorithm_version, row.get("stress"),
             row.get("confirmation"), row.get("quadrant") or "")
            for row in rows if isinstance(row.get("day"), str)
        ]
        with self._lock, self._conn:
            self._conn.executemany(
                """INSERT INTO replay(day,algorithm_version,stress,confirmation,quadrant)
                   VALUES(?,?,?,?,?)
                   ON CONFLICT(day) DO UPDATE SET
                     algorithm_version=excluded.algorithm_version,
                     stress=excluded.stress,
                     confirmation=excluded.confirmation,
                     quadrant=excluded.quadrant""",
                payload,
            )
        return len(payload)

    def replay_versions(self) -> dict[str, int]:
        """各算法版本的回放点数（诊断与失效判定用）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT algorithm_version AS v, COUNT(*) AS n FROM replay GROUP BY v"
            ).fetchall()
        return {row["v"]: int(row["n"]) for row in rows}

    # ── 版本化模型运行与数学审计 ──

    def save_model_run(self, payload: dict[str, Any]) -> None:
        run_id = str(payload.get("run_id") or "")
        if not run_id:
            raise ValueError("model run requires run_id")
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO model_runs(
                     run_id,model_id,data_policy_id,dataset_id,decision_as_of,
                     quality_status,created_at,payload
                   ) VALUES(?,?,?,?,?,?,?,?)""",
                (
                    run_id, payload.get("model_id", ""),
                    payload.get("data_policy_id", ""), payload.get("dataset_id", ""),
                    payload.get("decision_as_of", ""),
                    payload.get("quality_status", "INVALID_DATA"), int(time.time()),
                    json.dumps(payload, ensure_ascii=False),
                ),
            )

    def save_audit(self, audit_id: str, payload: dict[str, Any], markdown: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT OR REPLACE INTO audits(
                     audit_id,model_id,data_policy_id,dataset_id,status,created_at,
                     payload,markdown
                   ) VALUES(?,?,?,?,?,?,?,?)""",
                (
                    audit_id, payload.get("model_id", ""),
                    payload.get("data_policy_id", ""), payload.get("dataset_id", ""),
                    payload.get("status", "INSUFFICIENT_EVIDENCE"), int(time.time()),
                    json.dumps(payload, ensure_ascii=False), markdown,
                ),
            )

    def get_audit(self, audit_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT payload,markdown FROM audits WHERE audit_id=?", (audit_id,),
            ).fetchone()
        if row is None:
            return None
        payload = json.loads(row["payload"])
        payload.setdefault("audit_id", audit_id)
        payload["markdown"] = row["markdown"]
        return payload

    def latest_audit(self) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT audit_id FROM audits ORDER BY created_at DESC,audit_id DESC LIMIT 1",
            ).fetchone()
        return self.get_audit(row["audit_id"]) if row is not None else None

    def latest_audit_matching(self, model_id: str, data_policy_id: str,
                              dataset_id: str) -> Optional[dict[str, Any]]:
        """只返回与快照三元组完全一致的最新审计，禁止跨数据集错误绑定。"""
        with self._lock:
            row = self._conn.execute(
                """SELECT audit_id FROM audits
                   WHERE model_id=? AND data_policy_id=? AND dataset_id=?
                   ORDER BY created_at DESC,audit_id DESC LIMIT 1""",
                (model_id, data_policy_id, dataset_id),
            ).fetchone()
        return self.get_audit(row["audit_id"]) if row is not None else None

    def prune(self, snapshot_retention_days: int = 800) -> None:
        cutoff_ts = time.time() - max(90, snapshot_retention_days) * 86400
        cutoff_day = time.strftime("%Y-%m-%d", time.gmtime(cutoff_ts))
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM snapshots WHERE day<?", (cutoff_day,))
            self._conn.execute("DELETE FROM bottom_email_outbox WHERE day<?", (cutoff_day,))

    def close(self) -> None:
        with self._lock:
            self._conn.close()
