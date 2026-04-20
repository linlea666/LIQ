export interface TickerData {
  coin: string;
  ts: number;
  last: number;
  high_24h: number;
  low_24h: number;
  vol_24h: number;
  change_24h: number;
  change_pct_24h: number;
}

export interface FactorCard {
  id: string;
  name: string;
  value: string;
  direction: "bullish" | "bearish" | "neutral";
  sub_text: string;
  percentile: number;
  summary: string;
}

export interface MarketTemperature {
  coin: string;
  ts: number;
  score: number;
  label: string;
  pin_risk_level: string;
  pin_risk_label: string;
  factors: FactorCard[];
}

export interface WaterfallItem {
  factor_id: string;
  factor_name: string;
  contribution_pct: number;
  direction: "bullish" | "bearish";
}

export interface WaterfallData {
  coin: string;
  ts: number;
  items: WaterfallItem[];
  bullish_total: number;
  bearish_total: number;
  net_bias: number;
  net_label: string;
}

export interface PriceLevel {
  price: number;
  label: string;
  level_type: "support" | "resistance";
  strength: number;
  sources: string[];
  note: string;
}

export interface StopLossZone {
  direction: string;
  price: number;
  zone_from: number;
  zone_to: number;
  reasons: string[];
  atr_multiple: number;
}

export interface EntryZone {
  direction: string;
  price_from: number;
  price_to: number;
  confluence_sources: string[];
  confirmation_note: string;
}

export interface LadderEntry {
  tier: number;
  entry_price: number;
  stop_loss: number;
  take_profit: number;
  rr_ratio: number;
  position_weight: number;
  risk_pct: number;
  zone_label: string;
  entry_logic: string[];
  invalidation: string;
}

export interface LadderPlan {
  direction: string;
  tier_count: number;
  entries: LadderEntry[];
  total_risk_pct: number;
  best_case_rr: number;
  worst_case_loss_pct: number;
  expected_edge: string;
  plan_summary: string;
  coverage_range: string;
}

export interface LevelAnalysis {
  coin: string;
  ts: number;
  current_price: number;
  supports: PriceLevel[];
  resistances: PriceLevel[];
  stop_loss_zones: StopLossZone[];
  entry_zones: EntryZone[];
  pin_risk_zones: { price: number; side: string; liq_amount_usd: number; note: string }[];
  ladder_plans?: LadderPlan[];
}

export interface LiqBand {
  price_from: number;
  price_to: number;
  turnover_usd: number;
}

export interface LiqLeverageGroup {
  leverage: string;
  short_bands: LiqBand[];
  long_bands: LiqBand[];
  short_total_usd: number;
  long_total_usd: number;
}

export interface LiqCluster {
  price_center: number;
  price_from: number;
  price_to: number;
  total_usd: number;
  side: string;
  dominant_leverage: string;
  distance_pct: number;
}

export interface LiquidationMap {
  coin: string;
  ts: number;
  cycle: string;
  leverage_groups: LiqLeverageGroup[];
  clusters_above: LiqCluster[];
  clusters_below: LiqCluster[];
  vacuum_zones: { price_from: number; price_to: number; midpoint: number; note: string }[];
  imbalance_ratio: number;
}

export interface CVDPoint {
  ts: number;
  delta: number;
  cvd: number;
}

export interface OIData {
  coin: string;
  ts: number;
  current_usd: number;
  change_1h_pct: number;
  change_5m_pct: number;
  trend: string;
}

export interface FundingRateData {
  coin: string;
  ts: number;
  okx_rate: number | null;
  binance_rate: number | null;
  avg_rate: number;
  interpretation: string;
}

export interface BasisData {
  coin: string;
  ts: number;
  mark_price: number;
  index_price: number;
  basis_pct: number;
  interpretation: string;
}

export interface WallInfo {
  price: number;
  size: number;
  size_usd: number;
  order_count: number;
}

export interface OrderBookAnalysis {
  coin: string;
  ts: number;
  bid_walls: WallInfo[];
  ask_walls: WallInfo[];
  bid_total_usd: number;
  ask_total_usd: number;
  spread_pct: number;
}

