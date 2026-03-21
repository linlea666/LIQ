"""OKX WebSocket 数据源：实时成交、深度、爆仓"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Callable, Coroutine

import aiohttp

from config.settings import CoinConfig, get_settings

logger = logging.getLogger(__name__)

Callback = Callable[[str, dict], Coroutine[Any, Any, None]]


class OKXWebSocketSource:
    """OKX 公开 WebSocket 频道管理"""

    def __init__(self):
        cfg = get_settings().okx
        self._ws_url = cfg.ws_url
        self._channels = cfg.ws_channels
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._session: aiohttp.ClientSession | None = None
        self._running = False
        self._callbacks: dict[str, list[Callback]] = {}
        self._subscribed_coins: set[str] = set()
        self._reconnect_count = 0
        self._max_reconnect = 50

    def on(self, channel: str, callback: Callback):
        """注册频道回调"""
        self._callbacks.setdefault(channel, []).append(callback)

    async def subscribe_coin(self, coin: CoinConfig):
        """为指定币种订阅所有配置频道"""
        if not self._ws or self._ws.closed:
            logger.warning("OKX WS not connected, cannot subscribe | coin=%s", coin.ccy)
            return

        args = []
        for ch in self._channels:
            if ch == "liquidation-orders":
                args.append({"channel": ch, "instType": "SWAP"})
            else:
                args.append({"channel": ch, "instId": coin.symbol_okx_swap})

        msg = {"op": "subscribe", "args": args}
        await self._ws.send_json(msg)
        self._subscribed_coins.add(coin.ccy)
        logger.info("OKX WS subscribed | coin=%s channels=%s", coin.ccy, self._channels)

    async def unsubscribe_coin(self, coin: CoinConfig):
        """取消指定币种的订阅"""
        if not self._ws or self._ws.closed:
            return

        args = []
        for ch in self._channels:
            if ch == "liquidation-orders":
                args.append({"channel": ch, "instType": "SWAP"})
            else:
                args.append({"channel": ch, "instId": coin.symbol_okx_swap})

        msg = {"op": "unsubscribe", "args": args}
        await self._ws.send_json(msg)
        self._subscribed_coins.discard(coin.ccy)
        logger.info("OKX WS unsubscribed | coin=%s", coin.ccy)

    async def start(self, coins: list[CoinConfig]):
        """启动 WebSocket 连接并订阅所有币种"""
        self._running = True
        while self._running and self._reconnect_count < self._max_reconnect:
            try:
                await self._connect_and_listen(coins)
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

    async def _connect_and_listen(self, coins: list[CoinConfig]):
        self._session = aiohttp.ClientSession()
        try:
            self._ws = await self._session.ws_connect(self._ws_url, heartbeat=25)
            self._reconnect_count = 0
            logger.info("OKX WS connected | url=%s", self._ws_url)

            for coin in coins:
                await self.subscribe_coin(coin)

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
