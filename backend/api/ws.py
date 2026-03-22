"""WebSocket / Socket.IO 实时推送服务"""

from __future__ import annotations

import logging

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


async def push_to_coin(coin: str, event: str, data: dict):
    """向订阅了某币种的所有客户端推送数据"""
    room = f"coin:{coin}"
    await sio.emit(event, data, room=room)


async def push_to_all(event: str, data: dict):
    """向所有客户端广播"""
    await sio.emit(event, data)
