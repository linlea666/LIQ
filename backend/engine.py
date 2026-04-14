"""
主引擎：调度 Coinglass 统一数据源轮询 + 处理 + 缓存 + 推送。
每个币种运行独立的数据管线，互不干扰。
所有数据通过 Coinglass V4 API 获取，纯 REST 轮询架构。
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
    BasisData, CVDData, CVDPoint, CyclePositionData, ETFFlowData,
    FundingRateData, GlobalLiquidationData, LongShortRatioData,
    LongShortRatioExchange, MarketIndexData, MultiFundingRateData,
    ExchangeFundingRate, OIData, OISnapshot, RangeSignalData, TakerFlowData,
    SpotCVDData, NetPositionData, BasisHistoryData,
)
from models.key_level import KeyLevel, KeyLevelSnapshot
from models.levels import LevelAnalysis
from models.liquidation import (
    HeatmapData, LiqHistoryData, LiqMaxPainData,
    LiquidationEvent, LiquidationMap, LiquidationStats,
)
from models.macro import (
    AltcoinSeasonData, BtcDominanceData, BubbleIndexData,
    BullMarketPeakData, CoinbasePremiumData, EconomicCalendarData,
    FearGreedData, NewsData, StablecoinMcapData,
)
from models.market import OrderBookAnalysis, TickerData, VolumeProfileData
from models.onchain import ExchangeBalanceData, TokenUnlockData
from models.options import OptionInfoData, OptionMaxPainData
from models.orderbook_ext import FootprintData, LargeOrderSnapshot
from models.snapshot import (
    AIAnalysisResult, MarketTemperature, WaterfallData,
)
from models.whale import WhaleData
from processors.cvd import detect_cvd_price_divergence
from processors.levels import calculate_levels
from processors.liquidation import detect_liq_sweep, process_liquidation_map
from processors.market_temp import build_waterfall, calc_market_temperature
from processors.orderbook import analyze_orderbook
from processors.percentile import PercentileTracker
from processors.volume_profile import calc_volume_profile
from processors.cycle import calculate_cycle_position
from processors.key_level_tracker import update_key_levels
from processors.range_signal import calculate_range_signal
from sources.coinglass import CoinglassSource, create_coinglass_source

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
        self.oi_history: deque = deque(maxlen=720)
        self.ai_history: deque[AIAnalysisResult] = deque(maxlen=5)
        self.last_ai_ts: float = 0
        self.liq_events: deque[LiquidationEvent] = deque(maxlen=200)
        self.multi_funding: Optional[MultiFundingRateData] = None
        self.ls_ratio: Optional[LongShortRatioData] = None
        self.ls_ratio_top_account: Optional[LongShortRatioData] = None
        self.ls_ratio_top_position: Optional[LongShortRatioData] = None
        self.etf_flow: Optional[ETFFlowData] = None
        self.global_liq: Optional[GlobalLiquidationData] = None
        self.market_index: Optional[MarketIndexData] = None
        self.cycle_position: Optional[CyclePositionData] = None
        self.candles_daily: list = []
        self.candles_weekly: list = []
        self.range_signal: Optional[RangeSignalData] = None
        self.key_levels: list[KeyLevel] = []
        self.key_level_snapshot: Optional[KeyLevelSnapshot] = None
        self._prev_liq_map_24h: Optional[LiquidationMap] = None
        self._prev_price_at_liq_poll: float = 0
        self.liq_sweep_events: deque = deque(maxlen=120)
        # Phase 2: 清算热力图 + 最大痛点 + 爆仓历史
        self.liq_heatmaps: dict[str, HeatmapData] = {}
        self.liq_max_pain: Optional[LiqMaxPainData] = None
        self.liq_history: Optional[LiqHistoryData] = None
        # Phase 3: Coinglass 技术指标缓存
        self.rsi_14: Optional[float] = None
        self.macd_data: Optional[dict] = None
        self.ma60_daily_cg: Optional[float] = None
        self.ma120_daily_cg: Optional[float] = None
        self.ema20_cg: Optional[float] = None
        self.boll_data: Optional[dict] = None
        self.atr_cg: Optional[float] = None
        # Phase 4: 新维度
        self.option_max_pain: Optional[OptionMaxPainData] = None
        self.option_info: Optional[OptionInfoData] = None
        self.large_orders: Optional[LargeOrderSnapshot] = None
        self.footprint: Optional[FootprintData] = None
        self.whale_data: Optional[WhaleData] = None
        self.exchange_balance: Optional[ExchangeBalanceData] = None
        self.token_unlock: Optional[TokenUnlockData] = None
        self.fear_greed: Optional[FearGreedData] = None
        self.btc_dominance: Optional[BtcDominanceData] = None
        self.altcoin_season: Optional[AltcoinSeasonData] = None
        self.coinbase_premium: Optional[CoinbasePremiumData] = None
        self.stablecoin_mcap: Optional[StablecoinMcapData] = None
        self.bubble_index: Optional[BubbleIndexData] = None
        self.bull_peak: Optional[BullMarketPeakData] = None
        self.economic_calendar: Optional[EconomicCalendarData] = None
        self.news: Optional[NewsData] = None
        self.net_position: Optional[NetPositionData] = None
        self.basis_history: Optional[BasisHistoryData] = None
        self.spot_cvd: Optional[SpotCVDData] = None


class Engine:
    """主引擎：Coinglass 统一数据源，REST 轮询架构"""

    def __init__(self):
        self._settings = get_settings()
        self._cg: CoinglassSource = create_coinglass_source()
        self._analyzer = create_analyzer()
        self._percentile = PercentileTracker()
        self._states: dict[str, CoinState] = {}
        self._running = False
        self._ai_running: set[str] = set()

        self._default_coin = self._settings.default_coin
        self._active_coins: set[str] = {self._default_coin}
        self._coin_last_active: dict[str, float] = {}
        self._active_tasks: dict[str, list[asyncio.Task]] = {}
        self._inactive_poll_sec = self._settings.engine.inactive_poll_sec
        self._grace_period_sec = self._settings.engine.grace_period_sec

        self._poll_cfg = self._settings.coinglass.poll_intervals

        for ccy in self._settings.supported_coins:
            self._states[ccy] = CoinState(ccy)

    @property
    def ai_available(self) -> bool:
        return self._analyzer.available

    async def start(self):
        """启动 Coinglass REST 轮询数据管线"""
        self._running = True
        logger.info(
            "Engine starting (Coinglass) | coins=%s default=%s",
            self._settings.supported_coins, self._default_coin,
        )

        tasks = [
            asyncio.create_task(self._grace_check_loop()),
        ]

        # 全局层（不分币种，一次拿全部）
        btc_coin = self._settings.get_coin("BTC")
        tasks.extend([
            asyncio.create_task(self._poll_loop(
                "cg_ticker", self._poll_ticker_all, btc_coin,
                self._poll_cfg.get("ticker", 10), 0,
            )),
            asyncio.create_task(self._poll_loop(
                "cg_fr", self._poll_funding_all, btc_coin,
                self._poll_cfg.get("funding_rate", 60), 3,
            )),
            asyncio.create_task(self._poll_loop(
                "cg_etf", self._poll_etf_flow, btc_coin,
                self._poll_cfg.get("etf", 600), 10,
            )),
            asyncio.create_task(self._poll_loop(
                "cg_global_liq", self._poll_global_liq, btc_coin,
                self._poll_cfg.get("liquidation_map", 60), 5,
            )),
            asyncio.create_task(self._poll_loop(
                "cg_onchain", self._poll_onchain_cycle, btc_coin,
                self._poll_cfg.get("onchain", 1800), 15,
            )),
            asyncio.create_task(self._poll_loop(
                "cg_macro", self._poll_macro_index, btc_coin,
                self._poll_cfg.get("macro_index", 600), 20,
            )),
            asyncio.create_task(self._poll_loop(
                "cg_whale", self._poll_whale_data, btc_coin,
                self._poll_cfg.get("whale", 300), 25,
            )),
            asyncio.create_task(self._poll_loop(
                "cg_options", self._poll_options, btc_coin,
                self._poll_cfg.get("options", 300), 30,
            )),
            asyncio.create_task(self._poll_loop(
                "cg_news", self._poll_news, btc_coin,
                self._poll_cfg.get("news", 600), 35,
            )),
            asyncio.create_task(self._poll_loop(
                "cg_liq_max_pain", self._poll_liq_max_pain, btc_coin,
                self._poll_cfg.get("liquidation_map", 60), 8,
            )),
        ])

        for idx, ccy in enumerate(self._settings.supported_coins):
            coin = self._settings.get_coin(ccy)
            stagger = idx * 3

            if ccy == self._default_coin:
                tasks.extend(self._create_full_poll_tasks(coin, stagger))
            else:
                tasks.extend([
                    asyncio.create_task(self._poll_loop(
                        f"cg_liq_{ccy}", self._poll_liquidation_map, coin,
                        self._poll_cfg.get("liquidation_map", 60), stagger,
                    )),
                    asyncio.create_task(self._poll_loop(
                        f"cg_push_{ccy}", self._push_loop, coin, 10, stagger + 1,
                    )),
                ])

        await asyncio.gather(*tasks, return_exceptions=True)

    def _create_full_poll_tasks(self, coin: CoinConfig, stagger: int) -> list[asyncio.Task]:
        """为活跃币种创建完整轮询任务集"""
        ccy = coin.ccy
        return [
            asyncio.create_task(self._poll_loop(
                f"cg_oi_{ccy}", self._poll_oi, coin,
                self._poll_cfg.get("oi", 30), stagger,
            )),
            asyncio.create_task(self._poll_loop(
                f"cg_liq_{ccy}", self._poll_liquidation_map, coin,
                self._poll_cfg.get("liquidation_map", 60), stagger + 1,
            )),
            asyncio.create_task(self._poll_loop(
                f"cg_heatmap_{ccy}", self._poll_liq_heatmap, coin,
                self._poll_cfg.get("liquidation_heatmap", 300), stagger + 2,
            )),
            asyncio.create_task(self._poll_loop(
                f"cg_ls_{ccy}", self._poll_ls_ratio, coin,
                self._poll_cfg.get("long_short", 60), stagger + 3,
            )),
            asyncio.create_task(self._poll_loop(
                f"cg_cvd_{ccy}", self._poll_cvd, coin,
                self._poll_cfg.get("cvd", 60), stagger + 4,
            )),
            asyncio.create_task(self._poll_loop(
                f"cg_taker_{ccy}", self._poll_taker_volume, coin,
                self._poll_cfg.get("taker_volume", 60), stagger + 5,
            )),
            asyncio.create_task(self._poll_loop(
                f"cg_indicators_{ccy}", self._poll_indicators, coin,
                self._poll_cfg.get("indicators", 300), stagger + 6,
            )),
            asyncio.create_task(self._poll_loop(
                f"cg_basis_{ccy}", self._poll_basis, coin,
                self._poll_cfg.get("oi", 30), stagger + 7,
            )),
            asyncio.create_task(self._poll_loop(
                f"cg_large_orders_{ccy}", self._poll_large_orders, coin,
                self._poll_cfg.get("large_orders", 60), stagger + 8,
            )),
            asyncio.create_task(self._poll_loop(
                f"cg_liq_history_{ccy}", self._poll_liq_history, coin,
                self._poll_cfg.get("liquidation_map", 60), stagger + 9,
            )),
            asyncio.create_task(self._poll_loop(
                f"cg_candles_1h_{ccy}", self._poll_candles_1h, coin,
                30, stagger + 10,
            )),
            asyncio.create_task(self._poll_loop(
                f"cg_candles_1d_{ccy}", self._poll_candles_daily, coin,
                self._poll_cfg.get("indicators", 300), stagger + 11,
            )),
            asyncio.create_task(self._poll_loop(
                f"cg_candles_1w_{ccy}", self._poll_candles_weekly, coin,
                900, stagger + 12,
            )),
            asyncio.create_task(self._poll_loop(
                f"cg_push_{ccy}", self._push_loop, coin, 5, stagger,
            )),
        ]

    async def stop(self):
        self._running = False
        for ccy, tasks in self._active_tasks.items():
            for t in tasks:
                t.cancel()
        self._active_tasks.clear()
        await self._cg.close()
        logger.info("Engine stopped")

    # ── 活跃币种管理 ──

    async def activate_coin(self, ccy: str):
        """激活币种：启动活跃层轮询"""
        ccy = ccy.upper()
        if ccy not in self._states:
            return
        self._coin_last_active.pop(ccy, None)
        if ccy == self._default_coin or ccy in self._active_coins:
            return

        self._active_coins.add(ccy)
        coin = self._settings.get_coin(ccy)
        logger.info("Coin activated | ccy=%s", ccy)

        self._active_tasks[ccy] = self._create_full_poll_tasks(coin, 0)

    def mark_coin_viewer_left(self, ccy: str):
        ccy = ccy.upper()
        if ccy == self._default_coin or ccy not in self._active_coins:
            return
        self._coin_last_active[ccy] = time.time()
        logger.info("Coin grace period started | ccy=%s period=%ds", ccy, self._grace_period_sec)

    async def _deactivate_coin(self, ccy: str):
        if ccy == self._default_coin or ccy not in self._active_coins:
            return
        self._active_coins.discard(ccy)
        self._coin_last_active.pop(ccy, None)

        tasks = self._active_tasks.pop(ccy, [])
        for t in tasks:
            t.cancel()
        logger.info("Coin deactivated | ccy=%s cancelled_tasks=%d", ccy, len(tasks))

    async def _grace_check_loop(self):
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

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Phase 1: 行情 + OI + 资金费率
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    async def _poll_ticker_all(self, _coin: CoinConfig):
        """coins-markets 一次获取全币种行情"""
        data = await self._cg.fetch_coins_markets()
        if not data:
            return

        symbol_to_ccy = {
            self._settings.get_coin(c).symbol_cg: c
            for c in self._settings.supported_coins
        }

        for item in data:
            symbol = item.get("symbol", "")
            ccy = symbol_to_ccy.get(symbol)
            if not ccy:
                continue

            state = self._states[ccy]
            try:
                price = float(item.get("price", 0))
                if price <= 0:
                    continue
                open_24h = float(item.get("open24h", price))
                state.ticker = TickerData(
                    coin=ccy,
                    ts=int(time.time() * 1000),
                    last=price,
                    high_24h=float(item.get("high24h", price)),
                    low_24h=float(item.get("low24h", price)),
                    vol_24h=float(item.get("volUsd24h", 0)),
                    change_24h=round(price - open_24h, 2),
                    change_pct_24h=round(
                        (price - open_24h) / open_24h * 100, 2
                    ) if open_24h > 0 else 0,
                )

                oi_usd = float(item.get("openInterest", 0))
                if oi_usd > 0:
                    snapshot = OISnapshot(
                        coin=ccy, ts=int(time.time()),
                        oi=oi_usd, oi_usd=oi_usd,
                    )
                    state.oi_history.append(snapshot)
                    self._percentile.push(ccy, "oi", oi_usd)
            except (ValueError, KeyError):
                continue

    async def _poll_oi(self, coin: CoinConfig):
        """获取 OI 聚合历史"""
        data = await self._cg.fetch_oi_aggregated_history(
            coin.symbol_cg, interval="5m", limit=50,
        )
        if not data:
            return

        state = self._states[coin.ccy]
        for item in data:
            try:
                oi_usd = float(item.get("openInterest", item.get("value", 0)))
                ts = int(item.get("time", item.get("t", 0)))
                if oi_usd > 0:
                    snapshot = OISnapshot(
                        coin=coin.ccy, ts=ts, oi=oi_usd, oi_usd=oi_usd,
                    )
                    state.oi_history.append(snapshot)
            except (ValueError, KeyError):
                continue

        if state.oi_history:
            current = state.oi_history[-1]
            first = state.oi_history[0]
            change_1h = 0.0
            if first.oi_usd > 0:
                change_1h = (current.oi_usd - first.oi_usd) / first.oi_usd * 100

            recent_5m = list(state.oi_history)[-30:]
            change_5m = 0.0
            if recent_5m and recent_5m[0].oi_usd > 0:
                change_5m = (current.oi_usd - recent_5m[0].oi_usd) / recent_5m[0].oi_usd * 100

            trend = "stable"
            if change_1h > 3:
                trend = "surging"
            elif change_1h < -3:
                trend = "declining"

            state.oi = OIData(
                coin=coin.ccy, ts=current.ts,
                current_usd=current.oi_usd,
                change_1h_pct=round(change_1h, 2),
                change_5m_pct=round(change_5m, 2),
                trend=trend,
            )

    async def _poll_funding_all(self, _coin: CoinConfig):
        """获取全币种多交易所资金费率"""
        data = await self._cg.fetch_fr_exchange_list()
        if not data:
            return

        symbol_to_ccy = {
            self._settings.get_coin(c).symbol_cg: c
            for c in self._settings.supported_coins
        }

        for item in data:
            symbol = item.get("symbol", "")
            ccy = symbol_to_ccy.get(symbol)
            if not ccy:
                continue

            state = self._states[ccy]
            exchanges = []
            avg_current = 0.0
            count = 0
            okx_rate = None
            bn_rate = None

            margin_list = item.get("uMarginList", [])
            for ex_item in margin_list:
                ex_name = ex_item.get("exchangeName", "")
                rate = ex_item.get("rate")
                if rate is not None:
                    rate = float(rate)
                    exchanges.append(ExchangeFundingRate(
                        exchange=ex_name, current=rate,
                    ))
                    avg_current += rate
                    count += 1
                    if "okx" in ex_name.lower() or "okex" in ex_name.lower():
                        okx_rate = rate
                    elif "binance" in ex_name.lower():
                        bn_rate = rate

            if count > 0:
                avg_current /= count

            interp = "中性"
            if avg_current > 0.01:
                interp = "多头拥挤"
            elif avg_current < -0.01:
                interp = "空头拥挤"

            state.multi_funding = MultiFundingRateData(
                coin=ccy, ts=int(time.time()),
                exchanges=exchanges,
                avg_current=round(avg_current, 6),
                interpretation=interp,
            )

            state.funding = FundingRateData(
                coin=ccy, ts=int(time.time()),
                okx_rate=okx_rate, binance_rate=bn_rate,
                avg_rate=round(avg_current, 6),
                interpretation=interp,
            )
            self._percentile.push(ccy, "funding", avg_current)

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Phase 2: 清算 + 多空比 + CVD + Taker
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    async def _poll_liquidation_map(self, coin: CoinConfig):
        """获取多周期清算地图"""
        state = self._states[coin.ccy]
        price = state.ticker.last if state.ticker else 0

        for cycle in ("1d", "7d", "30d"):
            data = await self._cg.fetch_liquidation_aggregated_map(
                coin.symbol_cg, range_=cycle,
            )
            if not data:
                continue

            liq_map = self._parse_liquidation_map(data, coin.ccy, cycle)
            if liq_map and price > 0:
                liq_map = process_liquidation_map(
                    liq_map, price,
                    self._settings.processors.levels["min_liq_cluster_usd"],
                )
                if cycle == "1d":
                    self._detect_and_store_sweep(state, liq_map, price)
            if liq_map:
                state.liq_maps[cycle] = liq_map

        self._recompute(coin.ccy)

    def _parse_liquidation_map(self, data: dict, coin: str, cycle: str) -> Optional[LiquidationMap]:
        """解析 Coinglass 清算地图数据"""
        from models.liquidation import LiqBand, LiqLeverageGroup
        try:
            leverage_groups = []
            for lev_key in ("10", "25", "50", "100"):
                lev_data = data.get(f"x{lev_key}", data.get(lev_key, {}))
                if not lev_data:
                    continue
                short_bands = []
                long_bands = []
                for item in lev_data.get("shortList", lev_data.get("asks", [])):
                    short_bands.append(LiqBand(
                        price_from=float(item.get("price", item.get("p", 0))),
                        price_to=float(item.get("price", item.get("p", 0))),
                        turnover_usd=float(item.get("volUsd", item.get("v", 0))),
                    ))
                for item in lev_data.get("longList", lev_data.get("bids", [])):
                    long_bands.append(LiqBand(
                        price_from=float(item.get("price", item.get("p", 0))),
                        price_to=float(item.get("price", item.get("p", 0))),
                        turnover_usd=float(item.get("volUsd", item.get("v", 0))),
                    ))
                leverage_groups.append(LiqLeverageGroup(
                    leverage=lev_key,
                    short_bands=short_bands,
                    long_bands=long_bands,
                    short_total_usd=sum(b.turnover_usd for b in short_bands),
                    long_total_usd=sum(b.turnover_usd for b in long_bands),
                ))

            if not leverage_groups:
                return None

            return LiquidationMap(
                coin=coin, ts=int(time.time()), cycle=cycle,
                leverage_groups=leverage_groups,
            )
        except Exception:
            logger.error("Parse liquidation map failed | coin=%s cycle=%s", coin, cycle, exc_info=True)
            return None

    def _detect_and_store_sweep(self, state: CoinState, new_map: LiquidationMap, price: float):
        prev = state._prev_liq_map_24h
        prev_price = state._prev_price_at_liq_poll
        state._prev_liq_map_24h = new_map
        state._prev_price_at_liq_poll = price
        if not prev or prev_price <= 0:
            return
        events = detect_liq_sweep(prev, new_map, prev_price, price)
        if events:
            now = int(time.time())
            for evt in events:
                evt["ts"] = now
                state.liq_sweep_events.append(evt)

    async def _poll_liq_heatmap(self, coin: CoinConfig):
        """获取清算热力图（model1）"""
        state = self._states[coin.ccy]
        for range_ in ("24h", "7d"):
            data = await self._cg.fetch_liquidation_aggregated_heatmap(
                coin.symbol_cg, range_=range_, model=1,
            )
            if data:
                state.liq_heatmaps[f"m1_{range_}"] = HeatmapData(
                    coin=coin.ccy, ts=int(time.time()),
                    model=1, range=range_, data=[],
                )

    async def _poll_liq_max_pain(self, _coin: CoinConfig):
        """获取清算最大痛点"""
        for range_ in ("24h", "7d"):
            data = await self._cg.fetch_liquidation_max_pain(range_=range_)
            if not data:
                continue
            from models.liquidation import LiqMaxPainItem
            items = []
            for item in data:
                try:
                    items.append(LiqMaxPainItem(
                        symbol=item.get("symbol", ""),
                        price=float(item.get("price", 0)),
                        long_liq_usd=float(item.get("longLiqUsd", 0)),
                        short_liq_usd=float(item.get("shortLiqUsd", 0)),
                    ))
                except (ValueError, KeyError):
                    continue

            for ccy in self._settings.supported_coins:
                state = self._states[ccy]
                state.liq_max_pain = LiqMaxPainData(
                    ts=int(time.time()), range=range_, items=items,
                )

    async def _poll_liq_history(self, coin: CoinConfig):
        """获取聚合爆仓历史"""
        data = await self._cg.fetch_liquidation_aggregated_history(
            coin.symbol_cg, interval="1h", limit=24,
        )
        if not data:
            return

        state = self._states[coin.ccy]
        from models.liquidation import LiqHistoryPoint
        points = []
        total_long = 0.0
        total_short = 0.0
        for item in data:
            try:
                long_usd = float(item.get("longVolUsd", item.get("longLiqUsd", 0)))
                short_usd = float(item.get("shortVolUsd", item.get("shortLiqUsd", 0)))
                points.append(LiqHistoryPoint(
                    ts=int(item.get("time", item.get("t", 0))),
                    long_usd=long_usd, short_usd=short_usd,
                ))
                total_long += long_usd
                total_short += short_usd
            except (ValueError, KeyError):
                continue

        state.liq_history = LiqHistoryData(
            coin=coin.ccy, interval="1h", data=points,
        )

        ratio = total_long / total_short if total_short > 0 else (10.0 if total_long > 0 else 1.0)
        state.liq_stats = LiquidationStats(
            coin=coin.ccy, ts=int(time.time()),
            period_min=1440,
            long_total_usd=total_long, short_total_usd=total_short,
            ratio=round(ratio, 2),
        )

    async def _poll_ls_ratio(self, coin: CoinConfig):
        """获取多空比（全局 + 大户账户 + 大户持仓）"""
        state = self._states[coin.ccy]
        exchange = coin.exchange_primary

        global_data = await self._cg.fetch_global_ls_ratio_history(
            exchange, coin.symbol_cg_pair, interval="1h", limit=1,
        )
        if global_data and len(global_data) > 0:
            item = global_data[-1]
            long_pct = float(item.get("longAccount", item.get("longRatio", 50)))
            short_pct = float(item.get("shortAccount", item.get("shortRatio", 50)))
            ratio = long_pct / short_pct if short_pct > 0 else 1.0
            state.ls_ratio = LongShortRatioData(
                coin=coin.ccy, ts=int(time.time()),
                dimension="global",
                exchanges=[LongShortRatioExchange(
                    exchange=exchange, long_pct=long_pct, short_pct=short_pct, ratio=ratio,
                )],
                avg_ratio=ratio,
            )

        top_acct_data = await self._cg.fetch_top_ls_account_ratio_history(
            exchange, coin.symbol_cg_pair, interval="1h", limit=1,
        )
        if top_acct_data and len(top_acct_data) > 0:
            item = top_acct_data[-1]
            long_pct = float(item.get("longAccount", item.get("longRatio", 50)))
            short_pct = float(item.get("shortAccount", item.get("shortRatio", 50)))
            ratio = long_pct / short_pct if short_pct > 0 else 1.0
            state.ls_ratio_top_account = LongShortRatioData(
                coin=coin.ccy, ts=int(time.time()),
                dimension="top_account",
                exchanges=[LongShortRatioExchange(
                    exchange=exchange, long_pct=long_pct, short_pct=short_pct, ratio=ratio,
                )],
                avg_ratio=ratio,
            )

        top_pos_data = await self._cg.fetch_top_ls_position_ratio_history(
            exchange, coin.symbol_cg_pair, interval="1h", limit=1,
        )
        if top_pos_data and len(top_pos_data) > 0:
            item = top_pos_data[-1]
            long_pct = float(item.get("longPosition", item.get("longRatio", 50)))
            short_pct = float(item.get("shortPosition", item.get("shortRatio", 50)))
            ratio = long_pct / short_pct if short_pct > 0 else 1.0
            state.ls_ratio_top_position = LongShortRatioData(
                coin=coin.ccy, ts=int(time.time()),
                dimension="top_position",
                exchanges=[LongShortRatioExchange(
                    exchange=exchange, long_pct=long_pct, short_pct=short_pct, ratio=ratio,
                )],
                avg_ratio=ratio,
            )

    async def _poll_cvd(self, coin: CoinConfig):
        """从 Coinglass 直接获取 CVD"""
        state = self._states[coin.ccy]

        contract_data = await self._cg.fetch_aggregated_cvd_history(
            coin.symbol_cg, interval="5m", limit=100,
        )
        if contract_data:
            points = []
            for item in contract_data:
                try:
                    ts = int(item.get("time", item.get("t", 0)))
                    buy = float(item.get("buyVolUsd", item.get("buyVol", 0)))
                    sell = float(item.get("sellVolUsd", item.get("sellVol", 0)))
                    cvd_val = float(item.get("cvd", buy - sell))
                    points.append(CVDPoint(
                        ts=ts, buy_vol=buy, sell_vol=sell,
                        delta=buy - sell, cvd=cvd_val,
                    ))
                except (ValueError, KeyError):
                    continue

            if points:
                trend, delta_1h = self._calc_cvd_trend(points)
                state.cvd_contract = CVDData(
                    coin=coin.ccy, inst_type="CONTRACTS",
                    series=points, trend_1h=trend, delta_1h=delta_1h,
                )
                if state.candle_prices:
                    state.cvd_contract = detect_cvd_price_divergence(
                        state.cvd_contract, state.candle_prices, state.candle_ts,
                    )

        spot_data = await self._cg.fetch_spot_aggregated_cvd(
            coin.symbol_cg, interval="5m", limit=100,
        )
        if spot_data:
            points = []
            for item in spot_data:
                try:
                    ts = int(item.get("time", item.get("t", 0)))
                    buy = float(item.get("buyVolUsd", item.get("buyVol", 0)))
                    sell = float(item.get("sellVolUsd", item.get("sellVol", 0)))
                    cvd_val = float(item.get("cvd", buy - sell))
                    points.append(CVDPoint(
                        ts=ts, buy_vol=buy, sell_vol=sell,
                        delta=buy - sell, cvd=cvd_val,
                    ))
                except (ValueError, KeyError):
                    continue
            if points:
                trend, delta = self._calc_cvd_trend(points)
                state.cvd_spot = CVDData(
                    coin=coin.ccy, inst_type="SPOT",
                    series=points, trend_1h=trend, delta_1h=delta,
                )

    def _calc_cvd_trend(self, points: list[CVDPoint], lookback: int = 12) -> tuple[str, float]:
        if len(points) < 2:
            return "flat", 0.0
        recent = points[-lookback:]
        delta_sum = sum(p.delta for p in recent)
        start_cvd = recent[0].cvd
        end_cvd = recent[-1].cvd
        diff = end_cvd - start_cvd
        abs_values = [abs(p.delta) for p in recent if p.delta != 0]
        median_abs = sorted(abs_values)[len(abs_values) // 2] if abs_values else 1.0
        threshold = max(median_abs * 0.5, abs(delta_sum) * 0.05)
        if diff > threshold:
            return "rising", delta_sum
        elif diff < -threshold:
            return "declining", delta_sum
        return "flat", delta_sum

    async def _poll_taker_volume(self, coin: CoinConfig):
        """获取 Taker 买卖量"""
        state = self._states[coin.ccy]

        contract_data = await self._cg.fetch_aggregated_taker_bs_history(
            coin.symbol_cg, interval="5m", limit=24,
        )
        spot_data = await self._cg.fetch_spot_aggregated_taker_bs(
            coin.symbol_cg, interval="5m", limit=24,
        )

        c_buy = c_sell = s_buy = s_sell = 0.0
        if contract_data:
            for item in contract_data:
                try:
                    c_buy += float(item.get("buyVolUsd", item.get("buyVol", 0)))
                    c_sell += float(item.get("sellVolUsd", item.get("sellVol", 0)))
                except (ValueError, KeyError):
                    continue

        if spot_data:
            for item in spot_data:
                try:
                    s_buy += float(item.get("buyVolUsd", item.get("buyVol", 0)))
                    s_sell += float(item.get("sellVolUsd", item.get("sellVol", 0)))
                except (ValueError, KeyError):
                    continue

        total = c_buy + c_sell + s_buy + s_sell
        buy_ratio = (c_buy + s_buy) / total if total > 0 else 0.5
        state.taker_flow = TakerFlowData(
            coin=coin.ccy, ts=int(time.time()),
            buy_ratio=round(buy_ratio, 3),
            sell_ratio=round(1 - buy_ratio, 3),
            dominant="buyers" if buy_ratio > 0.55 else "sellers" if buy_ratio < 0.45 else "balanced",
            contract_buy_vol=c_buy, contract_sell_vol=c_sell,
            spot_buy_vol=s_buy, spot_sell_vol=s_sell,
        )

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Phase 3: 技术指标（Coinglass 直取）
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    async def _poll_indicators(self, coin: CoinConfig):
        """从 Coinglass 获取所有技术指标：RSI/MACD/MA/EMA/ATR/BOLL"""
        state = self._states[coin.ccy]
        exchange = coin.exchange_primary
        pair = coin.symbol_cg_pair

        rsi_data = await self._cg.fetch_rsi(exchange, pair, interval="1d", limit=2, window=14)
        if rsi_data and len(rsi_data) > 0:
            last = rsi_data[-1]
            state.rsi_14 = float(last.get("rsi", last.get("value", 0)))

        macd_data = await self._cg.fetch_macd(exchange, pair, interval="1d", limit=2)
        if macd_data and len(macd_data) > 0:
            last = macd_data[-1]
            state.macd_data = {
                "macd": float(last.get("macd", 0)),
                "signal": float(last.get("signal", 0)),
                "histogram": float(last.get("histogram", last.get("hist", 0))),
                "above_zero": float(last.get("macd", 0)) > 0,
            }

        ma60_data = await self._cg.fetch_ma(exchange, pair, interval="1d", limit=2, window=60)
        if ma60_data and len(ma60_data) > 0:
            state.ma60_daily_cg = float(ma60_data[-1].get("ma", ma60_data[-1].get("value", 0)))

        ma120_data = await self._cg.fetch_ma(exchange, pair, interval="1d", limit=2, window=120)
        if ma120_data and len(ma120_data) > 0:
            state.ma120_daily_cg = float(ma120_data[-1].get("ma", ma120_data[-1].get("value", 0)))

        atr_data = await self._cg.fetch_atr(exchange, pair, interval="1h", limit=2, window=14)
        if atr_data and len(atr_data) > 0:
            state.atr_cg = float(atr_data[-1].get("atr", atr_data[-1].get("value", 0)))
            state.atr = state.atr_cg

        boll_data = await self._cg.fetch_boll(exchange, pair, interval="1d", limit=2)
        if boll_data and len(boll_data) > 0:
            last = boll_data[-1]
            state.boll_data = {
                "upper": float(last.get("upper", last.get("upperBand", 0))),
                "middle": float(last.get("middle", last.get("middleBand", 0))),
                "lower": float(last.get("lower", last.get("lowerBand", 0))),
            }

        self._recompute_range_signal(coin.ccy)

    async def _poll_basis(self, coin: CoinConfig):
        """获取期现溢价"""
        data = await self._cg.fetch_basis_history(
            coin.exchange_primary, coin.symbol_cg_pair,
            interval="5m", limit=1,
        )
        if not data:
            return

        state = self._states[coin.ccy]
        try:
            last = data[-1]
            basis_pct = float(last.get("basisRate", last.get("basis", 0))) * 100
            price = state.ticker.last if state.ticker else 0
            interp = "合约偏贵" if basis_pct > 0.1 else "合约折价" if basis_pct < -0.1 else "中性"
            state.basis = BasisData(
                coin=coin.ccy, ts=int(time.time()),
                mark_price=price * (1 + basis_pct / 200),
                index_price=price * (1 - basis_pct / 200),
                basis_pct=round(basis_pct, 4),
                interpretation=interp,
            )
        except (ValueError, KeyError, IndexError):
            pass

    def _recompute_range_signal(self, ccy: str):
        """用 Coinglass 指标重新计算均线箱体信号。"""
        state = self._states[ccy]
        if not state.ticker:
            return
        price = state.ticker.last
        if price <= 0:
            return

        sweep_above = sum(
            e.get("usd", 0) for e in state.liq_sweep_events
            if e.get("side") == "above" and e.get("ts", 0) > int(time.time()) - 3600
        )
        sweep_below = sum(
            e.get("usd", 0) for e in state.liq_sweep_events
            if e.get("side") == "below" and e.get("ts", 0) > int(time.time()) - 3600
        )

        btc_state = self._states.get("BTC")
        cps = None
        if btc_state and btc_state.cycle_position and btc_state.cycle_position.cps is not None:
            cps = btc_state.cycle_position.cps

        rs_cfg = self._settings.processors.range_signal

        if state.candles_daily:
            state.range_signal = calculate_range_signal(
                candles_1d=state.candles_daily,
                candles_1w=state.candles_weekly,
                current_price=price,
                atr=state.atr,
                sweep_above_1h=sweep_above,
                sweep_below_1h=sweep_below,
                cps=cps,
                cfg=rs_cfg,
            )

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # K 线数据（VP / ATR / range_signal 依赖）
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _parse_candles(self, raw: list) -> list[dict]:
        """将 Coinglass K 线数据统一为 {ts, o, h, l, c, vol} 格式。"""
        candles = []
        for item in raw:
            try:
                candles.append({
                    "ts": int(item.get("t", item.get("ts", 0))),
                    "o": float(item.get("o", item.get("open", 0))),
                    "h": float(item.get("h", item.get("high", 0))),
                    "l": float(item.get("l", item.get("low", 0))),
                    "c": float(item.get("c", item.get("close", 0))),
                    "vol": float(item.get("v", item.get("vol", item.get("volume", 0)))),
                })
            except (ValueError, TypeError):
                continue
        return candles

    async def _poll_candles_1h(self, coin: CoinConfig):
        """获取 1H K线用于 Volume Profile / ATR 计算。"""
        from models.market import CandleData
        data = await self._cg.fetch_price_history(
            coin.exchange_primary, coin.symbol_cg_pair,
            interval="1h", limit=200,
        )
        if not data:
            return
        state = self._states[coin.ccy]
        raw = self._parse_candles(data)
        if not raw:
            return

        state.candle_prices = [c["c"] for c in raw]
        state.candle_ts = [c["ts"] for c in raw]

        candle_models = [
            CandleData(coin=coin.ccy, ts=c["ts"], o=c["o"], h=c["h"],
                       l=c["l"], c=c["c"], vol=c["vol"])
            for c in raw
        ]
        vp = calc_volume_profile(candle_models, coin=coin.ccy)
        if vp:
            state.vp = vp

        if len(candle_models) >= 15 and state.atr_cg is None:
            from processors.volume_profile import calc_atr
            atr_val = calc_atr(candle_models)
            if atr_val > 0:
                state.atr = atr_val

    async def _poll_candles_daily(self, coin: CoinConfig):
        """获取日线 K线用于 range_signal 箱体检测。"""
        from models.market import CandleData
        data = await self._cg.fetch_price_history(
            coin.exchange_primary, coin.symbol_cg_pair,
            interval="1d", limit=150,
        )
        if not data:
            return
        state = self._states[coin.ccy]
        raw = self._parse_candles(data)
        if raw:
            state.candles_daily = [
                CandleData(coin=coin.ccy, ts=c["ts"], o=c["o"], h=c["h"],
                           l=c["l"], c=c["c"], vol=c["vol"])
                for c in raw
            ]
            self._recompute_range_signal(coin.ccy)

    async def _poll_candles_weekly(self, coin: CoinConfig):
        """获取周线 K线用于 range_signal 周线 MA60。"""
        from models.market import CandleData
        data = await self._cg.fetch_price_history(
            coin.exchange_primary, coin.symbol_cg_pair,
            interval="1w", limit=70,
        )
        if not data:
            return
        state = self._states[coin.ccy]
        raw = self._parse_candles(data)
        if raw:
            state.candles_weekly = [
                CandleData(coin=coin.ccy, ts=c["ts"], o=c["o"], h=c["h"],
                           l=c["l"], c=c["c"], vol=c["vol"])
                for c in raw
            ]

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Phase 4: 新维度
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    async def _poll_options(self, _coin: CoinConfig):
        """获取期权数据"""
        for symbol in ("BTC", "ETH"):
            max_pain = await self._cg.fetch_option_max_pain(symbol)
            if max_pain:
                from models.options import OptionMaxPainExpiry
                expiries = []
                for item in max_pain:
                    try:
                        expiries.append(OptionMaxPainExpiry(
                            expiry_date=item.get("expiryDate", item.get("date", "")),
                            max_pain_price=float(item.get("maxPain", item.get("price", 0))),
                            call_oi=float(item.get("callOI", 0)),
                            put_oi=float(item.get("putOI", 0)),
                        ))
                    except (ValueError, KeyError):
                        continue

                nearest = expiries[0] if expiries else None
                for ccy in self._settings.supported_coins:
                    if ccy == symbol:
                        self._states[ccy].option_max_pain = OptionMaxPainData(
                            symbol=symbol, ts=int(time.time()),
                            expiries=expiries,
                            nearest_max_pain=nearest.max_pain_price if nearest else None,
                            nearest_expiry=nearest.expiry_date if nearest else "",
                        )

            info = await self._cg.fetch_option_info(symbol)
            if info and symbol in self._states:
                state = self._states[symbol]
                state.option_info = OptionInfoData(
                    symbol=symbol, ts=int(time.time()),
                    total_oi_usd=float(info.get("totalOI", 0)),
                    total_vol_24h_usd=float(info.get("totalVol24h", 0)),
                    put_call_oi_ratio=float(info.get("putCallOIRatio", 0)),
                    put_call_vol_ratio=float(info.get("putCallVolRatio", 0)),
                )

    async def _poll_large_orders(self, coin: CoinConfig):
        """获取大单追踪"""
        data = await self._cg.fetch_large_orders(coin.exchange_primary, coin.symbol_cg_pair)
        if not data:
            return

        state = self._states[coin.ccy]
        from models.orderbook_ext import LargeOrder
        orders = []
        total_bid = total_ask = 0.0
        for item in data:
            try:
                side = "bid" if item.get("side", "").lower() in ("buy", "bid") else "ask"
                size_usd = float(item.get("volUsd", item.get("sizeUsd", 0)))
                orders.append(LargeOrder(
                    ts=int(item.get("time", item.get("ts", 0))),
                    exchange=coin.exchange_primary,
                    symbol=coin.symbol_cg_pair,
                    price=float(item.get("price", 0)),
                    size_usd=size_usd,
                    side=side,
                    status=item.get("status", "active"),
                ))
                if side == "bid":
                    total_bid += size_usd
                else:
                    total_ask += size_usd
            except (ValueError, KeyError):
                continue

        state.large_orders = LargeOrderSnapshot(
            symbol=coin.symbol_cg_pair, ts=int(time.time()),
            orders=orders, total_bid_usd=total_bid, total_ask_usd=total_ask,
        )

    async def _poll_whale_data(self, _coin: CoinConfig):
        """获取巨鲸数据"""
        from models.whale import HyperliquidWhaleAlert, WhaleTransfer

        alerts_data = await self._cg.fetch_hyperliquid_whale_alert()
        alerts = []
        if alerts_data:
            for item in alerts_data:
                try:
                    alerts.append(HyperliquidWhaleAlert(
                        ts=int(item.get("time", item.get("ts", 0))),
                        symbol=item.get("symbol", ""),
                        side=item.get("side", ""),
                        size_usd=float(item.get("sizeUsd", item.get("volUsd", 0))),
                        entry_price=float(item.get("entryPrice", item.get("price", 0))),
                        address=item.get("address", ""),
                        action=item.get("action", ""),
                    ))
                except (ValueError, KeyError):
                    continue

        transfers_data = await self._cg.fetch_whale_transfer()
        transfers = []
        if transfers_data:
            for item in transfers_data:
                try:
                    transfers.append(WhaleTransfer(
                        ts=int(item.get("time", item.get("ts", 0))),
                        symbol=item.get("symbol", ""),
                        amount=float(item.get("amount", 0)),
                        amount_usd=float(item.get("amountUsd", item.get("valueUsd", 0))),
                        from_label=item.get("fromLabel", item.get("from", "")),
                        to_label=item.get("toLabel", item.get("to", "")),
                        tx_hash=item.get("txHash", ""),
                        blockchain=item.get("blockchain", item.get("chain", "")),
                    ))
                except (ValueError, KeyError):
                    continue

        whale = WhaleData(
            ts=int(time.time()),
            hl_alerts=alerts,
            transfers=transfers,
        )

        for ccy in self._settings.supported_coins:
            self._states[ccy].whale_data = whale

    async def _poll_etf_flow(self, _coin: CoinConfig):
        """获取 ETF 资金流"""
        for asset, fetch_fn in [
            ("BTC", self._cg.fetch_btc_etf_flow_history),
            ("ETH", self._cg.fetch_eth_etf_flow_history),
        ]:
            data = await fetch_fn()
            if not data:
                continue

            recent = data[-5:] if len(data) >= 5 else data
            days = []
            net_3d = 0.0
            for item in recent:
                try:
                    total_net = float(item.get("totalNetflow", item.get("netflow", 0)))
                    days.append(ETFFlowDay(
                        date=item.get("date", ""),
                        total_net=total_net,
                    ))
                    net_3d += total_net
                except (ValueError, KeyError):
                    continue

            trend = "inflow" if net_3d > 0 else "outflow" if net_3d < 0 else "mixed"
            etf = ETFFlowData(
                ts=int(time.time()), asset=asset,
                recent_days=days, net_3d=net_3d, trend=trend,
            )

            for ccy in self._settings.supported_coins:
                if asset == "BTC" or ccy == asset:
                    self._states[ccy].etf_flow = etf

    async def _poll_global_liq(self, _coin: CoinConfig):
        """获取全网爆仓统计"""
        data = await self._cg.fetch_liquidation_exchange_list(range_="24h")
        if not data:
            return

        long_24h = short_24h = 0.0
        for item in data:
            try:
                long_24h += float(item.get("longLiqUsd", item.get("buyVolUsd", 0)))
                short_24h += float(item.get("shortLiqUsd", item.get("sellVolUsd", 0)))
            except (ValueError, KeyError):
                continue

        ratio_24h = long_24h / short_24h if short_24h > 0 else 1.0
        gliq = GlobalLiquidationData(
            ts=int(time.time()),
            long_24h_usd=long_24h, short_24h_usd=short_24h,
            ratio_24h=round(ratio_24h, 2),
        )

        for ccy in self._settings.supported_coins:
            self._states[ccy].global_liq = gliq

    async def _poll_macro_index(self, _coin: CoinConfig):
        """获取宏观市场指标"""
        mi = MarketIndexData(ts=int(time.time()))

        fg_data = await self._cg.fetch_fear_greed()
        if fg_data and len(fg_data) > 0:
            last = fg_data[-1]
            mi.fear_greed = float(last.get("value", 0))

        dom_data = await self._cg.fetch_btc_dominance()
        if dom_data and len(dom_data) > 0:
            last = dom_data[-1]
            mi.btc_dominance = float(last.get("value", last.get("dominance", 0)))

        for ccy in self._settings.supported_coins:
            self._states[ccy].market_index = mi

    async def _poll_onchain_cycle(self, _coin: CoinConfig):
        """从 Coinglass 获取链上周期数据 → 计算 CPS"""
        from models.flow import OnchainCycleData

        raw = OnchainCycleData(ts=int(time.time()))

        ahr_data = await self._cg.fetch_ahr999()
        if ahr_data and len(ahr_data) > 0:
            raw.ahr999 = float(ahr_data[-1].get("value", ahr_data[-1].get("ahr999", 0)))

        pi_data = await self._cg.fetch_pi_cycle()
        if pi_data and len(pi_data) > 0:
            last = pi_data[-1]
            raw.pi_111dma_x2 = float(last.get("sma111X2", last.get("ma111x2", 0)))
            raw.pi_350dma = float(last.get("sma350", last.get("ma350", 0)))

        ma_200w_data = await self._cg.fetch_200w_ma_heatmap()
        if ma_200w_data and len(ma_200w_data) > 0:
            last = ma_200w_data[-1]
            raw.sma_200w = float(last.get("sma", last.get("ma200w", 0)))

        sth_data = await self._cg.fetch_sth_realized_price()
        if sth_data and len(sth_data) > 0:
            raw.sth_cost_1d = float(sth_data[-1].get("value", sth_data[-1].get("price", 0)))

        btc_state = self._states.get("BTC")
        btc_price = btc_state.ticker.last if btc_state and btc_state.ticker else 0
        cycle_pos = calculate_cycle_position(raw, btc_price) if btc_price > 0 else None

        for ccy in self._settings.supported_coins:
            self._states[ccy].cycle_position = cycle_pos

    async def _poll_news(self, _coin: CoinConfig):
        """获取新闻"""
        from models.macro import NewsArticle
        data = await self._cg.fetch_news(language="zh", per_page=10)
        if not data:
            return

        articles = []
        for item in data:
            try:
                articles.append(NewsArticle(
                    ts=int(item.get("time", item.get("ts", 0))),
                    title=item.get("title", ""),
                    content=item.get("content", item.get("desc", "")),
                    source=item.get("source", ""),
                    url=item.get("url", ""),
                ))
            except (ValueError, KeyError):
                continue

        news = NewsData(ts=int(time.time()), articles=articles)
        for ccy in self._settings.supported_coins:
            self._states[ccy].news = news

    # ── 重新计算 ──

    def _recompute(self, ccy: str):
        state = self._states[ccy]
        price = state.ticker.last if state.ticker else 0
        if price <= 0:
            return

        liq_map = state.liq_maps.get("1d") or state.liq_maps.get("24h")

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
            cycle_position=state.cycle_position,
        )

        if state.candles_daily:
            self._recompute_range_signal(ccy)

        self._recompute_key_levels(ccy)

    def _recompute_key_levels(self, ccy: str):
        state = self._states[ccy]
        price = state.ticker.last if state.ticker else 0
        if price <= 0:
            return

        cutoff = int(time.time()) - 3600
        recent_sweeps = [
            e for e in state.liq_sweep_events if e.get("ts", 0) > cutoff
        ]

        range_upper = None
        range_lower = None
        if state.range_signal:
            range_upper = state.range_signal.range_upper
            range_lower = state.range_signal.range_lower

        kl_cfg = self._settings.processors.key_level_tracker

        snapshot = update_key_levels(
            prev_levels=state.key_levels,
            current_price=price,
            levels=state.levels,
            liq_map=state.liq_maps.get("1d") or state.liq_maps.get("24h"),
            range_upper=range_upper,
            range_lower=range_lower,
            sweep_events_1h=recent_sweeps,
            atr=state.atr,
            cfg=kl_cfg if kl_cfg else None,
        )

        state.key_levels = snapshot.levels
        state.key_level_snapshot = snapshot

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
        if state.ls_ratio_top_account:
            payload["ls_ratio_top_account"] = state.ls_ratio_top_account.model_dump()
        if state.ls_ratio_top_position:
            payload["ls_ratio_top_position"] = state.ls_ratio_top_position.model_dump()
        if state.etf_flow:
            payload["etf_flow"] = state.etf_flow.model_dump()
        if state.global_liq:
            payload["global_liq"] = state.global_liq.model_dump()
        if state.market_index:
            payload["market_index"] = state.market_index.model_dump()
        if state.levels and state.levels.sniper_entries:
            payload["sniper_entries"] = [se.model_dump() for se in state.levels.sniper_entries[:4]]
        if state.levels and state.levels.ladder_plans:
            payload["ladder_plans"] = [lp.model_dump() for lp in state.levels.ladder_plans]
        if state.range_signal:
            payload["range_signal"] = state.range_signal.model_dump()
        if state.key_level_snapshot and state.key_level_snapshot.levels:
            payload["key_levels"] = state.key_level_snapshot.model_dump()

        # 新维度
        if state.option_max_pain:
            payload["option_max_pain"] = state.option_max_pain.model_dump()
        if state.option_info:
            payload["option_info"] = state.option_info.model_dump()
        if state.large_orders:
            payload["large_orders"] = state.large_orders.model_dump()
        if state.whale_data:
            payload["whale_data"] = {
                "hl_alerts_count": len(state.whale_data.hl_alerts),
                "transfers_count": len(state.whale_data.transfers),
            }
        if state.liq_max_pain:
            payload["liq_max_pain"] = state.liq_max_pain.model_dump()
        if state.liq_heatmaps:
            payload["liq_heatmaps"] = {k: v.model_dump() for k, v in state.liq_heatmaps.items()}
        if state.rsi_14 is not None:
            payload["rsi_14"] = state.rsi_14
        if state.macd_data:
            payload["macd"] = state.macd_data
        if state.boll_data:
            payload["boll"] = state.boll_data
        if state.news:
            payload["news_count"] = len(state.news.articles)

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
        liq = state.liq_maps.get("1d") or state.liq_maps.get("24h")
        if liq:
            result["liquidation_1d"] = liq.model_dump()
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

    def is_ai_running(self, ccy: str) -> bool:
        return ccy in self._ai_running

    async def fire_ai_analysis(self, ccy: str) -> None:
        if ccy in self._ai_running:
            raise RuntimeError(f"AI analysis already running for {ccy}")
        state = self._states[ccy]
        state.last_ai_ts = time.time()
        self._ai_running.add(ccy)
        asyncio.create_task(self._ai_analysis_task(ccy))

    async def _ai_analysis_task(self, ccy: str) -> None:
        try:
            result = await self.run_ai_analysis(ccy)
            await push_to_coin(ccy, "ai_result", result.model_dump())
            logger.info("AI result pushed via WebSocket | coin=%s | len=%d",
                        ccy, len(result.raw_text) if result.raw_text else 0)
        except Exception as e:
            logger.error("AI background task failed | coin=%s | %s: %s",
                         ccy, type(e).__name__, e, exc_info=True)
            await push_to_coin(ccy, "ai_error", {"coin": ccy, "message": str(e)})
        finally:
            self._ai_running.discard(ccy)

    async def run_ai_analysis(self, ccy: str) -> AIAnalysisResult:
        state = self._states[ccy]
        if not state.ticker:
            raise RuntimeError(f"No price data for {ccy}")

        cutoff = int(time.time()) - 3600
        recent_sweeps = [e for e in state.liq_sweep_events if e.get("ts", 0) > cutoff]

        opt = state.option_max_pain
        lo = state.large_orders
        snapshot = build_ai_snapshot(
            coin=ccy, price=state.ticker.last,
            high_24h=state.ticker.high_24h, low_24h=state.ticker.low_24h,
            liq_map=state.liq_maps.get("1d") or state.liq_maps.get("24h"),
            cvd_contract=state.cvd_contract,
            cvd_spot=state.cvd_spot, oi=state.oi, funding=state.funding,
            basis=state.basis, orderbook=state.orderbook, liq_stats=state.liq_stats,
            vp=state.vp, atr=state.atr,
            market_temp_score=state.temperature.score if state.temperature else 50,
            pin_risk_level=state.temperature.pin_risk_level if state.temperature else "low",
            multi_funding=state.multi_funding, ls_ratio=state.ls_ratio,
            etf_flow=state.etf_flow, global_liq=state.global_liq,
            market_index=state.market_index, taker_flow=state.taker_flow,
            levels=state.levels,
            liq_map_7d=state.liq_maps.get("7d"),
            cycle_position=state.cycle_position,
            liq_sweep_events=recent_sweeps,
            range_signal=state.range_signal,
            key_level_snapshot=state.key_level_snapshot,
            liq_map_30d=state.liq_maps.get("30d"),
            rsi_14=state.rsi_14,
            macd_data=state.macd_data,
            boll_data=state.boll_data,
            ema20=state.ema20_cg,
            ma60_daily=state.ma60_daily_cg,
            ma120_daily=state.ma120_daily_cg,
            option_max_pain_price=opt.nearest_max_pain if opt else None,
            option_nearest_expiry=opt.nearest_expiry if opt else "",
            option_call_oi=opt.expiries[0].call_oi if opt and opt.expiries else None,
            option_put_oi=opt.expiries[0].put_oi if opt and opt.expiries else None,
            large_orders_buy_count=len([o for o in lo.orders if o.side == "bid"]) if lo else 0,
            large_orders_sell_count=len([o for o in lo.orders if o.side == "ask"]) if lo else 0,
            large_orders_net_usd=sum(
                o.size_usd * (1 if o.side == "bid" else -1) for o in lo.orders
            ) if lo else 0,
        )

        result = await self._analyzer.analyze(snapshot)
        state.ai_history.append(result)
        state.last_ai_ts = time.time()
        return result

    def get_source_health(self) -> list[dict]:
        return [
            self._cg.health().model_dump(),
            {
                "name": "coinglass_daily_usage",
                "status": "ok",
                "daily_requests": self._cg.daily_request_count,
                "daily_limit": self._settings.coinglass.daily_limit,
                "usage_pct": round(
                    self._cg.daily_request_count / self._settings.coinglass.daily_limit * 100, 1
                ),
            },
        ]
