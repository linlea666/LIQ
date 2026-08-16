"""Binance aggTrade 大额成交检测单元测试（P3）。

不建真实 WS 连接，直接驱动 _handle_message 验证：
  - USD / 数量双阈值判定（whale_threshold_usd OR whale_threshold_{coin}）
  - m 字段方向映射（m=True → taker 卖）
  - pending 小时桶累加与 recent/big 事件 deque
  - 大脑事件流集成（_build_events 拉取 big_trade_events）
"""
from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

import sources.binance_trades_ws as tws_mod
from sources.binance_trades_ws import BinanceTradesWS


def _mk_ws(**kwargs) -> BinanceTradesWS:
    defaults = dict(
        coin_symbols={"BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT"},
        whale_threshold_usd=500_000.0,
        whale_threshold_qty={"BTC": 10.0, "ETH": 100.0, "SOL": 1000.0},
        big_trade_usd=5_000_000.0,
    )
    defaults.update(kwargs)
    return BinanceTradesWS(**defaults)


def _agg_msg(symbol: str, price: float, qty: float, is_maker_buy: bool,
             ts_ms: int = 0) -> str:
    return json.dumps({
        "stream": f"{symbol.lower()}@aggTrade",
        "data": {
            "e": "aggTrade", "s": symbol,
            "p": str(price), "q": str(qty), "m": is_maker_buy,
            "T": ts_ms or int(time.time() * 1000),
        },
    })


class TestFlushResilience:
    """flush 失败回滚 + 关停 final flush（防丢 pending 累计）。"""

    @pytest.mark.asyncio
    async def test_flush_failure_merges_pending_back(self, monkeypatch):
        ws = _mk_ws()
        ws._handle_message("spot", _agg_msg("BTCUSDT", 63_000, 10, False))
        assert len(ws._pending) == 1
        key = next(iter(ws._pending))
        buy_before = ws._pending[key][0]

        import processors.orderflow_stats as ofs

        def _boom():
            raise RuntimeError("store unavailable")
        monkeypatch.setattr(ofs, "get_orderflow_store", _boom)

        await ws._flush_pending()
        # 失败后 pending 不丢：桶还在且数值不变
        assert key in ws._pending
        assert ws._pending[key][0] == pytest.approx(buy_before)

    @pytest.mark.asyncio
    async def test_flush_now_writes_pending(self, monkeypatch):
        ws = _mk_ws()
        ws._handle_message("spot", _agg_msg("BTCUSDT", 63_000, 10, False))

        written = []

        class _FakeStore:
            def add_whale_trades(self, coin, market, hour_ts, buy, sell):
                written.append((coin, market, hour_ts, buy, sell))

        import processors.orderflow_stats as ofs
        monkeypatch.setattr(ofs, "get_orderflow_store", lambda: _FakeStore())

        await ws.flush_now()
        assert len(written) == 1
        assert written[0][0] == "BTC" and written[0][3] > 0
        assert not ws._pending


class _FakeResp:
    def __init__(self, rows):
        self._rows = rows
        self.status = 200

    async def json(self):
        return self._rows

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _FakeSession:
    def __init__(self, pages):
        self._pages = list(pages)
        self.calls: list[dict] = []

    def get(self, url, params=None, timeout=None):
        self.calls.append(dict(params or {}))
        return _FakeResp(self._pages.pop(0) if self._pages else [])


class TestFuturesRestPoll:
    """合约侧 REST aggTrades 轮询（fstream WS 推送不可用的替代路径）。"""

    @pytest.mark.asyncio
    async def test_poll_processes_and_tracks_from_id(self, monkeypatch):
        ws = _mk_ws()
        ts_ms = int(time.time() * 1000)
        rows = [
            {"a": 100, "p": "63000", "q": "10", "m": False, "T": ts_ms},   # $630k whale 买
            {"a": 101, "p": "63000", "q": "0.01", "m": True, "T": ts_ms},  # 小单忽略
        ]
        fake = _FakeSession([rows, [{"a": 102, "p": "63000", "q": "9",
                                     "m": True, "T": ts_ms}]])

        async def _sess():
            return fake
        monkeypatch.setattr(ws, "get_session", _sess)

        last_id: dict[str, int] = {}
        await ws._poll_futures_symbol("BTC", "BTCUSDT", last_id)
        assert last_id["BTCUSDT"] == 101
        assert fake.calls[0].get("fromId") is None          # 首轮无 fromId
        assert ws._whale_count["futures"] == 1
        key = next(iter(ws._pending))
        assert key[1] == "futures" and ws._pending[key][0] > 0

        # 第二轮：fromId = last+1 续读；qty 9 < 10 且 $567k ≥ $500k → whale 卖
        await ws._poll_futures_symbol("BTC", "BTCUSDT", last_id)
        assert fake.calls[1]["fromId"] == 102
        assert last_id["BTCUSDT"] == 102
        assert ws._whale_count["futures"] == 2


