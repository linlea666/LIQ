"""进程与容器内存诊断：供 /api/health 暴露常驻内存与容器上限。

只读 /proc 与 cgroup 文件，不引入第三方依赖；非 Linux 环境返回 None 而不是报错。
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

_STATUS_PATH = Path("/proc/self/status")
_CGROUP_V2_LIMIT = Path("/sys/fs/cgroup/memory.max")
_CGROUP_V1_LIMIT = Path("/sys/fs/cgroup/memory/memory.limit_in_bytes")
# cgroup v1 未设限时会返回一个接近 2^63 的哨兵值，视为"无上限"。
_NO_LIMIT_THRESHOLD = 1 << 53


def _read_status_kb(field: str) -> Optional[int]:
    try:
        for line in _STATUS_PATH.read_text(encoding="utf-8").splitlines():
            if line.startswith(f"{field}:"):
                return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        return None
    return None


def _read_memory_limit_bytes() -> Optional[int]:
    for path in (_CGROUP_V2_LIMIT, _CGROUP_V1_LIMIT):
        try:
            raw = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if raw == "max":
            return None
        try:
            value = int(raw)
        except ValueError:
            continue
        if value <= 0 or value >= _NO_LIMIT_THRESHOLD:
            return None
        return value
    return None


def process_memory_stats() -> dict:
    """返回当前进程 RSS/Swap（MiB）与容器内存上限（MiB）；不可用字段为 None。"""
    rss_kb = _read_status_kb("VmRSS")
    swap_kb = _read_status_kb("VmSwap")
    limit_bytes = _read_memory_limit_bytes()
    rss_mb = round(rss_kb / 1024, 1) if rss_kb is not None else None
    limit_mb = round(limit_bytes / 1024 / 1024, 1) if limit_bytes else None
    return {
        "rss_mb": rss_mb,
        "swap_mb": round(swap_kb / 1024, 1) if swap_kb is not None else None,
        "container_limit_mb": limit_mb,
        "usage_pct": (
            round(rss_mb / limit_mb * 100, 1)
            if rss_mb is not None and limit_mb else None
        ),
    }
