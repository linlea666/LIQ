"""订单流小时/日桶存储与聚合器单元测试（P2）。

覆盖要点：
  - store：taker 幂等 upsert 不触碰增量列；增量累加；日桶汇总；保留期清理
  - aggregator：5m 序列按小时分桶；左边界截断小时不覆写；executed 首见基线
    不计增量、二次 flush 计增量；日桶产出
"""
from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from processors.orderflow_stats import OrderflowStatsAggregator
from storage.orderflow_store import OrderflowStore, day_key_cn

_TZ_CN = timezone(timedelta(hours=8))


@pytest.fixture()
def store(tmp_path):
    s = OrderflowStore(data_dir=str(tmp_path))
    yield s
    s.close()


@pytest.fixture()
def agg(store):
    return OrderflowStatsAggregator(store)


# ─────────────────────────────────────────────────────────────────
# 1. Store 基础行为
# ─────────────────────────────────────────────────────────────────

class TestStore:
    def test_taker_upsert_idempotent(self, store):
        hour = 1700000000 - 1700000000 % 3600
        store.upsert_taker_hour("BTC", "spot", hour, 100.0, 60.0, 12)
        store.upsert_taker_hour("BTC", "spot", hour, 120.0, 70.0, 12)
        rows = store.query_hourly("BTC", market="spot")
        assert len(rows) == 1
        assert rows[0]["taker_buy_usd"] == 120.0
        assert rows[0]["net_usd"] == 50.0
        assert rows[0]["coverage_pct"] == 1.0

    def test_taker_upsert_preserves_incremental_columns(self, store):
        """整桶重算 taker 不得清掉 large_executed / whale 增量列。"""
        hour = 1700000000 - 1700000000 % 3600
        store.add_large_executed("BTC", "spot", hour, bid_usd=5e6, ask_usd=2e6)
        store.add_whale_trades("BTC", "spot", hour, buy_usd=1e6)
        store.upsert_taker_hour("BTC", "spot", hour, 100.0, 60.0, 6)
        row = store.query_hourly("BTC", market="spot")[0]
        assert row["large_executed_bid_usd"] == 5e6
        assert row["large_executed_ask_usd"] == 2e6
        assert row["whale_buy_usd"] == 1e6
        assert row["taker_buy_usd"] == 100.0
        assert row["coverage_pct"] == 0.5

    def test_incremental_adds_accumulate(self, store):
        hour = 1700000000 - 1700000000 % 3600
        store.add_large_executed("ETH", "futures", hour, bid_usd=1e6)
        store.add_large_executed("ETH", "futures", hour, bid_usd=2e6, ask_usd=3e6)
        row = store.query_hourly("ETH", market="futures")[0]
        assert row["large_executed_bid_usd"] == 3e6
        assert row["large_executed_ask_usd"] == 3e6

    def test_rollup_daily(self, store):
        # 用当天 UTC+8 的两个整点小时
        now = int(time.time())
        day = day_key_cn(now)
        day_start = int(
            datetime.strptime(day, "%Y%m%d").replace(tzinfo=_TZ_CN).timestamp()
        )
        store.upsert_taker_hour("BTC", "spot", day_start, 100.0, 40.0, 12)
        store.upsert_taker_hour("BTC", "spot", day_start + 3600, 50.0, 80.0, 12)
        store.add_whale_trades("BTC", "spot", day_start, buy_usd=7e6, buy_qty=110.0)
        # P4：价格列跨小时聚合——high 取 MAX、low 取非零 MIN、close 取最新小时
        store.merge_price_stats("BTC", "spot", day_start,
                                high=63_500, low=62_800, close=63_100)
        store.merge_price_stats("BTC", "spot", day_start + 3600,
                                high=63_400, low=63_000, close=63_300)
        store.rollup_daily("BTC", "spot", day)
        rows = store.query_daily("BTC", market="spot")
        assert len(rows) == 1
        d = rows[0]
        assert d["taker_buy_usd"] == 150.0
        assert d["taker_sell_usd"] == 120.0
        assert d["net_usd"] == 30.0
        assert d["whale_buy_usd"] == 7e6
        assert d["whale_buy_qty"] == pytest.approx(110.0)
        assert d["price_high"] == pytest.approx(63_500)
        assert d["price_low"] == pytest.approx(62_800)
        assert d["price_close"] == pytest.approx(63_300), "close = 最新小时的收盘"
        assert d["hours_covered"] == 2
        assert d["coverage_pct"] == round(2 / 24, 4)

    def test_whale_qty_accumulates(self, store):
        """P4：qty 与 usd 同步累加，VWAP = usd/qty。"""
        hour = 1700000000 - 1700000000 % 3600
        store.add_whale_trades("BTC", "spot", hour, buy_usd=630_000, buy_qty=10.0)
        store.add_whale_trades("BTC", "spot", hour,
                               buy_usd=1_280_000, buy_qty=20.0,
                               sell_usd=500_000, sell_qty=8.0)
        row = store.query_hourly("BTC", market="spot")[0]
        assert row["whale_buy_usd"] == pytest.approx(1_910_000)
        assert row["whale_buy_qty"] == pytest.approx(30.0)
        assert row["whale_sell_qty"] == pytest.approx(8.0)
        # VWAP 派生验证
        assert row["whale_buy_usd"] / row["whale_buy_qty"] == pytest.approx(63_666.67, rel=1e-4)

    def test_merge_price_stats(self, store):
        """P4：high 取 MAX、low 取非零 MIN、close 覆盖；0 视为无数据。"""
        hour = 1700000000 - 1700000000 % 3600
        store.merge_price_stats("BTC", "spot", hour, high=63_100, low=62_900, close=63_000)
        store.merge_price_stats("BTC", "spot", hour, high=63_500, low=63_200, close=63_400)
        row = store.query_hourly("BTC", market="spot")[0]
        assert row["price_high"] == pytest.approx(63_500)
        assert row["price_low"] == pytest.approx(62_900)
        assert row["price_close"] == pytest.approx(63_400)
        # close=0（无数据）不得回退已有 close；low=0 不得覆盖非零 low
        store.merge_price_stats("BTC", "spot", hour, high=63_600, low=0, close=0)
        row = store.query_hourly("BTC", market="spot")[0]
        assert row["price_high"] == pytest.approx(63_600)
        assert row["price_low"] == pytest.approx(62_900)
        assert row["price_close"] == pytest.approx(63_400)

    def test_merge_price_stats_all_zero_noop(self, store):
        store.merge_price_stats("BTC", "spot", 1700000000 - 1700000000 % 3600)
        assert store.query_hourly("BTC") == []

    def test_taker_upsert_preserves_price_columns(self, store):
        """整桶重算 taker 不得清掉 P4 价格/qty 列。"""
        hour = 1700000000 - 1700000000 % 3600
        store.merge_price_stats("BTC", "spot", hour, high=63_100, low=62_900, close=63_000)
        store.add_whale_trades("BTC", "spot", hour, buy_usd=630_000, buy_qty=10.0)
        store.upsert_taker_hour("BTC", "spot", hour, 100.0, 60.0, 12)
        row = store.query_hourly("BTC", market="spot")[0]
        assert row["price_high"] == pytest.approx(63_100)
        assert row["whale_buy_qty"] == pytest.approx(10.0)
        assert row["taker_buy_usd"] == 100.0

    def test_migration_adds_columns_to_legacy_db(self, tmp_path):
        """生产库是 P4 之前的旧 schema：init 时 ALTER TABLE 补列且不丢数据。"""
        import sqlite3 as _sq

        db_path = str(tmp_path / "orderflow.sqlite3")
        legacy = _sq.connect(db_path)
        legacy.executescript("""
            CREATE TABLE hourly_flows (
                coin TEXT NOT NULL, market TEXT NOT NULL, hour_ts INTEGER NOT NULL,
                taker_buy_usd REAL NOT NULL DEFAULT 0,
                taker_sell_usd REAL NOT NULL DEFAULT 0,
                net_usd REAL NOT NULL DEFAULT 0,
                large_executed_bid_usd REAL NOT NULL DEFAULT 0,
                large_executed_ask_usd REAL NOT NULL DEFAULT 0,
                whale_buy_usd REAL NOT NULL DEFAULT 0,
                whale_sell_usd REAL NOT NULL DEFAULT 0,
                samples INTEGER NOT NULL DEFAULT 0,
                coverage_pct REAL NOT NULL DEFAULT 0,
                updated_ts INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (coin, market, hour_ts)
            );
            CREATE TABLE daily_flows (
                coin TEXT NOT NULL, market TEXT NOT NULL, day_key TEXT NOT NULL,
                taker_buy_usd REAL NOT NULL DEFAULT 0,
                taker_sell_usd REAL NOT NULL DEFAULT 0,
                net_usd REAL NOT NULL DEFAULT 0,
                large_executed_bid_usd REAL NOT NULL DEFAULT 0,
                large_executed_ask_usd REAL NOT NULL DEFAULT 0,
                whale_buy_usd REAL NOT NULL DEFAULT 0,
                whale_sell_usd REAL NOT NULL DEFAULT 0,
                hours_covered INTEGER NOT NULL DEFAULT 0,
                coverage_pct REAL NOT NULL DEFAULT 0,
                updated_ts INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (coin, market, day_key)
            );
        """)
        recent_hour = int(time.time()) - int(time.time()) % 3600
        legacy.execute(
            "INSERT INTO hourly_flows (coin, market, hour_ts, taker_buy_usd,"
            " taker_sell_usd, net_usd, samples, coverage_pct, updated_ts)"
            " VALUES ('BTC', 'spot', ?, 100, 60, 40, 12, 1.0, 0)",
            (recent_hour,),
        )
        legacy.commit()
        legacy.close()

        s = OrderflowStore(data_dir=str(tmp_path))
        rows = s.query_hourly("BTC", market="spot")
        assert len(rows) == 1
        r = rows[0]
        assert r["taker_buy_usd"] == 100.0, "旧数据不丢"
        for col in ("whale_buy_qty", "whale_sell_qty",
                    "price_high", "price_low", "price_close"):
            assert r[col] == 0, f"新列 {col} 默认 0"
        # 新列可正常写入；重复 init 幂等
        s.merge_price_stats("BTC", "spot", recent_hour, high=1.0, low=1.0, close=1.0)
        s.close()
        s2 = OrderflowStore(data_dir=str(tmp_path))
        assert s2.query_hourly("BTC")[0]["price_high"] == pytest.approx(1.0)
        s2.close()

    def test_cleanup_removes_expired(self, tmp_path):
        s = OrderflowStore(data_dir=str(tmp_path), hourly_keep_days=7, daily_keep_days=30)
        old_hour = int(time.time()) - 10 * 86400
        old_hour -= old_hour % 3600
        s.upsert_taker_hour("BTC", "spot", old_hour, 1.0, 1.0, 12)
        old_day = day_key_cn(int(time.time()) - 40 * 86400)
        s.upsert_taker_hour("BTC", "spot", int(time.time()) - int(time.time()) % 3600, 2.0, 2.0, 12)
        with s._lock, s._conn:
            s._conn.execute(
                "INSERT INTO daily_flows (coin, market, day_key, updated_ts) VALUES (?,?,?,?)",
                ("BTC", "spot", old_day, 0),
            )
        s.cleanup()
        hours = s.query_hourly("BTC")
        assert all(r["hour_ts"] != old_hour for r in hours)
        assert len(hours) == 1
        assert s.query_daily("BTC") == []
        s.close()


