"""业务事件与决策审计（第二、三层可观测）。

与运维日志的分工：
  - 运维日志（logging_setup）回答"程序有没有出问题"，高频、走文件。
  - 本模块回答"雷达做了什么"以及"为什么这样判断"，低频但必须可检索，
    因此落 SQLite 的 radar_events 表。

事件分类法从第一天固定：禁止散落着写 "api error" / "币安请求失败" / "request failed"
这类自由文本，否则日后无法按类型聚合统计。

severity 与 importance 是两个独立维度：
  - severity  = 技术严重度（程序是否有故障）
  - importance = 业务重要度（这件事对交易决策是否重要）
一个 S2 警报是 severity=NOTICE + importance=HIGH；
数据库写失败是 severity=CRITICAL + importance=SYSTEM。
混用会让日志排序完全失去意义。
"""

from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

from .logging_setup import get_correlation_id, now_ms
from .redact import redact

logger = logging.getLogger("radar.events")


class Severity(str, Enum):
    """技术严重度。业务动作最高只能到 NOTICE；CRITICAL 仅限系统性故障。"""

    DEBUG = "DEBUG"
    INFO = "INFO"
    NOTICE = "NOTICE"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"

    @property
    def rank(self) -> int:
        return _SEVERITY_RANK[self]


_SEVERITY_RANK: dict[Severity, int] = {
    Severity.DEBUG: 10,
    Severity.INFO: 20,
    Severity.NOTICE: 25,
    Severity.WARNING: 30,
    Severity.ERROR: 40,
    Severity.CRITICAL: 50,
}


class Importance(str, Enum):
    """业务重要度。SYSTEM 表示与交易决策无关但影响系统可信度。"""

    LOW = "LOW"
    NORMAL = "NORMAL"
    HIGH = "HIGH"
    SYSTEM = "SYSTEM"


class Category(str, Enum):
    DISCOVERY = "discovery"
    COLLECTOR = "collector"
    DATA = "data"
    RISK = "risk"
    STRATEGY = "strategy"
    MILESTONE = "milestone"
    ALERT = "alert"
    NOTIFICATION = "notification"
    STORAGE = "storage"
    SYSTEM = "system"
    SCHEDULER = "scheduler"
    CONFIG = "config"
    USER = "user"


