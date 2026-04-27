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
    """解析 Coinglass V4 聚合清算地图（aggregated-map）数据。

    真实 API 返回结构（已抓包验证，dump @ data/liq_endpoint_dumps/agg-map__1d.json）：
      {
        "code": "0",
        "data": {
          "data": [
            {
              "liqMapV2": {"<price>": [[price, usd, null, null], ...], ...},
              "instrument": {"exName": "Binance"|"OKX"|"Bybit", ...}
            },
            ...  # 每个交易所一项，并列而非合并
          ],
          "last_price": 77860
        }
      }

    本函数职责：
      1. 按 `instrument.exName` 分组累加 → `by_exchange`（保留分交易所明细）
      2. 跨交易所合并 → 一组 `LiqLeverageGroup(leverage="all")`（供 processor 与可视化）
      3. 兼容老路径：`raw_response=False` 时上层可能传进来已被 _request 解包的对象
         （表现为顶层就是 list 或 dict.data 直接是 list）
    """
    try:
        # ── 标准化嵌套 ──
        # 优先按真实 V4 结构提取：data.data + data.last_price
        # 兼容老路径：data 直接是 list；或 data.data 是 list（_request 解包后）
        inner_list: Any
        last_price: float
        if isinstance(data, list):
            inner_list = data
            last_price = current_price
        elif isinstance(data, dict):
            outer = data.get("data", [])
            last_price = float(data.get("last_price", 0) or 0)
            if isinstance(outer, dict):
                # 真实 V4：data.data: list, data.last_price 在第二层
                if last_price <= 0:
                    last_price = float(outer.get("last_price", 0) or 0)
                inner_list = outer.get("data", [])
            else:
                inner_list = outer
            if last_price <= 0:
                last_price = current_price
        else:
            return None

        if not isinstance(inner_list, list) or not inner_list or last_price <= 0:
            return None

        # ── 分交易所累加 + 跨所合并 ──
        # by_exchange[exName][price_str] = usd_total（同所内同价位多 entry 累加）
        # merged[price_int] = usd_total（跨所合并）
        by_exchange: dict[str, dict[str, float]] = {}
        merged: dict[int, float] = {}

        for exchange_item in inner_list:
            if not isinstance(exchange_item, dict):
                continue
            liq_map_v2 = exchange_item.get("liqMapV2", {})
            if not isinstance(liq_map_v2, dict):
                continue
            instrument = exchange_item.get("instrument") or {}
            ex_name = str(instrument.get("exName", "Unknown")) if isinstance(instrument, dict) else "Unknown"
            ex_bucket = by_exchange.setdefault(ex_name, {})

            for price_str, entries in liq_map_v2.items():
                if not isinstance(entries, list):
                    continue
                # API 是 {price_str: [[price, usd, null, null], ...]}
                # 同价位可能多条（不同子档位/置信），按 entry[1] 累加
                ex_sum = 0.0
                for entry in entries:
                    if isinstance(entry, list) and len(entry) >= 2:
                        try:
                            ex_sum += float(entry[1])
                        except (ValueError, TypeError):
                            continue
                if ex_sum <= 0:
                    continue
                ex_bucket[price_str] = ex_bucket.get(price_str, 0.0) + ex_sum
                try:
                    price_key = int(float(price_str))
                except (ValueError, TypeError):
                    continue
                merged[price_key] = merged.get(price_key, 0.0) + ex_sum

        if not merged:
            return None

        short_bands: list[LiqBand] = []
        long_bands: list[LiqBand] = []
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
            by_exchange=by_exchange or None,
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


