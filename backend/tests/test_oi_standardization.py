from __future__ import annotations

from collections import deque
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from config.settings import CoinConfig
from polls.derivatives import normalize_oi_snapshot, poll_oi
from models.flow import OISnapshot


def test_linear_and_inverse_oi_normalization() -> None:
    linear = normalize_oi_snapshot(
        coin="BTC", ts=1, contracts=10, contract_type="linear",
        contract_size=0.001, margin_asset="USDT", mark_price=70_000,
        source="fixture",
    )
    assert linear.decision_valid is True
    assert linear.oi_base_equivalent == pytest.approx(0.01)
    assert linear.oi_usd_notional == pytest.approx(700)

    inverse = normalize_oi_snapshot(
        coin="BTC", ts=1, contracts=100, contract_type="inverse",
        contract_size=100, margin_asset="BTC", mark_price=50_000,
        source="fixture",
    )
    assert inverse.decision_valid is True
    assert inverse.oi_usd_notional == pytest.approx(10_000)
    assert inverse.oi_base_equivalent == pytest.approx(0.2)


def test_missing_contract_spec_is_display_only() -> None:
    snapshot = normalize_oi_snapshot(
        coin="BTC", ts=1, contracts=100, contract_type="linear",
        contract_size=None, margin_asset="USDT", mark_price=70_000,
        usd_notional=7_000_000, source="fixture",
    )
    assert snapshot.oi_usd_notional == 7_000_000
    assert snapshot.decision_valid is False
    assert snapshot.oi_base_equivalent is None


@pytest.mark.asyncio
async def test_price_mechanical_usd_rise_does_not_create_oi_signal() -> None:
    base = 1_700_000_000
    rows = [
        {
            "timestamp": (base + i * 300) * 1000,
            "sumOpenInterest": "1000",
            "sumOpenInterestValue": str(50_000_000 + i * 1_000_000),
        }
        for i in range(13)
    ]
    hourly = [
        {"time": base + i * 3600, "close": 50_000_000 + i * 1_000_000}
        for i in range(25)
    ]
    bn = SimpleNamespace(fetch_open_interest_history=AsyncMock(return_value=rows))
    cg = SimpleNamespace(fetch_oi_aggregated_history=AsyncMock(side_effect=[[], hourly]))
    state = SimpleNamespace(
        oi_history=deque(maxlen=720), oi=None, oi_change_24h_pct=None,
    )
    coin = CoinConfig(
        ccy="BTC", symbol_cg="BTC", symbol_cg_pair="BTCUSDT",
        exchange_primary="Binance", contract_type="linear",
        contract_size=1.0, margin_asset="USDT",
    )

    await poll_oi(cg, coin, state, bn=bn)

    assert state.oi is not None
    assert state.oi.change_1h_pct > 0  # 旧 USD 展示口径随价格上涨
    assert state.oi.decision_change_5m_pct == pytest.approx(0.0)
    assert state.oi.decision_change_1h_pct == pytest.approx(0.0)
    assert state.oi.decision_unit == "contracts"
    assert state.oi.decision_valid is True


@pytest.mark.asyncio
async def test_inverse_config_does_not_consume_linear_fapi_history() -> None:
    bn = SimpleNamespace(fetch_open_interest_history=AsyncMock(return_value=[]))
    cg = SimpleNamespace(fetch_oi_aggregated_history=AsyncMock(return_value=[]))
    state = SimpleNamespace(oi_history=deque(maxlen=720), oi=None)
    coin = CoinConfig(
        ccy="BTC", symbol_cg="BTC", symbol_cg_pair="BTCUSD_PERP",
        exchange_primary="Binance", contract_type="inverse",
        contract_size=100.0, margin_asset="BTC",
    )

    await poll_oi(cg, coin, state, bn=bn)

    bn.fetch_open_interest_history.assert_not_awaited()
    assert state.oi is None


@pytest.mark.asyncio
async def test_standardized_source_replaces_legacy_usd_only_history() -> None:
    base = 1_700_000_000
    rows = [
        {
            "timestamp": (base + index * 300) * 1000,
            "sumOpenInterest": str(1000 + index),
            "sumOpenInterestValue": str((1000 + index) * 70_000),
        }
        for index in range(13)
    ]
    hourly = [{"time": base + index * 3600, "close": 1_000_000} for index in range(25)]
    state = SimpleNamespace(
        oi_history=deque([
            OISnapshot(coin="BTC", ts=base - 300, oi=1, oi_usd=1,
                       oi_usd_notional=1, source="coinglass", decision_valid=False),
        ], maxlen=720),
        oi=None, oi_change_24h_pct=None,
    )
    coin = CoinConfig(
        ccy="BTC", symbol_cg="BTC", symbol_cg_pair="BTCUSDT",
        exchange_primary="Binance", contract_type="linear",
        contract_size=1.0, margin_asset="USDT",
    )
    bn = SimpleNamespace(fetch_open_interest_history=AsyncMock(return_value=rows))
    cg = SimpleNamespace(fetch_oi_aggregated_history=AsyncMock(side_effect=[[], hourly]))

    await poll_oi(cg, coin, state, bn=bn)

    assert state.oi is not None and state.oi.decision_valid is True
    assert all(point.decision_valid for point in state.oi_history)
    assert {point.source for point in state.oi_history} == {
        "binance-fapi-open-interest-hist"
    }


@pytest.mark.asyncio
async def test_usd_fallback_does_not_contaminate_existing_standardized_series() -> None:
    base = 1_700_000_000
    standardized = [
        normalize_oi_snapshot(
            coin="BTC", ts=base + index * 300, contracts=1000 + index,
            contract_type="linear", contract_size=1.0, margin_asset="USDT",
            mark_price=70_000, source="binance-fapi-open-interest-hist",
        )
        for index in range(13)
    ]
    previous = SimpleNamespace(ts=base + 12 * 300)
    state = SimpleNamespace(
        oi_history=deque(standardized, maxlen=720), oi=previous,
        oi_change_24h_pct=None,
    )
    coin = CoinConfig(
        ccy="BTC", symbol_cg="BTC", symbol_cg_pair="BTCUSDT",
        exchange_primary="Binance", contract_type="linear",
        contract_size=1.0, margin_asset="USDT",
    )
    fallback = [
        {"time": base + 13 * 300, "close": 80_000_000},
    ]
    bn = SimpleNamespace(fetch_open_interest_history=AsyncMock(return_value=[]))
    cg = SimpleNamespace(fetch_oi_aggregated_history=AsyncMock(side_effect=[fallback, []]))

    await poll_oi(cg, coin, state, bn=bn)

    assert state.oi.ts == previous.ts
    assert state.oi.decision_valid is True
    assert len(state.oi_history) == 13
    assert all(point.decision_valid for point in state.oi_history)
