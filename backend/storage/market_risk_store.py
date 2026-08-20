"""联合风险 PIT 事件平台：append-only 账本、物化快照、checkpoint 与独立 outbox。"""
from __future__ import annotations

import json
import hashlib
import os
import sqlite3
import threading
import time
from typing import Any, Optional

from models.market_risk import (
    CalibrationArtifact,
    MarketIncidentSnapshot,
    MarketRiskMachineContext,
    MarketRiskTransition,
)


class MarketRiskStore:
    def __init__(self, data_dir: str = "data/market_risk") -> None:
        os.makedirs(data_dir, exist_ok=True)
        self.path = os.path.join(data_dir, "market_risk.sqlite3")
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._conn:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS incidents (
                    incident_id TEXT PRIMARY KEY, coin TEXT NOT NULL,
                    direction TEXT NOT NULL, started_at INTEGER NOT NULL,
                    last_decision_time INTEGER NOT NULL, current_stage TEXT NOT NULL,
                    resolved_at INTEGER, payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_risk_incidents_coin_time
                    ON incidents(coin,last_decision_time DESC);
                CREATE TABLE IF NOT EXISTS episodes (
                    episode_id TEXT PRIMARY KEY, incident_id TEXT NOT NULL,
                    coin TEXT NOT NULL, direction TEXT NOT NULL,
                    started_at INTEGER NOT NULL, last_decision_time INTEGER NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_risk_episodes_incident
                    ON episodes(incident_id,started_at);
                CREATE TABLE IF NOT EXISTS transitions (
                    transition_id TEXT PRIMARY KEY, coin TEXT NOT NULL,
                    incident_id TEXT, episode_id TEXT, decision_time INTEGER NOT NULL,
                    from_stage TEXT NOT NULL, to_stage TEXT NOT NULL, payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_risk_transitions_time
                    ON transitions(coin,decision_time DESC);
                CREATE TABLE IF NOT EXISTS evidence_ledger (
                    evidence_id TEXT NOT NULL, decision_time INTEGER NOT NULL,
                    coin TEXT NOT NULL, incident_id TEXT, episode_id TEXT,
                    event_time INTEGER NOT NULL, observed_at INTEGER NOT NULL,
                    causal_root TEXT NOT NULL, pillar TEXT NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY(evidence_id,decision_time)
                );
                CREATE INDEX IF NOT EXISTS idx_risk_evidence_incident
                    ON evidence_ledger(incident_id,decision_time);
                CREATE INDEX IF NOT EXISTS idx_risk_evidence_time
                    ON evidence_ledger(coin,decision_time DESC);
                CREATE TABLE IF NOT EXISTS snapshots (
                    coin TEXT NOT NULL, decision_time INTEGER NOT NULL,
                    incident_id TEXT, episode_id TEXT, stage TEXT NOT NULL,
                    valid_for_calibration INTEGER, pit_violation_count INTEGER,
                    quality_layer TEXT,
                    payload TEXT NOT NULL,
                    PRIMARY KEY(coin,decision_time)
                );
                CREATE INDEX IF NOT EXISTS idx_risk_snapshots_time
                    ON snapshots(coin,decision_time DESC);
                CREATE TABLE IF NOT EXISTS current_snapshot (
                    coin TEXT PRIMARY KEY, decision_time INTEGER NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS machine_state (
                    coin TEXT PRIMARY KEY, decision_time INTEGER NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS engine_checkpoints (
                    source_id TEXT NOT NULL, coin TEXT NOT NULL,
                    updated_at INTEGER NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY(source_id,coin)
                );
                CREATE TABLE IF NOT EXISTS calibration_metadata (
                    calibration_version TEXT PRIMARY KEY, loaded_at INTEGER NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS gap_markers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, source_id TEXT NOT NULL,
                    coin TEXT NOT NULL, observed_at INTEGER NOT NULL,
                    reason TEXT NOT NULL, payload TEXT NOT NULL,
                    UNIQUE(source_id,coin,observed_at,reason)
                );
                CREATE TABLE IF NOT EXISTS governance_epochs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    scope TEXT NOT NULL, identity_hash TEXT NOT NULL,
                    started_at INTEGER NOT NULL, last_healthy_at INTEGER NOT NULL,
                    ended_at INTEGER, status TEXT NOT NULL DEFAULT 'open',
                    end_reason TEXT, payload TEXT NOT NULL DEFAULT '{}'
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_governance_epoch_open
                    ON governance_epochs(scope) WHERE status='open';
                CREATE INDEX IF NOT EXISTS idx_governance_epoch_history
                    ON governance_epochs(scope,started_at DESC);
                CREATE TABLE IF NOT EXISTS context_fact_versions (
                    source_id TEXT NOT NULL, fact_key TEXT NOT NULL,
                    version INTEGER NOT NULL, content_hash TEXT NOT NULL,
                    first_observed_at INTEGER NOT NULL, last_observed_at INTEGER NOT NULL,
                    effective_time INTEGER NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY(source_id,fact_key,version)
                );
                CREATE INDEX IF NOT EXISTS idx_context_fact_latest
                    ON context_fact_versions(source_id,fact_key,version DESC);
                CREATE TABLE IF NOT EXISTS market_risk_email_outbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    dedup_key TEXT NOT NULL UNIQUE, incident_id TEXT,
                    episode_id TEXT, stage TEXT NOT NULL,
                    subject TEXT NOT NULL, html TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_ts INTEGER NOT NULL, created_ts INTEGER NOT NULL,
                    sent_ts INTEGER, last_error TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_risk_outbox_due
                    ON market_risk_email_outbox(status,next_attempt_ts);
                CREATE TABLE IF NOT EXISTS schema_meta (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL
                );
                INSERT OR REPLACE INTO schema_meta(key,value)
                    VALUES('schema_version','3');
                """
            )
            # additive migration：同一事实在每个固定 tick 都会重新出现在快照中，
            # 账本只追加新事实或置信修订，不能把轮询频率当成独立证据。
            columns = {
                str(row[1])
                for row in self._conn.execute("PRAGMA table_info(evidence_ledger)").fetchall()
            }
            if "ledger_event_type" not in columns:
                self._conn.execute(
                    "ALTER TABLE evidence_ledger ADD COLUMN ledger_event_type TEXT NOT NULL DEFAULT 'evidence_added'"
                )
            if "content_hash" not in columns:
                self._conn.execute(
                    "ALTER TABLE evidence_ledger ADD COLUMN content_hash TEXT NOT NULL DEFAULT ''"
                )
            self._conn.execute(
                "UPDATE evidence_ledger SET content_hash=CAST(decision_time AS TEXT) WHERE content_hash=''"
            )
            self._conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_risk_evidence_identity ON evidence_ledger(evidence_id,content_hash)"
            )
            snapshot_columns = {
                str(row[1])
                for row in self._conn.execute("PRAGMA table_info(snapshots)").fetchall()
            }
            for name, kind in (
                ("valid_for_calibration", "INTEGER"),
                ("pit_violation_count", "INTEGER"),
                ("quality_layer", "TEXT"),
            ):
                if name not in snapshot_columns:
                    self._conn.execute(f"ALTER TABLE snapshots ADD COLUMN {name} {kind}")
            self._conn.execute(
                """CREATE INDEX IF NOT EXISTS idx_risk_snapshots_governance
                   ON snapshots(decision_time,valid_for_calibration,pit_violation_count)"""
            )

    def save_calibration(self, artifact: CalibrationArtifact) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO calibration_metadata(calibration_version,loaded_at,payload)
                   VALUES(?,?,?) ON CONFLICT(calibration_version) DO UPDATE SET
                   loaded_at=excluded.loaded_at,payload=excluded.payload""",
                (artifact.calibration_version, int(time.time()), artifact.model_dump_json()),
            )

    def load_machine_context(self, coin: str) -> Optional[MarketRiskMachineContext]:
        with self._lock:
            row = self._conn.execute(
                "SELECT payload FROM machine_state WHERE coin=?", (coin.upper(),),
            ).fetchone()
        return MarketRiskMachineContext.model_validate_json(row["payload"]) if row else None

    def save_checkpoint(self, source_id: str, coin: str, payload: dict[str, Any]) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO engine_checkpoints(source_id,coin,updated_at,payload)
                   VALUES(?,?,?,?) ON CONFLICT(source_id,coin) DO UPDATE SET
                   updated_at=excluded.updated_at,payload=excluded.payload""",
                (source_id, coin.upper(), int(time.time()), json.dumps(payload, separators=(",", ":"))),
            )

    def load_checkpoint(self, source_id: str, coin: str) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT payload FROM engine_checkpoints WHERE source_id=? AND coin=?",
                (source_id, coin.upper()),
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    def add_gap_marker(self, marker: dict[str, Any]) -> bool:
        source_id = str(marker.get("source_id") or marker.get("market") or "unknown")
        coin = str(marker.get("coin") or "UNKNOWN").upper()
        observed_at = int(marker.get("observed_at") or time.time())
        reason = str(marker.get("reason") or "unknown_gap")
        with self._lock, self._conn:
            cur = self._conn.execute(
                """INSERT OR IGNORE INTO gap_markers
                   (source_id,coin,observed_at,reason,payload) VALUES(?,?,?,?,?)""",
                (source_id, coin, observed_at, reason, json.dumps(marker, separators=(",", ":"))),
            )
            return cur.rowcount > 0

    def ensure_governance_epoch(
        self, scope: str, identity_hash: str, now: int,
        payload: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """返回当前质量纪元；身份变化必须先终止旧纪元再创建新纪元。"""
        scope = str(scope or "market_risk")
        identity_hash = str(identity_hash)
        timestamp = int(now)
        encoded = json.dumps(payload or {}, separators=(",", ":"), sort_keys=True)
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT * FROM governance_epochs WHERE scope=? AND status='open'",
                (scope,),
            ).fetchone()
            if row and str(row["identity_hash"]) != identity_hash:
                self._conn.execute(
                    """UPDATE governance_epochs SET status='closed',ended_at=?,
                       end_reason='identity_changed' WHERE id=?""",
                    (timestamp, int(row["id"])),
                )
                row = None
            if row is None:
                cur = self._conn.execute(
                    """INSERT INTO governance_epochs
                       (scope,identity_hash,started_at,last_healthy_at,status,payload)
                       VALUES(?,?,?,?, 'open',?)""",
                    (scope, identity_hash, timestamp, timestamp, encoded),
                )
                row = self._conn.execute(
                    "SELECT * FROM governance_epochs WHERE id=?", (int(cur.lastrowid),),
                ).fetchone()
            else:
                self._conn.execute(
                    "UPDATE governance_epochs SET last_healthy_at=? WHERE id=?",
                    (timestamp, int(row["id"])),
                )
                row = self._conn.execute(
                    "SELECT * FROM governance_epochs WHERE id=?", (int(row["id"]),),
                ).fetchone()
        result = dict(row)
        result["payload"] = json.loads(str(result.get("payload") or "{}"))
        return result

    def close_governance_epoch(
        self, scope: str, reason: str, observed_at: int,
        payload: Optional[dict[str, Any]] = None,
    ) -> bool:
        """持久化硬违规；重启不会恢复已关闭的连续时长。"""
        timestamp = int(observed_at)
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT id,payload FROM governance_epochs WHERE scope=? AND status='open'",
                (str(scope),),
            ).fetchone()
            if row is None:
                return False
            merged = json.loads(str(row["payload"] or "{}"))
            if payload:
                merged["violation"] = dict(payload)
            cur = self._conn.execute(
                """UPDATE governance_epochs SET status='closed',ended_at=?,end_reason=?,payload=?
                   WHERE id=? AND status='open'""",
                (
                    timestamp, str(reason),
                    json.dumps(merged, separators=(",", ":"), sort_keys=True),
                    int(row["id"]),
                ),
            )
            return cur.rowcount > 0

    def governance_status(self, scope: str, since: int = 0) -> dict[str, Any]:
        with self._lock:
            current = self._conn.execute(
                "SELECT * FROM governance_epochs WHERE scope=? AND status='open'",
                (str(scope),),
            ).fetchone()
            violations = self._conn.execute(
                """SELECT COUNT(*) AS n FROM governance_epochs
                   WHERE scope=? AND status='closed' AND ended_at>=?
                     AND COALESCE(end_reason,'')!='identity_changed'""",
                (str(scope), int(since)),
            ).fetchone()
            last_closed = self._conn.execute(
                """SELECT ended_at,end_reason FROM governance_epochs
                   WHERE scope=? AND status='closed' ORDER BY ended_at DESC LIMIT 1""",
                (str(scope),),
            ).fetchone()
        result: dict[str, Any] = {
            "open": current is not None,
            "started_at": int(current["started_at"] or 0) if current else 0,
            "identity_hash": str(current["identity_hash"] or "") if current else "",
            "hard_violations": int(violations["n"] or 0) if violations else 0,
            "last_reset_at": int(last_closed["ended_at"] or 0) if last_closed else 0,
            "last_reset_reason": str(last_closed["end_reason"] or "") if last_closed else "",
            "payload": json.loads(str(current["payload"] or "{}")) if current else {},
        }
        return result

    def record_context_observation(
        self, source_id: str, fact_key: str, effective_time: int,
        payload: dict[str, Any], observed_at: int,
    ) -> dict[str, Any]:
        """保存首次观测及修订版；同内容轮询只更新 last_observed_at。"""
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        content_hash = hashlib.sha256(encoded.encode()).hexdigest()[:24]
        with self._lock, self._conn:
            row = self._conn.execute(
                """SELECT version,content_hash,first_observed_at FROM context_fact_versions
                   WHERE source_id=? AND fact_key=? ORDER BY version DESC LIMIT 1""",
                (source_id, fact_key),
            ).fetchone()
            if row and row["content_hash"] == content_hash:
                self._conn.execute(
                    """UPDATE context_fact_versions SET last_observed_at=?
                       WHERE source_id=? AND fact_key=? AND version=?""",
                    (int(observed_at), source_id, fact_key, int(row["version"])),
                )
                return {
                    "version": int(row["version"]),
                    "first_observed_at": int(row["first_observed_at"]),
                    "revised": int(row["version"]) > 1,
                }
            version = int(row["version"] if row else 0) + 1
            first_observed_at = int(row["first_observed_at"] if row else observed_at)
            self._conn.execute(
                """INSERT INTO context_fact_versions
                   (source_id,fact_key,version,content_hash,first_observed_at,
                    last_observed_at,effective_time,payload)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (
                    source_id, fact_key, version, content_hash, first_observed_at,
                    int(observed_at), int(effective_time), encoded,
                ),
            )
            return {
                "version": version, "first_observed_at": first_observed_at,
                "revised": version > 1,
            }

    def commit_evaluation(
        self,
        snapshot: MarketIncidentSnapshot,
        context: MarketRiskMachineContext,
        transition: Optional[MarketRiskTransition | list[MarketRiskTransition]] = None,
        email: Optional[tuple[str, str, str]] = None,
    ) -> None:
        """快照、状态、证据、转换及可选邮件在同一事务提交。"""
        payload = snapshot.model_dump_json()
        now = int(time.time())
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT OR IGNORE INTO snapshots
                   (coin,decision_time,incident_id,episode_id,stage,
                    valid_for_calibration,pit_violation_count,quality_layer,payload)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (snapshot.coin, snapshot.decision_time, snapshot.incident_id,
                 snapshot.episode_id, snapshot.stage,
                 int(snapshot.valid_for_calibration), len(snapshot.pit_violations),
                 snapshot.quality_layer, payload),
            )
            self._conn.execute(
                """INSERT INTO current_snapshot(coin,decision_time,payload) VALUES(?,?,?)
                   ON CONFLICT(coin) DO UPDATE SET
                   decision_time=excluded.decision_time,payload=excluded.payload
                   WHERE excluded.decision_time>=current_snapshot.decision_time""",
                (snapshot.coin, snapshot.decision_time, payload),
            )
            self._conn.execute(
                """INSERT INTO machine_state(coin,decision_time,payload) VALUES(?,?,?)
                   ON CONFLICT(coin) DO UPDATE SET
                   decision_time=excluded.decision_time,payload=excluded.payload""",
                (snapshot.coin, snapshot.decision_time, context.model_dump_json()),
            )
            if snapshot.incident_id:
                self._conn.execute(
                    """INSERT INTO incidents
                       (incident_id,coin,direction,started_at,last_decision_time,current_stage,resolved_at,payload)
                       VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(incident_id) DO UPDATE SET
                       last_decision_time=excluded.last_decision_time,
                       current_stage=excluded.current_stage,
                       resolved_at=excluded.resolved_at,payload=excluded.payload""",
                    (snapshot.incident_id, snapshot.coin, snapshot.direction,
                     context.incident_started_at or snapshot.decision_time,
                     snapshot.decision_time, snapshot.stage,
                     context.resolved_at or None, payload),
                )
            if snapshot.episode_id and snapshot.incident_id:
                self._conn.execute(
                    """INSERT INTO episodes
                       (episode_id,incident_id,coin,direction,started_at,last_decision_time,payload)
                       VALUES(?,?,?,?,?,?,?) ON CONFLICT(episode_id) DO UPDATE SET
                       last_decision_time=excluded.last_decision_time,payload=excluded.payload""",
                    (snapshot.episode_id, snapshot.incident_id, snapshot.coin,
                     snapshot.direction, context.episode_started_at or snapshot.decision_time,
                     snapshot.decision_time, payload),
                )
            for item in snapshot.evidence:
                semantic = item.model_dump(exclude={"observed_at", "decision_time"})
                content_hash = hashlib.sha256(
                    json.dumps(semantic, sort_keys=True, separators=(",", ":"), default=str).encode()
                ).hexdigest()[:24]
                existed = self._conn.execute(
                    "SELECT 1 FROM evidence_ledger WHERE evidence_id=? LIMIT 1",
                    (item.evidence_id,),
                ).fetchone()
                self._conn.execute(
                    """INSERT OR IGNORE INTO evidence_ledger
                       (evidence_id,decision_time,coin,incident_id,episode_id,event_time,
                        observed_at,causal_root,pillar,payload,ledger_event_type,content_hash)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (item.evidence_id, item.decision_time, item.coin,
                     snapshot.incident_id, snapshot.episode_id, item.event_time,
                     item.observed_at, item.causal_root, item.pillar,
                     item.model_dump_json(),
                     "confidence_revised" if existed else "evidence_added",
                     content_hash),
                )
            transitions = (
                transition if isinstance(transition, list)
                else [transition] if transition is not None else []
            )
            for transition_item in transitions:
                self._conn.execute(
                    """INSERT OR IGNORE INTO transitions
                       (transition_id,coin,incident_id,episode_id,decision_time,
                        from_stage,to_stage,payload) VALUES(?,?,?,?,?,?,?,?)""",
                    (transition_item.transition_id, transition_item.coin,
                     transition_item.incident_id, transition_item.episode_id,
                     transition_item.decision_time, transition_item.from_stage,
                     transition_item.to_stage, transition_item.model_dump_json()),
                )
                if (
                    transition_item.to_stage == "resolved"
                    and transition_item.incident_id
                    and transition_item.incident_id != snapshot.incident_id
                ):
                    old = self._conn.execute(
                        "SELECT payload FROM incidents WHERE incident_id=?",
                        (transition_item.incident_id,),
                    ).fetchone()
                    if old:
                        old_payload = json.loads(old["payload"])
                        old_payload.update({
                            "stage": "resolved",
                            "decision_time": transition_item.decision_time,
                            "transition_reason": transition_item.reason,
                        })
                        self._conn.execute(
                            """UPDATE incidents SET last_decision_time=?,current_stage='resolved',
                               resolved_at=?,payload=? WHERE incident_id=?""",
                            (transition_item.decision_time, transition_item.decision_time,
                             json.dumps(old_payload, separators=(",", ":")),
                             transition_item.incident_id),
                        )
            if email is not None:
                dedup_key, subject, html = email
                self._conn.execute(
                    """INSERT OR IGNORE INTO market_risk_email_outbox
                       (dedup_key,incident_id,episode_id,stage,subject,html,status,
                        attempts,next_attempt_ts,created_ts)
                       VALUES(?,?,?,?,?,?,'pending',0,?,?)""",
                    (dedup_key, snapshot.incident_id, snapshot.episode_id,
                     snapshot.stage, subject, html, now, now),
                )

    def latest(self, coin: str) -> Optional[MarketIncidentSnapshot]:
        with self._lock:
            row = self._conn.execute(
                "SELECT payload FROM current_snapshot WHERE coin=?", (coin.upper(),),
            ).fetchone()
        if not row:
            return None
        payload = json.loads(row["payload"])
        try:
            return MarketIncidentSnapshot.model_validate(payload)
        except ValueError:
            # 旧 shadow 快照保留审计价值，但未来时间绝不能继续流入实时决策。
            decision_time = int(payload.get("decision_time") or 0)
            violations = list(payload.get("pit_violations") or [])
            for field in ("event_time", "observed_at", "watermark"):
                value = int(payload.get(field) or 0)
                if value > decision_time:
                    violations.append(f"legacy_snapshot:{field}_after_decision")
                    payload[f"reported_{field}"] = value
                    payload[field] = decision_time
            for item in payload.get("evidence", []) or []:
                item.setdefault("raw_strength", item.get("strength", 0.0))
                for field in ("event_time", "observed_at", "watermark", "decision_time"):
                    value = int(item.get(field) or 0)
                    if value > decision_time:
                        violations.append(f"{item.get('evidence_id', 'legacy')}:{field}_after_decision")
                        item.setdefault("values", {})[f"reported_{field}"] = value
                        item[field] = decision_time
                item["role"] = "informational"
            for source_id, quality in (payload.get("source_quality") or {}).items():
                for field in ("as_of", "observed_at", "watermark"):
                    value = int(quality.get(field) or 0)
                    if value > decision_time:
                        violations.append(f"{source_id}:{field}_after_decision")
                        quality[field] = decision_time
                if any(source_id in violation for violation in violations):
                    quality["decision_usable"] = False
                    quality["validity"] = "invalid"
                    quality.setdefault("reasons", []).append("legacy_pit_violation")
            payload.update({
                "quality_layer": "data_degraded",
                "stage_frozen": True,
                "valid_for_calibration": False,
                "notification_eligible": False,
                "pit_violations": list(dict.fromkeys(violations)),
            })
            return MarketIncidentSnapshot.model_validate(payload)

    def history(
        self, coin: str, from_ts: Optional[int] = None,
        to_ts: Optional[int] = None, limit: int = 2_000, *,
        order: str = "asc", cursor: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        clauses = ["coin=?"]
        params: list[Any] = [coin.upper()]
        if from_ts is not None:
            clauses.append("decision_time>=?")
            params.append(int(from_ts))
        if to_ts is not None:
            clauses.append("decision_time<=?")
            params.append(int(to_ts))
        direction = "DESC" if str(order).lower() == "desc" else "ASC"
        if cursor is not None:
            clauses.append("decision_time<?" if direction == "DESC" else "decision_time>?")
            params.append(int(cursor))
        params.append(max(1, min(int(limit), 10_000)))
        sql = (
            "SELECT payload FROM snapshots WHERE " + " AND ".join(clauses)
            + f" ORDER BY decision_time {direction} LIMIT ?"
        )
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def incident(self, incident_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            incident = self._conn.execute(
                "SELECT payload FROM incidents WHERE incident_id=?", (incident_id,),
            ).fetchone()
            if not incident:
                return None
            episodes = self._conn.execute(
                """SELECT payload FROM episodes WHERE incident_id=?
                   ORDER BY started_at ASC""", (incident_id,),
            ).fetchall()
            transitions = self._conn.execute(
                """SELECT payload FROM transitions WHERE incident_id=?
                   ORDER BY decision_time ASC""", (incident_id,),
            ).fetchall()
            evidence = self._conn.execute(
                """SELECT payload,ledger_event_type,decision_time FROM evidence_ledger WHERE incident_id=?
                   ORDER BY decision_time ASC""", (incident_id,),
            ).fetchall()
        return {
            "snapshot": json.loads(incident["payload"]),
            "episodes": [json.loads(row["payload"]) for row in episodes],
            "transitions": [json.loads(row["payload"]) for row in transitions],
            "evidence": [
                {
                    **json.loads(row["payload"]),
                    "ledger_event_type": row["ledger_event_type"],
                    "ledger_decision_time": row["decision_time"],
                }
                for row in evidence
            ],
        }

    def outbox_stats(self) -> dict[str, Any]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT status,COUNT(*) AS n FROM market_risk_email_outbox GROUP BY status"
            ).fetchall()
            pending = self._conn.execute(
                "SELECT MIN(created_ts) AS oldest FROM market_risk_email_outbox WHERE status='pending'"
            ).fetchone()
            sent = self._conn.execute(
                """SELECT MAX(sent_ts-created_ts) AS max_delay
                   FROM market_risk_email_outbox WHERE status='sent'"""
            ).fetchone()
        result: dict[str, Any] = {str(row["status"]): int(row["n"]) for row in rows}
        oldest = int(pending["oldest"] or 0) if pending else 0
        result["oldest_pending_age_sec"] = max(0, int(time.time()) - oldest) if oldest else 0
        result["max_sent_delay_sec"] = int(sent["max_delay"] or 0) if sent else 0
        return result

    def readiness_stats(self, since: int) -> dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                """SELECT COUNT(*) AS total,
                          COALESCE(SUM(CASE WHEN pit_violation_count>0 THEN 1 ELSE 0 END),0) AS pit,
                          COALESCE(SUM(CASE WHEN valid_for_calibration=1 THEN 1 ELSE 0 END),0) AS valid,
                          COALESCE(SUM(CASE WHEN quality_layer='normal' THEN 1 ELSE 0 END),0) AS core,
                          COALESCE(MIN(decision_time),0) AS first_governed_at
                   FROM snapshots
                   WHERE decision_time>=?
                     AND valid_for_calibration IS NOT NULL
                     AND pit_violation_count IS NOT NULL""",
                (int(since),),
            ).fetchone()
        total = int(row["total"] or 0) if row else 0
        return {
            "snapshot_count": total,
            "pit_violations": int(row["pit"] or 0) if row else 0,
            "valid_for_calibration": int(row["valid"] or 0) if row else 0,
            "core_coverage": int(row["core"] or 0) / total if row and total else 0.0,
            "first_governed_at": int(row["first_governed_at"] or 0) if row else 0,
        }

    def due_outbox(self, limit: int = 10) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT * FROM market_risk_email_outbox
                   WHERE status='pending' AND next_attempt_ts<=?
                   ORDER BY created_ts ASC LIMIT ?""",
                (int(time.time()), max(1, min(limit, 50))),
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_outbox_sent(self, item_id: int) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """UPDATE market_risk_email_outbox SET status='sent',sent_ts=?,last_error=NULL
                   WHERE id=?""", (int(time.time()), item_id),
            )

    def mark_outbox_failed(self, item_id: int, attempts: int, error: str) -> None:
        attempts = max(1, int(attempts))
        status = "dead" if attempts >= 10 else "pending"
        delay = min(3600, 30 * 2 ** min(attempts - 1, 7))
        with self._lock, self._conn:
            self._conn.execute(
                """UPDATE market_risk_email_outbox SET status=?,attempts=?,
                   next_attempt_ts=?,last_error=? WHERE id=?""",
                (status, attempts, int(time.time()) + delay, error[:500], item_id),
            )

    def defer_outbox(self, item_id: int, error: str, delay_sec: int = 600) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """UPDATE market_risk_email_outbox SET next_attempt_ts=?,last_error=?
                   WHERE id=? AND status='pending'""",
                (int(time.time()) + max(60, int(delay_sec)), error[:500], item_id),
            )

    def suppress_outbox(self, item_id: int, reason: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """UPDATE market_risk_email_outbox SET status='suppressed',last_error=?
                   WHERE id=? AND status='pending'""",
                (reason[:500], item_id),
            )

    def prune(self) -> None:
        now = int(time.time())
        feature_cutoff = now - 400 * 86_400
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM snapshots WHERE decision_time<?", (feature_cutoff,))
            self._conn.execute("DELETE FROM evidence_ledger WHERE decision_time<?", (feature_cutoff,))
            self._conn.execute("DELETE FROM transitions WHERE decision_time<?", (feature_cutoff,))
            self._conn.execute("DELETE FROM gap_markers WHERE observed_at<?", (now - 400 * 86_400,))
            self._conn.execute(
                """DELETE FROM market_risk_email_outbox
                   WHERE status='sent' AND sent_ts IS NOT NULL AND sent_ts<?""",
                (now - 180 * 86_400,),
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()