def parse_liq_heatmap(data: Any, coin: str, range_: str) -> Optional[HeatmapData]:
    """解析 Coinglass `aggregated-heatmap/model1` 数据。

    真实 API 返回结构（已抓包验证，dump @ data/liq_endpoint_dumps/agg-heatmap-m1__24h.json）：
      {
        "y_axis": [74604.75, 74665.24, ..., 82528.94],   # 价格刻度
        "liquidation_leverage_data": [
          [time_idx, price_idx, usd],   # 三元组（稀疏）
          ...
        ],
        "price_candlesticks": [
          [ts_sec, open, high, low, close, volume],
          ...
        ],
        "update_time": 1777283472660
      }

    旧解析（prices/y/x/data/z）完全错位 → 这是 P0：旧代码永远写出空 points。

    本函数职责：把"时间×价格"二维稀疏网格压缩为"价格维度汇总"（每个 price 的总
    清算量），而不是保留全部时空粒度。理由：
      1. 下游消费者（NOFX hotspots/AI prompts/前端）都只看价格维度峰值
      2. 全网格点数巨大（30923 行），保留浪费内存与序列化带宽
      3. 时间维度信息保留 `price_candlesticks` 的最近一根 close 用于 last_price
    """
    if not isinstance(data, dict):
        return None
    try:
        y_axis = data.get("y_axis", []) or []
        sparse = data.get("liquidation_leverage_data", []) or []
        candlesticks = data.get("price_candlesticks", []) or []

        if not isinstance(y_axis, list) or not isinstance(sparse, list):
            return None
        if not y_axis:
            return None

        # 价格维度汇总：price_idx -> usd_total
        agg: dict[int, float] = {}
        for triple in sparse:
            if not isinstance(triple, list) or len(triple) < 3:
                continue
            try:
                p_idx = int(triple[1])
                usd = float(triple[2])
            except (ValueError, TypeError):
                continue
            if usd <= 0 or p_idx < 0 or p_idx >= len(y_axis):
                continue
            agg[p_idx] = agg.get(p_idx, 0.0) + usd

        # 取最近一根 K 线 close 时间戳作为 ts（若无则用当前）
        ref_ts = 0
        if isinstance(candlesticks, list) and candlesticks:
            last = candlesticks[-1]
            if isinstance(last, list) and last:
                try:
                    ref_ts = int(last[0])
                except (ValueError, TypeError):
                    ref_ts = 0

        points: list[HeatmapDataPoint] = []
        for p_idx in sorted(agg.keys()):
            try:
                price = float(y_axis[p_idx])
            except (ValueError, TypeError):
                continue
            points.append(HeatmapDataPoint(
                price=price,
                value=agg[p_idx],
                ts=ref_ts,
            ))

        return HeatmapData(
            coin=coin, ts=int(time.time()),
            model=1, range=range_, data=points,
        )
    except Exception:
        logger.error("Parse liq heatmap failed | coin=%s range=%s", coin, range_, exc_info=True)
        return None


async def poll_liq_heatmap(
    cg: CoinglassSource,
    coin: CoinConfig,
    state: CoinState,
    ranges: tuple[str, ...] = ("24h", "7d"),
) -> None:
    """获取清算热力图（aggregated-heatmap/model1）。

    state key 约定（修正旧版 P0 错位）：
      - state.liq_heatmaps["24h"] / ["7d"]  ← 直接用 range 字符串
      - 旧版本写入 "m1_24h" 与下游读取 "24h" 不一致，导致 nofx_builder/engine 永远拿到 None
    """
    for range_ in ranges:
        data = await cg.fetch_liquidation_aggregated_heatmap(
            coin.symbol_cg, range_=range_, model=1,
        )
        if not data:
            continue
        parsed = parse_liq_heatmap(data, coin.ccy, range_)
        if parsed is None:
            # 不写入空数据，避免覆盖上一周期的好数据
            continue
        state.liq_heatmaps[range_] = parsed


async def poll_liq_max_pain(
    cg: CoinglassSource,
    supported_coins: list[str],
    states: dict[str, CoinState],
) -> None:
    """获取清算最大痛点（24h + 7d 分别存储，避免覆盖）。

    真实 API 返回（已抓包验证，dump @ data/liq_endpoint_dumps/max-pain__24h.json）：
      [
        {
          "symbol": "BTC", "price": 77903.2,
          "long_max_pain_liq_level": 86909802.27,   # 多头痛点 USD 量
          "long_max_pain_liq_price": 76963.86,      # 多头痛点价
          "short_max_pain_liq_level": 86909802.27,  # 空头痛点 USD 量
          "short_max_pain_liq_price": 78536.6       # 空头痛点价
        },
        ...  # 共 ~566 个币种
      ]

    旧版字段名 `long_liq_usd / short_liq_usd` 完全不存在 → P0：旧代码永远写 0，
    且把"价格"信息整个丢了（max-pain 数据死链路的根因）。本版按真实 4 字段重写。

    优化：API 返回 ~566 个币种，但本系统只需 supported_coins（BTC/ETH/SOL）3 个；
    在解析时按 symbol 过滤可避免把无关数据写入 state（556+ 条无效 items）。
    """
    supported_set = set(supported_coins)
    for range_ in ("24h", "7d"):
        data = await cg.fetch_liquidation_max_pain(range_=range_)
        if not data or not isinstance(data, list):
            continue
        items: list[LiqMaxPainItem] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            symbol = str(item.get("symbol", ""))
            if symbol not in supported_set:
                continue
            try:
                items.append(LiqMaxPainItem(
                    symbol=symbol,
                    price=float(item.get("price", 0) or 0),
                    long_pain_price=float(item.get("long_max_pain_liq_price", 0) or 0),
                    long_pain_usd=float(item.get("long_max_pain_liq_level", 0) or 0),
                    short_pain_price=float(item.get("short_max_pain_liq_price", 0) or 0),
                    short_pain_usd=float(item.get("short_max_pain_liq_level", 0) or 0),
                ))
            except (ValueError, TypeError):
                continue

        if not items:
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
