"""服务组装根：把所有部件接成一个可启停的整体。

单独成模块而不是塞进 API 层，是因为启动顺序本身就是需要被审视的逻辑：
哪些必须在接客之前就绪、哪个环节失败可以降级继续、
停机时按什么顺序收尾才不会丢数据——这些问题的答案分散在各处时
最容易在深夜出事，而且事后很难复盘。

**启动顺序的依据**：
  1. 数据库先起：其余所有组件都要往里写。
  2. 恢复内存状态：重启后必须先把存量代币和未定案追踪捞回来，
     否则前几分钟会把老币当新币重新建档，状态全部从头开始。
  3. 后台 worker 与采集器最后起：此时依赖都已就绪。

**停机顺序恰好相反**，且必须等写队列排空。
容器收到 SIGTERM 后通常只有 10 秒宽限期，
这段时间里没排空的写入就是永久丢失的数据。
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import time
from typing import Any

import yaml

from .alerts import AlertManager, EmailOutboxWorker
from .collectors import CollectorService
from .notify import EmailRenderer, SmtpTransport
from .obs.events import EventType, bus
from .obs.logging_setup import now_ms, setup_logging
from .obs.metrics import metrics
from .pipeline import EvaluationPipeline
from .registry import TokenRegistry
from .scheduler import RequestScheduler
from .settings import Settings, load_settings
from .sources.client import BinanceClient
from .storage import repo
from .storage.db import Database
from .tracker import KpiReporter, OutcomeTracker, TrackerService

logger = logging.getLogger("radar.service")


class RadarService:
    """雷达服务的完整生命周期。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or load_settings()
        self.started_at_ms = 0
        self._running = False
        self._maintenance_task: asyncio.Task[None] | None = None
        # 配置页"保存并重启"置位；main.py 据此以非零码退出，
        # 交由 docker on-failure 策略拉起新进程
        self.restart_requested = False
        self._restart_task: asyncio.Task[None] | None = None

        storage_cfg = self.settings.storage
        data_dir = self.settings.data_dir
        data_dir.mkdir(parents=True, exist_ok=True)

        self.db = Database(
            data_dir / "radar.db",
            queue_size=int(storage_cfg.get("write_queue_size", 5000)),
            batch_size=int(storage_cfg.get("write_batch_size", 200)),
            flush_interval_sec=float(storage_cfg.get("write_flush_interval_sec", 2.0)),
            busy_timeout_ms=int(storage_cfg.get("busy_timeout_ms", 8000)),
        )
        self.scheduler = RequestScheduler(self.settings)
        self.client = BinanceClient(self.settings, self.scheduler)
        self.registry = TokenRegistry(
            db=self.db, events=bus, config=self.settings.raw,
            fingerprint=self.settings.fingerprint(),
        )
        self.renderer = EmailRenderer(
            tz_offset_hours=self.settings.tz_offset_hours,
            fingerprint=self.settings.fingerprint(),
        )
        self.alerts = AlertManager(
            db=self.db, config=self.settings.raw,
            fingerprint=self.settings.fingerprint(), renderer=self.renderer,
        )
        self.tracker = OutcomeTracker(
            db=self.db, config=self.settings.raw,
            fingerprint=self.settings.fingerprint(),
        )
        self.kpi = KpiReporter(
            db=self.db, config=self.settings.raw,
            fingerprint=self.settings.fingerprint(),
        )
        self.pipeline = EvaluationPipeline(alerts=self.alerts, tracker=self.tracker)
        self.collectors = CollectorService(
            client=self.client, scheduler=self.scheduler, registry=self.registry,
            db=self.db, settings=self.settings, on_evaluation=self.pipeline.process,
        )
        self.email_worker = EmailOutboxWorker(
            db=self.db, transport=SmtpTransport(self.settings.email),
            config=self.settings.raw,
        )
        self.tracker_service = TrackerService(
            tracker=self.tracker, kpi=self.kpi, config=self.settings.raw,
        )

    # ═════════════════════════════════════════════════════════════════════
    # 启停
    # ═════════════════════════════════════════════════════════════════════

    async def start(self) -> None:
        obs = self.settings.observability
        setup_logging(
            log_dir=self.settings.data_dir.parent / "logs",
            level=str(obs.get("log_level", "INFO")),
            # 键名与 config.yaml 保持一致（log_file_*）。此前读的是
            # log_max_mb / log_backup_count，恰好与默认值相同才没暴露
            max_mb=int(obs.get("log_file_max_mb", 64)),
            backup_count=int(obs.get("log_file_backup_count", 10)),
            tz_offset_hours=self.settings.tz_offset_hours,
        )
        bus.configure_fingerprint(self.settings.fingerprint())

        await self.db.start()
        bus.set_sink(repo.make_event_sink(self.db))

        # 配置指纹只在变化时落库，因此每次重启不会灌满这张表；
        # 但阈值一旦调整，旧配置的完整快照会永久保留——
        # 否则半年后无法回答"当时那套参数到底是什么"
        await repo.record_config_audit(
            self.db,
            recorded_at=now_ms(),
            fingerprint=self.settings.fingerprint(),
            config_snapshot=_read_config_text(self.settings),
            changes=None,
            operator="startup",
        )

        bus.emit(
            EventType.SERVICE_STARTED,
            module="service",
            summary=f"雷达启动｜策略 {self.settings.strategy_version}",
            payload={
                "chains": [c.id for c in self.settings.enabled_chains],
                "config_hash": self.settings.config_hash,
                "code_commit": self.settings.code_commit,
                "email_usable": self.settings.email.usable,
            },
        )
        if self.settings.email.enabled and not self.settings.email.usable:
            # 配置说要发邮件但凭据不全，是最容易在部署时踩到的坑：
            # 系统一切正常，只是所有警报都静静地烂在队列里
            logger.error("邮件已启用但 SMTP 凭据不完整，警报将无法送达")

        restored_tokens = await self.registry.restore(now_ms())
        restored_outcomes = await self.tracker.restore()
        logger.info("状态恢复完成｜代币 %d｜追踪 %d", restored_tokens, restored_outcomes)

        await self.client.start()
        await self.email_worker.start()
        await self.tracker_service.start()
        await self.collectors.start()

        self._running = True
        self.started_at_ms = now_ms()
        self._maintenance_task = asyncio.create_task(
            self._maintenance_loop(), name="maintenance"
        )

    async def stop(self) -> None:
        """收尾。

        刻意不因为"没在运行"就提前返回：启动可能在任意一步失败，
        此时部分组件已经起来了——数据库 writer 有队列、aiohttp 有连接池。
        直接退出会让那些写入连同 WAL 悬在半空。
        每一步单独兜底，一个组件收尾失败不能连累后面的。
        """
        was_running = self._running
        self._running = False

        # 先停采集：继续拉数据只会往正在关闭的队列里塞东西
        if self._maintenance_task is not None:
            self._maintenance_task.cancel()
            await asyncio.gather(self._maintenance_task, return_exceptions=True)
            self._maintenance_task = None

        for name, closer in (
            ("collectors", self.collectors.stop),
            ("tracker", self.tracker_service.stop),
            ("email", self.email_worker.stop),
            ("client", self.client.stop),
        ):
            try:
                await closer()
            except Exception:
                logger.exception("停机时 %s 收尾失败", name)

        if was_running:
            bus.emit(
                EventType.SERVICE_STOPPED,
                module="service",
                summary="雷达停机",
                payload={"uptime_sec": int((now_ms() - self.started_at_ms) / 1000)},
            )
        # db.stop 放最后并且必须执行：它会排空写队列再 checkpoint。
        # compose 里给了 40 秒宽限期，够它跑完
        await self.db.stop()

    # ═════════════════════════════════════════════════════════════════════
    # 管理端重启
    # ═════════════════════════════════════════════════════════════════════

    def request_restart(self, delay_sec: float = 0.8) -> None:
        """延迟触发优雅停机，让 HTTP 响应先送达前端。

        实现方式是给自己发 SIGTERM：uvicorn 的信号处理会走完整的
        lifespan 收尾（排空写队列、checkpoint），与容器 stop 完全同路，
        不需要单独维护第二条停机路径。
        """
        if self.restart_requested:
            return
        self.restart_requested = True
        logger.warning("收到管理端重启请求，%.1f 秒后开始优雅停机", delay_sec)

        async def _fire() -> None:
            await asyncio.sleep(delay_sec)
            self._terminate()

        self._restart_task = asyncio.get_running_loop().create_task(
            _fire(), name="admin-restart"
        )

    def _terminate(self) -> None:
        """独立成方法以便测试替换，不在测试进程里真发信号。"""
        os.kill(os.getpid(), signal.SIGTERM)

    # ═════════════════════════════════════════════════════════════════════
    # 周期性维护
    # ═════════════════════════════════════════════════════════════════════

    async def _maintenance_loop(self) -> None:
        """低频维护：限流自适应、爆发窗口清理、摘要邮件、周报、内存水位。"""
        cleanup_interval = float(self.settings.storage.get("cleanup_interval_sec", 3600))
        drift_cfg = self.settings.observability.get("drift", {}) or {}
        drift_interval = float(drift_cfg.get("check_interval_sec", 900))
        last_cleanup = time.monotonic()
        last_drift = time.monotonic()

        while self._running:
            await asyncio.sleep(30)
            if not self._running:
                return
            try:
                self.scheduler.evaluate_adaptive()
                self.scheduler.prune_burst()
                await self.alerts.flush_digest()
                await self._maybe_send_weekly_report()
                self._report_memory()
                if time.monotonic() - last_drift >= drift_interval:
                    last_drift = time.monotonic()
                    self._check_feature_drift(drift_cfg)
                if time.monotonic() - last_cleanup >= cleanup_interval:
                    last_cleanup = time.monotonic()
                    await self._cleanup()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("维护循环异常")

    def _check_feature_drift(self, drift_cfg: dict[str, Any]) -> None:
        """核心字段分布检查：NULL 率或恒定值占比越界即告警。

        评分器对缺失字段的处理是静默降级（该因子不参与打分），
        所以接口改版不会产生任何报错——只会让所有分数悄悄变钝。
        这里是唯一能把这种无声失效变成显式告警的地方。
        """
        drifted = metrics.check_drift(
            min_samples=int(drift_cfg.get("min_samples", 50)),
            max_null_ratio=float(drift_cfg.get("max_null_ratio", 0.6)),
            max_constant_ratio=float(drift_cfg.get("max_constant_ratio", 0.9)),
        )
        if not drifted:
            return
        names = ", ".join(d["name"] for d in drifted)
        # 窗口在每次检查后重置，持续异常最多每 check_interval_sec 发一条
        bus.emit(
            EventType.DATA_DRIFT,
            module="service",
            summary=f"特征分布异常：{names}（NULL 率或恒定值占比越界）",
            payload={"features": drifted},
        )

    async def _maybe_send_weekly_report(self) -> None:
        """通知质量周报：每周一次汇总近 7 天推送的实际表现。

        通知质量是 V2 的核心 KPI，必须闭环回到收件人——
        只有推送没有复盘，阈值永远不知道该往哪调。
        outbox 的幂等键（weekly:ISO周）保证进程重启也不会重发。
        """
        email_cfg = self.settings.raw.get("email", {}) or {}
        if not bool(email_cfg.get("send_weekly_report", True)):
            return
        if not (self.settings.email.enabled and self.settings.email.usable):
            return

        key = weekly_report_key(
            now_ms(),
            tz_offset_hours=self.settings.tz_offset_hours,
            send_hour=int(email_cfg.get("weekly_report_hour_local", 9)),
        )
        if key is None or key == getattr(self, "_last_weekly_key", None):
            return
        self._last_weekly_key = key

        summary = await self.kpi.summarize(window_days=7)
        subject, html = self.renderer.render_weekly_report(
            summary, generated_at=now_ms()
        )
        await repo.enqueue_email(
            self.db,
            idempotency_key=f"weekly:{key}",
            kind="WEEKLY_REPORT",
            subject=subject,
            html=html,
            token_id=None,
            alert_id=None,
            created_at=now_ms(),
        )
        logger.info("通知质量周报已入队（%s）", key)

    def _report_memory(self) -> None:
        rss_mb = _rss_mb()
        if rss_mb is None:
            return
        metrics.gauge("rss_mb", rss_mb)
        # 容器上限 512MB。超过 400MB 就要提前知道，
        # 因为 OOM Kill 不会留下任何日志，只会让服务无声消失
        if rss_mb > 400:
            bus.emit(
                EventType.MEMORY_WARNING,
                module="service",
                summary=f"内存占用 {rss_mb:.0f}MB，接近容器上限",
                payload={"rss_mb": round(rss_mb, 1),
                         "tokens_in_memory": len(self.registry)},
            )

    async def _cleanup(self) -> None:
        """分级留存清理。

        三类数据的价值衰减速度完全不同，因此水位线各自独立：
        原始归档过期即删（它只服务于"接口改版后重放验证"）；
        快照抽稀但保留决策现场；警报与 Outcome 永不删除。
        """
        now = now_ms()
        storage = self.settings.storage
        removed: dict[str, int] = {}

        expired = await self.db.fetch_one(
            "SELECT COUNT(*) AS n FROM raw_archive WHERE expires_at IS NOT NULL "
            "AND expires_at < ?", (now,),
        )
        if expired and int(expired["n"]) > 0:
            self.db.submit(
                "DELETE FROM raw_archive WHERE expires_at IS NOT NULL AND expires_at < ?",
                (now,), label="cleanup_raw",
            )
            removed["raw_archive"] = int(expired["n"])

        # 抽稀：只动"非决策现场"的快照。keep_forever 标记的是
        # 警报当时那一帧，删掉它等于让那条警报永远无法复盘。
        # 两级抽稀：48h 后 5 分钟粒度（近期复盘还需要细节），
        # 168h 后进一步到 1 小时粒度（一周后只需要形态轮廓）
        self._downsample_snapshots(
            cutoff=now - int(float(storage.get("downsample_after_hours", 48))
                             * 3_600_000),
            interval_sec=int(storage.get("downsample_interval_sec", 300)),
            label="cleanup_downsample",
        )
        self._downsample_snapshots(
            cutoff=now - int(float(storage.get("downsample_2_after_hours", 168))
                             * 3_600_000),
            interval_sec=int(storage.get("downsample_2_interval_sec", 3600)),
            label="cleanup_downsample_2",
        )

        retention_days = int(
            float(storage.get("raw_detail_retention_days", 400))
        )
        self.db.submit(
            "DELETE FROM radar_events WHERE occurred_at < ? AND importance IN ('low')",
            (now - min(30, retention_days) * 86_400_000,),
            label="cleanup_events",
        )

        await self.db.drain()
        bus.emit(
            EventType.RETENTION_CLEANUP,
            module="service",
            summary="分级留存清理完成",
            payload=removed or {"note": "无过期归档"},
        )
        self._check_db_watermark()

    def _downsample_snapshots(self, *, cutoff: int, interval_sec: int,
                              label: str) -> None:
        """按时间粒度抽稀 cutoff 之前的快照，每个粒度桶保留最早一帧。"""
        self.db.submit(
            "DELETE FROM snapshots WHERE keep_forever = 0 AND observed_at < ? "
            "AND snapshot_id NOT IN ("
            "  SELECT MIN(snapshot_id) FROM snapshots "
            "  WHERE keep_forever = 0 AND observed_at < ? "
            "  GROUP BY token_id, observed_at / ?"
            ")",
            (cutoff, cutoff, interval_sec * 1000),
            label=label,
        )

    def _check_db_watermark(self) -> None:
        """数据库体积水位检查。只告警不自动删：留存策略已经在
        清理任务里按设计执行，越过水位说明设计假设本身失效
        （数据增速超预期），该调整的是配置或磁盘，不是让程序自作主张。"""
        max_db_gb = float(self.settings.storage.get("max_db_gb", 0) or 0)
        if max_db_gb <= 0:
            return
        db_path = self.settings.data_dir / "radar.db"
        try:
            total = sum(
                p.stat().st_size
                for p in (db_path, db_path.with_suffix(".db-wal"))
                if p.exists()
            )
        except OSError:
            return
        total_gb = total / 1e9
        if total_gb > max_db_gb:
            bus.emit(
                EventType.STORAGE_WATERMARK,
                module="service",
                summary=(
                    f"数据库体积 {total_gb:.2f}GB 超过水位 {max_db_gb:.1f}GB，"
                    "请检查留存配置或扩容磁盘"
                ),
                payload={"db_gb": round(total_gb, 3), "max_db_gb": max_db_gb},
            )

    # ═════════════════════════════════════════════════════════════════════
    # 诊断
    # ═════════════════════════════════════════════════════════════════════

    @property
    def running(self) -> bool:
        return self._running

    def health(self) -> dict[str, Any]:
        uptime = 0 if not self.started_at_ms else int(
            (now_ms() - self.started_at_ms) / 1000
        )
        last_cycle = self.collectors.last_cycle
        # 就绪的判据是"最近确实采到过数据"，而不是"进程还活着"：
        # 一个所有接口都在超时的雷达在进程层面完全健康，
        # 但它已经不再是雷达了
        collector_ok = bool(last_cycle and last_cycle.succeeded > 0)
        return {
            "status": "ok" if (self._running and collector_ok) else "degraded",
            "running": self._running,
            "uptime_sec": uptime,
            "tokens_in_memory": len(self.registry),
            "rss_mb": _rss_mb(),
            "last_cycle_at": None if last_cycle is None else last_cycle.started_at,
            "collector_ok": collector_ok,
            "email_usable": self.settings.email.usable,
            "version": self.settings.fingerprint(),
        }

    def diagnostics(self) -> dict[str, Any]:
        return {
            "health": self.health(),
            "scheduler": self.scheduler.snapshot(),
            "collectors": self.collectors.snapshot(),
            "registry": {
                "tokens": len(self.registry),
                "states": self.registry.state_counts(),
            },
            "alerts": self.alerts.snapshot(),
            "tracker": self.tracker.snapshot(),
            "pipeline": self.pipeline.snapshot(),
            "email": self.email_worker.snapshot(),
            "metrics": metrics.snapshot(),
            "events": bus.counts(),
        }


