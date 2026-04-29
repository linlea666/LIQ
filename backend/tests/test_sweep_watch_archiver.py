"""W4-T1 阶段 4 · sweep_watch_archiver 单测。

覆盖：
  1. append_sweep_watch_frame 写入 + read_sweep_watch_frames 读取
  2. 多次 append → 多行（追加而非覆盖）
  3. 老文件清理（mtime 老于 7 天）
  4. 失败不抛出（snapshot=None / 不可序列化字段）
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from datetime import datetime, timedelta

from models.sweep_watch import (
    BrainSweepWatch,
    SweepWatchSide,
    SweepWatchTraceEntry,
)
from processors import sweep_watch_archiver as arch


def _make_snapshot(coin: str = "BTC") -> BrainSweepWatch:
    return BrainSweepWatch(
        coin=coin,
        last_price=75_000.0,
        ts_iso="2026-04-30T00:00:00Z",
        below=SweepWatchSide(
            direction="below",
            label="下方多头止损带",
            representative_zone_id="zb",
            representative_zone_label="测试区",
            price_band=(74_900.0, 75_100.0),
            distance_pct=-0.3,
            sweep_phase="approaching",
            sweep_attractiveness=0.5,
            reversal_potential=0.4,
            continuation_risk=0.6,
            triggers=["t1"],
            invalidations=["i1"],
        ),
        above=None,
        trace_log=[
            SweepWatchTraceEntry(
                ts_iso="2026-04-30T00:00:00Z",
                side="below",
                step="select_representative",
                inputs={"side": "below"},
                output={"zone_id": "zb"},
                rule_hit="closest_strong_role_zone",
                notes="选最近的强角色 zone",
            ),
        ],
    )


class TestAppendAndRead:
    def test_append_creates_file_and_read_returns_frame(self, tmp_path, monkeypatch):
        monkeypatch.setattr(arch, "_ROOT", str(tmp_path / "sweep_watch"))
        arch._CLEANUP_LAST_DATE.clear()

        snap = _make_snapshot("ETH")
        arch.append_sweep_watch_frame(snap)

        frames = arch.read_sweep_watch_frames("ETH")
        assert len(frames) == 1
        assert frames[0]["coin"] == "ETH"
        assert frames[0]["below"]["sweep_phase"] == "approaching"
        assert len(frames[0]["trace_log"]) == 1
        assert frames[0]["trace_log"][0]["step"] == "select_representative"

    def test_multiple_appends_produce_multiple_lines(self, tmp_path, monkeypatch):
        monkeypatch.setattr(arch, "_ROOT", str(tmp_path / "sweep_watch"))
        arch._CLEANUP_LAST_DATE.clear()

        for i in range(3):
            snap = _make_snapshot("BTC")
            arch.append_sweep_watch_frame(snap)

        frames = arch.read_sweep_watch_frames("BTC")
        assert len(frames) == 3

    def test_none_snapshot_no_crash(self, tmp_path, monkeypatch):
        monkeypatch.setattr(arch, "_ROOT", str(tmp_path / "sweep_watch"))
        arch._CLEANUP_LAST_DATE.clear()
        arch.append_sweep_watch_frame(None)  # type: ignore[arg-type]
        assert arch.read_sweep_watch_frames("BTC") == []


class TestCleanup:
    def test_cleanup_removes_old_files(self, tmp_path, monkeypatch):
        monkeypatch.setattr(arch, "_ROOT", str(tmp_path / "sweep_watch"))
        arch._CLEANUP_LAST_DATE.clear()

        # 造一个 8 天前的文件
        coin_dir = tmp_path / "sweep_watch" / "SOL"
        coin_dir.mkdir(parents=True)
        old_date = (datetime.now(tz=arch._TZ_CN) - timedelta(days=8)).strftime("%Y%m%d")
        old_path = coin_dir / f"{old_date}.jsonl"
        old_path.write_text("{}\n")
        assert old_path.exists()

        # 触发 append → cleanup
        snap = _make_snapshot("SOL")
        arch.append_sweep_watch_frame(snap)

        assert not old_path.exists()
        # 当日文件应仍然存在
        today = datetime.now(tz=arch._TZ_CN).strftime("%Y%m%d")
        assert (coin_dir / f"{today}.jsonl").exists()

    def test_cleanup_keeps_recent_files(self, tmp_path, monkeypatch):
        monkeypatch.setattr(arch, "_ROOT", str(tmp_path / "sweep_watch"))
        arch._CLEANUP_LAST_DATE.clear()

        coin_dir = tmp_path / "sweep_watch" / "DOGE"
        coin_dir.mkdir(parents=True)
        recent_date = (datetime.now(tz=arch._TZ_CN) - timedelta(days=3)).strftime("%Y%m%d")
        recent_path = coin_dir / f"{recent_date}.jsonl"
        recent_path.write_text("{}\n")

        snap = _make_snapshot("DOGE")
        arch.append_sweep_watch_frame(snap)

        assert recent_path.exists()

    def test_p1_5_cross_day_re_cleanup(self, tmp_path, monkeypatch):
        """P1#5 回归：长期运行进程跨日时仍要触发清理。

        模拟：进程昨天清过一次（_CLEANUP_LAST_DATE[coin] = 昨天日期），
        今天产生了一个 8 天前的旧文件 — 必须在今天的 append 时被清掉。
        修复前用 set 缓存"已清过的 coin"，跨日永不再清，旧文件累积。
        """
        monkeypatch.setattr(arch, "_ROOT", str(tmp_path / "sweep_watch"))
        # 模拟昨天清过
        yesterday = (datetime.now(tz=arch._TZ_CN) - timedelta(days=1)).strftime("%Y%m%d")
        arch._CLEANUP_LAST_DATE.clear()
        arch._CLEANUP_LAST_DATE["XRP"] = yesterday

        coin_dir = tmp_path / "sweep_watch" / "XRP"
        coin_dir.mkdir(parents=True)
        old_date = (datetime.now(tz=arch._TZ_CN) - timedelta(days=8)).strftime("%Y%m%d")
        old_path = coin_dir / f"{old_date}.jsonl"
        old_path.write_text("{}\n")

        snap = _make_snapshot("XRP")
        arch.append_sweep_watch_frame(snap)

        # 应被清掉（修复前会保留）；同时 _CLEANUP_LAST_DATE 应更新为今天
        assert not old_path.exists()
        assert arch._CLEANUP_LAST_DATE["XRP"] == datetime.now(tz=arch._TZ_CN).strftime("%Y%m%d")
