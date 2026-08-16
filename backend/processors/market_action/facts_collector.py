"""FactsCollector · state → MarketActionFacts

职责：
- 读取 CoinState 上的各字段，严格组装成 MarketActionFacts
- 对每一项调用对应的派生器（footprint_analyzer / price_context / liq_cluster_analyzer / derived_labels）
- 标记缺失字段 + data_quality
"""

from __future__ import annotations

import time
from statistics import pstdev
from typing import Optional, TYPE_CHECKING

from models.market_action import (
    AbsorptionSnapshot,
    BasisSnapshot,
    CVDSnapshot,
    DataQuality,
    FactsDataMeta,
    FundingSnapshot,
    LiquidationSnapshot,
    MarketActionFacts,
    OISnapshot as MAOISnapshot,
    OIVenueEntry,
    OptionsSnapshot,
    OrderbookSnapshot,
    PriceSnapshot,
    TakerFlowSnapshot,
)

from processors.absorption_detector import detect_absorption_zones

from .derived_labels import (
    derive_funding_trend,
    derive_oi_price_coherence,
    derive_spot_contract_coherence,
)
from .footprint_analyzer import build_snapshot as build_footprint_snapshot
from .liq_cluster_analyzer import build_cluster_snapshot, build_sweep_snapshot
from .price_context import build_price_context

if TYPE_CHECKING:
    from engine import CoinState


