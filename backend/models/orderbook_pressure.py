"""挂单压力监测器 (Orderbook Pressure Monitor) · 数据模型

本模块定位 = **流动性墙 + 大单行为 + 持仓拥挤度 监测引擎**（辅助参考层）：

  L0  数据源（CoinState 共享，墙引擎只读）
        ↓
  L1  detect_walls_from_depth        从 5m 订单簿热力图找近距堆 (≤4%)
  L1' detect_walls_from_large_orders 从大单 lifecycle 找远距堆 (4-12%)
  L2  build_wall_zones               相邻 bin 合并为墙区（M1）
  L3  compute_zone_persistence       基于 orderbook_depth_history 的可见时长（M1）
  L4  detect_wall_events             基于 large_orders 18 字段差分识别 6 类事件（M2）
  L5  build_position_crowding        OI / Funding / LS 拥挤度（M2）
  L6  build_sweep_targets            max_pain → next_magnet_price（M2）
  L7  augment_with_absorption        footprint absorption 共振加分

铁律（与 V3 关键位 roadmap 一致）：
  - 不输出"真假阻力 / spoof = true"绝对结论 → 改用 wall_removal_risk 软分
  - 不直接修改 KL 的 final_score / strength_tier / cascade_risk
  - 不重复轮询；所有数据从 CoinState 读

字段命名遵循项目 model 风格：snake_case，金额用 USD，时间戳秒。
"""

from __future__ import annotations

import time
from typing import Literal, Optional

from pydantic import BaseModel, Field

# ── 类型别名 ─────────────────────────────────────────────────────────────
WallSide = Literal["ask", "bid"]   # ask = 上方卖墙；bid = 下方买墙

WallSource = Literal[
    "depth_5m",        # 来自 5min 订单簿热力图聚合（近+中距，≤4%）
    "large_orders",    # 来自大单 lifecycle（远距，4-12%；可精确知挂单时长）
]

# 中性描述标签 —— 不做"真/假"判定。
WallLabel = Literal[
    "wall_ask",        # 卖墙：上方挂单堆，可作潜在阻力区参考
    "wall_bid",        # 买墙：下方挂单堆，可作潜在支撑区参考
    "wall_vanished",   # 墙已消失（被吃 OR 被撤），仅用于历史追溯
    "wall_broken",     # 墙已被价格穿过（事实判定，无主观）
]


# ── 大单生命周期单条 ───────────────────────────────────────────────────────
class LargeOrderLifecycle(BaseModel):
    """单笔大单的完整生命周期。

    映射 Coinglass /orderbook/large-limit-order(-history) 实测 18 字段（probe 验证）。
    本次升级（M2）补齐 exchange_name 字段，用于多所共振计数。
    """
    id: int
    side: WallSide
    limit_price: float
    start_time_ms: int                       # ms（与 API 一致）
    end_time_ms: Optional[int] = None        # 仍 holding 时为 None

    start_quantity: float
    current_quantity: float
    executed_volume: float = 0.0
    executed_usd_value: float = 0.0          # ⭐ M2 关键：墙被吃 USD 的硬证据
    start_usd_value: float = 0.0
    current_usd_value: float = 0.0

    trade_count: int = 0                     # ⭐ M2 关键：触发成交次数
    state: Literal["holding", "ended"] = "holding"

    # M2 新增：多所共振计数依赖
    exchange_name: Optional[str] = None      # "Binance" / "OKX" / ...

    @property
    def holding_age_sec(self) -> int:
        """挂单已存活秒数。

        - holding：now - start_time
        - ended：end_time - start_time
        - 时间戳异常：0（防御）
        """
        if self.start_time_ms <= 0:
            return 0
        end_ms = self.end_time_ms if (self.state == "ended" and self.end_time_ms) \
                                  else int(time.time() * 1000)
        if end_ms <= self.start_time_ms:
            return 0
        return (end_ms - self.start_time_ms) // 1000


