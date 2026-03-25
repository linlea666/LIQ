"""应用入口：FastAPI + Socket.IO + Engine"""

from __future__ import annotations

import asyncio
import logging
import sys
from contextlib import asynccontextmanager
from typing import Any, Callable

import socketio
import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.routes import router, set_engine as set_routes_engine
from api.ws import sio, set_engine as set_ws_engine
from config.settings import get_settings
from engine import Engine

from collections import deque


class CORSASGIWrapper:
    """外层 ASGI 中间件：确保 HTTP CORS 头对所有请求生效。

    socketio.ASGIApp 包裹在 FastAPI 外层时，POST 预检 OPTIONS 可能
    未被转发至 FastAPI CORSMiddleware。此中间件在最外层拦截 OPTIONS
    并为普通响应注入 CORS 头，作为兜底保障。
    """

    def __init__(self, app: Any, allowed_origins: list[str]):
        self.app = app
        self.allowed_origins = set(allowed_origins)

    async def __call__(self, scope: dict, receive: Callable, send: Callable):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        origin = ""
        for key, value in scope.get("headers", []):
            if key == b"origin":
                origin = value.decode()
                break

        if origin not in self.allowed_origins:
            await self.app(scope, receive, send)
            return

        if scope["method"] == "OPTIONS":
            headers = [
                (b"access-control-allow-origin", origin.encode()),
                (b"access-control-allow-methods", b"GET, POST, PUT, DELETE, OPTIONS"),
                (b"access-control-allow-headers", b"*"),
                (b"access-control-allow-credentials", b"true"),
                (b"access-control-max-age", b"86400"),
                (b"content-length", b"0"),
            ]
            await send({"type": "http.response.start", "status": 204, "headers": headers})
            await send({"type": "http.response.body", "body": b""})
            return

        cors_headers = [
            (b"access-control-allow-origin", origin.encode()),
            (b"access-control-allow-credentials", b"true"),
        ]

        async def send_with_cors(message: dict):
            if message["type"] == "http.response.start":
                existing = list(message.get("headers", []))
                existing.extend(cors_headers)
                message = {**message, "headers": existing}
            await send(message)

        await self.app(scope, receive, send_with_cors)

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

_socket_app = socketio.ASGIApp(sio, other_asgi_app=app)
socket_app = CORSASGIWrapper(_socket_app, settings.server.cors_origins)


if __name__ == "__main__":
    uvicorn.run(
        "main:socket_app",
        host=settings.server.host,
        port=settings.server.port,
        reload=False,
        log_level="info",
    )
