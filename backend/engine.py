"""
主引擎：调度数据源轮询 + 处理 + 缓存 + 推送。
每个币种运行独立的数据管线，互不干扰。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import Any, Optional

from ai.analyzer import AIAnalyzer, create_analyzer
from ai.snapshot import build_ai_snapshot
from api.ws import push_to_coin
from config.settings import CoinConfig, get_settings
from models.flow import (
    BasisData, CVDData, ETFFlowData, FundingRateData, GlobalLiquidationData,
    LongShortRatioData, MarketIndexData, MultiFundingRateData, OIData, TakerFlowData,
)
from models.levels import LevelAnalysis
from models.liquidation import LiquidationEvent, LiquidationMap, LiquidationStats
from models.market import OrderBookAnalysis, OrderBookLevel, OrderBookSnapshot, TickerData, VolumeProfileData
from models.snapshot import (
    AIAnalysisResult,
    AISnapshot,
    MarketTemperature,
    WaterfallData,
)
from processors.cvd import build_cvd, detect_cvd_price_divergence
from processors.levels import calculate_levels
from processors.liquidation import process_liquidation_map
from processors.market_temp import build_waterfall, calc_market_temperature
from processors.orderbook import analyze_orderbook
from processors.percentile import PercentileTracker
from processors.volume_profile import calc_atr, calc_volume_profile
from sources.bbx import create_bbx_source, create_bbx_extended_source
from sources.binance_rest import create_binance_rest_source
from sources.okx_rest import create_okx_rest_source
from sources.okx_ws import create_okx_ws_source

logger = logging.getLogger(__name__)


class CoinState:
    """单个币种的完整数据状态"""

    def __init__(self, coin: str):
        self.coin = coin
        self.ticker: Optional[TickerData] = None
        self.liq_maps: dict[str, LiquidationMap] = {}
        self.cvd_contract: Optional[CVDData] = None
        self.cvd_spot: Optional[CVDData] = None
        self.oi: Optional[OIData] = None
        self.funding: Optional[FundingRateData] = None
        self.basis: Optional[BasisData] = None
        self.taker_flow: Optional[TakerFlowData] = None
        self.orderbook: Optional[OrderBookAnalysis] = None
        self.vp: Optional[VolumeProfileData] = None
        self.atr: float = 0
        self.temperature: Optional[MarketTemperature] = None
        self.waterfall: Optional[WaterfallData] = None
        self.levels: Optional[LevelAnalysis] = None
        self.liq_stats: Optional[LiquidationStats] = None
        self.candle_prices: list[float] = []
        self.candle_ts: list[int] = []
        self.oi_history: deque = deque(maxlen=720)  # 2小时 @10s
        self.ai_history: deque[AIAnalysisResult] = deque(maxlen=5)
        self.last_ai_ts: float = 0
        self.liq_events: deque[LiquidationEvent] = deque(maxlen=200)
        self.multi_funding: Optional[MultiFundingRateData] = None
        self.ls_ratio: Optional[LongShortRatioData] = None
        self.etf_flow: Optional[ETFFlowData] = None
        self.global_liq: Optional[GlobalLiquidationData] = None
        self.market_index: Optional[MarketIndexData] = None
        # L2 orderbook 维护（books50-l2-tbt 增量更新）
        self._raw_ob_asks: dict[float, list] = {}
        self._raw_ob_bids: dict[float, list] = {}
        self._last_ob_analysis_ts: float = 0


class Engine:
    """主引擎：分层轮询架构，默认币全速，非默认币按需激活"""

    def __init__(self):
        self._settings = get_settings()
        self._bbx = create_bbx_source()
        self._bbx_ext = create_bbx_extended_source()
        self._okx = create_okx_rest_source()
        self._okx_ws = create_okx_ws_source()
        self._binance = create_binance_rest_source()
        self._analyzer = create_analyzer()
        self._percentile = PercentileTracker()
        self._states: dict[str, CoinState] = {}
        self._running = False

        self._default_coin = self._settings.default_coin
        self._active_coins: set[str] = {self._default_coin}
        self._coin_last_active: dict[str, float] = {}
        self._active_tasks: dict[str, list[asyncio.Task]] = {}
        self._inactive_poll_sec = self._settings.engine.inactive_poll_sec
        self._grace_period_sec = self._settings.engine.grace_period_sec

        for ccy in self._settings.supported_coins:
            self._states[ccy] = CoinState(ccy)

    @property
    def ai_available(self) -> bool:
        return self._analyzer.available

    async def start(self):
        """启动分层数据管线"""
        self._running = True
        logger.info(
            "Engine starting | coins=%s default=%s inactive_poll=%ds grace=%ds",
            self._settings.supported_coins, self._default_coin,
            self._inactive_poll_sec, self._grace_period_sec,
        )

        coins = [self._settings.get_coin(c) for c in self._settings.supported_coins]

        self._okx_ws.on("books50-l2-tbt", self._on_orderbook)
        self._okx_ws.on("trades", self._on_trade)
        self._okx_ws.on("liquidation-orders", self._on_liquidation)
        self._okx_ws.on("tickers", self._on_ticker)

        tasks = [
            asyncio.create_task(
                self._okx_ws.start(coins, active_coins={self._default_coin})
            ),
            asyncio.create_task(self._grace_check_loop()),
        ]

        # 全局层（不分币种）
        btc_coin = self._settings.get_coin("BTC")
        tasks.extend([
            asyncio.create_task(self._poll_loop("bbx_market_idx", self._poll_market_index, btc_coin, 60, 0)),
            asyncio.create_task(self._poll_loop("bbx_etf_flow", self._poll_etf_flow, btc_coin, 300, 5)),
            asyncio.create_task(self._poll_loop("bbx_global_liq", self._poll_global_liq, btc_coin, 60, 3)),
        ])

        for idx, ccy in enumerate(self._settings.supported_coins):
            coin = self._settings.get_coin(ccy)
            stagger = idx * 2

            if ccy == self._default_coin:
                # 默认币种：全速轮询
                tasks.extend([
                    asyncio.create_task(self._poll_loop(f"bbx_{ccy}", self._poll_bbx, coin, 30, stagger)),
                    asyncio.create_task(self._poll_loop(f"okx_oi_{ccy}", self._poll_oi, coin, 10, stagger)),
                    asyncio.create_task(self._poll_loop(f"bbx_fr_{ccy}", self._poll_funding_bbx, coin, 60, stagger)),
                    asyncio.create_task(self._poll_loop(f"bbx_ls_{ccy}", self._poll_ls_ratio, coin, 60, stagger + 1)),
                    asyncio.create_task(self._poll_loop(f"okx_cvd_{ccy}", self._poll_cvd, coin, 60, stagger + idx * 5)),
                    asyncio.create_task(self._poll_loop(f"okx_candles_{ccy}", self._poll_candles, coin, 30, stagger)),
                    asyncio.create_task(self._poll_loop(f"okx_basis_{ccy}", self._poll_basis, coin, 10, stagger)),
                    asyncio.create_task(self._poll_loop(f"push_{ccy}", self._push_loop, coin, 5, stagger)),
                ])
            else:
                # 非默认币种：仅保底层（清算地图 + K 线）
                tasks.extend([
                    asyncio.create_task(self._poll_loop(f"bbx_{ccy}", self._poll_bbx, coin, 30, stagger)),
                    asyncio.create_task(self._poll_loop(
                        f"okx_candles_{ccy}", self._poll_candles, coin, self._inactive_poll_sec, stagger,
                    )),
                ])

        await asyncio.gather(*tasks, return_exceptions=True)

    async def stop(self):
        self._running = False
        for ccy, tasks in self._active_tasks.items():
            for t in tasks:
                t.cancel()
        self._active_tasks.clear()
        await self._okx_ws.stop()
        await self._bbx.close()
        await self._bbx_ext.close()
        await self._okx.close()
        await self._binance.close()
        logger.info("Engine stopped")

    # ── 活跃币种管理 ──

    async def activate_coin(self, ccy: str):
        """激活币种：启动活跃层轮询 + WS 深度订阅（幂等）"""
        ccy = ccy.upper()
        if ccy not in self._states:
            return
        self._coin_last_active.pop(ccy, None)
        if ccy == self._default_coin or ccy in self._active_coins:
            return

        self._active_coins.add(ccy)
        coin = self._settings.get_coin(ccy)
        logger.info("Coin activated | ccy=%s", ccy)

        self._active_tasks[ccy] = [
            asyncio.create_task(self._poll_loop(f"push_{ccy}", self._push_loop, coin, 5, 0)),
            asyncio.create_task(self._poll_loop(f"okx_oi_{ccy}", self._poll_oi, coin, 10, 1)),
            asyncio.create_task(self._poll_loop(f"okx_basis_{ccy}", self._poll_basis, coin, 10, 2)),
            asyncio.create_task(self._poll_loop(f"bbx_fr_{ccy}", self._poll_funding_bbx, coin, 60, 3)),
            asyncio.create_task(self._poll_loop(f"bbx_ls_{ccy}", self._poll_ls_ratio, coin, 60, 4)),
            asyncio.create_task(self._poll_loop(f"okx_cvd_{ccy}", self._poll_cvd, coin, 60, 5)),
        ]

        await self._okx_ws.subscribe_heavy_channels(coin)

    def mark_coin_viewer_left(self, ccy: str):
        """标记某币种最后一个观察者离开，启动宽限期"""
        ccy = ccy.upper()
        if ccy == self._default_coin or ccy not in self._active_coins:
            return
        self._coin_last_active[ccy] = time.time()
        logger.info("Coin grace period started | ccy=%s period=%ds", ccy, self._grace_period_sec)

    async def _deactivate_coin(self, ccy: str):
        """停用币种：取消活跃层任务 + WS 退订"""
        if ccy == self._default_coin or ccy not in self._active_coins:
            return
        self._active_coins.discard(ccy)
        self._coin_last_active.pop(ccy, None)

        tasks = self._active_tasks.pop(ccy, [])
        for t in tasks:
            t.cancel()
        logger.info("Coin deactivated | ccy=%s cancelled_tasks=%d", ccy, len(tasks))

        coin = self._settings.get_coin(ccy)
        await self._okx_ws.unsubscribe_heavy_channels(coin)

    async def _grace_check_loop(self):
        """定期检查宽限期，自动停用无观察者的币种"""
        while self._running:
            now = time.time()
            for ccy in list(self._coin_last_active):
                if now - self._coin_last_active[ccy] > self._grace_period_sec:
                    await self._deactivate_coin(ccy)
            await asyncio.sleep(10)

    # ── 轮询循环 ──

    async def _poll_loop(self, name: str, fn, coin: CoinConfig, interval: int, initial_delay: float = 0):
        if initial_delay > 0:
            await asyncio.sleep(initial_delay)
        logger.info("Poll loop started | name=%s coin=%s interval=%ds", name, coin.ccy, interval)
        while self._running:
            try:
                await fn(coin)
            except Exception:
                logger.error("Poll error | name=%s coin=%s", name, coin.ccy, exc_info=True)
            await asyncio.sleep(interval)

    # ── 数据拉取 ──

    async def _poll_bbx(self, coin: CoinConfig):
        result = await self._bbx.fetch_with_retry(coin)
        if not result:
            return
        state = self._states[coin.ccy]
        price = state.ticker.last if state.ticker else 0
        for cycle, liq_map in result.items():
            if price > 0:
                liq_map = process_liquidation_map(
                    liq_map, price,
                    self._settings.processors.levels["min_liq_cluster_usd"],
                )
            state.liq_maps[cycle] = liq_map
        self._recompute(coin.ccy)

    async def _poll_oi(self, coin: CoinConfig):
        snapshot = await self._okx.fetch_oi(coin)
        if not snapshot:
            return
        state = self._states[coin.ccy]
        state.oi_history.append(snapshot)
        self._percentile.push(coin.ccy, "oi", snapshot.oi_usd)

        current_usd = snapshot.oi_usd
        change_1h = 0.0
        change_5m = 0.0
        if len(state.oi_history) >= 2:
            first = state.oi_history[0]
            if first.oi_usd > 0:
                change_1h = (current_usd - first.oi_usd) / first.oi_usd * 100
            recent_5m = list(state.oi_history)[-30:]
            if recent_5m and recent_5m[0].oi_usd > 0:
                change_5m = (current_usd - recent_5m[0].oi_usd) / recent_5m[0].oi_usd * 100

        trend = "stable"
        if change_1h > 3:
            trend = "surging"
        elif change_1h < -3:
            trend = "declining"

        state.oi = OIData(
            coin=coin.ccy, ts=snapshot.ts,
            current_usd=current_usd, change_1h_pct=round(change_1h, 2),
            change_5m_pct=round(change_5m, 2), trend=trend,
        )

        bn_oi = await self._binance.fetch_oi(coin)
        if bn_oi:
            self._percentile.push(coin.ccy, "oi_bn", bn_oi.oi_usd)

    async def _poll_funding_bbx(self, coin: CoinConfig):
        """用 BBX 多交易所资金费率替代 OKX+Binance 独立调用"""
        state = self._states[coin.ccy]
        multi = await self._bbx_ext.fetch_multi_funding(coin.ccy)
        if not multi:
            return
        state.multi_funding = multi

        okx_rate = None
        bn_rate = None
        for ex in multi.exchanges:
            if "okx" in ex.exchange.lower() or "okex" in ex.exchange.lower():
                okx_rate = ex.current
            elif "binance" in ex.exchange.lower():
                bn_rate = ex.current

        state.funding = FundingRateData(
            coin=coin.ccy, ts=multi.ts,
            okx_rate=okx_rate, binance_rate=bn_rate,
            avg_rate=multi.avg_current,
            interpretation=multi.interpretation,
        )
        self._percentile.push(coin.ccy, "funding", multi.avg_current)

    async def _poll_ls_ratio(self, coin: CoinConfig):
        state = self._states[coin.ccy]
        ls = await self._bbx_ext.fetch_ls_ratio(coin.ccy, "1h")
        if ls:
            state.ls_ratio = ls

    async def _poll_etf_flow(self, _coin: CoinConfig):
        etf = await self._bbx_ext.fetch_etf_flow("us-btc")
        if etf:
            for ccy in self._settings.supported_coins:
                self._states[ccy].etf_flow = etf

    async def _poll_global_liq(self, _coin: CoinConfig):
        gliq = await self._bbx_ext.fetch_global_liquidation()
        if gliq:
            for ccy in self._settings.supported_coins:
                self._states[ccy].global_liq = gliq

    async def _poll_market_index(self, _coin: CoinConfig):
        mi = await self._bbx_ext.fetch_market_index()
        if mi:
            for ccy in self._settings.supported_coins:
                self._states[ccy].market_index = mi

    async def _poll_cvd(self, coin: CoinConfig):
        state = self._states[coin.ccy]
        contract_points = await self._okx.fetch_taker_volume(coin, "CONTRACTS")
        await asyncio.sleep(2)
        spot_points = await self._okx.fetch_taker_volume(coin, "SPOT")

        if contract_points:
            cvd = build_cvd(contract_points, "CONTRACTS", coin.ccy)
            if state.candle_prices:
                cvd = detect_cvd_price_divergence(cvd, state.candle_prices, state.candle_ts)
            state.cvd_contract = cvd

        if spot_points:
            state.cvd_spot = build_cvd(spot_points, "SPOT", coin.ccy)

        if contract_points and spot_points:
            c_total_buy = sum(p.buy_vol for p in contract_points[-12:])
            c_total_sell = sum(p.sell_vol for p in contract_points[-12:])
            s_total_buy = sum(p.buy_vol for p in spot_points[-12:])
            s_total_sell = sum(p.sell_vol for p in spot_points[-12:])
            total = c_total_buy + c_total_sell + s_total_buy + s_total_sell
            buy_ratio = (c_total_buy + s_total_buy) / total if total > 0 else 0.5
            state.taker_flow = TakerFlowData(
                coin=coin.ccy, ts=int(time.time()),
                buy_ratio=round(buy_ratio, 3),
                sell_ratio=round(1 - buy_ratio, 3),
                dominant="buyers" if buy_ratio > 0.55 else "sellers" if buy_ratio < 0.45 else "balanced",
                contract_buy_vol=c_total_buy, contract_sell_vol=c_total_sell,
                spot_buy_vol=s_total_buy, spot_sell_vol=s_total_sell,
                spot_contract_divergence=False,
            )

    async def _poll_candles(self, coin: CoinConfig):
        state = self._states[coin.ccy]
        candles = await self._okx.fetch_candles(coin, bar="1H", limit=100)
        if not candles:
            return

        state.candle_prices = [c.close for c in candles]
        state.candle_ts = [c.ts for c in candles]
        state.atr = calc_atr(candles, 14)
        state.vp = calc_volume_profile(candles, num_bins=50, coin=coin.ccy)

    async def _poll_basis(self, coin: CoinConfig):
        state = self._states[coin.ccy]
        mark = await self._okx.fetch_mark_price(coin)
        index = await self._okx.fetch_index_price(coin)
        if mark and index and index > 0:
            basis_pct = (mark - index) / index * 100
            interp = "合约偏贵" if basis_pct > 0.1 else "合约折价" if basis_pct < -0.1 else "中性"
            state.basis = BasisData(
                coin=coin.ccy, ts=int(time.time()),
                mark_price=mark, index_price=index,
                basis_pct=round(basis_pct, 4), interpretation=interp,
            )

    # ── WebSocket 回调 ──

    async def _on_orderbook(self, channel: str, data: dict):
        arg = data.get("arg", {})
        inst_id = arg.get("instId", "")
        coin = self._inst_to_coin(inst_id)
        if not coin:
            return

        state = self._states[coin]
        action = data.get("action", "snapshot")
        items = data.get("data", [])
        if not items:
            return
        book_data = items[0]

        if action == "snapshot":
            state._raw_ob_asks = {float(a[0]): a for a in book_data.get("asks", [])}
            state._raw_ob_bids = {float(b[0]): b for b in book_data.get("bids", [])}
        else:
            for a in book_data.get("asks", []):
                price = float(a[0])
                if float(a[1]) == 0:
                    state._raw_ob_asks.pop(price, None)
                else:
                    state._raw_ob_asks[price] = a
            for b in book_data.get("bids", []):
                price = float(b[0])
                if float(b[1]) == 0:
                    state._raw_ob_bids.pop(price, None)
                else:
                    state._raw_ob_bids[price] = b

        now = time.time()
        if now - state._last_ob_analysis_ts < 2.0:
            return
        state._last_ob_analysis_ts = now

        if not state.ticker or not state._raw_ob_asks or not state._raw_ob_bids:
            return

        try:
            sorted_asks = sorted(state._raw_ob_asks.values(), key=lambda x: float(x[0]))
            sorted_bids = sorted(state._raw_ob_bids.values(), key=lambda x: float(x[0]), reverse=True)

            snapshot_obj = OrderBookSnapshot(
                coin=coin,
                ts=int(book_data.get("ts", 0)),
                asks=[
                    OrderBookLevel(
                        price=float(a[0]), size=float(a[1]),
                        order_count=int(a[3]) if len(a) > 3 else 0,
                    )
                    for a in sorted_asks
                ],
                bids=[
                    OrderBookLevel(
                        price=float(b[0]), size=float(b[1]),
                        order_count=int(b[3]) if len(b) > 3 else 0,
                    )
                    for b in sorted_bids
                ],
                source="okx",
            )

            cfg = self._settings.processors.orderbook
            threshold = cfg.get(f"whale_threshold_{coin.lower()}", 50)
            threshold_usd = cfg.get("whale_threshold_usd", 500000)
            coin_cfg = self._settings.get_coin(coin)
            state.orderbook = analyze_orderbook(
                snapshot_obj, state.ticker.last,
                ct_val=coin_cfg.ct_val,
                wall_threshold_size=threshold,
                wall_threshold_usd=threshold_usd,
            )
        except Exception:
            logger.error("Orderbook analysis failed | coin=%s", coin, exc_info=True)

    async def _on_trade(self, channel: str, data: dict):
        pass

    async def _on_liquidation(self, channel: str, data: dict):
        items = data.get("data", [])
        for item in items:
            inst_id = item.get("instId", "")
            coin = self._inst_to_coin(inst_id)
            if not coin:
                continue
            state = self._states[coin]
            coin_cfg = self._settings.get_coin(coin)
            ct_val = coin_cfg.ct_val

            for detail in item.get("details", []):
                pos_side = detail.get("posSide", "")
                side_str = detail.get("side", "")
                if pos_side == "long" or (not pos_side and side_str == "sell"):
                    liq_side = "long"
                else:
                    liq_side = "short"

                bk_px = float(detail.get("bkPx", 0))
                sz = float(detail.get("sz", 0))
                sz_usd = sz * ct_val * bk_px
                ts_val = int(detail.get("ts", 0))

                state.liq_events.append(LiquidationEvent(
                    coin=coin, ts=ts_val, side=liq_side,
                    price=bk_px, size=sz * ct_val, size_usd=sz_usd,
                    source="okx",
                ))

            self._rebuild_liq_stats(coin)

    def _rebuild_liq_stats(self, ccy: str):
        """从最近30分钟的爆仓事件重建统计"""
        state = self._states[ccy]
        cutoff = int(time.time() * 1000) - 30 * 60 * 1000
        recent = [e for e in state.liq_events if e.ts > cutoff]

        long_usd = sum(e.size_usd for e in recent if e.side == "long")
        short_usd = sum(e.size_usd for e in recent if e.side == "short")
        long_count = sum(1 for e in recent if e.side == "long")
        short_count = sum(1 for e in recent if e.side == "short")

        ratio = long_usd / short_usd if short_usd > 0 else (10.0 if long_usd > 0 else 1.0)

        state.liq_stats = LiquidationStats(
            coin=ccy, ts=int(time.time()),
            period_min=30,
            long_total_usd=long_usd,
            short_total_usd=short_usd,
            long_count=long_count,
            short_count=short_count,
            ratio=round(ratio, 2),
        )

    async def _on_ticker(self, channel: str, data: dict):
        arg = data.get("arg", {})
        inst_id = arg.get("instId", "")
        coin = self._inst_to_coin(inst_id)
        if not coin:
            return
        items = data.get("data", [])
        if not items:
            return
        t = items[0]
        try:
            last = float(t["last"])
            open24 = float(t.get("open24h", last))
            self._states[coin].ticker = TickerData(
                coin=coin, ts=int(t.get("ts", 0)),
                last=last,
                high_24h=float(t.get("high24h", last)),
                low_24h=float(t.get("low24h", last)),
                vol_24h=float(t.get("vol24h", 0)),
                change_24h=round(last - open24, 2),
                change_pct_24h=round((last - open24) / open24 * 100, 2) if open24 else 0,
            )
        except (KeyError, ValueError):
            pass

    # ── 重新计算 ──

    def _recompute(self, ccy: str):
        """重新计算温度、价位、瀑布图"""
        state = self._states[ccy]
        price = state.ticker.last if state.ticker else 0
        if price <= 0:
            return

        liq_map = state.liq_maps.get("24h")

        state.temperature, _factor_scores = calc_market_temperature(
            coin=ccy, funding=state.funding, oi=state.oi,
            cvd_contract=state.cvd_contract, basis=state.basis,
            liq_map=liq_map, liq_stats=state.liq_stats,
            taker_flow=state.taker_flow, atr=state.atr,
            ls_ratio=state.ls_ratio, market_index=state.market_index,
            etf_flow=state.etf_flow, global_liq=state.global_liq,
            orderbook=state.orderbook,
            percentile_tracker=self._percentile,
        )

        if state.temperature:
            state.waterfall = build_waterfall(state.temperature, _factor_scores)

        vwap = state.vp.vwap if state.vp else 0
        liq_map_7d = state.liq_maps.get("7d")
        hist_vol = state.market_index.btc_hist_vol if state.market_index else None
        state.levels = calculate_levels(
            coin=ccy, current_price=price, liq_map=liq_map,
            vp=state.vp, orderbook=state.orderbook,
            atr=state.atr, vwap=vwap,
            liq_map_7d=liq_map_7d, btc_hist_vol=hist_vol,
        )

    # ── 推送循环 ──

    async def _push_loop(self, coin: CoinConfig):
        state = self._states[coin.ccy]
        self._recompute(coin.ccy)

        payload: dict[str, Any] = {"coin": coin.ccy, "ts": int(time.time())}

        if state.ticker:
            payload["ticker"] = state.ticker.model_dump()
        if state.temperature:
            payload["temperature"] = state.temperature.model_dump()
        if state.waterfall:
            payload["waterfall"] = state.waterfall.model_dump()
        if state.levels:
            payload["levels"] = state.levels.model_dump()
        if state.cvd_contract:
            payload["cvd_contract"] = {
                "trend": state.cvd_contract.trend_1h,
                "delta_1h": state.cvd_contract.delta_1h,
                "has_divergence": state.cvd_contract.has_divergence,
                "last_points": [p.model_dump() for p in state.cvd_contract.series[-60:]],
            }
        if state.oi:
            payload["oi"] = state.oi.model_dump()
        if state.funding:
            payload["funding"] = state.funding.model_dump()
        if state.basis:
            payload["basis"] = state.basis.model_dump()
        if state.orderbook:
            payload["orderbook"] = state.orderbook.model_dump()
        if state.multi_funding:
            payload["multi_funding"] = state.multi_funding.model_dump()
        if state.ls_ratio:
            payload["ls_ratio"] = state.ls_ratio.model_dump()
        if state.etf_flow:
            payload["etf_flow"] = state.etf_flow.model_dump()
        if state.global_liq:
            payload["global_liq"] = state.global_liq.model_dump()
        if state.market_index:
            payload["market_index"] = {
                "fear_greed": state.market_index.fear_greed,
                "btc_dominance": state.market_index.btc_dominance,
                "btc_max_pain": state.market_index.btc_max_pain,
                "btc_dvol": state.market_index.btc_dvol,
                "dxy": state.market_index.dxy,
                "nasdaq": state.market_index.nasdaq,
                "sp500": state.market_index.sp500,
                "gold": state.market_index.gold,
            }
        if state.levels and state.levels.sniper_entries:
            payload["sniper_entries"] = [se.model_dump() for se in state.levels.sniper_entries[:4]]
        if state.levels and state.levels.ladder_plans:
            payload["ladder_plans"] = [lp.model_dump() for lp in state.levels.ladder_plans]

        await push_to_coin(coin.ccy, "market_update", payload)

    # ── 公开接口 (供 REST API 使用) ──

    def get_snapshot(self, ccy: str) -> Optional[dict]:
        state = self._states.get(ccy)
        if not state or not state.ticker:
            return None
        self._recompute(ccy)
        result: dict[str, Any] = {"coin": ccy}
        if state.ticker:
            result["ticker"] = state.ticker.model_dump()
        if state.temperature:
            result["temperature"] = state.temperature.model_dump()
        if state.levels:
            result["levels"] = state.levels.model_dump()
        liq = state.liq_maps.get("24h")
        if liq:
            result["liquidation_24h"] = liq.model_dump()
        return result

    def get_temperature(self, ccy: str) -> Optional[MarketTemperature]:
        return self._states.get(ccy, CoinState(ccy)).temperature

    def get_levels(self, ccy: str) -> Optional[LevelAnalysis]:
        return self._states.get(ccy, CoinState(ccy)).levels

    def get_liquidation_map(self, ccy: str, cycle: str) -> Optional[LiquidationMap]:
        return self._states.get(ccy, CoinState(ccy)).liq_maps.get(cycle)

    def get_waterfall(self, ccy: str) -> Optional[WaterfallData]:
        return self._states.get(ccy, CoinState(ccy)).waterfall

    def get_last_ai_ts(self, ccy: str) -> float:
        return self._states.get(ccy, CoinState(ccy)).last_ai_ts

    def get_ai_history(self, ccy: str) -> list[AIAnalysisResult]:
        return list(self._states.get(ccy, CoinState(ccy)).ai_history)

    async def run_ai_analysis(self, ccy: str) -> AIAnalysisResult:
        state = self._states[ccy]
        if not state.ticker:
            raise RuntimeError(f"No price data for {ccy}")

        snapshot = build_ai_snapshot(
            coin=ccy, price=state.ticker.last,
            high_24h=state.ticker.high_24h, low_24h=state.ticker.low_24h,
            liq_map=state.liq_maps.get("24h"), cvd_contract=state.cvd_contract,
            cvd_spot=state.cvd_spot, oi=state.oi, funding=state.funding,
            basis=state.basis, orderbook=state.orderbook, liq_stats=state.liq_stats,
            vp=state.vp, atr=state.atr,
            market_temp_score=state.temperature.score if state.temperature else 50,
            pin_risk_level=state.temperature.pin_risk_level if state.temperature else "low",
            multi_funding=state.multi_funding, ls_ratio=state.ls_ratio,
            etf_flow=state.etf_flow, global_liq=state.global_liq,
            market_index=state.market_index, taker_flow=state.taker_flow,
            levels=state.levels,
        )

        result = await self._analyzer.analyze(snapshot)
        state.ai_history.append(result)
        state.last_ai_ts = time.time()
        return result

    def get_source_health(self) -> list[dict]:
        return [
            self._bbx.health().model_dump(),
            self._bbx_ext.health().model_dump(),
            self._okx.health().model_dump(),
            self._binance.health().model_dump(),
            {
                "name": "okx_ws",
                "status": "connected" if self._okx_ws.is_connected else "disconnected",
                "latency_ms": 0,
                "last_success_ts": 0,
                "error_count": 0,
            },
        ]

    def _inst_to_coin(self, inst_id: str) -> Optional[str]:
        for ccy in self._settings.supported_coins:
            coin_cfg = self._settings.get_coin(ccy)
            if inst_id == coin_cfg.symbol_okx_swap:
                return ccy
        return None
