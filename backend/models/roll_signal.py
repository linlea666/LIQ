"""滚仓引擎输出模型 —— 对单个活跃计划的实时评估结果

数据流：
    roll_position_engine.evaluate(position, plan, market_state)
        → RollSignal（推送到前端 + 写入 events.jsonl 作为 alert_*）

与现有模块的对比：
  - ExecutionPlan 是"新仓狙击"语义（entry/sl/tp），由 Synthesizer 产出
  - RollSignal 是"持仓管理"语义（add/reduce/close/hold/move_sl），
    由 roll_position_engine 针对已有仓位产出
  两者不重叠、不互相消费。

可解释性约定：
  - 每个 RollSignal 都必须同时包含 supporting（支持本动作）和 blocking（反对本动作）
    两组 SignalRef，让用户看到"系统为什么给/不给这个建议"
  - confidence_score 和 confidence_breakdown 一一对应，方便前端雷达图渲染
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

from models.roll_position import AddIntensity


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 动作 & 紧迫程度
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# 滚仓引擎给出的动作建议
RollAction = Literal["add", "reduce", "close", "hold", "move_sl"]

# 提醒紧迫程度（驱动前端颜色 + Notification 声音）
Urgency = Literal["info", "attention", "urgent"]

# 评估流水线阶段名 —— 用于 skipped_phases 透出
# 注：Phase 0（data_health / safety_gate）一旦触发会导致 hold，
#     但语义上数据健康/护栏是"评估中止"，不同于"下游被高优先级短路"，因此
#     data_health / safety_gate 触发时 skipped_phases 会包含所有后续 phase。
EvalPhase = Literal["exit", "reduce", "trail_sl", "add"]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 信号引用（可解释性单位）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class SignalRef(BaseModel):
    """单个信号引用 —— 引擎判断依据的最小单位。

    示例：
      - source="market_structure_4h", read="BOS_down", weight=20, detail="4H 结构向下延续"
      - source="key_level_v2#74150", read="bounced+retest_done", weight=15
      - source="trend_exhaustion", read="healthy_continuation (score=0.62)", weight=15
      - source="safety_gate#g3", read="liq_chaos", weight=-100 (硬红线)
    """

    source: str                  # 现有模块名 + 可选 ID（如 key_level_v2#74150）
    read: str                    # 该模块当前的读数（枚举值 / 数值描述）
    weight: float = 0.0          # 对 confidence_score 的贡献（带符号）
    detail: str = ""             # 白话解释，前端 tooltip 用


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 加仓预演（UI 强制可视化组件数据源）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class PreviewMetrics(BaseModel):
    """加仓前/后对比的指标集。

    前端 AddPreviewCard 直接按字段渲染表格；
    若 None 则显示 "-"（表示该指标不适用，如爆仓价在极端情况无法估算）。
    """

    avg_price: float = 0.0
    distance_to_price_pct: float = 0.0     # 均价距现价百分比（带符号，负=现价跌破均价）
    effective_leverage: float = 0.0
    liq_price: Optional[float] = None
    liq_distance_pct: Optional[float] = None
    position_value_usd: float = 0.0
    margin_used_usd: float = 0.0
    account_margin_pct: float = 0.0        # 占账户总额百分比


class GatesStatus(BaseModel):
    """三道闸门的通过状态（加仓预演专用）。

    字段命名与 processors/roll_risk.GateCheckResult 保持一致，便于引擎直接映射。
    A=均价距现价；B=爆仓距现价；C=有效杠杆。
    """

    gate_a_pass: bool = True
    gate_b_pass: bool = True
    gate_c_pass: bool = True

    # 每道闸门当前读数（便于前端显示 "3.3% ≥ 2.5% ✅"）
    gate_a_actual: float = 0.0
    gate_a_required: float = 0.0
    gate_b_actual: float = 0.0
    gate_b_required: float = 0.0
    gate_c_actual: float = 0.0
    gate_c_required: float = 0.0


class AddPreview(BaseModel):
    """加仓预演（action=add 时必填）。

    关键字段：
      - ideal_margin_usd：按策略模板理论应加仓的量
      - final_margin_usd：经 3 道闸门二分法缩量后的实际建议量
      - shrink_reason：若 final < ideal，说明被哪条闸门约束
      - intensity：加仓烈度（full/half/small/reject），对应量乘数
    """

    mode: str                              # 复用 AddMode Literal 的字符串值
    intensity: AddIntensity

    ideal_margin_usd: float = 0.0          # 未过闸门前的理论量
    intensity_multiplier: float = 1.0      # 烈度乘数（full=1.0, half=0.5, small=0.3）
    after_intensity_usd: float = 0.0       # 应用烈度乘数后
    final_margin_usd: float = 0.0          # 过闸门后的最终建议量

    shrink_reason: str = ""                # 若被缩量，指出主要约束来源
    add_size_delta: float = 0.0            # 加仓带来的币数变化

    suggested_new_sl: Optional[float] = None  # 加仓同时建议的新止损

    before: PreviewMetrics = Field(default_factory=PreviewMetrics)
    after: PreviewMetrics = Field(default_factory=PreviewMetrics)
    gates: GatesStatus = Field(default_factory=GatesStatus)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 前瞻窗口（预告类提醒）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

ForwardKind = Literal[
    "squeeze_release_imminent",     # BB Squeeze 即将释放
    "key_level_approaching",        # 关键位即将触及
    "structure_pending_confirm",    # 结构即将确认（swing 即将成型）
    "exhaustion_early_hint",        # 动能拐头早期迹象
]


class ForwardWindow(BaseModel):
    """前瞻窗口提醒 —— 不触发动作，只给用户心理准备。

    频控策略（在引擎中实现）：
      - 同 position_id + kind 在 settings.forward_alert_cooldown_min 分钟内最多 1 次
      - 不同 kind 可叠加
    """

    kind: ForwardKind
    ts: int
    expires_at: int                   # 预期窗口关闭时间戳
    hint_cn: str                      # 白话："4H 结构将在约 25 分钟后确认"
    related_signals: list[SignalRef] = Field(default_factory=list)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 主模型：RollSignal
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class RollSignal(BaseModel):
    """滚仓引擎对单个活跃计划的评估结果。

    推送通道：WebSocket 频道 roll:{position_id}，前端按 action 渲染对应 UI。
    持久化：仅重要事件（alert_add / alert_reduce / alert_close / alert_move_sl / gate_blocked）
           写入 events.jsonl 作为 RollEvent 记录，hold 状态不落盘避免噪声。
    """

    position_id: str
    plan_id: str
    ts: int
    coin: str
    current_price: float

    # ── 实时状态（恒有值） ────────────────────────────────
    unrealized_pnl_pct: float = 0.0       # 浮盈百分比（基于 entry_price 与 current_price）
    unrealized_pnl_usd: float = 0.0
    effective_leverage: float = 0.0       # 被动下降后的真实杠杆
    distance_to_liq_pct: Optional[float] = None   # 现价距爆仓百分比（None=无法估算）
    distance_to_sl_pct: Optional[float] = None    # 现价距止损百分比（None=无止损）

    # ── 动作建议 ─────────────────────────────────────────
    action: RollAction = "hold"
    urgency: Urgency = "info"

    # ── 置信度（分级滚仓核心） ───────────────────────────
    confidence_score: float = 0.0         # 累加分数（-100~100 之间，未封顶）
    confidence_breakdown: dict[str, float] = Field(default_factory=dict)
    # breakdown 示例：{"regime": 25, "structure_4h": 20, "exhaustion": 15, ...}
    add_intensity: AddIntensity = "reject"  # action=add 时有意义；否则为 reject

    # ── 加仓建议（action=add 时必填，其他情况为 None） ───
    add_preview: Optional[AddPreview] = None

    # ── 减仓建议（action=reduce 时填） ───────────────────
    reduce_pct: Optional[float] = None    # 减总仓的百分比（0~1）
    reduce_confidence: float = 0.0

    # ── 移止损建议（action=move_sl 时填） ────────────────
    suggested_new_sl: Optional[float] = None
    sl_move_reason: str = ""              # "breakeven" / "trail_atr" / ...

    # ── 前瞻窗口（独立 urgency=info 推送，不影响主动作） ──
    forward_windows: list[ForwardWindow] = Field(default_factory=list)

    # ── 可解释（必填，空列表也必须存在） ─────────────────
    supporting: list[SignalRef] = Field(default_factory=list)
    blocking: list[SignalRef] = Field(default_factory=list)

    # ── 白话输出 ─────────────────────────────────────────
    headline_cn: str = ""                 # 一句话结论，Banner 用
    detail_cn: str = ""                   # 两三句解释，展开用

    # ── 元信息 ────────────────────────────────────────────
    # 评估用到的市场数据是否完整；partial/insufficient 时降级显示
    data_quality: Literal["ok", "partial", "insufficient"] = "ok"
    missing_inputs: list[str] = Field(default_factory=list)

    # ── 流水线透明度 ─────────────────────────────────────
    # 高优先级 Phase 触发后被跳过的下游 Phase 列表。
    # 典型场景：Phase 1 Exit 命中 → skipped_phases=["reduce","trail_sl","add"]
    # 前端据此把 pipeline 可视化条的对应段置灰，避免用户误解为"加/减仓引擎没跑"。
    # 空列表表示 evaluate() 正常完整走完，或 Phase 4 自然停在 hold。
    skipped_phases: list[EvalPhase] = Field(default_factory=list)
