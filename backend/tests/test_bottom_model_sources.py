"""BGeometrics / YahooCME 源：响应解析与双窗口配额限流。"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sources.bgeometrics import _DualWindowLimiter, parse_bg_rows
from sources.yahoo_cme import parse_weekly_chart

WEEK = 7 * 86400


# ── BGeometrics 解析 ──

def test_parse_bg_rows_auto_value_key():
    rows = [
        {"d": "2026-08-10", "unixTs": "1786665600", "mvrvZscore": "0.18"},
        {"d": "2026-08-09", "unixTs": "1786579200", "mvrvZscore": 0.22},
    ]
    assert parse_bg_rows(rows) == [("2026-08-09", 0.22), ("2026-08-10", 0.18)]


def test_parse_bg_rows_tolerates_junk():
    rows = [
        {"d": "2026-08-10", "unixTs": 1, "sopr": None},   # 无值
        {"d": "bad", "sopr": 1.0},                         # 非法日期
        "not-a-dict",
        {"d": "2026-08-11", "unixTs": 1, "sopr": 1.01},
    ]
    assert parse_bg_rows(rows) == [("2026-08-11", 1.01)]
    assert parse_bg_rows({"error": "x"}) == []


# ── BGeometrics 双窗口限流 ──

def test_dual_window_limiter_hourly_then_daily(monkeypatch):
    clock = {"now": 1000.0}
    monkeypatch.setattr("sources.bgeometrics.time.monotonic", lambda: clock["now"])
    limiter = _DualWindowLimiter(hourly_limit=2, daily_limit=3)
    assert limiter.try_acquire() and limiter.try_acquire()
    # 小时窗口耗尽
    assert not limiter.try_acquire()
    # 1 小时后小时窗口恢复，但日窗口只剩 1 次
    clock["now"] += 3601
    assert limiter.try_acquire()
    assert not limiter.try_acquire()
    # 24 小时后日窗口恢复
    clock["now"] += 86401
    assert limiter.try_acquire()
    snap = limiter.snapshot()
    assert snap["hourly_used"] == 1 and snap["daily_used"] == 1


# ── Yahoo 周线解析 ──

def _yahoo_payload(timestamps, closes, volumes):
    return {"chart": {"result": [{
        "timestamp": timestamps,
        "indicators": {"quote": [{"close": closes, "volume": volumes}]},
    }]}}


def test_parse_weekly_chart_drops_incomplete_week():
    now = 2_000_000_000
    ts = [now - 3 * WEEK, now - 2 * WEEK, now - WEEK // 2]  # 最后一根为进行中的周
    payload = _yahoo_payload(ts, [50000.0, 48000.0, 47000.0], [1000, 2000, 500])
    rows = parse_weekly_chart(payload, now_ts=now)
    assert [r["week_start_ts"] for r in rows] == ts[:2]
    assert rows[1] == {"week_start_ts": ts[1], "close": 48000.0, "volume": 2000.0}


def test_parse_weekly_chart_skips_null_and_bad_payload():
    now = 2_000_000_000
    ts = [now - 3 * WEEK, now - 2 * WEEK]
    payload = _yahoo_payload(ts, [None, 48000.0], [1000, None])
    assert parse_weekly_chart(payload, now_ts=now) == []
    assert parse_weekly_chart({"chart": {"result": []}}, now_ts=now) == []
    assert parse_weekly_chart(None, now_ts=now) == []
