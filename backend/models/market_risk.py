"""LIQ BTC 联合风险预警系统公共契约。

市场结果不使用“确定性”措辞；可确定的只有事件时间、状态转换与回放输入。
新闻和 AI 不属于任何字段，也不得通过扩展字典注入评分证据。
"""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, model_validator


RiskStage = Literal["normal", "watch", "warning", "critical", "cooldown", "resolved"]
RiskDirection = Literal["up", "down", "mixed", "unknown"]
QualityLayer = Literal["normal", "data_degraded"]
EvidencePillar = Literal[
    "spot_demand",
    "leveraged_positioning",
    "liquidation_risk",
    "liquidity_structure",
    "market_response",
    "context",
]
EvidenceDirection = Literal["up", "down", "neutral", "unknown"]
EvidenceRole = Literal["scoring", "informational", "context"]


class SourceQuality(BaseModel):
    """数据质量与证据置信度正交；本模型绝不承载交易方向。"""

    source_id: str
    availability: Literal["available", "unavailable", "missing"] = "missing"
    freshness: Literal["fresh", "stale", "unknown"] = "unknown"
    completeness: float = 0.0
    continuity: Literal["continuous", "gap", "snapshot_only", "unknown"] = "unknown"
    validity: Literal["valid", "invalid", "unknown"] = "unknown"
    latency_ms: Optional[float] = None
    as_of: int = 0
    observed_at: int = 0
    watermark: int = 0
    decision_usable: bool = False
    reasons: list[str] = Field(default_factory=list)


class EvidenceItem(BaseModel):
    evidence_id: str
    coin: str
    pillar: EvidencePillar
    causal_root: str
    name: str
    direction: EvidenceDirection = "unknown"
    role: EvidenceRole = "scoring"
    strength: float = 0.0
    confidence: float = 0.0
    event_time: int
    observed_at: int
    decision_time: int
    watermark: int
    source_sequence: Optional[str] = None
    source_id: str
    config_version: str
    calibration_version: str
    values: dict[str, Any] = Field(default_factory=dict)
    explanation: str = ""


class PillarSnapshot(BaseModel):
    pillar: EvidencePillar
    direction: EvidenceDirection = "unknown"
    confidence: float = 0.0
    causal_roots: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    decision_usable: bool = False
    note: str = ""


class RealizedLiquidationFlow(BaseModel):
    coin: str
    window_sec: int
    long_executed_notional_usd: float = 0.0
    short_executed_notional_usd: float = 0.0
    executed_notional_usd: float = 0.0
    event_time: int
    observed_at: int
    source_id: str
    quality: SourceQuality


class EstimatedLiquidationDensity(BaseModel):
    coin: str
    direction: Literal["above", "below"]
    estimated_density_usd: float = 0.0
    nearest_price: Optional[float] = None
    event_time: int
    observed_at: int
    source_id: str
    quality: SourceQuality


class OnchainEntityEvent(BaseModel):
    event_id: str
    coin: str
    entity_id: str
    entity_label: str = ""
    event_type: Literal[
        "transfer_to_entity", "transfer_from_entity",
        "internal_rebalance", "unknown_counterparty",
    ]
    amount_base: float
    tx_id: str
    confirmations: int = 0
    label_source: str = ""
    label_confidence: float = 0.0
    label_valid_from: int = 0
    label_valid_to: Optional[int] = None
    known_at: int
    event_time: int
    observed_at: int
    decision_time: int
    reorged: bool = False
    source_id: str = ""

    @model_validator(mode="after")
    def _validate_point_in_time(self) -> "OnchainEntityEvent":
        if self.amount_base < 0:
            raise ValueError("amount_base cannot be negative")
        if self.event_time > self.observed_at:
            raise ValueError("event_time cannot be after observed_at")
        if self.observed_at > self.decision_time:
            raise ValueError("observed_at cannot be after decision_time")
        if self.known_at > self.decision_time:
            raise ValueError("label known_at cannot be after decision_time")
        return self


class MarketRiskTransition(BaseModel):
    transition_id: str
    coin: str
    incident_id: Optional[str] = None
    episode_id: Optional[str] = None
    from_stage: RiskStage
    to_stage: RiskStage
    direction: RiskDirection
    decision_time: int
    reason: str
    config_version: str
    calibration_version: str


class MarketRiskMachineContext(BaseModel):
    coin: str
    stage: RiskStage = "normal"
    direction: RiskDirection = "unknown"
    incident_id: Optional[str] = None
    episode_id: Optional[str] = None
    stage_since: int = 0
    incident_started_at: int = 0
    episode_started_at: int = 0
    last_qualifying_at: int = 0
    last_critical_at: int = 0
    resolved_at: int = 0


class MarketIncidentSnapshot(BaseModel):
    product_name: str = "LIQ BTC 联合风险预警系统"
    coin: str
    event_time: int
    observed_at: int
    decision_time: int
    watermark: int
    stage: RiskStage = "normal"
    quality_layer: QualityLayer = "normal"
    direction: RiskDirection = "unknown"
    incident_id: Optional[str] = None
    episode_id: Optional[str] = None
    stage_since: int = 0
    shadow_mode: bool = True
    research_signals: list[str] = Field(default_factory=list)
    causal_roots: list[str] = Field(default_factory=list)
    independent_root_count: int = 0
    spot_confirmed: bool = False
    pillars: dict[str, PillarSnapshot] = Field(default_factory=dict)
    evidence: list[EvidenceItem] = Field(default_factory=list)
    source_quality: dict[str, SourceQuality] = Field(default_factory=dict)
    realized_liquidation: Optional[RealizedLiquidationFlow] = None
    estimated_liquidation_density: list[EstimatedLiquidationDensity] = Field(default_factory=list)
    context: dict[str, Any] = Field(default_factory=dict)
    transition_reason: str = ""
    config_version: str
    calibration_version: str
    calibration_admitted: bool = False
    notification_eligible: bool = False


class CalibrationArtifact(BaseModel):
    calibration_version: str
    created_at: str
    status: str
    admitted_for_production: bool = False
    training_window: Optional[dict[str, Any]] = None
    notes: str = ""
    thresholds: dict[str, float]
    baseline_thresholds: dict[str, float] = Field(default_factory=dict)


class MarketRiskHealth(BaseModel):
    enabled: bool
    running: bool
    shadow_mode: bool
    config_version: str
    calibration_version: str = "unavailable"
    calibration_admitted: bool = False
    last_tick_at: int = 0
    last_error: str = ""
    latest_by_coin: dict[str, int] = Field(default_factory=dict)
    source_quality: dict[str, dict[str, SourceQuality]] = Field(default_factory=dict)
    raw_event_store: dict[str, Any] = Field(default_factory=dict)
    outbox: dict[str, int] = Field(default_factory=dict)


class GroundTruthEpisode(BaseModel):
    event_id: str
    coin: str
    direction: Literal["up", "down"]
    event_start: int
    onset: int
    threshold_time: int
    peak: int
    end: int
    mfe_pct: float
    mae_pct: float
    duration_sec: int


class MarketRiskMatch(BaseModel):
    incident_id: str
    event_id: str
    matched_once: bool = True
    lead_to_onset_sec: Optional[int] = None
    lead_to_threshold_sec: Optional[int] = None
