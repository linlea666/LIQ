from __future__ import annotations

import time

from engine import CoinState
from models.flow import CVDData, CVDPoint, FundingRateData, OIData
from models.liquidation import LiqCluster, LiquidationMap
from models.market import CandleData, TickerData
from processors.smc import build_smc_facts, build_smc_snapshot


def _candle(ts: int, o: float, h: float, l: float, c: float, vol: float = 1000) -> CandleData:
    return CandleData(coin="BTC", ts=ts, o=o, h=h, l=l, c=c, vol=vol)


def _state_with_pattern() -> CoinState:
    st = CoinState("BTC")
    base = int(time.time()) - 100 * 900
    closes = [
        100, 98, 99, 96, 97, 94, 95, 92, 93, 90, 91, 88,
        89, 86, 88, 85, 84.2, 87.5, 91, 96, 101, 99, 98, 97,
        95, 94, 93, 92, 91, 89, 90, 86.2, 88.5, 92, 96, 102,
    ]
    bars = []
    prev = closes[0]
    for i, close in enumerate(closes):
        wick = 1.0
        if i == 31:
            wick = 3.0
        high = max(prev, close) + wick
        low = min(prev, close) - wick
        bars.append(_candle(base + i * 900, prev, high, low, close, 1000 + i * 100))
        prev = close
    st.candles_15m = bars
    st.candles_1h = bars[::4]
    st.candles_4h = bars[::8]
    st.candles_daily = bars[::12]
    st.ticker = TickerData(
        coin="BTC", ts=base + len(bars) * 900, last=102,
        high_24h=104, low_24h=82, vol_24h=1_000_000,
    )
    st.liq_maps["1d"] = LiquidationMap(
        coin="BTC",
        ts=base,
        cycle="1d",
        leverage_groups=[],
        clusters_below=[
            LiqCluster(
                price_center=85,
                price_from=84.5,
                price_to=85.5,
                total_usd=25_000_000,
                side="long",
            )
        ],
        clusters_above=[
            LiqCluster(
                price_center=106,
                price_from=105.5,
                price_to=106.5,
                total_usd=30_000_000,
                side="short",
            )
        ],
    )
    st.liq_maps["7d"] = st.liq_maps["1d"].model_copy(update={"cycle": "7d"})
    st.cvd_contract = CVDData(
        coin="BTC",
        inst_type="CONTRACTS",
        series=[CVDPoint(ts=base, buy_vol=10, sell_vol=5, delta=5, cvd=5)],
        delta_1h=18_000_000,
    )
    st.cvd_spot = CVDData(
        coin="BTC",
        inst_type="SPOT",
        series=[CVDPoint(ts=base, buy_vol=8, sell_vol=4, delta=4, cvd=4)],
        delta_1h=10_000_000,
    )
    st.oi = OIData(coin="BTC", ts=base, current_usd=1_000_000_000, change_1h_pct=1.2)
    st.funding = FundingRateData(coin="BTC", ts=base, avg_rate=-0.0002, oi_weighted_rate=-0.0002)
    st.nansen_perp = {
        "token_symbol": "BTC",
        "smart_money_buy_volume": 12_000_000,
        "smart_money_sell_volume": 4_000_000,
        "net_position_change": 3_000_000,
    }
    st.nansen_flow_intelligence = {
        "token_symbol": "WBTC",
        "smart_trader_net_flow_usd": 2_000_000,
        "top_pnl_net_flow_usd": 1_500_000,
        "whale_net_flow_usd": 500_000,
    }
    st.nansen_exchange_flows = [
        {
            "date": "2026-05-15T01:00:00",
            "price_usd": 100_000,
            "total_inflows_cex": 2.0,
            "total_outflows_cex": -1.0,
            "total_inflows_dex": 0.0,
            "total_outflows_dex": 0.0,
        },
        {
            "date": "2026-05-22T01:00:00",
            "price_usd": 100_000,
            "total_inflows_cex": 1.0,
            "total_outflows_cex": -101.0,
            "total_inflows_dex": 0.0,
            "total_outflows_dex": 0.0,
        },
    ]
    st.nansen_updated_at = {"perp_screener": base, "flow_intelligence": base, "exchange_flows": base}
    return st


def test_smc_detects_raid_mss_and_entry_candidates():
    snap = build_smc_snapshot(_state_with_pattern(), horizon="intraday")

    assert snap.coin == "BTC"
    assert snap.horizon == "intraday"
    assert any(e.kind == "liquidity_raid" for e in snap.structure)
    assert any(e.kind in {"bos", "mss"} for e in snap.structure)
    assert any(z.kind in {"order_block", "fair_value_gap", "fib_ote"} for z in snap.zones)
    assert snap.observation in {"long_watch", "wait"}
    assert snap.data_quality.status in {"ok", "partial"}


def test_smc_horizon_periods_do_not_mix_intraday_and_swing():
    st = _state_with_pattern()
    intraday = build_smc_snapshot(st, horizon="intraday")
    swing = build_smc_snapshot(st, horizon="swing")

    assert "24h" in intraday.timeframe_map["active"]
    assert "7d" not in intraday.timeframe_map["active"]
    assert "7d" in swing.timeframe_map["active"]
    assert "30d" in swing.timeframe_map["active"]


def test_smc_degrades_without_nansen_but_still_returns_snapshot():
    st = _state_with_pattern()
    st.nansen_perp = None
    st.nansen_flow_intelligence = None

    snap = build_smc_snapshot(st, horizon="intraday")

    assert snap.smart_money.status == "missing"
    assert "nansen" in snap.data_quality.degraded
    assert snap.last_price > 0


def test_smc_fund_flow_tracks_exchange_outflow_as_bullish_confirmation():
    snap = build_smc_snapshot(_state_with_pattern(), horizon="swing")

    assert snap.fund_flow.status in {"ok", "partial"}
    assert snap.fund_flow.bias == "bullish"
    assert snap.fund_flow.seven_day is not None
    assert snap.fund_flow.seven_day.cex_net_token < 0
    assert snap.fund_flow.seven_day.cex_net_usd_approx is not None
    assert snap.fund_flow.seven_day.cex_net_usd_approx < 0
    assert any(item.source.startswith("nansen_exchange_flow") for item in snap.confirmations)


def test_smc_facts_expose_field_map_and_forbidden_contract():
    facts = build_smc_facts(_state_with_pattern(), horizon="intraday")

    assert facts["field_map"]
    forbidden = set(facts["forbidden_inputs"])
    assert "key_level_snapshot_v2" in forbidden
    assert "market_action_report" in forbidden
