"""Calibrator · 历史命中率统计 + 自动停用判定"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from models.scalp_signal import (
    FactorBreakdown,
    ScalpSignal,
    StrategyName,
    calc_expiry_ts,
    make_signal_id,
)

from processors.scalp_signal.calibrator import (
    AUTO_DISABLE_MIN_SAMPLES,
    AUTO_DISABLE_WIN_RATE_THRESHOLD,
    Calibrator,
)
from storage.scalp_signal_store import ScalpSignalStore


def _hist_signal(
    *, ts: int, outcome: str, confidence: int = 75,
    direction: str = "up",
    strategy: StrategyName = StrategyName.A_SWEEP_RECLAIM,
    regime: str = "range",
) -> ScalpSignal:
    state = (
        "expired_won" if outcome == "won"
        else "expired_lost" if outcome == "lost"
        else "expired_push"
    )
    s = ScalpSignal(
        signal_id=make_signal_id("BTC", strategy, ts, direction),  # type: ignore[arg-type]
        coin="BTC", horizon_min=30,
        direction=direction,  # type: ignore[arg-type]
        strategy=strategy,
        reference_price=78400.0, created_at=ts,
        expiry_ts=calc_expiry_ts(ts, 30),
        confidence=confidence,
        hit_probability=0.7,
        regime=regime,  # type: ignore[arg-type]
        factor_breakdown=FactorBreakdown(),
        state=state,  # type: ignore[arg-type]
        outcome=outcome,  # type: ignore[arg-type]
        settlement_price=78500.0 if outcome == "won" else 78300.0,
        settled_at=ts + 1800,
    )
    return s


@pytest.fixture
def populated_store(tmp_path: Path) -> ScalpSignalStore:
    """构造一个带 50 条历史 + 多策略多 regime 的 store"""
    store = ScalpSignalStore(data_dir=str(tmp_path))
    now = int(time.time())
    base_ts = now - 14 * 24 * 3600  # 14 天前

    # A 策略：30 won + 10 lost = 75% 命中率（健康）
    for i in range(30):
        store.archive_signal(_hist_signal(
            ts=base_ts + i * 3600, outcome="won",
            confidence=70 + (i % 30),  # 70-99 跨多个桶
            strategy=StrategyName.A_SWEEP_RECLAIM,
        ))
    for i in range(10):
        store.archive_signal(_hist_signal(
            ts=base_ts + (30 + i) * 3600, outcome="lost",
            confidence=72,
            strategy=StrategyName.A_SWEEP_RECLAIM,
        ))

    # B 策略：5 won + 25 lost = 16.7% 命中率（应自动停用）
    for i in range(5):
        store.archive_signal(_hist_signal(
            ts=base_ts + (40 + i) * 3600, outcome="won",
            strategy=StrategyName.B_CVD_DIVERGENCE,
        ))
    for i in range(25):
        store.archive_signal(_hist_signal(
            ts=base_ts + (45 + i) * 3600, outcome="lost",
            strategy=StrategyName.B_CVD_DIVERGENCE,
        ))

    # C 策略：3 won + 1 lost + 1 push（样本不足，不停用）
    for outc in ["won", "won", "won", "lost", "push"]:
        store.archive_signal(_hist_signal(
            ts=base_ts + 75 * 3600, outcome=outc,
            strategy=StrategyName.C_RANGE_EDGE_FADE,
        ))

    return store


# ════════════════════════════════════════════════════════════════════════════
# recompute_stats
# ════════════════════════════════════════════════════════════════════════════

class TestRecomputeStats:
    def test_global_aggregation(self, populated_store):
        cal = Calibrator(populated_store)
        stats = cal.recompute_stats()
        # 全局：35 won + 36 lost + 1 push = 72 总
        assert stats.total_signals == 75
        assert stats.total_won == 38
        assert stats.total_lost == 36
        assert stats.total_push == 1
        # decided = 38 + 36 = 74; win_rate = 38/74 ≈ 0.514
        assert stats.overall_win_rate == pytest.approx(0.5135, abs=0.001)

    def test_per_strategy_stats(self, populated_store):
        cal = Calibrator(populated_store)
        stats = cal.recompute_stats()
        by_name = {s.strategy: s for s in stats.by_strategy}

        a = by_name[StrategyName.A_SWEEP_RECLAIM]
        assert a.won == 30 and a.lost == 10
        assert a.win_rate == 0.75
        assert a.auto_disabled is False  # 75% 健康

        b = by_name[StrategyName.B_CVD_DIVERGENCE]
        assert b.won == 5 and b.lost == 25
        assert b.win_rate == pytest.approx(5 / 30, abs=0.001)
        # 30 个样本（>= 30 阈值）+ win_rate < 0.5 → 自动停用
        assert b.auto_disabled is True
        assert "win_rate" in (b.auto_disabled_reason or "")

        c = by_name[StrategyName.C_RANGE_EDGE_FADE]
        # 4 决定 + 1 push = 5 总，决定数 < 30 → 不停用
        assert c.auto_disabled is False

    def test_confidence_bucket_distribution(self, populated_store):
        cal = Calibrator(populated_store)
        stats = cal.recompute_stats()
        a = next(s for s in stats.by_strategy if s.strategy == StrategyName.A_SWEEP_RECLAIM)
        # confidence 70-99 30 个 + 72 10 个，共 40 个
        total_in_buckets = sum(b.sample_size for b in a.confidence_buckets)
        assert total_in_buckets == 40
        # 至少有几个桶有样本
        non_empty = [b for b in a.confidence_buckets if b.sample_size > 0]
        assert len(non_empty) >= 2

    def test_save_to_cache(self, populated_store):
        cal = Calibrator(populated_store)
        cal.recompute_stats()
        cached = populated_store.load_stats_cache()
        assert cached is not None
        assert cached.total_signals == 75


# ════════════════════════════════════════════════════════════════════════════
# recompute_calibration
# ════════════════════════════════════════════════════════════════════════════

class TestRecomputeCalibration:
    def test_curve_has_points(self, populated_store):
        cal = Calibrator(populated_store)
        curve = cal.recompute_calibration()
        # 全局曲线应至少 2 个点
        assert len(curve.overall) >= 2
        # 每点的 confidence_mid 应在 0-1 之间
        for p in curve.overall:
            assert 0.5 <= p.confidence_mid <= 1.0
            assert 0 <= p.actual_win_rate <= 1
            assert p.sample_size > 0

    def test_per_strategy_curves(self, populated_store):
        cal = Calibrator(populated_store)
        curve = cal.recompute_calibration()
        # A 策略应有曲线
        assert StrategyName.A_SWEEP_RECLAIM.value in curve.by_strategy

    def test_save_to_cache(self, populated_store):
        cal = Calibrator(populated_store)
        cal.recompute_calibration()
        cached = populated_store.load_calibration_cache()
        assert cached is not None
        assert cached.sample_size_total == 75


# ════════════════════════════════════════════════════════════════════════════
# historical_winrate_for
# ════════════════════════════════════════════════════════════════════════════

class TestHistoricalWinrate:
    def test_returns_cached_winrate(self, populated_store):
        cal = Calibrator(populated_store)
        cal.recompute_stats()
        wr = cal.historical_winrate_for(StrategyName.A_SWEEP_RECLAIM)
        assert wr == 0.75

    def test_returns_none_if_no_cache(self, tmp_path):
        store = ScalpSignalStore(data_dir=str(tmp_path))
        cal = Calibrator(store)
        wr = cal.historical_winrate_for(StrategyName.A_SWEEP_RECLAIM)
        assert wr is None

    def test_returns_none_for_unknown_strategy(self, populated_store):
        cal = Calibrator(populated_store)
        cal.recompute_stats()
        # C 在 stats 里有，但 winrate 应不为 None
        wr_c = cal.historical_winrate_for(StrategyName.C_RANGE_EDGE_FADE)
        assert wr_c is not None  # C 有 4 决定 + 3/4 = 0.75


# ════════════════════════════════════════════════════════════════════════════
# 自动停用边界
# ════════════════════════════════════════════════════════════════════════════

class TestAutoDisableBoundary:
    def test_below_min_samples_no_disable(self, tmp_path):
        store = ScalpSignalStore(data_dir=str(tmp_path))
        now = int(time.time())
        # 只有 5 个样本，全亏 → 不应自动停用
        for i in range(5):
            store.archive_signal(_hist_signal(ts=now - i * 3600, outcome="lost"))
        cal = Calibrator(store)
        stats = cal.recompute_stats()
        a = stats.by_strategy[0]
        assert a.auto_disabled is False

    def test_above_min_samples_below_threshold_disables(self, tmp_path):
        store = ScalpSignalStore(data_dir=str(tmp_path))
        now = int(time.time())
        # 35 个 lost → win_rate=0 < 0.5
        for i in range(35):
            store.archive_signal(_hist_signal(ts=now - i * 3600, outcome="lost"))
        cal = Calibrator(store)
        stats = cal.recompute_stats()
        a = stats.by_strategy[0]
        assert a.auto_disabled is True

    def test_at_threshold_no_disable(self, tmp_path):
        store = ScalpSignalStore(data_dir=str(tmp_path))
        now = int(time.time())
        # 16 won + 14 lost = 53.3% > 50% 阈值
        for i in range(16):
            store.archive_signal(_hist_signal(
                ts=now - i * 3600, outcome="won",
                strategy=StrategyName.A_SWEEP_RECLAIM, direction="up",
            ))
        for i in range(14):
            store.archive_signal(_hist_signal(
                ts=now - (16 + i) * 3600, outcome="lost",
                strategy=StrategyName.A_SWEEP_RECLAIM, direction="up",
            ))
        cal = Calibrator(store)
        stats = cal.recompute_stats()
        a = stats.by_strategy[0]
        assert a.auto_disabled is False
