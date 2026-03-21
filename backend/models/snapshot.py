"""AI 分析用的数据快照 + 因子卡片 + 市场温度"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel


class FactorCard(BaseModel):
    """单张因子卡片数据"""
    id: str             # "D1" - "D8"
    name: str           # "清算失衡"
    value: str          # "+2.4" 主显示值
    direction: str      # "bullish" | "bearish" | "neutral"
    sub_text: str       # "空1.03亿 / 多0.68亿"
    percentile: float   # 0-100 百分位
    summary: str        # "上扫空头" 一句话结论


class MarketTemperature(BaseModel):
    """市场温度计"""
    coin: str
    ts: int
    score: float            # 0-100, 50=中性
    label: str              # "极冷" / "偏冷" / "中性" / "偏热" / "极热"
    pin_risk_level: str     # "low" / "attention" / "high" / "extreme"
    pin_risk_label: str     # "🟢低风险" / "🟡关注" / "🟠中高" / "🔴极高"
    factors: list[FactorCard]


class WaterfallItem(BaseModel):
    """瀑布图单项"""
    factor_id: str
    factor_name: str
    contribution_pct: float  # 正=看多，负=看空
    direction: str           # "bullish" | "bearish"


class WaterfallData(BaseModel):
    """多空归因瀑布图"""
    coin: str
    ts: int
    items: list[WaterfallItem]
    bullish_total: float
    bearish_total: float
    net_bias: float
    net_label: str  # "偏向看多" / "偏向看空" / "多空均衡"


class MacroSnapshot(BaseModel):
    """宏观数据快照 (v2.0 预留)"""
    dxy: Optional[dict] = None
    nasdaq: Optional[dict] = None
    gold: Optional[dict] = None
    oil: Optional[dict] = None
    vix: Optional[dict] = None
    events: list[dict] = []


class SourceHealth(BaseModel):
    """数据源健康状态"""
    name: str
    status: str         # "connected" | "degraded" | "disconnected"
    latency_ms: float = 0
    last_success_ts: int = 0
    error_count: int = 0


class AISnapshot(BaseModel):
    """发送给 AI 的结构化数据快照"""
    coin: str
    ts: int
    price: float
    high_24h: float
    low_24h: float

    liq_clusters_above: list[dict]
    liq_clusters_below: list[dict]
    vacuum_zones: list[dict]
    liq_imbalance_ratio: float

    cvd_contract_trend: str
    cvd_contract_delta_1h: float
    cvd_spot_trend: str
    cvd_spot_delta_1h: float
    cvd_divergence: str

    oi_current_usd: float
    oi_change_1h_pct: float
    oi_change_5m_pct: float
    oi_trend: str

    funding_rate_okx: Optional[float]
    funding_rate_binance: Optional[float]
    funding_interpretation: str

    basis_pct: float

    orderbook_bid_walls: list[dict]
    orderbook_ask_walls: list[dict]

    recent_liq_30m_long_usd: float
    recent_liq_30m_short_usd: float

    volume_profile_poc: float
    value_area_high: float
    value_area_low: float
    vwap: float

    atr_14: float
    market_temperature: float
    pin_risk_level: str

    macro_context: Optional[MacroSnapshot] = None


class AIAnalysisResult(BaseModel):
    """AI 分析输出"""
    coin: str
    ts: int
    price_at_analysis: float
    market_overview: str
    key_levels: list[dict]
    stop_loss_suggestion: dict
    entry_zones: list[dict]
    risk_warnings: list[str]
    scenario_analysis: list[dict]
    raw_text: str
