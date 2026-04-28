"""挂单压力监测器 (Orderbook Pressure Monitor) · 核心算法

本次重构（2026-04）后定位 = **盘口订单流仪表盘**（辅助参考），
不再做"真假阻力"判定。设计理念：

  1. 数据源分层（避免单一周期的精度/覆盖不足）：
     - 近距 (≤1.5%) + 中距 (1.5-4%): depth 5min 热力图（撮合面真实压力）
     - 远距 (4-12%)               : large_orders lifecycle（精确知挂单时长）

  2. 强度评分（USD 主导 + 时间衰减/累积 + whale 加成 + 共振加成）：
       strength_score = size_usd × duration_factor × (1 + 0.3·whale) × (1 + 0.2·absorption)
     duration_factor 阶梯（仅 large_orders 路径有效；depth_5m 路径 = 1.0）：
       <1h    : 0.7   （快闪挂单，可信度低）
       1-6h   : 1.0   （日内级）
       6-24h  : 1.3   （耐心资金）
       1-7d   : 1.6   （长线大单）
       >7d    : 2.0   （超长期挂单）

  3. 强度等级（按绝对 USD 阈值，便于跨币种对齐）：
       S ≥ $30M
       A ≥ $10M
       B ≥ $3M
       C ≥ $500K（默认门槛）

入口：``compute_pressure_snapshot(state) -> OrderbookPressureSnapshot``
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

from models.orderbook_pressure import (
    DepthBin,
    LargeOrderLifecycle,
    OrderbookDepthSnapshot,
    OrderbookPressureSnapshot,
    PressureWall,
    WallSide,
)

if TYPE_CHECKING:
    from engine import CoinState
    from models.market_action import AbsorptionSnapshot

logger = logging.getLogger(__name__)


# ── 默认阈值（与 README/config.yaml 同源；可由 settings 覆盖） ──────────────
DEFAULTS = {
    # 数据源边界（depth_5m 走近+中距，large_orders 走远距）
    "depth_range_pct": 4.0,           # depth 路径只看 ±4%（≈关键位 近+中距）
    "large_orders_min_pct": 4.0,      # large_orders 路径起点 4%
    "large_orders_max_pct": 12.0,     # large_orders 路径终点 12%

    # depth 路径阈值
    "wall_size_top_pct": 0.20,        # top 20% by USD
    "wall_min_usd": 500_000.0,        # 单 wall 最低 USD 阈值（C 级门槛）
    "merge_tol_pct": 0.0005,          # 合并同价位 ±0.05%

    # large_orders 路径合并阈值
    "large_orders_merge_tol_pct": 0.0010,   # 大单聚合 ±0.10%

    # 输出限制
    "max_walls_per_side": 8,          # 每侧最多输出 8 个堆（merge 后）

    # 大单关联（depth 路径用来打 has_active_whale 标记）
    "large_match_tol_pct": 0.0010,

    # L2 共振容差
    "absorption_atr_mult": 0.5,       # ±0.5×ATR
    "min_atr_pct_fallback": 0.005,    # 无 ATR 时退化为 0.5% 价距

    # 强度阈值（USD 绝对值）—— 跨币种通用
    "tier_s_min_usd": 30_000_000.0,
    "tier_a_min_usd": 10_000_000.0,
    "tier_b_min_usd": 3_000_000.0,

    # 数据陈旧判定（depth 主源 ts_sec 离现在的秒数；OP 轮询 90s，缺省取 180s = 2 轮）
    "stale_age_sec": 180,
}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 共享数据结构
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dataclass
class _RawWall:
    side: WallSide
    price_lo: float
    price_hi: float
    size_usd: float
    size_base: float
    bin_count: int
    source: str = "depth_5m"               # depth_5m or large_orders
    holding_avg_age_sec: int = 0           # large_orders 路径才有意义
    large_order_ids: list[int] = None      # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.large_order_ids is None:
            self.large_order_ids = []


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# L1 路径 A：从 5m depth heatmap 找近+中距堆 (≤4%)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _filter_bins_by_range(
    bins: list[DepthBin], last_price: float,
    min_pct: float, max_pct: float, side: WallSide,
) -> list[DepthBin]:
    """筛选距离落在 [min_pct, max_pct] 区间内 + 同方向的 bins。

    ask 必须 > last_price；bid 必须 < last_price，避免穿越中线。
    距离按绝对百分比筛选（min_pct 通常为 0.0，max_pct = depth_range_pct）。
    """
    if last_price <= 0 or not bins:
        return []
    out = []
    for b in bins:
        if side == "ask" and b.price <= last_price:
            continue
        if side == "bid" and b.price >= last_price:
            continue
        dist_pct = abs(b.price - last_price) / last_price * 100.0
        if dist_pct < min_pct or dist_pct > max_pct:
            continue
        out.append(b)
    return out


def _detect_walls_from_depth_one_side(
    bins: list[DepthBin], side: WallSide, last_price: float, cfg: dict,
) -> list[_RawWall]:
    """单侧 depth 路径：阈值过滤 → 同价位合并 → 强度排序。

    阈值为 (top X% by USD) AND (USD ≥ MIN_USD) 双闸，
    避免冷清时段误判 + 极端噪声。
    """
    band = _filter_bins_by_range(
        bins, last_price, 0.0, cfg["depth_range_pct"], side,
    )
    if not band:
        return []

    # 双闸阈值
    sorted_bins = sorted(band, key=lambda b: b.usd_value, reverse=True)
    top_n = max(1, int(round(len(sorted_bins) * cfg["wall_size_top_pct"])))
    top_cut_usd = sorted_bins[top_n - 1].usd_value
    threshold_usd = max(top_cut_usd, cfg["wall_min_usd"])

    qualified = [b for b in band if b.usd_value >= threshold_usd]
    if not qualified:
        return []

    # 同价位合并 (±merge_tol_pct)
    qualified.sort(key=lambda b: b.price)
    merge_tol = max(0.01, last_price * cfg["merge_tol_pct"])
    walls: list[_RawWall] = []
    cur: Optional[_RawWall] = None
    for b in qualified:
        if cur is None:
            cur = _RawWall(side=side, price_lo=b.price, price_hi=b.price,
                           size_usd=b.usd_value, size_base=b.quantity,
                           bin_count=1, source="depth_5m")
            continue
        if b.price - cur.price_hi <= merge_tol:
            cur.price_hi = b.price
            cur.size_usd += b.usd_value
            cur.size_base += b.quantity
            cur.bin_count += 1
        else:
            walls.append(cur)
            cur = _RawWall(side=side, price_lo=b.price, price_hi=b.price,
                           size_usd=b.usd_value, size_base=b.quantity,
                           bin_count=1, source="depth_5m")
    if cur is not None:
        walls.append(cur)

    walls.sort(key=lambda w: w.size_usd, reverse=True)
    return walls[:cfg["max_walls_per_side"]]


def detect_walls_from_depth(
    depth: OrderbookDepthSnapshot, last_price: float, cfg: dict,
) -> list[_RawWall]:
    """L1 路径 A：从 5m depth 找近+中距堆（双侧）。"""
    if last_price <= 0 or depth is None:
        return []
    raw_asks = _detect_walls_from_depth_one_side(depth.asks, "ask", last_price, cfg)
    raw_bids = _detect_walls_from_depth_one_side(depth.bids, "bid", last_price, cfg)
    return raw_asks + raw_bids


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# L1 路径 B：从大单 lifecycle 找远距堆 (4-12%)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _filter_large_orders_by_range(
    large_orders: list[LargeOrderLifecycle], last_price: float,
    min_pct: float, max_pct: float, side: WallSide,
) -> list[LargeOrderLifecycle]:
    """筛选距离落在 [min_pct, max_pct] 内、方向匹配、且仍 holding 的大单。

    ended（已平/已撤）的大单不进入远距 wall 候选 —— 它们是"过去"，
    无法作为当前的盘口压力。
    """
    if last_price <= 0 or not large_orders:
        return []
    out: list[LargeOrderLifecycle] = []
    for lo in large_orders:
        if lo.side != side:
            continue
        if lo.state != "holding":
            continue
        if lo.limit_price <= 0 or lo.current_usd_value <= 0:
            continue
        if side == "ask" and lo.limit_price <= last_price:
            continue
        if side == "bid" and lo.limit_price >= last_price:
            continue
        dist_pct = abs(lo.limit_price - last_price) / last_price * 100.0
        if dist_pct < min_pct or dist_pct > max_pct:
            continue
        out.append(lo)
    return out


def _detect_walls_from_large_orders_one_side(
    large_orders: list[LargeOrderLifecycle], side: WallSide,
    last_price: float, cfg: dict,
) -> list[_RawWall]:
    """单侧 large_orders 路径：筛选 → 同价位合并 → 强度排序。

    与 depth 路径的关键差异：
      - 不做 top X% 双闸（大单本身已经是 ≥ $1M 的过滤结果）
      - 仍做 wall_min_usd 阈值（C 级门槛对齐）
      - 合并容差更宽（0.10%）—— 大单价位通常较散
      - 记录 holding_avg_age_sec 用于 strength_score 计算
    """
    in_band = _filter_large_orders_by_range(
        large_orders, last_price,
        cfg["large_orders_min_pct"], cfg["large_orders_max_pct"], side,
    )
    if not in_band:
        return []

    in_band.sort(key=lambda lo: lo.limit_price)
    merge_tol = max(0.01, last_price * cfg["large_orders_merge_tol_pct"])

    walls: list[_RawWall] = []
    cur: Optional[_RawWall] = None
    cur_age_weighted_usd = 0.0   # 累计 age × usd 用于加权平均

    for lo in in_band:
        usd = float(lo.current_usd_value)
        base = float(lo.current_quantity)
        age_sec = max(int(lo.holding_age_sec), 0)
        if cur is None:
            cur = _RawWall(
                side=side, price_lo=lo.limit_price, price_hi=lo.limit_price,
                size_usd=usd, size_base=base, bin_count=1,
                source="large_orders",
                large_order_ids=[lo.id],
            )
            cur_age_weighted_usd = age_sec * usd
            continue
        if lo.limit_price - cur.price_hi <= merge_tol:
            cur.price_hi = lo.limit_price
            cur.size_usd += usd
            cur.size_base += base
            cur.bin_count += 1
            cur.large_order_ids.append(lo.id)
            cur_age_weighted_usd += age_sec * usd
        else:
            cur.holding_avg_age_sec = int(cur_age_weighted_usd / cur.size_usd) \
                                       if cur.size_usd > 0 else 0
            walls.append(cur)
            cur = _RawWall(
                side=side, price_lo=lo.limit_price, price_hi=lo.limit_price,
                size_usd=usd, size_base=base, bin_count=1,
                source="large_orders",
                large_order_ids=[lo.id],
            )
            cur_age_weighted_usd = age_sec * usd

    if cur is not None:
        cur.holding_avg_age_sec = int(cur_age_weighted_usd / cur.size_usd) \
                                   if cur.size_usd > 0 else 0
        walls.append(cur)

    # USD 阈值过滤（与 depth 路径一致的 C 级门槛）
    qualified = [w for w in walls if w.size_usd >= cfg["wall_min_usd"]]
    qualified.sort(key=lambda w: w.size_usd, reverse=True)
    return qualified[:cfg["max_walls_per_side"]]


def detect_walls_from_large_orders(
    large_orders: list[LargeOrderLifecycle], last_price: float, cfg: dict,
) -> list[_RawWall]:
    """L1 路径 B：从大单 lifecycle 找远距堆（双侧）。"""
    if last_price <= 0 or not large_orders:
        return []
    raw_asks = _detect_walls_from_large_orders_one_side(
        large_orders, "ask", last_price, cfg,
    )
    raw_bids = _detect_walls_from_large_orders_one_side(
        large_orders, "bid", last_price, cfg,
    )
    return raw_asks + raw_bids


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 共用：把 _RawWall 转成 PressureWall + 大单关联 + label
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _raw_to_pressure_wall(raw: _RawWall, last_price: float) -> PressureWall:
    """把内部 _RawWall 转换为对外 PressureWall（不含 strength_score/tier）。"""
    mid = (raw.price_lo + raw.price_hi) / 2
    label = "wall_ask" if raw.side == "ask" else "wall_bid"
    ids = list(raw.large_order_ids or [])
    # large_orders 路径来源的 wall 必然由 holding 大单组成 → 直接标记 has_active_whale。
    # 写在这里而非 tag_with_large_orders，避免 large_orders 列表为空时丢标记。
    has_whale_inherent = raw.source == "large_orders" and len(ids) > 0
    return PressureWall(
        side=raw.side,
        price_lo=raw.price_lo, price_hi=raw.price_hi,
        price_mid=round(mid, 4),
        distance_pct=round((mid - last_price) / last_price * 100, 4),
        size_usd=round(raw.size_usd, 2),
        size_base=round(raw.size_base, 4),
        source=raw.source,                     # type: ignore[arg-type]
        large_order_ids=ids,
        large_order_count=len(ids),
        holding_avg_age_sec=raw.holding_avg_age_sec,
        has_active_whale=has_whale_inherent,
        label=label,                            # type: ignore[arg-type]
    )


def tag_with_large_orders(
    walls: list[PressureWall],
    large_orders: list[LargeOrderLifecycle],
    last_price: float,
    cfg: dict,
) -> None:
    """给 depth_5m 路径的 walls 关联同价位 holding 大单（in-place）。

    large_orders 路径来源的 wall 已经自带 ids，本函数只补 depth_5m 路径。
    """
    if not walls or last_price <= 0:
        return
    if not large_orders:
        # 没有大单数据时无法补充 depth_5m 路径的 whale 标记；
        # large_orders 路径的 has_active_whale 已在 _raw_to_pressure_wall 写入。
        return
    tol = max(0.01, last_price * cfg["large_match_tol_pct"])
    for wall in walls:
        if wall.source == "large_orders":
            # 自带 large_order_ids 与 has_active_whale，无需再补
            continue
        ids: list[int] = []
        has_active = False
        ages: list[int] = []
        for lo in large_orders:
            if lo.side != wall.side:
                continue
            if not (wall.price_lo - tol <= lo.limit_price <= wall.price_hi + tol):
                continue
            ids.append(lo.id)
            if lo.state == "holding":
                has_active = True
                ages.append(int(lo.holding_age_sec))
        wall.large_order_ids = ids
        wall.large_order_count = len(ids)
        wall.has_active_whale = has_active
        if ages:
            wall.holding_avg_age_sec = int(sum(ages) / len(ages))


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# L2 — 与 absorption_zone 共振加成
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def augment_with_absorption(
    walls: list[PressureWall], absorption: Optional["AbsorptionSnapshot"],
    last_price: float, atr: Optional[float], cfg: dict,
) -> None:
    """如果 wall 价位 ±0.5×ATR 内有 absorption_zone，标记 confluence_with_absorption。

    本次重构后只打标记，不再直接加 confidence；强度加成由 _compute_strength_score 统一处理。
    """
    if not walls or absorption is None:
        return
    tol = (atr * cfg["absorption_atr_mult"]) if (atr and atr > 0) \
        else last_price * cfg["min_atr_pct_fallback"]
    sup_zones = list(absorption.zones_support or [])
    res_zones = list(absorption.zones_resistance or [])
    for wall in walls:
        zones = res_zones if wall.side == "ask" else sup_zones
        for z in zones:
            if abs(z.price - wall.price_mid) <= tol:
                wall.confluence_with_absorption = True
                wall.absorption_zone_price = z.price
                break


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# L3 — 强度评分（USD × 持续时间 × whale × absorption）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _duration_factor(holding_age_sec: int, source: str) -> float:
    """挂单持续时间 → 强度乘数。

    设计原则：
      - depth_5m 路径无法精确知挂单时长（5min 快照粒度太粗）→ 统一 1.0
      - large_orders 路径有精确时长 → 阶梯乘数：
            <1h    : 0.7   快闪挂单（spoof 风险高）
            1-6h   : 1.0   日内级
            6-24h  : 1.3   耐心资金
            1-7d   : 1.6   长线大单
            >7d    : 2.0   超长期挂单
    """
    if source != "large_orders":
        return 1.0
    h = holding_age_sec / 3600.0
    if h < 1:
        return 0.7
    if h < 6:
        return 1.0
    if h < 24:
        return 1.3
    if h < 24 * 7:
        return 1.6
    return 2.0


def _compute_strength_score(wall: PressureWall) -> float:
    """挂单强度评分（高于阈值即足够，并非精确到 100）。

    公式：
        score = size_usd × duration_factor × (1 + 0.3·whale) × (1 + 0.2·absorption)

    其中：
      - whale = 1 if has_active_whale else 0
      - absorption = 1 if confluence_with_absorption else 0
    """
    base = max(wall.size_usd, 0.0)
    duration = _duration_factor(wall.holding_avg_age_sec, wall.source)
    whale_mult = 1.3 if wall.has_active_whale else 1.0
    absorption_mult = 1.2 if wall.confluence_with_absorption else 1.0
    return base * duration * whale_mult * absorption_mult


def _assign_strength_tier(wall: PressureWall, cfg: dict) -> str:
    """按绝对 USD 阈值给 wall 分级（跨币种通用）。

    使用 strength_score（已含时间/whale/absorption 加成）而非 size_usd 原值，
    避免"size 大但挂了 30 秒"被误判为 S 级。
    """
    s = wall.strength_score
    if s >= cfg["tier_s_min_usd"]:
        return "S"
    if s >= cfg["tier_a_min_usd"]:
        return "A"
    if s >= cfg["tier_b_min_usd"]:
        return "B"
    return "C"


def _build_reason(wall: PressureWall) -> str:
    """生成中性简短摘要（前端 tooltip / 日志用）。"""
    parts: list[str] = []
    src_cn = "5m订单簿" if wall.source == "depth_5m" else "巨鲸大单"
    parts.append(f"📊 {src_cn}")
    if wall.source == "large_orders" and wall.holding_avg_age_sec > 0:
        h = wall.holding_avg_age_sec
        if h >= 86400:
            parts.append(f"已挂 {h // 86400} 天")
        elif h >= 3600:
            parts.append(f"已挂 {h // 3600} 小时")
        else:
            parts.append(f"已挂 {h // 60} 分钟")
    if wall.has_active_whale and wall.source == "depth_5m":
        parts.append(f"覆盖大单 ×{wall.large_order_count}")
    if wall.confluence_with_absorption:
        parts.append("✓ 吸收共振")
    return " · ".join(parts)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 顶层组装入口
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _build_summary(walls: list[PressureWall], snap: OrderbookPressureSnapshot) -> None:
    """填充 top_resistance / top_support。

    选取规则：S/A 级的最近 ask wall (=top_resistance) / S/A 级的最近 bid wall (=top_support)。
    无 S/A 级时退化为最近的 B 级；C 级不进入 top（强度太弱）。
    """
    def _pick(side: str) -> Optional[float]:
        same = [w for w in walls if w.side == side]
        if not same:
            return None
        # 优先 S/A
        sa = [w for w in same if w.strength_tier in ("S", "A")]
        candidates = sa or [w for w in same if w.strength_tier == "B"]
        if not candidates:
            return None
        candidates.sort(key=lambda w: abs(w.distance_pct))
        return candidates[0].price_mid

    snap.top_resistance = _pick("ask")
    snap.top_support = _pick("bid")


def compute_pressure_snapshot(
    state: "CoinState",
    cfg_overrides: Optional[dict] = None,
    now_sec: Optional[int] = None,
) -> Optional[OrderbookPressureSnapshot]:
    """挂单压力监测器对外主入口。

    依赖 state 字段：
      - ticker.last (必需)
      - orderbook_depth_snapshot (近+中距路径)
      - large_orders_history (远距路径 + depth 路径标记 whale)
      - footprint_contract / footprint_spot (L2 absorption 共振)
      - atr (容差归一)

    返回 None 表示数据不足。本函数不抛异常。
    """
    if state is None or not getattr(state, "ticker", None):
        return None
    last_price = float(state.ticker.last)
    if last_price <= 0:
        return None

    cfg = {**DEFAULTS, **(cfg_overrides or {})}
    now = int(now_sec if now_sec is not None else time.time())

    depth: Optional[OrderbookDepthSnapshot] = getattr(state, "orderbook_depth_snapshot", None)
    large_orders: list[LargeOrderLifecycle] = list(
        getattr(state, "large_orders_history", []) or [])

    # 路径 A：5m 订单簿（近+中距 ≤4%）
    depth_raws: list[_RawWall] = []
    if depth is not None and (depth.bids or depth.asks):
        depth_raws = detect_walls_from_depth(depth, last_price, cfg)

    # 路径 B：大单 lifecycle（远距 4-12%）
    large_raws: list[_RawWall] = []
    if large_orders:
        large_raws = detect_walls_from_large_orders(large_orders, last_price, cfg)

    # 数据完全缺失（两路都空）→ 返回空 snapshot 但不报错
    if not depth_raws and not large_raws:
        return OrderbookPressureSnapshot(
            coin=state.coin, ts_sec=now, last_price=last_price,
            atr=getattr(state, "atr", None) or None,
            walls=[], data_quality="missing",
            sample_count_depth=2 if (depth and depth.prev_ts_sec) else (1 if depth else 0),
            sample_count_large_history=len(large_orders),
            sample_count_large_orders_walls=0,
            notes=["no_walls_in_range"],
        )

    # 转 PressureWall
    walls: list[PressureWall] = []
    for raw in depth_raws + large_raws:
        walls.append(_raw_to_pressure_wall(raw, last_price))

    # depth 路径补 has_active_whale 标记 + holding_avg_age
    tag_with_large_orders(walls, large_orders, last_price, cfg)

    # absorption 共振打标
    absorption = _load_absorption(state, last_price, now)
    augment_with_absorption(walls, absorption, last_price,
                            getattr(state, "atr", None) or None, cfg)

    # 计算强度 score → tier
    for wall in walls:
        wall.strength_score = round(_compute_strength_score(wall), 2)
        wall.strength_tier = _assign_strength_tier(wall, cfg)        # type: ignore[assignment]
        wall.reason = _build_reason(wall)

    # 同侧按 strength_score 重新排名
    walls.sort(key=lambda w: (w.side, -w.strength_score))
    rank_by_side: dict[str, int] = {"ask": 0, "bid": 0}
    for w in walls:
        rank_by_side[w.side] += 1
        w.rank = rank_by_side[w.side]

    snap = OrderbookPressureSnapshot(
        coin=state.coin, ts_sec=now, last_price=last_price,
        atr=getattr(state, "atr", None) or None, walls=walls,
        sample_count_depth=2 if (depth and depth.prev_ts_sec) else (1 if depth else 0),
        sample_count_large_history=len(large_orders),
        sample_count_large_orders_walls=sum(1 for w in walls if w.source == "large_orders"),
        data_quality="ok",
    )
    if not large_orders:
        snap.notes.append("no_large_orders_history")
        snap.data_quality = "partial"
    if depth is None or not (depth.bids or depth.asks):
        snap.notes.append("no_depth_snapshot")
        snap.data_quality = "partial"

    # depth 主源陈旧判定：覆盖 partial（stale 比 partial 更严重，需提示前端注意）
    stale_age = float(cfg.get("stale_age_sec", DEFAULTS["stale_age_sec"]))
    if depth is not None and depth.ts_sec > 0:
        depth_age = max(0, now - int(depth.ts_sec))
        if depth_age > stale_age:
            snap.data_quality = "stale"
            snap.notes.append(f"depth_age_{depth_age}s_gt_{int(stale_age)}s")

    _build_summary(walls, snap)
    return snap


def _load_absorption(
    state: "CoinState", last_price: float, now_sec: int,
) -> Optional["AbsorptionSnapshot"]:
    """复用 absorption_detector，不重复请求 footprint。"""
    fp_c = list(getattr(state, "footprint_contract", []) or [])
    fp_s = list(getattr(state, "footprint_spot", []) or [])
    if not fp_c and not fp_s:
        return None
    try:
        from processors.absorption_detector import detect_absorption_zones
        return detect_absorption_zones(fp_c, fp_s, last_price, now_ts=now_sec)
    except Exception as exc:    # 防御：absorption 异常不能影响主流程
        logger.warning("absorption_detector 调用失败 | coin=%s err=%s", state.coin, exc)
        return None
