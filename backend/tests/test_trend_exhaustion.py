"""趋势衰竭 processor 单元测试（v2：regime-aware + 踩踏识别 + 硬门闸 + 白话）

覆盖点：
    子项数学：
        - m1 MACD 二阶导：加速 / 拐头 两分支
        - m2 价格斜率 z：强趋势期放宽、climax 共振才衰竭
        - m3 RSI 区间：强趋势期 RSI75+ 给正分
        - m4 FVG 持续度
        - p1 CVD 同向/反向/absorption
        - p2 OI×Price 踩踏识别（真多 / 空头回补 / 踩踏）
        - p3 CB 溢价（BTC-only）
        - e1 TD Sequential 9+
    单周期聚合：
        - 趋势期健康上涨 → healthy_continuation（direction=up）
        - 冲顶背离 + TD9 + 硬门闸（需要 2 tick 才放行 close）
    MTF 共识：
        - 4h+1d 同向健康 → strong_agree
        - 冲突 → stand_aside
    Regime veto：
        - regime=range / high_vol_chop → 强制 stand_aside
    白话生成：
        - up+healthy_continuation → "还在涨，动能健康"
"""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from models.flow import CVDData, CVDPoint, OIData  # noqa: E402
from processors.trend_exhaustion import (  # noqa: E402
    _Context,
    _confirm_or_downgrade,
    _e1_td_sequential,
    _e4_liq_cluster_fuel,
    _m1_macd_hist_accel,
    _m1_macd_second_derivative,  # legacy shim
    _m3_rsi_zone,
    _p1_cvd_momentum,
    _p2_oi_price_alignment,  # legacy shim
    _p2_oi_price_confluence,
    _p3_coinbase_premium_flow,
    _p4_funding_extreme,
    _plain_cn,
    _resolve_consensus,
    compute_trend_exhaustion,
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 构造工具
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _mk_candles(closes: list[float], vol: float = 100.0, vol_spike_last: bool = False):
    res = []
    for i, c in enumerate(closes):
        h = c * 1.005
        lo = c * 0.995
        o = closes[i - 1] if i > 0 else c
        v = vol * (6.0 if (vol_spike_last and i == len(closes) - 1) else 1.0)
        res.append(SimpleNamespace(ts=i * 3600, open=o, high=h, low=lo, close=c, vol=v))
    return res


def _linear_up(n: int, start: float = 100.0, step: float = 0.5):
    return [start + step * i for i in range(n)]


def _linear_down(n: int, start: float = 200.0, step: float = 0.5):
    return [start - step * i for i in range(n)]


def _make_state(
    closes_1h,
    closes_4h=None,
    closes_1d=None,
    *,
    cvd_matches_price=True,
    oi_chg_1h=1.0,
    td_count=None,
    td_direction="",
    regime: str = "trend_up",
    direction_1d: str = "bullish",
    coin: str = "BTC",
    vol_spike_last_1h: bool = False,
    global_liq_short_1h: float = 0.0,
    global_liq_long_1h: float = 0.0,
    coinbase_premium: float | None = None,
    funding_rate: float | None = None,   # 小数形式如 0.0005=0.05%
    liq_map=None,                         # 传入伪造 LiquidationMap
):
    """构造最小 CoinState-like 对象（含 v2 所需 regime + structure）。"""
    candles_1h = _mk_candles(closes_1h, vol_spike_last=vol_spike_last_1h)
    candles_4h = _mk_candles(closes_4h) if closes_4h else []
    candles_1d = _mk_candles(closes_1d) if closes_1d else []

    if cvd_matches_price:
        cvd_vals = [c - closes_1h[0] for c in closes_1h]
    else:
        cvd_vals = [closes_1h[0] - c for c in closes_1h]
    cvd = CVDData(
        coin=coin,
        inst_type="CONTRACTS",
        series=[CVDPoint(ts=i, buy_vol=0, sell_vol=0, delta=0, cvd=v)
                for i, v in enumerate(cvd_vals)],
    )

    oi = OIData(coin=coin, ts=0, current_usd=1e9, change_1h_pct=oi_chg_1h)
    ticker = SimpleNamespace(last=closes_1h[-1])

    regime_features = SimpleNamespace(atr_pct=1.2, atr_pct_percentile=0.5)
    regime_snapshot = SimpleNamespace(regime=regime, features=regime_features)

    market_structure_1d = SimpleNamespace(direction=direction_1d)
    market_structure = SimpleNamespace(direction=direction_1d)

    taker_flow = SimpleNamespace(buy_ratio=0.55 if cvd_matches_price else 0.45)
    global_liq = SimpleNamespace(
        long_1h_usd=global_liq_long_1h, short_1h_usd=global_liq_short_1h,
        long_24h_usd=0, short_24h_usd=0,
    )

    coinbase_prem_obj = None
    if coinbase_premium is not None:
        coinbase_prem_obj = SimpleNamespace(current_premium=coinbase_premium)

    funding_obj = None
    multi_funding_obj = None
    if funding_rate is not None:
        funding_obj = SimpleNamespace(
            coin=coin, ts=0, okx_rate=funding_rate, binance_rate=funding_rate,
            avg_rate=funding_rate, oi_weighted_rate=funding_rate,
            next_funding_ts=0, interpretation="",
        )
        multi_funding_obj = SimpleNamespace(
            coin=coin, ts=0, exchanges=[],
            avg_current=funding_rate, avg_7d=funding_rate, oi_weighted=funding_rate,
            interpretation="",
        )

    liq_maps = {"1d": liq_map} if liq_map is not None else {}

    return SimpleNamespace(
        coin=coin,
        ticker=ticker,
        candles_1h=candles_1h,
        candles_4h=candles_4h,
        candles_daily=candles_1d,
        cvd_contract=cvd,
        cvd_spot=None,
        oi=oi,
        td_sequential_count=td_count,
        td_sequential_direction=td_direction,
        regime_snapshot=regime_snapshot,
        market_structure_1d=market_structure_1d,
        market_structure=market_structure,
        taker_flow=taker_flow,
        global_liq=global_liq,
        coinbase_premium=coinbase_prem_obj,
        funding=funding_obj,
        multi_funding=multi_funding_obj,
        liq_maps=liq_maps,
        trend_exhaustion=None,
    )


def _mk_liq_map(above_specs, below_specs, price_ref: float = 100.0):
    """构造最小 LiquidationMap-like 对象。
    specs = [(distance_pct, total_usd), ...]   distance_pct>0 = 远离当前价
    """
    def _c(dp, usd, side):
        sign = 1 if side == "short" else -1
        center = price_ref * (1 + sign * dp / 100.0)
        return SimpleNamespace(
            price_center=center, price_from=center * 0.99, price_to=center * 1.01,
            total_usd=float(usd), side=side, dominant_leverage="50",
            distance_pct=float(dp),
        )
    return SimpleNamespace(
        coin="BTC", ts=0, cycle="1d", leverage_groups=[],
        clusters_above=[_c(dp, u, "short") for dp, u in above_specs],
        clusters_below=[_c(dp, u, "long") for dp, u in below_specs],
        vacuum_zones=[], imbalance_ratio=0, exchange="",
    )


def _ctx_up_trend() -> _Context:
    return _Context(tf="1h", regime="trend_up", direction="up")


def _ctx_down_trend() -> _Context:
    return _Context(tf="1h", regime="trend_down", direction="down")


def _ctx_range() -> _Context:
    return _Context(tf="1h", regime="range", direction="up")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 子项测试
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_m1_macd_acceleration_up():
    closes = _linear_up(60, start=100.0, step=1.0)
    for i in range(1, 10):
        closes.append(closes[-1] + 1.5 + 0.2 * i)
    score, _, _ = _m1_macd_hist_accel(closes, _ctx_up_trend())
    assert score > 0, f"上涨加速 direction=up 应该给正分，得到 {score}"


def test_m1_macd_turning_to_down():
    """上涨 → 长期下跌，direction 仍是 up → raw_score 负 → signed 负 → 衰竭。"""
    closes = _linear_up(50, start=100.0, step=1.0)
    for i in range(40):
        closes.append(closes[-1] - 1.0 - 0.1 * i if i < 20 else closes[-1] - 0.5 + 0.05 * (i - 20))
    score, note, _ = _m1_macd_hist_accel(closes, _ctx_up_trend())
    assert score < 0.1, f"direction=up 但已转跌，应给衰竭负分，得到 {score} note={note}"


def test_m1_legacy_shim_still_works():
    """旧 _m1_macd_second_derivative 向下兼容。"""
    closes = _linear_up(70, start=100.0, step=1.0)
    score, _, _ = _m1_macd_second_derivative(closes)
    assert score > 0


def test_m3_rsi_in_strong_trend_is_positive():
    """趋势期 RSI 75+ → 正分（强势加速），不再判衰竭。"""
    # 构造 RSI > 75 的上涨序列
    closes = _linear_up(30, start=100.0, step=0.5)
    closes += [closes[-1] + 2.0 + 0.3 * i for i in range(20)]
    score_trend, note_trend, rsi = _m3_rsi_zone(closes, _ctx_up_trend())
    score_range, note_range, _ = _m3_rsi_zone(closes, _ctx_range())
    assert rsi is not None and rsi >= 70, f"期望 RSI 偏高，实际 {rsi}"
    assert score_trend > score_range, (
        f"趋势期应给更高分，trend={score_trend} ({note_trend}) range={score_range} ({note_range})"
    )


def test_p1_cvd_divergence_gives_negative():
    closes = _linear_up(20, start=100.0, step=1.0)
    cvd = _linear_down(20, start=0.0, step=1.0)  # 价涨 CVD 跌
    score, note, _ = _p1_cvd_momentum(closes, cvd, _ctx_up_trend())
    assert score < -0.5, f"量价背离应强负，得到 {score} ({note})"


def test_p2_oi_price_real_rally():
    score, note, _ = _p2_oi_price_confluence(
        price_now=110.0, price_1h_ago=100.0, oi_change_1h_pct=2.0, ctx=_ctx_up_trend(),
    )
    assert score > 0.5 and "真多" in note


def test_p2_oi_price_short_covering_fake_rally():
    """涨 1% + OI -1%（温和减仓）→ 空头回补假涨，应给负分。"""
    score, note, _ = _p2_oi_price_confluence(
        price_now=110.0, price_1h_ago=100.0, oi_change_1h_pct=-1.0, ctx=_ctx_up_trend(),
    )
    assert score < 0, f"空头回补假涨应负分，得到 {score} ({note})"
    assert "空头回补" in note


def test_p2_oi_price_short_squeeze_is_real_bullish():
    """涨 1% + OI -3% + 空头爆仓 5000 万 → 踩踏真多，正分。"""
    ctx = _ctx_up_trend()
    ctx.global_liq_short_1h = 5e7
    score, note, _ = _p2_oi_price_confluence(
        price_now=110.0, price_1h_ago=100.0, oi_change_1h_pct=-3.0, ctx=ctx,
    )
    assert score > 0.4, f"空头踩踏应给较强正分，得到 {score} ({note})"
    assert "踩踏" in note


def test_p2_oi_price_long_squeeze_is_real_bearish():
    """跌 + OI 大跌 + 多头爆仓 → 多头踩踏，direction=down 时应给"顺势"正分。"""
    ctx = _ctx_down_trend()
    ctx.global_liq_long_1h = 5e7
    score, note, _ = _p2_oi_price_confluence(
        price_now=90.0, price_1h_ago=100.0, oi_change_1h_pct=-3.0, ctx=ctx,
    )
    assert score > 0.4, (
        f"direction=down 下多头踩踏 = 顺势续航，应正分，得到 {score} ({note})"
    )
    assert "踩踏" in note


def test_p2_legacy_shim():
    score, _, _ = _p2_oi_price_alignment(price_now=110.0, price_1h_ago=100.0, oi_change_1h_pct=2.0)
    assert score > 0.5


def test_p3_coinbase_premium_btc_only():
    ctx_btc = _Context(tf="1d", regime="trend_up", direction="up", is_btc=True, coinbase_premium=40.0)
    ctx_alt = _Context(tf="1d", regime="trend_up", direction="up", is_btc=False, coinbase_premium=40.0)
    s_btc, _, _ = _p3_coinbase_premium_flow(ctx_btc)
    s_alt, _, _ = _p3_coinbase_premium_flow(ctx_alt)
    assert s_btc > 0.5 and s_alt == 0.0


def test_e1_td_sell_9_multi_dir():
    """direction=up 时 td_sell_9 = 多头衰竭 → 负分；direction=down 时 = 空头"反向"（正分）。"""
    s_up, _, _ = _e1_td_sequential(td_count=9, td_direction="sell", ctx=_ctx_up_trend())
    s_down, _, _ = _e1_td_sequential(td_count=9, td_direction="sell", ctx=_ctx_down_trend())
    assert s_up <= -0.8
    assert s_down >= 0.8


def test_e1_td_empty():
    s, _, _ = _e1_td_sequential(td_count=None, td_direction="", ctx=_ctx_up_trend())
    assert s == 0.0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Phase 3：p4 资金费率 + e4 清算簇磁吸
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_p4_funding_extreme_long_crowded_in_uptrend():
    """上涨趋势 + 资金费率 +30 bp 极端多头拥挤 → 衰竭信号（趋势期降权后仍显著为负）。"""
    ctx = _Context(tf="1d", regime="trend_up", direction="up", funding_bp_8h=30.0)
    s, note, val = _p4_funding_extreme(ctx)
    assert s < -0.2, f"极端多头拥挤（趋势期）应给明显衰竭负分，得到 {s} ({note})"
    assert "极端" in note and val == 30.0


def test_p4_funding_extreme_short_crowded_is_bullish_fuel():
    """上涨趋势 + 资金费率 -25 bp（空头极端拥挤）→ 对向上是正面（反向指标）。"""
    ctx = _Context(tf="1d", regime="trend_up", direction="up", funding_bp_8h=-25.0)
    s, note, _ = _p4_funding_extreme(ctx)
    assert s > 0.2, f"空头极端拥挤应为续航正分，得到 {s} ({note})"


def test_p4_funding_extreme_same_signal_in_downtrend_is_inverted():
    """下跌趋势 + 资金费率 -25 bp（空头拥挤）→ 对 direction=down 来说 = 衰竭。"""
    ctx_down = _Context(tf="1d", regime="trend_down", direction="down", funding_bp_8h=-25.0)
    s, note, _ = _p4_funding_extreme(ctx_down)
    assert s < -0.1, f"下跌趋势中空头拥挤 = 衰竭前兆，应负分，得到 {s} ({note})"


def test_p4_funding_neutral_range():
    ctx = _Context(tf="1d", regime="trend_up", direction="up", funding_bp_8h=2.0)
    s, _, _ = _p4_funding_extreme(ctx)
    assert s == 0.0


def test_p4_funding_missing_returns_zero():
    ctx = _Context(tf="1d", regime="trend_up", direction="up", funding_bp_8h=None)
    s, _, _ = _p4_funding_extreme(ctx)
    assert s == 0.0


def test_e4_liq_fuel_near_big_cluster_supports_continuation():
    """上涨 + 顺势 3% 处有 60% 占比的大空头清算簇 → 磁吸续航 +0.5。"""
    liq_map = _mk_liq_map(
        above_specs=[(3.0, 6e8), (15.0, 1e8), (25.0, 3e8)],  # 近端大簇
        below_specs=[(5.0, 1e8)],
    )
    ctx = _Context(tf="1d", regime="trend_up", direction="up", liq_map=liq_map)
    s, note, ratio = _e4_liq_cluster_fuel(price=100.0, ctx=ctx)
    assert s > 0.3, f"近端大簇应续航正分，得到 {s} ({note})"
    assert "磁吸" in note and ratio is not None and ratio >= 0.5


def test_e4_liq_fuel_no_near_cluster_signals_exhaustion():
    """上涨 + 顺势 10% 内簇总量极小 → 燃料耗尽 -0.4。"""
    liq_map = _mk_liq_map(
        above_specs=[(25.0, 5e8), (30.0, 3e8)],  # 远端簇
        below_specs=[(5.0, 1e8)],
    )
    ctx = _Context(tf="1d", regime="trend_up", direction="up", liq_map=liq_map)
    s, note, _ = _e4_liq_cluster_fuel(price=100.0, ctx=ctx)
    assert s < -0.2, f"近端无簇应衰竭负分，得到 {s} ({note})"
    assert "耗尽" in note


def test_e4_liq_fuel_downtrend_mirror():
    """下跌趋势 + 下方近端大多头清算簇 → 对 direction=down 续航正分。"""
    liq_map = _mk_liq_map(
        above_specs=[(10.0, 1e8)],
        below_specs=[(3.0, 6e8), (15.0, 1e8), (25.0, 3e8)],
    )
    ctx = _Context(tf="1d", regime="trend_down", direction="down", liq_map=liq_map)
    s, _, _ = _e4_liq_cluster_fuel(price=100.0, ctx=ctx)
    assert s > 0.3, "下跌趋势 + 下方近端大簇，应续航正分"


def test_e4_liq_fuel_flat_direction_returns_zero():
    liq_map = _mk_liq_map(above_specs=[(3.0, 6e8)], below_specs=[(3.0, 6e8)])
    ctx = _Context(tf="1d", regime="range", direction="flat", liq_map=liq_map)
    s, _, _ = _e4_liq_cluster_fuel(price=100.0, ctx=ctx)
    assert s == 0.0


def test_e4_liq_fuel_missing_map_returns_zero():
    ctx = _Context(tf="1d", regime="trend_up", direction="up", liq_map=None)
    s, _, _ = _e4_liq_cluster_fuel(price=100.0, ctx=ctx)
    assert s == 0.0


def test_end_to_end_phase3_features_appear_in_signal():
    """集成测试：funding + liq_map 都提供时，1d 的 sub_scores 应包含 p4 + e4。"""
    closes_1h = _linear_up(80, start=100.0, step=0.3)
    for _ in range(10):
        closes_1h.append(closes_1h[-1] + 0.5)
    last = closes_1h[-1]
    liq_map = _mk_liq_map(
        above_specs=[(3.0, 6e8), (20.0, 3e8)],
        below_specs=[(4.0, 2e8)],
        price_ref=last,
    )
    state = _make_state(
        closes_1h=closes_1h,
        closes_4h=closes_1h[-60:],
        closes_1d=closes_1h[-50:],
        cvd_matches_price=True,
        oi_chg_1h=1.5,
        regime="trend_up",
        direction_1d="bullish",
        funding_rate=0.0003,  # 3 bp，小幅多头偏拥挤
        liq_map=liq_map,
    )
    sig = compute_trend_exhaustion(state)
    assert sig is not None and sig.tf_1d is not None
    keys = {s.key for s in sig.tf_1d.sub_scores}
    assert "p4_funding" in keys, f"1d 应包含 p4_funding，实际 {keys}"
    assert "e4_liq_fuel" in keys, f"1d 应包含 e4_liq_fuel，实际 {keys}"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 单周期 + 主入口
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_healthy_uptrend_full_pipeline():
    closes_1h = _linear_up(80, start=100.0, step=0.3)
    for _ in range(10):
        closes_1h.append(closes_1h[-1] + 0.5)
    state = _make_state(
        closes_1h=closes_1h,
        closes_4h=closes_1h[-60:],
        closes_1d=closes_1h[-50:],
        cvd_matches_price=True,
        oi_chg_1h=1.5,
        regime="trend_up",
        direction_1d="bullish",
    )
    sig = compute_trend_exhaustion(state)
    assert sig is not None
    assert sig.tf_1h is not None
    assert sig.tf_1h.momentum_score > 0 or sig.tf_1h.participation_score > 0
    assert sig.overall_action != "close"
    assert sig.overall_direction == "up"
    assert "涨" in sig.overall_plain_cn or "续" in sig.overall_plain_cn or sig.overall_state != "neutral"


def test_healthy_downtrend_full_pipeline():
    """18w → 7.3w 场景：direction=down + 价格持续下跌 + CVD 同向。"""
    closes_1h = _linear_down(80, start=200.0, step=0.5)
    for _ in range(10):
        closes_1h.append(closes_1h[-1] - 0.8)
    state = _make_state(
        closes_1h=closes_1h,
        closes_4h=closes_1h[-60:],
        closes_1d=closes_1h[-50:],
        cvd_matches_price=True,  # CVD 与价同向（同下跌）
        oi_chg_1h=1.2,             # 跌+OI 加仓 = 真空
        regime="trend_down",
        direction_1d="bearish",
    )
    sig = compute_trend_exhaustion(state)
    assert sig is not None
    assert sig.overall_direction == "down"
    # 跌势中同向 + OI 加仓 = 健康空头续航，不应建议 add 或 close
    assert sig.overall_action not in ("add", "close")


def test_range_regime_forces_stand_aside():
    closes_1h = _linear_up(80, start=100.0, step=0.05)  # 微幅
    state = _make_state(
        closes_1h=closes_1h,
        closes_4h=closes_1h[-60:],
        closes_1d=closes_1h[-50:],
        regime="range",
        direction_1d="ranging",
    )
    sig = compute_trend_exhaustion(state)
    assert sig is not None
    assert sig.regime_vetoed is True
    assert sig.overall_action == "stand_aside"
    assert "震荡" in sig.overall_plain_cn or "无趋势" in sig.overall_plain_cn or "趋势" in sig.overall_plain_cn


def test_high_vol_chop_also_vetoed():
    closes_1h = _linear_up(80, start=100.0, step=0.1)
    state = _make_state(
        closes_1h=closes_1h,
        closes_4h=closes_1h[-60:],
        closes_1d=closes_1h[-50:],
        regime="high_vol_chop",
        direction_1d="transitioning",
    )
    sig = compute_trend_exhaustion(state)
    assert sig is not None and sig.overall_action == "stand_aside"
    assert sig.regime_vetoed is True


def test_insufficient_data():
    closes = _linear_up(10, start=100.0, step=0.5)
    state = _make_state(closes_1h=closes)
    sig = compute_trend_exhaustion(state)
    assert sig is not None
    assert sig.data_quality == "insufficient"
    assert sig.overall_action == "stand_aside"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 硬门闸（2 tick 确认）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_hard_gate_blocks_first_tick_exhaustion():
    from models.trend_exhaustion import TrendExhaustionState
    curr = TrendExhaustionState(tf="4h", state="exhaustion_warn", action_hint="close",
                                composite_score=-0.5, direction="up")
    gated = _confirm_or_downgrade(curr, prev=None)
    assert gated is not None
    # 首次出现 exhaustion_warn → 降级为 momentum_fading
    assert gated.state == "momentum_fading"
    assert gated.action_hint == "reduce"
    assert gated.confirmed_ticks == 1


def test_hard_gate_passes_on_second_tick():
    from models.trend_exhaustion import TrendExhaustionState
    prev = TrendExhaustionState(tf="4h", state="exhaustion_warn", action_hint="reduce",
                                composite_score=-0.5, direction="up", confirmed_ticks=1)
    curr = TrendExhaustionState(tf="4h", state="exhaustion_warn", action_hint="close",
                                composite_score=-0.6, direction="up")
    gated = _confirm_or_downgrade(curr, prev)
    assert gated is not None
    # 第二次 → 放行
    assert gated.state == "exhaustion_warn"
    assert gated.action_hint == "close"
    assert gated.confirmed_ticks == 2


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# MTF 共识
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _mk_te_state(tf, composite, state_name, direction="up"):
    from models.trend_exhaustion import TrendExhaustionState
    return TrendExhaustionState(
        tf=tf,
        direction=direction,
        composite_score=composite,
        state=state_name,
        momentum_score=composite * 0.9,
        participation_score=composite * 0.8,
        exhaustion_score=composite * 0.7,
    )


def test_consensus_strong_agree_up():
    tf_1h = _mk_te_state("1h", 0.5, "healthy_continuation")
    tf_4h = _mk_te_state("4h", 0.6, "healthy_continuation")
    tf_1d = _mk_te_state("1d", 0.55, "healthy_continuation")
    level, state, action, pos, _ = _resolve_consensus(tf_1h, tf_4h, tf_1d)
    assert level == "strong_agree" and state == "healthy_continuation"
    assert action == "add" and pos > 0.5


def test_consensus_conflict():
    tf_1h = _mk_te_state("1h", 0.6, "healthy_continuation")
    tf_4h = _mk_te_state("4h", -0.6, "exhaustion_warn", "up")
    tf_1d = _mk_te_state("1d", -0.5, "momentum_fading", "up")
    level, _, action, pos, _ = _resolve_consensus(tf_1h, tf_4h, tf_1d)
    assert level == "conflict" and action == "stand_aside" and pos == 0.0


def test_consensus_strong_exhaustion():
    tf_1h = _mk_te_state("1h", -0.3, "momentum_fading")
    tf_4h = _mk_te_state("4h", -0.6, "exhaustion_warn")
    tf_1d = _mk_te_state("1d", -0.5, "exhaustion_warn")
    level, state, action, _pos, _ = _resolve_consensus(tf_1h, tf_4h, tf_1d)
    assert level == "strong_agree" and state == "exhaustion_warn" and action == "close"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 白话
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_plain_cn_coverage():
    p1, t1 = _plain_cn("up", "healthy_continuation", False)
    assert "涨" in p1 and "健康" in p1
    assert "持有" in t1 or "加仓" in t1

    p2, t2 = _plain_cn("down", "momentum_fading", False)
    assert "跌" in p2 and ("慢" in p2 or "减" in p2)

    p3, t3 = _plain_cn("up", "exhaustion_warn", False)
    assert ("涨不动" in p3) or ("竭" in p3)
    assert "离场" in t3 or "观望" in t3

    p4, t4 = _plain_cn("up", "healthy_continuation", True)  # regime veto
    assert "震荡" in p4 or "极端" in p4
    assert "空仓" in t4 or "不要" in t4


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"✓ {name}")
    print("\nAll trend_exhaustion v2 tests passed.")
