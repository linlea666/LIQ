/**
 * 滚仓模块 TypeScript 类型 —— 与后端 Pydantic 模型一一对应
 *
 * 约定：
 *   - 字段名与 snake_case 保持一致（不做 camelCase 转换，避免运行时拷贝）
 *   - 所有枚举值直接复用后端 Literal 字符串集合
 *   - 可选字段统一用 `T | null`（Pydantic `Optional[T]` 序列化后为 null）
 */

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
// 枚举
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

export type Side = "long" | "short";
export type MarginMode = "isolated" | "cross";
export type PositionStatus = "active" | "closed";

export type AddMode =
  | "passive_deleveraging"
  | "pyramid_decay"
  | "layered_independent"
  | "fixed_ratio";

export type AddTrigger =
  | "structure_breakout_retest"
  | "key_level_bounce"
  | "ema_pullback_reclaim"
  | "float_profit_pct"
  | "squeeze_release"
  | "range_boundary_reversal"
  | "fake_break_reversal";

export type ReduceSignal =
  | "long_upper_wick"
  | "long_lower_wick"
  | "cvd_bear_div"
  | "cvd_bull_div"
  | "sweep_fail_to_hold"
  | "exhaustion_warn"
  | "volume_stall_at_extreme"
  | "fake_break"
  | "structure_choch_against"
  | "funding_extreme"
  | "reversal_pattern";

export type AddIntensity = "full" | "half" | "small" | "reject";

export type RollAction = "add" | "reduce" | "close" | "hold" | "move_sl";
export type Urgency = "info" | "attention" | "urgent";

export type EventKind =
  | "init"
  | "add"
  | "reduce"
  | "sl_move"
  | "close_manual"
  | "close_sl_hit"
  | "close_tp_hit"
  | "alert_add"
  | "alert_reduce"
  | "alert_close"
  | "alert_move_sl"
  | "alert_forward"
  | "gate_blocked"
  | "user_override_add";

export type ForwardKind =
  | "squeeze_release_imminent"
  | "key_level_approaching"
  | "structure_pending_confirm"
  | "exhaustion_early_hint";

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
// 配置
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

export interface SafetyGates {
  min_avg_distance_pct: number;
  min_liq_distance_pct: number;
  max_eff_leverage: number;
  min_add_margin_usd: number;
  min_add_bar_distance_atr: number;
}

export interface ConfidenceThresholds {
  full_add: number;
  half_add: number;
  small_add: number;
  full_reduce: number;
  half_reduce: number;
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
// 持仓 / 计划 / 事件
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

export interface RollEvent {
  ts: number;
  kind: EventKind;
  price: number;
  margin_delta_usd: number;
  size_delta: number;
  avg_price_after: number;
  leverage_after: number;
  liq_price_after: number;
  sl_after: number | null;
  reason: string;
  market_snapshot_ref: string | null;
  system_confidence: number;
  system_action: string;
  user_override: boolean;
}

export interface UserPosition {
  id: string;
  coin: string;
  side: Side;
  margin_mode: MarginMode;
  leverage: number;

  entry_price: number;
  position_size: number;
  position_value_usd: number;
  margin_used_usd: number;

  total_account_usd: number;

  stop_loss: number | null;
  initial_stop_loss: number | null;
  liq_price: number | null;

  status: PositionStatus;
  plan_id: string;
  created_at: number;
  updated_at: number;
  closed_at: number | null;

  events: RollEvent[];
  note: string;
}

export interface RollPlan {
  id: string;
  position_id: string;
  name: string;
  template_id: string;

  add_mode: AddMode;
  target_leverage: number;
  pyramid_decay_ratio: number;
  layered_pct_of_account: number;
  fixed_ratio_of_position: number;

  add_triggers: AddTrigger[];
  min_profit_pct_to_add: number;
  max_add_times: number;

  reduce_signals: ReduceSignal[];
  reduce_step_size_pct: number;

  trail_sl_after_add_n: number;
  trail_sl_atr_mult: number;

  gates: SafetyGates;
  thresholds: ConfidenceThresholds;

  max_margin_pct_of_account: number;
  active: boolean;
  created_at: number;
}

export interface RollTemplate {
  id: string;
  name: string;
  description: string;
  builtin: boolean;

  add_mode: AddMode;
  target_leverage: number;
  pyramid_decay_ratio: number;
  layered_pct_of_account: number;
  fixed_ratio_of_position: number;
  default_add_triggers: AddTrigger[];
  min_profit_pct_to_add: number;
  max_add_times: number;

  default_reduce_signals: ReduceSignal[];
  reduce_step_size_pct: number;

  trail_sl_after_add_n: number;
  trail_sl_atr_mult: number;

  gates: SafetyGates;
  thresholds: ConfidenceThresholds;

  max_margin_pct_of_account: number;
  recommended_margin_mode: MarginMode;
  created_at: number;
}

export interface RollGlobalSettings {
  total_account_usd: number;
  per_coin_margin_pct_cap: number;
  account_margin_pct_cap: number;

  quiet_hours_enabled: boolean;
  quiet_start_utc: number;
  quiet_end_utc: number;
  quiet_allow_urgent: boolean;

  notification_enabled: boolean;
  notification_sound_for_urgent: boolean;

  forward_alert_cooldown_min: number;