class EventType(str, Enum):
    # ── Discovery ──────────────────────────────────────────────────────
    TOKEN_DISCOVERED = "TOKEN_DISCOVERED"
    TOKEN_REGISTERED = "TOKEN_REGISTERED"
    TOKEN_REACTIVATED = "TOKEN_REACTIVATED"

    # ── Collector ──────────────────────────────────────────────────────
    API_REQUEST_FAILED = "API_REQUEST_FAILED"
    API_RATE_LIMITED = "API_RATE_LIMITED"
    API_SCHEMA_CHANGED = "API_SCHEMA_CHANGED"
    API_DEGRADED = "API_DEGRADED"
    API_RECOVERED = "API_RECOVERED"
    SOURCE_STALE = "SOURCE_STALE"

    # ── Data ───────────────────────────────────────────────────────────
    DATA_MISSING = "DATA_MISSING"
    DATA_CONFLICT = "DATA_CONFLICT"
    DATA_QUALITY_DEGRADED = "DATA_QUALITY_DEGRADED"
    DATA_QUALITY_RECOVERED = "DATA_QUALITY_RECOVERED"
    FEATURE_DRIFT = "FEATURE_DRIFT"
    DATA_DRIFT = "DATA_DRIFT"

    # ── Risk ───────────────────────────────────────────────────────────
    AUDIT_FAILED = "AUDIT_FAILED"
    HONEYPOT_DETECTED = "HONEYPOT_DETECTED"
    EXECUTION_BLOCKED = "EXECUTION_BLOCKED"
    RISK_GATE_BLOCKED = "RISK_GATE_BLOCKED"
    RISK_GATE_RECOVERED = "RISK_GATE_RECOVERED"
    TOKEN_REJECTED = "TOKEN_REJECTED"

    # ── Strategy ───────────────────────────────────────────────────────
    STATE_TRANSITION = "STATE_TRANSITION"
    S0_ENTER = "S0_ENTER"
    S1_ENTER = "S1_ENTER"
    S2_ENTER = "S2_ENTER"
    STATE_DOWNGRADE = "STATE_DOWNGRADE"
    DISTRIBUTION_ENTER = "DISTRIBUTION_ENTER"
    DISTRIBUTION_RECOVERY = "DISTRIBUTION_RECOVERY"
    DORMANT_ENTER = "DORMANT_ENTER"
    DEAD_ENTER = "DEAD_ENTER"
    DECISION_NEAR_MISS = "DECISION_NEAR_MISS"
    STRATEGY_ANOMALY = "STRATEGY_ANOMALY"

    # ── Milestone ──────────────────────────────────────────────────────
    MC_MILESTONE = "MC_MILESTONE"
    OUTCOME_FINALIZED = "OUTCOME_FINALIZED"
    KPI_GENERATED = "KPI_GENERATED"

    # ── Alert ──────────────────────────────────────────────────────────
    ALERT_CREATED = "ALERT_CREATED"
    ALERT_SUPPRESSED = "ALERT_SUPPRESSED"
    ALERT_COOLDOWN = "ALERT_COOLDOWN"

    # ── Notification ───────────────────────────────────────────────────
    EMAIL_QUEUED = "EMAIL_QUEUED"
    EMAIL_SENT = "EMAIL_SENT"
    EMAIL_FAILED = "EMAIL_FAILED"
    EMAIL_RATE_LIMITED = "EMAIL_RATE_LIMITED"
    EMAIL_DIGEST_SENT = "EMAIL_DIGEST_SENT"

    # ── Storage ────────────────────────────────────────────────────────
    DB_BUSY = "DB_BUSY"
    DB_QUEUE_HIGH = "DB_QUEUE_HIGH"
    DB_WRITE_FAILED = "DB_WRITE_FAILED"
    DB_READ_FAILED = "DB_READ_FAILED"
    REGISTRY_RESTORED = "REGISTRY_RESTORED"
    BACKUP_COMPLETED = "BACKUP_COMPLETED"
    BACKUP_FAILED = "BACKUP_FAILED"
    RETENTION_CLEANUP = "RETENTION_CLEANUP"

    # ── System ─────────────────────────────────────────────────────────
    SERVICE_STARTED = "SERVICE_STARTED"
    SERVICE_STOPPED = "SERVICE_STOPPED"
    CONFIG_LOADED = "CONFIG_LOADED"
    MEMORY_WARNING = "MEMORY_WARNING"

    # ── Scheduler ──────────────────────────────────────────────────────
    POLL_INTERVAL_CHANGED = "POLL_INTERVAL_CHANGED"
    BUDGET_SATURATED = "BUDGET_SATURATED"
    TIER_DEGRADED = "TIER_DEGRADED"
    BURST_WINDOW_OPENED = "BURST_WINDOW_OPENED"
    BURST_WINDOW_CLOSED = "BURST_WINDOW_CLOSED"
    ONBOARDING_THROTTLED = "ONBOARDING_THROTTLED"

    # ── Config ─────────────────────────────────────────────────────────
    CONFIG_CHANGED = "CONFIG_CHANGED"

    # ── User（V1 预留，前端人工操作在 V1.5 接入）────────────────────────
    USER_TOKEN_TAGGED = "USER_TOKEN_TAGGED"
    USER_ALERT_CLOSED = "USER_ALERT_CLOSED"
    USER_PRIORITY_CHANGED = "USER_PRIORITY_CHANGED"
    USER_REPLAY_STARTED = "USER_REPLAY_STARTED"
    USER_EXPORT_STARTED = "USER_EXPORT_STARTED"


