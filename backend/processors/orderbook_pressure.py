"""挂单压力监测器 (Orderbook Pressure Monitor) · 核心算法

四层模型：
  L1  detect_walls            从 depth heatmap 找 ±2% 内的挂单堆 (top 20% & ≥ $500K)
  L1+ tag_with_large_orders   把同价位的大单 lifecycle 关联到 wall 上
  L2  classify_change         撤单 vs 被吃 (优先看大单 executed/cancelled 比例)
  L3  classify_pressure       30min 价格反应 + CVD 方向 → real_R / fake_R / fake_R_break / ...
  L4  augment_with_absorption 与 footprint absorption_zone 共振则 +25 置信度

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
    WallChangeKind,
    WallLabel,
    WallSide,
)

if TYPE_CHECKING:
    from engine import CoinState
    from models.market_action import AbsorptionSnapshot

logger = logging.getLogger(__name__)

# ── 默认阈值（与文档/README 同源；可由 settings 覆盖） ─────────────────────
DEFAULTS = {
    "range_pct": 12.0,                # ±12% 价格带筛选（与 KL 距离段 0.25-1.5/1.5-4/4-12% 对齐）
    "wall_size_top_pct": 0.20,        # top 20% by USD
    "wall_min_usd": 500_000.0,        # 单个 wall ≥ $500K
    "merge_tol_pct": 0.0005,          # 合并同价位 ±0.05%
    "max_walls_per_side": 8,          # 每侧最多输出 8 个堆
    "large_match_tol_pct": 0.0010,    # 大单关联 ±0.10%
    "eaten_threshold": 0.70,          # executed/start ≥ 0.7 → eaten
    "cancelled_threshold": 0.30,      # executed/start ≤ 0.3 + 减量大 → cancelled
    "react_window_sec": 1800,         # 价格反应窗口 30 min
    "absorption_atr_mult": 0.5,       # L4 共振价位容差 ±0.5×ATR
    "min_atr_pct_fallback": 0.005,    # 无 ATR 时退化为 0.5% 价距
}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# L1 — 找堆 (从 depth heatmap)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dataclass
class _RawWall:
    side: WallSide
    price_lo: float
    price_hi: float
    size_usd: float
    size_base: float
    bin_count: int


def _filter_bins_by_range(bins: list[DepthBin], last_price: float,
                          range_pct: float, side: WallSide) -> list[DepthBin]:
    """只保留 ±range_pct 内 + 同方向的 bins。

    ask 必须 > last_price；bid 必须 < last_price，避免穿越中线的乱兵。
    """
    if last_price <= 0 or not bins:
        return []
    half = last_price * range_pct / 100.0
    lo = last_price - half
    hi = last_price + half
    out = []
    for b in bins:
        if not (lo <= b.price <= hi):
            continue
        if side == "ask" and b.price < last_price:
            continue
        if side == "bid" and b.price > last_price:
            continue
        out.append(b)
    return out


def _detect_walls_one_side(
    bins: list[DepthBin], side: WallSide, last_price: float, cfg: dict,
) -> list[_RawWall]:
    """对单侧 bins 做：阈值过滤 → 同价位合并 → 强度排序。

    阈值为 (top X% by USD) AND (USD ≥ MIN_USD) 双闸，避免冷清时段误判 +
    极端噪声。
    """
    band = _filter_bins_by_range(bins, last_price, cfg["range_pct"], side)
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
                           size_usd=b.usd_value, size_base=b.quantity, bin_count=1)
            continue
        if b.price - cur.price_hi <= merge_tol:
            cur.price_hi = b.price
            cur.size_usd += b.usd_value
            cur.size_base += b.quantity
            cur.bin_count += 1
        else:
            walls.append(cur)
            cur = _RawWall(side=side, price_lo=b.price, price_hi=b.price,
                           size_usd=b.usd_value, size_base=b.quantity, bin_count=1)
    if cur is not None:
        walls.append(cur)

    walls.sort(key=lambda w: w.size_usd, reverse=True)
    return walls[:cfg["max_walls_per_side"]]


def detect_walls(
    depth: OrderbookDepthSnapshot, last_price: float, cfg: dict,
) -> list[PressureWall]:
    """L1 主入口：返回按 size 降序排好的 PressureWall 列表（含 ask + bid）。"""
    if last_price <= 0 or depth is None:
        return []

    raw_asks = _detect_walls_one_side(depth.asks, "ask", last_price, cfg)
    raw_bids = _detect_walls_one_side(depth.bids, "bid", last_price, cfg)

    walls: list[PressureWall] = []
    for rank, raw in enumerate(raw_asks, start=1):
        mid = (raw.price_lo + raw.price_hi) / 2
        walls.append(PressureWall(
            side="ask", price_lo=raw.price_lo, price_hi=raw.price_hi,
            price_mid=round(mid, 4),
            distance_pct=round((mid - last_price) / last_price * 100, 4),
            size_usd=round(raw.size_usd, 2),
            size_base=round(raw.size_base, 4), rank=rank,
        ))
    for rank, raw in enumerate(raw_bids, start=1):
        mid = (raw.price_lo + raw.price_hi) / 2
        walls.append(PressureWall(
            side="bid", price_lo=raw.price_lo, price_hi=raw.price_hi,
            price_mid=round(mid, 4),
            distance_pct=round((mid - last_price) / last_price * 100, 4),
            size_usd=round(raw.size_usd, 2),
            size_base=round(raw.size_base, 4), rank=rank,
        ))
    return walls


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# L1+ — 关联大单 lifecycle
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def tag_with_large_orders(
    walls: list[PressureWall],
    large_orders: list[LargeOrderLifecycle],
    last_price: float,
    cfg: dict,
) -> None:
    """把每个 wall 价位区间 ±0.10% 内的大单关联进去（in-place）。

    side 必须匹配（ask wall 只关联 ask 大单，避免对侧污染）。
    """
    if not walls or not large_orders or last_price <= 0:
        return
    tol = max(0.01, last_price * cfg["large_match_tol_pct"])
    for wall in walls:
        ids: list[int] = []
        has_active = False
        for lo in large_orders:
            if lo.side != wall.side:
                continue
            if not (wall.price_lo - tol <= lo.limit_price <= wall.price_hi + tol):
                continue
            ids.append(lo.id)
            if lo.state == "holding":
                has_active = True
        wall.large_order_ids = ids
        wall.large_order_count = len(ids)
        wall.has_active_whale = has_active


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# L2 — 撤单 vs 被吃
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _sum_bins_in_range(bins: list[DepthBin], price_lo: float, price_hi: float) -> float:
    """把指定价格区间内 bins 的 usd 总额加起来（用于 prev/latest 减量对比）。"""
    return sum(b.usd_value for b in bins if price_lo <= b.price <= price_hi)


def _taker_in_window(
    taker_series: list[dict], window_start_sec: int, side: WallSide,
) -> float:
    """同窗口内的同向主动成交 USD（attack 该 wall 的方向）。

    上方卖墙 (ask) → 关注主动买盘吃掉的量
    下方买墙 (bid) → 关注主动卖盘吃掉的量
    """
    if not taker_series:
        return 0.0
    key = "buy_usd" if side == "ask" else "sell_usd"
    total = 0.0
    for pt in taker_series:
        try:
            ts = int(pt.get("ts", 0))
            if ts >= window_start_sec:
                total += float(pt.get(key, 0) or 0)
        except (TypeError, ValueError):
            continue
    return total


def _classify_by_large_orders(
    wall: PressureWall, large_orders: list[LargeOrderLifecycle],
    react_window_sec: int, now_sec: int, cfg: dict,
) -> Optional[WallChangeKind]:
    """大单优先路径：在反应窗口内变化的大单加权判断。"""
    relevant: list[LargeOrderLifecycle] = []
    cutoff_ms = (now_sec - react_window_sec) * 1000
    for lo in large_orders:
        if lo.id not in wall.large_order_ids:
            continue
        # 仅看窗口内有变化的：start_time 在窗口内，或 end_time 在窗口内
        in_win = (lo.start_time_ms >= cutoff_ms) or \
                 (lo.end_time_ms is not None and lo.end_time_ms >= cutoff_ms) or \
                 (lo.state == "holding")
        if in_win:
            relevant.append(lo)
    if not relevant:
        return None

    total_start = sum(lo.start_quantity for lo in relevant)
    if total_start <= 0:
        return None
    eaten = sum(lo.executed_volume for lo in relevant)
    cancelled = sum(lo.cancelled_quantity for lo in relevant)
    holding = sum(lo.current_quantity for lo in relevant if lo.state == "holding")

    eaten_ratio = eaten / total_start
    cancelled_ratio = cancelled / total_start
    holding_ratio = holding / total_start

    # 写入 USD 维度便于前端展示
    wall.eaten_usd = round(sum(lo.executed_usd_value for lo in relevant), 2)
    wall.cancelled_usd = round(
        sum(max(lo.start_usd_value - lo.current_usd_value - lo.executed_usd_value, 0.0)
            for lo in relevant), 2,
    )

    if eaten_ratio >= cfg["eaten_threshold"]:
        return "eaten"
    if cancelled_ratio >= cfg["eaten_threshold"]:   # 撤单 ≥70% 也算明确撤单
        return "cancelled"
    if eaten_ratio <= cfg["cancelled_threshold"] and cancelled_ratio <= cfg["cancelled_threshold"] \
            and holding_ratio >= 0.5:
        return "holding"
    return "partial"


def _classify_by_depth_delta(
    wall: PressureWall, depth: OrderbookDepthSnapshot, taker_series: list[dict],
    react_window_sec: int, now_sec: int,
) -> WallChangeKind:
    """无大单覆盖的散堆路径：用 prev/latest 减量 vs 同窗口主动成交对比。"""
    if not depth.prev_ts_sec:
        return "unknown"
    prev_bins = depth.prev_asks if wall.side == "ask" else depth.prev_bids
    cur_bins = depth.asks if wall.side == "ask" else depth.bids

    prev_usd = _sum_bins_in_range(prev_bins, wall.price_lo, wall.price_hi)
    cur_usd = _sum_bins_in_range(cur_bins, wall.price_lo, wall.price_hi)
    delta_usd = cur_usd - prev_usd
    wall.delta_usd = round(delta_usd, 2)

    if delta_usd > 0 and abs(delta_usd) >= prev_usd * 0.1:
        return "growing"
    if abs(delta_usd) < prev_usd * 0.1:
        return "holding"

    reduction = -delta_usd  # > 0
    window_start = now_sec - react_window_sec
    taker_usd = _taker_in_window(taker_series, window_start, wall.side)
    # eaten 标准：减少量 ≤ 主动成交（含 30% 容差）
    if reduction <= taker_usd * 1.3:
        wall.eaten_usd = round(reduction, 2)
        return "eaten"
    # cancelled 标准：减少量 >> 主动成交（≥ 1.5×）
    if reduction >= taker_usd * 1.5:
        wall.cancelled_usd = round(reduction - taker_usd, 2)
        wall.eaten_usd = round(taker_usd, 2)
        return "cancelled"
    # 介于之间 → partial
    wall.eaten_usd = round(min(reduction, taker_usd), 2)
    wall.cancelled_usd = round(max(reduction - taker_usd, 0.0), 2)
    return "partial"


def classify_change(
    walls: list[PressureWall],
    depth: OrderbookDepthSnapshot,
    large_orders: list[LargeOrderLifecycle],
    taker_series: list[dict],
    cfg: dict,
    now_sec: int,
) -> None:
    """L2 主入口：in-place 写入 wall.change_kind / eaten_usd / cancelled_usd。"""
    react = cfg["react_window_sec"]
    for wall in walls:
        kind: Optional[WallChangeKind] = None
        if wall.large_order_count > 0:
            kind = _classify_by_large_orders(wall, large_orders, react, now_sec, cfg)
        if kind is None:
            kind = _classify_by_depth_delta(wall, depth, taker_series, react, now_sec)
        wall.change_kind = kind


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# L3 — 真假阻力/支撑分类
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _price_reaction_in_window(
    candles_15m: list, wall: PressureWall, react_window_sec: int, now_sec: int,
) -> dict:
    """在反应窗口内统计：是否触及 wall、是否被压回 / 突破。

    返回 {touched, broken_up, broken_down, max_high, min_low, last_close}
    """
    out = {"touched": False, "broken_up": False, "broken_down": False,
           "max_high": 0.0, "min_low": 0.0, "last_close": 0.0}
    if not candles_15m:
        return out
    cutoff = now_sec - react_window_sec
    highs, lows, closes = [], [], []
    for c in candles_15m:
        try:
            ts = int(getattr(c, "ts", 0))
            ts_sec = ts // 1000 if ts > 10_000_000_000 else ts
            if ts_sec < cutoff:
                continue
            highs.append(float(c.high))
            lows.append(float(c.low))
            closes.append(float(c.close))
        except (TypeError, ValueError, AttributeError):
            continue
    if not highs:
        return out
    out["max_high"] = max(highs)
    out["min_low"] = min(lows)
    out["last_close"] = closes[-1]
    if wall.side == "ask":
        out["touched"] = out["max_high"] >= wall.price_lo
        # 突破：连续 close > price_hi
        out["broken_up"] = out["last_close"] > wall.price_hi and any(
            c > wall.price_hi for c in closes[-2:]
        )
    else:
        out["touched"] = out["min_low"] <= wall.price_hi
        out["broken_down"] = out["last_close"] < wall.price_lo and any(
            c < wall.price_lo for c in closes[-2:]
        )
    return out


def _cvd_state(state: "CoinState") -> Optional[str]:
    cvd = state.cvd_contract
    return cvd.trend_1h if cvd else None


def classify_pressure(
    walls: list[PressureWall], state: "CoinState", cfg: dict, now_sec: int,
) -> None:
    """L3 主入口：综合 change_kind + 价格反应 + CVD → label / confidence (in-place)。

    分类矩阵（卖墙 ask）：
      eaten     + touched + 未破 + CVD↑           → real_R   (60-90)
      eaten     + touched + 已破                   → fake_R_break (50)
      cancelled + 未触                              → fake_R   (40)
      cancelled + 已破                              → fake_R_break (30)
      holding   + 未触                              → untested
      holding   + 触及 + 未破                       → real_R 候选 (50)
      growing                                       → real_R 候选 (55)
      partial   + 触及 + 未破                       → real_R 候选 (60)
      partial   + 触及 + 已破                       → fake_R_break (45)
      partial   + 未触                              → untested (40, 高于 holding+untouched)
    bid 对称。
    """
    candles_15m = state.candles_15m or []
    cvd_dir = _cvd_state(state)
    for wall in walls:
        react = _price_reaction_in_window(candles_15m, wall, cfg["react_window_sec"], now_sec)
        wall.cvd_state = cvd_dir
        label, conf, reason = _label_one(wall, react, cvd_dir)
        wall.label = label
        wall.confidence = conf
        wall.reason = reason


def _label_one(wall: PressureWall, react: dict, cvd_dir: Optional[str]) -> tuple[WallLabel, int, str]:
    side = wall.side
    kind = wall.change_kind
    touched = react["touched"]
    broken = react["broken_up"] if side == "ask" else react["broken_down"]

    # ── ask（上方卖墙 = 阻力候选） ──
    if side == "ask":
        if kind == "eaten" and touched and not broken:
            base = 70
            if cvd_dir == "rising":
                base += 15  # 主动买盘强但价格被压住 = 经典吸收
            return ("real_R", min(base, 95), f"卖墙被吃但价格被压住 (CVD={cvd_dir or '?'})")
        if kind == "eaten" and touched and broken:
            return ("fake_R_break", 50, "卖墙被吃且价格已突破，墙已失效")
        if kind == "cancelled" and not touched:
            return ("fake_R", 40, "卖墙撤单且价格未到，疑似 spoof")
        if kind == "cancelled" and broken:
            return ("fake_R_break", 35, "卖墙撤单后价格突破，spoof 已确认")
        if kind == "growing":
            return ("real_R", 55, "卖墙正在堆积")
        # partial: 部分被吃 + 部分撤单/留存（混合状态，介于 eaten 和 holding 之间）
        if kind == "partial" and touched and not broken:
            return ("real_R", 60, "卖墙部分被吃但价格被压住")
        if kind == "partial" and touched and broken:
            return ("fake_R_break", 45, "卖墙部分被吃且价格已突破")
        if kind == "partial" and not touched:
            return ("untested", 40, "卖墙部分减量但价格未到")
        if kind == "holding" and touched and not broken:
            return ("real_R", 50, "卖墙挂着且价格触及未破")
        if kind == "holding" and not touched:
            return ("untested", 30, "卖墙挂着但价格未到")
        return ("untested", 25, f"卖墙状态={kind}")

    # ── bid（下方买墙 = 支撑候选） ──
    if kind == "eaten" and touched and not broken:
        base = 70
        if cvd_dir == "declining":
            base += 15
        return ("real_S", min(base, 95), f"买墙被吃但价格守住 (CVD={cvd_dir or '?'})")
    if kind == "eaten" and touched and broken:
        return ("fake_S_break", 50, "买墙被吃且价格已跌穿，墙已失效")
    if kind == "cancelled" and not touched:
        return ("fake_S", 40, "买墙撤单且价格未到，疑似 spoof")
    if kind == "cancelled" and broken:
        return ("fake_S_break", 35, "买墙撤单后价格跌穿，spoof 已确认")
    if kind == "growing":
        return ("real_S", 55, "买墙正在堆积")
    # partial: 部分被吃 + 部分撤单/留存（混合状态，介于 eaten 和 holding 之间）
    if kind == "partial" and touched and not broken:
        return ("real_S", 60, "买墙部分被吃但价格守住")
    if kind == "partial" and touched and broken:
        return ("fake_S_break", 45, "买墙部分被吃且价格已跌穿")
    if kind == "partial" and not touched:
        return ("untested", 40, "买墙部分减量但价格未到")
    if kind == "holding" and touched and not broken:
        return ("real_S", 50, "买墙挂着且价格触及未破")
    if kind == "holding" and not touched:
        return ("untested", 30, "买墙挂着但价格未到")
    return ("untested", 25, f"买墙状态={kind}")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# L4 — 与 absorption_zone 共振
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def augment_with_absorption(
    walls: list[PressureWall], absorption: Optional["AbsorptionSnapshot"],
    last_price: float, atr: Optional[float], cfg: dict,
) -> None:
    """如果 wall 价位 ±0.5×ATR 内有 absorption_zone，标记 confluence + confidence +25。"""
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
                wall.confidence = min(wall.confidence + 25, 100)
                if wall.reason:
                    wall.reason += " | 与 absorption_zone 共振"
                break


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 强度等级派生（与 KeyLevelV2.strength_tier 视觉语言对齐）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _assign_strength_tier(wall: PressureWall) -> str:
    """根据 wall.confidence + label + 共振 派生 S/A/B/C。

    设计要点：
      - real_R/real_S 才能进 S/A（spoof/已破墙不能给高级别，避免误导）
      - 共振（大单关联 或 absorption_zone）有 +1 等级 boost
      - fake_*_break (墙已失效) 强制 C
      - 阈值与关键位 displayScore→tier 风格对齐：≥85→S, ≥70→A, ≥50→B
    """
    c = wall.confidence
    label = wall.label

    # 已失效/已突破的墙：操作意义低，统一 C
    if label in ("fake_R_break", "fake_S_break"):
        return "C"

    # spoof / untested：封顶 B（不能误导成 S/A）
    if label in ("fake_R", "fake_S", "untested"):
        return "B" if c >= 50 else "C"

    # real_R / real_S：可达 S/A
    boost = bool(wall.large_order_count > 0 or wall.confluence_with_absorption)
    if c >= 85 or (c >= 75 and boost):
        return "S"
    if c >= 70:
        return "A"
    if c >= 50:
        return "B"
    return "C"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 顶层组装入口
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _build_summary(walls: list[PressureWall], snap: OrderbookPressureSnapshot) -> None:
    """填充 top_resistance / top_support / has_*_above|below 等汇总字段。"""
    real_R = [w for w in walls if w.side == "ask" and w.label == "real_R"]
    real_S = [w for w in walls if w.side == "bid" and w.label == "real_S"]
    real_R.sort(key=lambda w: abs(w.distance_pct))
    real_S.sort(key=lambda w: abs(w.distance_pct))
    snap.top_resistance = real_R[0].price_mid if real_R else None
    snap.top_support = real_S[0].price_mid if real_S else None
    snap.has_real_pressure_above = bool(real_R)
    snap.has_real_pressure_below = bool(real_S)
    snap.has_fake_break_above = any(
        w.side == "ask" and w.label == "fake_R_break" for w in walls
    )
    snap.has_fake_break_below = any(
        w.side == "bid" and w.label == "fake_S_break" for w in walls
    )


def compute_pressure_snapshot(
    state: "CoinState",
    cfg_overrides: Optional[dict] = None,
    now_sec: Optional[int] = None,
) -> Optional[OrderbookPressureSnapshot]:
    """挂单压力监测器对外主入口。

    依赖 state 字段：
      - ticker.last (必需)
      - orderbook_depth_snapshot (必需，由 polls/orderbook_pressure.py 写入)
      - large_orders_history (可选，提升 L2 精度)
      - taker_contract_series (L2 散堆减量备用路径)
      - candles_15m (L3 价格反应)
      - cvd_contract (L3 CVD 佐证)
      - footprint_contract / footprint_spot (L4 absorption 共振)
      - atr (L4 容差归一)

    返回 None 表示数据不足无法计算（不会抛异常）。
    """
    if state is None or not getattr(state, "ticker", None):
        return None
    last_price = float(state.ticker.last)
    if last_price <= 0:
        return None
    depth: Optional[OrderbookDepthSnapshot] = getattr(state, "orderbook_depth_snapshot", None)
    if depth is None or (not depth.bids and not depth.asks):
        return None

    cfg = {**DEFAULTS, **(cfg_overrides or {})}
    now = int(now_sec if now_sec is not None else time.time())

    # L1
    walls = detect_walls(depth, last_price, cfg)
    if not walls:
        snap = OrderbookPressureSnapshot(
            coin=state.coin, ts_sec=now, last_price=last_price,
            atr=getattr(state, "atr", None) or None,
            walls=[], data_quality="ok",
            sample_count_depth=2 if depth.prev_ts_sec else 1,
            sample_count_large_history=len(getattr(state, "large_orders_history", []) or []),
            notes=["no_walls_in_range"],
        )
        return snap

    # L1+
    large_orders: list[LargeOrderLifecycle] = list(
        getattr(state, "large_orders_history", []) or [])
    tag_with_large_orders(walls, large_orders, last_price, cfg)

    # L2
    taker_series: list[dict] = list(getattr(state, "taker_contract_series", []) or [])
    classify_change(walls, depth, large_orders, taker_series, cfg, now)

    # L3
    classify_pressure(walls, state, cfg, now)

    # L4
    absorption = _load_absorption(state, last_price, now)
    augment_with_absorption(walls, absorption, last_price,
                            getattr(state, "atr", None) or None, cfg)

    # 派生强度 tier（在 L1-L4 全部完成、confidence/label 稳定后再算）
    for wall in walls:
        wall.strength_tier = _assign_strength_tier(wall)

    snap = OrderbookPressureSnapshot(
        coin=state.coin, ts_sec=now, last_price=last_price,
        atr=getattr(state, "atr", None) or None, walls=walls,
        sample_count_depth=2 if depth.prev_ts_sec else 1,
        sample_count_large_history=len(large_orders),
        data_quality="ok" if depth.prev_ts_sec else "partial",
    )
    if not depth.prev_ts_sec:
        snap.notes.append("no_prev_depth_snapshot")
    if not large_orders:
        snap.notes.append("no_large_orders_history")
    if not state.candles_15m:
        snap.notes.append("no_15m_candles")
        snap.data_quality = "partial"
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
