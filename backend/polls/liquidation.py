"""
清算领域：Coinglass 清算地图、热力图、最大痛点、爆仓历史、全网爆仓统计。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

from config.settings import CoinConfig
from engine import CoinState
from models.flow import GlobalLiquidationData
from models.liquidation import (
    HeatmapData,
    HeatmapDataPoint,
    LiqBand,
    LiqHistoryData,
    LiqHistoryPoint,
    LiqLeverageGroup,
    LiqMaxPainData,
    LiqMaxPainItem,
    LiquidationMap,
    LiquidationStats,
)
from processors.liquidation import detect_liq_sweep, process_liquidation_map
from sources.coinglass import CoinglassSource

logger = logging.getLogger(__name__)


def _extract_all_row(data: Any) -> tuple[float, float]:
    """从 exchange-list 提取 'All' 汇总行，避免逐所累加重复统计"""
    if not data or not isinstance(data, list):
        return 0.0, 0.0
    for item in data:
        if item.get("exchange", "") == "All":
            l = float(item.get("longLiquidation_usd", item.get("long_liquidation_usd", 0)))
            s = float(item.get("shortLiquidation_usd", item.get("short_liquidation_usd", 0)))
            return l, s
    return 0.0, 0.0


def parse_liquidation_map(
    data: Any,
    coin: str,
    cycle: str,
    current_price: float = 0,
) -> Optional[LiquidationMap]:
    """解析 Coinglass V4 清算地图数据。

    raw_response=True 时 data 结构:
      {code, data: [{liqMapV2: {价格: [[价格,量USD,...]], ...}, ...}], last_price: int}
    _request 解包后也可能是 list（旧缓存/兼容），此处统一处理。
    """
    try:
        if isinstance(data, list):
            inner_list = data
            last_price = current_price
        elif isinstance(data, dict):
            inner_list = data.get("data", [])
            last_price = float(data.get("last_price", 0) or 0)
            if isinstance(inner_list, dict):
                if last_price <= 0:
                    last_price = float(inner_list.get("last_price", 0) or 0)
                inner_list = inner_list.get("data", [])
            if last_price <= 0:
                last_price = current_price
        else:
            return None

        if not isinstance(inner_list, list) or not inner_list or last_price <= 0:
            return None

        merged: dict[int, float] = {}
        for exchange_item in inner_list:
            if not isinstance(exchange_item, dict):
                continue
            liq_map_v2 = exchange_item.get("liqMapV2", {})
            if not isinstance(liq_map_v2, dict):
                continue
            for price_str, entries in liq_map_v2.items():
                price_key = int(float(price_str))
                for entry in entries:
                    if isinstance(entry, list) and len(entry) >= 2:
                        merged[price_key] = merged.get(price_key, 0) + float(entry[1])

        if not merged:
            return None

        short_bands = []
        long_bands = []
        for price_key in sorted(merged.keys()):
            vol = merged[price_key]
            band = LiqBand(
                price_from=float(price_key),
                price_to=float(price_key),
                turnover_usd=vol,
            )
            if price_key > last_price:
                short_bands.append(band)
            else:
                long_bands.append(band)

        group = LiqLeverageGroup(
            leverage="all",
            short_bands=short_bands,
            long_bands=long_bands,
            short_total_usd=sum(b.turnover_usd for b in short_bands),
            long_total_usd=sum(b.turnover_usd for b in long_bands),
        )

        return LiquidationMap(
            coin=coin, ts=int(time.time()), cycle=cycle,
            leverage_groups=[group],
        )
    except Exception:
        logger.error("Parse liquidation map failed | coin=%s cycle=%s", coin, cycle, exc_info=True)
        return None


def detect_and_store_sweep(state: CoinState, new_map: LiquidationMap, price: float) -> None:
    prev = state._prev_liq_map_24h
    prev_price = state._prev_price_at_liq_poll
    state._prev_liq_map_24h = new_map
    state._prev_price_at_liq_poll = price
    if not prev or prev_price <= 0:
        return
    events = detect_liq_sweep(prev, new_map, prev_price, price)
    if events:
        now = int(time.time())
        for evt in events:
            evt["ts"] = now
            state.liq_sweep_events.append(evt)


async def poll_liquidation_map(
    cg: CoinglassSource,
    coin: CoinConfig,
    state: CoinState,
    min_cluster_usd: float,
) -> None:
    """获取 1d + 7d 清算地图（30d 由 poll_liq_history 低频独立拉取）"""
    price = state.ticker.last if state.ticker else 0

    for cycle in ("1d", "7d"):
        data = await cg.fetch_liquidation_aggregated_map(
            coin.symbol_cg, range_=cycle,
        )
        if not data:
            continue

        liq_map = parse_liquidation_map(data, coin.ccy, cycle, current_price=price)
        if liq_map and price > 0:
            liq_map = process_liquidation_map(
                liq_map, price,
                min_cluster_usd,
            )
            if cycle == "1d":
                detect_and_store_sweep(state, liq_map, price)
        if liq_map:
            state.liq_maps[cycle] = liq_map


async def poll_liq_heatmap(
    cg: CoinglassSource,
    coin: CoinConfig,
    state: CoinState,
) -> None:
    """获取清算热力图（model1，仅 24h 以节省配额）"""
    for range_ in ("24h",):
        data = await cg.fetch_liquidation_aggregated_heatmap(
            coin.symbol_cg, range_=range_, model=1,
        )
        if not data:
            continue
        points: list[HeatmapDataPoint] = []
        try:
            if isinstance(data, dict):
                prices = data.get("prices", data.get("y", []))
                time_list = data.get("time", data.get("x", []))
                heat_data = data.get("data", data.get("z", []))
                if isinstance(heat_data, list):
                    for t_idx, row in enumerate(heat_data):
                        ts_val = int(time_list[t_idx]) if t_idx < len(time_list) else 0
                        if not isinstance(row, list):
                            continue
                        for p_idx, val in enumerate(row):
                            if val and float(val) > 0 and p_idx < len(prices):
                                points.append(HeatmapDataPoint(
                                    price=float(prices[p_idx]),
                                    value=float(val),
                                    ts=ts_val,
                                ))
            elif isinstance(data, list):
                for item in data:
                    if isinstance(item, dict):
                        points.append(HeatmapDataPoint(
                            price=float(item.get("price", 0)),
                            value=float(item.get("value", item.get("vol", 0))),
                            ts=int(item.get("time", item.get("ts", 0))),
                        ))
        except Exception:
            logger.debug("heatmap parse failed | coin=%s range=%s", coin.ccy, range_, exc_info=True)
        state.liq_heatmaps[f"m1_{range_}"] = HeatmapData(
            coin=coin.ccy, ts=int(time.time()),
            model=1, range=range_, data=points,
        )


async def poll_liq_max_pain(
    cg: CoinglassSource,
    supported_coins: list[str],
    states: dict[str, CoinState],
) -> None:
    """获取清算最大痛点（24h + 7d 分别存储，避免覆盖）"""
    for range_ in ("24h", "7d"):
        data = await cg.fetch_liquidation_max_pain(range_=range_)
        if not data:
            continue
        items = []
        for item in data:
            try:
                items.append(LiqMaxPainItem(
                    symbol=item.get("symbol", ""),
                    price=float(item.get("price", 0)),
                    long_liq_usd=float(item.get("long_liq_usd", item.get("longLiqUsd", 0))),
                    short_liq_usd=float(item.get("short_liq_usd", item.get("shortLiqUsd", 0))),
                ))
            except (ValueError, KeyError):
                continue

        pain_data = LiqMaxPainData(
            ts=int(time.time()), range=range_, items=items,
        )
        for ccy in supported_coins:
            state = states[ccy]
            state.liq_max_pain[range_] = pain_data


async def poll_liq_history(
    cg: CoinglassSource,
    coin: CoinConfig,
    state: CoinState,
    min_cluster_usd: float,
) -> None:
    """获取聚合爆仓历史 + 30d 清算地图"""
    data = await cg.fetch_liquidation_aggregated_history(
        coin.symbol_cg, interval="1h", limit=24,
    )
    if data and isinstance(data, list):
        points = []
        total_long = 0.0
        total_short = 0.0
        long_count = 0
        short_count = 0
        for item in data:
            try:
                long_usd = float(item.get("aggregated_long_liquidation_usd",
                                 item.get("longVolUsd", item.get("longLiqUsd", 0))))
                short_usd = float(item.get("aggregated_short_liquidation_usd",
                                  item.get("shortVolUsd", item.get("shortLiqUsd", 0))))
                lc = int(item.get("longCount", item.get("longLiqCount", 0)) or 0)
                sc = int(item.get("shortCount", item.get("shortLiqCount", 0)) or 0)
                points.append(LiqHistoryPoint(
                    ts=int(item.get("time", item.get("t", 0))),
                    long_usd=long_usd, short_usd=short_usd,
                    long_count=lc, short_count=sc,
                ))
                total_long += long_usd
                total_short += short_usd
                long_count += lc
                short_count += sc
            except (ValueError, KeyError):
                continue

        state.liq_history = LiqHistoryData(
            coin=coin.ccy, interval="1h", data=points,
        )

        ratio = total_long / total_short if total_short > 0 else (10.0 if total_long > 0 else 1.0)
        state.liq_stats = LiquidationStats(
            coin=coin.ccy, ts=int(time.time()),
            period_min=1440,
            long_total_usd=total_long, short_total_usd=total_short,
            long_count=long_count, short_count=short_count,
            ratio=round(ratio, 2),
        )

    map_30d = await cg.fetch_liquidation_aggregated_map(
        coin.symbol_cg, range_="30d",
    )
    if map_30d:
        price = state.ticker.last if state.ticker else 0
        liq_map = parse_liquidation_map(map_30d, coin.ccy, "30d", current_price=price)
        if liq_map and price > 0:
            liq_map = process_liquidation_map(
                liq_map, price,
                min_cluster_usd,
            )
        if liq_map:
            state.liq_maps["30d"] = liq_map


async def poll_global_liq(
    cg: CoinglassSource,
    supported_coins: list[str],
    states: dict[str, CoinState],
) -> None:
    """获取全网爆仓统计（1h + 24h）"""
    data_24h = await cg.fetch_liquidation_exchange_list(range_="24h")
    data_1h = await cg.fetch_liquidation_exchange_list(range_="1h")

    long_24h, short_24h = _extract_all_row(data_24h)
    long_1h, short_1h = _extract_all_row(data_1h)

    ratio_24h = long_24h / short_24h if short_24h > 0 else 1.0
    ratio_1h = long_1h / short_1h if short_1h > 0 else 1.0

    gliq = GlobalLiquidationData(
        ts=int(time.time()),
        long_24h_usd=long_24h, short_24h_usd=short_24h,
        ratio_24h=round(ratio_24h, 2),
        long_1h_usd=long_1h, short_1h_usd=short_1h,
        ratio_1h=round(ratio_1h, 2),
    )

    for ccy in supported_coins:
        states[ccy].global_liq = gliq