# 事件默认属性表：(分类, 技术严重度, 业务重要度)
_SPEC: dict[EventType, tuple[Category, Severity, Importance]] = {
    EventType.TOKEN_DISCOVERED: (Category.DISCOVERY, Severity.DEBUG, Importance.LOW),
    EventType.TOKEN_REGISTERED: (Category.DISCOVERY, Severity.INFO, Importance.LOW),
    EventType.TOKEN_REACTIVATED: (Category.DISCOVERY, Severity.INFO, Importance.NORMAL),

    EventType.API_REQUEST_FAILED: (Category.COLLECTOR, Severity.WARNING, Importance.SYSTEM),
    EventType.API_RATE_LIMITED: (Category.COLLECTOR, Severity.WARNING, Importance.SYSTEM),
    EventType.API_SCHEMA_CHANGED: (Category.COLLECTOR, Severity.ERROR, Importance.SYSTEM),
    EventType.API_DEGRADED: (Category.COLLECTOR, Severity.ERROR, Importance.SYSTEM),
    EventType.API_RECOVERED: (Category.COLLECTOR, Severity.NOTICE, Importance.SYSTEM),
    EventType.SOURCE_STALE: (Category.COLLECTOR, Severity.WARNING, Importance.SYSTEM),

    EventType.DATA_MISSING: (Category.DATA, Severity.WARNING, Importance.SYSTEM),
    EventType.DATA_CONFLICT: (Category.DATA, Severity.WARNING, Importance.SYSTEM),
    EventType.DATA_QUALITY_DEGRADED: (Category.DATA, Severity.WARNING, Importance.HIGH),
    EventType.DATA_QUALITY_RECOVERED: (Category.DATA, Severity.NOTICE, Importance.NORMAL),
    EventType.FEATURE_DRIFT: (Category.DATA, Severity.ERROR, Importance.SYSTEM),
    EventType.DATA_DRIFT: (Category.DATA, Severity.ERROR, Importance.SYSTEM),

    EventType.AUDIT_FAILED: (Category.RISK, Severity.WARNING, Importance.NORMAL),
    EventType.HONEYPOT_DETECTED: (Category.RISK, Severity.NOTICE, Importance.HIGH),
    EventType.EXECUTION_BLOCKED: (Category.RISK, Severity.NOTICE, Importance.HIGH),
    EventType.RISK_GATE_BLOCKED: (Category.RISK, Severity.INFO, Importance.NORMAL),
    EventType.RISK_GATE_RECOVERED: (Category.RISK, Severity.INFO, Importance.NORMAL),
    EventType.TOKEN_REJECTED: (Category.RISK, Severity.INFO, Importance.LOW),

    EventType.STATE_TRANSITION: (Category.STRATEGY, Severity.INFO, Importance.NORMAL),
    EventType.S0_ENTER: (Category.STRATEGY, Severity.INFO, Importance.NORMAL),
    EventType.S1_ENTER: (Category.STRATEGY, Severity.NOTICE, Importance.HIGH),
    EventType.S2_ENTER: (Category.STRATEGY, Severity.NOTICE, Importance.HIGH),
    EventType.STATE_DOWNGRADE: (Category.STRATEGY, Severity.INFO, Importance.NORMAL),
    EventType.DISTRIBUTION_ENTER: (Category.STRATEGY, Severity.NOTICE, Importance.HIGH),
    EventType.DISTRIBUTION_RECOVERY: (Category.STRATEGY, Severity.INFO, Importance.NORMAL),
    EventType.DORMANT_ENTER: (Category.STRATEGY, Severity.DEBUG, Importance.LOW),
    EventType.DEAD_ENTER: (Category.STRATEGY, Severity.INFO, Importance.LOW),
    EventType.DECISION_NEAR_MISS: (Category.STRATEGY, Severity.DEBUG, Importance.NORMAL),
    EventType.STRATEGY_ANOMALY: (Category.STRATEGY, Severity.ERROR, Importance.SYSTEM),

    EventType.MC_MILESTONE: (Category.MILESTONE, Severity.NOTICE, Importance.HIGH),
    # Outcome 定案是研究链路的终点事件，重要性高于普通里程碑：
    # 它标记"这条判断已经可以进入 KPI 统计了"
    EventType.OUTCOME_FINALIZED: (Category.MILESTONE, Severity.INFO, Importance.HIGH),
    EventType.KPI_GENERATED: (Category.MILESTONE, Severity.INFO, Importance.NORMAL),

    EventType.ALERT_CREATED: (Category.ALERT, Severity.NOTICE, Importance.HIGH),
    EventType.ALERT_SUPPRESSED: (Category.ALERT, Severity.DEBUG, Importance.LOW),
    EventType.ALERT_COOLDOWN: (Category.ALERT, Severity.DEBUG, Importance.LOW),

    EventType.EMAIL_QUEUED: (Category.NOTIFICATION, Severity.INFO, Importance.NORMAL),
    EventType.EMAIL_SENT: (Category.NOTIFICATION, Severity.INFO, Importance.NORMAL),
    EventType.EMAIL_FAILED: (Category.NOTIFICATION, Severity.ERROR, Importance.SYSTEM),
    EventType.EMAIL_RATE_LIMITED: (Category.NOTIFICATION, Severity.WARNING, Importance.HIGH),
    EventType.EMAIL_DIGEST_SENT: (Category.NOTIFICATION, Severity.INFO, Importance.NORMAL),

    EventType.DB_BUSY: (Category.STORAGE, Severity.WARNING, Importance.SYSTEM),
    EventType.DB_QUEUE_HIGH: (Category.STORAGE, Severity.WARNING, Importance.SYSTEM),
    EventType.DB_WRITE_FAILED: (Category.STORAGE, Severity.CRITICAL, Importance.SYSTEM),
    # 读失败不像写失败那样丢数据，但会让系统基于不完整状态决策，仍属高危
    EventType.DB_READ_FAILED: (Category.STORAGE, Severity.ERROR, Importance.SYSTEM),
    EventType.REGISTRY_RESTORED: (Category.SYSTEM, Severity.NOTICE, Importance.SYSTEM),
    EventType.BACKUP_COMPLETED: (Category.STORAGE, Severity.INFO, Importance.SYSTEM),
    EventType.BACKUP_FAILED: (Category.STORAGE, Severity.ERROR, Importance.SYSTEM),
    EventType.RETENTION_CLEANUP: (Category.STORAGE, Severity.INFO, Importance.SYSTEM),

    EventType.SERVICE_STARTED: (Category.SYSTEM, Severity.NOTICE, Importance.SYSTEM),
    EventType.SERVICE_STOPPED: (Category.SYSTEM, Severity.NOTICE, Importance.SYSTEM),
    EventType.CONFIG_LOADED: (Category.SYSTEM, Severity.INFO, Importance.SYSTEM),
    EventType.MEMORY_WARNING: (Category.SYSTEM, Severity.WARNING, Importance.SYSTEM),

    EventType.POLL_INTERVAL_CHANGED: (Category.SCHEDULER, Severity.DEBUG, Importance.LOW),
    EventType.BUDGET_SATURATED: (Category.SCHEDULER, Severity.WARNING, Importance.SYSTEM),
    EventType.TIER_DEGRADED: (Category.SCHEDULER, Severity.WARNING, Importance.SYSTEM),
    EventType.BURST_WINDOW_OPENED: (Category.SCHEDULER, Severity.INFO, Importance.NORMAL),
    EventType.BURST_WINDOW_CLOSED: (Category.SCHEDULER, Severity.DEBUG, Importance.LOW),
    EventType.ONBOARDING_THROTTLED: (Category.SCHEDULER, Severity.INFO, Importance.SYSTEM),

    EventType.CONFIG_CHANGED: (Category.CONFIG, Severity.NOTICE, Importance.SYSTEM),

    EventType.USER_TOKEN_TAGGED: (Category.USER, Severity.INFO, Importance.NORMAL),
    EventType.USER_ALERT_CLOSED: (Category.USER, Severity.INFO, Importance.NORMAL),
    EventType.USER_PRIORITY_CHANGED: (Category.USER, Severity.INFO, Importance.NORMAL),
    EventType.USER_REPLAY_STARTED: (Category.USER, Severity.INFO, Importance.SYSTEM),
    EventType.USER_EXPORT_STARTED: (Category.USER, Severity.INFO, Importance.LOW),
}


