"""NOFX 接口契约回归测试 (Schema v1.0.0)

核心目标：
  1. 顶层与 snapshot 必填字段全部存在（NOFX 端 prompt 依赖字段名）
  2. **加工层字段坚决不出现**（trend_exhaustion / market_structure /
     direction_vote / temperature / range_signal / execution_plan / ...）
  3. ready=True 时 ticker 为正常 dict-like，不抛异常
  4. recent_sweeps_1h 保留原始事件结构
  5. JSON 可序列化（NOFX 用 json.Unmarshal 反序列化）
"""

from __future__ import annotations

import json
import os
import sys
import time
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from api.nofx_builder import SCHEMA_VERSION, build_nofx_snapshot
from models.flow import (
    CVDData, CVDPoint, ETFFlowData, ETFFlowDay, ExchangeFundingRate,
    FundingRateData, GlobalLiquidationData, LongShortRatioData,
    LongShortRatioExchange, MarketIndexData, MultiFundingRateData, OIData,
    OISnapshot, TakerFlowData,
)
from models.liquidation import (
    HeatmapData, HeatmapDataPoint, LiqCluster, LiqLeverageGroup,
    LiquidationMap, LiquidationStats, VacuumZone,
)
from models.macro import CoinbasePremiumData, StablecoinMcapData, StablecoinMcapPoint
from models.market import OrderBookAnalysis, TickerData, VolumeProfileData, WallInfo
from models.orderbook_ext import LargeOrder, LargeOrderSnapshot
from models.options import OptionInfoData, OptionMaxPainData, OptionMaxPainExpiry
from models.market import CandleData
from models.flow import CyclePositionData
from models.whale import HyperliquidWhaleAlert, WhaleData, WhaleTransfer


