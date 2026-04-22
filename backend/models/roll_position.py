"""滚仓模块（RollPosition Manager）—— 持仓 / 计划 / 模板 / 事件 数据模型

模块定位：
    为"主观实盘交易者的滚仓计划管家"提供核心数据契约。
    引擎 (processors/roll_position_engine.py) 和 API 层 (api/roll_position.py)
    围绕这些模型展开，前端通过序列化后的 JSON 消费。

设计要点：
  - 纯数据模型（Pydantic v2），不含业务逻辑
  - 所有字段显式可序列化，便于本地 JSON 持久化与 WS 推送
  - 时间戳统一秒级 int，金额统一 USD float，价格 float
  - 所有"比例"字段显式带 _pct 后缀（0~100）或 _ratio（0~1），避免混淆

与其他模型的关系：
  - 引用 common_enums.MarketRegimeLabel（由 roll_signal 模块组合使用）
  - UserPosition 与 RollPlan 一对一绑定（一个计划管理一个持仓）
  - RollEvent 追加写 events.jsonl，同时内嵌于 UserPosition.events 便于查询
  - RollTemplate 是 RollPlan 的配置原型（派生复制）
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 枚举定义 —— 滚仓专属语义，不污染 common_enums
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Side = Literal["long", "short"]
MarginMode = Literal["isolated", "cross"]
PositionStatus = Literal["active", "closed"]

# 加仓量计算模式 —— 决定每次加仓保证金多少
# 详见 processors/roll_risk.py compute_ideal_add_margin()
AddMode = Literal[
    "passive_deleveraging",  # 肥仔派：补齐被动下降的杠杆到 target_leverage
    "pyramid_decay",          # 通用金字塔：每次加仓 = 上次 × decay_ratio
    "layered_independent",    # 李法师派：每次 = 账户总额 × 固定比例（不合并视角）
    "fixed_ratio",            # 定比例：每次 = 当前仓位 × 固定比例（用户自定义常用）
]

# 加仓触发器 —— 5 重过滤中第 5 条"至少一个触发命中"
AddTrigger = Literal[
    "structure_breakout_retest",  # 箱体/结构突破后回踩
    "key_level_bounce",           # 关键位反弹（bounced + retest_done）
    "ema_pullback_reclaim",       # 均线回踩重新站稳
    "float_profit_pct",           # 浮盈达到阈值（李法师派分层独立用）
    "squeeze_release",            # BB Squeeze 释放方向确认
    "range_boundary_reversal",    # 震荡区间边界反转（small 档专用）
    "fake_break_reversal",        # 假突破回收反手（small 档专用）
]

# 减仓信号 —— 减仓 pipeline 的打分项
ReduceSignal = Literal[
    "long_upper_wick",            # 长上影（持多时）
    "long_lower_wick",            # 长下影（持空时）
    "cvd_bear_div",               # CVD 顶背离
    "cvd_bull_div",               # CVD 底背离
    "sweep_fail_to_hold",         # 关键位扫盘未站稳
    "exhaustion_warn",            # trend_exhaustion.overall_state == exhaustion_warn
    "volume_stall_at_extreme",    # 极值位成交萎缩
    "fake_break",                 # 关键位 state == fake_break
    "structure_choch_against",    # market_structure.last_event = CHoCH 反向
    "funding_extreme",            # funding rate 极端值（方向不利）
    "reversal_pattern",           # 反转 K 线形态命中（射击之星/锤子）
]

# 加仓烈度 —— 分级滚仓核心字段
AddIntensity = Literal["full", "half", "small", "reject"]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 安全闸门参数
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class SafetyGates(BaseModel):
    """三道硬闸门 + 加仓间距硬约束。

    任何加仓量计算结果都必须通过这些闸门。若违反，引擎用二分法缩量；
    缩到 min_add_margin_usd 以下 → 放弃加仓。这是防爆仓的数学底线。
    """

    # 闸门 A · 加仓后均价距现价最小百分比（防止均价被过度压缩）
    min_avg_distance_pct: float = 2.5

    # 闸门 B · 加仓后爆仓价距现价最小百分比（保留足够回撤空间）
    min_liq_distance_pct: float = 10.0

    # 闸门 C · 加仓后有效杠杆上限（防止杠杆失控）
    max_eff_leverage: float = 10.0

    # 缩量下限：二分法求解后若建议加仓 < 此值，视为放弃加仓
    min_add_margin_usd: float = 10.0

    # 加仓间距约束：距上次加仓至少 N × ATR（防止同一腿连加）
    min_add_bar_distance_atr: float = 1.0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 置信度阈值 —— 分级滚仓的软映射
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class ConfidenceThresholds(BaseModel):
    """置信度分数到加仓烈度的映射阈值。

    加仓 score >= full_add → full；>= half_add → half；>= small_add → small；否则 hold。
    减仓 score >= full_reduce → 完整 step；>= half_reduce → 半量 step；否则 hold。

    用户可在策略模板中调整，但必须保持严格递减且在允许范围内
    （范围硬约束在 roll_templates.py 中校验）。
    """

    full_add: float = 80.0
    half_add: float = 55.0
    small_add: float = 40.0

    full_reduce: float = 60.0
    half_reduce: float = 40.0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 滚仓事件（追加写入 events.jsonl）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

EventKind = Literal[
    "init",                # 建仓初始化
    "add",                 # 加仓（用户确认执行）
    "reduce",              # 减仓（用户确认执行）
    "sl_move",             # 止损上移（用户确认执行）
    "close_manual",        # 手动平仓
    "close_sl_hit",        # 止损触发平仓
    "close_tp_hit",        # 止盈触发平仓
    "alert_add",           # 加仓建议提醒（未执行）
    "alert_reduce",        # 减仓建议提醒
    "alert_close",         # 离场建议提醒
    "alert_move_sl",       # 移止损建议提醒
    "alert_forward",       # 前瞻窗口提醒
    "gate_blocked",        # 通过 5 重过滤但被闸门拦截
    "user_override_add",   # 用户手动覆盖加仓（系统原本不推荐）
]


class RollEvent(BaseModel):
    """滚仓生命周期中的单次事件。

    同时用于：
      - UserPosition.events 内嵌（便于查询当前持仓历史）
      - events.jsonl 追加（永久可回放）

    设计为尽量自包含：回放时不强依赖外部市场快照，
    但通过 market_snapshot_ref 指向 snapshot_archiver 的 key
    便于深度复盘。
    """

    ts: int
    kind: EventKind

    # 动作层字段
    price: float = 0.0
    margin_delta_usd: float = 0.0        # 正=加，负=减
    size_delta: float = 0.0              # 币数变化（同号 margin）

    # 动作后快照（便于回放不依赖前一事件）
    avg_price_after: float = 0.0
    leverage_after: float = 0.0          # 有效杠杆
    liq_price_after: float = 0.0
    sl_after: Optional[float] = None

    # 溯源
    reason: str = ""
    market_snapshot_ref: Optional[str] = None  # 指向 snapshot_archiver
    system_confidence: float = 0.0              # 事件发生时系统给出的 confidence_score
    system_action: str = ""                      # 事件发生时系统建议的动作（用于覆盖率统计）
    user_override: bool = False                  # 本事件是否由用户手动覆盖产生


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 用户持仓（一个滚仓计划管理一个持仓）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class UserPosition(BaseModel):
    """用户当前持有的合约仓位状态。

    字段会随事件发生而被引擎更新：
      - entry_price / position_size / margin_used_usd 每次 add/reduce 重算
      - liq_price 每次 add/reduce/sl_move 重算（基于 margin_mode + leverage）
      - stop_loss 用户执行 sl_move 后更新
      - status 在 close_* 事件后变为 "closed"

    注意：本字段为"用户声明的实盘仓位"，不从交易所 API 同步。
    实际交易所仓位与此值不一致时，由用户在 UI 手动校准。
    """

    id: str                                # 唯一 ID（UUID4）
    coin: str                              # 币种（BTC/ETH/SOL，大写）
    side: Side
    margin_mode: MarginMode
    leverage: int = Field(..., ge=1, le=125)  # 名义杠杆（交易所设置值）

    # 加权均价与仓位
    entry_price: float = Field(..., gt=0)
    # 注意：平仓后 size/value/margin 置为 0，因此约束为 ge=0 而非 gt=0
    # 建仓时必须 >0 由 RollService.create_position 业务层校验
    position_size: float = Field(..., ge=0)        # 币数
    position_value_usd: float = Field(..., ge=0)   # 名义价值（size × current_price，建仓时 = size × entry）
    margin_used_usd: float = Field(..., ge=0)      # 已占用保证金

    # 账户维度（用于资金占用硬约束）
    total_account_usd: float = Field(..., gt=0)    # 用户声明的账户总额

    # 止损与爆仓
    stop_loss: Optional[float] = None              # 当前止损价（可能为 None 表示无）
    initial_stop_loss: Optional[float] = None      # 建仓时的初始止损（用于复盘对比）
    liq_price: Optional[float] = None              # 估算爆仓价

    # 生命周期
    status: PositionStatus = "active"
    plan_id: str                                   # 关联的 RollPlan.id
    created_at: int
    updated_at: int
    closed_at: Optional[int] = None

    # 事件历史（内嵌便于查询；持久化由 events.jsonl 承担，加载时回填）
    events: list[RollEvent] = Field(default_factory=list)

    # 用户自定义别名（便于多计划区分）
    note: str = ""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 滚仓计划（策略规则 · 与 UserPosition 一对一）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class RollPlan(BaseModel):
    """滚仓规则的完整配置。

    一个计划绑定一个持仓，由用户从模板派生（复制模板默认值后可微调）。
    切换模板不影响已有计划，只影响未来新建——这是产品硬约定。
    """

    id: str
    position_id: str
    name: str = ""                         # 用户自定义名称（如 "BTC 大趋势滚仓-1"）
    template_id: str                       # 来源模板（fatzhai/li_fashi/pyramid/conservative/custom:xxx）

    # ── 加仓部分 ──
    add_mode: AddMode
    # passive_deleveraging 用：滚仓要维持的名义杠杆
    target_leverage: float = 10.0
    # pyramid_decay 用：每次加仓额 = 上次 × decay_ratio
    pyramid_decay_ratio: float = 0.6
    # layered_independent 用：每次加仓保证金 = 账户总额 × 此比例
    layered_pct_of_account: float = 0.1
    # fixed_ratio 用：每次加仓保证金 = 当前仓位保证金 × 此比例
    fixed_ratio_of_position: float = 0.3

    add_triggers: list[AddTrigger] = Field(default_factory=list)
    min_profit_pct_to_add: float = 3.0     # 浮盈达到此比例才评估加仓
    max_add_times: int = Field(3, ge=1, le=10)

    # ── 减仓部分 ──
    reduce_signals: list[ReduceSignal] = Field(default_factory=list)
    # 减仓比例（full 档时减总仓的百分比）
    reduce_step_size_pct: float = 0.3

    # ── 止损移动 ──
    # 第 N 次加仓成功后，建议把止损挪到保本（N=1 即首次加仓后）
    trail_sl_after_add_n: int = 1
    trail_sl_atr_mult: float = 1.5

    # ── 安全闸门 ──
    gates: SafetyGates = Field(default_factory=SafetyGates)

    # ── 置信度阈值 ──
    thresholds: ConfidenceThresholds = Field(default_factory=ConfidenceThresholds)

    # ── 资金占用硬约束 ──
    # 单计划保证金占账户比例上限（超过则拒绝加仓）
    max_margin_pct_of_account: float = 0.30

    # ── 生命周期 ──
    active: bool = True
    created_at: int


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 策略模板（RollPlan 的配置原型）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class RollTemplate(BaseModel):
    """滚仓策略模板。

    内置模板（builtin=True）只读，用户可"派生复制"产生自定义模板（builtin=False）。
    字段完全镜像 RollPlan 的配置部分，用 default_ 前缀区分"模板默认值"与"实例值"。
    """

    id: str                                # "fatzhai" / "li_fashi" / "pyramid" / "conservative" / "custom:xxx"
    name: str
    description: str = ""
    builtin: bool = False

    # ── 加仓配置 ──
    add_mode: AddMode
    target_leverage: float = 10.0
    pyramid_decay_ratio: float = 0.6
    layered_pct_of_account: float = 0.1
    fixed_ratio_of_position: float = 0.3
    default_add_triggers: list[AddTrigger] = Field(default_factory=list)
    min_profit_pct_to_add: float = 3.0
    max_add_times: int = 3

    # ── 减仓配置 ──
    default_reduce_signals: list[ReduceSignal] = Field(default_factory=list)
    reduce_step_size_pct: float = 0.3

    # ── 止损配置 ──
    trail_sl_after_add_n: int = 1
    trail_sl_atr_mult: float = 1.5

    # ── 闸门配置 ──
    gates: SafetyGates = Field(default_factory=SafetyGates)
    thresholds: ConfidenceThresholds = Field(default_factory=ConfidenceThresholds)

    # ── 资金约束 ──
    max_margin_pct_of_account: float = 0.30

    # ── 推荐的 margin mode（新建计划时的默认值，用户可改） ──
    recommended_margin_mode: MarginMode = "isolated"

    # ── 元信息 ──
    created_at: int = 0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 全局设置（/roll/settings 页面配置）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class RollGlobalSettings(BaseModel):
    """滚仓模块的全局设置。

    与计划分离：这些字段作用于所有活跃计划。
    持久化到 backend/data/roll/settings.json。
    """

    # 账户维度 —— 所有计划共用
    total_account_usd: float = Field(10000.0, gt=0)

    # 同币种所有活跃计划合计保证金 / 账户 上限
    per_coin_margin_pct_cap: float = 0.50
    # 全账户所有活跃计划合计保证金 / 账户 上限
    account_margin_pct_cap: float = 0.80

    # 静默时段（UTC 小时，范围 [start, end)，跨日用 start>end）
    quiet_hours_enabled: bool = False
    quiet_start_utc: int = 23
    quiet_end_utc: int = 7
    # 静默时段内是否仍推 urgent 级别提醒
    quiet_allow_urgent: bool = True

    # 浏览器 Notification
    notification_enabled: bool = True
    notification_sound_for_urgent: bool = True

    # 前瞻窗口提醒频控（分钟）—— 同计划同类型 N 分钟最多 1 次
    forward_alert_cooldown_min: int = 30

    # 覆盖行为熔断
    override_cooldown_enabled: bool = True
    override_warn_threshold: int = 7       # 近 N 次覆盖亏 ≥ 此数触发警告
    override_warn_window: int = 10         # 近 N 次
    override_cooldown_hours: int = 24      # 连续 2 次警告仍覆盖 → 冷却时长

    # 更新时间（便于 UI 显示）
    updated_at: int = 0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 复盘统计（/roll/replay 页面消费）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class ReplayStats(BaseModel):
    """单个持仓（通常为已平仓）的回放统计。

    pure-function 计算自 events.jsonl 事件流，不引入新状态。
    由 processors/roll_replay.py::compute_replay_stats() 生成。

    字段语义：
      - follow_rate_X = 用户在阈值窗口内对 alert_X 做出的匹配动作数 / alert_X 总数
      - override_rate = user_override_add / (add + user_override_add)
      - 所有 rate 在样本为 0 时为 None，UI 呈现为 "—"
      - realized_pnl_usd 从 reduce/close 事件累计（price × closed_size × 方向）
    """

    position_id: str
    plan_id: str
    coin: str
    side: Side
    status: PositionStatus

    # 时间
    opened_at: int
    closed_at: Optional[int] = None
    duration_sec: int = 0

    # 事件总量（kind → count）
    total_events: int = 0
    counts_by_kind: dict[str, int] = Field(default_factory=dict)

    # 动作汇总
    adds: int = 0
    reduces: int = 0
    sl_moves: int = 0
    closes: int = 0
    overrides: int = 0
    gate_blocks: int = 0

    # 告警汇总（按对应动作）
    alerts_by_action: dict[str, int] = Field(default_factory=dict)

    # 覆盖率
    follow_rate_add: Optional[float] = None
    follow_rate_reduce: Optional[float] = None
    follow_rate_close: Optional[float] = None
    follow_rate_move_sl: Optional[float] = None
    follow_rate_overall: Optional[float] = None
    avg_follow_delay_sec: Optional[float] = None

    # 覆盖行为
    override_rate: Optional[float] = None

    # P&L
    realized_pnl_usd: float = 0.0
    realized_pnl_pct: float = 0.0                 # 相对初始保证金
    max_margin_used_usd: float = 0.0
    peak_effective_leverage: float = 0.0

    # 关仓类型
    final_close_kind: Optional[str] = None       # close_manual / close_sl_hit / close_tp_hit
