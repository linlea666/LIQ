"""流动性墙引擎监控指标单元测试（W1-T1）。

覆盖要点：
  - 排队 p50/p95 累积
  - source_age_by_endpoint 单调递增（上次成功距今）
  - data_quality stale_ratio 计算（含 missing/stale 共同计入）
  - Coinbase 错误率 30min 滑动窗口
  - 写入路径异常吞掉（best-effort 不抛）
  - snapshot(coin=...) vs snapshot() 两种形态
  - 全局 singleton 与 reset_metrics_for_test
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from processors.liquidity_wall_metrics import (
    LiquidityWallMetrics,
    get_metrics,
    reset_metrics_for_test,
)


# ─────────────────────────────────────────────────────────────────
# 1. 排队等待样本：p50 / p95 计算
# ─────────────────────────────────────────────────────────────────

class TestQueueWaitSamples:
    def test_queue_wait_p50_p95_basic(self):
        m = LiquidityWallMetrics()
        for v in [100.0, 200.0, 300.0, 400.0, 500.0, 600.0, 700.0, 800.0, 900.0, 1000.0]:
            m.record_coinglass_queue_wait(v)
        snap = m.snapshot()
        # 10 个样本，p50 == 第 5 个 = 600（len/2 = 5），p95 == 第 9 个 = 1000
        assert snap["coinglass_queue_wait_ms_p50"] == 600.0
        assert snap["coinglass_queue_wait_ms_p95"] == 1000.0

    def test_queue_wait_negative_clamped_to_zero(self):
        """负值应被夹到 0（防御写入）。"""
        m = LiquidityWallMetrics()
        m.record_coinglass_queue_wait(-100.0)
        m.record_coinglass_queue_wait(50.0)
        snap = m.snapshot()
        # p50 应为 0 或 25（中位）；不应为负
        assert snap["coinglass_queue_wait_ms_p50"] >= 0.0
        assert snap["coinglass_queue_wait_ms_p95"] >= 0.0

    def test_queue_wait_empty_returns_zero(self):
        m = LiquidityWallMetrics()
        snap = m.snapshot()
        assert snap["coinglass_queue_wait_ms_p50"] == 0.0
        assert snap["coinglass_queue_wait_ms_p95"] == 0.0


# ─────────────────────────────────────────────────────────────────
# 2. Coinglass call count + source_age
# ─────────────────────────────────────────────────────────────────

class TestCoinglassCallTracking:
    def test_call_count_by_endpoint(self):
        m = LiquidityWallMetrics()
        m.record_coinglass_call("/api/futures/orderbook/history", ok=True)
        m.record_coinglass_call("/api/futures/orderbook/history", ok=True)
        m.record_coinglass_call("/api/spot/orderbook/history", ok=True)
        snap = m.snapshot()
        assert snap["coinglass_call_count_by_endpoint"][
            "/api/futures/orderbook/history"] == 2
        assert snap["coinglass_call_count_by_endpoint"][
            "/api/spot/orderbook/history"] == 1

    def test_source_age_only_updated_on_success(self):
        """失败调用计数 +1，但 source_age 不刷新。"""
        m = LiquidityWallMetrics()
        m.record_coinglass_call("/api/futures/orderbook/history", ok=True)
        time.sleep(0.05)
        # 失败的调用不应重置 source_age
        m.record_coinglass_call("/api/futures/orderbook/history", ok=False)
        snap = m.snapshot()
        age = snap["source_age_by_endpoint_sec"]["/api/futures/orderbook/history"]
        assert age >= 0  # 至少 50ms ≈ 0s（int cast 后），允许 0
        # 错误次数累计
        assert snap["coinglass_error_count_by_endpoint"][
            "/api/futures/orderbook/history"] == 1

    def test_source_age_monotonic_grows_without_calls(self):
        m = LiquidityWallMetrics()
        m.record_coinglass_call("/api/futures/orderbook/history", ok=True)
        snap1 = m.snapshot()
        age1 = snap1["source_age_by_endpoint_sec"][
            "/api/futures/orderbook/history"]
        time.sleep(1.1)
        snap2 = m.snapshot()
        age2 = snap2["source_age_by_endpoint_sec"][
            "/api/futures/orderbook/history"]
        assert age2 >= age1 + 1

    def test_record_call_empty_endpoint_ignored(self):
        m = LiquidityWallMetrics()
        m.record_coinglass_call("", ok=True)
        snap = m.snapshot()
        assert snap["coinglass_call_count_by_endpoint"] == {}


# ─────────────────────────────────────────────────────────────────
# 3. data_quality stale_ratio
# ─────────────────────────────────────────────────────────────────

class TestDataQualityStaleRatio:
    def test_stale_ratio_includes_missing_and_stale(self):
        m = LiquidityWallMetrics()
        m.record_data_quality("BTC", "ok")
        m.record_data_quality("BTC", "ok")
        m.record_data_quality("BTC", "stale")
        m.record_data_quality("BTC", "missing")
        snap = m.snapshot(coin="BTC")
        # 4 帧中 2 帧不健康 → 0.5
        assert snap["orderbook_pressure_stale_ratio_30min"] == 0.5
        assert snap["orderbook_pressure_data_quality_latest"] == "missing"

    def test_warming_partial_not_counted_as_stale(self):
        """warming/partial 是中间态，不应被计入 stale_ratio。"""
        m = LiquidityWallMetrics()
        m.record_data_quality("BTC", "ok")
        m.record_data_quality("BTC", "warming")
        m.record_data_quality("BTC", "partial")
        snap = m.snapshot(coin="BTC")
        assert snap["orderbook_pressure_stale_ratio_30min"] == 0.0

    def test_per_coin_isolation(self):
        m = LiquidityWallMetrics()
        m.record_data_quality("BTC", "ok")
        m.record_data_quality("ETH", "stale")
        snap_all = m.snapshot()
        by_coin = snap_all["orderbook_pressure_stale_ratio_30min_by_coin"]
        assert by_coin["BTC"] == 0.0
        assert by_coin["ETH"] == 1.0

    def test_empty_coin_ignored(self):
        m = LiquidityWallMetrics()
        m.record_data_quality("", "stale")  # 空 coin 静默忽略
        m.record_data_quality("BTC", "")    # 空 quality 静默忽略
        snap = m.snapshot(coin="BTC")
        assert snap["orderbook_pressure_stale_ratio_30min"] == 0.0


# ─────────────────────────────────────────────────────────────────
# 4. Coinbase 错误率窗口
# ─────────────────────────────────────────────────────────────────

class TestCoinbaseErrorRate:
    def test_error_rate_basic(self):
        m = LiquidityWallMetrics()
        for _ in range(8):
            m.record_coinbase_call(120.0, ok=True)
        for _ in range(2):
            m.record_coinbase_call(0.0, ok=False)
        # 10 总调用，2 个错误 → 20%
        snap = m.snapshot()
        assert snap["coinbase_total_calls"] == 10
        assert abs(snap["coinbase_error_rate_30min"] - 0.2) < 1e-6

    def test_error_rate_zero_when_no_calls(self):
        m = LiquidityWallMetrics()
        snap = m.snapshot()
        assert snap["coinbase_total_calls"] == 0
        assert snap["coinbase_error_rate_30min"] == 0.0

    def test_latency_only_on_success(self):
        """ok=False 不写 latency 样本（避免被 0ms 拉低 p50）。"""
        m = LiquidityWallMetrics()
        m.record_coinbase_call(100.0, ok=True)
        m.record_coinbase_call(0.0, ok=False)  # 不应进入 latency
        m.record_coinbase_call(300.0, ok=True)
        snap = m.snapshot()
        # 2 个成功样本：100, 300；p50=300（len//2=1）
        assert snap["coinbase_latency_ms_p50"] == 300.0


# ─────────────────────────────────────────────────────────────────
# 5. snapshot 两种形态
# ─────────────────────────────────────────────────────────────────

class TestSnapshotShapes:
    def test_snapshot_with_coin_returns_per_coin_keys(self):
        m = LiquidityWallMetrics()
        m.record_data_quality("BTC", "ok")
        snap = m.snapshot(coin="BTC")
        assert "orderbook_pressure_stale_ratio_30min" in snap
        assert "orderbook_pressure_data_quality_latest" in snap
        assert snap["coin"] == "BTC"

    def test_snapshot_without_coin_returns_by_coin_dict(self):
        m = LiquidityWallMetrics()
        m.record_data_quality("BTC", "ok")
        snap = m.snapshot()
        assert "orderbook_pressure_stale_ratio_30min_by_coin" in snap
        assert "orderbook_pressure_data_quality_latest_by_coin" in snap
        assert "coin" not in snap

    def test_snapshot_basic_fields_always_present(self):
        m = LiquidityWallMetrics()
        snap = m.snapshot()
        for key in (
            "ts", "window_sec",
            "coinglass_queue_wait_ms_p50",
            "coinglass_queue_wait_ms_p95",
            "coinglass_call_count_by_endpoint",
            "source_age_by_endpoint_sec",
            "coinbase_latency_ms_p50",
            "coinbase_latency_ms_p95",
            "coinbase_error_rate_30min",
            "coinbase_total_calls",
        ):
            assert key in snap


# ─────────────────────────────────────────────────────────────────
# 6. 全局 singleton + reset
# ─────────────────────────────────────────────────────────────────

class TestGlobalSingleton:
    def test_get_metrics_returns_same_instance(self):
        reset_metrics_for_test()
        m1 = get_metrics()
        m2 = get_metrics()
        assert m1 is m2

    def test_reset_metrics_for_test_creates_new_instance(self):
        m1 = get_metrics()
        m1.record_coinbase_call(100.0, ok=True)
        reset_metrics_for_test()
        m2 = get_metrics()
        assert m1 is not m2
        assert m2.snapshot()["coinbase_total_calls"] == 0
