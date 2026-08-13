"""BottomModelStore：序列幂等写入、采集账本、快照保留裁剪。"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from storage.bottom_model_store import BottomModelStore


def _store(tmp_path) -> BottomModelStore:
    return BottomModelStore(str(tmp_path / "bm"))


def test_upsert_series_idempotent_and_ordered(tmp_path):
    store = _store(tmp_path)
    n = store.upsert_series("nupl", [("2026-08-10", 0.2), ("2026-08-09", 0.1)])
    assert n == 2
    # 重复写入 + 值更新：幂等，不产生重复行
    store.upsert_series("nupl", [("2026-08-10", 0.25)])
    rows = store.series("nupl")
    assert rows == [("2026-08-09", 0.1), ("2026-08-10", 0.25)]
    assert store.latest_day("nupl") == "2026-08-10"
    store.close()


def test_upsert_series_skips_invalid_rows(tmp_path):
    store = _store(tmp_path)
    n = store.upsert_series("m", [
        ("2026-08-10", 1.0),
        ("bad-day", 2.0),          # 非法日期长度
        ("2026-08-11", "oops"),    # 非数值
    ])
    assert n == 1
    assert store.series("m") == [("2026-08-10", 1.0)]
    store.close()


def test_series_limit_returns_recent_ascending(tmp_path):
    store = _store(tmp_path)
    store.upsert_series("m", [(f"2026-08-{d:02d}", float(d)) for d in range(1, 11)])
    rows = store.series("m", limit=3)
    assert rows == [("2026-08-08", 8.0), ("2026-08-09", 9.0), ("2026-08-10", 10.0)]
    store.close()


def test_fetch_log_success_day_monotonic(tmp_path):
    store = _store(tmp_path)
    store.record_fetch("nupl", ok=True, rows=100, success_day="2026-08-10")
    # 失败不回退 success_day
    store.record_fetch("nupl", ok=False, error="boom")
    assert store.last_success_day("nupl") == "2026-08-10"
    log = store.fetch_log()["nupl"]
    assert log["last_ok"] is False and log["last_error"] == "boom"
    # 更新到更新的成功日
    store.record_fetch("nupl", ok=True, rows=100, success_day="2026-08-11")
    assert store.last_success_day("nupl") == "2026-08-11"
    # 旧成功日不覆盖新成功日（乱序写入防御）
    store.record_fetch("nupl", ok=True, rows=100, success_day="2026-08-01")
    assert store.last_success_day("nupl") == "2026-08-11"
    store.close()


def test_snapshot_roundtrip_and_prune(tmp_path):
    store = _store(tmp_path)
    old_day = time.strftime("%Y-%m-%d", time.gmtime(time.time() - 900 * 86400))
    today = time.strftime("%Y-%m-%d", time.gmtime())
    store.save_snapshot(old_day, {"day": old_day, "stress": 10})
    store.save_snapshot(today, {"day": today, "stress": 70})
    assert store.latest_snapshot()["stress"] == 70
    store.prune(snapshot_retention_days=800)
    history = store.snapshot_history()
    assert [item["day"] for item in history] == [today]
    store.close()


def test_coverage(tmp_path):
    store = _store(tmp_path)
    store.upsert_series("a", [("2026-08-01", 1.0), ("2026-08-02", 2.0)])
    cov = store.coverage()
    assert cov["a"] == {"first_day": "2026-08-01", "last_day": "2026-08-02", "count": 2}
    store.close()


def test_append_observations_is_idempotent_and_versions_revisions(tmp_path):
    store = _store(tmp_path)
    row = [("2026-08-10", 1.0)]
    assert store.append_observations("m", row, source="source", cadence="daily", unit="usd") == 1
    assert store.append_observations("m", row, source="source", cadence="daily", unit="usd") == 0
    assert store.append_observations("m", [("2026-08-10", 2.0)], source="source", cadence="daily", unit="usd") == 1
    revisions = store._conn.execute(
        "SELECT revision,value FROM observations WHERE metric='m' ORDER BY revision",
    ).fetchall()
    assert [(row["revision"], row["value"]) for row in revisions] == [(1, 1.0), (2, 2.0)]
    latest = store.observation_meta("m", as_of_day="2026-08-10")
    assert latest is not None
    assert latest["available_at"] >= latest["ingested_at"]
    assert store.observation_meta("m", as_of_day="2026-08-09") is None
    store.close()


def test_model_runs_are_append_only(tmp_path):
    store = _store(tmp_path)
    base = {
        "model_id": "bottom-v4", "data_policy_id": "pit-final-v2",
        "dataset_id": "data", "decision_as_of": "2026-08-13T01:00:00Z",
        "quality_status": "OK",
    }
    store.save_model_run({**base, "run_id": "run-a"})
    store.save_model_run({**base, "run_id": "run-b"})
    assert store._conn.execute("SELECT COUNT(*) FROM model_runs").fetchone()[0] == 2
    store.close()
