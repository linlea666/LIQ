"""数据快照组装：将所有维度数据汇总为 AISnapshot"""

from __future__ import annotations

import time
from typing import Optional

from models.flow import (
    BasisData, CVDData, ETFFlowData, FundingRateData,
    GlobalLiquidationData, LongShortRatioData, MarketIndexData,
    MultiFundingRateData, OIData, TakerFlowData,
)
from models.levels import LevelAnalysis
from models.liquidation import LiquidationMap, LiquidationStats
from models.market import OrderBookAnalysis, VolumeProfileData
from models.snapshot import AISnapshot


def _macro_change_pct(
    raw_items: list,
    resolved_value: Optional[float],
    key_substrings: tuple[str, ...],
) -> Optional[float]:
    """在 raw_items 中匹配已解析的数值或 key 子串，取涨跌幅。"""
    if not raw_items:
        return None
    if resolved_value is not None:
        for item in raw_items:
            if item.value is None:
                continue
            if abs(item.value - resolved_value) <= max(1e-9, abs(resolved_value) * 1e-9):
                return item.change_pct
    for item in raw_items:
        k = (item.key or "").lower()
        n = item.name or ""
        for sub in key_substrings:
            if sub.lower() in k or sub in n:
                return item.change_pct
    return None


def build_ai_snapshot(
    coin: str,
    price: float,
    high_24h: float,
    low_24h: float,
    liq_map: Optional[LiquidationMap],
    cvd_contract: Optional[CVDData],
    cvd_spot: Optional[CVDData],
    oi: Optional[OIData],
    funding: Optional[FundingRateData],
    basis: Optional[BasisData],
    orderbook: Optional[OrderBookAnalysis],
    liq_stats: Optional[LiquidationStats],
    vp: Optional[VolumeProfileData],
    atr: float,
    market_temp_score: float,
    pin_risk_level: str,
    multi_funding: Optional[MultiFundingRateData] = None,
    ls_ratio: Optional[LongShortRatioData] = None,
    etf_flow: Optional[ETFFlowData] = None,
    global_liq: Optional[GlobalLiquidationData] = None,
    market_index: Optional[MarketIndexData] = None,
    taker_flow: Optional[TakerFlowData] = None,
    levels: Optional[LevelAnalysis] = None,
    liq_map_7d: Optional[LiquidationMap] = None,
) -> AISnapshot:
    """组装所有维度数据为 AI 可消费的快照"""

    clusters_above = []
    clusters_below = []
    vacuum_zones = []
    imbalance = 0.0

    if liq_map:
        clusters_above = [c.model_dump() for c in liq_map.clusters_above[:5]]
        clusters_below = [c.model_dump() for c in liq_map.clusters_below[:5]]
        vacuum_zones = [v.model_dump() for v in liq_map.vacuum_zones[:5]]
        imbalance = liq_map.imbalance_ratio

    clusters_above_7d: list[dict] = []
    clusters_below_7d: list[dict] = []
    vacuum_zones_7d: list[dict] = []
    imbalance_7d = 0.0
    if liq_map_7d:
        clusters_above_7d = [c.model_dump() for c in liq_map_7d.clusters_above[:8]]
        clusters_below_7d = [c.model_dump() for c in liq_map_7d.clusters_below[:8]]
        vacuum_zones_7d = [v.model_dump() for v in liq_map_7d.vacuum_zones[:5]]
        imbalance_7d = liq_map_7d.imbalance_ratio

    bid_walls = []
    ask_walls = []
    if orderbook:
        bid_walls = [w.model_dump() for w in orderbook.bid_walls[:5]]
        ask_walls = [w.model_dump() for w in orderbook.ask_walls[:5]]

    funding_exchanges = []
    funding_avg_7d = None
    if multi_funding:
        funding_exchanges = [e.model_dump() for e in multi_funding.exchanges]
        funding_avg_7d = multi_funding.avg_7d

    # 宏观数据（涨跌幅：优先按数值对齐 raw_items，其次 key/name 子串）
    nasdaq_val = None
    nasdaq_chg = None
    gold_val = None
    gold_chg = None
    sp500_val = None
    sp500_chg = None
    # Phase 5 新增
    mi_btc_mvrv = None
    mi_btc_hist_vol = None
    mi_btc_implied_vol = None
    mi_btc_iv_skew_1m = None
    mi_exchange_btc_total = None
    mi_exchange_btc_change_pct = None
    mi_ahr999 = None
    mi_stablecoin_dominance = None
    mi_coinbase_btc_premium = None
    mi_usdt_otc_premium = None
    mi_us_10y_yield = None
    mi_fed_rate = None
    mi_btc_put_call_oi = None
    mi_usdt_market_cap = None
    mi_btc_hashrate = None
    mi_okx_ls_ratio = None
    mi_binance_ls_ratio = None

    if market_index:
        nasdaq_val = market_index.nasdaq
        gold_val = market_index.gold
        sp500_val = market_index.sp500
        items = market_index.raw_items
        nasdaq_chg = _macro_change_pct(items, nasdaq_val, ("nasdaq", "ndx", "qqq", "纳斯达克"))
        gold_chg = _macro_change_pct(items, gold_val, ("gold", "xau", "黄金"))
        sp500_chg = _macro_change_pct(items, sp500_val, ("spx", "sp500", "标普", "s&p"))

        mi_btc_mvrv = market_index.btc_mvrv
        mi_btc_hist_vol = market_index.btc_hist_vol
        mi_btc_implied_vol = market_index.btc_implied_vol
        mi_btc_iv_skew_1m = market_index.btc_iv_skew_1m
        mi_ahr999 = market_index.ahr999
        mi_stablecoin_dominance = market_index.stablecoin_dominance
        mi_coinbase_btc_premium = market_index.coinbase_btc_premium
        mi_usdt_otc_premium = market_index.usdt_otc_premium
        mi_us_10y_yield = market_index.us_10y_yield
        mi_fed_rate = market_index.fed_rate
        mi_btc_put_call_oi = market_index.btc_put_call_oi
        mi_usdt_market_cap = market_index.usdt_market_cap
        mi_btc_hashrate = market_index.btc_hashrate
        mi_okx_ls_ratio = market_index.okx_ls_ratio_btc
        mi_binance_ls_ratio = market_index.binance_ls_ratio_btc

        bnb_bal = market_index.binance_btc_balance
        okx_bal = market_index.okx_btc_balance
        bf_bal = market_index.bitfinex_btc_balance
        cb_bal = market_index.coinbase_btc_balance
        bal_parts = [b for b in (bnb_bal, okx_bal, bf_bal, cb_bal) if b is not None]
        if bal_parts:
            mi_exchange_btc_total = sum(bal_parts)
            chg_parts = []
            for bal_val, subs in (
                (bnb_bal, ("binancebtcbalance",)),
                (okx_bal, ("okexbtcbalance",)),
                (bf_bal, ("bitfinexbtcbalance",)),
                (cb_bal, ("coinbtchold",)),
            ):
                c = _macro_change_pct(items, bal_val, subs)
                if c is not None:
                    chg_parts.append(c)
            if chg_parts:
                mi_exchange_btc_change_pct = sum(chg_parts) / len(chg_parts)

    ob_bid_total = 0.0
    ob_ask_total = 0.0
    ob_spread = 0.0
    if orderbook:
        ob_bid_total = orderbook.bid_total_usd
        ob_ask_total = orderbook.ask_total_usd
        ob_spread = orderbook.spread_pct

    # 规则引擎预计算结果
    rule_supports = []
    rule_resistances = []
    rule_stop_loss = []
    sniper_entries = []
    ladder_plans = []
    if levels:
        rule_supports = [{"price": s.price, "sources": s.sources, "strength": s.strength}
                         for s in levels.supports[:3]]
        rule_resistances = [{"price": r.price, "sources": r.sources, "strength": r.strength}
                            for r in levels.resistances[:3]]
        rule_stop_loss = [sl.model_dump() for sl in levels.stop_loss_zones]
        sniper_entries = [se.model_dump() for se in levels.sniper_entries[:4]]
        ladder_plans = [lp.model_dump() for lp in levels.ladder_plans]

    return AISnapshot(
        coin=coin,
        ts=int(time.time()),
        price=price,
        high_24h=high_24h,
        low_24h=low_24h,
        liq_clusters_above=clusters_above,
        liq_clusters_below=clusters_below,
        vacuum_zones=vacuum_zones,
        liq_imbalance_ratio=imbalance,
        liq_clusters_above_7d=clusters_above_7d,
        liq_clusters_below_7d=clusters_below_7d,
        vacuum_zones_7d=vacuum_zones_7d,
        liq_imbalance_ratio_7d=imbalance_7d,
        cvd_contract_trend=cvd_contract.trend_1h if cvd_contract else "",
        cvd_contract_delta_1h=cvd_contract.delta_1h if cvd_contract else 0,
        cvd_spot_trend=cvd_spot.trend_1h if cvd_spot else "",
        cvd_spot_delta_1h=cvd_spot.delta_1h if cvd_spot else 0,
        cvd_divergence=cvd_contract.divergence_note if cvd_contract else "",
        oi_current_usd=oi.current_usd if oi else 0,
        oi_change_1h_pct=oi.change_1h_pct if oi else 0,
        oi_change_5m_pct=oi.change_5m_pct if oi else 0,
        oi_trend=oi.trend if oi else "",
        funding_rate_okx=funding.okx_rate if funding else None,
        funding_rate_binance=funding.binance_rate if funding else None,
        funding_interpretation=funding.interpretation if funding else "",
        funding_avg_7d=funding_avg_7d,
        funding_exchanges=funding_exchanges,
        basis_pct=basis.basis_pct if basis else 0,
        orderbook_bid_walls=bid_walls,
        orderbook_ask_walls=ask_walls,
        orderbook_bid_total_usd=ob_bid_total,
        orderbook_ask_total_usd=ob_ask_total,
        orderbook_spread_pct=ob_spread,
        recent_liq_30m_long_usd=liq_stats.long_total_usd if liq_stats else 0,
        recent_liq_30m_short_usd=liq_stats.short_total_usd if liq_stats else 0,
        volume_profile_poc=vp.poc_price if vp else 0,
        value_area_high=vp.value_area_high if vp else 0,
        value_area_low=vp.value_area_low if vp else 0,
        vwap=vp.vwap if vp else 0,
        atr_14=atr,
        market_temperature=market_temp_score,
        pin_risk_level=pin_risk_level,
        ls_ratio=ls_ratio.avg_ratio if ls_ratio else None,
        ls_ratio_interpretation=ls_ratio.interpretation if ls_ratio else "",
        fear_greed_index=market_index.fear_greed if market_index else None,
        etf_net_3d=etf_flow.net_3d if etf_flow else None,
        etf_trend=etf_flow.trend if etf_flow else "",
        etf_recent_days=[d.model_dump() for d in etf_flow.recent_days[:5]] if etf_flow else [],
        global_liq_long_24h=global_liq.long_24h_usd if global_liq else 0,
        global_liq_short_24h=global_liq.short_24h_usd if global_liq else 0,
        global_liq_long_1h=global_liq.long_1h_usd if global_liq else 0,
        global_liq_short_1h=global_liq.short_1h_usd if global_liq else 0,
        global_liq_ratio_24h=global_liq.ratio_24h if global_liq else 1.0,
        global_liq_largest_single=global_liq.largest_single_usd if global_liq else 0,
        btc_max_pain=market_index.btc_max_pain if market_index else None,
        btc_dvol=market_index.btc_dvol if market_index else None,
        dxy=market_index.dxy if market_index else None,
        btc_dominance=market_index.btc_dominance if market_index else None,
        taker_buy_ratio=taker_flow.buy_ratio if taker_flow else None,
        taker_dominant=taker_flow.dominant if taker_flow else "",
        nasdaq=nasdaq_val,
        nasdaq_change_pct=nasdaq_chg,
        gold=gold_val,
        gold_change_pct=gold_chg,
        sp500=sp500_val,
        sp500_change_pct=sp500_chg,
        btc_mvrv=mi_btc_mvrv,
        btc_hist_vol=mi_btc_hist_vol,
        btc_implied_vol=mi_btc_implied_vol,
        btc_iv_skew_1m=mi_btc_iv_skew_1m,
        exchange_btc_total=mi_exchange_btc_total,
        exchange_btc_change_pct=mi_exchange_btc_change_pct,
        ahr999=mi_ahr999,
        stablecoin_dominance=mi_stablecoin_dominance,
        coinbase_btc_premium=mi_coinbase_btc_premium,
        usdt_otc_premium=mi_usdt_otc_premium,
        us_10y_yield=mi_us_10y_yield,
        fed_rate=mi_fed_rate,
        btc_put_call_oi=mi_btc_put_call_oi,
        usdt_market_cap=mi_usdt_market_cap,
        btc_hashrate=mi_btc_hashrate,
        okx_ls_ratio_btc=mi_okx_ls_ratio,
        binance_ls_ratio_btc=mi_binance_ls_ratio,
        rule_supports=rule_supports,
        rule_resistances=rule_resistances,
        rule_stop_loss=rule_stop_loss,
        sniper_entries=sniper_entries,
        ladder_plans=ladder_plans,
    )