def spec_for(event_type: EventType) -> tuple[Category, Severity, Importance]:
    return _SPEC.get(event_type, (Category.SYSTEM, Severity.INFO, Importance.NORMAL))


@dataclass
class RadarEvent:
    """一条业务事件 / 决策审计记录。

    高频查询字段是独立列；不常查询的明细放 payload（落库为 JSON）。
    """

    event_type: EventType
    category: Category
    severity: Severity
    importance: Importance
    module: str
    occurred_at: int
    summary: str = ""
    chain_id: str | None = None
    token_id: int | None = None
    contract_address: str | None = None
    symbol: str | None = None
    correlation_id: str = ""
    old_state: str | None = None
    new_state: str | None = None
    snapshot_id: int | None = None
    alert_id: int | None = None
    duration_ms: int | None = None
    strategy_version: str = ""
    feature_version: str = ""
    config_hash: str = ""
    payload: dict[str, Any] = field(default_factory=dict)

    def log_level(self) -> int:
        # NOTICE 不是 Python 标准级别，映射到 INFO 输出但保留语义字段
        return {
            Severity.DEBUG: logging.DEBUG,
            Severity.INFO: logging.INFO,
            Severity.NOTICE: logging.INFO,
            Severity.WARNING: logging.WARNING,
            Severity.ERROR: logging.ERROR,
            Severity.CRITICAL: logging.CRITICAL,
        }[self.severity]


