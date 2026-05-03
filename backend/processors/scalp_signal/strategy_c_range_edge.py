"""策略 C · Range Edge Fade（区间边缘均值回归）

策略哲学（dev-constraints #1 根因优先）：
  确认的区间行情中，价格触达边缘 = 大概率回归中线（70%+ 历史命中率）
  + 反转 K 线形态确认 = 主力意图明确
  + RSI 极端度 = 动能耗尽信号
  → 短期内大概率反向运动（30-60min 内回归区间中位）

逻辑链路：
  1) regime = range（必须，确认是区间行情）
  2) range_position_pct ≤ 20（下沿）或 ≥ 80（上沿）
  3) 15m K 线出现与位置匹配的反转形态（pin bar / engulfing / doji）
  4) RSI_14 偏极端（下沿要 RSI ≤ 35，上沿要 RSI ≥ 65）

预测方向：
  - 下沿 + 看涨形态 → up（反弹回中线）
  - 上沿 + 看跌形态 → down（回落到中线）

适用 regime：仅 range（其他 regime 用此策略意义弱）
适用 horizon：30 / 60（10 太短，区间回归未必启动）

复用决策（dev-constraints #3）：
  - state.range_signal.price_position_pct → 直接复用
  - detect_reversal_pattern() → 直接复用
  - state.rsi_14 → 直接复用
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, ClassVar, Optional

from models.scalp_signal import EvidenceItem, ScalpDirection, StrategyName

from processors.candlestick_patterns import detect_reversal_pattern
from processors.scalp_signal.base_strategy import (
    BaseStrategy,
    StrategyCandidate,
    StrategyContext,
)


@dataclass
class _RangeIntegrityResult:
    """P0-5 二次确认结果（内部使用）"""
    passed: bool
    reason: str = ""
    adx: float = 0.0
    test_count_max: int = 0
    box_state: str = ""
    box_width_pct: float = 0.0

    @property
    def summary(self) -> str:
        return self.reason

logger = logging.getLogger(__name__)


# 策略参数
RANGE_LOWER_PCT_MAX = 20.0          # ≤ 20 算下沿
RANGE_UPPER_PCT_MIN = 80.0          # ≥ 80 算上沿
RSI_OVERSOLD_MAX = 35.0             # 下沿要 RSI ≤ 35
RSI_OVERBOUGHT_MIN = 65.0           # 上沿要 RSI ≥ 65

# P0-5 二次确认参数（避免假区间被误判）
ADX_RANGE_MAX = 25.0                # ADX ≥ 25 → 趋势抬头，区间不真，拒绝
RANGE_TEST_COUNT_MIN = 2            # 边界至少被测试 2 次（"区间已成立"佐证）
ALLOWED_BOX_STATES: set[str] = {"confirmed", "mature", "squeeze"}
# broken / breaking_up / breaking_down / forming / none → 拒绝
RANGE_BOX_WIDTH_MIN_PCT = 1.5       # 箱体宽度 ≥ 1.5% （太窄 → 噪音内）


class RangeEdgeFadeStrategy(BaseStrategy):
    """策略 C · 区间边缘均值回归"""

    name: ClassVar[StrategyName] = StrategyName.C_RANGE_EDGE_FADE
    display_name: ClassVar[str] = "C 区间边缘回归"
    suitable_regimes: ClassVar[set[str]] = {"range"}
    suitable_horizons: ClassVar[set[int]] = {30, 60}

    def detect(self, ctx: StrategyContext) -> Optional[StrategyCandidate]:
        # ── 0) regime/range_position 由 RegimeGate 提供 ──
        if ctx.regime != "range":
            return None
        if ctx.range_position_pct is None:
            return None

        # ── 1) 取参考价 ──
        ref = self.safe_attr(ctx.state, "ticker", "last", default=0.0)
        if not ref or ref <= 0:
            return None

        # ── 2) 判定边缘 ──
        position = float(ctx.range_position_pct)
        if position <= RANGE_LOWER_PCT_MAX:
            edge = "lower"
            side_for_pattern = "support"
            direction: ScalpDirection = "up"
        elif position >= RANGE_UPPER_PCT_MIN:
            edge = "upper"
            side_for_pattern = "resistance"
            direction = "down"
        else:
            return None

        # ── 3) P0-5 二次确认（区间真伪）──
        confirmation = self._check_secondary_confirmations(ctx)
        if not confirmation.passed:
            logger.debug(
                "scalp strategy_c rejected by secondary confirmations | reason=%s",
                confirmation.reason,
            )
            return None

        # ── 4) RSI 极端度确认 ──
        rsi = getattr(ctx.state, "rsi_14", None)
        if rsi is None:
            return None
        rsi_val = float(rsi)
        if edge == "lower" and rsi_val > RSI_OVERSOLD_MAX:
            return None
        if edge == "upper" and rsi_val < RSI_OVERBOUGHT_MIN:
            return None

        # ── 5) K 线反转形态 ──
        candles = getattr(ctx.state, "candles_15m", None) or []
        if len(candles) < 2:
            return None
        pattern = detect_reversal_pattern(candles, side=side_for_pattern)
        if not pattern.found:
            return None

        # ── 6) raw_strength（叠加二次确认 bonus）──
        raw = self._compute_strength(position, rsi_val, pattern, edge)
        # 二次确认全过 → +0.05 raw_strength（鼓励真区间）
        raw = float(min(1.0, raw + 0.05))

        # ── 7) Evidence ──
        edge_cn = "下沿" if edge == "lower" else "上沿"
        rsi_tag = "超卖" if edge == "lower" else "超买"
        evidence = [
            EvidenceItem(
                dimension="RangePosition",
                observation=(
                    f"价格位于区间{edge_cn}（{position:.1f}/100）"
                    f"，预期回归中线"
                ),
                score_contribution=self._position_extremity(position),
                weight="high",
            ),
            EvidenceItem(
                dimension="RangeIntegrity",
                observation=confirmation.summary,
                score_contribution=1.0,
                weight="high",
            ),
            EvidenceItem(
                dimension="RSI",
                observation=f"RSI_14={rsi_val:.1f}（{rsi_tag}）",
                score_contribution=self._rsi_extremity(rsi_val, edge),
                weight="medium",
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
                f"edge={edge}",
                f"position={position:.1f}",
                f"rsi={rsi_val:.1f}",
                f"pattern={pattern.name}",
                f"adx={confirmation.adx:.1f}",
                f"box_state={confirmation.box_state}",
                f"test_count_max={confirmation.test_count_max}",
                f"box_width={confirmation.box_width_pct:.2f}%",
            ],
            extra_data={
                "edge": edge,
                "range_position_pct": position,
                "rsi_14": rsi_val,
                "pattern_name": pattern.name,
                "pattern_strength": pattern.strength,
                "adx": confirmation.adx,
                "box_state": confirmation.box_state,
                "box_width_pct": confirmation.box_width_pct,
                "range_test_count_max": confirmation.test_count_max,
            },
        )

    # ── P0-5 二次确认 ──────────────────────────────────────────

    def _check_secondary_confirmations(self, ctx: StrategyContext) -> "_RangeIntegrityResult":
        """P0-5：4 重二次确认，缺一不可

        1) ADX < 25（趋势弱 → 区间为真）
        2) max(test_count) >= 2（边界已被测试 ≥ 2 次）
        3) box_state ∈ {confirmed, mature, squeeze}（不能 broken / breaking）
        4) box_width_pct >= 1.5%（不能太窄）

        任意一项缺失数据 → 拒绝（fail-closed），避免"假区间"出错信号
        """
        regime_snap = getattr(ctx.state, "regime_snapshot", None)
        if regime_snap is None:
            return _RangeIntegrityResult(False, "regime_snapshot 缺失")
        adx = float(getattr(regime_snap, "adx", 0) or 0)
        if adx <= 0:
            return _RangeIntegrityResult(False, "ADX 缺失（无法判断趋势强度）")
        if adx >= ADX_RANGE_MAX:
            return _RangeIntegrityResult(
                False, f"ADX={adx:.1f} ≥ {ADX_RANGE_MAX:.0f}（趋势抬头，非真区间）",
                adx=adx,
            )

        rs = getattr(ctx.state, "range_signal", None)
        if rs is None:
            return _RangeIntegrityResult(False, "range_signal 缺失", adx=adx)

        upper_n = int(getattr(rs, "range_upper_test_count", 0) or 0)
        lower_n = int(getattr(rs, "range_lower_test_count", 0) or 0)
        max_n = max(upper_n, lower_n)
        if max_n < RANGE_TEST_COUNT_MIN:
            return _RangeIntegrityResult(
                False,
                f"边界测试 max(upper={upper_n}, lower={lower_n}) < {RANGE_TEST_COUNT_MIN}（区间未充分形成）",
                adx=adx, test_count_max=max_n,
            )

        box_state = str(getattr(rs, "box_state", "") or "")
        if box_state not in ALLOWED_BOX_STATES:
            return _RangeIntegrityResult(
                False,
                f"box_state={box_state} 不在 {sorted(ALLOWED_BOX_STATES)}（已破位/未形成）",
                adx=adx, test_count_max=max_n, box_state=box_state,
            )

        box_width = float(getattr(rs, "box_width_pct", 0) or 0)
        if box_width < RANGE_BOX_WIDTH_MIN_PCT:
            return _RangeIntegrityResult(
                False,
                f"box_width_pct={box_width:.2f}% < {RANGE_BOX_WIDTH_MIN_PCT:.1f}%（箱体过窄）",
                adx=adx, test_count_max=max_n, box_state=box_state, box_width_pct=box_width,
            )

        summary = (
            f"区间真：ADX={adx:.1f}<{ADX_RANGE_MAX:.0f} | "
            f"box_state={box_state} | "
            f"测试次数 max={max_n} | "
            f"宽度={box_width:.2f}%"
        )
        return _RangeIntegrityResult(
            True, summary, adx=adx, test_count_max=max_n,
            box_state=box_state, box_width_pct=box_width,
        )

    # ── 私有 ──────────────────────────────────────────────────

    @staticmethod
    def _position_extremity(position: float) -> float:
        """位置极端度归一 → [0, 1]

        - position=0 或 100 → 1.0
        - position=20 或 80 → 0.0（边界值）
        - 中间 → 0.0
        """
        if position <= RANGE_LOWER_PCT_MAX:
            # 0 → 1.0；20 → 0.0
            return max(0.0, min(1.0, (RANGE_LOWER_PCT_MAX - position) / RANGE_LOWER_PCT_MAX))
        if position >= RANGE_UPPER_PCT_MIN:
            # 80 → 0.0；100 → 1.0
            return max(0.0, min(1.0, (position - RANGE_UPPER_PCT_MIN) / (100 - RANGE_UPPER_PCT_MIN)))
        return 0.0

    @staticmethod
    def _rsi_extremity(rsi: float, edge: str) -> float:
        """RSI 极端度归一 → [0, 1]

        - 下沿：RSI=35 → 0.0；RSI=20 → 1.0；RSI=10 → 1.0（封顶）
        - 上沿：RSI=65 → 0.0；RSI=80 → 1.0；RSI=90 → 1.0
        """
        if edge == "lower":
            return max(0.0, min(1.0, (RSI_OVERSOLD_MAX - rsi) / (RSI_OVERSOLD_MAX - 20.0)))
        # upper
        return max(0.0, min(1.0, (rsi - RSI_OVERBOUGHT_MIN) / (80.0 - RSI_OVERBOUGHT_MIN)))

    def _compute_strength(
        self,
        position: float,
        rsi: float,
        pattern: Any,
        edge: str,
    ) -> float:
        """raw_strength = 0.5 + position×0.20 + rsi×0.15 + pattern×0.15

        范围 [0, 1]
        """
        base = 0.5
        pos_bonus = self._position_extremity(position) * 0.20
        rsi_bonus = self._rsi_extremity(rsi, edge) * 0.15
        pattern_strength = float(getattr(pattern, "strength", 0.0) or 0.0)
        # pattern 0.5 → +0.0，1.0 → +0.15
        pattern_bonus = max(0.0, min(0.15, (pattern_strength - 0.5) / 0.5 * 0.15))
        total = base + pos_bonus + rsi_bonus + pattern_bonus
        return float(max(0.0, min(1.0, total)))
