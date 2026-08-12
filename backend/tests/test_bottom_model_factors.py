"""Bottom Model 因子引擎：原语、六因子弃权、双层评分、假底过滤、象限。"""
from __future__ import annotations

import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from processors.bottom_model.factors import (
    blended_percentile,
    build_counter_evidence,
    classify_quadrant,
    compute_confirmation,
    compute_factors,
    compute_fake_bottom_filter,
    compute_seller_exhaustion,
    compute_stress,
    drawdown_series,
    percentile_rank,
    ratio_series,
    weekly_higher_low,
)

END = date(2026, 8, 11)


def mk(values, end=END, step_days=1):
    """构造升序 [(day, value)]，末日为 end。"""
    n = len(values)
    return [
        ((end - timedelta(days=(n - 1 - i) * step_days)).strftime("%Y-%m-%d"), float(v))
        for i, v in enumerate(values)
    ]


# ── 原语 ──

def test_percentile_rank():
    values = [float(v) for v in range(1, 101)]
    assert percentile_rank(values, 100.0) == 100.0
    assert percentile_rank(values, 0.5) == 0.0


def test_blended_percentile_window_degradation():
    # 仅 200 天 → 3y/5y/全历史三窗口全部退化为同一窗口，权重重归一化
    rows = mk(range(200))
    bp = blended_percentile(rows)
    assert bp is not None and bp["pct"] == 100.0
    # 全部窗口样本 < 90 → None
    assert blended_percentile(mk(range(50))) is None


def test_blended_percentile_weekly_cadence():
    # 周级序列：窗口天数按 7 天/行换算——300 周（≈5.7 年）序列中，
    # 3y 窗口应只取最近 156 行而非 1095 行
    rows = mk(range(300), step_days=7)
    bp = blended_percentile(rows, cadence="weekly")
    assert bp is not None
    by_days = {w["days"]: w["n"] for w in bp["windows"]}
    assert by_days[1095] == 1095 // 7   # 3y 窗口 = 156 周
    assert by_days[1825] == 1825 // 7   # 5y 窗口 = 260 周
    assert by_days[None] == 300         # 全历史
    # 周级最小样本按比例放宽：30 周（约 200 天）不应整体 None
    assert blended_percentile(mk(range(30), step_days=7), cadence="weekly") is not None


def test_drawdown_and_ratio_series():
    rows = mk([100, 80, 120, 60])
    dd = drawdown_series(rows)
    assert [round(v, 2) for _, v in dd] == [0.0, 0.2, 0.0, 0.5]
    num, den = mk([10, 20]), mk([2, 4])
    assert [v for _, v in ratio_series(num, den)] == [5.0, 5.0]


def test_weekly_higher_low():
    # 最低周后连续抬高 → True
    lows = mk([50, 45, 40, 30, 33, 36, 39, 42], step_days=7)
    assert weekly_higher_low(lows) is True
    # 仍在创新低 → False
    lows2 = mk([50, 45, 40, 38, 35, 33, 31, 30], step_days=7)
    assert weekly_higher_low(lows2) is False
    assert weekly_higher_low(mk([1, 2, 3], step_days=7)) is None  # 样本不足


# ── 因子与 Stress ──

