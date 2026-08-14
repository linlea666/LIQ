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


def _alert(day: str, *, now: int = 1_000) -> dict:
    return {
        "alert_id": f"alert-{day}",
        "model_id": "bottom-v5",
        "data_policy_id": "pit-final-v2",
        "state": "confirmed_recovery",
        "status": "PENDING",
        "subject": "subject",
        "html_body": "<p>body</p>",
        "created_at": now,
        "next_attempt_at": now,
        "expires_at": now + 86400,
    }


def test_snapshot_and_bottom_alert_are_atomic_and_deduplicated(tmp_path):
    store = _store(tmp_path)
    day = "2026-08-12"
    assert store.save_snapshot_with_bottom_alert(day, {"day": day}, _alert(day)) is True
    assert store.save_snapshot_with_bottom_alert(day, {"day": day}, _alert(day)) is False
    assert store.bottom_alert_health()["pending"] == 1

    broken = _alert("2026-08-13")
    del broken["subject"]
    try:
        store.save_snapshot_with_bottom_alert("2026-08-13", {"day": "2026-08-13"}, broken)
    except KeyError:
        pass
    else:
        raise AssertionError("malformed alert must abort the transaction")
    assert all(item["day"] != "2026-08-13" for item in store.snapshot_history())
    store.close()


def test_bottom_alert_retry_schedule_expiry_and_config_defer(tmp_path):
    store = _store(tmp_path)
    day = "2026-08-12"
    store.save_snapshot_with_bottom_alert(day, {"day": day}, _alert(day))
    item = store.claim_due_bottom_alerts(now=1_000)[0]
    store.defer_bottom_alert(item["alert_id"], "config missing", now=1_000)
    row = store._conn.execute(
        "SELECT * FROM bottom_email_outbox WHERE alert_id=?", (item["alert_id"],),
    ).fetchone()
    assert row["attempts"] == 0 and row["next_attempt_at"] == 1_600

    expected = [(1, 2_500, "PENDING"), (2, 7_000, "PENDING"),
                (3, 18_800, "PENDING"), (4, 8_000, "FAILED")]
    for attempt, next_at, status in expected:
        now = [1_600, 3_400, 8_000, 8_000][attempt - 1]
        store.mark_bottom_alert_failed(item["alert_id"], "smtp failed", now=now)
        row = store._conn.execute(
            "SELECT * FROM bottom_email_outbox WHERE alert_id=?", (item["alert_id"],),
        ).fetchone()
        assert (row["attempts"], row["next_attempt_at"], row["status"]) == (
            attempt, next_at, status,
        )

    expired = _alert("2026-08-13", now=2_000)
    expired["expires_at"] = 2_100
    store.save_snapshot_with_bottom_alert("2026-08-13", {"day": "2026-08-13"}, expired)
    assert store.expire_bottom_alerts(now=2_100) == 1
    assert store.bottom_alert_health()["expired"] == 1
    store.close()


def test_bottom_alert_survives_restart_and_prunes_with_snapshots(tmp_path):
    root = tmp_path / "bm"
    store = BottomModelStore(str(root))
    old_day = time.strftime("%Y-%m-%d", time.gmtime(time.time() - 900 * 86400))
    store.save_snapshot_with_bottom_alert(old_day, {"day": old_day}, _alert(old_day))
    store.close()

    reopened = BottomModelStore(str(root))
    assert reopened.bottom_alert_health()["pending"] == 1
    assert reopened.save_snapshot_with_bottom_alert(old_day, {"day": old_day}, _alert(old_day)) is False
    reopened.prune(snapshot_retention_days=800)
    assert reopened.bottom_alert_health()["pending"] == 0
    reopened.close()


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


def test_latest_audit_matching_uses_full_lineage_key(tmp_path):
    store = _store(tmp_path)
    store.save_audit("old-exact", {
        "audit_id": "old-exact", "model_id": "bottom-v5",
        "data_policy_id": "pit-final-v2", "dataset_id": "data-a",
    }, "# old")
    store.save_audit("new-wrong-dataset", {
        "audit_id": "new-wrong-dataset", "model_id": "bottom-v5",
        "data_policy_id": "pit-final-v2", "dataset_id": "data-b",
    }, "# wrong")
    matched = store.latest_audit_matching("bottom-v5", "pit-final-v2", "data-a")
    assert matched is not None and matched["audit_id"] == "old-exact"
    assert store.latest_audit_matching("bottom-v5", "pit-final-v1", "data-a") is None
    assert store.latest_audit_matching("bottom-v4", "pit-final-v2", "data-a") is None
    store.close()
