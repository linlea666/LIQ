"""市场动作分析器（Market Action Analyzer）数据模型

本模块实现"基于真实市场动作"的场景识别：
- 输入（`MarketActionFacts`）：14 字段严格锁定，来自 polls 层数据
- 输出（`MarketActionReport`）：AI Arbiter 给出的 6 块结构化结论

设计原则（已与用户拍板）：
1. 只收真实反映市场动作的指标（剔除宏观叙事/零售情绪）
2. 场景枚举 9 种，AI 必须落在其中一类
3. 数据缺失不崩，通过 `data_quality` + `missing` 降级
4. 所有字段对 SOL 兼容（期权维度 SOL=None）
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

# ────────────────────────────────────────────────────────────────────────────
# S 级 · 核心 6 项（facts 输入）
# ────────────────────────────────────────────────────────────────────────────

class PriceSnapshot(BaseModel):
    """S1 · 价格 + 多周期涨跌"""
    last: float
    change_1h_pct: Optional[float] = None
    change_4h_pct: Optional[float] = None
    change_24h_pct: Optional[float] = None
    high_24h: Optional[float] = None
    low_24h: Optional[float] = None
    recent_bars_1h: list[list[float]] = Field(default_factory=list)
    # 每根 bar = [ts, open, high, low, close, volume]，近 6 根


class OISnapshot(BaseModel):
    """S2 · OI 规模 + 变化率 + 历史分位

    派生字段（基于 30d hourly 历史）：
      - percentile_30d_hourly：当前 OI 在 30d（按 1h 采样）中的百分位（0-100）
      - is_near_local_high_7d：当前 OI ≥ 近 7d 最高值的 98%
    两项均为真实历史采样计算，无推算成分；若历史样本不足会返回 None。
    """
    current_usd: float
    change_5m_pct: Optional[float] = None
    change_1h_pct: Optional[float] = None
    change_24h_pct: Optional[float] = None
    trend: Optional[str] = None  # "rising" / "declining" / "flat"
    # ── P0 派生字段（基于 30d hourly 真实采样） ──
    percentile_30d_hourly: Optional[float] = None
    is_near_local_high_7d: Optional[bool] = None
    history_sample_size: Optional[int] = None  # 透明度：实际使用的样本点数


class FundingSnapshot(BaseModel):
    """S3 · Funding + 多交易所分散度 + 资金费成本/持续性

    派生字段（基于 7d × 8h 结算点真实采样）：
      - hourly_cost_usd：按当前费率 × 当前 OI / 8 估算的每小时成本（美元）
      - cost_24h_usd：近 24h 累计成本（美元，**基于当前 OI 近似**，误差约 ±OI 24h 变化率）
      - days_negative_streak：最新连续负费率天数（0 表示最新为正）
      - sign_flip_7d：近 7d 均费率符号相对前 7d 是否翻转（bool）
    均为采样 + 算术派生，无任何推算；历史样本不足时返回 None。
    """
    avg_current: float
    avg_7d: Optional[float] = None
    oi_weighted: Optional[float] = None
    interpretation: Optional[str] = None
    exchange_count: int = 0
    dispersion_abs: Optional[float] = None
    # 各交易所 current 的标准差，反映是否一致
    # ── P0 派生字段（基于 7d × 8h 真实采样） ──
    hourly_cost_usd: Optional[float] = None
    cost_24h_usd: Optional[float] = None
    days_negative_streak: Optional[float] = None
    sign_flip_7d: Optional[bool] = None
    history_sample_size: Optional[int] = None  # 透明度：实际使用的 8h 点数（上限 21）


class CVDSnapshot(BaseModel):
    """S4/S5 · CVD 合约或现货（可贴附短窗 netflow）"""
    delta_1h: float
    trend_1h: Optional[str] = None  # rising / declining / flat
    has_divergence: bool = False
    divergence_note: Optional[str] = None
    recent_delta_5m: list[float] = Field(default_factory=list)
    # 最近 6 个 5m bar 的 delta（buy - sell），用于看瞬时方向


class LiquidationSnapshot(BaseModel):
    """S6 · 全网清算流"""
    long_1h_usd: float
    short_1h_usd: float
    long_24h_usd: float
    short_24h_usd: float
    ratio_1h: float = 1.0
    dominant_side_1h: Literal["long_being_liquidated", "short_being_liquidated", "balanced"] = "balanced"


# ────────────────────────────────────────────────────────────────────────────
# A 级 · 关键区分 9 项
# ────────────────────────────────────────────────────────────────────────────

class BasisSnapshot(BaseModel):
    """A1 · 期现溢价 + 趋势"""
    basis_pct: float
    basis_trend: Literal["widening", "narrowing", "stable"] = "stable"
    # 近 1h 对比
    recent_values: list[float] = Field(default_factory=list)  # 最近 12 点（1h，5m 间隔）
    interpretation: Optional[str] = None


class OrderbookSnapshot(BaseModel):
    """A2 · 盘口挂单失衡度 + 趋势

    ⚠ 命名澄清：这里的字段**不是** bid-ask spread（点差），而是**挂单失衡度**
    （(ask - bid) / avg × 100）。历史上曾用 `spread_pct` 命名，容易被误解为点差，
    P0-3 起改用语义准确的 `book_imbalance_pct`。

    - `book_imbalance_pct`：负数 = bid 侧挂单更厚（潜在支撑强）；正数 = ask 更厚
    - `imbalance_trend`：挂单失衡度的**绝对值**趋势（widening=失衡加剧，
      narrowing=趋向均衡，stable=基本不变）
    - `recent_imbalances`：最近 12 点（5m 间隔）的 book_imbalance_pct 序列
    """
    bid_total_usd: float
    ask_total_usd: float
    book_imbalance_pct: float
    imbalance_trend: Literal["widening", "narrowing", "stable"] = "stable"
    recent_imbalances: list[float] = Field(default_factory=list)


class LiqClusterSnapshot(BaseModel):
    """A3 · 清算图上下簇对比"""
    above_cluster_usd: float = 0
    below_cluster_usd: float = 0
    above_nearest_price: Optional[float] = None
    below_nearest_price: Optional[float] = None
    above_distance_pct: Optional[float] = None
    below_distance_pct: Optional[float] = None
    bias: Literal["short_squeeze_fuel", "long_squeeze_fuel", "balanced", "unknown"] = "unknown"


class LiqSweepSnapshot(BaseModel):
    """A4 · 清算热区连续触发"""
    recent_sweeps_count: int = 0  # 近 N 分钟被触发的次数
    recent_window_min: int = 30
    last_sweep_ts: Optional[int] = None
    last_sweep_side: Optional[Literal["long_side", "short_side"]] = None
    continuous_trigger: bool = False
    # 连续 3+ 次同方向


class PriceContextSnapshot(BaseModel):
    """A8 · 价格位置上下文（swing + 区间 + POC）"""
    swing_high_1h: Optional[float] = None   # 最近 20 根 1h 的 swing high
    swing_low_1h: Optional[float] = None
    range_20d_high: Optional[float] = None
    range_20d_low: Optional[float] = None
    range_position_pct: Optional[float] = None  # 0(下沿) ~ 100(上沿)
    poc_price: Optional[float] = None            # Volume Profile POC
    vah_price: Optional[float] = None
    val_price: Optional[float] = None
    price_vs_poc: Optional[Literal["above", "below", "at"]] = None
    distance_to_swing_high_pct: Optional[float] = None
    distance_to_swing_low_pct: Optional[float] = None


# ────────────────────────────────────────────────────────────────────────────
# A9 · Footprint（足迹图派生，合约+现货）
# ────────────────────────────────────────────────────────────────────────────

class FootprintBarStats(BaseModel):
    """单根 K 线的足迹统计（派生，不含原始 buckets）"""
    ts: int
    total_buy_usd: float = 0
    total_sell_usd: float = 0
    delta_usd: float = 0
    delta_pct: float = 0  # delta / total，±1.0 区间
    poc_price: Optional[float] = None
    # 这根 K 线内成交最密集的价位
    top_imbalance_zones: list[dict] = Field(default_factory=list)
    # [{price: 78150, buy: 12000, sell: 800, ratio: 15.0, side: "stacked_buy"}, ...]
    # 只保留 ratio > 3 的强失衡价位
    high_price_delta_pct: Optional[float] = None
    # K 线上 1/3 价位区的 delta_pct（用于衰竭识别）
    low_price_delta_pct: Optional[float] = None
    # K 线下 1/3 价位区的 delta_pct


class FootprintSnapshot(BaseModel):
    """A9 · 合约+现货足迹图派生摘要"""
    contract_latest: Optional[FootprintBarStats] = None
    contract_prev: Optional[FootprintBarStats] = None
    spot_latest: Optional[FootprintBarStats] = None
    spot_prev: Optional[FootprintBarStats] = None
    # 期现 delta 差（最新 K 线）
    spot_contract_delta_diff_pct: Optional[float] = None
    # spot.delta_pct - contract.delta_pct，反映期现一致性
    interpretation: Optional[str] = None


# ────────────────────────────────────────────────────────────────────────────
# A10 · Absorption Zone · 价位级被动吸收（Footprint 派生硬证据）
# ────────────────────────────────────────────────────────────────────────────

class AbsorptionZone(BaseModel):
    """单个吸收价位带 · 从 Footprint buckets 派生

    「吸收」= 某价位出现**大量真实成交**但**买卖接近均衡**。
    技术特征：该价位 `buy_quote + sell_quote` 显著高于同 bar 其他 buckets
    （top 20%），且 `|delta_pct|` 偏低（< 0.20），说明被动端在持续接单。
    这是**已成交事实**，不可撤单，因此比挂单（订单墙）更可靠。

    - `side=support`：价位位于当时价下方，买方被动吸收卖盘（潜在支撑）
    - `side=resistance`：价位位于当时价上方，卖方被动吸收买盘（潜在阻力）
    - `age_hours`：从对应 Footprint bar 到现在的小时数（0 = 最新 bar）
    - `bar_count`：跨多少根 bar 在该价位重复出现吸收（1-3，越大越可靠）
    """
    price: float
    side: Literal["support", "resistance"]
    taker_volume_usd: float
    # 该价位累计（跨 bar 合并）总成交额
    delta_pct_abs_avg: float
    # 加权平均的 |delta_pct|（0~1，越接近 0 吸收越纯粹）
    bar_count: int
    # 在近 N 根 bar 中出现吸收特征的次数
    age_hours: float
    # 最近一次出现的 bar 距今小时数
    source: Literal["contract", "spot", "both"] = "contract"


class AbsorptionSnapshot(BaseModel):
    """A10 · 价位级吸收带汇总（被动吸收 = 硬证据）

    Footprint 数据源的派生产物，每根 bar 的 buckets 中筛选出
    「高成交量 + 低方向性 delta」的价位，跨 bars 合并为 zone 列表。

    供 MAA AI 和关键位系统共同消费：
    - MAA：作为"近期真实成交留痕"证据（dimension="Absorption"）
    - 关键位：作为 capital_flow 维度候选位（source_tag="absorption_zone"）

    阈值采保守基线（top 20% vol + |delta_pct| < 0.20）；若该基线
    下完全无 zone 命中，detector 会兜底放宽一次（top 30% + 0.30），
    并设置 `fallback_used=True` 透明告知。
    """
    zones_support: list[AbsorptionZone] = Field(default_factory=list)
    zones_resistance: list[AbsorptionZone] = Field(default_factory=list)
    total_zone_count: int = 0
    strongest_support: Optional[AbsorptionZone] = None
    strongest_resistance: Optional[AbsorptionZone] = None
    window_hours: float = 3.0
    # 数据覆盖时间窗（= footprint deque maxlen × bar 时长，默认 3h）
    lookback_bars: int = 0
    # 实际使用的 footprint bar 数
    fallback_used: bool = False
    # 是否启用了放宽阈值兜底


# ────────────────────────────────────────────────────────────────────────────
# B 级 · 加分 2 项
# ────────────────────────────────────────────────────────────────────────────

class TakerFlowSnapshot(BaseModel):
    """B1 · Taker 期现 5m 序列"""
    contract_recent_5m: list[dict] = Field(default_factory=list)
    # [{ts, buy_usd, sell_usd, delta_usd}, ...] 最近 12 根
    spot_recent_5m: list[dict] = Field(default_factory=list)
    # 最近 5m 期现净流入对比
    spot_vs_contract_divergence: bool = False
    latest_contract_delta_usd: Optional[float] = None
    latest_spot_delta_usd: Optional[float] = None


class OptionsSnapshot(BaseModel):
    """B2 · 期权（仅 BTC/ETH）"""
    total_oi_usd: Optional[float] = None
    oi_change_24h_pct: Optional[float] = None
    vol_change_24h_pct: Optional[float] = None
    pcr_oi: Optional[float] = None  # put/call ratio by OI
    magnet_price: Optional[float] = None  # 近 3 个到期日 max_pain × OI 加权
    iv_current: Optional[float] = None  # 来自 BBX（复用）
    iv_change_24h_pct: Optional[float] = None
    iv_skew_1m: Optional[float] = None


# ────────────────────────────────────────────────────────────────────────────
# MarketActionFacts · AI Arbiter 的输入契约（14 字段严格锁定）
# ────────────────────────────────────────────────────────────────────────────

MarketActionCoherence = Literal["confirming", "diverging", "neutral"]
SpotContractCoherence = Literal["spot_leads", "spot_lags", "aligned", "unknown"]
FundingTrend = Literal["building", "easing", "stable", "extreme"]
DataQuality = Literal["ok", "partial", "insufficient"]


class MarketActionFacts(BaseModel):
    """AI Arbiter 输入契约 · 14 字段 + 元数据"""
    coin: str
    timestamp: int

    # S 级 6
    price: Optional[PriceSnapshot] = None
    oi: Optional[OISnapshot] = None
    funding: Optional[FundingSnapshot] = None
    cvd_contract: Optional[CVDSnapshot] = None
    cvd_spot: Optional[CVDSnapshot] = None
    liquidation_flow: Optional[LiquidationSnapshot] = None

    # A 级 9
    basis: Optional[BasisSnapshot] = None
    orderbook: Optional[OrderbookSnapshot] = None
    liq_map_clusters: Optional[LiqClusterSnapshot] = None
    liq_sweep_recent: Optional[LiqSweepSnapshot] = None
    oi_price_coherence: MarketActionCoherence = "neutral"
    spot_contract_coherence: SpotContractCoherence = "unknown"
    funding_trend: FundingTrend = "stable"
    price_context: Optional[PriceContextSnapshot] = None
    footprint: Optional[FootprintSnapshot] = None
    absorption: Optional[AbsorptionSnapshot] = None
    # A10 · 价位级被动吸收带（Footprint 派生，硬证据，替代 orderbook 软信号）

    # B 级 2
    taker_flow_5m: Optional[TakerFlowSnapshot] = None
    options: Optional[OptionsSnapshot] = None

    # 元数据
    data_quality: DataQuality = "ok"
    missing: list[str] = Field(default_factory=list)


# ────────────────────────────────────────────────────────────────────────────
# MarketActionReport · AI 输出契约（6 块 JSON）
# ────────────────────────────────────────────────────────────────────────────

# 9 种场景（与用户拍板一致）
MarketScenario = Literal[
    "trend_continuation_up",
    "trend_continuation_down",
    "short_squeeze_up",
    "long_squeeze_down",
    "fake_breakout_up",
    "fake_breakdown_down",
    "exhaustion_top",
    "exhaustion_bottom",
    "range_bound",
]

MarketPhase = Literal[
    "accumulation",
    "markup",
    "distribution",
    "markdown",
    "transition",
]


class EvidenceItem(BaseModel):
    """单条证据（AI 从 facts 中引用的关键点 + 推断 + 立场）

    - observation：纯事实陈述（必须引用 facts 的具体数值）
    - inference：**从观察推出的判断**（交易员语气：跨维度因果、对比历史形态等）
    - supports：这条证据是支持主结论（main）、与主结论矛盾（contrarian），还是中性信息（neutral）
      禁止把 contrarian 证据以 high/medium 假装支持 main
    """
    dimension: str
    observation: str
    inference: Optional[str] = None
    supports: Literal["main", "contrarian", "neutral"] = "main"
    weight: Literal["high", "medium", "low"] = "medium"


class AlternativeScenario(BaseModel):
    """对立视角 · AI 必须给出的"第二可能性"

    强制产出对立假设 + 发生概率 + 触发条件，逼 AI 自己辩论而非单向填表。
    """
    scenario: str                       # 9 场景之一
    probability_pct: int = Field(ge=0, le=100)
    trigger: str                        # 触发这个替代场景所需的观察条件


class ContinuityVerdict(BaseModel):
    """时序连续性判断 · 本次分析相对"上一份报告"的立场

    - continuation：主假设延续，证据方向无变化
    - refinement  ：方向不变但细节修正（如强度/阶段/置信度调整）
    - reversal    ：方向反转或关键证据反转（如从 exhaustion_top → trend_continuation_up）
    - first_run   ：首次分析，无前序参考
    """
    stance: Literal["continuation", "refinement", "reversal", "first_run"] = "first_run"
    previous_scenario: Optional[str] = None     # 上一份的 scenario（便于前端对比展示）
    previous_ts: Optional[int] = None           # 上一份的 timestamp
    note: str = ""                              # 中文自然语言描述：为什么是延续/修正/反转


class TradingImplications(BaseModel):
    """AI 给出的操作建议（仅提示，不自动下单）"""
    bias: Literal["long", "short", "neutral", "wait"] = "wait"
    entry_zone: Optional[list[float]] = None  # [low, high]
    stop_loss_beyond: Optional[float] = None
    take_profit_targets: list[float] = Field(default_factory=list)
    notes: Optional[str] = None
    trader_intuition: Optional[str] = None
    # 50-100 字交易员直觉："如果我是机构交易员，此刻我会……"


class PromptSection(BaseModel):
    """Prompt 中的章节锚点，供前端生成 TOC"""
    anchor: str                       # "§1" / "§2" 等
    title: str                        # "当前行情速览"
    level: int = 2                    # markdown 标题级别（2=##, 3=###）


class PromptDebug(BaseModel):
    """AI 调用透明度 · 供前端"本轮喂给 AI 的完整数据"卡片展示"""
    system: str                       # system prompt 原文
    user: str                         # user prompt 原文（含 facts + 规则）
    chars: int                        # user prompt 字符数
    sections: list[PromptSection] = Field(default_factory=list)
    model: str                        # e.g. "deepseek-reasoner"
    tokens_prompt: Optional[int] = None
    tokens_completion: Optional[int] = None
    tokens_reasoning: Optional[int] = None
    latency_ms: int = 0
    generated_at: int = 0             # 秒级 unix
    ai_raw_response: Optional[str] = None  # AI 返回的原始文本（调试/复盘用）
    ai_reasoning_content: Optional[str] = None
    # deepseek-reasoner 的 Chain-of-Thought 原文；让前端可展开看"AI 是怎么想的"
    parse_ok: bool = True
    parse_error: Optional[str] = None


class MarketActionReport(BaseModel):
    """AI Arbiter 输出契约

    字段分层：
      1. 结论层：market_conclusion / scenario / market_phase
      2. 推理层：**analyst_reasoning / confidence_rationale / alternative_scenario**
         —— 让 AI 显式给出"交易员思维链"，而不是只填格子
      3. 证据层：evidence_breakdown（每条含 inference + supports）
      4. 建议层：trading_implications / invalidation_conditions
      5. 置信层：confidence + data_quality
    """
    coin: str
    timestamp: int

    # 结论层
    market_conclusion: str                    # 2-3 句中文总结
    scenario: MarketScenario
    market_phase: MarketPhase

    # 推理层（核心升级）
    analyst_reasoning: Optional[str] = None
    # 200-500 字"交易员思维链"：扫描 → 印证/矛盾 → 假设 → 反事实 → 结论
    confidence_rationale: Optional[str] = None
    # 为什么是这个 confidence，扣分/加分的具体原因
    alternative_scenario: Optional[AlternativeScenario] = None
    # 对立假设（第二可能性） + 概率 + 触发条件
    continuity: Optional[ContinuityVerdict] = None
    # 时序连续性：本次相对上一份报告是延续/修正/反转/首次

    # 证据层
    evidence_breakdown: list[EvidenceItem] = Field(default_factory=list)

    # 建议层
    trading_implications: TradingImplications = Field(default_factory=TradingImplications)
    invalidation_conditions: list[str] = Field(default_factory=list)

    # 置信层
    confidence: int = Field(ge=0, le=100)

    # 元数据
    data_quality: DataQuality = "ok"
    stale_minutes: int = 0  # 距上次成功 AI 调用的分钟数（若为降级结果）
    facts_snapshot: Optional[MarketActionFacts] = None  # 快照留档用
    prompt_debug: Optional[PromptDebug] = None  # 透明度：本轮发给 AI 的原文
