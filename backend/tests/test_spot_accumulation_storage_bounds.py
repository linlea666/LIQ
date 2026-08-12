"""存储有界性回归：启动恢复、归档保留与入口重复导入保护。

对应治理目标：后端常驻内存不得随历史总量线性增长。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from models.spot_accumulation import (
    EvidenceScore,
    SpotAccumulationRuntimeState,
    SpotOpportunity,
    SpotOpportunityJournalEvent,
)
from processors.spot_accumulation_service import SpotAccumulationService
from storage.spot_accumulation_store import (
    SpotAccumulationStore,
    SpotStorageCorruption,
    _MAX_ACTIVE_JOURNAL_EVENTS,
    _TAIL_READ_CHUNK_BYTES,
)


def _event(sequence: int, cycle_ath: float = 100_000.0) -> SpotOpportunityJournalEvent:
    return SpotOpportunityJournalEvent(
        event_id=f"evt-{sequence}",
        sequence=sequence,
        event_type="market",
        created_at=1_700_000_000 + sequence,
        runtime=SpotAccumulationRuntimeState(cycle_ath=cycle_ath, updated_at=sequence),
    )


def _bulky_event(sequence: int, cycle_ath: float = 100_000.0) -> SpotOpportunityJournalEvent:
    """构造与线上同量级的记录：runtime 内嵌 200 条历史机会，单条远超尾部读取块。

    线上 _prune_terminal 保留最近 200 条终态机会，单条日志约 160KB。
    """
    event = _event(sequence, cycle_ath)
    for index in range(200):
        event.runtime.opportunities[f"op-{sequence}-{index}"] = SpotOpportunity(
            opportunity_id=f"op-{sequence}-{index}",
            stage="insurance",
            bucket="core",
            allocation_usdt=1_000,
            status="invalidated",
            price_zone_low=49_000,
            price_zone_high=51_000,
            trigger_price=50_000,
            scores=EvidenceScore(valuation=80, capital_flow=70, acceptance=67.53),
            reasons=["估值层通过：回撤达到保险仓阈值", "资金层通过：ETF 连续净流入"],
            blocked_by=["现货承接不足，等待吸筹确认"],
            created_at=1_700_000_000,
            updated_at=1_700_000_000 + index,
        )
    return event


def _write_journal(path: Path, events: list[SpotOpportunityJournalEvent]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for event in events:
            f.write(json.dumps(event.model_dump(mode="json"), ensure_ascii=False) + "\n")


def test_unbounded_legacy_journal_is_migrated_without_full_parse(tmp_path):
    store = SpotAccumulationStore(str(tmp_path))
    events = [_event(seq, cycle_ath=100_000.0 + seq) for seq in range(1, 501)]
    _write_journal(store.journal_path, events)

    runtime = store.latest_journal_runtime()

    assert runtime is not None
    assert runtime.cycle_ath == 100_500.0
    assert not store.journal_path.exists()
    assert store.legacy_journal_path.exists()
    assert store.journal_checkpoint_path.exists()
    # 旧数据整体保留为只读审计源，不得被删除
    assert len(store.legacy_journal_path.read_text(encoding="utf-8").splitlines()) == 500


def test_tail_read_returns_whole_line_when_record_exceeds_read_chunk(tmp_path):
    """线上单条日志 160KB+：只读一个尾部块会返回半截 JSON，必须继续向前扩窗。"""
    store = SpotAccumulationStore(str(tmp_path))
    events = [_bulky_event(seq) for seq in (1, 2)]
    _write_journal(store.journal_path, events)
    serialized = json.dumps(events[-1].model_dump(mode="json"), ensure_ascii=False)
    assert len(serialized.encode("utf-8")) > _TAIL_READ_CHUNK_BYTES

    offset, line = SpotAccumulationStore._read_last_jsonl_line(store.journal_path)

    assert json.loads(line)["sequence"] == 2
    with open(store.journal_path, "rb") as f:
        f.seek(offset)
        assert f.read(1) == b"{"


def test_tail_read_skips_trailing_blank_lines_and_handles_single_record(tmp_path):
    store = SpotAccumulationStore(str(tmp_path))
    _write_journal(store.journal_path, [_bulky_event(7)])
    with open(store.journal_path, "a", encoding="utf-8") as f:
        f.write("\n\n")

    offset, line = SpotAccumulationStore._read_last_jsonl_line(store.journal_path)

    assert json.loads(line)["sequence"] == 7
    assert offset == 0


def test_tail_read_rejects_pathologically_long_line(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "storage.spot_accumulation_store._MAX_JSONL_LINE_BYTES", 256 * 1024,
    )
    store = SpotAccumulationStore(str(tmp_path))
    store.journal_path.write_text("x" * (512 * 1024), encoding="utf-8")

    with pytest.raises(SpotStorageCorruption, match="超过"):
        SpotAccumulationStore._read_last_jsonl_line(store.journal_path)


def test_production_sized_journal_migrates_instead_of_failing_closed(tmp_path):
    """线上回归：380MB / 2511 条、单条 160KB 的日志必须能迁移，而不是判定为损坏。"""
    store = SpotAccumulationStore(str(tmp_path))
    _write_journal(store.journal_path, [_bulky_event(seq, 100_000.0 + seq) for seq in range(1, 21)])

    service = SpotAccumulationService(str(tmp_path), lambda: None)

    assert service.recovery_errors == []
    assert service.recovery_required is False
    assert service.runtime.cycle_ath == 100_020.0
    assert store.journal_checkpoint_path.exists()
    assert store.legacy_journal_path.exists()
    assert store.storage_stats()["journal_checkpoint_sequence"] == 20


def test_legacy_migration_is_idempotent_and_keeps_sequence_monotonic(tmp_path):
    store = SpotAccumulationStore(str(tmp_path))
    _write_journal(store.journal_path, [_event(seq) for seq in range(1, 301)])

    assert store.migrate_legacy_journal() is True
    assert store.migrate_legacy_journal() is False

    appended = store.append_journal(_event(1))
    assert appended.sequence == 301
    assert store.append_journal(_event(1)).sequence == 302


def test_active_journal_rotates_and_recovery_reads_checkpoint(tmp_path):
    store = SpotAccumulationStore(str(tmp_path))
    last_sequence = 0
    for index in range(_MAX_ACTIVE_JOURNAL_EVENTS + 5):
        last_sequence = store.append_journal(_event(1, cycle_ath=1_000.0 + index)).sequence

    archives = list(store.journal_archive_dir.glob("opportunity_journal_*.jsonl"))
    assert archives, "活动 journal 达到上限后必须轮转归档"
    assert len(store.load_journal()) <= _MAX_ACTIVE_JOURNAL_EVENTS
    runtime = store.latest_journal_runtime()
    assert runtime is not None
    assert runtime.cycle_ath == 1_000.0 + _MAX_ACTIVE_JOURNAL_EVENTS + 4
    assert store.storage_stats()["journal_checkpoint_sequence"] > 0
    assert last_sequence == _MAX_ACTIVE_JOURNAL_EVENTS + 5


def test_oversized_active_journal_self_heals_when_checkpoint_exists(tmp_path):
    """检查点已存在但活动 journal 超限时必须自愈轮转，不能让恢复路径永久失败。"""
    store = SpotAccumulationStore(str(tmp_path))
    _write_journal(store.journal_path, [_event(1)])
    assert store.migrate_legacy_journal() is True
    _write_journal(
        store.journal_path,
        [_event(seq, cycle_ath=200_000.0 + seq) for seq in range(1, _MAX_ACTIVE_JOURNAL_EVENTS + 60)],
    )

    runtime = store.latest_journal_runtime()

    assert runtime is not None
    assert runtime.cycle_ath == 200_000.0 + _MAX_ACTIVE_JOURNAL_EVENTS + 59
    assert not store.journal_path.exists()
    assert list(store.journal_archive_dir.glob("opportunity_journal_*.jsonl"))


def test_corrupt_journal_tail_still_fails_closed(tmp_path):
    store = SpotAccumulationStore(str(tmp_path))
    store.append_journal(_event(1))
    with open(store.journal_path, "a", encoding="utf-8") as f:
        f.write("{not-json}\n")

    with pytest.raises(SpotStorageCorruption):
        store.latest_journal_runtime()


def test_facts_retention_prunes_compact_and_raw_archives(tmp_path):
    store = SpotAccumulationStore(str(tmp_path))
    now = int(time.time())
    store.append_compact_facts_snapshot({"timestamp": now - 200 * 86400}, now - 200 * 86400)
    store.append_raw_facts_snapshot({"timestamp": now - 5 * 86400}, now - 5 * 86400)
    store.append_compact_facts_snapshot({"timestamp": now}, now)
    store.append_raw_facts_snapshot({"timestamp": now}, now)

    compact_days = sorted(path.stem for path in store.compact_facts_dir.glob("*.jsonl"))
    raw_days = sorted(path.stem for path in store.raw_facts_dir.glob("*.jsonl"))
    today = time.strftime("%Y%m%d", time.gmtime(now))

    assert compact_days == [today]
    assert raw_days == [today]
    assert len(store.load_facts_snapshots()) == 1


def test_legacy_monthly_facts_are_not_loaded_unless_requested(tmp_path):
    store = SpotAccumulationStore(str(tmp_path))
    (store.root / "facts_2026-07.jsonl").write_text(
        json.dumps({"archive_schema_version": 3, "timestamp": 1}) + "\n", encoding="utf-8",
    )
    now = int(time.time())
    store.append_compact_facts_snapshot({"archive_schema_version": 4, "timestamp": now}, now)

    assert len(store.load_facts_snapshots()) == 1
    assert len(store.load_facts_snapshots(include_legacy=True)) == 2


def test_daily_rollup_is_not_mistaken_for_a_legacy_monthly_archive(tmp_path):
    """facts_daily.jsonl 同在根目录且匹配 facts_*.jsonl，不得混入旧归档口径。"""
    store = SpotAccumulationStore(str(tmp_path))
    now = int(time.time())
    store.append_compact_facts_snapshot({"archive_schema_version": 4, "timestamp": now}, now)
    store.append_daily_facts_rollup({"day": "20260810", "sample_count": 2})

    assert store.storage_stats()["legacy_monthly_facts"] == {"files": 0, "bytes": 0}
    assert store.storage_stats()["daily_facts"]["files"] == 1
    assert len(store.load_facts_snapshots(include_legacy=True)) == 1


def test_daily_rollup_is_written_once_per_day_and_bounded(tmp_path):
    store = SpotAccumulationStore(str(tmp_path))
    day = "20260810"
    assert store.append_daily_facts_rollup({"day": day, "sample_count": 2}) is True
    assert store.append_daily_facts_rollup({"day": day, "sample_count": 2}) is False
    assert store.append_daily_facts_rollup({"day": "20240101", "sample_count": 1}) is True

    days = sorted(store.daily_rollup_days())
    # 400 天以外的记录不得留在日汇总文件里
    assert days == [day]


def test_service_rolls_up_previous_day_from_compact_archive(tmp_path):
    store = SpotAccumulationStore(str(tmp_path))
    yesterday = int(time.time()) - 86400
    day_key = time.strftime("%Y%m%d", time.gmtime(yesterday))
    for index in range(3):
        store.append_compact_facts_snapshot({
            "archive_schema_version": 4,
            "record_type": "spot_accumulation_full_fact_snapshot",
            "timestamp": yesterday + index * 300,
            "coin": "BTC",
            "policy_version": 3,
            "facts": {
                "price": 50_000 + index * 100,
                "drawdown_pct": 20.0 + index,
                "scores": {"valuation": 60 + index, "capital_flow": 50, "acceptance": 40},
            },
            "opportunities": [{"opportunity_id": "op-1", "status": "eligible"}],
            "opportunity_changes": [{"opportunity_id": "op-1", "from": None, "to": "eligible"}],
            "portfolio": {"total_btc": 0.5, "average_cost_usdt": 49_000},
        }, yesterday + index * 300)

    service = SpotAccumulationService(str(tmp_path), lambda: None)
    service._rollup_previous_day(int(time.time()))

    rollups = store.load_daily_facts_rollups()
    assert len(rollups) == 1
    rollup = rollups[0]
    assert rollup["day"] == day_key
    assert rollup["record_type"] == "spot_accumulation_daily_rollup"
    assert rollup["sample_count"] == 3
    assert rollup["price"] == {"first": 50_000, "last": 50_200, "min": 50_000, "max": 50_200}
    assert rollup["drawdown_pct"]["max"] == 22.0
    assert rollup["scores"]["valuation"] == {"min": 60, "max": 62, "last": 62}
    assert rollup["opportunity_status_counts"] == {"eligible": 1}
    assert rollup["opportunity_change_count"] == 3
    assert rollup["portfolio"]["total_btc"] == 0.5

    # 同一天重复触发保持幂等
    service._last_rollup_check_day = ""
    service._rollup_previous_day(int(time.time()))
    assert len(store.load_daily_facts_rollups()) == 1
