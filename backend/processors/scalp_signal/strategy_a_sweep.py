"""策略 A · Sweep & Reclaim（扫单回归）

策略哲学（dev-constraints #1 根因优先）：
  关键位被扫破后快速回收 = 流动性已被吃掉，反向力量不再被压制
  + 反转 K 线形态（pin bar / engulfing）确认 = 主力意图明确
  → 短期内大概率向相反方向运动（30min 内回收损失）

逻辑链路：
  1) state.key_level_snapshot_v2.levels 中存在 KL，状态 ∈ {swept, fake_break}
  2) KL 距离 reference_price ≤ 0.5%（足够近，价格还在 KL 引力区）
  3) 15m K 线（最新一根，已封闭）出现与 KL side 匹配的反转形态
  4) KL final_score ≥ 50（中等以上质量）

预测方向：
  - KL.side = "support" + 被扫后回收 → 预测 up（多头清出后反弹）
  - KL.side = "resistance" + 被扫后回收 → 预测 down（空头清出后回落）

适用 regime：trend_up / trend_down / range
  - squeeze: 蓄力期扫单意义弱，常见 fake_breakout，剔除
  - high_vol_chop / extreme: regime_gate 已 block

适用 horizon：30 / 60（10min 容易被噪音打断；60 是奖励机会）

复用决策（dev-constraints #3）：
  - detect_reversal_pattern() → 直接复用 candlestick_patterns.py
  - KeyLevelV2.state / final_score / strength_tier → 直接复用 KL V2 输出
"""

from __future__ import annotations

import logging
import time
from typing import Any, ClassVar, Optional

from models.scalp_signal import EvidenceItem, ScalpDirection, StrategyName

from processors.candlestick_patterns import detect_reversal_pattern
from processors.scalp_signal.base_strategy import (
    BaseStrategy,
    StrategyCandidate,
    StrategyContext,
)

logger = logging.getLogger(__name__)


# 策略参数（暴露为类常量，便于单测覆盖与未来调优）
KL_NEAR_PCT_MAX = 0.5                          # KL 距 reference 必须 ≤ 0.5%
KL_FINAL_SCORE_MIN = 50.0                      # KL final_score 最低门槛
ELIGIBLE_KL_STATES: frozenset[str] = frozenset({"swept", "fake_break"})


