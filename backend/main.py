"""应用入口：FastAPI + Socket.IO + Engine"""

from __future__ import annotations

import asyncio
import logging
import sys
from contextlib import asynccontextmanager

import socketio
import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.routes import router, set_engine as set_routes_engine
from api.ws import sio, set_engine as set_ws_engine
from config.settings import get_settings
from engine import Engine

from collections import deque

LOG_FORMAT = "[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s"
LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"

log_buffer: deque[dict] = deque(maxlen=500)


class MemoryHandler(logging.Handler):
    """将日志写入内存 deque，供 /api/logs 端点读取"""
    def emit(self, record: logging.LogRecord):
        log_buffer.append({
            "ts": record.created,
            "time": self.format(record),
            "level": record.levelname,
            "name": record.name,
            "msg": record.getMessage(),
        })


logging.basicConfig(
    level=logging.INFO,
    format=LOG_FORMAT,
    datefmt=LOG_DATEFMT,
    stream=sys.stdout,
)

mem_handler = MemoryHandler()
mem_handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=LOG_DATEFMT))
logging.getLogger().addHandler(mem_handler)

logger = logging.getLogger("liq")

engine = Engine()


@asynccontextmanager
async def lifespan(app: FastAPI):
    set_routes_engine(engine)
    set_ws_engine(engine)
    task = asyncio.create_task(engine.start())
    logger.info("LIQ Engine started")
    yield
    engine._running = False
    await engine.stop()
    task.cancel()
    logger.info("LIQ Engine stopped")


settings = get_settings()

app = FastAPI(
    title="LIQ 防猎杀数据大屏",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.server.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)

socket_app = socketio.ASGIApp(sio, other_asgi_app=app)


if __name__ == "__main__":
    uvicorn.run(
        "main:socket_app",
        host=settings.server.host,
        port=settings.server.port,
        reload=False,
        log_level="info",
    )
