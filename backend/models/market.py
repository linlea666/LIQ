"""市场基础数据模型：K线、订单簿、成交、Ticker"""

from __future__ import annotations

from pydantic import BaseModel, Field


class CandleData(BaseModel):
    """单根K线"""
    coin: str
    ts: int
    open: float = Field(alias="o")
    high: float = Field(alias="h")
    low: float = Field(alias="l")
    close: float = Field(alias="c")
    vol: float = 0
    vol_ccy: float = 0

    model_config = {"populate_by_name": True}


class WallInfo(BaseModel):
    """买卖墙信息。

    注：生产环境下 walls 的填充源是 Coinglass 大单追踪（large_orders），
    不是订单簿档位聚合。`size` 字段为占位，`size_usd` 才是真实指标。
    详见 engine._build_ai_snapshot 中的回填逻辑。
    """
    price: float
    size: float
    size_usd: float = 0
    order_count: int = 0


class OrderBookAnalysis(BaseModel):
    """订单簿分析结果。

    数据来源组合：
    - `bid_total_usd / ask_total_usd / spread_pct`：Coinglass 聚合深度（多交易所）
    - `bid_walls / ask_walls`：由 Coinglass 大单追踪 (large_orders) 回填，非订单簿档位
    """
    coin: str
    ts: int
    bid_walls: list[WallInfo] = []
    ask_walls: list[WallInfo] = []
    bid_total_usd: float = 0
    ask_total_usd: float = 0
    spread_pct: float = 0


class TradeData(BaseModel):
    """单笔成交"""
    coin: str
    ts: int
    price: float
    size: float
    side: str  # "buy" | "sell"
    source: str = "okx"


class TickerData(BaseModel):
    """行情摘要"""
    coin: str
    ts: int
    last: float
    high_24h: float
    low_24h: float
    vol_24h: float = 0
    change_24h: float = 0
    change_pct_24h: float = 0


class VolumeProfileBin(BaseModel):
    """Volume Profile 单个价格区间"""
    price_low: float
    price_high: float
    volume: float
    buy_volume: float = 0
    sell_volume: float = 0


class VolumeProfileData(BaseModel):
    """Volume Profile 分析结果"""
    coin: str
    ts: int
    bins: list[VolumeProfileBin]
    poc_price: float  # Point of Control
    value_area_high: float
    value_area_low: float
    vwap: float