  override_cooldown_enabled: boolean;
  override_warn_threshold: number;
  override_warn_window: number;
  override_cooldown_hours: number;

  /** 距爆仓百分比小于此值（%）时强制 close + urgent。范围 [1, 15]，默认 5.0 */
  liq_emergency_pct: number;

  updated_at: number;
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
// 信号（RollSignal）
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

export interface SignalRef {
  source: string;
  read: string;
  weight: number;
  detail: string;
}

export interface PreviewMetrics {
  avg_price: number;
  distance_to_price_pct: number;
  effective_leverage: number;
  liq_price: number | null;
  liq_distance_pct: number | null;
  position_value_usd: number;
  margin_used_usd: number;
  account_margin_pct: number;
}

export interface GatesStatus {
  gate_a_pass: boolean;
  gate_b_pass: boolean;
  gate_c_pass: boolean;
  gate_a_actual: number;
  gate_a_required: number;
  gate_b_actual: number;
  gate_b_required: number;
  gate_c_actual: number;
  gate_c_required: number;
}

export interface AddPreview {
  mode: AddMode;
  intensity: AddIntensity;

  ideal_margin_usd: number;
  intensity_multiplier: number;
  after_intensity_usd: number;
  final_margin_usd: number;

  shrink_reason: string;
  add_size_delta: number;
  suggested_new_sl: number | null;

  before: PreviewMetrics;
  after: PreviewMetrics;
  gates: GatesStatus;
}

export interface ForwardWindow {
  kind: ForwardKind;
  ts: number;
  expires_at: number;
  hint_cn: string;
  related_signals: SignalRef[];
}

export interface RollSignal {
  position_id: string;
  plan_id: string;
  ts: number;
  coin: string;
  current_price: number;

  unrealized_pnl_pct: number;
  unrealized_pnl_usd: number;
  effective_leverage: number;
  distance_to_liq_pct: number | null;
  distance_to_sl_pct: number | null;

  action: RollAction;
  urgency: Urgency;

  confidence_score: number;
  confidence_breakdown: Record<string, number>;
  add_intensity: AddIntensity;

  add_preview: AddPreview | null;

  reduce_pct: number | null;
  reduce_confidence: number;

  suggested_new_sl: number | null;
  sl_move_reason: string;

  forward_windows: ForwardWindow[];
  supporting: SignalRef[];
  blocking: SignalRef[];

  headline_cn: string;
  detail_cn: string;
  data_quality: "ok" | "partial" | "insufficient";
  missing_inputs: string[];
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
// API 请求/响应
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

export interface CreatePositionReq {
  coin: string;
  side: Side;
  margin_mode: MarginMode;
  leverage: number;
  entry_price: number;
  margin_usd: number;
  template_id: string;
  name?: string;
  note?: string;
  stop_loss?: number | null;
  plan_overrides?: Record<string, unknown> | null;
}

export interface PositionWithPlanResp {
  position: UserPosition;
  plan: RollPlan;
  latest_signal: RollSignal | null;
}

export interface ExecuteEventReq {
  kind: "add" | "reduce" | "close" | "move_sl";
  price: number;
  margin_delta_usd?: number | null;
  reduce_pct?: number | null;
  close_kind?: "close_manual" | "close_sl_hit" | "close_tp_hit";
  new_sl?: number | null;
  reason?: string;
  system_confidence?: number;
  system_action?: string;
}

export interface OverrideAddReq {
  price: number;
  margin_delta_usd: number;
  reason?: string;
  system_confidence?: number;
  system_action?: string;
}

export interface DeriveTemplateReq {
  source_id: string;
  new_id: string;
  new_name: string;
}

export interface UpdateTemplateReq {
  patch: Record<string, unknown>;
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
// 复盘统计（/roll/replay 页面消费）
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

export interface ReplayStats {
  position_id: string;
  plan_id: string;
  coin: string;
  side: Side;
  status: PositionStatus;

  opened_at: number;
  closed_at: number | null;
  duration_sec: number;

  total_events: number;
  counts_by_kind: Record<string, number>;

  adds: number;
  reduces: number;
  sl_moves: number;
  closes: number;
  overrides: number;
  gate_blocks: number;

  alerts_by_action: Record<string, number>;

  follow_rate_add: number | null;
  follow_rate_reduce: number | null;
  follow_rate_close: number | null;
  follow_rate_move_sl: number | null;
  follow_rate_overall: number | null;
  avg_follow_delay_sec: number | null;

  override_rate: number | null;

  realized_pnl_usd: number;
  realized_pnl_pct: number;
  max_margin_used_usd: number;
  peak_effective_leverage: number;

  final_close_kind: "close_manual" | "close_sl_hit" | "close_tp_hit" | null;
}

export interface ReplayResp {
  position: UserPosition;
  plan: RollPlan | null;
  events: RollEvent[];
  stats: ReplayStats;
}

// 枚举 metadata（后端 /api/roll/enums 返回）—— 纯字符串集合 + 默认值
export interface RollEnumsPayload {
  sides: Side[];
  margin_modes: MarginMode[];
  add_modes: AddMode[];
  add_triggers: AddTrigger[];
  reduce_signals: ReduceSignal[];
  safety_gates_defaults: SafetyGates;
  thresholds_defaults: ConfidenceThresholds;
}
