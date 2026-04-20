"""数据快照组装：将所有维度数据汇总为 AISnapshot"""

from __future__ import annotations

import time
from typing import Optional

from models.flow import (
    BasisData, CVDData, CyclePositionData, ETFFlowData, FundingRateData,
    GlobalLiquidationData, LongShortRatioData, MarketIndexData,
    MultiFundingRateData, OIData, RangeSignalData, TakerFlowData,
)
from models.key_level import KeyLevelSnapshotV2
from models.levels import LevelAnalysis
from models.liquidation import HeatmapData, LiquidationMap, LiquidationStats
from models.market import OrderBookAnalysis, VolumeProfileData
from models.market_structure import MarketStructure
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
    cycle_position: Optional[CyclePositionData] = None,
    liq_sweep_events: list[dict] | None = None,
    range_signal: Optional[RangeSignalData] = None,
    key_level_snapshot_v2: Optional[KeyLevelSnapshotV2] = None,
    liq_map_30d: Optional[LiquidationMap] = None,
    rsi_14: Optional[float] = None,
    macd_data: Optional[dict] = None,
    boll_data: Optional[dict] = None,
    ema20: Optional[float] = None,
    ma60_daily: Optional[float] = None,
    ma120_daily: Optional[float] = None,
    option_max_pain_price: Optional[float] = None,
    option_nearest_expiry: str = "",
    option_call_oi: Optional[float] = None,
    option_put_oi: Optional[float] = None,
    large_orders_buy_count: int = 0,
    large_orders_sell_count: int = 0,
    large_orders_net_usd: float = 0,
    ls_ratio_top_account: Optional[float] = None,
    ls_ratio_top_position: Optional[float] = None,
    ls_ratio_long_pct: Optional[float] = None,
    ls_ratio_short_pct: Optional[float] = None,
    ls_ratio_change_24h: Optional[float] = None,
    ls_top_acct_long_pct: Optional[float] = None,
    ls_top_acct_short_pct: Optional[float] = None,
    ls_top_acct_change_24h: Optional[float] = None,
    oi_change_24h_pct: Optional[float] = None,
    fear_greed_prev: Optional[int] = None,
    whale_hl_alerts_count: int = 0,
    whale_transfers_count: int = 0,
    whale_net_direction: str = "",
    whale_hl_positions: list[dict] | None = None,
    whale_transfer_inflow_usd: float = 0,
    whale_transfer_outflow_usd: float = 0,
    whale_transfer_net_usd: float = 0,
    whale_top_transfers: list[dict] | None = None,
    coinbase_premium: float = 0,
    coinbase_premium_trend: str = "",
    stablecoin_total_mcap: float = 0,
    stablecoin_7d_change_pct: float = 0,
    oi_exchange_rank: list[dict] | None = None,
    candles_4h: list | None = None,
    net_position_latest: Optional[float] = None,
    net_position_trend: str = "",
    net_position_change_24h: Optional[float] = None,
    futures_coin_netflow_1h: Optional[float] = None,
    futures_coin_netflow_trend: str = "",
    td_sequential_count: Optional[int] = None,
    td_sequential_direction: str = "",
    liq_heatmap: Optional[HeatmapData] = None,
    poll_failures: dict[str, str] | None = None,
    market_structure: Optional[MarketStructure] = None,
) -> AISnapshot:
    """组装所有维度数据为 AI 可消费的快照"""

    clusters_above = []
    clusters_below = []
    vacuum_zones = []
    imbalance = 0.0

    if liq_map:
        clusters_above = [c.model_dump() for c in liq_map.clusters_above[:8]]
        clusters_below = [c.model_dump() for c in liq_map.clusters_below[:8]]
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

    clusters_above_30d: list[dict] = []
    clusters_below_30d: list[dict] = []
    imbalance_30d = 0.0
    if liq_map_30d:
        clusters_above_30d = [c.model_dump() for c in liq_map_30d.clusters_above[:10]]
        clusters_below_30d = [c.model_dump() for c in liq_map_30d.clusters_below[:10]]
        imbalance_30d = liq_map_30d.imbalance_ratio

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

    # MI 字段现在由 BBX 数据源填充（DXY/纳指/黄金/MVRV/波动率等）
    mi = market_index
    mi_nasdaq = mi.nasdaq if mi else None
    mi_nasdaq_chg = mi.nasdaq_change_pct if mi else None
    mi_gold = mi.gold if mi else None
    mi_gold_chg = mi.gold_change_pct if mi else None
    mi_sp500 = mi.sp500 if mi else None
    mi_sp500_chg = mi.sp500_change_pct if mi else None
    mi_exchange_btc_total = None
    mi_exchange_btc_change_pct = None
    if mi:
        bal_parts = [b for b in (mi.binance_btc_balance, mi.okx_btc_balance,
                                  mi.bitfinex_btc_balance, mi.coinbase_btc_balance)
                     if b is not None]
        if bal_parts:
            mi_exchange_btc_total = sum(bal_parts)
            chg = mi.exchange_btc_change_24h
            if chg is not None and mi_exchange_btc_total > 0:
                prev = mi_exchange_btc_total - chg
                if prev > 0:
                    mi_exchange_btc_change_pct = round(chg / prev * 100, 2)

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
                         for s in levels.supports[:5]]
        rule_resistances = [{"price": r.price, "sources": r.sources, "strength": r.strength}
                            for r in levels.resistances[:5]]
        rule_stop_loss = [sl.model_dump() for sl in levels.stop_loss_zones]
        sniper_entries = [se.model_dump() for se in levels.sniper_entries[:6]]
        ladder_plans = [lp.model_dump() for lp in levels.ladder_plans]

    # 清算热力图摘要：提取 Top-5 密度峰值
    heatmap_hotspots: list[dict] = []
    if liq_heatmap and liq_heatmap.data:
        pts = sorted(liq_heatmap.data, key=lambda p: p.value, reverse=True)
        for pt in pts[:5]:
            pct = ((pt.price - price) / price * 100) if price > 0 else 0
            heatmap_hotspots.append({
                "price": pt.price,
                "total_usd": pt.value,
                "pct_above": round(pct, 2),
            })

    # K 线形态检测（最近 4H K 线）
    pattern_name = ""
    pattern_side = ""
    pattern_strength = 0.0
    if candles_4h and len(candles_4h) >= 2:
        from processors.candlestick_patterns import detect_reversal_pattern
        for _side in ("support", "resistance"):
            pr = detect_reversal_pattern(candles_4h, _side)
            if pr.found and pr.strength > pattern_strength:
                pattern_name = pr.name
                pattern_side = _side
                pattern_strength = pr.strength

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
        recent_liq_24h_long_usd=liq_stats.long_total_usd if liq_stats else 0,
        recent_liq_24h_short_usd=liq_stats.short_total_usd if liq_stats else 0,
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
        btc_max_pain=mi.btc_max_pain if mi else None,
        btc_dvol=mi.btc_dvol if mi else None,
        dxy=mi.dxy if mi else None,
        dxy_change_pct=mi.dxy_change_pct if mi else None,
        btc_dominance=mi.btc_dominance if mi else None,
        taker_buy_ratio=taker_flow.buy_ratio if taker_flow else None,
        taker_dominant=taker_flow.dominant if taker_flow else "",
        nasdaq=mi_nasdaq,
        nasdaq_change_pct=mi_nasdaq_chg,
        gold=mi_gold,
        gold_change_pct=mi_gold_chg,
        sp500=mi_sp500,
        sp500_change_pct=mi_sp500_chg,
        btc_mvrv=mi.btc_mvrv if mi else None,
        btc_hist_vol=mi.btc_hist_vol if mi else None,
        btc_implied_vol=mi.btc_implied_vol if mi else None,
        btc_iv_skew_1m=mi.btc_iv_skew_1m if mi else None,
        exchange_btc_total=mi_exchange_btc_total,
        exchange_btc_change_24h=mi.exchange_btc_change_24h if mi else None,
        exchange_btc_change_pct=mi_exchange_btc_change_pct,
        ahr999=(mi.ahr999 if mi and mi.ahr999 and mi.ahr999 > 0 else (cycle_position.ahr999_value if cycle_position and cycle_position.ahr999_value and cycle_position.ahr999_value > 0 else None)),
        stablecoin_dominance=mi.stablecoin_dominance if mi else None,
        coinbase_btc_premium=mi.coinbase_btc_premium if mi else None,
        usdt_otc_premium=mi.usdt_otc_premium if mi else None,
        us_10y_yield=mi.us_10y_yield if mi else None,
        fed_rate=mi.fed_rate if mi else None,
        btc_put_call_oi=mi.btc_put_call_oi if mi else None,
        usdt_market_cap=mi.usdt_market_cap if mi else None,
        btc_hashrate=mi.btc_hashrate if mi else None,
        okx_ls_ratio_btc=mi.okx_ls_ratio_btc if mi else None,
        binance_ls_ratio_btc=mi.binance_ls_ratio_btc if mi else None,
        cycle_position=cycle_position.model_dump() if cycle_position else None,
        liq_sweep_above_usd_1h=sum(
            e.get("usd", 0) for e in (liq_sweep_events or []) if e.get("side") == "above"
        ),
        liq_sweep_below_usd_1h=sum(
            e.get("usd", 0) for e in (liq_sweep_events or []) if e.get("side") == "below"
        ),
        liq_sweep_events=liq_sweep_events or [],
        range_signal=range_signal.model_dump() if range_signal else None,
        key_levels=key_level_snapshot_v2.model_dump() if key_level_snapshot_v2 else None,
        market_structure=market_structure.model_dump() if market_structure else None,
        liq_clusters_above_30d=clusters_above_30d,
        liq_clusters_below_30d=clusters_below_30d,
        liq_imbalance_ratio_30d=imbalance_30d,
        rsi_14=rsi_14,
        macd_histogram=macd_data.get("histogram") if macd_data else None,
        macd_above_zero=macd_data.get("above_zero") if macd_data else None,
        boll_upper=boll_data.get("upper") if boll_data else None,
        boll_middle=boll_data.get("middle") if boll_data else None,
        boll_lower=boll_data.get("lower") if boll_data else None,
        ema20=ema20,
        ma60_daily=ma60_daily,
        ma120_daily=ma120_daily,
        option_max_pain_price=option_max_pain_price,
        option_nearest_expiry=option_nearest_expiry,
        option_call_oi=option_call_oi,
        option_put_oi=option_put_oi,
        large_orders_buy_count=large_orders_buy_count,
        large_orders_sell_count=large_orders_sell_count,
        large_orders_net_usd=large_orders_net_usd,
        ls_ratio_top_account=ls_ratio_top_account,
        ls_ratio_top_position=ls_ratio_top_position,
        ls_ratio_long_pct=ls_ratio_long_pct,
        ls_ratio_short_pct=ls_ratio_short_pct,
        ls_ratio_change_24h=ls_ratio_change_24h,
        ls_top_acct_long_pct=ls_top_acct_long_pct,
        ls_top_acct_short_pct=ls_top_acct_short_pct,
        ls_top_acct_change_24h=ls_top_acct_change_24h,
        oi_change_24h_pct=oi_change_24h_pct,
        fear_greed_prev=fear_greed_prev,
        whale_hl_alerts_count=whale_hl_alerts_count,
        whale_transfers_count=whale_transfers_count,
        whale_net_direction=whale_net_direction,
        whale_hl_positions=whale_hl_positions or [],
        whale_transfer_inflow_usd=whale_transfer_inflow_usd,
        whale_transfer_outflow_usd=whale_transfer_outflow_usd,
        whale_transfer_net_usd=whale_transfer_net_usd,
        whale_top_transfers=whale_top_transfers or [],
        coinbase_premium=coinbase_premium,
        coinbase_premium_trend=coinbase_premium_trend,
        stablecoin_total_mcap=stablecoin_total_mcap,
        stablecoin_7d_change_pct=stablecoin_7d_change_pct,
        oi_exchange_rank=oi_exchange_rank or [],
        liq_heatmap_hotspots=heatmap_hotspots,
        candlestick_pattern_name=pattern_name,
        candlestick_pattern_side=pattern_side,
        candlestick_pattern_strength=pattern_strength,
        net_position_latest=net_position_latest,
        net_position_trend=net_position_trend,
        net_position_change_24h=net_position_change_24h,
        futures_coin_netflow_1h=futures_coin_netflow_1h,
        futures_coin_netflow_trend=futures_coin_netflow_trend,
        td_sequential_count=td_sequential_count,
        td_sequential_direction=td_sequential_direction,
        poll_failures=poll_failures or {},
        rule_supports=rule_supports,
        rule_resistances=rule_resistances,
        rule_stop_loss=rule_stop_loss,
        sniper_entries=sniper_entries,
        ladder_plans=ladder_plans,
        **_collect_news_context(),
    )


