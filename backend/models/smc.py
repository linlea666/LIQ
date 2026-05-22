"""SMC / Smart Money Concepts data contracts.

The SMC module is an independent interpretation layer.  These models describe
its output shape only; the processor must build them from raw market state.
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


SMCHorizon = Literal["intraday", "swing"]
SMCObservation = Literal["long_watch", "short_watch", "wait"]
SMCSetupState = Literal[
    "candidate",
    "raid_detected",
    "mss_confirmed",
    "entry_zone_active",
    "invalidated",
    "expired",
]
SMCZoneKind = Literal[
    "liquidity",
    "order_block",
    "fair_value_gap",
    "breaker",
    "po3",
    "fib_ote",
    "turnover_sr",
]
SMCZoneRole = Literal[
    "buy_side_liquidity",
    "sell_side_liquidity",
    "bullish_demand",
    "bearish_supply",
    "support",
    "resistance",
    "neutral",
]
SMCKeyLevelSide = Literal["support", "resistance"]
SMCKeyLevelTier = Literal["near", "mid", "far"]
SMCKeyLevelConfidence = Literal["low", "medium", "high"]


class SMCFieldMapItem(BaseModel):
    """Internal field mapping item returned by the facts endpoint."""

    field: str
    priority: Literal["P0", "P1", "P2"]
    periods: list[str] = []
    use: str = ""
    notes: str = ""


class SMCDataQuality(BaseModel):
    score: int = 0
    status: Literal["ok", "partial", "degraded"] = "degraded"
    missing: list[str] = []
    stale: list[str] = []
    degraded: list[str] = []
    source_status: dict[str, str] = {}
    notes: list[str] = []


class SMCStructureEvent(BaseModel):
    event_id: str
    kind: Literal["swing_high", "swing_low", "bos", "mss", "liquidity_raid"]
    direction: Literal["bullish", "bearish", "neutral"] = "neutral"
    timeframe: str
    ts: int
    price: float
    strength: float = 0
    note: str = ""


class SMCLiquidityPool(BaseModel):
    pool_id: str
    side: Literal["buy_side", "sell_side"]
    source: str
    timeframe: str
    price: float
    price_from: float
    price_to: float
    strength: float = 0
    swept: bool = False
    distance_pct: float = 0
    evidence: list[str] = []


class SMCZone(BaseModel):
    zone_id: str
    kind: SMCZoneKind
    role: SMCZoneRole = "neutral"
    state: SMCSetupState = "candidate"
    timeframe: str
    price_from: float
    price_to: float
    midpoint: float
    strength: float = 0
    distance_pct: float = 0
    created_ts: int = 0
    invalidation_price: Optional[float] = None
    evidence: list[str] = []
    notes: list[str] = []


class SMCTargetZone(BaseModel):
    price: float
    kind: str
    side: Literal["above", "below", "neutral"] = "neutral"
    distance_pct: float = 0
    note: str = ""


class SMCKeyLevel(BaseModel):
    level_id: str
    side: SMCKeyLevelSide
    tier: SMCKeyLevelTier
    price: float
    price_from: float
    price_to: float
    distance_pct: float = 0
    strength: float = 0
    confidence: SMCKeyLevelConfidence = "low"
    sources: list[str] = []
    evidence: list[str] = []
    note: str = ""


class SMCConfirmation(BaseModel):
    source: str
    direction: Literal["bullish", "bearish", "neutral"] = "neutral"
    score_delta: float = 0
    confidence: float = 0
    note: str = ""


class SMCContradiction(BaseModel):
    source: str
    severity: Literal["low", "medium", "high"] = "low"
    note: str = ""


class SMCNansenPerp(BaseModel):
    token_symbol: str = ""
    mark_price: Optional[float] = None
    funding: Optional[float] = None
    open_interest: Optional[float] = None
    smart_money_volume: Optional[float] = None
    smart_money_buy_volume: Optional[float] = None
    smart_money_sell_volume: Optional[float] = None
    net_position_change: Optional[float] = None
    current_longs_usd: Optional[float] = None
    current_shorts_usd: Optional[float] = None
    trader_count: Optional[int] = None
    updated_at: int = 0


class SMCNansenFlow(BaseModel):
    token_symbol: str = ""
    token_address: str = ""
    chain: str = "ethereum"
    timeframe: str = "1d"
    smart_trader_net_flow_usd: Optional[float] = None
    top_pnl_net_flow_usd: Optional[float] = None
    whale_net_flow_usd: Optional[float] = None
    exchange_net_flow_usd: Optional[float] = None
    updated_at: int = 0


class SMCExchangeFlowWindow(BaseModel):
    window: Literal["1d", "7d"] = "7d"
    from_ts: Optional[str] = None
    to_ts: Optional[str] = None
    rows: int = 0
    latest_price_usd: Optional[float] = None
    cex_in_token: float = 0
    cex_out_token_abs: float = 0
    cex_out_token_raw: float = 0
    cex_net_token: float = 0
    cex_net_usd_approx: Optional[float] = None
    dex_net_token: float = 0
    dex_net_usd_approx: Optional[float] = None
    direction: Literal["exchange_inflow", "exchange_outflow", "neutral"] = "neutral"
    interpretation: str = ""


class SMCFundFlowAlert(BaseModel):
    alert_id: str
    severity: Literal["low", "medium", "high"] = "low"
    direction: Literal["bullish", "bearish", "neutral"] = "neutral"
    title: str = ""
    note: str = ""
    tags: list[str] = []
    ts: int = 0


class SMCFundFlowEvent(BaseModel):
    event_id: str
    ts: int = 0
    direction: Literal["bullish", "bearish", "neutral"] = "neutral"
    severity: Literal["low", "medium", "high"] = "low"
    title: str = ""
    note: str = ""
    cex_net_usd_approx: Optional[float] = None
    cex_net_token: float = 0
    price_usd: Optional[float] = None
    tags: list[str] = []


class SMCFundFlowContext(BaseModel):
    status: Literal["ok", "partial", "missing"] = "missing"
    bias: Literal["bullish", "bearish", "neutral"] = "neutral"
    confidence: float = 0
    one_day: Optional[SMCExchangeFlowWindow] = None
    seven_day: Optional[SMCExchangeFlowWindow] = None
    alerts: list[SMCFundFlowAlert] = []
    events: list[SMCFundFlowEvent] = []
    updated_at: int = 0
    notes: list[str] = []


class SMCSmartMoneyContext(BaseModel):
    status: Literal["ok", "partial", "missing"] = "missing"
    bias: Literal["bullish", "bearish", "neutral"] = "neutral"
    confidence: float = 0
    perp: Optional[SMCNansenPerp] = None
    flow: Optional[SMCNansenFlow] = None
    market_breadth_score: Optional[float] = None
    notes: list[str] = []


class SMCMarketBreadth(BaseModel):
    ts: int = 0
    status: Literal["ok", "partial", "missing"] = "missing"
    chains: list[str] = []
    smart_money_netflow: list[dict[str, Any]] = []
    token_screener: list[dict[str, Any]] = []
    breadth_score: float = 0
    data_quality: SMCDataQuality = Field(default_factory=SMCDataQuality)


class SMCSnapshot(BaseModel):
    coin: str
    ts: int
    horizon: SMCHorizon
    last_price: float = 0
    observation: SMCObservation = "wait"
    setup_state: SMCSetupState = "candidate"
    confidence: int = 0
    summary: str = ""
    timeframe_map: dict[str, list[str]] = {}
    invalidation_price: Optional[float] = None
    structure: list[SMCStructureEvent] = []
    liquidity_pools: list[SMCLiquidityPool] = []
    zones: list[SMCZone] = []
    key_levels: list[SMCKeyLevel] = []
    targets: list[SMCTargetZone] = []
    confirmations: list[SMCConfirmation] = []
    contradictions: list[SMCContradiction] = []
    smart_money: SMCSmartMoneyContext = Field(default_factory=SMCSmartMoneyContext)
    fund_flow: SMCFundFlowContext = Field(default_factory=SMCFundFlowContext)
    data_quality: SMCDataQuality = Field(default_factory=SMCDataQuality)
    facts_version: str = "smc.v1"
