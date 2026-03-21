"""数据快照组装：将所有维度数据汇总为 AISnapshot"""

from __future__ import annotations

import time
from typing import Any

from models.flow import BasisData, CVDData, FundingRateData, OIData
from models.levels import LevelAnalysis
from models.liquidation import LiquidationMap, LiquidationStats
from models.market import OrderBookAnalysis, VolumeProfileData
from models.snapshot import AISnapshot


def build_ai_snapshot(
    coin: str,
    price: float,
    high_24h: float,
    low_24h: float,
    liq_map: LiquidationMap | None,
    cvd_contract: CVDData | None,
    cvd_spot: CVDData | None,
    oi: OIData | None,
    funding: FundingRateData | None,
    basis: BasisData | None,
    orderbook: OrderBookAnalysis | None,
    liq_stats: LiquidationStats | None,
    vp: VolumeProfileData | None,
    atr: float,
    market_temp_score: float,
    pin_risk_level: str,
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

    bid_walls = []
    ask_walls = []
    if orderbook:
        bid_walls = [w.model_dump() for w in orderbook.bid_walls[:5]]
        ask_walls = [w.model_dump() for w in orderbook.ask_walls[:5]]

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
        basis_pct=basis.basis_pct if basis else 0,
        orderbook_bid_walls=bid_walls,
        orderbook_ask_walls=ask_walls,
        recent_liq_30m_long_usd=liq_stats.long_total_usd if liq_stats else 0,
        recent_liq_30m_short_usd=liq_stats.short_total_usd if liq_stats else 0,
        volume_profile_poc=vp.poc_price if vp else 0,
        value_area_high=vp.value_area_high if vp else 0,
        value_area_low=vp.value_area_low if vp else 0,
        vwap=vp.vwap if vp else 0,
        atr_14=atr,
        market_temperature=market_temp_score,
        pin_risk_level=pin_risk_level,
    )
