"""趋势动能 / 衰竭 / 反转侦测引擎（独立 processor，Phase 1）

职责边界（单一职责 —— 不做以下事情）：
    - 不预测具体顶 / 底价位（只做"续航还是衰竭"的状态分类）。
    - 不做关键位攻防（交给 key_level_tracker_v2）。
    - 不做箱体骨架识别（交给 range_signal）。
    - 不产生交易指令（仅给 action_hint 供上层 AI/Fusion 参考）。

三维打分模型（每子项返回 -1 ~ +1，+ 为支持"趋势续航"，- 为支持"衰竭"）：
    D1 动能（Momentum）
        m1: MACD histogram 二阶导（最近 3 根斜率反向=减速；同向放大=加速）
        m2: 价格离 EMA20 的 σ 偏离（>2σ=过度伸展；<0.5σ 回归=中性）
        m3: RSI 绝对区间（>70 顺势超买/<30 顺势超卖=衰竭倾向；40-60 中性）
    D2 参与度（Participation）
        p1: CVD 斜率 vs 价格斜率（同向=资金在跟；反向=量价背离）
        p2: OI × Price 共振（↑↑=真上涨 / ↑↓=空头回补假涨 / ↓↑=多头强平假跌）
    D3 衰竭触发器（Exhaustion）
        e1: TD Sequential count ≥ 9（buy/sell setup 完成）
        e2: RSI-Price 背离（价新高 RSI 未新高 / 价新低 RSI 未新低）
        e3: Fib 扩展 1.272 / 1.618 命中（预留，依赖 levels 数据；本期用价格接近
            swing 派生的扩展位估算）

MTF 共识规则（只在三周期都计算出后触发）：
    strong_agree : 4h + 1d 状态一致（同 state 或 composite 同号），且 1h 不反向
    partial      : 仅 4h 或 1d 其中之一与主方向一致
    conflict     : 1h 与 4h/1d 主方向相反
    neutral      : 所有周期都是 neutral

防过拟合 / 防噪：
    - 每个子项内部都有"样本不足就置 0"的守门。
    - state_age_min 用 prev_signal 的状态对比，避免一次 flip 反复抖动。
    - data_quality=insufficient 时直接 overall_action=stand_aside，宁可不动，
      也不给假信号（与规则 §9、§10 一致）。
"""

from __future__ import annotations

import logging
import math
import time
from typing import TYPE_CHECKING, Optional

from models.trend_exhaustion import (
    ConsensusLevel,
    ExhaustionState,
    OverallAction,
    SubScore,
    Timeframe,
    TrendExhaustionSignal,
    TrendExhaustionState,
)
from processors.ta_core import calc_ema, calc_macd, calc_rsi, last_valid

if TYPE_CHECKING:
    from engine import CoinState

logger = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 参数阈值（集中放置，便于回测调参）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# 各子项权重（归一化到三维总分）
_W_MOMENTUM = {"m1_macd_2d": 0.45, "m2_ema_dev": 0.30, "m3_rsi_zone": 0.25}
_W_PART = {"p1_cvd_momo": 0.55, "p2_oi_price": 0.45}
_W_EXH = {"e1_td_seq": 0.40, "e2_rsi_div": 0.40, "e3_fib_ext": 0.20}

# 三维合成权重
_W_D1 = 0.40
_W_D2 = 0.30
_W_D3 = 0.30

# state 映射阈值
_TH_HEALTHY = 0.30
_TH_FADING = -0.15
_TH_EXHAUST = -0.40

