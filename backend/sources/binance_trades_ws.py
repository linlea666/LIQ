"""Binance aggTrade 实时成交流（P3 · 真实大额成交检测）。

背景：
    Coinglass 无原始成交流（CVD/taker 都是 5m 聚合），无法回答"刚才这笔
    千万级市价单是谁在买"。Binance aggTrade 免费、无配额压力，
    合约（fstream）+ 现货（stream.binance.com）× BTC/ETH/SOL 共 6 流。

职责：
    1. 现货：订阅 3 路 aggTrade combined stream（一条 WS 连接）
       合约：REST /fapi/v1/aggTrades fromId 连续轮询（30s/币）
       —— 生产诊断（scripts/diagnose_fstream.py）证实 fstream 对本部署
       网络路径「连接/订阅成功但市场数据零推送」（LIST_SUBSCRIPTIONS 可
       确认订阅在案，REST 正常），本地与服务器同病 → WS 不可用是网络层
       事实，合约侧改 REST 轮询（whale 统计对 30s 延迟不敏感）
    2. 本地阈值过滤（接线激活 config.yaml processors.orderbook 的
       whale_threshold_usd / whale_threshold_{btc,eth,sol} 死配置）：
         whale = 单笔聚合成交 usd ≥ whale_threshold_usd
                 或 数量 ≥ whale_threshold_{coin}（按币种数量阈值）
    3. whale 累计值（usd+qty）与全量成交价统计（high/low/close）每 60s
       冲入 orderflow 小时桶（whale_*_usd/qty · price_high/low/close）
    4. 近 24h whale 明细入有界 deque；单笔 ≥ big_trade_usd（默认 5M）的
       事件供交易大脑事件流拉取（大脑只读，best-effort）；
       deque 同时支撑 /orderflow/{coin}/whale-summary 多周期滚动汇总
    5. 合约 REST 首轮历史重放只喂 deque 不入桶（防重启后 whale/price 双计）

复用决策：扩展复用 sources/binance_futures.py 的 WS 健壮性模式
    （aiohttp heartbeat=30 + 45s 应用级读超时防 TCP 半开 + 指数退避重连）；
    连接管理复用 DataSource.get_session。

资源核算：spot 3 流高峰 ~50-100 msg/s，仅做浮点比较与 dict 累加；
    futures REST 6 req/min（weight 20/req，限额 2400/min，占用 <1%）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import deque
from typing import Any, Optional

import aiohttp

from sources.base import DataSource

logger = logging.getLogger(__name__)

_WS_READ_TIMEOUT_SEC = 45          # 与 binance_futures 同口径（heartbeat 30s + 余量）
_FLUSH_INTERVAL_SEC = 60           # whale 累计冲入小时桶的周期
_WHALE_RETENTION_SEC = 24 * 3600   # 明细 deque 的时间窗
_RECONNECT_MIN_SEC = 1
_RECONNECT_MAX_SEC = 60

_FUTURES_WS_BASE = "wss://fstream.binance.com/stream"
_SPOT_WS_BASE = "wss://stream.binance.com:9443/stream"

# 合约 REST 轮询（fstream WS 推送在本部署网络路径不可用，见模块 docstring）
_FUTURES_REST_URL = "https://fapi.binance.com/fapi/v1/aggTrades"
_REST_POLL_INTERVAL_SEC = 30
_REST_PAGE_LIMIT = 1000
_REST_MAX_PAGES = 5                # 单轮每币最多补 5000 条（极端行情尽力而为）


class BinanceTradesWS(DataSource):
    """aggTrade 双连接客户端（futures + spot combined streams）。"""

    def __init__(
        self,
        coin_symbols: dict[str, str],
        whale_threshold_usd: float = 500_000.0,
        whale_threshold_qty: Optional[dict[str, float]] = None,
        big_trade_usd: float = 5_000_000.0,
        timeout_sec: int = 10,
        checkpoint_path: Optional[str] = None,
    ) -> None:
        super().__init__(name="binance_trades_ws", timeout_sec=timeout_sec, max_retries=0)
        # {ccy: "BTCUSDT"}
        self._coin_symbols = {c.upper(): s.upper() for c, s in coin_symbols.items()}
        self._symbol_to_coin = {s: c for c, s in self._coin_symbols.items()}
        self._whale_usd = float(whale_threshold_usd)
        self._whale_qty = {k.upper(): float(v) for k, v in (whale_threshold_qty or {}).items()}
        self._big_trade_usd = float(big_trade_usd)
        self._checkpoint_path = checkpoint_path or os.path.join(
            os.path.dirname(__file__), "..", "data", "binance_trade_checkpoints.json",
        )
        self._checkpoints = self._load_checkpoints()
        self._checkpoint_dirty = False
        self._sequence_seen: dict[tuple[str, str], int] = {}
        for checkpoint_key, sequence in self._checkpoints.items():
            try:
                market, coin = checkpoint_key.split(":", 1)
            except ValueError:
                continue
            self._sequence_seen[(market, coin)] = sequence
        self._gap_markers: deque[dict] = deque(maxlen=200)

        self._running = False
        self._started_ts = time.time()
        # (coin, market, hour_ts) → [buy_usd, sell_usd, buy_qty, sell_qty]，
        # _flush_loop 定期清空（P4 追加 qty，供 VWAP=usd/qty）
        self._pending: dict[tuple[str, str, int], list[float]] = {}
        # (coin, market, hour_ts) → [high, low, close, close_ts]（P4 全量成交价统计）
        self._pending_price: dict[tuple[str, str, int], list[float]] = {}
        # 全量主动成交 1min 短窗。与 whale 桶完全分开，联合风险引擎消费此处。
        self._flow_buckets: dict[tuple[str, str, int], list[float]] = {}
        # coin → deque[{ts, market, side, price, qty, usd}]（近 24h whale 明细）
        self._recent_whales: dict[str, deque] = {
            c: deque(maxlen=2000) for c in self._coin_symbols
        }
        # coin → deque[大额事件]（单笔 ≥ big_trade_usd，供大脑事件流）
        self._big_events: dict[str, deque] = {
            c: deque(maxlen=100) for c in self._coin_symbols
        }
        # 运维统计
        self._msg_count: dict[str, int] = {"spot": 0, "futures": 0}
        self._whale_count: dict[str, int] = {"spot": 0, "futures": 0}
        self._last_msg_ts: dict[str, float] = {"spot": 0.0, "futures": 0.0}

    # DataSource 抽象要求（本源纯 WS，不参与轮询协议）
    def get_poll_interval(self) -> int:
        return 3600

    async def fetch(self, coin) -> Any:
        return None

    # ── 生命周期 ───────────────────────────────────────────────────────

    async def run(self) -> None:
        """常驻协程：spot WS 流 + futures REST 轮询 + 定期冲桶。engine 负责创建 task。"""
        self._running = True
        try:
            await asyncio.gather(
                self._futures_rest_loop(),
                self._stream_loop("spot"),
                self._flush_loop(),
            )
        except asyncio.CancelledError:
            pass
        finally:
            self._running = False

    def stop(self) -> None:
        self._running = False

    async def flush_now(self) -> None:
        """立即把 pending whale 累计落盘（关停路径调用，防丢最多 60s 增量）。"""
        await self._flush_pending()
        if not self._pending and not self._pending_price and self._checkpoint_dirty:
            self._save_checkpoints()

    # ── 消费接口（whale 明细 / 大额事件 / 统计）────────────────────────

    def recent_whales(self, coin: str, within_sec: int = _WHALE_RETENTION_SEC) -> list[dict]:
        dq = self._recent_whales.get(coin.upper())
        if not dq:
            return []
        cutoff = time.time() - within_sec
        return [w for w in dq if w["ts"] >= cutoff]

    def big_trade_events(self, coin: str, within_sec: int = 1800) -> list[dict]:
        dq = self._big_events.get(coin.upper())
        if not dq:
            return []
        cutoff = time.time() - within_sec
        return [e for e in dq if e["ts"] >= cutoff]

    def data_age_sec(self) -> float:
        """deque/统计自进程启动累积的时长（whale-summary 端点诚实标注用）。"""
        return max(0.0, time.time() - self._started_ts)

    def stats(self) -> dict:
        return {
            "running": self._running,
            "futures_mode": "rest_poll",   # fstream WS 推送不可用（见模块 docstring）
            "data_age_sec": round(self.data_age_sec(), 1),
            "msg_count": dict(self._msg_count),
            "whale_count": dict(self._whale_count),
            "last_msg_age_sec": {
                m: round(time.time() - ts, 1) if ts > 0 else None
                for m, ts in self._last_msg_ts.items()
            },
            "whale_threshold_usd": self._whale_usd,
            "whale_threshold_qty": dict(self._whale_qty),
            "big_trade_usd": self._big_trade_usd,
            "checkpoint_path": self._checkpoint_path,
            "checkpoint_count": len(self._checkpoints),
            "last_gap_marker": self._gap_markers[-1] if self._gap_markers else None,
        }

    def aggressor_flow(
        self, coin: str, market: str, within_sec: int = 300,
    ) -> dict[str, Any]:
        """返回全量主动买卖 quote 短窗；没有完整连续性时显式降级。"""
        now = int(time.time())
        cutoff = now - max(1, int(within_sec))
        buy = sell = 0.0
        first_ts: Optional[int] = None
        last_ts: Optional[int] = None
        for (bucket_coin, bucket_market, minute_ts), values in self._flow_buckets.items():
            if bucket_coin != coin.upper() or bucket_market != market or minute_ts < cutoff - 60:
                continue
            buy += values[0]
            sell += values[1]
            first_ts = minute_ts if first_ts is None else min(first_ts, minute_ts)
            last_ts = minute_ts if last_ts is None else max(last_ts, minute_ts)
        recent_gap = next((
            marker for marker in reversed(self._gap_markers)
            if marker.get("coin") == coin.upper()
            and marker.get("market") == market
            and int(marker.get("observed_at", 0)) >= cutoff
        ), None)
        return {
            "coin": coin.upper(), "market": market, "window_sec": within_sec,
            "aggressor_buy_quote": buy,
            "aggressor_sell_quote": sell,
            "total_quote": buy + sell,
            "as_of": last_ts or 0,
            "first_bucket_ts": first_ts,
            "continuity": "gap" if recent_gap else ("continuous" if last_ts else "unknown"),
            "gap_reason": recent_gap.get("reason") if recent_gap else None,
        }

    def gap_markers(self, within_sec: int = 3600) -> list[dict]:
        cutoff = int(time.time()) - max(1, within_sec)
        return [item for item in self._gap_markers if item["observed_at"] >= cutoff]

    def _load_checkpoints(self) -> dict[str, int]:
        try:
            with open(self._checkpoint_path, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
            return {
                str(key): int(value) for key, value in raw.items()
                if int(value) > 0
            } if isinstance(raw, dict) else {}
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return {}

    def _save_checkpoints(self) -> bool:
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self._checkpoint_path)), exist_ok=True)
            temp_path = self._checkpoint_path + ".tmp"
            with open(temp_path, "w", encoding="utf-8") as handle:
                json.dump(self._checkpoints, handle, sort_keys=True, separators=(",", ":"))
            os.replace(temp_path, self._checkpoint_path)
            self._checkpoint_dirty = False
            return True
        except OSError:
            logger.warning("[trades_ws] checkpoint persist failed", exc_info=True)
            return False

    def _record_gap(
        self, coin: str, market: str, reason: str,
        expected_sequence: Optional[int] = None, observed_sequence: Optional[int] = None,
    ) -> None:
        marker = {
            "coin": coin, "market": market, "reason": reason,
            "expected_sequence": expected_sequence,
            "observed_sequence": observed_sequence,
            "observed_at": int(time.time()),
        }
        self._gap_markers.append(marker)
        logger.warning("[trades_ws] gap marker | %s", marker)

    # ── 内部：WS 流 ────────────────────────────────────────────────────

    def _ws_url(self, market: str) -> str:
        streams = "/".join(
            f"{s.lower()}@aggTrade" for s in sorted(self._coin_symbols.values())
        )
        base = _FUTURES_WS_BASE if market == "futures" else _SPOT_WS_BASE
        return f"{base}?streams={streams}"

    async def _stream_loop(self, market: str) -> None:
        backoff = _RECONNECT_MIN_SEC
        url = self._ws_url(market)
        logger.info("[trades_ws] %s loop started | url=%s", market, url)
        while self._running:
            try:
                session = await self.get_session()
                async with session.ws_connect(url, heartbeat=30) as ws:
                    logger.info("[trades_ws] %s connected", market)
                    backoff = _RECONNECT_MIN_SEC
                    while self._running:
                        try:
                            msg = await asyncio.wait_for(
                                ws.receive(), timeout=_WS_READ_TIMEOUT_SEC,
                            )
                        except asyncio.TimeoutError:
                            logger.warning(
                                "[trades_ws] %s receive timeout (%ds), reconnecting",
                                market, _WS_READ_TIMEOUT_SEC,
                            )
                            break
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            self._handle_message(market, msg.data)
                        elif msg.type in (
                            aiohttp.WSMsgType.CLOSED,
                            aiohttp.WSMsgType.CLOSING,
                            aiohttp.WSMsgType.ERROR,
                        ):
                            break
            except Exception:
                logger.warning("[trades_ws] %s stream failed", market, exc_info=True)
            if not self._running:
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, _RECONNECT_MAX_SEC)

    def _handle_message(self, market: str, raw: str) -> None:
        try:
            payload = json.loads(raw)
            data = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(data, dict) or data.get("e") != "aggTrade":
                return
            symbol = str(data.get("s", "")).upper()
            coin = self._symbol_to_coin.get(symbol)
            if not coin:
                return
            now = time.time()
            self._process_trade(
                market, coin,
                price=float(data.get("p", 0) or 0),
                qty=float(data.get("q", 0) or 0),
                is_maker_buy=bool(data.get("m")),
                trade_ts=int(data.get("T", now * 1000)) // 1000,
                source_sequence=int(data.get("a", 0) or 0),
            )
        except (ValueError, TypeError, KeyError):
            pass

    def _process_trade(
        self, market: str, coin: str, *,
        price: float, qty: float, is_maker_buy: bool, trade_ts: int,
        record_bucket: bool = True,
        source_sequence: Optional[int] = None,
    ) -> None:
        """单笔聚合成交的阈值判定与累计（WS 与 REST 轮询共用）。

        record_bucket=False：只喂 deque/big_events，不入 _pending/_pending_price。
        用于合约 REST 首轮历史重放——这些成交在进程重启前已冲入小时桶，
        再累计会造成 whale/price 列双计（宁可少记 1-3 分钟，不伪造）。
        """
        if price <= 0 or qty <= 0 or trade_ts <= 0:
            return
        self._msg_count[market] += 1
        self._last_msg_ts[market] = time.time()

        sequence_key = (market, coin)
        previous_sequence = self._sequence_seen.get(sequence_key)
        if source_sequence and previous_sequence and source_sequence > previous_sequence + 1:
            self._record_gap(
                coin, market, "source_sequence_gap",
                previous_sequence + 1, source_sequence,
            )
        if source_sequence:
            self._sequence_seen[sequence_key] = max(previous_sequence or 0, source_sequence)
            self._checkpoints[f"{market}:{coin}"] = source_sequence
            if record_bucket:
                self._checkpoint_dirty = True

        hour_ts = trade_ts - trade_ts % 3600
        if record_bucket:
            # P4：全量成交价统计（在鲸鱼阈值过滤之前，覆盖所有观测到的成交）
            st = self._pending_price.setdefault(
                (coin, market, hour_ts), [0.0, 0.0, 0.0, 0.0],
            )
            st[0] = max(st[0], price)
            st[1] = price if st[1] <= 0 else min(st[1], price)
            if trade_ts >= st[3]:
                st[2], st[3] = price, float(trade_ts)

        usd = price * qty
        # m=True → 买方是 maker → taker 主动卖。必须在 whale 阈值之前累计全量流。
        side = "sell" if is_maker_buy else "buy"
        if record_bucket:
            minute_ts = trade_ts - trade_ts % 60
            flow = self._flow_buckets.setdefault(
                (coin, market, minute_ts), [0.0, 0.0],
            )
            flow[0 if side == "buy" else 1] += usd
            cutoff = int(time.time()) - 6 * 3600
            if len(self._flow_buckets) > 3_000:
                for key in [key for key in self._flow_buckets if key[2] < cutoff]:
                    self._flow_buckets.pop(key, None)
        try:
            from storage.raw_event_store import get_raw_event_store
            raw_store = get_raw_event_store()
            if raw_store is not None and record_bucket:
                observed_at = int(time.time())
                raw_store.append({
                    "event_time": trade_ts,
                    "observed_at": observed_at,
                    "decision_time": observed_at,
                    "watermark": trade_ts,
                    "coin": coin,
                    "market": market,
                    "source_id": f"binance_{market}_aggtrade",
                    "source_sequence": source_sequence,
                    "price": price,
                    "base_quantity": qty,
                    "quote_notional": usd,
                    "aggressor_side": side,
                    "event_schema_version": "aggtrade-v1",
                })
        except Exception:
            logger.debug("[trades_ws] raw event append failed", exc_info=True)
        qty_thr = self._whale_qty.get(coin, float("inf"))
        if usd < self._whale_usd and qty < qty_thr:
            return

        self._whale_count[market] += 1

        if record_bucket:
            bucket = self._pending.setdefault(
                (coin, market, hour_ts), [0.0, 0.0, 0.0, 0.0],
            )
            if side == "buy":
                bucket[0] += usd
                bucket[2] += qty
            else:
                bucket[1] += usd
                bucket[3] += qty

        self._recent_whales[coin].append({
            "ts": trade_ts, "market": market, "side": side,
            "price": price, "qty": qty, "usd": usd,
        })
        if usd >= self._big_trade_usd:
            self._big_events[coin].append({
                "ts": trade_ts, "market": market, "side": side,
                "price": price, "usd": usd,
            })
            logger.info(
                "[trades_ws] big trade | %s %s %s $%.1fM @ %.2f",
                coin, market, side, usd / 1e6, price,
            )

    # ── 内部：合约 REST 轮询 ───────────────────────────────────────────

    async def _futures_rest_loop(self) -> None:
        """fromId 连续轮询 /fapi/v1/aggTrades（fstream WS 推送不可用的替代）。

        首轮无 fromId 拿最近 1000 条（约数分钟量，trade_ts 真实所以桶归属
        与 deque 时间过滤都正确）；此后 fromId=last+1 无缝续读。
        """
        last_id: dict[str, int] = {
            symbol: int(self._checkpoints[f"futures:{coin}"])
            for coin, symbol in self._coin_symbols.items()
            if f"futures:{coin}" in self._checkpoints
        }
        logger.info("[trades_ws] futures REST poll started | interval=%ds",
                    _REST_POLL_INTERVAL_SEC)
        while self._running:
            for coin, symbol in self._coin_symbols.items():
                if not self._running:
                    break
                try:
                    await self._poll_futures_symbol(coin, symbol, last_id)
                except Exception:
                    self._record_gap(coin, "futures", "rest_poll_exception")
                    logger.debug("[trades_ws] futures REST poll failed | %s",
                                 symbol, exc_info=True)
            await asyncio.sleep(_REST_POLL_INTERVAL_SEC)

    async def _poll_futures_symbol(
        self, coin: str, symbol: str, last_id: dict[str, int],
    ) -> None:
        session = await self.get_session()
        # 首轮（无 fromId）返回的是重启前的历史成交，可能已在重启前冲入小时桶，
        # 只喂 deque/big_events 回填明细，不入 _pending（防 whale/price 列双计）
        first_poll = symbol not in last_id
        if first_poll:
            self._record_gap(coin, "futures", "startup_without_checkpoint")
        for _ in range(_REST_MAX_PAGES):
            params: dict[str, Any] = {"symbol": symbol, "limit": _REST_PAGE_LIMIT}
            if symbol in last_id:
                params["fromId"] = last_id[symbol] + 1
            async with session.get(
                _FUTURES_REST_URL, params=params,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    self._record_gap(
                        coin, "futures", f"rest_http_{resp.status}",
                        (last_id.get(symbol, 0) + 1) if symbol in last_id else None,
                    )
                    logger.debug("[trades_ws] aggTrades HTTP %d | %s",
                                 resp.status, symbol)
                    return
                rows = await resp.json()
            if not isinstance(rows, list) or not rows:
                return
            if symbol in last_id:
                try:
                    first_id = int(rows[0].get("a", 0) or 0)
                    expected = last_id[symbol] + 1
                    if first_id > expected:
                        self._record_gap(
                            coin, "futures", "rest_backfill_gap", expected, first_id,
                        )
                except (ValueError, TypeError, AttributeError):
                    pass
            for r in rows:
                try:
                    self._process_trade(
                        "futures", coin,
                        price=float(r.get("p", 0) or 0),
                        qty=float(r.get("q", 0) or 0),
                        is_maker_buy=bool(r.get("m")),
                        trade_ts=int(r.get("T", 0) or 0) // 1000,
                        record_bucket=not first_poll,
                        source_sequence=int(r.get("a", 0) or 0),
                    )
                except (ValueError, TypeError):
                    continue
            last_id[symbol] = int(rows[-1].get("a", 0) or 0)
            self._checkpoints[f"futures:{coin}"] = last_id[symbol]
            if first_poll:
                # 首次页已明确标记为不可回放 gap，且未写入特征桶。
                self._save_checkpoints()
            if len(rows) < _REST_PAGE_LIMIT:
                return

    # ── 内部：冲桶 ─────────────────────────────────────────────────────

    async def _flush_loop(self) -> None:
        while self._running:
            await asyncio.sleep(_FLUSH_INTERVAL_SEC)
            await self._flush_pending()

    async def _flush_pending(self) -> None:
        """冲桶：失败时把未落盘的累计合并回 pending，等下轮重试（不丢数据）。"""
        pending, self._pending = self._pending, {}
        pending_price, self._pending_price = self._pending_price, {}
        if not pending and not pending_price:
            return
        loop = asyncio.get_running_loop()
        try:
            from processors.orderflow_stats import get_orderflow_store
            store = get_orderflow_store()
            while pending:
                key, (buy, sell, buy_qty, sell_qty) = next(iter(pending.items()))
                coin, market, hour_ts = key
                await loop.run_in_executor(
                    None, store.add_whale_trades, coin, market, hour_ts,
                    buy, sell, buy_qty, sell_qty,
                )
                pending.pop(key)
            while pending_price:
                key, (high, low, close, _cts) = next(iter(pending_price.items()))
                coin, market, hour_ts = key
                await loop.run_in_executor(
                    None, store.merge_price_stats, coin, market, hour_ts,
                    high, low, close,
                )
                pending_price.pop(key)
            if self._checkpoint_dirty:
                # 只有特征桶成功持久化后才推进 checkpoint；失败会合并回 pending。
                self._save_checkpoints()
        except Exception:
            logger.warning(
                "[trades_ws] flush to store failed | pending_kept=%d price_kept=%d",
                len(pending), len(pending_price), exc_info=True,
            )
            # 未写入的桶合并回去（flush 期间可能有新增，累加/取极值而非覆盖）
            for key, (buy, sell, buy_qty, sell_qty) in pending.items():
                bucket = self._pending.setdefault(key, [0.0, 0.0, 0.0, 0.0])
                bucket[0] += buy
                bucket[1] += sell
                bucket[2] += buy_qty
                bucket[3] += sell_qty
            for key, (high, low, close, cts) in pending_price.items():
                st = self._pending_price.setdefault(key, [0.0, 0.0, 0.0, 0.0])
                st[0] = max(st[0], high)
                if low > 0:
                    st[1] = low if st[1] <= 0 else min(st[1], low)
                # close 取时间更新的一侧（flush 期间新增的成交更晚）
                if close > 0 and cts >= st[3]:
                    st[2], st[3] = close, cts


# ── process 单例 ──────────────────────────────────────────────────────

_instance: Optional[BinanceTradesWS] = None


def init_trades_ws(
    coin_symbols: dict[str, str],
    whale_threshold_usd: float,
    whale_threshold_qty: dict[str, float],
    big_trade_usd: float = 5_000_000.0,
    checkpoint_path: Optional[str] = None,
) -> BinanceTradesWS:
    """engine 启动时调用一次。"""
    global _instance
    if _instance is None:
        _instance = BinanceTradesWS(
            coin_symbols=coin_symbols,
            whale_threshold_usd=whale_threshold_usd,
            whale_threshold_qty=whale_threshold_qty,
            big_trade_usd=big_trade_usd,
            checkpoint_path=checkpoint_path,
        )
    return _instance


def get_trades_ws() -> Optional[BinanceTradesWS]:
    """未初始化（如测试环境 / WS 关闭）时返回 None，调用方 best-effort。"""
    return _instance
