/**
 * 潜力币雷达 · 类型定义
 *
 * 一条原则贯穿所有类型：**数值可以为 null，但可信度不能**。
 *
 * 后端对"没查过"和"查过但没有"做了严格区分（前者 null，后者 0/false），
 * 前端类型必须把这个区分保留下来。一旦在这层用 `?? 0` 抹平，
 * 界面上就再也分不清"该币没有智能钱包买入"和"我们还没查过它的智能钱包"，
 * 而这两者在决策上是完全相反的含义。
 */

/** 五维评分。永远整体出现——单看机会分会做出危险的判断。 */
export interface RadarScores {
  opportunity: number;
  confidence: number;
  data_quality: number;
  rug_risk: number;
  distribution: number;
}

export interface RadarRisk {
  blocked: boolean;
  block_reason: string | null;
  gate_blocked: boolean;
  gate_reasons: string[];
  audit_checked: boolean;
}

export type RadarState =
  | "DISCOVERED"
  | "WATCHING"
  | "S0"
  | "S1"
  | "S2"
  | "MOMENTUM"
  | "DISTRIBUTION"
  | "DORMANT"
  | "DEAD"
  | "BLOCKED";

export interface RadarToken {
  chain_id: string;
  contract_address: string;
  symbol: string | null;
  name: string | null;
  state: RadarState;
  state_since_ms: number | null;
  age_sec: number | null;
  first_seen_ms: number | null;
  last_observed_ms: number | null;
  price: number | null;
  market_cap: number | null;
  /** "reported" 表示接口直接给的；"computed" 表示我们用供应量算的（可能偏差很大）。 */
  mc_source: string | null;
  liquidity: number | null;
  holders: number | null;
  top10_percent: number | null;
  dev_percent: number | null;
  smart_money_count: number | null;
  net_inflow: number | null;
  pct_change_1h: number | null;
  volume_1h: number | null;
  scores: RadarScores;
  risk: RadarRisk;
  quality_degraded: boolean;
  is_reject_sample: boolean;
  tags: string[];
}

export interface RadarTokenListResponse {
  total: number;
  items: RadarToken[];
  states: Record<string, number>;
}

export interface RadarAlert {
  alert_id: number;
  token_id: number;
  chain_id?: string;
  contract_address?: string;
  symbol?: string | null;
  alert_kind: string;
  is_near_miss: number;
  created_at: number;
  opportunity: number | null;
  confidence: number | null;
  data_quality: number | null;
  rug_risk: number | null;
  distribution: number | null;
  review_state: string | null;
  reviewed_at: number | null;
  strategy_version?: string | null;
  config_hash?: string | null;
  trigger?: Record<string, unknown> | null;
  factors?: Array<Record<string, unknown>> | null;
  prev_scores?: Record<string, number> | null;
  peak_multiple?: number | null;
  current_multiple?: number | null;
  outcome_label?: string | null;
  is_final?: number | null;
}

export interface RadarOutcome {
  alert_id: number;
  signal_at: number;
  signal_price: number | null;
  signal_market_cap: number | null;
  raw_ath_price: number | null;
  raw_ath_mc: number | null;
  raw_ath_at: number | null;
  /** 剔除瞬时插针后的高点。它比 raw 低，但那才是真能卖出去的价。 */
  sustained_ath_price: number | null;
  sustained_ath_mc: number | null;
  /** 按流动性折算后的可实现倍数——纸面 10 倍常常只有 3 倍能落袋。 */
  liq_adjusted_multiple: number | null;
  peak_multiple: number | null;
  current_multiple: number | null;
  mfe_pct: number | null;
  mae_pct: number | null;
  time_to_2x_sec: number | null;
  time_to_5x_sec: number | null;
  time_to_10x_sec: number | null;
  lead_time_sec: number | null;
  outcome_label: string | null;
  is_final: number | null;
  horizons: Record<string, unknown> | null;
  entry_15s?: number | null;
  entry_30s?: number | null;
  entry_60s?: number | null;
  entry_120s?: number | null;
}

export interface RadarRejection {
  rejection_id: number;
  token_id: number;
  chain_id?: string;
  contract_address?: string;
  symbol?: string | null;
  occurred_at: number;
  gate: string;
  rule: string;
  actual_value: number | null;
  threshold_value: number | null;
  actual_text: string | null;
  data_quality: number | null;
}

export interface RadarSnapshot {
  observed_at: number;
  price: number | null;
  market_cap: number | null;
  liquidity: number | null;
  holders: number | null;
  top10_percent: number | null;
  opportunity: number | null;
  confidence: number | null;
  data_quality: number | null;
  rug_risk: number | null;
  distribution: number | null;
  state: string | null;
  endpoint: string | null;
}

