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


@sio.event
async def connect(sid, environ):
    logger.info("Client connected | sid=%s", sid)


@sio.event
async def disconnect(sid):
    logger.info("Client disconnected | sid=%s", sid)


@sio.event
async def subscribe(sid, data):
    """客户端订阅币种频道"""
    coin = data.get("coin", "BTC").upper()
    supported = get_settings().supported_coins
    if coin not in supported:
        await sio.emit("error", {"msg": f"Unsupported coin: {coin}"}, to=sid)
        return

    for c in supported:
        await sio.leave_room(sid, f"coin:{c}")

    await sio.enter_room(sid, f"coin:{coin}")
    logger.info("Client subscribed | sid=%s coin=%s", sid, coin)
    await sio.emit("subscribed", {"coin": coin}, to=sid)


async def push_to_coin(coin: str, event: str, data: dict):
    """向订阅了某币种的所有客户端推送数据"""
    room = f"coin:{coin}"
    await sio.emit(event, data, room=room)


async def push_to_all(event: str, data: dict):
    """向所有客户端广播"""
    await sio.emit(event, data)