def _bottomish_data(n=1200):
    """合成一份"底部特征"数据：低估值、投降峰值已过且衰竭、OI 出清、企稳回升。

    价格：长升 →跌 200 天 →最后 50 天回升（不再创新低）。
    卖压（已实现亏损/清算）：90d 窗口早期爆量、近期衰减（卖方衰竭形态）。
    """
    up = list(range(30000, 30000 + n - 250))
    down = list(range(30000 + n - 250, 30000 + n - 450, -1))
    price = up + down + [down[-1] + 3 * i for i in range(50)]
    burst = [10.0] * (n - 90) + [100.0] * 30 + [8.0] * 60
    return {
        "mvrv_zscore": mk([5.0 - 4.8 * i / n for i in range(n)]),        # 降至低位
        "nupl": mk([0.7 - 0.65 * i / n for i in range(n)]),
        "reserve_risk": mk([0.02 - 0.019 * i / n for i in range(n)]),
        "sth_mvrv": mk([1.5 - 0.6 * i / n for i in range(n)]),
        "btc_price_onchain": mk(price),
        "ma_200w": mk([30000.0 + i for i in range(n)]),
        "realized_loss": mk([-(v * 1e6) for v in burst]),                 # 爆量后衰减
        "sopr": mk([1.0] * (n - 120) + [0.95] * 90 + [1.005] * 30),       # 跌破后收复
        "lth_sopr": mk([2.0 - 1.5 * i / n for i in range(n)]),
        "lth_realized_loss": mk([-(v * 1e5) for v in burst]),
        "sth_supply": mk([4e6 - 5e5 * i / n for i in range(n)]),
        "oi_agg_usd": mk([3e10] * (n - 90) + [2e10] * 90),                # 出清 33%
        "cme_oi_usd": mk([7e9] * (n - 90) + [5e9] * 90),
        "funding_oiw": mk([0.01] * (n - 120) + [-0.02] * 90 + [0.0] * 30),
        "liq_long_usd": mk(burst),
        "liq_short_usd": mk(burst),
        "cme_vol_1w": mk([1e4] * 400 + [9e4] * 4, step_days=7),           # 恐慌周量
        "etf_flow_usd": mk([-1e8] * 60 + [5e7] * 7),                      # 流出后转正
        "stablecoin_total_mcap": mk([2e11 + 1e8 * i for i in range(n)]),
        "exchange_balance_btc": mk([3e6 - 1e3 * i for i in range(n)]),    # 持续流出
        "btc_low_1w": mk([50, 45, 40, 30, 33, 36, 39, 42], step_days=7),
        "btc_close_1d": mk(price[-1000:]),
        "sth_realized_price": mk([p * 0.98 for p in price]),              # 价格站上 STH RP
        "global_m2_yoy": mk([5.0 - 4.0 * i / 600 for i in range(590)] + [1.2] * 10,
                            step_days=7),
        "fear_greed": mk([50.0] * (n - 30) + [15.0] * 30),                # 极端恐惧持续
    }


def test_factors_and_stress_bottomish():
    data = _bottomish_data()
    factors = compute_factors(data)
    assert len(factors) == 6
    by_key = {f["key"]: f for f in factors}
    assert by_key["valuation"]["score"] > 60      # 低估值 → 高分
    assert by_key["capitulation"]["score"] > 55   # 投降后衰竭
    assert by_key["leverage"]["score"] > 60       # 出清充分
    stress = compute_stress(factors)
    assert stress is not None and stress["score"] > 55
    assert stress["abstained"] == []


def test_factor_abstention_and_stress_none():
    factors = compute_factors({})  # 无任何数据
    assert all(f["score"] is None for f in factors)
    assert compute_stress(factors) is None


def test_confirmation_and_fake_filter_bottomish():
    data = _bottomish_data()
    conf = compute_confirmation(data)
    assert conf["score"] is not None and conf["score"] >= 60  # 多项确认共振
    filt = compute_fake_bottom_filter(data)
    trigger_keys = {t["key"] for t in filt["triggers"]}
    assert "oi_rebuild" not in trigger_keys       # OI 是出清不是回堆
    assert "price_new_low" not in trigger_keys    # 未创新低


def test_fake_filter_triggers_on_new_low_and_oi_rebuild():
    n = 400
    falling = [50000.0 - 30 * i for i in range(n)]  # 一路新低
    data = {
        "btc_close_1d": mk(falling),
        "btc_price_onchain": mk(falling),
        "oi_agg_usd": mk([2e10] * (n - 30) + [2.5e10] * 30),  # 30d +25% 回堆
        "etf_flow_usd": mk([-1e8] * 60),                       # 持续流出
    }
    filt = compute_fake_bottom_filter(data)
    keys = {t["key"] for t in filt["triggers"]}
    assert {"price_new_low", "oi_rebuild", "etf_outflow"} <= keys
    assert filt["total_penalty"] >= 45


def test_seller_exhaustion_and_counter_evidence():
    data = _bottomish_data()
    se = compute_seller_exhaustion(data)
    assert se is not None and se["score"] > 60
    factors = compute_factors(data)
    filt = compute_fake_bottom_filter(data)
    ce = build_counter_evidence(factors, filt)
    assert len(ce["supporting"]) >= 3
    assert isinstance(ce["opposing"], list)
    assert compute_seller_exhaustion({}) is None


def test_classify_quadrant():
    assert classify_quadrant(None, None)["key"] == "unknown"
    assert classify_quadrant(40, 20)["key"] == "bear_market"
    assert classify_quadrant(70, 20)["key"] == "panic_flush"
    assert classify_quadrant(70, 50)["key"] == "basing"
    assert classify_quadrant(70, 80)["key"] == "confirmed_recovery"
    assert classify_quadrant(70, None)["key"] == "panic_flush"
