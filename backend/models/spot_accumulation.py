"""BTC 现货动态抄底模块的强类型契约。

规则层只产出建议和预算预留；真实持仓仅由手工成交账本改变。
"""

from __future__ import annotations

from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator


BudgetBucket = Literal["core", "swing", "tail"]
LedgerSide = Literal["buy", "sell"]
OpportunityStatus = Literal[
    "observing", "eligible", "accepted", "skipped", "expired", "invalidated", "filled",
]
OpportunityStage = Literal[
    "insurance", "value_1", "deep_value", "capitulation", "bottom_confirmed",
    "tail_extreme", "tail_catch_up", "swing",
]
MetricFreshness = Literal["fresh", "stale", "missing", "invalid"]
MetricParseStatus = Literal[
    "ok", "missing", "empty", "invalid_type", "missing_field", "invalid_timestamp",
    "non_finite", "request_error",
]


class SpotAccumulationConfig(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)

    schema_version: int = 3
    policy_version: int = Field(default=1, ge=1)
    coin: str = "BTC"
    initial_capital_usdt: float = 20_000.0
    core_ratio: float = 0.65
    swing_ratio: float = 0.20
    tail_ratio: float = 0.15
    insurance_ratio: float = 0.05
    core_stage_ratios: dict[str, float] = Field(default_factory=lambda: {
        "insurance": 0.05,
        "value_1": 0.10,
        "deep_value": 0.15,
        "capitulation": 0.15,
        "bottom_confirmed": 0.20,
    })
    core_thresholds: dict[str, dict[str, float]] = Field(default_factory=lambda: {
        "insurance": {"v": 55.0, "m": 40.0, "a": 65.0},
        "value_1": {"v": 65.0, "m": 45.0, "a": 60.0},
        "deep_value": {"v": 75.0, "m": 45.0, "a": 60.0},
        "capitulation": {"v": 80.0, "m": 0.0, "a": 65.0},
        "bottom_confirmed": {"v": 60.0, "m": 65.0, "a": 75.0},
    })
    tail_extreme_v: float = 90.0
    tail_extreme_a: float = 65.0
    tail_catch_up_v: float = 60.0
    tail_catch_up_m: float = 65.0
    tail_catch_up_a: float = 75.0
    min_price_gap_ratio: float = 0.05
    atr_gap_multiplier: float = 1.5
    acceptance_grace_seconds: int = 900
    weekly_reclaim_weeks: int = 2
    max_swing_loss_ratio: float = 0.01
    min_swing_rr: float = 2.0
    cycle_ath_override: Optional[float] = None
    email_notifications: bool = False
    ai_explanation_enabled: bool = True

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_absolute_budgets(cls, value: Any) -> Any:
        """兼容v1绝对金额配置，持久化后统一转为比例模型。"""
        if not isinstance(value, dict):
            return value
        migrated = dict(value)
        migrated.pop("version", None)
        migrated["schema_version"] = 3
        capital = float(migrated.get("initial_capital_usdt") or 0)
        legacy = (
            "core_budget_usdt" in migrated
            or "swing_budget_usdt" in migrated
            or "tail_budget_usdt" in migrated
        )
        if capital > 0 and legacy:
            migrated.setdefault("core_ratio", float(migrated.get("core_budget_usdt", 0)) / capital)
            migrated.setdefault("swing_ratio", float(migrated.get("swing_budget_usdt", 0)) / capital)
            migrated.setdefault("tail_ratio", float(migrated.get("tail_budget_usdt", 0)) / capital)
            if "insurance_cap_usdt" in migrated:
                migrated.setdefault("insurance_ratio", float(migrated["insurance_cap_usdt"]) / capital)
            if "max_swing_loss_usdt" in migrated:
                migrated.setdefault(
                    "max_swing_loss_ratio",
                    float(migrated["max_swing_loss_usdt"]) / capital,
                )
        if "core_stage_ratios" not in migrated:
            insurance = float(migrated.get("insurance_ratio", 0.05))
            remaining = max(0.0, float(migrated.get("core_ratio", 0.65)) - insurance)
            migrated["core_stage_ratios"] = {
                "insurance": insurance,
                "value_1": remaining * (10 / 60),
                "deep_value": remaining * (15 / 60),
                "capitulation": remaining * (15 / 60),
                "bottom_confirmed": remaining * (20 / 60),
            }
        else:
            ratios = migrated.get("core_stage_ratios")
            if isinstance(ratios, dict) and "insurance" in ratios:
                migrated["insurance_ratio"] = ratios["insurance"]
        return migrated

    @model_validator(mode="after")
    def validate_budgets(self) -> "SpotAccumulationConfig":
        if self.coin.upper() != "BTC":
            raise ValueError("首版仅支持 BTC")
        if self.initial_capital_usdt <= 0:
            raise ValueError("抄底总资金必须大于0")
        if min(self.core_ratio, self.swing_ratio, self.tail_ratio) < 0:
            raise ValueError("预算比例不能为负数")
        if abs(self.core_ratio + self.swing_ratio + self.tail_ratio - 1.0) > 1e-9:
            raise ValueError("core_ratio+swing_ratio+tail_ratio 必须等于1")
        if self.insurance_ratio < 0 or self.insurance_ratio > self.core_ratio:
            raise ValueError("踏空保险不能超过核心预算")
        if not 0 < self.max_swing_loss_ratio <= self.swing_ratio:
            raise ValueError("波段单笔风险比例必须大于0且不超过波段预算比例")
        stages = {"insurance", "value_1", "deep_value", "capitulation", "bottom_confirmed"}
        if set(self.core_stage_ratios) != stages:
            raise ValueError("core_stage_ratios 必须包含五个核心档位且不能有未知档位")
        if any(value < 0 for value in self.core_stage_ratios.values()):
            raise ValueError("核心档位比例不能为负数")
        if abs(sum(self.core_stage_ratios.values()) - self.core_ratio) > 1e-9:
            raise ValueError("五个核心档位比例之和必须等于core_ratio")
        if abs(self.insurance_ratio - self.core_stage_ratios["insurance"]) > 1e-9:
            raise ValueError("insurance_ratio必须与insurance档位比例一致")
        if set(self.core_thresholds) != stages:
            raise ValueError("core_thresholds 必须包含五个核心档位")
        for stage, thresholds in self.core_thresholds.items():
            if set(thresholds) != {"v", "m", "a"}:
                raise ValueError(f"{stage}阈值必须包含v/m/a")
            if any(value < 0 or value > 100 for value in thresholds.values()):
                raise ValueError(f"{stage}阈值必须在0-100")
        for value in (
            self.tail_extreme_v, self.tail_extreme_a, self.tail_catch_up_v,
            self.tail_catch_up_m, self.tail_catch_up_a,
        ):
            if value < 0 or value > 100:
                raise ValueError("尾部阈值必须在0-100")
        if not 0 <= self.min_price_gap_ratio <= 1:
            raise ValueError("min_price_gap_ratio必须在0-1")
        if self.atr_gap_multiplier < 0:
            raise ValueError("atr_gap_multiplier不能为负")
        if not 60 <= self.acceptance_grace_seconds <= 86_400:
            raise ValueError("acceptance_grace_seconds必须在60-86400秒")
        if not 1 <= self.weekly_reclaim_weeks <= 8:
            raise ValueError("weekly_reclaim_weeks必须在1-8")
        return self

    @property
    def version(self) -> int:
        """兼容旧调用方；新代码应区分schema_version与policy_version。"""
        return self.schema_version

    @property
    def core_budget_usdt(self) -> float:
        return round(self.initial_capital_usdt * self.core_ratio, 8)

    @property
    def swing_budget_usdt(self) -> float:
        return round(self.initial_capital_usdt * self.swing_ratio, 8)

    @property
    def tail_budget_usdt(self) -> float:
        return round(self.initial_capital_usdt * self.tail_ratio, 8)

    @property
    def insurance_cap_usdt(self) -> float:
        return round(self.initial_capital_usdt * self.insurance_ratio, 8)

    @property
    def max_swing_loss_usdt(self) -> float:
        return round(self.initial_capital_usdt * self.max_swing_loss_ratio, 8)

    @property
    def tail_tranche_usdt(self) -> float:
        return round(self.tail_budget_usdt / 3.0, 8)

    def core_stage_allocations(self) -> dict[str, float]:
        return {
            stage: round(self.initial_capital_usdt * ratio, 8)
            for stage, ratio in self.core_stage_ratios.items()
        }

    def public_dump(self) -> dict[str, Any]:
        """API返回比例配置及当前总资金派生出的明确金额。"""
        payload = self.model_dump(mode="json")
        payload["version"] = self.schema_version
        payload.update({
            "core_budget_usdt": self.core_budget_usdt,
            "swing_budget_usdt": self.swing_budget_usdt,
            "tail_budget_usdt": self.tail_budget_usdt,
            "insurance_cap_usdt": self.insurance_cap_usdt,
            "max_swing_loss_usdt": self.max_swing_loss_usdt,
            "tail_tranche_usdt": self.tail_tranche_usdt,
            "core_stage_allocations_usdt": self.core_stage_allocations(),
        })
        return payload


