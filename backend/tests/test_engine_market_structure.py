"""Commit 2 接入层单元测试：recompute_market_structure

覆盖：
- 正常路径：state.candles_1h 有数据 → state.market_structure 被填充
- 空数据：不 set，state.market_structure 保持原值
- 异常路径：detect 抛异常时不污染 state（return 原值，warning 日志）
- 日志节流：同一结构重算多次只 INFO 一次，后续 DEBUG
"""
from __future__ import annotations

import logging
import os
import sys
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from models.market import CandleData
from models.market_structure import MarketStructure


def _make_state(coin="BTC", candles=None):
    """构造最小化的 state 替身（不引入 Engine/CoinState 依赖）。"""
    return SimpleNamespace(
        coin=coin,
        candles_1h=candles or [],
        market_structure=None,
        _prev_ms_summary=(),
    )


def _make_bullish_candles(n: int = 80) -> list[CandleData]:
    """构造明显上升趋势 K 线，足以产生 HH+HL 结构。"""
    candles = []
    base_ts = 1_700_000_000
    price = 100.0
    for i in range(n):
        # 锯齿上行：每 10 根一个周期，整体趋势 +0.5/bar
        drift = i * 0.5
        wobble = 2.0 if i % 10 < 5 else -1.5
        c = price + drift + wobble
        candles.append(CandleData(
            coin="BTC",
            ts=base_ts + i * 3600,
            o=c - 0.5, h=c + 1.5, l=c - 1.5, c=c,
            vol=100.0,
        ))
    return candles


class TestRecomputeMarketStructure:
    def test_populates_state_on_sufficient_data(self):
        from polls.candles import recompute_market_structure

        state = _make_state(candles=_make_bullish_candles(80))
        recompute_market_structure(state)

        assert isinstance(state.market_structure, MarketStructure)
        assert state.market_structure.timeframe == "1h"
        # 至少能识别出一些 swing（具体方向不强制，由算法决定）
        assert state.market_structure.direction in (
            "bullish", "bearish", "ranging", "transitioning",
        )

    def test_empty_candles_no_op(self):
        from polls.candles import recompute_market_structure

        state = _make_state(candles=[])
        recompute_market_structure(state)

        assert state.market_structure is None

    def test_algorithm_exception_isolated(self, caplog):
        """算法内部异常不能污染 state，必须 WARNING 日志。"""
        from polls import candles as candles_mod

        state = _make_state(candles=_make_bullish_candles(80))

        with patch.object(candles_mod, "detect_market_structure",
                          side_effect=RuntimeError("boom")):
            with caplog.at_level(logging.WARNING, logger="polls.candles"):
                candles_mod.recompute_market_structure(state)

        assert state.market_structure is None
        assert any("市场结构计算失败" in r.message for r in caplog.records)

    def test_change_logs_info_unchanged_logs_debug(self, caplog):
        """首次 / 变化 → INFO，重复相同结果 → DEBUG（节流避免刷屏）。"""
        from polls.candles import recompute_market_structure

        candles = _make_bullish_candles(80)
        state = _make_state(candles=candles)

        # 首次调用 → 一定走 INFO 路径（_prev_ms_summary 初始为空元组）
        with caplog.at_level(logging.DEBUG, logger="polls.candles"):
            recompute_market_structure(state)
        info_records_first = [
            r for r in caplog.records
            if r.levelno == logging.INFO and "市场结构" in r.message
        ]
        assert len(info_records_first) == 1

        # 相同数据再算一次 → 结构相同，应走 DEBUG
        caplog.clear()
        with caplog.at_level(logging.DEBUG, logger="polls.candles"):
            recompute_market_structure(state)
        info_records_second = [
            r for r in caplog.records
            if r.levelno == logging.INFO and "市场结构" in r.message
        ]
        debug_records_second = [
            r for r in caplog.records
            if r.levelno == logging.DEBUG and "市场结构无变化" in r.message
        ]
        assert info_records_second == []
        assert len(debug_records_second) == 1

    def test_previous_value_preserved_on_exception(self):
        """异常时不应清空已有的 market_structure（保守修改原则）。"""
        from polls import candles as candles_mod

        state = _make_state(candles=_make_bullish_candles(80))
        # 先正常填充
        candles_mod.recompute_market_structure(state)
        prior = state.market_structure
        assert prior is not None

        # 再制造异常 — state.market_structure 保持原值
        with patch.object(candles_mod, "detect_market_structure",
                          side_effect=ValueError("bad")):
            candles_mod.recompute_market_structure(state)

        assert state.market_structure is prior
