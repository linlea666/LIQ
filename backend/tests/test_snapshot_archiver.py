"""P2.4 · SnapshotArchiver 单测"""

from __future__ import annotations

import gzip
import json
import os
import time
from dataclasses import dataclass

import pytest
from pydantic import BaseModel

from processors.snapshot_archiver import (
    ReplayFrame,
    SnapshotArchiver,
    _day_key,
)


@pytest.fixture
def tmp_archiver(tmp_path):
    return SnapshotArchiver(root=str(tmp_path / "replay"), keep_days=30)


class _Model(BaseModel):
    coin: str
    price: float


@dataclass
class _DC:
    name: str
    value: int


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# append + read
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestAppend:
    def test_append_dict(self, tmp_archiver):
        ok = tmp_archiver.append(
            coin="btc",
            snapshot={"a": 1},
            execution_plan={"b": 2},
            price_at_capture=72500.0,
            ai_analysis_brief="测试 headline",
        )
        assert ok
        # 文件已创建
        days = tmp_archiver.list_days()
        assert len(days) == 1

    def test_append_pydantic(self, tmp_archiver):
        model = _Model(coin="ETH", price=4100)
        ok = tmp_archiver.append(
            coin="ETH",
            snapshot=model,
            execution_plan=None,
            ai_trader_report=model,
            final_decision=None,
        )
        assert ok

    def test_append_dataclass(self, tmp_archiver):
        ok = tmp_archiver.append(
            coin="BTC",
            snapshot=_DC(name="x", value=1),
        )
        assert ok

    def test_append_none_tolerated(self, tmp_archiver):
        ok = tmp_archiver.append(coin="BTC", snapshot=None)
        assert ok

    def test_brief_truncated(self, tmp_archiver):
        long = "x" * 500
        tmp_archiver.append(coin="BTC", snapshot={}, ai_analysis_brief=long)
        items = tmp_archiver.read_range("BTC")
        assert len(items[0]["ai_analysis_brief"]) <= 280