# ─────────────────────────────────────────────────────────────────
# 2. Aggregator：taker 小时分桶
# ─────────────────────────────────────────────────────────────────

def _mk_series(start_hour_ts: int, n_bars: int, buy: float = 10.0, sell: float = 4.0):
    return [
        SimpleNamespace(
            ts=start_hour_ts + i * 300, buy_vol=buy, sell_vol=sell,
            delta=buy - sell, cvd=0.0,
        )
        for i in range(n_bars)
    ]


def _mk_state(coin="BTC", cvd_spot=None, cvd_contract=None,
              large=None, spot_large=None):
    return SimpleNamespace(
        coin=coin,   # 与 engine.CoinState 真实属性名一致（曾因 mock 用 ccy 掩盖生产 bug）
        cvd_spot=cvd_spot,
        cvd_contract=cvd_contract,
        large_orders_history=large or [],
        spot_large_orders_history=spot_large or [],
    )


class TestAggregatorTaker:
    def test_full_hours_bucketed(self, agg, store):
        hour = 1700000000 - 1700000000 % 3600
        # 两个完整小时（24 根 5m bar），序列恰好从整点开始
        cvd = SimpleNamespace(series=_mk_series(hour, 24))
        agg.flush_coin(_mk_state(cvd_spot=cvd))
        rows = {r["hour_ts"]: r for r in store.query_hourly("BTC", market="spot")}
        assert set(rows) == {hour, hour + 3600}
        assert rows[hour]["taker_buy_usd"] == pytest.approx(120.0)
        assert rows[hour]["taker_sell_usd"] == pytest.approx(48.0)
        assert rows[hour]["coverage_pct"] == 1.0

    def test_truncated_left_boundary_hour_skipped(self, agg, store):
        """序列从小时中间开始 → 该小时只有部分 bar，不写入（防覆写完整桶）。"""
        hour = 1700000000 - 1700000000 % 3600
        # 从 hour+30min 开始：首小时只有 6 bar，第二小时完整 12 bar
        series = _mk_series(hour + 1800, 18)
        agg.flush_coin(_mk_state(cvd_spot=SimpleNamespace(series=series)))
        rows = {r["hour_ts"]: r for r in store.query_hourly("BTC", market="spot")}
        assert hour not in rows, "截断的左边界小时不应写入"
        assert rows[hour + 3600]["samples"] == 12

    def test_current_partial_hour_written(self, agg, store):
        """右边界（进行中小时）允许部分写入，coverage 如实 < 1。"""
        hour = 1700000000 - 1700000000 % 3600
        series = _mk_series(hour, 15)  # 1 完整小时 + 当前小时 3 bar
        agg.flush_coin(_mk_state(cvd_contract=SimpleNamespace(series=series)))
        rows = {r["hour_ts"]: r for r in store.query_hourly("BTC", market="futures")}
        assert rows[hour + 3600]["samples"] == 3
        assert rows[hour + 3600]["coverage_pct"] == 0.25