# ── ±range 流动性时序快照（Phase B 新增）────────────────────────────────
class AskBidsRangeSnapshot(BaseModel):
    """合约 ±range 内 ask/bid USD 总量（来自 /orderbook/aggregated-ask-bids-history）。

    数据形态：每帧 4 标量 + ts_ms。无价格分布——不能定位单个墙位置，
    但可用于"宏观流动性侧翻"分析：
      - 30min 内 same-side USD 大幅下跌 → 卖方提前抽流动性 → break_through_risk +
      - 30min 内 same-side USD 显著增厚 → 该侧供给加大 → 攻防加强

    数据源选 multi-exchange aggregated（Binance + OKX + Bybit）：
      - 单一接口廉价补充"合约多家"维度（lifecycle 大单仍是单家）
      - aggregated 趋势比单家更稳健（避免单家偶发抽挂误导）
    """
    ts_ms: int                              # API 原始 ms 时间戳
    ts_sec: int                             # 同 ts_ms // 1000，便于其他模块对齐
    range_pct: float                        # 拉取参数：±X% 内（默认 1）
    aggregated_bids_usd: float              # 多家聚合 bid 总 USD
    aggregated_asks_usd: float              # 多家聚合 ask 总 USD
    aggregated_bids_qty: float = 0.0
    aggregated_asks_qty: float = 0.0


# ── 订单簿深度 snapshot ────────────────────────────────────────────────────
class DepthBin(BaseModel):
    """单个价位 bin（来自 /orderbook/history 解析）。"""
    price: float
    quantity: float                  # base coin 数量
    usd_value: float                 # = price × quantity，预先算好方便排序


class OrderbookDepthSnapshot(BaseModel):
    """完整订单簿深度快照（含 latest 与 prev，用于减量对比）。

    bids/asks 价格升序；prev_* 可能为空（首次拉取或样本不够）。
    M1 后由 orderbook_depth_history deque 滚动收集；prev 仍保留向后兼容。
    """
    coin: str
    exchange: str
    symbol: str
    ts_sec: int
    bids: list[DepthBin] = Field(default_factory=list)
    asks: list[DepthBin] = Field(default_factory=list)

    prev_ts_sec: Optional[int] = None
    prev_bids: list[DepthBin] = Field(default_factory=list)
    prev_asks: list[DepthBin] = Field(default_factory=list)


# ── 压力堆（单个 wall · 旧模型，保留向后兼容） ─────────────────────────
class PressureWall(BaseModel):
    """聚合后的单个挂单堆（**旧模型**，保留向后兼容）。

    M1 后建议优先消费 WallZone（区间 + 持续性 + 行为）。
    本模型仍由 detect_walls_from_depth / detect_walls_from_large_orders 输出，
    在 OrderbookPressureSnapshot.walls 字段保留作 fallback。
    """
    side: WallSide
    price_lo: float
    price_hi: float
    price_mid: float                            # 报告/前端显示用
    distance_pct: float                         # 带符号 (price_mid - last) / last × 100

    # 强度
    size_usd: float                             # USD 总值
    size_base: float                            # base coin 数量
    rank: int = 0                               # 同侧 size 排名(1=最大)

    # 数据源（影响前端徽章显示）
    source: WallSource = "depth_5m"

    # 大单覆盖
    large_order_ids: list[int] = Field(default_factory=list)
    large_order_count: int = 0
    has_active_whale: bool = False              # ≥1 笔大单仍 holding
    holding_avg_age_sec: int = 0                # large_orders 路径才有效；depth_5m 路径为 0

    # 中性标签（仅用于前端文案，不做真假判定）
    label: WallLabel = "wall_ask"

    # 与 footprint absorption_zone 共振（用于强度加成）
    confluence_with_absorption: bool = False
    absorption_zone_price: Optional[float] = None

    # 强度评分（内部用，前端不显示原始数）
    strength_score: float = 0.0
    strength_tier: Literal["S", "A", "B", "C"] = "C"

    # 简短中性摘要（前端 tooltip 用，例如"5m订单簿·近距·$15M"）
    reason: str = ""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# M1+M2 新增模型 · 流动性墙引擎核心
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# 墙区状态（M2 行为评估输出）
WallZoneStatus = Literal[
    "active",          # 默认稳定
    "strengthening",   # 增厚中（current > start）
    "weakening",       # 减薄中（current < start 但仍 holding）
    "removed",         # 已撤（state ended + executed=0）
    "consumed",        # 已被吃（state ended + executed>0）
    "reloaded",        # 撤后短时间同价位重挂
    "absorbed",        # 被攻击但守住（有 absorption 信号）
    "unknown",         # 数据不足
]