def _collect_news_context() -> dict:
    """P1.2b · 从 news_brief / geo_risk / narrative tracker 抓取最新摘要。

    返回可作为 AISnapshot 构造参数的 dict（任一模块缺失时降级为空值，
    绝不阻断主快照构建）。
    """
    ctx: dict = {
        "news_brief_text": "",
        "news_brief_version": 0,
        "news_brief_trigger": "",
        "news_brief_updated_at": None,
        "geo_overview": None,
        "active_narratives": [],
    }
    try:
        from processors.news_brief import get_current_brief
        brief = get_current_brief()
        if brief is not None:
            # ─────────────────────────────────────────────────────────────
            # P0-3 · events=0 熔断：当简报没有任何真实事件支撑时
            #   不注入 news_brief_text 到主 AI prompt（防止虚构新闻污染决策）
            #   仍保留 version / trigger / updated_at 作为元数据，便于前端展示
            # ─────────────────────────────────────────────────────────────
            based_on = int(getattr(brief, "based_on_events_count", 0) or 0)
            if based_on <= 0:
                ctx["news_brief_text"] = ""
                ctx["news_brief_version"] = int(getattr(brief, "version", 0) or 0)
                ctx["news_brief_trigger"] = str(getattr(brief, "update_trigger", "") or "")
                ctx["news_brief_updated_at"] = int(getattr(brief, "updated_at", 0) or 0) or None
            else:
                import json as _json
                payload = brief.model_dump(mode="json")
                # 精简 json 字段：sections + themes + coverage
                keep = {
                    "version": payload.get("version"),
                    "ts_range_start": payload.get("ts_range_start"),
                    "ts_range_end": payload.get("ts_range_end"),
                    "update_trigger": payload.get("update_trigger"),
                    "based_on_events_count": based_on,
                    "tldr_cn": payload.get("tldr_cn") or "",
                    "sections": payload.get("sections") or [],
                    "tracked_themes": payload.get("tracked_themes") or [],
                    "diff_from_prev_version": payload.get("diff_from_prev_version") or "",
                }
                ctx["news_brief_text"] = _json.dumps(keep, ensure_ascii=False, separators=(",", ":"))
                ctx["news_brief_version"] = int(payload.get("version") or 0)
                ctx["news_brief_trigger"] = str(payload.get("update_trigger") or "")
                ctx["news_brief_updated_at"] = int(payload.get("updated_at") or 0) or None
    except Exception:
        pass

    try:
        from processors.geo_risk_tracker import get_geo_risk_tracker
        overview = get_geo_risk_tracker().get_overview()
        if overview is not None:
            ctx["geo_overview"] = overview.model_dump(mode="json")
    except Exception:
        pass

    try:
        from processors.narrative_tracker import get_narrative_tracker
        themes = get_narrative_tracker().get_active(limit=5)
        ctx["active_narratives"] = [
            {
                "theme_id": t.theme_id,
                "theme_name_cn": getattr(t, "theme_name_cn", "") or "",
                "current_direction_bias": getattr(t, "current_direction_bias", "neutral"),
                "current_intensity": getattr(t, "current_intensity", 0),
                "flip_flop_count_24h": getattr(t, "flip_flop_count_24h", 0),
            }
            for t in themes
        ]
    except Exception:
        pass

    return ctx