export interface SourceHealth {
  name: string;
  status: "connected" | "degraded" | "disconnected";
  latency_ms: number;
}

export interface SignalSummary {
  direction: "bullish" | "bearish" | "neutral";
  confidence: "high" | "medium" | "low";
  reason: string;
}

export interface AIAnalysisResult {
  coin: string;
  ts: number;
  price_at_analysis: number;
  market_overview: string;
  key_levels: { type: string; price: string; strength: string; reason: string }[];
  stop_loss_suggestion: { raw: string };
  entry_zones: { direction: string; raw: string; details: string[] }[];
  sniper_setup?: string;
  sniper_plans?: {
    direction: string;
    entry: number | null;
    stop_loss: number | null;
    tp1: number | null;
    tp2: number | null;
    rr: number | null;
    logic: string;
    invalidation: string;
    raw_text: string;
  }[];
  ladder_plan_text?: string;
  trading_plan?: string;
  trading_plan_entries?: {
    tier: string;
    direction: string;
    entry: number | null;
    stop_loss: number | null;
    tp1: number | null;
    tp2: number | null;
    rr: number | null;
    source: string;
    logic: string;
  }[];
  risk_warnings: string[];
  scenario_analysis: { label: string; description: string }[];
  data_quality_feedback?: string;
  raw_text: string;
  user_prompt?: string;
  signal_summary?: SignalSummary | null;
}

export interface RangeSignalData {
  ts: number;
  range_upper: number | null;
  range_upper_source: string;
  range_upper_tier: string;
  range_upper_score: number;
  range_upper_test_count: number;
  range_lower: number | null;
  range_lower_source: string;
  range_lower_tier: string;
  range_lower_score: number;
  range_lower_test_count: number;
  micro_upper: number | null;
  micro_upper_source: string;
  micro_upper_tier: string;
  micro_lower: number | null;
  micro_lower_source: string;
  micro_lower_tier: string;
  micro_width_pct: number;
  price_position: string;
  price_position_pct: number;
  box_state: string;
  box_state_ts: number;
  box_age_hours: number;
  box_width_pct: number;
  box_quality: number;
  breakout_probability: number;
  breakout_direction_bias: string;
  breakout_reason: string;
  ma60_daily: number | null;
  ma120_daily: number | null;
  ma60_weekly: number | null;
  macd_daily_above_zero: boolean | null;
  macd_daily_histogram: number | null;
  macd_daily_hist_rising: boolean | null;
  unfilled_wick_low: number | null;
  unfilled_wick_high: number | null;
  signal_grade: string | null;
  signal_direction: string | null;
  signal_reason: string;
  signal_entry: number | null;
  signal_stop_loss: number | null;
  signal_tp1: number | null;
  signal_rr_ratio: number | null;
  sweep_confirmed: boolean;
  cps_aligned: boolean;
  bb_squeeze: boolean;
  oi_buildup: boolean;
  volume_declining: boolean;
  funding_extreme: boolean;
  orderbook_imbalance: string;
  confluence_count: number;

  // Commit 3: 市场结构对齐度（与 1h MarketStructure 的关系）
  ms_direction?: "bullish" | "bearish" | "ranging" | "transitioning" | null;
  ms_event?: "BOS_up" | "BOS_down" | "CHoCH_up" | "CHoCH_down" | null;
  ms_bias?: "long_only" | "short_only" | "both_ok" | "stand_aside" | null;
  ms_confidence?: number;
  ms_alignment?: "aligned" | "conflict" | "neutral" | "unknown" | "";
}

