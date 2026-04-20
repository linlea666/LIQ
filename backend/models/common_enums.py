"""共享枚举类型 —— 多模块共用的 Literal 枚举集中管理。

职责：
  - 统一 direction / resonance / traffic_light 等语义枚举，
    避免在 Synthesizer / NewsAgent / AIAnalyzer 各处重复定义。
  - 新增维度时集中维护，下游类型检查自动生效。

风格：
  - 使用 `Literal[...]` 而非 `Enum class`（对齐 market_structure.py 现有风格）
  - 纯类型定义，无业务逻辑
"""

from __future__ import annotations

from typing import Literal


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 方向 & 强度 —— 基础判断维度
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# 方向枚举：正反向 + 中性 + 反向风险
# "potential_reversal" 用于资金费率极端 / 恐贪极端等"反向指标"
Direction = Literal["bullish", "bearish", "neutral", "potential_reversal"]

# 共振强度：该因子对总体结论贡献大小
Resonance = Literal["low", "medium", "high"]

# 信号强度分档：对齐现有 KeyLevelV2.strength_tier
SignalTier = Literal["S", "A", "B", "C"]

# AI 主观置信度（粗粒度）
Confidence = Literal["high", "medium", "low"]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 时效 & 交易动作
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# 事件/信号影响时效
TimeHorizon = Literal["immediate", "short", "medium", "lasting"]
# immediate = 0-1h, short = 1-24h, medium = 1-7d, lasting = >7d

# 交易动作
TradingAction = Literal["long", "short", "wait", "avoid"]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 市场状态 (Regime)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# 市场状态六分类（驱动 Synthesizer 权重切换）
MarketRegimeLabel = Literal[
    "trend_up",       # 上升趋势
    "trend_down",     # 下降趋势
    "range",          # 箱体震荡
    "squeeze",        # 蓄力收敛
    "high_vol_chop",  # 高波动无序
    "extreme",        # 极端行情（触发熔断）
]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 地缘风险等级
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# 6 档地缘风险等级（0=peace, 5=war）
# 使用 int Literal 方便排序比较；label 在 geo_risk.py 中映射
GeoRiskLevelInt = Literal[0, 1, 2, 3, 4, 5]

GeoRiskLabel = Literal[
    "PEACE",       # 0 · 无风险
    "WATCHING",    # 1 · 关注
    "TENSION",     # 2 · 紧张
    "CRISIS",      # 3 · 危机
    "ESCALATION",  # 4 · 升级
    "WAR",         # 5 · 战争
]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 决策输出
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# 前端三灯（gray = 数据未就绪）
TrafficLight = Literal["green", "yellow", "orange", "red", "gray"]

# 双引擎共识等级
ConsensusLevel = Literal[
    "strong",     # 两引擎同向 + 分数接近
    "agree",      # 两引擎同向
    "math_lead",  # AI 观望，数学引擎有方向
    "ai_lead",    # 数学引擎观望，AI 有方向
    "conflict",   # 两引擎反向（分歧）
]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 新闻 & 叙事
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# 事件处理层：规则层快速分类 vs AI 层深度结构化
NewsProcessor = Literal["rule", "ai"]

# 事件重要性分档
NewsTier = Literal["blackswan", "major", "normal", "minor"]

# 事件风险类型（影响 SafetyGate 行为）
NewsRiskType = Literal[
    "none",
    "geopolitical",
    "regulatory",
    "technical",        # 交易所技术故障 / 协议漏洞
    "macro_economic",
    "black_swan",
]

# 叙事主题在生命周期中的阶段
NarrativeStage = Literal[
    "new",            # 全新叙事
    "continuing",     # 延续（无新进展）
    "reversal",       # 反转
    "fading",         # 衰减
    "escalation",     # 升级
    "de-escalation",  # 降级
]

# 叙事主题的活跃度趋势
NarrativeTrend = Literal["emerging", "active", "fading", "dormant"]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 安全护栏
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# 单道护栏的通过状态
SafetyGateStatus = Literal["pass", "warn", "block"]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 融合层动作建议
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

RecommendedAction = Literal[
    "execute",       # 建议执行
    "reduce_size",   # 减仓执行
    "wait",          # 观望
    "avoid",         # 回避
]
