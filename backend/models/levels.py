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


class LadderEntry(BaseModel):
    """阶梯埋伏单：单层挂单"""
    tier: int               # 阶梯层级 (1=最近, N=最远)
    entry_price: float
    stop_loss: float
    take_profit: float
    rr_ratio: float
    position_weight: float  # 仓位权重 (越低层越大, 0-1之间, 全部层加总=1)
    risk_pct: float         # 该层止损占总预算的百分比
    zone_label: str         # 所在区域描述
    entry_logic: list[str]  # 挂单位置逻辑
    invalidation: str       # 失效条件


class LadderPlan(BaseModel):
    """阶梯式底部埋伏计划（Scaled-In Limit Order Strategy）"""
    direction: str              # "long" (底部埋伏做多) | "short" (顶部埋伏做空)
    tier_count: int             # 阶梯层数
    entries: list[LadderEntry]
    total_risk_pct: float       # 全部层止损总风险占账户百分比
    best_case_rr: float         # 最佳情况 R:R (最低层吃到底反弹)
    worst_case_loss_pct: float  # 最差情况: 全部扫损亏损百分比
    expected_edge: str          # 期望优势描述
    plan_summary: str           # 计划概要
    coverage_range: str         # 覆盖价格范围描述


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
    ladder_plans: list[LadderPlan] = []
