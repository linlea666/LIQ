"""AI计算的关键价位模型：支撑、阻力、止损、入场"""

from __future__ import annotations

from pydantic import BaseModel


class PriceLevel(BaseModel):
    """通用价位"""
    price: float
    label: str          # "S1", "S2", "R1" 等
    level_type: str     # "support" | "resistance"
    strength: float     # 强度评分 (0-100)
    sources: list[str]  # 依据来源列表
    note: str = ""


class StopLossZone(BaseModel):
    """止损安全区"""
    direction: str      # "long" | "short" — 为做多还是做空设置的止损
    price: float
    zone_from: float
    zone_to: float
    reasons: list[str]
    atr_multiple: float = 0


class EntryZone(BaseModel):
    """最佳入场观察区"""
    direction: str      # "long" | "short"
    price_from: float
    price_to: float
    confluence_sources: list[str]
    confirmation_note: str = ""


class PinRiskZone(BaseModel):
    """插针高危区"""
    price: float
    side: str           # "above" | "below"
    liq_amount_usd: float
    note: str = ""


class SniperEntry(BaseModel):
    """狙击挂单：极端R:R入场点（小亏大赚哲学）"""
    direction: str          # "long" | "short"
    entry_price: float
    stop_loss: float
    take_profit_1: float
    take_profit_2: float
    rr_ratio_1: float
    rr_ratio_2: float
    risk_usd_per_unit: float
    cluster_usd: float
    logic: list[str]


class LevelAnalysis(BaseModel):
    """完整价位分析结果"""
    coin: str
    ts: int
    current_price: float
    supports: list[PriceLevel]
    resistances: list[PriceLevel]
    stop_loss_zones: list[StopLossZone]
    entry_zones: list[EntryZone]
    pin_risk_zones: list[PinRiskZone]
    sniper_entries: list[SniperEntry] = []
