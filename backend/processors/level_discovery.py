"""Level Discovery Engine — 三维候选位生成

三大维度：
  A. 价格结构 (Swing H/L, 前高前低, 未回补影线, 成交密集区)
  B. 数学指标 (EMA 簇, SMA200, BMSA, Fibonacci, Pivot, Ichimoku, BOLL)
  C. 资金仓位 (清算簇, VP POC/VA, VWAP, 吸收带, 链上周期价)

每个候选位输出 RawCandidate，后续由 confluence_scoring 做聚类打分。

资金仓位维度说明：
  · 原"订单墙"(orderbook_bid/ask_walls) 候选已移除 —— 挂单属"意图"软信号，
    可被 spoof / 撤单，长期实盘会系统性污染支撑/阻力判断
  · 替代为"吸收带"(absorption_zone_*) —— 从 Footprint 派生的已成交硬证据：
    价位级「高成交量 + 买卖接近均衡」的 zone 视为被动承接/压制价位
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from models.flow import CyclePositionData, RangeSignalData


def fmt_usd_cn(usd: float) -> str:
    """将 USD 金额格式化为中文单位（亿/千万/百万/万）。

    正确处理负值（保留符号）和小值（<1万直接显示美元）。
    """
    sign = "-" if usd < 0 else ""
    a = abs(usd)
    if a >= 1e8:
        return f"{sign}{a / 1e8:.1f}亿"
    if a >= 1e7:
        return f"{sign}{a / 1e7:.0f}千万"
    if a >= 1e6:
        return f"{sign}{a / 1e6:.0f}百万"
    if a >= 1e4:
        return f"{sign}{a / 1e4:.0f}万"
    if a >= 1:
        return f"{sign}{a:,.0f}"
    return "0"
from models.liquidation import LiquidationMap
from models.market import CandleData, VolumeProfileData
from models.market_action import AbsorptionSnapshot, FootprintSnapshot
from processors.ta_core import (
    BMSAResult,
    FibLevel,
    IchimokuResult,
    KeltnerResult,
    calc_bmsa,
    calc_ema,
    calc_fibonacci_levels,
    calc_ichimoku,
    calc_keltner,
    calc_pivot_classic,
    calc_sma,
    detect_swings,
    find_major_swing,
    last_valid,
)

logger = logging.getLogger(__name__)


def detect_round_numbers(
    price: float,
    radius_pct: float = 3.0,
) -> list[dict]:
    """检测当前价附近的心理整数关口（散户挂单/止损密集带）。

    输入：
      price: 当前价
      radius_pct: 搜索半径（%），默认 ±3%

    返回 list[dict]，每项含：
      price / side / source / source_tag / base_score

    步长根据币价自适应：
      >= 10000   → 主 1000  / 次 500    (BTC 档)
      >= 1000    → 主 100   / 次 50     (ETH 档)
      >= 100     → 主 10    / 次 5      (SOL 档)
      >= 10      → 主 1     / 次 0.5    (小币 10 档)
      >= 1       → 主 0.1   / 次 None
      < 1        → 主 0.01  / 次 None

    主步长：base_score=15（source_tag='round_major'）
    次步长：base_score=8 （source_tag='round_minor'）

    不对当前价本身打标签（距离 < 步长 5% 的视为已在位内）。
    """
    out: list[dict] = []
    if price <= 0:
        return out

    if price >= 10000:
        major, minor = 1000.0, 500.0
    elif price >= 1000:
        major, minor = 100.0, 50.0
    elif price >= 100:
        major, minor = 10.0, 5.0
    elif price >= 10:
        major, minor = 1.0, 0.5
    elif price >= 1:
        major, minor = 0.1, None  # type: ignore[assignment]
    else:
        major, minor = 0.01, None  # type: ignore[assignment]

    radius = price * radius_pct / 100.0
    low = price - radius
    high = price + radius

    def _emit(step: float, tag: str, score: float, label_prefix: str) -> None:
        # 在 [low, high] 区间内枚举 step 的倍数
        start = int(low // step)
        end = int(high // step) + 1
        for k in range(start, end):
            rn = round(step * k, 6)
            if rn <= 0:
                continue
            # 过滤"与当前价几乎重合"（<0.05 * step）
            if abs(rn - price) < step * 0.05:
                continue
            if rn < low or rn > high:
                continue
            side = "support" if rn < price else "resistance"
            # 格式化价格显示
            if step >= 1:
                pretty = f"${int(rn):,}"
            elif step >= 0.1:
                pretty = f"${rn:.1f}"
            else:
                pretty = f"${rn:.2f}"
            out.append({
                "price": rn, "side": side,
                "source": f"{label_prefix}{pretty}",
                "source_tag": tag,
                "base_score": score,
            })

    _emit(major, "round_major", 15.0, "心理关口")
    if minor is not None:
        # 次级关口：排除与主级重合的
        major_set = {round(c["price"], 6) for c in out}
        before = len(out)
        _emit(minor, "round_minor", 8.0, "心理关口(次)")
        # 去重
        out[before:] = [c for c in out[before:] if round(c["price"], 6) not in major_set]

    return out


def detect_oi_surge_zones(
    oi_history: list[dict],
    candles: list[CandleData] | None,
    threshold_pct: float = 5.0,
) -> list[tuple[float, float]]:
    """检测 OI 骤变区 — OI 在短时间内变化 > threshold_pct 的价格区间。

    Returns: list of (price, oi_change_pct)
    """
    if not oi_history or not candles or len(oi_history) < 3:
        return []
    zones: list[tuple[float, float]] = []
    for i in range(1, len(oi_history)):
        prev_oi = oi_history[i - 1].get("oi", 0)
        curr_oi = oi_history[i].get("oi", 0)
        if prev_oi <= 0:
            continue
        change_pct = (curr_oi - prev_oi) / prev_oi * 100
        if abs(change_pct) >= threshold_pct:
            ts = oi_history[i].get("ts", 0)
            closest_candle = min(candles, key=lambda c: abs(c.ts - ts), default=None)
            if closest_candle:
                price = (closest_candle.high + closest_candle.low) / 2
                zones.append((round(price, 2), round(change_pct, 2)))
    return zones


@dataclass
class RawCandidate:
    """单个候选价位（未合并前）"""
    price: float
    side: str                  # "support" / "resistance"
    dimension: str             # "price_structure" / "math_indicator" / "capital_flow"
    source: str                # 可读来源描述
    source_tag: str            # 机器用标签 (如 "swing_high_4h")
    base_score: float = 0     # 维度内基础评分 (0-50)
    timeframe: str = ""        # "1H"/"4H"/"1D"/"1W"
    data_age_hours: float = 0  # 数据新鲜度（小时）
    # ── M1 新增：簇元数据透传（仅 capital_flow 清算簇填充） ──
    # 设计目的：把 LiqCluster.exchange_count / leverage_intensity / dominant_leverage
    # 透传到 _score_cluster，使共识 multiplier / explain_chips 能正确生成。
    # 设为 dict 而非独立字段：避免 RawCandidate 持续膨胀，扩展性好。
    cluster_meta: dict = field(default_factory=dict)
    # ── M2 新增：独立证据组（GPT V3 评审采纳）──
    # 由 source_tag 在 _score_cluster 阶段映射；非 cluster 候选可在 discover 阶段直接填。
    # 8 组：structure_anchor / macro_technical / local_technical /
    #       liquidation_macro / liquidation_meso / liquidation_short /
    #       microstructure_local / flow_dynamic
    evidence_group: str = ""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# M2 · source_tag → evidence_group 映射（8 组）
# 用法：(1) discover 时 explicit 设置；(2) _score_cluster 末段 fallback 用此 dict
# 设计要点：每个 source_tag 必属唯一组（避免 GPT 担心的"重复计数"）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SOURCE_TAG_TO_EVIDENCE_GROUP: dict[str, str] = {
    # ── structure_anchor: 价格结构锚点（swing/wick/HVN/round_number/onchain）──
    "round_major": "structure_anchor",
    "round_minor": "structure_anchor",
    "unfilled_wick_low": "structure_anchor",
    "unfilled_wick_high": "structure_anchor",
    "hvn": "structure_anchor",
    "swing_high_1h": "structure_anchor",
    "swing_low_1h": "structure_anchor",
    "swing_high_4h": "structure_anchor",
    "swing_low_4h": "structure_anchor",
    "swing_high_1d": "structure_anchor",
    "swing_low_1d": "structure_anchor",
    "swing_high_1w": "structure_anchor",
    "swing_low_1w": "structure_anchor",
    "onchain_real": "structure_anchor",        # CVDD/真实成本
    "onchain_btc-": "structure_anchor",        # BTC 历史均价
    "onchain_etfm": "structure_anchor",        # ETF 平均成本
    # ── macro_technical: 宏观技术指标（200W SMA/CVDD/BMSA/月线 EMA200）──
    "sma_200_1d": "macro_technical",
    "bmsa_upper": "macro_technical",
    "bmsa_lower": "macro_technical",
    "ema_200_1d": "macro_technical",
    # ── local_technical: 本地技术指标（EMA cluster/Fib/Pivot/Ichimoku/VWAP）──
    "ema_50_1d": "local_technical",
    "ema_100_1d": "local_technical",
    "ma_cluster": "local_technical",
    "fib_0.382": "local_technical",
    "fib_0.5": "local_technical",
    "fib_0.618": "local_technical",
    "fib_0.786": "local_technical",
    "pivot_p": "local_technical",
    "pivot_r1": "local_technical",
    "pivot_r2": "local_technical",
    "pivot_s1": "local_technical",
    "pivot_s2": "local_technical",
    "ichimoku_cloud_top": "local_technical",
    "ichimoku_cloud_bottom": "local_technical",
    "ichimoku_kijun": "local_technical",
    "vwap": "local_technical",
    "vp_poc": "local_technical",
    "vp_val": "local_technical",
    "vp_vah": "local_technical",
    # ── liquidation_*: 清算簇按周期分 3 组 ──
    "liq_cluster_below_1d": "liquidation_short",
    "liq_cluster_above_1d": "liquidation_short",
    "liq_cluster_below_7d": "liquidation_meso",
    "liq_cluster_above_7d": "liquidation_meso",
    "liq_cluster_below_30d": "liquidation_macro",
    "liq_cluster_above_30d": "liquidation_macro",
    # ── microstructure_local: 盘口微结构（footprint/absorption）──
    "footprint_stacked": "microstructure_local",
    "absorption_zone_support": "microstructure_local",
    "absorption_zone_resistance": "microstructure_local",
    # ── flow_dynamic: 资金流/持仓动态（OI 突增）──
    "oi_surge_zone": "flow_dynamic",
}


def resolve_evidence_group(source_tag: str) -> str:
    """source_tag → evidence_group 映射（含 swing 前缀通配）。

    Fallback：未映射 source_tag 兜底为 "local_technical"
    （比纯空字符串更有用，避免 count_independent_groups 漏算）
    """
    if source_tag in SOURCE_TAG_TO_EVIDENCE_GROUP:
        return SOURCE_TAG_TO_EVIDENCE_GROUP[source_tag]
    # swing_* 通配（_discover_price_structure 动态生成 swing_<kind>_<tf>）
    if source_tag.startswith("swing_"):
        return "structure_anchor"
    if source_tag.startswith("liq_cluster_"):
        # liq_cluster_<...>_30d / 7d / 1d 兜底
        if source_tag.endswith("_30d"):
            return "liquidation_macro"
        if source_tag.endswith("_7d"):
            return "liquidation_meso"
        return "liquidation_short"
    if source_tag.startswith("fib_") or source_tag.startswith("pivot_"):
        return "local_technical"
    if source_tag.startswith("ema_"):
        return "macro_technical" if "200" in source_tag else "local_technical"
    if source_tag.startswith("onchain_"):
        return "structure_anchor"
    if source_tag.startswith("ichimoku_"):
        return "local_technical"
    return "local_technical"


@dataclass
class DiscoveryResult:
    """Level Discovery 输出"""
    candidates: list[RawCandidate] = field(default_factory=list)
    ichimoku: IchimokuResult | None = None
    bmsa: BMSAResult | None = None
    keltner: KeltnerResult | None = None
    fib_levels: list[FibLevel] = field(default_factory=list)
    fib_swing_high: float = 0
    fib_swing_low: float = 0
    fib_direction: str = ""
    sma200d: float | None = None


def discover_levels(
    current_price: float,
    atr: float,
    candles_1h: list[CandleData] | None = None,
    candles_4h: list[CandleData] | None = None,
    candles_1d: list[CandleData] | None = None,
    candles_1w: list[CandleData] | None = None,
    liq_map: LiquidationMap | None = None,
    liq_map_7d: LiquidationMap | None = None,
    liq_map_30d: LiquidationMap | None = None,
    vp: VolumeProfileData | None = None,
    absorption: AbsorptionSnapshot | None = None,
    footprint_snapshot: FootprintSnapshot | None = None,
    ema_daily: dict[int, float] | None = None,
    sma200_daily: float | None = None,
    boll_data: dict | None = None,
    boll_4h_data: dict | None = None,
    vwap: float = 0,
    cycle_position: CyclePositionData | None = None,
    range_signal: RangeSignalData | None = None,
    oi_history: list[dict] | None = None,
) -> DiscoveryResult:
    """执行三维候选位发现，返回所有候选位 + 辅助数据。"""
    if current_price <= 0:
        return DiscoveryResult()

    result = DiscoveryResult()
    result.sma200d = sma200_daily
    cands = result.candidates

    # ── 维度 A: 价格结构 ──
    _discover_price_structure(
        cands, current_price, atr,
        candles_4h, candles_1d, candles_1w,
        range_signal, vp,
    )

    # ── 维度 B: 数学指标 ──
    _discover_math_indicators(
        cands, result, current_price, atr,
        candles_1d, candles_1w,
        ema_daily, sma200_daily,
        boll_data, boll_4h_data,
    )

    # ── 维度 C: 资金与仓位 ──
    _discover_capital_flow(
        cands, current_price, atr,
        liq_map, liq_map_7d, liq_map_30d,
        vp, absorption, footprint_snapshot, vwap,
        cycle_position,
        oi_history, candles_1h,
    )

    # M2 · 出口统一 fill evidence_group（零侵入：不动 30+ 处 RawCandidate(...) 调用）
    # 设计：根据稳定 source_tag 做映射，map miss 兜底"local_technical"
    for c in cands:
        if not c.evidence_group:
            c.evidence_group = resolve_evidence_group(c.source_tag)

    return result


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 维度 A: 价格结构
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _discover_price_structure(
    cands: list[RawCandidate],
    price: float,
    atr: float,
    candles_4h: list[CandleData] | None,
    candles_1d: list[CandleData] | None,
    candles_1w: list[CandleData] | None,
    range_signal: RangeSignalData | None,
    vp: VolumeProfileData | None,
):
    _add_swing_levels(cands, candles_4h, price, "4H", lookback=5, base_score=25)
    _add_swing_levels(cands, candles_1d, price, "1D", lookback=5, base_score=35)
    _add_swing_levels(cands, candles_1w, price, "1W", lookback=3, base_score=45)

    if range_signal:
        if range_signal.unfilled_wick_low and range_signal.unfilled_wick_low < price:
            cands.append(RawCandidate(
                price=range_signal.unfilled_wick_low, side="support",
                dimension="price_structure", source="未回补日线下影线",
                source_tag="unfilled_wick_low", base_score=30, timeframe="1D",
            ))
        if range_signal.unfilled_wick_high and range_signal.unfilled_wick_high > price:
            cands.append(RawCandidate(
                price=range_signal.unfilled_wick_high, side="resistance",
                dimension="price_structure", source="未回补日线上影线",
                source_tag="unfilled_wick_high", base_score=30, timeframe="1D",
            ))

    if vp and vp.bins:
        sorted_bins = sorted(vp.bins, key=lambda b: b.volume, reverse=True)
        for bin_ in sorted_bins[:3]:
            mid = (bin_.price_low + bin_.price_high) / 2
            if abs(mid - vp.poc_price) / price < 0.003:
                continue
            side = "support" if mid < price else "resistance"
            cands.append(RawCandidate(
                price=round(mid, 2), side=side,
                dimension="price_structure", source=f"成交密集区(HVN)",
                source_tag="hvn", base_score=20, timeframe="1H",
            ))

    # ── 心理整数关口（散户挂单/止损密集区）──
    for rn in detect_round_numbers(price, radius_pct=3.0):
        cands.append(RawCandidate(
            price=rn["price"], side=rn["side"],
            dimension="price_structure",
            source=rn["source"], source_tag=rn["source_tag"],
            base_score=rn["base_score"], timeframe="1D",
        ))


def _add_swing_levels(
    cands: list[RawCandidate],
    candles: list[CandleData] | None,
    price: float,
    tf: str,
    lookback: int,
    base_score: float,
):
    if not candles or len(candles) < 2 * lookback + 1:
        return
    highs = [c.high for c in candles]
    lows = [c.low for c in candles]
    ts_list = [c.ts for c in candles]
    swings = detect_swings(highs, lows, ts_list, lookback)

    is_weekly = tf == "1W"
    max_dist = 25.0 if is_weekly else 15.0
    decay_floor = 0.5 if is_weekly else 0.3

    seen: set[str] = set()
    for sp in reversed(swings):
        side = "resistance" if sp.kind == "high" else "support"
        key = f"{side}_{round(sp.price / price, 3)}"
        if key in seen:
            continue
        seen.add(key)
        dist_pct = abs(sp.price - price) / price * 100
        if dist_pct > max_dist:
            continue
        cands.append(RawCandidate(
            price=round(sp.price, 2), side=side,
            dimension="price_structure",
            source=f"{tf}前{'高' if sp.kind == 'high' else '低'}",
            source_tag=f"swing_{sp.kind}_{tf.lower()}",
            base_score=base_score * max(decay_floor, 1 - dist_pct / max_dist),
            timeframe=tf,
        ))
        if len(seen) >= 8:
            break


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 维度 B: 数学指标
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _discover_math_indicators(
    cands: list[RawCandidate],
    result: DiscoveryResult,
    price: float,
    atr: float,
    candles_1d: list[CandleData] | None,
    candles_1w: list[CandleData] | None,
    ema_daily: dict[int, float] | None,
    sma200_daily: float | None,
    boll_data: dict | None,
    boll_4h_data: dict | None,
):
    # EMA 簇
    ema_daily = ema_daily or {}
    ema_prices = []
    for period, val in sorted(ema_daily.items()):
        if val and val > 0 and 0.001 < abs(val - price) / price < 0.15:
            side = "support" if val < price else "resistance"
            cands.append(RawCandidate(
                price=round(val, 2), side=side,
                dimension="math_indicator", source=f"日线EMA{period}",
                source_tag=f"ema_{period}_1d", base_score=18, timeframe="1D",
            ))
            ema_prices.append(val)

    # 均线共振区检测：多根 EMA 聚集在 ATR 宽度内
    if len(ema_prices) >= 2:
        ema_prices.sort()
        for i in range(len(ema_prices) - 1):
            if abs(ema_prices[i+1] - ema_prices[i]) < atr * 0.5:
                mid = (ema_prices[i] + ema_prices[i+1]) / 2
                side = "support" if mid < price else "resistance"
                cands.append(RawCandidate(
                    price=round(mid, 2), side=side,
                    dimension="math_indicator", source="均线共振区",
                    source_tag="ma_cluster", base_score=30, timeframe="1D",
                ))

    # 200 日 SMA — 多空分界线（特殊标记，高权重）
    if sma200_daily and sma200_daily > 0 and abs(sma200_daily - price) / price < 0.20:
        side = "support" if sma200_daily < price else "resistance"
        cands.append(RawCandidate(
            price=round(sma200_daily, 2), side=side,
            dimension="math_indicator", source="200日SMA(多空分界线)",
            source_tag="sma_200_1d", base_score=40, timeframe="1D",
        ))

    # Bull Market Support Band
    if candles_1w and len(candles_1w) >= 21:
        weekly_closes = [c.close for c in candles_1w]
        bmsa = calc_bmsa(weekly_closes)
        result.bmsa = bmsa
        if bmsa.band_upper and bmsa.band_lower:
            if abs(bmsa.band_upper - price) / price < 0.20:
                side = "support" if bmsa.band_upper < price else "resistance"
                cands.append(RawCandidate(
                    price=round(bmsa.band_upper, 2), side=side,
                    dimension="math_indicator", source="牛市支撑带上沿(20W SMA)",
                    source_tag="bmsa_upper", base_score=35, timeframe="1W",
                ))
            if abs(bmsa.band_lower - price) / price < 0.20:
                side = "support" if bmsa.band_lower < price else "resistance"
                cands.append(RawCandidate(
                    price=round(bmsa.band_lower, 2), side=side,
                    dimension="math_indicator", source="牛市支撑带下沿(21W EMA)",
                    source_tag="bmsa_lower", base_score=35, timeframe="1W",
                ))

    # Fibonacci
    if candles_1d and len(candles_1d) >= 30:
        highs = [c.high for c in candles_1d]
        lows = [c.low for c in candles_1d]
        ts_list = [c.ts for c in candles_1d]
        sh, sl = find_major_swing(highs, lows, ts_list, lookback=10)
        if sh and sl and sh.price > sl.price:
            direction = "up" if sh.index > sl.index else "down"
            fib_levels = calc_fibonacci_levels(sh.price, sl.price, direction)
            result.fib_levels = fib_levels
            result.fib_swing_high = sh.price
            result.fib_swing_low = sl.price
            result.fib_direction = direction
            key_ratios = {0.382, 0.5, 0.618}
            for fl in fib_levels:
                if fl.ratio not in key_ratios:
                    continue
                if fl.price <= 0 or abs(fl.price - price) / price > 0.15:
                    continue
                side = "support" if fl.price < price else "resistance"
                label = "黄金支撑区" if fl.ratio == 0.618 and side == "support" else fl.label
                cands.append(RawCandidate(
                    price=fl.price, side=side,
                    dimension="math_indicator", source=label,
                    source_tag=f"fib_{fl.ratio}", base_score=22, timeframe="1D",
                ))

    # Pivot Points (日线)
    if candles_1d and len(candles_1d) >= 2:
        prev = candles_1d[-2]
        pivot = calc_pivot_classic(prev.high, prev.low, prev.close)
        for label, val in [
            ("Pivot", pivot.pivot), ("Pivot S1", pivot.s1), ("Pivot S2", pivot.s2),
            ("Pivot R1", pivot.r1), ("Pivot R2", pivot.r2),
        ]:
            if val > 0 and abs(val - price) / price < 0.10:
                side = "support" if val < price else "resistance"
                cands.append(RawCandidate(
                    price=round(val, 2), side=side,
                    dimension="math_indicator", source=label,
                    source_tag=f"pivot_{label.lower().replace(' ', '_')}",
                    base_score=15, timeframe="1D",
                ))

    # Ichimoku
    if candles_1d and len(candles_1d) >= 52:
        highs_d = [c.high for c in candles_1d]
        lows_d = [c.low for c in candles_1d]
        closes_d = [c.close for c in candles_1d]
        ichi = calc_ichimoku(highs_d, lows_d, closes_d)
        result.ichimoku = ichi
        if ichi.cloud_top and abs(ichi.cloud_top - price) / price < 0.10:
            side = "support" if ichi.cloud_top < price else "resistance"
            cands.append(RawCandidate(
                price=round(ichi.cloud_top, 2), side=side,
                dimension="math_indicator", source="一目均衡云层上沿",
                source_tag="ichimoku_cloud_top", base_score=20, timeframe="1D",
            ))
        if ichi.cloud_bottom and abs(ichi.cloud_bottom - price) / price < 0.10:
            side = "support" if ichi.cloud_bottom < price else "resistance"
            cands.append(RawCandidate(
                price=round(ichi.cloud_bottom, 2), side=side,
                dimension="math_indicator", source="一目均衡云层下沿",
                source_tag="ichimoku_cloud_bottom", base_score=20, timeframe="1D",
            ))
        if ichi.kijun and abs(ichi.kijun - price) / price < 0.05:
            side = "support" if ichi.kijun < price else "resistance"
            cands.append(RawCandidate(
                price=round(ichi.kijun, 2), side=side,
                dimension="math_indicator", source="一目均衡基准线",
                source_tag="ichimoku_kijun", base_score=15, timeframe="1D",
            ))

    # Bollinger Bands
    if boll_data:
        for key, tag, label in [
            ("upper", "boll_upper_1d", "日线布林上轨"),
            ("lower", "boll_lower_1d", "日线布林下轨"),
        ]:
            val = boll_data.get(key)
            if val and val > 0 and abs(val - price) / price < 0.10:
                side = "support" if val < price else "resistance"
                cands.append(RawCandidate(
                    price=round(val, 2), side=side,
                    dimension="math_indicator", source=label,
                    source_tag=tag, base_score=12, timeframe="1D",
                ))

    # Keltner Channel (从日线 K 线本地算)
    if candles_1d and len(candles_1d) >= 30:
        closes_d = [c.close for c in candles_1d]
        highs_d = [c.high for c in candles_1d]
        lows_d = [c.low for c in candles_1d]
        kc = calc_keltner(closes_d, highs_d, lows_d)
        result.keltner = kc

    # 箱体边界（从 range_signal 继承）
    # 已在价格结构里处理了 unfilled wick；range 的 upper/lower 可作为候选
    # 但避免与 MA120/MA60 重复，这里不重复添加


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 维度 C: 资金与仓位
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _liq_cluster_meta(c) -> dict:
    """提取 LiqCluster 的元数据（透传到 _score_cluster 用）。

    M1 关键：把 cluster.exchange_count / leverage_intensity / dominant_leverage
    挂到 RawCandidate.cluster_meta，使 confluence_scoring 能算共识 multiplier。
    """
    return {
        "exchange_count": int(getattr(c, "exchange_count", 1) or 1),
        "dominant_exchange": getattr(c, "dominant_exchange", "") or "",
        "dominant_leverage": getattr(c, "dominant_leverage", "") or "",
        "leverage_intensity": float(getattr(c, "leverage_intensity", 0.0) or 0.0),
        "total_usd": float(getattr(c, "total_usd", 0) or 0),
    }


def _discover_capital_flow(
    cands: list[RawCandidate],
    price: float,
    atr: float,
    liq_map: LiquidationMap | None,
    liq_map_7d: LiquidationMap | None,
    liq_map_30d: LiquidationMap | None,
    vp: VolumeProfileData | None,
    absorption: AbsorptionSnapshot | None,
    footprint_snapshot: FootprintSnapshot | None,
    vwap: float,
    cycle_position: CyclePositionData | None,
    oi_history: list[dict] | None = None,
    candles_1h: list[CandleData] | None = None,
):
    # 清算簇（24h — 高权重，数据新鲜）
    if liq_map:
        for c in liq_map.clusters_below:
            if c.distance_pct > 15:
                continue
            score = min(c.total_usd / 1e6, 40)
            cands.append(RawCandidate(
                price=c.price_from, side="support",
                dimension="capital_flow",
                source=f"{c.dominant_leverage}x多头清算{fmt_usd_cn(c.total_usd)}",
                source_tag="liq_cluster_below_1d",
                base_score=score, timeframe="1D", data_age_hours=0,
                cluster_meta=_liq_cluster_meta(c),
            ))
        for c in liq_map.clusters_above:
            if c.distance_pct > 15:
                continue
            score = min(c.total_usd / 1e6, 40)
            cands.append(RawCandidate(
                price=c.price_to, side="resistance",
                dimension="capital_flow",
                source=f"{c.dominant_leverage}x空头清算{fmt_usd_cn(c.total_usd)}",
                source_tag="liq_cluster_above_1d",
                base_score=score, timeframe="1D", data_age_hours=0,
                cluster_meta=_liq_cluster_meta(c),
            ))

    # 清算簇（7d — 远距覆盖，较低权重，各方向最多 6 个）
    _MAX_7D_LIQ_PER_SIDE = 6
    if liq_map_7d:
        existing_prices = {round(c.price, -1) for c in cands}
        below_sorted = sorted(
            [c for c in liq_map_7d.clusters_below if 5 <= c.distance_pct <= 20],
            key=lambda c: c.total_usd, reverse=True,
        )
        added_below = 0
        for c in below_sorted:
            if added_below >= _MAX_7D_LIQ_PER_SIDE:
                break
            if round(c.price_from, -1) in existing_prices:
                continue
            cands.append(RawCandidate(
                price=c.price_from, side="support",
                dimension="capital_flow",
                source=f"7d清算簇{fmt_usd_cn(c.total_usd)}",
                source_tag="liq_cluster_below_7d",
                base_score=min(c.total_usd / 1e6, 25) * 0.7,
                timeframe="1D", data_age_hours=72,
                cluster_meta=_liq_cluster_meta(c),
            ))
            added_below += 1
        above_sorted = sorted(
            [c for c in liq_map_7d.clusters_above if 5 <= c.distance_pct <= 20],
            key=lambda c: c.total_usd, reverse=True,
        )
        added_above = 0
        for c in above_sorted:
            if added_above >= _MAX_7D_LIQ_PER_SIDE:
                break
            if round(c.price_to, -1) in existing_prices:
                continue
            cands.append(RawCandidate(
                price=c.price_to, side="resistance",
                dimension="capital_flow",
                source=f"7d清算簇{fmt_usd_cn(c.total_usd)}",
                source_tag="liq_cluster_above_7d",
                base_score=min(c.total_usd / 1e6, 25) * 0.7,
                timeframe="1D", data_age_hours=72,
                cluster_meta=_liq_cluster_meta(c),
            ))
            added_above += 1

    # ── M1 新增：清算簇（30d — 超远距宏观结构位，最低权重） ──────
    # 距离窗 8-30%（不与 1d 0-15% / 7d 5-20% 冲突），各方向最多 4 个，base × 0.5
    # 设计依据：30d 数据反映"长期累积清算筹码"，是潜在的强结构位（如周线、月线锚点），
    # 但因数据老（720h），time_decay 会自然衰减到 0.4×；这里再叠加 base × 0.5 避免污染评分
    _MAX_30D_LIQ_PER_SIDE = 4
    if liq_map_30d:
        existing_prices_2 = {round(c.price, -1) for c in cands}
        below_30d = sorted(
            [c for c in liq_map_30d.clusters_below if 8 <= c.distance_pct <= 30],
            key=lambda c: c.total_usd, reverse=True,
        )
        added_below_30d = 0
        for c in below_30d:
            if added_below_30d >= _MAX_30D_LIQ_PER_SIDE:
                break
            if round(c.price_from, -1) in existing_prices_2:
                continue
            cands.append(RawCandidate(
                price=c.price_from, side="support",
                dimension="capital_flow",
                source=f"30d清算簇{fmt_usd_cn(c.total_usd)}",
                source_tag="liq_cluster_below_30d",
                base_score=min(c.total_usd / 1e6, 20) * 0.5,
                timeframe="1W", data_age_hours=720,
                cluster_meta=_liq_cluster_meta(c),
            ))
            added_below_30d += 1
        above_30d = sorted(
            [c for c in liq_map_30d.clusters_above if 8 <= c.distance_pct <= 30],
            key=lambda c: c.total_usd, reverse=True,
        )
        added_above_30d = 0
        for c in above_30d:
            if added_above_30d >= _MAX_30D_LIQ_PER_SIDE:
                break
            if round(c.price_to, -1) in existing_prices_2:
                continue
            cands.append(RawCandidate(
                price=c.price_to, side="resistance",
                dimension="capital_flow",
                source=f"30d清算簇{fmt_usd_cn(c.total_usd)}",
                source_tag="liq_cluster_above_30d",
                base_score=min(c.total_usd / 1e6, 20) * 0.5,
                timeframe="1W", data_age_hours=720,
                cluster_meta=_liq_cluster_meta(c),
            ))
            added_above_30d += 1

    # Volume Profile POC + VA
    if vp:
        if vp.poc_price > 0 and abs(vp.poc_price - price) / price < 0.10:
            side = "support" if vp.poc_price < price else "resistance"
            cands.append(RawCandidate(
                price=round(vp.poc_price, 2), side=side,
                dimension="capital_flow", source="VP成交量控制点(POC)",
                source_tag="vp_poc", base_score=30, timeframe="1H",
            ))
        if vp.value_area_low > 0 and vp.value_area_low < price:
            cands.append(RawCandidate(
                price=round(vp.value_area_low, 2), side="support",
                dimension="capital_flow", source="VP价值区下沿(VAL)",
                source_tag="vp_val", base_score=20, timeframe="1H",
            ))
        if vp.value_area_high > 0 and vp.value_area_high > price:
            cands.append(RawCandidate(
                price=round(vp.value_area_high, 2), side="resistance",
                dimension="capital_flow", source="VP价值区上沿(VAH)",
                source_tag="vp_vah", base_score=20, timeframe="1H",
            ))

    # VWAP
    if vwap > 0 and abs(vwap - price) / price < 0.10:
        side = "support" if vwap < price else "resistance"
        cands.append(RawCandidate(
            price=round(vwap, 2), side=side,
            dimension="capital_flow", source="日线VWAP",
            source_tag="vwap", base_score=15, timeframe="1D",
        ))

    # 吸收带（替代原订单墙 · 已成交硬证据）
    #   评分逻辑（加分项可叠乘）：
    #   · base = min(taker_volume_usd / 1e6, 30)   —— 每 $1M 贡献 1 分，封顶 30
    #   · bar_count 加成：1→1.0x / 2→1.2x / 3→1.5x （跨 bar 重复越多越可靠）
    #   · delta 纯度：1 - (delta_pct_abs_avg / 0.30)  —— 越接近 0 越纯粹
    #   · age 衰减：max(0.3, 1 - age_hours/3)       —— 3h 前衰减到基准的 30%
    #   · fallback 折扣：detector 用放宽阈值兜底时 × 0.7
    #   每侧最多 5 个（detector 返回时已按 vol 降序），防止污染 cluster 打分
    if absorption:
        def _absorption_score(zone_dict: dict, used_fallback: bool) -> float:
            vol_m = (zone_dict.get("taker_volume_usd") or 0) / 1e6
            base = min(vol_m, 30.0)
            bc = zone_dict.get("bar_count") or 1
            bar_mult = 1.0 if bc <= 1 else (1.2 if bc == 2 else 1.5)
            d_abs = min(zone_dict.get("delta_pct_abs_avg") or 0.30, 0.30)
            purity = max(0.0, 1.0 - d_abs / 0.30)
            age = zone_dict.get("age_hours") or 0.0
            age_factor = max(0.3, 1.0 - age / 3.0)
            fb_factor = 0.7 if used_fallback else 1.0
            return base * bar_mult * purity * age_factor * fb_factor

        absorp_dict = absorption.model_dump() if hasattr(absorption, "model_dump") else absorption
        fb_used = bool(absorp_dict.get("fallback_used"))
        for z in (absorp_dict.get("zones_support") or [])[:5]:
            score = _absorption_score(z, fb_used)
            if score <= 0:
                continue
            # 距离过远的不加入（和其他 capital_flow 候选统一的 15% 范围）
            z_price = z.get("price") or 0
            if z_price <= 0 or abs(z_price - price) / price > 0.15:
                continue
            cands.append(RawCandidate(
                price=round(z_price, 2), side="support",
                dimension="capital_flow",
                source=(
                    f"吸收带{fmt_usd_cn(z.get('taker_volume_usd') or 0)}"
                    f"(bar×{z.get('bar_count')}|Δ{z.get('delta_pct_abs_avg') or 0:.2f})"
                ),
                source_tag="absorption_zone_support",
                base_score=score, timeframe="1H",
                data_age_hours=z.get("age_hours") or 0,
            ))
        for z in (absorp_dict.get("zones_resistance") or [])[:5]:
            score = _absorption_score(z, fb_used)
            if score <= 0:
                continue
            z_price = z.get("price") or 0
            if z_price <= 0 or abs(z_price - price) / price > 0.15:
                continue
            cands.append(RawCandidate(
                price=round(z_price, 2), side="resistance",
                dimension="capital_flow",
                source=(
                    f"吸收带{fmt_usd_cn(z.get('taker_volume_usd') or 0)}"
                    f"(bar×{z.get('bar_count')}|Δ{z.get('delta_pct_abs_avg') or 0:.2f})"
                ),
                source_tag="absorption_zone_resistance",
                base_score=score, timeframe="1H",
                data_age_hours=z.get("age_hours") or 0,
            ))

    # 链上周期价位（BTC 特有）
    if cycle_position:
        onchain_levels = [
            (cycle_position.sma_200w, 40, "200周均线(周期支撑)"),
            (cycle_position.sth_cost_1d, 25, "STH成本(短期盈亏线)"),
            (cycle_position.sth_cost_1w, 20, "STH成本1周"),
            (cycle_position.sth_cost_1m, 18, "STH成本1月"),
            (cycle_position.pi_350dma, 22, "Pi周期350DMA"),
            (cycle_position.cvdd, 35, "CVDD(已销毁币天价值)"),
        ]
        for val, score, source in onchain_levels:
            if not val or val <= 0:
                continue
            dist_pct = abs(val - price) / price * 100
            if dist_pct > 25:
                continue
            side = "support" if val < price else "resistance"
            cands.append(RawCandidate(
                price=round(val, 2), side=side,
                dimension="capital_flow", source=source,
                source_tag=f"onchain_{source[:4].lower()}",
                base_score=score, timeframe="1W",
            ))

    # OI 骤变区
    if oi_history and candles_1h:
        surge_zones = detect_oi_surge_zones(oi_history, candles_1h, threshold_pct=5.0)
        for surge_price, change_pct in surge_zones:
            if abs(surge_price - price) / price > 0.10:
                continue
            if change_pct > 0:
                side = "resistance" if surge_price > price else "support"
                source = f"OI骤增区(+{change_pct:.0f}%)"
            else:
                side = "support" if surge_price < price else "resistance"
                source = f"OI骤减区({change_pct:.0f}%)"
            cands.append(RawCandidate(
                price=surge_price, side=side,
                dimension="capital_flow", source=source,
                source_tag="oi_surge_zone",
                base_score=min(abs(change_pct) * 2, 30), timeframe="1H",
                data_age_hours=0,
            ))

    # ── M1 新增：Footprint Stacked Imbalance 候选 ──────────────────
    # 数据源：footprint_snapshot.contract_latest.top_imbalance_zones（已是该 bar 内
    # 强失衡价位 ratio>3）。stacked_buy → support；stacked_sell → resistance。
    # 设计：
    #   - 仅取 contract（合约更代表杠杆资金行为；spot 走 absorption 通道）
    #   - 取 latest + prev 两根 bar，去重合并（防止单 bar 噪声）
    #   - base_score = min(volume_ratio×8, 22)；ATR 自适应距离过滤（|d| ≤ 5×ATR）
    #   - data_age_hours：latest=0.25h，prev=0.5h（保守按 15min bar）
    #   - V3-P2-2：单 zone 总成交量（buy+sell）必须 ≥ _FOOTPRINT_ZONE_MIN_USD
    #     避免 ratio=15 但绝对成交量极小的"高比例噪声"污染候选
    #     注：footprint_analyzer 已对整 bar 做 low_volume 过滤；此处补单 zone 维度
    _FOOTPRINT_ZONE_MIN_USD = 30_000.0
    if footprint_snapshot is not None and price > 0:
        atr_safe = atr if atr > 0 else price * 0.005
        max_dist = atr_safe * 5  # 5×ATR：约 1-3% 价格范围

        seen_fp_prices: set[float] = set()
        for bar_attr, age_hours in (
            ("contract_latest", 0.25),
            ("contract_prev", 0.50),
        ):
            bar = getattr(footprint_snapshot, bar_attr, None)
            if not bar or not getattr(bar, "top_imbalance_zones", None):
                continue
            for z in bar.top_imbalance_zones[:5]:  # 每 bar 至多 5 个
                z_price = z.get("price") or 0
                z_side_raw = (z.get("side") or "").lower()
                ratio = z.get("ratio") or 0
                if z_price <= 0 or ratio <= 0:
                    continue
                if abs(z_price - price) > max_dist:
                    continue
                # V3-P2-2：单 zone 成交量门槛（绝对量过滤）
                z_volume_usd = float(z.get("buy") or 0) + float(z.get("sell") or 0)
                if z_volume_usd < _FOOTPRINT_ZONE_MIN_USD:
                    continue
                # 去重：同一价格 ±0.3×ATR 视为同一带
                bucket = round(z_price / max(atr_safe * 0.3, 1), 2)
                if bucket in seen_fp_prices:
                    continue
                seen_fp_prices.add(bucket)

                if "buy" in z_side_raw:
                    cand_side = "support"
                    side_label = "买盘失衡"
                elif "sell" in z_side_raw:
                    cand_side = "resistance"
                    side_label = "卖盘失衡"
                else:
                    continue  # 未知 side 跳过

                # 修正：失衡带方向应与价格相对位置一致（防止 stacked_buy 出现在价格
                # 上方造成"上方支撑"语义错误 — 这种情况通常是大单被吃后撤退迹象，归到
                # 反方向作 resistance 更合理）
                if cand_side == "support" and z_price > price:
                    cand_side = "resistance"
                    side_label = "买盘被吸收"
                elif cand_side == "resistance" and z_price < price:
                    cand_side = "support"
                    side_label = "卖盘被吸收"

                cands.append(RawCandidate(
                    price=round(z_price, 2),
                    side=cand_side,
                    dimension="capital_flow",
                    source=f"Footprint{side_label}(×{ratio:.1f})",
                    source_tag="footprint_stacked",
                    base_score=min(ratio * 4, 22),
                    timeframe="1H",
                    data_age_hours=age_hours,
                ))
