"""WebSocket / Socket.IO 实时推送服务"""

from __future__ import annotations

import logging
import time

import socketio

from config.settings import get_settings

logger = logging.getLogger(__name__)

sio = socketio.AsyncServer(
    async_mode="asgi",
    cors_allowed_origins=get_settings().server.cors_origins,
    logger=False,
    engineio_logger=False,
)

_engine = None
_sid_coin: dict[str, str] = {}
_coin_viewer_count: dict[str, int] = {}


def set_engine(engine):
    """由 main.py 在启动时注入 Engine 实例"""
    global _engine
    _engine = engine


@sio.event
async def connect(sid, environ):
    logger.info("Client connected | sid=%s", sid)


@sio.event
async def disconnect(sid):
    old_coin = _sid_coin.pop(sid, None)
    if old_coin:
        _coin_viewer_count[old_coin] = max(0, _coin_viewer_count.get(old_coin, 0) - 1)
        if _coin_viewer_count.get(old_coin, 0) == 0 and _engine:
            _engine.mark_coin_viewer_left(old_coin)
    logger.info("Client disconnected | sid=%s coin=%s", sid, old_coin)


@sio.event
async def subscribe(sid, data):
    """客户端订阅币种频道"""
    coin = data.get("coin", "BTC").upper()
    supported = get_settings().supported_coins
    if coin not in supported:
        await sio.emit("error", {"msg": f"Unsupported coin: {coin}"}, to=sid)
        return

    old_coin = _sid_coin.get(sid)
    if old_coin and old_coin != coin:
        _coin_viewer_count[old_coin] = max(0, _coin_viewer_count.get(old_coin, 0) - 1)
        if _coin_viewer_count.get(old_coin, 0) == 0 and _engine:
            _engine.mark_coin_viewer_left(old_coin)

    _sid_coin[sid] = coin
    _coin_viewer_count[coin] = _coin_viewer_count.get(coin, 0) + 1

    for c in supported:
        await sio.leave_room(sid, f"coin:{c}")
    await sio.enter_room(sid, f"coin:{coin}")

    if _engine:
        await _engine.activate_coin(coin)

    logger.info("Client subscribed | sid=%s coin=%s viewers=%d", sid, coin, _coin_viewer_count.get(coin, 0))
    await sio.emit("subscribed", {"coin": coin}, to=sid)

    if _engine:
        history = _engine.get_ai_history(coin)
        if history:
            latest = history[-1]
            age = time.time() - latest.ts
            if age < 300:
                await sio.emit("ai_result", latest.model_dump(), to=sid)
                logger.info("AI result replayed on subscribe | sid=%s coin=%s age=%.0fs", sid, coin, age)

    # TE · AI 解读 replay：若当前信号有缓存解读，订阅时推一次（与主 AI 对齐）
    if _engine:
        try:
            state = _engine._states.get(coin)
            if state and state.trend_exhaustion:
                from ai.te_interpreter import get_te_interpreter
                interpreter = get_te_interpreter()
                fp = interpreter.compute_fingerprint(coin, state.trend_exhaustion.model_dump())
                cached = interpreter.peek_cache(fp)
                if cached is not None:
                    await sio.emit("te_ai_result", cached.model_dump(), to=sid)
                    logger.info(
                        "TE-AI result replayed on subscribe | sid=%s coin=%s age=%ds",
                        sid, coin, cached.from_cache_age_sec,
                    )
        except Exception:
            logger.debug("TE-AI replay failed", exc_info=True)


async def push_to_coin(coin: str, event: str, data: dict):
    """向订阅了某币种的所有客户端推送数据"""
    room = f"coin:{coin}"
    await sio.emit(event, data, room=room)


async def push_to_all(event: str, data: dict):
    """向所有客户端广播"""
    await sio.emit(event, data)
