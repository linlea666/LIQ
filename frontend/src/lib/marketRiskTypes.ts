export type RiskStage = "normal" | "watch" | "warning" | "critical" | "cooldown" | "resolved";
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
  direction: "up" | "down" | "neutral" | "unknown";
  role: "scoring" | "informational" | "context";
  strength: number;
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
  direction: "up" | "down" | "neutral" | "unknown";
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
  direction: "up" | "down" | "mixed" | "unknown";
  incident_id: string | null;
  episode_id: string | null;
  stage_since: number;
  shadow_mode: boolean;
  research_signals: string[];
  causal_roots: string[];
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
