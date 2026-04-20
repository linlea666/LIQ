"""官方 funding rate 取数单测（sources/funding_official.py）

覆盖：
  - to_okx_inst_id 的多种输入形态
  - Binance / OKX 成功解析
  - 异常响应不抛异常，返回 None
  - fetch_official_pair 并发语义
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sources.funding_official import (
    fetch_binance_funding, fetch_okx_funding, fetch_official_pair,
    to_okx_inst_id,
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# to_okx_inst_id
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_to_okx_inst_id_usdt_pair():
    assert to_okx_inst_id("BTCUSDT") == "BTC-USDT-SWAP"
    assert to_okx_inst_id("ETHUSDT") == "ETH-USDT-SWAP"


def test_to_okx_inst_id_usdc_pair():
    assert to_okx_inst_id("BTCUSDC") == "BTC-USDC-SWAP"


def test_to_okx_inst_id_passthrough_instid():
    assert to_okx_inst_id("BTC-USDT-SWAP") == "BTC-USDT-SWAP"


def test_to_okx_inst_id_lowercase_normalized():
    assert to_okx_inst_id("solusdt") == "SOL-USDT-SWAP"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# HTTP response mocks
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _mock_session_with_response(payload, *, raises: bool = False):
    """构造 aiohttp 风格的 session mock：session.get(...).__aenter__() → resp"""
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    if raises:
        resp.json = AsyncMock(side_effect=RuntimeError("boom"))
    else:
        resp.json = AsyncMock(return_value=payload)
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)

    session = MagicMock()
    session.get = MagicMock(return_value=resp)
    return session


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# fetch_binance_funding
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_fetch_binance_funding_parses_decimal_string():
    payload = {
        "symbol": "BTCUSDT",
        "markPrice": "65000",
        "indexPrice": "64900",
        "lastFundingRate": "0.00012345",
    }
    session = _mock_session_with_response(payload)
    rate = asyncio.run(fetch_binance_funding(session, "BTCUSDT"))
    assert rate == pytest.approx(0.00012345, abs=1e-9)


def test_fetch_binance_funding_missing_field_returns_none():
    session = _mock_session_with_response({"symbol": "BTCUSDT"})
    rate = asyncio.run(fetch_binance_funding(session, "BTCUSDT"))
    assert rate is None


def test_fetch_binance_funding_exception_returns_none_not_raise():
    session = _mock_session_with_response(None, raises=True)
    rate = asyncio.run(fetch_binance_funding(session, "BTCUSDT"))
    assert rate is None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# fetch_okx_funding
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_fetch_okx_funding_parses_decimal_string():
    payload = {
        "code": "0",
        "data": [{"instId": "BTC-USDT-SWAP", "fundingRate": "0.0000987"}],
    }
    session = _mock_session_with_response(payload)
    rate = asyncio.run(fetch_okx_funding(session, "BTC-USDT-SWAP"))
    assert rate == pytest.approx(0.0000987, abs=1e-10)


def test_fetch_okx_funding_empty_data_returns_none():
    payload = {"code": "0", "data": []}
    session = _mock_session_with_response(payload)
    rate = asyncio.run(fetch_okx_funding(session, "BTC-USDT-SWAP"))
    assert rate is None


def test_fetch_okx_funding_malformed_returns_none_not_raise():
    payload = {"code": "51001", "msg": "Instrument ID does not exist"}
    session = _mock_session_with_response(payload)
    rate = asyncio.run(fetch_okx_funding(session, "BOGUS-USDT-SWAP"))
    assert rate is None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# fetch_official_pair（并发编排）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_fetch_official_pair_both_ok():
    async def _bn(session, symbol): return 0.0001
    async def _okx(session, inst): return 0.00015

    with patch("sources.funding_official.fetch_binance_funding", side_effect=_bn), \
         patch("sources.funding_official.fetch_okx_funding", side_effect=_okx):
        bn, okx = asyncio.run(fetch_official_pair("BTCUSDT"))
    assert bn == 0.0001
    assert okx == 0.00015


def test_fetch_official_pair_partial_failure():
    async def _bn(session, symbol): return None
    async def _okx(session, inst): return 0.00012

    with patch("sources.funding_official.fetch_binance_funding", side_effect=_bn), \
         patch("sources.funding_official.fetch_okx_funding", side_effect=_okx):
        bn, okx = asyncio.run(fetch_official_pair("BTCUSDT"))
    assert bn is None
    assert okx == 0.00012


def test_fetch_official_pair_derives_okx_instid_from_binance_symbol():
    captured: dict[str, str] = {}

    async def _okx(session, inst):
        captured["inst_id"] = inst
        return 0.0001

    async def _bn(session, symbol): return 0.0001

    with patch("sources.funding_official.fetch_binance_funding", side_effect=_bn), \
         patch("sources.funding_official.fetch_okx_funding", side_effect=_okx):
        asyncio.run(fetch_official_pair("ETHUSDT"))
    assert captured["inst_id"] == "ETH-USDT-SWAP"