export interface KeyLevelSignal {
  level_price: number;
  side: string;
  state: string;
  action: string;
  confidence: "A" | "B" | "C";
  entry_price?: number;
  stop_loss?: number;
  tp1?: number;
  tp2?: number;
  rr_ratio?: number;
  reason: string;
  warnings: string[];
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
// V2 关键位系统（V1 KeyLevelSnapshot 已于 Commit 2 下线，payload 不再产出）
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

export interface KeyLevelV2 {
  price: number;
  side: "support" | "resistance";
  category: string;
  sources: string[];
  source_count: number;
  confluence_score: number;
  strength_tier: "S" | "A" | "B" | "C";
  state: "idle" | "approaching" | "testing" | "swept" | "bounced" | "broken" | "flipped";
  state_ts: number;
  prev_state: string;
  test_count: number;
  sweep_usd: number;
  lowest_wick?: number;
  break_start_ts: number;
  cascade_risk: number;
  cascade_layers: number;
  cascade_total_usd: number;
  distance_pct: number;
  timeframe: string;
  first_seen_ts: number;
  last_confirmed_ts: number;
  note: string;
  pattern_detected?: string;
  pattern_strength?: number;
  // Phase 2: 历史验证 + 屏障 + 最终打分
  bounce_count?: number;
  historical_validity?: number;
  barrier_score?: number;
  final_score?: number;

