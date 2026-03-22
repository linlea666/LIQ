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

export interface LevelAnalysis {
  coin: string;
  ts: number;
  current_price: number;
  supports: PriceLevel[];
  resistances: PriceLevel[];
  stop_loss_zones: StopLossZone[];
  entry_zones: EntryZone[];
  pin_risk_zones: { price: number; side: string; liq_amount_usd: number; note: string }[];
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

export interface AIAnalysisResult {
  coin: string;
  ts: number;
  price_at_analysis: number;
  market_overview: string;
  key_levels: { type: string; price: string; strength: string; reason: string }[];
  stop_loss_suggestion: { raw: string };
  entry_zones: { direction: string; raw: string; details: string[] }[];
  /** 第四节「狙击挂单计划」解析文本 */
  sniper_setup?: string;
  risk_warnings: string[];
  scenario_analysis: { label: string; description: string }[];
  raw_text: string;
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
}
