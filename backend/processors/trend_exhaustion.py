"""趋势动能 / 衰竭 / 反转侦测引擎（独立 processor，v2 hardened）

职责边界（单一职责 —— 不做以下事情）：
    - 不预测具体顶 / 底价位。
    - 不做关键位攻防（交给 key_level_tracker_v2）。
    - 不做箱体骨架识别（交给 range_signal）。
    - 不产生交易指令（仅给 action_hint 供上层 AI/Fusion 参考）。

v2 相比 v1 的四个关键升级（针对 Phase 1 审计暴露的问题）：
    [1] regime-aware：强趋势 regime 下 RSI>75 不再视为衰竭，而是"强势加速"。
    [2] OI×Price 踩踏识别：区分"涨+OI 跌=空头回补假涨" 与 "涨+OI 大跌=空头踩踏真多头"。
    [3] 信号硬门闸：exhaustion_warn / structural_reversal 需要 **连续 2 tick 确认**
        才会正式发出，避免单次噪声触发 close 动作。
    [4] 震荡/极端 regime 直接 veto 为 stand_aside，避免在假趋势里给方向性结论。

三维打分模型（每子项返回 -1 ~ +1；+ 为支持"趋势续航"，- 为支持"衰竭"；
符号自动相对 direction，即"续航"在下跌趋势中也是正分）：
    D1 动能（Momentum）
        m1: MACD histogram 二阶导（EMA(3) 平滑，反向 2 根才算拐头）
        m2: 价格斜率 z-score（dev_pct / return_std，ctx 修正阈值）
        m3: RSI 区间（regime-aware：趋势期 75 = 健康加速）
        m4: FVG 持续度（未被回补的 3 根 imbalance 数量，顺势+，逆势-）
    D2 参与度（Participation）
        p1: CVD 斜率 × 价格斜率（+ 吸筹 absorption 识别）
        p2: OI×Price 踩踏识别（四象限 × 强度 + 爆仓辅助）
        p3: Coinbase 溢价流向（仅 BTC 启用）
        p4: 资金费率极端（反向指标：多头拥挤 = 衰竭前兆）
    D3 衰竭触发器（Exhaustion）
        e1: TD Sequential count ≥ 9
        e2: RSI-Price 显性/隐性背离
        e3: Fib 扩展 1.272 / 1.618 命中
        e4: 清算簇磁吸（顺势侧近端大簇 = 续航燃料；无簇 = 动能耗尽）

MTF 共识（4h+1d 为主，1h 作冲突检查，带 regime veto 与 2 tick 硬门闸）。
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

from models.trend_exhaustion import (
    ConsensusLevel,
    Direction,
    ExhaustionState,
    OverallAction,
    RegimeLabel,
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
# 阈值（集中放置）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# 默认子项权重（无 context 时的 baseline）
_W_D1_SUBS = {"m1_macd_2d": 0.35, "m2_slope_z": 0.25, "m3_rsi_zone": 0.20, "m4_fvg": 0.20}
_W_D2_SUBS = {"p1_cvd_momo": 0.40, "p2_oi_price": 0.25, "p3_cb_premium": 0.10, "p4_funding": 0.25}
_W_D3_SUBS = {"e1_td_seq": 0.30, "e2_rsi_div": 0.30, "e3_fib_ext": 0.20, "e4_liq_fuel": 0.20}

# 三维合成默认权重
_W_D_DEFAULT = {"D1": 0.40, "D2": 0.30, "D3": 0.30}

# state 阈值
_TH_HEALTHY = 0.30
_TH_FADING = -0.15
_TH_EXHAUST = -0.40

# 最短样本数要求
_MIN_CANDLES = {"1h": 60, "4h": 50, "1d": 40}

# 方向映射：market_structure.direction → 衰竭模块 Direction
_MS_DIR_MAP: dict[str, Direction] = {
    "bullish": "up",
    "bearish": "down",
    "ranging": "flat",
    "transitioning": "flat",
}

# 各 regime 在共识层的"veto 倾向"
_REGIME_VETO: set[str] = {"range", "high_vol_chop", "extreme"}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Context（给子项注入 regime / direction / climax 感知）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dataclass
class _Context:
    tf: Timeframe
    regime: str = "range"               # 来自 state.regime_snapshot.regime
    direction: Direction = "flat"       # 来自 market_structure_1d（衰竭只相对趋势定义）
    atr_pct: float = 0.0                # 当前 ATR%
    atr_pct_pctl: float = 0.5           # 历史百分位（0~1）
    vol_climax: bool = False            # 当根 vol z-score > 2
    trend_age_min: int = 0              # 趋势已持续分钟（预留）
    # 额外 payload
    taker_buy_ratio: Optional[float] = None
    global_liq_long_1h: float = 0.0
    global_liq_short_1h: float = 0.0
    coinbase_premium: Optional[float] = None
    funding_bp_8h: Optional[float] = None   # 单次 8h 资金费率（bp=万分之），+5 = 0.05%
    liq_map: object = None                  # LiquidationMap (1d/3d) 供 e4 读取
    is_btc: bool = False
    dynamic_warnings: list[str] = field(default_factory=list)

    @property
    def is_strong_trend(self) -> bool:
        return self.regime in ("trend_up", "trend_down")

    @property
    def is_veto_regime(self) -> bool:
        return self.regime in _REGIME_VETO

    def trend_sign(self) -> int:
        """续航方向符号：up=+1, down=-1, flat=0。
        所有子项返回的分数 * 这个符号才是"相对方向"的续航/衰竭分。
        """
        return 1 if self.direction == "up" else (-1 if self.direction == "down" else 0)


def _build_context(
    tf: Timeframe,
    coin: str,
    state: "CoinState",
    candles: list,
) -> _Context:
    """从 CoinState 构造 Context。"""
    ctx = _Context(tf=tf, is_btc=(coin.upper() == "BTC"))

    if state.regime_snapshot is not None:
        ctx.regime = state.regime_snapshot.regime or "range"
        ctx.atr_pct = float(getattr(state.regime_snapshot.features, "atr_pct", 0.0) or 0.0)
        ctx.atr_pct_pctl = float(
            getattr(state.regime_snapshot.features, "atr_pct_percentile", 0.0) or 0.0
        )
        # percentile 有时是 0~100 有时 0~1，统一归 0~1
        if ctx.atr_pct_pctl > 1.5:
            ctx.atr_pct_pctl = ctx.atr_pct_pctl / 100.0

    # 主方向复用 1d 结构（缺失则退化到 4h，再缺失留 flat）
    if state.market_structure_1d is not None:
        ctx.direction = _MS_DIR_MAP.get(state.market_structure_1d.direction, "flat")
    elif getattr(state, "market_structure", None) is not None:
        ctx.direction = _MS_DIR_MAP.get(state.market_structure.direction, "flat")

    # vol climax：当前 bar vol 相对最近 40 根的 z-score > 2
    if candles and len(candles) >= 30:
        vols = [float(getattr(c, "vol", 0) or 0) for c in candles[-40:]]
        vols = [v for v in vols if v > 0]
        if len(vols) >= 10:
            m = sum(vols) / len(vols)
            var = sum((v - m) ** 2 for v in vols) / max(len(vols) - 1, 1)
            sd = math.sqrt(var) if var > 0 else 0.0
            if sd > 0:
                z = (vols[-1] - m) / sd
                ctx.vol_climax = z > 2.0

    if state.taker_flow is not None:
        ctx.taker_buy_ratio = float(state.taker_flow.buy_ratio or 0.0)

    if state.global_liq is not None:
        ctx.global_liq_long_1h = float(state.global_liq.long_1h_usd or 0.0)
        ctx.global_liq_short_1h = float(state.global_liq.short_1h_usd or 0.0)

    if ctx.is_btc and state.coinbase_premium is not None:
        ctx.coinbase_premium = float(state.coinbase_premium.current_premium or 0.0)

    # 资金费率：优先 multi_funding.oi_weighted（多所 OI 加权），退化到 funding.oi_weighted_rate
    #   原始单位为"小数"（如 0.0005 = 0.05% per 8h），乘 10000 得 bp
    fr_raw: Optional[float] = None
    mf = getattr(state, "multi_funding", None)
    funding = getattr(state, "funding", None)
    if mf is not None and getattr(mf, "oi_weighted", 0) not in (0, None):
        fr_raw = float(mf.oi_weighted)
    elif mf is not None and getattr(mf, "avg_current", 0) not in (0, None):
        fr_raw = float(mf.avg_current)
    elif funding is not None:
        fr_raw = float(
            getattr(funding, "oi_weighted_rate", 0)
            or getattr(funding, "avg_rate", 0)
            or 0.0
        )
    if fr_raw is not None and fr_raw != 0:
        ctx.funding_bp_8h = fr_raw * 10000.0  # 0.0005 → 5 bp

    # 清算地图：优先取 1d，否则 3d / 7d
    lmaps = getattr(state, "liq_maps", None) or {}
    ctx.liq_map = lmaps.get("1d") or lmaps.get("24h") or lmaps.get("3d") or lmaps.get("7d")

    return ctx


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 小工具
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _sign(x: float) -> int:
    return 1 if x > 0 else (-1 if x < 0 else 0)


def _signed(score: float, ctx: _Context) -> float:
    """把"支持续航 = +score"相对无方向的原始读数，映射到带方向的分数。

    规则：
      - direction=up 且原始读数看多 → +score（续航）
      - direction=up 且原始读数看空 → -score（衰竭）
      - direction=down 相反
      - direction=flat → 返回 0（无法定义续航，让上层 veto 处理）

    原函数约定：raw_score 的正值 = 支持"价格向上继续"，负值 = 支持"价格向下或反转"。
    """
    if ctx.direction == "flat":
        return 0.0
    if ctx.direction == "up":
        return score
    return -score  # down


def _ema3(values: list[float]) -> list[float]:
    """长度相同的 EMA(3) 平滑（首 2 根用可得前缀均值回填）。"""
    if not values:
        return []
    k = 2 / (3 + 1)
    out: list[float] = []
    ema_prev: Optional[float] = None
    for v in values:
        if ema_prev is None:
            ema_prev = v
        else:
            ema_prev = v * k + ema_prev * (1 - k)
        out.append(ema_prev)
    return out


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# D1 动能子项
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _m1_macd_hist_accel(closes: list[float], ctx: _Context) -> tuple[float, str, Optional[float]]:
    """MACD histogram 二阶导（v2：EMA(3) 平滑 + 连续 2 根反向才算拐头）。

    返回："相对价格继续向上"视角的 raw_score（由 _signed 映射到方向相对分）。
    """
    if len(closes) < 40:
        return 0.0, "MACD 样本不足", None
    macd = calc_macd(closes)
    hist_raw = [h for h in macd["histogram"] if h is not None]
    if len(hist_raw) < 6:
        return 0.0, "MACD histogram 不足 6 点", None
    hist = _ema3(hist_raw[-8:])  # 平滑最近 8 根
    if len(hist) < 4:
        return 0.0, "平滑 histogram 不足", None
    h0, h1, h2, h3 = hist[-4], hist[-3], hist[-2], hist[-1]
    d1 = h2 - h1
    d2 = h3 - h2
    d_prev = h1 - h0
    sign_last = 1 if h3 > 0 else -1
    # 加速：最近 2 根斜率同号 + |d2|>|d1| + sign 与 histogram 同号
    if d1 * d2 > 0 and abs(d2) > abs(d1) and _sign(d2) == sign_last:
        raw = 0.8 * sign_last
        note = f"MACD 柱加速（平滑 {h1:.4g}→{h2:.4g}→{h3:.4g}）"
    # 拐头：d_prev 与 d2 方向相反 且 d1/d2 也反向 → 确认拐头
    elif d_prev * d2 < 0 and d1 * d2 <= 0:
        raw = -0.6 * sign_last
        note = f"MACD 柱拐头确认（斜率 {d_prev:+.3g}→{d2:+.3g}）"
    # 钝化：斜率绝对值很小
    elif abs(d2) < abs(h3) * 0.1:
        raw = -0.25 * sign_last
        note = "MACD 柱动能钝化"
    else:
        raw = 0.1 * sign_last
        note = f"MACD 柱缓慢推进（hist={h3:+.4g}）"
    return _signed(raw, ctx), note, float(h3)


def _m2_price_slope_zscore(closes: list[float], ctx: _Context) -> tuple[float, str, Optional[float]]:
    """价格相对 EMA20 的 σ 偏离 + dynamic thresholds + climax 共振。

    强趋势 regime 下允许的 |z| 更大（3σ 才算伸展），且仅在 vol_climax 共振时才给负分。
    """
    if len(closes) < 30:
        return 0.0, "EMA20 样本不足", None
    ema_series = calc_ema(closes, 20)
    ema_last = last_valid(ema_series)
    if ema_last is None or ema_last <= 0:
        return 0.0, "EMA20 无效", None
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
    dev_pct = (closes[-1] - ema_last) / ema_last if ema_last else 0.0
    z = dev_pct / std if std > 0 else 0.0

    # 动态阈值：强趋势期放宽
    th_exhaust = 3.0 if ctx.is_strong_trend else 2.0
    th_mild = 2.0 if ctx.is_strong_trend else 1.0

    z_abs = abs(z)
    if z_abs >= th_exhaust:
        # 必须共振 climax 才给衰竭分（否则只是"强势延伸"）
        if ctx.vol_climax:
            raw = -0.7 * _sign(z)
            note = f"价格离 EMA20 {z:+.1f}σ + 放量 climax → 抛压/踩踏伸展"
        else:
            raw = 0.2 * _sign(z)
            note = f"价格离 EMA20 {z:+.1f}σ（强趋势无放量，归为延伸）"
    elif z_abs >= th_mild:
        raw = 0.35 * _sign(z) if ctx.is_strong_trend else -0.15 * _sign(z)
        note = f"价格离 EMA20 {z:+.1f}σ（{'健康延伸' if ctx.is_strong_trend else '轻度过度'}）"
    else:
        raw = 0.3 * _sign(z) if z_abs > 0.2 else 0.0
        note = f"价格离 EMA20 {z:+.2f}σ，健康区"
    return _signed(raw, ctx), note, float(z)


def _m3_rsi_zone(closes: list[float], ctx: _Context) -> tuple[float, str, Optional[float]]:
    """RSI 区间（regime-aware）。

    - 强趋势：RSI 75-85 视为"强势加速"（正分），>88 才算严重超买（轻微负分）
    - 非趋势：RSI >= 75 → 超买衰竭（常规）
    - 对称用于空头趋势 + RSI 低位
    """
    if len(closes) < 20:
        return 0.0, "RSI 样本不足", None
    series = calc_rsi(closes, period=14)
    rsi = last_valid(series)
    if rsi is None:
        return 0.0, "RSI 无效", None

    # raw_score 的"正向"含义：支持价格继续向上
    # 强趋势 → 高 RSI 视为强势
    strong = ctx.is_strong_trend
    if rsi >= 88:
        raw = -0.5 if not strong else -0.2
        note = f"RSI={rsi:.1f} 极度超买"
    elif rsi >= 75:
        raw = 0.4 if strong else -0.6
        note = f"RSI={rsi:.1f} " + ("强势加速" if strong else "超买区")
    elif rsi >= 65:
        raw = 0.3 if strong else -0.1
        note = f"RSI={rsi:.1f} 偏强"
    elif rsi <= 12:
        raw = 0.5 if not strong else 0.2
        note = f"RSI={rsi:.1f} 极度超卖（反弹机会）"
    elif rsi <= 25:
        raw = -0.4 if strong else 0.6
        note = f"RSI={rsi:.1f} " + ("空头强势" if strong else "超卖反弹区")
    elif rsi <= 35:
        raw = -0.3 if strong else 0.1
        note = f"RSI={rsi:.1f} 偏弱"
    else:
        raw = 0.1 if rsi >= 50 else -0.1
        note = f"RSI={rsi:.1f} 中性"
    return _signed(raw, ctx), note, float(rsi)


def _m4_fvg_persistence(
    highs: list[float], lows: list[float], closes: list[float], ctx: _Context,
) -> tuple[float, str, Optional[float]]:
    """FVG（Fair Value Gap）未被回补计数：顺势 FVG 越多越健康。

    FVG 定义（3 根蜡烛）：
        上涨：bar[i-2].high < bar[i].low（bar[i-1] 形成 gap）
        下跌：bar[i-2].low  > bar[i].high
    "未被回补"：随后 N=10 根内最低价未触及 gap 上沿（或最高价未触及下沿）。
    """
    n = len(closes)
    if n < 30:
        return 0.0, "FVG 样本不足", None
    up_count = 0
    down_count = 0
    window_start = max(2, n - 40)
    for i in range(window_start, n):
        # 上涨 FVG
        if highs[i - 2] < lows[i]:
            gap_top = lows[i]
            # 检查之后 1..min(10,n-1-i) 根是否被回补
            refilled = False
            for j in range(i + 1, min(i + 11, n)):
                if lows[j] < gap_top:
                    refilled = True
                    break
            if not refilled:
                up_count += 1
        # 下跌 FVG
        if lows[i - 2] > highs[i]:
            gap_bot = highs[i]
            refilled = False
            for j in range(i + 1, min(i + 11, n)):
                if highs[j] > gap_bot:
                    refilled = True
                    break
            if not refilled:
                down_count += 1

    # raw_score：正向支持"继续向上"。顺向 FVG 多 = 续航强。
    net = up_count - down_count
    if abs(net) >= 3:
        raw = 0.6 * _sign(net)
        note = f"FVG 未回补 多{up_count}/空{down_count}，顺势结构强"
    elif abs(net) >= 1:
        raw = 0.25 * _sign(net)
        note = f"FVG 未回补 多{up_count}/空{down_count}"
    else:
        raw = 0.0
        note = f"FVG 未回补 多{up_count}/空{down_count}，中性"
    return _signed(raw, ctx), note, float(net)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# D2 参与度子项
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _p1_cvd_momentum(
    closes: list[float], cvd_series: list[float], ctx: _Context,
) -> tuple[float, str, Optional[float]]:
    """CVD 动能 + absorption（吸筹）检测。

    - 同向放大 → 资金在跟（+0.8）
    - 反向 → 量价背离（-0.8）
    - 价创新高但 CVD 斜率绝对值在下降 → 吸筹 absorption（-0.4，衰竭早期）
    """
    n = min(len(closes), len(cvd_series), 16)
    if n < 10:
        return 0.0, "CVD/价格样本不足", None
    p_slope = (closes[-1] - closes[-n]) / max(abs(closes[-n]), 1e-9)
    c_slope = cvd_series[-1] - cvd_series[-n]
    if abs(p_slope) < 5e-5 or abs(c_slope) < 1e-4:
        return 0.0, "CVD/价格变化太小", float(c_slope)

    same_sign = (p_slope > 0) == (c_slope > 0)
    if not same_sign:
        raw = -0.8 * _sign(p_slope)
        note = "CVD 与价格背离，量价不配"
        return _signed(raw, ctx), note, float(c_slope)

    # absorption：价格做高 but CVD 增速减缓（后半段 < 前半段）
    half = n // 2
    c_first = cvd_series[-n + half] - cvd_series[-n]
    c_second = cvd_series[-1] - cvd_series[-n + half]
    p_first = closes[-n + half] - closes[-n]
    p_second = closes[-1] - closes[-n + half]
    # 同向前提下，价格二阶段加速但 CVD 二阶段减速 → absorption
    if (
        p_first * p_second > 0
        and abs(p_second) >= abs(p_first) * 0.8
        and abs(c_second) < abs(c_first) * 0.5
    ):
        raw = -0.4 * _sign(p_slope)
        note = "CVD 吸筹：价新高/低但量能减半，被动承接"
        return _signed(raw, ctx), note, float(c_slope)

    raw = 0.8 * _sign(p_slope)
    note = "CVD 与价格同向，资金在跟"
    return _signed(raw, ctx), note, float(c_slope)


def _p2_oi_price_confluence(
    price_now: float,
    price_1h_ago: Optional[float],
    oi_change_1h_pct: Optional[float],
    ctx: _Context,
) -> tuple[float, str, Optional[float]]:
    """OI×Price 共振（v2：带踩踏识别）。

    涨+OI↑  → 真多：+0.8
    涨+OI↓(<-2%) → 空头踩踏：爆仓驱动真多头，+0.6；但需 global_liq.short_1h 放量
             否则只是空头减仓回补 → 0
    涨+OI↓(-0.2%~-2%) → 空头温和回补，假涨 → -0.3
    跌+OI↑  → 真空：-0.8
    跌+OI↓(<-2%) → 多头踩踏：爆仓驱动真空头，+0.6 反向（即 -0.6 相对向上视角）
    跌+OI↓(-0.2%~-2%) → 多头止损假跌 → +0.3
    """
    if price_1h_ago is None or oi_change_1h_pct is None or price_1h_ago <= 0:
        return 0.0, "OI/价差样本缺失", None
    p_chg = (price_now - price_1h_ago) / price_1h_ago * 100
    if abs(p_chg) < 0.1:
        return 0.0, "价格变化太小", float(oi_change_1h_pct)
    p_up = p_chg > 0
    # OI 变化阈值
    oi = oi_change_1h_pct
    big_down = oi < -2.0
    mild_down = -2.0 <= oi < -0.2
    up = oi > 0.2

    # 爆仓辅助：全网 1h 清算金额 > 3000 万美元视作显著踩踏（BTC 级别）
    short_liq_big = ctx.global_liq_short_1h > 3e7
    long_liq_big = ctx.global_liq_long_1h > 3e7

    if p_up and up:
        raw = 0.8
        note = f"涨+加仓（真多 P{p_chg:+.2f}% OI{oi:+.2f}%）"
    elif p_up and big_down and short_liq_big:
        raw = 0.6
        note = f"涨+OI 大跌+空头爆仓 {ctx.global_liq_short_1h/1e6:.0f}M（踩踏真多）"
    elif p_up and big_down:
        raw = 0.2
        note = f"涨+OI 大跌（空头大额减仓 P{p_chg:+.2f}% OI{oi:+.2f}%）"
    elif p_up and mild_down:
        raw = -0.3
        note = f"涨+OI 温和减（空头回补假涨 P{p_chg:+.2f}% OI{oi:+.2f}%）"
    elif (not p_up) and up:
        raw = -0.8
        note = f"跌+加仓（真空 P{p_chg:+.2f}% OI{oi:+.2f}%）"
    elif (not p_up) and big_down and long_liq_big:
        raw = -0.6
        note = f"跌+OI 大跌+多头爆仓 {ctx.global_liq_long_1h/1e6:.0f}M（踩踏真空）"
    elif (not p_up) and big_down:
        raw = -0.2
        note = f"跌+OI 大跌（多头大额止损 P{p_chg:+.2f}% OI{oi:+.2f}%）"
    elif (not p_up) and mild_down:
        raw = 0.3
        note = f"跌+OI 温和减（多头止损假跌 P{p_chg:+.2f}% OI{oi:+.2f}%）"
    else:
        raw = 0.0
        note = f"OI 变化不显著（{oi:+.2f}%）"
    return _signed(raw, ctx), note, float(oi_change_1h_pct)


def _p3_coinbase_premium_flow(ctx: _Context) -> tuple[float, str, Optional[float]]:
    """Coinbase 溢价流向（仅 BTC）。

    premium > 0 表明美国机构买盘强势；< 0 表明亚洲/现货抛压。
    """
    if not ctx.is_btc or ctx.coinbase_premium is None:
        return 0.0, "非 BTC 或 CB 溢价缺失", None
    cb = ctx.coinbase_premium
    if abs(cb) < 5:
        return 0.0, f"CB 溢价 {cb:+.1f} 中性", float(cb)
    if cb >= 30:
        raw = 0.7
        note = f"CB 溢价 +{cb:.0f}（美机构强烈买入）"
    elif cb >= 5:
        raw = 0.35
        note = f"CB 溢价 +{cb:.0f}（美机构买盘）"
    elif cb <= -30:
        raw = -0.7
        note = f"CB 溢价 {cb:.0f}（美机构强烈抛售）"
    else:
        raw = -0.35
        note = f"CB 溢价 {cb:.0f}（美机构抛盘）"
    return _signed(raw, ctx), note, float(cb)


def _p4_funding_extreme(ctx: _Context) -> tuple[float, str, Optional[float]]:
    """资金费率极端（反向指标）。

    语义：
      - 极端正费率（多头付空头）= 多头过度拥挤 = 对价格继续向上是负面（反向）
      - 极端负费率 = 空头过度拥挤 = 对价格向上是正面
      - 强趋势期阈值放宽（趋势中极端费率可维持数周）
    阈值（单位 bp，8h 周期；年化 = bp × 3 × 365 / 100 ≈ bp × 11 %/年）：
        +20 bp 极端拥挤 / +10 bp 拥挤 / +5 bp 偏拥挤 / 0 中性 / -5 / -10 / -20
    """
    if ctx.funding_bp_8h is None:
        return 0.0, "资金费率缺失", None
    fr = ctx.funding_bp_8h

    # raw_score 正向 = 支持向上继续。高正费率 → raw 负。
    if fr >= 20:
        raw, note = -0.8, f"资金费率 {fr:+.1f}bp 极端多头拥挤（年化 ~{fr*11:.0f}%）"
    elif fr >= 10:
        raw, note = -0.5, f"资金费率 {fr:+.1f}bp 多头拥挤"
    elif fr >= 5:
        raw, note = -0.2, f"资金费率 {fr:+.1f}bp 多头偏拥挤"
    elif fr <= -20:
        raw, note = 0.8, f"资金费率 {fr:+.1f}bp 极端空头拥挤（反向机会）"
    elif fr <= -10:
        raw, note = 0.5, f"资金费率 {fr:+.1f}bp 空头拥挤"
    elif fr <= -5:
        raw, note = 0.2, f"资金费率 {fr:+.1f}bp 空头偏拥挤"
    else:
        raw, note = 0.0, f"资金费率 {fr:+.1f}bp 中性"

    # 强趋势期：极端费率可维持很久，降权避免过早反转判定
    if ctx.is_strong_trend and abs(raw) > 0:
        raw *= 0.5
        note += "（趋势期降权）"

    return _signed(raw, ctx), note, float(fr)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# D3 衰竭触发器
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _e1_td_sequential(
    td_count: Optional[int], td_direction: str, ctx: _Context,
) -> tuple[float, str, Optional[float]]:
    """TD Sequential ≥ 9 = 多/空头衰竭。"""
    if td_count is None or not td_direction:
        return 0.0, "TD 未就绪", None
    # raw 为"相对向上"语义的原始分
    if td_count >= 9:
        if td_direction == "sell":
            raw = -0.9
            note = f"TD Sell Setup {td_count}（多头衰竭）"
        elif td_direction == "buy":
            raw = 0.9
            note = f"TD Buy Setup {td_count}（空头衰竭）"
        else:
            raw = 0.0
            note = f"TD {td_direction} {td_count}"
    elif td_count >= 7:
        raw = -0.4 if td_direction == "sell" else (0.4 if td_direction == "buy" else 0.0)
        note = f"TD {td_direction} {td_count}（接近衰竭）"
    else:
        raw = 0.0
        note = f"TD {td_direction} {td_count}"
    return _signed(raw, ctx), note, float(td_count)


def _e2_rsi_price_divergence(
    highs: list[float], lows: list[float], closes: list[float], ctx: _Context,
) -> tuple[float, str, Optional[float]]:
    """RSI-Price 背离。要求 RSI 在极端区才算"顶/底"背离。"""
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
    mid = window // 2

    prev_hi_idx = max(range(mid), key=lambda i: seg_h[i])
    curr_hi_idx = mid + max(range(window - mid), key=lambda i: seg_h[mid + i])
    prev_lo_idx = max(range(mid), key=lambda i: -seg_l[i])
    curr_lo_idx = mid + max(range(window - mid), key=lambda i: -seg_l[mid + i])
    r_prev_hi = seg_r[prev_hi_idx]
    r_curr_hi = seg_r[curr_hi_idx]
    r_prev_lo = seg_r[prev_lo_idx]
    r_curr_lo = seg_r[curr_lo_idx]

    # 顶背离（raw_score 负 = 衰竭）
    if (
        seg_h[curr_hi_idx] > seg_h[prev_hi_idx]
        and r_prev_hi is not None and r_curr_hi is not None
        and r_curr_hi < r_prev_hi - 3.0
        and r_curr_hi > 60  # 高位才算顶背离
    ):
        delta = r_curr_hi - r_prev_hi
        return _signed(-0.8, ctx), f"顶背离（价新高 RSI{delta:+.1f}）", float(delta)

    if (
        seg_l[curr_lo_idx] < seg_l[prev_lo_idx]
        and r_prev_lo is not None and r_curr_lo is not None
        and r_curr_lo > r_prev_lo + 3.0
        and r_curr_lo < 40
    ):
        delta = r_curr_lo - r_prev_lo
        return _signed(0.8, ctx), f"底背离（价新低 RSI{delta:+.1f}）", float(delta)

    return 0.0, "无显著背离", None


def _e3_fib_extension_hit(
    highs: list[float], lows: list[float], closes: list[float], ctx: _Context,
) -> tuple[float, str, Optional[float]]:
    """Fib 扩展 1.272/1.618 命中。"""
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

    if lo_idx < hi_idx:
        amplitude = hi - lo
        ext_1272 = hi + amplitude * 0.272
        ext_1618 = hi + amplitude * 0.618
        for ext, label in ((ext_1272, "1.272"), (ext_1618, "1.618")):
            if abs(curr_price - ext) / ext < 0.005:
                return _signed(-0.7, ctx), f"触及 Fib 扩展 {label}（{ext:.4g}）多头衰竭区", float(ext)
    else:
        amplitude = hi - lo
        ext_1272 = lo - amplitude * 0.272
        ext_1618 = lo - amplitude * 0.618
        for ext, label in ((ext_1272, "1.272"), (ext_1618, "1.618")):
            if ext > 0 and abs(curr_price - ext) / ext < 0.005:
                return _signed(0.7, ctx), f"触及 Fib 扩展 {label}（{ext:.4g}）空头衰竭区", float(ext)
    return 0.0, "未触及扩展位", None


def _e4_liq_cluster_fuel(
    price: float, ctx: _Context,
) -> tuple[float, str, Optional[float]]:
    """清算簇磁吸/燃料余量（基于 LiquidationMap.clusters_above/below）。

    逻辑（与币种无关，全用相对占比）：
        1. 取顺势方向侧（up → clusters_above 空头爆仓簇；down → clusters_below 多头爆仓簇）
        2. 该侧 10% 内总量 vs 整侧总量 = near_ratio
        3. 找近端最大单簇占该侧比重 = top_ratio
        4. 打分：
             near_ratio≥0.30 且 top_ratio≥0.20（近端有大磁铁）→ +0.5（续航燃料充足）
             near_ratio<0.10（近端已被洗过）→ -0.4（燃料耗尽，动能衰竭）
             其他中间值 → 线性映射
    """
    liq_map = ctx.liq_map
    if liq_map is None or price <= 0 or ctx.direction == "flat":
        return 0.0, "清算地图/方向缺失", None

    use_above = ctx.direction == "up"
    clusters = list(getattr(liq_map, "clusters_above", []) or []) if use_above \
        else list(getattr(liq_map, "clusters_below", []) or [])
    if not clusters:
        return 0.0, "顺势侧无清算簇", None

    total_side = sum(float(c.total_usd or 0) for c in clusters)
    if total_side <= 0:
        return 0.0, "顺势侧簇总量为 0", None

    def _dpct(c) -> float:
        dp = getattr(c, "distance_pct", None)
        if dp is not None and dp > 0:
            return float(dp)
        center = float(getattr(c, "price_center", 0) or 0)
        if center <= 0:
            return 999.0
        return abs(center - price) / price * 100.0

    near = [c for c in clusters if _dpct(c) <= 10.0]
    near_total = sum(float(c.total_usd or 0) for c in near)
    near_ratio = near_total / total_side if total_side > 0 else 0.0
    top_usd = max((float(c.total_usd or 0) for c in near), default=0.0)
    top_ratio = top_usd / total_side if total_side > 0 else 0.0

    # 先算"支持续航"的绝对强度，再按 direction 映射到"支持向上"语义（_signed 期望）
    if near_ratio >= 0.30 and top_ratio >= 0.20:
        abs_strength = 0.5
        note = (
            f"顺势 10% 内簇占 {near_ratio*100:.0f}%，最大簇 {top_usd/1e6:.0f}M"
            f"（{top_ratio*100:.0f}%）→ 磁吸续航"
        )
    elif near_ratio < 0.10:
        abs_strength = -0.4
        note = f"顺势 10% 内簇仅占 {near_ratio*100:.0f}%，燃料耗尽"
    elif near_ratio >= 0.20:
        abs_strength = 0.25
        note = f"顺势 10% 内簇占 {near_ratio*100:.0f}%（一般）"
    else:
        abs_strength = -0.15
        note = f"顺势 10% 内簇占 {near_ratio*100:.0f}%（偏弱）"

    # direction=up：abs_strength 正 = 向上续航 = raw 正 ✓
    # direction=down：abs_strength 正 = 向下续航 = "向上" raw 应为负，_signed 再翻回正
    raw = abs_strength if ctx.direction == "up" else -abs_strength
    return _signed(raw, ctx), note, float(near_ratio)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 聚合
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


def _dynamic_weights(ctx: _Context) -> dict[str, float]:
    """三维权重动态化：
      - 强趋势（trend_up/down）：D3 权重降为 0.20（防过早反转）
      - 震荡/极端：D1 降权 D3 升权（但 regime_veto 会直接覆盖最终动作）
      - 蓄力（squeeze）：D2 升权（参与度是突破前的关键证据）
    """
    if ctx.is_strong_trend:
        return {"D1": 0.45, "D2": 0.35, "D3": 0.20}
    if ctx.regime == "squeeze":
        return {"D1": 0.35, "D2": 0.40, "D3": 0.25}
    if ctx.regime in _REGIME_VETO:
        return {"D1": 0.30, "D2": 0.30, "D3": 0.40}
    return dict(_W_D_DEFAULT)


def _compute_single_tf(
    tf: Timeframe,
    candles: list,
    cvd_series: list[float],
    oi_change_1h_pct: Optional[float],
    td_count: Optional[int],
    td_direction: str,
    price_1h_ago: Optional[float],
    ctx: _Context,
) -> Optional[TrendExhaustionState]:
    if not candles or len(candles) < _MIN_CANDLES[tf]:
        return None

    closes = [float(getattr(c, "close", c.get("c", 0.0) if isinstance(c, dict) else 0.0))
              for c in candles]
    highs = [float(getattr(c, "high", c.get("h", 0.0) if isinstance(c, dict) else 0.0))
             for c in candles]
    lows = [float(getattr(c, "low", c.get("l", 0.0) if isinstance(c, dict) else 0.0))
            for c in candles]

    # —— D1 动能 ——
    m1_s, m1_note, m1_val = _m1_macd_hist_accel(closes, ctx)
    m2_s, m2_note, m2_val = _m2_price_slope_zscore(closes, ctx)
    m3_s, m3_note, m3_val = _m3_rsi_zone(closes, ctx)
    m4_s, m4_note, m4_val = _m4_fvg_persistence(highs, lows, closes, ctx)
    d1_subs = [
        SubScore(key="m1_macd_2d", name="MACD 二阶导", score=m1_s, note=m1_note, value=m1_val),
        SubScore(key="m2_slope_z", name="价格斜率 z", score=m2_s, note=m2_note, value=m2_val),
        SubScore(key="m3_rsi_zone", name="RSI 区间", score=m3_s, note=m3_note, value=m3_val),
        SubScore(key="m4_fvg", name="FVG 续航", score=m4_s, note=m4_note, value=m4_val),
    ]
    momentum_score = _weighted(d1_subs, _W_D1_SUBS)

    # —— D2 参与度 ——
    p1_s, p1_note, p1_val = _p1_cvd_momentum(closes, cvd_series or [], ctx)
    d2_subs = [SubScore(key="p1_cvd_momo", name="CVD 动能", score=p1_s, note=p1_note, value=p1_val)]
    weights_d2 = dict(_W_D2_SUBS)
    if tf == "1h":
        p2_s, p2_note, p2_val = _p2_oi_price_confluence(
            price_now=closes[-1], price_1h_ago=price_1h_ago,
            oi_change_1h_pct=oi_change_1h_pct, ctx=ctx,
        )
        d2_subs.append(SubScore(key="p2_oi_price", name="OI×Price 踩踏", score=p2_s,
                                note=p2_note, value=p2_val))
    else:
        weights_d2.pop("p2_oi_price", None)

    # BTC + 1d 启用 CB 溢价（p3 更偏"慢变量"，日线用最自然）
    if ctx.is_btc and tf == "1d":
        p3_s, p3_note, p3_val = _p3_coinbase_premium_flow(ctx)
        d2_subs.append(SubScore(key="p3_cb_premium", name="CB 溢价", score=p3_s,
                                note=p3_note, value=p3_val))
    else:
        weights_d2.pop("p3_cb_premium", None)

    # p4 资金费率：4h/1d 启用（1h 过于嘈杂，funding 天然是 8h 粒度）
    if tf in ("4h", "1d") and ctx.funding_bp_8h is not None:
        p4_s, p4_note, p4_val = _p4_funding_extreme(ctx)
        d2_subs.append(SubScore(key="p4_funding", name="资金费率", score=p4_s,
                                note=p4_note, value=p4_val))
    else:
        weights_d2.pop("p4_funding", None)

    # 归一化 D2 权重
    if weights_d2:
        total = sum(weights_d2.values())
        if total > 0:
            weights_d2 = {k: v / total for k, v in weights_d2.items()}
    participation_score = _weighted(d2_subs, weights_d2)

    # —— D3 衰竭触发器 ——
    if tf == "1d":
        e1_s, e1_note, e1_val = _e1_td_sequential(td_count, td_direction, ctx)
    else:
        e1_s, e1_note, e1_val = 0.0, "TD 仅在日线参与", None
    e2_s, e2_note, e2_val = _e2_rsi_price_divergence(highs, lows, closes, ctx)
    e3_s, e3_note, e3_val = _e3_fib_extension_hit(highs, lows, closes, ctx)
    d3_subs = [
        SubScore(key="e1_td_seq", name="TD Sequential", score=e1_s, note=e1_note, value=e1_val),
        SubScore(key="e2_rsi_div", name="RSI 背离", score=e2_s, note=e2_note, value=e2_val),
        SubScore(key="e3_fib_ext", name="Fib 扩展命中", score=e3_s, note=e3_note, value=e3_val),
    ]
    weights_d3 = dict(_W_D3_SUBS)
    # e4 清算簇磁吸：4h/1d 启用（1h 清算簇变化太快，噪声大）
    if tf in ("4h", "1d") and ctx.liq_map is not None:
        e4_s, e4_note, e4_val = _e4_liq_cluster_fuel(closes[-1], ctx)
        d3_subs.append(SubScore(key="e4_liq_fuel", name="清算簇磁吸", score=e4_s,
                                note=e4_note, value=e4_val))
    else:
        weights_d3.pop("e4_liq_fuel", None)
    if weights_d3:
        total = sum(weights_d3.values())
        if total > 0:
            weights_d3 = {k: v / total for k, v in weights_d3.items()}
    exhaustion_score = _weighted(d3_subs, weights_d3)

    w = _dynamic_weights(ctx)
    composite = w["D1"] * momentum_score + w["D2"] * participation_score + w["D3"] * exhaustion_score
    composite = max(-1.0, min(1.0, composite))

    # 若 direction=flat，composite 的方向语义消失，强制 neutral
    if ctx.direction == "flat":
        state_val: ExhaustionState = "neutral"
        action = "stand_aside"
        reason = "方向未定（无 1d/4h 结构），保持观望"
    else:
        triggers_hit = [s.key for s in d3_subs if abs(s.score) >= 0.5]
        if composite >= _TH_HEALTHY:
            state_val = "healthy_continuation"
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
        direction=ctx.direction,
        momentum_score=round(momentum_score, 3),
        participation_score=round(participation_score, 3),
        exhaustion_score=round(exhaustion_score, 3),
        composite_score=round(composite, 3),
        state=state_val,
        state_age_min=0,
        confirmed_ticks=0,
        triggers=[s.key for s in (d1_subs + d2_subs + d3_subs) if abs(s.score) >= 0.5],
        sub_scores=d1_subs + d2_subs + d3_subs,
        action_hint=action,  # type: ignore[arg-type]
        reason_cn=reason,
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 硬门闸：exhaustion_warn / structural_reversal 需要连续 2 tick 确认
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_GATED_STATES = {"exhaustion_warn", "structural_reversal"}


def _confirm_or_downgrade(
    curr: Optional[TrendExhaustionState],
    prev: Optional[TrendExhaustionState],
) -> Optional[TrendExhaustionState]:
    """应用硬门闸：同 state 连续两 tick 才放行高危档位。"""
    if curr is None:
        return None
    prev_state = prev.state if prev else None
    prev_ticks = prev.confirmed_ticks if prev else 0
    if curr.state == prev_state:
        curr.confirmed_ticks = prev_ticks + 1
    else:
        curr.confirmed_ticks = 1

    # 首次出现高危状态 → 降级为 momentum_fading
    if curr.state in _GATED_STATES and curr.confirmed_ticks < 2:
        # 保留 composite / 子项，但对外输出降级
        orig = curr.state
        curr.state = "momentum_fading"
        curr.action_hint = "reduce"  # type: ignore[assignment]
        curr.reason_cn = f"[待确认] 首次检测到 {orig}，降级为减仓观察"
    return curr


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

    mid_long = [s for s in (tf_4h, tf_1d) if s is not None]
    if not mid_long:
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

    h1_sign = 0
    if tf_1h:
        if tf_1h.composite_score > _TH_HEALTHY:
            h1_sign = 1
        elif tf_1h.composite_score < _TH_FADING:
            h1_sign = -1
    ml_sign = 1 if ml_avg > _TH_HEALTHY else (-1 if ml_avg < _TH_FADING else 0)

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

    parts = [f"{s.tf}={s.state}({s.composite_score:+.2f})"
             for s in (tf_4h, tf_1d, tf_1h) if s]
    reason = f"共识={consensus}；" + " / ".join(parts)
    return consensus, overall_state, action, round(pos, 2), reason  # type: ignore[return-value]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 白话生成器（小白卡）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_DIR_EMOJI = {"up": "📈 ", "down": "📉 ", "flat": "⏸ "}

_PLAIN_CN_MAP: dict[tuple[Direction, ExhaustionState], tuple[str, str]] = {
    # (direction, state) -> (plain, tip)
    ("up", "healthy_continuation"): ("还在涨，动能健康", "顺势持有或加仓都可以"),
    ("up", "momentum_fading"):      ("还在涨，但动能在变慢", "已有仓位减半，别再追高"),
    ("up", "exhaustion_warn"):      ("涨不动了，多头要竭", "有仓位建议离场观望"),
    ("up", "structural_reversal"):  ("顶部已确认，方向切换", "清仓，别扛单"),
    ("up", "neutral"):              ("上涨方向，信号不清晰", "保持观望或轻仓跟随"),
    ("down", "healthy_continuation"): ("还在跌，空头动能健康", "顺势持空或加空都可以"),
    ("down", "momentum_fading"):     ("还在跌，但空头动能在变慢", "已有空单减半，别再追空"),
    ("down", "exhaustion_warn"):     ("跌不动了，空头要竭", "有空单建议离场观望"),
    ("down", "structural_reversal"): ("底部已确认，方向切换", "清空单，别扛单"),
    ("down", "neutral"):             ("下跌方向，信号不清晰", "保持观望或轻仓跟随"),
    ("flat", "healthy_continuation"): ("方向未定但动能偏强", "等方向明朗再进场"),
    ("flat", "momentum_fading"):      ("方向未定，动能在变弱", "空仓观望"),
    ("flat", "exhaustion_warn"):      ("方向未定，疑似变盘", "空仓观望"),
    ("flat", "structural_reversal"):  ("方向未定，疑似反转", "空仓观望"),
    ("flat", "neutral"):              ("震荡或样本不足", "空仓观望"),
}


def _plain_cn(direction: Direction, state: ExhaustionState, regime_vetoed: bool) -> tuple[str, str]:
    if regime_vetoed:
        return ("当前是震荡/极端行情，没有趋势", "不要做趋势单，空仓或等方向明朗")
    plain, tip = _PLAIN_CN_MAP.get((direction, state), ("信号不明确", "空仓观望"))
    return _DIR_EMOJI.get(direction, "") + plain, tip


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 对外主入口
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _extract_cvd_values(cvd_data) -> list[float]:
    if not cvd_data or not getattr(cvd_data, "series", None):
        return []
    return [float(p.cvd) for p in cvd_data.series]


def _resample_1h_to(candles_1h: list, bars_per_unit: int) -> list:
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
    """v2 主入口：regime-aware + 踩踏识别 + 硬门闸 + 白话总结。"""
    ts_now = int(time.time())
    missing: list[str] = []

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

    price_1h_ago: Optional[float] = None
    if c_1h:
        price_1h_ago = float(c_1h[-2].close) if len(c_1h) >= 2 else None

    td_count = state.td_sequential_count
    td_dir = state.td_sequential_direction or ""

    ctx_1h = _build_context("1h", state.coin, state, c_1h)
    ctx_4h = _build_context("4h", state.coin, state, c_4h)
    ctx_1d = _build_context("1d", state.coin, state, c_1d)

    tf_1h_state = _compute_single_tf("1h", c_1h, cvd_vals, oi_chg_1h, td_count, td_dir, price_1h_ago, ctx_1h)
    tf_4h_state = _compute_single_tf("4h", c_4h, cvd_vals, oi_chg_1h, td_count, td_dir, price_1h_ago, ctx_4h)
    tf_1d_state = _compute_single_tf("1d", c_1d, cvd_vals, oi_chg_1h, td_count, td_dir, price_1h_ago, ctx_1d)

    # 硬门闸：对每个周期单独 confirm/downgrade
    tf_1h_state = _confirm_or_downgrade(tf_1h_state, prev_signal.tf_1h if prev_signal else None)
    tf_4h_state = _confirm_or_downgrade(tf_4h_state, prev_signal.tf_4h if prev_signal else None)
    tf_1d_state = _confirm_or_downgrade(tf_1d_state, prev_signal.tf_1d if prev_signal else None)

    consensus, overall_state, action, pos, reason = _resolve_consensus(
        tf_1h_state, tf_4h_state, tf_1d_state
    )

    # Regime veto：震荡/极端直接 stand_aside
    regime = ctx_1d.regime  # 以 1d context 的 regime 为共识层判定
    regime_vetoed = regime in _REGIME_VETO
    if regime_vetoed:
        action = "stand_aside"
        pos = 0.0
        overall_state = "neutral"
        consensus = "neutral"
        reason = f"当前 regime={regime}，趋势不成立，强制观望（{reason}）"

    # state_age：粗粒度
    def _age(prev: Optional[TrendExhaustionState], curr: Optional[TrendExhaustionState]) -> int:
        if not curr:
            return 0
        if not prev or prev.state != curr.state:
            return 0
        return prev.state_age_min + max(0, (ts_now - (prev_signal.ts if prev_signal else ts_now)) // 60)

    if prev_signal:
        if tf_1h_state:
            tf_1h_state.state_age_min = _age(prev_signal.tf_1h, tf_1h_state)
        if tf_4h_state:
            tf_4h_state.state_age_min = _age(prev_signal.tf_4h, tf_4h_state)
        if tf_1d_state:
            tf_1d_state.state_age_min = _age(prev_signal.tf_1d, tf_1d_state)

    tf_present = sum(1 for x in (tf_1h_state, tf_4h_state, tf_1d_state) if x is not None)
    if tf_present == 3 and not missing:
        dq = "ok"
    elif tf_present >= 2:
        dq = "partial"
    else:
        dq = "insufficient"

    overall_direction: Direction = ctx_1d.direction if ctx_1d.direction != "flat" else ctx_4h.direction
    plain, tip = _plain_cn(overall_direction, overall_state, regime_vetoed)

    if dq == "insufficient":
        return TrendExhaustionSignal(
            coin=state.coin,
            ts=ts_now,
            tf_1h=tf_1h_state,
            tf_4h=tf_4h_state,
            tf_1d=tf_1d_state,
            consensus_level="neutral",
            overall_direction=overall_direction,
            overall_state="neutral",
            overall_action="stand_aside",
            overall_position_pct=0.0,
            overall_plain_cn="样本不足或数据未齐",
            overall_tip_cn="等数据齐了再看",
            overall_reason_cn="样本不足或数据未齐，保持观望",
            regime=regime,  # type: ignore[arg-type]
            regime_vetoed=False,
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
        overall_direction=overall_direction,
        overall_state=overall_state,
        overall_action=action,
        overall_position_pct=pos,
        overall_plain_cn=plain,
        overall_tip_cn=tip,
        overall_reason_cn=reason,
        regime=regime,  # type: ignore[arg-type]
        regime_vetoed=regime_vetoed,
        data_quality=dq,
        missing_inputs=missing,
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 向下兼容：保留旧函数名（给旧测试 import）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _m1_macd_second_derivative(closes: list[float]) -> tuple[float, str, Optional[float]]:
    """Deprecated：旧 API，内部走 v2（direction=up 的 ctx）。"""
    ctx = _Context(tf="1h", direction="up", regime="range")
    return _m1_macd_hist_accel(closes, ctx)


def _m2_ema_deviation(closes: list[float]) -> tuple[float, str, Optional[float]]:
    """Deprecated：旧 API。"""
    ctx = _Context(tf="1h", direction="up", regime="range")
    return _m2_price_slope_zscore(closes, ctx)


def _p2_oi_price_alignment(
    price_now: float,
    price_1h_ago: Optional[float],
    oi_change_1h_pct: Optional[float],
) -> tuple[float, str, Optional[float]]:
    """Deprecated：旧 API。"""
    ctx = _Context(tf="1h", direction="up", regime="range")
    return _p2_oi_price_confluence(price_now, price_1h_ago, oi_change_1h_pct, ctx)