  // Commit 4: 质量标注（主动吸筹 / 被动触发 · 三步确认）
  bounce_quality?: "proactive" | "passive" | "";
  breakout_stage?: 0 | 1 | 2 | 3;
}

export interface BullBearLine {
  sma200d: number | null;
  bmsa_upper: number | null;
  bmsa_lower: number | null;
  ichimoku_cloud_top: number | null;
  ichimoku_cloud_bottom: number | null;
  current_regime: "bull" | "bear" | "neutral" | "";
  regime_reason: string;
}

export interface BreakoutZone {
  bb_squeeze: boolean;
  squeeze_direction: "up" | "down" | "unknown" | "";
  bb_upper: number | null;
  bb_lower: number | null;
  keltner_upper: number | null;
  keltner_lower: number | null;
  note: string;
}

export interface FibSnapshotLevel {
  ratio: number;
  price: number;
  label: string;
}

export interface FibSnapshot {
  swing_high: number;
  swing_low: number;
  direction: string;
  levels: FibSnapshotLevel[];
}

export interface KeyLevelSnapshotV2 {
  ts: number;
  current_price: number;
  atr: number;
  levels: KeyLevelV2[];
  bull_bear_line: BullBearLine | null;
  breakout_zone: BreakoutZone | null;
  fib_snapshot: FibSnapshot | null;
  signals: KeyLevelSignal[];
  active_count: number;
  structure_summary: string;
  nearest_strong_support: number | null;
  nearest_strong_resistance: number | null;
  daily_strong_support: string | null;
  daily_strong_resistance: string | null;
  weekly_strong_support: string | null;
  weekly_strong_resistance: string | null;
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
// L4 数学引擎输出：ExecutionPlan（对应后端 models/execution_plan.py）
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

export type TrafficLight = "green" | "yellow" | "orange" | "red" | "gray";
export type SafetyGateStatus = "pass" | "warn" | "block";
export type MarketRegimeLabel =
  | "trend_up"
  | "trend_down"
  | "range"
  | "squeeze"
  | "high_vol_chop"
  | "extreme";
export type TradingAction = "long" | "short" | "wait" | "avoid";
export type SignalDirection = "bullish" | "bearish" | "neutral" | "potential_reversal";

export interface ExecutionScoreBreakdown {
  base: number;
  tier_bonus: number;
  confidence_bonus: number;
  regime_alignment: number;
  corroboration_bonus: number;
  cascade_penalty: number;
  rr_bonus: number;
  backtest_bonus: number;
  event_factor: number;
  geo_risk_factor: number;
  safety_gate_delta: number;
  final_score: number;
}

export interface SafetyGateResultUI {
  g1_extreme_vol: SafetyGateStatus;
  g2_macro_event: SafetyGateStatus;
  g3_liq_chaos: SafetyGateStatus;
  g4_api_degrade: SafetyGateStatus;
  g5_blackswan: SafetyGateStatus;
  triggered: boolean;
  block_reason: string;
  warnings: string[];
}

export interface ExecutionPlan {
  coin: string;
  ts: number;
  current_price: number;
  regime: MarketRegimeLabel;
  regime_confidence: number;
  execution_score: number;
  traffic_light: TrafficLight;
  headline: string;
  one_liner: string;
  action: TradingAction;
  direction: SignalDirection;
  tier_hint: "S" | "A" | "B" | "C";
  entry_zone_low: number | null;
  entry_zone_high: number | null;
  stop_loss: number | null;
  tp1: number | null;
  tp2: number | null;
  rr_ratio: number | null;
  position_size_pct: number | null;
  expires_at: number | null;
  breakdown: ExecutionScoreBreakdown;
  safety_gates: SafetyGateResultUI;
  corroborating_sources: string[];
  historical_win_rate: number | null;
  historical_sample_size: number;
}

export interface ExecutionPlanResponse {
  ready: boolean;
  coin?: string;
  plan?: ExecutionPlan;
}

export interface MarketUpdate {
  coin: string;
  ts: number;
  ticker?: TickerData;
  temperature?: MarketTemperature;
  waterfall?: WaterfallData;
  levels?: LevelAnalysis;
  cvd_contract?: {
    trend: string;
    delta_1h: number;
    has_divergence: boolean;
    last_points: CVDPoint[];
  };
  oi?: OIData;
  funding?: FundingRateData;
  basis?: BasisData;
  orderbook?: OrderBookAnalysis;
  multi_funding?: Record<string, unknown>;
  ls_ratio?: Record<string, unknown>;
  ls_ratio_top_account?: Record<string, unknown>;
  ls_ratio_top_position?: Record<string, unknown>;
  etf_flow?: Record<string, unknown>;
  global_liq?: Record<string, unknown>;
  market_index?: Record<string, unknown>;
  sniper_entries?: Record<string, unknown>[];
  ladder_plans?: LadderPlan[];
  range_signal?: RangeSignalData;
  key_levels_v2?: KeyLevelSnapshotV2;
  option_max_pain?: Record<string, unknown>;
  option_info?: Record<string, unknown>;
  large_orders?: Record<string, unknown>;
  whale_data?: Record<string, unknown>;
  liq_max_pain?: Record<string, unknown>;
  liq_heatmaps?: Record<string, Record<string, unknown>>;
  rsi_14?: number;
  macd?: Record<string, unknown>;
  boll?: Record<string, unknown>;
  news_count?: number;
}


// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
// P1.3 · L7 AITraderReport（AI 引擎独立交易员产出）
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

export type Resonance = "low" | "medium" | "high";
export type AgreementWithMath = "agree" | "caution" | "disagree";

export interface AIFactorRow {
  dimension: string;
  value_display: string;
  value_raw?: number | null;
  signal: string;
  direction: SignalDirection;
  resonance: Resonance;
  data_source_ref: string;
  link_anchor: string;
  confidence: number;
}

export interface AIFactorSection {
  section_id: "A" | "B" | "C" | "D" | "E" | "F" | "G";
  section_name_cn: string;
  section_emoji: string;
  rows: AIFactorRow[];
  section_summary: string;
  section_bias: SignalDirection;
}

export interface AIFactorMatrix {
  sections: AIFactorSection[];
  summary_line: string;
  overall_bias: SignalDirection;
  overall_confidence: "high" | "medium" | "low";
  analysis_ts: number;
  price_at_analysis: number;
}

export interface AIKeyLevelInterpretation {
  primary_support_price: number | null;
  primary_support_reason: string;
  primary_resistance_price: number | null;
  primary_resistance_reason: string;
  trap_warning: string;
  extra_levels: Array<{ price?: number; side?: string; reason?: string }>;
}

export interface AITradingPlan {
  priority: number;
  direction: TradingAction;
  entry_zone_low: number | null;
  entry_zone_high: number | null;
  trigger_condition: string;
  stop_loss: number | null;
  tp1: number | null;
  tp2: number | null;
  rr_ratio: number | null;
  position_suggestion_pct: number;
  conviction: number;
  tier_hint: "S" | "A" | "B" | "C";
  invalidation: string;
  related_narratives: string[];
  reason: string;
  aligned_with_math_engine: boolean;
  alignment_note: string;
}

export interface AINarrativeImpact {
  theme_id: string;
  theme_name_cn: string;
  ai_view_cn: string;
  weight_on_current_plan: Resonance;
}

export interface AITraderReport {
  coin: string;
  ts: number;
  price_at_analysis: number;
  model: string;
  thinking_tokens: number;
  latency_ms: number;
  market_view_cn: string;
  bias: SignalDirection;
  conviction: number;
  key_level_interpretation: AIKeyLevelInterpretation;
  trading_plans: AITradingPlan[];
  narrative_impact: AINarrativeImpact[];
  news_impact_summary_cn: string;
  geo_risk_assessment_cn: string;
  agreement_with_math_engine: AgreementWithMath;
  agreement_notes_cn: string;
  key_risks: string[];
  factor_matrix: AIFactorMatrix | null;
  scenario_analysis: Array<Record<string, unknown>>;
  raw_text: string;
  user_prompt: string;
}

export interface AITraderReportResponse {
  ready: boolean;
  coin?: string;
  report?: AITraderReport;
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
// P1.3 · L7.5 FinalDecision（双引擎融合层对外主视图）
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

export type ConsensusLevel =
  | "strong"
  | "agree"
  | "math_lead"
  | "ai_lead"
  | "conflict"
  | "both_wait";

export type RecommendedAction = "execute" | "reduce_size" | "wait" | "avoid";

export interface EngineBrief {
  engine_name: "math" | "ai";
  score: number;
  bias: SignalDirection;
  action: TradingAction;
  entry_hint: number | null;
  stop_loss_hint: number | null;
  tp1_hint: number | null;
  rr_ratio: number | null;
  position_pct: number;
  summary_cn: string;
}

export interface DivergenceStats {
  divergence_type: string;
  sample_size: number;
  math_win_rate: number;
  ai_win_rate: number;
  avg_delta_pct_24h: number;
  winner_hint_cn: string;
}

export interface FinalDecision {
  coin: string;
  ts: number;
  current_price: number;
  final_score: number;
  traffic_light: TrafficLight;
  headline: string;
  one_liner: string;
  consensus_level: ConsensusLevel;
  consensus_stars: number;
  consensus_summary_cn: string;
  math_brief: EngineBrief;
  ai_brief: EngineBrief;
  divergence_summary_cn: string;
  historical_divergence: DivergenceStats | null;
  recommended_action: RecommendedAction;
  recommended_position_pct: number;
  entry_zone_low: number | null;
  entry_zone_high: number | null;
  stop_loss: number | null;
  tp1: number | null;
  tp2: number | null;
  rr_ratio: number | null;
  underlying_math_plan_ref: string;
  underlying_ai_plan_priority: number;
  active_themes_count: number;
  geo_risk_overall_level: number;
  geo_risk_label: string;
  has_blackswan_warning: boolean;
  safety_gate_triggered: boolean;
  safety_gate_reason: string;
  expires_at: number | null;
}

export interface FinalDecisionResponse {
  ready: boolean;
  coin?: string;
  decision?: FinalDecision;
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
// AI 详情页专用：AIAnalysisResult + L7 trader_report + L7.5 final_decision + 新闻简报
// 由 /api/ai/detail/{coin}/{ts} 返回，字段非破坏性追加
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

export interface AIDetailNewsBrief {
  text: string;
  version: number;
  trigger: string;
  updated_at: number;
  geo_overview: Record<string, unknown> | null;
  active_narratives: Array<Record<string, unknown>>;
}

export interface AIDetailResponse extends AIAnalysisResult {
  ai_trader_report: AITraderReport | null;
  final_decision: FinalDecision | null;
  execution_plan: Record<string, unknown> | null;
  news_brief: AIDetailNewsBrief | null;
  _extras_source: "live" | "archive" | "none";
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
// D09 · 滚动新闻简报（供 /news-brief 页面人工审计 AI 记忆锚）
// 由 GET /api/news-brief/current 返回
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

export interface NewsBriefSection {
  section_id: "macro" | "regulatory" | "onchain" | "risk";
  section_title_cn: string;
  bullets: string[];
  max_bullets: number;
  last_rewritten_ts: number;
}

export interface NewsBriefTrackedTheme {
  theme_id: string;
  theme_name_cn: string;
  current_stance_cn: string;
  flip_flop_count_24h: number;
  latest_update_ts: number;
  relevance_score: number;
}

export interface NewsBriefFull {
  version: number;
  updated_at: number;
  ts_range_start: number;
  ts_range_end: number;
  coverage_hours: number;
  sections: NewsBriefSection[];
  tldr_cn: string;
  tracked_themes: NewsBriefTrackedTheme[];
  char_count: number;
  token_estimate: number;
  update_trigger: "scheduled" | "blackswan" | "user";
  based_on_events_count: number;
  model_used: string;
  generation_cost_ms: number;
  diff_from_prev_version: string;
  prev_version_updated_at: number | null;
}

export type NewsBriefUIStatus =
  | "ok"
  | "circuit_break"
  | "ai_failed"
  | "unexpected_empty"
  | "warming_up"
  | "bootstrap";

export interface NewsBriefCurrentResponse {
  ready: boolean;
  status: NewsBriefUIStatus;
  reason?: string;
  brief?: NewsBriefFull;
}

export interface NewsBriefHistoryResponse {
  ready: boolean;
  count: number;
  items: NewsBriefFull[];
}


// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
// P1.6 · DecisionTracker · D1-D17 全景灯
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

export type DecisionStatus =
  | "pending"
  | "in_progress"
  | "ok"
  | "warn"
  | "failed"
  | "skipped";

export type OverallHealth =
  | "all_ok"
  | "partial"
  | "degraded"
  | "unhealthy";

export interface DecisionRecord {
  id: string;
  title: string;
  owner_module: string;
  success_criteria: string;
  status: DecisionStatus;
  detail: string;
  metrics: Record<string, unknown>;
  last_update_ts: number;
  last_ok_ts: number;
  last_warn_ts: number;
  last_fail_ts: number;
  total_marks: number;
  ok_count: number;
  warn_count: number;
  fail_count: number;
}

export interface DecisionSummary {
  ts: number;
  decisions: DecisionRecord[];
  overall_health: OverallHealth;
}


// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
// P1.5 · 分歧回测样本（原始轨迹 + 聚合统计）
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

export type DivergenceOutcome = "pending" | "resolved" | "expired";

export interface DivergenceSampleRaw {
  sample_id: string;
  coin: string;
  divergence_type: string;
  math_action: string;
  math_bias: string;
  ai_action: string;
  ai_bias: string;
  price_at_record: number;
  created_ts: number;