# 数据来源融合（M1 + Phase A 现货扩展）
# 命名遵循"基础源_扩展源"语义：
#   - depth_only       ：仅来自合约 5m 深度热力图
#   - depth+large_order：合约深度 + 合约大单确认
#   - spot_only        ：仅来自现货 5m 深度热力图（无合约源覆盖此价位）
#   - spot+depth       ：合约深度 + 现货深度同价位共振（💎 双源高可信墙的硬证据）
#   - large_order_only ：保留兼容旧消费者，引擎当前不输出此值
WallZoneSource = Literal[
    "depth_only",
    "large_order_only",
    "depth+large_order",
    "spot_only",
    "spot+depth",
]

# 趋势（M1）
WallZoneTrend = Literal["new", "strengthening", "weakening", "stable"]

# WallEvent 类型（M2 + W2-T5）
WallEventType = Literal[
    "wall_appeared",
    "wall_strengthened",
    "wall_weakened",
    "wall_removed",
    "wall_consumed",
    "wall_reloaded",
    # W2-T5：第 7 类复合事件 — 同一 zone 在同一帧既被部分吃单又被部分撤单
    # 语义："试盘 + 撤退" footprint —— 机构挂大单试探流动性，部分被吃后撤掉剩余
    # 比单纯 consumed 或 removed 都更可疑（既不是真支撑，也不是纯 spoof）
    # 同帧仍会独立发 wall_consumed + wall_removed（向后兼容）；本事件作"复合解读"
    "wall_consumed_and_removed",
]

# W3-T2：墙区主导角色分类（互斥）
# 优先级（高→低）：dual_battleground > institutional_footprint > support_resistance_strong
# > magnet_strong > spoof_suspect > transient > ordinary
DominantRole = Literal[
    "dual_battleground",         # 双向博弈热点（SR ≥ 0.6 AND SA ≥ 0.6）
    "institutional_footprint",   # 机构 footprint（coinbase_max_single_order_usd ≥ 100k）
    "support_resistance_strong", # 强支撑/阻力（SR ≥ 0.7 且 SA < 0.4）
    "magnet_strong",             # 强清算磁铁（SA ≥ 0.6 且 SR < 0.5）
    "spoof_suspect",             # 撤单嫌疑（trust < 0.55 且 wall_removal_risk ≥ 0.6）
    "transient",                 # 短期墙（persistence < 0.3 且不变薄）
    "ordinary",                  # 普通（默认）
]


# 拥挤度推断状态（M2）
InferredPositionState = Literal[
    "long_opening",                  # 多头主动开仓
    "short_opening",                 # 空头主动开仓
    "long_closing_or_liquidation",   # 多头平仓 / 被清算
    "short_covering_or_liquidation", # 空头回补 / 被清算
    "liquidation_flush",             # 双向清算潮
    "mixed",                         # 信号矛盾
    "unknown",                       # 数据不足
]

# OI 保证金分布
OIMarginSplit = Literal[
    "coin_dominant",        # 币本位 OI 占优（存量博弈）
    "stable_dominant",      # U 本位 OI 占优（新资金进入）
    "balanced",
    "unknown",
]

# 扫单磁铁方向
SweepDirection = Literal["below", "above"]