class SpotMetricFact(BaseModel):
    """单个可审计指标；过期值可展示但不得参与评分。"""

    value: Union[float, bool, str, None] = None
    source_timestamp: int = Field(default=0, ge=0)
    freshness: MetricFreshness = "missing"
    parse_status: MetricParseStatus = "missing"
    included_in_score: bool = False
    score: Optional[float] = Field(default=None, ge=0.0, le=100.0)
    source: str = ""


class SpotLayerQuality(BaseModel):
    fresh_count: int = Field(default=0, ge=0)
    total_count: int = Field(default=0, ge=0)
    required_count: int = Field(default=0, ge=0)
    required_metrics: list[str] = Field(default_factory=list)
    blocking_reasons: list[str] = Field(default_factory=list)
    passed: bool = False


class SpotDataQuality(BaseModel):
    completeness: float = Field(0.0, ge=0.0, le=1.0)
    stale_sources: list[str] = Field(default_factory=list)
    missing_sources: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    can_open_new_opportunity: bool = False
    layer_quality: dict[str, SpotLayerQuality] = Field(default_factory=dict)


class EvidenceScore(BaseModel):
    valuation: float = Field(0.0, ge=0.0, le=100.0)
    capital_flow: float = Field(0.0, ge=0.0, le=100.0)
    acceptance: float = Field(0.0, ge=0.0, le=100.0)