def _safe_float(v, default=None):
    try:
        if v is None:
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def _trend_label(series: list[dict], field: str = "basis_pct") -> str:
    """对近 1h 的序列做线性趋势判定。"""
    vals = [s.get(field) for s in series if s.get(field) is not None]
    if len(vals) < 3:
        return "stable"
    first_third = sum(vals[:len(vals)//3]) / max(len(vals)//3, 1)
    last_third = sum(vals[-len(vals)//3:]) / max(len(vals)//3, 1)
    diff = last_third - first_third
    if abs(first_third) < 1e-6:
        return "stable"
    pct_change = diff / (abs(first_third) or 1e-6)
    if abs(pct_change) < 0.1:
        return "stable"
    # 对 spread 和 basis_pct 都是：绝对值扩大 = widening
    if abs(last_third) > abs(first_third):
        return "widening"
    return "narrowing"


def build_price_snapshot(state: "CoinState") -> Optional[PriceSnapshot]:
    if not state.ticker:
        return None
    last = state.ticker.last
    # 1h / 4h 变化从 candles_1h 推断
    c1h = _safe_float(getattr(state.ticker, "change_pct_24h", None))
    ch_1h = None
    ch_4h = None
    if state.candles_1h and len(state.candles_1h) >= 5:
        try:
            # 每根 1h = close
            def _close(c):
                return float(c.close if hasattr(c, "close") else c.get("close", c.get("c", 0)))
            closes = [_close(c) for c in state.candles_1h[-5:]]
            if closes[0] > 0:
                ch_1h = round((closes[-1] - closes[-2]) / closes[-2] * 100, 3) if len(closes) >= 2 and closes[-2] > 0 else None
                ch_4h = round((closes[-1] - closes[0]) / closes[0] * 100, 3) if closes[0] > 0 else None
        except Exception:
            pass

    # 近 6 根 1h bars（OHLCV）· ts 统一输出为秒
    def _ts_to_sec(ts_raw: int) -> int:
        return int(ts_raw // 1000) if ts_raw > 10_000_000_000 else int(ts_raw)

    recent_bars: list[list[float]] = []
    if state.candles_1h:
        for c in state.candles_1h[-6:]:
            try:
                if hasattr(c, "ts"):
                    recent_bars.append([
                        _ts_to_sec(int(c.ts)),
                        float(c.open), float(c.high),
                        float(c.low), float(c.close), float(getattr(c, "vol", 0) or 0),
                    ])
                elif isinstance(c, dict):
                    recent_bars.append([
                        _ts_to_sec(int(c.get("ts", c.get("t", 0)))),
                        float(c.get("open", c.get("o", 0))),
                        float(c.get("high", c.get("h", 0))),
                        float(c.get("low", c.get("l", 0))),
                        float(c.get("close", c.get("c", 0))),
                        float(c.get("vol", c.get("v", 0))),
                    ])
            except Exception:
                continue

    # 最后一根 1h bar 是否已收盘：ts + 3600s 到达当前时间（30s 容忍）
    latest_bar_closed: Optional[bool] = None
    if recent_bars:
        try:
            last_ts = int(recent_bars[-1][0])
            now = int(time.time())
            latest_bar_closed = bool(now + 30 >= last_ts + 3600)
        except (TypeError, ValueError, IndexError):
            latest_bar_closed = None

    return PriceSnapshot(
        last=last,
        price_kind="last",
        change_1h_pct=ch_1h,
        change_4h_pct=ch_4h,
        change_24h_pct=c1h,
        high_24h=getattr(state.ticker, "high_24h", None),
        low_24h=getattr(state.ticker, "low_24h", None),
        recent_bars_1h=recent_bars,
        latest_bar_closed=latest_bar_closed,
    )


def _derive_oi_history_fields(
    current_oi_usd: float,
    hourly_history: list,
) -> dict:
    """基于 30d hourly OI 历史派生 percentile / is_near_local_high_7d。

    全部基于真实采样（bisect 排序 + 切片 max），无任何推算。
    样本不足时对应字段返回 None，透明标注。
    """
    out: dict = {
        "percentile_30d_hourly": None,
        "is_near_local_high_7d": None,
        "history_sample_size": None,
    }
    if not hourly_history or current_oi_usd <= 0:
        return out
    pts = [p.get("oi_usd") for p in hourly_history if p.get("oi_usd")]
    pts = [float(x) for x in pts if x is not None and float(x) > 0]
    n = len(pts)
    out["history_sample_size"] = n
    if n >= 24:  # 至少 1 天样本才算分位
        sorted_pts = sorted(pts)
        # bisect 写法等价于：(count of values <= current_oi) / n × 100
        import bisect
        rank = bisect.bisect_right(sorted_pts, current_oi_usd)
        out["percentile_30d_hourly"] = round(rank / n * 100, 1)
    # 近 7d 最高（168 点 1h）
    if n >= 24:
        last_7d = pts[-168:] if n >= 168 else pts
        local_max = max(last_7d)
        if local_max > 0:
            out["is_near_local_high_7d"] = bool(current_oi_usd >= local_max * 0.98)
    return out


_VENUE_WHITELIST = {"binance", "okx"}
_VENUE_DISPLAY = {"binance": "Binance", "okx": "OKX"}


def _build_oi_venue_split(state: "CoinState") -> list[OIVenueEntry]:
    """从 state.oi_exchange_rank 提取 Binance / OKX 两家头部分布。

    覆盖逻辑：rank 数据来自 poll_oi_exchange_rank（Coinglass futures-oi list 接口），
    含 oi_usd / change_1h / change_24h 三列；占比基于全市场 total_oi_usd 计算
    （含 Bybit/Bitget/Gate 等其他交易所），符合"识别哪个 venue 在带方向"的目标。
    """
    rank = getattr(state, "oi_exchange_rank", None) or {}
    exchanges = rank.get("exchanges") or []
    total_oi = float(rank.get("total_oi_usd") or 0)
    out: list[OIVenueEntry] = []
    for item in exchanges:
        ex_name = str(item.get("exchange") or "").strip()
        if ex_name.lower() not in _VENUE_WHITELIST:
            continue
        try:
            oi_usd = float(item.get("oi_usd") or 0)
        except (TypeError, ValueError):
            continue
        if oi_usd <= 0:
            continue
        share = round(oi_usd / total_oi * 100, 2) if total_oi > 0 else None
        ch_1h = _safe_float(item.get("change_1h"))
        ch_24h = _safe_float(item.get("change_24h"))
        out.append(OIVenueEntry(
            venue=_VENUE_DISPLAY.get(ex_name.lower(), ex_name),
            oi_usd=oi_usd,
            share_pct=share,
            change_1h_pct=ch_1h,
            change_24h_pct=ch_24h,
        ))
    out.sort(key=lambda e: e.oi_usd, reverse=True)
    return out


def build_oi_snapshot(state: "CoinState") -> Optional[MAOISnapshot]:
    oi = state.oi
    if not oi:
        return None
    # P0 派生：30d hourly 分位 + 近 7d 是否接近局部高
    hist = list(getattr(state, "oi_hourly_history", []) or [])
    derived = _derive_oi_history_fields(float(oi.current_usd), hist)
    return MAOISnapshot(
        current_usd=float(oi.current_usd),
        change_5m_pct=_safe_float(oi.change_5m_pct),
        change_1h_pct=_safe_float(oi.change_1h_pct),
        change_24h_pct=_safe_float(state.oi_change_24h_pct),
        trend=oi.trend or None,
        percentile_30d_hourly=derived["percentile_30d_hourly"],
        is_near_local_high_7d=derived["is_near_local_high_7d"],
        history_sample_size=derived["history_sample_size"],
        venue_split=_build_oi_venue_split(state),
    )


# ── slope_24h 阈值（与 SYSTEM_PROMPT dead zone funding=0.0001 同根源） ──
# 8h 一个点，最近 3 个点跨度 ≈ 24h；用 (p[-1] - p[-3]) 作为方向变化度量
_FUNDING_SLOPE_FLAT = 0.0001       # |Δ| < flat → "flat"
_FUNDING_SLOPE_FAST = 0.0003       # |Δ| ≥ fast → "rising_fast" / "falling_fast"


def _classify_funding_slope(delta: float) -> str:
    """把 24h funding 变化幅度映射到 5 档自然语言标签。"""
    abs_d = abs(delta)
    if abs_d < _FUNDING_SLOPE_FLAT:
        return "flat"
    if abs_d >= _FUNDING_SLOPE_FAST:
        return "rising_fast" if delta > 0 else "falling_fast"
    return "rising" if delta > 0 else "falling"


def _percentile_of(value: float, sample: list[float]) -> Optional[int]:
    """当前值在样本里的百分位（1-100，越大代表越偏正向）。

    定义：percentile = round( (#sample < value + 0.5 × #sample == value) / N × 100 )
    采用"中点修正"避免相同值堆积造成 0 或 100 偏差；空样本返回 None。
    """
    n = len(sample)
    if n == 0:
        return None
    less = sum(1 for x in sample if x < value)
    eq = sum(1 for x in sample if x == value)
    pct = (less + 0.5 * eq) / n * 100.0
    # 钳制到 [1, 100]，让 AI 看到 1/100 就知道是极端
    return max(1, min(100, int(round(pct))))


def _derive_funding_history_fields(
    avg_current: float,
    current_oi_usd: Optional[float],
    history_8h: list,
) -> dict:
    """基于 30d × 8h 结算点历史派生派生字段（采样 + 算术，无推算）。

    P0：
      - hourly_cost_usd  = avg_current × current_oi / 8（8h 一结算，折算每小时）
      - cost_24h_usd     = Σ(最近 3 个 8h 点) × current_oi（**基于当前 OI 近似**）
      - days_negative_streak = 从最新往前数连续 rate < 0 的点数 / 3
      - sign_flip_7d     = 近 7d 均值与"前 7 天滚动参考"符号相反
    P1（短期斜率 + 极值上下文）：
      - slope_24h        = 最近 3 个 8h 点 (p[-1]-p[-3]) 映射到 5 档；< 3 点返回 None
      - percentile_7d    = 当前 avg_current 在 21 个 8h 点中的百分位；< 14 点 None
      - percentile_30d   = 当前 avg_current 在 90 个 8h 点中的百分位；< 30 点 None
    """
    out: dict = {
        "hourly_cost_usd": None,
        "cost_24h_usd": None,
        "days_negative_streak": None,
        "sign_flip_7d": None,
        "history_sample_size": None,
        # P1 新字段
        "slope_24h": None,
        "percentile_7d": None,
        "percentile_30d": None,
    }
    pts = [p for p in (history_8h or []) if p.get("rate") is not None]
    n = len(pts)
    out["history_sample_size"] = n

    # 1) hourly_cost_usd：即时值即可算（不依赖历史），有 OI 即可
    if current_oi_usd and current_oi_usd > 0:
        out["hourly_cost_usd"] = round(avg_current * current_oi_usd / 8.0, 2)

    if n == 0:
        return out

    # 2) cost_24h_usd：需要最近 3 个 8h 点 + 当前 OI（近似口径）
    if n >= 3 and current_oi_usd and current_oi_usd > 0:
        last3_sum = sum(float(p["rate"]) for p in pts[-3:])
        out["cost_24h_usd"] = round(last3_sum * current_oi_usd, 2)

    # 3) days_negative_streak：从最新往前数连续 < 0
    streak_points = 0
    for p in reversed(pts):
        if float(p["rate"]) < 0:
            streak_points += 1
        else:
            break
    # 8h 一个点，3 个点 = 1 天
    out["days_negative_streak"] = round(streak_points / 3.0, 2)

    # 4) sign_flip_7d：近 7d（最近 21 点）均值 vs 前 7d（再往前 21 点）均值
    #   需要两个独立的 21 点窗口（共 42 点 ≈ 14 天），否则诚实返回 None
    #   任何"样本不足时用 24h 近似 7d"的退化方案数学上都有 overlap bug，不做
    if n >= 42:
        recent_7d = sum(float(p["rate"]) for p in pts[-21:]) / 21.0
        prior_7d = sum(float(p["rate"]) for p in pts[-42:-21]) / 21.0
        if abs(recent_7d) > 1e-9 and abs(prior_7d) > 1e-9:
            out["sign_flip_7d"] = bool((recent_7d > 0) != (prior_7d > 0))
    # n < 42 时 sign_flip_7d 保持 None，AI 看到 history_sample_size 会知道为何

    # ── P1：短期斜率 + 极值上下文 ──
    # 5) slope_24h：最近 3 个 8h 点跨度 ≈ 24h 的方向变化分类
    if n >= 3:
        delta = float(pts[-1]["rate"]) - float(pts[-3]["rate"])
        out["slope_24h"] = _classify_funding_slope(delta)

    # 6) percentile_7d：当前值在最近 21 点里的百分位（要求 ≥ 14 点才有意义）
    #    口径说明：history（OI 加权）与 avg_current（多所均值）虽来源略不同，
    #    但量级一致，作为"当前 funding 在历史里偏高/偏低"的近似指标足够
    if n >= 14:
        sample_7d = [float(p["rate"]) for p in pts[-21:]]
        out["percentile_7d"] = _percentile_of(avg_current, sample_7d)

    # 7) percentile_30d：当前值在最近 90 点里的百分位（要求 ≥ 30 点）
    if n >= 30:
        sample_30d = [float(p["rate"]) for p in pts[-90:]]
        out["percentile_30d"] = _percentile_of(avg_current, sample_30d)

    return out


def _compute_avg_7d_from_history(history_8h: list) -> Optional[float]:
    """从 funding_history_8h（已归一化为小数单位）直接计算 7d 均值。

    单点真相原则：不依赖 state.multi_funding.avg_7d（会被 poll_funding_all 覆盖），
    facts_collector 每次组装 snapshot 时都从 deque 现算。
    """
    recent_21 = list(history_8h)[-21:]
    if not recent_21:
        return None
    try:
        return round(sum(float(p["rate"]) for p in recent_21) / len(recent_21), 8)
    except (TypeError, ValueError, KeyError, ZeroDivisionError):
        return None


def _latest_rate_from_history(history_8h: list) -> Optional[float]:
    """从 funding_history_8h 取最新点作为 oi_weighted（接口本身就是 OI 加权口径）。"""
    if not history_8h:
        return None
    try:
        return round(float(list(history_8h)[-1]["rate"]), 8)
    except (TypeError, ValueError, KeyError):
        return None


def build_funding_snapshot(state: "CoinState") -> Optional[FundingSnapshot]:
    # 先确定 current_oi（派生成本需要用）
    current_oi_usd: Optional[float] = None
    if state.oi and state.oi.current_usd:
        try:
            current_oi_usd = float(state.oi.current_usd)
        except (TypeError, ValueError):
            current_oi_usd = None

    history_8h = list(getattr(state, "funding_history_8h", []) or [])
    # 直接从 deque 现算，避免依赖 multi_funding.avg_7d（会被 poll_funding_all 覆盖）
    avg_7d_computed = _compute_avg_7d_from_history(history_8h)
    oi_weighted_computed = _latest_rate_from_history(history_8h)

    mf = state.multi_funding
    if mf and mf.exchanges:
        vals = [_safe_float(e.current) for e in mf.exchanges if _safe_float(e.current) is not None]
        disp = round(pstdev(vals), 8) if len(vals) >= 2 else 0.0
        avg_current = float(mf.avg_current)
        derived = _derive_funding_history_fields(avg_current, current_oi_usd, history_8h)
        return FundingSnapshot(
            avg_current=avg_current,
            # avg_7d / oi_weighted 从 history 现算，不读 mf（mf 字段会被覆盖成 0）
            avg_7d=avg_7d_computed,
            oi_weighted=oi_weighted_computed,
            interpretation=mf.interpretation or None,
            exchange_count=len(mf.exchanges),
            dispersion_abs=disp,
            hourly_cost_usd=derived["hourly_cost_usd"],
            cost_24h_usd=derived["cost_24h_usd"],
            days_negative_streak=derived["days_negative_streak"],
            sign_flip_7d=derived["sign_flip_7d"],
            history_sample_size=derived["history_sample_size"],
            slope_24h=derived["slope_24h"],
            percentile_7d=derived["percentile_7d"],
            percentile_30d=derived["percentile_30d"],
        )
    f = state.funding
    if f:
        avg_current = float(f.avg_rate)
        derived = _derive_funding_history_fields(avg_current, current_oi_usd, history_8h)
        return FundingSnapshot(
            avg_current=avg_current,
            avg_7d=avg_7d_computed,
            oi_weighted=oi_weighted_computed if oi_weighted_computed is not None
            else _safe_float(f.oi_weighted_rate),
            interpretation=f.interpretation or None,
            exchange_count=(1 if f.okx_rate is not None else 0) + (1 if f.binance_rate is not None else 0),
            hourly_cost_usd=derived["hourly_cost_usd"],
            cost_24h_usd=derived["cost_24h_usd"],
            days_negative_streak=derived["days_negative_streak"],
            sign_flip_7d=derived["sign_flip_7d"],
            history_sample_size=derived["history_sample_size"],
            slope_24h=derived["slope_24h"],
            percentile_7d=derived["percentile_7d"],
            percentile_30d=derived["percentile_30d"],
        )
    return None


def _trend_label_from_window(window: list[float], noise_ratio: float = 0.15) -> Optional[str]:
    """从一段 delta 序列派生 rising / declining / flat 标签。

    自适应噪声阈值：|sum| 超过 (max|x| × noise_ratio) 才判方向，避免抖动误判。
    低币值、样本少时自动倾向 flat。
    """
    if not window:
        return None
    s = sum(window)
    peak = max((abs(x) for x in window), default=0.0)
    noise = peak * noise_ratio
    if s > noise:
        return "rising"
    if s < -noise:
        return "declining"
    return "flat"


def build_cvd_snapshot(cvd, recent_delta_5m: Optional[list[float]] = None) -> Optional[CVDSnapshot]:
    if not cvd or not cvd.series:
        return None
    delta_arr: list[float] = []
    if recent_delta_5m is None:
        # 取 12 点（近 1h）与 delta_1h 的窗口对齐，逐点求和 ≈ delta_1h
        # 防 off-by-one：Coinglass 返回的是 5m K 线，恰好 12 根 = 60min
        delta_arr = [float(p.delta) for p in cvd.series[-12:]]
    else:
        delta_arr = list(recent_delta_5m)
    # 拐点识别辅助字段：后 6 根 5m = 近 30min
    trend_recent_30m = _trend_label_from_window(delta_arr[-6:]) if len(delta_arr) >= 3 else None

    # 最后一根 5m bar 是否已收盘（Coinglass series 的 ts 单位为秒，5m=300s）
    latest_bar_closed: Optional[bool] = None
    try:
        last_pt = cvd.series[-1]
        last_ts = int(getattr(last_pt, "ts", 0) or 0)
        if last_ts > 10_000_000_000:  # 防御毫秒
            last_ts //= 1000
        if last_ts > 0:
            latest_bar_closed = bool(int(time.time()) + 30 >= last_ts + 300)
    except (AttributeError, TypeError, ValueError, IndexError):
        latest_bar_closed = None

    return CVDSnapshot(
        delta_1h=float(cvd.delta_1h or 0),
        trend_1h=cvd.trend_1h or None,
        trend_recent_30m=trend_recent_30m,
        has_divergence=bool(cvd.has_divergence),
        divergence_note=cvd.divergence_note or None,
        recent_delta_5m=delta_arr,
        latest_bar_closed=latest_bar_closed,
    )


def build_liquidation_snapshot(state: "CoinState") -> Optional[LiquidationSnapshot]:
    gl = state.global_liq
    if not gl:
        return None
    ratio_1h = float(gl.ratio_1h) if gl.ratio_1h else 1.0
    if ratio_1h > 1.3:
        dom = "long_being_liquidated"
    elif ratio_1h < 0.77:
        dom = "short_being_liquidated"
    else:
        dom = "balanced"
    return LiquidationSnapshot(
        long_1h_usd=float(gl.long_1h_usd),
        short_1h_usd=float(gl.short_1h_usd),
        long_24h_usd=float(gl.long_24h_usd),
        short_24h_usd=float(gl.short_24h_usd),
        ratio_1h=ratio_1h,
        dominant_side_1h=dom,
    )


def build_basis_snapshot(state: "CoinState") -> Optional[BasisSnapshot]:
    if not state.basis:
        return None
    series = list(getattr(state, "basis_history", []) or [])
    recent_vals = [s["basis_pct"] for s in series if "basis_pct" in s][-12:]
    trend = _trend_label(series, "basis_pct")
    return BasisSnapshot(
        basis_pct=float(state.basis.basis_pct),
        basis_trend=trend,
        recent_values=recent_vals,
        interpretation=state.basis.interpretation or None,
    )


def build_orderbook_snapshot(state: "CoinState") -> Optional[OrderbookSnapshot]:
    """构建盘口挂单失衡度快照。

    注意：上游 state.orderbook.spread_pct 和 orderbook_series 里的 `spread_pct`
    仍沿用老字段名（老接口共用，不便改动）。本函数**只在 MAA 出口把数据映射到
    语义准确的 book_imbalance_pct**，老系统的字段保持不变避免连锁改动。
    """
    if not state.orderbook:
        return None
    series = getattr(state, "orderbook_series", []) or []
    imbalances = [s.get("spread_pct", 0) for s in series if "spread_pct" in s][-12:]
    trend = _trend_label(series, "spread_pct")
    return OrderbookSnapshot(
        bid_total_usd=float(state.orderbook.bid_total_usd),
        ask_total_usd=float(state.orderbook.ask_total_usd),
        book_imbalance_pct=float(state.orderbook.spread_pct),
        imbalance_trend=trend,
        recent_imbalances=imbalances,
    )


def build_absorption_snapshot(state: "CoinState") -> AbsorptionSnapshot:
    """构建价位级被动吸收带快照。

    输入：
      - state.footprint_contract / state.footprint_spot（polls.footprint 写入）
      - state.ticker.last（当前价用于判 support/resistance）
    输出：
      AbsorptionSnapshot（永不 None；无数据时为空 snapshot）

    详细阈值和合并策略见 processors.absorption_detector。
    """
    current_price = float(state.ticker.last) if state.ticker and state.ticker.last else 0.0
    fp_contract = list(getattr(state, "footprint_contract", []) or [])
    fp_spot = list(getattr(state, "footprint_spot", []) or [])
    return detect_absorption_zones(
        footprint_contract=fp_contract,
        footprint_spot=fp_spot,
        current_price=current_price,
    )


def build_taker_snapshot(state: "CoinState") -> Optional[TakerFlowSnapshot]:
    c_series = getattr(state, "taker_contract_series", []) or []
    s_series = getattr(state, "taker_spot_series", []) or []
    if not c_series and not s_series:
        return None
    latest_c = c_series[-1]["delta_usd"] if c_series else None
    latest_s = s_series[-1]["delta_usd"] if s_series else None
    divergence = False
    if latest_c is not None and latest_s is not None:
        if (latest_c > 0) != (latest_s > 0) and abs(latest_c) > 0 and abs(latest_s) > 0:
            divergence = True
    return TakerFlowSnapshot(
        contract_recent_5m=c_series,
        spot_recent_5m=s_series,
        spot_vs_contract_divergence=divergence,
        latest_contract_delta_usd=latest_c,
        latest_spot_delta_usd=latest_s,
    )


def build_options_snapshot(state: "CoinState") -> Optional[OptionsSnapshot]:
    """仅 BTC/ETH 返回；SOL 返回 None。"""
    coin = state.coin.upper()
    if coin not in ("BTC", "ETH"):
        return None
    info = state.option_info
    if not info and state.option_pcr_oi is None:
        return None
    # IV 从 BBX MarketIndexData 回填
    iv_cur = None
    iv_skew = None
    if state.market_index:
        if coin == "BTC":
            iv_cur = state.market_index.btc_implied_vol
            iv_skew = state.market_index.btc_iv_skew_1m
    return OptionsSnapshot(
        total_oi_usd=float(info.total_oi_usd) if info and info.total_oi_usd else None,
        oi_change_24h_pct=_safe_float(getattr(state, "option_oi_change_24h_pct", None)),
        vol_change_24h_pct=_safe_float(getattr(state, "option_vol_change_24h_pct", None)),
        pcr_oi=_safe_float(getattr(state, "option_pcr_oi", None)),
        magnet_price=_safe_float(getattr(state, "option_magnet_price", None)),
        iv_current=_safe_float(iv_cur),
        iv_skew_1m=_safe_float(iv_skew),
    )


def _coherent_price_change(state: "CoinState") -> Optional[float]:
    """从 candles_1h 推断 1h 价格变化百分比。"""
    if not state.candles_1h or len(state.candles_1h) < 2:
        return None
    try:
        c_prev = state.candles_1h[-2]
        c_now = state.candles_1h[-1]
        p_prev = float(c_prev.close if hasattr(c_prev, "close") else c_prev.get("close", c_prev.get("c", 0)))
        p_now = float(c_now.close if hasattr(c_now, "close") else c_now.get("close", c_now.get("c", 0)))
        if p_prev > 0:
            return (p_now - p_prev) / p_prev * 100
    except Exception:
        return None
    return None


def collect(state: "CoinState") -> MarketActionFacts:
    """主入口：state → MarketActionFacts"""
    coin = state.coin
    now = int(time.time())
    facts = MarketActionFacts(coin=coin, timestamp=now)
    missing: list[str] = []

    # S 级 6
    facts.price = build_price_snapshot(state)
    if facts.price is None:
        missing.append("price")
    facts.oi = build_oi_snapshot(state)
    if facts.oi is None:
        missing.append("oi")
    facts.funding = build_funding_snapshot(state)
    if facts.funding is None:
        missing.append("funding")
    facts.cvd_contract = build_cvd_snapshot(state.cvd_contract)
    if facts.cvd_contract is None:
        missing.append("cvd_contract")
    facts.cvd_spot = build_cvd_snapshot(state.cvd_spot)
    if facts.cvd_spot is None:
        missing.append("cvd_spot")
    facts.liquidation_flow = build_liquidation_snapshot(state)
    if facts.liquidation_flow is None:
        missing.append("liquidation_flow")

    # A 级 9
    facts.basis = build_basis_snapshot(state)
    if facts.basis is None:
        missing.append("basis")
    facts.orderbook = build_orderbook_snapshot(state)
    if facts.orderbook is None:
        missing.append("orderbook")

    # liq_map_clusters（优先 LiquidationMap 预聚合簇，回退 HeatmapData.data）
    # heatmap key 已统一为 "24h"/"7d"（旧版 "m1_24h"/"m1_3d"/"3d" 已废弃；
    # liq_maps 周期为 "1d"/"7d"/"30d"，无 "3d"）。
    heatmap = None
    if state.liq_heatmaps:
        heatmap = (
            state.liq_heatmaps.get("24h")
            or state.liq_heatmaps.get("7d")
            or next(iter(state.liq_heatmaps.values()))
        )
    from models.liquidation import pick_primary_liq_map
    liq_map = pick_primary_liq_map(getattr(state, "liq_maps", None))
    facts.liq_map_clusters = build_cluster_snapshot(
        heatmap,
        state.ticker.last if state.ticker else None,
        liq_map=liq_map,
    )
    if facts.liq_map_clusters.above_cluster_usd == 0 and facts.liq_map_clusters.below_cluster_usd == 0:
        missing.append("liq_map_clusters")

    facts.liq_sweep_recent = build_sweep_snapshot(
        getattr(state, "liq_sweep_events", []), window_min=30,
    )

    # 派生标签
    price_ch_1h = _coherent_price_change(state)
    oi_ch_1h = _safe_float(state.oi.change_1h_pct) if state.oi else None
    facts.oi_price_coherence = derive_oi_price_coherence(oi_ch_1h, price_ch_1h)

    facts.spot_contract_coherence = derive_spot_contract_coherence(
        _safe_float(state.cvd_spot.delta_1h) if state.cvd_spot else None,
        _safe_float(state.cvd_contract.delta_1h) if state.cvd_contract else None,
        state.cvd_spot.trend_1h if state.cvd_spot else None,
        state.cvd_contract.trend_1h if state.cvd_contract else None,
    )

    funding_cur = facts.funding.avg_current if facts.funding else None
    funding_7d = facts.funding.avg_7d if facts.funding else None
    facts.funding_trend = derive_funding_trend(funding_cur, funding_7d)

    facts.price_context = build_price_context(
        state.ticker.last if state.ticker else None,
        state.candles_1h, state.candles_daily, state.vp,
    )

    fp_contract = list(getattr(state, "footprint_contract", []) or [])
    fp_spot = list(getattr(state, "footprint_spot", []) or [])
    facts.footprint = build_footprint_snapshot(
        fp_contract, fp_spot,
        coin=coin, bar_seconds=3600, now_ts=now,
    )
    if facts.footprint is None:
        missing.append("footprint")

    # A10 · Absorption（复用 footprint 数据，不新增 poll；AbsorptionSnapshot 永不 None）
    facts.absorption = build_absorption_snapshot(state)
    if facts.absorption.total_zone_count == 0:
        missing.append("absorption")

    # B 级 2
    facts.taker_flow_5m = build_taker_snapshot(state)
    if facts.taker_flow_5m is None:
        missing.append("taker_flow_5m")
    facts.options = build_options_snapshot(state)
    if facts.options is None and state.coin.upper() in ("BTC", "ETH"):
        missing.append("options")

    # 数据质量评级
    core_fields = ["price", "oi", "funding", "cvd_contract", "liquidation_flow"]
    missing_core = [m for m in missing if m in core_fields]
    quality: DataQuality
    if not missing_core:
        quality = "ok" if len(missing) <= 3 else "partial"
    elif len(missing_core) <= 2:
        quality = "partial"
    else:
        quality = "insufficient"

    facts.missing = missing
    facts.data_quality = quality

    # ── data_meta · 透明度元信息（哪些 bar 未收盘、用了哪些数据源）──
    provisional_fields: list[str] = []
    if facts.price is not None and facts.price.latest_bar_closed is False:
        provisional_fields.append("price.recent_bars_1h[last]")
    if facts.cvd_contract is not None and facts.cvd_contract.latest_bar_closed is False:
        provisional_fields.append("cvd_contract.recent_delta_5m[last]")
    if facts.cvd_spot is not None and facts.cvd_spot.latest_bar_closed is False:
        provisional_fields.append("cvd_spot.recent_delta_5m[last]")
    if facts.footprint is not None:
        if facts.footprint.contract_latest is not None and facts.footprint.contract_latest.bar_closed is False:
            provisional_fields.append("footprint.contract_latest")
        if facts.footprint.spot_latest is not None and facts.footprint.spot_latest.bar_closed is False:
            provisional_fields.append("footprint.spot_latest")

    sources_used: list[str] = []
    for fld in (
        "price", "oi", "funding", "cvd_contract", "cvd_spot",
        "liquidation_flow", "basis", "orderbook", "liq_map_clusters",
        "liq_sweep_recent", "price_context", "footprint", "absorption",
        "taker_flow_5m", "options",
    ):
        v = getattr(facts, fld, None)
        if v is None:
            continue
        if fld == "absorption" and getattr(v, "total_zone_count", 0) == 0:
            continue
        if fld == "liq_map_clusters" and (
            (v.above_cluster_usd or 0) == 0 and (v.below_cluster_usd or 0) == 0
        ):
            continue
        sources_used.append(fld)

    facts.data_meta = FactsDataMeta(
        generated_at=now,
        has_provisional_bars=bool(provisional_fields),
        sources_used=sources_used,
        provisional_fields=provisional_fields,
    )
    return facts
