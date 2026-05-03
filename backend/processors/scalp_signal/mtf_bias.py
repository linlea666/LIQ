"""多周期偏置（Multi-Timeframe Bias）计算

职责：
  - 综合 1h MAA 场景 + 1h/1d/1w market_structure，给出 [-1, +1] 偏置分数
  - 0 = 中性，>0 看多，<0 看空
  - 仅消费已有信号（MAA report + market_structure），不重新跑 AI / 不重新计算结构

为什么独立模块而非复用 OpportunityEngine？
  - OpportunityEngine 输出"机会评分"，逻辑围绕 RR setup
  - 我们需要的是"短线方向偏置"，输入侧重 prior + 近期方向
  - 强行复用会引入 OpportunityEngine 的滑点 / RR / cooldown 逻辑（不相关耦合）
  - 故 dev-constraints #3 选择"独立新写"，但**完全只读**，零副作用

权重设计（理论 max ≈ 1.1，clamp 到 [-1, +1]）：
  - 1h MAA scenario：±0.5（最强信号源，AI 综合判断）
  - 1h market_structure operate_bias：±0.3
  - 1d market_structure direction：±0.2（仅 horizon ≥ 30min 计入）
  - 1w market_structure direction：±0.1（仅 horizon ≥ 60min 计入）
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from models.scalp_signal import HorizonMin

logger = logging.getLogger(__name__)


# 1h MAA 9 个 scenario → 多空打分（基于 backend/models/market_action.py 的 Literal）
_SCENARIO_BIAS_MAP: dict[str, float] = {
    # 强方向延续（+/- 0.5）
    "trend_continuation_up": +0.5,
    "trend_continuation_down": -0.5,
    "short_squeeze_up": +0.5,
    "long_squeeze_down": -0.5,
    # 假突破：方向反向（+/- 0.4）
    "fake_breakout_up": -0.4,         # 上沿假突破 → 即将下行
    "fake_breakdown_down": +0.4,      # 下沿假跌破 → 即将反弹
    # 衰竭：温和反向（+/- 0.3）
    "exhaustion_top": -0.3,
    "exhaustion_bottom": +0.3,
    # 区间震荡：中性
    "range_bound": 0.0,
}

# 1h market_structure.operate_bias → 偏置
_OPERATE_BIAS_MAP: dict[str, float] = {
    "long_only": +0.3,
    "short_only": -0.3,
    "both_ok": 0.0,
    "stand_aside": 0.0,
}

# 1d / 1w market_structure.direction → 偏置（仅取 1d、1w 用，1h 用 operate_bias 更准）
_STRUCTURE_DIRECTION_BIAS_MAP: dict[str, float] = {
    "bullish": 1.0,     # 1d 乘 0.2，1w 乘 0.1
    "bearish": -1.0,
    "ranging": 0.0,
    "transitioning": 0.0,
}


@dataclass
class MTFBiasResult:
    """多周期偏置结果"""
    bias_score: float                          # ∈ [-1, +1]
    components: dict[str, float] = field(default_factory=dict)
    summary_cn: str = ""
    alignment: str = "neutral"                 # "bullish_aligned" / "bearish_aligned" / "mixed" / "neutral"


class MTFBiasComputer:
    """多周期偏置计算器 · 无状态 compute(state, horizon) → MTFBiasResult"""

    def compute(self, state: Any, horizon_min: HorizonMin) -> MTFBiasResult:
        components: dict[str, float] = {}

        # 1) 1h MAA scenario（优先用 stability accepted_scenario）
        rpt = getattr(state, "market_action_report", None)
        if rpt is not None:
            scenario = getattr(rpt, "scenario", "") or ""
            stab = getattr(rpt, "stability", None)
            if stab is not None:
                accepted = getattr(stab, "accepted_scenario", None)
                if accepted:
                    scenario = accepted
            if scenario:
                components["maa_1h"] = _SCENARIO_BIAS_MAP.get(scenario, 0.0)

        # 2) 1h market_structure operate_bias
        ms_1h = getattr(state, "market_structure", None)
        if ms_1h is not None:
            bias = getattr(ms_1h, "operate_bias", None)
            if bias:
                components["ms_1h"] = _OPERATE_BIAS_MAP.get(bias, 0.0)

        # 3) 1d market_structure direction（仅 horizon ≥ 30 计入，10min 噪音放大）
        if horizon_min >= 30:
            ms_1d = getattr(state, "market_structure_1d", None)
            if ms_1d is not None:
                d = getattr(ms_1d, "direction", None)
                if d:
                    components["ms_1d"] = _STRUCTURE_DIRECTION_BIAS_MAP.get(d, 0.0) * 0.2

        # 4) 1w market_structure direction（仅 horizon ≥ 60 计入）
        if horizon_min >= 60:
            ms_1w = getattr(state, "market_structure_1w", None)
            if ms_1w is not None:
                d = getattr(ms_1w, "direction", None)
                if d:
                    components["ms_1w"] = _STRUCTURE_DIRECTION_BIAS_MAP.get(d, 0.0) * 0.1

        total = sum(components.values())
        bias_score = max(-1.0, min(1.0, total))

        # alignment 判定（用于策略评分时奖励"全方向一致"）
        alignment = _classify_alignment(components)

        # 中文摘要（看板 evidence 用）
        summary_cn = _format_summary_cn(bias_score, components, alignment)

        return MTFBiasResult(
            bias_score=bias_score,
            components=components,
            summary_cn=summary_cn,
            alignment=alignment,
        )


def _classify_alignment(components: dict[str, float]) -> str:
    """判断各周期方向是否一致

    - 全部正 → bullish_aligned
    - 全部负 → bearish_aligned
    - 有正有负 → mixed
    - 全部 0 或为空 → neutral
    """
    nonzero = [v for v in components.values() if abs(v) > 1e-9]
    if not nonzero:
        return "neutral"
    if all(v > 0 for v in nonzero):
        return "bullish_aligned"
    if all(v < 0 for v in nonzero):
        return "bearish_aligned"
    return "mixed"


def _format_summary_cn(bias: float, components: dict[str, float], alignment: str) -> str:
    if bias > 0.45:
        tag = "强多"
    elif bias > 0.15:
        tag = "偏多"
    elif bias < -0.45:
        tag = "强空"
    elif bias < -0.15:
        tag = "偏空"
    else:
        tag = "中性"

    parts = []
    if "maa_1h" in components:
        parts.append(f"MAA {components['maa_1h']:+.2f}")
    if "ms_1h" in components:
        parts.append(f"MS1h {components['ms_1h']:+.2f}")
    if "ms_1d" in components:
        parts.append(f"MS1d {components['ms_1d']:+.2f}")
    if "ms_1w" in components:
        parts.append(f"MS1w {components['ms_1w']:+.2f}")
    detail = " | ".join(parts) if parts else "无可用上下文"

    return f"多周期偏置={bias:+.2f}（{tag}, {alignment}）· {detail}"
