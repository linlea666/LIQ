"""清算数据模型：清算地图(多周期)、热力图(3模型)、最大痛点、爆仓事件"""

from __future__ import annotations

from typing import Optional

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
    distance_pct: float = 0


class VacuumZone(BaseModel):
    """清算真空区"""
    price_from: float
    price_to: float
    midpoint: float = 0
    note: str = ""


class LiquidationMap(BaseModel):
    """完整清算地图（支持多周期）"""
    coin: str
    ts: int
    cycle: str  # "1d" | "3d" | "7d" | "30d"
    leverage_groups: list[LiqLeverageGroup]
    clusters_above: list[LiqCluster] = []
    clusters_below: list[LiqCluster] = []
    vacuum_zones: list[VacuumZone] = []
    imbalance_ratio: float = 0
    exchange: str = ""  # 空=聚合所有交易所


class LiquidationEvent(BaseModel):
    """单笔爆仓事件"""
    coin: str
    ts: int
    side: str  # "long" | "short"
    price: float
    size: float
    size_usd: float = 0
    source: str = "coinglass"
    exchange: str = ""


class LiquidationStats(BaseModel):
    """爆仓统计"""
    coin: str
    ts: int
    period_min: int = 30
    long_total_usd: float = 0
    short_total_usd: float = 0
    long_count: int = 0
    short_count: int = 0
    ratio: float = 0


# ── 新增：清算热力图 ──

class HeatmapDataPoint(BaseModel):
    """清算热力图单点"""
    price: float
    value: float  # 清算量（USD）
    ts: int = 0


class HeatmapData(BaseModel):
    """清算热力图（支持 model1/model2/model3）"""
    coin: str
    ts: int
    model: int = 1  # 1 | 2 | 3
    range: str = "24h"  # "12h" | "24h" | "3d" | "7d" | "30d" | "3m" | "6m" | "1y"
    exchange: str = ""
    data: list[HeatmapDataPoint] = []


# ── 新增：清算最大痛点 ──

class LiqMaxPainItem(BaseModel):
    """单个币种清算最大痛点"""
    symbol: str
    price: float
    long_liq_usd: float = 0
    short_liq_usd: float = 0


class LiqMaxPainData(BaseModel):
    """清算最大痛点数据"""
    ts: int
    range: str = "24h"
    items: list[LiqMaxPainItem] = []


# ── 新增：爆仓历史（聚合） ──

class LiqHistoryPoint(BaseModel):
    """爆仓历史单点"""
    ts: int
    long_usd: float = 0
    short_usd: float = 0
    long_count: int = 0
    short_count: int = 0


class LiqHistoryData(BaseModel):
    """爆仓历史数据"""
    coin: str
    exchange: str = ""
    interval: str = "1h"
    data: list[LiqHistoryPoint] = []