class TestWhaleDetection:
    def test_below_both_thresholds_ignored(self):
        ws = _mk_ws()
        # $63k、1 BTC：都低于阈值
        ws._handle_message("futures", _agg_msg("BTCUSDT", 63000, 1.0, False))
        assert ws._pending == {}
        assert ws.recent_whales("BTC") == []

    def test_usd_threshold_triggers(self):
        ws = _mk_ws()
        # 9 BTC × 63k = $567k ≥ 500k（数量 9 < 10）
        ws._handle_message("futures", _agg_msg("BTCUSDT", 63000, 9.0, False))
        whales = ws.recent_whales("BTC")
        assert len(whales) == 1
        assert whales[0]["side"] == "buy"
        assert whales[0]["usd"] == pytest.approx(567_000)

    def test_qty_threshold_triggers_even_below_usd(self):
        ws = _mk_ws()
        # 1200 SOL × $150 = $180k < 500k，但数量 ≥ 1000
        ws._handle_message("spot", _agg_msg("SOLUSDT", 150, 1200, True))
        whales = ws.recent_whales("SOL")
        assert len(whales) == 1
        assert whales[0]["side"] == "sell", "m=True → taker 主动卖"
        assert whales[0]["market"] == "spot"

    def test_pending_hour_bucket_accumulates(self):
        ws = _mk_ws()
        now_ms = int(time.time() * 1000)
        hour = (now_ms // 1000) - (now_ms // 1000) % 3600
        ws._handle_message("futures", _agg_msg("BTCUSDT", 63000, 10.0, False, now_ms))
        ws._handle_message("futures", _agg_msg("BTCUSDT", 63000, 12.0, True, now_ms))
        bucket = ws._pending[("BTC", "futures", hour)]
        assert bucket[0] == pytest.approx(630_000)   # buy
        assert bucket[1] == pytest.approx(756_000)   # sell

    def test_big_trade_event_recorded(self):
        ws = _mk_ws()
        # 100 BTC × 63k = $6.3M ≥ 5M
        ws._handle_message("futures", _agg_msg("BTCUSDT", 63000, 100.0, False))
        events = ws.big_trade_events("BTC")
        assert len(events) == 1
        assert events[0]["usd"] == pytest.approx(6_300_000)
        assert events[0]["side"] == "buy"
        # 普通 whale（$567k）不产生 big 事件
        ws._handle_message("futures", _agg_msg("BTCUSDT", 63000, 9.0, False))
        assert len(ws.big_trade_events("BTC")) == 1

    def test_unknown_symbol_and_bad_payload_ignored(self):
        ws = _mk_ws()
        ws._handle_message("spot", _agg_msg("DOGEUSDT", 0.1, 1e9, False))
        ws._handle_message("spot", "not json{{{")
        ws._handle_message("spot", json.dumps({"data": {"e": "trade"}}))
        assert ws._pending == {}

    def test_stats_shape(self):
        ws = _mk_ws()
        ws._handle_message("futures", _agg_msg("BTCUSDT", 63000, 10.0, False))
        st = ws.stats()
        assert st["msg_count"]["futures"] == 1
        assert st["whale_count"]["futures"] == 1
        assert st["whale_threshold_usd"] == 500_000.0


class TestBrainEventIntegration:
    def test_build_events_includes_big_trades(self, monkeypatch):
        ws = _mk_ws()
        ws._handle_message("spot", _agg_msg("BTCUSDT", 63000, 100.0, False))
        monkeypatch.setattr(tws_mod, "_instance", ws)

        from processors.trading_brain_builder import _build_events
        events = _build_events(None, now_sec=int(time.time()), coin="BTC")
        assert len(events) == 1
        ev = events[0]
        assert ev.source == "binance_aggtrade"
        assert ev.layer == "spot"
        assert "大额主动买入" in ev.message
        assert "$6.3M" in ev.message

    def test_build_events_no_ws_instance_silent(self, monkeypatch):
        monkeypatch.setattr(tws_mod, "_instance", None)
        from processors.trading_brain_builder import _build_events
        assert _build_events(None, now_sec=int(time.time()), coin="BTC") == []
