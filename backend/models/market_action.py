"""市场动作分析器（Market Action Analyzer）数据模型

本模块实现"基于真实市场动作"的场景识别：
- 输入（`MarketActionFacts`）：14 字段严格锁定，来自 polls 层数据
- 输出（`MarketActionReport`）：AI Arbiter 给出的 6 块结构化结论

设计原则（已与用户拍板）：
1. 只收真实反映市场动作的指标（剔除宏观叙事/零售情绪）
2. 场景枚举 9 种，AI 必须落在其中一类
3. 数据缺失不崩，通过 `data_quality` + `missing` 降级
4. 所有字段对 SOL 兼容（期权维度 SOL=None）
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

# ────────────────────────────────────────────────────────────────────────────
# S 级 · 核心 6 项（facts 输入）
# ────────────────────────────────────────────────────────────────────────────

class PriceSnapshot(BaseModel):
    """S1 · 价格 + 多周期涨跌"""
    last: float
    change_1h_pct: Optional[float] = None
    change_4h_pct: Optional[float] = None
    change_24h_pct: Optional[float] = None
    high_24h: Optional[float] = None
    low_24h: Optional[float] = None
    recent_bars_1h: list[list[float]] = Field(default_factory=list)
    # 每根 bar = [ts, open, high, low, close, volume]，近 6 根


class OISnapshot(BaseModel):
    """S2 · OI 规模 + 变化率"""
    current_usd: float
    change_5m_pct: Optional[float] = None
    change_1h_pct: Optional[float] = None
    change_24h_pct: Optional[float] = None
    trend: Optional[str] = None  # "rising" / "declining" / "flat"


class FundingSnapshot(BaseModel):
    """S3 · Funding + 多交易所分散度"""
    avg_current: float
    avg_7d: Optional[float] = None
    oi_weighted: Optional[float] = None
    interpretation: Optional[str] = None
    exchange_count: int = 0
    dispersion_abs: Optional[float] = None
    # 各交易所 current 的标准差，反映是否一致


class CVDSnapshot(BaseModel):
    """S4/S5 · CVD 合约或现货（可贴附短窗 netflow）"""
    delta_1h: float
    trend_1h: Optional[str] = None  # rising / declining / flat
    has_divergence: bool = False
    divergence_note: Optional[str] = None
    recent_delta_5m: list[float] = Field(default_factory=list)
    # 最近 6 个 5m bar 的 delta（buy - sell），用于看瞬时方向


class LiquidationSnapshot(BaseModel):
    """S6 · 全网清算流"""
    long_1h_usd: float
    short_1h_usd: float
    long_24h_usd: float
    short_24h_usd: float
    ratio_1h: float = 1.0
    dominant_side_1h: Literal["long_being_liquidated", "short_being_liquidated", "balanced"] = "balanced"


# ────────────────────────────────────────────────────────────────────────────
# A 级 · 关键区分 9 项
# ────────────────────────────────────────────────────────────────────────────

class BasisSnapshot(BaseModel):
    """A1 · 期现溢价 + 趋势"""
    basis_pct: float
    basis_trend: Literal["widening", "narrowing", "stable"] = "stable"
    # 近 1h 对比
    recent_values: list[float] = Field(default_factory=list)  # 最近 12 点（1h，5m 间隔）
    interpretation: Optional[str] = None


class OrderbookSnapshot(BaseModel):
    """A2 · 盘口深度 + spread 趋势"""
    bid_total_usd: float
    ask_total_usd: float
    spread_pct: float
    spread_trend: Literal["widening", "narrowing", "stable"] = "stable"
    recent_spreads: list[float] = Field(default_factory=list)
    # 最近 12 点 5m spread


class LiqClusterSnapshot(BaseModel):
    """A3 · 清算图上下簇对比"""
    above_cluster_usd: float = 0
    below_cluster_usd: float = 0
    above_nearest_price: Optional[float] = None
    below_nearest_price: Optional[float] = None
    above_distance_pct: Optional[float] = None
    below_distance_pct: Optional[float] = None
    bias: Literal["short_squeeze_fuel", "long_squeeze_fuel", "balanced", "unknown"] = "unknown"


class LiqSweepSnapshot(BaseModel):
    """A4 · 清算热区连续触发"""
    recent_sweeps_count: int = 0  # 近 N 分钟被触发的次数
    recent_window_min: int = 30
    last_sweep_ts: Optional[int] = None
    last_sweep_side: Optional[Literal["long_side", "short_side"]] = None
    continuous_trigger: bool = False
    # 连续 3+ 次同方向


class PriceContextSnapshot(BaseModel):
    """A8 · 价格位置上下文（swing + 区间 + POC）"""
    swing_high_1h: Optional[float] = None   # 最近 20 根 1h 的 swing high
    swing_low_1h: Optional[float] = None
    range_20d_high: Optional[float] = None
    range_20d_low: Optional[float] = None
    range_position_pct: Optional[float] = None  # 0(下沿) ~ 100(上沿)
    poc_price: Optional[float] = None            # Volume Profile POC
    vah_price: Optional[float] = None
    val_price: Optional[float] = None
    price_vs_poc: Optional[Literal["above", "below", "at"]] = None
    distance_to_swing_high_pct: Optional[float] = None
    distance_to_swing_low_pct: Optional[float] = None


# ────────────────────────────────────────────────────────────────────────────
# A9 · Footprint（足迹图派生，合约+现货）
# ────────────────────────────────────────────────────────────────────────────

class FootprintBarStats(BaseModel):
    """单根 K 线的足迹统计（派生，不含原始 buckets）"""
    ts: int
    total_buy_usd: float = 0
    total_sell_usd: float = 0
    delta_usd: float = 0
    delta_pct: float = 0  # delta / total，±1.0 区间
    poc_price: Optional[float] = None
    # 这根 K 线内成交最密集的价位
    top_imbalance_zones: list[dict] = Field(default_factory=list)
    # [{price: 78150, buy: 12000, sell: 800, ratio: 15.0, side: "stacked_buy"}, ...]
    # 只保留 ratio > 3 的强失衡价位
    high_price_delta_pct: Optional[float] = None
    # K 线上 1/3 价位区的 delta_pct（用于衰竭识别）
    low_price_delta_pct: Optional[float] = None
    # K 线下 1/3 价位区的 delta_pct


class FootprintSnapshot(BaseModel):
    """A9 · 合约+现货足迹图派生摘要"""
    contract_latest: Optional[FootprintBarStats] = None
    contract_prev: Optional[FootprintBarStats] = None
    spot_latest: Optional[FootprintBarStats] = None
    spot_prev: Optional[FootprintBarStats] = None
    # 期现 delta 差（最新 K 线）
    spot_contract_delta_diff_pct: Optional[float] = None
    # spot.delta_pct - contract.delta_pct，反映期现一致性
    interpretation: Optional[str] = None


# ────────────────────────────────────────────────────────────────────────────
# B 级 · 加分 2 项
# ────────────────────────────────────────────────────────────────────────────

class TakerFlowSnapshot(BaseModel):
    """B1 · Taker 期现 5m 序列"""
    contract_recent_5m: list[dict] = Field(default_factory=list)
    # [{ts, buy_usd, sell_usd, delta_usd}, ...] 最近 12 根
    spot_recent_5m: list[dict] = Field(default_factory=list)
    # 最近 5m 期现净流入对比
    spot_vs_contract_divergence: bool = False
    latest_contract_delta_usd: Optional[float] = None
    latest_spot_delta_usd: Optional[float] = None


class OptionsSnapshot(BaseModel):
    """B2 · 期权（仅 BTC/ETH）"""
    total_oi_usd: Optional[float] = None
    oi_change_24h_pct: Optional[float] = None
    vol_change_24h_pct: Optional[float] = None
    pcr_oi: Optional[float] = None  # put/call ratio by OI
    magnet_price: Optional[float] = None  # 近 3 个到期日 max_pain × OI 加权
    iv_current: Optional[float] = None  # 来自 BBX（复用）
    iv_change_24h_pct: Optional[float] = None
    iv_skew_1m: Optional[float] = None


# ────────────────────────────────────────────────────────────────────────────
# MarketActionFacts · AI Arbiter 的输入契约（14 字段严格锁定）
# ────────────────────────────────────────────────────────────────────────────

MarketActionCoherence = Literal["confirming", "diverging", "neutral"]
SpotContractCoherence = Literal["spot_leads", "spot_lags", "aligned", "unknown"]
FundingTrend = Literal["building", "easing", "stable", "extreme"]
DataQuality = Literal["ok", "partial", "insufficient"]


class MarketActionFacts(BaseModel):
    """AI Arbiter 输入契约 · 14 字段 + 元数据"""
    coin: str
    timestamp: int

    # S 级 6
    price: Optional[PriceSnapshot] = None
    oi: Optional[OISnapshot] = None
    funding: Optional[FundingSnapshot] = None
    cvd_contract: Optional[CVDSnapshot] = None
    cvd_spot: Optional[CVDSnapshot] = None
    liquidation_flow: Optional[LiquidationSnapshot] = None

    # A 级 9
    basis: Optional[BasisSnapshot] = None
    orderbook: Optional[OrderbookSnapshot] = None
    liq_map_clusters: Optional[LiqClusterSnapshot] = None
    liq_sweep_recent: Optional[LiqSweepSnapshot] = None
    oi_price_coherence: MarketActionCoherence = "neutral"
    spot_contract_coherence: SpotContractCoherence = "unknown"
    funding_trend: FundingTrend = "stable"
    price_context: Optional[PriceContextSnapshot] = None
    footprint: Optional[FootprintSnapshot] = None

    # B 级 2
    taker_flow_5m: Optional[TakerFlowSnapshot] = None
    options: Optional[OptionsSnapshot] = None

    # 元数据
    data_quality: DataQuality = "ok"
    missing: list[str] = Field(default_factory=list)


# ────────────────────────────────────────────────────────────────────────────
# MarketActionReport · AI 输出契约（6 块 JSON）
# ────────────────────────────────────────────────────────────────────────────

# 9 种场景（与用户拍板一致）
MarketScenario = Literal[
    "trend_continuation_up",
    "trend_continuation_down",
    "short_squeeze_up",
    "long_squeeze_down",
    "fake_breakout_up",
    "fake_breakdown_down",
    "exhaustion_top",
    "exhaustion_bottom",
    "range_bound",
]

MarketPhase = Literal[
    "accumulation",
    "markup",
    "distribution",
    "markdown",
    "transition",
]


class EvidenceItem(BaseModel):
    """单条证据（AI 从 facts 中引用的关键点）"""
    dimension: str          # e.g. "CVD 期现", "Footprint", "Basis"
    observation: str        # 中文自然语言描述
    weight: Literal["high", "medium", "low"] = "medium"


class TradingImplications(BaseModel):
    """AI 给出的操作建议（仅提示，不自动下单）"""
    bias: Literal["long", "short", "neutral", "wait"] = "wait"
    entry_zone: Optional[list[float]] = None  # [low, high]
    stop_loss_beyond: Optional[float] = None
    take_profit_targets: list[float] = Field(default_factory=list)
    notes: Optional[str] = None


class MarketActionReport(BaseModel):
    """AI Arbiter 输出契约"""
    coin: str
    timestamp: int

    # 6 块结构
    market_conclusion: str                    # 2-3 句中文总结
    scenario: MarketScenario
    market_phase: MarketPhase
    evidence_breakdown: list[EvidenceItem] = Field(default_factory=list)
    trading_implications: TradingImplications = Field(default_factory=TradingImplications)
    invalidation_conditions: list[str] = Field(default_factory=list)
    confidence: int = Field(ge=0, le=100)

    # 元数据
    data_quality: DataQuality = "ok"
    stale_minutes: int = 0  # 距上次成功 AI 调用的分钟数（若为降级结果）
    facts_snapshot: Optional[MarketActionFacts] = None  # 快照留档用