def _make_state(coin: str = "BTC", price: float = 76234.5):
    """构造一个尽量"满"的 CoinState 替身，覆盖所有 builder 分支。"""
    now = int(time.time())

    candles_1h = [
        CandleData(
            coin=coin, ts=now - i * 3600, o=price, h=price + 50,
            l=price - 50, c=price, vol=1000.0, vol_ccy=0.0,
        )
        for i in range(50, 0, -1)
    ]
    candles_15m = [
        CandleData(
            coin=coin, ts=now - i * 900, o=price, h=price + 20,
            l=price - 20, c=price, vol=200.0, vol_ccy=0.0,
        )
        for i in range(30, 0, -1)
    ]

    cvd_contract = CVDData(
        coin=coin, inst_type="CONTRACTS",
        series=[CVDPoint(ts=now - 300, buy_vol=100, sell_vol=120, delta=-20, cvd=1500.0)],
        delta_1h=-1250.3, has_divergence=True,
    )

    oi = OIData(
        coin=coin, ts=now - 30, current_usd=12.3e9,
        change_5m_pct=0.12, change_1h_pct=-0.42,
        history=[
            OISnapshot(coin=coin, ts=now - i * 600, oi=100.0, oi_usd=12.3e9)
            for i in range(5)
        ],
    )

    funding = FundingRateData(
        coin=coin, ts=now - 60, okx_rate=0.0089, binance_rate=0.0091,
        avg_rate=0.0090, oi_weighted_rate=0.0090, next_funding_ts=now + 1800,
    )
    multi_funding = MultiFundingRateData(
        coin=coin, ts=now - 60,
        exchanges=[
            ExchangeFundingRate(exchange="OKX", current=0.0089, avg_3d=0.008, avg_7d=0.0072, avg_30d=0.0065),
            ExchangeFundingRate(exchange="Binance", current=0.0091, avg_3d=0.0079, avg_7d=0.0070, avg_30d=0.0062),
        ],
        avg_current=0.0090, avg_7d=0.0071, oi_weighted=0.0090,
    )

    ls_global = LongShortRatioData(
        coin=coin, ts=now - 30, cycle="1h", dimension="global",
        exchanges=[
            LongShortRatioExchange(exchange="Binance", long_pct=55.0, short_pct=45.0, ratio=1.22),
        ],
        avg_ratio=1.23,
    )
    ls_top_acct = LongShortRatioData(
        coin=coin, ts=now - 30, cycle="1h", dimension="top_account",
        exchanges=[], avg_ratio=1.05,
    )
    ls_top_pos = LongShortRatioData(
        coin=coin, ts=now - 30, cycle="1h", dimension="top_position",
        exchanges=[], avg_ratio=0.92,
    )

    liq_map_24h = LiquidationMap(
        coin=coin, ts=now - 60, cycle="1d",
        leverage_groups=[
            LiqLeverageGroup(leverage="50", short_bands=[], long_bands=[], short_total_usd=1e8, long_total_usd=1.2e8),
        ],
        clusters_above=[
            LiqCluster(price_center=78500, price_from=78300, price_to=78700,
                       total_usd=3.2e8, side="short", dominant_leverage="50", distance_pct=2.97),
        ],
        clusters_below=[
            LiqCluster(price_center=74800, price_from=74600, price_to=75000,
                       total_usd=2.8e8, side="long", dominant_leverage="25", distance_pct=-1.88),
        ],
        vacuum_zones=[VacuumZone(price_from=80100, price_to=81200, midpoint=80650, note="")],
        imbalance_ratio=1.18,
    )

    heatmap = HeatmapData(
        coin=coin, ts=now - 120, model=1, range="24h",
        data=[
            HeatmapDataPoint(price=78900, value=4.2e8, ts=now - 1800),
            HeatmapDataPoint(price=75100, value=3.6e8, ts=now - 2400),
            HeatmapDataPoint(price=80000, value=2.1e8, ts=now - 3600),
        ],
    )

    orderbook = OrderBookAnalysis(
        coin=coin, ts=now - 30,
        bid_walls=[WallInfo(price=75100, size=0, size_usd=8.5e6, order_count=0)],
        ask_walls=[WallInfo(price=78000, size=0, size_usd=7.2e6, order_count=0)],
        bid_total_usd=2.4e8, ask_total_usd=1.95e8, spread_pct=0.012,
    )

    large_orders = LargeOrderSnapshot(
        symbol="BTCUSDT", ts=now - 60,
        orders=[
            LargeOrder(ts=now - 100, exchange="Binance", symbol="BTCUSDT",
                       price=76200, size_usd=2.5e6, side="bid", status="active"),
            LargeOrder(ts=now - 200, exchange="OKX", symbol="BTCUSDT",
                       price=77800, size_usd=1.8e6, side="ask", status="active"),
        ],
        total_bid_usd=1.8e7, total_ask_usd=1.38e7,
    )

    whale_data = WhaleData(
        ts=now - 90,
        hl_alerts=[
            HyperliquidWhaleAlert(ts=now - 200, symbol=coin, side="long",
                                  size_usd=5e6, entry_price=76100, address="0xabc",
                                  action="open"),
        ],
        hl_positions=[],
        transfers=[
            WhaleTransfer(ts=now - 300, symbol=coin, amount=100, amount_usd=7.6e6,
                          from_label="unknown", to_label="binance", blockchain="bitcoin"),
            WhaleTransfer(ts=now - 400, symbol=coin, amount=50, amount_usd=3.8e6,
                          from_label="binance", to_label="unknown", blockchain="bitcoin"),
        ],
    )

    etf_flow = ETFFlowData(
        ts=now - 1800, asset="BTC",
        recent_days=[
            ETFFlowDay(date="2026-04-22", total_net=-1.2e7, detail={}),
            ETFFlowDay(date="2026-04-21", total_net=-8.5e6, detail={}),
        ],
        net_3d=-1.25e8,
    )

    cycle_position = CyclePositionData(
        ts=now - 3600, cps=6.3, cps_label="late_bull",
        mvrv_z_score=2.1, mvrv_z_contribution=0.8,
        ahr999_value=0.42, ahr999_contribution=0.6,
        price_vs_200w_ratio=2.45, price_vs_200w_contribution=1.2,
        price_vs_sth_label="above", price_vs_sth_contribution=0.3,
        pi_cycle_ratio=0.87, pi_cycle_contribution=0.5,
        rplr_proxy=1.34, btc_rsi_daily=58.2,
        sma_200w=31200.0,
    )

    option_max_pain = OptionMaxPainData(
        symbol=coin, ts=now - 600,
        expiries=[
            OptionMaxPainExpiry(expiry_date="2026-04-25", max_pain_price=76500,
                                call_oi=3.2e8, put_oi=2.45e8, put_call_ratio=0.77),
        ],
        nearest_max_pain=76500, nearest_expiry="2026-04-25",
    )
    option_info = OptionInfoData(
        symbol=coin, ts=now - 600,
        total_oi_usd=1.85e10, total_vol_24h_usd=1.82e9,
        put_call_oi_ratio=0.78, put_call_vol_ratio=0.65, iv_atm=58.4,
    )

    market_index = MarketIndexData(
        ts=now - 120,
        fear_greed=38.0, btc_dominance=54.2,
        dxy=104.2, dxy_change_pct=-0.18,
        nasdaq=18450.0, nasdaq_change_pct=0.42,
        sp500=5230.0, sp500_change_pct=0.31,
        gold=2380.0, gold_change_pct=0.12,
        us_10y_yield=4.21, fed_rate=5.50,
        stablecoin_dominance=7.8, usdt_market_cap=1.1e11,
        ahr999=0.42, btc_mvrv=2.1, btc_implied_vol=0.58,
    )

    coinbase_premium = CoinbasePremiumData(ts=now - 120, current_premium=-0.012)
    stablecoin_mcap = StablecoinMcapData(
        ts=now - 120, current_total=1.6e11,
        history=[
            StablecoinMcapPoint(ts=now - i * 86400, total_mcap=1.6e11 - i * 1e8,
                                usdt_mcap=1.1e11, usdc_mcap=3.5e10)
            for i in range(7, 0, -1)
        ],
    )

    taker_flow = TakerFlowData(
        coin=coin, ts=now - 60, buy_ratio=0.512, sell_ratio=0.488,
        spot_buy_vol=1.2e9, spot_sell_vol=1.1e9,
        contract_buy_vol=3.8e9, contract_sell_vol=3.7e9,
    )

    vp = VolumeProfileData(
        coin=coin, ts=now - 600, bins=[],
        poc_price=76050, value_area_high=76800, value_area_low=75100, vwap=76200,
    )

    liq_stats = LiquidationStats(
        coin=coin, ts=now - 60, period_min=1440,
        long_total_usd=8.5e7, short_total_usd=1.05e8,
        long_count=420, short_count=510, ratio=1.24,
    )
    global_liq = GlobalLiquidationData(
        ts=now - 60, long_1h_usd=1.2e7, short_1h_usd=8.5e6,
        long_24h_usd=1.5e8, short_24h_usd=2.7e8,
        ratio_1h=1.41, ratio_24h=1.80, largest_single_usd=8.2e6,
    )

    state = SimpleNamespace(
        coin=coin,
        ticker=TickerData(
            coin=coin, ts=now - 12, last=price,
            high_24h=77100, low_24h=75200, vol_24h=1.245e10,
            change_24h=-630, change_pct_24h=-0.82,
        ),
        candles_1h=candles_1h,
        candles_15m=candles_15m,
        candles_4h=[],
        candles_daily=[],
        candles_weekly=[],
        cvd_contract=cvd_contract,
        cvd_spot=None,
        oi=oi,
        oi_change_24h_pct=1.83,
        oi_exchange_rank={"exchanges": [{"exchange": "Binance", "oi_usd": 6.5e9}]},
        funding=funding,
        multi_funding=multi_funding,
        ls_ratio=ls_global,
        ls_ratio_top_account=ls_top_acct,
        ls_ratio_top_position=ls_top_pos,
        ls_ratio_long_pct=55.2, ls_ratio_short_pct=44.8, ls_ratio_change_24h=-0.04,
        ls_top_acct_long_pct=51.2, ls_top_acct_short_pct=48.8, ls_top_acct_change_24h=0.02,
        liq_maps={"1d": liq_map_24h},
        liq_heatmaps={"24h": heatmap},
        orderbook=orderbook,
        large_orders=large_orders,
        whale_data=whale_data,
        etf_flow=etf_flow,
        cycle_position=cycle_position,
        option_max_pain=option_max_pain,
        option_info=option_info,
        market_index=market_index,
        coinbase_premium=coinbase_premium,
        stablecoin_mcap=stablecoin_mcap,
        taker_flow=taker_flow,
        vp=vp,
        atr=825.4,
        net_position_latest=-12500.3,
        net_position_change_24h=-3200.1,
        futures_coin_netflow_1h=-8.5e6,
        td_sequential_count=9,
        liq_stats=liq_stats,
        global_liq=global_liq,
        liq_sweep_events=[
            {"ts": now - 600, "side": "above", "usd": 1.24e7,
             "price": 78520, "cluster_price": 78500, "cluster_distance_pct": 0.03},
            {"ts": now - 7200, "side": "above", "usd": 5e6,  # 1h 之外
             "price": 78400, "cluster_price": 78500, "cluster_distance_pct": -0.13},
        ],
        fear_greed_prev=42,
    )
    return state