class SpotAccumulationFacts(BaseModel):
    coin: str = "BTC"
    timestamp: int
    price: float = Field(gt=0)
    cycle_ath: float = Field(gt=0)
    drawdown_pct: float = Field(ge=0.0)
    valuation_inputs: dict[str, Optional[float]] = Field(default_factory=dict)
    capital_inputs: dict[str, Optional[float]] = Field(default_factory=dict)
    acceptance_inputs: dict[str, Union[float, bool, str, None]] = Field(default_factory=dict)
    source_timestamps: dict[str, int] = Field(default_factory=dict)
    metric_facts: dict[str, SpotMetricFact] = Field(default_factory=dict)
    data_quality: SpotDataQuality = Field(default_factory=SpotDataQuality)
    scores: EvidenceScore = Field(default_factory=EvidenceScore)
    hard_vetoes: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)


class SpotOpportunity(BaseModel):
    opportunity_id: str
    coin: str = "BTC"
    stage: OpportunityStage
    bucket: BudgetBucket
    allocation_usdt: float = Field(gt=0)
    reserved_usdt: float = Field(default=0.0, ge=0.0)
    filled_usdt: float = Field(default=0.0, ge=0.0)
    status: OpportunityStatus = "observing"
    price_zone_low: float = Field(gt=0)
    price_zone_high: float = Field(gt=0)
    trigger_price: float = Field(gt=0)
    scores: EvidenceScore
    reasons: list[str] = Field(default_factory=list)
    blocked_by: list[str] = Field(default_factory=list)
    created_at: int
    updated_at: int
    expires_at: Optional[int] = None
    structural_stop: Optional[float] = None
    target_price: Optional[float] = None
    expected_rr: Optional[float] = None
    policy_version: int = Field(default=1, ge=1)
    batch_id: Optional[str] = None
    batch_sequence: Optional[int] = Field(default=None, ge=1)
    creation_sequence: int = Field(default=0, ge=0)
    accepted_at: Optional[int] = None
    grace_expires_at: Optional[int] = None
    notification_sent_at: Optional[int] = None


