"""双引擎融合决策（Signal Fusion Layer）输出模型

职责：
  - 接收数学引擎 ExecutionPlan + AI 引擎 AITraderReport
  - 计算双引擎共识等级（strong / agree / math_lead / ai_lead / conflict）
  - 产出最终对外决策 FinalDecision

核心设计：
  - 不做"谁对谁错"的裁判，而是把共识/分歧作为**信号本身**显式展示
  - 用户最终看到三个层次：
      1. 结论（红绿灯 + 动作 + 仓位）
      2. 两引擎各自的简版摘要
      3. 分歧提示（如果 consensus=conflict，历史上哪边胜率高）
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

from models.common_enums import (
    ConsensusLevel, Direction, RecommendedAction, TradingAction, TrafficLight,
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 单引擎简报（嵌入 FinalDecision）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class EngineBrief(BaseModel):
    """一个引擎的极简版摘要（用于融合层展示）"""

    engine_name: Literal["math", "ai"] = "math"
    score: float = 50.0                          # 0-100（math=execution_score, ai=conviction）
    bias: Direction = "neutral"
    action: TradingAction = "wait"

    # 核心参数
    entry_hint: Optional[float] = None
    stop_loss_hint: Optional[float] = None
    tp1_hint: Optional[float] = None
    rr_ratio: Optional[float] = None
    position_pct: float = 0.0

    # 一句话总结
    summary_cn: str = ""                         # "A级狙击做多@75,080·仓位40%"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 分歧历史统计
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class DivergenceStats(BaseModel):
    """历史上这种分歧类型的胜率统计（回测闭环产出）"""

    divergence_type: str = ""
    # "math_long_ai_short" / "math_A_ai_low_conv" / ...

    sample_size: int = 0
    math_win_rate: float = 0.0                   # 数学引擎方向命中率 0-1
    ai_win_rate: float = 0.0                     # AI 方向命中率 0-1
    avg_delta_pct_24h: float = 0.0               # 分歧后 24h 平均幅度

    winner_hint_cn: str = ""                     # "历史上此类分歧 AI 胜率 62%"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 主模型
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class FinalDecision(BaseModel):
    """双引擎融合层最终输出 —— 这是对外展示的主视图"""

    # ── 基础信息 ──
    coin: str
    ts: int = 0                                  # 秒级
    current_price: float = 0.0

    # ── 最终结论（用户一眼看懂） ──
    final_score: float = 50.0                    # 0-100
    traffic_light: TrafficLight = "gray"
    headline: str = ""                           # "🟢 共识做多 A 级 · 两引擎同向"
    one_liner: str = ""                          # "关键位反转+新闻催化双源共识 · 仓位 35%"

    # ── 双引擎共识 ──
    consensus_level: ConsensusLevel = "agree"
    consensus_stars: int = 3                     # 1-5 颗星（前端展示）
    consensus_summary_cn: str = ""               # "两引擎强共识 · 方向一致 · 分数都 > 75"

    # ── 两引擎各自简报 ──
    math_brief: EngineBrief = Field(default_factory=EngineBrief)
    ai_brief: EngineBrief = Field(default_factory=EngineBrief)

    # ── 分歧提示（consensus=conflict 时填充） ──
    divergence_summary_cn: str = ""              # "数学做多 AI 观望·建议降仓或等待"
    historical_divergence: Optional[DivergenceStats] = None

    # ── 融合后的推荐动作（交给执行层） ──
    recommended_action: RecommendedAction = "wait"
    recommended_position_pct: float = 0.0        # 0-100（分歧时会自动减半）

    # 最终入场参数（融合后）
    entry_zone_low: Optional[float] = None
    entry_zone_high: Optional[float] = None
    stop_loss: Optional[float] = None
    tp1: Optional[float] = None
    tp2: Optional[float] = None
    rr_ratio: Optional[float] = None

    # ── 溯源链（每一环都能回查） ──
    underlying_math_plan_ref: str = ""           # ExecutionPlan 的关键位 id 或分数指纹
    underlying_ai_plan_priority: int = 1         # AITraderReport.trading_plans[i] 的 priority

    # ── 新闻 / 地缘快速提示（同步展示） ──
    active_themes_count: int = 0
    geo_risk_overall_level: int = 0
    geo_risk_label: str = "PEACE"
    has_blackswan_warning: bool = False

    # ── 元信息 ──
    safety_gate_triggered: bool = False
    safety_gate_reason: str = ""
    expires_at: Optional[int] = None             # 秒级
