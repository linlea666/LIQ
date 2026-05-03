"""命中率统计 + 自动停用 · Calibrator

职责：
  - 从 store.iter_history() 读全量历史信号
  - 按策略 / regime / hour / confidence 多维度切片统计
  - 输出 GlobalStats（看板顶部 KPI） + CalibrationCurve（命中率 vs 置信度）
  - 自动停用判定：单策略样本足够 + 命中率 < 临界 → auto_disabled=True

为什么独立模块？
  - dev-constraints #3 选择"独立新写"：项目里无对应"二元方向命中率"统计
  - OpportunityStateMachine 的统计是 RR-based（净利润），不适用

性能：
  - 每次 settle 后调用一次 recompute_stats()
  - 历史 jsonl 按月分片，N 个月信号也是 O(N) 扫描，单次 < 100ms
  - 结果缓存到 stats_cache.json 供 API 直接返回
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from typing import Optional

from models.scalp_signal import (
    BINANCE_EVENT_PAYOUT_RATIO,
    CalibrationCurve,
    CalibrationPoint,
    ConfidenceBucket,
    DEFAULT_STAKE_USDT,
    GlobalStats,
    HorizonMin,
    HourSlice,
    RegimeSlice,
    ScalpSignal,
    StrategyName,
    StrategyStats,
    break_even_win_rate,
    expected_return_per_signal,
)

from storage.scalp_signal_store import ScalpSignalStore

logger = logging.getLogger(__name__)


# 自动停用阈值
AUTO_DISABLE_MIN_SAMPLES = 30           # 至少 30 个已结算（含 push）才考虑停用
AUTO_DISABLE_WIN_RATE_THRESHOLD = 0.50  # 命中率 < 50% → 远低于临界 55.6%（保留 5.6% 安全边际）
WINDOW_DAYS = 30                        # 统计窗口（最近 N 天）

# Confidence 分桶（左闭右开，最后一个桶含 100）
CONFIDENCE_BUCKETS: list[tuple[int, int, str]] = [
    (50, 60, "50-60"),
    (60, 70, "60-70"),
    (70, 75, "70-75"),
    (75, 80, "75-80"),
    (80, 85, "80-85"),
    (85, 90, "85-90"),
    (90, 101, "90-100"),
]


class Calibrator:
    """命中率统计 + 自动停用判定"""

    def __init__(
        self,
        store: ScalpSignalStore,
        *,
        window_days: int = WINDOW_DAYS,
        min_samples_for_disable: int = AUTO_DISABLE_MIN_SAMPLES,
        disable_win_rate_threshold: float = AUTO_DISABLE_WIN_RATE_THRESHOLD,
    ) -> None:
        self._store = store
        self._window_days = window_days
        self._min_samples = min_samples_for_disable
        self._disable_threshold = disable_win_rate_threshold

    # ── 公共 API ──────────────────────────────────────────────

    def recompute_stats(
        self,
        *,
        coin: Optional[str] = None,
        horizon_min: Optional[int] = None,
        now_ts: Optional[int] = None,
    ) -> GlobalStats:
        """重新计算 GlobalStats，写入 store 缓存"""
        now = now_ts if now_ts is not None else int(time.time())
        cutoff = now - self._window_days * 24 * 3600

        history = self._store.iter_history(coin=coin, horizon_min=horizon_min, since_ts=cutoff)

        # 按策略分组
        by_strategy: dict[StrategyName, list[ScalpSignal]] = defaultdict(list)
        for sig in history:
            by_strategy[sig.strategy].append(sig)

        strategy_stats_list: list[StrategyStats] = []
        for strategy, signals in by_strategy.items():
            stats = self._compute_one_strategy(strategy, signals, now)
            strategy_stats_list.append(stats)

        # 全局聚合
        total = sum(s.total_signals for s in strategy_stats_list)
        won = sum(s.won for s in strategy_stats_list)
        lost = sum(s.lost for s in strategy_stats_list)
        push = sum(s.push for s in strategy_stats_list)

        decided = won + lost
        overall_wr = won / decided if decided > 0 else None
        overall_net = (
            expected_return_per_signal(overall_wr) * decided if overall_wr is not None else None
        )

        # P0-4 全局 shadow 聚合
        shadow_total = sum(s.shadow_total for s in strategy_stats_list)
        shadow_won = sum(s.shadow_won for s in strategy_stats_list)
        shadow_lost = sum(s.shadow_lost for s in strategy_stats_list)
        shadow_decided = shadow_won + shadow_lost
        shadow_wr = shadow_won / shadow_decided if shadow_decided > 0 else None

        global_stats = GlobalStats(
            total_signals=total,
            total_won=won,
            total_lost=lost,
            total_push=push,
            overall_win_rate=overall_wr,
            overall_net_return_usdt=overall_net,
            overall_shadow_total=shadow_total,
            overall_shadow_win_rate=shadow_wr,
            by_strategy=strategy_stats_list,
            generated_at=now,
        )

        self._store.save_stats_cache(global_stats)
        return global_stats

    def recompute_calibration(
        self,
        *,
        coin: Optional[str] = None,
        horizon_min: Optional[int] = None,
        now_ts: Optional[int] = None,
    ) -> CalibrationCurve:
        """重新计算 CalibrationCurve，写入 store 缓存"""
        now = now_ts if now_ts is not None else int(time.time())
        cutoff = now - self._window_days * 24 * 3600
        history = self._store.iter_history(coin=coin, horizon_min=horizon_min, since_ts=cutoff)

        # 全局曲线
        overall_buckets = self._build_buckets(history)
        overall_points = [
            CalibrationPoint(
                confidence_mid=(low + high - 1) / 2.0 / 100.0,
                sample_size=b.sample_size,
                actual_win_rate=b.actual_win_rate or 0.0,
            )
            for (low, high, _label), b in zip(CONFIDENCE_BUCKETS, overall_buckets)
            if b.sample_size > 0 and b.actual_win_rate is not None
        ]

        # 各策略独立曲线
        by_strategy_points: dict[str, list[CalibrationPoint]] = {}
        by_strategy: dict[StrategyName, list[ScalpSignal]] = defaultdict(list)
        for sig in history:
            by_strategy[sig.strategy].append(sig)
        for strategy, signals in by_strategy.items():
            buckets = self._build_buckets(signals)
            points = [
                CalibrationPoint(
                    confidence_mid=(low + high - 1) / 2.0 / 100.0,
                    sample_size=b.sample_size,
                    actual_win_rate=b.actual_win_rate or 0.0,
                )
                for (low, high, _label), b in zip(CONFIDENCE_BUCKETS, buckets)
                if b.sample_size > 0 and b.actual_win_rate is not None
            ]
            if points:
                by_strategy_points[strategy.value] = points

        curve = CalibrationCurve(
            overall=overall_points,
            by_strategy=by_strategy_points,
            sample_size_total=len(history),
            generated_at=now,
        )
        self._store.save_calibration_cache(curve)
        return curve

    def historical_winrate_for(self, strategy: StrategyName) -> Optional[float]:
        """快速查询单策略历史命中率（供 ConfidenceScorer 使用）

        从 stats_cache 读，避免每次扫历史
        """
        cache = self._store.load_stats_cache()
        if cache is None:
            return None
        for s in cache.by_strategy:
            if s.strategy == strategy:
                return s.win_rate
        return None

    def historical_winrate_with_sample_size(
        self,
        strategy: StrategyName,
    ) -> tuple[Optional[float], int]:
        """P0-3：返回 (win_rate, decided_sample_size)

        decided_sample_size = won + lost（不含 push / cancelled）
        样本量用于 ConfidenceScorer 做样本量惩罚 blending
        """
        cache = self._store.load_stats_cache()
        if cache is None:
            return None, 0
        for s in cache.by_strategy:
            if s.strategy == strategy:
                return s.win_rate, int(s.won + s.lost)
        return None, 0

    def lookup_calibrated_probability(
        self,
        strategy: StrategyName,
        confidence: int,
    ) -> tuple[Optional[float], int]:
        """P0-2：根据 confidence 查 calibration bucket，返回 (actual_win_rate, sample_size)

        - 优先查"该策略"独立曲线；退化到"全局"曲线
        - 任何一档样本不足由调用方根据 CALIBRATION_MIN_BUCKET_SAMPLES 判断
        """
        cache = self._store.load_calibration_cache()
        if cache is None:
            return None, 0
        bucket = self._find_bucket(confidence)
        if bucket is None:
            return None, 0
        low, high, _label = bucket
        bucket_mid_min = (low + high - 1) / 2.0 / 100.0

        def _match(points) -> tuple[Optional[float], int]:
            for p in points:
                if abs(p.confidence_mid - bucket_mid_min) < 1e-6:
                    return float(p.actual_win_rate), int(p.sample_size)
            return None, 0

        # 1) 策略独立曲线
        strategy_points = cache.by_strategy.get(strategy.value, [])
        wr, n = _match(strategy_points)
        if n > 0:
            return wr, n
        # 2) 全局曲线
        return _match(cache.overall)

    @staticmethod
    def _find_bucket(confidence: int) -> Optional[tuple[int, int, str]]:
        for low, high, label in CONFIDENCE_BUCKETS:
            if low <= confidence < high:
                return (low, high, label)
        return None

    # ── 私有 ──────────────────────────────────────────────────

    def _compute_one_strategy(
        self,
        strategy: StrategyName,
        signals: list[ScalpSignal],
        now_ts: int,
    ) -> StrategyStats:
        """单策略统计聚合（P0-4 增加 shadow window 双口径）"""
        # 主口径（state ∈ {expired_won, expired_lost, expired_push}）
        won = sum(1 for s in signals if s.outcome == "won" and s.state != "cancelled")
        lost = sum(1 for s in signals if s.outcome == "lost" and s.state != "cancelled")
        push = sum(1 for s in signals if s.outcome == "push" and s.state != "cancelled")
        cancelled = sum(1 for s in signals if s.state == "cancelled")
        decided = won + lost
        wr = won / decided if decided > 0 else None

        avg_conf = (
            sum(s.confidence for s in signals) / len(signals) if signals else None
        )

        net_per_signal = (
            expected_return_per_signal(
                wr,
                odds_payout=BINANCE_EVENT_PAYOUT_RATIO,
                stake=DEFAULT_STAKE_USDT,
            )
            if wr is not None else None
        )

        # P0-4 Shadow 口径：cancelled 且已 shadow 结算的
        shadow_signals = [s for s in signals if s.state == "cancelled" and s.shadow_outcome is not None]
        shadow_won = sum(1 for s in shadow_signals if s.shadow_outcome == "won")
        shadow_lost = sum(1 for s in shadow_signals if s.shadow_outcome == "lost")
        shadow_decided = shadow_won + shadow_lost
        shadow_wr = shadow_won / shadow_decided if shadow_decided > 0 else None
        shadow_breakdown: dict[str, int] = {}
        for s in shadow_signals:
            kind = s.invalidation_kind or "unknown"
            shadow_breakdown[kind] = shadow_breakdown.get(kind, 0) + 1

        # 多维度切片（_build_buckets 已过滤 outcome ∈ {won/lost/push}，cancelled 不会污染）
        confidence_buckets = self._build_buckets(signals)
        regime_slices = self._build_regime_slices(signals)
        hour_slices = self._build_hour_slices(signals)

        # horizon（取众数；通常一个策略只用一个 horizon）
        horizon = signals[0].horizon_min if signals else 30
        # 自动停用判定
        auto_disabled, reason = self._evaluate_auto_disable(decided, wr)

        return StrategyStats(
            strategy=strategy,
            horizon_min=horizon,  # type: ignore[arg-type]
            total_signals=len(signals),
            won=won,
            lost=lost,
            push=push,
            cancelled=cancelled,
            win_rate=wr,
            avg_confidence=avg_conf,
            net_return_per_signal_usdt=net_per_signal,
            confidence_buckets=confidence_buckets,
            by_regime=regime_slices,
            by_hour_utc=hour_slices,
            window_days=self._window_days,
            last_window_sample_size=len(signals),
            shadow_total=len(shadow_signals),
            shadow_won=shadow_won,
            shadow_lost=shadow_lost,
            shadow_win_rate=shadow_wr,
            shadow_breakdown_by_kind=shadow_breakdown,
            auto_disabled=auto_disabled,
            auto_disabled_reason=reason,
            auto_disabled_at=now_ts if auto_disabled else None,
            generated_at=now_ts,
        )

    @staticmethod
    def _build_buckets(signals: list[ScalpSignal]) -> list[ConfidenceBucket]:
        """按置信度区间分桶（仅统计 won/lost/push 已结算的，不含 cancelled / active）"""
        out: list[ConfidenceBucket] = []
        for low, high, label in CONFIDENCE_BUCKETS:
            in_bucket = [s for s in signals if low <= s.confidence < high
                         and s.outcome in ("won", "lost", "push")]
            won = sum(1 for s in in_bucket if s.outcome == "won")
            lost = sum(1 for s in in_bucket if s.outcome == "lost")
            push = sum(1 for s in in_bucket if s.outcome == "push")
            decided = won + lost
            actual = won / decided if decided > 0 else None
            expected = (low + high - 1) / 2.0 / 100.0
            out.append(ConfidenceBucket(
                range_label=label,
                sample_size=len(in_bucket),
                won_count=won,
                lost_count=lost,
                push_count=push,
                actual_win_rate=actual,
                expected_win_rate=expected,
            ))
        return out

    @staticmethod
    def _build_regime_slices(signals: list[ScalpSignal]) -> list[RegimeSlice]:
        """按 regime 切片"""
        groups: dict[str, list[ScalpSignal]] = defaultdict(list)
        for s in signals:
            groups[s.regime or "unknown"].append(s)
        out: list[RegimeSlice] = []
        for regime, ss in groups.items():
            won = sum(1 for x in ss if x.outcome == "won")
            lost = sum(1 for x in ss if x.outcome == "lost")
            push = sum(1 for x in ss if x.outcome == "push")
            decided = won + lost
            wr = won / decided if decided > 0 else None
            out.append(RegimeSlice(
                regime=regime, sample_size=len(ss),
                won=won, lost=lost, push=push, win_rate=wr,
            ))
        return sorted(out, key=lambda x: -x.sample_size)

    @staticmethod
    def _build_hour_slices(signals: list[ScalpSignal]) -> list[HourSlice]:
        """按 UTC 小时切片（识别强/弱时段）"""
        groups: dict[int, list[ScalpSignal]] = defaultdict(list)
        for s in signals:
            hour = time.gmtime(s.created_at).tm_hour
            groups[hour].append(s)
        out: list[HourSlice] = []
        for hour, ss in sorted(groups.items()):
            won = sum(1 for x in ss if x.outcome == "won")
            lost = sum(1 for x in ss if x.outcome == "lost")
            decided = won + lost
            wr = won / decided if decided > 0 else None
            out.append(HourSlice(
                hour_utc=hour, sample_size=len(ss),
                won=won, lost=lost, win_rate=wr,
            ))
        return out

    def _evaluate_auto_disable(
        self,
        decided: int,
        win_rate: Optional[float],
    ) -> tuple[bool, Optional[str]]:
        """判断是否触发自动停用

        条件：
          - 已结算（won + lost）≥ min_samples
          - win_rate < disable_threshold（远低于临界 55.6%）

        push 不计入决定数（push 不影响盈亏）
        """
        if decided < self._min_samples:
            return False, None
        if win_rate is None:
            return False, None
        if win_rate >= self._disable_threshold:
            return False, None
        be = break_even_win_rate()
        return True, (
            f"win_rate={win_rate:.3f} < threshold {self._disable_threshold:.2f} "
            f"(临界点 {be:.3f}, samples={decided})"
        )