def weekly_report_key(at_ms: int, *, tz_offset_hours: int,
                      send_hour: int) -> str | None:
    """周报的发送键：本地时间每周一 send_hour 点之后返回该 ISO 周的键。

    独立成纯函数以便测试。返回 None 表示当前不在发送窗口
    （周一 send_hour 之前，或不是周一——周一错过则顺延到当周任意
    时刻首次满足条件时补发，避免"重启恰好跨过周一早上"漏掉一期）。
    """
    from datetime import datetime, timedelta, timezone

    local = datetime.fromtimestamp(
        at_ms / 1000, timezone(timedelta(hours=tz_offset_hours))
    )
    iso_year, iso_week, iso_weekday = local.isocalendar()
    if iso_weekday == 1 and local.hour < send_hour:
        return None
    return f"{iso_year}-W{iso_week:02d}"


def _rss_mb() -> float | None:
    """读取常驻内存。

    刻意不引入 psutil：为了一个数字多装一个依赖，
    在 512MB 的容器里并不划算。Linux 直接读 /proc 即可，
    非 Linux（本地开发）返回 None 而不是报错。
    """
    try:
        with open("/proc/self/statm", "r", encoding="ascii") as handle:
            pages = int(handle.read().split()[1])
        return pages * 4096 / 1_048_576
    except (OSError, IndexError, ValueError):
        return None


def _read_config_text(settings: Settings) -> str:
    """审计快照存**生效配置**（默认值 + 覆盖层合并结果）。

    存 config.yaml 原文的话，覆盖层的改动就在审计里隐形了——
    半年后复盘时会以为当时跑的是出厂参数。
    """
    try:
        return yaml.safe_dump(settings.raw, allow_unicode=True, sort_keys=True)
    except yaml.YAMLError:
        return ""
