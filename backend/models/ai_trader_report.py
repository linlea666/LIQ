"""AI 交易员（AI Trader）完整输出模型 —— 双引擎中的 AI 引擎

职责：
  - AI 主模块（deepseek-reasoner）作为"独立资深交易员"产出的完整交易计划
  - 非简单的 Synthesizer 审计员，而是独立决策方
  - 和 ExecutionPlan 并列输入到融合层（Signal Fusion）

核心亮点（用户指定）：
  - 保留并扩展"多维度看盘表"（AIFactorMatrix）作为核心输出
  - 7 板块：宏观 / 资金 / 衍生品 / 技术面 / 新闻叙事 / 地缘风险 / 双引擎共识

复用决策：
  - 现有 AIAnalysisResult 保留不动（旧链路兼容）
  - 新 AITraderReport 是扩展版，P0 并存，P1 逐步迁移
  - 现有 TradingPlanEntry 同样保留不动
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

from models.common_enums import (
    Confidence, Direction, Resonance, SignalTier, TradingAction,
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 多维度看盘表（AIFactorMatrix）—— 用户指定保留的结构
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class AIFactorRow(BaseModel):
    """单行看盘项"""

    dimension: str                               # "DXY" / "资金费率" / "OI 变化" / ...
    value_display: str                           # "98.23 (+0.00%)"（人类可读）
    value_raw: Optional[float] = None            # 数值（排序/阈值用）

    signal: str                                  # "持平" / "空头拥挤" / "多头共识"
    direction: Direction = "neutral"
    resonance: Resonance = "medium"

    # 数据源锚点（前端可点击跳转详情）
    data_source_ref: str = ""                    # "§1" / "§4" / "§6"
    link_anchor: str = ""                        # 前端路由 anchor

    # AI 自评该行对本次结论的权重（内部用）
    confidence: float = 0.5                      # 0-1


class AIFactorSection(BaseModel):
    """看盘表的一个板块（共 7 个）"""

    section_id: Literal["A", "B", "C", "D", "E", "F", "G"]
    section_name_cn: str                         # "宏观联动" / "资金流" / ...
    section_emoji: str = ""                      # "🌐" / "💰" / "⚙️" / "📈" / "📰" / "⚠️" / "🎯"
    rows: list[AIFactorRow] = Field(default_factory=list)

    section_summary: str = ""                    # 板块一句话结论
    section_bias: Direction = "neutral"          # 板块综合偏向


class AIFactorMatrix(BaseModel):
    """完整的 7 板块看盘表

    板块定义：
      A · 宏观联动    （DXY / 纳指 / 黄金 / VIX / 10Y / 原油）
      B · 资金流      （稳定币市值 / ETF / 交易所余额 / 巨鲸）
      C · 衍生品      （OI / 资金费率 / 多空比 / 爆仓 / 期权 IV）
      D · 技术面      （RSI/MACD/BB / MA / 结构 / 关键位）
      E · 新闻叙事    （活跃主题 / flip-flop / priced-in）
      F · 地缘风险    （overall_level / 升级次数）
      G · 双引擎共识  （数学引擎 vs AI 观点对齐度）
    """

    sections: list[AIFactorSection] = Field(default_factory=list)

    summary_line: str = ""                       # "看多（中）" / "看空（强）"
    overall_bias: Direction = "neutral"
    overall_confidence: Confidence = "medium"

    analysis_ts: int = 0                         # 秒级
    price_at_analysis: float = 0.0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 关键位解读（AI 加料，不替代数学引擎的 KeyLevelV2）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class AIKeyLevelInterpretation(BaseModel):
    """AI 对关键位的解读与加强"""

    primary_support_price: Optional[float] = None
    primary_support_reason: str = ""             # "74,800 是日线 demand zone + 500d EMA"

    primary_resistance_price: Optional[float] = None
    primary_resistance_reason: str = ""

    # 诱多/诱空警示
    trap_warning: str = ""                       # "76,200 上方疑似 liquidity sweep"

    # AI 独立识别但数学引擎未覆盖的位置
    extra_levels: list[dict] = Field(default_factory=list)
    # [{price, side, reason}]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# AI 交易计划（单独一份 plan）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class AITradingPlan(BaseModel):
    """AI 交易员产出的完整交易方案（可多份 —— 主/备）"""

    priority: int = 1                            # 1=主计划，2/3=备选
    direction: TradingAction = "wait"            # "long" / "short" / "wait" / "avoid"

    # 入场
    entry_zone_low: Optional[float] = None
    entry_zone_high: Optional[float] = None
    trigger_condition: str = ""                  # "回踩 74,800 + 15m 锤子线"

    # 止损止盈
    stop_loss: Optional[float] = None
    tp1: Optional[float] = None
    tp2: Optional[float] = None
    rr_ratio: Optional[float] = None

    # 仓位 & 置信
    position_suggestion_pct: float = 0.0         # 0-100
    conviction: int = 50                         # 0-100 AI 主观信心
    tier_hint: SignalTier = "C"                  # AI 类比 A/B/C

    # 失效条件
    invalidation: str = ""                       # "跌破 74,500 立即止损"

    # 关联叙事
    related_narratives: list[str] = Field(default_factory=list)
    # 例：["Middle_East_Iran", "ETF_inflow"]

    # 解释
    reason: str = ""                             # 决策理由

    # 与数学引擎对齐度
    aligned_with_math_engine: bool = True
    alignment_note: str = ""                     # "同向 · 入场价差 0.3%"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 叙事影响判读
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class AINarrativeImpact(BaseModel):
    """AI 对具体叙事主题的操作启示"""

    theme_id: str
    theme_name_cn: str = ""
    ai_view_cn: str = ""                         # "已反复 3 次，下次事件不会再引发 4% 波动"
    weight_on_current_plan: Resonance = "medium"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 主模型：AITraderReport
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class AITraderReport(BaseModel):
    """AI 主模块的完整输出（作为双引擎之一）"""

    # ── 元信息 ──
    coin: str
    ts: int = 0                                  # 秒级
    price_at_analysis: float = 0.0
    model: str = ""                              # "deepseek-reasoner"
    thinking_tokens: int = 0                     # reasoner 的思考 token 数（成本追踪）
    latency_ms: int = 0

    # ── 总体判断 ──
    market_view_cn: str = ""                     # 2-3 段自然语言市场观点
    bias: Direction = "neutral"
    conviction: int = 50                         # 0-100

    # ── 关键位解读（AI 加料） ──
    key_level_interpretation: AIKeyLevelInterpretation = Field(
        default_factory=AIKeyLevelInterpretation
    )

    # ── 交易计划（可多份） ──
    trading_plans: list[AITradingPlan] = Field(default_factory=list)
    # 约定：plans[0] 是主计划，其他是备选

    # ── 叙事 / 新闻 / 地缘 影响 ──
    narrative_impact: list[AINarrativeImpact] = Field(default_factory=list)
    news_impact_summary_cn: str = ""             # ≤60 字新闻影响总结
    geo_risk_assessment_cn: str = ""             # ≤60 字地缘风险评估

    # ── 与数学引擎（ExecutionPlan）的对齐 ──
    agreement_with_math_engine: Literal["agree", "caution", "disagree"] = "agree"
    agreement_notes_cn: str = ""                 # "数学引擎给 A 级做多 @75k，我同意但仓位降到 30%"

    # ── 风险提示 ──
    key_risks: list[str] = Field(default_factory=list)

    # ── 多维看盘表（核心亮点） ──
    factor_matrix: Optional[AIFactorMatrix] = None

    # ── 场景分析（保留 AIAnalysisResult 的场景推演概念） ──
    scenario_analysis: list[dict] = Field(default_factory=list)
    # [{"scenario": "FOMC 鸽派", "probability": 0.4, "bias": "bullish", "plan": ""}]

    # ── 调试用 ──
    raw_text: str = ""                           # AI 原始输出
    user_prompt: str = ""                        # 本次 prompt（脱敏后）
