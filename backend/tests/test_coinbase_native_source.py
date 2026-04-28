"""Coinbase 原生源 + 解析 + product_id 派生逻辑测试（Phase C）。

覆盖：
  parse_orderbook_frame
    - bids/asks 升序排序
    - num_orders 缺失时默认 1
    - 字符串价格/数量转 float
    - 异常档位（非数字 / 价格 0 / 数量 0）静默跳过
    - bids+asks 全空 → 返回 None
    - 缺少 bids/asks key → 返回 None
    - 含 sequence 与 time 字段
  _resolve_product_id（polls.coinbase_orderbook）
    - symbol_coinbase 缺省 → 自动派生 f"{ccy}-USD"
    - symbol_coinbase = "" → 显式禁用 → None
    - symbol_coinbase = "BTC-USDC" → 直接使用
    - symbol_coinbase 含空白 → strip 后判空
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from models.coinbase_orderbook import CoinbaseBookLevel, CoinbaseOrderbookFrame
from polls.coinbase_orderbook import _resolve_product_id
from sources.coinbase_native import parse_orderbook_frame


# ──────────────────────────────────────────────────────────────────────
# parse_orderbook_frame
# ──────────────────────────────────────────────────────────────────────
def _coin_cfg(ccy: str = "BTC", symbol_coinbase=None) -> SimpleNamespace:
    """模拟 CoinConfig dataclass（使用 SimpleNamespace 跳过 frozen 限制）。"""
    return SimpleNamespace(ccy=ccy, symbol_coinbase=symbol_coinbase)


def test_parse_orderbook_frame_basic():
    raw = {
        "bids": [["100.5", "0.5", 3], ["99.0", "1.0", 5], ["100.0", "0.7", 2]],
        "asks": [["101.0", "0.4", 1], ["102.5", "0.8", 4]],
        "sequence": 12345,
        "time": "2026-04-28T17:44:20.773321552Z",
    }
    frame = parse_orderbook_frame("BTC", "BTC-USD", raw)
    assert frame is not None
    assert isinstance(frame, CoinbaseOrderbookFrame)
    assert frame.coin == "BTC"
    assert frame.product_id == "BTC-USD"
    assert frame.sequence == 12345
    assert frame.api_ts_iso.startswith("2026-04-28")

    # 升序排序
    assert [b.price for b in frame.bids] == [99.0, 100.0, 100.5]
    assert [a.price for a in frame.asks] == [101.0, 102.5]
    assert frame.bid_count == 3
    assert frame.ask_count == 2

    # num_orders 转 int
    assert frame.bids[0].num_orders == 5
    assert frame.bids[2].num_orders == 3


def test_parse_orderbook_frame_num_orders_missing_defaults_to_1():
    raw = {
        "bids": [["100.0", "1.0"]],     # 只有 2 个字段
        "asks": [["101.0", "0.5", 0]],  # num_orders=0 应被 max(_, 1) 提到 1
    }
    frame = parse_orderbook_frame("ETH", "ETH-USD", raw)
    assert frame is not None
    assert frame.bids[0].num_orders == 1
    assert frame.asks[0].num_orders == 1   # 被 max(0, 1) 提升


def test_parse_orderbook_frame_string_to_float():
    raw = {
        "bids": [["1.234e5", "0.001", 1]],
        "asks": [["123456.789", "0.5", 2]],
    }
    frame = parse_orderbook_frame("BTC", "BTC-USD", raw)
    assert frame.bids[0].price == 123400.0
    assert frame.bids[0].size == 0.001
    assert frame.asks[0].price == 123456.789


def test_parse_orderbook_frame_skips_invalid_levels():
    raw = {
        "bids": [
            ["abc", "1.0", 1],          # 价格非数字 → 跳
            ["100.0", "0", 1],          # size=0 → 跳
            ["0", "1.0", 1],            # price=0 → 跳
            ["100.0"],                  # 缺 size → 跳（len < 2）
            ["99.0", "1.0", 1],         # 合法
        ],
        "asks": [["101.0", "0.5", 1]],
    }
    frame = parse_orderbook_frame("BTC", "BTC-USD", raw)
    assert frame is not None
    assert len(frame.bids) == 1
    assert frame.bids[0].price == 99.0


def test_parse_orderbook_frame_empty_returns_none():
    raw = {"bids": [], "asks": []}
    assert parse_orderbook_frame("BTC", "BTC-USD", raw) is None


def test_parse_orderbook_frame_missing_keys_returns_none():
    assert parse_orderbook_frame("BTC", "BTC-USD", {}) is None
    assert parse_orderbook_frame("BTC", "BTC-USD", {"bids": []}) is None


def test_parse_orderbook_frame_non_dict_returns_none():
    assert parse_orderbook_frame("BTC", "BTC-USD", []) is None
    assert parse_orderbook_frame("BTC", "BTC-USD", None) is None
    assert parse_orderbook_frame("BTC", "BTC-USD", "string") is None


def test_parse_orderbook_frame_bad_sequence_falls_back_to_none():
    raw = {
        "bids": [["100", "1.0", 1]],
        "asks": [["101", "1.0", 1]],
        "sequence": "not-a-number",
    }
    frame = parse_orderbook_frame("BTC", "BTC-USD", raw)
    assert frame is not None
    assert frame.sequence is None


def test_book_level_usd_value():
    lv = CoinbaseBookLevel(price=100.0, size=2.5, num_orders=3)
    assert lv.usd_value == 250.0


# ──────────────────────────────────────────────────────────────────────
# _resolve_product_id
# ──────────────────────────────────────────────────────────────────────
def test_resolve_product_id_default_derives_from_ccy():
    coin = _coin_cfg(ccy="BTC", symbol_coinbase=None)
    assert _resolve_product_id(coin) == "BTC-USD"

    coin = _coin_cfg(ccy="ETH", symbol_coinbase=None)
    assert _resolve_product_id(coin) == "ETH-USD"


def test_resolve_product_id_explicit_uses_provided():
    coin = _coin_cfg(ccy="BTC", symbol_coinbase="BTC-USDC")
    assert _resolve_product_id(coin) == "BTC-USDC"


def test_resolve_product_id_empty_string_disables():
    coin = _coin_cfg(ccy="SUI", symbol_coinbase="")
    assert _resolve_product_id(coin) is None


def test_resolve_product_id_whitespace_only_disables():
    coin = _coin_cfg(ccy="SUI", symbol_coinbase="   ")
    assert _resolve_product_id(coin) is None


def test_resolve_product_id_strips_whitespace():
    coin = _coin_cfg(ccy="BTC", symbol_coinbase="  BTC-USD  ")
    assert _resolve_product_id(coin) == "BTC-USD"


def test_resolve_product_id_missing_attribute_falls_back_to_ccy():
    """若 CoinConfig 是旧版（没有 symbol_coinbase 属性），应自动派生。"""
    coin = SimpleNamespace(ccy="BTC")  # 无 symbol_coinbase 属性
    assert _resolve_product_id(coin) == "BTC-USD"