# 最短样本数要求
_MIN_CANDLES = {"1h": 60, "4h": 50, "1d": 40}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 子项实现：D1 动能
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _m1_macd_second_derivative(closes: list[float]) -> tuple[float, str, Optional[float]]:
    """MACD histogram 最近 3 根的斜率变化（二阶导）。

    趋势方向判断：最近 histogram > 0 视作多方趋势，反之空方。
    - 连续 2 根 histogram 斜率同号且放大 → 加速 → +0.8（续航）
    - 连续 2 根斜率反号 → 减速拐头 → -0.6（衰竭初现）
    - 斜率接近 0 → 动能钝化 → -0.3
    """
    if len(closes) < 40:
        return 0.0, "MACD 样本不足", None
    macd = calc_macd(closes)
    hist = macd["histogram"]
    valid = [h for h in hist if h is not None]
    if len(valid) < 3:
        return 0.0, "MACD histogram 不足 3 点", None
    h1, h2, h3 = valid[-3], valid[-2], valid[-1]
    d1 = h2 - h1
    d2 = h3 - h2
    sign_last = 1 if h3 > 0 else -1
    # 同向且放大 → 加速；反向 → 减速/拐头
    if d1 * d2 > 0 and abs(d2) > abs(d1):
        score = 0.8 * sign_last  # 和 sign 挂钩，让"趋势方向"自然体现
        note = f"MACD 柱加速（{h1:.4g}→{h2:.4g}→{h3:.4g}）"
    elif d1 * d2 < 0:
        score = -0.6 * sign_last
        note = f"MACD 柱拐头（{h1:.4g}→{h2:.4g}→{h3:.4g}）"
    else:
        score = -0.3 * sign_last
        note = "MACD 柱动能钝化"
    return score, note, float(h3)


def _m2_ema_deviation(closes: list[float]) -> tuple[float, str, Optional[float]]:
    """价格偏离 EMA20 的 σ 倍数（离均值过远 = 过度伸展 = 短期衰竭倾向）。

    算法：
        1. 取 EMA20 序列
        2. 用 (close - ema20) / atr_proxy 做标准化（atr_proxy = EMA20 窗口内收益 std）
        3. |z| > 2 → -0.7 伸展
           |z| in [1, 2] → -0.2 注意
           |z| < 1 → +0.3（趋势内常态）
    """
    if len(closes) < 30:
        return 0.0, "EMA20 样本不足", None
    ema_series = calc_ema(closes, 20)
    ema_last = last_valid(ema_series)
    if ema_last is None or ema_last <= 0:
        return 0.0, "EMA20 无效", None
    # 用最近 20 根收益率 std 作为 σ
    window = closes[-21:]
    if len(window) < 21:
        return 0.0, "收益样本不足", None
    rets = [
        (window[i] - window[i - 1]) / window[i - 1]
        for i in range(1, len(window))
        if window[i - 1] > 0
    ]
    if not rets:
        return 0.0, "收益计算失败", None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / max(len(rets) - 1, 1)
    std = math.sqrt(var) if var > 0 else 0.0
    price = closes[-1]
    dev_pct = (price - ema_last) / ema_last if ema_last else 0.0
    z = dev_pct / std if std > 0 else 0.0
    if abs(z) > 2.0:
        score = -0.7 * (1 if z > 0 else -1)  # z>0（高于 EMA 过多）→ 看多衰竭
        note = f"价格离 EMA20 {z:+.1f}σ，过度伸展"
    elif abs(z) > 1.0:
        score = -0.2 * (1 if z > 0 else -1)
        note = f"价格离 EMA20 {z:+.1f}σ，偏高"
    else:
        # 方向跟随：若价格 > EMA 且趋势方向上行就给正分
        score = 0.3 if z > 0 else (-0.3 if z < 0 else 0.0)
        note = f"价格离 EMA20 {z:+.2f}σ，健康区"
    return score, note, float(z)


