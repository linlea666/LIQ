"""新闻账本（D13）

职责：
  - 维护 24h 滚动 EnrichedNewsEvent 账本
  - 去重（event_id）、过期清理（publish_ts 超过 48h 丢弃）、LRU 容量兜底
  - 供 price_reaction_backfill / news_brief 查询
  - 轻量落盘（进程重启可恢复）

数据形态：
  - 主存储按 insertion order 的 OrderedDict[event_id → EnrichedNewsEvent]
  - 最大条目 _DEFAULT_MAX_EVENTS（默认 500）

线程安全：
  - 所有读写加 RLock（可能被多个 loop 同时调）

落实日志锚点：
  - D.D04_BACKTEST_LOOP：stats() 上报 ledger_size / backfill_pending
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import OrderedDict
from typing import Optional

from models.news_event import EnrichedNewsEvent

logger = logging.getLogger(__name__)


_DEFAULT_MAX_EVENTS = 500
_DEFAULT_MAX_AGE_SEC = 48 * 3600       # 48h 彻底过期
_RECENT_WINDOW_SEC = 24 * 3600         # 24h 滚动窗口


class NewsLedger:
    """24h 滚动新闻账本"""

    def __init__(
        self,
        *,
        max_events: int = _DEFAULT_MAX_EVENTS,
        max_age_sec: int = _DEFAULT_MAX_AGE_SEC,
        persist_path: Optional[str] = None,
    ) -> None:
        # 下限 1：测试可用小容量；生产默认 500 不受影响
        self._max_events = max(1, int(max_events))
        self._max_age_sec = max(60, int(max_age_sec))
        self._persist_path = persist_path
        self._lock = threading.RLock()
        self._events: "OrderedDict[str, EnrichedNewsEvent]" = OrderedDict()
        self._last_persist_ts = 0

        if self._persist_path:
            self._load_from_disk()

    # ── 写入 ──
    def upsert_many(self, events: list[EnrichedNewsEvent]) -> tuple[int, int]:
        """批量写入 / 更新。返回 (added, updated)"""
        added = 0
        updated = 0
        with self._lock:
            for ev in events:
                eid = ev.structured.event_id
                if not eid:
                    continue
                if eid in self._events:
                    # 以新覆盖旧（保留原插入顺序）
                    self._events[eid] = ev
                    updated += 1
                else:
                    self._events[eid] = ev
                    added += 1
            self._prune_locked()
        return added, updated

    def upsert(self, event: EnrichedNewsEvent) -> bool:
        """单条写入。返回是否新增"""
        added, _ = self.upsert_many([event])
        return added == 1

    def replace_many(self, events: list[EnrichedNewsEvent]) -> int:
        """替换已有事件（price_reaction_backfill 写回）。返回更新数"""
        updated = 0
        with self._lock:
            for ev in events:
                eid = ev.structured.event_id
                if eid and eid in self._events:
                    self._events[eid] = ev
                    updated += 1
        return updated

    # ── 查询 ──
    def get_all(self) -> list[EnrichedNewsEvent]:
        with self._lock:
            return list(self._events.values())

    def get_recent(self, window_sec: int = _RECENT_WINDOW_SEC) -> list[EnrichedNewsEvent]:
        cutoff = int(time.time()) - max(60, int(window_sec))
        with self._lock:
            return [e for e in self._events.values() if int(e.structured.ts or 0) >= cutoff]

    def get_pending_backfill(self) -> list[EnrichedNewsEvent]:
        with self._lock:
            return [e for e in self._events.values() if e.backfill_status != "complete"]

    def get(self, event_id: str) -> Optional[EnrichedNewsEvent]:
        with self._lock:
            return self._events.get(event_id)

    def size(self) -> int:
        with self._lock:
            return len(self._events)

    def contains(self, event_id: str) -> bool:
        with self._lock:
            return event_id in self._events

    def stats(self) -> dict:
        with self._lock:
            all_ev = list(self._events.values())
        cutoff_24h = int(time.time()) - _RECENT_WINDOW_SEC
        recent = [e for e in all_ev if int(e.structured.ts or 0) >= cutoff_24h]
        by_tier: dict[str, int] = {"blackswan": 0, "major": 0, "normal": 0, "minor": 0}
        for e in recent:
            t = e.structured.tier
            if t in by_tier:
                by_tier[t] += 1
        pending = sum(1 for e in all_ev if e.backfill_status != "complete")
        return {
            "total": len(all_ev),
            "recent_24h": len(recent),
            "pending_backfill": pending,
            "by_tier_24h": by_tier,
        }

    # ── 维护 ──
    def prune_expired(self, now_ts: Optional[int] = None) -> int:
        with self._lock:
            return self._prune_locked(now_ts)

    def _prune_locked(self, now_ts: Optional[int] = None) -> int:
        now = int(now_ts if now_ts is not None else time.time())
        cutoff = now - self._max_age_sec
        removed = 0
        # 删除 publish_ts < cutoff 的
        expired_ids = [
            eid for eid, ev in self._events.items()
            if int(ev.structured.ts or 0) < cutoff
        ]
        for eid in expired_ids:
            del self._events[eid]
            removed += 1
        # 容量兜底：超过 max_events 时删最旧
        while len(self._events) > self._max_events:
            self._events.popitem(last=False)
            removed += 1
        return removed

    def reset(self) -> None:
        """测试用"""
        with self._lock:
            self._events.clear()
            self._last_persist_ts = 0

    # ── 持久化 ──
    def _load_from_disk(self) -> int:
        if not self._persist_path or not os.path.exists(self._persist_path):
            return 0
        try:
            with open(self._persist_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            events_raw = data.get("events") if isinstance(data, dict) else data
            if not isinstance(events_raw, list):
                return 0
            loaded = 0
            for raw in events_raw:
                try:
                    ev = EnrichedNewsEvent.model_validate(raw)
                except Exception:
                    continue
                eid = ev.structured.event_id
                if not eid:
                    continue
                self._events[eid] = ev
                loaded += 1
            self._prune_locked()
            logger.info("[D13] news_ledger loaded %d events from %s", loaded, self._persist_path)
            return loaded
        except Exception as e:  # noqa: BLE001
            logger.warning("[D13] news_ledger load failed: %s", e)
            return 0

    def persist_to_disk(self, force: bool = False) -> bool:
        if not self._persist_path:
            return False
        now = int(time.time())
        if not force and (now - self._last_persist_ts) < 60:
            return False
        with self._lock:
            snapshot = [e.model_dump(mode="json") for e in self._events.values()]
        try:
            tmp = self._persist_path + ".tmp"
            os.makedirs(os.path.dirname(self._persist_path) or ".", exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"events": snapshot, "saved_at": now}, f, ensure_ascii=False)
            os.replace(tmp, self._persist_path)
            self._last_persist_ts = now
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning("[D13] news_ledger persist failed: %s", e)
            return False


# ── 单例 ──
_LEDGER: Optional[NewsLedger] = None
_LEDGER_LOCK = threading.Lock()


def get_ledger() -> NewsLedger:
    global _LEDGER
    if _LEDGER is None:
        with _LEDGER_LOCK:
            if _LEDGER is None:
                path = os.path.join(
                    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "data", "news_ledger.json",
                )
                _LEDGER = NewsLedger(persist_path=path)
    return _LEDGER


def reset_ledger() -> None:
    """测试用"""
    global _LEDGER
    with _LEDGER_LOCK:
        _LEDGER = None