class SweepReclaimStrategy(BaseStrategy):
    """策略 A · 扫单回归"""

    name: ClassVar[StrategyName] = StrategyName.A_SWEEP_RECLAIM
    display_name: ClassVar[str] = "A 扫单回归"
    suitable_regimes: ClassVar[set[str]] = {"trend_up", "trend_down", "range"}
    suitable_horizons: ClassVar[set[int]] = {30, 60}

    def detect(self, ctx: StrategyContext) -> Optional[StrategyCandidate]:
        # ── 1) 取参考价 ──
        ref = self.safe_attr(ctx.state, "ticker", "last", default=0.0)
        if not ref or ref <= 0:
            return None

        # ── 2) 取 KL snapshot ──
        snap = getattr(ctx.state, "key_level_snapshot_v2", None)
        if snap is None:
            return None
        levels = getattr(snap, "levels", None) or []
        if not levels:
            return None

        # ── 3) 找候选 KL：state ∈ eligible，距离够近，分数够高 ──
        best = self._pick_best_eligible_kl(levels, ref)
        if best is None:
            return None

        # ── 4) 取最近 15m K 线，做形态检查 ──
        candles = getattr(ctx.state, "candles_15m", None) or []
        if len(candles) < 2:
            return None

        side = getattr(best, "side", "")        # "support" / "resistance"
        if side not in ("support", "resistance"):
            return None

        pattern = detect_reversal_pattern(candles, side=side)
        if not pattern.found:
            return None

        # ── 5) 决策方向 ──
        direction: ScalpDirection = "up" if side == "support" else "down"

        # ── 6) raw_strength 计算 ──
        raw = self._compute_strength(best, pattern)

        # ── 7) 装配 evidence + extra_data ──
        kl_state = getattr(best, "state", "")
        kl_price = getattr(best, "price", 0.0)
        kl_score = float(getattr(best, "final_score", 0.0) or 0.0)
        kl_tier = getattr(best, "strength_tier", "")
        sweep_usd = float(getattr(best, "sweep_usd", 0.0) or 0.0)
        dist_pct = abs(kl_price - ref) / ref * 100.0 if ref > 0 else 0.0

        evidence = [
            EvidenceItem(
                dimension="Sweep",
                observation=(
                    f"KL @ ${kl_price:.0f}（{side}, {kl_tier}, score={kl_score:.0f}） "
                    f"状态={kl_state} | 距现价 {dist_pct:.2f}% | "
                    f"扫单 ${sweep_usd / 1e6:.1f}M"
                ),
                score_contribution=min(1.0, kl_score / 100.0),
                weight="high",
            ),
            EvidenceItem(
                dimension="Pattern",
                observation=f"{pattern.name}（强度 {pattern.strength:.2f}）确认反转",
                score_contribution=pattern.strength,
                weight="high" if pattern.strength >= 0.8 else "medium",
            ),
        ]

        return StrategyCandidate(
            direction=direction,
            reference_price=float(ref),
            raw_strength=raw,
            evidence=evidence,
            triggered_conditions=[
                f"kl_state={kl_state}",
                f"pattern={pattern.name}",
                f"distance={dist_pct:.2f}%",
            ],
            extra_data={
                "kl_price": kl_price,
                "kl_side": side,
                "kl_tier": kl_tier,
                "kl_final_score": kl_score,
                "kl_state": kl_state,
                "pattern_name": pattern.name,
                "pattern_strength": pattern.strength,
                "sweep_usd": sweep_usd,
            },
        )

    # ── 私有 ──────────────────────────────────────────────────

    @staticmethod
    def _pick_best_eligible_kl(levels: list[Any], ref: float) -> Optional[Any]:
        """从 KL 列表中挑出最佳"被扫且回收"候选

        优先级（先按状态 → 再按距离 → 再按 final_score）：
          1) state ∈ eligible
          2) distance_pct ≤ KL_NEAR_PCT_MAX
          3) final_score ≥ KL_FINAL_SCORE_MIN
          4) 同时满足时按 final_score 倒序选最强
        """
        eligible: list[tuple[float, float, Any]] = []
        for lv in levels:
            state = getattr(lv, "state", "")
            if state not in ELIGIBLE_KL_STATES:
                continue
            price = float(getattr(lv, "price", 0.0) or 0.0)
            if price <= 0 or ref <= 0:
                continue
            dist_pct = abs(price - ref) / ref * 100.0
            if dist_pct > KL_NEAR_PCT_MAX:
                continue
            final_score = float(getattr(lv, "final_score", 0.0) or 0.0)
            if final_score < KL_FINAL_SCORE_MIN:
                continue
            eligible.append((-final_score, dist_pct, lv))

        if not eligible:
            return None
        eligible.sort()  # final_score 倒序 + 距离正序
        return eligible[0][2]

    @staticmethod
    def _compute_strength(kl: Any, pattern: Any) -> float:
        """raw_strength = 0.5（基础）+ KL 评分加分 + 形态加分 + state 奖励

        范围 [0, 1]
        """
        base = 0.5

        # KL 质量加分（final_score 50 → +0.0；100 → +0.30）
        final_score = float(getattr(kl, "final_score", 0.0) or 0.0)
        kl_bonus = max(0.0, min(0.30, (final_score - KL_FINAL_SCORE_MIN) / 50.0 * 0.30))

        # 形态强度加分（pattern.strength 0.5 → +0.0；1.0 → +0.20）
        pattern_strength = float(getattr(pattern, "strength", 0.0) or 0.0)
        pattern_bonus = max(0.0, min(0.20, (pattern_strength - 0.5) / 0.5 * 0.20))

        # state 奖励（fake_break 比 swept 更明确）
        state_bonus = 0.05 if getattr(kl, "state", "") == "fake_break" else 0.0

        # bounce_quality=proactive 主动反弹再加 0.05
        bounce_bonus = 0.05 if getattr(kl, "bounce_quality", "") == "proactive" else 0.0

        total = base + kl_bonus + pattern_bonus + state_bonus + bounce_bonus
        return float(max(0.0, min(1.0, total)))
