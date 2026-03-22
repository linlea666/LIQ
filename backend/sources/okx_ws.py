"""OKX WebSocket 数据源：实时成交、深度、爆仓"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Callable, Coroutine, Optional

import aiohttp

from config.settings import CoinConfig, get_settings

logger = logging.getLogger(__name__)

Callback = Callable[[str, dict], Coroutine[Any, Any, None]]

HEAVY_CHANNELS = ["books50-l2-tbt"]


class OKXWebSocketSource:
    """OKX 公开 WebSocket 频道管理（支持分层订阅）"""

    def __init__(self):
        cfg = get_settings().okx
        self._ws_url = cfg.ws_url
        self._channels = cfg.ws_channels
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._session: Optional[aiohttp.ClientSession] = None
        self._running = False
        self._callbacks: dict[str, list[Callback]] = {}
        self._reconnect_count = 0
        self._max_reconnect = 50
        self._all_coins: list[CoinConfig] = []
        self._active_coins_ws: set[str] = set()

    def on(self, channel: str, callback: Callback):
        """注册频道回调"""
        self._callbacks.setdefault(channel, []).append(callback)

    async def start(self, coins: list[CoinConfig], active_coins: Optional[set[str]] = None):
        """启动 WebSocket 连接并按分层策略订阅"""
        self._running = True
        self._all_coins = coins
        if active_coins is not None:
            self._active_coins_ws = active_coins.copy()
        else:
            self._active_coins_ws = {c.ccy for c in coins}

        while self._running and self._reconnect_count < self._max_reconnect:
            try:
                await self._connect_and_listen()
            except Exception:
                self._reconnect_count += 1
                wait = min(2 ** self._reconnect_count, 60)
                logger.error(
                    "OKX WS disconnected | reconnect=%d/%d wait=%ds",
                    self._reconnect_count, self._max_reconnect, wait,
                    exc_info=True,
                )
                await asyncio.sleep(wait)

        if self._reconnect_count >= self._max_reconnect:
            logger.error("OKX WS max reconnect reached, giving up")

    async def _connect_and_listen(self):
        self._session = aiohttp.ClientSession()
        try:
            self._ws = await self._session.ws_connect(self._ws_url, heartbeat=25)
            self._reconnect_count = 0
            logger.info("OKX WS connected | url=%s", self._ws_url)

            await self._subscribe_all()

            async for msg in self._ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    await self._handle_message(msg.data)
                elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    logger.warning("OKX WS closed/error | type=%s", msg.type)
                    break
        finally:
            if self._ws and not self._ws.closed:
                await self._ws.close()
            if self._session and not self._session.closed:
                await self._session.close()

    async def _subscribe_all(self):
        """重连时重新订阅：所有币种 tickers + 活跃币种 heavy channels + liquidation"""
        for coin in self._all_coins:
            baseline_args = [{"channel": "tickers", "instId": coin.symbol_okx_swap}]
            await self._ws.send_json({"op": "subscribe", "args": baseline_args})

            if coin.ccy in self._active_coins_ws:
                heavy_args = [{"channel": ch, "instId": coin.symbol_okx_swap} for ch in HEAVY_CHANNELS]
                await self._ws.send_json({"op": "subscribe", "args": heavy_args})

        await self._ws.send_json({"op": "subscribe", "args": [
            {"channel": "liquidation-orders", "instType": "SWAP"}
        ]})
        logger.info(
            "OKX WS subscribed | coins=%s active_heavy=%s",
            [c.ccy for c in self._all_coins], self._active_coins_ws,
        )

    async def subscribe_heavy_channels(self, coin: CoinConfig):
        """动态订阅重量级频道（books50-l2-tbt）"""
        self._active_coins_ws.add(coin.ccy)
        if not self._ws or self._ws.closed:
            return
        args = [{"channel": ch, "instId": coin.symbol_okx_swap} for ch in HEAVY_CHANNELS]
        await self._ws.send_json({"op": "subscribe", "args": args})
        logger.info("OKX WS heavy subscribe | coin=%s", coin.ccy)

    async def unsubscribe_heavy_channels(self, coin: CoinConfig):
        """动态退订重量级频道"""
        self._active_coins_ws.discard(coin.ccy)
        if not self._ws or self._ws.closed:
            return
        args = [{"channel": ch, "instId": coin.symbol_okx_swap} for ch in HEAVY_CHANNELS]
        await self._ws.send_json({"op": "unsubscribe", "args": args})
        logger.info("OKX WS heavy unsubscribe | coin=%s", coin.ccy)

    async def _handle_message(self, raw: str):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return

        if "event" in data:
            logger.debug("OKX WS event: %s", data.get("event"))
            return

        arg = data.get("arg", {})
        channel = arg.get("channel", "")

        if not channel or "data" not in data:
            return

        callbacks = self._callbacks.get(channel, [])
        for cb in callbacks:
            try:
                await cb(channel, data)
            except Exception:
                logger.error("OKX WS callback error | channel=%s", channel, exc_info=True)

    async def stop(self):
        self._running = False
        if self._ws and not self._ws.closed:
            await self._ws.close()
        if self._session and not self._session.closed:
            await self._session.close()
        logger.info("OKX WS stopped")

    @property
    def is_connected(self) -> bool:
        return self._ws is not None and not self._ws.closed


def create_okx_ws_source() -> OKXWebSocketSource:
    return OKXWebSocketSource()
