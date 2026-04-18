"""
主引擎：调度 Coinglass 统一数据源轮询 + 处理 + 缓存 + 推送。
每个币种运行独立的数据管线，互不干扰。
所有数据通过 Coinglass V4 API 获取，纯 REST 轮询架构。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import deque
from datetime import datetime
from typing import Any, Optional

from ai.analyzer import AIAnalyzer, create_analyzer
from ai.snapshot import build_ai_snapshot
from api.ws import push_to_coin
from config.settings import CoinConfig, get_settings
from models.flow import (
    BasisData, CVDData, CVDPoint, CyclePositionData, ETFFlowData, ETFFlowDay,
    FundingRateData, GlobalLiquidationData, LongShortRatioData,
    LongShortRatioExchange, MarketIndexData, MultiFundingRateData,
    ExchangeFundingRate, OIData, OISnapshot, RangeSignalData, TakerFlowData,
)
from models.key_level import KeyLevelSnapshotV2
from models.market_structure import MarketStructure
from models.levels import LevelAnalysis
from models.liquidation import (
    HeatmapData, LiqHistoryData, LiqMaxPainData,
    LiquidationMap, LiquidationStats,
)
from models.macro import (
    CoinbasePremiumData, NewsData, StablecoinMcapData,
)
from models.market import OrderBookAnalysis, TickerData, VolumeProfileData
from models.options import OptionInfoData, OptionMaxPainData
from models.orderbook_ext import LargeOrderSnapshot
from models.snapshot import (
    AIAnalysisResult, MarketTemperature, WaterfallData,
)
from models.whale import WhaleData
from processors.cvd import detect_cvd_price_divergence
from processors.levels import calculate_levels
from processors.liquidation import detect_liq_sweep, process_liquidation_map
from processors.market_temp import build_waterfall, calc_market_temperature
from processors.percentile import PercentileTracker
from processors.volume_profile import calc_volume_profile
from processors.cycle import calculate_cycle_position
from processors.range_signal import calculate_range_signal
from sources.coinglass import CoinglassSource, create_coinglass_source
from sources.binance_futures import BinanceFuturesSource, create_binance_source

logger = logging.getLogger(__name__)


class CoinState:
    """单个币种的完整数据状态"""

    def __init__(self, coin: str, max_history: int = 500):
        self.coin = coin
        self._max_history = max_history
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
        self.candles_1h: list = []
        self.candles_15m: list = []
        self.oi_history: deque = deque(maxlen=720)
        self.ai_history: deque[AIAnalysisResult] = deque(maxlen=max_history)
        self.last_ai_ts: float = 0
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
        self.market_structure: Optional[MarketStructure] = None
        self._prev_ms_summary: tuple = ()
        self._prev_liq_map_24h: Optional[LiquidationMap] = None
        self._prev_price_at_liq_poll: float = 0
        self.liq_sweep_events: deque = deque(maxlen=120)
        # Phase 2: 清算热力图 + 最大痛点 + 爆仓历史
        self.liq_heatmaps: dict[str, HeatmapData] = {}
        self.liq_max_pain: dict[str, LiqMaxPainData] = {}
        self.liq_history: Optional[LiqHistoryData] = None
        # Phase 3: Coinglass 技术指标缓存
        self.rsi_14: Optional[float] = None
        self.macd_data: Optional[dict] = None
        self.ma60_daily_cg: Optional[float] = None
        self.ma120_daily_cg: Optional[float] = None
        self.ema20_cg: Optional[float] = None
        self.boll_data: Optional[dict] = None
        self.atr_cg: Optional[float] = None
        # Phase 4: 新维度（已接入）
        self.option_max_pain: Optional[OptionMaxPainData] = None
        self.option_info: Optional[OptionInfoData] = None
        self.large_orders: Optional[LargeOrderSnapshot] = None
        self.whale_data: Optional[WhaleData] = None
        self.news: Optional[NewsData] = None
        # Phase 4: 已接入的机构级数据
        self.coinbase_premium: Optional[CoinbasePremiumData] = None
        self.stablecoin_mcap: Optional[StablecoinMcapData] = None
        self.oi_exchange_rank: Optional[dict] = None
        # V2 关键位系统新增字段
        self.candles_4h: list = []
        self.ema_daily: dict[int, float] = {}   # {20: price, 50: price, 100: price, 200: price}
        self.sma200_daily_cg: Optional[float] = None
        self.boll_4h_data: Optional[dict] = None
        self.key_levels_v2: list = []
        self.key_level_snapshot_v2: Optional[KeyLevelSnapshotV2] = None
        self.kl_history: deque[KeyLevelSnapshotV2] = deque(maxlen=max_history)
        self._kl_v2_discovery_ts: float = 0.0
        # §9h: 净持仓 + 合约资金流 + TD 序列
        self.net_position_latest: Optional[float] = None
        self.net_position_trend: str = ""
        self.net_position_change_24h: Optional[float] = None
        self.futures_coin_netflow_1h: Optional[float] = None
        self.futures_coin_netflow_trend: str = ""
        self.td_sequential_count: Optional[int] = None
        self.td_sequential_direction: str = ""
        self.poll_failures: dict[str, str] = {}
        self._log_once_keys: set[str] = set()
        # 历史对比字段
        self.ls_ratio_change_24h: Optional[float] = None
        self.ls_ratio_long_pct: Optional[float] = None
        self.ls_ratio_short_pct: Optional[float] = None
        self.ls_top_acct_long_pct: Optional[float] = None
        self.ls_top_acct_short_pct: Optional[float] = None
        self.ls_top_acct_change_24h: Optional[float] = None
        self.oi_change_24h_pct: Optional[float] = None
        self.fear_greed_prev: Optional[int] = None


class Engine:
    """主引擎：Coinglass 统一数据源，REST 轮询架构"""

    def __init__(self):
        self._settings = get_settings()
        self._cg: CoinglassSource = create_coinglass_source()
        self._bn: BinanceFuturesSource = create_binance_source()
        from sources.bbx import BBXSource
        self._bbx = BBXSource(
            cache_ttl=self._settings.bbx.cache_ttl,
            timeout_sec=self._settings.bbx.timeout_sec,
        )
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
        self._logged_keys: set[str] = set()
        self._bn_ws_last_push_ts: dict[str, float] = {}
        self._bn_ws_min_push_interval_sec = 0.5

        self._data_dir = os.path.join(os.path.dirname(__file__), "data")
        self._ai_history_file = os.path.join(self._data_dir, "ai_history.json")
        self._kl_history_file = os.path.join(self._data_dir, "kl_history.json")

        max_hist = self._settings.ai.max_history
        for ccy in self._settings.supported_coins:
            self._states[ccy] = CoinState(ccy, max_history=max_hist)

        self._load_ai_history()
        self._load_kl_history()

        # S 级信号邮件通知
        from notifications.signal_monitor import AlertDedup
        self._alert_dedup = AlertDedup(
            cooldown_seconds=self._settings.notifications.email.cooldown_minutes * 60,
        )
        self._notif_cfg = self._settings.notifications.email
        self._alert_config_warned = False

    @property
    def ai_available(self) -> bool:
        return self._analyzer.available

    def _load_ai_history(self):
        if not os.path.exists(self._ai_history_file):
            logger.info("No AI history file found, starting fresh")
            return
        try:
            with open(self._ai_history_file, "r", encoding="utf-8") as f:
                raw: dict = json.load(f)
            total = 0
            for ccy, items in raw.items():
                if ccy not in self._states:
                    continue
                for item in items:
                    try:
                        self._states[ccy].ai_history.append(AIAnalysisResult(**item))
                        total += 1
                    except Exception:
                        continue
            logger.info("AI history loaded from disk | entries=%d", total)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to load AI history: %s", e)

    def _save_ai_history(self):
        try:
            os.makedirs(self._data_dir, exist_ok=True)
            data: dict[str, list] = {}
            for ccy, state in self._states.items():
                if state.ai_history:
                    data[ccy] = [h.model_dump() for h in state.ai_history]
            tmp_path = self._ai_history_file + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
            os.replace(tmp_path, self._ai_history_file)
            logger.debug("AI history saved to disk | coins=%d", len(data))
        except (OSError, TypeError) as e:
            logger.warning("Failed to save AI history: %s", e)

    def _load_kl_history(self):
        if not os.path.exists(self._kl_history_file):
            logger.info("No KL history file found, starting fresh")
            return
        try:
            with open(self._kl_history_file, "r", encoding="utf-8") as f:
                raw: dict = json.load(f)
            total = 0
            for ccy, items in raw.items():
                if ccy not in self._states:
                    continue
                for item in items:
                    try:
                        self._states[ccy].kl_history.append(KeyLevelSnapshotV2(**item))
                        total += 1
                    except Exception:
                        continue
            logger.info("KL history loaded from disk | entries=%d", total)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to load KL history: %s", e)

    def _save_kl_history(self):
        try:
            os.makedirs(self._data_dir, exist_ok=True)
            data: dict[str, list] = {}
            for ccy, state in self._states.items():
                if state.kl_history:
                    data[ccy] = [h.model_dump() for h in state.kl_history]
            tmp_path = self._kl_history_file + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
            os.replace(tmp_path, self._kl_history_file)
            logger.debug("KL history saved to disk | coins=%d", len(data))
        except (OSError, TypeError) as e:
            logger.warning("Failed to save KL history: %s", e)

    def get_kl_history(self, ccy: str) -> list[KeyLevelSnapshotV2]:
        return list(self._states.get(ccy, CoinState(ccy)).kl_history)

    def compute_backtest_stats(self, ccy: str) -> dict:
        """从 AI 历史计算轻量级回测统计。"""
        from models.snapshot import BacktestStats
        state = self._states.get(ccy)
        if not state:
            return BacktestStats(coin=ccy).model_dump()

        history = list(state.ai_history)
        if len(history) < 2:
            return BacktestStats(coin=ccy, ts=int(time.time())).model_dump()

        history.sort(key=lambda h: h.ts)

        price_highs: list[float] = []
        price_lows: list[float] = []
        for h in history:
            price_highs.append(h.price_at_analysis)
            price_lows.append(h.price_at_analysis)

        total = triggered = tp1_hit = sl_hit = pending = 0
        rr_sum = 0.0
        rr_count = 0
        tier_stats: dict[str, dict] = {}
        dir_stats: dict[str, dict] = {}
        src_stats: dict[str, dict] = {}
        recent: list[dict] = []

        for idx, report in enumerate(history[:-1]):
            if not report.trading_plan_entries:
                continue
            future_prices = [h.price_at_analysis for h in history[idx + 1:]]
            if not future_prices:
                continue
            future_high = max(future_prices)
            future_low = min(future_prices)

            for entry in report.trading_plan_entries:
                if not entry.entry or not entry.direction:
                    continue
                total += 1
                tier = entry.tier or "short"
                direction = entry.direction
                source = entry.source or "engine"

                for bucket_key, bucket_val, stats_dict in [
                    ("tier", tier, tier_stats),
                    ("direction", direction, dir_stats),
                    ("source", source, src_stats),
                ]:
                    if bucket_val not in stats_dict:
                        stats_dict[bucket_val] = {"total": 0, "triggered": 0, "tp1": 0, "sl": 0}
                    stats_dict[bucket_val]["total"] += 1

                entry_triggered = False
                if direction == "long":
                    entry_triggered = future_low <= entry.entry
                else:
                    entry_triggered = future_high >= entry.entry

                if not entry_triggered:
                    pending += 1
                    continue
                triggered += 1
                for stats_dict in (tier_stats, dir_stats, src_stats):
                    for k, v in [(tier, tier_stats), (direction, dir_stats), (source, src_stats)]:
                        if k in stats_dict:
                            stats_dict[k]["triggered"] += 1
                            break

                outcome = "pending"
                if direction == "long":
                    if entry.stop_loss and future_low <= entry.stop_loss:
                        if entry.tp1 and future_high >= entry.tp1:
                            outcome = "tp1"
                        else:
                            outcome = "sl"
                    elif entry.tp1 and future_high >= entry.tp1:
                        outcome = "tp1"
                else:
                    if entry.stop_loss and future_high >= entry.stop_loss:
                        if entry.tp1 and future_low <= entry.tp1:
                            outcome = "tp1"
                        else:
                            outcome = "sl"
                    elif entry.tp1 and future_low <= entry.tp1:
                        outcome = "tp1"

                if outcome == "tp1":
                    tp1_hit += 1
                elif outcome == "sl":
                    sl_hit += 1

                if entry.rr:
                    rr_sum += entry.rr
                    rr_count += 1

                if len(recent) < 20:
                    recent.append({
                        "ts": report.ts,
                        "price": report.price_at_analysis,
                        "direction": direction,
                        "tier": tier,
                        "entry": entry.entry,
                        "tp1": entry.tp1,
                        "sl": entry.stop_loss,
                        "rr": entry.rr,
                        "source": source,
                        "outcome": outcome,
                    })

        resolved = tp1_hit + sl_hit
        stats = BacktestStats(
            coin=ccy,
            ts=int(time.time()),
            total_signals=total,
            triggered=triggered,
            tp1_hit=tp1_hit,
            sl_hit=sl_hit,
            pending=pending,
            win_rate=round(tp1_hit / resolved * 100, 1) if resolved > 0 else 0,
            avg_rr=round(rr_sum / rr_count, 2) if rr_count > 0 else 0,
            by_tier=tier_stats,
            by_direction=dir_stats,
            by_source=src_stats,
            recent_signals=recent,
        )
        return stats.model_dump()

    async def start(self):
        """启动 Coinglass REST 轮询数据管线"""
        self._running = True
        logger.info(
            "Engine starting (Coinglass) | coins=%s default=%s",
            self._settings.supported_coins, self._default_coin,
        )

        tasks = [
            asyncio.create_task(self._grace_check_loop()),
            asyncio.create_task(self._cache_persist_loop()),
            asyncio.create_task(self._source_observe_loop()),
            asyncio.create_task(self._binance_ticker_ws_loop()),
        ]

        # 全局层 —— stagger 0.3s 间隔，关键数据优先，4s 内全部启动
        btc_coin = self._settings.get_coin("BTC")
        tasks.extend([
            asyncio.create_task(self._poll_loop(
                "cg_ticker", self._poll_ticker_all, btc_coin,
                self._poll_cfg.get("ticker", 15), 0,
            )),
            asyncio.create_task(self._poll_loop(
                "cg_fr", self._poll_funding_all, btc_coin,
                self._poll_cfg.get("funding_rate", 60), 0.3,
            )),
            asyncio.create_task(self._poll_loop(
                "cg_global_liq", self._poll_global_liq, btc_coin,
                self._poll_cfg.get("liquidation_map", 60), 0.6,
            )),
            asyncio.create_task(self._poll_loop(
                "cg_liq_max_pain", self._poll_liq_max_pain, btc_coin,
                self._poll_cfg.get("liquidation_map", 60), 0.9,
            )),
            asyncio.create_task(self._poll_loop(
                "bbx_index", self._poll_bbx_index, btc_coin,
                self._settings.bbx.poll_interval, 18,
            )),
            asyncio.create_task(self._poll_loop(
                "cg_etf", self._poll_etf_flow, btc_coin,
                self._poll_cfg.get("etf", 3600), 21,
            )),
            asyncio.create_task(self._poll_loop(
                "cg_whale", self._poll_whale_data, btc_coin,
                self._poll_cfg.get("whale", 900), 24,
            )),
            # cg_cb_premium 已被 BBX 替代（i:btcdpi:aicoin），节省 Coinglass 配额
            asyncio.create_task(self._poll_loop(
                "cg_options", self._poll_options, btc_coin,
                self._poll_cfg.get("options", 1200), 30,
            )),
            asyncio.create_task(self._poll_loop(
                "cg_onchain", self._poll_onchain_cycle, btc_coin,
                self._poll_cfg.get("onchain", 3600), 33,
            )),
            asyncio.create_task(self._poll_loop(
                "cg_stablecoin", self._poll_stablecoin_mcap, btc_coin,
                3600, 36,
            )),
        ])

        for idx, ccy in enumerate(self._settings.supported_coins):
            coin = self._settings.get_coin(ccy)
            stagger = 1.0 + idx * 1.5

            if ccy == self._default_coin:
                tasks.extend(self._create_full_poll_tasks(coin, stagger))

        auto_ai_sec = self._settings.ai.auto_interval_sec
        if auto_ai_sec > 0 and self.ai_available:
            tasks.append(asyncio.create_task(self._auto_ai_loop(auto_ai_sec)))

        kl_snap_sec = self._settings.processors.key_level_tracker.get("snapshot_interval_sec", 0)
        if kl_snap_sec > 0:
            tasks.append(asyncio.create_task(self._auto_kl_snapshot_loop(kl_snap_sec)))

        email_cfg = getattr(self._settings.notifications, 'email', None)
        if email_cfg and getattr(email_cfg, 'to', None):
            tasks.append(asyncio.create_task(self._digest_email_loop()))

        await asyncio.gather(*tasks, return_exceptions=True)

    def _create_full_poll_tasks(self, coin: CoinConfig, stagger: float) -> list[asyncio.Task]:
        """为活跃币种创建完整轮询任务集。
        stagger 间隔 0.5s，关键数据优先，8s 内全部启动。
        """
        ccy = coin.ccy
        s = stagger
        liq_map_interval = self._poll_cfg.get("liquidation_map", 60)
        oi_interval = self._poll_cfg.get("oi", 60)
        cvd_interval = self._poll_cfg.get("cvd", 60)
        ls_interval = self._poll_cfg.get("long_short", 120)
        taker_interval = self._poll_cfg.get("taker_volume", 120)
        orderbook_interval = self._poll_cfg.get("orderbook", 60)
        liq_history_interval = max(liq_map_interval * 5, 300)
        large_orders_interval = max(self._poll_cfg.get("large_orders", 120), 180)
        heatmap_interval = self._poll_cfg.get("liquidation_heatmap", 600)
        indicators_interval = 120
        candles_1d_interval = 600
        candles_1w_interval = 3600
        candles_4h_interval = 900
        candles_15m_interval = 60
        oi_rank_interval = 300
        net_pos_interval = 900
        netflow_interval = 900
        td_seq_interval = 3600
        return [
            asyncio.create_task(self._poll_loop(
                f"cg_push_{ccy}", self._push_loop, coin, 5, s,
            )),
            asyncio.create_task(self._poll_loop(
                f"cg_oi_{ccy}", self._poll_oi, coin,
                oi_interval, s + 0.4,
            )),
            asyncio.create_task(self._poll_loop(
                f"cg_liq_{ccy}", self._poll_liquidation_map, coin,
                liq_map_interval, s + 0.8,
            )),
            asyncio.create_task(self._poll_loop(
                f"cg_cvd_{ccy}", self._poll_cvd, coin,
                cvd_interval, s + 1.2,
            )),
            asyncio.create_task(self._poll_loop(
                f"cg_ls_{ccy}", self._poll_ls_ratio, coin,
                ls_interval, s + 1.6,
            )),
            asyncio.create_task(self._poll_loop(
                f"cg_candles_1h_{ccy}", self._poll_candles_1h, coin,
                60, s + 2.0,
            )),
            asyncio.create_task(self._poll_loop(
                f"cg_basis_{ccy}", self._poll_basis, coin,
                60, s + 2.4,
            )),
            asyncio.create_task(self._poll_loop(
                f"cg_taker_{ccy}", self._poll_taker_volume, coin,
                taker_interval, s + 2.8,
            )),
            asyncio.create_task(self._poll_loop(
                f"cg_large_orders_{ccy}", self._poll_large_orders, coin,
                large_orders_interval, s + 15.0,
            )),
            asyncio.create_task(self._poll_loop(
                f"cg_liq_history_{ccy}", self._poll_liq_history, coin,
                liq_history_interval, s + 18.0,
            )),
            asyncio.create_task(self._poll_loop(
                f"cg_indicators_{ccy}", self._poll_indicators, coin,
                indicators_interval, s + 3.2,
            )),
            asyncio.create_task(self._poll_loop(
                f"cg_heatmap_{ccy}", self._poll_liq_heatmap, coin,
                heatmap_interval, s + 3.6,
            )),
            asyncio.create_task(self._poll_loop(
                f"cg_orderbook_{ccy}", self._poll_orderbook_depth, coin,
                orderbook_interval, s + 4.0,
            )),
            asyncio.create_task(self._poll_loop(
                f"cg_oi_rank_{ccy}", self._poll_oi_exchange_rank, coin,
                oi_rank_interval, s + 21.0,
            )),
            asyncio.create_task(self._poll_loop(
                f"cg_candles_1d_{ccy}", self._poll_candles_daily, coin,
                candles_1d_interval, s + 4.4,
            )),
            asyncio.create_task(self._poll_loop(
                f"cg_candles_1w_{ccy}", self._poll_candles_weekly, coin,
                candles_1w_interval, s + 4.8,
            )),
            asyncio.create_task(self._poll_loop(
                f"cg_candles_4h_{ccy}", self._poll_candles_4h, coin,
                candles_4h_interval, s + 5.2,
            )),
            asyncio.create_task(self._poll_loop(
                f"cg_candles_15m_{ccy}", self._poll_candles_15m, coin,
                candles_15m_interval, s + 5.6,
            )),
            asyncio.create_task(self._poll_loop(
                f"cg_net_pos_{ccy}", self._poll_net_position, coin,
                net_pos_interval, s + 24.0,
            )),
            asyncio.create_task(self._poll_loop(
                f"cg_coin_netflow_{ccy}", self._poll_futures_coin_netflow, coin,
                netflow_interval, s + 27.0,
            )),
            asyncio.create_task(self._poll_loop(
                f"cg_td_seq_{ccy}", self._poll_td_sequential, coin,
                td_seq_interval, s + 30.0,
            )),
        ]

    async def stop(self):
        self._running = False
        for ccy, tasks in self._active_tasks.items():
            for t in tasks:
                t.cancel()
        self._active_tasks.clear()
        try:
            self._cg.save_cache_to_disk()
            logger.info("Cache saved to disk before shutdown")
        except Exception:
            logger.error("Failed to save cache on shutdown", exc_info=True)
        try:
            self._save_ai_history()
            logger.info("AI history saved to disk before shutdown")
        except Exception:
            logger.error("Failed to save AI history on shutdown", exc_info=True)
        try:
            self._save_kl_history()
            logger.info("KL history saved to disk before shutdown")
        except Exception:
            logger.error("Failed to save KL history on shutdown", exc_info=True)
        await self._cg.close()
        await self._bn.close()
        await self._bbx.close()
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

        self._active_tasks[ccy] = self._create_full_poll_tasks(coin, 3)

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

    async def _cache_persist_loop(self):
        """每 60 秒将 API 内存缓存持久化到磁盘。"""
        while self._running:
            await asyncio.sleep(60)
            try:
                self._cg.save_cache_to_disk()
            except Exception:
                logger.error("Cache persist failed", exc_info=True)

    async def _binance_ticker_ws_loop(self):
        """Binance ticker 实时流：仅负责价格更新，不影响 Coinglass 轮询链路。"""
        bn_cfg = self._settings.binance
        if not bn_cfg.ws_enabled or not bn_cfg.use_for_ticker:
            logger.info(
                "Binance WS ticker disabled | ws_enabled=%s use_for_ticker=%s",
                bn_cfg.ws_enabled, bn_cfg.use_for_ticker,
            )
            return

        symbol_to_ccy = {
            self._settings.get_coin(ccy).symbol_cg_pair: ccy
            for ccy in self._settings.supported_coins
        }
        watched_symbols = set(symbol_to_ccy.keys())
        backoff = max(1, bn_cfg.ws_reconnect_min_sec)

        logger.info("Binance WS ticker loop started | symbols=%s", sorted(watched_symbols))
        while self._running:
            try:
                async for events in self._bn.stream_tickers(watched_symbols):
                    if not self._running:
                        break
                    now = time.time()
                    backoff = max(1, bn_cfg.ws_reconnect_min_sec)
                    for item in events:
                        symbol = str(item.get("s", item.get("symbol", "")))
                        ccy = symbol_to_ccy.get(symbol)
                        if not ccy:
                            continue
                        state = self._states.get(ccy)
                        if not state:
                            continue
                        try:
                            price = float(item.get("c", item.get("lastPrice", 0)))
                            if price <= 0:
                                continue
                            chg_pct = float(item.get("P", item.get("priceChangePercent", 0)))
                            open_24h = price / (1 + chg_pct / 100) if chg_pct != 0 else price
                            ticker = TickerData(
                                coin=ccy,
                                ts=int(item.get("E", now * 1000)),
                                last=price,
                                high_24h=float(item.get("h", item.get("highPrice", price))),
                                low_24h=float(item.get("l", item.get("lowPrice", price))),
                                vol_24h=float(item.get("q", item.get("quoteVolume", 0))),
                                change_24h=round(price - open_24h, 2),
                                change_pct_24h=round(chg_pct, 2),
                            )
                            state.ticker = ticker
                            if "binance_ws_ticker_ready" not in state._log_once_keys:
                                state._log_once_keys.add("binance_ws_ticker_ready")
                                logger.info(
                                    "Binance WS ticker 生效 | coin=%s last=%.4f chg24h=%.2f%%",
                                    ccy, price, chg_pct,
                                )
                            last_push = self._bn_ws_last_push_ts.get(ccy, 0.0)
                            if now - last_push >= self._bn_ws_min_push_interval_sec:
                                self._bn_ws_last_push_ts[ccy] = now
                                await push_to_coin(
                                    ccy,
                                    "market_update",
                                    {"coin": ccy, "ts": int(now), "ticker": ticker.model_dump()},
                                )
                        except (TypeError, ValueError):
                            continue
                if self._running:
                    logger.warning("Binance WS ticker stream ended, reconnecting...")
            except Exception:
                logger.warning("Binance WS ticker loop error", exc_info=True)

            if not self._running:
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, max(1, bn_cfg.ws_reconnect_max_sec))

    async def _source_observe_loop(self):
        """每分钟输出一次数据源与关键数据就绪心跳。"""
        while self._running:
            await asyncio.sleep(60)
            try:
                ready = []
                for ccy in self._settings.supported_coins:
                    st = self._states[ccy]
                    ready.append({
                        "coin": ccy,
                        "ticker": bool(st.ticker),
                        "liq_1d": bool(st.liq_maps.get("1d") or st.liq_maps.get("24h")),
                        "kline_1h": bool(st.candles_1h),
                        "kline_15m": bool(st.candles_15m),
                        "indicators": st.rsi_14 is not None and bool(st.macd_data) and bool(st.boll_data),
                        "oi": st.oi is not None,
                        "funding": st.funding is not None,
                        "market_structure": st.market_structure is not None,
                    })
                bbx_h = self._bbx.health() if self._bbx else {}
                logger.info(
                    "Source heartbeat | coinglass=%s binance=%s bbx=%s ready=%s",
                    self._cg.health().status,
                    self._bn.health().status,
                    bbx_h.get("status", "n/a"),
                    ready,
                )
            except Exception:
                logger.debug("source heartbeat failed", exc_info=True)

    # ── 轮询循环 ──

    def _log_keys_once(self, tag: str, sample):
        if tag in self._logged_keys:
            return
        self._logged_keys.add(tag)
        if isinstance(sample, dict):
            logger.info("API fields [%s]: %s", tag, list(sample.keys())[:20])
        elif isinstance(sample, list) and sample and isinstance(sample[0], dict):
            logger.info("API fields [%s]: %s", tag, list(sample[0].keys())[:20])

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
        from polls.derivatives import poll_ticker_all
        bn = self._bn if self._settings.binance.use_for_ticker else None
        await poll_ticker_all(
            self._cg, self._states, self._settings.supported_coins,
            self._settings.get_coin, self._percentile, self._logged_keys, bn,
        )

    async def _poll_oi(self, coin: CoinConfig):
        from polls.derivatives import poll_oi
        await poll_oi(self._cg, coin, self._states[coin.ccy])

    async def _poll_funding_all(self, _coin: CoinConfig):
        from polls.derivatives import poll_funding_all
        await poll_funding_all(
            self._cg, self._states, self._settings.supported_coins,
            self._settings.get_coin, self._percentile, self._logged_keys,
        )

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Phase 2: 清算 + 多空比 + CVD + Taker
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    async def _poll_liquidation_map(self, coin: CoinConfig):
        from polls.liquidation import poll_liquidation_map
        await poll_liquidation_map(
            self._cg, coin, self._states[coin.ccy],
            self._settings.processors.levels["min_liq_cluster_usd"],
        )
        self._recompute(coin.ccy)

    def _parse_liquidation_map(self, data, coin, cycle, current_price=0):
        from polls.liquidation import parse_liquidation_map
        return parse_liquidation_map(data, coin, cycle, current_price)

    def _detect_and_store_sweep(self, state, new_map, price):
        from polls.liquidation import detect_and_store_sweep
        detect_and_store_sweep(state, new_map, price)

    async def _poll_liq_heatmap(self, coin: CoinConfig):
        from polls.liquidation import poll_liq_heatmap
        await poll_liq_heatmap(self._cg, coin, self._states[coin.ccy])

    async def _poll_liq_max_pain(self, _coin: CoinConfig):
        from polls.liquidation import poll_liq_max_pain
        await poll_liq_max_pain(self._cg, self._settings.supported_coins, self._states)

    async def _poll_liq_history(self, coin: CoinConfig):
        from polls.liquidation import poll_liq_history
        await poll_liq_history(
            self._cg, coin, self._states[coin.ccy],
            self._settings.processors.levels["min_liq_cluster_usd"],
        )

    async def _poll_ls_ratio(self, coin: CoinConfig):
        from polls.derivatives import poll_ls_ratio
        await poll_ls_ratio(self._cg, coin, self._states[coin.ccy], self._logged_keys)

    async def _poll_cvd(self, coin: CoinConfig):
        from polls.orderflow import poll_cvd
        await poll_cvd(self._cg, coin, self._states[coin.ccy])

    @staticmethod
    def _calc_cvd_trend(series, window=12):
        from polls.orderflow import calc_cvd_trend
        return calc_cvd_trend(series, window)

    async def _poll_taker_volume(self, coin: CoinConfig):
        from polls.orderflow import poll_taker_volume
        await poll_taker_volume(self._cg, coin, self._states[coin.ccy])

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Phase 3: 技术指标（Coinglass 直取）
    # 设计说明：这些指标(RSI/MACD/BOLL/MA)取最新值，服务 AI 分析 prompt。
    # 箱体模块(range_signal)使用本地 K 线计算完整序列，以支持交叉/背离检测，
    # 两者数据源相同(Coinglass K 线)但用途不同，属设计分工而非冗余。
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    async def _poll_indicators(self, coin: CoinConfig):
        from polls.candles import poll_indicators
        await poll_indicators(self._cg, coin, self._states[coin.ccy], self._bn)

    async def _poll_basis(self, coin: CoinConfig):
        from polls.derivatives import poll_basis
        bn = self._bn if self._settings.binance.use_for_basis else None
        await poll_basis(self._cg, coin, self._states[coin.ccy], bn)

    async def _poll_orderbook_depth(self, coin: CoinConfig):
        from polls.orderflow import poll_orderbook_depth
        await poll_orderbook_depth(self._cg, coin, self._states[coin.ccy])

    def _recompute_range_signal(self, ccy: str):
        from polls.candles import recompute_range_signal
        state = self._states[ccy]
        btc_state = self._states.get("BTC")
        recompute_range_signal(state, btc_state, self._settings.processors.range_signal)

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # K 线数据（VP / ATR / range_signal 依赖）
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    async def _poll_candles_15m(self, coin: CoinConfig):
        from polls.candles import poll_candles_15m
        bn = self._bn if self._settings.binance.use_for_klines else None
        await poll_candles_15m(self._cg, coin, self._states[coin.ccy], bn)

    async def _poll_candles_1h(self, coin: CoinConfig):
        from polls.candles import poll_candles_1h
        bn = self._bn if self._settings.binance.use_for_klines else None
        await poll_candles_1h(self._cg, coin, self._states[coin.ccy], bn)

    async def _poll_candles_daily(self, coin: CoinConfig):
        from polls.candles import poll_candles_daily
        bn = self._bn if self._settings.binance.use_for_klines else None
        await poll_candles_daily(self._cg, coin, self._states[coin.ccy], bn)

    async def _poll_candles_weekly(self, coin: CoinConfig):
        from polls.candles import poll_candles_weekly
        bn = self._bn if self._settings.binance.use_for_klines else None
        await poll_candles_weekly(self._cg, coin, self._states[coin.ccy], bn)

    async def _poll_candles_4h(self, coin: CoinConfig):
        from polls.candles import poll_candles_4h
        bn = self._bn if self._settings.binance.use_for_klines else None
        await poll_candles_4h(self._cg, coin, self._states[coin.ccy], bn)

    async def _poll_net_position(self, coin: CoinConfig):
        from polls.derivatives import poll_net_position
        await poll_net_position(self._cg, coin, self._states[coin.ccy])

    async def _poll_futures_coin_netflow(self, coin: CoinConfig):
        from polls.derivatives import poll_futures_coin_netflow
        await poll_futures_coin_netflow(self._cg, coin, self._states[coin.ccy])

    async def _poll_td_sequential(self, coin: CoinConfig):
        from polls.derivatives import poll_td_sequential
        await poll_td_sequential(self._cg, coin, self._states[coin.ccy])

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Phase 4: 新维度
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    async def _poll_options(self, _coin: CoinConfig):
        from polls.macro import poll_options
        await poll_options(self._cg, self._states, self._settings.supported_coins)

    async def _poll_large_orders(self, coin: CoinConfig):
        from polls.orderflow import poll_large_orders
        await poll_large_orders(self._cg, coin, self._states[coin.ccy])

    async def _poll_whale_data(self, _coin: CoinConfig):
        from polls.macro import poll_whale_data
        await poll_whale_data(self._cg, self._states, self._settings.supported_coins)

    async def _poll_etf_flow(self, _coin: CoinConfig):
        from polls.macro import poll_etf_flow
        await poll_etf_flow(self._cg, self._states, self._settings.supported_coins)

    async def _poll_coinbase_premium(self, _coin: CoinConfig):
        from polls.macro import poll_coinbase_premium
        await poll_coinbase_premium(self._cg, self._states, self._settings.supported_coins)

    async def _poll_stablecoin_mcap(self, _coin: CoinConfig):
        from polls.macro import poll_stablecoin_mcap
        await poll_stablecoin_mcap(self._cg, self._states, self._settings.supported_coins)

    async def _poll_oi_exchange_rank(self, coin: CoinConfig):
        from polls.derivatives import poll_oi_exchange_rank
        await poll_oi_exchange_rank(self._cg, coin, self._states[coin.ccy])

    async def _poll_global_liq(self, _coin: CoinConfig):
        from polls.liquidation import poll_global_liq
        await poll_global_liq(self._cg, self._settings.supported_coins, self._states)

    async def _poll_macro_index(self, _coin: CoinConfig):
        from polls.macro import poll_macro_index
        await poll_macro_index(self._cg, self._states, self._settings.supported_coins)

    async def _poll_bbx_index(self, _coin: CoinConfig):
        from polls.bbx_index import poll_bbx_index
        await poll_bbx_index(self._bbx, self._states, self._settings.supported_coins)

    async def _poll_onchain_cycle(self, _coin: CoinConfig):
        from polls.macro import poll_onchain_cycle
        await poll_onchain_cycle(self._cg, self._states, self._settings.supported_coins)

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

        # V2 关键位先行：独立于 calculate_levels，产出信号供后者桥接
        self._recompute_key_levels_v2(ccy)

        kl_signals = None
        if state.key_level_snapshot_v2:
            kl_signals = state.key_level_snapshot_v2.signals or None

        vwap = state.vp.vwap if state.vp else 0
        liq_map_7d = state.liq_maps.get("7d")
        hist_vol = state.market_index.btc_hist_vol if state.market_index else None
        state.levels = calculate_levels(
            coin=ccy, current_price=price, liq_map=liq_map,
            vp=state.vp, orderbook=state.orderbook,
            atr=state.atr, vwap=vwap,
            liq_map_7d=liq_map_7d, btc_hist_vol=hist_vol,
            cycle_position=state.cycle_position,
            kl_signals=kl_signals,
        )

        self._recompute_range_signal(ccy)

        if self._notif_cfg.enabled:
            asyncio.ensure_future(self._check_alerts(ccy))

    async def _check_alerts(self, ccy: str):
        """检测 S/A 级信号变化并发送邮件通知。"""
        try:
            # 配置完整性前置闸门：enabled=True 但 SMTP 未填，则完全跳过扫描
            # 避免"每 5 秒匹配 → 发送失败 → 不锁冷却 → 下轮再匹配"的日志刷屏死循环
            if not self._notif_cfg.smtp_user or not self._notif_cfg.to:
                if not self._alert_config_warned:
                    logger.warning(
                        "[alert] email config incomplete, scanning disabled | "
                        "smtp_user=%s recipients=%d | 修复 .env 后重启后端生效",
                        "<empty>" if not self._notif_cfg.smtp_user else "<set>",
                        len(self._notif_cfg.to or []),
                    )
                    self._alert_config_warned = True
                return

            from notifications.signal_monitor import scan_alerts
            from notifications.email_alert import send_alert_email

            state = self._states[ccy]
            price = state.ticker.last if state.ticker else 0
            if price <= 0:
                return

            events = scan_alerts(
                coin=ccy,
                price=price,
                kl_snapshot=state.key_level_snapshot_v2,
                range_signal=state.range_signal,
                min_tier=self._notif_cfg.min_signal_tier,
                include_key_levels=self._notif_cfg.include_key_levels,
                include_range=self._notif_cfg.include_range,
            )

            sent = 0
            cooled = 0
            failed = 0
            for event in events:
                if not self._alert_dedup.should_send(event.dedup_key):
                    cooled += 1
                    continue
                ok = await send_alert_email(event, self._notif_cfg)
                if ok:
                    self._alert_dedup.mark_sent(event.dedup_key)
                    sent += 1
                else:
                    # 发送失败不占冷却位：SMTP 恢复后下一轮即可重试，
                    # 避免"配置缺失/网络抖动→静默锁 45 分钟"的可靠性坑
                    failed += 1

            # 诊断日志：每次触发都打印扫描结果，便于排查"为什么没收到邮件"
            if events or sent:
                logger.info(
                    "[alert] ccy=%s min_tier=%s scanned_signals=%d matched=%d cooled=%d sent=%d failed=%d",
                    ccy,
                    self._notif_cfg.min_signal_tier,
                    len(state.key_level_snapshot_v2.signals) if state.key_level_snapshot_v2 and state.key_level_snapshot_v2.signals else 0,
                    len(events),
                    cooled,
                    sent,
                    failed,
                )

            self._alert_dedup.cleanup()
        except Exception:
            logger.debug("_check_alerts error", exc_info=True)

    _KL_V2_DISCOVERY_INTERVAL = 60

    def _recompute_key_levels_v2(self, ccy: str):
        from processors.key_level_tracker_v2 import run_tracker_v2

        state = self._states[ccy]
        price = state.ticker.last if state.ticker else 0
        if price <= 0:
            return

        now = time.time()
        need_full = (now - state._kl_v2_discovery_ts) >= self._KL_V2_DISCOVERY_INTERVAL

        if need_full:
            snapshot_v2 = self._run_kl_v2_discovery(ccy, price)
            state._kl_v2_discovery_ts = now
        else:
            snapshot_v2 = state.key_level_snapshot_v2
            if not snapshot_v2:
                return
            snapshot_v2.current_price = price
            for lv in snapshot_v2.levels:
                lv.distance_pct = round(abs(lv.price - price) / price * 100, 4) if price else 0

        liq_map = state.liq_maps.get("1d") or state.liq_maps.get("24h")
        cutoff = int(now) - 3600
        recent_sweeps = [
            e for e in state.liq_sweep_events if e.get("ts", 0) > cutoff
        ]

        taker_buy = 0.0
        taker_sell = 0.0
        if state.taker_flow:
            taker_buy = state.taker_flow.contract_buy_vol + state.taker_flow.spot_buy_vol
            taker_sell = state.taker_flow.contract_sell_vol + state.taker_flow.spot_sell_vol

        temp_score = state.temperature.score if state.temperature else 50
        oi_change_1h = state.oi.change_1h_pct if state.oi else 0
        kl_cfg = self._settings.processors.key_level_tracker

        snapshot_v2 = run_tracker_v2(
            snapshot=snapshot_v2,
            liq_map=liq_map,
            sweep_events_1h=recent_sweeps,
            taker_buy_vol=taker_buy,
            taker_sell_vol=taker_sell,
            oi_change_pct_1h=oi_change_1h,
            temperature_score=temp_score,
            candles_4h=state.candles_4h or None,
            candles_15m=state.candles_15m or None,
            cfg=kl_cfg if kl_cfg else None,
        )

        state.key_levels_v2 = snapshot_v2.levels
        state.key_level_snapshot_v2 = snapshot_v2

    def _run_kl_v2_discovery(self, ccy: str, price: float) -> "KeyLevelSnapshotV2":
        from processors.level_discovery import discover_levels
        from processors.confluence_scoring import score_and_build_snapshot

        state = self._states[ccy]
        liq_map = state.liq_maps.get("1d") or state.liq_maps.get("24h")
        liq_map_7d = state.liq_maps.get("7d")
        vwap = state.vp.vwap if state.vp else 0

        oi_hist = None
        if state.oi and state.oi.history:
            oi_hist = [{"ts": s.ts, "oi": s.oi_usd} for s in state.oi.history]

        discovery = discover_levels(
            current_price=price,
            atr=state.atr,
            candles_4h=state.candles_4h or None,
            candles_1d=state.candles_daily or None,
            candles_1w=state.candles_weekly or None,
            liq_map=liq_map,
            liq_map_7d=liq_map_7d,
            vp=state.vp,
            orderbook=state.orderbook,
            ema_daily=state.ema_daily if state.ema_daily else None,
            sma200_daily=state.sma200_daily_cg,
            boll_data=state.boll_data,
            boll_4h_data=state.boll_4h_data,
            vwap=vwap,
            cycle_position=state.cycle_position,
            range_signal=state.range_signal,
            oi_history=oi_hist,
        )

        macd_hist = None
        if state.macd_data:
            macd_hist = state.macd_data.get("histogram")

        return score_and_build_snapshot(
            discovery=discovery,
            current_price=price,
            atr=state.atr,
            prev_levels=state.key_levels_v2 or None,
            boll_data=state.boll_data,
            boll_4h_data=state.boll_4h_data,
            macd_histogram=macd_hist,
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
            ob_dict = state.orderbook.model_dump()
            if state.large_orders and state.large_orders.orders:
                from models.market import WallInfo
                bid_walls, ask_walls = [], []
                for o in sorted(state.large_orders.orders, key=lambda x: x.size_usd, reverse=True)[:10]:
                    wall = WallInfo(price=o.price, size=0, size_usd=o.size_usd).model_dump()
                    if o.side == "bid":
                        bid_walls.append(wall)
                    else:
                        ask_walls.append(wall)
                ob_dict["bid_walls"] = bid_walls
                ob_dict["ask_walls"] = ask_walls
            payload["orderbook"] = ob_dict
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
        if state.key_level_snapshot_v2 and state.key_level_snapshot_v2.levels:
            payload["key_levels_v2"] = state.key_level_snapshot_v2.model_dump()

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
            payload["liq_max_pain"] = {k: v.model_dump() for k, v in state.liq_max_pain.items()}
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

    def _is_coin_data_ready(self, ccy: str) -> bool:
        """判断某币种核心数据是否已就绪（ticker + K线 + 指标 + 清算 + OI + 资金费率）。

        注：CPS（链上周期评分）为日级低频指标，首次计算可能需 1 小时以上，
        不纳入此处硬性就绪门，由 `_has_cycle_data` 作为软指标供 auto_ai_loop 权衡。
        """
        state = self._states.get(ccy)
        if not state or not state.ticker:
            return False
        if not state.candles_1h:
            return False
        if state.rsi_14 is None:
            return False
        if not state.liq_maps.get("1d") and not state.liq_maps.get("24h"):
            return False
        if state.oi is None:
            return False
        if state.funding is None:
            return False
        return True

    def _has_cycle_data(self, ccy: str) -> bool:
        """软指标：CPS 是否已算出。BTC 才有；ETH/SOL 跳过。"""
        if ccy != "BTC":
            return True
        state = self._states.get(ccy)
        return bool(state and state.cycle_position is not None)

    async def _auto_ai_loop(self, interval_sec: int) -> None:
        """定时自动触发 AI 分析（所有支持的币种）。

        启动阶段策略：
        1. 先等硬指标（ticker/K线/指标/清算/OI/资金费率）就绪，最多 5 分钟
        2. 硬指标 OK 后若 CPS 仍未算出，再给它一个"优雅等待期"（最多额外 3 分钟），
           避免冷启动首轮 AI 分析缺 CPS（上游 onchain poll 是 60min 间隔）
        3. 优雅等待期满仍无 CPS 则降级触发，AI prompt 自带"§9e 未提供"fallback
        """
        await asyncio.sleep(30)
        max_wait = 300
        waited = 30
        default = self._default_coin
        while self._running and waited < max_wait:
            if self._is_coin_data_ready(default):
                break
            logger.info("Auto AI waiting for data readiness | coin=%s waited=%ds", default, waited)
            await asyncio.sleep(15)
            waited += 15

        # 硬指标 OK 后的 CPS 优雅等待：最多额外 180s，有就等、没有不死等
        cps_grace_max = 180
        cps_grace = 0
        while self._running and cps_grace < cps_grace_max:
            if self._has_cycle_data(default):
                break
            logger.info(
                "Auto AI grace-wait for CPS | coin=%s waited=%ds (max=%ds)",
                default, cps_grace, cps_grace_max,
            )
            await asyncio.sleep(30)
            cps_grace += 30

        logger.info(
            "Auto AI analysis loop started | interval=%ds data_ready=%s cps_ready=%s waited=%ds",
            interval_sec,
            self._is_coin_data_ready(default),
            self._has_cycle_data(default),
            waited + cps_grace,
        )
        while self._running:
            for ccy in self._settings.supported_coins:
                if ccy in self._ai_running:
                    continue
                if not self._is_coin_data_ready(ccy):
                    continue
                elapsed = time.time() - self._states[ccy].last_ai_ts if self._states[ccy].last_ai_ts else float("inf")
                if elapsed < interval_sec:
                    continue
                try:
                    await self.fire_ai_analysis(ccy)
                    logger.info("Auto AI analysis triggered | coin=%s", ccy)
                except Exception:
                    logger.error("Auto AI trigger failed | coin=%s", ccy, exc_info=True)
                await asyncio.sleep(5)
            await asyncio.sleep(60)

    async def _auto_kl_snapshot_loop(self, interval_sec: int) -> None:
        """定时保存关键位快照到历史。"""
        await asyncio.sleep(120)
        logger.info("Auto KL snapshot loop started | interval=%ds", interval_sec)
        _last_snap_ts: dict[str, float] = {}
        while self._running:
            for ccy in self._settings.supported_coins:
                state = self._states.get(ccy)
                if not state or not state.key_level_snapshot_v2:
                    continue
                last = _last_snap_ts.get(ccy, 0)
                if time.time() - last < interval_sec:
                    continue
                snap = state.key_level_snapshot_v2
                if snap.ts > 0:
                    state.kl_history.append(snap.model_copy(deep=True))
                    _last_snap_ts[ccy] = time.time()
                    self._save_kl_history()
                    logger.info("KL snapshot saved | coin=%s levels=%d", ccy, len(snap.levels))
            await asyncio.sleep(60)

    async def _digest_email_loop(self) -> None:
        """每日发送回测统计邮件。"""
        from notifications.email_alert import send_backtest_digest
        await asyncio.sleep(300)
        logger.info("Digest email loop started")
        _last_daily = 0
        _last_weekly = 0
        while self._running:
            now = time.time()
            hour_utc = int(datetime.utcfromtimestamp(now).hour)
            digest_hour = getattr(self._settings, '_digest_hour_utc', 0)

            if now - _last_daily > 82800 and hour_utc == digest_hour:
                stats_map = {}
                for ccy in self._settings.supported_coins:
                    st = self.compute_backtest_stats(ccy)
                    if st.get("total_signals", 0) > 0:
                        stats_map[ccy] = st
                if stats_map:
                    try:
                        await send_backtest_digest(stats_map, self._settings.notifications.email, "日报")
                    except Exception:
                        logger.error("Digest email failed", exc_info=True)
                _last_daily = now

                weekday = datetime.utcfromtimestamp(now).weekday()
                if weekday == 0 and now - _last_weekly > 600000:
                    if stats_map:
                        try:
                            await send_backtest_digest(stats_map, self._settings.notifications.email, "周报")
                        except Exception:
                            logger.error("Weekly digest failed", exc_info=True)
                    _last_weekly = now

            await asyncio.sleep(600)

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

        ob_for_ai = state.orderbook
        if ob_for_ai and lo and lo.orders and not ob_for_ai.bid_walls and not ob_for_ai.ask_walls:
            from models.market import WallInfo
            bid_walls, ask_walls = [], []
            for o in sorted(lo.orders, key=lambda x: x.size_usd, reverse=True)[:10]:
                wall = WallInfo(price=o.price, size=0, size_usd=o.size_usd)
                if o.side == "bid":
                    bid_walls.append(wall)
                else:
                    ask_walls.append(wall)
            ob_for_ai = ob_for_ai.model_copy(update={"bid_walls": bid_walls[:5], "ask_walls": ask_walls[:5]})

        snapshot = build_ai_snapshot(
            coin=ccy, price=state.ticker.last,
            high_24h=state.ticker.high_24h, low_24h=state.ticker.low_24h,
            liq_map=state.liq_maps.get("1d") or state.liq_maps.get("24h"),
            cvd_contract=state.cvd_contract,
            cvd_spot=state.cvd_spot, oi=state.oi, funding=state.funding,
            basis=state.basis, orderbook=ob_for_ai, liq_stats=state.liq_stats,
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
            key_level_snapshot_v2=state.key_level_snapshot_v2,
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
            ls_ratio_top_account=state.ls_ratio_top_account.avg_ratio if state.ls_ratio_top_account else None,
            ls_ratio_top_position=state.ls_ratio_top_position.avg_ratio if state.ls_ratio_top_position else None,
            ls_ratio_long_pct=state.ls_ratio_long_pct,
            ls_ratio_short_pct=state.ls_ratio_short_pct,
            ls_ratio_change_24h=state.ls_ratio_change_24h,
            ls_top_acct_long_pct=state.ls_top_acct_long_pct,
            ls_top_acct_short_pct=state.ls_top_acct_short_pct,
            ls_top_acct_change_24h=state.ls_top_acct_change_24h,
            oi_change_24h_pct=state.oi_change_24h_pct,
            fear_greed_prev=state.fear_greed_prev,
            whale_hl_alerts_count=len(state.whale_data.hl_alerts) if state.whale_data else 0,
            whale_transfers_count=len(state.whale_data.transfers) if state.whale_data else 0,
            whale_net_direction=self._calc_whale_direction(state.whale_data) if state.whale_data else "",
            whale_hl_positions=self._build_hl_positions(state.whale_data, ccy),
            coinbase_premium=state.coinbase_premium.current_premium if state.coinbase_premium else 0,
            coinbase_premium_trend=self._calc_cb_premium_trend(state.coinbase_premium),
            stablecoin_total_mcap=state.stablecoin_mcap.current_total if state.stablecoin_mcap else 0,
            stablecoin_7d_change_pct=self._calc_stablecoin_change(state.stablecoin_mcap),
            oi_exchange_rank=state.oi_exchange_rank.get("exchanges", []) if state.oi_exchange_rank else [],
            candles_4h=state.candles_4h or None,
            liq_heatmap=state.liq_heatmaps.get("24h") or state.liq_heatmaps.get("3d"),
            net_position_latest=state.net_position_latest,
            net_position_trend=state.net_position_trend,
            net_position_change_24h=state.net_position_change_24h,
            futures_coin_netflow_1h=state.futures_coin_netflow_1h,
            futures_coin_netflow_trend=state.futures_coin_netflow_trend,
            td_sequential_count=state.td_sequential_count,
            td_sequential_direction=state.td_sequential_direction,
            poll_failures=dict(state.poll_failures),
            market_structure=state.market_structure,
        )

        result = await self._analyzer.analyze(snapshot)
        state.ai_history.append(result)
        state.last_ai_ts = time.time()
        self._save_ai_history()
        return result

    @staticmethod
    def _calc_whale_direction(whale):
        from polls.macro import calc_whale_direction
        return calc_whale_direction(whale)

    @staticmethod
    def _build_hl_positions(whale, ccy):
        from polls.macro import build_hl_positions
        return build_hl_positions(whale, ccy)

    @staticmethod
    def _calc_cb_premium_trend(cb):
        from polls.macro import calc_cb_premium_trend
        return calc_cb_premium_trend(cb)

    @staticmethod
    def _calc_stablecoin_change(sc):
        from polls.macro import calc_stablecoin_change
        return calc_stablecoin_change(sc)

    def get_source_health(self) -> list[dict]:
        daily = self._cg.daily_request_count
        limit = self._settings.coinglass.daily_limit
        usage_pct = round(daily / limit * 100, 1) if limit > 0 else 0
        sources = [
            self._cg.health().model_dump(),
            self._bn.health().model_dump(),
            {
                "name": "coinglass_daily_usage",
                "status": "degraded" if usage_pct > 80 else "connected",
                "daily_requests": daily,
                "daily_limit": limit,
                "usage_pct": usage_pct,
                "latency_ms": 0,
            },
        ]
        if self._bbx:
            sources.append(self._bbx.health())
        sources.append({
            "name": "market_readiness",
            "status": "connected",
            "coins": [
                {
                    "coin": ccy,
                    "ticker_ready": bool(st.ticker),
                    "liquidation_ready": bool(st.liq_maps.get("1d") or st.liq_maps.get("24h")),
                    "kline_1h_ready": bool(st.candles_1h),
                    "indicators_ready": st.rsi_14 is not None and bool(st.macd_data) and bool(st.boll_data),
                    "market_structure_ready": st.market_structure is not None,
                }
                for ccy, st in self._states.items()
            ],
        })
        return sources
