"""流动性墙引擎（M1+M2）—— 现有 OP 模块基础上的能力升级层。

外部入口：``build_liquidity_wall_outputs(state, base_snap, cfg, now)``

内部分两组：
  M1（墙观测）
    - _resolve_merge_pct                  合并容差 = clamp(0.15×ATR/price, 0.05%, 0.30%)
    - _filter_bins_by_distance            按距离百分比过滤
    - _merge_adjacent_bins_to_zones       相邻 bin → 墙区原始结构
    - _compute_zone_history_stats         current/max/avg/persistence/first/last_seen/trend
    - _classify_zone_trend                strengthening/weakening/stable/new

  M2（行为/拥挤/磁铁）
    - _detect_zone_lifecycle_events       6 类事件（appeared/strengthened/weakened/removed/consumed/reloaded）
    - _compute_wall_consumed_confidence   GPT 加权公式（large_order 0.5 + taker 0.25 + price 0.25）
    - _compute_wall_removal_risk          0-1 软分（不输出"假单"）
    - _classify_zone_status               基于事件 + 强度差分推断 status
    - _build_position_crowding            从 state.oi_exchange_rank.all_aggregated + multi_funding + ls_ratio
    - _classify_oi_margin_split           U 本位 vs 币本位 OI 分流（GPT 提出的新洞察）
    - _classify_inferred_position_state   多空开仓/平仓/清算（按价格×OI×taker 矩阵）
    - _build_sweep_target                 next_magnet = liq_max_pain.{long,short}_pain_price
    - _compute_break_through_risk         0-1 综合分

约束：
  - 只读 state，不写其他模块字段
  - 暖机期 30min（可配置）内 data_quality=warming，前端不显 persistence/magnet 数字
  - 不修改 KL 的 final_score / strength_tier / cascade_risk（铁律）
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, NamedTuple, Optional, Sequence

from models.orderbook_pressure import (
    DepthBin,
    InferredPositionState,
    LargeOrderLifecycle,
    OIMarginSplit,
    OrderbookDepthSnapshot,
    OrderbookPressureSnapshot,
    PositionCrowdingSnapshot,
    SweepTarget,
    WallEvent,
    WallEventType,
    WallSide,
    WallZone,
    WallZoneSource,
    WallZoneStatus,
    WallZoneTrend,
)

if TYPE_CHECKING:
    from engine import CoinState

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────
# 默认配置（可由 cfg_overrides / settings 覆盖）
# ──────────────────────────────────────────────────────────────────────
ENGINE_DEFAULTS: dict[str, Any] = {
    # M1：墙区聚合
    "merge_pct_atr_mult": 0.15,            # 0.15 × ATR/price
    "merge_pct_min": 0.0005,               # 0.05% 下限
    "merge_pct_max": 0.0030,               # 0.30% 上限
    "wall_min_usd": 500_000.0,             # 单墙区最低 USD（C 级门槛）
    "seed_min_usd": 1_000_000.0,           # 种子 bin 最低 USD（≥ 此阈值才算"显著厚度"）
    "top_seed_count": 30,                  # 每侧 top USD 的 bin 数（防种子膨胀）
    "max_zones_per_side": 5,               # 每侧最多输出 5 个墙区
    "max_distance_pct_for_zone": 12.0,     # 仅看 ±12% 内
    "history_window_minutes": 60,          # 1h 滚动（5m × 12）
    "augment_match_tol_pct": 0.001,        # 大单 ↔ zone 容差匹配 0.1%
    "augment_match_tol_usd_min": 5.0,      # 容差最低 5 USD（防小币太小不够）
    # M2.5 + Phase A + Phase C：trust_score 计算权重（多维度独立累加，最终 clamp 到 1.0）
    "trust_base": 0.50,                    # 仅合约源（默认）
    "trust_bonus_dual_source": 0.30,       # Phase A：现货+合约 5m 双源共振（最强单一证据）
    "trust_bonus_spot_confluence": 0.15,   # 现货大单 lifecycle 共振
    "trust_bonus_multi_exchange": 0.10,    # 多家交易所共振（保留兼容，当前未触发；详见 audit）
    "trust_bonus_persistent": 0.10,        # 持续 ≥ 0.7
    "trust_persistent_threshold": 0.70,
    # Phase C：Coinbase 现货原生 API（机构资金独立验证维度）
    "trust_bonus_coinbase_confluence": 0.10,   # Coinbase 同价位有 ≥ ratio×wall_min 厚度时加分
    "coinbase_min_usd_ratio": 0.30,            # Coinbase USD 至少为 wall_min_usd 的 30%（兼顾 USD/USDT 价差）
    "coinbase_min_num_orders": 3,              # 至少 3 笔订单聚集（< 3 视为单大单 spoof 嫌疑）
    "coinbase_match_tol_pct": 0.0010,          # 价位匹配容差 10 bp（吸收 USD/USDT spread）

    # M1：persistence / trend
    "warming_seconds": 1800,               # 30min 暖机期内不出 persistence/magnet
    "persistence_target_minutes": 45,      # 持续 45min = 满分 1.0
    "trend_strengthening_pct": 0.20,       # current vs avg +20% 算 strengthening
    "trend_weakening_pct": -0.15,          # -15% 算 weakening

    # M2：事件识别
    "reload_window_seconds": 60,           # consume 后 60s 内同价位重挂 = reloaded
    "reload_price_tol_pct": 0.0010,        # 同价位 ±0.10%

    # M2：consumed_confidence 加权（GPT 公式）
    "consumed_lo_weight": 0.50,
    "consumed_taker_weight": 0.25,
    "consumed_price_weight": 0.25,
    "consumed_lo_full_threshold": 0.30,    # large_order 已成交 ≥30% wall 总厚度 → 满分

    # M2：crowding 阈值
    "funding_high_pct": 0.05,              # ≥ 0.05% (8h) → high
    "funding_low_pct": -0.02,              # ≤ -0.02% → low/short_crowded
    "ls_extreme_high": 2.5,                # 多空比 > 2.5 = top_long
    "ls_extreme_low": 0.4,                 # < 0.4 = top_short
    "oi_delta_strong_pct": 1.0,            # 单周期 ≥ 1% 算显著
    "oi_margin_dominant_ratio": 0.65,      # > 65% 一方 = dominant

    # M2：sweep & break_through
    "vacuum_max_distance_pct": 1.5,        # 真空跨度上限
    "break_through_thinning_threshold": 0.5,   # current/max < 0.5 算 thinning
}


# ──────────────────────────────────────────────────────────────────────
# 中间数据结构 + 输出
# ──────────────────────────────────────────────────────────────────────
@dataclass
class _RawZone:
    """build_wall_zones 阶段的中间 raw（M1）。"""
    side: WallSide
    price_low: float
    price_high: float
    peak_price: float
    peak_usd: float
    total_usd: float
    total_qty: float
    bin_count: int


class _BuildOutputs(NamedTuple):
    walls_above: list[WallZone]
    walls_below: list[WallZone]
    zones: list[WallZone]
    events: list[WallEvent]
    crowding: Optional[PositionCrowdingSnapshot]
    warming: bool
    window_min: int
    history_size: int
    usd_usdt_basis_pct: Optional[float] = None  # W2-T4：Coinbase USD vs futures USDT 基差


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# M1：墙区聚合 + 持续性
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _resolve_merge_pct(atr: Optional[float], last_price: float, cfg: dict) -> float:
    """合并容差 = clamp(0.15 × ATR / price, 0.05%, 0.30%)。"""
    mp_min = cfg.get("merge_pct_min", ENGINE_DEFAULTS["merge_pct_min"])
    mp_max = cfg.get("merge_pct_max", ENGINE_DEFAULTS["merge_pct_max"])
    mult = cfg.get("merge_pct_atr_mult", ENGINE_DEFAULTS["merge_pct_atr_mult"])
    if atr and atr > 0 and last_price > 0:
        raw = float(mult) * float(atr) / float(last_price)
    else:
        raw = mp_min
    return max(mp_min, min(mp_max, raw))


def _filter_bins_by_distance(
    bins: Sequence[DepthBin],
    last_price: float,
    side: WallSide,
    max_distance_pct: float,
) -> list[DepthBin]:
    """卖墙看上方、买墙看下方，按距离百分比过滤。"""
    out: list[DepthBin] = []
    if last_price <= 0:
        return out
    max_pct = max_distance_pct / 100.0
    for b in bins:
        if b.price <= 0:
            continue
        rel = (b.price - last_price) / last_price
        if side == "ask" and 0 < rel <= max_pct:
            out.append(b)
        elif side == "bid" and -max_pct <= rel < 0:
            out.append(b)
    return out


def _merge_adjacent_bins_to_zones(
    bins: list[DepthBin], merge_pct: float, side: WallSide,
) -> list[_RawZone]:
    """相邻 bin（gap ≤ merge_pct）合并为墙区。"""
    if not bins:
        return []
    bins_sorted = sorted(bins, key=lambda b: b.price)
    zones: list[_RawZone] = []
    cur = _RawZone(
        side=side,
        price_low=bins_sorted[0].price,
        price_high=bins_sorted[0].price,
        peak_price=bins_sorted[0].price,
        peak_usd=bins_sorted[0].usd_value,
        total_usd=bins_sorted[0].usd_value,
        total_qty=bins_sorted[0].quantity,
        bin_count=1,
    )
    for b in bins_sorted[1:]:
        gap = (b.price - cur.price_high) / max(cur.price_high, 1e-9)
        if gap <= merge_pct:
            cur.price_high = b.price
            cur.total_usd += b.usd_value
            cur.total_qty += b.quantity
            cur.bin_count += 1
            if b.usd_value > cur.peak_usd:
                cur.peak_usd = b.usd_value
                cur.peak_price = b.price
        else:
            zones.append(cur)
            cur = _RawZone(
                side=side,
                price_low=b.price, price_high=b.price,
                peak_price=b.price, peak_usd=b.usd_value,
                total_usd=b.usd_value, total_qty=b.quantity,
                bin_count=1,
            )
    zones.append(cur)
    return zones


def _frame_zone_range_usd(
    frame: OrderbookDepthSnapshot,
    raw: _RawZone,
) -> float:
    """统计某帧中落在 zone 价区 [price_low, price_high] 内的**全部** bin USD 之和。

    既然 zone 的价区**已经由种子 bin 合并决定**（很窄，通常几十 USD 跨度），
    "价区内全部 bin"几乎都是 zone 的有效流动性，不会出现"全 bin 灌水"问题。
    用全部 bin 而非仅种子，理由：
      - current/max/avg 同基准（避免 trend 失真）
      - 反映该价位完整流动性厚度（用户视角更直观）
    """
    bins = frame.bids if raw.side == "bid" else frame.asks
    if not bins:
        return 0.0
    total = 0.0
    for b in bins:
        if raw.price_low <= b.price <= raw.price_high:
            total += b.usd_value
    return total


def _frame_has_wall_seed(
    frame: OrderbookDepthSnapshot,
    raw: _RawZone,
    seed_min_usd: float,
) -> bool:
    """该帧 zone 价区内是否存在 ≥ seed_min_usd 的"种子 bin"——
    即"墙是否真的在该帧出现"。

    用于 seen_count / first_seen_ts / last_seen_ts / visible_minutes 计算。
    比"价区内总 USD ≥ wall_min_usd"更严格：避免"几个普通小 bin 之和达标"
    导致每帧都被判 seen 而出现"所有 zone 都 55min 持续"的失真。
    """
    bins = frame.bids if raw.side == "bid" else frame.asks
    if not bins:
        return False
    for b in bins:
        if raw.price_low <= b.price <= raw.price_high and b.usd_value >= seed_min_usd:
            return True
    return False


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# W1-T4：稳定 wall_zone_id + spot/coinbase bin 区间 overlap 加权
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _build_wall_zone_id(
    coin: str, side: str, peak_price: float, atr: Optional[float],
) -> str:
    """生成稳定 wall_zone_id（同一物理墙跨帧不变）。

    bucket_size 选择（关键：避免 peak×0.0015 与 atr×0.5 同时取 max 时的"平台效应"）：
      - ATR 已知：bucket = max(atr × 0.5, 0.5)
        高波动币 bucket 大（BTC ATR 200 → bucket 100）；低波动小币 bucket 小。
      - ATR 缺失：bucket = max(peak × 0.0015, 0.5)
        按价格 0.15% 比例兜底。仅在 ATR 缺失时启用，不参与 max（否则会被
        peak 等比例放大，让相距数百 USD 的 peak 仍同桶）。
      - 0.5 USD 绝对兜底：极低价币（< 1 USD）不致桶为 0。

    bucket_idx = floor(peak_price / bucket_size)
    id = sha1(f"{coin}|{side}|{bucket_idx}").hexdigest()[:12]

    设计要点：
      - 不放 source / dual_source：source 切换（spot_only ↔ spot+depth）不应改 ID
      - 不放 ts_sec：必须跨帧稳定
      - 不直接放 peak_price：浮点抖动会破坏稳定性，用桶号离散化
      - 12 hex chars = 48 bit ≈ 2.8e14 空间，单币单侧不会冲突

    边界说明：peak 刚好在桶边界附近（如 76400 / 76500，bucket=100）时小漂移
    可能跨桶 → 不同 ID。这是离散化的必然代价；生产实际数据中 peak 跨帧漂移
    通常 < 1/4 bucket（~25 USD），同桶概率极高。
    """
    side_norm = "ask" if side == "ask" else "bid"
    coin_norm = (coin or "").upper()
    peak = float(peak_price or 0)
    if peak <= 0:
        return ""
    atr_val = float(atr) if atr else 0
    if atr_val > 0:
        bucket_size = max(atr_val * 0.5, 0.5)
    else:
        bucket_size = max(peak * 0.0015, 0.5)
    bucket_idx = int(peak // bucket_size)
    raw = f"{coin_norm}|{side_norm}|{bucket_idx}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def _estimate_bin_step(bins: Sequence[Any]) -> float:
    """从 bins 推导间距（中位数，鲁棒）。

    spot heatmap 间距通常固定（BTC ≈ 100 USD / ETH ≈ 5 USD），但偶有缺位。
    取所有相邻 price 差的中位数 → 抗稀疏 bin 干扰。
    返回 0 表示无法估算（bins 不足 2 个或都是同价）。
    """
    if len(bins) < 2:
        return 0.0
    diffs: list[float] = []
    sorted_bins = sorted(bins, key=lambda b: b.price)
    for i in range(len(sorted_bins) - 1):
        d = abs(sorted_bins[i + 1].price - sorted_bins[i].price)
        if d > 0:
            diffs.append(d)
    if not diffs:
        return 0.0
    diffs.sort()
    return diffs[len(diffs) // 2]


def _bin_overlap_ratio(
    bin_price: float, bin_half_width: float,
    zone_lo: float, zone_hi: float,
) -> float:
    """spot/coinbase bin（视为 [bin_price-h, bin_price+h] 区间）与 zone [lo, hi]
    的重叠比例 ∈ [0, 1]。

    背景：spot heatmap bin 间距 100 USD（BTC），但 zone 跨度 30-50 USD。
      - 旧实现 `if z.lo <= b.price <= z.hi` 点匹配会让 zone 漏算 bin 厚度
        （bin price=76050 实际代表 [76000,76100] 的累积，zone [76020,76080]
        应分到 60/100=0.6 比例的 USD，旧实现漏算或全算都失真）。
      - 区间 overlap 加权按物理意义分配：zone 占多少 bin 区间，就拿多少 USD。
      - 当 bin 同时跨越两个相邻 zone 时，每个 zone 只拿自己那部分（不会
        让一个 spot bin 同时给多个 zone 全额加 dual_source）。

    bin_half_width 为 0 时退化为点匹配（保留旧行为，向后兼容）。
    """
    if bin_half_width <= 0:
        return 1.0 if zone_lo <= bin_price <= zone_hi else 0.0
    bin_lo = bin_price - bin_half_width
    bin_hi = bin_price + bin_half_width
    overlap = min(bin_hi, zone_hi) - max(bin_lo, zone_lo)
    if overlap <= 0:
        return 0.0
    bin_width = max(bin_hi - bin_lo, 1e-9)
    return min(1.0, overlap / bin_width)


def _compute_zone_history_stats(
    raw: _RawZone,
    history: Sequence[OrderbookDepthSnapshot],
    last_price: float,
    cfg: dict,
) -> dict:
    """对单个 raw zone 用 history 回填 max_usd_1h / avg_usd_1h / persistence / trend。

    采用**两层语义分离**：
      - max/avg/current（流动性视图）：价区内**全部** bin USD 总和 → trend 不失真
      - seen_count / visible_minutes（墙真实存在性）：价区内是否有 ≥ seed_min_usd
        的种子 bin → 严格判定"墙真的在那一帧出现"

    避免初版 bug：seen 用"全部 bin USD ≥ wall_min（500K）"门槛过松，价区内
    几个普通小 bin 之和很容易达标，导致**所有 zone 都显示"持续 55min"**。
    """
    wall_min = cfg.get("wall_min_usd", ENGINE_DEFAULTS["wall_min_usd"])
    seed_min = cfg.get("seed_min_usd", ENGINE_DEFAULTS["seed_min_usd"])
    target_min = cfg.get("persistence_target_minutes",
                         ENGINE_DEFAULTS["persistence_target_minutes"])

    frame_totals: list[tuple[int, float]] = []   # (ts_sec, range_usd_in_zone)
    seen_ts: list[int] = []                      # 该帧"墙真的在"
    for frame in history:
        ft = _frame_zone_range_usd(frame, raw)
        frame_totals.append((frame.ts_sec, ft))
        if _frame_has_wall_seed(frame, raw, seed_min):
            seen_ts.append(frame.ts_sec)

    seen_count = len(seen_ts)
    if seen_ts:
        first_seen_ts = seen_ts[0]
        last_seen_ts = seen_ts[-1]
        visible_minutes = max(0.0, (last_seen_ts - first_seen_ts) / 60.0)
    else:
        first_seen_ts = last_seen_ts = 0
        visible_minutes = 0.0

    valid_values = [v for _, v in frame_totals if v >= wall_min]
    if valid_values:
        max_usd = max(valid_values)
        avg_usd = sum(valid_values) / len(valid_values)
    else:
        max_usd = raw.total_usd
        avg_usd = raw.total_usd

    persistence_score = max(0.0, min(1.0, visible_minutes / max(target_min, 1.0)))

    # current = 当前帧（history[-1]）该价区内全部 bin USD 总和
    # 比 raw.total_usd（仅种子）更全；避免 current vs max 基准不一致
    current_usd = frame_totals[-1][1] if frame_totals else raw.total_usd

    trend = _classify_zone_trend(
        seen_count=seen_count, current_usd=current_usd,
        max_usd=max_usd, avg_usd=avg_usd, history_len=len(history), cfg=cfg,
    )

    return {
        "current_usd": current_usd,
        "max_usd_1h": max_usd,
        "avg_usd_1h": avg_usd,
        "seen_count": seen_count,
        "visible_minutes": visible_minutes,
        "persistence_score": persistence_score,
        "first_seen_ts": first_seen_ts,
        "last_seen_ts": last_seen_ts,
        "trend": trend,
    }


def _classify_zone_trend(
    seen_count: int, current_usd: float, max_usd: float, avg_usd: float,
    history_len: int, cfg: dict,
) -> WallZoneTrend:
    """趋势分类：基于 current 相对 avg 的偏离 + 是否新出现。

    new          ：seen_count <= 2（新出现，刚被识别 1-2 帧）
    strengthening：current ≥ avg × (1 + strengthening_pct)
    weakening    ：current ≤ avg × (1 + weakening_pct)
    stable       ：其余
    """
    if history_len <= 1:
        return "new"
    if seen_count <= 2:
        return "new"
    s_pct = cfg.get("trend_strengthening_pct",
                    ENGINE_DEFAULTS["trend_strengthening_pct"])
    w_pct = cfg.get("trend_weakening_pct",
                    ENGINE_DEFAULTS["trend_weakening_pct"])
    if avg_usd <= 0:
        return "stable"
    diff_pct = (current_usd - avg_usd) / avg_usd
    if diff_pct >= s_pct:
        return "strengthening"
    if diff_pct <= w_pct:
        return "weakening"
    return "stable"


def _build_zones_for_side(
    history: Sequence[OrderbookDepthSnapshot],
    last_price: float,
    side: WallSide,
    atr: Optional[float],
    cfg: dict,
) -> list[WallZone]:
    """完整 M1 流水：filter → 选种子 → merge → history stats → WallZone。

    关键设计（修复初版"全 bin 合并"灌水问题）：
      1. 在 ±max_distance_pct 内过滤 bin（按侧）
      2. **只用"种子 bin"参与合并**：USD ≥ seed_min_usd 且在 top_seed_count 内
      3. 相邻种子（gap ≤ merge_pct）合并成 zone
      4. zone.current_usd = 该 zone 价区内**仅种子 bin** 的 USD 之和（真实墙厚度，
         不被中间密集小 bin 灌水）
      5. 历史回填用同一定义（_frame_zone_seed_usd），保证 current/max/avg 同基准
    """
    if not history:
        return []
    latest = history[-1]
    bins = latest.bids if side == "bid" else latest.asks
    if not bins:
        return []

    max_dist = cfg.get("max_distance_pct_for_zone",
                       ENGINE_DEFAULTS["max_distance_pct_for_zone"])
    bins_in_range = _filter_bins_by_distance(bins, last_price, side, max_dist)
    if not bins_in_range:
        return []

    # ── 选种子：USD 排序 → top-N 且 ≥ seed_min_usd ──
    seed_min = cfg.get("seed_min_usd", ENGINE_DEFAULTS["seed_min_usd"])
    top_n = cfg.get("top_seed_count", ENGINE_DEFAULTS["top_seed_count"])
    sorted_by_usd = sorted(bins_in_range, key=lambda b: b.usd_value, reverse=True)
    seeds = [b for b in sorted_by_usd[:top_n] if b.usd_value >= seed_min]
    if not seeds:
        return []

    # ── 仅用种子合并相邻成 zone ──
    merge_pct = _resolve_merge_pct(atr, last_price, cfg)
    raw_zones = _merge_adjacent_bins_to_zones(seeds, merge_pct, side)

    # ── 单 zone 总厚度（仅种子）≥ wall_min_usd 才保留 ──
    wall_min = cfg.get("wall_min_usd", ENGINE_DEFAULTS["wall_min_usd"])
    raw_zones = [z for z in raw_zones if z.total_usd >= wall_min]
    if not raw_zones:
        return []

    zones: list[WallZone] = []
    for raw in raw_zones:
        stats = _compute_zone_history_stats(raw, history, last_price, cfg)
        price_mid = (raw.price_low + raw.price_high) / 2.0
        distance_pct = (price_mid - last_price) / max(last_price, 1e-9) * 100.0
        zone = WallZone(
            side=side,
            price_low=raw.price_low,
            price_high=raw.price_high,
            price_mid=price_mid,
            peak_price=raw.peak_price,
            distance_pct=distance_pct,
            current_usd=stats["current_usd"],
            max_usd_1h=stats["max_usd_1h"],
            avg_usd_1h=stats["avg_usd_1h"],
            bin_count=raw.bin_count,
            seen_count=stats["seen_count"],
            visible_minutes=stats["visible_minutes"],
            persistence_score=stats["persistence_score"],
            first_seen_ts=stats["first_seen_ts"],
            last_seen_ts=stats["last_seen_ts"],
            trend=stats["trend"],
            source="depth_only",                # 默认；后续 _augment_with_large_orders 可升级
        )
        zones.append(zone)

    # 按 strength（current_usd × persistence + 距离衰减）降序
    def _zone_strength(z: WallZone) -> float:
        # 距离衰减：4% scale
        dist_factor = 1.0
        if abs(z.distance_pct) > 0:
            import math
            dist_factor = math.exp(-abs(z.distance_pct) / 4.0)
        return z.current_usd * (0.5 + 0.5 * z.persistence_score) * dist_factor

    zones.sort(key=lambda z: -_zone_strength(z))
    max_zones = cfg.get("max_zones_per_side", ENGINE_DEFAULTS["max_zones_per_side"])
    return zones[:max_zones]


def _attach_strength_tier(zones: list[WallZone], base_cfg: dict) -> None:
    """对 WallZone 用现有 OP tier 阈值（与 PressureWall 对齐）。"""
    s_min = base_cfg.get("tier_s_min_usd", 30_000_000.0)
    a_min = base_cfg.get("tier_a_min_usd", 10_000_000.0)
    b_min = base_cfg.get("tier_b_min_usd", 3_000_000.0)

    for z in zones:
        # strength_score 设计：max 厚度 + persistence 加权
        score = z.max_usd_1h * (0.7 + 0.3 * z.persistence_score)
        z.strength_score = round(score, 2)
        if score >= s_min:
            z.strength_tier = "S"
        elif score >= a_min:
            z.strength_tier = "A"
        elif score >= b_min:
            z.strength_tier = "B"
        else:
            z.strength_tier = "C"


def _augment_zones_with_large_orders(
    zones: list[WallZone],
    large_orders: Sequence[LargeOrderLifecycle],
    last_price: float,
    cfg: dict,
) -> None:
    """把 large_orders（仍 holding 的）匹配到 zone：

    - 价格落入 [price_low - tol, price_high + tol] → 加入 large_order_ids
      容差 tol = max(price * augment_match_tol_pct, augment_match_tol_usd_min)
      原因：大单 limit_price 是精确价（如 $77,455），但 zone 边界由热力图离散
      bin 决定（如 $77,460–510，步进 5/10 USD）。两者本就同源（都是订单簿挂单），
      不容差匹配会因几 USD 错位而错过几乎所有大单。
    - 同时记录覆盖的 exchange_name 集合 → exchange_count
    - source 升级：depth_only → depth+large_order
    """
    if not zones or not large_orders:
        return
    holding = [lo for lo in large_orders if lo.state == "holding" and lo.limit_price > 0]
    if not holding:
        return

    tol_pct = cfg.get("augment_match_tol_pct",
                      ENGINE_DEFAULTS.get("augment_match_tol_pct", 0.001))   # 0.1%
    tol_min = cfg.get("augment_match_tol_usd_min",
                      ENGINE_DEFAULTS.get("augment_match_tol_usd_min", 5.0))  # ≥ 5 USD

    for z in zones:
        tol = max(z.peak_price * tol_pct, tol_min)
        lo_bound = z.price_low - tol
        hi_bound = z.price_high + tol
        ids: list[int] = []
        exchanges: set[str] = set()
        for lo in holding:
            if lo.side != z.side:
                continue
            if lo_bound <= lo.limit_price <= hi_bound:
                ids.append(lo.id)
                if lo.exchange_name:
                    exchanges.add(lo.exchange_name)
        if ids:
            z.large_order_ids = ids
            z.source = "depth+large_order"
        # exchange_count：至少 1（当前所），若 large_orders 来自多所则取 max(1, len(set))
        z.exchange_count = max(1, len(exchanges)) if exchanges else 1


def _augment_zones_with_spot_depth(
    zones: list[WallZone],
    spot_history: Sequence[OrderbookDepthSnapshot],
    cfg: dict,
) -> None:
    """Phase A 核心：现货 5m 热力图 → 在已有 zone 价区上叠加现货厚度。

    设计原则（dev-constraints #3 复用决策）：
      不重新对现货独立跑 _build_zones_for_side，因为：
        1. 合约 zone 已用合约 ATR-aware merge_pct 决定了"墙"边界（更高粒度，bin 5-10 USD）
        2. 现货 bin 间距 100 USD，价区分辨率粗，不适合主导 zone 边界
        3. 主路径"合约锚点 + 现货验证"语义更清晰

    W1-T4 升级：用区间 overlap 加权代替点匹配
      旧实现：`if z.lo <= b.price <= z.hi` → 粗 bin 误差大（bin 间距 100 USD，
        zone 跨度 30-50 USD 时漏算/误扩散）。
      新实现：把 spot bin 视为 [b.price ± bin_step/2] 的区间，按与 zone 的
        overlap 比例加权 USD。bin 跨越两个相邻 zone 时，每个 zone 只拿自己
        那部分，不会同时给多个 zone 全额加 dual_source。
      bin_step 不可估算时（bins < 2 或同价），退化为点匹配（向后兼容）。

      当 spot_current_usd ≥ wall_min_usd 且 spot_max_usd_1h ≥ wall_min_usd
        → 标 dual_source=True + source="spot+depth"

    "仅现货独立 zone"（不在任何合约 zone 价区里）由 _build_spot_only_zones 单独承担。
    """
    if not zones or not spot_history:
        return
    wall_min = cfg.get("wall_min_usd", ENGINE_DEFAULTS["wall_min_usd"])
    latest_spot = spot_history[-1]

    def _frame_zone_usd(bins: list, zone_lo: float, zone_hi: float) -> float:
        if not bins:
            return 0.0
        bin_step = _estimate_bin_step(bins)
        half = bin_step / 2.0 if bin_step > 0 else 0.0
        total = 0.0
        for b in bins:
            ratio = _bin_overlap_ratio(b.price, half, zone_lo, zone_hi)
            if ratio > 0:
                total += b.usd_value * ratio
        return total

    for z in zones:
        bins_latest = latest_spot.bids if z.side == "bid" else latest_spot.asks
        cur_usd = _frame_zone_usd(bins_latest, z.price_low, z.price_high)
        frame_totals: list[float] = []
        for frame in spot_history:
            bins = frame.bids if z.side == "bid" else frame.asks
            if not bins:
                continue
            frame_totals.append(_frame_zone_usd(bins, z.price_low, z.price_high))
        max_usd = max(frame_totals) if frame_totals else cur_usd

        z.spot_current_usd = round(cur_usd, 2)
        z.spot_max_usd_1h = round(max_usd, 2)

        if cur_usd >= wall_min and max_usd >= wall_min:
            z.dual_source = True
            z.source = "spot+depth"


def _build_spot_only_zones(
    spot_history: Sequence[OrderbookDepthSnapshot],
    last_price: float,
    side: WallSide,
    atr: Optional[float],
    cfg: dict,
    excluded_price_ranges: list[tuple[float, float]],
) -> list[WallZone]:
    """Phase A：在现货 history 上独立跑 zone 检测，仅保留**未被合约 zone 覆盖**的价区。

    excluded_price_ranges：合约 zone 的 [price_low, price_high] 列表。
    现货独立 zone 的 peak_price 落入任一区间 → 视为 dual_source 已处理，不重复输出。

    输出 source="spot_only"，trust_score 计算时会因缺少合约源而拿不到 dual_source 加分，
    但仍保留 spot_confluence/multi_exchange/persistence 加分，正常进入排序。
    """
    if not spot_history:
        return []
    zones = _build_zones_for_side(spot_history, last_price, side, atr, cfg)
    if not zones:
        return []

    out: list[WallZone] = []
    for z in zones:
        in_excluded = any(
            lo <= z.peak_price <= hi for (lo, hi) in excluded_price_ranges
        )
        if in_excluded:
            continue
        z.source = "spot_only"
        z.spot_current_usd = z.current_usd
        z.spot_max_usd_1h = z.max_usd_1h
        out.append(z)
    return out


def _augment_zones_with_spot_large_orders(
    zones: list[WallZone],
    spot_large_orders: Sequence[LargeOrderLifecycle],
    cfg: dict,
) -> None:
    """M2.5：现货大单匹配（区分真支撑 vs 合约清算磁铁）。

    现货大单 = 真买家/卖家（真金白银），与合约大单（杠杆挂单/清算磁铁）互补：
      - 仅合约共振 → 普通合约墙，可能是清算磁铁
      - 仅现货共振 → 真支撑/真阻力（真金白银资金布局）
      - **双源共振 → 最强真支撑**（真买卖家 + 合约流动性同位）

    与合约 augment 用同一容差匹配机制：5 USD 或 0.1% 之间取大。
    """
    if not zones or not spot_large_orders:
        return
    holding = [lo for lo in spot_large_orders if lo.state == "holding" and lo.limit_price > 0]
    if not holding:
        return

    tol_pct = cfg.get("augment_match_tol_pct",
                      ENGINE_DEFAULTS.get("augment_match_tol_pct", 0.001))
    tol_min = cfg.get("augment_match_tol_usd_min",
                      ENGINE_DEFAULTS.get("augment_match_tol_usd_min", 5.0))

    for z in zones:
        tol = max(z.peak_price * tol_pct, tol_min)
        lo_bound = z.price_low - tol
        hi_bound = z.price_high + tol
        ids: list[int] = []
        for lo in holding:
            if lo.side != z.side:
                continue
            if lo_bound <= lo.limit_price <= hi_bound:
                ids.append(lo.id)
        if ids:
            z.spot_large_order_ids = ids
            z.has_spot_confluence = True


def _compute_usd_usdt_basis_pct(
    coinbase_frame: Any, ticker_last: Optional[float],
) -> Optional[float]:
    """W2-T4：USD/USDT 基差（pct） = (coinbase_mid - ticker_last) / ticker_last × 100。

    设计：
      - coinbase_mid = (best_bid + best_ask) / 2，best 是 levels 列表中最接近现价的档
        bids 升序时 best_bid = bids[-1]；asks 升序时 best_ask = asks[0]
      - ticker_last 缺失或 ≤ 0 → None
      - coinbase_frame 无 bids/asks → None
      - 正常 BTC < 5bp（0.05%），> 30bp 表示明显基差异常（前端可高亮）
    """
    if coinbase_frame is None or ticker_last is None or ticker_last <= 0:
        return None
    bids = list(getattr(coinbase_frame, "bids", []) or [])
    asks = list(getattr(coinbase_frame, "asks", []) or [])
    if not bids or not asks:
        return None
    try:
        best_bid = bids[-1].price
        best_ask = asks[0].price
        if best_bid <= 0 or best_ask <= 0 or best_ask < best_bid:
            return None
        coinbase_mid = (best_bid + best_ask) / 2.0
        return round((coinbase_mid - ticker_last) / ticker_last * 100.0, 4)
    except (AttributeError, TypeError, ValueError, IndexError):
        return None


def _augment_zones_with_coinbase(
    zones: list[WallZone],
    coinbase_frame: Any,
    cfg: dict,
) -> None:
    """Phase C：Coinbase 现货原生订单簿 → 在已有 zone 价区上叠加 Coinbase USD。

    设计原则（dev-constraints #3 复用决策 + #2 全局视角）：
      不在 Coinbase 数据上独立检测 zone，而是只在合约 / 现货已检出的 zone 价区
      [price_low, price_high] 上累加 Coinbase USD。原因：
        1. Coinbase 用 BTC-USD（法币），与项目主链 BTC-USDT 价位有 ~5bp spread；
           独立跑 zone 边界会和合约 zone 错位
        2. Coinbase 数据是"机构资金验证"维度，定位是补充而非替代主源
        3. 不消耗任何额外计算（仅一遍 O(zones × bins) 扫描）

    判定逻辑（决策点：用户选择 default_a_a_c）：
      - 价位匹配容差：max(peak_price × coinbase_match_tol_pct, augment_match_tol_usd_min)
        = 默认 10bp 或 5 USD（吸收 USD/USDT spread，详见 audit Phase C）
      - 共振门槛：
          coinbase_spot_usd ≥ wall_min_usd × coinbase_min_usd_ratio (默认 30%)
        AND coinbase_num_orders ≥ coinbase_min_num_orders (默认 3 笔)
      - num_orders < 3 视为单笔大单 spoof 嫌疑，不算 confluence

    侧匹配：bid zone 累加 Coinbase bids，ask zone 累加 Coinbase asks。
    """
    if not zones or coinbase_frame is None:
        return

    bids = list(getattr(coinbase_frame, "bids", []) or [])
    asks = list(getattr(coinbase_frame, "asks", []) or [])
    if not bids and not asks:
        return

    wall_min = cfg.get("wall_min_usd", ENGINE_DEFAULTS["wall_min_usd"])
    usd_ratio = cfg.get("coinbase_min_usd_ratio",
                        ENGINE_DEFAULTS["coinbase_min_usd_ratio"])
    min_orders = cfg.get("coinbase_min_num_orders",
                         ENGINE_DEFAULTS["coinbase_min_num_orders"])
    tol_pct = cfg.get("coinbase_match_tol_pct",
                      ENGINE_DEFAULTS["coinbase_match_tol_pct"])
    tol_usd_min = cfg.get("augment_match_tol_usd_min",
                          ENGINE_DEFAULTS.get("augment_match_tol_usd_min", 5.0))
    threshold_usd = wall_min * usd_ratio

    # W1-T4：Coinbase aggregated frame 的 levels 已被聚合为 0.5%/level（看
    # coinbase_native.py），bin_step 通常较细（< 5 USD），但在远价位也可能稀疏。
    # 用 overlap 加权同样适用：
    #   - levels 间距小时退化为近似点匹配（半宽 < zone 跨度，overlap=1.0）
    #   - levels 间距大时按比例分配（防止单个聚合 level 同时给多个 zone 加共振）
    bid_step = _estimate_bin_step(bids)
    ask_step = _estimate_bin_step(asks)

    for z in zones:
        levels = bids if z.side == "bid" else asks
        if not levels:
            continue
        tol = max(z.peak_price * tol_pct, tol_usd_min)
        lo_bound = z.price_low - tol
        hi_bound = z.price_high + tol
        # 仍然保留 tol 容差用于"扩展 zone 边界吸收 USD/USDT spread"；
        # overlap 在扩展后的边界 [lo_bound, hi_bound] 上计算
        bin_step = bid_step if z.side == "bid" else ask_step
        half = bin_step / 2.0 if bin_step > 0 else 0.0

        cb_usd = 0.0
        cb_orders = 0
        # W2-T4：追踪 zone 内最大的"单笔订单 USD"
        # 定义：max(level.usd_value / level.num_orders)，仅在 overlap >= 0.5
        # （主重叠）的 level 上统计；num_orders=0 视为 1 笔避免 div-by-zero
        max_single_usd = 0.0
        for lv in levels:
            ratio = _bin_overlap_ratio(lv.price, half, lo_bound, hi_bound)
            if ratio > 0:
                cb_usd += lv.usd_value * ratio
                if ratio >= 0.5:
                    cb_orders += lv.num_orders
                    n = lv.num_orders if lv.num_orders > 0 else 1
                    single = lv.usd_value / n
                    if single > max_single_usd:
                        max_single_usd = single

        z.coinbase_spot_usd = round(cb_usd, 2)
        z.coinbase_num_orders = cb_orders
        z.coinbase_max_single_order_usd = round(max_single_usd, 2)
        if cb_usd >= threshold_usd and cb_orders >= min_orders:
            z.coinbase_spot_confluence = True


def _compute_trust_breakdown(zone: WallZone, cfg: dict) -> tuple[float, dict[str, float]]:
    """综合 trust_score（0-1）—— 区分高可信墙 vs 合约清算磁铁。

    W2-T1 新增主函数：返回 (final_score, components)，components 含各因子贡献明细，
    供：
      - archiver 落盘（后验脚本分析"哪些因子最能预测墙被反弹/打穿"）
      - AI snapshot 透明化（可选展示）
      - 前端 hover 显示构成

    既有调用方继续用 _compute_trust_score（向后兼容包装，仅返回 final）。


    阶梯加分（多维度独立累加，最终 clamp 到 1.0）：
      - base 0.50（合约 5m 热力图源，已是真实订单簿但有 spoof/钓鱼可能）
      - +0.30 双源共振 dual_source=True（现货 + 合约 5m 同价区都有 ≥ wall_min 厚度，
              单一硬证据中最强：真买卖家与杠杆资金共同布局）
      - +0.15 现货大单共振（额外 spot 大单 lifecycle 证据，与 dual_source 可叠加）
      - +0.10 多家交易所共振（exchange_count ≥ 2，当前 dead-code，预留未来原生 API 接入）
      - +0.10 Coinbase 现货共振（Phase C：机构资金独立验证维度，正交于 Binance 系）
      - +0.10 持续 ≥ 0.7（visible ≥ 75% 历史窗口）
      - 单源现货 zone：spot_only 与合约 spot+depth 共用同一加分体系，但缺少 base 之外
        的合约源验证，需要更长持久或更多大单证据才能升到高分位。

    阈值含义：
      ≥ 0.85：💎 双源 + 多重硬证据（最强）
      ≥ 0.65：较可信
      ≥ 0.50：普通（仅单源，需结合磁铁/被扫风险解读）
      < 0.50：短期墙
    """
    components: dict[str, float] = {}
    base = cfg.get("trust_base", ENGINE_DEFAULTS["trust_base"])
    components["base"] = round(base, 3)
    score = base

    if zone.dual_source:
        bonus = cfg.get("trust_bonus_dual_source",
                        ENGINE_DEFAULTS.get("trust_bonus_dual_source", 0.30))
        score += bonus
        components["dual_source"] = round(bonus, 3)
    if zone.has_spot_confluence:
        bonus = cfg.get("trust_bonus_spot_confluence",
                        ENGINE_DEFAULTS["trust_bonus_spot_confluence"])
        score += bonus
        components["spot_confluence"] = round(bonus, 3)
    if zone.exchange_count >= 2:
        bonus = cfg.get("trust_bonus_multi_exchange",
                        ENGINE_DEFAULTS["trust_bonus_multi_exchange"])
        score += bonus
        components["multi_exchange"] = round(bonus, 3)
    if zone.coinbase_spot_confluence:
        bonus = cfg.get("trust_bonus_coinbase_confluence",
                        ENGINE_DEFAULTS["trust_bonus_coinbase_confluence"])
        score += bonus
        components["coinbase_confluence"] = round(bonus, 3)
    persistent_thr = cfg.get("trust_persistent_threshold",
                             ENGINE_DEFAULTS["trust_persistent_threshold"])
    if zone.persistence_score >= persistent_thr:
        bonus = cfg.get("trust_bonus_persistent",
                        ENGINE_DEFAULTS["trust_bonus_persistent"])
        score += bonus
        components["persistent"] = round(bonus, 3)
    final = round(max(0.0, min(1.0, score)), 3)
    components["total"] = final
    return final, components


def _compute_trust_score(zone: WallZone, cfg: dict) -> float:
    """向后兼容包装（既有测试 / KL bridge 仍按 float 调用）。

    新代码请直接用 _compute_trust_breakdown 拿 components。
    """
    final, _ = _compute_trust_breakdown(zone, cfg)
    return final


def _compute_support_resistance_trust_score(zone: WallZone, cfg: dict) -> float:
    """SR：作为支撑/阻力被反弹的可信度（W2-T1 独立维度）。

    与 trust_score 不同的核心点：
      - trust_score：墙的"真实性"（是不是 spoof / 是不是真买卖家挂的）
      - SR：墙的"作用力"（真的被打到时会不会反弹？）
      实际差异：dual_source + 多重硬证据的高 trust 墙，若也容易撤单（wall_removal_risk
      高），SR 应显著低于 trust（因为反弹时墙可能消失，价格穿过）。

    公式（与 trust 共享因子但调整权重 + 加 wall_consumed_confidence 正贡献 -
    wall_removal_risk 负贡献）：
      base 0.30（合约单源给低基准；spot 多源才能上 0.85+）
      +0.30 dual_source（同 trust 权重）
      +0.15 has_spot_confluence
      +0.10 coinbase_spot_confluence
      +0.10 persistence ≥ 0.7
      +0.10 wall_consumed_confidence ≥ 0.6（已被验证承接过卖盘 → 强支撑/阻力硬证据）
      +0.05 W2-T4 机构 footprint：coinbase_max_single_order_usd ≥ 100k USD/笔
            （区分"散户 N 单聚集"vs"机构孤立巨单"，两者都是真买卖家但意图不同）
      −0.30 × wall_removal_risk（容易消失的墙不可信）
    """
    sr = 0.30
    if zone.dual_source:
        sr += 0.30
    if zone.has_spot_confluence:
        sr += 0.15
    if zone.coinbase_spot_confluence:
        sr += 0.10
    if zone.persistence_score >= 0.7:
        sr += 0.10
    if zone.wall_consumed_confidence >= 0.6:
        sr += 0.10
    if zone.coinbase_max_single_order_usd >= 100_000:
        sr += 0.05
    sr -= 0.30 * max(0.0, min(1.0, zone.wall_removal_risk))
    return round(max(0.0, min(1.0, sr)), 3)


def _compute_sweep_attractiveness_score(
    zone: WallZone,
    crowding: Optional[PositionCrowdingSnapshot],
    last_price: float,
    cfg: dict,
) -> float:
    """SA：作为扫单磁铁被打穿的可吸引度（W2-T1 独立维度）。

    与 SR 完全不同的因子组合 —— 关键洞察：高 trust 大墙也可能因为是机构清算磁铁
    而被打穿（高 SA），SR 与 SA 可以同时高（"双向博弈热点"，需结合 §1 CVD / §9g
    absorption 综合判断方向）。

    构成（条件性加分，无 base，clamp 1.0）：
      0.25 × 同向 crowding_risk（bid wall 看 long_crowding，ask wall 看 short_crowding；
            墙下方拥挤多头止损 = 扫单动机）
      磁铁邻近 + 真空（最高 0.20）：
        - magnet 距 < 0.5%：+0.15
        - magnet 距 < 2%：  +0.10
        - magnet 距 < 5%：  +0.05
        - vacuum_gap_pct ≥ 1%：+0.05；≥ 0.5%：+0.025
      0.20 × active_attack_score（同向 taker + cvd 同向 + 流动性衰竭）
      0.15 × wall_removal_risk（spoof 容易撤）
      厚度衰减（最高 0.10）：
        - current/max < 0.3：+0.10
        - current/max < 0.5：+0.05
    """
    sa = 0.0
    if crowding is not None:
        if zone.side == "bid":
            crowd_risk = float(getattr(crowding, "long_crowding_risk", 0) or 0)
        else:
            crowd_risk = float(getattr(crowding, "short_crowding_risk", 0) or 0)
        sa += 0.25 * max(0.0, min(1.0, crowd_risk))

    sweep = zone.sweep_target
    if sweep is not None and last_price > 0 and sweep.magnet_price > 0:
        dist_pct = abs(sweep.magnet_price - last_price) / last_price
        if dist_pct < 0.005:
            sa += 0.15
        elif dist_pct < 0.02:
            sa += 0.10
        elif dist_pct < 0.05:
            sa += 0.05
        if sweep.vacuum_gap_pct >= 1.0:
            sa += 0.05
        elif sweep.vacuum_gap_pct >= 0.5:
            sa += 0.025

    sa += 0.20 * max(0.0, min(1.0, zone.active_attack_score))
    sa += 0.15 * max(0.0, min(1.0, zone.wall_removal_risk))

    if zone.max_usd_1h > 0:
        ratio = zone.current_usd / zone.max_usd_1h
        if ratio < 0.3:
            sa += 0.10
        elif ratio < 0.5:
            sa += 0.05

    return round(max(0.0, min(1.0, sa)), 3)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# M2：拥挤度 + OI 分流 + inferred_position_state
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _classify_oi_margin_split(
    coin_usd: Optional[float], stable_usd: Optional[float], cfg: dict,
) -> OIMarginSplit:
    if coin_usd is None or stable_usd is None or (coin_usd + stable_usd) <= 0:
        return "unknown"
    total = coin_usd + stable_usd
    coin_ratio = coin_usd / total
    dom = cfg.get("oi_margin_dominant_ratio",
                  ENGINE_DEFAULTS["oi_margin_dominant_ratio"])
    if coin_ratio >= dom:
        return "coin_dominant"
    if coin_ratio <= (1 - dom):
        return "stable_dominant"
    return "balanced"


def _classify_inferred_position_state(
    crowding: PositionCrowdingSnapshot,
    taker_flow: Any,
    last_price: float,
    prev_price: Optional[float],
    has_recent_long_liq: bool,
    has_recent_short_liq: bool,
    cfg: dict,
) -> InferredPositionState:
    """根据 价格×OI×taker / 清算 矩阵推断主导仓位行为。

    规则（GPT 简化版）：
      价格上涨 + OI 上升 + taker buy 强  → long_opening
      价格下跌 + OI 上升 + taker sell 强 → short_opening
      价格下跌 + OI 下降 + long_liq 增   → long_closing_or_liquidation
      价格上涨 + OI 下降 + short_liq 增  → short_covering_or_liquidation
      OI 大幅下降 + 双侧清算同时出现     → liquidation_flush
      其余                              → mixed / unknown
    """
    oi_5m = crowding.oi_delta_5m_pct or 0
    oi_1h = crowding.oi_delta_1h_pct or 0
    if prev_price is None or prev_price <= 0:
        price_dir = 0.0
    else:
        price_dir = (last_price - prev_price) / prev_price * 100  # %

    strong_pct = cfg.get("oi_delta_strong_pct",
                         ENGINE_DEFAULTS["oi_delta_strong_pct"])

    # taker dominance（None-safe）
    taker_buy = float(getattr(taker_flow, "buy_volume_usd", 0) or 0) if taker_flow else 0
    taker_sell = float(getattr(taker_flow, "sell_volume_usd", 0) or 0) if taker_flow else 0
    taker_buy_dom = taker_buy > taker_sell * 1.2
    taker_sell_dom = taker_sell > taker_buy * 1.2

    if has_recent_long_liq and has_recent_short_liq and oi_1h <= -strong_pct:
        return "liquidation_flush"

    # 价格上涨方向
    if price_dir > 0.05:   # +0.05% 算上涨
        if oi_1h >= strong_pct and taker_buy_dom:
            return "long_opening"
        if oi_1h <= -strong_pct and has_recent_short_liq:
            return "short_covering_or_liquidation"
    elif price_dir < -0.05:
        if oi_1h >= strong_pct and taker_sell_dom:
            return "short_opening"
        if oi_1h <= -strong_pct and has_recent_long_liq:
            return "long_closing_or_liquidation"

    # OI 单独驱动（价格不明显）
    if abs(oi_5m) >= strong_pct or abs(oi_1h) >= strong_pct:
        return "mixed"
    return "unknown"


def _build_position_crowding(
    state: "CoinState", cfg: dict,
) -> Optional[PositionCrowdingSnapshot]:
    """从 state 已有字段读取拥挤度（不发起 poll）。"""
    rank = getattr(state, "oi_exchange_rank", None) or {}
    all_agg: Optional[dict] = rank.get("all_aggregated") if isinstance(rank, dict) else None

    crowding = PositionCrowdingSnapshot()

    # ── OI 6 周期 delta + 保证金分布 ──
    if all_agg:
        crowding.oi_delta_5m_pct = all_agg.get("change_5m_pct")
        crowding.oi_delta_15m_pct = all_agg.get("change_15m_pct")
        crowding.oi_delta_30m_pct = all_agg.get("change_30m_pct")
        crowding.oi_delta_1h_pct = all_agg.get("change_1h_pct")
        crowding.oi_delta_4h_pct = all_agg.get("change_4h_pct")
        crowding.oi_delta_24h_pct = all_agg.get("change_24h_pct")
        crowding.oi_coin_margin_usd = all_agg.get("oi_coin_margin_usd")
        crowding.oi_stable_margin_usd = all_agg.get("oi_stable_margin_usd")
        crowding.oi_margin_split = _classify_oi_margin_split(
            crowding.oi_coin_margin_usd, crowding.oi_stable_margin_usd, cfg,
        )

    # ── Funding ──
    multi_fund = getattr(state, "multi_funding", None)
    if multi_fund is not None:
        try:
            crowding.funding_now_pct = float(getattr(multi_fund, "avg_current", 0)) * 100.0
        except (TypeError, ValueError):
            crowding.funding_now_pct = None

    fund_hist = getattr(state, "funding_history_8h", None)
    if fund_hist and crowding.funding_now_pct is not None:
        try:
            sample = [abs(float(p["rate"])) for p in fund_hist if isinstance(p, dict)]
            if sample:
                cur = abs(crowding.funding_now_pct / 100.0)
                pos = sum(1 for v in sample if v <= cur)
                crowding.funding_percentile_30d = round(pos / len(sample), 3)
        except (TypeError, ValueError, KeyError):
            crowding.funding_percentile_30d = None

    # ── Long/Short ──
    ls = getattr(state, "ls_ratio", None)
    if ls and getattr(ls, "avg_ratio", None) is not None:
        crowding.global_account_ls_ratio = float(ls.avg_ratio)
    top_pos = getattr(state, "top_position_ratio", None)
    if top_pos and getattr(top_pos, "avg_ratio", None) is not None:
        crowding.top_position_ls_ratio = float(top_pos.avg_ratio)

    # ── inferred_position_state ──
    taker_flow = getattr(state, "taker_flow", None)
    ticker = getattr(state, "ticker", None)
    last_price = float(ticker.last) if ticker and ticker.last else 0.0
    candles = getattr(state, "candles_4h", []) or []
    prev_price = None
    if candles and isinstance(candles[-1], dict):
        prev_price = float(candles[-1].get("open") or 0) or None
    elif candles and hasattr(candles[-1], "open"):
        prev_price = float(candles[-1].open or 0) or None

    # 清算简化：如果近 1h 清算 USD 显著 → 视作有清算
    liq_summary = getattr(state, "liq_summary", None)
    has_long_liq = False
    has_short_liq = False
    if liq_summary is not None:
        try:
            long_1h = float(getattr(liq_summary, "long_liquidation_usd_1h", 0) or 0)
            short_1h = float(getattr(liq_summary, "short_liquidation_usd_1h", 0) or 0)
            has_long_liq = long_1h >= 1_000_000
            has_short_liq = short_1h >= 1_000_000
        except (TypeError, ValueError):
            pass

    crowding.inferred_position_state = _classify_inferred_position_state(
        crowding, taker_flow, last_price, prev_price,
        has_long_liq, has_short_liq, cfg,
    )

    # ── crowding_risk 软分（0-1）──
    long_risk = 0.0
    short_risk = 0.0
    # Funding：偏高 → 多头拥挤
    f_high = cfg.get("funding_high_pct", ENGINE_DEFAULTS["funding_high_pct"])
    f_low = cfg.get("funding_low_pct", ENGINE_DEFAULTS["funding_low_pct"])
    if crowding.funding_now_pct is not None:
        if crowding.funding_now_pct >= f_high:
            long_risk += 0.4
        elif crowding.funding_now_pct <= f_low:
            short_risk += 0.4
    # LS 极值
    ls_h = cfg.get("ls_extreme_high", ENGINE_DEFAULTS["ls_extreme_high"])
    ls_l = cfg.get("ls_extreme_low", ENGINE_DEFAULTS["ls_extreme_low"])
    if crowding.top_position_ls_ratio is not None:
        if crowding.top_position_ls_ratio >= ls_h:
            long_risk += 0.3
        elif crowding.top_position_ls_ratio <= ls_l:
            short_risk += 0.3
    # OI 1h 大涨 + 价格趋势 → 拥挤加速
    oi_strong = cfg.get("oi_delta_strong_pct",
                        ENGINE_DEFAULTS["oi_delta_strong_pct"])
    if (crowding.oi_delta_1h_pct or 0) >= oi_strong:
        long_risk += 0.2 if crowding.inferred_position_state == "long_opening" else 0.1
    elif (crowding.oi_delta_1h_pct or 0) <= -oi_strong:
        # 减仓不算拥挤
        pass

    crowding.long_crowding_risk = round(min(1.0, long_risk), 3)
    crowding.short_crowding_risk = round(min(1.0, short_risk), 3)

    # ── chips（中文短文案）──
    chips: list[str] = []
    if crowding.oi_margin_split == "stable_dominant":
        chips.append("U本位主导(新资金加杠杆)")
    elif crowding.oi_margin_split == "coin_dominant":
        chips.append("币本位主导(老用户加杠杆)")
    if crowding.inferred_position_state == "long_opening":
        chips.append("多头主动开仓")
    elif crowding.inferred_position_state == "short_opening":
        chips.append("空头主动开仓")
    elif crowding.inferred_position_state == "long_closing_or_liquidation":
        chips.append("多头平仓/清算")
    elif crowding.inferred_position_state == "short_covering_or_liquidation":
        chips.append("空头回补/清算")
    elif crowding.inferred_position_state == "liquidation_flush":
        chips.append("双向清算潮")
    if crowding.long_crowding_risk >= 0.6:
        chips.append(f"多头拥挤度高({crowding.long_crowding_risk:.0%})")
    if crowding.short_crowding_risk >= 0.6:
        chips.append(f"空头拥挤度高({crowding.short_crowding_risk:.0%})")
    crowding.explain_chips = chips

    return crowding


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# M2：扫单磁铁 + 真空跨度 + 击穿风险
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _pick_max_pain_for_coin(max_pain_data: Any, coin: str) -> Optional[Any]:
    """从 LiqMaxPainData 取本币的 item。"""
    if max_pain_data is None or not getattr(max_pain_data, "items", None):
        return None
    for item in max_pain_data.items:
        if getattr(item, "symbol", "").upper() == coin.upper():
            return item
    return None


def _compute_vacuum_gap_pct(
    zone: WallZone,
    depth_latest: Optional[OrderbookDepthSnapshot],
    direction: str,
    target_price: float,
    cfg: dict,
) -> float:
    """计算 wall 到 magnet 之间的真空跨度（%）。

    简化定义：在 [zone, target_price] 之间，遍历同侧 bin，
    最大相邻 bin 价差 / last_price 即真空跨度（最大 1.5% 截断）。
    """
    if depth_latest is None or zone is None or target_price <= 0:
        return 0.0
    bins = depth_latest.bids if zone.side == "bid" else depth_latest.asks
    if not bins:
        return 0.0
    max_dist = cfg.get("vacuum_max_distance_pct",
                       ENGINE_DEFAULTS["vacuum_max_distance_pct"])

    if direction == "below":
        rel_bins = [b for b in bins if target_price <= b.price <= zone.price_low]
    else:
        rel_bins = [b for b in bins if zone.price_high <= b.price <= target_price]

    if len(rel_bins) < 2:
        # 区间内完全没 bin / 仅 1 个 bin → 直接以两端价差为真空
        ref_price = max(target_price, zone.price_low) if direction == "below" \
                    else min(target_price, zone.price_high)
        if ref_price <= 0:
            return 0.0
        gap_pct = abs(target_price - (zone.price_low if direction == "below" else zone.price_high))
        return min(max_dist, gap_pct / max(ref_price, 1e-9) * 100.0)

    rel_bins.sort(key=lambda b: b.price)
    max_gap = 0.0
    for i in range(1, len(rel_bins)):
        gap = abs(rel_bins[i].price - rel_bins[i-1].price)
        max_gap = max(max_gap, gap)
    last_price = max(target_price, 1e-9)
    return min(max_dist, max_gap / last_price * 100.0)


def _build_sweep_target(
    zone: WallZone,
    max_pain_item: Any,
    last_price: float,
    depth_latest: Optional[OrderbookDepthSnapshot],
    cfg: dict,
) -> Optional[SweepTarget]:
    """根据 zone 的 side 选取磁铁方向：

    - bid wall（下方买墙）→ 打穿后价格继续下跌 → 取 long_pain_price（多头清算磁铁）
    - ask wall（上方卖墙）→ 打穿后价格继续上涨 → 取 short_pain_price（空头清算磁铁）
    """
    if max_pain_item is None or last_price <= 0:
        return None
    if zone.side == "bid":
        magnet_price = float(getattr(max_pain_item, "long_pain_price", 0) or 0)
        magnet_amount = float(getattr(max_pain_item, "long_pain_usd", 0) or 0)
        direction = "below"
    else:
        magnet_price = float(getattr(max_pain_item, "short_pain_price", 0) or 0)
        magnet_amount = float(getattr(max_pain_item, "short_pain_usd", 0) or 0)
        direction = "above"

    if magnet_price <= 0:
        return None
    distance_pct = (magnet_price - last_price) / last_price * 100.0
    vacuum_pct = _compute_vacuum_gap_pct(zone, depth_latest, direction, magnet_price, cfg)

    label = "多头" if zone.side == "bid" else "空头"
    explain = f"下方{label}清算磁铁 {magnet_price:.2f}（{magnet_amount/1e6:.0f}M USD）" \
              if direction == "below" else \
              f"上方{label}清算磁铁 {magnet_price:.2f}（{magnet_amount/1e6:.0f}M USD）"

    return SweepTarget(
        direction=direction,
        magnet_price=magnet_price,
        magnet_amount_usd=magnet_amount,
        distance_pct=distance_pct,
        vacuum_gap_pct=vacuum_pct,
        explain=explain,
    )


def _liquidity_drain_pct(history: Optional[Sequence], side: str) -> Optional[float]:
    """Phase B+：从 ±range 时序计算 same-side USD 衰减比例（0-∞，None=数据不足）。

    返回 None：history 为空或长度 < 2 / 基线为 0
    返回 0.0：未衰减或在增厚
    返回 > 0：衰减比例（0.05 = 5%）
    """
    if not history or len(history) < 2:
        return None
    recent = list(history)
    baseline = recent[0]
    latest = recent[-1]
    if side == "bid":
        base = float(getattr(baseline, "aggregated_bids_usd", 0) or 0)
        cur = float(getattr(latest, "aggregated_bids_usd", 0) or 0)
    else:
        base = float(getattr(baseline, "aggregated_asks_usd", 0) or 0)
        cur = float(getattr(latest, "aggregated_asks_usd", 0) or 0)
    if base <= 0:
        return None
    if cur >= base:
        return 0.0
    return (base - cur) / base


def _stale_weight(age_sec: int, fresh_max: int = 600, dead_min: int = 900) -> float:
    """W2-T3：根据数据 age 计算降权系数（0-1）。

    - age ≤ fresh_max（默认 10min）→ 1.0（数据新鲜，全权重）
    - age ≥ dead_min（默认 15min）→ 0.0（数据 stale，因子贡献清零）
    - 中间线性 ramp：avoid cliff jump

    设计理由：
      - taker_flow 5min 拉一次，>10min 仍未刷新 = 拉取出错 → 不应用作"实时攻击"信号
      - cvd 5min 粒度，>15min 跨多帧 → 趋势判定可能滞后
      - drain 30min 窗口本身已带容差，>15min 该衰减早就过期了
      - age = 0（数据源完全没时间戳，旧测试夹具）→ 1.0 不降权（向后兼容）
    """
    if age_sec <= 0 or age_sec <= fresh_max:
        return 1.0
    if age_sec >= dead_min:
        return 0.0
    # 线性插值：age=fresh_max → 1.0；age=dead_min → 0.0
    return round(max(0.0, min(1.0, (dead_min - age_sec) / max(dead_min - fresh_max, 1))), 3)


def _ts_of(obj: Any, attr_candidates: tuple = ("ts_sec", "ts")) -> int:
    """从对象多个候选字段取 unix-second 时间戳；失败返 0。"""
    if obj is None:
        return 0
    for attr in attr_candidates:
        v = getattr(obj, attr, None)
        if v is not None:
            try:
                ts = int(v)
                # 兼容 ms：> 1e12 视为 ms 转 s
                if ts > 1_000_000_000_000:
                    return ts // 1000
                return ts
            except (TypeError, ValueError):
                continue
    return 0


def _compute_active_attack_score(
    zone: WallZone,
    taker_flow: Any,
    cvd_spot: Any,
    cfg: dict,
    ask_bids_history: Optional[Sequence] = None,
    spot_ask_bids_history: Optional[Sequence] = None,
    now_sec: Optional[int] = None,
) -> float:
    """Phase A+B+：实时主动攻击强度（0-1，作为 break_through_risk 加项）。

    回应 GPT P1-3："break_through_risk 缺乏'主动攻击'因子，纯静态指标无法
    反映正在发生的吃单/挤兑。"

    W2-T3 升级（source-aware stale 降权）：
      数据源新鲜度直接决定该因子的权重。stale 数据（如 taker_flow 已 30min 未更新）
      不再被错误当作"实时攻击"，避免在数据老化时 break_through_risk 被高估。
      - taker_flow.ts：> 10min ramp 降权；> 15min 因子清零
      - cvd_spot：从 series[-1].ts 推 age（无 series 时 fallback obj.ts）
      - drain factor：以 history[-1].ts_sec 为准
      now_sec=None 或对象无 ts → 默认全权重（向后兼容旧测试夹具）

    构成（同向攻击才计分；逆向攻击 = 0；三因子加权 × 各自 stale 降权系数）：
      - 0.40 × taker 同向占比 × stale_weight(taker_age)
              bid wall 看 sell_ratio；ask wall 看 buy_ratio
              ≥ 60% 同向 → 1.0（线性映射 0.5→0, 0.6→1.0）
      - 0.30 × cvd_spot 同向 trend × stale_weight(cvd_age)
              bid wall：trend ∈ {down, strong_down} / ask wall：trend ∈ {up, strong_up}
      - 0.30 × **流动性衰竭因子** × stale_weight(drain_age)
              现货优先 → 合约 fallback
              30min 内 same-side USD 下跌 ≥ 5% → 1.0（线性映射）
    """
    score = 0.0

    # 1) taker 同向占比 × stale 降权
    if taker_flow is not None:
        taker_factor = 0.0
        # 兼容两种字段名：旧 buy_volume_usd / sell_volume_usd（测试夹具）+
        # 新 buy_ratio / sell_ratio（生产 TakerFlowData 模型）
        try:
            br = getattr(taker_flow, "buy_ratio", None)
            sr = getattr(taker_flow, "sell_ratio", None)
            if br is not None and sr is not None and (float(br) + float(sr)) > 0:
                same_ratio = float(sr) if zone.side == "bid" else float(br)
            else:
                buy = float(getattr(taker_flow, "buy_volume_usd", 0) or 0)
                sell = float(getattr(taker_flow, "sell_volume_usd", 0) or 0)
                total = buy + sell
                same_ratio = (sell if zone.side == "bid" else buy) / total if total > 0 else 0.0
            if same_ratio > 0.5:
                taker_factor = min(1.0, max(0.0, (same_ratio - 0.5) / 0.10))
        except (TypeError, ValueError):
            pass
        if taker_factor > 0:
            tw = 1.0
            if now_sec is not None:
                ts = _ts_of(taker_flow)
                if ts > 0:
                    tw = _stale_weight(now_sec - ts, fresh_max=600, dead_min=900)
            score += 0.40 * taker_factor * tw

    # 2) cvd_spot 同向趋势 × stale 降权
    if cvd_spot is not None:
        trend = getattr(cvd_spot, "trend_1h", None)
        if zone.side == "bid":
            same_trend = trend in ("down", "strong_down")
        else:
            same_trend = trend in ("up", "strong_up")
        if same_trend:
            cw = 1.0
            if now_sec is not None:
                # CVDData 有 series[-1].ts；旧夹具没 series → 退化全权重
                series = getattr(cvd_spot, "series", None) or []
                ts = _ts_of(series[-1]) if series else _ts_of(cvd_spot)
                if ts > 0:
                    cw = _stale_weight(now_sec - ts, fresh_max=600, dead_min=900)
            score += 0.30 * cw

    # 3) Phase B+：流动性衰竭因子 × stale 降权（现货优先 → 合约 fallback）
    drain_pct = _liquidity_drain_pct(spot_ask_bids_history, zone.side)
    drain_history = spot_ask_bids_history
    if drain_pct is None:
        drain_pct = _liquidity_drain_pct(ask_bids_history, zone.side)
        drain_history = ask_bids_history
    if drain_pct is not None and drain_pct > 0:
        dw = 1.0
        if now_sec is not None and drain_history:
            ts = _ts_of(drain_history[-1])
            if ts > 0:
                dw = _stale_weight(now_sec - ts, fresh_max=600, dead_min=900)
        score += 0.30 * min(1.0, drain_pct / 0.05) * dw

    return round(min(1.0, score), 3)


def _liquidity_imbalance_score(
    zone: WallZone,
    ask_bids_history: Optional[Sequence],
    spot_ask_bids_history: Optional[Sequence],
) -> float:
    """W2-T2 新增：本侧 vs 对侧供给失衡评分（0-1）。

    逻辑：bid 墙的"对侧"是 ask 供给（卖盘）；ask 墙的"对侧"是 bid 供给（买盘）。
      - 对侧供给远大于本侧 → 价格更可能朝对侧推进 → 本侧墙易被打穿
      - 比例 = same_side_usd / opposite_side_usd
        - ratio ≥ 1.0：无失衡（本侧 ≥ 对侧）→ 0
        - ratio < 0.5：对侧是本侧的 2x → 0.5
        - ratio < 0.3：对侧是本侧的 3.3x → 1.0（线性映射）
        - 中间线性插值

    数据源优先级：现货 aggregated > 合约 aggregated（与 active_attack 一致）。
    """
    def _ratio_from_history(history: Optional[Sequence]) -> Optional[float]:
        if not history:
            return None
        latest = history[-1]
        same = float(getattr(latest, "aggregated_bids_usd", 0) or 0) if zone.side == "bid" \
            else float(getattr(latest, "aggregated_asks_usd", 0) or 0)
        opp = float(getattr(latest, "aggregated_asks_usd", 0) or 0) if zone.side == "bid" \
            else float(getattr(latest, "aggregated_bids_usd", 0) or 0)
        if same <= 0 or opp <= 0:
            return None
        return same / opp

    ratio = _ratio_from_history(spot_ask_bids_history)
    if ratio is None:
        ratio = _ratio_from_history(ask_bids_history)
    if ratio is None or ratio >= 1.0:
        return 0.0
    if ratio <= 0.3:
        return 1.0
    # 0.3 → 1.0；0.5 → 0.5；线性映射在 [0.3, 1.0] → [1.0, 0.0]
    return round(max(0.0, min(1.0, (1.0 - ratio) / 0.7)), 3)


def _compute_break_through_risk(
    zone: WallZone,
    crowding: Optional[PositionCrowdingSnapshot],
    sweep: Optional[SweepTarget],
    cfg: dict,
    taker_flow: Any = None,
    cvd_spot: Any = None,
    ask_bids_history: Optional[Sequence] = None,
    spot_ask_bids_history: Optional[Sequence] = None,
) -> float:
    """0-1 软分：墙是否容易被打穿。

    W2-T2 重构（回应审计 P1）：
      根因 1：旧 "persistence < 0.3 → +0.20" 单调项陷阱
        新墙 / 暖机期墙因 persistence 低被误判为"高打穿风险"，但新墙也可能是
        刚出现的强支撑，不应一刀切惩罚。
        修正：去掉单调项，改为"thinning AND persistence < 0.3 → +0.10"复合条件
        （只有当墙变薄 + 短期 时才加分；稳定的新墙不加分）。
      根因 2：缺失"流动性失衡分"
        只看单边 thinning，没看"对侧供给是否远大于本侧"。
        新增：_liquidity_imbalance_score（同侧/对侧 USD 比例 < 0.5 时加分）。
      根因 3：active_attack 重复计算
        W2-T1 主流程已把 active_attack_score 写入 zone 字段。优先用 zone 字段，
        无值时 fallback 重算（保持既有测试调用方兼容）。

    构成（权重重新分配，总上限 1.0）：
      - thinning（current/max < 0.5）：             +0.25
      - thinning AND persistence < 0.3 复合：        +0.10（短期 + 变薄）
      - 同向清算磁铁距离 < 0.5%：                   +0.20
      - 真空跨度 ≥ 0.5%：                          +0.15
      - 同向 crowding_risk ≥ 0.6：                 +0.10
      - active_attack_score：                        × 0.10（最多 +0.10）
      - 流动性失衡（对侧供给远大于本侧）：          × 0.10（最多 +0.10）
      合计上限 1.00（实际峰值场景下 ≈ 0.95）
    """
    score = 0.0
    is_thinning = False
    if zone.max_usd_1h > 0:
        ratio = zone.current_usd / zone.max_usd_1h
        if ratio < cfg.get("break_through_thinning_threshold",
                           ENGINE_DEFAULTS["break_through_thinning_threshold"]):
            score += 0.25
            is_thinning = True
    # W2-T2：复合条件 — 只有"变薄 + 短期"才加分；稳定新墙不被惩罚
    if is_thinning and zone.persistence_score < 0.3:
        score += 0.10
    if sweep is not None and abs(sweep.distance_pct) < 0.5:
        score += 0.20
    if sweep is not None and sweep.vacuum_gap_pct >= 0.5:
        score += 0.15
    if crowding is not None:
        risk = crowding.long_crowding_risk if zone.side == "bid" else crowding.short_crowding_risk
        if risk >= 0.6:
            score += 0.10
    # W2-T2：优先用 zone.active_attack_score 字段（W2-T1 主流程已写入）；
    # 字段为 0 时 fallback 重算（向后兼容旧测试调用方直接传 taker_flow / cvd_spot）
    aa = zone.active_attack_score
    if aa <= 0:
        aa = _compute_active_attack_score(
            zone, taker_flow, cvd_spot, cfg,
            ask_bids_history=ask_bids_history,
            spot_ask_bids_history=spot_ask_bids_history,
        )
    score += 0.10 * aa
    # W2-T2：新增流动性失衡因子
    imbalance = _liquidity_imbalance_score(
        zone, ask_bids_history, spot_ask_bids_history,
    )
    score += 0.10 * imbalance
    return round(min(1.0, score), 3)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# M2：行为事件 + consumed_confidence + removal_risk
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _compute_wall_consumed_confidence(
    zone: WallZone,
    consumed_orders_in_zone: list[LargeOrderLifecycle],
    taker_flow: Any,
    last_price: float,
    cfg: dict,
) -> float:
    """GPT 加权公式：wall_consumed_confidence。

      = 0.50 × large_order_executed_score
      + 0.25 × taker_pressure_score
      + 0.25 × price_through_score

    各分项 0-1。
    """
    w_lo = cfg.get("consumed_lo_weight", ENGINE_DEFAULTS["consumed_lo_weight"])
    w_taker = cfg.get("consumed_taker_weight", ENGINE_DEFAULTS["consumed_taker_weight"])
    w_price = cfg.get("consumed_price_weight", ENGINE_DEFAULTS["consumed_price_weight"])

    # 1. large_order 已成交 USD 占 wall 1h max 厚度的比例（0-1，超过 full_threshold 算满分）
    lo_score = 0.0
    if zone.max_usd_1h > 0 and consumed_orders_in_zone:
        executed_total = sum(lo.executed_usd_value for lo in consumed_orders_in_zone)
        full_thr = cfg.get("consumed_lo_full_threshold",
                           ENGINE_DEFAULTS["consumed_lo_full_threshold"])
        lo_score = min(1.0, executed_total / max(zone.max_usd_1h * full_thr, 1.0))

    # 2. taker pressure（bid wall 看 taker_sell；ask wall 看 taker_buy）
    taker_score = 0.0
    if taker_flow is not None:
        try:
            buy = float(getattr(taker_flow, "buy_volume_usd", 0) or 0)
            sell = float(getattr(taker_flow, "sell_volume_usd", 0) or 0)
            total = buy + sell
            if total > 0:
                if zone.side == "bid":
                    taker_score = min(1.0, sell / total / 0.6)   # ≥60% sell → 1.0
                else:
                    taker_score = min(1.0, buy / total / 0.6)
        except (TypeError, ValueError):
            taker_score = 0.0

    # 3. price_through_score（价格穿透程度）
    price_score = 0.0
    if last_price > 0:
        if zone.side == "bid":
            # bid wall 被吃 → 价格应跌破 price_low
            if last_price < zone.price_low:
                penetration = (zone.price_low - last_price) / max(zone.price_low, 1e-9)
                price_score = min(1.0, penetration / 0.005)   # 跌破 0.5% → 1.0
        else:
            if last_price > zone.price_high:
                penetration = (last_price - zone.price_high) / max(zone.price_high, 1e-9)
                price_score = min(1.0, penetration / 0.005)

    return round(w_lo * lo_score + w_taker * taker_score + w_price * price_score, 3)


def _compute_wall_removal_risk(
    zone: WallZone,
    lifecycles_in_zone: list[LargeOrderLifecycle],
    cfg: dict,
) -> float:
    """0-1 软分（不输出"假单"）。

    构成：
      - ended without execution（state=ended & executed=0）占比 ≥ 0.5：+0.40
      - persistence < 0.2：                                            +0.30
      - current_usd / max_usd < 0.4（厚度大幅下滑）：                    +0.20
      - holding 时间均值 < 5min（快闪挂单）：                            +0.10
    """
    score = 0.0
    if lifecycles_in_zone:
        ended_no_exec = sum(
            1 for lo in lifecycles_in_zone
            if lo.state == "ended" and lo.executed_usd_value == 0
        )
        if ended_no_exec / max(len(lifecycles_in_zone), 1) >= 0.5:
            score += 0.40
        avg_holding = sum(lo.holding_age_sec for lo in lifecycles_in_zone) / \
                      max(len(lifecycles_in_zone), 1)
        if avg_holding < 300:
            score += 0.10
    if zone.persistence_score < 0.2:
        score += 0.30
    if zone.max_usd_1h > 0 and zone.current_usd / zone.max_usd_1h < 0.4:
        score += 0.20
    return round(min(1.0, score), 3)


def _detect_zone_lifecycle_events(
    zones: list[WallZone],
    large_orders: Sequence[LargeOrderLifecycle],
    last_price: float,
    cfg: dict,
    now: int,
) -> list[WallEvent]:
    """基于 large_orders 18 字段差分识别 6 类事件。

    简化版（M2 第一阶段）：仅基于 large_order 状态识别 consumed/removed/reloaded，
    appeared/strengthened/weakened 由 zone.trend 推断（已在 M1 stats 计算）。

    后续 M3+ 可加入"按 zone 跨帧差分"的更精细事件。
    """
    events: list[WallEvent] = []
    if not zones:
        return events

    # 按 zone 收集 large_orders 子集
    for z in zones:
        # W1-T4：所有事件统一带上 zone 的 wall_zone_id（已由主流程预先赋值）
        zid = z.wall_zone_id or ""

        if not z.large_order_ids:
            # 仅 trend 派生事件
            if z.trend == "new":
                events.append(WallEvent(
                    ts_sec=now, side=z.side, price_mid=z.price_mid,
                    event_type="wall_appeared",
                    size_after_usd=z.current_usd, confidence=0.5,
                    explain=f"{('上方卖' if z.side == 'ask' else '下方买')}墙首次出现",
                    wall_zone_id=zid,
                ))
            elif z.trend == "strengthening":
                events.append(WallEvent(
                    ts_sec=now, side=z.side, price_mid=z.price_mid,
                    event_type="wall_strengthened",
                    size_before_usd=z.avg_usd_1h, size_after_usd=z.current_usd,
                    confidence=0.6,
                    explain=f"墙增厚 {(z.current_usd/max(z.avg_usd_1h,1)-1)*100:.0f}%",
                    wall_zone_id=zid,
                ))
            elif z.trend == "weakening":
                events.append(WallEvent(
                    ts_sec=now, side=z.side, price_mid=z.price_mid,
                    event_type="wall_weakened",
                    size_before_usd=z.avg_usd_1h, size_after_usd=z.current_usd,
                    confidence=0.6,
                    explain=f"墙减薄 {(1-z.current_usd/max(z.avg_usd_1h,1))*100:.0f}%",
                    wall_zone_id=zid,
                ))
            continue

        # large_orders 共振：按 id 取出对应 lifecycles
        zone_lo_ids = set(z.large_order_ids)
        zone_los = [lo for lo in large_orders if lo.id in zone_lo_ids]
        ended_with_exec = [lo for lo in zone_los
                            if lo.state == "ended" and lo.executed_usd_value > 0]
        ended_no_exec = [lo for lo in zone_los
                          if lo.state == "ended" and lo.executed_usd_value == 0]

        # consumed
        if ended_with_exec:
            total_executed = sum(lo.executed_usd_value for lo in ended_with_exec)
            taker_flow = None  # 没传，confidence 用简化版
            conf = _compute_wall_consumed_confidence(
                z, ended_with_exec, taker_flow, last_price, cfg,
            )
            events.append(WallEvent(
                ts_sec=now, side=z.side, price_mid=z.price_mid,
                event_type="wall_consumed",
                size_before_usd=z.max_usd_1h,
                size_after_usd=z.current_usd,
                executed_usd_value=total_executed,
                confidence=conf,
                explain=f"已有大额限价单成交消耗 {total_executed/1e6:.1f}M USD",
                wall_zone_id=zid,
            ))

        # removed
        if ended_no_exec:
            events.append(WallEvent(
                ts_sec=now, side=z.side, price_mid=z.price_mid,
                event_type="wall_removed",
                size_before_usd=z.max_usd_1h,
                size_after_usd=z.current_usd,
                confidence=0.7,
                explain=f"{len(ended_no_exec)} 笔大单未成交结束(撤单风险)",
                wall_zone_id=zid,
            ))

        # W2-T5：第 7 类复合事件 — 同帧既消耗又撤单（试盘 + 撤退 footprint）
        # 触发条件：ended_with_exec 与 ended_no_exec 都非空
        # 语义：机构试探流动性后撤退；比单一 consumed / removed 都更可疑
        # confidence 0.75 高于单一事件（联合证据强）
        if ended_with_exec and ended_no_exec:
            total_executed = sum(lo.executed_usd_value for lo in ended_with_exec)
            events.append(WallEvent(
                ts_sec=now, side=z.side, price_mid=z.price_mid,
                event_type="wall_consumed_and_removed",
                size_before_usd=z.max_usd_1h,
                size_after_usd=z.current_usd,
                executed_usd_value=total_executed,
                confidence=0.75,
                explain=(
                    f"试盘后撤退：吃单 {total_executed/1e6:.1f}M USD + 撤单 "
                    f"{len(ended_no_exec)} 笔（机构 footprint，比纯 spoof 更可疑）"
                ),
                wall_zone_id=zid,
            ))

        # reloaded：在 reload_window_seconds 内出现新挂单（end_time + window > now，且新 id 落在同价位）
        win = cfg.get("reload_window_seconds", ENGINE_DEFAULTS["reload_window_seconds"])
        tol = cfg.get("reload_price_tol_pct", ENGINE_DEFAULTS["reload_price_tol_pct"])
        recently_ended_prices: set[float] = set()
        for lo in ended_no_exec + ended_with_exec:
            if lo.end_time_ms and (now - lo.end_time_ms / 1000) <= win:
                recently_ended_prices.add(lo.limit_price)
        new_holdings = [lo for lo in zone_los
                         if lo.state == "holding"
                         and any(abs(lo.limit_price - p) / max(p, 1e-9) <= tol
                                 for p in recently_ended_prices)]
        if new_holdings:
            events.append(WallEvent(
                ts_sec=now, side=z.side, price_mid=z.price_mid,
                event_type="wall_reloaded",
                size_after_usd=sum(lo.current_usd_value for lo in new_holdings),
                confidence=0.65,
                explain=f"撤后 {win}s 内同价位重挂 {len(new_holdings)} 笔",
                wall_zone_id=zid,
            ))

        # appeared/strengthened/weakened（trend 派生）
        if z.trend == "new":
            events.append(WallEvent(
                ts_sec=now, side=z.side, price_mid=z.price_mid,
                event_type="wall_appeared",
                size_after_usd=z.current_usd, confidence=0.5,
                explain=f"{('上方卖' if z.side == 'ask' else '下方买')}墙首次出现",
                wall_zone_id=zid,
            ))
        elif z.trend == "strengthening":
            events.append(WallEvent(
                ts_sec=now, side=z.side, price_mid=z.price_mid,
                event_type="wall_strengthened",
                size_before_usd=z.avg_usd_1h, size_after_usd=z.current_usd,
                confidence=0.6,
                explain=f"墙增厚 {(z.current_usd/max(z.avg_usd_1h,1)-1)*100:.0f}%",
                wall_zone_id=zid,
            ))
        elif z.trend == "weakening":
            events.append(WallEvent(
                ts_sec=now, side=z.side, price_mid=z.price_mid,
                event_type="wall_weakened",
                size_before_usd=z.avg_usd_1h, size_after_usd=z.current_usd,
                confidence=0.6,
                explain=f"墙减薄 {(1-z.current_usd/max(z.avg_usd_1h,1))*100:.0f}%",
                wall_zone_id=zid,
            ))

    return events


def _classify_zone_status(
    zone: WallZone,
    events_for_zone: list[WallEvent],
) -> WallZoneStatus:
    """status 优先级：consumed > removed > reloaded > strengthening/weakening > active。"""
    types = {e.event_type for e in events_for_zone}
    if "wall_consumed" in types:
        return "consumed"
    if "wall_removed" in types:
        return "removed"
    if "wall_reloaded" in types:
        return "reloaded"
    if "wall_strengthened" in types:
        return "strengthening"
    if "wall_weakened" in types:
        return "weakening"
    if zone.confluence_with_absorption:
        return "absorbed"
    return "active"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 主入口：build_liquidity_wall_outputs
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def build_liquidity_wall_outputs(
    state: "CoinState",
    base_snap: OrderbookPressureSnapshot,
    cfg: dict,
    now: int,
) -> _BuildOutputs:
    """流动性墙引擎主入口（被 compute_pressure_snapshot 末尾调用）。

    输入：state（只读）、已构造的 base_snap（含 walls / atr / last_price）
    输出：M1+M2 全套字段，由调用方写回 base_snap 的新字段。
    """
    last_price = base_snap.last_price
    atr = base_snap.atr
    coin = state.coin

    history_raw = list(getattr(state, "orderbook_depth_history", []) or [])
    history_size = len(history_raw)
    spot_history_raw = list(getattr(state, "spot_orderbook_depth_history", []) or [])

    # ── A6：seed_min_usd 按币动态覆盖（cfg 已合并 by_coin 表）──
    # coin 是 str（CoinState.coin），by_coin 表来自 config.liquidity_wall_engine
    seed_by_coin = cfg.get("seed_min_usd_by_coin")
    if isinstance(seed_by_coin, dict) and coin:
        coin_seed = seed_by_coin.get(str(coin).upper())
        if coin_seed and coin_seed > 0:
            cfg = {**cfg, "seed_min_usd": float(coin_seed)}

    # 暖机判定（仅当存在但不满时才标 warming；完全缺失走旧路径不打扰）：
    #   - history_size == 0：未启用滚动历史（旧测试夹具/兼容路径），不动 data_quality
    #   - 1 ≤ history_size < 4：暖机中（< 20min）
    #   - history_size ≥ 4 但第一帧距今 < warming_seconds（30min）：仍暖机
    warming_seconds = cfg.get("warming_seconds", ENGINE_DEFAULTS["warming_seconds"])
    warming = False
    if 0 < history_size < 4:
        warming = True
    elif history_size >= 4 and history_raw[0].ts_sec > 0:
        elapsed = now - history_raw[0].ts_sec
        if elapsed < warming_seconds:
            warming = True

    # ── M1：墙区聚合（合约 5m 热力图为主路径）──
    walls_above: list[WallZone] = []
    walls_below: list[WallZone] = []
    if history_raw and last_price > 0:
        walls_above = _build_zones_for_side(
            history_raw, last_price, "ask", atr, cfg,
        )
        walls_below = _build_zones_for_side(
            history_raw, last_price, "bid", atr, cfg,
        )

    # ── Phase A：现货 5m 热力图叠加 → dual_source 标记 + spot_only 增量 ──
    if spot_history_raw and last_price > 0:
        # 叠加：在合约 zone 价区上累加现货厚度
        _augment_zones_with_spot_depth(walls_above, spot_history_raw, cfg)
        _augment_zones_with_spot_depth(walls_below, spot_history_raw, cfg)
        # 增量：现货独立 zone（仅保留未被合约 zone 覆盖的价区）
        excl_above = [(z.price_low, z.price_high) for z in walls_above]
        excl_below = [(z.price_low, z.price_high) for z in walls_below]
        spot_only_above = _build_spot_only_zones(
            spot_history_raw, last_price, "ask", atr, cfg, excl_above,
        )
        spot_only_below = _build_spot_only_zones(
            spot_history_raw, last_price, "bid", atr, cfg, excl_below,
        )
        walls_above.extend(spot_only_above)
        walls_below.extend(spot_only_below)

    # large_orders augment（合约大单 → 流动性墙 / 清算磁铁基础源）
    large_orders = list(getattr(state, "large_orders_history", []) or [])
    if large_orders:
        _augment_zones_with_large_orders(walls_above, large_orders, last_price, cfg)
        _augment_zones_with_large_orders(walls_below, large_orders, last_price, cfg)

    # M2.5：spot_large_orders augment（现货大单 lifecycle → 真买家/卖家硬证据）
    spot_large_orders = list(getattr(state, "spot_large_orders_history", []) or [])
    if spot_large_orders:
        _augment_zones_with_spot_large_orders(walls_above, spot_large_orders, cfg)
        _augment_zones_with_spot_large_orders(walls_below, spot_large_orders, cfg)

    # Phase C：Coinbase 现货原生 orderbook augment（机构资金独立验证维度）
    coinbase_frame = getattr(state, "coinbase_orderbook", None)
    if coinbase_frame is not None:
        _augment_zones_with_coinbase(walls_above, coinbase_frame, cfg)
        _augment_zones_with_coinbase(walls_below, coinbase_frame, cfg)

    # W2-T4：USD/USDT basis（暖机期或缺数据时为 None；正常 < 5bp）
    usd_usdt_basis_pct = _compute_usd_usdt_basis_pct(coinbase_frame, last_price)

    # 强度等级
    _attach_strength_tier(walls_above, cfg)
    _attach_strength_tier(walls_below, cfg)

    # W1-T4：稳定 wall_zone_id（必须在 _detect_zone_lifecycle_events 之前赋值，
    # 否则事件无法关联到 zone）。同一物理墙跨帧 ID 不变 → 后验脚本据此串联生命周期。
    for z in walls_above:
        z.wall_zone_id = _build_wall_zone_id(coin, "ask", z.peak_price, atr)
    for z in walls_below:
        z.wall_zone_id = _build_wall_zone_id(coin, "bid", z.peak_price, atr)

    # M2.5：trust_score（必须在 spot augment + tier 之后；用 has_spot_confluence /
    # exchange_count / persistence_score 综合）
    # W2-T1：用 _compute_trust_breakdown 同时拿 components；写入 raw_trust_score
    for z in walls_above:
        final, comps = _compute_trust_breakdown(z, cfg)
        z.trust_score = final
        z.raw_trust_score = final
        z.trust_components = comps
    for z in walls_below:
        final, comps = _compute_trust_breakdown(z, cfg)
        z.trust_score = final
        z.raw_trust_score = final
        z.trust_components = comps

    # ── M2：拥挤度（一次性算，全局共享） ──
    crowding = None
    try:
        crowding = _build_position_crowding(state, cfg)
    except Exception as exc:
        logger.warning("position_crowding build failed | coin=%s err=%s", coin, exc)

    # ── M2：扫单磁铁（每个 zone 单独算）──
    max_pain_data = None
    liq_max_pain = getattr(state, "liq_max_pain", None) or {}
    if isinstance(liq_max_pain, dict):
        max_pain_data = liq_max_pain.get("24h") or liq_max_pain.get("4h")
    max_pain_item = _pick_max_pain_for_coin(max_pain_data, coin) if max_pain_data else None
    depth_latest: Optional[OrderbookDepthSnapshot] = history_raw[-1] if history_raw else None

    # ── M2：行为事件（所有 zone 一次性识别） ──
    all_zones = walls_above + walls_below
    events: list[WallEvent] = []
    if all_zones:
        events = _detect_zone_lifecycle_events(
            all_zones, large_orders, last_price, cfg, now,
        )

    # ── 把上下文 + sweep + status + confidence 写到每个 zone（暖机不写 magnet/persistence 数字） ──
    for z in all_zones:
        z.crowding_context = crowding
        # 暖机期不写 sweep（避免 magnet 数据误导）
        if not warming and max_pain_item is not None:
            sweep = _build_sweep_target(z, max_pain_item, last_price, depth_latest, cfg)
            z.sweep_target = sweep
            if sweep is not None:
                z.next_magnet_price = sweep.magnet_price

        # consumed_confidence / removal_risk
        zone_lo_ids = set(z.large_order_ids)
        zone_los = [lo for lo in large_orders if lo.id in zone_lo_ids]
        consumed_los = [lo for lo in zone_los
                         if lo.state == "ended" and lo.executed_usd_value > 0]
        if consumed_los:
            z.wall_consumed_confidence = _compute_wall_consumed_confidence(
                z, consumed_los, getattr(state, "taker_flow", None), last_price, cfg,
            )
        z.wall_removal_risk = _compute_wall_removal_risk(z, zone_los, cfg)

        # status（基于 zone 自身的事件子集）
        z_events = [e for e in events
                     if e.side == z.side and abs(e.price_mid - z.price_mid) < 1e-6]
        z.status = _classify_zone_status(z, z_events)

        # W2-T1：active_attack_score 提升为 zone 字段（供 SR/SA + archiver 复用）
        # 必须在 break_through_risk 之前写入
        # W2-T3：传入 now_sec 启用 source-aware stale 降权（数据老化时因子贡献清零）
        z.active_attack_score = _compute_active_attack_score(
            z,
            getattr(state, "taker_flow", None),
            getattr(state, "cvd_spot", None),
            cfg,
            ask_bids_history=list(
                getattr(state, "aggregated_ask_bids_history", []) or []
            ),
            spot_ask_bids_history=list(
                getattr(state, "spot_aggregated_ask_bids_history", []) or []
            ),
            now_sec=now,
        )

        # break_through_risk（Phase A+B+：主动攻击 + 流动性衰竭，现货优先 fallback 合约）
        z.break_through_risk = _compute_break_through_risk(
            z, crowding, z.sweep_target, cfg,
            taker_flow=getattr(state, "taker_flow", None),
            cvd_spot=getattr(state, "cvd_spot", None),
            ask_bids_history=list(
                getattr(state, "aggregated_ask_bids_history", []) or []
            ),
            spot_ask_bids_history=list(
                getattr(state, "spot_aggregated_ask_bids_history", []) or []
            ),
        )

        # W2-T1：SR / SA 独立维度（必须在 active_attack / sweep_target / removal_risk
        # / consumed_confidence 都写入后计算）
        z.support_resistance_trust_score = _compute_support_resistance_trust_score(z, cfg)
        z.sweep_attractiveness_score = _compute_sweep_attractiveness_score(
            z, crowding, last_price, cfg,
        )

        # zone-level explain_chips（最多 3 条；Phase A 优先输出双源/现货标签）
        chips: list[str] = []
        # 注：来源标签（dual_source / spot_only / has_spot_confluence）由前端独立渲染
        # 此处只补"前端徽章未覆盖"的辅助 chip，避免与前端徽章重复展示
        # 1) 持续性（带"中"档区分高 / 中持续）
        if z.persistence_score >= 0.7:
            chips.append(f"持续 {int(z.visible_minutes)}min")
        elif z.persistence_score >= 0.4:
            chips.append(f"持续 {int(z.visible_minutes)}min(中)")
        # 2) 多所共振
        if z.exchange_count >= 2:
            chips.append(f"{z.exchange_count}所共振")
        # 3) 行为状态变化（前端已有但简短，这里补全语义）
        if z.status in ("consumed", "removed", "reloaded"):
            chips.append({"consumed": "已被吃",
                           "removed": "已撤",
                           "reloaded": "重挂"}[z.status])
        z.explain_chips = chips[:3]

    # 距离升序：above 由近及远（升序），below 由近及远（降序）
    walls_above.sort(key=lambda z: z.distance_pct)
    walls_below.sort(key=lambda z: -z.distance_pct)

    # zones：按 strength_score 降序（前端用作 top-list 时方便）
    zones_sorted = sorted(all_zones, key=lambda z: -z.strength_score)

    # events 滚动：保留最近 100 条
    events_sorted = sorted(events, key=lambda e: e.ts_sec)[-100:]

    return _BuildOutputs(
        walls_above=walls_above,
        walls_below=walls_below,
        zones=zones_sorted,
        events=events_sorted,
        crowding=crowding,
        warming=warming,
        window_min=cfg.get("history_window_minutes",
                           ENGINE_DEFAULTS["history_window_minutes"]),
        history_size=history_size,
        usd_usdt_basis_pct=usd_usdt_basis_pct,
    )
