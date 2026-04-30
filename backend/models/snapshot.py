"""AI 分析用数据快照：Strategic AI 输入（AISnapshot）+ 市场温度/瀑布图等 UI 模型"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

from models.key_level import KeyLevelSnapshotV2
from models.liquidation import LiqCluster, VacuumZone, LiqMaxPainItem
from models.market_action import (
    AbsorptionSnapshot,
    BasisSnapshot,
    CVDSnapshot,
    FootprintSnapshot,
    FundingSnapshot,
    LiqClusterSnapshot,
    LiqSweepSnapshot,
    OISnapshot,
    OptionsSnapshot,
    OrderbookSnapshot,
    PriceContextSnapshot,
    TakerFlowSnapshot,
)
from models.orderbook_pressure import (
    PositionCrowdingSnapshot,
    WallEvent,
    WallZone,
)
from models.trading_brain import TradingBrainSnapshot


class FactorCard(BaseModel):
    """单张因子卡片数据"""
    id: str
    name: str
    value: str
    direction: str
    sub_text: str
    percentile: float
    summary: str


class MarketTemperature(BaseModel):
    """市场温度计"""
    coin: str
    ts: int
    score: float
    label: str
    pin_risk_level: str
    pin_risk_label: str
    factors: list[FactorCard]


class WaterfallItem(BaseModel):
    """瀑布图单项"""
    factor_id: str
    factor_name: str
    contribution_pct: float
    direction: str


class WaterfallData(BaseModel):
    """多空归因瀑布图"""
    coin: str
    ts: int
    items: list[WaterfallItem]
    bullish_total: float
    bearish_total: float
    net_bias: float
    net_label: str


class MacroSnapshot(BaseModel):
    """宏观数据快照 (v2.0 预留)"""
    dxy: Optional[dict] = None
    nasdaq: Optional[dict] = None
    gold: Optional[dict] = None
    oil: Optional[dict] = None
    vix: Optional[dict] = None
    events: list[dict] = []


class LiquidationMapBlock(BaseModel):
    """单时间窗清算图聚合块（强类型，供 Strategic AI §8）。"""

    cycle: str = ""
    clusters_above: list[LiqCluster] = Field(default_factory=list)
    clusters_below: list[LiqCluster] = Field(default_factory=list)
    vacuum_zones: list[VacuumZone] = Field(default_factory=list)
    imbalance_ratio: float = 0.0
    max_pain: Optional[LiqMaxPainItem] = None
    by_exchange_summary: list[dict] = Field(default_factory=list)


class SourceHealth(BaseModel):
    """数据源健康状态"""
    name: str
    status: str
    latency_ms: float = 0
    last_success_ts: int = 0
    error_count: int = 0


class AISnapshot(BaseModel):
    """Strategic AI 唯一主快照（PR-5 瘦身：去掉旧 Trader/数学层重复弱类型字段）。"""

    coin: str
    ts: int
    price: float
    high_24h: float
    low_24h: float

    atr_14: float = 0.0
    rsi_14: Optional[float] = None
    volume_profile_poc: float = 0.0
    value_area_high: float = 0.0
    value_area_low: float = 0.0
    vwap: float = 0.0
    market_temperature: float = 50.0
    pin_risk_level: str = "low"
    range_signal: Optional[dict] = None

    boll_upper: Optional[float] = None
    boll_middle: Optional[float] = None
    boll_lower: Optional[float] = None
    ema20: Optional[float] = None
    btc_hist_vol: Optional[float] = None

    poll_failures: dict[str, str] = Field(default_factory=dict)

    global_liq_long_24h: float = 0.0
    global_liq_short_24h: float = 0.0
    global_liq_ratio_24h: float = 1.0
    liq_sweep_above_usd_1h: float = 0.0
    liq_sweep_below_usd_1h: float = 0.0
    liq_sweep_events: list[dict] = Field(default_factory=list)

    dxy: Optional[float] = None
    dxy_change_pct: Optional[float] = None
    btc_dominance: Optional[float] = None
    us_10y_yield: Optional[float] = None
    fed_rate: Optional[float] = None
    nasdaq: Optional[float] = None
    nasdaq_change_pct: Optional[float] = None
    gold: Optional[float] = None
    gold_change_pct: Optional[float] = None
    fear_greed_index: Optional[float] = None
    ahr999: Optional[float] = None
    btc_mvrv: Optional[float] = None
    etf_net_3d: Optional[float] = None
    etf_trend: str = ""
    stablecoin_total_mcap: float = 0.0
    stablecoin_7d_change_pct: float = 0.0
    coinbase_premium: float = 0.0
    coinbase_premium_trend: str = ""
    whale_net_direction: str = ""
    whale_transfers_count: int = 0
    whale_transfer_net_usd: float = 0.0
    exchange_btc_total: Optional[float] = None
    exchange_btc_change_pct: Optional[float] = None

    active_narratives: list[dict] = Field(default_factory=list)
    news_brief_text: str = ""
    news_brief_version: int = 0
    news_brief_trigger: str = ""
    news_brief_updated_at: Optional[int] = None

    option_max_pain_price: Optional[float] = None
    option_nearest_expiry: str = ""
    btc_implied_vol: Optional[float] = None
    btc_put_call_oi: Optional[float] = None

    trading_brain: Optional[TradingBrainSnapshot] = None
    key_level_snapshot: Optional[KeyLevelSnapshotV2] = None
    liq_map_block_1d: Optional[LiquidationMapBlock] = None
    liq_map_block_7d: Optional[LiquidationMapBlock] = None
    liq_map_block_30d: Optional[LiquidationMapBlock] = None
    wall_zones_above: list[WallZone] = Field(default_factory=list)
    wall_zones_below: list[WallZone] = Field(default_factory=list)
    wall_events_v2: list[WallEvent] = Field(default_factory=list)
    crowding_global: Optional[PositionCrowdingSnapshot] = None
    usd_usdt_basis_pct: Optional[float] = None

    facts_oi: Optional[OISnapshot] = None
    facts_funding: Optional[FundingSnapshot] = None
    facts_cvd_contract: Optional[CVDSnapshot] = None
    facts_cvd_spot: Optional[CVDSnapshot] = None
    facts_basis: Optional[BasisSnapshot] = None
    facts_orderbook: Optional[OrderbookSnapshot] = None
    facts_liq_clusters: Optional[LiqClusterSnapshot] = None
    facts_liq_sweep: Optional[LiqSweepSnapshot] = None
    facts_price_context: Optional[PriceContextSnapshot] = None
    facts_footprint: Optional[FootprintSnapshot] = None
    facts_absorption: Optional[AbsorptionSnapshot] = None
    facts_taker_flow: Optional[TakerFlowSnapshot] = None
    facts_options: Optional[OptionsSnapshot] = None

    facts_data_quality: str = ""
    facts_missing: list[str] = Field(default_factory=list)
    facts_has_provisional_bars: bool = False
    facts_provisional_fields: list[str] = Field(default_factory=list)
    facts_sources_used: list[str] = Field(default_factory=list)
