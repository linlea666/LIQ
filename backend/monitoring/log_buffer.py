"""内存日志缓冲：供 /api/logs 读取最近日志。

独立于 main.py，避免 API 层通过 `from main import ...` 触发入口模块二次导入
（会重复构造 Engine 并重复加载历史数据）。
"""

from __future__ import annotations

import logging
import traceback
from collections import deque

LOG_FORMAT = "[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s"
LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"

log_buffer: deque[dict] = deque(maxlen=500)


class MemoryHandler(logging.Handler):
    """将日志写入内存 deque，供 /api/logs 端点读取"""

    def emit(self, record: logging.LogRecord):
        msg = record.getMessage()
        if record.exc_info and record.exc_info[1] is not None:
            tb = self.format(record).split("\n", 1)
            if len(tb) > 1:
                msg = f"{msg}\n{tb[1]}"
            else:
                msg = f"{msg}\n{''.join(traceback.format_exception(*record.exc_info))}"
        log_buffer.append({
            "ts": record.created,
            "time": self.format(record),
            "level": record.levelname,
            "name": record.name,
            "msg": msg,
        })


def install_memory_handler() -> MemoryHandler:
    """挂载内存日志 handler；重复调用不会叠加。"""
    root = logging.getLogger()
    for existing in root.handlers:
        if isinstance(existing, MemoryHandler):
            return existing
    handler = MemoryHandler()
    handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=LOG_DATEFMT))
    root.addHandler(handler)
    return handler