export interface RadarMilestone {
  milestone_usd: number;
  occurred_at: number;
  market_cap: number | null;
  token_age_sec: number | null;
}

export interface RadarTokenDetail {
  identity: Record<string, unknown> & {
    token_id: number;
    chain_id: string;
    contract_address: string;
    symbol: string | null;
    in_memory: boolean;
  };
  live: RadarToken | null;
  quality: {
    degraded: boolean;
    group_updated_at: Record<string, number>;
    field_source: Record<string, string>;
    observation_count: number;
    history_depth: number;
  } | null;
  snapshots: RadarSnapshot[];
  alerts: RadarAlert[];
  milestones: RadarMilestone[];
  outcomes: RadarOutcome[];
  rejections: RadarRejection[];
}

export interface RadarHealth {
  status: "ok" | "degraded";
  running: boolean;
  uptime_sec: number;
  tokens_in_memory: number;
  rss_mb: number | null;
  last_cycle_at: number | null;
  collector_ok: boolean;
  email_usable: boolean;
  version: { config_hash: string; code_commit: string; strategy_version: string };
}

export interface RadarEvent {
  event_id: number;
  occurred_at: number;
  event_type: string;
  category: string;
  severity: string;
  importance: string;
  module: string | null;
  summary: string | null;
  correlation_id: string | null;
  chain_id: string | null;
  contract_address: string | null;
  payload: Record<string, unknown> | null;
}

export interface RadarKpi {
  stat_date: string;
  strategy_version: string;
  alert_kind: string;
  horizon: string;
  /** 样本量必须显示：3 个样本的 67% 和 300 个样本的 67% 不是同一个信息。 */
  matured_count: number;
  payload: Record<string, number> | null;
}

/** 近 N 天推送质量汇总（/research/kpi/summary，周报同款口径）。 */
export interface RadarKpiSummaryGroup {
  alert_kind: string;
  strategy_version: string;
  horizon: string;
  matured_count: number;
  labels: Record<string, number>;
  hit_2x_ratio: number | null;
  hit_5x_ratio: number | null;
  hit_10x_ratio: number | null;
  median_peak_multiple: number | null;
  median_liq_adjusted: number | null;
  median_mae_pct: number | null;
  rug_ratio: number | null;
}

export interface RadarKpiSummary {
  window_days: number;
  since: number;
  until: number;
  /** 含未成熟样本——看板必须能区分"没发"和"还没到期"。 */
  total_alerts: number;
  groups: RadarKpiSummaryGroup[];
}

export interface RadarDiagnostics {
  health: RadarHealth;
  scheduler: Record<string, unknown>;
  collectors: Record<string, unknown>;
  registry: { tokens: number; states: Record<string, number> };
  alerts: Record<string, unknown>;
  tracker: Record<string, unknown>;
  pipeline: Record<string, unknown>;
  email: Record<string, unknown>;
  metrics: Record<string, unknown>;
  events: Record<string, number>;
}

// ── 管理接口：运行时配置 ──────────────────────────────────────────────────

/** 参数控件类型，由后端注册表驱动，前端不硬编码任何参数。 */
export type AdminParamKind =
  | "int"
  | "float"
  | "bool"
  | "str"
  | "choice"
  | "num_list"
  | "str_list"
  | "json";

export interface AdminConfigParam {
  path: string;
  kind: AdminParamKind;
  label: string;
  desc: string;
  lo: number | null;
  hi: number | null;
  choices: Array<string | number> | null;
  ascending: boolean;
  unit: string;
  restart_required: boolean;
  /** 出厂默认值（config.yaml） */
  default: unknown;
  /** 已保存的生效值（默认值 + 覆盖层合并；重启前运行值可能与此不同） */
  value: unknown;
  /** 是否被覆盖层修改过 */
  overridden: boolean;
}

export interface AdminConfigGroup {
  id: string;
  label: string;
  desc: string;
  params: AdminConfigParam[];
}

export interface AdminConfigResponse {
  running: RadarHealth["version"];
  saved_config_hash: string;
  /** true = 已保存的配置与运行中的不同，需要重启生效 */
  restart_pending: boolean;
  override_count: number;
  groups: AdminConfigGroup[];
}

export interface AdminSaveResponse {
  saved: boolean;
  reason?: string;
  changed?: Record<string, { old: unknown; new: unknown }>;
  removed?: string[];
  saved_config_hash: string;
  restart_pending: boolean;
  restart_required?: boolean;
}
