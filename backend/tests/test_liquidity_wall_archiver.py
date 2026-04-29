"""流动性墙引擎历史落盘器单元测试（W1-T2）。

覆盖要点：
  - 落盘往返一致（写入 → 反序列化 → 字段一致）
  - 按 coin/day 切片（多币 / 跨日）
  - 写入失败 best-effort（snapshot=None / 缺字段）
  - 磁盘高水位时跳过 + dropped_count 计数
  - 90 天 GC 删除超期文件
  - 字段向前兼容（没有 wall_zone_id / raw_trust_score 等新字段时默认值兼容）
  - reset_for_testing 清空目录
  - global singleton
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from processors.liquidity_wall_archiver import (
    LiquidityWallArchiver,
    get_archiver,
    reset_archiver_for_test,
)


_TZ_CN = timezone(timedelta(hours=8))


# ─────────────────────────────────────────────────────────────────
# 测试夹具：模拟 OrderbookPressureSnapshot
# ─────────────────────────────────────────────────────────────────

def _make_zone(
    side: str = "ask",
    price_mid: float = 76450.0,
    source: str = "dual_source",
    trust_score: float = 0.92,
    coinbase_confluence: bool = True,
    **extra,
) -> SimpleNamespace:
    """构造 WallZone-like 鸭子对象（避免依赖真实 pydantic 模型）。"""
    return SimpleNamespace(
        side=side,
        source=source,
        dual_source=(source == "dual_source"),
        price_mid=price_mid,
        price_low=price_mid - 50,
        price_high=price_mid + 50,
        current_usd=8.2e6,
        max_usd_1h=12.5e6,
        trust_score=trust_score,
        # W2-T1 后会有真实值；当前默认 0
        raw_trust_score=getattr(extra, "raw_trust_score", 0.0),
        trust_components={},
        support_resistance_trust_score=0.0,
        sweep_attractiveness_score=0.0,
        break_through_risk=0.42,
        active_attack_score=0.31,
        wall_removal_risk=0.2,
        wall_consumed_confidence=0.0,
        persistence_score=0.85,
        visible_minutes=38.0,
        exchange_count=1,
        has_spot_confluence=False,
        spot_current_usd=0.0,
        coinbase_spot_confluence=coinbase_confluence,
        coinbase_spot_usd=2.4e6,
        coinbase_num_orders=14,
        trend="strengthening",
        next_magnet_price=None,
        sweep_target=None,
        # W1-T4 后会有真实值；当前默认 ""
        wall_zone_id="",
        **extra,
    )


def _make_event(
    event_type: str = "wall_strengthened",
    side: str = "bid",
    price_mid: float = 75800.0,
) -> SimpleNamespace:
    return SimpleNamespace(
        event_type=event_type,
        side=side,
        price_mid=price_mid,
        ts_sec=int(time.time()) - 120,
        size_before_usd=4.2e6,
        size_after_usd=8.1e6,
        executed_usd_value=None,
        confidence=0.75,
        wall_zone_id="",
    )


def _make_snapshot(
    coin: str = "BTC",
    ts_sec: int = 0,
    walls_above: list = None,
    walls_below: list = None,
    wall_events: list = None,
    data_quality: str = "ok",
) -> SimpleNamespace:
    return SimpleNamespace(
        coin=coin,
        ts_sec=ts_sec or int(time.time()),
        last_price=76234.5,
        atr=312.0,
        data_quality=data_quality,
        walls_above=walls_above or [],
        walls_below=walls_below or [],
        wall_events=wall_events or [],
        crowding_global=SimpleNamespace(
            oi_delta_1h_pct=0.025,
            oi_delta_24h_pct=0.082,
            oi_margin_split="stable_dominant",
            funding_now_pct=0.00012,
            funding_avg_8h_pct=0.00010,
            funding_percentile_30d=0.78,
            top_position_ls_ratio=1.45,
            global_account_ls_ratio=1.20,
            inferred_position_state="long_opening",
            long_crowding_risk=0.45,
            short_crowding_risk=0.20,
        ),
        top_sweep_targets=[],
    )


# ─────────────────────────────────────────────────────────────────
# 1. 落盘往返一致
# ─────────────────────────────────────────────────────────────────

class TestRoundTrip:
    def test_basic_append_and_read(self, tmp_path):
        archiver = LiquidityWallArchiver(root=str(tmp_path), keep_days=90)
        snap = _make_snapshot(walls_above=[_make_zone()])
        ok = archiver.append(snap, source_age={"orderbook": 12, "spot": 35})
        assert ok is True

        # 反序列化
        day = datetime.fromtimestamp(snap.ts_sec, tz=_TZ_CN).strftime("%Y%m%d")
        path = tmp_path / "BTC" / f"{day}.jsonl"
        assert path.exists()
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        assert len(rows) == 1
        row = rows[0]
        assert row["coin"] == "BTC"
        assert row["ts"] == snap.ts_sec
        assert row["last_price"] == 76234.5
        assert row["data_quality"] == "ok"
        assert len(row["walls_above"]) == 1
        wall = row["walls_above"][0]
        assert wall["price_mid"] == 76450.0
        assert wall["coinbase_spot_confluence"] is True
        assert wall["coinbase_num_orders"] == 14
        # source_age 冗余审计
        assert row["source_age"] == {"orderbook": 12, "spot": 35}

    def test_multiple_appends_same_day(self, tmp_path):
        archiver = LiquidityWallArchiver(root=str(tmp_path))
        for i in range(3):
            snap = _make_snapshot(walls_above=[_make_zone(price_mid=76000 + i * 100)])
            assert archiver.append(snap) is True
        day = datetime.now(_TZ_CN).strftime("%Y%m%d")
        path = tmp_path / "BTC" / f"{day}.jsonl"
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        assert len(rows) == 3

    def test_crowding_global_serialized(self, tmp_path):
        archiver = LiquidityWallArchiver(root=str(tmp_path))
        snap = _make_snapshot()
        archiver.append(snap)
        day = datetime.now(_TZ_CN).strftime("%Y%m%d")
        rows = [json.loads(line) for line in (tmp_path / "BTC" / f"{day}.jsonl").read_text().splitlines()]
        cg = rows[0]["crowding_global"]
        assert cg["oi_margin_split"] == "stable_dominant"
        assert cg["inferred_position_state"] == "long_opening"
        assert cg["long_crowding_risk"] == 0.45


# ─────────────────────────────────────────────────────────────────
# 2. 按 coin/day 切片
# ─────────────────────────────────────────────────────────────────

class TestPartitioning:
    def test_per_coin_isolation(self, tmp_path):
        archiver = LiquidityWallArchiver(root=str(tmp_path))
        archiver.append(_make_snapshot(coin="BTC"))
        archiver.append(_make_snapshot(coin="ETH"))
        archiver.append(_make_snapshot(coin="SOL"))
        day = datetime.now(_TZ_CN).strftime("%Y%m%d")
        for c in ("BTC", "ETH", "SOL"):
            assert (tmp_path / c / f"{day}.jsonl").exists()

    def test_cross_day_partitioning(self, tmp_path):
        archiver = LiquidityWallArchiver(root=str(tmp_path))
        # 第 1 天
        ts_d1 = int(datetime(2026, 4, 28, 12, tzinfo=_TZ_CN).timestamp())
        archiver.append(_make_snapshot(ts_sec=ts_d1))
        # 第 2 天
        ts_d2 = int(datetime(2026, 4, 29, 12, tzinfo=_TZ_CN).timestamp())
        archiver.append(_make_snapshot(ts_sec=ts_d2))
        assert (tmp_path / "BTC" / "20260428.jsonl").exists()
        assert (tmp_path / "BTC" / "20260429.jsonl").exists()

    def test_list_days(self, tmp_path):
        archiver = LiquidityWallArchiver(root=str(tmp_path))
        ts1 = int(datetime(2026, 4, 28, tzinfo=_TZ_CN).timestamp())
        ts2 = int(datetime(2026, 4, 29, tzinfo=_TZ_CN).timestamp())
        archiver.append(_make_snapshot(coin="BTC", ts_sec=ts1))
        archiver.append(_make_snapshot(coin="BTC", ts_sec=ts2))
        archiver.append(_make_snapshot(coin="ETH", ts_sec=ts2))
        assert archiver.list_days(coin="BTC") == ["20260428", "20260429"]
        assert archiver.list_days(coin="ETH") == ["20260429"]
        assert archiver.list_days() == ["20260428", "20260429"]


# ─────────────────────────────────────────────────────────────────
# 3. best-effort 写入：异常吞掉
# ─────────────────────────────────────────────────────────────────

class TestBestEffort:
    def test_none_snapshot_returns_false(self, tmp_path):
        archiver = LiquidityWallArchiver(root=str(tmp_path))
        assert archiver.append(None) is False

    def test_missing_coin_or_ts_skipped(self, tmp_path):
        archiver = LiquidityWallArchiver(root=str(tmp_path))
        assert archiver.append(_make_snapshot(coin="")) is False
        assert archiver.append(_make_snapshot(ts_sec=-1)) is False

    def test_zone_without_extended_fields_default_zero(self, tmp_path):
        """W1-T4/W2-T1 字段未填充时默认 0/空，不抛异常。"""
        archiver = LiquidityWallArchiver(root=str(tmp_path))
        # 完全裸的 zone（没有任何属性）
        bare_zone = SimpleNamespace(
            side="bid", source="futures_only", dual_source=False,
            price_mid=75000.0, price_low=74950.0, price_high=75050.0,
            current_usd=2e6, max_usd_1h=3e6,
            trust_score=0.55, break_through_risk=0.5,
            active_attack_score=0.0, wall_removal_risk=0.0,
            wall_consumed_confidence=0.0, persistence_score=0.3,
            visible_minutes=10.0, exchange_count=1,
            has_spot_confluence=False, spot_current_usd=0.0,
            coinbase_spot_confluence=False, coinbase_spot_usd=0.0,
            coinbase_num_orders=0, trend="new",
            next_magnet_price=None, sweep_target=None,
            # 故意缺 wall_zone_id / raw_trust_score / SR/SA 等扩展字段
        )
        snap = _make_snapshot(walls_below=[bare_zone])
        assert archiver.append(snap) is True
        day = datetime.now(_TZ_CN).strftime("%Y%m%d")
        rows = [json.loads(line) for line in (tmp_path / "BTC" / f"{day}.jsonl").read_text().splitlines()]
        wall = rows[0]["walls_below"][0]
        assert wall["wall_zone_id"] == ""
        assert wall["raw_trust_score"] == 0.0
        assert wall["trust_components"] == {}
        assert wall["support_resistance_trust_score"] == 0.0
        assert wall["sweep_attractiveness_score"] == 0.0


# ─────────────────────────────────────────────────────────────────
# 4. 磁盘水位保护
# ─────────────────────────────────────────────────────────────────

class TestDiskWatermark:
    def test_high_disk_skips_and_counts_dropped(self, tmp_path):
        archiver = LiquidityWallArchiver(root=str(tmp_path))
        snap = _make_snapshot()

        # 模拟 95% 使用率
        fake_usage = SimpleNamespace(
            total=1_000_000_000_000,
            used=950_000_000_000,
            free=50_000_000_000,
        )
        with patch("shutil.disk_usage", return_value=fake_usage):
            ok = archiver.append(snap)
        assert ok is False

        stats = archiver.stats()
        assert stats["dropped_count"] == 1
        assert stats["disk_high_until_ts"] > 0

    def test_disk_check_failure_does_not_block(self, tmp_path):
        """shutil.disk_usage 抛异常时落盘退化为不保护，仍继续写入。"""
        archiver = LiquidityWallArchiver(root=str(tmp_path))
        snap = _make_snapshot()

        with patch("shutil.disk_usage", side_effect=OSError("simulated")):
            ok = archiver.append(snap)
        assert ok is True


# ─────────────────────────────────────────────────────────────────
# 5. 90 天 GC
# ─────────────────────────────────────────────────────────────────

class TestGarbageCollection:
    def test_old_files_removed(self, tmp_path):
        archiver = LiquidityWallArchiver(root=str(tmp_path), keep_days=30)

        # 先手动放一个 60 天前的文件
        old_day = (datetime.now(_TZ_CN) - timedelta(days=60)).strftime("%Y%m%d")
        old_file = tmp_path / "BTC" / f"{old_day}.jsonl"
        old_file.parent.mkdir(parents=True, exist_ok=True)
        old_file.write_text('{"ts": 1, "coin": "BTC"}\n')

        # 触发 GC（强制 _last_gc_ts 早于 1h）
        archiver._last_gc_ts = 0
        archiver.append(_make_snapshot())

        assert not old_file.exists(), "60 天前文件应被 GC 删除"

    def test_recent_files_preserved(self, tmp_path):
        archiver = LiquidityWallArchiver(root=str(tmp_path), keep_days=30)
        recent_day = (datetime.now(_TZ_CN) - timedelta(days=15)).strftime("%Y%m%d")
        recent_file = tmp_path / "BTC" / f"{recent_day}.jsonl"
        recent_file.parent.mkdir(parents=True, exist_ok=True)
        recent_file.write_text('{"ts": 1, "coin": "BTC"}\n')

        archiver._last_gc_ts = 0
        archiver.append(_make_snapshot())

        assert recent_file.exists(), "15 天前文件应被保留（< 30 天）"


# ─────────────────────────────────────────────────────────────────
# 6. 全局 singleton + reset
# ─────────────────────────────────────────────────────────────────

class TestSingleton:
    def test_singleton_returns_same_instance(self):
        a1 = get_archiver()
        a2 = get_archiver()
        assert a1 is a2

    def test_reset_for_testing_creates_new_instance(self, tmp_path):
        a1 = get_archiver()
        a2 = reset_archiver_for_test(root=str(tmp_path))
        assert a1 is not a2
        assert a2._root == str(tmp_path)

    def test_reset_for_testing_clears_directory(self, tmp_path):
        archiver = reset_archiver_for_test(root=str(tmp_path))
        archiver.append(_make_snapshot())
        archiver.append(_make_snapshot(coin="ETH"))
        assert any(tmp_path.iterdir())
        archiver.reset_for_testing()
        # 目录可能仍存在但应该是空的
        assert not any(tmp_path.iterdir())
        stats = archiver.stats()
        assert stats["success_count"] == 0
        assert stats["error_count"] == 0
        assert stats["dropped_count"] == 0


# ─────────────────────────────────────────────────────────────────
# 7. stats 暴露
# ─────────────────────────────────────────────────────────────────

class TestStats:
    def test_stats_basic(self, tmp_path):
        archiver = LiquidityWallArchiver(root=str(tmp_path), keep_days=60)
        archiver.append(_make_snapshot())
        archiver.append(_make_snapshot(coin="ETH"))
        archiver.append(None)  # 应被拒绝但不算 error（snapshot None 提前 return）
        stats = archiver.stats()
        assert stats["success_count"] == 2
        assert stats["keep_days"] == 60
        assert stats["root"] == str(tmp_path)