def _m3_rsi_zone(closes: list[float]) -> tuple[float, str, Optional[float]]:
    """RSI 绝对区间判定（配合趋势方向给分）。"""
    if len(closes) < 20:
        return 0.0, "RSI 样本不足", None
    series = calc_rsi(closes, period=14)
    rsi = last_valid(series)
    if rsi is None:
        return 0.0, "RSI 无效", None
    if rsi >= 75:
        return -0.7, f"RSI={rsi:.1f} 严重超买", float(rsi)
    if rsi >= 65:
        return -0.2, f"RSI={rsi:.1f} 偏高", float(rsi)
    if rsi <= 25:
        return 0.7, f"RSI={rsi:.1f} 严重超卖（反向机会）", float(rsi)
    if rsi <= 35:
        return 0.2, f"RSI={rsi:.1f} 偏低", float(rsi)
    return 0.3, f"RSI={rsi:.1f} 中性健康", float(rsi)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 子项实现：D2 参与度
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _p1_cvd_momentum(
    closes: list[float], cvd_series: list[float]
) -> tuple[float, str, Optional[float]]:
    """CVD 斜率 vs 价格斜率的匹配度。

    取最近 N=10 点做简单线性回归斜率（符号）：
        同符号 → +0.8（资金在跟）
        反符号 → -0.8（量价背离，典型衰竭前兆）
    """
    n = min(len(closes), len(cvd_series), 12)
    if n < 8:
        return 0.0, "CVD/价格样本不足", None
    p_slope = (closes[-1] - closes[-n]) / max(abs(closes[-n]), 1e-9)
    c_slope = cvd_series[-1] - cvd_series[-n]
    if abs(p_slope) < 1e-4 or abs(c_slope) < 1e-4:
        return 0.0, "CVD/价格变化太小", float(c_slope)
    same_sign = (p_slope > 0) == (c_slope > 0)
    if same_sign:
        return 0.8 * (1 if p_slope > 0 else -1), "CVD 与价格同向，资金在跟", float(c_slope)
    return -0.8 * (1 if p_slope > 0 else -1), "CVD 与价格背离，量价不配", float(c_slope)


def _p2_oi_price_alignment(
    price_now: float,
    price_1h_ago: Optional[float],
    oi_change_1h_pct: Optional[float],
) -> tuple[float, str, Optional[float]]:
    """OI-Price 共振模式：
        Price↑ OI↑  → 真多（+0.8）
        Price↑ OI↓  → 空头回补（+0.1，但假涨，警惕）
        Price↓ OI↑  → 真空（-0.8 但方向=空）
        Price↓ OI↓  → 多头止损（假跌）
    """
    if price_1h_ago is None or oi_change_1h_pct is None or price_1h_ago <= 0:
        return 0.0, "OI/价差样本缺失", None
    p_chg = (price_now - price_1h_ago) / price_1h_ago * 100
    if abs(p_chg) < 0.1:
        return 0.0, "价格变化太小", float(oi_change_1h_pct)
    p_up = p_chg > 0
    oi_up = oi_change_1h_pct > 0.1
    oi_down = oi_change_1h_pct < -0.1
    if p_up and oi_up:
        return 0.8, f"涨+加仓（真多 P{p_chg:+.2f}% OI{oi_change_1h_pct:+.2f}%）", float(oi_change_1h_pct)
    if p_up and oi_down:
        return -0.2, f"涨+减仓（空头回补 P{p_chg:+.2f}% OI{oi_change_1h_pct:+.2f}%）", float(oi_change_1h_pct)
    if not p_up and oi_up:
        return -0.8, f"跌+加仓（真空 P{p_chg:+.2f}% OI{oi_change_1h_pct:+.2f}%）", float(oi_change_1h_pct)
    if not p_up and oi_down:
        return 0.2, f"跌+减仓（多头止损 P{p_chg:+.2f}% OI{oi_change_1h_pct:+.2f}%）", float(oi_change_1h_pct)
    return 0.0, f"OI 变化不显著（{oi_change_1h_pct:+.2f}%）", float(oi_change_1h_pct)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 子项实现：D3 衰竭触发器
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _e1_td_sequential(
    td_count: Optional[int], td_direction: str
) -> tuple[float, str, Optional[float]]:
    """TD Sequential：buy setup 9+ 空头衰竭；sell setup 9+ 多头衰竭。"""
    if td_count is None or not td_direction:
        return 0.0, "TD 未就绪", None
    if td_count >= 9:
        if td_direction == "sell":
            return -0.9, f"TD Sell Setup {td_count}（多头衰竭）", float(td_count)
        if td_direction == "buy":
            return 0.9, f"TD Buy Setup {td_count}（空头衰竭，反弹机会）", float(td_count)
    if td_count >= 7:
        sign = -0.4 if td_direction == "sell" else (0.4 if td_direction == "buy" else 0.0)
        return sign, f"TD {td_direction} {td_count}（接近衰竭）", float(td_count)
    return 0.0, f"TD {td_direction or '-'} {td_count}", float(td_count)


