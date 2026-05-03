"""ScalpSignalStore 单测 · 覆盖配置/活跃池/历史归档/缓存/cooldown 查询"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from models.scalp_signal import (
    CalibrationCurve,
    CalibrationPoint,
    EvidenceItem,
    FactorBreakdown,
    GlobalStats,
    ScalpConfig,
    ScalpSignal,
    StrategyConfig,
    StrategyName,
    StrategyStats,
    calc_expiry_ts,
    make_signal_id,
)
from storage.scalp_signal_store import ScalpSignalStore


def _make_signal(
    *,
    coin: str = "BTC",
    strategy: StrategyName = StrategyName.A_SWEEP_RECLAIM,
    direction: str = "down",
    confidence: int = 78,
    state: str = "active",
    created_at: int | None = None,
    ref_price: float = 78400.0,
) -> ScalpSignal:
    ts = created_at if created_at is not None else int(time.time())
    return ScalpSignal(
        signal_id=make_signal_id(coin, strategy, ts, direction),  # type: ignore[arg-type]
        coin=coin,
        horizon_min=30,
        direction=direction,  # type: ignore[arg-type]
        strategy=strategy,
        reference_price=ref_price,
        created_at=ts,
        expiry_ts=calc_expiry_ts(ts, 30),
        confidence=confidence,
        hit_probability=0.65,
        regime="range",
        factor_breakdown=FactorBreakdown(
            core_signal_strength=0.85, multi_tf_alignment=0.7,
            key_level_quality=0.6, data_freshness=0.9, historical_winrate=0.65,
        ),
        evidence=[EvidenceItem(dimension="Sweep", observation="测试", weight="high")],
        state=state,  # type: ignore[arg-type]
    )


@pytest.fixture
def store(tmp_path: Path) -> ScalpSignalStore:
    return ScalpSignalStore(data_dir=str(tmp_path))


# ────────────────────────────────────────────────────────────────────────────
# Config 持久化
# ────────────────────────────────────────────────────────────────────────────

class TestConfigPersistence:
    def test_load_default_when_no_file(self, store: ScalpSignalStore):
        cfg = store.load_config()
        assert isinstance(cfg, ScalpConfig)
        assert cfg.coin == "BTC" and cfg.horizon_min == 30
        assert cfg.test_mode is True
        # 默认全部策略 enabled=False
        for sc in cfg.strategies.values():
            assert sc.enabled is False

    def test_save_and_reload(self, store: ScalpSignalStore):
        cfg = store.load_config()
        cfg.strategies[StrategyName.A_SWEEP_RECLAIM].enabled = True
        cfg.strategies[StrategyName.A_SWEEP_RECLAIM].confidence_threshold = 82
        cfg.notification.email_min_confidence = 90
        store.save_config(cfg)

        reload = store.load_config()
        assert reload.strategies[StrategyName.A_SWEEP_RECLAIM].enabled is True
        assert reload.strategies[StrategyName.A_SWEEP_RECLAIM].confidence_threshold == 82
        assert reload.notification.email_min_confidence == 90

    def test_corrupted_config_falls_back_to_default(self, store: ScalpSignalStore):
        # 写一个损坏的 json
        store.root.mkdir(parents=True, exist_ok=True)
        (store.root / "config.json").write_text("not a valid json {{{")
        cfg = store.load_config()
        assert isinstance(cfg, ScalpConfig)
        assert cfg.coin == "BTC"  # 默认值

    def test_schema_invalid_falls_back_to_default(self, store: ScalpSignalStore):
        # 合法 JSON 但 schema 不对
        store.root.mkdir(parents=True, exist_ok=True)
        (store.root / "config.json").write_text(json.dumps({"unknown_field": 123, "coin": 999}))
        cfg = store.load_config()
        assert cfg.coin == "BTC"

    def test_atomic_write_no_partial_file(self, store: ScalpSignalStore):
        cfg = ScalpConfig()
        store.save_config(cfg)
        # tmp 文件应该已经被 rename 掉
        assert not (store.root / "config.json.tmp").exists()
        assert (store.root / "config.json").exists()


# ────────────────────────────────────────────────────────────────────────────
# 活跃池
# ────────────────────────────────────────────────────────────────────────────

class TestActivePool:
    def test_get_empty_when_no_file(self, store: ScalpSignalStore):
        assert store.get_active() == []

    def test_add_and_get(self, store: ScalpSignalStore):
        s1 = _make_signal(direction="down")
        store.add_active(s1)
        items = store.get_active()
        assert len(items) == 1
        assert items[0].signal_id == s1.signal_id
        assert items[0].direction == "down"

    def test_add_duplicate_raises(self, store: ScalpSignalStore):
        s1 = _make_signal()
        store.add_active(s1)
        with pytest.raises(ValueError, match="already exists"):
            store.add_active(s1)

    def test_update_active(self, store: ScalpSignalStore):
        s = _make_signal(state="active")
        store.add_active(s)
        s.state = "expired_won"  # type: ignore[assignment]
        s.outcome = "won"  # type: ignore[assignment]
        store.update_active(s)
        loaded = store.get_active()
        assert loaded[0].state == "expired_won"
        assert loaded[0].outcome == "won"

    def test_update_non_existent_raises(self, store: ScalpSignalStore):
        with pytest.raises(KeyError):
            store.update_active(_make_signal())

    def test_remove_active(self, store: ScalpSignalStore):
        s = _make_signal()
        store.add_active(s)
        assert store.remove_active(s.signal_id) is True
        assert store.get_active() == []
        # 第二次删除返回 False（不报错）
        assert store.remove_active(s.signal_id) is False

    def test_get_by_id(self, store: ScalpSignalStore):
        s1 = _make_signal(direction="down", created_at=1000)
        s2 = _make_signal(direction="up", created_at=2000, strategy=StrategyName.B_CVD_DIVERGENCE)
        store.add_active(s1)
        store.add_active(s2)
        got = store.get_active_by_id(s2.signal_id)
        assert got is not None and got.direction == "up"
        assert store.get_active_by_id("not_exist") is None

    def test_corrupted_active_returns_empty(self, store: ScalpSignalStore):
        store.root.mkdir(parents=True, exist_ok=True)
        (store.root / "signals_active.json").write_text("garbage")
        assert store.get_active() == []

    def test_partial_invalid_items_skipped(self, store: ScalpSignalStore):
        # 一个合法 + 一个损坏 → 只返回合法的
        s = _make_signal()
        store.root.mkdir(parents=True, exist_ok=True)
        (store.root / "signals_active.json").write_text(json.dumps([
            s.model_dump(mode="json"),
            {"signal_id": "broken", "coin": "BTC"},  # 缺字段
        ]))
        items = store.get_active()
        assert len(items) == 1
        assert items[0].signal_id == s.signal_id


# ────────────────────────────────────────────────────────────────────────────
# 历史归档
# ────────────────────────────────────────────────────────────────────────────

class TestHistoryArchive:
    def test_archive_writes_jsonl(self, store: ScalpSignalStore):
        s = _make_signal(state="expired_won", created_at=1735689600)  # 2025-01-01 UTC
        s.settled_at = 1735691400  # 30min 后
        s.outcome = "won"  # type: ignore[assignment]
        s.settlement_price = 78380.0
        store.archive_signal(s)
        # 文件名按 settled_at 月份
        path = store.root / "signals_history_2025-01.jsonl"
        assert path.exists()
        lines = path.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 1
        obj = json.loads(lines[0])
        assert obj["signal_id"] == s.signal_id
        assert obj["outcome"] == "won"

    def test_archive_appends_multiple(self, store: ScalpSignalStore):
        for i in range(3):
            s = _make_signal(created_at=1735689600 + i, ref_price=78400 + i)
            s.settled_at = 1735691400 + i
            s.outcome = "won"  # type: ignore[assignment]
            store.archive_signal(s)
        path = store.root / "signals_history_2025-01.jsonl"
        lines = path.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 3

    def test_iter_history_filters_and_order(self, store: ScalpSignalStore):
        # 创建跨月信号 + 不同策略
        for i, (created, strat) in enumerate([
            (1735689600, StrategyName.A_SWEEP_RECLAIM),  # 2025-01
            (1738368000, StrategyName.B_CVD_DIVERGENCE),  # 2025-02
            (1738454400, StrategyName.A_SWEEP_RECLAIM),  # 2025-02
        ]):
            s = _make_signal(strategy=strat, created_at=created, ref_price=78000 + i)
            s.settled_at = created + 1800
            s.outcome = "won"  # type: ignore[assignment]
            store.archive_signal(s)

        all_signals = store.iter_history()
        assert len(all_signals) == 3
        # 倒序：最新在前
        assert all_signals[0].created_at == 1738454400
        assert all_signals[-1].created_at == 1735689600

        # 按策略过滤
        only_a = store.iter_history(strategy=StrategyName.A_SWEEP_RECLAIM)
        assert len(only_a) == 2
        assert all(s.strategy == StrategyName.A_SWEEP_RECLAIM for s in only_a)

        # limit
        limited = store.iter_history(limit=2)
        assert len(limited) == 2
        assert limited[0].created_at == 1738454400  # 最新两条

    def test_iter_history_corrupted_line_skipped(self, store: ScalpSignalStore):
        s = _make_signal(created_at=1735689600)
        s.settled_at = 1735691400
        s.outcome = "won"  # type: ignore[assignment]
        store.archive_signal(s)
        # 在文件后追加一条损坏行
        path = store.root / "signals_history_2025-01.jsonl"
        with open(path, "a") as f:
            f.write("not json\n")
        results = store.iter_history()
        assert len(results) == 1
        assert results[0].signal_id == s.signal_id


# ────────────────────────────────────────────────────────────────────────────
# 统计 / Calibration 缓存
# ────────────────────────────────────────────────────────────────────────────

class TestCaches:
    def test_stats_cache_roundtrip(self, store: ScalpSignalStore):
        assert store.load_stats_cache() is None  # 首次为空
        stats = GlobalStats(
            total_signals=10, total_won=7, total_lost=3,
            overall_win_rate=0.7, by_strategy=[
                StrategyStats(strategy=StrategyName.A_SWEEP_RECLAIM, horizon_min=30,
                              total_signals=10, won=7, lost=3, win_rate=0.7),
            ],
            generated_at=int(time.time()),
        )
        store.save_stats_cache(stats)
        loaded = store.load_stats_cache()
        assert loaded is not None
        assert loaded.total_signals == 10
        assert loaded.overall_win_rate == 0.7
        assert loaded.by_strategy[0].strategy == StrategyName.A_SWEEP_RECLAIM

    def test_calibration_cache_roundtrip(self, store: ScalpSignalStore):
        curve = CalibrationCurve(
            overall=[
                CalibrationPoint(confidence_mid=0.725, sample_size=20, actual_win_rate=0.65),
                CalibrationPoint(confidence_mid=0.825, sample_size=15, actual_win_rate=0.80),
            ],
            sample_size_total=35,
            generated_at=int(time.time()),
        )
        store.save_calibration_cache(curve)
        loaded = store.load_calibration_cache()
        assert loaded is not None
        assert len(loaded.overall) == 2
        assert loaded.overall[1].actual_win_rate == 0.80


# ────────────────────────────────────────────────────────────────────────────
# Cooldown 查询
# ────────────────────────────────────────────────────────────────────────────

class TestCooldownQuery:
    def test_no_match_returns_none(self, store: ScalpSignalStore):
        assert store.get_last_signal_ts(StrategyName.A_SWEEP_RECLAIM, "down") is None

    def test_finds_recent_active_signal(self, store: ScalpSignalStore):
        now = int(time.time())
        s = _make_signal(created_at=now - 300, direction="down")
        store.add_active(s)
        ts = store.get_last_signal_ts(StrategyName.A_SWEEP_RECLAIM, "down")
        assert ts == now - 300

    def test_different_direction_no_match(self, store: ScalpSignalStore):
        now = int(time.time())
        s = _make_signal(created_at=now - 300, direction="down")
        store.add_active(s)
        # 查询 up 方向 → 应该找不到
        assert store.get_last_signal_ts(StrategyName.A_SWEEP_RECLAIM, "up") is None

    def test_within_seconds_filter(self, store: ScalpSignalStore):
        now = int(time.time())
        # 老信号（活跃池 + 超出 within_seconds 窗口）
        old = _make_signal(created_at=now - 7200, direction="down")
        store.add_active(old)
        # 默认窗口 24h，应该找得到
        ts = store.get_last_signal_ts(StrategyName.A_SWEEP_RECLAIM, "down", within_seconds=24 * 3600)
        assert ts == now - 7200
        # 收紧窗口到 1h，应该找不到
        ts = store.get_last_signal_ts(StrategyName.A_SWEEP_RECLAIM, "down", within_seconds=3600)
        assert ts is None

    def test_finds_in_history(self, store: ScalpSignalStore):
        now = int(time.time())
        s = _make_signal(created_at=now - 1800, direction="down", state="expired_won")
        s.settled_at = now
        s.outcome = "won"  # type: ignore[assignment]
        store.archive_signal(s)
        ts = store.get_last_signal_ts(StrategyName.A_SWEEP_RECLAIM, "down")
        assert ts == now - 1800

    def test_active_takes_priority_over_history(self, store: ScalpSignalStore):
        """活跃池里有更新的同向信号时，应优先返回活跃池的"""
        now = int(time.time())
        # 历史的早信号
        old = _make_signal(created_at=now - 3600, direction="down", state="expired_won")
        old.settled_at = now - 1800
        old.outcome = "won"  # type: ignore[assignment]
        store.archive_signal(old)
        # 活跃的近信号
        new_active = _make_signal(created_at=now - 300, direction="down")
        store.add_active(new_active)

        ts = store.get_last_signal_ts(StrategyName.A_SWEEP_RECLAIM, "down")
        assert ts == now - 300


# ────────────────────────────────────────────────────────────────────────────
# 全链路：add → update → archive
# ────────────────────────────────────────────────────────────────────────────

class TestEndToEndLifecycle:
    def test_signal_lifecycle_from_active_to_archive(self, store: ScalpSignalStore):
        """模拟真实生命周期：
        1) 信号生成 → add_active
        2) 30min 后到期 → 改 state + 写 settlement → update_active
        3) archive_signal → remove_active
        """
        now = int(time.time())
        s = _make_signal(created_at=now - 1800)
        store.add_active(s)
        assert len(store.get_active()) == 1

        # 模拟结算
        s.state = "expired_won"  # type: ignore[assignment]
        s.outcome = "won"  # type: ignore[assignment]
        s.settlement_price = 78380.0
        s.settled_at = now
        store.update_active(s)

        # 归档 + 清理活跃池
        store.archive_signal(s)
        store.remove_active(s.signal_id)

        assert store.get_active() == []
        history = store.iter_history()
        assert len(history) == 1
        assert history[0].signal_id == s.signal_id
        assert history[0].outcome == "won"
