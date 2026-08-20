from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from config.settings import CoinConfig
from polls.coinbase_orderbook import poll_coinbase_orderbook


def _coin() -> CoinConfig:
    return CoinConfig(
        ccy="BTC", symbol_cg="BTC", symbol_cg_pair="BTCUSDT",
        exchange_primary="Binance", symbol_coinbase="BTC-USD",
    )


def _state(sequence: int | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        coinbase_orderbook=None,
        coinbase_orderbook_sequence_checkpoint=sequence,
        coinbase_orderbook_watermark=sequence,
        coinbase_orderbook_gap_reason="",
        _log_once_keys=set(),
    )


@pytest.mark.asyncio
async def test_snapshot_only_accepts_forward_jump_and_sets_watermark() -> None:
    source = SimpleNamespace(fetch_orderbook=AsyncMock(return_value={
        "sequence": 120,
        "time": "2026-08-20T00:00:00Z",
        "bids": [["69999", "1", 2]],
        "asks": [["70001", "1", 3]],
    }))
    state = _state(100)
    assert await poll_coinbase_orderbook(source, _coin(), state) is True
    assert state.coinbase_orderbook_sequence_checkpoint == 120
    assert state.coinbase_orderbook.source_capability == "snapshot_only"
    assert state.coinbase_orderbook.watermark == 120


@pytest.mark.asyncio
async def test_sequence_regression_keeps_previous_snapshot() -> None:
    old = object()
    state = _state(100)
    state.coinbase_orderbook = old
    source = SimpleNamespace(fetch_orderbook=AsyncMock(return_value={
        "sequence": 99,
        "bids": [["69999", "1", 1]],
        "asks": [["70001", "1", 1]],
    }))
    assert await poll_coinbase_orderbook(source, _coin(), state) is False
    assert state.coinbase_orderbook is old
    assert state.coinbase_orderbook_gap_reason == "sequence_regression"


@pytest.mark.asyncio
async def test_crossed_snapshot_is_invalid_and_not_published() -> None:
    state = _state()
    source = SimpleNamespace(fetch_orderbook=AsyncMock(return_value={
        "sequence": 1,
        "bids": [["70002", "1", 1]],
        "asks": [["70001", "1", 1]],
    }))
    assert await poll_coinbase_orderbook(source, _coin(), state) is False
    assert state.coinbase_orderbook is None
    assert state.coinbase_orderbook_gap_reason == "invalid_crossed_or_incomplete_snapshot"
