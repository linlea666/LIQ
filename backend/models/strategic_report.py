"""主 AI Strategic 决策官输出契约（AIStrategicReport + 子模型）。

定位：
    主 AI 是"全程决策官"——综合所有数据后自主产出**完整候选交易计划**
    （方向 + 入场区 + 失效条件 + 目标 + 风险描述）。
    本模块定义其唯一输出 schema，与 MAA `MarketActionReport`（短线动作仲裁）
    并行存在，互不耦合。

设计纪律：
    1. **不输出仓位百分比 / 杠杆倍数**——LLM 不掌握账户参数，仓位由用户/外部
       规则按 hard_invalidation 距离反推。AI 仅给出风险等级描述。
    2. **WAIT / NO_TRADE 是合法且优先输出**——RR 不足、关键源 stale、价格在
       中间位置时应优先 WAIT。
    3. **每条 evidence 必须引用 §N 数据章节**——减少凭感觉编结论；
       prompt Step 7 强制冲突矩阵自我辩论。
    4. horizon 由 AI 自决（不限制为单一时间窗）。
    5. `prompt_debug` 透明化复用通用模型（`models.common_prompt_debug`）。
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

from models.common_prompt_debug import PromptDebug


# ────────────────────────────────────────────────────────────────────────────
# 类型别名
# ────────────────────────────────────────────────────────────────────────────

StrategicDecision = Literal[
    "WAIT",                   # 不开仓（RR 不足 / 数据冲突 / 关键源 stale / 中间位置）
    "LONG_OBSERVATION",       # 看多观察（结构在但触发未到）
    "SHORT_OBSERVATION",      # 看空观察
    "LONG_PLAN",              # 多头计划（具备触发条件 + 完整 plan）
    "SHORT_PLAN",             # 空头计划
    "NO_TRADE",               # 数据自检不通过 / hard_stop 触发，禁止任何方向
]

StrategicHorizon = Literal["scalp", "intraday", "swing", "strategic"]
"""时间窗（AI 自决）：
- scalp     ：分钟到小时级（数小时内闭仓）
- intraday  ：日内（当日完成）
- swing     ：波段（数日 - 1-2 周）
- strategic ：战略（月级 / 周期级）
"""

StrategicBias = Literal["bullish", "bearish", "neutral", "conflicted"]
"""综合偏向：
- conflicted = 多空证据势均力敌，明确"看不清" → 通常配合 WAIT
"""

EvidenceSupports = Literal["main", "contrarian", "neutral"]
EvidenceWeight = Literal["high", "medium", "low"]
LeverageRiskLevel = Literal["low", "medium", "high", "extreme"]
DataQuality = Literal["ok", "partial", "insufficient"]


# ────────────────────────────────────────────────────────────────────────────
# 证据 + 冲突矩阵
# ────────────────────────────────────────────────────────────────────────────

class Evidence(BaseModel):
    """单条证据（带 §N 引用 + 立场标注 + 推断）。

    设计：
      - section_ref：必须引用数据章节锚点（"§7" / "§4" / "§9" 等），
        减少"凭感觉编结论"。引用错误 → prompt 自检阶段标错。
      - observation：纯事实陈述（必须包含 facts 的具体字段+数值）。
      - inference：从观察推出的判断（交易员语气，跨维度因果）。
      - supports：该证据支持哪个立场——main（主结论）/ contrarian（反对）
        / neutral（中性背景）。**禁止把 contrarian 证据伪装成 main**。
    """
    section_ref: str
    observation: str
    inference: str = ""
    supports: EvidenceSupports = "main"
    weight: EvidenceWeight = "medium"


class EvidenceMatrix(BaseModel):
    """冲突矩阵（强制结构化自我辩论）。

    AI 必须在产出 trading_plan 之前显式列出三类证据 + 关键矛盾：
      - long_evidence：所有支持做多/反弹的证据
      - short_evidence：所有支持做空/破位的证据
      - wait_evidence：所有支持等待的证据（含数据冲突 / RR 不足 / 中间位置）
      - contradictions：主要矛盾点（中文白话，列出"哪两条互相打架"）

    若 contradictions 非空且严重 → 优先 WAIT（prompt Step 7 硬约束）。
    """
    long_evidence: list[Evidence] = Field(default_factory=list)
    short_evidence: list[Evidence] = Field(default_factory=list)
    wait_evidence: list[Evidence] = Field(default_factory=list)
    contradictions: list[str] = Field(default_factory=list)


# ────────────────────────────────────────────────────────────────────────────
# 当前区评估
# ────────────────────────────────────────────────────────────────────────────

class CurrentZoneAssessment(BaseModel):
    """AI 对当前价格所处区位的判定。

    回答 4 个核心问题：
      1. 当前价在哪个 PriceZone？
      2. 该 zone 的 dominant_role（防守 / 磁铁 / 争夺 / 中间位置 / 关键位）
      3. 距上方/下方最近"决定性"区的距离
      4. 当前关键冲突一句话总结
    """
    zone_id: str = ""
    """关联 BrainPriceZone.zone_id；空 = 不在任何已识别 zone 内"""
    role: str = ""
    """"spot_defense" / "futures_target" / "liquidation_magnet" / "contested"
    / "key_level_only" / "mid_air" / "other" """
    nearest_critical_above_pct: Optional[float] = None
    """距上方最近"决定性"区的百分比（含符号，正数）"""
    nearest_critical_below_pct: Optional[float] = None
    """距下方最近"决定性"区的百分比（含符号，正数）"""
    key_conflict: str = ""
    """当前最重要的一句话冲突总结（如"现货墙强支撑 vs 下方 0.8% 处巨型清算磁铁"）"""


# ────────────────────────────────────────────────────────────────────────────
# 交易计划
# ────────────────────────────────────────────────────────────────────────────

class Target(BaseModel):
    """目标位（含理由 + RR 透明度）。"""
    price: float
    reason: str
    """命中理由（"上方 7d 清算簇 + Coinbase 强阻力"等）"""
    rr: float
    """相对 hard_invalidation 的盈亏比"""


class TradingPlan(BaseModel):
    """单个候选交易计划（不含仓位/杠杆具体数）。

    核心字段：
      - setup_type：场景描述（"扫单反转" / "回踩支撑" / "突破延续" / "区间高抛低吸"）
      - entry_zone：入场区间（落入即视为触达；不是"必须挂单"）
      - trigger_conditions：触发前置条件（"现货墙未撤" + "CVD 5m 转正" 等）
      - soft_invalidation：软失效（允许快速收回）
      - hard_invalidation：硬失效（结构失败的明确价位 + 时间条件）
      - targets：目标位列表（含每个 RR 透明度）
      - cancel_conditions：观察取消条件（"价格直接上行 X% 不回踩"等）

    风险描述（取代 position_pct / leverage_hint）：
      - risk_unit：单笔风险描述（"按 1R 风险计算" / "保守开仓"等文字）
      - leverage_risk_level：杠杆风险等级（low/medium/high/extreme）
      - position_sizing_note：仓位计算说明（"若 hard_invalidation 距离 1.2%
        且账户允许 1R/笔，仓位 ≈ R / 1.2%"）
    """
    setup_type: str
    entry_zone_low: float
    entry_zone_high: float
    trigger_conditions: list[str] = Field(default_factory=list)
    soft_invalidation: str = ""
    hard_invalidation: str
    targets: list[Target] = Field(default_factory=list)
    cancel_conditions: list[str] = Field(default_factory=list)

    risk_unit: str = ""
    leverage_risk_level: LeverageRiskLevel = "medium"
    position_sizing_note: str = ""


# ────────────────────────────────────────────────────────────────────────────
# 替代场景
# ────────────────────────────────────────────────────────────────────────────

class AlternativeScenario(BaseModel):
    """对立视角 · AI 必须给出的"第二可能性"。

    强制产出对立假设 + 概率 + 触发条件，逼 AI 自己辩论而非单向填表。
    """
    description: str
    """对立场景描述（"若现货墙被吃 → 转为下行延续"等）"""
    probability_pct: int = Field(ge=0, le=100)
    trigger: str
    """触发该场景所需的观察条件"""


# ────────────────────────────────────────────────────────────────────────────
# 扫单状态评估（GPT-15 必修 · 反骑墙规则的结构化锚点）
# ────────────────────────────────────────────────────────────────────────────

SweepBandType = Literal[
    "short_stops_above",   # 上方空头止损/轧空磁铁
    "long_stops_below",    # 下方多头止损/扫多磁铁
    "",                    # 未识别 / 无邻近带
]

SweepBandState = Literal[
    "not_swept",           # 未被触及
    "sweeping",            # 正在扫单（价格刚进入带）
    "swept_rejected",      # 上方扫空带被快速反向（突破失败 → 看空信号）
    "swept_accepted",      # 上方扫空带被站稳（突破确认 → 看多信号）
    "swept_reclaimed",     # 下方扫多带被快速收回（假跌破 → 看多信号）
    "swept_failed",        # 下方扫多带破后无法收回（破位确认 → 看空信号）
    "",                    # 未评估
]

SweepPreferredAction = Literal[
    "wait_for_sweep",                # 等待扫单
    "wait_for_reclaim",              # 等扫后收回
    "wait_for_breakout_acceptance",  # 等突破后站稳
    "limit_probe_ok",                # 边缘限价试错合格
    "no_trade",                      # 无可执行计划
    "",                              # 未评估
]


class SweepBandAssessment(BaseModel):
    """单条止损/清算带的扫单状态评估。

    设计：
      - range_low/high：带价区下/上沿（与 §8 聚合带 price_low/high 对齐）
      - band_type：标注磁铁方向（short_stops_above = 轧空磁铁；不是阻力）
      - state：扫单状态（未触/扫中/扫破被反向/扫破被站稳 等）
      - interpretation：AI 文字解读（"扫空 76,800 后被卖墙吸收，做空候选"等）
    """
    range_low: Optional[float] = None
    range_high: Optional[float] = None
    band_type: SweepBandType = ""
    state: SweepBandState = ""
    interpretation: str = ""


class SweepStateAssessment(BaseModel):
    """扫单状态总评（GPT-15 必修 · 反骑墙规则配套结构）。

    根因：反骑墙规则要求 AI 回答"扫前 / 扫中 / 扫后"，但光靠 prompt 文字让 AI
    自由表述是不可审计的。强制结构化字段才能让前端展示、回测脚本筛选、
    AI 自检（"为什么 preferred_action=wait_for_sweep？state 不是 sweeping
    而是 not_swept 应该 wait_for_sweep ✓"）。

    使用约束：
      - §8 存在止损带时**必须**填写（否则 prompt 自检阶段会标"missing"）
      - §8 完全无数据时可省略（保持向后兼容旧历史 JSON）
    """
    nearest_upper_band: Optional[SweepBandAssessment] = None
    """最近的上方扫空带（短头止损带聚合后最近一条）"""
    nearest_lower_band: Optional[SweepBandAssessment] = None
    """最近的下方扫多带（多头止损带聚合后最近一条）"""
    preferred_action: SweepPreferredAction = ""
    """当前最优动作（wait_for_sweep / wait_for_reclaim / wait_for_breakout_acceptance
    / limit_probe_ok / no_trade）"""
    notes: str = ""
    """AI 综合扫单状态后的一句话总评"""


# ────────────────────────────────────────────────────────────────────────────
# 数据自检
# ────────────────────────────────────────────────────────────────────────────

class DataSelfCheck(BaseModel):
    """AI 对本轮输入数据的自检（透明化 + confidence 解释）。

    设计：
      - missing：必要字段缺失（按 §1 数据透明度对照）
      - stale：超 TTL 的源
      - provisional：未收盘 bar 字段（has_provisional_bars=True 时填）
      - hard_stop_triggered：是否因数据严重不可信而强制 NO_TRADE
      - confidence_penalty_reason：confidence 为何打折的具体原因
    """
    missing: list[str] = Field(default_factory=list)
    stale: list[str] = Field(default_factory=list)
    provisional: list[str] = Field(default_factory=list)
    hard_stop_triggered: bool = False
    confidence_penalty_reason: str = ""


# ────────────────────────────────────────────────────────────────────────────
# AIStrategicReport · 顶层契约
# ────────────────────────────────────────────────────────────────────────────

class AIStrategicReport(BaseModel):
    """主 AI Strategic 决策官唯一输出契约。

    字段分层：
      1. 决策核心：decision / horizon / bias / confidence
      2. 当前区评估：current_zone_assessment
      3. 战略叙事：market_phase / cycle_position / 综合理由（结构/流量/宏观）
      4. 交易计划：primary_plan + alternative_plan + no_trade_conditions
      5. 替代场景：alternative_scenario
      6. 冲突矩阵：evidence_matrix（强制自我辩论）
      7. 数据自检：data_self_check
      8. 失效条件：invalidation_conditions（计划级失效；plan 内有自己的
         soft/hard_invalidation）
      9. 元数据：data_quality / prompt_debug
         （新鲜度由 REST 路由层根据 timestamp 实时计算 stale_sec 返回）

    使用约定（与 MAA 风格一致）：
      - 调用方应在 LLM 返回非法 JSON 时构造一个 `decision="NO_TRADE"` +
        `data_self_check.hard_stop_triggered=True` 的兜底实例并设置
        `prompt_debug.parse_ok=False / parse_error=...`，保证状态机连续。
    """
    coin: str
    timestamp: int

    # ── 决策核心 ──
    decision: StrategicDecision = "WAIT"
    horizon: StrategicHorizon = "intraday"
    bias: StrategicBias = "neutral"
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    confidence_rationale: str = ""

    # ── 战略叙事层 ──
    market_phase: str = ""
    """市场阶段（AI 自由表述，不限固定枚举）：
    "累积底部 / 主升浪调整 / 分配顶部 / 趋势确认 / 区间震荡 / 派发完成 / ..." """
    cycle_position: str = ""
    """周期位置（"周期早期 / 中期 / 晚期 / 顶部 / 底部"或更细致表述）"""

    # ── 当前区评估 ──
    current_zone_assessment: CurrentZoneAssessment = Field(
        default_factory=CurrentZoneAssessment,
    )

    # ── 综合理由（必须引用 §N） ──
    structure_analysis: str = ""
    """结构位 + 订单墙 + 清算图段（引用 §4-§8）"""
    flow_analysis: str = ""
    """实时流量 + 现货流入流出 + 硬证据段（引用 §9-§10）"""
    macro_context: str = ""
    """宏观 + 情绪 + 资金面慢变量段（引用 §11-§12）"""

    # ── 主交易计划（decision in [LONG_PLAN, SHORT_PLAN] 时必填） ──
    primary_plan: Optional[TradingPlan] = None

    # ── 备选计划（如主计划失效） ──
    alternative_plan: Optional[TradingPlan] = None

    # ── WAIT 触发条件（decision=WAIT 时建议填）──
    no_trade_conditions: list[str] = Field(default_factory=list)

    # ── 替代场景（强制） ──
    alternative_scenario: Optional[AlternativeScenario] = None

    # ── 扫单状态评估（反骑墙规则配套结构 · §8 有止损带时建议填写） ──
    # 旧版历史 JSON 没有此字段，Optional 兜底向后兼容
    sweep_state_assessment: Optional[SweepStateAssessment] = None

    # ── 冲突矩阵（强制） ──
    evidence_matrix: EvidenceMatrix = Field(default_factory=EvidenceMatrix)

    # ── 失效条件（计划级） ──
    invalidation_conditions: list[str] = Field(default_factory=list)

    # ── 数据自检 ──
    data_self_check: DataSelfCheck = Field(default_factory=DataSelfCheck)

    # ── 慢变量修正说明 ──
    macro_modifier_note: str = ""
    """宏观/情绪如何修正主计划（如"宏观偏空 → 仓位积极性下调 / horizon 缩短"）"""

    # ── 元数据 ──
    data_quality: DataQuality = "ok"
    prompt_debug: Optional[PromptDebug] = None
    # 新鲜度（stale_sec）由 routes_strategic._staleness_sec 在 API 层动态计算注入
    # 不持久化在 schema 里，避免 arbiter 写入与 REST 计算两套真相
