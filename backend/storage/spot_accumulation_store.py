"""现货抄底模块持久化：原子快照 + append-only 资金账本。"""

from __future__ import annotations

import json
import fcntl
import logging
import os
import shutil
import time
from contextlib import contextmanager
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
        self.ledger_lock_path = self.root / ".ledger.lock"
        self.journal_lock_path = self.root / ".journal.lock"
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
                return prior_reversal, target
            existing = self._existing_or_conflict(events, reversal)
            if existing is not None:
                raise SpotIdempotencyConflict(
                    f"client_event_id={reversal.client_event_id} 未冲正目标事件"
                )
            reversal.sequence = max((item.sequence or 0 for item in events), default=0) + 1
            self.build_portfolio(config, events=events + [reversal])
            return self._append_event_unlocked(reversal), target

    def _load_journal_unlocked(self) -> list[SpotOpportunityJournalEvent]:
        if not self.journal_path.exists():
            return []
        result: list[SpotOpportunityJournalEvent] = []
        with open(self.journal_path, "r", encoding="utf-8") as f:
            for line_no, line in enumerate(f, 1):
                if not line.strip():
                    continue
                try:
                    result.append(SpotOpportunityJournalEvent.model_validate_json(line))
                except Exception as exc:  # noqa: BLE001
                    raise SpotStorageCorruption(
                        f"opportunity_journal.jsonl 第{line_no}行损坏: {exc}"
                    ) from exc
        return result

    def load_journal(self) -> list[SpotOpportunityJournalEvent]:
        with self._exclusive_lock(self.journal_lock_path):
            return self._load_journal_unlocked()

    def append_journal(
        self,
        event: SpotOpportunityJournalEvent,
    ) -> SpotOpportunityJournalEvent:
        with self._exclusive_lock(self.journal_lock_path):
            events = self._load_journal_unlocked()
            event.sequence = max((item.sequence for item in events), default=0) + 1
            payload = json.dumps(event.model_dump(mode="json"), ensure_ascii=False) + "\n"
            fd = os.open(self.journal_path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
            try:
                os.write(fd, payload.encode("utf-8"))
                os.fsync(fd)
            finally:
                os.close(fd)
            return event

    def latest_journal_runtime(self) -> Optional[SpotAccumulationRuntimeState]:
        events = self.load_journal()
        return events[-1].runtime.model_copy(deep=True) if events else None

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

    def append_facts_snapshot(self, payload: dict, timestamp: int) -> None:
        month = time.strftime("%Y-%m", time.gmtime(timestamp))
        path = self.root / f"facts_{month}.jsonl"
        line = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
        fd = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)

    def build_portfolio(
        self,
        config: SpotAccumulationConfig,
        events: Optional[list[SpotLedgerEvent]] = None,
    ) -> SpotPortfolio:
        events = self.load_events() if events is None else list(events)
        reversed_ids = {
            event.reverses_event_id
            for event in events
            if event.event_type == "reversal" and event.reverses_event_id
        }
        active = [
            event for event in events
            if event.event_type == "fill" and event.event_id not in reversed_ids
        ]
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