class PositionCrowdingSnapshot(BaseModel):
    """持仓拥挤度上下文（M2）。

    全部从 CoinState 读取，不发起新 poll：
      - oi_exchange_rank["All"]：自带 5m/15m/30m/1h/4h/24h delta + 币本位/U 本位分流
      - multi_funding：当前 funding rate
      - funding_history_8h：用于百分位
      - ls_ratio + top_position_ratio
    """
    # OI 多周期变化率（百分比，已是 percent_change_*，无需本地计算）
    oi_delta_5m_pct: Optional[float] = None
    oi_delta_15m_pct: Optional[float] = None
    oi_delta_30m_pct: Optional[float] = None
    oi_delta_1h_pct: Optional[float] = None
    oi_delta_4h_pct: Optional[float] = None
    oi_delta_24h_pct: Optional[float] = None

    # OI 分流（GPT 提出的新洞察 · "U 本位激增 = 新资金 / 币本位激增 = 老用户加杠杆"）
    oi_coin_margin_usd: Optional[float] = None
    oi_stable_margin_usd: Optional[float] = None
    oi_margin_split: OIMarginSplit = "unknown"

    # Funding
    funding_now_pct: Optional[float] = None       # 当前 funding 百分比
    funding_percentile_30d: Optional[float] = None  # 0-1（30 天分位）

    # 多空比
    top_position_ls_ratio: Optional[float] = None    # 大户持仓多空比
    global_account_ls_ratio: Optional[float] = None  # 全网账户多空比

    # 推断
    inferred_position_state: InferredPositionState = "unknown"
    long_crowding_risk: float = 0.0      # 0-1
    short_crowding_risk: float = 0.0     # 0-1

    # 表达
    explain_chips: list[str] = Field(default_factory=list)


class SweepTarget(BaseModel):
    """扫单磁铁（M2）—— 墙被打穿后的下一个目标。

    第一版直接读 liq_max_pain.{long,short}_max_pain_liq_price，
    后续可由 liq_maps 簇增强校验。
    """
    direction: SweepDirection                   # below = 下方多头磁铁；above = 上方空头磁铁
    magnet_price: float
    magnet_amount_usd: float                    # max_pain 提供的清算金额
    distance_pct: float                         # 相对当前价的百分比（带符号）
    vacuum_gap_pct: float = 0.0                 # wall 到 magnet 之间的真空跨度（%）
    explain: str = ""                           # 前端文案（中文）


class WallEvent(BaseModel):
    """墙区事件（M2）—— 用 large_orders 18 字段差分识别。

    事件流写到 OrderbookPressureSnapshot.wall_events，最近 100 条滚动保留。
    """
    ts_sec: int
    side: WallSide
    price_mid: float                            # 区间中位价
    event_type: WallEventType
    size_before_usd: Optional[float] = None
    size_after_usd: Optional[float] = None
    executed_usd_value: Optional[float] = None  # consumed 才有
    confidence: float = 0.0                     # GPT 加权公式（0-1）
    explain: str = ""                           # 前端 chip 文字
    # W1-T4：关联的 wall_zone_id（同 ID 同一物理墙；后验脚本据此串联事件链）
    wall_zone_id: str = ""


