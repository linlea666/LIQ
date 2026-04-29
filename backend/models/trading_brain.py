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


SetupType = Literal[
    "support_limit_probe",
    "resistance_limit_probe",
    "fake_break_reclaim_long",
    "fake_break_reclaim_short",
]
"""支持的 setup 类型（MVP，禁喊单；UI 用"做多/做空观察 / 等待"措辞）：

- support_limit_probe       ：防守位限价试错（做多观察）
- resistance_limit_probe    ：阻力位限价试错（做空观察）
- fake_break_reclaim_long   ：扫破支撑后收回（做多观察）
- fake_break_reclaim_short  ：扫破阻力后收回（做空观察）
"""

SetupDirection = Literal["long", "short", "neutral"]
"""后端方向（UI 必须转译为「做多观察 / 做空观察 / 等待」）。"""

SetupStateName = Literal[
    "forming",
    "waiting_for_trigger",
    "triggered",
    "confirmation_pending",
    "confirmed",
    "invalidated",
    "cancelled",
    "missed",
    "cooldown",
]


class SetupEntryStyle(BaseModel):
    """入场方案（不含交易指令；只是观察区间）。"""

    style: Literal["aggressive", "conservative"]
    entry_zone: tuple[float, float]
    """[low, high]，价格落入即视为触达；UI 显示为观察区间。"""
    requires: list[str] = Field(default_factory=list)
    """触发前需满足的前置条件（中文白话）。"""
    risk_note: str = ""
    """风格的风险提示（"易被扫"/"可能错过"等）。"""


class SetupTarget(BaseModel):
    """目标观察位。"""

    price: float
    type: str
    """目标性质："nearest_resistance" / "spot_wall" / "short_liq_magnet" 等。"""
    rr: float
    """相对硬止损的盈亏比（仅展示）。"""
    note: str = ""


class SetupRiskPlan(BaseModel):
    """失效结构：软失效 + 硬止损（无下单指令）。"""

    soft_invalidation: float
    """软失效价：跌破/突破但允许快速收回；不立即判死。"""
    hard_stop: float
    """硬止损价：结构失败的明确价位。"""
    structural_invalidation: str = ""
    """结构性失效条件（如「1h 收盘跌破 X 且无法收回」）。"""
    stop_logic: list[str] = Field(default_factory=list)


class SetupState(BaseModel):
    """状态机当前态 + 简短历史。"""

    name: SetupStateName = "forming"
    since_ts: int = 0
    pending_reason: str = ""
    """当处于 forming/waiting 时，说明「在等什么」。"""
    history: list[dict] = Field(default_factory=list)
    """最近 5 条状态变迁：[{ts, from, to, reason}]。"""


class TradeSetupCandidate(BaseModel):
    """高盈亏比观察区候选（不输出交易指令；UI 严格用观察措辞）。"""

    setup_id: str
    coin: str
    zone_id: str
    """关联的 BrainPriceZone.zone_id。"""
    setup_type: SetupType
    direction: SetupDirection
    """后端方向；UI 必须转译："long"→「做多观察」, "short"→「做空观察」, "neutral"→「等待」。"""

    entry_styles: list[SetupEntryStyle] = Field(default_factory=list)
    risk_plan: SetupRiskPlan
    targets: list[SetupTarget] = Field(default_factory=list)

    asymmetry_score: float = 0.0
    """不对称评分（0–1，亏少赚多）。"""
    opportunity_score: float = 0.0
    """机会综合评分（0–1）。"""
    data_confidence: float = 0.0
    """承袭自 zone.data_confidence。"""

    state: SetupState = Field(default_factory=SetupState)
    cancel_conditions: list[str] = Field(default_factory=list)
    """挂单/观察取消条件（中文白话；前端必须展示）。"""

    evidence: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


SpotBookBracket = Literal["near", "mid", "far"]
"""现货订单簿距离档位（按 |distance_pct| 分桶；与合约堆积模块共用语义）：

- near : ≤ 0.5%      —— 即时关注（短线）
- mid  : 0.5%–2.0%   —— 中短期仓位区
- far  : 2.0%–5.0%   —— 战略观察 / 远端流动性
"""


class BrainSpotBookItem(BaseModel):
    """现货订单簿展示项（基于 WallZone 抽取，不重新评分）。

    数据完全来自 OrderbookPressureSnapshot.walls_above / walls_below；
    此模型只做"按距离分桶 + 拆现货/合约厚度"的视图整理。
    """

    wall_zone_id: str
    side: Literal["bid", "ask"]
    """bid = 买墙 (下方支撑) / ask = 卖墙 (上方阻力)。"""
    price: float
    """以 peak_price 表示（厚度峰值价位）。"""
    distance_pct: float
    """带符号距离百分比，正=上方、负=下方。"""
    bracket: SpotBookBracket

    total_usd: float
    """整段墙体当前帧 USD 厚度（含合约+现货融合）。"""
    spot_usd: float
    """现货侧 USD 厚度 (= spot_current_usd + coinbase_spot_usd)；越高 → 越是真买卖家。"""
    futures_usd: float
    """合约侧 USD 厚度 (= max(total_usd - spot_usd, 0))。"""

    is_dual_source: bool = False
    """合约+现货同价区共振（trust 阶梯加分最强证据）。"""
    has_coinbase: bool = False
    """Coinbase 现货独立链路共振（机构资金 footprint）。"""
    trust_score: float = 0.0
    """0–1 综合可信度，沿用 WallZone.trust_score。"""
    strength_tier: Literal["S", "A", "B", "C"] = "C"
    dominant_role: str = "ordinary"
    """沿用 WallZone.dominant_role（true_support / sweep_target / ordinary 等）。"""


class BrainSpotBook(BaseModel):
    """现货订单簿模块输出（按距离分层）。"""

    asks: list[BrainSpotBookItem] = Field(default_factory=list)
    """上方卖墙；按距离升序（先近后远）。"""
    bids: list[BrainSpotBookItem] = Field(default_factory=list)
    """下方买墙；按距离升序绝对值（先近后远）。"""
    bracket_caps: dict[str, int] = Field(
        default_factory=lambda: {"near": 8, "mid": 8, "far": 6}
    )
    """各档位每侧上限（前端展示用，便于折叠/分页）。"""
    notes: list[str] = Field(default_factory=list)


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
    opportunities: list[TradeSetupCandidate] = Field(default_factory=list)
    """Phase 2：从 zones 派生的高盈亏比观察区候选；前端右侧机会雷达消费。"""

    spot_book: Optional[BrainSpotBook] = None
    """Phase B：现货订单簿模块（按近/中/远分层；不重打分，仅视图整理）。"""
