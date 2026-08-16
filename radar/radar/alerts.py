"""警报调度。

这是系统唯一对外输出的环节，也是最容易"技术上正确、体验上失败"的地方：
一个每小时发 40 封邮件的雷达，等价于一个不工作的雷达——
人会在第三天就把它拉进垃圾箱，然后错过真正重要的那一封。

因此警报有三层独立抑制，缺一不可：

  1. **状态机滞回**（在 states.py）——同一枚币不会反复进出 S1。
  2. **冷却窗口**（本模块）——同一枚币同一类警报在冷却期内只发一次。
     状态机防的是状态抖动，冷却防的是"降级后又升级"这类合法但高频的往返。
  3. **全局限速**（本模块）——所有币加起来每小时最多几封。
     行情狂热时几十枚币同时达标是常态，这时候必须自动合并成摘要，
     而不是老老实实发四十封。

Near-Miss 只落库不发信：它的价值在于三个月后回答
"如果当时把 S1 阈值从 72 降到 68，会多抓到几个赢家、多踩几个坑"。
这是反事实研究的数据，不是给人看的通知。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Mapping

from .domain.models import TokenState
from .obs.events import EventType, Severity, bus
from .obs.logging_setup import get_correlation_id, now_ms
from .registry import Evaluation
from .storage import repo
from .storage.db import Database, json_dump

logger = logging.getLogger("radar.alerts")

KIND_S1 = "S1"
KIND_S2 = "S2"
KIND_DISTRIBUTION = "DISTRIBUTION"
KIND_NEAR_MISS = "NEAR_MISS"

# 会发邮件的警报类型（Near-Miss 只落库）
_MAILABLE = (KIND_S1, KIND_S2, KIND_DISTRIBUTION)


@dataclass(slots=True)
class AlertRecord:
    """一条已生成的警报。"""

    alert_id: int | None
    kind: str
    token_key: tuple[str, str]
    symbol: str
    created_at: int
    scores: dict[str, float]
    is_near_miss: bool = False


@dataclass
class AlertStats:
    created: int = 0
    near_miss: int = 0
    suppressed_cooldown: int = 0
    suppressed_rate_limit: int = 0
    emails_queued: int = 0
    digests_queued: int = 0


class RateLimiter:
    """滚动小时窗口限速。"""

    def __init__(self, max_per_hour: int) -> None:
        self._max = max(1, max_per_hour)
        self._sent: deque[float] = deque()

    def _prune(self, now: float) -> None:
        cutoff = now - 3600.0
        while self._sent and self._sent[0] < cutoff:
            self._sent.popleft()

    def allow(self) -> bool:
        now = time.monotonic()
        self._prune(now)
        if len(self._sent) >= self._max:
            return False
        self._sent.append(now)
        return True

    def remaining(self) -> int:
        self._prune(time.monotonic())
        return max(0, self._max - len(self._sent))


class AnomalyDetector:
    """策略异常检测。

    要抓的是这种情况：某次配置改动或上游接口变化之后，
    S1 产出速率从每小时 2 个突然变成每小时 60 个。
    系统本身不会报错——它只是在按新的（错误的）标准疯狂发信号。

    冷启动期只累积基线不告警：运行不满两天时样本量不足，
    这时候做倍数比较必然误报，而误报会让人很快学会忽略这类告警。
    """

    def __init__(self, config: Mapping[str, Any]) -> None:
        self._warmup_ms = int(float(config.get("warmup_hours", 48)) * 3_600_000)
        self._window_ms = int(float(config.get("baseline_window_hours", 168)) * 3_600_000)
        self._multiple = float(config.get("deviation_multiple", 8.0))
        # 起点取第一次记录的时刻，而不是构造时的墙上时钟：
        # 回放历史数据时构造时间与数据时间相差几个月，
        # 用墙上时钟会让冷启动保护形同虚设，一开跑就开始误报
        self._started_at: int | None = None
        # 每类警报的发生时刻（有界，防止长期运行后无限增长）
        self._history: dict[str, deque[int]] = {}
        self._last_alarm_ms: dict[str, int] = {}

    def record(self, kind: str, at_ms: int) -> tuple[bool, float, float]:
        """记录一次警报，返回 (是否异常, 近一小时速率, 基线速率)。"""
        if self._started_at is None:
            self._started_at = at_ms
        history = self._history.setdefault(kind, deque(maxlen=5000))
        history.append(at_ms)

        cutoff = at_ms - self._window_ms
        while history and history[0] < cutoff:
            history.popleft()

        recent = sum(1 for ts in history if ts >= at_ms - 3_600_000)
        elapsed_ms = at_ms - self._started_at
        if elapsed_ms < self._warmup_ms:
            return False, float(recent), 0.0

        window_hours = max(1.0, min(elapsed_ms, self._window_ms) / 3_600_000)
        baseline = len(history) / window_hours
        if baseline <= 0:
            return False, float(recent), baseline

        anomalous = recent > max(3.0, baseline * self._multiple)
        if anomalous:
            # 异常告警本身也要防刷屏：同一类型一小时最多提醒一次
            last = self._last_alarm_ms.get(kind, 0)
            if at_ms - last < 3_600_000:
                return False, float(recent), baseline
            self._last_alarm_ms[kind] = at_ms
        return anomalous, float(recent), baseline


class AlertManager:
    def __init__(
        self,
        *,
        db: Database,
        config: Mapping[str, Any],
        fingerprint: Mapping[str, str],
        renderer: Any,
    ) -> None:
        self._db = db
        self._fingerprint = dict(fingerprint)
        self._renderer = renderer

        alerts_cfg = config.get("alerts", {}) or {}
        cooldown = alerts_cfg.get("cooldown_sec", {}) or {}
        self._cooldown_ms: dict[str, int] = {
            KIND_S1: int(cooldown.get("s1", 3600)) * 1000,
            KIND_S2: int(cooldown.get("s2", 1800)) * 1000,
            KIND_DISTRIBUTION: int(cooldown.get("distribution", 3600)) * 1000,
        }
        self._near_miss_cooldown_ms = int(
            alerts_cfg.get("near_miss_cooldown_sec", 600)
        ) * 1000
        self._anomaly = AnomalyDetector(alerts_cfg.get("anomaly", {}) or {})

        email_cfg = config.get("email", {}) or {}
        self._email_enabled = bool(email_cfg.get("enabled", True))
        self._send_kinds = {
            KIND_S1: bool(email_cfg.get("send_s1", True)),
            KIND_S2: bool(email_cfg.get("send_s2", True)),
            KIND_DISTRIBUTION: bool(email_cfg.get("send_distribution", True)),
        }
        self._rate_limiter = RateLimiter(int(email_cfg.get("max_per_hour", 12)))
        self._digest_on_overflow = bool(email_cfg.get("digest_on_overflow", True))

        # 冷却状态：(币, 类型) → 上次发出时刻
        self._last_alert_ms: dict[tuple[tuple[str, str], str], int] = {}
        self._last_near_miss_ms: dict[tuple[tuple[str, str], str], int] = {}
        # 被限速压下来的警报，等待合并成摘要
        self._digest_queue: list[AlertRecord] = []
        # 摘要自身的最小间隔。摘要不走 RateLimiter（它本来就是限速的溢出通道），
        # 所以必须自己限速：维护循环每 30 秒调一次 flush_digest，
        # 不加间隔的话行情狂热时一小时能发 120 封摘要，
        # 恰好在 max_per_hour 最该起作用的时刻把它彻底架空。
        # 何况 30 秒一封根本没起到"合并"的作用
        self._digest_min_interval_ms = int(
            float(email_cfg.get("digest_interval_sec", 900)) * 1000
        )
        self._last_digest_ms = 0
        self.stats = AlertStats()

    # ═════════════════════════════════════════════════════════════════════
    # 主入口
    # ═════════════════════════════════════════════════════════════════════

    async def handle(self, ev: Evaluation) -> AlertRecord | None:
        """处理一次评估结果。由采集器在每次评估后调用。"""
        kind = self._kind_for(ev)
        if kind is None:
            if ev.state.near_miss:
                await self._record_near_miss(ev)
            return None

        if not self._pass_cooldown(ev.view.key, kind, ev.evaluated_at):
            self.stats.suppressed_cooldown += 1
            bus.emit_token(
                EventType.ALERT_COOLDOWN,
                token=ev.view,
                module="alerts",
                summary=f"{kind} 在冷却期内，未重复通知",
            )
            return None

        record = await self._create_alert(ev, kind)
        if record is None:
            return None

        # 落库成功之后才开始计冷却。写库失败时那次判断等于从未发生过，
        # 若此时已经起算冷却，这枚币会在接下来整个窗口里被静默——
        # 既没有警报、没有邮件、也没有 Outcome 追踪，而且没有任何重试机会
        self._last_alert_ms[(ev.view.key, kind)] = ev.evaluated_at

        self._check_anomaly(kind, ev.evaluated_at)
        await self._notify(record, ev)
        return record

    def _kind_for(self, ev: Evaluation) -> str | None:
        """只有"进入"某状态才报警，维持在该状态不报。

        降级同样不报警：用户不需要为"某枚币从 S1 掉回 S0"收一封邮件，
        那既不可操作，数量也远多于晋升。
        """
        if not ev.state.changed:
            return None
        new = ev.state.new_state
        if new.rank <= ev.state.old_state.rank:
            return None
        if new in (TokenState.S2, TokenState.MOMENTUM):
            return KIND_S2
        if new == TokenState.S1:
            return KIND_S1
        if new == TokenState.DISTRIBUTION:
            return KIND_DISTRIBUTION
        return None

    def _pass_cooldown(self, key: tuple[str, str], kind: str, now_ms_: int) -> bool:
        """纯判断，不写入。冷却由调用方在警报确实落库后才起算。"""
        window = self._cooldown_ms.get(kind, 3600_000)
        last = self._last_alert_ms.get((key, kind))
        return last is None or now_ms_ - last >= window

    # ═════════════════════════════════════════════════════════════════════
    # 落库
    # ═════════════════════════════════════════════════════════════════════

    async def _create_alert(self, ev: Evaluation, kind: str) -> AlertRecord | None:
        view = ev.view
        if view.token_id is None:
            return None

        trigger = {
            "reason": ev.state.reason,
            "old_state": ev.state.old_state.value,
            "new_state": ev.state.new_state.value,
            "requirements": ev.state.as_dict()["requirements"].get(
                ev.state.new_state.value, []
            ),
            "risk": ev.risk.as_dict(),
            "quality": ev.quality.as_dict(),
            "market_cap": ev.market_cap,
            "mc_source": ev.mc_source,
            "distribution_reasons": ev.scores.distribution_reasons,
        }

        try:
            alert_id = await repo.insert_alert(
                self._db,
                view=view,
                alert_kind=kind,
                is_near_miss=False,
                created_at=ev.evaluated_at,
                correlation_id=get_correlation_id(),
                snapshot_id=ev.snapshot_id,
                scores=ev.scores,
                factors_json=json_dump(ev.scores.factors_as_list()),
                trigger_json=json_dump(trigger),
                prev_scores_json=json_dump(view.last_scores) if view.last_scores else None,
                fingerprint=self._fingerprint,
            )
        except Exception as exc:  # noqa: BLE001
            # 警报写库失败必须显式暴露：这条记录是 Outcome 追踪的起点，
            # 丢了就等于这次判断从未发生过，事后无法评估
            bus.emit(
                EventType.DB_WRITE_FAILED,
                module="alerts",
                summary=f"警报落库失败: {exc}",
                chain_id=view.chain_id,
                contract_address=view.contract_address,
            )
            return None

        self.stats.created += 1
        record = AlertRecord(
            alert_id=alert_id,
            kind=kind,
            token_key=view.key,
            symbol=view.symbol or view.contract_address[:10],
            created_at=ev.evaluated_at,
            scores=ev.scores.as_scores_dict(),
        )
        bus.emit_token(
            EventType.ALERT_CREATED,
            token=view,
            module="alerts",
            alert_id=alert_id,
            snapshot_id=ev.snapshot_id,
            summary=f"{kind} 警报｜{record.symbol}｜机会分 {ev.scores.opportunity:.0f}",
            payload={"kind": kind, "scores": record.scores, "trigger": trigger},
        )
        return record

    async def _record_near_miss(self, ev: Evaluation) -> None:
        """Near-Miss 只落库不发信。

        带冷却是必需的：分数在阈值附近震荡的币每个周期都会命中，
        不加限制会让 alerts 表被同一枚币的几百条记录淹没，
        反而让反事实研究更难做。
        """
        view = ev.view
        target = ev.state.blocked_state
        if view.token_id is None or target is None:
            return

        cooldown_key = (view.key, target.value)
        last = self._last_near_miss_ms.get(cooldown_key)
        if last is not None and ev.evaluated_at - last < self._near_miss_cooldown_ms:
            return
        self._last_near_miss_ms[cooldown_key] = ev.evaluated_at

        trigger = {
            "blocked_state": target.value,
            "blocked_by": [r.as_dict() for r in ev.state.blocked_by],
            "risk": ev.risk.as_dict(),
            "quality": ev.quality.as_dict(),
        }
        try:
            alert_id = await repo.insert_alert(
                self._db,
                view=view,
                alert_kind=target.value,
                is_near_miss=True,
                created_at=ev.evaluated_at,
                correlation_id=get_correlation_id(),
                snapshot_id=ev.snapshot_id,
                scores=ev.scores,
                factors_json=json_dump(ev.scores.factors_as_list()),
                trigger_json=json_dump(trigger),
                prev_scores_json=None,
                fingerprint=self._fingerprint,
            )
        except Exception:  # noqa: BLE001
            logger.warning("Near-Miss 落库失败 | %s", view.contract_address[:12])
            return

        self.stats.near_miss += 1
        bus.emit_token(
            EventType.DECISION_NEAR_MISS,
            token=view,
            module="alerts",
            alert_id=alert_id,
            summary=(
                f"差一点进 {target.value}："
                + "，".join(
                    f"{r.label} {r.actual:.1f}/{r.threshold:.1f}"
                    for r in ev.state.blocked_by
                    if r.actual is not None and r.threshold is not None
                )
            ),
            payload=trigger,
        )

    # ═════════════════════════════════════════════════════════════════════
    # 通知
    # ═════════════════════════════════════════════════════════════════════

    async def _notify(self, record: AlertRecord, ev: Evaluation) -> None:
        if not self._email_enabled or not self._send_kinds.get(record.kind, False):
            return
        if record.kind not in _MAILABLE:
            return

        if not self._rate_limiter.allow():
            self.stats.suppressed_rate_limit += 1
            if self._digest_on_overflow:
                # 不是丢弃，而是攒起来合并成一封摘要：
                # 行情狂热时几十枚币同时达标是常态，逐封发等于自毁通知渠道
                self._digest_queue.append(record)
            bus.emit_token(
                EventType.EMAIL_RATE_LIMITED,
                token=ev.view,
                module="alerts",
                summary=f"邮件限速，{record.kind} 转入摘要队列",
                payload={"pending_digest": len(self._digest_queue)},
            )
            return

        subject, html = self._renderer.render_alert(record, ev)
        await self._enqueue(
            kind=f"alert_{record.kind.lower()}",
            subject=subject,
            html=html,
            token_id=ev.view.token_id,
            alert_id=record.alert_id,
            created_at=record.created_at,
            idem_source=f"alert:{record.alert_id}",
        )

    async def flush_digest(self) -> bool:
        """把攒下的警报合并成一封摘要邮件。由后台任务定期调用。"""
        if not self._digest_queue:
            return False

        created_at = now_ms()
        # 第一封立即发（让人尽快知道正在限速），之后按间隔节流
        if self._last_digest_ms and created_at - self._last_digest_ms < self._digest_min_interval_ms:
            return False

        pending = self._digest_queue
        self._digest_queue = []
        self._last_digest_ms = created_at

        subject, html = self._renderer.render_digest(pending)
        # 摘要的幂等键包含内容指纹：同一批警报重放不会产生第二封，
        # 但下一批不同的警报仍能正常发出
        digest_id = hashlib.sha256(
            "|".join(f"{r.kind}:{r.alert_id}" for r in pending).encode("utf-8")
        ).hexdigest()[:16]
        await self._enqueue(
            kind="alert_digest",
            subject=subject,
            html=html,
            token_id=None,
            alert_id=None,
            created_at=created_at,
            idem_source=f"digest:{digest_id}",
        )
        self.stats.digests_queued += 1
        bus.emit(
            EventType.EMAIL_DIGEST_SENT,
            module="alerts",
            summary=f"合并 {len(pending)} 条警报为摘要邮件",
            payload={"count": len(pending),
                     "kinds": sorted({r.kind for r in pending})},
        )
        return True

    async def _enqueue(self, *, kind: str, subject: str, html: str,
                       token_id: int | None, alert_id: int | None,
                       created_at: int, idem_source: str) -> None:
        try:
            await repo.enqueue_email(
                self._db,
                idempotency_key=idem_source,
                kind=kind,
                subject=subject,
                html=html,
                token_id=token_id,
                alert_id=alert_id,
                created_at=created_at,
            )
        except Exception as exc:  # noqa: BLE001
            bus.emit(
                EventType.EMAIL_FAILED,
                module="alerts",
                summary=f"邮件入队失败: {exc}",
                payload={"kind": kind},
            )
            return
        self.stats.emails_queued += 1
        bus.emit(
            EventType.EMAIL_QUEUED,
            module="alerts",
            summary=f"邮件入队: {subject}",
            alert_id=alert_id,
            payload={"kind": kind},
        )

    # ═════════════════════════════════════════════════════════════════════
    # 异常检测
    # ═════════════════════════════════════════════════════════════════════

    def _check_anomaly(self, kind: str, at_ms: int) -> None:
        anomalous, recent, baseline = self._anomaly.record(kind, at_ms)
        if not anomalous:
            return
        bus.emit(
            EventType.STRATEGY_ANOMALY,
            module="alerts",
            severity=Severity.ERROR,
            summary=(
                f"{kind} 产出速率异常：近 1 小时 {recent:.0f} 次，"
                f"基线 {baseline:.1f} 次/小时"
            ),
            payload={
                "kind": kind,
                "recent_per_hour": recent,
                "baseline_per_hour": round(baseline, 2),
                "config_hash": self._fingerprint.get("config_hash"),
            },
        )

    # ── 诊断 ────────────────────────────────────────────────────────────
    def snapshot(self) -> dict[str, Any]:
        return {
            "created": self.stats.created,
            "near_miss": self.stats.near_miss,
            "suppressed_cooldown": self.stats.suppressed_cooldown,
            "suppressed_rate_limit": self.stats.suppressed_rate_limit,
            "emails_queued": self.stats.emails_queued,
            "digests_queued": self.stats.digests_queued,
            "email_budget_remaining": self._rate_limiter.remaining(),
            "digest_pending": len(self._digest_queue),
        }


# ═════════════════════════════════════════════════════════════════════════
# 邮件发送 worker
# ═════════════════════════════════════════════════════════════════════════

@dataclass
class OutboxStats:
    sent: int = 0
    failed: int = 0
    gave_up: int = 0


class EmailOutboxWorker:
    """从 outbox 表取待发邮件并投递。

    为什么要有 outbox 表，而不是在生成警报时直接发信：
    SMTP 可能超时、网络可能抖动、163 可能临时拒收。
    直接发信意味着这些情况下警报**永久丢失**，而且丢失时
    进程往往正忙着处理下一轮采集，没人会注意到。

    落库再异步投递则可以安全重试，且进程崩溃重启后未发的邮件仍在队列里。
    幂等键保证重放不会产生重复邮件。
    """

    def __init__(self, *, db: Database, transport: Any,
                 config: Mapping[str, Any]) -> None:
        self._db = db
        self._transport = transport
        email_cfg = config.get("email", {}) or {}
        self._enabled = bool(email_cfg.get("enabled", True))
        self._max_retries = int(email_cfg.get("outbox_max_retries", 5))
        self._backoff_sec = int(email_cfg.get("outbox_retry_backoff_sec", 120))
        self._batch = 5
        self._running = False
        self._task: asyncio.Task[None] | None = None
        self.stats = OutboxStats()

    async def start(self) -> None:
        if not self._enabled:
            logger.info("邮件通知已关闭，outbox worker 不启动")
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="email_outbox")

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _loop(self) -> None:
        while self._running:
            try:
                await self.process_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("邮件 worker 异常")
            await asyncio.sleep(10)

    async def process_once(self) -> int:
        """处理一批待发邮件，返回成功发出的数量。"""
        now = now_ms()
        rows = await self._db.fetch_all(
            "SELECT id, kind, subject, html, retry_count FROM email_outbox "
            "WHERE status='pending' AND next_retry_at <= ? "
            "ORDER BY created_at ASC LIMIT ?",
            (now, self._batch),
        )
        sent = 0
        for row in rows:
            if await self._deliver(row):
                sent += 1
        return sent

    async def _deliver(self, row: Mapping[str, Any]) -> bool:
        outbox_id = int(row["id"])
        try:
            await self._transport.send(subject=row["subject"], html=row["html"])
        except Exception as exc:  # noqa: BLE001
            retry_count = int(row["retry_count"]) + 1
            give_up = retry_count >= self._max_retries
            # 指数退避：SMTP 故障通常持续几分钟，每 10 秒重试只会
            # 把日志刷满，还可能触发服务商的连接限制
            delay = self._backoff_sec * (2 ** min(retry_count - 1, 4))
            repo.mark_email_failed(
                self._db, outbox_id,
                error=f"{type(exc).__name__}: {exc}",
                retry_count=retry_count,
                next_retry_at=now_ms() + delay * 1000,
                give_up=give_up,
            )
            self.stats.failed += 1
            if give_up:
                self.stats.gave_up += 1
            bus.emit(
                EventType.EMAIL_FAILED,
                module="email",
                severity=Severity.CRITICAL if give_up else Severity.WARNING,
                summary=(
                    f"邮件投递失败（第 {retry_count} 次）"
                    + ("，已放弃" if give_up else f"，{delay}s 后重试")
                ),
                payload={"outbox_id": outbox_id, "kind": row["kind"],
                         "error": str(exc)[:300]},
            )
            return False

        repo.mark_email_sent(self._db, outbox_id, now_ms())
        self.stats.sent += 1
        bus.emit(
            EventType.EMAIL_SENT,
            module="email",
            summary=f"邮件已发送: {row['subject']}",
            payload={"outbox_id": outbox_id, "kind": row["kind"]},
        )
        return True

    def snapshot(self) -> dict[str, Any]:
        return {
            "enabled": self._enabled,
            "sent": self.stats.sent,
            "failed": self.stats.failed,
            "gave_up": self.stats.gave_up,
        }
