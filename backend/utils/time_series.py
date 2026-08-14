"""Small timestamp-aware helpers shared by realtime market-data consumers."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Optional, TypeVar


T = TypeVar("T")


def normalize_epoch_seconds(value: object) -> int:
    """Normalize second/millisecond epoch values; invalid values become ``0``."""
    try:
        ts = int(value or 0)
    except (TypeError, ValueError):
        return 0
    if ts > 10_000_000_000:
        ts //= 1000
    return ts if ts > 0 else 0


def dedupe_sorted_points(
    points: Iterable[T],
    *,
    ts_getter: Callable[[T], object],
) -> list[T]:
    """Return points ordered by normalized timestamp, keeping the newest duplicate."""
    by_ts: dict[int, T] = {}
    for point in points:
        ts = normalize_epoch_seconds(ts_getter(point))
        if ts > 0:
            by_ts[ts] = point
    return [by_ts[ts] for ts in sorted(by_ts)]


def percent_change_at_lookback(
    points: Iterable[T],
    *,
    lookback_sec: int,
    ts_getter: Callable[[T], object],
    value_getter: Callable[[T], object],
    max_baseline_lag_sec: int,
) -> Optional[float]:
    """Calculate latest-vs-target change using a timestamp-aligned baseline.

    The baseline must be at or before ``latest_ts - lookback_sec`` and no older
    than ``max_baseline_lag_sec``.  Missing gaps therefore remain unavailable
    instead of silently becoming a wider, mislabeled window.
    """
    ordered = dedupe_sorted_points(points, ts_getter=ts_getter)
    if len(ordered) < 2 or lookback_sec <= 0:
        return None
    latest = ordered[-1]
    latest_ts = normalize_epoch_seconds(ts_getter(latest))
    target_ts = latest_ts - lookback_sec
    baseline: Optional[T] = None
    baseline_ts = 0
    for point in ordered:
        point_ts = normalize_epoch_seconds(ts_getter(point))
        if point_ts <= target_ts and point_ts >= baseline_ts:
            baseline = point
            baseline_ts = point_ts
        if point_ts > target_ts:
            break
    if baseline is None or target_ts - baseline_ts > max_baseline_lag_sec:
        return None
    try:
        baseline_value = float(value_getter(baseline))
        latest_value = float(value_getter(latest))
    except (TypeError, ValueError):
        return None
    if baseline_value <= 0:
        return None
    return (latest_value - baseline_value) / baseline_value * 100.0
