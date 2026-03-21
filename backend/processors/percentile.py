"""历史百分位计算：判断当前值在历史中的位置"""

from __future__ import annotations

import bisect
import logging
from collections import deque

logger = logging.getLogger(__name__)


class PercentileTracker:
    """
    维护一个滑动窗口，实时计算任意值的历史百分位。
    每个 (coin, metric) 组合有独立的窗口。
    """

    def __init__(self, max_size: int = 2016):
        self._data: dict[str, deque[float]] = {}
        self._sorted_cache: dict[str, list[float]] = {}
        self._max_size = max_size

    def _key(self, coin: str, metric: str) -> str:
        return f"{coin}:{metric}"

    def push(self, coin: str, metric: str, value: float):
        """添加一个新数据点"""
        k = self._key(coin, metric)
        if k not in self._data:
            self._data[k] = deque(maxlen=self._max_size)
        self._data[k].append(value)
        self._sorted_cache.pop(k, None)

    def percentile(self, coin: str, metric: str, value: float) -> float:
        """返回 value 在历史数据中的百分位 (0-100)"""
        k = self._key(coin, metric)
        if k not in self._data or len(self._data[k]) < 5:
            return 50.0

        if k not in self._sorted_cache:
            self._sorted_cache[k] = sorted(self._data[k])

        sorted_data = self._sorted_cache[k]
        pos = bisect.bisect_left(sorted_data, value)
        return round(pos / len(sorted_data) * 100, 1)

    def get_size(self, coin: str, metric: str) -> int:
        k = self._key(coin, metric)
        return len(self._data.get(k, []))