# ── 顶层契约 ──────────────────────────────────────────────

REQUIRED_TOP_KEYS = {
    "schema_version", "ready", "ts", "coin", "symbol",
    "price", "high_24h", "low_24h", "vol_24h", "change_pct_24h",
    "snapshot", "data_age_sec", "source_health",
}

REQUIRED_SNAPSHOT_KEYS = {
    "candles", "cvd", "oi", "funding", "long_short_ratio",
    "liquidation_map", "liquidation_heatmap", "liquidation_stats",
    "recent_sweeps_1h", "orderbook", "large_orders", "whale", "etf",
    "on_chain_cycle", "options", "taker_volume", "volume_profile",
    "atr_14", "net_position_td", "macro", "news",
    "key_levels_raw", "regime_context",
}


def test_schema_version_constant():
    """Schema 版本必须存在，且非空字符串。"""
    assert isinstance(SCHEMA_VERSION, str) and SCHEMA_VERSION


def test_top_level_contract_with_full_state():
    state = _make_state()
    candle_limit = {"15m": 96, "1h": 168, "4h": 120, "1d": 90, "1w": 60}
    out = build_nofx_snapshot(state, "BTCUSDT", candle_limit, source_health=[])

    assert set(REQUIRED_TOP_KEYS).issubset(out.keys()), \
        f"missing top keys: {REQUIRED_TOP_KEYS - set(out.keys())}"
    assert out["ready"] is True
    assert out["coin"] == "BTC"
    assert out["symbol"] == "BTCUSDT"
    assert out["schema_version"] == SCHEMA_VERSION
    assert out["price"] == 76234.5

    snap = out["snapshot"]
    assert set(REQUIRED_SNAPSHOT_KEYS).issubset(snap.keys()), \
        f"missing snapshot keys: {REQUIRED_SNAPSHOT_KEYS - set(snap.keys())}"