class WallZone(BaseModel):
    """墙区（M1+M2 核心模型）—— 相邻 bin 合并 + 持续性 + 行为 + 上下文。

    取代单点 PressureWall 视角，回答"上方哪里有卖墙、下方哪里有买墙"。
    """
    # ── W1-T4：稳定 zone ID（跨帧匹配）──
    # 由 (coin, side, peak_price 桶号) 生成 SHA1 短摘要：
    #   bucket_size = max(atr × 0.5, peak_price × 0.0015, 0.5)
    #   bucket_idx  = floor(peak_price / bucket_size)
    #   id          = sha1(f"{coin}|{side}|{bucket_idx}").hexdigest()[:12]
    # 同一物理墙 ±0.5 ATR 漂移内 ID 不变，方便 wall_events / 后验脚本串联生命周期。
    # 不依赖 source（dual_source / spot_only 切换不影响 ID）。
    wall_zone_id: str = ""

    # ── 区间定位（M1）──
    side: WallSide                              # bid 下方买墙 / ask 上方卖墙
    price_low: float
    price_high: float
    price_mid: float
    peak_price: float                           # 厚度最大的 bin 价位
    distance_pct: float                         # 带符号（price_mid - last) / last × 100

    # ── 厚度统计（M1，基于滚动历史）──
    current_usd: float                          # 当前帧 USD 厚度
    max_usd_1h: float                           # 1h 内峰值
    avg_usd_1h: float                           # 1h 内均值
    bin_count: int                              # 合并了几个原始 bin

    # ── 持续性（M1）──
    seen_count: int                             # history 中出现帧数（最大 = window_size）
    visible_minutes: float                      # 持续可见分钟数
    persistence_score: float                    # 0-1
    first_seen_ts: int = 0
    last_seen_ts: int = 0
    trend: WallZoneTrend = "new"

    # ── 数据源融合（M1）──
    source: WallZoneSource = "depth_only"
    exchange_count: int = 1                     # 多所共振计数
    large_order_ids: list[int] = Field(default_factory=list)

    # ── 现货 vs 合约 区分（M2.5：诉求"现货=真支撑、合约=清算磁铁"）──
    # 当前 zone 的底层数据来自 futures 5m 热力图（合约订单簿）。spot 大单作为"真买卖家"
    # 的硬证据：若同一价位也有 holding 的 spot 大单 → has_spot_confluence=True，意味着
    # 该墙不只是合约杠杆挂单，背后有真金白银的现货资金 → 真支撑/真阻力可信度↑。
    has_spot_confluence: bool = False           # 是否与现货大单共振
    spot_large_order_ids: list[int] = Field(default_factory=list)
    trust_score: float = 0.5                    # 综合可信度 0-1
    # ≥ 0.85：高可信墙（双源 + 持久 + 多所，叠加多重硬证据）
    # ≥ 0.65：较可信
    # ≥ 0.50：普通（仅合约源，需结合磁铁/被扫风险解读）
    # < 0.50：短期墙 / 可能被扫
    # ── Phase A：现货 5m 深度热力图双源共振 ──
    # dual_source = True 当且仅当此 zone 的价区在合约+现货 5m 热力图同时存在
    # ≥ wall_min_usd 的厚度 → 该价位"真买卖家与杠杆资金共同布局"，是 trust_score
    # 阶梯加分的最强单一证据（>spot 大单单笔/多所共振）。
    dual_source: bool = False
    spot_current_usd: float = 0.0               # 现货侧同价区 USD 厚度（dual_source=True 时填）
    spot_max_usd_1h: float = 0.0                # 现货侧 1h 峰值（用于现货侧 trend 派生）

    # ── Phase C：Coinbase 现货原生 API（机构买卖维度，独立于 Binance dual_source）──
    # coinbase_spot_confluence = True 当且仅当 Coinbase 现货同价区有显著厚度
    # （≥ wall_min_usd × 30%，且 num_orders ≥ 3 表示多笔聚集而非单大单 spoof）。
    # 该字段独立于 dual_source / has_spot_confluence：捕获 Binance 之外的机构资金
    # （主要是 USA ETF 链路）。trust_score +0.10 单独加分，详见 _compute_trust_score。
    coinbase_spot_confluence: bool = False
    coinbase_spot_usd: float = 0.0              # Coinbase 同价区当前帧 USD 厚度
    coinbase_num_orders: int = 0                # Coinbase 同价区累计订单笔数（≥3 才算共振）
    # W2-T4：Coinbase 单笔大单分支 — 机构资金 footprint 独立硬证据
    # = max(level.usd_value / level.num_orders) over levels overlapping this zone
    # ≥ 100k USD/笔 视为"机构级孤立大单"，SR +0.05（区分散户聚集 vs 机构布局）
    coinbase_max_single_order_usd: float = 0.0

    # ── 行为评估（M2）──
    status: WallZoneStatus = "active"
    wall_consumed_confidence: float = 0.0       # GPT 加权公式（0-1）
    wall_removal_risk: float = 0.0              # 0-1（不写"假单"）

    # ── W2-T1：trust_score 拆分（透明化 + 维度独立）──
    # raw_trust_score：原始多因子加权（与 trust_score 等价；显式命名供后验脚本/AI 引用）
    # trust_components：各因子贡献明细（base / dual_source / spot_confluence / ...）
    # support_resistance_trust_score (SR)：作为支撑/阻力被反弹的可信度（独立维度）
    #   - 共享 trust 因子，但额外对 wall_removal_risk 给负贡献
    #   - 加 wall_consumed_confidence 正贡献（已被验证承接过买卖盘）
    # sweep_attractiveness_score (SA)：作为扫单磁铁被打穿的可吸引度（独立维度）
    #   - 完全不同的因子组合：crowding 同向 + magnet 邻近 + active_attack + thinning
    #   - SR 与 SA 可同时高（"双向博弈热点"）
    # active_attack_score：实时同向 taker + cvd_spot + 流动性衰竭（0-1）
    #   - 原本是 break_through_risk 的中间计算；现提升为字段供 SR/SA 复用，
    #     也方便 archiver 落盘 + 前端展示（W3-T1）
    raw_trust_score: float = 0.0
    trust_components: dict = Field(default_factory=dict)  # str -> float
    support_resistance_trust_score: float = 0.0
    sweep_attractiveness_score: float = 0.0
    active_attack_score: float = 0.0

    # W3-T2：主导角色分类（基于 SR/SA/trust/removal_risk/persistence 综合判定）
    # 让 AI/前端一目了然该墙的"性质"，省去多字段组合推理；archiver 落盘后
    # W4-T1 后验脚本可直接统计各角色的实际反弹/打穿率
    dominant_role: DominantRole = "ordinary"

    # ── 上下文引用（M2）──
    crowding_context: Optional[PositionCrowdingSnapshot] = None  # 与全局 crowding 同；冗余存方便前端 zone 自包含
    sweep_target: Optional[SweepTarget] = None
    break_through_risk: float = 0.0             # 0-1
    next_magnet_price: Optional[float] = None   # 便于前端快速访问

    # ── absorption（沿用 PressureWall 设计）──
    confluence_with_absorption: bool = False
    absorption_zone_price: Optional[float] = None

    # ── 评分 + 表达 ──
    strength_score: float = 0.0
    strength_tier: Literal["S", "A", "B", "C"] = "C"
    explain_chips: list[str] = Field(default_factory=list)


