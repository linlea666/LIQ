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
  ladder_plan_text?: string;
  risk_warnings: string[];
  scenario_analysis: { label: string; description: string }[];
  raw_text: string;
  user_prompt?: string;
  signal_summary?: SignalSummary | null;
}

export interface RangeSignalData {
  ts: number;
  ma60_daily: number | null;
  ma120_daily: number | null;
  ma60_weekly: number | null;
  macd_daily_above_zero: boolean | null;
  macd_daily_histogram: number | null;
  macd_daily_hist_rising: boolean | null;
  range_upper: number | null;
  range_upper_source: string;
  range_lower: number | null;
  range_lower_source: string;
  price_position: string;
  price_position_pct: number;
  unfilled_wick_low: number | null;
  unfilled_wick_high: number | null;
  signal_grade: string | null;
  signal_direction: string | null;
  signal_reason: string;
  sweep_confirmed: boolean;
  cps_aligned: boolean;
}

export interface KeyLevel {
  price: number;
  side: "support" | "resistance";
  sources: string[];
  strength: number;
  state: "idle" | "approaching" | "testing" | "swept" | "bounced" | "broken" | "flipped";
  state_ts: number;
  prev_state: string;
  test_count: number;
  sweep_usd: number;
  cascade_risk: number;
  cascade_layers: number;
  cascade_total_usd: number;
  distance_pct: number;
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
  rr_ratio?: number;
  reason: string;
  warnings: string[];
}

export interface KeyLevelSnapshot {
  ts: number;
  levels: KeyLevel[];
  signals: KeyLevelSignal[];
  active_count: number;
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
// V2 关键位系统
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
  key_levels?: KeyLevelSnapshot;
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
