from __future__ import annotations

import asyncio
import json

import pytest

from api import routes_trend
from models.hyperliquid_whale import pending_hyperliquid_whale_distributions
from processors.hyperliquid_whale_stats import (
    build_hyperliquid_whale_distributions,
    refreshed_distribution_quality,
)
from sources.coinglass import CoinglassSource


NOW = 1_700_000_000


def _position(
    *,
    user: str,
    symbol: str,
    size: float,
    notional: float,
    entry: object,
    liquidation: object,
    mark: object,
    leverage: object = 2,
    updated: int = NOW,
) -> dict:
    return {
        "user": user,
        "symbol": symbol,
        "position_size": size,
        "position_value_usd": notional,
        "entry_price": entry,
        "liq_price": liquidation,
        "mark_price": mark,
        "leverage": leverage,
        "update_time": updated * 1000,
    }


def test_builds_btc_and_eth_from_one_snapshot_and_dedupes_latest_wallet():
    rows = [
        _position(
            user="0xbtc-long", symbol="BTC", size=1, notional=900,
            entry=80, liquidation=70, mark=100, updated=NOW - 10,
        ),
        _position(
            user="0xbtc-long", symbol="BTC", size=1, notional=1_000,
            entry=90, liquidation=80, mark=100, updated=NOW,
        ),
        _position(
            user="0xbtc-short", symbol="BTC", size=-1, notional=2_000,
            entry=110, liquidation=120, mark=100, leverage=4,
        ),
        _position(
            user="0xeth-long", symbol="ETH", size=2, notional=4_000,
            entry=1_900, liquidation=1_500, mark=2_000, leverage=3,
        ),
        _position(
            user="0xsol", symbol="SOL", size=10, notional=1_000,
            entry=100, liquidation=50, mark=100,
        ),
    ]

    result = build_hyperliquid_whale_distributions(
        rows, fetched_at_ts=NOW, now_sec=NOW,
    )

    btc = result.assets["BTC"]
    eth = result.assets["ETH"]
    assert btc.position_count == 2
    assert btc.long_count == 1
    assert btc.short_count == 1
    assert btc.long_notional_usd == 1_000
    assert btc.short_notional_usd == 2_000
    assert btc.mark_price == 100
    assert eth.position_count == 1
    assert eth.long_notional_usd == 4_000
    assert all(bucket.price_mid < 500 for bucket in btc.entry_buckets)
    assert all(bucket.price_mid > 1_000 for bucket in eth.entry_buckets)


def test_bucket_math_invalid_prices_and_extreme_liquidation_are_independent():
    rows = [
        _position(
            user="0x1", symbol="BTC", size=1, notional=1_000,
            entry=100.1, liquidation=0, mark=100, leverage=2,
        ),
        _position(
            user="0x2", symbol="BTC", size=2, notional=3_000,
            entry=100.4, liquidation=3_000_000, mark=100, leverage=6,
        ),
        _position(
            user="0x3", symbol="BTC", size=-1, notional=2_000,
            entry=float("nan"), liquidation=120, mark=100, leverage=4,
        ),
        _position(
            user="0x4", symbol="BTC", size=0, notional=9_999,
            entry=100, liquidation=90, mark=100,
        ),
    ]
    result = build_hyperliquid_whale_distributions(rows, now_sec=NOW)
    btc = result.assets["BTC"]

    assert btc.position_count == 3
    assert btc.valid_entry_price_count == 2
    assert btc.invalid_entry_price_count == 1
    assert btc.valid_liquidation_price_count == 2
    assert btc.invalid_liquidation_price_count == 1
    entry = next(bucket for bucket in btc.entry_buckets if bucket.long_count == 2)
    assert entry.distance_from_mark_pct == pytest.approx(0.25)
    assert entry.long_notional_usd == 4_000
    assert entry.long_avg_leverage == pytest.approx(5.0)
    assert max(bucket.price_mid for bucket in btc.liquidation_buckets) > 1_000_000


def test_missing_asset_and_dynamic_staleness_do_not_affect_other_asset():
    rows = [
        _position(
            user="0xbtc", symbol="BTC", size=1, notional=1_000,
            entry=100, liquidation=80, mark=100, updated=NOW,
        ),
    ]
    original = build_hyperliquid_whale_distributions(rows, now_sec=NOW)
    assert original.assets["BTC"].quality.valid is True
    assert original.assets["ETH"].quality.status == "missing"

    stale = refreshed_distribution_quality(original, now_sec=NOW + 1_801)
    assert stale.assets["BTC"].quality.valid is False
    assert stale.assets["BTC"].quality.status == "stale"
    assert stale.assets["ETH"].quality.status == "missing"
    assert original.assets["BTC"].quality.valid is True


def test_api_returns_memory_aggregate_without_wallet_addresses():
    payload = build_hyperliquid_whale_distributions([
        _position(
            user="0xsecret-wallet", symbol="BTC", size=1, notional=1_000,
            entry=100, liquidation=80, mark=100,
        ),
    ], now_sec=NOW)

    class FakeService:
        enabled = True

        def hyperliquid_whale_distributions(self):
            return payload

    routes_trend.set_service(FakeService())
    try:
        result = asyncio.run(routes_trend.get_hyperliquid_whale_distributions())
    finally:
        routes_trend.set_service(None)

    encoded = json.dumps(result)
    assert result["score_weight"] == 0
    assert set(result["assets"]) == {"BTC", "ETH"}
    assert "0xsecret-wallet" not in encoded
    assert "user" not in encoded


def test_pending_contract_has_both_assets():
    payload = pending_hyperliquid_whale_distributions()
    assert set(payload.assets) == {"BTC", "ETH"}
    assert all(asset.quality.status == "pending" for asset in payload.assets.values())


def test_identical_concurrent_source_requests_share_singleflight(tmp_path):
    async def scenario():
        source = CoinglassSource(
            base_url="https://example.invalid",
            api_key="test",
            rate_per_min=10,
            provider_limit_per_min=11,
        )
        source._cache = {}
        calls = 0

        async def fake_request_once(*args, **kwargs):
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.01)
            return [{"symbol": "BTC"}]

        source._request_once = fake_request_once
        try:
            first, second = await asyncio.gather(
                source.fetch_hyperliquid_whale_position(),
                source.fetch_hyperliquid_whale_position(),
            )
        finally:
            await source.close()
        return calls, first, second

    calls, first, second = asyncio.run(scenario())
    assert calls == 1
    assert first == second == [{"symbol": "BTC"}]