EventSink = Callable[[RadarEvent], None]


class ErrorAggregator:
    """同类错误聚合降噪。

    币安接口连续失败 200 次不应产生 200 条一模一样的 WARNING，
    而应聚合成一条 API_DEGRADED（起始时间 + 累计次数），
    恢复时再补一条 API_RECOVERED（持续时长 + 总失败数）。
    """

    def __init__(self, window_sec: int = 300) -> None:
        self._window_sec = window_sec
        # key → [首次时间, 累计次数, 是否已发出 DEGRADED]
        self._state: dict[str, list[Any]] = {}

    def record_failure(self, key: str) -> tuple[bool, int, float]:
        """记录一次失败。

        返回 (是否应发出 DEGRADED 事件, 累计失败次数, 已持续秒数)。
        """
        now = time.time()
        entry = self._state.get(key)
        if entry is None:
            self._state[key] = [now, 1, True]
            return True, 1, 0.0
        entry[1] += 1
        elapsed = now - entry[0]
        # 窗口内只发一次；跨过窗口后允许再提醒一次（问题仍在持续）
        if not entry[2] or elapsed >= self._window_sec:
            entry[2] = True
            entry[0] = now if elapsed >= self._window_sec else entry[0]
            return True, int(entry[1]), elapsed
        return False, int(entry[1]), elapsed

    def record_success(self, key: str) -> tuple[bool, int, float]:
        """记录一次成功。

        返回 (是否应发出 RECOVERED 事件, 期间总失败次数, 降级持续秒数)。
        """
        entry = self._state.pop(key, None)
        if entry is None:
            return False, 0, 0.0
        return True, int(entry[1]), time.time() - entry[0]

    def is_degraded(self, key: str) -> bool:
        return key in self._state

    def degraded_keys(self) -> dict[str, dict[str, Any]]:
        now = time.time()
        return {
            key: {"since_ms": int(v[0] * 1000), "failures": int(v[1]), "elapsed_sec": now - v[0]}
            for key, v in self._state.items()
        }