def test_ready_false_when_ticker_missing():
    state = SimpleNamespace(coin="BTC", ticker=None)
    out = build_nofx_snapshot(state, "BTCUSDT", {"1h": 100}, source_health=[])
    assert out["ready"] is False
    assert "snapshot" not in out
    assert out["coin"] == "BTC"


def test_no_processed_signals_leak():
    """加工层字段绝对不能出现在 snapshot 顶层 / 子层。"""
    state = _make_state()
    out = build_nofx_snapshot(state, "BTCUSDT",
                              {"15m": 96, "1h": 168, "4h": 120, "1d": 90, "1w": 60})
    snap = out["snapshot"]
    forbidden_top = {
        "trend_exhaustion", "market_structure", "market_structure_1d",
        "market_structure_1w", "direction_vote",
        "temperature", "market_temperature", "pin_risk_level",
        "range_signal", "key_levels", "key_level_snapshot_v2",
        "regime_snapshot",
        "rule_supports", "rule_resistances", "sniper_entries",
        "ladder_plans", "execution_plan", "ai_trader_report",
        "final_decision", "waterfall", "candlestick_pattern_1h",
        "candlestick_pattern_4h", "levels",
    }
    leaked = set(snap.keys()) & forbidden_top
    assert not leaked, f"加工层字段泄漏到 snapshot: {leaked}"

    # cycle 子节点不能含解读标签
    cycle = snap.get("on_chain_cycle") or {}
    assert "cps_label" not in cycle, "on_chain_cycle 不能包含 cps_label（解读类）"
    assert "price_vs_sth_label" not in cycle, "on_chain_cycle 不能包含 price_vs_sth_label（解读类）"

    # cvd 子节点不能含 trend / divergence_note 文本
    for side in ("contract", "spot"):
        sub = snap.get("cvd", {}).get(side)
        if sub is None:
            continue
        assert "trend_1h" not in sub, "cvd 子项不能含 trend_1h 文本"
        assert "divergence_note" not in sub, "cvd 子项不能含 divergence_note 文本"

    # funding 不能含解读字段
    funding = snap.get("funding") or {}
    assert "interpretation" not in funding, "funding 不能含 interpretation 文本"

    # taker 不能含 dominant 文本
    taker = snap.get("taker_volume") or {}
    assert "dominant" not in taker, "taker 不能含 dominant 文本"

    # oi 不能含 trend 文本
    oi = snap.get("oi") or {}
    assert "trend" not in oi, "oi 不能含 trend 文本"


