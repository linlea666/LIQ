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
# PR-3 · NOFX router 已下线（旧外部 AI 决策接口随数学引擎一并删除）
from api.roll_position import router as roll_router, set_service as set_roll_service
from api.routes_market_action import router as maa_router, set_engine as set_maa_engine
from api.routes_strategic import router as strategic_router, set_engine as set_strategic_engine
from api.routes_scalp import (
    router as scalp_router,
    set_components as set_scalp_components,
    push_signal_created as scalp_push_created,
    push_signal_settled as scalp_push_settled,
)
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

    _log = logging.getLogger("cors_wrapper")

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

        method = scope.get("method", "")
        path = scope.get("path", "")

        if origin not in self.allowed_origins:
            if origin and method == "POST":
                self._log.warning(
                    "CORS reject: origin=%s not in allowed | %s %s",
                    origin, method, path,
                )
            await self.app(scope, receive, send)
            return

        if method == "OPTIONS":
            self._log.info("CORS preflight OK | %s | origin=%s", path, origin)
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
                status = message.get("status", 0)
                headers = [
                    (k, v) for k, v in message.get("headers", [])
                    if k not in (b"access-control-allow-origin",
                                 b"access-control-allow-credentials")
                ]
                headers.extend(cors_headers)
                message = {**message, "headers": headers}
                if method == "POST":
                    self._log.info(
                        "CORS headers injected | %s %s | status=%d | origin=%s",
                        method, path, status, origin,
                    )
            await send(message)

        await self.app(scope, receive, send_with_cors)

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
                import traceback
                msg = f"{msg}\n{''.join(traceback.format_exception(*record.exc_info))}"
        log_buffer.append({
            "ts": record.created,
            "time": self.format(record),
            "level": record.levelname,
            "name": record.name,
            "msg": msg,
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


async def _te_eval_scheduler():
    """P0-B · 后台任务：每小时跑一次 TE 事后打标，生成昨日/今日日报。

    策略：
      - 启动后等 60s 再跑（避免和 engine 启动期抢 CPU）
      - 每次评估 "昨天" + "今天"（今天当日滚动生成，24h 后 pending 信号会被打标）
      - 单次失败不影响循环
    """
    from monitoring.te_eval import evaluate_day, _yesterday_slug
    from monitoring.te_shadow import _BJ_TZ
    from datetime import datetime as _dt

    await asyncio.sleep(60)
    while True:
        try:
            today = _dt.now(_BJ_TZ).strftime("%Y-%m-%d")
            yesterday = _yesterday_slug()
            for d in {yesterday, today}:
                try:
                    stats, path = evaluate_day(d)
                    logger.info(
                        "[TE-Eval] scheduled run date=%s records=%d judged=%d report=%s",
                        d, stats.total_records, stats.overall.judged, path,
                    )
                except Exception:
                    logger.warning("[TE-Eval] scheduled run failed date=%s", d, exc_info=True)
        except Exception:
            logger.exception("[TE-Eval] scheduler outer loop error")
        # 每小时一次
        await asyncio.sleep(3600)


def _build_scalp_signal_engine(main_engine: Engine):
    """构造短线信号引擎全套组件（store + 3 策略 + scorer / veto / calibrator + engine）

    返回 (store, signal_engine, calibrator)；调用方负责注入路由 + start/stop。
    """
    import time as _time
    from processors.scalp_signal.calibrator import Calibrator
    from processors.scalp_signal.signal_engine import SignalEngine
    from processors.scalp_signal.strategy_a_sweep import SweepReclaimStrategy
    from processors.scalp_signal.strategy_b_cvd_div import CVDDivergenceStrategy
    from processors.scalp_signal.strategy_c_range_edge import RangeEdgeFadeStrategy
    from processors.scalp_signal.strategy_registry import StrategyRegistry
    from storage.scalp_signal_store import ScalpSignalStore

    cfg = get_settings().scalp_signal
    store = ScalpSignalStore(data_dir=cfg.data_dir)

    registry = StrategyRegistry()
    registry.register(SweepReclaimStrategy())
    registry.register(CVDDivergenceStrategy())
    registry.register(RangeEdgeFadeStrategy())

    calibrator = Calibrator(store)

    def _state_getter(coin: str):
        return main_engine._states.get(coin)

    def _blackswan_getter() -> tuple[bool, "Any"]:
        try:
            from processors.news_brief import get_current_brief
            b = get_current_brief()
            if b is None:
                return False, None
            is_bs = b.update_trigger == "blackswan"
            age = int(_time.time() - b.updated_at) if b.updated_at else None
            return is_bs, age
        except Exception:
            logger.debug("scalp blackswan_getter failed", exc_info=True)
            return False, None

    signal_engine = SignalEngine(
        store=store,
        registry=registry,
        calibrator=calibrator,
        config_getter=store.load_config,
        state_getter=_state_getter,
        blackswan_getter=_blackswan_getter,
        on_signal_created=scalp_push_created,
        on_signal_settled=scalp_push_settled,
        tick_interval_sec=cfg.tick_interval_sec,
    )
    return store, signal_engine, calibrator


@asynccontextmanager
async def lifespan(app: FastAPI):
    set_routes_engine(engine)
    set_ws_engine(engine)
    # PR-3 · set_nofx_engine 已下线（NOFX router 删除）
    set_roll_service(engine.roll_service)
    set_maa_engine(engine)
    set_strategic_engine(engine)

    # P0-A Shadow Logger：启动后台 writer
    try:
        from monitoring.te_shadow import get_te_shadow_logger
        get_te_shadow_logger().start()
    except Exception:
        logger.warning("[TE-Shadow] startup failed", exc_info=True)

    task = asyncio.create_task(engine.start())

    # P0-B 每小时评估任务
    te_eval_task = asyncio.create_task(_te_eval_scheduler(), name="te_eval_scheduler")

    # 短线信号引擎（独立模块，仅消费 state，不写回）
    scalp_signal_engine = None
    scalp_cfg = get_settings().scalp_signal
    if scalp_cfg.enabled:
        try:
            scalp_store, scalp_signal_engine, scalp_calibrator = _build_scalp_signal_engine(engine)
            set_scalp_components(
                store=scalp_store,
                engine=scalp_signal_engine,
                calibrator=scalp_calibrator,
            )
            await scalp_signal_engine.start()
            logger.info("scalp signal engine started | data_dir=%s tick=%.1fs",
                        scalp_cfg.data_dir, scalp_cfg.tick_interval_sec)
        except Exception:
            logger.exception("scalp signal engine startup failed (continuing without it)")
            scalp_signal_engine = None
    else:
        logger.info("scalp signal engine disabled by config")

    logger.info("LIQ Engine started")
    yield
    engine._running = False
    await engine.stop()
    task.cancel()
    te_eval_task.cancel()
    # 短线信号引擎清理
    if scalp_signal_engine is not None:
        try:
            await scalp_signal_engine.stop()
        except Exception:
            logger.warning("scalp signal engine stop failed", exc_info=True)
    # Shadow Logger 清理
    try:
        from monitoring.te_shadow import get_te_shadow_logger
        await get_te_shadow_logger().stop()
    except Exception:
        pass
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
# PR-3 · nofx_router 已下线（NOFX 接口随数学引擎一并删除）
app.include_router(roll_router)
app.include_router(maa_router)
app.include_router(strategic_router)
app.include_router(scalp_router)

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
