"""Bottom Model 历史频率层：样本量口径、回放缓存与版本失效、条件统计。"""
from __future__ import annotations

import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from processors.bottom_model import base_rate as br
from processors.bottom_model.base_rate import (
    _ForwardPrice,
    _window_stats,
    compute_base_rate,
    count_independent,
    count_segments,
    load_or_build_replay,
    replay_days,
)
from processors.bottom_model.snapshot import ALGORITHM_VERSION, compute_core
from storage.bottom_model_store import BottomModelStore
from tests.test_bottom_model_factors import _bottomish_data, mk

END = date(2026, 8, 11)


def _days(*offsets: int) -> list[str]:
    return [(END - timedelta(days=n)).strftime("%Y-%m-%d") for n in offsets]


# ── 样本量口径 ──

def test_count_segments_merges_within_gap():
    # 三个相邻 7 天的点属于同一段；隔 200 天的点另起一段
    assert count_segments(_days(214, 207, 200, 14, 7, 0)) == 2
    assert count_segments([]) == 0
    assert count_segments(_days(0)) == 1


def test_count_independent_excludes_overlapping_windows():
    """周级连续的时点在 26 周窗口下几乎全部重叠，有效样本远小于时点数。"""
    weekly = _days(*[7 * i for i in range(52)])       # 一年 52 个周点
    assert len(weekly) == 52
    # 52 个点跨 51 周，26 周窗口下只容得下 2 个互不重叠的观测
    assert count_independent(weekly, 26) == 2
    assert count_independent(weekly, 13) == 4
    # 段数口径会把这 52 个点算成 1 段——两个口径回答的是不同问题
    assert count_segments(weekly) == 1


def test_window_stats_withholds_frequency_when_sample_too_small():
    """样本不足时不输出胜率与中位数，但照实给出最差一次作为风险线索。"""
    price = mk([100.0] * 200 + [300.0] * 200)
    fwd = _ForwardPrice(price)
    rows = [{"day": d} for d in _days(210, 203)]      # 仅 2 个相邻时点
    stats = _window_stats(rows, 13, fwd)
    assert stats["points"] == 2 and stats["independent"] == 1
    assert stats["reliable"] is False
    assert stats["hit_rate"] is None and stats["median_return"] is None
    assert stats["worst_return"] is not None

    spread = [{"day": d} for d in _days(*[210 - 30 * i for i in range(6)])]
    ok = _window_stats(spread, 4, fwd)
    assert ok["reliable"] is True and ok["hit_rate"] is not None


def test_forward_price_excludes_unfinished_window():
    price = mk([100.0 * (1 + i / 100) for i in range(200)])
    fwd = _ForwardPrice(price)
    early = (END - timedelta(days=190)).strftime("%Y-%m-%d")
    assert fwd.ret(early, 4) is not None
    # 最后一天起算的 13 周窗口尚未走完 → 不外推，直接排除
    assert fwd.ret(END.strftime("%Y-%m-%d"), 13) is None


def test_replay_days_weekly_cadence():
    data = {"btc_price_onchain": mk([1.0] * 100)}
    days = replay_days(data, (END - timedelta(days=28)).strftime("%Y-%m-%d"), 7)
    assert days == _days(28, 21, 14, 7, 0)
    assert replay_days({}, "2013-01-01", 7) == []


# ── 回放缓存与版本失效 ──

def _fake_core(calls: list):
    def core(data, as_of=None):
        calls.append(as_of)
        return {
            "stress": {"score": 60.0},
            "confirmation": {"score": 40.0},
            "quadrant": {"key": "basing"},
        }
    return core


