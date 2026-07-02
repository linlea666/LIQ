"""将 Hyperliquid 巨鲸仓位快照聚合为 BTC/ETH 价格厚度分布。"""

from __future__ import annotations

import math
import statistics
import time
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from models.hyperliquid_whale import (
    HyperliquidWhaleAssetDistribution,
    HyperliquidWhaleDistributions,
    HyperliquidWhalePriceBucket,
    _default_caveats,
)
from models.trend_monitor import DataQuality


BIN_SIZE_PCT = 0.5
STALE_AFTER_SEC = 30 * 60
_SYMBOLS = ("BTC", "ETH")


@dataclass
class _BucketAccumulator:
    long_notional_usd: float = 0
    short_notional_usd: float = 0
    long_count: int = 0
    short_count: int = 0
    long_leverage_weighted: float = 0
    short_leverage_weighted: float = 0


def build_hyperliquid_whale_distributions(
    raw_positions: Any,
    *,
    fetched_at_ts: Optional[int] = None,
    now_sec: Optional[int] = None,
) -> HyperliquidWhaleDistributions:
    """一次原始响应同时构建 BTC 和 ETH，且不暴露钱包地址。"""
    now = int(now_sec or time.time())
    fetched_at = int(fetched_at_ts or now)
    rows = raw_positions if isinstance(raw_positions, list) else []
    deduped = _dedupe_positions(rows)
    assets = {
        symbol: _build_asset_distribution(
            symbol,
            (row for row in deduped if str(row.get("symbol", "")).upper() == symbol),
            fetched_at=fetched_at,
            now=now,
        )
        for symbol in _SYMBOLS
    }
    return HyperliquidWhaleDistributions(
        fetched_at_ts=fetched_at,
        assets=assets,
    )


def refreshed_distribution_quality(
    payload: HyperliquidWhaleDistributions,
    *,
    now_sec: Optional[int] = None,
) -> HyperliquidWhaleDistributions:
    """返回动态新鲜度副本；不会修改服务内存中的最后有效结果。"""
    now = int(now_sec or time.time())
    result = payload.model_copy(deep=True)
    for asset in result.assets.values():
        quality = asset.quality
        if asset.as_of_ts is None:
            continue
        quality.age_sec = max(0, now - asset.as_of_ts)
        if quality.age_sec > STALE_AFTER_SEC:
            quality.valid = False
            quality.status = "stale"
            quality.reason = "Hyperliquid 巨鲸仓位快照超过30分钟未更新"
    return result


def _dedupe_positions(rows: list[Any]) -> list[dict[str, Any]]:
    latest: dict[tuple[str, str], tuple[int, int, dict[str, Any]]] = {}
    for index, candidate in enumerate(rows):
        if not isinstance(candidate, dict):
            continue
        symbol = str(candidate.get("symbol", "")).upper()
        if symbol not in _SYMBOLS:
            continue
        user = str(candidate.get("user") or "").lower()
        # 缺少地址时不能把不相关仓位错误合并。
        identity = user if user else f"__row_{index}"
        updated = _timestamp_seconds(candidate.get("update_time")) or 0
        key = (symbol, identity)
        previous = latest.get(key)
        if previous is None or (updated, index) >= (previous[0], previous[1]):
            latest[key] = (updated, index, candidate)
    return [item[2] for item in latest.values()]


