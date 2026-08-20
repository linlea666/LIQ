"""PIT-safe BTC 原生链实体标签与事件账本（采集器可后续接入）。"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from typing import Optional

from models.market_risk import OnchainEntityEvent


class OnchainEntityStore:
    def __init__(self, data_dir: str) -> None:
        os.makedirs(data_dir, exist_ok=True)
        self.path = os.path.join(data_dir, "onchain_entities.sqlite3")
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._conn:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS entity_labels (
                    label_version_id TEXT PRIMARY KEY, entity_id TEXT NOT NULL,
                    entity_label TEXT NOT NULL, address TEXT NOT NULL,
                    label_source TEXT NOT NULL, confidence REAL NOT NULL,
                    valid_from INTEGER NOT NULL, valid_to INTEGER,
                    known_at INTEGER NOT NULL, payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_entity_address_pit
                    ON entity_labels(address,known_at,valid_from,valid_to);
                CREATE TABLE IF NOT EXISTS onchain_events (
                    event_id TEXT PRIMARY KEY, tx_id TEXT NOT NULL, output_index INTEGER NOT NULL,
                    decision_time INTEGER NOT NULL, reorged INTEGER NOT NULL DEFAULT 0,
                    payload TEXT NOT NULL, UNIQUE(tx_id,output_index)
                );
                CREATE TABLE IF NOT EXISTS onchain_event_revisions (
                    revision_id TEXT PRIMARY KEY, event_id TEXT NOT NULL,
                    decision_time INTEGER NOT NULL, revision_type TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                """
            )

    def register_label(
        self, *, entity_id: str, entity_label: str, address: str,
        label_source: str, confidence: float, valid_from: int, known_at: int,
        valid_to: Optional[int] = None,
    ) -> str:
        if not address or not label_source or not entity_id:
            raise ValueError("entity_id, address and label_source are required")
        if known_at <= 0 or valid_from <= 0:
            raise ValueError("known_at and valid_from must be positive")
        if valid_to is not None and valid_to < valid_from:
            raise ValueError("valid_to cannot be before valid_from")
        identity = "|".join(map(str, (
            entity_id, address, label_source, valid_from, valid_to, known_at,
        )))
        version_id = "lbl_" + hashlib.sha256(identity.encode()).hexdigest()[:20]
        payload = {
            "label_version_id": version_id, "entity_id": entity_id,
            "entity_label": entity_label, "address": address,
            "label_source": label_source, "confidence": max(0.0, min(1.0, confidence)),
            "valid_from": valid_from, "valid_to": valid_to, "known_at": known_at,
        }
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT OR IGNORE INTO entity_labels
                   (label_version_id,entity_id,entity_label,address,label_source,
                    confidence,valid_from,valid_to,known_at,payload)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (version_id, entity_id, entity_label, address, label_source,
                 payload["confidence"], valid_from, valid_to, known_at,
                 json.dumps(payload, separators=(",", ":"))),
            )
        return version_id

    def resolve_label(self, address: str, *, event_time: int, decision_time: int) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                """SELECT payload FROM entity_labels
                   WHERE address=? AND known_at<=? AND valid_from<=?
                     AND (valid_to IS NULL OR valid_to>=?)
                   ORDER BY confidence DESC,known_at DESC LIMIT 1""",
                (address, decision_time, event_time, event_time),
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    def ingest_transfer(
        self, *, tx_id: str, output_index: int, from_address: str, to_address: str,
        amount_base: float, event_time: int, observed_at: int, decision_time: int,
        confirmations: int, source_id: str,
    ) -> OnchainEntityEvent:
        if not tx_id or output_index < 0:
            raise ValueError("tx_id and non-negative output_index are required")
        if event_time > observed_at or observed_at > decision_time:
            raise ValueError("invalid PIT event/observed/decision ordering")
        from_label = self.resolve_label(
            from_address, event_time=event_time, decision_time=decision_time,
        )
        to_label = self.resolve_label(
            to_address, event_time=event_time, decision_time=decision_time,
        )
        if from_label and to_label and from_label["entity_id"] == to_label["entity_id"]:
            event_type = "internal_rebalance"
            label = to_label
        elif to_label and not from_label:
            event_type = "transfer_to_entity"
            label = to_label
        elif from_label and not to_label:
            event_type = "transfer_from_entity"
            label = from_label
        else:
            # 两个不同已知实体之间的转移也不能压缩成“买/卖”；保守标 unknown。
            event_type = "unknown_counterparty"
            label = to_label or from_label or {}
        event_id = "oc_" + hashlib.sha256(
            f"BTC|{tx_id}|{output_index}".encode()
        ).hexdigest()[:24]
        event = OnchainEntityEvent(
            event_id=event_id, coin="BTC",
            entity_id=str(label.get("entity_id") or "unknown"),
            entity_label=str(label.get("entity_label") or ""),
            event_type=event_type, amount_base=amount_base, tx_id=tx_id,
            confirmations=confirmations,
            label_source=str(label.get("label_source") or ""),
            label_confidence=float(label.get("confidence") or 0),
            label_valid_from=int(label.get("valid_from") or 0),
            label_valid_to=label.get("valid_to"),
            known_at=int(label.get("known_at") or decision_time),
            event_time=event_time, observed_at=observed_at,
            decision_time=decision_time, source_id=source_id,
        )
        with self._lock, self._conn:
            existing = self._conn.execute(
                "SELECT payload FROM onchain_events WHERE tx_id=? AND output_index=?",
                (tx_id, output_index),
            ).fetchone()
            if existing:
                return OnchainEntityEvent.model_validate_json(existing["payload"])
            self._conn.execute(
                """INSERT INTO onchain_events
                   (event_id,tx_id,output_index,decision_time,reorged,payload)
                   VALUES(?,?,?,?,0,?)""",
                (event_id, tx_id, output_index, decision_time, event.model_dump_json()),
            )
        return event

    def mark_reorg(self, event_id: str, *, decision_time: int) -> bool:
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT payload,reorged FROM onchain_events WHERE event_id=?", (event_id,),
            ).fetchone()
            if not row or int(row["reorged"]):
                return False
            event = OnchainEntityEvent.model_validate_json(row["payload"])
            event.reorged = True
            revision_id = "rev_" + hashlib.sha256(
                f"{event_id}|reorg|{decision_time}".encode()
            ).hexdigest()[:20]
            self._conn.execute(
                "UPDATE onchain_events SET reorged=1,payload=? WHERE event_id=?",
                (event.model_dump_json(), event_id),
            )
            self._conn.execute(
                """INSERT OR IGNORE INTO onchain_event_revisions
                   (revision_id,event_id,decision_time,revision_type,payload)
                   VALUES(?,?,?,'reorg',?)""",
                (revision_id, event_id, decision_time, json.dumps({
                    "event_id": event_id, "reorged": True,
                }, separators=(",", ":"))),
            )
            return True

    def recent_events(self, *, decision_time: int, since: int, limit: int = 100) -> list[OnchainEntityEvent]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT payload FROM onchain_events
                   WHERE decision_time<=? AND decision_time>=? AND reorged=0
                   ORDER BY decision_time DESC LIMIT ?""",
                (decision_time, since, max(1, min(limit, 1000))),
            ).fetchall()
        return [OnchainEntityEvent.model_validate_json(row["payload"]) for row in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()