  price_1h?: number | null;
  price_2h?: number | null;
  price_24h?: number | null;
  ts_1h?: number | null;
  ts_2h?: number | null;
  ts_24h?: number | null;

  resolved_ts?: number | null;
  delta_pct_1h?: number | null;
  delta_pct_2h?: number | null;
  delta_pct_24h?: number | null;
  math_win?: boolean | null;
  ai_win?: boolean | null;
  outcome: DivergenceOutcome;
}

export interface DivergenceStatsResponse {
  ready: boolean;
  coin: string;
  stats: DivergenceStats[];
  total_samples: number;
  recent_samples: DivergenceSampleRaw[];
  error?: string;
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
// P1.8a · AI Quality Ledger
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

export type AIMatrixSource =
  | "ai_json"
  | "rule_fallback"
  | "internal_conflict";

export type AIPlansSource =
  | "ai_json"
  | "markdown"
  | "sniper_fallback"
  | "wait_placeholder";

export type AIBiasVsText =
  | "consistent"
  | "conflict"
  | "text_missing"
  | "json_missing"
  | "unknown";

export type AIMathAgreement = "agree" | "caution" | "disagree" | "no_math_plan";

export interface AIQualityRecord {
  ts: number;
  coin: string;
  price_at_analysis: number;

  matrix_source: AIMatrixSource;
  plans_source: AIPlansSource;
  json_valid: boolean;
  json_invalid_reason: string;