class TestRead:
    def test_read_range_latest_first(self, tmp_archiver):
        now = int(time.time())
        for i in range(3):
            tmp_archiver.append(
                coin="BTC",
                snapshot={"i": i},
                price_at_capture=72000 + i,
            )
            time.sleep(0.01)
        items = tmp_archiver.read_range("BTC")
        assert len(items) == 3
        # 新的在前
        assert items[0]["ts"] >= items[1]["ts"] >= items[2]["ts"]

    def test_filter_by_coin(self, tmp_archiver):
        tmp_archiver.append(coin="BTC", snapshot={})
        tmp_archiver.append(coin="ETH", snapshot={})
        tmp_archiver.append(coin="BTC", snapshot={})
        btc = tmp_archiver.read_range("BTC")
        eth = tmp_archiver.read_range("ETH")
        assert len(btc) == 2
        assert len(eth) == 1

    def test_filter_by_time_range(self, tmp_archiver):
        # 直接塞进不同日期：需手动写
        now = int(time.time())
        old = now - 86400 * 3
        # 一条旧的 + 一条新的
        path_old = tmp_archiver._path_for_day(_day_key(old))
        os.makedirs(os.path.dirname(path_old), exist_ok=True)
        with gzip.open(path_old, "at", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": old, "coin": "BTC", "snapshot": {}, "price_at_capture": 70000,
                "ai_analysis_brief": "旧数据",
            }) + "\n")
        tmp_archiver.append(coin="BTC", snapshot={})

        # 只查最近 1 天 → 只拿到新
        items = tmp_archiver.read_range(
            "BTC", since_ts=now - 86400,
        )
        assert all(it["ts"] >= now - 86400 for it in items)
        # 查旧窗口
        items_old = tmp_archiver.read_range(
            "BTC", since_ts=old - 1, until_ts=old + 1,
        )
        assert len(items_old) == 1
        assert items_old[0]["ts"] == old

    def test_limit_respected(self, tmp_archiver):
        for i in range(20):
            tmp_archiver.append(coin="BTC", snapshot={"i": i})
        items = tmp_archiver.read_range("BTC", limit=5)
        assert len(items) == 5

    def test_limit_returns_latest_not_oldest(self, tmp_archiver):
        """单日写了 20 帧，limit=5 必须返回最新 5 帧（ts 最大的），
        而非最老 5 帧——这是之前 read_range 的顺序 bug。"""
        import time as _t
        base = int(_t.time()) - 100
        for i in range(20):
            tmp_archiver.append(
                coin="BTC",
                snapshot={"seq": i},
                price_at_capture=base + i,
            )
        items = tmp_archiver.read_range("BTC", limit=5)
        assert len(items) == 5
        ts_list = [it["ts"] for it in items]
        # 必须降序（最新在前）
        assert ts_list == sorted(ts_list, reverse=True)
        # 不能全都是最老的（price_at_capture ≈ base）
        max_price = max(it["price_at_capture"] for it in items)
        assert max_price > base + 5

    def test_read_frame_exact(self, tmp_archiver):
        tmp_archiver.append(
            coin="BTC", snapshot={"k": "v"},
            execution_plan={"action": "long"},
        )
        items = tmp_archiver.read_range("BTC")
        ts = items[0]["ts"]
        frame = tmp_archiver.read_frame("BTC", ts)
        assert frame is not None
        assert frame["snapshot"] == {"k": "v"}
        assert frame["execution_plan"] == {"action": "long"}

    def test_read_frame_not_found(self, tmp_archiver):
        tmp_archiver.append(coin="BTC", snapshot={})
        assert tmp_archiver.read_frame("BTC", 1) is None
        assert tmp_archiver.read_frame("ETH", int(time.time())) is None

    def test_corrupt_line_skipped(self, tmp_archiver):
        path = tmp_archiver._path_for_day(_day_key(int(time.time())))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with gzip.open(path, "at", encoding="utf-8") as f:
            f.write("not-json\n")
            f.write(json.dumps({"ts": 1, "coin": "BTC", "snapshot": {}}) + "\n")
            f.write("{bad json\n")
        items = tmp_archiver.read_range("BTC")
        assert len(items) == 1


class TestGC:
    def test_gc_removes_old(self, tmp_path):
        arch = SnapshotArchiver(
            root=str(tmp_path / "replay"), keep_days=2,
        )
        # 手动制造 5 天文件
        today_ts = int(time.time())
        for back in range(5):
            day_ts = today_ts - back * 86400
            p = arch._path_for_day(_day_key(day_ts))
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with gzip.open(p, "at", encoding="utf-8") as f:
                f.write(json.dumps({"ts": day_ts, "coin": "BTC", "snapshot": {}}) + "\n")
        assert len(arch.list_days()) == 5
        # 强制触发 gc
        arch._last_gc_ts = 0
        arch._gc_if_due()
        # 只保留 2 天
        assert len(arch.list_days()) == 2


class TestFrameSerialization:
    def test_roundtrip(self):
        f = ReplayFrame(
            ts=123, coin="BTC",
            snapshot={"a": 1}, execution_plan={"b": 2},
            ai_trader_report=None, final_decision={"c": 3},
            price_at_capture=72000, ai_analysis_brief="测试",
        )
        d = f.to_dict()
        f2 = ReplayFrame.from_dict(d)
        assert f2.ts == 123
        assert f2.coin == "BTC"
        assert f2.snapshot == {"a": 1}
        assert f2.execution_plan == {"b": 2}
        assert f2.ai_trader_report is None


class TestReadRangeFlags:
    def test_flags_reflect_presence(self, tmp_archiver):
        tmp_archiver.append(
            coin="BTC", snapshot={},
            execution_plan={"x": 1}, ai_trader_report={"y": 2},
            final_decision=None,
        )
        items = tmp_archiver.read_range("BTC")
        assert items[0]["has_plan"] is True
        assert items[0]["has_ai_report"] is True
        assert items[0]["has_final"] is False