def _build_asset_distribution(
    symbol: str,
    rows: Iterable[dict[str, Any]],
    *,
    fetched_at: int,
    now: int,
) -> HyperliquidWhaleAssetDistribution:
    valid_rows: list[dict[str, Any]] = []
    mark_prices: list[float] = []
    update_times: list[int] = []
    for row in rows:
        position_size = _positive_or_negative(row.get("position_size"))
        notional = _positive(row.get("position_value_usd"))
        if position_size is None or notional is None:
            continue
        valid_rows.append(row)
        mark = _positive(row.get("mark_price"))
        if mark is not None:
            mark_prices.append(mark)
        updated = _timestamp_seconds(row.get("update_time"))
        if updated is not None:
            update_times.append(updated)

    if not valid_rows or not mark_prices:
        return HyperliquidWhaleAssetDistribution(
            symbol=symbol,
            position_count=len(valid_rows),
            quality=DataQuality(
                valid=False,
                status="missing",
                points=len(valid_rows),
                reason=(
                    "官方巨鲸池暂无该币种有效仓位"
                    if not valid_rows else "该币种仓位缺少有效标记价格"
                ),
                fetched_at_ts=fetched_at,
            ),
            caveats=_default_caveats(),
        )

    mark_price = statistics.median(mark_prices)
    as_of_ts = max(update_times) if update_times else fetched_at
    entry_acc: dict[int, _BucketAccumulator] = {}
    liquidation_acc: dict[int, _BucketAccumulator] = {}
    long_count = short_count = 0
    long_notional = short_notional = 0.0
    valid_entry = invalid_entry = valid_liquidation = invalid_liquidation = 0

    for row in valid_rows:
        position_size = float(row["position_size"])
        side = "long" if position_size > 0 else "short"
        notional = float(row["position_value_usd"])
        leverage = _positive(row.get("leverage")) or 0.0
        if side == "long":
            long_count += 1
            long_notional += notional
        else:
            short_count += 1
            short_notional += notional

        entry_price = _positive(row.get("entry_price"))
        if entry_price is None:
            invalid_entry += 1
        else:
            valid_entry += 1
            _add_bucket(entry_acc, entry_price, mark_price, side, notional, leverage)

        liquidation_price = _positive(row.get("liq_price"))
        if liquidation_price is None:
            invalid_liquidation += 1
        else:
            valid_liquidation += 1
            _add_bucket(
                liquidation_acc,
                liquidation_price,
                mark_price,
                side,
                notional,
                leverage,
            )

    age_sec = max(0, now - as_of_ts)
    fresh = age_sec <= STALE_AFTER_SEC
    return HyperliquidWhaleAssetDistribution(
        symbol=symbol,
        mark_price=round(mark_price, 8),
        as_of_ts=as_of_ts,
        bin_size_pct=BIN_SIZE_PCT,
        position_count=len(valid_rows),
        long_count=long_count,
        short_count=short_count,
        long_notional_usd=round(long_notional, 2),
        short_notional_usd=round(short_notional, 2),
        valid_entry_price_count=valid_entry,
        invalid_entry_price_count=invalid_entry,
        valid_liquidation_price_count=valid_liquidation,
        invalid_liquidation_price_count=invalid_liquidation,
        entry_buckets=_materialize_buckets(entry_acc, mark_price),
        liquidation_buckets=_materialize_buckets(liquidation_acc, mark_price),
        quality=DataQuality(
            valid=fresh,
            status="fresh" if fresh else "stale",
            points=len(valid_rows),
            reason="" if fresh else "Hyperliquid 巨鲸仓位快照超过30分钟未更新",
            age_sec=age_sec,
            as_of_ts=as_of_ts,
            fetched_at_ts=fetched_at,
        ),
        caveats=_default_caveats(),
    )


def _add_bucket(
    buckets: dict[int, _BucketAccumulator],
    price: float,
    mark_price: float,
    side: str,
    notional: float,
    leverage: float,
) -> None:
    distance_pct = (price / mark_price - 1.0) * 100.0
    index = math.floor(distance_pct / BIN_SIZE_PCT)
    bucket = buckets.setdefault(index, _BucketAccumulator())
    if side == "long":
        bucket.long_notional_usd += notional
        bucket.long_count += 1
        bucket.long_leverage_weighted += leverage * notional
    else:
        bucket.short_notional_usd += notional
        bucket.short_count += 1
        bucket.short_leverage_weighted += leverage * notional


def _materialize_buckets(
    buckets: dict[int, _BucketAccumulator],
    mark_price: float,
) -> list[HyperliquidWhalePriceBucket]:
    result = []
    for index, bucket in sorted(buckets.items()):
        distance_from = index * BIN_SIZE_PCT
        distance_to = distance_from + BIN_SIZE_PCT
        price_from = mark_price * (1.0 + distance_from / 100.0)
        price_to = mark_price * (1.0 + distance_to / 100.0)
        result.append(HyperliquidWhalePriceBucket(
            price_from=round(price_from, 8),
            price_to=round(price_to, 8),
            price_mid=round((price_from + price_to) / 2.0, 8),
            distance_from_mark_pct=round((distance_from + distance_to) / 2.0, 4),
            long_notional_usd=round(bucket.long_notional_usd, 2),
            short_notional_usd=round(bucket.short_notional_usd, 2),
            long_count=bucket.long_count,
            short_count=bucket.short_count,
            long_avg_leverage=round(
                bucket.long_leverage_weighted / bucket.long_notional_usd, 2,
            ) if bucket.long_notional_usd else 0,
            short_avg_leverage=round(
                bucket.short_leverage_weighted / bucket.short_notional_usd, 2,
            ) if bucket.short_notional_usd else 0,
        ))
    return result


def _finite(value: Any) -> Optional[float]:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _positive(value: Any) -> Optional[float]:
    result = _finite(value)
    return result if result is not None and result > 0 else None


def _positive_or_negative(value: Any) -> Optional[float]:
    result = _finite(value)
    return result if result is not None and result != 0 else None


def _timestamp_seconds(value: Any) -> Optional[int]:
    parsed = _positive(value)
    if parsed is None:
        return None
    if parsed > 10_000_000_000:
        parsed /= 1000.0
    return int(parsed)