class EventBus:
    """事件总线：写日志 + 落库（通过 sink）。

    sink 在存储层就绪后注入。就绪前的事件先进有界缓冲，
    避免启动阶段（SERVICE_STARTED / CONFIG_LOADED）的事件丢失。
    """

    def __init__(self, *, buffer_size: int = 500) -> None:
        self._sink: EventSink | None = None
        self._buffer: deque[RadarEvent] = deque(maxlen=buffer_size)
        self._fingerprint: dict[str, str] = {}
        self.errors = ErrorAggregator()
        self._counts: dict[str, int] = {}

    def configure_fingerprint(self, fingerprint: dict[str, str]) -> None:
        self._fingerprint = dict(fingerprint)

    def set_sink(self, sink: EventSink) -> None:
        """注入持久化 sink，并回放缓冲期事件。"""
        self._sink = sink
        while self._buffer:
            try:
                sink(self._buffer.popleft())
            except Exception:
                logger.exception("事件回放失败")

    def counts(self) -> dict[str, int]:
        return dict(self._counts)

    def emit(
        self,
        event_type: EventType,
        *,
        module: str,
        summary: str = "",
        chain_id: str | None = None,
        token_id: int | None = None,
        contract_address: str | None = None,
        symbol: str | None = None,
        old_state: str | None = None,
        new_state: str | None = None,
        snapshot_id: int | None = None,
        alert_id: int | None = None,
        duration_ms: int | None = None,
        severity: Severity | None = None,
        importance: Importance | None = None,
        payload: dict[str, Any] | None = None,
        correlation_id: str | None = None,
    ) -> RadarEvent:
        category, default_severity, default_importance = spec_for(event_type)
        event = RadarEvent(
            event_type=event_type,
            category=category,
            severity=severity or default_severity,
            importance=importance or default_importance,
            module=module,
            occurred_at=now_ms(),
            summary=summary,
            chain_id=chain_id,
            token_id=token_id,
            contract_address=contract_address,
            symbol=symbol,
            correlation_id=correlation_id if correlation_id is not None else get_correlation_id(),
            old_state=old_state,
            new_state=new_state,
            snapshot_id=snapshot_id,
            alert_id=alert_id,
            duration_ms=duration_ms,
            strategy_version=self._fingerprint.get("strategy_version", ""),
            feature_version=self._fingerprint.get("feature_version", ""),
            config_hash=self._fingerprint.get("config_hash", ""),
            payload=redact(payload or {}),
        )

        self._counts[event_type.value] = self._counts.get(event_type.value, 0) + 1

        logger.log(
            event.log_level(),
            "%s | %s",
            event_type.value,
            summary or (symbol or contract_address or ""),
            extra={
                "event_type": event_type.value,
                "severity": event.severity.value,
                "importance": event.importance.value,
                "chain": chain_id,
                "symbol": symbol,
            },
        )

        if self._sink is not None:
            try:
                self._sink(event)
            except Exception:
                logger.exception("事件落库失败 | %s", event_type.value)
        else:
            self._buffer.append(event)

        return event

    def emit_token(
        self,
        event_type: EventType,
        *,
        token: Any,
        module: str,
        summary: str = "",
        **kwargs: Any,
    ) -> RadarEvent:
        """针对某个代币发事件。

        token 用鸭子类型而不是 import TokenView：可观测层被领域层依赖，
        反过来 import 会形成环，而这里真正需要的只是四个身份字段。
        注册表、警报器、追踪器都要发代币事件，抽出来避免每处重复展开。
        """
        return self.emit(
            event_type,
            module=module,
            summary=summary,
            chain_id=getattr(token, "chain_id", None),
            token_id=getattr(token, "token_id", None),
            contract_address=getattr(token, "contract_address", None),
            symbol=getattr(token, "symbol", None),
            **kwargs,
        )


# 进程级单例：所有模块共用同一条事件总线
bus = EventBus()
