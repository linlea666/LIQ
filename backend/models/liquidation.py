"""清算地图数据模型"""

from __future__ import annotations

from pydantic import BaseModel


class LiqBand(BaseModel):
    """单个清算价格区间"""
    price_from: float
    price_to: float
    turnover_usd: float


class LiqLeverageGroup(BaseModel):
    """某杠杆倍数下的清算分布"""
    leverage: str  # "10", "25", "50", "100"
    short_bands: list[LiqBand] = []
    long_bands: list[LiqBand] = []
    short_total_usd: float = 0
    long_total_usd: float = 0


class LiqCluster(BaseModel):
    """清算密集区（跨杠杆聚合后）"""
    price_center: float
    price_from: float
    price_to: float
    total_usd: float
    side: str  # "long" | "short" — 指会被清算的方向
    dominant_leverage: str = ""
    distance_pct: float = 0  # 距当前价格的百分比


class VacuumZone(BaseModel):
    """清算真空区：上下无密集清算的区域"""
    price_from: float
    price_to: float
    midpoint: float = 0
    note: str = ""


class LiquidationMap(BaseModel):
    """完整清算地图"""
    coin: str
    ts: int
    cycle: str  # "24h" | "7d"
    leverage_groups: list[LiqLeverageGroup]
    clusters_above: list[LiqCluster] = []
    clusters_below: list[LiqCluster] = []
    vacuum_zones: list[VacuumZone] = []
    imbalance_ratio: float = 0  # >1偏多头清算多(看空)，<1偏空头清算多(看多)


class LiquidationEvent(BaseModel):
    """单笔实时爆仓事件"""
    coin: str
    ts: int
    side: str  # "long" | "short" — 被爆的方向
    price: float
    size: float
    size_usd: float = 0
    source: str = "okx"


class LiquidationStats(BaseModel):
    """爆仓统计"""
    coin: str
    ts: int
    period_min: int = 30
    long_total_usd: float = 0
    short_total_usd: float = 0
    long_count: int = 0
    short_count: int = 0
    ratio: float = 0  # long/short