def _e2_rsi_price_divergence(
    highs: list[float], lows: list[float], closes: list[float]
) -> tuple[float, str, Optional[float]]:
    """RSI-Price 背离：取最近两个波段（~20 根内）极值比较。

    简化版（Phase 1）：
        看 20 根内：
          最新 15 根价格新高 & RSI 未新高 → 顶背离 -0.8
          最新 15 根价格新低 & RSI 未新低 → 底背离 +0.8
    """
    if len(closes) < 30:
        return 0.0, "背离检测样本不足", None
    rsi_series = calc_rsi(closes, period=14)
    if not rsi_series or rsi_series[-1] is None:
        return 0.0, "RSI 不足以做背离", None

    window = 20
    seg_c = closes[-window:]
    seg_h = highs[-window:]
    seg_l = lows[-window:]
    seg_r = rsi_series[-window:]

    # 分成前后两段找各自的 swing
    mid = window // 2
    prev_hi_idx = max(range(mid), key=lambda i: seg_h[i])
    curr_hi_idx = mid + max(range(window - mid), key=lambda i: seg_h[mid + i])
    prev_lo_idx = max(range(mid), key=lambda i: -seg_l[i])
    curr_lo_idx = mid + max(range(window - mid), key=lambda i: -seg_l[mid + i])

    # 顶背离：后段高点 > 前段高点，但 RSI 反之
    r_prev_hi = seg_r[prev_hi_idx]
    r_curr_hi = seg_r[curr_hi_idx]
    r_prev_lo = seg_r[prev_lo_idx]
    r_curr_lo = seg_r[curr_lo_idx]

    if (seg_h[curr_hi_idx] > seg_h[prev_hi_idx]
            and r_prev_hi is not None and r_curr_hi is not None
            and r_curr_hi < r_prev_hi - 2.0
            and r_curr_hi > 55):  # 高位才算顶背离
        delta = r_curr_hi - r_prev_hi
        return -0.8, f"顶背离（价新高 RSI{delta:+.1f}）", float(delta)

    if (seg_l[curr_lo_idx] < seg_l[prev_lo_idx]
            and r_prev_lo is not None and r_curr_lo is not None
            and r_curr_lo > r_prev_lo + 2.0
            and r_curr_lo < 45):
        delta = r_curr_lo - r_prev_lo
        return 0.8, f"底背离（价新低 RSI{delta:+.1f}）", float(delta)

    # 同时记录隐性背离（趋势中继确认） —— 给较小的正分
    if (seg_h[curr_hi_idx] < seg_h[prev_hi_idx]
            and r_prev_hi is not None and r_curr_hi is not None
            and r_curr_hi > r_prev_hi + 2.0):
        return 0.3, "隐性顶背离（价低高 RSI 高高，多头中继）", float(r_curr_hi - r_prev_hi)

    return 0.0, "无显著背离", None


