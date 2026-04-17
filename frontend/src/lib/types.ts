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