def test_recent_sweeps_filters_to_1h():
    state = _make_state()
    out = build_nofx_snapshot(state, "BTCUSDT", {"1h": 100})
    sweeps = out["snapshot"]["recent_sweeps_1h"]
    # 第一条 600s 之前 → 入选；第二条 7200s 之前 → 出局
    assert len(sweeps) == 1
    s = sweeps[0]
    assert {"ts", "side", "usd", "price", "cluster_price", "cluster_distance_pct"}.issubset(s.keys())
    assert s["side"] == "above"


def test_candles_compact_array_shape():
    state = _make_state()
    out = build_nofx_snapshot(state, "BTCUSDT",
                              {"15m": 30, "1h": 50, "4h": 0, "1d": 0, "1w": 0})
    candles = out["snapshot"]["candles"]
    assert "1h" in candles and "15m" in candles
    assert "4h" not in candles or candles.get("4h") == []
    if candles["1h"]:
        row = candles["1h"][0]
        assert isinstance(row, list) and len(row) == 6, \
            "K 线必须是 [ts, o, h, l, c, v] 数组"


def test_long_short_ratio_3_dimensions():
    state = _make_state()
    out = build_nofx_snapshot(state, "BTCUSDT", {"1h": 100})
    ls = out["snapshot"]["long_short_ratio"]
    assert "global" in ls and "top_account" in ls and "top_position" in ls
    assert ls["global"]["dimension"] == "global"
    assert ls["top_account"]["dimension"] == "top_account"
    assert ls["top_position"]["dimension"] == "top_position"


def test_whale_transfer_flow_classified():
    state = _make_state()
    out = build_nofx_snapshot(state, "BTCUSDT", {"1h": 100})
    whale = out["snapshot"]["whale"]
    # to=binance 7.6M 转入；from=binance 3.8M 转出
    assert whale["transfer_inflow_usd"] == pytest.approx(7.6e6)
    assert whale["transfer_outflow_usd"] == pytest.approx(3.8e6)
    assert whale["transfer_net_usd"] == pytest.approx(3.8e6)


def test_liquidation_heatmap_hotspots_sorted():
    state = _make_state()
    out = build_nofx_snapshot(state, "BTCUSDT", {"1h": 100})
    hm = out["snapshot"]["liquidation_heatmap"]
    assert hm is not None
    vals = [h["total_usd"] for h in hm["hotspots"]]
    assert vals == sorted(vals, reverse=True), "hotspots 必须按 USD 降序"


def test_payload_is_json_serializable():
    """NOFX 端用 json.Unmarshal 反序列化，禁止有任何不可序列化对象。"""
    state = _make_state()
    out = build_nofx_snapshot(state, "BTCUSDT",
                              {"15m": 96, "1h": 168, "4h": 120, "1d": 90, "1w": 60})
    body = json.dumps(out, ensure_ascii=False)
    assert len(body) > 1000
    assert isinstance(json.loads(body), dict)


def test_data_age_sec_present_for_known_fields():
    state = _make_state()
    out = build_nofx_snapshot(state, "BTCUSDT", {"1h": 100})
    age = out["data_age_sec"]
    for key in ("ticker", "oi", "funding", "orderbook", "long_short_ratio",
                "etf", "on_chain_cycle", "macro"):
        assert key in age, f"data_age_sec 缺少 {key}"
        assert age[key] is None or age[key] >= 0


def test_funding_includes_avg_7d_and_by_exchange():
    state = _make_state()
    out = build_nofx_snapshot(state, "BTCUSDT", {"1h": 100})
    fr = out["snapshot"]["funding"]
    assert fr["okx"] == 0.0089
    assert fr["binance"] == 0.0091
    assert fr["avg_7d"] is not None
    assert len(fr["by_exchange"]) == 2


def test_news_only_brief_conclusion():
    """news.brief 字段集合：只能含 tldr / 元信息，不能含 sections / tracked_themes 等"""
    state = _make_state()
    out = build_nofx_snapshot(state, "BTCUSDT", {"1h": 100})
    news = out["snapshot"]["news"]
    if news["brief"] is not None:
        allowed = {
            "version", "updated_at", "coverage_hours", "tldr_cn",
            "update_trigger", "based_on_events_count", "model_used",
        }
        leak = set(news["brief"].keys()) - allowed
        assert not leak, f"news.brief 出现非法字段: {leak}"


