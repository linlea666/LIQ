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

from api.routes import router, set_engine
from api.ws import sio
from config.settings import get_settings
from engine import Engine

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger("liq")

engine = Engine()


@asynccontextmanager
async def lifespan(app: FastAPI):
    set_engine(engine)
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
