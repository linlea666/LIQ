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


class SpotAccumulationConfig(BaseModel):
    version: int = 2
    coin: str = "BTC"
    initial_capital_usdt: float = 20_000.0
    core_ratio: float = 0.65
    swing_ratio: float = 0.20
    tail_ratio: float = 0.15
    insurance_ratio: float = 0.05
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
        return self

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
        remaining = self.core_ratio - self.insurance_ratio
        ratios = {
            "insurance": self.insurance_ratio,
            "value_1": remaining * (10 / 60),
            "deep_value": remaining * (15 / 60),
            "capitulation": remaining * (15 / 60),
            "bottom_confirmed": remaining * (20 / 60),
        }
        return {
            stage: round(self.initial_capital_usdt * ratio, 8)
            for stage, ratio in ratios.items()
        }

    def public_dump(self) -> dict[str, Any]:
        """API返回比例配置及当前总资金派生出的明确金额。"""
        payload = self.model_dump(mode="json")
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


class SpotDataQuality(BaseModel):
    completeness: float = Field(0.0, ge=0.0, le=1.0)
    stale_sources: list[str] = Field(default_factory=list)
    missing_sources: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    can_open_new_opportunity: bool = False


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
    version: int = 1
    cycle_ath: float = 0.0
    opportunities: dict[str, SpotOpportunity] = Field(default_factory=dict)
    tail_mode: Optional[Literal["extreme", "catch_up"]] = None
    last_filled_price: Optional[float] = None
    weekly_reclaim_count: int = 0
    updated_at: int = 0


class SpotOpportunityJournalEvent(BaseModel):
    """机会状态的只追加检查点；state.json 只是该日志的可重建缓存。"""

    event_id: str
    sequence: int = Field(ge=1)
    event_type: Literal["migration", "decision", "fill", "reversal", "config"]
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