def test_replay_is_cached_and_invalidated_by_version(tmp_path, monkeypatch):
    monkeypatch.setattr(br, "REPLAY_START", (END - timedelta(days=140)).strftime("%Y-%m-%d"))
    store = BottomModelStore(str(tmp_path / "bm"))
    data = {"btc_price_onchain": mk([100.0] * 200)}

    calls: list = []
    rows = load_or_build_replay(store, data, "test-v1", _fake_core(calls))
    assert len(rows) == 21 and len(calls) == 21
    assert all(row["quadrant"] == "basing" for row in rows)

    # 同版本重复调用命中缓存，不重算
    calls.clear()
    assert load_or_build_replay(store, data, "test-v1", _fake_core(calls)) == rows
    assert calls == []

    # 算法版本变化 → 全量重算并覆盖旧行
    calls.clear()
    load_or_build_replay(store, data, "test-v2", _fake_core(calls))
    assert len(calls) == 21
    assert store.replay_versions() == {"test-v2": 21}
    store.close()


def test_replay_only_computes_new_days(tmp_path, monkeypatch):
    monkeypatch.setattr(br, "REPLAY_START", (END - timedelta(days=70)).strftime("%Y-%m-%d"))
    store = BottomModelStore(str(tmp_path / "bm"))
    short = {"btc_price_onchain": mk([100.0] * 100)[:-14]}
    calls: list = []
    first = load_or_build_replay(store, short, "test-v1", _fake_core(calls))
    assert len(calls) == len(first)

    calls.clear()
    full = {"btc_price_onchain": mk([100.0] * 100)}
    second = load_or_build_replay(store, full, "test-v1", _fake_core(calls))
    assert len(second) == len(first) + 2      # 多出两个周级时点
    assert len(calls) == 2                    # 只算新增的那两个
    store.close()


def test_replay_stores_null_scores_without_retrying(tmp_path, monkeypatch):
    """算不出分数的早年时点也要入表，否则每次增量都会重试它们。"""
    monkeypatch.setattr(br, "REPLAY_START", (END - timedelta(days=28)).strftime("%Y-%m-%d"))
    store = BottomModelStore(str(tmp_path / "bm"))
    data = {"btc_price_onchain": mk([100.0] * 60)}
    calls: list = []

    def core(payload, as_of=None):
        calls.append(as_of)
        return {"stress": None, "confirmation": {"score": None}, "quadrant": {"key": ""}}

    rows = load_or_build_replay(store, data, "test-v1", core)
    assert rows and all(row["stress"] is None for row in rows)
    calls.clear()
    load_or_build_replay(store, data, "test-v1", core)
    assert calls == []
    store.close()


# ── 端到端 ──

def test_compute_base_rate_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setattr(br, "REPLAY_START", "2021-01-01")
    monkeypatch.setattr(br, "REPLAY_STEP_DAYS", 28)      # 压缩点数，保持测试秒级
    store = BottomModelStore(str(tmp_path / "bm"))
    data = _bottomish_data(n=2200)
    core = compute_core(data, "2026-08-11")
    monkeypatch.setattr(br, "MIN_INDEPENDENT", 3)
    monkeypatch.setattr(br, "MIN_REPLAY_POINTS", 50)   # 合成数据只有 74 个月级点

    result = compute_base_rate(store, data, core, ALGORITHM_VERSION, compute_core)
    assert result is not None
    assert result["algorithm_version"] == ALGORITHM_VERSION
    assert result["replay"]["points"] >= 50
    assert result["baseline"]["windows"][0]["weeks"] == 13
    assert result["baseline"]["windows"][0]["hit_rate"] is not None
    assert len(result["conditions"]) >= 1
    assert result["conditions"][0]["label"].startswith("当前象限")
    assert len(result["stress_ladder"]) == 4
    assert len(result["confirmation_ladder"]) == 4
    assert result["caveats"]
    for cond in [result["baseline"], *result["conditions"]]:
        for window in cond["windows"]:
            # 不可靠的格子一律不带频率，避免任何消费方误读
            if not window["reliable"]:
                assert window["hit_rate"] is None
                assert window["median_return"] is None
    store.close()


def test_compute_base_rate_abstains_without_history(tmp_path):
    store = BottomModelStore(str(tmp_path / "bm"))
    data = {"btc_price_onchain": mk([100.0] * 50)}
    assert compute_base_rate(store, data, {}, "test-v1", compute_core) is None
    store.close()
