"""Regime 闸门 · Stage 1 过滤（所有策略前置硬规则）

职责：
  - 评估当前 regime 是否允许出任何短线信号
  - 提取统一的 range_position_pct 上下文（供策略消费）
  - 不替代策略本身的 suitable_regimes 白名单（双重保险）

设计原则：
  - 直接复用 state.regime_snapshot（不重新计算）
  - 直接复用 state.range_signal.price_position_pct
  - 完全只读，无副作用

硬阻塞 regime（block all strategies）：
  - extreme：极端波动（黑天鹅）→ 任何方向预测都不可靠
  - high_vol_chop：高波动无序震荡 → 短线噪音 > 信号
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

from models.common_enums import MarketRegimeLabel

logger = logging.getLogger(__name__)


# 当 regime ∈ 此集合时，闸门一律 block（任何策略都不出信号）
REGIME_BLOCK_SET: frozenset[str] = frozenset({"extreme", "high_vol_chop"})


@dataclass
class RegimeGateResult:
    """闸门评估结果"""
    regime: MarketRegimeLabel
    range_position_pct: Optional[float]   # 0-100，无数据时 None
    regime_confidence: float              # 0-1（来自 RegimeSnapshot）
    allow: bool
    skip_reason: Optional[str]            # 不通过时的人类可读原因


class RegimeGate:
    """Regime 闸门 · 无状态 evaluate(state) → RegimeGateResult"""

    def __init__(self, block_set: Optional[frozenset[str]] = None) -> None:
        self._block_set = block_set if block_set is not None else REGIME_BLOCK_SET

    def evaluate(self, state: Any) -> RegimeGateResult:
        """评估闸门

        块路径：
          - state.regime_snapshot 缺失 → block（无 regime 上下文）
          - regime ∈ block_set → block（极端 / 高噪音）
          - 否则 → allow

        不在闸门里检查的（由 veto_gate 负责）：
          - 数据陈旧（ticker/cvd 时间戳）
          - 黑天鹅新闻
          - K 线未封闭
        """
        snap = getattr(state, "regime_snapshot", None)
        if snap is None:
            return RegimeGateResult(
                regime="range",  # 默认（不影响 block）
                range_position_pct=None,
                regime_confidence=0.0,
                allow=False,
                skip_reason="no_regime_snapshot",
            )

        regime: MarketRegimeLabel = snap.regime
        regime_confidence = float(getattr(snap, "confidence", 0.0) or 0.0)

        # 取 range_position_pct（即使 regime 不是 range 也可以提供，供策略 C 判断）
        range_pos: Optional[float] = None
        rs = getattr(state, "range_signal", None)
        if rs is not None:
            try:
                pp = getattr(rs, "price_position_pct", None)
                if pp is not None:
                    range_pos = float(pp)
            except (TypeError, ValueError):
                range_pos = None

        if regime in self._block_set:
            return RegimeGateResult(
                regime=regime,
                range_position_pct=range_pos,
                regime_confidence=regime_confidence,
                allow=False,
                skip_reason=f"regime_{regime}_block",
            )

        return RegimeGateResult(
            regime=regime,
            range_position_pct=range_pos,
            regime_confidence=regime_confidence,
            allow=True,
            skip_reason=None,
        )