class SpotLedgerEvent(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)

    event_id: str
    client_event_id: str
    client_payload_hash: Optional[str] = None
    sequence: Optional[int] = Field(default=None, ge=1)
    event_type: Literal["fill", "reversal"] = "fill"
    side: Optional[LedgerSide] = None
    bucket: Optional[BudgetBucket] = None
    quantity_btc: float = Field(default=0.0, ge=0.0)
    price_usdt: float = Field(default=0.0, ge=0.0)
    fee_usdt: float = Field(default=0.0, ge=0.0)
    executed_at: int
    created_at: int
    opportunity_id: Optional[str] = None
    note: str = ""
    reverses_event_id: Optional[str] = None
    policy_override: bool = False
    policy_version: int = Field(default=1, ge=1)
    opportunity_stage: Optional[OpportunityStage] = None
    opportunity_allocation_usdt: Optional[float] = Field(default=None, gt=0)
    batch_id: Optional[str] = None

    @model_validator(mode="after")
    def validate_event(self) -> "SpotLedgerEvent":
        if self.event_type == "fill":
            if self.side is None or self.bucket is None:
                raise ValueError("fill 必须包含 side 和 bucket")
            if self.quantity_btc <= 0 or self.price_usdt <= 0:
                raise ValueError("fill 的 quantity_btc 和 price_usdt 必须大于 0")
            if self.reverses_event_id:
                raise ValueError("fill 不能设置 reverses_event_id")
        else:
            if not self.reverses_event_id:
                raise ValueError("reversal 必须指定 reverses_event_id")
        if self.side == "sell" and self.fee_usdt > self.quantity_btc * self.price_usdt:
            raise ValueError("卖出手续费不能超过卖出总额")
        return self


class BucketPosition(BaseModel):
    bucket: BudgetBucket
    cash_usdt: float = 0.0
    btc_quantity: float = 0.0
    cost_basis_usdt: float = 0.0
    average_cost_usdt: float = 0.0
    realized_pnl_usdt: float = 0.0


class SpotPortfolio(BaseModel):
    initial_capital_usdt: float
    buckets: dict[BudgetBucket, BucketPosition]
    total_cash_usdt: float = 0.0
    total_btc: float = 0.0
    total_cost_basis_usdt: float = 0.0
    average_cost_usdt: float = 0.0
    realized_pnl_usdt: float = 0.0
    core_bonus_from_swing_usdt: float = 0.0


class SpotAccumulationRuntimeState(BaseModel):
    version: int = 2
    cycle_ath: float = 0.0
    opportunities: dict[str, SpotOpportunity] = Field(default_factory=dict)
    tail_mode: Optional[Literal["extreme", "catch_up"]] = None
    last_filled_price: Optional[float] = None
    weekly_reclaim_count: int = 0
    creation_sequence: int = 0
    updated_at: int = 0


DecisionState = Literal["blocked", "conditional", "eligible", "accepted", "complete"]
LadderState = Literal[
    "waiting_anchor", "waiting_event", "conditional", "eligible", "accepted",
    "partial", "filled",
]
PricingMode = Literal["price_ladder", "event_driven"]


class SpotDecisionSummary(BaseModel):
    """小白版确定性结论；只解释规则状态，不产生资金副作用。"""

    state: DecisionState = "blocked"
    headline: str = "当前不买"
    detail: str = "等待估值、资金和现货承接共同确认"
    opportunity_id: Optional[str] = None
    stage: Optional[OpportunityStage] = None
    bucket: Optional[BudgetBucket] = None
    amount_usdt: Optional[float] = Field(default=None, ge=0.0)
    price_low: Optional[float] = Field(default=None, gt=0.0)
    price_high: Optional[float] = Field(default=None, gt=0.0)
    estimated_btc: Optional[float] = Field(default=None, ge=0.0)
    blockers: list[str] = Field(default_factory=list)
    grace_expires_at: Optional[int] = None
    updated_at: int = 0


