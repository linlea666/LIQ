"""数据快照组装：汇总为 Strategic AI 消费的 AISnapshot（PR-5 瘦身版）"""

from __future__ import annotations

import time
from typing import Optional

from models.flow import CyclePositionData, ETFFlowData, GlobalLiquidationData, MarketIndexData, RangeSignalData
from models.key_level import KeyLevelSnapshotV2
from models.liquidation import LiqMaxPainItem, LiquidationMap
from models.market import VolumeProfileData
from models.market_action import MarketActionFacts
from models.orderbook_pressure import OrderbookPressureSnapshot
from models.snapshot import AISnapshot, LiquidationMapBlock
from models.trading_brain import TradingBrainSnapshot


def _macro_change_pct(
    raw_items: list,
    resolved_value: Optional[float],
    key_substrings: tuple[str, ...],
) -> Optional[float]:
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
    atr: float,
    market_temp_score: float,
    pin_risk_level: str,
    *,
    liq_map: Optional[LiquidationMap] = None,
    liq_map_7d: Optional[LiquidationMap] = None,
    liq_map_30d: Optional[LiquidationMap] = None,
    liq_max_pain_24h: Optional[LiqMaxPainItem] = None,
    vp: Optional[VolumeProfileData] = None,
    pressure_snapshot: Optional[OrderbookPressureSnapshot] = None,
    key_level_snapshot_v2: Optional[KeyLevelSnapshotV2] = None,
    trading_brain: Optional[TradingBrainSnapshot] = None,
    market_action_facts: Optional[MarketActionFacts] = None,
    global_liq: Optional[GlobalLiquidationData] = None,
    liq_sweep_events: list[dict] | None = None,
    market_index: Optional[MarketIndexData] = None,
    etf_flow: Optional[ETFFlowData] = None,
    cycle_position: Optional[CyclePositionData] = None,
    range_signal: Optional[RangeSignalData] = None,
    rsi_14: Optional[float] = None,
    boll_data: Optional[dict] = None,
    ema20: Optional[float] = None,
    btc_hist_vol: Optional[float] = None,
    option_max_pain_price: Optional[float] = None,
    option_nearest_expiry: str = "",
    btc_implied_vol: Optional[float] = None,
    btc_put_call_oi: Optional[float] = None,
    poll_failures: dict[str, str] | None = None,
    coinbase_premium: float = 0,
    coinbase_premium_trend: str = "",
    stablecoin_total_mcap: float = 0,
    stablecoin_7d_change_pct: float = 0,
    whale_net_direction: str = "",
    whale_transfers_count: int = 0,
    whale_transfer_net_usd: float = 0,
) -> AISnapshot:
    """组装 Strategic AISnapshot。仅保留 prompt 与数据自检所需字段。"""

    liq_block_1d = _build_liq_map_block(liq_map, "1d", max_pain=liq_max_pain_24h)
    liq_block_7d = _build_liq_map_block(liq_map_7d, "7d")
    liq_block_30d = _build_liq_map_block(liq_map_30d, "30d")

    wall_zones_above: list = []
    wall_zones_below: list = []
    wall_events_v2: list = []
    crowding_global = None
    usd_usdt_basis_v2: Optional[float] = None
    if pressure_snapshot:
        wall_zones_above = list(pressure_snapshot.walls_above[:12])
        wall_zones_below = list(pressure_snapshot.walls_below[:12])
        wall_events_v2 = list(pressure_snapshot.wall_events[:20])
        crowding_global = pressure_snapshot.crowding_global
        usd_usdt_basis_v2 = pressure_snapshot.usd_usdt_basis_pct

    facts_oi_v2 = facts_funding_v2 = None
    facts_cvd_contract_v2 = facts_cvd_spot_v2 = None
    facts_basis_v2 = facts_orderbook_v2 = None
    facts_liq_clusters_v2 = facts_liq_sweep_v2 = None
    facts_price_ctx_v2 = facts_footprint_v2 = facts_absorption_v2 = None
    facts_taker_v2 = facts_options_v2 = None
    facts_dq = ""
    facts_missing_list: list[str] = []
    facts_has_prov = False
    facts_prov_fields: list[str] = []
    facts_sources_used_list: list[str] = []
    if market_action_facts:
        facts_oi_v2 = market_action_facts.oi
        facts_funding_v2 = market_action_facts.funding
        facts_cvd_contract_v2 = market_action_facts.cvd_contract
        facts_cvd_spot_v2 = market_action_facts.cvd_spot
        facts_basis_v2 = market_action_facts.basis
        facts_orderbook_v2 = market_action_facts.orderbook
        facts_liq_clusters_v2 = market_action_facts.liq_map_clusters
        facts_liq_sweep_v2 = market_action_facts.liq_sweep_recent
        facts_price_ctx_v2 = market_action_facts.price_context
        facts_footprint_v2 = market_action_facts.footprint
        facts_absorption_v2 = market_action_facts.absorption
        facts_taker_v2 = market_action_facts.taker_flow_5m
        facts_options_v2 = market_action_facts.options
        facts_dq = market_action_facts.data_quality or ""
        facts_missing_list = list(market_action_facts.missing or [])
        meta = market_action_facts.data_meta
        facts_has_prov = bool(meta.has_provisional_bars)
        facts_prov_fields = list(meta.provisional_fields or [])
        facts_sources_used_list = list(meta.sources_used or [])

    mi = market_index
    mi_nasdaq = mi.nasdaq if mi else None
    mi_nasdaq_chg = mi.nasdaq_change_pct if mi else None
    mi_gold = mi.gold if mi else None
    mi_gold_chg = mi.gold_change_pct if mi else None
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

    if mi and mi.raw_items:
        if mi_nasdaq_chg is None:
            mi_nasdaq_chg = _macro_change_pct(mi.raw_items, mi_nasdaq, ("nasdaq", "ndx"))
        if mi_gold_chg is None:
            mi_gold_chg = _macro_change_pct(mi.raw_items, mi_gold, ("gold", "xau"))

    ev_sweeps = list(liq_sweep_events or [])
    above_sum = sum(e.get("usd", 0) for e in ev_sweeps if e.get("side") == "above")
    below_sum = sum(e.get("usd", 0) for e in ev_sweeps if e.get("side") == "below")

    return AISnapshot(
        coin=coin,
        ts=int(time.time()),
        price=price,
        high_24h=high_24h,
        low_24h=low_24h,
        atr_14=atr,
        rsi_14=rsi_14,
        volume_profile_poc=vp.poc_price if vp else 0,
        value_area_high=vp.value_area_high if vp else 0,
        value_area_low=vp.value_area_low if vp else 0,
        vwap=vp.vwap if vp else 0,
        market_temperature=market_temp_score,
        pin_risk_level=pin_risk_level,
        range_signal=range_signal.model_dump() if range_signal else None,
        boll_upper=boll_data.get("upper") if boll_data else None,
        boll_middle=boll_data.get("middle") if boll_data else None,
        boll_lower=boll_data.get("lower") if boll_data else None,
        ema20=ema20,
        btc_hist_vol=btc_hist_vol if btc_hist_vol is not None else (mi.btc_hist_vol if mi else None),
        poll_failures=poll_failures or {},
        global_liq_long_24h=global_liq.long_24h_usd if global_liq else 0,
        global_liq_short_24h=global_liq.short_24h_usd if global_liq else 0,
        global_liq_ratio_24h=global_liq.ratio_24h if global_liq else 1.0,
        liq_sweep_above_usd_1h=above_sum,
        liq_sweep_below_usd_1h=below_sum,
        liq_sweep_events=ev_sweeps,
        dxy=mi.dxy if mi else None,
        dxy_change_pct=mi.dxy_change_pct if mi else None,
        btc_dominance=mi.btc_dominance if mi else None,
        us_10y_yield=mi.us_10y_yield if mi else None,
        fed_rate=mi.fed_rate if mi else None,
        nasdaq=mi_nasdaq,
        nasdaq_change_pct=mi_nasdaq_chg,
        gold=mi_gold,
        gold_change_pct=mi_gold_chg,
        fear_greed_index=mi.fear_greed if mi else None,
        ahr999=(
            cycle_position.ahr999_value
            if cycle_position and cycle_position.ahr999_value and cycle_position.ahr999_value > 0
            else (mi.ahr999 if mi and mi.ahr999 and mi.ahr999 > 0 else None)
        ),
        btc_mvrv=mi.btc_mvrv if mi else None,
        etf_net_3d=etf_flow.net_3d if etf_flow else None,
        etf_trend=etf_flow.trend if etf_flow else "",
        stablecoin_total_mcap=stablecoin_total_mcap,
        stablecoin_7d_change_pct=stablecoin_7d_change_pct,
        coinbase_premium=coinbase_premium,
        coinbase_premium_trend=coinbase_premium_trend,
        whale_net_direction=whale_net_direction,
        whale_transfers_count=whale_transfers_count,
        whale_transfer_net_usd=whale_transfer_net_usd,
        exchange_btc_total=mi_exchange_btc_total,
        exchange_btc_change_pct=mi_exchange_btc_change_pct,
        option_max_pain_price=option_max_pain_price,
        option_nearest_expiry=option_nearest_expiry,
        btc_implied_vol=btc_implied_vol if btc_implied_vol is not None else (mi.btc_implied_vol if mi else None),
        btc_put_call_oi=btc_put_call_oi if btc_put_call_oi is not None else (mi.btc_put_call_oi if mi else None),
        trading_brain=trading_brain,
        key_level_snapshot=key_level_snapshot_v2,
        liq_map_block_1d=liq_block_1d,
        liq_map_block_7d=liq_block_7d,
        liq_map_block_30d=liq_block_30d,
        wall_zones_above=wall_zones_above,
        wall_zones_below=wall_zones_below,
        wall_events_v2=wall_events_v2,
        crowding_global=crowding_global,
        usd_usdt_basis_pct=usd_usdt_basis_v2,
        facts_oi=facts_oi_v2,
        facts_funding=facts_funding_v2,
        facts_cvd_contract=facts_cvd_contract_v2,
        facts_cvd_spot=facts_cvd_spot_v2,
        facts_basis=facts_basis_v2,
        facts_orderbook=facts_orderbook_v2,
        facts_liq_clusters=facts_liq_clusters_v2,
        facts_liq_sweep=facts_liq_sweep_v2,
        facts_price_context=facts_price_ctx_v2,
        facts_footprint=facts_footprint_v2,
        facts_absorption=facts_absorption_v2,
        facts_taker_flow=facts_taker_v2,
        facts_options=facts_options_v2,
        facts_data_quality=facts_dq,
        facts_missing=facts_missing_list,
        facts_has_provisional_bars=facts_has_prov,
        facts_provisional_fields=facts_prov_fields,
        facts_sources_used=facts_sources_used_list,
        **_collect_news_context(),
    )


