"""挂单压力监测器 (Orderbook Pressure Monitor) · 数据模型

本次重构（2026-04）后定位 = **盘口订单流仪表盘**（辅助参考），
不再做"真假阻力"判定。原因：

  - 5min 快照无法支撑秒级"撤单 vs 被吃"判定（数据精度天花板）
  - CVD 1h 与 wall 5min 时间分辨率错配（12× 差距，结构性误判）
  - "fake_R/spoof" 标签是过度解读 → 容易误导新手交易员

新数据流：
  L1 detect_walls_from_depth      —— 从 5min 订单簿热力图找近+中距堆 (≤4%)
  L1' detect_walls_from_large_orders —— 从大单 lifecycle 找远距堆 (4-12%)
  L2 augment_with_absorption      —— footprint absorption_zone 共振加分（×1.2）
  L3 _compute_strength_score      —— USD × 持续时间 × whale × absorption
  L4 _assign_strength_tier        —— 按绝对 USD 阈值分级 S/A/B/C

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

    映射 Coinglass /orderbook/large-limit-order(-history)，
    本次重构新增 holding_age_sec 派生属性，用于 strength_score 公式。
    """
    id: int
    side: WallSide
    limit_price: float
    start_time_ms: int                       # ms（与 API 一致）
    end_time_ms: Optional[int] = None        # 仍 holding 时为 None

    start_quantity: float
    current_quantity: float
    executed_volume: float = 0.0
    executed_usd_value: float = 0.0
    start_usd_value: float = 0.0
    current_usd_value: float = 0.0

    trade_count: int = 0
    state: Literal["holding", "ended"] = "holding"

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


# ── 订单簿深度 snapshot ────────────────────────────────────────────────────
class DepthBin(BaseModel):
    """单个价位 bin（来自 /orderbook/history 解析）。"""
    price: float
    quantity: float                  # base coin 数量
    usd_value: float                 # = price × quantity，预先算好方便排序


class OrderbookDepthSnapshot(BaseModel):
    """完整订单簿深度快照（含 latest 与 prev，用于减量对比）。

    bids/asks 价格升序；prev_* 可能为空（首次拉取或样本不够）。
    本次重构后 prev_* 不再用于撤吃判定，但仍保留供未来诊断用。
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


# ── 压力堆(单个 wall) ────────────────────────────────────────────────────
class PressureWall(BaseModel):
    """聚合后的单个挂单堆（中性辅助参考）。

    可能来源：
      - depth_5m：多个小单聚合形成的"堆"（近+中距，≤4%）
      - large_orders：单笔/多笔巨鲸大单（远距，4-12%；可精确知挂单时长）

    重要约束：本模块只描述"挂单的客观状态"，不判定"真假阻力/支撑"。
    最终交易决策应该由用户结合关键位、CVD、消息面等综合判断。
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


# ── 顶层 snapshot（state 字段类型）────────────────────────────────────────
class OrderbookPressureSnapshot(BaseModel):
    """挂单压力监测器的顶层输出。

    被两个消费方使用：
      - 前端独立卡片（直接渲染 walls）
      - key_level_tracker_v2（作为 confirmation 入参；按 tier 匹配）

    重要约束：snapshot 不再产出 OrderbookPressureSignal —— 已砍掉
    独立 snipe 通道，避免数据基础不足却给出可执行交易指令。
    """
    coin: str
    ts_sec: int
    last_price: float
    atr: Optional[float] = None

    walls: list[PressureWall] = Field(default_factory=list)

    # 顶层汇总（前端徽章 / KL tracker 快速读）
    # 取最强 ask wall（≥A 级）的 price_mid，无则 None
    top_resistance: Optional[float] = None
    top_support: Optional[float] = None

    # 元数据
    sample_count_depth: int = 0
    sample_count_large_history: int = 0
    sample_count_large_orders_walls: int = 0    # 来自 large_orders 路径的 wall 数
    data_quality: Literal["ok", "partial", "stale", "missing"] = "ok"
    notes: list[str] = Field(default_factory=list)