# ── 顶层 snapshot（state 字段类型）────────────────────────────────────────
class OrderbookPressureSnapshot(BaseModel):
    """挂单压力监测器的顶层输出（向后兼容 + M1+M2 新字段）。

    兼容性策略：
      - 旧 walls 字段保留（旧消费者 / 旧 kl_history.json 仍可用）
      - 新 walls_above / walls_below / wall_zones / wall_events 为升级后主消费路径
      - 旧字段（top_resistance / top_support）保留
      - data_quality 加 "warming" 状态（暖机期 30min 内）
    """
    coin: str
    ts_sec: int
    last_price: float
    atr: Optional[float] = None

    # ── 旧字段（保留向后兼容）──
    walls: list[PressureWall] = Field(default_factory=list)
    top_resistance: Optional[float] = None
    top_support: Optional[float] = None

    # ── M1 新字段：墙区视图 ──
    walls_above: list[WallZone] = Field(default_factory=list)   # 上方卖墙（升序，距现价由近到远）
    walls_below: list[WallZone] = Field(default_factory=list)   # 下方买墙（降序，距现价由近到远）
    wall_zones: list[WallZone] = Field(default_factory=list)    # = above + below 但保 strength_score 排序

    # ── M2 新字段：事件 + 全局拥挤度 ──
    wall_events: list[WallEvent] = Field(default_factory=list)  # 最近 100 条滚动
    crowding_global: Optional[PositionCrowdingSnapshot] = None  # 全局拥挤度（也分发到每个 zone）

    # ── W2-T4 顶层字段：USD/USDT 基差（前端 / AI 展示用）──
    # = (coinbase_last - ticker_last) / ticker_last × 100  (单位 %)
    # 例：BTC-USD 76300, BTCUSDT 76250 → basis = +0.066%（USD 比 USDT 贵 6.6bp）
    # 暖机期 / 缺数据时为 None；正常 < 5bp，> 30bp 表示明显基差异常
    usd_usdt_basis_pct: Optional[float] = None

    # ── 元数据 ──
    history_window_minutes: int = 60            # 滚动历史窗口（M1 默认 1h）
    sample_count_depth: int = 0
    sample_count_depth_history: int = 0          # M1 新增：滚动 deque 实际帧数
    sample_count_large_history: int = 0
    sample_count_large_orders_walls: int = 0    # 来自 large_orders 路径的 wall 数
    data_quality: Literal["ok", "partial", "stale", "warming", "missing"] = "ok"
    notes: list[str] = Field(default_factory=list)
