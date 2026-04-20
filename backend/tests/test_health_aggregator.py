"""P2.2 · HealthAggregator 单测"""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from monitoring.health_aggregator import HealthAggregator, _overall


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# overall 计算
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestOverall:
    def test_all_ok(self):
        assert _overall(ok=10, warn=0, failed=0, pending=0) == "ok"

    def test_any_fail(self):
        assert _overall(ok=10, warn=5, failed=1, pending=0) == "fail"

    def test_warn_without_fail(self):
        assert _overall(ok=10, warn=3, failed=0, pending=0) == "warn"

    def test_pending_only(self):
        assert _overall(ok=0, warn=0, failed=0, pending=5) == "pending"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# summary + events
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _mk_tracker_snapshot(items):
    """构造 DecisionTracker.get_summary_dict 的返回值"""
    return {
        "ts": int(time.time()),
        "decisions": [
            {
                "id": it["id"],
                "title": it.get("title", ""),
                "owner_module": "test",
                "status": it["status"],
                "detail": it.get("detail", ""),
                "metrics": it.get("metrics", {}),
                "last_update_ts": it.get("last_update_ts", int(time.time())),
                "last_ok_ts": it.get("last_ok_ts", 0),
                "last_warn_ts": it.get("last_warn_ts", 0),
                "last_fail_ts": it.get("last_fail_ts", 0),
                "total_marks": 1, "ok_count": 0, "warn_count": 0, "fail_count": 0,
            }
            for it in items
        ],
        "overall_health": "healthy",
    }


class TestSummary:
    def test_all_ok(self):
        agg = HealthAggregator()
        snap = _mk_tracker_snapshot([
            {"id": "D01", "status": "ok"},
            {"id": "D02", "status": "ok"},
        ])
        with patch(
            "utils.decision_tracker.get_tracker"
        ) as m:
            m.return_value.get_summary_dict.return_value = snap
            out = agg.summary()
        assert out["overall"] == "ok"
        assert out["counts"]["green"] == 2
        assert out["degraded"] == []

    def test_warn_and_fail(self):
        agg = HealthAggregator()
        snap = _mk_tracker_snapshot([
            {"id": "D01", "status": "ok", "last_ok_ts": int(time.time())},
            {"id": "D02", "status": "warn",
             "title": "Cascade Risk", "detail": "low sample",
             "metrics": {"sample_size": 3}},
            {"id": "D03", "status": "failed",
             "title": "Safety Gate", "detail": "api_degrade"},
        ])
        with patch("utils.decision_tracker.get_tracker") as m:
            m.return_value.get_summary_dict.return_value = snap
            out = agg.summary()
        assert out["overall"] == "fail"
        assert out["counts"]["yellow"] == 1
        assert out["counts"]["red"] == 1
        assert {d["id"] for d in out["degraded"]} == {"D02", "D03"}

    def test_empty_tracker_tolerated(self):
        agg = HealthAggregator()
        with patch("utils.decision_tracker.get_tracker",
                   side_effect=Exception("boom")):
            out = agg.summary()
        assert out["overall"] == "pending"


class TestEventDetection:
    def test_degrade_event_generated(self):
        agg = HealthAggregator()
        # 初始化 prev_status
        snap1 = _mk_tracker_snapshot([
            {"id": "D01", "status": "ok"},
        ])
        with patch("utils.decision_tracker.get_tracker") as m:
            m.return_value.get_summary_dict.return_value = snap1
            agg.observe()

        snap2 = _mk_tracker_snapshot([
            {"id": "D01", "status": "warn", "detail": "rate_limit"},
        ])
        with patch("utils.decision_tracker.get_tracker") as m:
            m.return_value.get_summary_dict.return_value = snap2
            events = agg.observe()

        assert len(events) == 1
        e = events[0]
        assert e["id"] == "D01"
        assert e["kind"] == "degrade"
        assert e["from"] == "ok"
        assert e["to"] == "warn"

    def test_recover_event(self):
        agg = HealthAggregator()
        snap1 = _mk_tracker_snapshot([{"id": "D01", "status": "failed"}])
        with patch("utils.decision_tracker.get_tracker") as m:
            m.return_value.get_summary_dict.return_value = snap1
            agg.observe()
        snap2 = _mk_tracker_snapshot([{"id": "D01", "status": "ok"}])
        with patch("utils.decision_tracker.get_tracker") as m:
            m.return_value.get_summary_dict.return_value = snap2
            events = agg.observe()
        assert events[0]["kind"] == "recover"

    def test_escalate_warn_to_fail(self):
        agg = HealthAggregator()
        snap1 = _mk_tracker_snapshot([{"id": "D01", "status": "warn"}])
        with patch("utils.decision_tracker.get_tracker") as m:
            m.return_value.get_summary_dict.return_value = snap1
            agg.observe()
        snap2 = _mk_tracker_snapshot([{"id": "D01", "status": "failed"}])
        with patch("utils.decision_tracker.get_tracker") as m:
            m.return_value.get_summary_dict.return_value = snap2
            events = agg.observe()
        assert events[0]["kind"] == "escalate"

    def test_no_event_when_unchanged(self):
        agg = HealthAggregator()
        snap = _mk_tracker_snapshot([{"id": "D01", "status": "warn"}])
        with patch("utils.decision_tracker.get_tracker") as m:
            m.return_value.get_summary_dict.return_value = snap
            agg.observe()
            events = agg.observe()
        assert events == []

    def test_events_capped_at_50(self):
        agg = HealthAggregator()
        # 初始化 ok
        snap1 = _mk_tracker_snapshot([{"id": f"D{i:02d}", "status": "ok"}
                                       for i in range(5)])
        with patch("utils.decision_tracker.get_tracker") as m:
            m.return_value.get_summary_dict.return_value = snap1
            agg.observe()

        # 生成大量变化
        for trigger in range(60):
            s = "warn" if trigger % 2 == 0 else "ok"
            snap = _mk_tracker_snapshot([{"id": f"D{i:02d}", "status": s}
                                          for i in range(5)])
            with patch("utils.decision_tracker.get_tracker") as m:
                m.return_value.get_summary_dict.return_value = snap
                agg.observe()
        out_snap = _mk_tracker_snapshot([{"id": f"D{i:02d}", "status": "ok"}
                                           for i in range(5)])
        with patch("utils.decision_tracker.get_tracker") as m:
            m.return_value.get_summary_dict.return_value = out_snap
            # 最终 summary 不抛
            out = agg.summary()
        assert len(out["events"]) <= 50

    def test_reset_for_testing(self):
        agg = HealthAggregator()
        snap = _mk_tracker_snapshot([{"id": "D01", "status": "ok"}])
        with patch("utils.decision_tracker.get_tracker") as m:
            m.return_value.get_summary_dict.return_value = snap
            agg.observe()
        assert agg._prev_status
        agg.reset_for_testing()
        assert agg._prev_status == {}
