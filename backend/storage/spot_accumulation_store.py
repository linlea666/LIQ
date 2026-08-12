"""现货抄底模块持久化：原子快照 + append-only 资金账本。"""

from __future__ import annotations

import json
import fcntl
import logging
import os
import shutil
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Optional

from models.spot_accumulation import (
    BucketPosition,
    SpotAccumulationConfig,
    SpotAccumulationRuntimeState,
    SpotLedgerEvent,
    SpotOpportunityJournalEvent,
    SpotPortfolio,
)

logger = logging.getLogger(__name__)


_JOURNAL_CHECKPOINT_VERSION = 1
_MAX_ACTIVE_JOURNAL_EVENTS = 128
_JOURNAL_ARCHIVE_RETENTION_DAYS = 90
_COMPACT_FACT_RETENTION_DAYS = 90
_DAILY_FACT_RETENTION_DAYS = 400
_RAW_FACT_RETENTION_SECONDS = 48 * 3600
_DAY_SECONDS = 86_400
_TAIL_READ_CHUNK_BYTES = 64 * 1024
# 单条机会日志内嵌完整 runtime，线上约 160KB；超过该上限视为异常，避免读尾部时吃穿内存。
_MAX_JSONL_LINE_BYTES = 8 * 1024 * 1024


@dataclass
class SpotStageExecution:
    spent_usdt: float = 0.0
    quantity_btc: float = 0.0
    fee_usdt: float = 0.0

    @property
    def average_price(self) -> float:
        gross = max(0.0, self.spent_usdt - self.fee_usdt)
        return gross / self.quantity_btc if self.quantity_btc > 0 else 0.0


@dataclass
class SpotLedgerExecutionSummary:
    stages: dict[str, SpotStageExecution] = field(default_factory=dict)
    unassigned_core_buy_usdt: float = 0.0
    linked_opportunity_ids: set[str] = field(default_factory=set)


class SpotStorageCorruption(ValueError):
    """资金事实文件损坏；必须停止放款，禁止静默跳过。"""


class SpotIdempotencyConflict(ValueError):
    """同一个幂等键被用于不同业务载荷。"""


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


