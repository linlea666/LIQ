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
    """清算密集区（跨杠杆聚合后）

    M1 扩展（V3 关键位输入）：
    - exchange_count：贡献该价位的交易所数量（反查 LiquidationMap.by_exchange 得出）
      用于「跨所共识 multiplier」：单所偶发簇 vs 多所共振簇的可信度差异巨大。
    - dominant_exchange：贡献 USD 最大的交易所名称（仅展示用）
    - leverage_intensity：该簇内主导杠杆 USD 占比（0-1），用于 cascade 级联强度判断
    所有字段对旧消费者向后兼容（默认值），不会破坏现有 process_liquidation_map 流水线。
    """
    price_center: float
    price_from: float
    price_to: float
    total_usd: float
    side: str  # "long" | "short" — 指会被清算的方向
    dominant_leverage: str = ""
    distance_pct: float = 0
    # ── M1 新增 ─────────────────────────────────────────────────
    exchange_count: int = 1           # 1=单所偶发；≥3=多所共振强簇
    dominant_exchange: str = ""       # 贡献 USD 最大所
    leverage_intensity: float = 0.0   # 主导杠杆 USD / total_usd（0-1）


class VacuumZone(BaseModel):
    """清算真空区"""
    price_from: float
    price_to: float
    midpoint: float = 0
    note: str = ""


class LiquidationMap(BaseModel):
    """完整清算地图（支持多周期）

    数据契约说明：
    - Coinglass `aggregated-map` 返回 `data.data: [{liqMapV2, instrument:{exName}}, ...]`
      是按交易所并列的列表（非真正合并），故采集层会做两件事：
        1. 按 `exName` 分组累加，写入 `by_exchange`（保留分交易所明细）
        2. 跨交易所合并产 short/long bands（用于整体可视化与算法消费）
    - 这样既兼容现有 processor/前端只看 leverage_groups 的逻辑，又为未来"分交易所
      切换""失衡比交叉验证"等高级功能提供原始数据。
    """
    coin: str
    ts: int
    cycle: str  # "1d" | "3d" | "7d" | "30d"
    leverage_groups: list[LiqLeverageGroup]
    clusters_above: list[LiqCluster] = []
    clusters_below: list[LiqCluster] = []
    vacuum_zones: list[VacuumZone] = []
    imbalance_ratio: float = 0
    exchange: str = ""  # 空=聚合所有交易所
    # 按交易所拆分的明细：{exName: {price_str: usd_total}}
    # None 表示该字段未填充（兼容旧调用方 / 单交易所数据源）
    by_exchange: Optional[dict[str, dict[str, float]]] = None


def pick_primary_liq_map(liq_maps: Optional[dict]) -> Optional["LiquidationMap"]:
    """统一的清算地图降级链：1d → 7d → 30d（2026-08 P1 统一）。

    历史遗留：engine 多处写 `liq_maps.get("1d") or liq_maps.get("24h")`，
    但写入侧只存 1d/7d/30d 三个键（见 polls/liquidation.poll_liquidation_map
    与 poll_liq_history），"24h" 分支是死代码——导致 1d 缺失时 REST 路径
    （1d→7d→30d）与 Strategic 装配路径（1d→24h=None）降级行为不一致。
    所有消费点统一走本函数。
    """
    if not liq_maps:
        return None
    return liq_maps.get("1d") or liq_maps.get("7d") or liq_maps.get("30d")


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
    """爆仓统计（实际窗口由 poll_liq_history 决定，默认 24h = 1440 min）"""
    coin: str
    ts: int
    period_min: int = 1440
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
    """单个币种清算最大痛点

    Coinglass `liquidation/max-pain` 实际返回 4 个核心字段（每币种各一份）：
      - long_max_pain_liq_level   多头清算压力最大的"金额"（USD）
      - long_max_pain_liq_price   多头清算压力最大的"价格"
      - short_max_pain_liq_level  空头清算压力最大的"金额"（USD）
      - short_max_pain_liq_price  空头清算压力最大的"价格"

    旧模型只有 long_liq_usd / short_liq_usd（且字段名错），且**完全丢掉了价格信息**，
    这是导致旧 max-pain 数据死链路的直接原因。本版按真实 API 4 字段重新建模。
    """
    symbol: str
    price: float = 0             # 当前价（API 实测有 `price` 字段）
    # 多头痛点：价格"下行"触达该位 → 多头集中爆仓；通常 < 当前价
    long_pain_price: float = 0
    long_pain_usd: float = 0     # 多头痛点对应的爆仓 USD 金额
    # 空头痛点：价格"上行"触达该位 → 空头集中爆仓；通常 > 当前价
    short_pain_price: float = 0
    short_pain_usd: float = 0    # 空头痛点对应的爆仓 USD 金额


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