class SpotConditionalLadderItem(BaseModel):
    """一档动态条件计划；conditional 状态绝不等价于买入授权。"""

    stage: OpportunityStage
    target_usdt: float = Field(ge=0.0)
    filled_usdt: float = Field(ge=0.0)
    remaining_usdt: float = Field(ge=0.0)
    planned_usdt: float = Field(default=0.0, ge=0.0)
    cash_shortfall_usdt: float = Field(default=0.0, ge=0.0)
    status: LadderState = "waiting_anchor"
    pricing_mode: PricingMode = "price_ladder"
    is_actionable: bool = False
    opportunity_id: Optional[str] = None
    reference_price_low: Optional[float] = Field(default=None, gt=0.0)
    reference_price_high: Optional[float] = Field(default=None, gt=0.0)
    reference_price_mid: Optional[float] = Field(default=None, gt=0.0)
    anchor_source: str = ""
    anchor_label: str = ""
    support_trust: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    blockers: list[str] = Field(default_factory=list)
    invalidation_reasons: list[str] = Field(default_factory=list)
    historical_quantity_btc: float = Field(default=0.0, ge=0.0)
    historical_average_price: Optional[float] = Field(default=None, ge=0.0)
    estimated_btc: Optional[float] = Field(default=None, ge=0.0)
    projected_total_btc: Optional[float] = Field(default=None, ge=0.0)
    projected_average_cost: Optional[float] = Field(default=None, ge=0.0)
    projected_cash_remaining: float = Field(ge=0.0)
    projected_core_cash_remaining: float = Field(default=0.0, ge=0.0)
    projected_total_cash_remaining: float = Field(default=0.0, ge=0.0)


class SpotSupportMapItem(BaseModel):
    """近场现货承接事实；挂单意图和已成交吸收严格分栏。"""

    support_id: str
    price_low: float = Field(gt=0.0)
    price_high: float = Field(gt=0.0)
    price_mid: float = Field(gt=0.0)
    distance_pct: float
    binance_spot_usd: float = Field(default=0.0, ge=0.0)
    coinbase_spot_usd: float = Field(default=0.0, ge=0.0)
    spot_wall_usd: float = Field(default=0.0, ge=0.0)
    absorption_usd: float = Field(default=0.0, ge=0.0)
    absorption_bar_count: int = Field(default=0, ge=0)
    absorption_age_hours: Optional[float] = Field(default=None, ge=0.0)
    persistence_1h: float = Field(default=0.0, ge=0.0, le=1.0)
    persistence_8h: float = Field(default=0.0, ge=0.0, le=1.0)
    max_usd_1h: float = Field(default=0.0, ge=0.0)
    max_usd_8h: float = Field(default=0.0, ge=0.0)
    support_trust: float = Field(default=0.0, ge=0.0, le=1.0)
    support_strength: float = Field(default=0.0, ge=0.0, le=1.0)
    support_fragility: float = Field(default=0.0, ge=0.0, le=1.0)
    dominant_role: str = "other"
    label: str = "观察区"
    wall_source_timestamp: int = Field(default=0, ge=0)
    wall_fresh: bool = False
    absorption_source_timestamp: int = Field(default=0, ge=0)
    absorption_fresh: bool = False
    source_timestamp: int = Field(default=0, ge=0)
    is_fresh: bool = False
    anchor_eligible: bool = False
    evidence: list[str] = Field(default_factory=list)


class SpotOpportunityJournalEvent(BaseModel):
    """机会状态的只追加检查点；state.json 只是该日志的可重建缓存。"""

    event_id: str
    sequence: int = Field(ge=1)
    event_type: Literal["migration", "market", "decision", "fill", "reversal", "config"]
    created_at: int
    runtime: SpotAccumulationRuntimeState
    note: str = ""


class SpotAccumulationSnapshot(BaseModel):
    coin: str = "BTC"
    timestamp: int
    facts: SpotAccumulationFacts
    portfolio: SpotPortfolio
    opportunities: list[SpotOpportunity] = Field(default_factory=list)
    budget_reserved_usdt: dict[BudgetBucket, float] = Field(default_factory=dict)
    next_action: str = "等待数据"
    warnings: list[str] = Field(default_factory=list)
    ai_explanation: Optional[str] = None
    decision_summary: Optional[SpotDecisionSummary] = None
    conditional_ladder: list[SpotConditionalLadderItem] = Field(default_factory=list)
    spot_support_map: list[SpotSupportMapItem] = Field(default_factory=list)
    view_warnings: list[str] = Field(default_factory=list)
