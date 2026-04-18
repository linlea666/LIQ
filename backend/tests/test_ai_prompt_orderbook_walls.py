"""订单簿买墙/卖墙空值友好提示回归测试

背景：生产 AI 反馈"未提供订单簿主要买墙/卖墙阈值和大单信息"，根因是
当 walls 列表为空时 prompt 只输出 header 没条目，AI 把空 header
当作"数据缺失"。

修复：空值渲染时输出"无超阈值大单 → 深度分散..."的可消费信息。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ai.prompts import build_user_prompt


def _base_snapshot(**kwargs) -> dict:
    """最小 snapshot 骨架（足以触发 §6 订单簿渲染）。"""
    base = {
        "coin": "BTC",
        "price": 77000.0,
        "high_24h": 78000.0,
        "low_24h": 76000.0,
        "orderbook_bid_total_usd": 15000000,
        "orderbook_ask_total_usd": 12000000,
        "orderbook_spread_pct": 0.05,
        "orderbook_bid_walls": [],
        "orderbook_ask_walls": [],
    }
    base.update(kwargs)
    return base


def test_empty_bid_walls_render_informative_hint():
    """买墙为空时必须渲染"深度分散"类提示，而非留空 header。"""
    up = build_user_prompt(_base_snapshot())
    assert "主要买墙" in up
    assert "无超阈值" in up
    # 关键：给 AI 可消费的语义，而不只是"无数据"
    assert "深度较分散" in up or "分散" in up


def test_empty_ask_walls_render_informative_hint():
    """卖墙为空时同样要有可消费的语义信息。"""
    up = build_user_prompt(_base_snapshot())
    assert "主要卖墙" in up
    assert "无超阈值" in up
    assert "抛压分散" in up or "分散" in up


def test_non_empty_walls_still_render_price_and_size():
    """非空列表仍然正常渲染每条买卖墙详情（不要把这个 case 弄坏）。"""
    walls_data = [
        {"price": 76800.0, "size_usd": 5000000, "order_count": 3},
        {"price": 76500.0, "size_usd": 3000000, "order_count": 2},
    ]
    up = build_user_prompt(_base_snapshot(
        orderbook_bid_walls=walls_data,
        orderbook_ask_walls=[{"price": 77500.0, "size_usd": 4000000, "order_count": 4}],
    ))
    assert "$76,800.0" in up
    assert "$76,500.0" in up
    assert "$77,500.0" in up
    # 非空时不应出现空值 fallback 文案
    assert "无超阈值" not in up


def test_mixed_bid_empty_ask_populated():
    """买墙空+卖墙非空：两种渲染分支互不干扰。"""
    up = build_user_prompt(_base_snapshot(
        orderbook_bid_walls=[],
        orderbook_ask_walls=[{"price": 77500.0, "size_usd": 4000000, "order_count": 4}],
    ))
    # 买墙走 fallback
    assert "主要买墙: 无超阈值" in up
    # 卖墙走正常渲染
    assert "主要卖墙(超阈值):" in up
    assert "$77,500.0" in up