# ── v1.0.1: 时间戳归一化 & 源头字段兜底 ────────────────────────

def test_ts_always_in_seconds_not_milliseconds():
    """所有对外 ts 都必须是 unix 秒（10 位），禁止毫秒（13 位）。

    上游 Binance K 线 / Coinglass whale / Hyperliquid 会返回毫秒，
    builder 必须统一归一化。阈值：秒 < 1e10 < 毫秒。
    """
    state = _make_state()

    # 模拟上游把毫秒直接丢进 state（常见真实情况）
    now_ms = int(time.time() * 1000)
    state.candles_1h = [
        CandleData(coin="BTC", ts=now_ms - i * 3_600_000, o=1, h=1, l=1, c=1, vol=1.0, vol_ccy=0.0)
        for i in range(10, 0, -1)
    ]
    state.cvd_contract = CVDData(
        coin="BTC", inst_type="CONTRACTS",
        series=[CVDPoint(ts=now_ms, buy_vol=1, sell_vol=1, delta=0, cvd=0.0)],
        delta_1h=0, has_divergence=False,
    )
    state.whale_data = WhaleData(
        ts=int(time.time()),
        hl_alerts=[HyperliquidWhaleAlert(
            ts=now_ms, symbol="BTC", side="long", action="open",
            size_usd=1_000_000, entry_price=70000,
        )],
        transfers=[],
        hl_positions=[],
    )

    out = build_nofx_snapshot(state, "BTCUSDT", {"1h": 100})
    snap = out["snapshot"]

    THRESHOLD = 10_000_000_000  # 秒最大 ≈ 2286 年；毫秒都超过此阈值

    for row in snap["candles"].get("1h", []):
        assert row[0] < THRESHOLD, f"K 线 ts 必须是秒，实际={row[0]}"

    last_pt = snap["cvd"]["contract"]["last_point"]
    if last_pt is not None:
        assert last_pt["ts"] < THRESHOLD, f"cvd last_point.ts 必须是秒，实际={last_pt['ts']}"

    for a in snap["whale"]["hl_alerts_recent"]:
        assert a["ts"] < THRESHOLD, f"whale alert ts 必须是秒，实际={a['ts']}"


def test_whale_empty_transfers_filtered():
    """Coinglass 有时返回 ts=0 amount_usd=0 的空壳转账，必须从 top_transfers 过滤。"""
    state = _make_state()
    state.whale_data = WhaleData(
        ts=int(time.time()),
        hl_alerts=[],
        hl_positions=[],
        transfers=[
            WhaleTransfer(ts=0, symbol="", amount=0, amount_usd=0,
                          from_label="unknown", to_label="unknown", blockchain=""),
            WhaleTransfer(ts=0, symbol="", amount=0, amount_usd=0,
                          from_label="Binance", to_label="Coinbase", blockchain=""),
            WhaleTransfer(ts=int(time.time()), symbol="BTC", amount=100, amount_usd=7_600_000,
                          from_label="unknown", to_label="binance", blockchain="bitcoin"),
        ],
    )
    out = build_nofx_snapshot(state, "BTCUSDT", {"1h": 100})
    top = out["snapshot"]["whale"]["top_transfers"]
    assert len(top) == 1, "空壳 transfers 必须被过滤"
    assert top[0]["amount_usd"] == 7_600_000


def test_options_put_call_ratio_autofilled():
    """源头 put_call_ratio 常为 0，builder 应按 put_oi/call_oi 自算补齐。"""
    state = _make_state()
    state.option_max_pain = OptionMaxPainData(
        coin="BTC", symbol="BTC", ts=int(time.time()),
        nearest_max_pain=76500.0, nearest_expiry="260430",
        expiries=[
            OptionMaxPainExpiry(expiry_date="260430", max_pain_price=76500.0,
                                call_oi=10_000.0, put_oi=6_000.0, put_call_ratio=0.0),
            OptionMaxPainExpiry(expiry_date="260529", max_pain_price=74000.0,
                                call_oi=20_000.0, put_oi=15_000.0, put_call_ratio=0.0),
        ],
    )
    state.option_info = OptionInfoData(
        coin="BTC", symbol="BTC", ts=int(time.time()),
        total_oi_usd=1e9, total_vol_24h_usd=5e8,
        put_call_oi_ratio=0.0, put_call_vol_ratio=0.0, iv_atm=None,
    )
    out = build_nofx_snapshot(state, "BTCUSDT", {"1h": 100})
    opt = out["snapshot"]["options"]
    # 每个 expiry 补算
    assert opt["expiries"][0]["put_call_ratio"] == pytest.approx(0.6, rel=1e-3)
    assert opt["expiries"][1]["put_call_ratio"] == pytest.approx(0.75, rel=1e-3)
    # 顶层汇总：(6000+15000)/(10000+20000) = 0.7
    assert opt["put_call_oi_ratio"] == pytest.approx(0.7, rel=1e-3)


