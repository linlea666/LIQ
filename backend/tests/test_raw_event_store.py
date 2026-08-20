from __future__ import annotations

import time

import pytest

from storage.raw_event_store import RawEventStore


def test_stop_flushes_partial_tail_batch(tmp_path, monkeypatch) -> None:
    store = RawEventStore(str(tmp_path), queue_max=1000, batch_size=100)
    store._pyarrow_available = True
    written: list[dict] = []
    monkeypatch.setattr(store, "_write_batch", lambda rows: written.extend(rows))

    store.start()
    now = int(time.time())
    assert store.append({
        "coin": "BTC", "market": "spot", "event_time": now,
        "decision_time": now,
    })
    store.stop()

    assert len(written) == 1
    assert store.health()["written"] == 1


def test_real_parquet_zstd_and_aggregates_are_readable(tmp_path) -> None:
    parquet = pytest.importorskip("pyarrow.parquet")
    store = RawEventStore(str(tmp_path), queue_max=1000, batch_size=100)
    store.start()
    assert store.health()["format"] == "parquet-zstd"
    for sequence, side in ((10, "buy"), (11, "sell")):
        assert store.append({
            "coin": "BTC", "market": "spot", "event_time": 1_700_000_001,
            "decision_time": 1_700_000_001,
            "source_sequence": sequence, "aggressor_side": side,
            "quote_notional": 100.0,
        })
    store.stop()
    raw_files = list(tmp_path.glob("raw/**/*.parquet"))
    one_second_files = list(tmp_path.glob("aggregate_1s/**/*.parquet"))
    five_second_files = list(tmp_path.glob("aggregate_5s/**/*.parquet"))
    assert len(raw_files) == len(one_second_files) == len(five_second_files) == 1
    # 单文件读取用 ParquetFile，避免把父目录的 Hive 分区字段与文件内审计字段
    # 重复推断成不同 dictionary/string 类型。
    assert parquet.ParquetFile(raw_files[0]).read().num_rows == 2
    aggregate = parquet.ParquetFile(one_second_files[0]).read().to_pylist()[0]
    assert aggregate["aggressor_buy_quote"] == 100.0
    assert aggregate["aggressor_sell_quote"] == 100.0
    assert aggregate["first_sequence"] == 10
    assert aggregate["last_sequence"] == 11


def test_restart_duplicate_trade_does_not_double_aggregate(tmp_path) -> None:
    pytest.importorskip("pyarrow.parquet")
    first = RawEventStore(str(tmp_path), queue_max=1000, batch_size=100)
    first._write_batch([{
        "coin": "BTC", "market": "spot", "event_time": 1_700_000_001,
        "source_sequence": 10, "aggressor_side": "buy", "quote_notional": 100.0,
    }])
    second = RawEventStore(str(tmp_path), queue_max=1000, batch_size=100)
    second._started_at = first._started_at + 1
    second._write_batch([{
        "coin": "BTC", "market": "spot", "event_time": 1_700_000_001,
        "source_sequence": 10, "aggressor_side": "buy", "quote_notional": 100.0,
    }, {
        "coin": "BTC", "market": "spot", "event_time": 1_700_000_001,
        "source_sequence": 11, "aggressor_side": "sell", "quote_notional": 100.0,
    }])
    aggregate = second.read_aggregates(
        "aggregate_1s", "BTC", "spot", 1_700_000_001, 1_700_000_001,
    )[0]
    assert aggregate["aggressor_buy_quote"] == 100.0
    assert aggregate["aggressor_sell_quote"] == 100.0
    assert aggregate["trade_count"] == 2


def test_one_atomic_write_per_closed_segment(tmp_path, monkeypatch) -> None:
    store = RawEventStore(str(tmp_path), queue_max=1000, batch_size=100, segment_sec=300)
    store._pyarrow_available = True
    batches: list[list[dict]] = []
    monkeypatch.setattr(store, "_write_batch", lambda rows: batches.append(list(rows)))
    store.start()
    segment_start = int(time.time()) // 300 * 300
    for event_time in range(segment_start, segment_start + 100):
        assert store.append({
            "coin": "BTC", "market": "spot", "event_time": event_time,
            "decision_time": event_time, "quote_notional": 1,
        })
    store.stop()
    assert len(batches) == 1
    assert len(batches[0]) == 100


def test_non_configured_coin_is_not_collected(tmp_path) -> None:
    store = RawEventStore(str(tmp_path), allowed_coins=("BTC",))
    store._running = True
    assert store.append({"coin": "ETH", "market": "spot", "event_time": 1}) is False
    assert store._queue.empty()


def test_backlogged_closed_segment_uses_batch_size_but_commits_once(tmp_path, monkeypatch) -> None:
    store = RawEventStore(str(tmp_path), queue_max=1000, batch_size=100, segment_sec=300)
    store._pyarrow_available = True
    batches: list[list[dict]] = []
    monkeypatch.setattr(store, "_write_batch", lambda rows: batches.append(list(rows)))
    store._running = True
    segment_start = int(time.time()) // 300 * 300 - 300
    for sequence in range(250):
        event_time = segment_start + sequence % 300
        assert store.append({
            "coin": "BTC", "market": "spot", "event_time": event_time,
            "decision_time": event_time, "source_sequence": sequence,
        })
    store._running = False
    store._worker()
    assert len(batches) == 1
    assert len(batches[0]) == 250
    assert store.health()["segments_committed"] == 1


def test_persisted_drop_counter_survives_restart(tmp_path) -> None:
    saved: dict = {}
    first = RawEventStore(
        str(tmp_path), state_saver=lambda payload: saved.update(payload),
    )
    first._running = True
    assert first.append({
        "coin": "BTC", "market": "spot", "event_time": 20,
        "decision_time": 10,
    }) is False
    first._persist_state(force=True)
    second = RawEventStore(str(tmp_path), state_loader=lambda: saved)
    assert second.health()["dropped"] == 1


def test_append_gap_persistence_is_deferred_off_producer_path(tmp_path) -> None:
    persisted: list[dict] = []
    store = RawEventStore(str(tmp_path), gap_sink=persisted.append)
    store._running = True
    assert store.append({
        "coin": "BTC", "market": "spot", "event_time": 20,
        "decision_time": 10,
    }) is False
    assert persisted == []
    store._drain_gap_markers()
    assert persisted[0]["reason"] == "raw_event_pit_violation"


def test_hot_resource_refresh_does_not_walk_entire_tree(tmp_path, monkeypatch) -> None:
    store = RawEventStore(str(tmp_path))
    store._resource_cache = (1, {
        "file_count": 10_000, "total_bytes": 123,
        "free_bytes": 456, "free_inodes": 789,
    })

    def fail_walk(*_args, **_kwargs):
        raise AssertionError("hot path must not scan the parquet tree")

    monkeypatch.setattr("storage.raw_event_store.os.walk", fail_walk)
    stats = store._resource_stats()
    assert stats["file_count"] == 10_000
    assert stats["total_bytes"] == 123
