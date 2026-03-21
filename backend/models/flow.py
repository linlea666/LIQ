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
