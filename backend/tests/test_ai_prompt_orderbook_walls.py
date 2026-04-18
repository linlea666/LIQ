"""订单簿深度 + 大单追踪渲染回归测试

背景：历史 prompt §6 曾把"大单追踪回填的 walls"标注为"订单簿 ≥50 BTC 大单"，
语义错乱让 AI 反复质疑"数据缺失/阈值不明"。

方案 B 修复后：
- §6 只展示聚合深度总额（Coinglass 订单簿快照的真实产出）
- §8d 统一展示大单追踪（含明细价格/金额/距现价）
- 空值有可消费语义，不再只留 header
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ai.prompts import build_user_prompt


def _base_snapshot(**kwargs) -> dict:
    """最小 snapshot 骨架（足以触发 §6/§8d 渲染）。"""
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
        "large_orders_buy_count": 0,
        "large_orders_sell_count": 0,
        "large_orders_net_usd": 0,
    }
    base.update(kwargs)
    return base


def test_section_6_renders_only_aggregate_depth_and_points_to_section_8d():
    """§6 只应包含聚合深度，walls 细节全部移到 §8d。"""
    up = build_user_prompt(_base_snapshot())
    assert "### 6. 订单簿聚合深度" in up
    assert "数据源: Coinglass" in up
    assert "聚合深度总额" in up or "聚合深度" in up
    # §6 不再有"主要买墙"或"主要卖墙"
    assert "主要买墙" not in up
    assert "主要卖墙" not in up
    # §6 明确引导到 §8d
    assert "§8d" in up


def test_empty_large_orders_and_walls_renders_friendly_fallback_in_section_8d():
    """大单和 walls 全空时 §8d 仍应渲染章节+友好降级提示（避免 AI 误报为'数据缺失'）。

    对应修复：此前冷启动/无活跃大单时整节不渲染，AI §八自检会列为"缺失数据(§8d)"。
    现在：章节始终存在，空时渲染"无超阈值大单活跃→非缺失，常态信号"。
    """
    up = build_user_prompt(_base_snapshot())
    assert "### 8d. 大单追踪" in up
    assert "无超阈值大单活跃" in up
    assert "非数据缺失" in up
    assert "常态信号" in up


def test_populated_walls_render_in_section_8d_with_distance_to_price():
    """有 walls 时走 §8d 渲染，且每条附带距现价百分比。"""
    walls_data = [
        {"price": 76800.0, "size_usd": 5000000, "order_count": 3},
        {"price": 76500.0, "size_usd": 3000000, "order_count": 2},
    ]
    up = build_user_prompt(_base_snapshot(
        orderbook_bid_walls=walls_data,
        orderbook_ask_walls=[{"price": 77500.0, "size_usd": 4000000, "order_count": 4}],
    ))
    assert "### 8d. 大单追踪" in up
    assert "Top 买方大单" in up
    assert "Top 卖方大单" in up
    assert "$76,800.0" in up
    assert "$76,500.0" in up
    assert "$77,500.0" in up
    # 距现价百分比必须出现，形如 "(距现价 -0.26%)"
    assert "距现价 -0.26%" in up  # 76800 vs 77000
    assert "距现价 +0.65%" in up  # 77500 vs 77000


def test_empty_walls_with_active_large_orders_still_renders_section_8d():
    """large_orders 有数据但未回填到 walls 时，§8d 仍渲染头部统计 + 空提示。"""
    up = build_user_prompt(_base_snapshot(
        large_orders_buy_count=5,
        large_orders_sell_count=3,
        large_orders_net_usd=2000000,
    ))
    assert "### 8d. 大单追踪" in up
    assert "大单买入: 5笔" in up
    assert "大单卖出" in up or "卖出: 3笔" in up
    # 空 walls 提示
    assert "近期下方无活跃大额买单挂单" in up
    assert "近期上方无活跃大额卖单挂单" in up


def test_mixed_empty_bid_populated_ask():
    """买方 walls 空 + 卖方 walls 有数据：两个分支互不干扰。"""
    up = build_user_prompt(_base_snapshot(
        orderbook_bid_walls=[],
        orderbook_ask_walls=[{"price": 77500.0, "size_usd": 4000000, "order_count": 4}],
    ))
    assert "### 8d. 大单追踪" in up
    # 买方走 fallback
    assert "近期下方无活跃大额买单挂单" in up
    # 卖方走正常渲染
    assert "Top 卖方大单" in up
    assert "$77,500.0" in up
