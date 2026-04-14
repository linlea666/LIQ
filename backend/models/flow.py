"""资金流数据模型：CVD、OI、资金费率、期现溢价、多空比、ETF、宏观指标等"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel


class CVDPoint(BaseModel):
    """CVD 单个数据点"""
    ts: int
    buy_vol: float
    sell_vol: float
    delta: float
    cvd: float


class CVDData(BaseModel):
    """CVD 数据集"""
    coin: str
    inst_type: str  # "CONTRACTS" | "SPOT"
    series: list[CVDPoint]
    trend_1h: str = ""
    delta_1h: float = 0
    has_divergence: bool = False
    divergence_note: str = ""


class OISnapshot(BaseModel):
    """OI 快照"""
    coin: str
    ts: int
    oi: float
    oi_usd: float
    source: str = "coinglass"


class OIData(BaseModel):
    """OI 分析结果"""
    coin: str
    ts: int
    current_usd: float
    change_1h_pct: float = 0
    change_5m_pct: float = 0
    trend: str = ""
    history: list[OISnapshot] = []


class FundingRateData(BaseModel):
    """资金费率"""
    coin: str
    ts: int
    okx_rate: Optional[float] = None
    binance_rate: Optional[float] = None
    avg_rate: float = 0
    oi_weighted_rate: float = 0
    next_funding_ts: int = 0
    interpretation: str = ""


class BasisData(BaseModel):
    """期现溢价"""
    coin: str
    ts: int
    mark_price: float
    index_price: float
    basis_pct: float
    interpretation: str = ""


class TakerFlowData(BaseModel):
    """Taker 买卖力量"""
    coin: str
    ts: int
    buy_ratio: float
    sell_ratio: float
    dominant: str = ""
    spot_buy_vol: float = 0
    spot_sell_vol: float = 0
    contract_buy_vol: float = 0
    contract_sell_vol: float = 0
    spot_contract_divergence: bool = False


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
    oi_weighted: float = 0
    interpretation: str = ""


class LongShortRatioExchange(BaseModel):
    """单交易所多空比"""
    exchange: str
    long_pct: float
    short_pct: float
    ratio: float


class LongShortRatioData(BaseModel):
    """多空比汇总（支持三维度）"""
    coin: str
    ts: int
    cycle: str = "1h"
    dimension: str = "global"  # "global" | "top_account" | "top_position"
    exchanges: list[LongShortRatioExchange] = []
    avg_ratio: float = 1.0
    interpretation: str = ""


class ETFFlowDay(BaseModel):
    """单日 ETF 流入流出"""
    date: str
    total_net: float
    detail: dict = {}


class ETFFlowData(BaseModel):
    """ETF 资金流（支持 BTC/ETH/SOL/XRP）"""
    ts: int
    asset: str = "BTC"
    recent_days: list[ETFFlowDay] = []
    net_3d: float = 0
    trend: str = ""


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
    """市场指标单项"""
    key: str
    name: str
    value: float
    change_pct: Optional[float] = None


class MarketIndexData(BaseModel):
    """市场指标集"""
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


class OnchainCycleData(BaseModel):
    """链上周期指标（Coinglass 直取）"""
    ts: int
    sma_200w: Optional[float] = None
    mvrv_z: Optional[float] = None
    mvrv_market_cap: Optional[float] = None
    mvrv_realized_cap: Optional[float] = None
    sth_cost_1d: Optional[float] = None
    sth_cost_1w: Optional[float] = None
    sth_cost_1m: Optional[float] = None
    sth_cost_3m: Optional[float] = None
    ahr999: Optional[float] = None
    pi_111dma_x2: Optional[float] = None
    pi_350dma: Optional[float] = None
    cvdd: Optional[float] = None
    btc_daily_prices: list[float] = []
    # Coinglass 新增
    puell_multiple: Optional[float] = None
    nupl: Optional[float] = None
    rhodl_ratio: Optional[float] = None
    reserve_risk: Optional[float] = None
    sth_sopr: Optional[float] = None
    lth_sopr: Optional[float] = None


class CyclePositionData(BaseModel):
    """BTC 周期位置评分 (CPS 0~10) + 链上关键价位"""
    ts: int
    cps: float
    cps_label: str
    mvrv_z_score: Optional[float] = None
    mvrv_z_contribution: float = 0
    ahr999_value: Optional[float] = None
    ahr999_contribution: float = 0
    price_vs_200w_ratio: Optional[float] = None
    price_vs_200w_contribution: float = 0
    price_vs_sth_label: str = ""
    price_vs_sth_contribution: float = 0
    pi_cycle_ratio: Optional[float] = None
    pi_cycle_contribution: float = 0
    rplr_proxy: Optional[float] = None
    btc_rsi_daily: Optional[float] = None
    sma_200w: Optional[float] = None
    sth_cost_1d: Optional[float] = None
    sth_cost_1w: Optional[float] = None
    sth_cost_1m: Optional[float] = None
    sth_cost_3m: Optional[float] = None
    pi_350dma: Optional[float] = None
    pi_111dma_x2: Optional[float] = None
    cvdd: Optional[float] = None


class RangeSignalData(BaseModel):
    """多时间框架均线箱体 + MACD 位置 + 信号分级"""
    ts: int
    ma60_daily: Optional[float] = None
    ma120_daily: Optional[float] = None
    ma60_weekly: Optional[float] = None
    macd_daily_above_zero: Optional[bool] = None
    macd_daily_histogram: Optional[float] = None
    macd_daily_hist_rising: Optional[bool] = None
    range_upper: Optional[float] = None
    range_upper_source: str = ""
    range_lower: Optional[float] = None
    range_lower_source: str = ""
    price_position: str = "middle"
    price_position_pct: float = 50.0
    unfilled_wick_low: Optional[float] = None
    unfilled_wick_high: Optional[float] = None
    signal_grade: Optional[str] = None
    signal_direction: Optional[str] = None
    signal_reason: str = ""
    sweep_confirmed: bool = False
    cps_aligned: bool = False


# ── 新增数据模型 ──

class NetPositionPoint(BaseModel):
    """净多空头寸单点"""
    ts: int
    net_position: float = 0


class NetPositionData(BaseModel):
    """净多空头寸"""
    coin: str
    exchange: str
    data: list[NetPositionPoint] = []


class BasisHistoryPoint(BaseModel):
    ts: int
    basis: float = 0
    basis_pct: float = 0


class BasisHistoryData(BaseModel):
    coin: str
    exchange: str
    data: list[BasisHistoryPoint] = []


class SpotCVDData(BaseModel):
    """现货 CVD"""
    coin: str
    series: list[CVDPoint] = []
    trend_1h: str = ""
    delta_1h: float = 0


class FuturesSpotVolRatioPoint(BaseModel):
    ts: int
    ratio: float = 0


class FuturesSpotVolRatioData(BaseModel):
    symbol: str
    data: list[FuturesSpotVolRatioPoint] = []