# ─────────────────────────────────────────────────────────────────
# 3. Aggregator：大额挂单 executed 增量
# ─────────────────────────────────────────────────────────────────

def _mk_lifecycle(oid: int, side: str, executed: float):
    return SimpleNamespace(id=oid, side=side, executed_usd_value=executed)


class TestAggregatorExecuted:
    def test_first_sight_records_baseline_only(self, agg, store):
        state = _mk_state(large=[_mk_lifecycle(1, "bid", 5e6)])
        agg.flush_coin(state)
        rows = store.query_hourly("BTC", market="futures")
        assert all(r["large_executed_bid_usd"] == 0 for r in rows), \
            "首见 order 只记基线不计增量"

    def test_second_flush_counts_delta(self, agg, store):
        agg.flush_coin(_mk_state(large=[
            _mk_lifecycle(1, "bid", 5e6), _mk_lifecycle(2, "ask", 1e6),
        ]))
        agg.flush_coin(_mk_state(large=[
            _mk_lifecycle(1, "bid", 8e6),   # +3M bid
            _mk_lifecycle(2, "ask", 1.5e6), # +0.5M ask
            _mk_lifecycle(3, "bid", 9e6),   # 新 order，只记基线
        ]))
        now = int(time.time())
        hour = now - now % 3600
        rows = {r["hour_ts"]: r for r in store.query_hourly("BTC", market="futures")}
        assert rows[hour]["large_executed_bid_usd"] == pytest.approx(3e6)
        assert rows[hour]["large_executed_ask_usd"] == pytest.approx(0.5e6)

    def test_executed_decrease_ignored(self, agg, store):
        """executed 减少（数据修正/换 id 语义）不产生负增量。"""
        agg.flush_coin(_mk_state(large=[_mk_lifecycle(1, "bid", 5e6)]))
        agg.flush_coin(_mk_state(large=[_mk_lifecycle(1, "bid", 4e6)]))
        rows = store.query_hourly("BTC", market="futures")
        assert all(r["large_executed_bid_usd"] == 0 for r in rows)


# ─────────────────────────────────────────────────────────────────
# 4. Aggregator：日桶
# ─────────────────────────────────────────────────────────────────

class TestAggregatorDaily:
    def test_daily_rollup_produced(self, agg, store):
        now = int(time.time())
        cur_hour = now - now % 3600
        cvd = SimpleNamespace(series=_mk_series(cur_hour, 12))
        agg.flush_coin(_mk_state(cvd_spot=cvd))
        days = store.query_daily("BTC", market="spot")
        assert len(days) >= 1
        assert days[0]["day_key"] == day_key_cn(cur_hour)
        assert days[0]["taker_buy_usd"] == pytest.approx(120.0)