class SpotAccumulationStore:
    def __init__(self, data_dir: str) -> None:
        self.root = Path(data_dir) / "spot_accumulation"
        self.root.mkdir(parents=True, exist_ok=True)
        self.config_path = self.root / "config.json"
        self.state_path = self.root / "state.json"
        self.raw_path = self.root / "long_term_facts.json"
        self.ledger_path = self.root / "ledger.jsonl"
        self.journal_path = self.root / "opportunity_journal.jsonl"
        self.journal_checkpoint_path = self.root / "runtime_checkpoint.json"
        self.legacy_journal_path = self.root / "opportunity_journal.legacy.jsonl"
        self.journal_archive_dir = self.root / "journal_archive"
        self.compact_facts_dir = self.root / "facts_compact"
        self.raw_facts_dir = self.root / "facts_raw"
        self.daily_facts_path = self.root / "facts_daily.jsonl"
        self.ledger_lock_path = self.root / ".ledger.lock"
        self.journal_lock_path = self.root / ".journal.lock"
        self.config_lock_path = self.root / ".config.lock"
        self.facts_lock_path = self.root / ".facts.lock"
        self.migration_lock_path = self.root / ".migration.lock"

    @staticmethod
    @contextmanager
    def _exclusive_lock(path: Path) -> Iterator[None]:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def load_config(self) -> SpotAccumulationConfig:
        if not self.config_path.exists():
            return SpotAccumulationConfig()
        try:
            return SpotAccumulationConfig.model_validate_json(
                self.config_path.read_text(encoding="utf-8")
            )
        except Exception as exc:  # noqa: BLE001
            raise SpotStorageCorruption(f"config.json 损坏: {exc}") from exc

    def save_config(self, config: SpotAccumulationConfig) -> None:
        _atomic_write_json(self.config_path, config.model_dump(mode="json"))

    @contextmanager
    def config_transaction(self) -> Iterator[None]:
        with self._exclusive_lock(self.config_lock_path):
            yield

    def config_needs_v3_migration(self) -> bool:
        if not self.config_path.exists():
            return False
        raw = json.loads(self.config_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return False
        return int(raw.get("schema_version", raw.get("version", 1)) or 1) < 3

    def backup_config_v2_once(self) -> None:
        with self._exclusive_lock(self.migration_lock_path):
            backup = self.root / "migration_backup_v2"
            backup.mkdir(parents=True, exist_ok=True)
            target = backup / "config.json"
            if self.config_path.exists() and not target.exists():
                shutil.copy2(self.config_path, target)

    def load_state(self) -> SpotAccumulationRuntimeState:
        if not self.state_path.exists():
            return SpotAccumulationRuntimeState()
        try:
            return SpotAccumulationRuntimeState.model_validate_json(
                self.state_path.read_text(encoding="utf-8")
            )
        except Exception as exc:  # noqa: BLE001
            raise SpotStorageCorruption(f"state.json 损坏: {exc}") from exc

    def save_state(self, state: SpotAccumulationRuntimeState) -> None:
        _atomic_write_json(self.state_path, state.model_dump(mode="json"))

    def load_long_term_facts(self) -> dict:
        if not self.raw_path.exists():
            return {}
        try:
            raw = json.loads(self.raw_path.read_text(encoding="utf-8"))
            return raw if isinstance(raw, dict) else {}
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("spot accumulation long-term facts invalid: %s", exc)
            return {}

    def save_long_term_facts(self, payload: dict) -> None:
        _atomic_write_json(self.raw_path, payload)

    def _load_events_unlocked(self) -> list[SpotLedgerEvent]:
        if not self.ledger_path.exists():
            return []
        events: list[SpotLedgerEvent] = []
        with open(self.ledger_path, "r", encoding="utf-8") as f:
            for line_no, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    event = SpotLedgerEvent.model_validate_json(line)
                    if event.sequence is None:
                        # 旧账本不重写，运行时以原始行号作为稳定顺序。
                        event.sequence = line_no
                    events.append(event)
                except Exception as exc:  # noqa: BLE001
                    raise SpotStorageCorruption(
                        f"ledger.jsonl 第{line_no}行损坏: {exc}"
                    ) from exc
        return events

    def load_events(self) -> list[SpotLedgerEvent]:
        with self._exclusive_lock(self.ledger_lock_path):
            return self._load_events_unlocked()

    @staticmethod
    def active_fills_from_events(events: list[SpotLedgerEvent]) -> list[SpotLedgerEvent]:
        """统一的未冲正成交事实；组合、恢复与规划必须共用该口径。"""
        reversed_ids = {
            event.reverses_event_id
            for event in events
            if event.event_type == "reversal" and event.reverses_event_id
        }
        return [
            event for event in events
            if event.event_type == "fill" and event.event_id not in reversed_ids
        ]

    def build_execution_summary(
        self,
        events: Optional[list[SpotLedgerEvent]] = None,
        opportunity_stage_lookup: Optional[dict[str, str]] = None,
    ) -> SpotLedgerExecutionSummary:
        events = self.load_events() if events is None else list(events)
        result = SpotLedgerExecutionSummary()
        core_stages = {
            "insurance", "value_1", "deep_value", "capitulation", "bottom_confirmed",
        }
        for event in self.active_fills_from_events(events):
            stage_name = event.opportunity_stage or (
                (opportunity_stage_lookup or {}).get(event.opportunity_id or "")
            )
            if event.opportunity_id:
                result.linked_opportunity_ids.add(event.opportunity_id)
            if event.side != "buy":
                continue
            spent = event.quantity_btc * event.price_usdt + event.fee_usdt
            if event.bucket == "core" and stage_name not in core_stages:
                result.unassigned_core_buy_usdt += spent
                continue
            if event.bucket != "core" or stage_name not in core_stages:
                continue
            stage = result.stages.setdefault(stage_name, SpotStageExecution())
            stage.spent_usdt += spent
            stage.quantity_btc += event.quantity_btc
            stage.fee_usdt += event.fee_usdt
        result.unassigned_core_buy_usdt = round(result.unassigned_core_buy_usdt, 8)
        return result

    def get_by_client_event_id(self, client_event_id: str) -> Optional[SpotLedgerEvent]:
        return next(
            (event for event in self.load_events() if event.client_event_id == client_event_id),
            None,
        )

    @staticmethod
    def _idempotency_payload(event: SpotLedgerEvent) -> dict:
        return event.model_dump(
            mode="json",
            exclude={
                "event_id", "sequence", "created_at", "policy_version",
                "opportunity_stage", "opportunity_allocation_usdt", "batch_id",
                "client_payload_hash",
            },
        )

    def _existing_or_conflict(
        self,
        events: list[SpotLedgerEvent],
        event: SpotLedgerEvent,
    ) -> Optional[SpotLedgerEvent]:
        existing = next(
            (item for item in events if item.client_event_id == event.client_event_id),
            None,
        )
        if existing is None:
            return None
        if existing.client_payload_hash and event.client_payload_hash:
            if existing.client_payload_hash == event.client_payload_hash:
                return existing
            raise SpotIdempotencyConflict(
                f"client_event_id={event.client_event_id} 已用于不同载荷"
            )
        if self._idempotency_payload(existing) != self._idempotency_payload(event):
            raise SpotIdempotencyConflict(
                f"client_event_id={event.client_event_id} 已用于不同载荷"
            )
        return existing

    def _append_event_unlocked(self, event: SpotLedgerEvent) -> SpotLedgerEvent:
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(event.model_dump(mode="json"), ensure_ascii=False) + "\n"
        fd = os.open(self.ledger_path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
        try:
            os.write(fd, payload.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        return event

    def append_event(self, event: SpotLedgerEvent) -> SpotLedgerEvent:
        """兼容测试/迁移的原始追加；生产成交使用 commit_event。"""
        with self._exclusive_lock(self.ledger_lock_path):
            events = self._load_events_unlocked()
            existing = self._existing_or_conflict(events, event)
            if existing is not None:
                return existing
            event.sequence = max((item.sequence or 0 for item in events), default=0) + 1
            return self._append_event_unlocked(event)

    def commit_event(
        self,
        event: SpotLedgerEvent,
        config: SpotAccumulationConfig,
    ) -> SpotLedgerEvent:
        """在同一文件锁内完成幂等、完整重放验证和追加。"""
        with self._exclusive_lock(self.ledger_lock_path):
            events = self._load_events_unlocked()
            existing = self._existing_or_conflict(events, event)
            if existing is not None:
                return existing
            event.sequence = max((item.sequence or 0 for item in events), default=0) + 1
            self.build_portfolio(config, events=events + [event])
            return self._append_event_unlocked(event)

    def commit_reversal(
        self,
        event_id: str,
        reversal: SpotLedgerEvent,
        config: SpotAccumulationConfig,
    ) -> tuple[SpotLedgerEvent, SpotLedgerEvent]:
        """冲正必须先证明删除目标成交后整个账本仍可重放。"""
        with self._exclusive_lock(self.ledger_lock_path):
            events = self._load_events_unlocked()
            target = next(
                (item for item in events if item.event_id == event_id and item.event_type == "fill"),
                None,
            )
            if target is None:
                raise KeyError("成交事件不存在")
            prior_reversal = next(
                (item for item in events if item.reverses_event_id == event_id),
                None,
            )
            if prior_reversal is not None:
                existing = self._existing_or_conflict(events, reversal)
                if existing is not None and existing.event_id == prior_reversal.event_id:
                    return prior_reversal, target
                raise SpotIdempotencyConflict(f"成交事件 {event_id} 已被冲正")
            existing = self._existing_or_conflict(events, reversal)
            if existing is not None:
                raise SpotIdempotencyConflict(
                    f"client_event_id={reversal.client_event_id} 未冲正目标事件"
                )
            reversal.sequence = max((item.sequence or 0 for item in events), default=0) + 1
            self.build_portfolio(config, events=events + [reversal])
            return self._append_event_unlocked(reversal), target

    @staticmethod
    def _read_last_jsonl_line(path: Path) -> tuple[int, str] | None:
        """读取文件最后一条完整 JSONL 记录，不加载历史全文。

        返回 (该行起始字节偏移, 行内容)。

        关键约束：只有当候选行之前在窗口内出现过换行符（或窗口已回退到文件头）时，
        才能确认这一行没有被读取窗口截断。单条机会日志内嵌完整 runtime，线上可达
        160+ KB，远超单次读取块；若只看一个块就返回末尾片段，会得到半截 JSON 并被
        误判为“记录损坏”，进而让整个现货模块 fail-closed 停摆。
        """
        if not path.exists():
            return None
        size = path.stat().st_size
        if size == 0:
            return None
        with open(path, "rb") as f:
            pos = size
            window = b""
            while pos > 0:
                step = min(_TAIL_READ_CHUNK_BYTES, pos)
                pos -= step
                f.seek(pos)
                window = f.read(step) + window
                lines = window.split(b"\n")
                offsets: list[int] = []
                cursor = pos
                for item in lines:
                    offsets.append(cursor)
                    cursor += len(item) + 1
                # 窗口未到文件头时，lines[0] 可能被截断，不能作为完整行使用。
                lowest = 0 if pos == 0 else 1
                for index in range(len(lines) - 1, lowest - 1, -1):
                    if lines[index].strip():
                        return offsets[index], lines[index].decode("utf-8")
                if len(window) > _MAX_JSONL_LINE_BYTES:
                    raise SpotStorageCorruption(
                        f"{path.name} 末尾单行超过 "
                        f"{_MAX_JSONL_LINE_BYTES // (1024 * 1024)}MiB，拒绝载入"
                    )
        return None

    @staticmethod
    def _parse_journal_event(line: str, line_ref: str) -> SpotOpportunityJournalEvent:
        try:
            return SpotOpportunityJournalEvent.model_validate_json(line)
        except Exception as exc:  # noqa: BLE001
            raise SpotStorageCorruption(f"{line_ref}损坏: {exc}") from exc

    def _load_journal_unlocked(self) -> list[SpotOpportunityJournalEvent]:
        if not self.journal_path.exists():
            return []
        result: list[SpotOpportunityJournalEvent] = []
        with open(self.journal_path, "r", encoding="utf-8") as f:
            for line_no, line in enumerate(f, 1):
                if not line.strip():
                    continue
                # 超限时立即失败：绝不能把无界历史全部解析进内存后再报错。
                if len(result) >= _MAX_ACTIVE_JOURNAL_EVENTS:
                    raise SpotStorageCorruption(
                        "活动机会日志超过上限；请先运行存储迁移，不允许全量恢复"
                    )
                result.append(self._parse_journal_event(
                    line, f"opportunity_journal.jsonl 第{line_no}行",
                ))
        return result

    @staticmethod
    def _line_count_exceeds(path: Path, limit: int) -> bool:
        """流式判断 JSONL 行数是否超限；不解析内容，内存占用与文件大小无关。"""
        if not path.exists():
            return False
        count = 0
        with open(path, "rb") as f:
            while True:
                chunk = f.read(1024 * 1024)
                if not chunk:
                    return False
                count += chunk.count(b"\n")
                if count > limit:
                    return True

    def _load_checkpoint_unlocked(self) -> tuple[int, SpotAccumulationRuntimeState] | None:
        if not self.journal_checkpoint_path.exists():
            return None
        try:
            raw = json.loads(self.journal_checkpoint_path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("checkpoint must be an object")
            if int(raw.get("schema_version", 0)) != _JOURNAL_CHECKPOINT_VERSION:
                raise ValueError("unsupported checkpoint schema")
            sequence = int(raw.get("sequence", 0))
            if sequence < 0:
                raise ValueError("negative checkpoint sequence")
            runtime = SpotAccumulationRuntimeState.model_validate(raw.get("runtime") or {})
            return sequence, runtime
        except Exception as exc:  # noqa: BLE001
            raise SpotStorageCorruption(f"runtime_checkpoint.json 损坏: {exc}") from exc

    def _write_checkpoint_unlocked(self, event: SpotOpportunityJournalEvent) -> None:
        _atomic_write_json(self.journal_checkpoint_path, {
            "schema_version": _JOURNAL_CHECKPOINT_VERSION,
            "sequence": event.sequence,
            "event_id": event.event_id,
            "event_type": event.event_type,
            "created_at": event.created_at,
            "runtime": event.runtime.model_dump(mode="json"),
        })

    def _migrate_legacy_journal_unlocked(self) -> bool:
        """Archive a legacy unbounded journal and checkpoint its final durable state."""
        if self._load_checkpoint_unlocked() is not None:
            return False
        source = self.journal_path if self.journal_path.exists() else self.legacy_journal_path
        if not source.exists() or source.stat().st_size == 0:
            return False
        last = self._read_last_jsonl_line(source)
        if last is None:
            return False
        _offset, line = last
        event = self._parse_journal_event(line, f"{source.name} 最后一条记录")
        if source == self.journal_path:
            if self.legacy_journal_path.exists():
                raise SpotStorageCorruption(
                    "旧机会日志与待迁移日志同时存在，拒绝覆盖审计数据"
                )
            os.replace(self.journal_path, self.legacy_journal_path)
        self._write_checkpoint_unlocked(event)
        logger.info(
            "spot journal migrated to checkpoint | sequence=%d legacy=%s",
            event.sequence, self.legacy_journal_path.name,
        )
        return True

    def _archive_active_journal_unlocked(self, event: SpotOpportunityJournalEvent) -> None:
        """检查点落盘后把活动 journal 移入归档目录；同名归档不覆盖。"""
        self._write_checkpoint_unlocked(event)
        self.journal_archive_dir.mkdir(parents=True, exist_ok=True)
        archive = self.journal_archive_dir / f"opportunity_journal_{event.sequence}.jsonl"
        suffix = 1
        while archive.exists():
            archive = self.journal_archive_dir / (
                f"opportunity_journal_{event.sequence}_{suffix}.jsonl"
            )
            suffix += 1
        os.replace(self.journal_path, archive)
        self._prune_journal_archive_unlocked()

    def _prune_journal_archive_unlocked(self) -> None:
        if not self.journal_archive_dir.exists():
            return
        cutoff = time.time() - _JOURNAL_ARCHIVE_RETENTION_DAYS * _DAY_SECONDS
        for path in self.journal_archive_dir.glob("opportunity_journal_*.jsonl"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
            except OSError:
                logger.warning("journal archive prune failed | file=%s", path.name, exc_info=True)

    def _rotate_oversized_journal_unlocked(self) -> bool:
        """活动 journal 超限时自愈轮转：保留全部审计数据，避免恢复路径被永久阻断。

        正常运行由 append_journal 触发轮转；此处覆盖轮转中断、或历史遗留的
        “检查点已存在但活动 journal 仍然超限”的场景。
        """
        if not self._line_count_exceeds(self.journal_path, _MAX_ACTIVE_JOURNAL_EVENTS):
            return False
        last = self._read_last_jsonl_line(self.journal_path)
        if last is None:
            return False
        _offset, line = last
        event = self._parse_journal_event(line, "opportunity_journal.jsonl 最后一条记录")
        self._archive_active_journal_unlocked(event)
        logger.warning(
            "spot active journal exceeded %d events; rotated to archive | sequence=%d",
            _MAX_ACTIVE_JOURNAL_EVENTS, event.sequence,
        )
        return True

    def _ensure_bounded_journal_unlocked(self) -> None:
        self._migrate_legacy_journal_unlocked()
        self._rotate_oversized_journal_unlocked()

    def migrate_legacy_journal(self) -> bool:
        with self._exclusive_lock(self.journal_lock_path):
            return self._migrate_legacy_journal_unlocked()

    def load_journal(self) -> list[SpotOpportunityJournalEvent]:
        with self._exclusive_lock(self.journal_lock_path):
            self._ensure_bounded_journal_unlocked()
            return self._load_journal_unlocked()

    def append_journal(
        self,
        event: SpotOpportunityJournalEvent,
    ) -> SpotOpportunityJournalEvent:
        with self._exclusive_lock(self.journal_lock_path):
            self._ensure_bounded_journal_unlocked()
            checkpoint = self._load_checkpoint_unlocked()
            events = self._load_journal_unlocked()
            base_sequence = checkpoint[0] if checkpoint else 0
            event.sequence = max(
                base_sequence,
                max((item.sequence for item in events), default=0),
            ) + 1
            payload = json.dumps(event.model_dump(mode="json"), ensure_ascii=False) + "\n"
            fd = os.open(self.journal_path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
            try:
                os.write(fd, payload.encode("utf-8"))
                os.fsync(fd)
            finally:
                os.close(fd)
            if len(events) + 1 >= _MAX_ACTIVE_JOURNAL_EVENTS:
                self._archive_active_journal_unlocked(event)
            return event

    def latest_journal_runtime(self) -> Optional[SpotAccumulationRuntimeState]:
        with self._exclusive_lock(self.journal_lock_path):
            self._ensure_bounded_journal_unlocked()
            checkpoint = self._load_checkpoint_unlocked()
            events = self._load_journal_unlocked()
            latest = events[-1] if events else None
            if latest and (checkpoint is None or latest.sequence > checkpoint[0]):
                return latest.runtime.model_copy(deep=True)
            return checkpoint[1].model_copy(deep=True) if checkpoint else None

    def backup_legacy_files_once(self) -> None:
        """首次事件日志迁移前保存用户原文件；不覆盖已有备份。"""
        with self._exclusive_lock(self.migration_lock_path):
            backup = self.root / "migration_backup_v1"
            if backup.exists():
                return
            backup.mkdir(parents=True, exist_ok=False)
            for path in (self.config_path, self.state_path, self.ledger_path):
                if path.exists():
                    shutil.copy2(path, backup / path.name)

    def _append_jsonl(self, path: Path, payload: dict) -> None:
        line = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
        try:
            os.write(fd, line.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)

    @staticmethod
    def _day_key(timestamp: int) -> str:
        return time.strftime("%Y%m%d", time.gmtime(timestamp))

    def append_compact_facts_snapshot(self, payload: dict, timestamp: int) -> None:
        with self._exclusive_lock(self.facts_lock_path):
            self._append_jsonl(self.compact_facts_dir / f"{self._day_key(timestamp)}.jsonl", payload)
            self._prune_facts_unlocked(timestamp)

    def append_raw_facts_snapshot(self, payload: dict, timestamp: int) -> None:
        with self._exclusive_lock(self.facts_lock_path):
            self._append_jsonl(self.raw_facts_dir / f"{self._day_key(timestamp)}.jsonl", payload)
            self._prune_facts_unlocked(timestamp)

    def _prune_facts_unlocked(self, now_ts: int) -> None:
        compact_cutoff = self._day_key(now_ts - _COMPACT_FACT_RETENTION_DAYS * 86400)
        raw_cutoff = self._day_key(now_ts - _RAW_FACT_RETENTION_SECONDS)
        for root, cutoff in ((self.compact_facts_dir, compact_cutoff), (self.raw_facts_dir, raw_cutoff)):
            if not root.exists():
                continue
            for path in root.glob("*.jsonl"):
                if path.stem.isdigit() and path.stem < cutoff:
                    path.unlink()

    @staticmethod
    def _read_jsonl_records(path: Path) -> list[dict]:
        """严格读取一个 JSONL 文件；损坏行不得被静默跳过。"""
        if not path.exists():
            return []
        records: list[dict] = []
        with open(path, "r", encoding="utf-8") as f:
            for line_no, line in enumerate(f, 1):
                if not line.strip():
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise SpotStorageCorruption(
                        f"{path.name} 第{line_no}行损坏: {exc}"
                    ) from exc
                if not isinstance(payload, dict):
                    raise SpotStorageCorruption(
                        f"{path.name} 第{line_no}行必须是JSON对象"
                    )
                records.append(payload)
        return records

    def _legacy_monthly_fact_paths(self) -> list[Path]:
        """按月切分的旧事实归档 facts_YYYY-MM.jsonl。

        必须排除 facts_daily.jsonl：它同在根目录且同样匹配 facts_*.jsonl，但存的是
        日汇总而非事实快照，混入会污染前向报告输入并让旧归档统计永远清不到 0。
        """
        return sorted(
            path for path in self.root.glob("facts_*.jsonl")
            if path != self.daily_facts_path
        )

    def load_facts_snapshots(self, *, include_legacy: bool = False) -> list[dict]:
        """Read compact archives by default; legacy multi-GB files require explicit opt-in."""
        records: list[dict] = []
        with self._exclusive_lock(self.facts_lock_path):
            paths = sorted(self.compact_facts_dir.glob("*.jsonl"))
            if include_legacy:
                paths.extend(self._legacy_monthly_fact_paths())
            for path in paths:
                records.extend(self._read_jsonl_records(path))
        return records

    def load_compact_facts_for_day(self, day_key: str) -> list[dict]:
        """读取单日紧凑事实；用于生成日汇总，内存占用限定为一天的数据量。"""
        with self._exclusive_lock(self.facts_lock_path):
            return self._read_jsonl_records(self.compact_facts_dir / f"{day_key}.jsonl")

    def load_daily_facts_rollups(self) -> list[dict]:
        """读取 400 天日汇总；文件按保留策略裁剪，可安全全量加载。"""
        with self._exclusive_lock(self.facts_lock_path):
            return self._read_jsonl_records(self.daily_facts_path)

    def daily_rollup_days(self) -> set[str]:
        return {
            str(record.get("day"))
            for record in self.load_daily_facts_rollups()
            if record.get("day")
        }

    def append_daily_facts_rollup(self, payload: dict) -> bool:
        """追加一条日汇总；同日已存在则跳过，并按 400 天上限裁剪。"""
        day = str(payload.get("day") or "")
        if not day:
            raise ValueError("daily rollup payload 必须包含 day")
        with self._exclusive_lock(self.facts_lock_path):
            existing = self._read_jsonl_records(self.daily_facts_path)
            if any(str(record.get("day")) == day for record in existing):
                return False
            merged = existing + [payload]
            # 保留窗口以最新一天为锚：补写历史日汇总不得把窗口整体往前拉。
            anchor = max(str(record.get("day") or "") for record in merged)
            kept = [
                record for record in merged
                if str(record.get("day") or "") >= self._retention_cutoff_day(anchor)
            ]
            kept.sort(key=lambda record: str(record.get("day") or ""))
            if len(kept) == len(existing) + 1:
                self._append_jsonl(self.daily_facts_path, payload)
            else:
                self._rewrite_jsonl(self.daily_facts_path, kept)
            return True

    @staticmethod
    def _retention_cutoff_day(day: str) -> str:
        try:
            anchor = time.mktime(time.strptime(day, "%Y%m%d"))
        except ValueError:
            return ""
        return time.strftime(
            "%Y%m%d", time.localtime(anchor - _DAILY_FACT_RETENTION_DAYS * _DAY_SECONDS)
        )

    def _rewrite_jsonl(self, path: Path, records: list[dict]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            for record in records:
                f.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)

    @staticmethod
    def _tree_stats(root: Path) -> dict[str, int]:
        if not root.exists():
            return {"files": 0, "bytes": 0}
        if root.is_file():
            return {"files": 1, "bytes": root.stat().st_size}
        files = 0
        total = 0
        for path in root.rglob("*"):
            if path.is_file():
                files += 1
                total += path.stat().st_size
        return {"files": files, "bytes": total}

    @staticmethod
    def _path_list_stats(paths: list[Path]) -> dict[str, int]:
        total = 0
        for path in paths:
            try:
                total += path.stat().st_size
            except OSError:
                continue
        return {"files": len(paths), "bytes": total}

    def storage_stats(self) -> dict:
        checkpoint = self._load_checkpoint_unlocked()
        return {
            "journal_checkpoint_sequence": checkpoint[0] if checkpoint else 0,
            "active_journal": self._tree_stats(self.journal_path),
            "legacy_journal": self._tree_stats(self.legacy_journal_path),
            "journal_archive": self._tree_stats(self.journal_archive_dir),
            "compact_facts": self._tree_stats(self.compact_facts_dir),
            "raw_facts": self._tree_stats(self.raw_facts_dir),
            "daily_facts": self._tree_stats(self.daily_facts_path),
            "legacy_monthly_facts": self._path_list_stats(self._legacy_monthly_fact_paths()),
            "retention": {
                "compact_days": _COMPACT_FACT_RETENTION_DAYS,
                "daily_days": _DAILY_FACT_RETENTION_DAYS,
                "raw_hours": _RAW_FACT_RETENTION_SECONDS // 3600,
                "journal_archive_days": _JOURNAL_ARCHIVE_RETENTION_DAYS,
            },
        }

    def build_portfolio(
        self,
        config: SpotAccumulationConfig,
        events: Optional[list[SpotLedgerEvent]] = None,
    ) -> SpotPortfolio:
        events = self.load_events() if events is None else list(events)
        active = self.active_fills_from_events(events)
        buckets: dict[str, BucketPosition] = {
            "core": BucketPosition(bucket="core", cash_usdt=config.core_budget_usdt),
            "swing": BucketPosition(bucket="swing", cash_usdt=config.swing_budget_usdt),
            "tail": BucketPosition(bucket="tail", cash_usdt=config.tail_budget_usdt),
        }
        core_bonus = 0.0
        for event in sorted(active, key=lambda item: (item.executed_at, item.sequence or 0)):
            assert event.bucket is not None and event.side is not None
            pos = buckets[event.bucket]
            gross = event.quantity_btc * event.price_usdt
            if event.side == "buy":
                spend = gross + event.fee_usdt
                if spend > pos.cash_usdt + 0.01:
                    raise ValueError(
                        f"ledger overspend bucket={event.bucket} spend={spend:.2f} cash={pos.cash_usdt:.2f}"
                    )
                pos.cash_usdt -= spend
                pos.btc_quantity += event.quantity_btc
                pos.cost_basis_usdt += spend
            else:
                if event.quantity_btc > pos.btc_quantity + 1e-12:
                    raise ValueError(
                        f"ledger oversell bucket={event.bucket} qty={event.quantity_btc} held={pos.btc_quantity}"
                    )
                average = pos.cost_basis_usdt / pos.btc_quantity if pos.btc_quantity else 0.0
                removed_basis = average * event.quantity_btc
                proceeds = gross - event.fee_usdt
                pnl = proceeds - removed_basis
                pos.btc_quantity -= event.quantity_btc
                pos.cost_basis_usdt -= removed_basis
                pos.realized_pnl_usdt += pnl
                if event.bucket == "swing":
                    pos.cash_usdt += removed_basis + min(pnl, 0.0)
                    if pnl > 0:
                        buckets["core"].cash_usdt += pnl
                        core_bonus += pnl
                else:
                    pos.cash_usdt += proceeds

            if pos.cash_usdt < -0.01:
                raise ValueError(
                    f"ledger negative cash bucket={event.bucket} cash={pos.cash_usdt:.2f}"
                )

            pos.cash_usdt = round(pos.cash_usdt, 8)
            pos.btc_quantity = max(0.0, round(pos.btc_quantity, 12))
            pos.cost_basis_usdt = max(0.0, round(pos.cost_basis_usdt, 8))
            pos.average_cost_usdt = (
                pos.cost_basis_usdt / pos.btc_quantity if pos.btc_quantity else 0.0
            )

        total_cash = sum(pos.cash_usdt for pos in buckets.values())
        total_btc = sum(pos.btc_quantity for pos in buckets.values())
        total_basis = sum(pos.cost_basis_usdt for pos in buckets.values())
        total_pnl = sum(pos.realized_pnl_usdt for pos in buckets.values())
        return SpotPortfolio(
            initial_capital_usdt=config.initial_capital_usdt,
            buckets=buckets,  # type: ignore[arg-type]
            total_cash_usdt=round(total_cash, 8),
            total_btc=round(total_btc, 12),
            total_cost_basis_usdt=round(total_basis, 8),
            average_cost_usdt=total_basis / total_btc if total_btc else 0.0,
            realized_pnl_usdt=round(total_pnl, 8),
            core_bonus_from_swing_usdt=round(core_bonus, 8),
        )
