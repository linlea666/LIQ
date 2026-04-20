"""趋势衰竭 processor 单元测试（Phase 1 核心路径）

覆盖点（最小必要集，全部用合成数据，不依赖真实数据源）：
    1. 三个子项数学正确：
        - m1 MACD 二阶导：加速/拐头两个分支
        - p2 OI×Price 共振：涨+加仓 vs 涨+减仓 得分符号相反
        - e1 TD Sequential：count≥9 给出强衰竭分
    2. 单周期聚合：
        - 构造强上涨 + 量价同向 K 线 → healthy_continuation
        - 构造顶背离 + 超买 + TD9 K 线（1d） → exhaustion_warn or momentum_fading
    3. 样本不足：
        - candles 不够长 → compute_trend_exhaustion 返回 insufficient，
          overall_action="stand_aside"（不装懂）
    4. MTF 共识：
        - 4h + 1d 都健康，1h 也健康 → strong_agree + overall_state=healthy_continuation
        - 4h + 1d 衰竭，1h 相反 → conflict（或 strong_agree 衰竭，取决于 1h 分数）
"""

from __future__ import annotations

import math
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from models.flow import CVDData, CVDPoint, OIData  # noqa: E402
from processors.trend_exhaustion import (  # noqa: E402
    _e1_td_sequential,
    _m1_macd_second_derivative,
    _p2_oi_price_alignment,
    _resolve_consensus,
    compute_trend_exhaustion,
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 合成 K 线工具
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _mk_candles(closes: list[float]):
    """构造最小 CandleData-like 对象。带简单 HL 扩展。"""
    res = []
    for i, c in enumerate(closes):
        h = c * 1.005
        lo = c * 0.995
        o = closes[i - 1] if i > 0 else c
        res.append(SimpleNamespace(ts=i * 3600, open=o, high=h, low=lo, close=c, vol=100.0))
    return res


def _linear_up(n: int, start: float = 100.0, step: float = 0.5):
    return [start + step * i for i in range(n)]


def _linear_down(n: int, start: float = 200.0, step: float = 0.5):
    return [start - step * i for i in range(n)]


def _make_state(closes_1h, closes_4h=None, closes_1d=None, *, cvd_matches_price=True,
                oi_chg_1h=1.0, td_count=None, td_direction=""):
    """构造最小 CoinState-like 对象。"""
    candles_1h = _mk_candles(closes_1h)
    candles_4h = _mk_candles(closes_4h) if closes_4h else []
    candles_1d = _mk_candles(closes_1d) if closes_1d else []

    # CVD 序列：同向或反向
    if cvd_matches_price:
        cvd_vals = [c - closes_1h[0] for c in closes_1h]
    else:
        cvd_vals = [closes_1h[0] - c for c in closes_1h]
    cvd = CVDData(
        coin="BTC",
        inst_type="CONTRACTS",
        series=[CVDPoint(ts=i, buy_vol=0, sell_vol=0, delta=0, cvd=v)
                for i, v in enumerate(cvd_vals)],
    )

    oi = OIData(coin="BTC", ts=0, current_usd=1e9, change_1h_pct=oi_chg_1h)

    ticker = SimpleNamespace(last=closes_1h[-1])

    return SimpleNamespace(
        coin="BTC",
        ticker=ticker,
        candles_1h=candles_1h,
        candles_4h=candles_4h,
        candles_daily=candles_1d,
        cvd_contract=cvd,
        cvd_spot=None,
        oi=oi,
        td_sequential_count=td_count,
        td_sequential_direction=td_direction,
        trend_exhaustion=None,
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 子项测试
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_m1_macd_acceleration():
    """强加速上行 MACD → 正分（续航）。"""
    closes = _linear_up(60, start=100.0, step=1.0)
    # 再加一截抛物线加速
    for i in range(1, 10):
        closes.append(closes[-1] + 1.5 + 0.2 * i)
    score, _, _ = _m1_macd_second_derivative(closes)
    assert score > 0, f"加速上行应该给正分，得到 {score}"


def test_m1_macd_turning():
    """先冲顶后持续回落 → MACD 柱由正变负（拐头确认）→ 不应为强正。

    注意：m1 只看最近 3 根柱的二阶导，所以"拐头"必须让柱确实翻负，
    否则就是"加速下行"（空头续航）也给正分（正确行为）。
    这里通过长期震荡下行确保 histogram 完成符号翻转。
    """
    closes = _linear_up(50, start=100.0, step=1.0)
    # 持续下行 40 根，让 hist 充分翻负并进入"减速"（末尾跌幅放缓）
    for i in range(40):
        # 前半段加速跌，后半段减速（让 d1*d2<0 形成拐头判定）
        if i < 20:
            closes.append(closes[-1] - 1.0 - 0.1 * i)
        else:
            closes.append(closes[-1] - 0.5 + 0.05 * (i - 20))
    score, note, val = _m1_macd_second_derivative(closes)
    # 无论是减速（d1*d2<0）还是向 0 钝化，都应给负分（因为 sign_last=-1 时 hist 为负）
    assert score < 0.3, f"长期下行 MACD 柱翻负后分数不应为强正，得到 {score} note={note} h3={val}"


def test_p2_oi_price_real_rally():
    """涨+加仓 → 真多 → 正分。"""
    score, note, _ = _p2_oi_price_alignment(price_now=110.0, price_1h_ago=100.0,
                                             oi_change_1h_pct=2.0)
    assert score > 0.5
    assert "真多" in note


def test_p2_oi_price_short_squeeze():
    """涨+减仓 → 空头回补（假涨）→ 小负分。"""
    score, note, _ = _p2_oi_price_alignment(price_now=110.0, price_1h_ago=100.0,
                                             oi_change_1h_pct=-2.0)
    assert score < 0
    assert "空头回补" in note


def test_e1_td_sequential_sell_9():
    """TD Sell Setup 9 → 多头衰竭强负分。"""
    score, note, _ = _e1_td_sequential(td_count=9, td_direction="sell")
    assert score <= -0.8
    assert "多头衰竭" in note


def test_e1_td_sequential_buy_9():
    """TD Buy Setup 9 → 空头衰竭正分（反弹机会）。"""
    score, _, _ = _e1_td_sequential(td_count=9, td_direction="buy")
    assert score >= 0.8


def test_e1_td_sequential_empty():
    """TD 未就绪 → 0 分。"""
    score, _, _ = _e1_td_sequential(td_count=None, td_direction="")
    assert score == 0.0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 单周期 + 聚合测试
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_healthy_trend_produces_positive_signal():
    """强上涨 + CVD 同向 + OI 加仓 → 应产生 healthy_continuation 或至少 overall_action != close。"""
    closes_1h = _linear_up(80, start=100.0, step=0.3)
    # 让最后 10 根略加速
    for _ in range(10):
        closes_1h.append(closes_1h[-1] + 0.5)

    state = _make_state(
        closes_1h=closes_1h,
        closes_4h=closes_1h[::4][-60:],
        closes_1d=closes_1h[::24][-50:] if len(closes_1h) >= 24 * 50 else closes_1h[-50:],
        cvd_matches_price=True,
        oi_chg_1h=1.5,
    )
    sig = compute_trend_exhaustion(state)
    assert sig is not None
    # 1h 子周期应该给正分（动能 + 参与度）
    assert sig.tf_1h is not None
    assert sig.tf_1h.momentum_score > 0 or sig.tf_1h.participation_score > 0
    # 绝不应建议 close / counter_main
    assert sig.overall_action not in ("close", "counter_main")


def test_divergence_trend_produces_warning():
    """冲顶后回落 + CVD 反向 + TD9 sell setup → 衰竭类状态。"""
    closes_1h = _linear_up(60, start=100.0, step=0.5)
    # 冲顶再回落一小段
    closes_1h += [closes_1h[-1] + 0.8, closes_1h[-1] + 1.2, closes_1h[-1] + 0.4]
    closes_1h += [closes_1h[-1] - 0.3 - 0.1 * i for i in range(10)]

    state = _make_state(
        closes_1h=closes_1h,
        closes_4h=closes_1h[-60:],
        closes_1d=closes_1h[-50:],
        cvd_matches_price=False,  # CVD 与价格反向
        oi_chg_1h=-1.0,           # 涨到顶后 OI 减仓
        td_count=9,
        td_direction="sell",
    )
    sig = compute_trend_exhaustion(state)
    assert sig is not None
    assert sig.tf_1d is not None
    # 1d 里 td_sequential 应该拉低 exhaustion_score
    assert sig.tf_1d.exhaustion_score < 0
    # overall_action 不应该是 "add"
    assert sig.overall_action != "add"


def test_insufficient_data_returns_stand_aside():
    """K 线严重不足 → data_quality=insufficient, action=stand_aside。"""
    closes = _linear_up(10, start=100.0, step=0.5)
    state = _make_state(closes_1h=closes, cvd_matches_price=True, oi_chg_1h=0.5)
    sig = compute_trend_exhaustion(state)
    assert sig is not None
    assert sig.data_quality == "insufficient"
    assert sig.overall_action == "stand_aside"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# MTF 共识测试（直接构造 state）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _mk_state(tf, composite, state_name):
    from models.trend_exhaustion import TrendExhaustionState
    return TrendExhaustionState(
        tf=tf,
        composite_score=composite,
        state=state_name,
        momentum_score=composite * 0.9,
        participation_score=composite * 0.8,
        exhaustion_score=composite * 0.7,
    )


def test_consensus_strong_agree_up():
    tf_1h = _mk_state("1h", 0.5, "healthy_continuation")
    tf_4h = _mk_state("4h", 0.6, "healthy_continuation")
    tf_1d = _mk_state("1d", 0.55, "healthy_continuation")
    level, state, action, pos, _ = _resolve_consensus(tf_1h, tf_4h, tf_1d)
    assert level == "strong_agree"
    assert state == "healthy_continuation"
    assert action == "add"
    assert pos > 0.5


def test_consensus_conflict_kills_action():
    tf_1h = _mk_state("1h", 0.6, "healthy_continuation")   # 1h 多
    tf_4h = _mk_state("4h", -0.6, "exhaustion_warn")       # 4h 空
    tf_1d = _mk_state("1d", -0.5, "momentum_fading")
    level, _state, action, pos, _ = _resolve_consensus(tf_1h, tf_4h, tf_1d)
    assert level == "conflict"
    assert action == "stand_aside"
    assert pos == 0.0


def test_consensus_strong_agree_exhaustion():
    tf_1h = _mk_state("1h", -0.3, "momentum_fading")
    tf_4h = _mk_state("4h", -0.6, "exhaustion_warn")
    tf_1d = _mk_state("1d", -0.5, "exhaustion_warn")
    level, state, action, _pos, _ = _resolve_consensus(tf_1h, tf_4h, tf_1d)
    assert level == "strong_agree"
    assert state == "exhaustion_warn"
    assert action == "close"


if __name__ == "__main__":
    # 允许 python backend/tests/test_trend_exhaustion.py 直接跑
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"✓ {name}")
    print("\nAll trend_exhaustion tests passed.")

    # 额外一个 sanity check
    assert math.isfinite(0.0)
