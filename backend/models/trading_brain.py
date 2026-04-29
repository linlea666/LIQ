"""交易大脑大屏 · 统一 PriceZone 视图（只读聚合，不重新打分）。

本模块仅为「可视化与证据链」服务：
- 不修改 KeyLevelV2.final_score / strength_tier / cascade_risk
- 不修改 WallZone 任何评分字段
- 区块内评分一律复用引擎既有字段；仅做展示层映射与中文证据摘要
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


class BrainZoneRoles(BaseModel):
    """价格区角色标记（多层可同时为 True）。"""

    key_level: bool = False
    """该价区包含关键位 V2。"""

    spot_supply_wall: bool = False
    """现货供需墙（高可信支撑/阻力候选，非指令）。"""

    futures_liquidity_wall: bool = False
    """合约订单簿流动性墙（杠杆侧挂单，易被扫/短线压制）。"""

    liquidation_magnet: bool = False
    """清算磁铁/痛点/密集区（扫单目标，非支撑阻力）。"""

    coinbase_confluence: bool = False
    """Coinbase 现货同价区共振（独立机构链路证据）。"""


class BrainScenario(BaseModel):
    """情景提示（观察框架，不含交易指令）。"""

    if_hold: str = ""
    if_break: str = ""
    invalidates_if: str = ""


DominantRole = Literal[
    "spot_defense",
    "futures_target",
    "liquidation_magnet",
    "contested",
    "key_level_only",
    "other",
]
"""价格区主导角色（语义分层，UI 颜色映射，禁喊单）：

- spot_defense：现货供需墙 / Coinbase 共振 / 关键位 ≥A，更适合作支撑/阻力候选
- futures_target：合约挂单墙 + 清算磁铁，更易被扫；目标位 / 不入场区
- liquidation_magnet：纯清算簇/磁铁，磁吸目标位
- contested：同价区 spot 与 futures+liq 共存，争夺区（建议观察）
- key_level_only：仅有关键位证据
- other：其他/弱聚合
"""


class BrainPriceZone(BaseModel):
    """大屏核心单元：统一价格区。"""

    zone_id: str
    coin: str
    price_low: float
    price_high: float
    price_mid: float
    distance_pct: float

    roles: BrainZoneRoles = Field(default_factory=BrainZoneRoles)
    dominant_label: str = ""
    """主导标签（人类可读，如「多源争夺区」「清算磁铁」）。"""

    dominant_role: DominantRole = "other"
    """主导角色（语义分层；前端按此上色 + 排行分桶）。"""

    wall_zone_ids: list[str] = Field(default_factory=list)
    key_level_prices: list[float] = Field(default_factory=list)

    support_trust: float = 0.0
    resistance_trust: float = 0.0
    sweep_attractiveness: float = 0.0
    break_through_risk: float = 0.0
    data_confidence: float = 0.0

    evidence: list[str] = Field(default_factory=list)
    scenario: BrainScenario = Field(default_factory=BrainScenario)

    # 分层展示用（可选）
    layer_notes: list[str] = Field(default_factory=list)


class BrainRankings(BaseModel):
    """各类排行（存 zone_id，便于前端联动）。"""

    support_trust: list[str] = Field(default_factory=list)
    resistance_trust: list[str] = Field(default_factory=list)
    sweep_targets: list[str] = Field(default_factory=list)
    break_through_risk: list[str] = Field(default_factory=list)
    # Phase 1：按 dominant_role 分桶的排行
    top_defenses: list[str] = Field(default_factory=list)
    """spot_defense 角色，按 max(support_trust, resistance_trust) 排序。"""
    top_targets: list[str] = Field(default_factory=list)
    """futures_target / liquidation_magnet，按 sweep_attractiveness 排序。"""
    top_contested: list[str] = Field(default_factory=list)
    """contested 角色，按 (support_trust + sweep_attractiveness) 综合排序。"""


class BrainEvent(BaseModel):
    """统一事件流条目。"""

    ts: int
    layer: Literal["spot", "futures", "liquidation", "key_level", "system"]
    price_mid: float = 0.0
    zone_id: str = ""
    message: str
    source: str = ""


class BrainDataQuality(BaseModel):
    """数据质量汇总（必须展示）。"""

    liquidity_wall_quality: str = ""
    usd_usdt_basis_pct: Optional[float] = None
    overall_freshness_score: Optional[float] = None
    stale_sources: list[str] = Field(default_factory=list)
    missing_sources: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    # 暖机/数据齐备度（Phase 0）
    is_partial_ready: bool = False
    """True 表示尚未拿全核心源（KL/挂单墙/清算 任一缺失），UI 应显示 banner。"""
    ready_count: int = 0
    """已就绪的核心源数量（最大 = total_count）。"""
    total_count: int = 3
    """核心源总数：当前=3（KL、挂单墙、清算地图）。"""


class BrainContextChips(BaseModel):
    """顶栏情境 chips（无交易指令）。"""

    regime: str = ""
    regime_description: str = ""
    oi_delta_1h_pct: Optional[float] = None
    funding_interpretation: str = ""
    cvd_contract_trend: str = ""
    cvd_spot_trend: str = ""
    nearest_magnet_above: Optional[float] = None
    nearest_magnet_below: Optional[float] = None


class TradingBrainSnapshot(BaseModel):
    """单币交易大脑聚合快照（GET /api/trading-brain/{coin}）。"""

    coin: str
    ts: int
    last_price: float
    atr: float = 0.0

    summary: str = ""
    context: BrainContextChips = Field(default_factory=BrainContextChips)

    zones: list[BrainPriceZone] = Field(default_factory=list)
    rankings: BrainRankings = Field(default_factory=BrainRankings)
    events: list[BrainEvent] = Field(default_factory=list)
    data_quality: BrainDataQuality = Field(default_factory=BrainDataQuality)
