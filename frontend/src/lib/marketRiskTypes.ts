export type RiskStage = "normal" | "watch" | "warning" | "critical" | "cooldown" | "resolved";
export type MarketRiskMode = "shadow" | "production_read_only" | "production_alerting";
export type RiskDirection = "up" | "down" | "mixed" | "unknown";
export type EvidencePillar =
  | "spot_demand"
  | "leveraged_positioning"
  | "liquidation_risk"
  | "liquidity_structure"
  | "market_response"
  | "context";

export interface SourceQuality {
  source_id: string;
  availability: "available" | "unavailable" | "missing";
  freshness: "fresh" | "stale" | "unknown";
  completeness: number;
  continuity: "continuous" | "gap" | "snapshot_only" | "unknown";
  validity: "valid" | "invalid" | "unknown";
  as_of: number;
  observed_at: number;
  watermark: number;
  decision_usable: boolean;
  reasons: string[];
}

export interface EvidenceItem {
  evidence_id: string;
  pillar: EvidencePillar;
  causal_root: string;
  name: string;
  direction: RiskDirection | "neutral";
  role: "scoring" | "informational" | "context";
  strength: number;
  raw_strength: number;
  confidence: number;
  event_time: number;
  observed_at: number;
  decision_time: number;
  source_id: string;
  values: Record<string, unknown>;
  explanation: string;
}

export interface PillarSnapshot {
  pillar: EvidencePillar;
  direction: RiskDirection | "neutral";
  confidence: number;
  causal_roots: string[];
  evidence_ids: string[];
  decision_usable: boolean;
  note: string;
}

export interface MarketIncidentSnapshot {
  product_name: string;
  coin: string;
  event_time: number;
  observed_at: number;
  decision_time: number;
  watermark: number;
  stage: RiskStage;
  quality_layer: "normal" | "data_degraded";
  direction: RiskDirection;
  live_direction: RiskDirection;
  incident_id: string | null;
  episode_id: string | null;
  stage_since: number;
  mode: MarketRiskMode;
  shadow_mode: boolean;
  stage_frozen: boolean;
  frozen_since: number;
  last_confirmed_at: number;
  valid_for_calibration: boolean;
  pit_violations: string[];
  research_signals: string[];
  causal_roots: string[];
  live_causal_roots: string[];
  independent_root_count: number;
  spot_confirmed: boolean;
  pillars: Record<string, PillarSnapshot>;
  evidence: EvidenceItem[];
  source_quality: Record<string, SourceQuality>;
  context: Record<string, unknown>;
  transition_reason: string;
  config_version: string;
  calibration_version: string;
  calibration_admitted: boolean;
  notification_eligible: boolean;
}

export interface MarketFactor {
  factor_id: string;
  label: string;
  direction: RiskDirection;
  status: "normal" | "unusual" | "extreme" | "missing" | "conflict";
  strength_band: "unavailable" | "weak" | "medium" | "strong";
  decision_role: "scoring" | "informational" | "blocked";
  source_ids: string[];
  as_of: number;
  decision_usable: boolean;
  plain_summary: string;
  values: Record<string, unknown>;
}

export interface ContextItem {
  availability?: "available" | "unavailable";
  source?: string;
  known_at?: number;
  published_at?: number;
  note?: string;
  reason?: string;
  [key: string]: unknown;
}

export interface MarketRiskContext {
  market_overview?: {
    trend_horizons?: Record<string, {
      availability: "available" | "unavailable";
      change_pct: number | null;
      as_of: number;
      closed: boolean;
      direction: RiskDirection;
    }>;
  };
  etf?: ContextItem;
  options?: ContextItem;
  native_btc_onchain?: ContextItem;
  stablecoin?: ContextItem;
  institutional_futures?: ContextItem;
  exchange_flows?: ContextItem;
  institutional_entities?: ContextItem;
  [key: string]: unknown;
}

export interface MarketRiskIntelligence {
  product_name: string;
  coin: string;
  mode: MarketRiskMode;
  decision_time: number;
  live_observation: {
    decision_time: number;
    direction: RiskDirection;
    quality_layer: "normal" | "data_degraded";
    spot_confirmed: boolean;
    independent_root_count: number;
    causal_roots: string[];
    summary: string;
  };
  confirmed_incident: {
    stage: RiskStage;
    direction: RiskDirection;
    confirmed_at: number;
    stage_since: number;
    frozen: boolean;
    frozen_since: number;
    frozen_age_sec: number;
    incident_id: string | null;
    episode_id: string | null;
  };
  decision_support: {
    stance: "observe_long" | "observe_short" | "wait";
    strength_band: "unavailable" | "weak" | "medium" | "strong";
    summary: string;
    supporting_evidence: string[];
    opposing_evidence: string[];
    blockers: string[];
    invalidation_conditions: string[];
    execution_eligible: false;
  };
  factors: MarketFactor[];
  context: MarketRiskContext;
  incident: MarketIncidentSnapshot;
}

export interface MarketRiskReady {
  ready_for_mode: MarketRiskMode;
  current_mode: MarketRiskMode;
  pit_violations_24h: number;
  valid_for_calibration_24h: number;
  snapshot_count_24h: number;
  core_coverage_24h: number;
  governed_shadow_age_sec: number;
  rss_observation_age_sec: number;
  rss_p95_gib: number;
  rss_slope_mib_per_hour: number;
  raw_queue_dropped: number;
  raw_store: {
    file_count?: number;
    projected_files_per_day?: number;
    total_bytes?: number;
    free_bytes?: number;
    free_inodes?: number;
    resource_admissible?: boolean;
  };
  blockers: string[];
}
