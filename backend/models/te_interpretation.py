"""趋势衰竭模块 · AI 解读结果数据契约

设计原则
--------
1. **AI 是规则的审计员 + 再判断者**，不是翻译器
   - 规则给的 `overall_direction/state/action` 只是**候选结论**
   - AI 基于 sub_scores 原始读数 + 关键位数据**再判一遍**，有权推翻
2. **融合关键位**：通过 `key_levels_snapshot_v2` 把 S/A 级强位 + 牛熊分界 + 挤压带喂给 AI
3. **五大卡片结构化**：趋势评估 + 关键位投射 + 矛盾消解 + 陷阱 + 触发条件
4. **显式对齐度**：让用户看到 AI 是否推翻规则，避免盲从
5. **思考链单独字段**：reasoning_content 归档（v4-flash 非思考模式恒为空，保留兼容 R1）

本模型不直接入 WebSocket 广播（按需触发），触发后通过 WS `te_ai_result` 推送。
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 枚举类型
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

AlignmentWithRules = Literal[
    "agree",              # AI 同意规则结论（绿色小标：AI 确认）
    "partial_disagree",   # AI 有补充 / 部分不同意（黄色：AI 有补充）
    "strong_disagree",    # AI 认为规则错了（红色：AI 不同意，请警惕）
    "neutral",            # AI 既不同意也不反对，给独立视角（青色：AI 独立观察）
    "insufficient",       # 证据不足，AI 不敢下判（灰色：证据不足）
]


ScenarioLabel = Literal[
    "trend_continuation",     # 顺势续航
    "bear_rebound",           # 熊市反弹
    "bull_pullback",          # 牛市回调
    "reversal_early",         # 反转早期
    "reversal_confirmed",     # 反转确认
    "choppy_range",           # 震荡/没戏
    "unclear",                # AI 也看不清
]


# ── 新增：趋势评估 ────────────────────────────────────
PrimaryTrend = Literal["uptrend", "downtrend", "sideways", "transition"]
MomentumQuality = Literal["fuel_full", "fuel_adequate", "fuel_fading", "fuel_exhausted", "unclear"]
MomentumDirection = Literal["accelerating", "stable", "decelerating", "unclear"]


class TrendAssessment(BaseModel):
    """AI 对当前趋势 + 动能健康度的独立判断（可能推翻规则的 direction）。"""

    primary_trend: PrimaryTrend = "transition"
    momentum_quality: MomentumQuality = "unclear"
    momentum_direction: MomentumDirection = "unclear"
    health_summary_cn: str = ""          # "当前上涨趋势，动能充足，未见衰减"
    evidence_cn: str = ""                # 引用的数值证据（RSI=65.5 + 价离 EMA20 +3.4σ）


# ── 新增：关键位投射 ──────────────────────────────────
BreakLikelihood = Literal[
    "very_likely",        # 很可能突破（>75% 自信）
    "likely",             # 可能突破（55-75%）
    "uncertain",          # 未定（45-55%）
    "unlikely",           # 难以突破（25-45%）
    "very_unlikely",      # 很难突破（<25%）
    "insufficient",       # 数据不足
]

DirectionTested = Literal["resistance", "support", "both", "none"]


class LevelProjection(BaseModel):
    """AI 对最近关键位能否突破/跌破的概率判断。"""

    target_level: Optional[float] = None      # 目标价（从 key_levels 引用，不可编造）
    direction_tested: DirectionTested = "none"
    break_likelihood: BreakLikelihood = "insufficient"
    break_conviction: float = 0.0             # 0-1 量化副字段（专业用户参考）
    reasoning_cn: str = ""                    # "动能充足 + 日线结构向上共振"
    if_break_cn: str = ""                     # "突破后下一目标 78200（日线强阻力）"
    if_fail_cn: str = ""                      # "若失守则回测 74973（S 级强支撑）"


# ── 新增：交易倾向 ────────────────────────────────────
TradeBiasDirection = Literal["long", "short", "neutral", "avoid"]
TradeBiasStrength = Literal["probe", "standard", "strong", "none"]


class TradeBias(BaseModel):
    """AI 给出的具体交易方向倾向（允许给区间 / 失效位 / 时间窗）。"""

    direction: TradeBiasDirection = "neutral"
    strength: TradeBiasStrength = "none"
    entry_zone_cn: str = ""          # "76200-76500 回踩时"
    invalidation_cn: str = ""        # "跌破 75800 则取消"
    timeframe_cn: str = ""           # "2-6 小时"
    why_cn: str = ""                 # 一句理由（≤60 字）


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 主契约
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TEAIInterpretation(BaseModel):
    """单次 AI 解读结果（既供前端展示，也供 shadow log 归档）。"""

    # ── 元信息 ────────────────────────────────────────
    coin: str
    ts: int                             # 触发解读的 unix ts
    signal_fingerprint: str             # 输入信号的指纹（缓存 key / 去重）
    model: str = ""
    cache_hit: bool = False
    from_cache_age_sec: int = 0
    latency_ms: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    reasoning_tokens: int = 0

    # ── 主结论 ────────────────────────────────────────
    summary_cn: str = ""                # 一句话讲清场景（≤60 字，放宽自由度）
    scenario: ScenarioLabel = "unclear"

    # ── 新增：趋势评估（AI 审计员定位） ──────────────────
    trend_assessment: Optional[TrendAssessment] = None

    # ── 新增：关键位投射 ──────────────────────────────────
    level_projection: Optional[LevelProjection] = None

    # ── 新增：交易倾向（可选，AI 可以选择不给） ─────────────
    trade_bias: Optional[TradeBias] = None

    # ── 矛盾消解 / 陷阱 / 触发条件 ────────────────────────
    conflict_resolution: str = ""
    traps: list[str] = Field(default_factory=list)
    triggers_to_watch: list[str] = Field(default_factory=list)

    # ── 新增：AI 独立观察（规则没覆盖但值得说的） ───────────
    independent_view: str = ""

    # ── 综合行动建议（放宽到 150 字，允许给方向 + 区间 + 时间） ─
    action_suggestion: str = ""

    # ── 置信度 + 对齐度 ───────────────────────────────
    confidence: float = 0.0
    alignment_with_rules: AlignmentWithRules = "insufficient"
    alignment_reason: str = ""          # 必须引用具体数值证据

    # ── 思考过程（可选，默认前端折叠） ────────────────
    reasoning: str = ""

    # ── 错误兜底（解析失败时） ────────────────────────
    error: Optional[str] = None
    raw_text: str = ""
