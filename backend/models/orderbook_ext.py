"""扩展订单簿数据模型：大单追踪、热力图、足迹图"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel


class LargeOrder(BaseModel):
    """大额限价挂单"""
    ts: int
    exchange: str
    symbol: str
    price: float
    size_usd: float
    side: str  # "ask" | "bid"
    status: str = "active"  # "active" | "filled" | "cancelled"


class LargeOrderSnapshot(BaseModel):
    """大单追踪快照"""
    symbol: str
    ts: int
    orders: list[LargeOrder] = []
    total_bid_usd: float = 0
    total_ask_usd: float = 0


class OrderbookHeatmapPoint(BaseModel):
    """订单簿热力图单点"""
    ts: int
    price: float
    size: float


class OrderbookHeatmapData(BaseModel):
    """订单簿历史热力图"""
    symbol: str
    exchange: str
    data: list[OrderbookHeatmapPoint] = []


class FootprintBar(BaseModel):
    """足迹图单根"""
    ts: int
    open: float = 0
    high: float = 0
    low: float = 0
    close: float = 0
    buy_vol: float = 0
    sell_vol: float = 0
    delta: float = 0


class FootprintData(BaseModel):
    """足迹图数据"""
    symbol: str
    exchange: str
    interval: str = "1h"
    bars: list[FootprintBar] = []
