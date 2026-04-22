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

# 滚仓模块订阅：sid → {position_id}，单个 sid 可订阅多个持仓
_sid_roll_positions: dict[str, set[str]] = {}


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

    # 滚仓订阅清理
    _sid_roll_positions.pop(sid, None)

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
                from api._ai_helpers import collect_extras
                interpreter = get_te_interpreter()
                signal_dict = state.trend_exhaustion.model_dump()
                # 指纹计算必须与 POST /ai_interpret 完全一致（同 state 同输入）
                kl_dict = None
                if state.key_level_snapshot_v2:
                    try:
                        kl_dict = state.key_level_snapshot_v2.model_dump()
                    except Exception:
                        kl_dict = None
                extras_dict = collect_extras(state)
                fp = interpreter.compute_fingerprint(
                    coin, signal_dict, kl_dict, extras_dict,
                )
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


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 滚仓模块订阅（独立频道，不影响行情订阅）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _roll_room(position_id: str) -> str:
    return f"roll:{position_id}"


@sio.event
async def subscribe_roll(sid, data):
    """客户端订阅一个持仓的滚仓信号流。

    data = {"position_id": "pos-xxx"}
    同一 sid 可多次调用以订阅多个持仓；已订阅则幂等。
    """
    position_id = str((data or {}).get("position_id") or "").strip()
    if not position_id:
        await sio.emit("error", {"msg": "position_id required"}, to=sid)
        return

    subs = _sid_roll_positions.setdefault(sid, set())
    if position_id in subs:
        await sio.emit("roll_subscribed", {"position_id": position_id}, to=sid)
        return

    await sio.enter_room(sid, _roll_room(position_id))
    subs.add(position_id)
    logger.info("Roll subscribe | sid=%s position=%s subs=%d", sid, position_id, len(subs))
    await sio.emit("roll_subscribed", {"position_id": position_id}, to=sid)


@sio.event
async def unsubscribe_roll(sid, data):
    position_id = str((data or {}).get("position_id") or "").strip()
    if not position_id:
        return
    subs = _sid_roll_positions.get(sid)
    if not subs or position_id not in subs:
        return
    await sio.leave_room(sid, _roll_room(position_id))
    subs.discard(position_id)
    logger.info("Roll unsubscribe | sid=%s position=%s", sid, position_id)
    await sio.emit("roll_unsubscribed", {"position_id": position_id}, to=sid)


async def push_roll_signal(position_id: str, data: dict):
    """向订阅了该持仓的客户端推送滚仓引擎评估结果。"""
    await sio.emit("roll_signal", data, room=_roll_room(position_id))


async def push_roll_event(position_id: str, data: dict):
    """向订阅了该持仓的客户端推送执行/关闭/覆盖等事件（供前端刷新列表）。"""
    await sio.emit("roll_event", data, room=_roll_room(position_id))
