"""BTC 原生趋势与资金流模块的公开数据契约。"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, model_validator


TrendState = Literal[
    "data_invalid", "range", "bullish_watch", "bearish_watch",
    "bullish_candidate", "bearish_candidate", "bullish_confirmed",
    "bearish_confirmed", "weakening", "reversal_watch", "reversal_confirmed",
]


class DataQuality(BaseModel):
    valid: bool = False
    age_sec: Optional[int] = None
    points: int = 0
    reason: str = ""
    as_of_ts: Optional[int] = None
    fetched_at_ts: Optional[int] = None
    status: Literal["fresh", "stale", "pending", "missing"] = "missing"

    @model_validator(mode="after")
    def _cohere_status(self):
        if self.valid and self.status == "missing":
            self.status = "fresh"
        elif not self.valid and self.status == "fresh":
            self.status = "missing"
        return self


class TimeframeTrend(BaseModel):
    timeframe: Literal["15m", "1h", "4h", "1d"]
    score: float = Field(ge=-100, le=100)
    direction: Literal["bullish", "bearish", "range", "invalid"]
    price_volume_score: float = 0
    orderflow_score: float = 0
    oi_participation_score: float = 0
    spot_confirms: bool = False
    oi_interpretation: str = ""
    reasons: list[str] = Field(default_factory=list)
    quality: DataQuality = Field(default_factory=DataQuality)


class FlowWindow(BaseModel):
    window: Literal["1h", "4h", "24h", "3d", "7d"]
    buy_usd: float = 0
    sell_usd: float = 0
    net_usd: float = 0
    net_ratio: float = 0
    historical_percentile: Optional[float] = None


class ActiveFlowSnapshot(BaseModel):
    market: Literal["spot", "futures"]
    semantics: str
    windows: list[FlowWindow] = Field(default_factory=list)
    cvd_consistent: Optional[bool] = None
    quality: DataQuality = Field(default_factory=DataQuality)


class WalletContribution(BaseModel):
    exchange: str
    balance_btc: float = 0
    change_1d_btc: float = 0
    change_7d_btc: float = 0
    change_30d_btc: float = 0


class WalletChartPoint(BaseModel):
    ts: int
    price: Optional[float] = None
    balance_btc: float
    net_change_btc: Optional[float] = None


class WalletFlowSnapshot(BaseModel):
    source_granularity: Literal["1d"] = "1d"
    total_balance_btc: float = 0
    change_1d_btc: Optional[float] = None
    change_3d_btc: Optional[float] = None
    change_7d_btc: Optional[float] = None
    change_30d_btc: Optional[float] = None
    consecutive_direction_days: int = 0
    robust_zscore_90d: Optional[float] = None
    direction_consistent: bool = False
    dominant_exchange_ratio: float = 0
    contributions: list[WalletContribution] = Field(default_factory=list)
    chart: list[WalletChartPoint] = Field(default_factory=list)
    exchange_charts: dict[str, list[WalletChartPoint]] = Field(default_factory=dict)
    confidence_modifier: float = 0
    modifier_reason: str = ""
    quality: DataQuality = Field(default_factory=DataQuality)
    caveat: str = "余额增加代表潜在卖压，不等于已经卖出。链上余额源为日级更新。"


class ExchangeTransferPoint(BaseModel):
    ts: int
    inflow_btc: float = 0
    outflow_btc: float = 0
    netflow_btc: float = 0


class ExchangeTransferWindow(BaseModel):
    window: Literal["1d", "3d", "7d", "30d"]
    inflow_btc: float = 0
    outflow_btc: float = 0
    netflow_btc: float = 0
    net_ratio: float = 0
    inflow_percentile_365d: Optional[float] = None
    outflow_percentile_365d: Optional[float] = None
    abs_net_percentile_365d: Optional[float] = None
    same_sign_days: int = 0


class ExchangeTransferFlowSnapshot(BaseModel):
    source: Literal["looknode"] = "looknode"
    source_granularity: Literal["1d"] = "1d"
    scope: str = "Binance、OKX、Bitfinex、Deribit、Bybit、Huobi、KuCoin"
    covered_exchanges: list[str] = Field(default_factory=lambda: [
        "Binance", "OKX", "Bitfinex", "Deribit", "Bybit", "Huobi", "KuCoin",
    ])
    latest_date_ts: Optional[int] = None
    windows: list[ExchangeTransferWindow] = Field(default_factory=list)
    chart: list[ExchangeTransferPoint] = Field(default_factory=list)
    activity_regime: Literal[
        "unknown", "normal", "high_inflow", "high_outflow", "high_turnover",
    ] = "unknown"
    cross_source_status: Literal[
        "unavailable", "neutral", "confirmed", "conflict",
    ] = "unavailable"
    coinglass_7d_abs_percentile: Optional[float] = None
    score_weight: int = 0
    quality: DataQuality = Field(default_factory=DataQuality)
    caveat: str = (
        "仅覆盖七家主要交易所。充值不等于卖出、提现不等于买入，"
        "可能包含交易所内部钱包迁移。"
    )


class FundingSnapshot(BaseModel):
    binance_rate: Optional[float] = None
    okx_rate: Optional[float] = None
    oi_weighted_rate: Optional[float] = None
    avg_24h: Optional[float] = None
    avg_3d: Optional[float] = None
    avg_7d: Optional[float] = None
    same_sign_settlements: int = 0
    percentile_30d: Optional[float] = None
    basis_pct: Optional[float] = None
    crowding: Literal["unknown", "neutral", "long_crowded", "short_crowded"] = "unknown"
    market_bias: Literal["bullish", "bearish", "neutral"] = "neutral"
    confidence_modifier: float = 0
    modifier_reason: str = ""
    quality: DataQuality = Field(default_factory=DataQuality)


class EtfFlowSnapshot(BaseModel):
    net_1d_usd: Optional[float] = None
    net_3_sessions_usd: Optional[float] = None
    net_5_sessions_usd: Optional[float] = None
    latest_session_ts: Optional[int] = None
    confidence_modifier: float = 0
    quality: DataQuality = Field(default_factory=DataQuality)


class FootprintStatus(BaseModel):
    enabled: bool = True
    score_weight: int = 0
    available: bool = False
    availability_14d_pct: Optional[float] = None
    ablation_validated: bool = False
    promotion_eligible: bool = False
    quality: DataQuality = Field(default_factory=DataQuality)
    note: str = "首版仅展示数据健康，趋势评分权重为0。"


class ModifierBreakdown(BaseModel):
    funding_applied: float = 0
    wallet_market_bias: float = 0
    wallet_applied: float = 0
    etf_applied: float = 0
    total: float = 0
    wallet_cross_source_status: Literal[
        "unavailable", "neutral", "confirmed", "conflict",
    ] = "unavailable"


class TrendEvent(BaseModel):
    id: Optional[int] = None
    ts: int
    event_type: str
    severity: Literal["info", "warning", "critical"] = "info"
    direction: Optional[Literal["bullish", "bearish"]] = None
    title: str
    message: str
    dedup_key: str
    payload: dict[str, Any] = Field(default_factory=dict)


class TrendSnapshot(BaseModel):
    coin: Literal["BTC"] = "BTC"
    ts: int
    closed_5m_ts: int
    algorithm_version: str
    state: TrendState
    direction: Literal["bullish", "bearish", "range", "invalid"]
    core_score: float = Field(ge=-100, le=100)
    confidence: float = Field(ge=0, le=100)
    core_direction_immutable: bool = True
    consecutive_core_confirmations: int = 0
    confirmation_target: int = 3
    timeframes: dict[str, TimeframeTrend] = Field(default_factory=dict)
    active_flows: dict[str, ActiveFlowSnapshot] = Field(default_factory=dict)
    wallet_flow: WalletFlowSnapshot = Field(default_factory=WalletFlowSnapshot)
    exchange_transfer_flow: ExchangeTransferFlowSnapshot = Field(
        default_factory=ExchangeTransferFlowSnapshot,
    )
    funding: FundingSnapshot = Field(default_factory=FundingSnapshot)
    etf_flow: EtfFlowSnapshot = Field(default_factory=EtfFlowSnapshot)
    footprint: FootprintStatus = Field(default_factory=FootprintStatus)
    modifier_total: float = 0
    modifier_breakdown: ModifierBreakdown = Field(default_factory=ModifierBreakdown)
    ai_review: Literal["not_run", "accept", "downgrade", "veto"] = "not_run"
    ai_review_reason: str = ""
    data_quality: DataQuality = Field(default_factory=DataQuality)
    source_diagnostics: dict[str, Any] = Field(default_factory=dict)
    disclaimer: str = "仅用于趋势与资金状态监控，不构成交易、开仓、止盈止损或仓位建议。"


class TrendMachineContext(BaseModel):
    """状态机的持久化上下文，不能从单个展示快照反推。"""

    algorithm_version: str = ""
    confirmed_direction: Optional[Literal["bullish", "bearish"]] = None
    confirmation_direction: Optional[Literal["bullish", "bearish"]] = None
    confirmation_count: int = 0
    last_counted_bar: int = 0
