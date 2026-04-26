"""挂单压力监测器 (Orderbook Pressure Monitor) 数据模型

独立 snipe 信号源：
  L1 在订单簿热力图里找上方卖墙/下方买墙；
  L2 用大单 lifecycle (executed/cancelled/holding) + 小堆减量 vs 主动成交，判断撤单还是被吃；
  L3 结合 30min 价格反应 + CVD 方向，分类真假阻力/支撑；
  L4 与 footprint absorption_zone 联动，置信度加成。

输出供：
  (a) 独立 OrderbookPressureSignal（snipe 通道）；
  (b) key_level_tracker_v2 作为 confirmation/warning 入参。

字段命名遵循项目其他 model 风格：snake_case，金额用 USD，时间戳秒。
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

# ── 类型别名 ─────────────────────────────────────────────────────────────
WallSide = Literal["ask", "bid"]   # ask = 卖墙(上方阻力)；bid = 买墙(下方支撑)

WallChangeKind = Literal[
    "eaten",       # 被市价单吃掉为主 (>= 70% executed)
    "cancelled",   # 被撤单为主 (>= 70% canceled, executed <= 30%)
    "partial",     # 介于两者之间，灰色
    "growing",     # 反而增加 (有人继续堆)
    "holding",     # 几乎没变化（仍挂着）
    "unknown",     # 数据不足无法判断
]

WallLabel = Literal[
    "real_R",         # 真阻力：被吃 + 价格被压住
    "fake_R",         # 假阻力：撤单 spoof（价格还没到墙）
    "fake_R_break",   # 假阻力：撤单后价格已突破
    "real_S",         # 真支撑：被吃 + 价格守住
    "fake_S",         # 假支撑：撤单 spoof
    "fake_S_break",   # 假支撑：撤单后价格已跌穿
    "untested",       # 价格还没接近，先放着观察
]

# 模型语义 trace：上面 6 类对应 GPT 设想的"真/假压力 4 象限 + 是否突破"


# ── 大单生命周期单条 ───────────────────────────────────────────────────────
class LargeOrderLifecycle(BaseModel):
    """单笔大单的完整生命周期。

    字段直接映射 Coinglass /orderbook/large-limit-order(-history) 的返回，
    保留 USD/数量双轨方便 L2 计算。
    """
    id: int
    side: WallSide                           # bid=买单 / ask=卖单
    limit_price: float
    start_time_ms: int                       # ms（与 API 一致）
    end_time_ms: Optional[int] = None        # 仍持仓时为 None

    # 数量层
    start_quantity: float                    # 初始挂单数量(base)
    current_quantity: float                  # 当前剩余数量(base)
    executed_volume: float = 0.0             # 已成交数量(base) — 被市价单吃掉的部分
    executed_usd_value: float = 0.0
    start_usd_value: float = 0.0
    current_usd_value: float = 0.0

    trade_count: int = 0
    state: Literal["holding", "ended"] = "holding"   # API 1=holding, 2=ended

    @property
    def cancelled_quantity(self) -> float:
        """撤单部分 ≈ start - current - executed (永远 >= 0)。"""
        return max(self.start_quantity - self.current_quantity - self.executed_volume, 0.0)

    @property
    def executed_ratio(self) -> float:
        """被吃比例 = executed / start，用于 L2 分类。"""
        if self.start_quantity <= 0:
            return 0.0
        return min(self.executed_volume / self.start_quantity, 1.0)

    @property
    def cancelled_ratio(self) -> float:
        if self.start_quantity <= 0:
            return 0.0
        return min(self.cancelled_quantity / self.start_quantity, 1.0)


# ── 订单簿深度 snapshot ────────────────────────────────────────────────────
class DepthBin(BaseModel):
    """单个价位 bin（来自 /orderbook/history 解析）。"""
    price: float
    quantity: float                  # base coin 数量（如 BTC）
    usd_value: float                 # = price × quantity，预先算好方便排序


class OrderbookDepthSnapshot(BaseModel):
    """完整订单簿深度快照（含 latest 与 prev，用于减量对比）。

    bids/asks 价格升序；prev_* 可能为 None（首次拉取或样本不够）。
    """
    coin: str
    exchange: str
    symbol: str
    ts_sec: int                                 # latest snapshot 的时间(秒)
    bids: list[DepthBin] = Field(default_factory=list)
    asks: list[DepthBin] = Field(default_factory=list)

    prev_ts_sec: Optional[int] = None
    prev_bids: list[DepthBin] = Field(default_factory=list)
    prev_asks: list[DepthBin] = Field(default_factory=list)


# ── 压力堆(单个 wall) ────────────────────────────────────────────────────
class PressureWall(BaseModel):
    """聚合后的单个压力堆。

    可能是：
      - 多个小单聚合形成的"堆"（来自 depth snapshot）；
      - 单笔巨鲸大单（来自 large_orders 当前 + history）；
      - 两者同价位重合 → 强信号。
    """
    side: WallSide                              # ask=卖墙(上方阻力) / bid=买墙(下方支撑)
    price_lo: float                             # 价格区间(±0.05% tolerance 合并后)
    price_hi: float
    price_mid: float                            # 报告/前端显示用
    distance_pct: float                         # (price_mid - last) / last * 100，带符号

    # 强度
    size_usd: float                             # 当前堆 USD 总值
    size_base: float                            # 当前堆 base coin 数量
    rank: int = 0                               # 同侧 size 排名(1 = 最大)

    # 大单覆盖
    large_order_ids: list[int] = Field(default_factory=list)
    large_order_count: int = 0                  # 在该价位区间的大单笔数
    has_active_whale: bool = False              # 至少 1 笔 ≥ $1M 仍 holding

    # L2 — 撤单 vs 被吃
    change_kind: WallChangeKind = "unknown"
    eaten_usd: float = 0.0
    cancelled_usd: float = 0.0
    delta_usd: float = 0.0                      # 与 prev snapshot 相比的净变化（正=增加 / 负=减少）

    # L3 — 真假分类
    label: WallLabel = "untested"
    confidence: int = 0                         # 0-100

    # L4 — 与 footprint absorption_zone 共振
    confluence_with_absorption: bool = False
    absorption_zone_price: Optional[float] = None

    # 强度等级（派生字段，根据 confidence + label 综合算出）
    # 与关键位 KeyLevelV2.strength_tier (S/A/B/C) 视觉语言对齐，便于前端复用 UI 模式。
    strength_tier: Literal["S", "A", "B", "C"] = "C"

    # 诊断字段（前端 tooltip / 日志）
    reason: str = ""                            # 简短自然语言摘要
    cvd_state: Optional[str] = None             # 当前 CVD 趋势 rising/declining/flat


# ── 顶层 snapshot（state 字段类型）────────────────────────────────────────
class OrderbookPressureSnapshot(BaseModel):
    """挂单压力监测器的顶层输出。

    被三个消费方共同使用：
      - 前端独立卡片（直接渲染 walls + last_signals）；
      - key_level_tracker_v2（作为 confirmation 入参）；
      - OrderbookPressureSignal 生成器（snipe 通道）。
    """
    coin: str
    ts_sec: int
    last_price: float
    atr: Optional[float] = None                 # 价格反应窗口归一用，可为空

    walls: list[PressureWall] = Field(default_factory=list)

    # 顶层汇总（前端徽章 / KL tracker 快速读）
    top_resistance: Optional[float] = None      # 距离最近的 real_R wall.price_mid
    top_support: Optional[float] = None         # 距离最近的 real_S wall.price_mid
    has_real_pressure_above: bool = False
    has_real_pressure_below: bool = False
    has_fake_break_above: bool = False          # 有 fake_R_break ⇒ 上方刚被假突破
    has_fake_break_below: bool = False

    # 元数据
    sample_count_depth: int = 0                 # 本次用到的 depth snapshot 数(0/1/2)
    sample_count_large_history: int = 0
    data_quality: Literal["ok", "partial", "stale", "missing"] = "ok"
    notes: list[str] = Field(default_factory=list)


# ── 独立 snipe 信号 ──────────────────────────────────────────────────────
class OrderbookPressureSignal(BaseModel):
    """挂单压力监测器产出的独立 snipe 信号。

    走关键位通道的子频道（不污染主关键位流，但前端显示在同一区域）。
    去重：同币种 30 min 内、同价位（按 0.25×ATR 量化）只触发一次。
    """
    coin: str
    ts_sec: int
    side: Literal["long", "short"]              # long = 在下方真支撑做多；short = 在上方真阻力做空
    wall_label: WallLabel
    wall_price: float
    distance_pct: float
    last_price: float

    # 触发置信度（合成 wall.confidence + absorption confluence + CVD 佐证）
    confidence: int                             # 0-100

    # 入场建议
    entry_price: float                          # 建议入场（一般 = wall_price ± 0.1×ATR）
    stop_loss: float                            # 反向 0.5×ATR
    take_profit: float                          # 1.5×ATR 或下一个反向 wall

    # 触发原因（人话）
    reason: str = ""
    factors: list[str] = Field(default_factory=list)

    # 去重 key（写入 dedup map）
    dedup_key: str = ""
