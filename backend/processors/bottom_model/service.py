"""Bottom Model 服务层：每日调度、失败自愈重试与 API 门面。

调度纪律：
- 每日 UTC ``daily_run_hour_utc``（默认 01:00，北京 09:00）后运行一轮，
  此时 Coinglass/BGeometrics 的 T-1 链上数据已更新完整。
- 采集账本按目标日去重，重启补跑不重复消耗配额（BGeometrics 15/天）。
- 自愈：当日快照已出但仍有 spec 失败（如 BGeometrics 小时配额 429）时，
  每隔 ≥2h 轻量重试一次——collector 只会重拉失败项，成本极低。
- 手动触发走后台任务（一轮冷采集含 spacing 可达数分钟，不能阻塞 HTTP）。
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Optional

from processors.bottom_model.collector import BottomModelCollector, target_day_for
from processors.bottom_model.snapshot import build_snapshot
from sources.bgeometrics import create_bgeometrics_source
from sources.yahoo_cme import create_yahoo_cme_source
from storage.bottom_model_store import BottomModelStore

logger = logging.getLogger(__name__)

_SCHEDULER_TICK_SEC = 600       # 调度检查间隔
_RETRY_MIN_INTERVAL_SEC = 7200  # 失败 spec 重试最小间隔


class BottomModelService:
    def __init__(self, *, coinglass, settings):
        self._cfg = settings.bottom_model
        data_dir = self._cfg.data_dir
        if not os.path.isabs(data_dir):
            data_dir = os.path.join(os.path.dirname(os.path.dirname(
                os.path.dirname(__file__))), data_dir)
        self._store = BottomModelStore(data_dir)
        self._bg = create_bgeometrics_source(settings.bgeometrics)
        self._yahoo = create_yahoo_cme_source(settings.yahoo_cme)
        self._collector = BottomModelCollector(
            self._store, coinglass,
            bgeometrics=self._bg, yahoo_cme=self._yahoo,
            coinglass_spacing_sec=self._cfg.coinglass_spacing_sec,
        )
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._manual_task: Optional[asyncio.Task] = None
        # 懒创建：Python 3.9 的 asyncio.Lock() 构造期即要求事件循环，
        # 而本服务在 Engine.__init__（同步上下文）中实例化
        self._run_lock: Optional[asyncio.Lock] = None
        self._last_run_summary: Optional[dict[str, Any]] = None
        self._last_run_ts: float = 0.0

    # ── API 门面 ──

    @property
    def store(self) -> BottomModelStore:
        return self._store

    @property
    def enabled(self) -> bool:
        return bool(self._cfg.enabled)

    @property
    def running(self) -> bool:
        return self._running

    def latest(self) -> Optional[dict[str, Any]]:
        return self._store.latest_snapshot()

    def history(self, limit: int = 400) -> list[dict[str, Any]]:
        return self._store.snapshot_history(limit)

    def _run_in_progress(self) -> bool:
        return self._run_lock is not None and self._run_lock.locked()

    def health(self) -> dict[str, Any]:
        latest = self._store.latest_snapshot()
        return {
            "enabled": self.enabled,
            "running": self._running,
            "latest_day": latest["day"] if latest else None,
            "expected_day": target_day_for("daily"),
            "last_run_ts": int(self._last_run_ts) if self._last_run_ts else None,
            "last_run_summary": self._last_run_summary,
            "run_in_progress": self._run_in_progress(),
            **self._collector.health(),
        }

    # ── 生命周期 ──

    async def start(self) -> None:
        if self._running or not self.enabled:
            return
        self._running = True
        self._task = asyncio.create_task(self._run(), name="bottom-model-daily")
        logger.info("Bottom model service started | run_hour_utc=%d",
                    self._cfg.daily_run_hour_utc)

    async def stop(self) -> None:
        self._running = False
        for task in (self._task, self._manual_task):
            if task:
                task.cancel()
        await asyncio.gather(
            *(t for t in (self._task, self._manual_task) if t),
            return_exceptions=True,
        )
        for source in (self._bg, self._yahoo):
            if source is not None:
                try:
                    await source.close()
                except Exception:
                    pass
        self._store.close()
        logger.info("Bottom model service stopped")

    # ── 调度 ──

    async def _run(self) -> None:
        while self._running:
            try:
                if self._should_run_now():
                    await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Bottom model scheduled run failed")
            await asyncio.sleep(_SCHEDULER_TICK_SEC)

    def _should_run_now(self) -> bool:
        now = datetime.now(timezone.utc)
        if now.hour < self._cfg.daily_run_hour_utc:
            return False
        expected_day = target_day_for("daily", now)
        latest = self._store.latest_snapshot()
        if latest is None or latest.get("day", "") < expected_day:
            return True
        # 当日快照已出：仍有失败 spec 且距上次尝试 ≥2h → 轻量自愈重试
        failed = [
            item for item in self._store.fetch_log().values() if not item["last_ok"]
        ]
        if failed:
            newest_attempt = max(item["last_attempt_ts"] for item in failed)
            return time.time() - newest_attempt >= _RETRY_MIN_INTERVAL_SEC
        return False

    async def run_once(self, force: bool = False) -> dict[str, Any]:
        """采集一轮（账本去重）+ 重建快照。串行化防止并发重入。"""
        if self._run_lock is None:
            self._run_lock = asyncio.Lock()
        async with self._run_lock:
            summary = await self._collector.run_once(force=force)
            snapshot = await asyncio.to_thread(build_snapshot, self._store)
            self._store.prune(self._cfg.snapshot_retention_days)
            self._last_run_summary = {
                "fetched": summary["fetched"],
                "skipped_fresh": summary["skipped_fresh"],
                "failed": summary["failed"],
                "elapsed_sec": summary["elapsed_sec"],
            }
            self._last_run_ts = time.time()
            logger.info(
                "Bottom model run done | day=%s stress=%s confirmation=%s quadrant=%s",
                snapshot["day"],
                (snapshot["stress"] or {}).get("score"),
                snapshot["confirmation"].get("score"),
                snapshot["quadrant"].get("key"),
            )
            return snapshot

    def trigger_run(self, force: bool = False) -> dict[str, Any]:
        """手动触发（后台执行，立即返回）。"""
        if self._run_in_progress() or (
            self._manual_task is not None and not self._manual_task.done()
        ):
            return {"started": False, "reason": "run_in_progress"}

        async def _manual() -> None:
            try:
                await self.run_once(force=force)
            except Exception:
                logger.exception("Bottom model manual run failed")

        self._manual_task = asyncio.create_task(_manual(), name="bottom-model-manual")
        return {"started": True}
