"""Bottom Model 日级采集器：按注册表拉取三源原始序列并落库。

配额保护核心：
- 每个 FetchSpec 以采集账本 ``last_success_day`` 做日期戳去重——同一目标日
  成功过就不再外呼（force 除外）。进程重启后账本仍在，绝不重复消耗
  BGeometrics 15 次/天配额。
- Coinglass 请求之间强制 spacing（默认 10s），每日一轮约 19 个请求摊到
  ~3 分钟，不与常规轮询争抢 10/min 限流窗口。
- 每个 spec 独立 fail-open：单指标失败只记账本，不阻塞其余指标。
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from processors.bottom_model.metrics import FetchSpec, build_registry, metric_contract
from storage.bottom_model_store import BottomModelStore

logger = logging.getLogger(__name__)


def target_day_for(cadence: str, now: Optional[datetime] = None) -> str:
    """采集目标日：daily = 昨日（UTC，上游 T-1 更新）；weekly = 最近已收盘完整周的周一。"""
    current = now.astimezone(timezone.utc) if now else datetime.now(timezone.utc)
    if cadence == "weekly":
        monday = (current - timedelta(days=current.weekday())).date()
        last_complete_monday = monday - timedelta(days=7)
        return last_complete_monday.strftime("%Y-%m-%d")
    return (current.date() - timedelta(days=1)).strftime("%Y-%m-%d")


class BottomModelCollector:
    def __init__(
        self,
        store: BottomModelStore,
        coinglass: Any,
        bgeometrics: Any = None,
        yahoo_cme: Any = None,
        coinglass_spacing_sec: float = 10.0,
        registry: Optional[list[FetchSpec]] = None,
    ):
        self._store = store
        self._sources: dict[str, Any] = {
            "coinglass": coinglass,
            "bgeometrics": bgeometrics,
            "yahoo_cme": yahoo_cme,
        }
        self._cg_spacing = max(0.0, float(coinglass_spacing_sec))
        self._registry = registry if registry is not None else build_registry()
        # 懒创建：Python 3.9 的 asyncio.Lock() 构造期即要求事件循环，
        # 而本类在同步上下文（Engine.__init__/脚本）中实例化
        self._run_lock: Optional[asyncio.Lock] = None

    @property
    def registry(self) -> list[FetchSpec]:
        return self._registry

    async def run_once(
        self,
        force: bool = False,
        only_sources: Optional[set[str]] = None,
        now: Optional[datetime] = None,
    ) -> dict[str, Any]:
        """执行一轮采集。返回逐 spec 结果摘要（供脚本/健康接口展示）。"""
        if self._run_lock is None:
            self._run_lock = asyncio.Lock()
        async with self._run_lock:
            return await self._run_once_locked(force, only_sources, now)

    async def _run_once_locked(
        self,
        force: bool,
        only_sources: Optional[set[str]],
        now: Optional[datetime],
    ) -> dict[str, Any]:
        started = time.monotonic()
        results: dict[str, dict[str, Any]] = {}
        fetched = skipped = failed = 0
        prev_coinglass_call = 0.0

        for spec in self._registry:
            if only_sources is not None and spec.source not in only_sources:
                continue
            source = self._sources.get(spec.source)
            if source is None:
                results[spec.key] = {"status": "no_source"}
                continue

            target_day = target_day_for(spec.cadence, now)
            if not force and self._store.last_success_day(spec.key) >= target_day:
                results[spec.key] = {"status": "fresh", "target_day": target_day}
                skipped += 1
                continue

            # Coinglass 请求间 spacing，摊薄对全局 10/min 限流窗口的冲击
            if spec.source == "coinglass":
                elapsed = time.monotonic() - prev_coinglass_call
                if prev_coinglass_call > 0 and elapsed < self._cg_spacing:
                    await asyncio.sleep(self._cg_spacing - elapsed)
                prev_coinglass_call = time.monotonic()

            try:
                raw = await spec.fetch(source)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                raw = None
                logger.warning(
                    "BottomModel fetch failed | spec=%s err=%s: %s",
                    spec.key, type(exc).__name__, exc,
                )

            if raw is None:
                error = str(getattr(source, "last_error", "") or "fetch_returned_none")
                self._store.record_fetch(spec.key, ok=False, error=error)
                results[spec.key] = {"status": "failed", "error": error}
                failed += 1
                continue

            try:
                parsed = spec.parse(raw)
            except Exception as exc:
                error = f"parse_error: {type(exc).__name__}: {exc}"
                logger.warning("BottomModel parse failed | spec=%s %s", spec.key, error)
                self._store.record_fetch(spec.key, ok=False, error=error)
                results[spec.key] = {"status": "failed", "error": error}
                failed += 1
                continue

            accepted: dict[str, list[tuple[str, float]]] = {}
            rejected = 0
            for metric in spec.metrics:
                rows = parsed.get(metric) or []
                valid = [(day, value) for day, value in rows if day <= target_day]
                future = [(day, value) for day, value in rows if day > target_day]
                if future:
                    rejected += self._store.quarantine_rows(
                        metric, future, source=spec.source,
                        reason="AFTER_DECISION_CUTOFF",
                        payload={"target_day": target_day, "cadence": spec.cadence},
                    )
                accepted[metric] = valid

            required_errors: list[str] = []
            target = date.fromisoformat(target_day)
            # “展示可容忍几天滞后”与“本轮采集是否完成”是两套口径。
            # 采集默认必须命中目标日，否则账本会错误记成功并停止重试；只有
            # global M2 等显式声明发布滞后的 spec 才允许例外。
            tolerance = spec.staleness_days if spec.staleness_days is not None else 0
            for metric in spec.required():
                rows = accepted.get(metric) or []
                if not rows:
                    required_errors.append(f"{metric}:empty")
                    continue
                latest = date.fromisoformat(rows[-1][0])
                if (target - latest).days > tolerance:
                    required_errors.append(
                        f"{metric}:stale:{(target - latest).days}d>{tolerance}d"
                    )
            if required_errors:
                error = "required_output_invalid:" + ",".join(required_errors)
                self._store.record_fetch(spec.key, ok=False, error=error)
                results[spec.key] = {"status": "failed", "error": error}
                failed += 1
                continue

            total_rows = 0
            observation_rows = 0
            for metric, rows in accepted.items():
                if not rows:
                    continue
                total_rows += self._store.upsert_series(metric, rows)
                contract = metric_contract(metric)
                observation_rows += self._store.append_observations(
                    metric, rows, source=spec.source, cadence=spec.cadence,
                    unit=str(contract.get("unit") or "unknown"),
                    publication_lag_sec=spec.publication_lag_sec,
                    quality_flag="PIT_APPROX",
                )
            if total_rows <= 0:
                error = "parsed_empty"
                self._store.record_fetch(spec.key, ok=False, error=error)
                results[spec.key] = {"status": "failed", "error": error}
                failed += 1
                continue

            self._store.record_fetch(
                spec.key, ok=True, rows=total_rows, success_day=target_day,
            )
            results[spec.key] = {
                "status": "fetched", "rows": total_rows,
                "observation_versions": observation_rows,
                "quarantined": rejected, "target_day": target_day,
            }
            fetched += 1

        summary = {
            "fetched": fetched,
            "skipped_fresh": skipped,
            "failed": failed,
            "elapsed_sec": round(time.monotonic() - started, 1),
            "specs": results,
        }
        logger.info(
            "BottomModel collect done | fetched=%d fresh=%d failed=%d elapsed=%.1fs",
            fetched, skipped, failed, summary["elapsed_sec"],
        )
        return summary

    def health(self) -> dict[str, Any]:
        """采集健康：账本 + 覆盖概况 + BGeometrics 配额。"""
        bg = self._sources.get("bgeometrics")
        return {
            "fetch_log": self._store.fetch_log(),
            "coverage": self._store.coverage(),
            "bgeometrics_quota": bg.quota_snapshot() if bg is not None else None,
        }
