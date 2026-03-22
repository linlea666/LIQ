"""订单簿分析：大单检测、买卖墙识别"""

from __future__ import annotations

import logging

from models.market import OrderBookAnalysis, OrderBookLevel, OrderBookSnapshot, WallInfo

logger = logging.getLogger(__name__)


def analyze_orderbook(
    snapshot: OrderBookSnapshot,
    current_price: float,
    wall_threshold_size: float = 50.0,
    wall_threshold_usd: float = 0,
    top_n: int = 5,
) -> OrderBookAnalysis:
    """
    分析订单簿快照：
    1. 识别大买墙/卖墙（优先使用 USD 阈值）
    2. 计算总深度
    3. 计算价差
    """
    usd_threshold = wall_threshold_usd if wall_threshold_usd > 0 else wall_threshold_size * current_price

    bid_walls: list[WallInfo] = []
    ask_walls: list[WallInfo] = []
    bid_total = 0.0
    ask_total = 0.0

    for level in snapshot.bids:
        usd_value = level.size * current_price
        bid_total += usd_value
        if usd_value >= usd_threshold:
            bid_walls.append(WallInfo(
                price=level.price,
                size=level.size,
                size_usd=usd_value,
                order_count=level.order_count,
            ))

    for level in snapshot.asks:
        usd_value = level.size * current_price
        ask_total += usd_value
        if usd_value >= usd_threshold:
            ask_walls.append(WallInfo(
                price=level.price,
                size=level.size,
                size_usd=usd_value,
                order_count=level.order_count,
            ))

    bid_walls.sort(key=lambda w: w.size, reverse=True)
    ask_walls.sort(key=lambda w: w.size, reverse=True)

    spread_pct = 0.0
    if snapshot.asks and snapshot.bids:
        best_ask = snapshot.asks[0].price
        best_bid = snapshot.bids[0].price
        mid = (best_ask + best_bid) / 2
        spread_pct = (best_ask - best_bid) / mid * 100 if mid > 0 else 0

    return OrderBookAnalysis(
        coin=snapshot.coin,
        ts=snapshot.ts,
        bid_walls=bid_walls[:top_n],
        ask_walls=ask_walls[:top_n],
        bid_total_usd=round(bid_total, 2),
        ask_total_usd=round(ask_total, 2),
        spread_pct=round(spread_pct, 4),
    )


def parse_okx_orderbook(data: dict, coin: str) -> OrderBookSnapshot | None:
    """解析 OKX WebSocket books5 推送数据"""
    try:
        arg = data.get("arg", {})
        book_data = data.get("data", [{}])[0]

        asks = [
            OrderBookLevel(
                price=float(a[0]),
                size=float(a[1]),
                order_count=int(a[3]) if len(a) > 3 else 0,
            )
            for a in book_data.get("asks", [])
        ]
        bids = [
            OrderBookLevel(
                price=float(b[0]),
                size=float(b[1]),
                order_count=int(b[3]) if len(b) > 3 else 0,
            )
            for b in book_data.get("bids", [])
        ]

        return OrderBookSnapshot(
            coin=coin,
            ts=int(book_data.get("ts", 0)),
            asks=asks,
            bids=bids,
            source="okx",
        )
    except Exception:
        logger.error("Failed to parse OKX orderbook | coin=%s", coin, exc_info=True)
        return None