def _build_liq_map_block(
    liq_map: Optional[LiquidationMap],
    cycle: str,
    max_pain: Optional[LiqMaxPainItem] = None,
) -> Optional[LiquidationMapBlock]:
    if liq_map is None:
        return None

    by_ex_summary: list[dict] = []
    by_exchange = liq_map.by_exchange
    if isinstance(by_exchange, dict) and by_exchange:
        ex_totals: list[tuple[str, float]] = []
        for ex_name, price_dict in by_exchange.items():
            if not isinstance(price_dict, dict):
                continue
            total = sum(float(v or 0) for v in price_dict.values())
            if total > 0:
                ex_totals.append((ex_name, total))
        total_all = sum(t for _, t in ex_totals)
        ex_totals.sort(key=lambda kv: kv[1], reverse=True)
        for ex_name, t in ex_totals[:3]:
            share = (t / total_all * 100) if total_all > 0 else 0
            by_ex_summary.append({
                "exchange": ex_name,
                "total_usd": round(t, 0),
                "share_pct": round(share, 1),
            })

    return LiquidationMapBlock(
        cycle=cycle,
        clusters_above=list(liq_map.clusters_above[:8]),
        clusters_below=list(liq_map.clusters_below[:8]),
        vacuum_zones=list(liq_map.vacuum_zones[:5]),
        imbalance_ratio=liq_map.imbalance_ratio,
        max_pain=max_pain,
        by_exchange_summary=by_ex_summary,
    )


def _collect_news_context() -> dict:
    ctx: dict = {
        "active_narratives": [],
        "news_brief_text": "",
        "news_brief_version": 0,
        "news_brief_trigger": "",
        "news_brief_updated_at": None,
        "geo_overview": None,
    }
    try:
        from processors.news_brief import get_current_brief
        brief = get_current_brief()
        if brief is not None:
            based_on = int(getattr(brief, "based_on_events_count", 0) or 0)
            if based_on <= 0:
                ctx["news_brief_text"] = ""
                ctx["news_brief_version"] = int(getattr(brief, "version", 0) or 0)
                ctx["news_brief_trigger"] = str(getattr(brief, "update_trigger", "") or "")
                ctx["news_brief_updated_at"] = int(getattr(brief, "updated_at", 0) or 0) or None
            else:
                import json as _json
                payload = brief.model_dump(mode="json")
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
