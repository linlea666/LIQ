"""置信度评分器 · 5 因子加权 → confidence (0-100) + 校准命中率

设计原则（dev-constraints #4 一次到位）：
  - 中央化 confidence 计算逻辑（策略只输出 raw_strength + evidence）
  - 公式可解释、可校准（每个因子独立可观测）
  - 不依赖未来信息（仅消费 state 当前快照）

5 因子 + 权重：
  | 因子                | 权重  | 含义                                              |
  |--------------------|-------|--------------------------------------------------|
  | core_signal_strength | 0.40 | 策略本身硬证据强度（candidate.raw_strength）     |
  | multi_tf_alignment   | 0.25 | bias 方向与 candidate 方向是否一致               |
  | key_level_quality    | 0.15 | 最近 KL 的 final_score 归一                      |
  | data_freshness       | 0.10 | ticker / cvd / candle 时效性                     |
  | historical_winrate   | 0.10 | 该策略历史命中率（冷启动 0.55 + 样本量 blended） |

confidence = round(weighted_score × 100)

hit_probability 取值（P0-2 改造）：
  - 优先：calibration_lookup(strategy, confidence) → 桶内 actual_win_rate
    若样本 ≥ CALIBRATION_MIN_BUCKET_SAMPLES → calibrated（含 sample_size）
  - 否则：返回 None + source="uncalibrated"，前端显"未校准"
  - 不再使用 0.5 + (c - 50) × 0.7 的伪线性映射（GPT 审查 EXTRA-3）

historical_winrate（P0-3 改造）：
  - 冷启动默认 0.55（= 临界值，不再过度乐观给 0.70）
  - blended：effective = winrate × min(1, n/100) + DEFAULT × (1 - min(1, n/100))
    n < 100 时 DEFAULT 主导；n ≥ 100 完全用真实命中率
  - factor_breakdown 同时记录 sample_size 给前端透明展示
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from models.scalp_signal import (
    CALIBRATION_MIN_BUCKET_SAMPLES,
    EvidenceItem,
    FactorBreakdown,
    HitProbabilitySource,
    StrategyName,
)

from processors.scalp_signal.base_strategy import StrategyCandidate, StrategyContext

logger = logging.getLogger(__name__)


# 数据新鲜度阈值（秒）
TICKER_STALE_SEC = 60                 # ticker 超过 60s 算 stale（实际 polling 15s）
CVD_STALE_SEC = 90                    # cvd 90s（实际 polling 60s）
CANDLE_15M_STALE_SEC = 16 * 60        # 15m K 线 16min（容忍 1min 偏移）

# P0-3 修复：冷启动从 0.70 → 0.55（= 临界值，避免 confidence 过度乐观）
DEFAULT_HISTORICAL_WINRATE = 0.55
# P0-3 样本量惩罚饱和阈值：≥ N 个已结算样本时，完全使用真实命中率
HISTORICAL_WINRATE_FULL_TRUST_SAMPLES = 100


def _blend_historical_winrate(
    real_winrate: Optional[float],
    sample_size: int,
    *,
    default: float = DEFAULT_HISTORICAL_WINRATE,
    full_trust_n: int = HISTORICAL_WINRATE_FULL_TRUST_SAMPLES,
) -> tuple[float, bool]:
    """样本量加权 blended winrate（P0-3）

    公式：
      trust = min(1, n / full_trust_n)
      effective = real × trust + default × (1 - trust)

    Returns:
        (effective_wr, blended_with_default)
        blended_with_default = True 表示样本不足，混入了默认值
    """
    if real_winrate is None or sample_size <= 0:
        return default, True
    real = max(0.0, min(1.0, real_winrate))
    trust = min(1.0, sample_size / max(1, full_trust_n))
    effective = real * trust + default * (1.0 - trust)
    return float(max(0.0, min(1.0, effective))), trust < 1.0


@dataclass
class ScoringResult:
    """评分结果

    hit_probability is None ⇔ source == "uncalibrated"
    """
    factor_breakdown: FactorBreakdown
    confidence: int                              # 0-100
    hit_probability: Optional[float]             # P0-2: None 表示未校准
    hit_probability_source: HitProbabilitySource = "uncalibrated"
    calibration_sample_size: int = 0             # 当前 confidence 桶内样本量
    extra_evidence: list[EvidenceItem] = field(default_factory=list)


# 类型签名：(strategy, confidence_score) → (calibrated_winrate | None, bucket_sample_size)
CalibrationLookup = Callable[[StrategyName, int], tuple[Optional[float], int]]


class ConfidenceScorer:
    """置信度评分器 · 无状态 score(candidate, ctx, ...) → ScoringResult"""

    def __init__(self, *, default_winrate: float = DEFAULT_HISTORICAL_WINRATE) -> None:
        # 不允许外部把默认值改得太乐观（P0-3 安全栓）
        self._default_winrate = max(0.50, min(0.65, default_winrate))

    def score(
        self,
        candidate: StrategyCandidate,
        ctx: StrategyContext,
        *,
        strategy_name: Optional[StrategyName] = None,
        historical_winrate: Optional[float] = None,
        historical_winrate_sample_size: int = 0,
        calibration_lookup: Optional[CalibrationLookup] = None,
        now_ts: Optional[int] = None,
    ) -> ScoringResult:
        """打分

        Args:
            candidate: 策略 detect 输出
            ctx: 策略上下文（含 bias_score / bias_components / state）
            historical_winrate: 该策略历史命中率（None 时用默认 0.55）
            historical_winrate_sample_size: 用于 P0-3 样本量惩罚
            calibration_lookup: P0-2 calibration 查表回调；不传 → hit_probability=None
            now_ts: 评估基准时间（秒级 unix），None 时取 time.time()

        Returns:
            ScoringResult
        """
        now = now_ts if now_ts is not None else int(time.time())
        extra_evidence: list[EvidenceItem] = []

        # 因子 1：核心信号强度（直接复用 candidate.raw_strength）
        core = float(max(0.0, min(1.0, candidate.raw_strength)))

        # 因子 2：多周期对齐
        align, align_evidence = self._score_alignment(candidate, ctx)
        if align_evidence:
            extra_evidence.append(align_evidence)

        # 因子 3：KL 质量
        kl_quality, kl_evidence = self._score_key_level(candidate, ctx)
        if kl_evidence:
            extra_evidence.append(kl_evidence)

        # 因子 4：数据新鲜度
        freshness, freshness_evidence = self._score_freshness(ctx, now=now)
        if freshness_evidence:
            extra_evidence.append(freshness_evidence)

        # 因子 5：历史命中率（P0-3 blended with default）
        wr_effective, blended = _blend_historical_winrate(
            historical_winrate,
            historical_winrate_sample_size,
            default=self._default_winrate,
            full_trust_n=HISTORICAL_WINRATE_FULL_TRUST_SAMPLES,
        )

        breakdown = FactorBreakdown(
            core_signal_strength=core,
            multi_tf_alignment=align,
            key_level_quality=kl_quality,
            data_freshness=freshness,
            historical_winrate=wr_effective,
            historical_winrate_sample_size=int(max(0, historical_winrate_sample_size)),
            historical_winrate_blended_with_default=blended,
        )
        weighted = breakdown.weighted_score
        confidence = int(round(weighted * 100))
        confidence = max(0, min(100, confidence))

        # hit_probability：P0-2 calibration-driven，样本不足返 None
        hit_p: Optional[float] = None
        hit_source: HitProbabilitySource = "uncalibrated"
        bucket_n = 0
        if calibration_lookup is not None and strategy_name is not None:
            try:
                calibrated_p, bucket_n = calibration_lookup(strategy_name, confidence)
            except Exception as exc:
                logger.warning(
                    "calibration_lookup failed: strategy=%s conf=%s err=%s",
                    strategy_name, confidence, exc,
                )
                calibrated_p, bucket_n = None, 0
            if (
                calibrated_p is not None
                and bucket_n >= CALIBRATION_MIN_BUCKET_SAMPLES
            ):
                hit_p = float(max(0.0, min(1.0, calibrated_p)))
                hit_source = "calibrated"

        return ScoringResult(
            factor_breakdown=breakdown,
            confidence=confidence,
            hit_probability=hit_p,
            hit_probability_source=hit_source,
            calibration_sample_size=int(max(0, bucket_n)),
            extra_evidence=extra_evidence,
        )

    # ── 因子计算 ──────────────────────────────────────────────

    def _score_alignment(
        self,
        candidate: StrategyCandidate,
        ctx: StrategyContext,
    ) -> tuple[float, Optional[EvidenceItem]]:
        """多周期对齐分

        - direction=up 时：bias_score 正向越大，分越高
        - direction=down 时：bias_score 负向越大，分越高
        - 完全反向（candidate.up 但 bias_score 强负）：扣到 0
        """
        signed = ctx.bias_score if candidate.direction == "up" else -ctx.bias_score
        # signed ∈ [-1, +1]，映射到 [0, 1]
        align = max(0.0, min(1.0, (signed + 1.0) / 2.0))

        # 给一条证据（可读）
        if ctx.bias_components:
            tag = "对齐" if signed >= 0.2 else ("中性" if abs(signed) < 0.2 else "逆向")
            ev = EvidenceItem(
                dimension="MTF",
                observation=f"多周期偏置={ctx.bias_score:+.2f} vs 方向={candidate.direction}（{tag}）",
                score_contribution=align,
                weight="high" if abs(signed) >= 0.4 else "medium",
            )
            return align, ev
        return align, None

    def _score_key_level(
        self,
        candidate: StrategyCandidate,
        ctx: StrategyContext,
    ) -> tuple[float, Optional[EvidenceItem]]:
        """KL 质量分 · 找离 reference_price 最近 1.0% 内的 KL，取 final_score / 100"""
        snap = getattr(ctx.state, "key_level_snapshot_v2", None)
        if snap is None:
            return 0.0, None
        levels = getattr(snap, "levels", None) or getattr(snap, "key_levels", None) or []
        if not levels:
            return 0.0, None

        ref = candidate.reference_price
        if ref <= 0:
            return 0.0, None

        # 最近 1.0% 内的 KL（按距离排序）
        nearby: list[tuple[float, Any]] = []
        for lv in levels:
            lv_price = getattr(lv, "price", 0.0)
            if lv_price <= 0:
                continue
            dist_pct = abs(lv_price - ref) / ref * 100.0
            if dist_pct > 1.0:
                continue
            nearby.append((dist_pct, lv))

        if not nearby:
            return 0.0, None

        nearby.sort(key=lambda x: x[0])
        _, best = nearby[0]
        score = float(getattr(best, "final_score", 0.0) or 0.0) / 100.0
        score = max(0.0, min(1.0, score))

        # is_stale 衰减
        if getattr(best, "is_stale", False):
            score *= 0.7

        ev = EvidenceItem(
            dimension="KeyLevel",
            observation=(
                f"最近 KL @ ${getattr(best, 'price', 0):.0f} | "
                f"tier={getattr(best, 'strength_tier', '')} | "
                f"final_score={getattr(best, 'final_score', 0):.0f}"
                f"{' · stale' if getattr(best, 'is_stale', False) else ''}"
            ),
            score_contribution=score,
            weight="high" if score >= 0.7 else "medium",
        )
        return score, ev

    def _score_freshness(
        self,
        ctx: StrategyContext,
        *,
        now: int,
    ) -> tuple[float, Optional[EvidenceItem]]:
        """数据新鲜度 · 三段式：ticker / cvd / 15m K 线

        所有数据都新鲜 → 1.0
        缺一项 → 0.7
        缺两项 → 0.4
        全缺 → 0.0
        """
        state = ctx.state
        tags: list[str] = []
        ok_count = 0

        ticker = getattr(state, "ticker", None)
        if ticker is not None:
            ts = getattr(ticker, "ts", 0)
            if ts and now - ts < TICKER_STALE_SEC:
                ok_count += 1
                tags.append("ticker:OK")
            else:
                tags.append(f"ticker:{now - ts}s 旧" if ts else "ticker:无")

        cvd = getattr(state, "cvd_contract", None)
        if cvd is not None:
            ts = getattr(cvd, "ts", 0)
            if ts and now - ts < CVD_STALE_SEC:
                ok_count += 1
                tags.append("CVD:OK")
            else:
                tags.append(f"CVD:{now - ts}s 旧" if ts else "CVD:无")

        candles = getattr(state, "candles_15m", None) or []
        if candles:
            last = candles[-1]
            ts = getattr(last, "ts", 0)
            if ts and now - ts < CANDLE_15M_STALE_SEC:
                ok_count += 1
                tags.append("15mK:OK")
            else:
                tags.append(f"15mK:{(now - ts) // 60}min 旧" if ts else "15mK:无")

        if ok_count == 3:
            score = 1.0
        elif ok_count == 2:
            score = 0.7
        elif ok_count == 1:
            score = 0.4
        else:
            score = 0.0

        ev = EvidenceItem(
            dimension="DataFreshness",
            observation=" | ".join(tags) if tags else "无可检测数据源",
            score_contribution=score,
            weight="medium",
        )
        return score, ev
