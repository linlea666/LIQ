"""Bottom Model 快照组装：端到端、截断类比、Δ 评分与数据质量标注。"""
from __future__ import annotations

import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from processors.bottom_model.snapshot import (
    build_snapshot,
    compute_core,
    truncate_asof,
)
from storage.bottom_model_store import BottomModelStore
from tests.test_bottom_model_factors import _bottomish_data, mk

END = date(2026, 8, 11)


def _seeded_store(tmp_path) -> BottomModelStore:
    store = BottomModelStore(str(tmp_path / "bm"))
    for metric, rows in _bottomish_data().items():
        store.upsert_series(metric, rows)
    return store


def test_truncate_asof():
    data = {"m": mk([1, 2, 3])}
    cut = truncate_asof(data, (END - timedelta(days=1)).strftime("%Y-%m-%d"))
    assert [v for _, v in cut["m"]] == [1.0, 2.0]


def test_build_snapshot_end_to_end(tmp_path):
    store = _seeded_store(tmp_path)
    snap = build_snapshot(store)
    assert snap["day"] == END.strftime("%Y-%m-%d")
    assert snap["algorithm_version"] == "bottom-v1"
    assert snap["stress"]["score"] > 55
    assert snap["confirmation"]["score"] is not None
    assert snap["quadrant"]["key"] in {"panic_flush", "basing", "confirmed_recovery"}
    assert len(snap["factors"]) == 6
    assert snap["price_context"]["price"] is not None
    # 类比：合成数据没有 2015-2022 历史 → 全部"共同因子不足"或被跳过
    assert isinstance(snap["analogs"], list)
    # 数据质量：注册表中未喂入的指标进 missing
    assert "puell_multiple" in snap["data_quality"]["missing"]
    # 快照已落库
    assert store.latest_snapshot()["day"] == snap["day"]
    store.close()


def test_snapshot_delta_uses_history(tmp_path):
    store = _seeded_store(tmp_path)
    ago_8d = (END - timedelta(days=8)).strftime("%Y-%m-%d")
    store.save_snapshot(ago_8d, {
        "day": ago_8d,
        "stress": {"score": 40.0},
        "confirmation": {"score": 10.0},
    })
    snap = build_snapshot(store)
    assert snap["delta"]["stress_7d"] is not None
    assert abs(snap["delta"]["stress_7d"] - (snap["stress"]["score"] - 40.0)) < 0.2
    assert snap["delta"]["stress_30d"] is None  # 30d 前无快照
    store.close()


def test_analogs_with_long_history(tmp_path):
    """给足跨越 2022-11 的历史后，FTX 底类比应产出相似度。"""
    store = BottomModelStore(str(tmp_path / "bm"))
    long_data = _bottomish_data(n=2200)   # 覆盖 2020-08 起 → 含 2022-11-21
    for metric, rows in long_data.items():
        store.upsert_series(metric, rows)
    snap = build_snapshot(store)
    ftx = next(a for a in snap["analogs"] if a["day"] == "2022-11-21")
    assert ftx["similarity"] is not None
    assert len(ftx["common_factors"]) >= 2
    store.close()


def test_compute_core_pure_function_no_side_effects():
    data = _bottomish_data()
    core1 = compute_core(data)
    core2 = compute_core(data)
    assert core1["stress"] == core2["stress"]
    assert core1["quadrant"] == core2["quadrant"]
