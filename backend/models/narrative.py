"""叙事主题（Narrative Theme）追踪模型

职责：
  - 把零散的 MarketEventSignal 按 narrative_theme 聚合
  - 追踪主题的生命周期（emerging → active → fading → dormant）
  - 检测"反复拉扯"（flip-flop）频次
  - 记录历史价格反应均值（供 AI/数学引擎评估本次事件权重）

与 geo_risk 的关系：
  - NarrativeTheme 是**所有叙事**的通用容器（宏观/监管/技术/地缘）
  - GeoRiskState 是**地缘叙事专属**的风险等级视图（PEACE/WATCHING/.../WAR）
  - 一个 geo 主题同时出现在两边：narrative.py 负责叙事追踪，geo_risk.py 负责等级评估
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

from models.common_enums import Direction, NarrativeTrend


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 历史价格反应（用于评估本次事件是否被 priced-in）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class NarrativePriceReaction(BaseModel):
    """一次事件对应的价格反应（回填后存档）"""

    event_id: str
    ts: int = 0                                  # 秒级
    direction: Direction = "neutral"             # 事件 AI 判定方向
    impact_score: int = 0                        # 事件 AI 判定强度

    price_before: float = 0.0
    delta_pct_1h: Optional[float] = None
    delta_pct_2h: Optional[float] = None
    delta_pct_24h: Optional[float] = None

    matched_direction: Optional[bool] = None     # 实际价格走向是否符合 AI 判定


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 叙事主题
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class NarrativeTheme(BaseModel):
    """单一叙事主题的状态容器"""

    # ── 身份 ──
    theme_id: str                                # "Middle_East_Iran" / "Fed_Rate_Policy" / ...
    theme_name_cn: str                           # "中东-伊朗局势"
    category: str = ""                           # "geopolitical" / "macro_policy" / "regulatory" / "tech" / "sector"

    # ── 生命周期 ──
    first_seen_ts: int = 0                       # 秒级
    last_seen_ts: int = 0
    active: bool = False                         # 24h 内有新事件视为 active

    # ── 频次统计 ──
    event_count_24h: int = 0
    event_count_7d: int = 0
    flip_flop_count_24h: int = 0                 # 24h 内方向反转次数
    flip_flop_count_7d: int = 0

    # ── 价格反应历史（最近 N 次，滚动） ──
    price_reactions: list[NarrativePriceReaction] = Field(default_factory=list)
    avg_abs_reaction_pct: float = 0.0            # 绝对均幅（用于 priced-in 估算）
    latest_reaction_pct: Optional[float] = None
    hit_rate: float = 0.0                        # AI 方向命中率 0-1

    # ── 当前状态 ──
    trend: NarrativeTrend = "emerging"
    current_direction_bias: Direction = "neutral"
    current_intensity: int = 0                   # 0-5 综合强度（近 24h impact_score 均值）
    current_stage_summary: str = ""              # "今日三次反复·方向偏看空"

    # ── 最近一条事件快照 ──
    latest_event_id: str = ""
    latest_event_summary: str = ""
    latest_event_direction: Direction = "neutral"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 前端精简视图
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class ActiveTheme(BaseModel):
    """前端 NewsIntelPanel 展示的精简版"""

    theme_id: str
    theme_name_cn: str
    category: str = ""
    latest_event_summary: str = ""
    latest_event_ts: int = 0
    flip_flop_count_24h: int = 0
    trend: NarrativeTrend = "emerging"
    current_intensity: int = 0
    current_direction_bias: Direction = "neutral"
    avg_abs_reaction_pct: float = 0.0
    hit_rate: float = 0.0