  overlay_fields: number;
  ai_plans_count: number;
  ai_extra_rows: number;
  bias_vs_text: AIBiasVsText;

  final_bias: string;
  final_conviction: number;
  math_agreement: AIMathAgreement;

  latency_ms: number;
  reasoning_tokens: number;
  model: string;

  notes?: string[];
}

export interface AIQualityStats {
  coin: string;
  sample_size: number;
  window: number;

  ai_json_hit_rate: number;
  ai_plans_hit_rate: number;
  bias_consistency_rate: number;
  internal_conflict_rate: number;
  math_agreement_rate: number;

  avg_latency_ms: number;
  avg_reasoning_tokens: number;
  avg_overlay_fields: number;
  avg_ai_plans: number;

  first_ts: number;
  last_ts: number;

  trend_hint_cn: string;
  top_invalid_reasons?: Array<{ reason: string; count: number }>;
}

export interface AIQualityResponse {
  ready: boolean;
  coin: string;
  stats: AIQualityStats;
  recent: AIQualityRecord[];
  error?: string;
}

// ── P2.1 · Signal PnL 回放 ──────────────────────────

export type PlanPnLOrigin = "math" | "ai" | "final" | "all";
export type PlanPnLOutcome =
  | "pending"
  | "entry_filled"
  | "tp1_hit"
  | "tp2_hit"
  | "sl_hit"
  | "expired"
  | "invalidated";

export interface PlanPnLSample {
  sample_id: string;
  coin: string;
  origin: "math" | "ai" | "final";
  priority: number;
  action: "long" | "short" | "wait";
  tier: string;
  regime: string;
  entry_low: number;
  entry_high: number | null;
  stop_loss: number;
  tp1: number | null;
  tp2: number | null;
  rr_ratio: number | null;
  position_pct: number;
  created_ts: number;
  price_at_create: number;
  entry_filled_ts: number | null;
  entry_filled_price: number | null;
  outcome: PlanPnLOutcome;
  outcome_ts: number | null;
  outcome_price: number | null;
  r_multiple: number | null;
  max_favorable_r: number;
  max_adverse_r: number;
  invalidation_reason?: string;
}

export interface SignalPnLStats {
  coin: string;
  origin: string;
  tier: string;
  sample_size: number;
  window: number;
  win_rate: number;
  avg_r: number;
  expectancy_r: number;
  tp_hits: number;
  sl_hits: number;
  expired: number;
  invalidated: number;
  avg_mfe_r: number;
  avg_mae_r: number;
  entry_fill_rate: number;
  trend_hint_cn: string;
}

export interface SignalPnLResponse {
  ready: boolean;
  coin: string;
  stats: SignalPnLStats;
  origin_breakdown: SignalPnLStats[];
  tier_breakdown: SignalPnLStats[];
  recent: PlanPnLSample[];
  error?: string;
}

// ── P2.2 · Decision Health ─────────────────────────

export type HealthOverall = "ok" | "warn" | "fail" | "pending";
export type HealthEventKind =
  | "degrade"
  | "recover"
  | "escalate"
  | "de-escalate"
  | "change";

export interface HealthEvent {
  ts: number;
  id: string;
  title: string;
  from: string;
  to: string;
  kind: HealthEventKind;
  detail: string;
  metrics: Record<string, unknown>;
}

export interface HealthDegradedItem {
  id: string;
  title: string;
  owner_module: string;
  status: "warn" | "failed";
  detail: string;
  stuck_sec: number;
  metrics: Record<string, unknown>;
  last_update_ts: number;
}

export interface HealthSummaryResponse {
  ts: number;
  overall: HealthOverall;
  counts: { green: number; yellow: number; red: number; pending: number };
  ok_ids: string[];
  warn_ids: string[];
  fail_ids: string[];
  pending_ids: string[];
  degraded: HealthDegradedItem[];
  events: HealthEvent[];
}

// ── P2.4 · 历史快照回放 ──────────────────────────

export interface ReplayListItem {
  ts: number;
  coin: string;
  price_at_capture: number;
  ai_analysis_brief: string;
  has_plan: boolean;
  has_ai_report: boolean;
  has_final: boolean;
}

export interface ReplayFrame {
  ts: number;
  coin: string;
  price_at_capture: number;
  ai_analysis_brief: string;
  snapshot: Record<string, unknown>;
  execution_plan: Record<string, unknown> | null;
  ai_trader_report: Record<string, unknown> | null;
  final_decision: Record<string, unknown> | null;
}

export interface ReplayListResponse {
  ready: boolean;
  count: number;
  items: ReplayListItem[];
  error?: string;
}

export interface ReplayFrameResponse {
  ready: boolean;
  frame: ReplayFrame;
  error?: string;
}
