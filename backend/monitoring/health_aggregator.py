"""P2.2 · Decision Health 聚合器

职责：
    在 DecisionTracker 的原子快照之上，再做一层"前端用的聚合"：
      1. overall = ok / warn / fail（按最坏状态计算）
      2. degraded：所有非 ok 的决策点清单（含 stuck 时长、近因 metrics）
      3. events：降级事件历史（最近 50 条），由 observe() 轮询探测：
         - 任意 id 从 ok → warn/fail 生成一条事件
         - 任意 id 从 warn/fail → ok 生成一条 recovered 事件
      4. WebSocket 广播：前端订阅 `decision_health` 主题，实时收到新事件

复用：
    - DecisionTracker.get_summary_dict() 作为数据源；不重复存状态
    - ws_manager.broadcast_topic() 作为推送通道（若已存在）

不包含：
    - 外部告警（邮件 / 企微 / Telegram）：本项目不接入，仅前端弹窗
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Any, Deque, Optional

logger = logging.getLogger(__name__)


_EVENT_KEEP = 50                     # 最近 50 条事件
_OBSERVE_MIN_INTERVAL_SEC = 8       # 最快每 8s 轮询一次


class HealthAggregator:
    """DecisionTracker 的聚合 + 事件检测层（进程单例）"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._prev_status: dict[str, str] = {}
        self._events: Deque[dict[str, Any]] = deque(maxlen=_EVENT_KEEP)
        self._last_observe_ts = 0

    # ── 汇总：前端拉取 ─────────────────────────────────

    def summary(self) -> dict[str, Any]:
        """返回当前聚合快照"""
        self._observe_if_due()
        try:
            from utils.decision_tracker import get_tracker
            data = get_tracker().get_summary_dict()
        except Exception as e:
            logger.debug("[P2.2] tracker summary failed: %s", e)
            return self._empty()

        decisions = data.get("decisions") or []
        now = int(time.time())
        buckets = {"ok": [], "warn": [], "failed": [], "pending": []}
        degraded: list[dict[str, Any]] = []

        for d in decisions:
            status = str(d.get("status", "pending"))
            buckets.setdefault(status, []).append(d["id"])
            if status in ("warn", "failed"):
                last_ok_ts = int(d.get("last_ok_ts", 0) or 0)
                stuck_sec = (now - last_ok_ts) if last_ok_ts > 0 else -1
                degraded.append({
                    "id": d["id"],
                    "title": d.get("title", ""),
                    "owner_module": d.get("owner_module", ""),
                    "status": status,
                    "detail": d.get("detail", ""),
                    "stuck_sec": stuck_sec,
                    "metrics": _compact_metrics(d.get("metrics") or {}),
                    "last_update_ts": d.get("last_update_ts", 0),
                })

        overall = _overall(
            ok=len(buckets["ok"]),
            warn=len(buckets["warn"]),
            failed=len(buckets["failed"]),
            pending=len(buckets["pending"]),
        )

        with self._lock:
            events = list(self._events)

        return {
            "ts": now,
            "overall": overall,
            "counts": {
                "green": len(buckets["ok"]),
                "yellow": len(buckets["warn"]),
                "red": len(buckets["failed"]),
                "pending": len(buckets["pending"]),
            },
            "ok_ids": sorted(buckets["ok"]),
            "warn_ids": sorted(buckets["warn"]),
            "fail_ids": sorted(buckets["failed"]),
            "pending_ids": sorted(buckets["pending"]),
            "degraded": sorted(degraded, key=lambda x: x["id"]),
            "events": events[::-1],   # 新的在前
        }

    # ── 事件探测：tracker.mark 调用后触发 ────────────

    def _observe_if_due(self) -> None:
        now = int(time.time())
        with self._lock:
            if now - self._last_observe_ts < _OBSERVE_MIN_INTERVAL_SEC:
                return
            self._last_observe_ts = now
        self.observe()

    def observe(self) -> list[dict[str, Any]]:
        """立即轮询一次 tracker；返回本轮新增事件（可能为空）。"""
        try:
            from utils.decision_tracker import get_tracker
            data = get_tracker().get_summary_dict()
        except Exception:
            return []

        new_events: list[dict[str, Any]] = []
        now = int(time.time())
        with self._lock:
            for d in data.get("decisions") or []:
                d_id = str(d["id"])
                new_status = str(d.get("status", "pending"))
                old_status = self._prev_status.get(d_id)
                self._prev_status[d_id] = new_status
                if old_status is None:
                    # 首次见到，不计事件（避免 boot 噪声）
                    continue
                if old_status == new_status:
                    continue

                # 转换分类：degrade / recover / change
                if new_status in ("warn", "failed") and old_status == "ok":
                    kind = "degrade"
                elif new_status == "ok" and old_status in ("warn", "failed"):
                    kind = "recover"
                elif new_status == "failed" and old_status == "warn":
                    kind = "escalate"
                elif new_status == "warn" and old_status == "failed":
                    kind = "de-escalate"
                else:
                    kind = "change"

                evt = {
                    "ts": now,
                    "id": d_id,
                    "title": d.get("title", ""),
                    "from": old_status,
                    "to": new_status,
                    "kind": kind,
                    "detail": d.get("detail", ""),
                    "metrics": _compact_metrics(d.get("metrics") or {}),
                }
                self._events.append(evt)
                new_events.append(evt)

        if new_events:
            self._broadcast(new_events)
        return new_events

    # ── WebSocket 广播（可选） ─────────────────────

    def _broadcast(self, events: list[dict[str, Any]]) -> None:
        """尝试通过既有 ws_manager 推送；失败则静默（不影响功能）。"""
        try:
            from api.ws import push_to_all  # type: ignore
        except Exception:
            return
        try:
            import asyncio
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            for e in events:
                try:
                    if loop is not None and loop.is_running():
                        asyncio.ensure_future(
                            push_to_all("decision_health", e)
                        )
                    # 非运行中的 loop（脚本模式）时静默跳过推送
                except Exception:
                    logger.debug("[P2.2] broadcast push failed", exc_info=True)
        except Exception:
            logger.debug("[P2.2] broadcast entry failed", exc_info=True)

    # ── 测试辅助 ───────────────────────────────────

    def reset_for_testing(self) -> None:
        with self._lock:
            self._prev_status.clear()
            self._events.clear()
            self._last_observe_ts = 0

    def _empty(self) -> dict[str, Any]:
        return {
            "ts": int(time.time()),
            "overall": "pending",
            "counts": {"green": 0, "yellow": 0, "red": 0, "pending": 0},
            "ok_ids": [], "warn_ids": [], "fail_ids": [], "pending_ids": [],
            "degraded": [],
            "events": [],
        }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 辅助
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _compact_metrics(metrics: dict, limit: int = 6) -> dict:
    """只保留前 limit 个键，并截断长字符串。"""
    out: dict[str, Any] = {}
    for i, (k, v) in enumerate(metrics.items()):
        if i >= limit:
            break
        if isinstance(v, str) and len(v) > 80:
            out[k] = v[:77] + "..."
        else:
            out[k] = v
    return out


def _overall(*, ok: int, warn: int, failed: int, pending: int) -> str:
    if failed > 0:
        return "fail"
    if warn > 0:
        return "warn"
    if pending > 0 and ok == 0:
        return "pending"
    return "ok"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 单例
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_instance: Optional[HealthAggregator] = None
_lock = threading.Lock()


def get_health_aggregator() -> HealthAggregator:
    global _instance
    if _instance is None:
        with _lock:
            if _instance is None:
                _instance = HealthAggregator()
    return _instance
