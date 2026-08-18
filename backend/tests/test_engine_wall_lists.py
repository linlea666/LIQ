"""engine.build_wall_lists：WS 推送墙列表的过滤契约。

背景：legacy 回填曾无 holding / 穿越校验，导致 ended 单与
方向穿越现价的脏数据进 UI（卖墙价低于买墙的荒谬展示）。
"""
from __future__ import annotations

from engine import build_wall_lists
from models.orderbook_ext import LargeOrder


def _order(price: float, side: str, usd: float, status: str = "active") -> LargeOrder:
    return LargeOrder(
        ts=0, exchange="Binance", symbol="BTCUSDT",
        price=price, size_usd=usd, side=side, status=status,
    )


def test_crossing_orders_filtered():
    """买墙必须在现价下方、卖墙在现价上方，穿越者剔除。"""
    orders = [
        _order(64000, "bid", 10e6),   # 合法买墙（下方）
        _order(66000, "bid", 20e6),   # 穿越：bid 在现价上方 → 剔除
        _order(66500, "ask", 15e6),   # 合法卖墙（上方）
        _order(52000, "ask", 42e6),   # 穿越：ask 在现价下方 → 剔除
    ]
    bid_walls, ask_walls = build_wall_lists(orders, last_px=65000.0)
    assert [w["price"] for w in bid_walls] == [64000]
    assert [w["price"] for w in ask_walls] == [66500]


def test_ended_orders_filtered():
    orders = [
        _order(64000, "bid", 10e6, status="ended"),
        _order(66500, "ask", 15e6, status="active"),
    ]
    bid_walls, ask_walls = build_wall_lists(orders, last_px=65000.0)
    assert bid_walls == []
    assert len(ask_walls) == 1


def test_per_side_top_n():
    """按侧各取 Top N，而非全局取 Top N 再拆。"""
    orders = [_order(64000 - i * 10, "bid", (10 - i) * 1e6) for i in range(8)]
    orders += [_order(66000 + i * 10, "ask", 1e6) for i in range(3)]
    bid_walls, ask_walls = build_wall_lists(orders, last_px=65000.0, top_n=5)
    assert len(bid_walls) == 5
    assert len(ask_walls) == 3
    # 按金额降序
    assert bid_walls[0]["size_usd"] >= bid_walls[-1]["size_usd"]


def test_no_last_price_keeps_sides_unfiltered():
    """现价缺失时不做穿越过滤（保守回退），但仍过滤非 active。"""
    orders = [
        _order(64000, "bid", 10e6),
        _order(52000, "ask", 42e6),
    ]
    bid_walls, ask_walls = build_wall_lists(orders, last_px=0.0)
    assert len(bid_walls) == 1
    assert len(ask_walls) == 1