def _e3_fib_extension_hit(
    highs: list[float], lows: list[float], closes: list[float]
) -> tuple[float, str, Optional[float]]:
    """Fib 扩展 1.272 / 1.618 命中检测（基于最近 swing）。

    简化版：取近 60 根中最近的明显 swing high-low 摆动，
    计算正向 1.272 / 1.618 扩展位，若当前价格在 ±0.5% 内 → 衰竭加分。
    """
    if len(closes) < 50:
        return 0.0, "Fib 样本不足", None
    window = min(60, len(closes))
    seg_h = highs[-window:]
    seg_l = lows[-window:]
    curr_price = closes[-1]

    hi = max(seg_h)
    lo = min(seg_l)
    hi_idx = seg_h.index(hi)
    lo_idx = seg_l.index(lo)
    if abs(hi - lo) < curr_price * 0.01:
        return 0.0, "Swing 幅度太小", None

    # 判断摆动方向：先低后高 = 上行，用 up 扩展；反之用 down 扩展
    if lo_idx < hi_idx:
        # 上行趋势 → 扩展位在 hi 之上
        amplitude = hi - lo
        ext_1272 = hi + amplitude * 0.272
        ext_1618 = hi + amplitude * 0.618
        for ext, label in ((ext_1272, "1.272"), (ext_1618, "1.618")):
            if abs(curr_price - ext) / ext < 0.005:
                return -0.7, f"触及 Fib 扩展 {label}（{ext:.4g}）多头衰竭区", float(ext)
    else:
        # 下行趋势 → 扩展位在 lo 之下
        amplitude = hi - lo
        ext_1272 = lo - amplitude * 0.272
        ext_1618 = lo - amplitude * 0.618
        for ext, label in ((ext_1272, "1.272"), (ext_1618, "1.618")):
            if ext > 0 and abs(curr_price - ext) / ext < 0.005:
                return 0.7, f"触及 Fib 扩展 {label}（{ext:.4g}）空头衰竭区", float(ext)

    return 0.0, "未触及扩展位", None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 单周期聚合
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _weighted(subs: list[SubScore], weights: dict[str, float]) -> float:
    total_w = 0.0
    total_s = 0.0
    for s in subs:
        w = weights.get(s.key, 0.0)
        if w <= 0:
            continue
        total_w += w
        total_s += w * s.score
    return total_s / total_w if total_w > 0 else 0.0


