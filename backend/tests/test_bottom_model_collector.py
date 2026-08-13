"""Bottom Model 采集器：解析器、目标日计算、日期戳去重与 fail-open。"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from processors.bottom_model.collector import BottomModelCollector, target_day_for
from processors.bottom_model.metrics import (
    FetchSpec,
    build_registry,
    parse_exchange_balance,
    parse_fear_greed,
    parse_spot_cvd,
    parse_stablecoin_mcap,
    parse_ts_rows,
)
from storage.bottom_model_store import BottomModelStore

DAY_MS = 86400_000
D0 = 1754870400_000  # 2025-08-11 00:00 UTC


# ── 解析器 ──

def test_parse_ts_rows_multi_output_and_str_values():
    raw = [
        {"time": D0, "close": "50000.5", "volume_usd": "1e9"},
        {"time": D0 + DAY_MS, "close": 51000, "volume_usd": None},
        {"time": 0, "close": 1},           # 非法时间戳
        "junk",
    ]
    out = parse_ts_rows(raw, {"c": "close", "v": "volume_usd"})
    assert out["c"] == [("2025-08-11", 50000.5), ("2025-08-12", 51000.0)]
    assert out["v"] == [("2025-08-11", 1e9)]


def test_parse_ts_rows_auto_skips_meta_keys():
    raw = [{"timestamp": D0, "price": 50000, "reserve_risk": 0.002}]
    out = parse_ts_rows(raw, {"reserve_risk": "auto"})
    assert out["reserve_risk"] == [("2025-08-11", 0.002)]


def test_parse_stablecoin_mcap_usd_units():
    raw = {
        "data_list": [{"USDT": 1.8e11, "USDC": 0.7e11, "note": "x"}],
        "time_list": [D0],
    }
    out = parse_stablecoin_mcap(raw)
    assert out["stablecoin_total_mcap"] == [("2025-08-11", 2.5e11)]
    # 异常小值（< $1B）跳过
    assert parse_stablecoin_mcap(
        {"data_list": [{"USDT": 1.0}], "time_list": [D0]}
    )["stablecoin_total_mcap"] == []


def test_parse_exchange_balance_sums_and_skips_none():
    raw = {
        "time_list": [D0, D0 + DAY_MS],
        "price_list": [60000, 61000],
        "data_map": {
            "Binance": [500000.0, 499000.0],
            "CoinEx": [None, 1000.0],
        },
    }
    out = parse_exchange_balance(raw)
    assert out["exchange_balance_btc"] == [
        ("2025-08-11", 500000.0), ("2025-08-12", 500000.0),
    ]
    assert parse_exchange_balance(None)["exchange_balance_btc"] == []


def test_parse_spot_cvd_nets_taker_and_drops_placeholder_zeros():
    """上游 2017-08 之前买卖量恒为 0，那是占位行而非"买卖平衡"的真实观测。"""
    raw = [
        {"time": D0, "agg_taker_buy_vol": 0, "agg_taker_sell_vol": 0},
        {"time": D0 + DAY_MS, "agg_taker_buy_vol": "3e8", "agg_taker_sell_vol": 1e8},
        {"time": D0 + 2 * DAY_MS, "agg_taker_buy_vol": 1e8, "agg_taker_sell_vol": 4e8},
        {"time": 0, "agg_taker_buy_vol": 1e8, "agg_taker_sell_vol": 1},   # 非法时间戳
        {"time": D0 + 3 * DAY_MS, "agg_taker_buy_vol": 1e8},              # 缺 sell
        "junk",
    ]
    assert parse_spot_cvd(raw)["spot_net_taker_usd"] == [
        ("2025-08-12", 2e8), ("2025-08-13", -3e8),
    ]
    assert parse_spot_cvd(None)["spot_net_taker_usd"] == []


def test_spot_demand_specs_registered():
    """两个现货需求指标必须在注册表里，否则采集器不会去取。"""
    by_key = {spec.key: spec for spec in build_registry()}
    assert by_key["coinbase_premium"].metrics == ("coinbase_premium_rate",)
    assert by_key["spot_cvd"].metrics == ("spot_net_taker_usd",)
    assert by_key["spot_cvd"].source == "coinglass"


def test_parse_fear_greed_both_shapes():
    as_dict = {"data_list": [25, 30], "time_list": [D0, D0 + DAY_MS]}
    assert parse_fear_greed(as_dict)["fear_greed"] == [
        ("2025-08-11", 25.0), ("2025-08-12", 30.0),
    ]
    as_list = [{"time": D0, "value": 40}]
    assert parse_fear_greed(as_list)["fear_greed"] == [("2025-08-11", 40.0)]


# ── 目标日 ──

def test_target_day_for():
    # 2026-08-12 是周三；最近已收盘完整周 = 上上周一 8-03
    now = datetime(2026, 8, 12, 9, 30, tzinfo=timezone.utc)
    assert target_day_for("daily", now) == "2026-08-11"
    assert target_day_for("weekly", now) == "2026-08-03"
    # 周一凌晨：上一周（8-03 起始的周）刚收盘
    monday = datetime(2026, 8, 10, 0, 5, tzinfo=timezone.utc)
    assert target_day_for("weekly", monday) == "2026-08-03"


# ── 采集器 ──

class _StubSource:
    last_error = ""


def _spec(key, source, payload, cadence="daily"):
    async def fetch(_src):
        if isinstance(payload, Exception):
            raise payload
        return payload

    def parse(raw):
        return {key: raw}

    return FetchSpec(key=key, source=source, cadence=cadence,
                     metrics=(key,), fetch=fetch, parse=parse)


@pytest.mark.asyncio
async def test_collector_fetch_dedup_and_failopen(tmp_path):
    store = BottomModelStore(str(tmp_path / "bm"))
    now = datetime(2026, 8, 12, 9, 0, tzinfo=timezone.utc)
    registry = [
        _spec("ok_metric", "coinglass", [("2026-08-11", 1.0)]),
        _spec("none_metric", "coinglass", None),           # 源返回 None
        _spec("boom_metric", "bgeometrics", RuntimeError("boom")),  # 抛异常
    ]
    collector = BottomModelCollector(
        store, _StubSource(), bgeometrics=_StubSource(), yahoo_cme=None,
        coinglass_spacing_sec=0.0, registry=registry,
    )
    summary = await collector.run_once(now=now)
    assert summary["fetched"] == 1 and summary["failed"] == 2
    assert summary["specs"]["ok_metric"]["status"] == "fetched"
    assert summary["specs"]["none_metric"]["status"] == "failed"
    assert summary["specs"]["boom_metric"]["status"] == "failed"
    assert store.series("ok_metric") == [("2026-08-11", 1.0)]

    # 第二轮：成功指标按日期戳跳过，失败指标重试
    summary2 = await collector.run_once(now=now)
    assert summary2["specs"]["ok_metric"]["status"] == "fresh"
    assert summary2["specs"]["none_metric"]["status"] == "failed"

    # force 无视日期戳
    summary3 = await collector.run_once(force=True, now=now)
    assert summary3["specs"]["ok_metric"]["status"] == "fetched"
    store.close()


@pytest.mark.asyncio
async def test_collector_only_sources_and_missing_source(tmp_path):
    store = BottomModelStore(str(tmp_path / "bm"))
    registry = [
        _spec("cg_m", "coinglass", [("2026-08-11", 1.0)]),
        _spec("bg_m", "bgeometrics", [("2026-08-11", 2.0)]),
        _spec("y_m", "yahoo_cme", [("2026-08-03", 3.0)], cadence="weekly"),
    ]
    collector = BottomModelCollector(
        store, _StubSource(), bgeometrics=None, yahoo_cme=_StubSource(),
        coinglass_spacing_sec=0.0, registry=registry,
    )
    summary = await collector.run_once(only_sources={"coinglass"})
    assert set(summary["specs"]) == {"cg_m"}
    # bgeometrics 源未配置 → no_source，不算失败
    summary2 = await collector.run_once()
    assert summary2["specs"]["bg_m"]["status"] == "no_source"
    assert summary2["specs"]["y_m"]["status"] == "fetched"
    assert summary2["failed"] == 0
    store.close()


def test_registry_shape():
    """注册表自检：key/metric 唯一，源合法，BGeometrics 一轮 ≤ 小时配额。"""
    registry = build_registry()
    keys = [spec.key for spec in registry]
    assert len(keys) == len(set(keys))
    all_metrics = [m for spec in registry for m in spec.metrics]
    assert len(all_metrics) == len(set(all_metrics))
    assert {spec.source for spec in registry} <= {"coinglass", "bgeometrics", "yahoo_cme"}
    assert {spec.cadence for spec in registry} <= {"daily", "weekly"}
    bg_count = sum(1 for spec in registry if spec.source == "bgeometrics")
    assert bg_count <= 8, "BGeometrics 端点数超过 8/h 免费配额，一轮无法完成"
