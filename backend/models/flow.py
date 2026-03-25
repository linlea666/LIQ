"""资金流数据模型：CVD、OI、资金费率、期现溢价"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel


class CVDPoint(BaseModel):
    """CVD 单个数据点"""
    ts: int
    buy_vol: float
    sell_vol: float
    delta: float  # buy - sell
    cvd: float    # 累计 delta


class CVDData(BaseModel):
    """CVD 数据集"""
    coin: str
    inst_type: str  # "CONTRACTS" | "SPOT"
    series: list[CVDPoint]
    trend_1h: str = ""  # "rising" | "declining" | "flat"
    delta_1h: float = 0
    has_divergence: bool = False
    divergence_note: str = ""


class OISnapshot(BaseModel):
    """OI 快照"""
    coin: str
    ts: int
    oi: float           # 张数
    oi_usd: float       # USD 计价
    source: str = "okx"


class OIData(BaseModel):
    """OI 分析结果"""
    coin: str
    ts: int
    current_usd: float
    change_1h_pct: float = 0
    change_5m_pct: float = 0
    trend: str = ""     # "surging" | "declining" | "stable"
    history: list[OISnapshot] = []


class FundingRateData(BaseModel):
    """资金费率"""
    coin: str
    ts: int
    okx_rate: Optional[float] = None
    binance_rate: Optional[float] = None
    avg_rate: float = 0
    next_funding_ts: int = 0
    interpretation: str = ""  # "多头拥挤" / "空头拥挤" / "中性"


class BasisData(BaseModel):
    """期现溢价"""
    coin: str
    ts: int
    mark_price: float
    index_price: float
    basis_pct: float  # (mark - index) / index * 100
    interpretation: str = ""


class TakerFlowData(BaseModel):
    """Taker 买卖力量"""
    coin: str
    ts: int
    buy_ratio: float   # 买入占比
    sell_ratio: float   # 卖出占比
    dominant: str = ""  # "buyers" | "sellers" | "balanced"
    spot_buy_vol: float = 0
    spot_sell_vol: float = 0
    contract_buy_vol: float = 0
    contract_sell_vol: float = 0
    spot_contract_divergence: bool = False


# ─── 新增数据模型（Phase 3） ───


class ExchangeFundingRate(BaseModel):
    """单交易所资金费率"""
    exchange: str
    current: Optional[float] = None
    avg_3d: Optional[float] = None
    avg_7d: Optional[float] = None
    avg_30d: Optional[float] = None


class MultiFundingRateData(BaseModel):
    """多交易所资金费率汇总"""
    coin: str
    ts: int
    exchanges: list[ExchangeFundingRate] = []
    avg_current: float = 0
    avg_7d: float = 0
    interpretation: str = ""


class LongShortRatioExchange(BaseModel):
    """单交易所多空比"""
    exchange: str
    long_pct: float
    short_pct: float
    ratio: float


class LongShortRatioData(BaseModel):
    """多空比汇总"""
    coin: str
    ts: int
    cycle: str = "1h"
    exchanges: list[LongShortRatioExchange] = []
    avg_ratio: float = 1.0
    interpretation: str = ""


class ETFFlowDay(BaseModel):
    """单日 ETF 流入流出"""
    date: str
    total_net: float
    detail: dict = {}


class ETFFlowData(BaseModel):
    """BTC ETF 资金流"""
    ts: int
    recent_days: list[ETFFlowDay] = []
    net_3d: float = 0
    trend: str = ""  # "inflow" | "outflow" | "mixed"


class GlobalLiquidationData(BaseModel):
    """全网爆仓统计"""
    ts: int
    long_1h_usd: float = 0
    short_1h_usd: float = 0
    long_24h_usd: float = 0
    short_24h_usd: float = 0
    ratio_1h: float = 1.0
    ratio_24h: float = 1.0
    largest_single_usd: float = 0


class MarketIndexItem(BaseModel):
    """market/index 单个指标"""
    key: str
    name: str
    value: float
    change_pct: Optional[float] = None


class MarketIndexData(BaseModel):
    """BBX market/index 精选指标集"""
    ts: int
    fear_greed: Optional[float] = None
    btc_dominance: Optional[float] = None
    btc_max_pain: Optional[float] = None
    btc_dvol: Optional[float] = None
    btc_put_call_oi: Optional[float] = None
    btc_mvrv: Optional[float] = None
    dxy: Optional[float] = None
    nasdaq: Optional[float] = None
    sp500: Optional[float] = None
    gold: Optional[float] = None
    binance_btc_balance: Optional[float] = None
    okx_ls_ratio_btc: Optional[float] = None
    binance_ls_ratio_btc: Optional[float] = None
    # A 级新增指标
    btc_hist_vol: Optional[float] = None
    btc_implied_vol: Optional[float] = None
    btc_iv_skew_1m: Optional[float] = None
    okx_btc_balance: Optional[float] = None
    bitfinex_btc_balance: Optional[float] = None
    coinbase_btc_balance: Optional[float] = None
    ahr999: Optional[float] = None
    usdt_market_cap: Optional[float] = None
    stablecoin_dominance: Optional[float] = None
    coinbase_btc_premium: Optional[float] = None
    usdt_otc_premium: Optional[float] = None
    btc_hashrate: Optional[float] = None
    us_10y_yield: Optional[float] = None
    fed_rate: Optional[float] = None
    raw_items: list[MarketIndexItem] = []
