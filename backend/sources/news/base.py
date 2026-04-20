"""新闻源基类（签名 · 不含实现）

职责：
  - 所有新闻源（OKX 行业 / OKX 博主 / Twitter / RSS …）统一接口
  - 上层 registry.fetch_all() 按 poll interval 轮询各源

与 sources/base.py（DataSource）的区别：
  - DataSource 面向 coin-scoped 行情数据（一个 coin 一次 fetch）
  - NewsSource 面向全局内容流（无 coin 参数；按 since_ts 增量拉取）
  - 刻意不继承 DataSource，避免签名被迫 coin 化

落实日志锚点：
  - D.D07_NEWS_SOURCES：每次 fetch 成功/失败上报 items 数 / 去重数 / 错误
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Optional

from models.news_event import RawNewsItem

logger = logging.getLogger(__name__)


class NewsSource(ABC):
    """新闻源抽象基类"""

    # ── 元信息（子类填充） ──
    source_type: str = ""                 # "okx" / "twitter" / "rss"
    source_author: str = ""               # "OKX-Industry" / "OKX-KOL-xxx"
    source_reliability: float = 0.8       # 0-1（registry 可覆盖）

    # ── 拉取节奏（分钟） ──
    poll_interval_min: int = 15

    def __init__(self, name: str) -> None:
        self.name = name
        self._last_fetch_ts: int = 0
        self._last_external_ids: set[str] = set()   # 内存去重（ring buffer 式）
        self._dedupe_capacity: int = 500

    # ── 主 API ──
    @abstractmethod
    async def fetch(self, since_ts: Optional[int] = None) -> list[RawNewsItem]:
        """拉取新闻。

        参数：
          since_ts: 秒级。若提供，子类应尽量只返回 publish_time >= since_ts 的条目
        返回：
          列表里条目按 publish_time 升序
        要求：
          - 不做跨源去重（由 registry 统一去重）
          - 失败抛异常（registry 捕获并标 fail）
        """

    # ── 去重键生成（子类可覆盖） ──
    def dedupe_key(self, item: RawNewsItem) -> str:
        """默认：source_type + external_id。子类可换策略（如 hash(title)）"""
        return f"{item.source_type}:{item.external_id}"

    # ── 内存去重（registry 调用） ──
    def filter_new(self, items: list[RawNewsItem]) -> list[RawNewsItem]:
        """仅保留从未见过的条目，同时更新内存集合"""
        fresh: list[RawNewsItem] = []
        for it in items:
            k = self.dedupe_key(it)
            if k in self._last_external_ids:
                continue
            fresh.append(it)
            self._last_external_ids.add(k)
        # 维持容量（简单 FIFO 裁剪）
        if len(self._last_external_ids) > self._dedupe_capacity:
            overflow = len(self._last_external_ids) - self._dedupe_capacity
            for k in list(self._last_external_ids)[:overflow]:
                self._last_external_ids.discard(k)
        return fresh

    # ── 健康 ──
    @property
    def last_fetch_ts(self) -> int:
        return self._last_fetch_ts

    def _mark_fetched(self, ts: int) -> None:
        self._last_fetch_ts = ts