# ── M3 · 1.2.0：key_levels_raw + regime_context ─────────────

from models.key_level import KeyLevelV2, KeyLevelSnapshotV2, LifecycleEvent
from models.regime import RegimeSnapshot, RegimeFeatures


def _attach_kl_and_regime(state, *, with_kl: bool = True, with_regime: bool = True):
    """挂上 M3 R11 所需的 key_level_snapshot_v2 / regime_snapshot mock。"""
    now = int(time.time())
    if with_kl:
        events_a = [
            LifecycleEvent(ts=now - 600, event_type="born", detail="首次出现",
                           score_after=80.0, tier_after="A"),
            LifecycleEvent(ts=now - 200, event_type="strengthening", detail="共振增强",
                           score_before=80.0, score_after=92.0,
                           tier_before="A", tier_after="S"),
            LifecycleEvent(ts=now - 100_000, event_type="born",
                           detail="超过 24h 应被过滤"),
        ]
        events_b = [
            LifecycleEvent(ts=now - 1200, event_type="tested",
                           detail="idle->testing", state_before="idle", state_after="testing"),
        ]
        levels = [
            KeyLevelV2(
                price=75800.0, side="support", category="strong_support",
                sources=["liq_map_7d", "vp", "fib_618"], source_count=3,
                evidence_groups=["liq_cluster", "vp_hvn", "fib"],
                independent_group_count=3,
                explain_chips=["7d清算簇", "VP HVN", "Fib0.618"],
                confluence_score=88.0, final_score=92.0,
                strength_tier="S", s_class="P3_high_freshness",
                distance_pct=-0.57, exchange_count=3, consensus_multiplier=1.30,
                dominant_leverage="50x", leverage_intensity=0.78,
                contradiction_penalty=0.0, contradiction_reasons=[],
                regime_modifier_applied=1.05, regime_at_score="trend_up",
                level_id="kl_a1b2c3d4e5f6",
                lifecycle_events=events_a,
                invalidation_price=75200.0, invalidation_condition="1h close < 75200",
            ),
            KeyLevelV2(
                price=78500.0, side="resistance", category="strong_resistance",
                sources=["liq_map_24h", "ema200"], source_count=2,
                evidence_groups=["liq_cluster", "ma_cluster"],
                independent_group_count=2,
                explain_chips=["24h 清算簇", "EMA200"],
                confluence_score=72.0, final_score=70.0,
                strength_tier="A",
                distance_pct=2.97, exchange_count=2, consensus_multiplier=1.15,
                level_id="kl_z9y8x7w6v5u4",
                lifecycle_events=events_b,
            ),
        ]
        state.key_level_snapshot_v2 = KeyLevelSnapshotV2(
            ts=now - 30, current_price=76234.5, atr=825.4,
            levels=levels,
            regime="trend_up", regime_confidence=0.78,
            regime_description="趋势上行", regime_weight_version="1.0.0",
        )
    if with_regime:
        state.regime_snapshot = RegimeSnapshot(
            coin="BTC", ts=now - 30, regime="trend_up", confidence=0.78,
            description_cn="ADX 27 + atr_pct 1.4% 上行",
            features=RegimeFeatures(
                atr_pct=1.42, atr_pct_percentile=65.0, adx=27.5,
                bbw=0.045, trend_slope_pct=0.32, cvd_persistence=0.68,
                structure_alignment="bullish_aligned", liq_24h_vs_7d_avg=1.18,
            ),
            prev_regime="range", regime_changed_at=now - 20_000,
            stable_duration_sec=20_000,
            action_weights={"snipe_long": 1.3, "scalp_long": 1.1},
        )


