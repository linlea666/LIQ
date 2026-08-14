from __future__ import annotations

from engine import CoinState
from models.flow import CVDData, CVDPoint, FundingRateData, OIData
from processors.market_read import build_market_read_from_state


NOW = 1_800_000_000


def _cvd(trend: str, *, age_sec: int = 400) -> CVDData:
    latest = NOW - age_sec
    points = [
        CVDPoint(
            ts=latest - (11 - i) * 300,
            buy_vol=200 if trend == "rising" else 100,
            sell_vol=100 if trend == "rising" else 200,
            delta=100 if trend == "rising" else -100,
            cvd=(i + 1) * (100 if trend == "rising" else -100),
        )
        for i in range(12)
    ]
    return CVDData(
        coin="BTC", inst_type="SPOT", ts=latest, series=points,
        trend_1h=trend,
    )


def _state(
    *, spot: str = "rising", contract: str = "declining",
    rank_oi: float = -0.62, local_oi: float = -0.62,
    funding_decimal: float = 0.000059,
    cvd_age_sec: int = 400,
) -> CoinState:
    state = CoinState("BTC")
    state.cvd_spot = _cvd(spot, age_sec=cvd_age_sec)
    state.cvd_spot.inst_type = "SPOT"
    state.cvd_contract = _cvd(contract, age_sec=cvd_age_sec)
    state.cvd_contract.inst_type = "CONTRACTS"
    state.oi = OIData(
        coin="BTC", ts=NOW - 30, current_usd=48_000_000_000,
        change_1h_pct=local_oi, change_5m_pct=0,
    )
    state.oi_exchange_rank = {
        "ts": NOW - 60,
        "all_aggregated": {"change_1h_pct": rank_oi},
    }
    state.funding = FundingRateData(
        coin="BTC", ts=NOW - 30,
        avg_rate=funding_decimal,
        interpretation="中性",
    )
    return state


def test_production_like_split_is_small_oi_change_not_deleveraging():
    out = build_market_read_from_state(_state(), now_ts=NOW)
    read = out["market_read"]
    assert out["oi_delta_1h_pct"] == -0.62
    assert read.title == "资金流分化 · 现货偏强"
    assert read.bias == "bullish"
    assert read.leverage_state == "small_change"
    assert "短线分化偏多" in read.summary
    assert "持仓小幅减少" in read.summary
    assert "杠杆退潮" not in read.summary
    assert "底部" not in read.summary


def test_deleveraging_requires_inclusive_minus_one_percent():
    out = build_market_read_from_state(
        _state(rank_oi=-1.0, local_oi=-1.0), now_ts=NOW,
    )
    assert out["market_read"].leverage_state == "deleveraging"
    assert out["market_read"].title == "现货偏强 · 杠杆退潮"
    assert "杠杆退潮" in out["market_read"].summary


def test_leverage_building_requires_inclusive_plus_one_percent():
    out = build_market_read_from_state(
        _state(rank_oi=1.0, local_oi=1.0), now_ts=NOW,
    )
    assert out["market_read"].leverage_state == "leverage_building"
    assert "杠杆升温" in out["market_read"].summary


def test_stale_core_cvd_blocks_directional_read():
    out = build_market_read_from_state(
        _state(cvd_age_sec=601), now_ts=NOW,
    )
    read = out["market_read"]
    assert read.bias == "insufficient"
    assert read.evidence_grade == "insufficient"
    assert "暂不判断偏多偏空" in read.summary


def test_open_five_minute_bar_is_provisional_and_weak():
    out = build_market_read_from_state(
        _state(cvd_age_sec=60), now_ts=NOW,
    )
    read = out["market_read"]
    assert out["source_meta"]["cvd_spot"].status == "pending"
    assert read.evidence_grade == "weak"
    assert any("尚未收盘" in item for item in read.cautions)


def test_oi_endpoint_direction_conflict_fails_closed():
    out = build_market_read_from_state(
        _state(rank_oi=1.2, local_oi=-1.3), now_ts=NOW,
    )
    read = out["market_read"]
    assert read.leverage_state == "conflict"
    assert "口径方向冲突" in read.summary


def test_funding_crowding_is_warning_not_direction_flip():
    long_crowded = build_market_read_from_state(
        _state(funding_decimal=0.0005), now_ts=NOW,
    )["market_read"]
    assert long_crowded.funding_state == "long_crowded"
    assert long_crowded.bias == "bullish"

    short_crowded = build_market_read_from_state(
        _state(funding_decimal=-0.0002), now_ts=NOW,
    )["market_read"]
    assert short_crowded.funding_state == "short_crowded"
    assert short_crowded.bias == "bullish"