def _compute_single_tf(
    tf: Timeframe,
    candles: list,
    cvd_series: list[float],
    oi_change_1h_pct: Optional[float],
    td_count: Optional[int],
    td_direction: str,
    price_1h_ago: Optional[float],
) -> Optional[TrendExhaustionState]:
    """对单个周期计算 TrendExhaustionState。返回 None 表示样本严重不足。"""
    if not candles or len(candles) < _MIN_CANDLES[tf]:
        return None

    closes = [float(getattr(c, "close", c.get("c", 0.0) if isinstance(c, dict) else 0.0))
              for c in candles]
    highs = [float(getattr(c, "high", c.get("h", 0.0) if isinstance(c, dict) else 0.0))
             for c in candles]
    lows = [float(getattr(c, "low", c.get("l", 0.0) if isinstance(c, dict) else 0.0))
            for c in candles]

    # —— D1 动能 ——
    m1_s, m1_note, m1_val = _m1_macd_second_derivative(closes)
    m2_s, m2_note, m2_val = _m2_ema_deviation(closes)
    m3_s, m3_note, m3_val = _m3_rsi_zone(closes)
    d1_subs = [
        SubScore(key="m1_macd_2d", name="MACD 二阶导", score=m1_s, note=m1_note, value=m1_val),
        SubScore(key="m2_ema_dev", name="EMA20 偏离σ", score=m2_s, note=m2_note, value=m2_val),
        SubScore(key="m3_rsi_zone", name="RSI 区间", score=m3_s, note=m3_note, value=m3_val),
    ]
    momentum_score = _weighted(d1_subs, _W_MOMENTUM)

    # —— D2 参与度（注意：OI/CVD 在非 1h 周期的数据天然偏向 1h，
    #     所以 4h/1d 周期只使用 p1 CVD 动能，不复用 p2 1h 级 OI 变化避免串扰） ——
    p1_s, p1_note, p1_val = _p1_cvd_momentum(closes, cvd_series or [])
    d2_subs = [SubScore(key="p1_cvd_momo", name="CVD 动能", score=p1_s, note=p1_note, value=p1_val)]
    weights_d2 = dict(_W_PART)
    if tf == "1h":
        p2_s, p2_note, p2_val = _p2_oi_price_alignment(
            price_now=closes[-1], price_1h_ago=price_1h_ago, oi_change_1h_pct=oi_change_1h_pct
        )
        d2_subs.append(SubScore(key="p2_oi_price", name="OI×Price 共振", score=p2_s,
                                note=p2_note, value=p2_val))
    else:
        weights_d2 = {"p1_cvd_momo": 1.0}
    participation_score = _weighted(d2_subs, weights_d2)

    # —— D3 衰竭触发器 ——
    # TD 序列只在 1d 最稳（Coinglass 官方 TD count 就是日线），但项目里
    # state.td_sequential_count 字段与周期无关，这里在 1d 上使用它。
    if tf == "1d":
        e1_s, e1_note, e1_val = _e1_td_sequential(td_count, td_direction)
    else:
        e1_s, e1_note, e1_val = 0.0, "TD 仅在日线参与", None
    e2_s, e2_note, e2_val = _e2_rsi_price_divergence(highs, lows, closes)
    e3_s, e3_note, e3_val = _e3_fib_extension_hit(highs, lows, closes)
    d3_subs = [
        SubScore(key="e1_td_seq", name="TD Sequential", score=e1_s, note=e1_note, value=e1_val),
        SubScore(key="e2_rsi_div", name="RSI 背离", score=e2_s, note=e2_note, value=e2_val),
        SubScore(key="e3_fib_ext", name="Fib 扩展命中", score=e3_s, note=e3_note, value=e3_val),
    ]
    exhaustion_score = _weighted(d3_subs, _W_EXH)

    composite = _W_D1 * momentum_score + _W_D2 * participation_score + _W_D3 * exhaustion_score
    composite = max(-1.0, min(1.0, composite))

    # —— state 映射 ——
    triggers_hit = [s.key for s in d3_subs if abs(s.score) >= 0.5]
    if composite >= _TH_HEALTHY:
        state_val: ExhaustionState = "healthy_continuation"
        action = "add"
        reason = f"三维合成 {composite:+.2f}，动能+参与度齐头并进"
    elif composite <= _TH_EXHAUST:
        if len(triggers_hit) >= 2:
            state_val = "exhaustion_warn"
            action = "close"
            reason = f"三维合成 {composite:+.2f}，{len(triggers_hit)} 个衰竭触发器共振"
        else:
            state_val = "momentum_fading"
            action = "reduce"
            reason = f"三维合成 {composite:+.2f}，动能明显减速"
    elif composite <= _TH_FADING:
        state_val = "momentum_fading"
        action = "reduce"
        reason = f"三维合成 {composite:+.2f}，动能减速但衰竭尚未确认"
    else:
        state_val = "neutral"
        action = "hold"
        reason = f"三维合成 {composite:+.2f}，无明显方向"

    return TrendExhaustionState(
        tf=tf,
        momentum_score=round(momentum_score, 3),
        participation_score=round(participation_score, 3),
        exhaustion_score=round(exhaustion_score, 3),
        composite_score=round(composite, 3),
        state=state_val,
        state_age_min=0,  # 由上层根据 prev_signal 填充
        triggers=[s.key for s in (d1_subs + d2_subs + d3_subs) if abs(s.score) >= 0.5],
        sub_scores=d1_subs + d2_subs + d3_subs,
        action_hint=action,
        reason_cn=reason,
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# MTF 共识
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_NEGATIVE_STATES = {"momentum_fading", "exhaustion_warn", "structural_reversal"}
_POSITIVE_STATES = {"healthy_continuation"}


def _resolve_consensus(
    tf_1h: Optional[TrendExhaustionState],
    tf_4h: Optional[TrendExhaustionState],
    tf_1d: Optional[TrendExhaustionState],
) -> tuple[ConsensusLevel, ExhaustionState, OverallAction, float, str]:
    states = [s for s in (tf_1h, tf_4h, tf_1d) if s is not None]
    if not states:
        return "neutral", "neutral", "stand_aside", 0.0, "三周期数据均不足，保持观望"

    # 取 4h + 1d 的合成分作为"主方向判定"（更稳）
    mid_long = [s for s in (tf_4h, tf_1d) if s is not None]
    if not mid_long:
        # 只有 1h，信号弱，降级观望
        s = tf_1h
        assert s is not None
        return (
            "partial",
            s.state,
            "hold" if s.state in _POSITIVE_STATES else ("stand_aside" if s.state == "neutral" else "reduce"),
            0.3 if s.state == "healthy_continuation" else 0.0,
            f"仅 1h 样本足够，{s.reason_cn}",
        )

    ml_avg = sum(s.composite_score for s in mid_long) / len(mid_long)
    ml_negative = sum(1 for s in mid_long if s.state in _NEGATIVE_STATES)
    ml_positive = sum(1 for s in mid_long if s.state in _POSITIVE_STATES)

    # 1h 只做"是否冲突"的校验
    h1_sign = 0
    if tf_1h:
        if tf_1h.composite_score > _TH_HEALTHY:
            h1_sign = 1
        elif tf_1h.composite_score < _TH_FADING:
            h1_sign = -1

    ml_sign = 1 if ml_avg > _TH_HEALTHY else (-1 if ml_avg < _TH_FADING else 0)

    # 共识等级
    if ml_negative == len(mid_long) and h1_sign <= 0:
        consensus: ConsensusLevel = "strong_agree"
        overall_state: ExhaustionState = "exhaustion_warn" if any(
            s.state == "exhaustion_warn" for s in mid_long
        ) else "momentum_fading"
    elif ml_positive == len(mid_long) and h1_sign >= 0:
        consensus = "strong_agree"
        overall_state = "healthy_continuation"
    elif h1_sign != 0 and ml_sign != 0 and h1_sign != ml_sign:
        consensus = "conflict"
        overall_state = "neutral"
    elif ml_sign == 0 and h1_sign == 0:
        consensus = "neutral"
        overall_state = "neutral"
    else:
        consensus = "partial"
        if ml_negative > ml_positive:
            overall_state = "momentum_fading"
        elif ml_positive > ml_negative:
            overall_state = "healthy_continuation"
        else:
            overall_state = "neutral"

    # action + position
    if consensus == "strong_agree" and overall_state == "healthy_continuation":
        action, pos = "add", 0.7
    elif consensus == "strong_agree" and overall_state == "exhaustion_warn":
        action, pos = "close", 0.0
    elif consensus == "strong_agree" and overall_state == "momentum_fading":
        action, pos = "reduce", 0.3
    elif consensus == "partial" and overall_state == "healthy_continuation":
        action, pos = "hold", 0.5
    elif consensus == "partial" and overall_state in ("momentum_fading", "exhaustion_warn"):
        action, pos = "reduce", 0.2
    elif consensus == "conflict":
        action, pos = "stand_aside", 0.0
    else:
        action, pos = "stand_aside", 0.0

    parts = []
    for s in (tf_4h, tf_1d, tf_1h):
        if s:
            parts.append(f"{s.tf}={s.state}({s.composite_score:+.2f})")
    reason = f"共识={consensus}；" + " / ".join(parts)

    return consensus, overall_state, action, round(pos, 2), reason


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 对外主入口
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _extract_cvd_values(cvd_data) -> list[float]:
    if not cvd_data or not getattr(cvd_data, "series", None):
        return []
    return [float(p.cvd) for p in cvd_data.series]


def _resample_1h_to(candles_1h: list, bars_per_unit: int) -> list:
    """把 1h K 线向上聚合为 4h / 1d K 线（合成 candle-like dict）。

    仅在缺少原生 4h / 1d K 线序列时作为回退；当 state.candles_4h / candles_daily
    已存在时应优先使用原生数据。
    """
    if not candles_1h or bars_per_unit <= 1:
        return []
    result: list[dict] = []
    buf: list = []
    for c in candles_1h:
        buf.append(c)
        if len(buf) == bars_per_unit:
            ts = int(getattr(buf[-1], "ts", 0))
            o = float(getattr(buf[0], "open", 0))
            h = max(float(getattr(x, "high", 0)) for x in buf)
            lo = min(float(getattr(x, "low", 0)) for x in buf)
            c_ = float(getattr(buf[-1], "close", 0))
            v = sum(float(getattr(x, "vol", 0)) for x in buf)
            result.append({"ts": ts, "o": o, "h": h, "l": lo, "c": c_, "vol": v})
            buf = []
    return result


def compute_trend_exhaustion(
    state: "CoinState",
    prev_signal: Optional[TrendExhaustionSignal] = None,
) -> Optional[TrendExhaustionSignal]:
    """主入口：基于 CoinState 实时计算多周期衰竭信号。

    防空转：
        - 没有 1h 与 1d candles 时直接返回 data_quality=insufficient 的空信号，
          而非 None，便于前端"样本不足"状态持续可见。
    """
    ts_now = int(time.time())
    missing: list[str] = []

    # 收集各周期 candles
    c_1h = list(state.candles_1h or [])
    c_4h = list(state.candles_4h or [])
    c_1d = list(state.candles_daily or [])
    if not c_4h and c_1h:
        c_4h = _resample_1h_to(c_1h, 4) or c_4h
    if not c_1d and c_1h:
        c_1d = _resample_1h_to(c_1h, 24) or c_1d

    cvd_vals = _extract_cvd_values(state.cvd_contract) or _extract_cvd_values(state.cvd_spot)
    if not cvd_vals:
        missing.append("cvd_series")

    oi_chg_1h = state.oi.change_1h_pct if state.oi else None
    if oi_chg_1h is None:
        missing.append("oi_change_1h")

    # 估算 1 小时前价格
    price_now = state.ticker.last if state.ticker else (c_1h[-1].close if c_1h else 0.0)
    price_1h_ago: Optional[float] = None
    if c_1h:
        price_1h_ago = float(c_1h[-2].close) if len(c_1h) >= 2 else None

    td_count = state.td_sequential_count
    td_dir = state.td_sequential_direction or ""

    tf_1h_state = _compute_single_tf("1h", c_1h, cvd_vals, oi_chg_1h, td_count, td_dir, price_1h_ago)
    tf_4h_state = _compute_single_tf("4h", c_4h, cvd_vals, oi_chg_1h, td_count, td_dir, price_1h_ago)
    tf_1d_state = _compute_single_tf("1d", c_1d, cvd_vals, oi_chg_1h, td_count, td_dir, price_1h_ago)

    consensus, overall_state, action, pos, reason = _resolve_consensus(
        tf_1h_state, tf_4h_state, tf_1d_state
    )

    # state_age：根据 prev_signal 对比，粗粒度分钟级
    def _age(prev: Optional[TrendExhaustionState], curr: Optional[TrendExhaustionState]) -> int:
        if not curr:
            return 0
        if not prev or prev.state != curr.state:
            return 0
        # 沿用上次 age + 增量（按上次 snapshot 到现在的时间差）
        return prev.state_age_min + max(0, (ts_now - (prev_signal.ts if prev_signal else ts_now)) // 60)

    if prev_signal:
        if tf_1h_state:
            tf_1h_state.state_age_min = _age(prev_signal.tf_1h, tf_1h_state)
        if tf_4h_state:
            tf_4h_state.state_age_min = _age(prev_signal.tf_4h, tf_4h_state)
        if tf_1d_state:
            tf_1d_state.state_age_min = _age(prev_signal.tf_1d, tf_1d_state)

    # data_quality 判定
    tf_present = sum(1 for x in (tf_1h_state, tf_4h_state, tf_1d_state) if x is not None)
    if tf_present == 3 and not missing:
        dq = "ok"
    elif tf_present >= 2:
        dq = "partial"
    else:
        dq = "insufficient"

    if dq == "insufficient":
        # 保底输出：前端会看到 overall_action=stand_aside + reason 为何不够
        return TrendExhaustionSignal(
            coin=state.coin,
            ts=ts_now,
            tf_1h=tf_1h_state,
            tf_4h=tf_4h_state,
            tf_1d=tf_1d_state,
            consensus_level="neutral",
            overall_state="neutral",
            overall_action="stand_aside",
            overall_position_pct=0.0,
            overall_reason_cn="样本不足或数据未齐，保持观望",
            data_quality="insufficient",
            missing_inputs=missing,
        )

    return TrendExhaustionSignal(
        coin=state.coin,
        ts=ts_now,
        tf_1h=tf_1h_state,
        tf_4h=tf_4h_state,
        tf_1d=tf_1d_state,
        consensus_level=consensus,
        overall_state=overall_state,
        overall_action=action,
        overall_position_pct=pos,
        overall_reason_cn=reason,
        data_quality=dq,
        missing_inputs=missing,
    )
