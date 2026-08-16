"""结构化运维日志（第一层可观测）。

高频技术日志走 stdout + 轮转 JSONL 文件，**不进 SQLite**——
否则每个请求/每次 tick 都写库会让 SQLite 承担大量无意义 I/O。

correlation_id 通过 contextvars 自动附着：一次处理链路
（API 响应 → 解析 → 质量评估 → 特征 → 评分 → 状态迁移 → 警报 → 邮件）
共享同一个 ID，事后可在运行中心把整条链路串起来看。
"""

from __future__ import annotations

import contextvars
import gzip
import json
import logging
import logging.handlers
import os
import shutil
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .redact import redact, scrub_text

_correlation_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "radar_correlation_id", default=""
)

# 日志记录里属于 LogRecord 自带的字段，序列化时跳过
_RESERVED_RECORD_KEYS = frozenset(
    {
        "args", "asctime", "created", "exc_info", "exc_text", "filename",
        "funcName", "levelname", "levelno", "lineno", "module", "msecs",
        "message", "msg", "name", "pathname", "process", "processName",
        "relativeCreated", "stack_info", "thread", "threadName", "taskName",
    }
)


def new_correlation_id(prefix: str = "c") -> str:
    """生成新的链路 ID。前缀用于快速辨识来源（如 scan / burst / api）。"""
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def set_correlation_id(value: str) -> contextvars.Token:
    return _correlation_id.set(value)


def reset_correlation_id(token: contextvars.Token) -> None:
    _correlation_id.reset(token)


def get_correlation_id() -> str:
    return _correlation_id.get()


class CorrelationScope:
    """上下文管理器：进入时设置链路 ID，退出时恢复。

    with CorrelationScope("scan") as cid:
        ...
    """

    def __init__(self, prefix_or_id: str = "c", *, is_full_id: bool = False) -> None:
        self.cid = prefix_or_id if is_full_id else new_correlation_id(prefix_or_id)
        self._token: contextvars.Token | None = None

    def __enter__(self) -> str:
        self._token = set_correlation_id(self.cid)
        return self.cid

    def __exit__(self, *exc: object) -> None:
        if self._token is not None:
            reset_correlation_id(self._token)


class JsonlFormatter(logging.Formatter):
    """一行一个 JSON 对象，便于 jq/研究脚本直接消费。"""

    def __init__(self, tz_offset_hours: int = 8) -> None:
        super().__init__()
        self._tz = timezone(timedelta(hours=tz_offset_hours))

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=self._tz).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname,
            "logger": record.name,
            "msg": scrub_text(record.getMessage()),
        }
        cid = get_correlation_id()
        if cid:
            payload["cid"] = cid

        # 调用方通过 logger.info(..., extra={...}) 传入的附加字段
        extras = {
            k: v
            for k, v in record.__dict__.items()
            if k not in _RESERVED_RECORD_KEYS and not k.startswith("_")
        }
        if extras:
            payload["extra"] = redact(extras)

        if record.exc_info:
            payload["exc"] = scrub_text(self.formatException(record.exc_info))[:4000]

        return json.dumps(payload, ensure_ascii=False, default=str)


class PlainFormatter(logging.Formatter):
    """stdout 用的可读格式（docker logs 直接看）。"""

    def __init__(self, tz_offset_hours: int = 8) -> None:
        super().__init__()
        self._tz = timezone(timedelta(hours=tz_offset_hours))

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created, tz=self._tz).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        cid = get_correlation_id()
        cid_part = f" [{cid[:12]}]" if cid else ""
        msg = scrub_text(record.getMessage())
        line = f"[{ts}] [{record.levelname}] [{record.name}]{cid_part} {msg}"
        if record.exc_info:
            line += "\n" + scrub_text(self.formatException(record.exc_info))
        return line


def _gzip_rotator(source: str, dest: str) -> None:
    """轮转时压缩归档，控制磁盘占用。"""
    try:
        with open(source, "rb") as fin, gzip.open(f"{dest}.gz", "wb") as fout:
            shutil.copyfileobj(fin, fout)
        os.remove(source)
    except OSError:
        # 压缩失败不能影响服务；退回普通改名
        try:
            shutil.move(source, dest)
        except OSError:
            pass


def setup_logging(
    *,
    log_dir: Path,
    level: str = "INFO",
    max_mb: int = 64,
    backup_count: int = 10,
    tz_offset_hours: int = 8,
) -> None:
    """配置根 logger：stdout 可读格式 + 文件 JSONL 格式。"""
    log_dir.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    for handler in list(root.handlers):
        root.removeHandler(handler)

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(PlainFormatter(tz_offset_hours))
    root.addHandler(stream)

    file_handler = logging.handlers.RotatingFileHandler(
        log_dir / "radar.jsonl",
        maxBytes=max_mb * 1024 * 1024,
        backupCount=backup_count,
        encoding="utf-8",
    )
    file_handler.setFormatter(JsonlFormatter(tz_offset_hours))
    file_handler.rotator = _gzip_rotator  # type: ignore[assignment]
    root.addHandler(file_handler)

    # 第三方库降噪：aiohttp 的连接日志对我们没有价值
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)


def now_ms() -> int:
    """统一的毫秒时间戳来源。"""
    return int(time.time() * 1000)
