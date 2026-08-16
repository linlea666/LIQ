"""指标（第四层可观测）。

指标是"系统整体现在是否健康"的答案，不适合一条条写日志，
因此全部放在内存计数器里，由运行中心 API 按需读取。

同时承担特征分布采样职责：这是防"程序没挂但答案全错"的关键——
如果某天所有代币的 Top10 突然都变成 0，几乎一定是解析出了问题，
而不是市场突然变健康了。
"""

from __future__ import annotations

import resource
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round(pct / 100.0 * (len(ordered) - 1)))))
    return ordered[idx]


def process_rss_mb() -> float:
    """当前进程常驻内存（MB）。容器内优先读 /proc，本地回退到 rusage。"""
    try:
        with open("/proc/self/status", "r", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return float(line.split()[1]) / 1024.0
    except OSError:
        pass
    usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # Linux 返回 KB，macOS 返回 bytes
    return usage / 1024.0 if sys.platform != "darwin" else usage / (1024.0 * 1024.0)


def cgroup_memory_limit_mb() -> float | None:
    """容器内存上限（MB）。用于 /health 暴露"用了多少 / 上限多少"。"""
    candidates = (
        "/sys/fs/cgroup/memory.max",                     # cgroup v2
        "/sys/fs/cgroup/memory/memory.limit_in_bytes",   # cgroup v1
    )
    for path in candidates:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                raw = fh.read().strip()
            if raw in ("max", ""):
                continue
            value = float(raw)
            # cgroup v1 未设限时会返回一个极大值
            if value > 1 << 50:
                continue
            return value / (1024.0 * 1024.0)
        except (OSError, ValueError):
            continue
    return None


@dataclass
class EndpointStats:
    """单个币安端点的健康统计。"""

    name: str
    success: int = 0
    failure: int = 0
    rate_limited: int = 0
    last_success_ms: int = 0
    last_failure_ms: int = 0
    last_error: str = ""
    latencies_ms: deque[float] = field(default_factory=lambda: deque(maxlen=200))

    @property
    def total(self) -> int:
        return self.success + self.failure

    @property
    def success_ratio(self) -> float:
        return self.success / self.total if self.total else 1.0

    def snapshot(self) -> dict[str, Any]:
        samples = list(self.latencies_ms)
        return {
            "name": self.name,
            "success": self.success,
            "failure": self.failure,
            "rate_limited": self.rate_limited,
            "success_ratio": round(self.success_ratio, 4),
            "p50_ms": round(_percentile(samples, 50), 1),
            "p95_ms": round(_percentile(samples, 95), 1),
            "last_success_ms": self.last_success_ms,
            "last_failure_ms": self.last_failure_ms,
            "last_error": self.last_error[:200],
        }


@dataclass
class FeatureDistribution:
    """单个特征的近期分布，用于漂移检测。"""

    name: str
    values: deque[float] = field(default_factory=lambda: deque(maxlen=500))
    null_count: int = 0
    total_count: int = 0

    def observe(self, value: float | None) -> None:
        self.total_count += 1
        if value is None:
            self.null_count += 1
        else:
            self.values.append(float(value))

    @property
    def null_ratio(self) -> float:
        return self.null_count / self.total_count if self.total_count else 0.0

    @property
    def constant_ratio(self) -> float:
        """最高频取值的占比。接近 1 说明该特征已失去区分度（常见于解析错误）。"""
        if not self.values:
            return 0.0
        counts: dict[float, int] = {}
        for v in self.values:
            counts[v] = counts.get(v, 0) + 1
        return max(counts.values()) / len(self.values)

    def snapshot(self) -> dict[str, Any]:
        samples = list(self.values)
        return {
            "name": self.name,
            "samples": len(samples),
            "null_ratio": round(self.null_ratio, 4),
            "constant_ratio": round(self.constant_ratio, 4),
            "median": round(_percentile(samples, 50), 6),
            "p95": round(_percentile(samples, 95), 6),
        }

    def reset_window(self) -> None:
        self.null_count = 0
        self.total_count = 0
        self.values.clear()


class Metrics:
    """进程级指标注册表。"""

    def __init__(self) -> None:
        self.started_at_ms = int(time.time() * 1000)
        self.endpoints: dict[str, EndpointStats] = {}
        self.features: dict[str, FeatureDistribution] = {}
        self.counters: dict[str, int] = {}
        self.gauges: dict[str, float] = {}
        # 最近请求时间戳，用于计算真实 rpm
        self._request_times: deque[float] = deque(maxlen=2000)
        self._rate_limit_times: deque[float] = deque(maxlen=500)
        self.db_write_latencies_ms: deque[float] = deque(maxlen=200)

    # ── 计数器 / 仪表 ───────────────────────────────────────────────────
    def incr(self, key: str, amount: int = 1) -> None:
        self.counters[key] = self.counters.get(key, 0) + amount

    def gauge(self, key: str, value: float) -> None:
        self.gauges[key] = value

    def get_gauge(self, key: str, default: float = 0.0) -> float:
        return self.gauges.get(key, default)

    # ── 端点统计 ────────────────────────────────────────────────────────
    def endpoint(self, name: str) -> EndpointStats:
        stats = self.endpoints.get(name)
        if stats is None:
            stats = EndpointStats(name=name)
            self.endpoints[name] = stats
        return stats

    def record_request(
        self,
        endpoint: str,
        *,
        ok: bool,
        latency_ms: float,
        rate_limited: bool = False,
        error: str = "",
    ) -> None:
        now = time.time()
        self._request_times.append(now)
        stats = self.endpoint(endpoint)
        stats.latencies_ms.append(latency_ms)
        if rate_limited:
            stats.rate_limited += 1
            self._rate_limit_times.append(now)
        if ok:
            stats.success += 1
            stats.last_success_ms = int(now * 1000)
        else:
            stats.failure += 1
            stats.last_failure_ms = int(now * 1000)
            if error:
                stats.last_error = error

    def actual_rpm(self, window_sec: float = 60.0) -> float:
        cutoff = time.time() - window_sec
        return float(sum(1 for t in self._request_times if t >= cutoff))

    def rate_limit_ratio(self, window_sec: float = 300.0) -> float:
        cutoff = time.time() - window_sec
        requests = sum(1 for t in self._request_times if t >= cutoff)
        if requests == 0:
            return 0.0
        limited = sum(1 for t in self._rate_limit_times if t >= cutoff)
        return limited / requests

    # ── 特征分布 ────────────────────────────────────────────────────────
    def observe_feature(self, name: str, value: float | None) -> None:
        dist = self.features.get(name)
        if dist is None:
            dist = FeatureDistribution(name=name)
            self.features[name] = dist
        dist.observe(value)

    # ── 汇总 ────────────────────────────────────────────────────────────
    def snapshot(self) -> dict[str, Any]:
        rss = process_rss_mb()
        limit = cgroup_memory_limit_mb()
        write_samples = list(self.db_write_latencies_ms)
        return {
            "uptime_sec": int(time.time() - (self.started_at_ms / 1000.0)),
            "memory": {
                "rss_mb": round(rss, 1),
                "limit_mb": round(limit, 1) if limit else None,
                "used_ratio": round(rss / limit, 4) if limit else None,
            },
            "requests": {
                "actual_rpm": self.actual_rpm(),
                "rate_limit_ratio_5m": round(self.rate_limit_ratio(), 4),
            },
            "db": {
                "write_p95_ms": round(_percentile(write_samples, 95), 1),
                "queue_depth": int(self.get_gauge("db_queue_depth")),
            },
            "counters": dict(self.counters),
            "gauges": {k: round(v, 4) for k, v in self.gauges.items()},
            "endpoints": [s.snapshot() for s in self.endpoints.values()],
        }


metrics = Metrics()
