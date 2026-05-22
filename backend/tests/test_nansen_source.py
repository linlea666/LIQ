from __future__ import annotations

import pytest

from config.settings import NansenSourceConfig
from sources.nansen import NansenSource


@pytest.mark.asyncio
async def test_nansen_perp_screener_filters_symbol(monkeypatch):
    source = NansenSource(NansenSourceConfig(), api_key="test")

    async def fake_post(path, body, *, cache_ttl):
        assert path == "perp-screener"
        assert cache_ttl == 900
        assert body["date"]["from"]
        assert body["date"]["to"]
        assert body["filters"]["token_symbol"] == "BTC"
        assert body["only_smart_money"] is True
        return {
            "data": [
                {"token_symbol": "ETH", "smart_money_buy_volume": 1},
                {"token_symbol": "BTC", "smart_money_buy_volume": 2},
            ]
        }

    monkeypatch.setattr(source, "_post", fake_post)

    row = await source.fetch_perp_screener("BTC")
    assert row["token_symbol"] == "BTC"
    assert row["smart_money_buy_volume"] == 2


@pytest.mark.asyncio
async def test_nansen_flow_intelligence_parses_first_row(monkeypatch):
    source = NansenSource(NansenSourceConfig(), api_key="test")

    async def fake_post(path, body, *, cache_ttl):
        assert path == "tgm/flow-intelligence"
        assert body["chain"] == "ethereum"
        assert body["timeframe"] == "1d"
        return {"data": [{"token_symbol": "WBTC", "smart_trader_net_flow_usd": 123.0}]}

    monkeypatch.setattr(source, "_post", fake_post)

    row = await source.fetch_flow_intelligence(
        chain="ethereum",
        token_address="0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599",
    )
    assert row["token_symbol"] == "WBTC"
    assert row["smart_trader_net_flow_usd"] == 123.0


@pytest.mark.asyncio
async def test_nansen_missing_key_degrades_without_request():
    source = NansenSource(NansenSourceConfig(), api_key="")

    row = await source._post("token-screener", {}, cache_ttl=60)

    assert row is None
    assert source.last_error == "missing_api_key"