def test_key_levels_raw_top_n_and_evidence_summary():
    state = _make_state()
    _attach_kl_and_regime(state)
    out = build_nofx_snapshot(state, "BTCUSDT", {"1h": 100})
    kl = out["snapshot"]["key_levels_raw"]
    assert kl is not None
    assert kl["snapshot_ts"] > 0
    assert kl["current_price"] == pytest.approx(76234.5)
    assert kl["atr"] == pytest.approx(825.4)

    cands = kl["raw_candidates"]
    assert len(cands) == 2
    # 按 final_score 降序：92 > 70
    assert cands[0]["final_score"] >= cands[1]["final_score"]
    head = cands[0]
    # 关键评分摘要必须存在
    for k in ("level_id", "price", "side", "tier", "final_score",
              "evidence_groups", "explain_chips", "independent_group_count",
              "exchange_count", "consensus_multiplier",
              "regime_modifier_applied", "regime_at_score"):
        assert k in head, f"raw_candidate 缺少 {k}"
    assert head["evidence_groups"] == ["liq_cluster", "vp_hvn", "fib"]
    assert head["independent_group_count"] == 3
    assert head["regime_modifier_applied"] == pytest.approx(1.05)
    # 内部状态机 / cascade 字段不能泄漏到 raw 候选
    forbidden = {"state", "break_start_ts", "cascade_risk", "cascade_components",
                 "snipe_signal", "break_impact_projection", "test_history"}
    assert not (forbidden & set(head.keys()))


def test_key_levels_raw_recent_events_within_24h_and_sorted_desc():
    state = _make_state()
    _attach_kl_and_regime(state)
    out = build_nofx_snapshot(state, "BTCUSDT", {"1h": 100})
    events = out["snapshot"]["key_levels_raw"]["recent_events_24h"]
    # 跨 levels 合并：A 内 2 条 24h 内（第3条超 24h 被过滤），B 内 1 条 → 共 3
    assert len(events) == 3
    types = [e["event_type"] for e in events]
    assert "born" in types and "strengthening" in types and "tested" in types
    # 必须按 ts 倒序
    ts_list = [e["ts"] for e in events]
    assert ts_list == sorted(ts_list, reverse=True)
    # 每条事件需带 level_id / price / side / detail
    for e in events:
        assert e["level_id"]
        assert e["price"] > 0
        assert e["side"] in ("support", "resistance", "pivot")


def test_key_levels_raw_none_when_snapshot_missing():
    state = _make_state()
    # 不挂 key_level_snapshot_v2
    out = build_nofx_snapshot(state, "BTCUSDT", {"1h": 100})
    assert out["snapshot"]["key_levels_raw"] is None


def test_regime_context_features_exposed_no_action_weights():
    state = _make_state()
    _attach_kl_and_regime(state)
    out = build_nofx_snapshot(state, "BTCUSDT", {"1h": 100})
    rc = out["snapshot"]["regime_context"]
    assert rc is not None
    assert rc["regime"] == "trend_up"
    assert rc["confidence"] == pytest.approx(0.78)
    assert rc["prev_regime"] == "range"
    assert rc["stable_duration_sec"] == 20_000
    feats = rc["features"]
    assert feats["atr_pct"] == pytest.approx(1.42)
    assert feats["adx"] == pytest.approx(27.5)
    assert feats["structure_alignment"] == "bullish_aligned"
    # 严禁暴露 action_weights（属于已下结论的乘数表）
    assert "action_weights" not in rc


def test_regime_context_none_when_missing():
    state = _make_state()
    # 仅挂 KL，不挂 regime
    _attach_kl_and_regime(state, with_kl=True, with_regime=False)
    out = build_nofx_snapshot(state, "BTCUSDT", {"1h": 100})
    assert out["snapshot"]["regime_context"] is None


def test_schema_version_is_1_2_0():
    """M3 · R11：版本必须升至 1.2.0。"""
    assert SCHEMA_VERSION == "1.2.0"


def test_payload_with_m3_fields_json_serializable():
    """key_levels_raw + regime_context 必须 JSON 可序列化。"""
    state = _make_state()
    _attach_kl_and_regime(state)
    out = build_nofx_snapshot(state, "BTCUSDT",
                              {"15m": 96, "1h": 168, "4h": 120, "1d": 90, "1w": 60})
    body = json.dumps(out, ensure_ascii=False)
    obj = json.loads(body)
    assert obj["snapshot"]["key_levels_raw"]["raw_candidates"]
    assert obj["snapshot"]["regime_context"]["regime"] == "trend_up"
