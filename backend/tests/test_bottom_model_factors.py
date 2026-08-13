"""Bottom Model 因子引擎：原语、六因子弃权、双层评分、假底过滤、象限。"""
from __future__ import annotations

import os
import sys
from datetime import date, timedelta

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from processors.bottom_model.correlation import compute_correlation_audit, pearson
from processors.bottom_model.factors import (
    PROXY_CME_VOL,
    SeriesIndex,
    blended_percentile,
    build_counter_evidence,
    change_rate_series,
    classify_quadrant,
    compute_confirmation,
    compute_factors,
    compute_fake_bottom_filter,
    compute_seller_exhaustion,
    compute_stress,
    drawdown_series,
    evidence_quality,
    median_gap_days,
    oi_flush_ratio,
    percentile_rank,
    ratio_series,
    rolling_mean_series,
    weekly_higher_high,
    weekly_higher_low,
    weekly_structure_stage,
    window_coverage,
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


# ── 证据质量 EQ ──

def test_evidence_quality_span_multiplier():
    # 跨度 8 年（两个 BTC 周期）= 满分；2 年跨度触及 0.30 下限
    eight_years = mk(range(3000))
    eq, note = evidence_quality(eight_years, as_of=END.strftime("%Y-%m-%d"))
    assert eq == 100.0 and "→1.00" in note
    two_years = mk(range(2 * 365))
    eq2, _ = evidence_quality(two_years, as_of=END.strftime("%Y-%m-%d"))
    assert eq2 == 30.0
    # 四年史（BGeometrics 量级）落在中段，明显低于 2009 起的链上指标
    four_years = mk(range(4 * 365))
    eq4, _ = evidence_quality(four_years, as_of=END.strftime("%Y-%m-%d"))
    assert 45.0 <= eq4 <= 55.0
    assert evidence_quality([])[0] is None


def test_evidence_quality_freshness_and_proxy():
    rows = mk(range(3000))
    # 日级序列滞后 2 天以内不罚；滞后到 6 倍间隔触及 0.50 下限
    assert evidence_quality(rows, as_of="2026-08-13")[0] == 100.0
    assert evidence_quality(rows, as_of="2026-08-17")[0] == 50.0
    # 周级序列 gap=7，滞后 10 天仍在 2×gap 内 → 不罚
    weekly = mk(range(500), step_days=7)
    assert evidence_quality(weekly, as_of="2026-08-21")[0] == 100.0
    # as_of 缺省 = 不做新鲜度折扣（历史类比传入各自截断日）
    assert evidence_quality(rows)[0] == 100.0
    eq_proxy, note = evidence_quality(rows, as_of="2026-08-11", proxy=PROXY_CME_VOL)
    assert eq_proxy == 80.0 and "代理 0.80" in note


def test_window_coverage_and_median_gap():
    # 样本充足 → 三窗口全达标；样本不足以估计分布 → 0（分位本身也会返回 None）
    assert window_coverage(mk(range(1200))) == 1.0
    assert window_coverage(mk(range(50))) == 0.0
    assert blended_percentile(mk(range(50))) is None
    assert window_coverage([]) == 0.0
    # 周级最小样本按比例放宽
    assert window_coverage(mk(range(60), step_days=7), cadence="weekly") == 1.0
    assert median_gap_days(mk(range(20))) == 1.0
    assert median_gap_days(mk(range(20), step_days=7)) == 7.0


def test_factor_weights_by_evidence_quality():
    """短窗口子信号应被自动降权：同样的分数，历史越短影响越小。"""
    n = 1200
    base = {
        "nupl": mk([0.7 - 0.65 * i / n for i in range(n)]),        # 16 年不可得，
        "reserve_risk": mk([0.02 - 0.019 * i / n for i in range(n)]),
    }
    factors = {f["key"]: f for f in compute_factors(base, END.strftime("%Y-%m-%d"))}
    valuation = factors["valuation"]
    eqs = {s["key"]: s["evidence_quality"] for s in valuation["sub_signals"] if s["ok"]}
    # 两个子信号跨度相同（合成数据 1200 天）→ EQ 相同且低于满分
    assert all(eq is not None and eq < 60 for eq in eqs.values())
    assert valuation["evidence_quality"] == pytest.approx(
        sum(eqs.values()) / len(eqs), abs=0.1,
    )


# ── 周线结构分阶段 ──

def test_weekly_higher_high():
    # 最低周后：前半段高点 45，后半段突破 → True
    lows = mk([50, 45, 40, 30, 33, 36, 39, 42], step_days=7)
    highs = mk([55, 50, 45, 35, 40, 42, 48, 52], step_days=7)
    assert weekly_higher_high(lows, highs) is True
    # 反弹高点持续走低 → False
    highs2 = mk([55, 50, 45, 35, 44, 43, 38, 37], step_days=7)
    assert weekly_higher_high(lows, highs2) is False
    assert weekly_higher_high(mk([1, 2, 3], step_days=7), mk([1, 2, 3], step_days=7)) is None


def test_weekly_structure_stage_grades():
    highs = mk([55, 50, 45, 35, 40, 42, 48, 52], step_days=7)
    rising = mk([50, 45, 40, 30, 33, 36, 39, 42], step_days=7)
    falling = mk([50, 45, 40, 38, 35, 33, 31, 30], step_days=7)

    # 阶段 0：仍在创新低
    stage = weekly_structure_stage({"btc_low_1w": falling, "btc_high_1w": highs})
    assert stage["stage"] == 0 and stage["score"] == 10.0

    # 阶段 2：已形成 HL，但价格仍低于 STH 成本线与 200W 均线
    below = {"btc_low_1w": rising, "btc_high_1w": highs,
             "btc_price_onchain": mk([90.0]), "sth_realized_price": mk([120.0]),
             "ma_200w": mk([130.0])}
    assert weekly_structure_stage(below)["stage"] == 2

    # 阶段 3：HL + 收复成本线，但无更高高点
    flat_highs = mk([55, 50, 45, 35, 44, 43, 38, 37], step_days=7)
    reclaimed = {**below, "btc_high_1w": flat_highs, "btc_price_onchain": mk([150.0])}
    stage3 = weekly_structure_stage(reclaimed)
    assert stage3["stage"] == 3 and stage3["score"] == 75.0

    # 阶段 4：HL + HH + 站上成本线
    stage4 = weekly_structure_stage({**reclaimed, "btc_high_1w": highs})
    assert stage4["stage"] == 4 and stage4["score"] == 100.0
    assert weekly_structure_stage({}) is None


def test_weekly_hl_not_double_counted_in_structure_factor():
    """周线结构只应出现在确认层，Stress 的结构因子不得再计一次。"""
    data = _bottomish_data()
    factors = {f["key"]: f for f in compute_factors(data)}
    sub_keys = {s["key"] for s in factors["structure"]["sub_signals"]}
    assert "weekly_hl" not in sub_keys
    check_keys = {c["key"] for c in compute_confirmation(data)["checks"]}
    assert "structure_stage" in check_keys and "weekly_hl" not in check_keys


# ── 资金费 × OI 制度矩阵 ──

def _funding_oi(funding_tail, oi_tail):
    """构造 120 天资金费 + 60 天 OI 序列，尾部按传入形态覆盖。"""
    funding = [0.01] * (120 - len(funding_tail)) + list(funding_tail)
    oi = [3e10] * (60 - len(oi_tail)) + list(oi_tail)
    return {"funding_oiw": mk(funding), "oi_agg_usd": mk(oi)}


def _regime(data):
    checks = {c["key"]: c for c in compute_confirmation(data)["checks"]}
    return checks["funding_oi_regime"]


def test_funding_oi_regime_branches():
    recovering = [-0.02] * 60 + [0.0] * 30    # 90d 有负极端，14d 均回到 0
    extreme = [-0.02] * 90                    # 仍处负极端

    # 1. 资金费回升 + OI 未回堆 = 健康正常化
    healthy = _regime(_funding_oi(recovering, [3e10] * 31 + [2.4e10] * 29))
    assert healthy["score"] == 100.0

    # 2. 资金费回升但 OI 30d 增幅 > 15% = 杠杆重新堆积（本轮新增的关键修正）
    rebuild = _regime(_funding_oi(recovering, [2e10] * 31 + [2.6e10] * 29))
    assert rebuild["score"] == 25.0 and "杠杆重新堆积" in rebuild["note"]

    # 3. 资金费仍为负 + OI 下降 = 恐慌出清进行中（压力事件，非确认）
    flushing = _regime(_funding_oi(extreme, [3e10] * 31 + [2.4e10] * 29))
    assert flushing["score"] == 40.0

    # 4. 资金费仍为负 + OI 上升 = 空头累积
    shorts = _regime(_funding_oi(extreme, [2e10] * 31 + [2.6e10] * 29))
    assert shorts["score"] == 35.0

    # 5. 两者均平稳
    calm = _regime(_funding_oi([0.001] * 90, [3e10] * 60))
    assert calm["score"] == 70.0

    # OI 缺失时退化为资金费单边判定，并在备注中说明
    no_oi = _regime({"funding_oiw": mk([0.01] * 30 + [-0.02] * 60 + [0.0] * 30)})
    assert no_oi["score"] == 70.0 and "OI 数据不足" in no_oi["note"]


def test_funding_shallow_dip_is_not_normalization_and_note_uses_percent_unit():
    """funding_oiw 单位是百分比/8h：贴近零的负值不算负极端，备注不得放大 100 倍。

    真实数据里 90d 低点仅 -0.0008%（历史 10% 分位上方），旧口径会把它当成
    "从负极端回升"并给出 100 分的健康正常化确认。
    """
    shallow = _regime(_funding_oi([-0.0008] * 30 + [0.005] * 60, [3e10] * 60))
    assert shallow["score"] == 70.0
    assert "-0.0008%" in shallow["note"] and "0.0050%/8h" in shallow["note"]

    deep = _regime(_funding_oi([-0.006] * 30 + [0.005] * 60,
                               [3e10] * 31 + [2.4e10] * 29))
    assert deep["score"] == 100.0


def test_oi_flush_ratio_shared_helper():
    rows = mk([3e10] * 100 + [2.1e10] * 20)
    assert oi_flush_ratio(rows) == pytest.approx(0.30, abs=1e-6)
    assert oi_flush_ratio(mk([1e10] * 10)) is None    # 样本不足


# ── 相关性审计 ──

def test_correlation_audit_flags_valuation_cluster():
    data = _bottomish_data()
    audit = compute_correlation_audit(data)
    groups = {g["key"]: g for g in audit["groups"]}
    # 合成数据中估值簇均为同向线性趋势 → 必然高度相关
    assert groups["valuation"]["max_abs_rho"] >= 0.9
    assert groups["valuation"]["strong_pairs"] >= 1
    # 结构性冗余只做声明，不给相关系数（避免同义反复的假精度）
    assert audit["structural_redundancies"]
    assert all("rho" not in item for item in audit["structural_redundancies"])
    assert len(audit["cross_layer_overlaps"]) >= 3


def test_pearson_requires_overlap():
    assert pearson(mk(range(100)), mk(range(100)))[0] == pytest.approx(1.0)
    assert pearson(mk([5.0] * 100), mk(range(100))) is None   # 方差为 0
    assert pearson(mk(range(10)), mk(range(10))) is None      # 重叠不足


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
    # v4：note 必须带时间尺度限定——同一象限在 13 周与 52 周的表现可能相反
    for stress, conf in ((40, 20), (70, 20), (70, 50), (70, 80)):
        assert "周" in classify_quadrant(stress, conf)["note"]


# ── v4：现货需求子信号 ──

def _spot_demand_data(n=1200):
    """在底部合成数据上补两个现货需求序列：长期净卖出，最近 30 天转为净买入。"""
    data = _bottomish_data(n)
    data["coinbase_premium_rate"] = mk([-0.2] * (n - 30) + [0.35] * 30)
    data["spot_net_taker_usd"] = mk([-5e6] * (n - 30) + [8e6] * 30)
    return data


def test_demand_spot_subsignals_score_and_coverage():
    demand = next(f for f in compute_factors(_spot_demand_data()) if f["key"] == "demand")
    subs = {s["key"]: s for s in demand["sub_signals"]}
    assert len(demand["sub_signals"]) == 5
    # 30d 均值恰好处在历史最高 → 分位接近满分
    assert subs["coinbase_premium"]["score"] > 90
    assert subs["spot_net_taker"]["score"] > 90
    assert demand["coverage"] == 1.0
    # 缺这两个序列时覆盖率下降，但因子不弃权（3/5 仍高于 0.4 门槛）
    bare = next(f for f in compute_factors(_bottomish_data()) if f["key"] == "demand")
    assert 0.4 <= bare["coverage"] < 1.0
    assert bare["score"] is not None


def test_demand_spot_subsignals_evidence_quality_tracks_span():
    """合成数据只有 3.3 年，跨度乘子应把 EQ 压到满分之下——EQ 反映的是窗口而非读数。"""
    short = next(f for f in compute_factors(_spot_demand_data(1200)) if f["key"] == "demand")
    long = next(f for f in compute_factors(_spot_demand_data(3000)) if f["key"] == "demand")
    eq_of = lambda factor, key: next(  # noqa: E731
        s["evidence_quality"] for s in factor["sub_signals"] if s["key"] == key
    )
    for key in ("coinbase_premium", "spot_net_taker"):
        assert eq_of(short, key) < 60
        assert eq_of(long, key) > eq_of(short, key)


def test_mean_pct_sub_abstains_when_series_too_short():
    data = _bottomish_data()
    data["coinbase_premium_rate"] = mk([0.1] * 10)      # 不足 30 天滚动窗口
    demand = next(f for f in compute_factors(data) if f["key"] == "demand")
    sub = next(s for s in demand["sub_signals"] if s["key"] == "coinbase_premium")
    assert sub["ok"] is False and sub["score"] is None


def test_demand_cluster_correlation_declared():
    """需求簇必须进相关性审计：四个指标都被当作"谁在买"的证据。"""
    audit = compute_correlation_audit(_spot_demand_data())
    group = next(g for g in audit["groups"] if g["key"] == "demand")
    metrics = {pair["a"] for pair in group["pairs"]} | {pair["b"] for pair in group["pairs"]}
    assert {"Coinbase 溢价", "现货净 taker"} <= metrics
    # 稳定币走的是 30d 增速而非市值水平，否则单调增长会造出伪相关
    assert "稳定币 30d 增速" in metrics


# ── v4：序列原语 ──

def test_change_rate_series_and_rolling_mean():
    rows = mk([100.0, 110.0, 121.0, 133.1])
    assert [round(v, 4) for _, v in change_rate_series(rows, 1)] == [0.1, 0.1, 0.1]
    assert change_rate_series(mk([0.0, 5.0]), 1) == []      # 基准≈0 跳过
    means = rolling_mean_series(mk([1.0, 2.0, 3.0, 4.0]), 2)
    assert [v for _, v in means] == [1.5, 2.5, 3.5]
    assert rolling_mean_series(mk([1.0]), 2) == []


def test_series_index_truncate_matches_linear_filter():
    data = {"a": mk([1.0, 2.0, 3.0, 4.0]), "b": mk([9.0])}
    index = SeriesIndex(data)
    for offset in range(6):
        day = (END - timedelta(days=offset)).strftime("%Y-%m-%d")
        expected = {m: [(d, v) for d, v in rows if d <= day] for m, rows in data.items()}
        assert index.truncate(day) == expected
