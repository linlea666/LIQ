"""MTF 扩展 · 日线/周线市场结构单测

覆盖：
- _strip_unclosed_last: 未收盘最后一根被丢弃 / 已收盘保留
- recompute_market_structure_daily: 填充 state.market_structure_1d
- recompute_market_structure_weekly: 参数差异化（fractal_k=2, min_candles=30）生效
- poll_candles_daily / _weekly: 成功拉取后同步触发 recompute
- 异常隔离：recompute 抛异常不污染 state
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from config.settings import CoinConfig
from models.market import CandleData
from models.market_structure import MarketStructure


def _mk_candles(
    n: int, bar_sec: int, *, close_last_bar: bool, price_drift: float = 0.5,
) -> list[CandleData]:
    """构造 n 根锯齿上行 K 线，最后一根要么"已收盘"要么"未收盘"。

    close_last_bar=True → 最后一根 bar 起始时间 <= now - bar_sec（已收盘）
    close_last_bar=False → 最后一根 bar 起始时间 > now - bar_sec（未收盘）
    """
    now = int(time.time())
    # 已收盘：最后一根起始点 = now - bar_sec - 1（刚好完整）
    # 未收盘：最后一根起始点 = now - bar_sec + 60（距 now 仅 1 分钟差，远未到完整 bar）
    last_start = now - bar_sec - 1 if close_last_bar else now - 60
    candles = []
    price = 100.0
    for i in range(n):
        ts = last_start - (n - 1 - i) * bar_sec
        drift = i * price_drift
        wobble = 2.0 if i % 10 < 5 else -1.5
        c = price + drift + wobble
        candles.append(CandleData(
            coin="BTC", ts=ts,
            o=c - 0.5, h=c + 1.5, l=c - 1.5, c=c, vol=100.0,
        ))
    return candles


def _mk_state(coin: str = "BTC"):
    return SimpleNamespace(
        coin=coin,
        candles_1h=[],
        candles_daily=[],
        candles_weekly=[],
        market_structure=None,
        market_structure_1d=None,
        market_structure_1w=None,
        _prev_ms_summary=(),
        _prev_ms_summary_1d=(),
        _prev_ms_summary_1w=(),
        _log_once_keys=set(),
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# _strip_unclosed_last
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestStripUnclosedLast:
    def test_drops_unclosed_last_bar(self):
        from polls.candles import _DAILY_BAR_SEC, _strip_unclosed_last

        candles = _mk_candles(10, _DAILY_BAR_SEC, close_last_bar=False)
        result = _strip_unclosed_last(candles, _DAILY_BAR_SEC)
        assert len(result) == 9, "未收盘最后一根必须被丢"

    def test_keeps_closed_last_bar(self):
        from polls.candles import _DAILY_BAR_SEC, _strip_unclosed_last

        candles = _mk_candles(10, _DAILY_BAR_SEC, close_last_bar=True)
        result = _strip_unclosed_last(candles, _DAILY_BAR_SEC)
        assert len(result) == 10, "已收盘最后一根应保留"

    def test_empty_returns_empty(self):
        from polls.candles import _DAILY_BAR_SEC, _strip_unclosed_last

        assert _strip_unclosed_last([], _DAILY_BAR_SEC) == []

    def test_single_bar_returns_as_is(self):
        """只有 1 根 bar 时，不论是否收盘都保留（避免把唯一数据丢光）。"""
        from polls.candles import _DAILY_BAR_SEC, _strip_unclosed_last

        single = [CandleData(
            coin="BTC", ts=int(time.time()),
            o=1, h=1, l=1, c=1, vol=1,
        )]
        assert len(_strip_unclosed_last(single, _DAILY_BAR_SEC)) == 1


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# recompute_market_structure_daily
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestRecomputeDaily:
    def test_populates_state_on_sufficient_data(self):
        from polls.candles import _DAILY_BAR_SEC, recompute_market_structure_daily

        state = _mk_state()
        state.candles_daily = _mk_candles(80, _DAILY_BAR_SEC, close_last_bar=True)
        recompute_market_structure_daily(state)

        assert isinstance(state.market_structure_1d, MarketStructure)
        assert state.market_structure_1d.timeframe == "1d"

    def test_empty_candles_no_op(self):
        from polls.candles import recompute_market_structure_daily

        state = _mk_state()
        recompute_market_structure_daily(state)
        assert state.market_structure_1d is None

    def test_exception_preserves_previous_value(self):
        from polls import candles as candles_mod

        state = _mk_state()
        state.candles_daily = _mk_candles(
            80, candles_mod._DAILY_BAR_SEC, close_last_bar=True,
        )
        candles_mod.recompute_market_structure_daily(state)
        prior = state.market_structure_1d
        assert prior is not None

        with patch.object(candles_mod, "detect_market_structure",
                          side_effect=RuntimeError("boom")):
            candles_mod.recompute_market_structure_daily(state)
        assert state.market_structure_1d is prior


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# recompute_market_structure_weekly
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestRecomputeWeekly:
    def test_passes_weekly_specific_params(self):
        """周线必须用 fractal_k=2, min_candles=30, min_gap_pct=3.0。"""
        from polls import candles as candles_mod

        state = _mk_state()
        state.candles_weekly = _mk_candles(
            40, candles_mod._WEEKLY_BAR_SEC, close_last_bar=True,
        )

        with patch.object(candles_mod, "detect_market_structure") as m:
            m.return_value = MarketStructure(timeframe="1w", direction="bullish")
            candles_mod.recompute_market_structure_weekly(state)

        # 断言传参
        assert m.call_count == 1
        kwargs = m.call_args.kwargs
        assert kwargs.get("timeframe") == "1w"
        assert kwargs.get("fractal_k") == candles_mod._WEEKLY_FRACTAL_K == 2
        assert kwargs.get("min_candles") == candles_mod._WEEKLY_MIN_CANDLES == 30
        assert kwargs.get("min_gap_pct") == candles_mod._WEEKLY_MIN_GAP_PCT == 3.0

    def test_populates_state_on_sufficient_data(self):
        from polls.candles import _WEEKLY_BAR_SEC, recompute_market_structure_weekly

        state = _mk_state()
        # 40 bars（> min_candles=30）
        state.candles_weekly = _mk_candles(40, _WEEKLY_BAR_SEC, close_last_bar=True)
        recompute_market_structure_weekly(state)

        assert isinstance(state.market_structure_1w, MarketStructure)
        assert state.market_structure_1w.timeframe == "1w"

    def test_drops_unclosed_bar_before_detect(self):
        """周线最后一根未收盘 → detect 收到的 bar 数少 1。"""
        from polls import candles as candles_mod

        state = _mk_state()
        state.candles_weekly = _mk_candles(
            40, candles_mod._WEEKLY_BAR_SEC, close_last_bar=False,
        )
        with patch.object(candles_mod, "detect_market_structure") as m:
            m.return_value = MarketStructure(timeframe="1w", direction="bullish")
            candles_mod.recompute_market_structure_weekly(state)

        passed_candles = m.call_args.args[0]
        assert len(passed_candles) == 39, "未收盘最后一根必须被丢（40→39）"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# poll_candles_daily / _weekly 触发链路
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestPollTriggersRecompute:
    def test_poll_daily_calls_recompute(self):
        from polls import candles as candles_mod
        from polls.candles import poll_candles_daily

        coin = CoinConfig(
            ccy="BTC", symbol_cg="BTC", symbol_cg_pair="BTCUSDT",
            exchange_primary="Binance",
        )
        state = _mk_state()
        fake_klines = [
            [int(time.time() * 1000) - i * 86400_000,
             str(100.0 + i * 0.1), str(101.0), str(99.0), str(100.5), "100"]
            for i in range(10)
        ]
        bn = SimpleNamespace(fetch_klines=AsyncMock(return_value=fake_klines))

        with patch.object(candles_mod, "recompute_market_structure_daily") as m:
            asyncio.run(poll_candles_daily(None, coin, state, bn=bn))

        assert m.call_count == 1
        assert m.call_args.args[0] is state

    def test_poll_weekly_calls_recompute(self):
        from polls import candles as candles_mod
        from polls.candles import poll_candles_weekly

        coin = CoinConfig(
            ccy="BTC", symbol_cg="BTC", symbol_cg_pair="BTCUSDT",
            exchange_primary="Binance",
        )
        state = _mk_state()
        fake_klines = [
            [int(time.time() * 1000) - i * 86400_000 * 7,
             str(100.0 + i * 0.1), str(101.0), str(99.0), str(100.5), "100"]
            for i in range(10)
        ]
        bn = SimpleNamespace(fetch_klines=AsyncMock(return_value=fake_klines))

        with patch.object(candles_mod, "recompute_market_structure_weekly") as m:
            asyncio.run(poll_candles_weekly(None, coin, state, bn=bn))

        assert m.call_count == 1
        assert m.call_args.args[0] is state

    def test_poll_daily_empty_does_not_recompute(self):
        """接口返回空数据时不应触发 recompute。"""
        from polls import candles as candles_mod
        from polls.candles import poll_candles_daily

        coin = CoinConfig(
            ccy="BTC", symbol_cg="BTC", symbol_cg_pair="BTCUSDT",
            exchange_primary="Binance",
        )
        state = _mk_state()
        bn = SimpleNamespace(fetch_klines=AsyncMock(return_value=[]))

        with patch.object(candles_mod, "recompute_market_structure_daily") as m:
            asyncio.run(poll_candles_daily(None, coin, state, bn=bn))

        assert m.call_count == 0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 日志节流（相同结构只 INFO 一次）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestLogThrottle:
    def test_daily_unchanged_only_first_info(self, caplog):
        from polls.candles import _DAILY_BAR_SEC, recompute_market_structure_daily

        state = _mk_state()
        state.candles_daily = _mk_candles(80, _DAILY_BAR_SEC, close_last_bar=True)

        with caplog.at_level(logging.INFO, logger="polls.candles"):
            recompute_market_structure_daily(state)
        info_first = [
            r for r in caplog.records
            if r.levelno == logging.INFO and "日线结构(1d)" in r.message
        ]
        assert len(info_first) == 1

        caplog.clear()
        with caplog.at_level(logging.INFO, logger="polls.candles"):
            recompute_market_structure_daily(state)
        info_second = [
            r for r in caplog.records
            if r.levelno == logging.INFO and "日线结构(1d)" in r.message
        ]
        assert info_second == [], "结构无变化时不应再 INFO 日志"
