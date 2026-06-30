"""
主引擎：调度 Coinglass 统一数据源轮询 + 处理 + 缓存 + 推送。
每个币种运行独立的数据管线，互不干扰。
所有数据通过 Coinglass V4 API 获取，纯 REST 轮询架构。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

# PR-3 · ai.analyzer 已下线（旧 Trader）。Strategic AI 走 ai.strategic_arbiter。
from ai.snapshot import build_ai_snapshot
from api.ws import push_roll_signal, push_to_coin
from config.settings import CoinConfig, get_settings
from models.flow import (
    BasisData, CVDData, CVDPoint, CyclePositionData, ETFFlowData, ETFFlowDay,
    FundingRateData, GlobalLiquidationData, LongShortRatioData,
    LongShortRatioExchange, MarketIndexData, MultiFundingRateData,
    ExchangeFundingRate, OIData, OISnapshot, RangeSignalData, TakerFlowData,
)
from models.key_level import KeyLevelSnapshotV2
from models.market_structure import MarketStructure
from models.liquidation import (
    HeatmapData, LiqHistoryData, LiqMaxPainData, LiqMaxPainItem,
    LiquidationMap, LiquidationStats,
)
from models.macro import (
    CoinbasePremiumData, NewsData, StablecoinMcapData,
)
from models.market import OrderBookAnalysis, TickerData, VolumeProfileData
from models.options import OptionInfoData, OptionMaxPainData
from models.orderbook_ext import LargeOrderSnapshot
from models.snapshot import (
    MarketTemperature, WaterfallData,
)
from models.whale import WhaleData
from processors.cvd import detect_cvd_price_divergence
# PR-3 · processors.levels 已下线（数学引擎核心被 Strategic AI 取代）
from processors.liquidation import detect_liq_sweep, process_liquidation_map
from processors.market_temp import build_waterfall, calc_market_temperature
from processors.percentile import PercentileTracker
from processors.volume_profile import calc_volume_profile
from processors.cycle import calculate_cycle_position
from processors.range_signal import calculate_range_signal
from sources.coinglass import CoinglassSource, create_coinglass_source
from sources.binance_futures import BinanceFuturesSource, create_binance_source
from sources.coinbase_native import CoinbaseNativeSource

logger = logging.getLogger(__name__)


def _pick_max_pain_for_coin(
    pain_data: Optional[LiqMaxPainData], ccy: str,
) -> Optional[LiqMaxPainItem]:
    """从 LiqMaxPainData.items 中按 symbol 提取当前币种的 LiqMaxPainItem。

    poll 层用 supported_coins 过滤后写入的 items 里仅含 BTC/ETH/SOL 三条；
    此处再做一次按 symbol 取值，避免 BTC 看到 ETH 的痛点。
    """
    if pain_data is None or not pain_data.items:
        return None
    for it in pain_data.items:
        if it.symbol == ccy:
            return it
    return None


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
        # PR-3 · self.levels 已下线（数学引擎 calculate_levels 删除，前端走 KeyLevelV3）。
        # 历史 _recompute / /api/snapshot 中的 levels 路径全部移除
        self.liq_stats: Optional[LiquidationStats] = None
        self.candle_prices: list[float] = []
        self.candle_ts: list[int] = []
        self.candles_1h: list = []
        self.candles_15m: list = []
        self.oi_history: deque = deque(maxlen=720)
        # PR-3 · ai_history / last_ai_ts 已下线（旧 Trader 历史）。
        # Strategic 走 strategic_history / strategic_last_ts。
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
        # MTF 扩展（日线 / 周线级别价格结构，用于 AI MTF 一致性判定）
        # - 由 poll_candles_daily / poll_candles_weekly 成功后同步 recompute
        # - 防过拟合：仅作为 AI 偏置输入，不进入决策硬门（D15 融合层暂不消费）
        self.market_structure_1d: Optional[MarketStructure] = None
        self.market_structure_1w: Optional[MarketStructure] = None
        self._prev_ms_summary_1d: tuple = ()
        self._prev_ms_summary_1w: tuple = ()
        # L2 MarketRegime 快照（D01）。由 _recompute 末尾写入
        from models.regime import RegimeSnapshot  # local import 避免顶部循环
        self.regime_snapshot: Optional[RegimeSnapshot] = None
        # 趋势衰竭信号（Phase 1，独立 processor，与 range_signal / key_level_v2 正交）
        from models.trend_exhaustion import TrendExhaustionSignal as _TrendExhaustionSignal
        self.trend_exhaustion: Optional[_TrendExhaustionSignal] = None
        # 规则引擎 8 维方向共识（独立 processor，纯聚合既有字段，零 I/O）
        from models.direction_vote import DirectionVoteSummary as _DirectionVoteSummary
        self.direction_vote: Optional[_DirectionVoteSummary] = None
        # PR-3 · execution_plan / ai_trader_report / final_decision / last_fusion_ts 已下线
        # 旧 Trader/Fusion/Math 引擎链路被 Strategic AI 全程决策官取代。
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
        # ── Orderbook Pressure Monitor (盘口订单流仪表盘，辅助参考) ──
        # 由 polls/orderflow.poll_large_orders 写入：snapshot active + history ended 合并去重
        from models.orderbook_pressure import (
            LargeOrderLifecycle as _LargeOrderLifecycle,
            OrderbookDepthSnapshot as _OrderbookDepthSnapshot,
            OrderbookPressureSnapshot as _OrderbookPressureSnapshot,
        )
        self.large_orders_history: list[_LargeOrderLifecycle] = []
        # M2.5：现货大单（与 large_orders_history 互补——区分真支撑 vs 合约清算磁铁）
        self.spot_large_orders_history: list[_LargeOrderLifecycle] = []
        # 由 polls/orderbook_pressure.poll_orderbook_pressure 写入（90s 间隔）
        self.orderbook_depth_snapshot: Optional[_OrderbookDepthSnapshot] = None
        # M1 滚动深度历史 —— WallZone 持续性评分基础。
        # 档位 2A：maxlen=100（Coinglass 文档上限，5m × 100 ≈ 8.3h 完整窗口）
        #   · 下游 _compute_zone_history_stats 按 ts_sec 真截窗为 1h / 8h 双窗口
        #   · max_usd_1h 严格只看过去 60min，max_usd_8h 看过去 480min
        #   · 同一次 API 调用拉 100 帧，不增加配额成本
        self.orderbook_depth_history: deque[_OrderbookDepthSnapshot] = deque(maxlen=100)
        # Phase A：现货 5m 深度热力图独立 deque（与合约 history 同结构、同窗口）
        # 由 polls/orderbook_pressure.poll_spot_orderbook_pressure 写入
        # 用于 liquidity_wall_engine 双源 zone 检测（spot+depth = 💎 双源高可信墙）
        self.spot_orderbook_depth_history: deque[_OrderbookDepthSnapshot] = deque(maxlen=100)
        # Phase B：合约多家聚合 ±range 流动性时序（与 heatmap 互补）
        # 由 polls/orderbook_pressure.poll_aggregated_ask_bids_history 写入
        # 用于 _compute_active_attack_score 的"宏观流动性衰竭"因子
        from models.orderbook_pressure import AskBidsRangeSnapshot as _AskBidsRangeSnapshot
        self.aggregated_ask_bids_history: deque[_AskBidsRangeSnapshot] = deque(maxlen=12)
        # Phase B+：现货多家聚合 ±range 流动性时序（语义更强：真买卖家撤单）
        # 现货抽流动性 → active_attack_score 衰竭因子优先取此源；为空时 fallback 合约
        self.spot_aggregated_ask_bids_history: deque[_AskBidsRangeSnapshot] = deque(maxlen=12)
        # Phase C：Coinbase 现货原生订单簿（机构资金独立验证维度，不走 Coinglass）
        # 由 polls/coinbase_orderbook.poll_coinbase_orderbook 写入；仅存 latest 帧
        # liquidity_wall_engine 的 _augment_zones_with_coinbase 消费此字段
        from models.coinbase_orderbook import CoinbaseOrderbookFrame as _CoinbaseOrderbookFrame
        self.coinbase_orderbook: Optional[_CoinbaseOrderbookFrame] = None
        # 由 _recompute 末尾调用 compute_pressure_snapshot 写入
        self.orderbook_pressure_snapshot: Optional[_OrderbookPressureSnapshot] = None
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
        # ── Market Action Analyzer (MAA) 新增字段 · v4 ──
        # poll 层写入的原始/序列数据（不破坏现有字段）
        self.basis_history: deque = deque(maxlen=60)        # {ts, basis_pct}，60s 粒度近 1h
        self.orderbook_series: list = []                    # 近 12 点 5m [{ts,bid_usd,ask_usd,spread_pct}]
        self.taker_contract_series: list = []               # 近 12 点 5m [{ts,buy_usd,sell_usd,delta_usd}]
        self.taker_spot_series: list = []
        self.footprint_contract: deque = deque(maxlen=3)    # 原始 footprint bars（dict 结构，含 buckets）
        self.footprint_spot: deque = deque(maxlen=3)
        self.footprint_last_ts: Optional[int] = None
        # 现货抄底独立新鲜度；旧 footprint_last_ts 继续保持 MAA 的合约语义。
        self.footprint_spot_last_ts: Optional[int] = None
        # ── MAA P0 增强：funding 8h 结算点历史 + OI 30d hourly 历史 ──
        # funding_history_8h：[{ts_sec, rate}]，由 poll_funding_history_8h 写入（5min 调用一次）
        #   - 21 点 = 7 天 × 3 次 8h 结算
        #   - 用于派生 avg_7d / cost_24h_usd / days_negative_streak / sign_flip_7d
        #   - 90 点 = 30 天 × 3 个 8h 结算点，用于 percentile_30d 极值上下文
        # oi_hourly_history：[{ts_sec, oi_usd}]，由 poll_oi_hourly_30d 写入
        #   - 720 点 = 30 天 × 1h；用于派生 percentile_30d_hourly / is_near_local_high_7d
        from collections import deque as _deque
        # maxlen=90 = 30 天 × 3 个 8h 结算点：
        #   - 上限 90 既覆盖完整 30d 百分位，也保留 sign_flip_7d 所需的 42 点窗口
        #   - 旧版本为 60（20d），扩容向后兼容（其他算法都用切片，只看尾部 21/42 点）
        self.funding_history_8h: _deque = _deque(maxlen=90)
        self.oi_hourly_history: _deque = _deque(maxlen=720)
        # 期权派生字段（仅 BTC/ETH 生效，SOL 保持 None）
        self.option_pcr_oi: Optional[float] = None
        self.option_magnet_price: Optional[float] = None
        self.option_oi_change_24h_pct: Optional[float] = None
        self.option_vol_change_24h_pct: Optional[float] = None
        # 最新一次 MAA 分析结果缓存
        self.market_action_report: Optional[Any] = None
        self.market_action_last_ts: float = 0.0
        # MAA 历史（最多 max_history，实际由 settings.market_action.max_history 决定）
        self.market_action_history: deque = deque(maxlen=max_history)
        # PR-2 · Strategic AI 决策官最新报告 + 历史
        # 容量稍后由 settings.strategic.max_history 在 Engine.__init__ 重置
        self.strategic_report: Optional[Any] = None
        self.strategic_last_ts: float = 0.0
        self.strategic_history: deque = deque(maxlen=max_history)
        # 历史对比字段
        self.ls_ratio_change_24h: Optional[float] = None
        self.ls_ratio_long_pct: Optional[float] = None
        self.ls_ratio_short_pct: Optional[float] = None
        self.ls_top_acct_long_pct: Optional[float] = None
        self.ls_top_acct_short_pct: Optional[float] = None
        self.ls_top_acct_change_24h: Optional[float] = None
        self.oi_change_24h_pct: Optional[float] = None
        self.fear_greed_prev: Optional[int] = None
        # Trading Brain v3：高盈亏比观察区状态机持久化（修复 P1-A 伪状态机）
        # key=setup_id, value=SetupState；由 GET /api/trading-brain/{coin} 写回。
        # zone_id 已在 P1-B 改为 price-bucket+role_group 稳定哈希，
        # setup_id (= sha1(coin|zone_id|setup_type)) 也跨帧稳定，可作字典 key。
        # 写回时按"当帧仍存在的 setup_id"做 GC，避免字典无限增长。
        from models.trading_brain import SetupState as _BrainSetupState
        self.brain_setup_states: dict[str, _BrainSetupState] = {}
        # SMC · Nansen confirmation cache（仅 BTC/ETH 使用；失败只降级 SMC 数据质量）
        self.nansen_perp: Optional[dict] = None
        self.nansen_flow_intelligence: Optional[dict] = None
        self.nansen_exchange_flows: list[dict] = []
        self.nansen_updated_at: dict[str, int] = {}
        self.nansen_errors: dict[str, str] = {}


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
        # Phase C：Coinbase Exchange 公开 REST（仅 orderbook，免 auth，独立 rate limiter）
        self._cb: CoinbaseNativeSource = CoinbaseNativeSource(
            base_url=self._settings.coinbase.base_url,
            timeout_sec=self._settings.coinbase.timeout_sec,
            rate_per_min=self._settings.coinbase.rate_per_min,
        )
        from sources.nansen import create_nansen_source
        self._nansen = create_nansen_source()
        from sources.looknode import create_looknode_source
        self._looknode = create_looknode_source(self._settings.looknode)
        self._nansen_market_breadth: Optional[dict] = None
        # BTC 原生趋势模块：复用底层客户端和缓存，但不消费 Engine 里的派生结论。
        from processors.trend_service import TrendService

        async def _push_trend(event: str, payload: dict) -> None:
            await push_to_coin("BTC", event, payload)

        self.trend_service = TrendService(
            coinglass=self._cg, binance=self._bn, looknode=self._looknode,
            settings=self._settings,
            push_callback=_push_trend,
        )
        # PR-3 · self._analyzer 已下线（旧 Trader）。Strategic 通过 _get_strategic_arbiter 懒加载。
        # MAA arbiter（懒创建：只在需要时导入，不影响旧链路）
        self._maa_arbiter = None  # type: ignore[assignment]
        # PR-2 · Strategic AI arbiter（懒创建，与 MAA 相同模式）
        self._strategic_arbiter = None  # type: ignore[assignment]
        # F-15 · arbiter 初始化失败时的冷却时间戳，避免每次调用都重新 import / 建客户端
        self._strategic_arbiter_init_failed_at: Optional[float] = None
        self._percentile = PercentileTracker()
        self._states: dict[str, CoinState] = {}
        self._running = False
        self._ai_running: set[str] = set()
        self._maa_running: set[str] = set()
        self._strategic_running: set[str] = set()

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
        # PR-3 · _ai_history_file 已下线（旧 Trader 历史持久化）
        self._kl_history_file = os.path.join(self._data_dir, "kl_history.json")
        self._maa_history_file = os.path.join(self._data_dir, "market_action_history.json")
        self._strategic_history_file = os.path.join(self._data_dir, "strategic_history.json")

        # MAA 事后评估（Phase 5）· coin → EvalSummary.to_dict()，由 _auto_maa_eval_loop 周期刷新
        self._maa_eval_summary: dict[str, dict] = {}
        self._maa_eval_last_ts: float = 0.0

        max_hist = self._settings.ai.max_history
        for ccy in self._settings.supported_coins:
            self._states[ccy] = CoinState(ccy, max_history=max_hist)

        # MAA 的历史 deque 容量独立配置（不与 ai max_history 共用）
        maa_max = self._settings.market_action.max_history
        for state in self._states.values():
            state.market_action_history = deque(
                state.market_action_history, maxlen=maa_max,
            )

        # PR-2 · Strategic 历史 deque 容量独立配置
        strat_max = self._settings.strategic.max_history
        for state in self._states.values():
            state.strategic_history = deque(
                state.strategic_history, maxlen=strat_max,
            )

        # PR-3 · self._load_ai_history() 已下线
        self._load_kl_history()
        self._load_market_action_history()
        self._load_strategic_history()

        # S 级信号邮件通知（关键位/箱体 共享同一冷却池）
        from notifications.signal_monitor import AlertDedup
        self._alert_dedup = AlertDedup(
            cooldown_seconds=self._settings.notifications.email.cooldown_minutes * 60,
        )
        self._notif_cfg = self._settings.notifications.email
        self._alert_config_warned = False
        # MAA 强信号独立冷却池（默认 20 min < 普通 45 min）
        # 与普通 dedup 隔离，避免"普通信号占位 → 强信号被压住"
        self._maa_strong_dedup = AlertDedup(
            cooldown_seconds=int(self._notif_cfg.market_action_strong_cooldown_minutes) * 60,
        )

        # ── 滚仓模块 · RollService ──
        # 把 on_signal 回调接到 WS 推送：评估产生信号 → roll_signal event
        from processors.roll_service import RollService
        self._roll_loop_ref: Optional[asyncio.AbstractEventLoop] = None
        self.roll_service = RollService(
            data_dir=self._data_dir,
            on_signal=self._on_roll_signal,
        )
        self._roll_eval_interval_sec: int = 10

        # BTC现货动态抄底：独立服务仅读CoinState，不向旧模块回写任何字段。
        from processors.spot_accumulation_service import SpotAccumulationService
        self.spot_accumulation_service = SpotAccumulationService(
            data_dir=self._data_dir,
            state_getter=lambda: self._states.get("BTC"),
        )
        self._spot_accumulation_push_signature = ""
        self._spot_email_dedup = AlertDedup(cooldown_seconds=900)

    # PR-3 · ai_available / _load_ai_history / _save_ai_history 已下线
    # （旧 Trader 历史持久化）。Strategic 走独立 _save_strategic_history。

    # ── MAA 历史持久化 ────────────────────────────────────────────────────
    def _load_market_action_history(self) -> None:
        if not os.path.exists(self._maa_history_file):
            logger.info("No MAA history file found, starting fresh")
            return
        try:
            from models.market_action import MarketActionReport
            with open(self._maa_history_file, "r", encoding="utf-8") as f:
                raw: dict = json.load(f)
            total = 0
            for ccy, items in raw.items():
                if ccy not in self._states:
                    continue
                for item in items:
                    try:
                        rpt = MarketActionReport(**item)
                        self._states[ccy].market_action_history.append(rpt)
                        self._states[ccy].market_action_report = rpt
                        self._states[ccy].market_action_last_ts = float(
                            rpt.timestamp or 0
                        )
                        total += 1
                    except Exception:
                        continue
            logger.info("MAA history loaded from disk | entries=%d", total)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to load MAA history: %s", e)

    def _save_market_action_history(self) -> None:
        try:
            os.makedirs(self._data_dir, exist_ok=True)
            data: dict[str, list] = {}
            for ccy, state in self._states.items():
                hist = getattr(state, "market_action_history", None)
                if not hist:
                    continue
                data[ccy] = [r.model_dump() for r in hist]
            tmp_path = self._maa_history_file + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
            os.replace(tmp_path, self._maa_history_file)
            logger.debug("MAA history saved | coins=%d", len(data))
        except (OSError, TypeError) as e:
            logger.warning("Failed to save MAA history: %s", e)

    # ── PR-2 · Strategic 历史持久化 ──────────────────────────────────────
    def _load_strategic_history(self) -> None:
        if not os.path.exists(self._strategic_history_file):
            logger.info("No Strategic history file found, starting fresh")
            return
        try:
            from models.strategic_report import AIStrategicReport
            with open(self._strategic_history_file, "r", encoding="utf-8") as f:
                raw: dict = json.load(f)
            total = 0
            for ccy, items in raw.items():
                if ccy not in self._states:
                    continue
                for item in items:
                    try:
                        rpt = AIStrategicReport(**item)
                        self._states[ccy].strategic_history.append(rpt)
                        self._states[ccy].strategic_report = rpt
                        self._states[ccy].strategic_last_ts = float(
                            rpt.timestamp or 0
                        )
                        total += 1
                    except Exception:
                        continue
            logger.info("Strategic history loaded from disk | entries=%d", total)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to load Strategic history: %s", e)

    def _save_strategic_history(self) -> None:
        try:
            os.makedirs(self._data_dir, exist_ok=True)
            data: dict[str, list] = {}
            for ccy, state in self._states.items():
                hist = getattr(state, "strategic_history", None)
                if not hist:
                    continue
                data[ccy] = [r.model_dump() for r in hist]
            tmp_path = self._strategic_history_file + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
            os.replace(tmp_path, self._strategic_history_file)
            logger.debug("Strategic history saved | coins=%d", len(data))
        except (OSError, TypeError) as e:
            logger.warning("Failed to save Strategic history: %s", e)

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

    async def start(self):
        """启动 Coinglass REST 轮询数据管线"""
        self._running = True
        self._roll_loop_ref = asyncio.get_running_loop()
        logger.info(
            "Engine starting (Coinglass) | coins=%s default=%s",
            self._settings.supported_coins, self._default_coin,
        )
        await self.trend_service.start()

        # ── 滚仓模块启动：从磁盘加载 positions + plans + events + settings ──
        try:
            self.roll_service.bootstrap()
            logger.info(
                "[Roll] service ready | positions=%d plans=%d templates=%d",
                len(self.roll_service.store.positions),
                len(self.roll_service.store.plans),
                len(self.roll_service.templates),
            )
        except Exception:
            logger.warning("[Roll] service bootstrap failed", exc_info=True)

        # PR-3 · decision_tracker boot log 已下线（utils/decision_tracker 被删除）

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
                self._poll_cfg.get("liquidation_max_pain", 300), 0.9,
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
            asyncio.create_task(self._poll_loop(
                "cg_spot_accumulation_fast", self._poll_spot_accumulation_fast, btc_coin,
                300, 39,
            )),
            asyncio.create_task(self._poll_loop(
                "cg_spot_accumulation_slow", self._poll_spot_accumulation_slow, btc_coin,
                21600, 72,
            )),
            asyncio.create_task(self._poll_loop(
                "spot_accumulation_evaluate", self._poll_spot_accumulation_evaluate, btc_coin,
                60, 90,
            )),
        ])

        # SMC · Nansen 只做低频确认层：不影响 Coinglass 限流，不阻塞 SMC 主判断。
        if self._nansen is not None:
            nsn_cfg = self._settings.nansen.poll_intervals
            for idx, ccy in enumerate(("BTC", "ETH")):
                if ccy not in self._settings.supported_coins:
                    continue
                coin = self._settings.get_coin(ccy)
                tasks.append(asyncio.create_task(self._poll_loop(
                    f"nansen_perp_{ccy}", self._poll_nansen_perp, coin,
                    nsn_cfg.get("perp_screener", 900), 42 + idx * 3,
                )))
                tasks.append(asyncio.create_task(self._poll_loop(
                    f"nansen_flow_{ccy}", self._poll_nansen_flow_intelligence, coin,
                    nsn_cfg.get("flow_intelligence", 3600), 48 + idx * 3,
                )))
                tasks.append(asyncio.create_task(self._poll_loop(
                    f"nansen_exchange_flows_{ccy}", self._poll_nansen_exchange_flows, coin,
                    nsn_cfg.get("exchange_flows", 14400), 54 + idx * 3,
                )))
            tasks.append(asyncio.create_task(self._poll_loop(
                "nansen_breadth", self._poll_nansen_market_breadth, btc_coin,
                nsn_cfg.get("market_breadth", 14400), 61,
            )))

        for idx, ccy in enumerate(self._settings.supported_coins):
            coin = self._settings.get_coin(ccy)
            stagger = 1.0 + idx * 1.5

            if ccy == self._default_coin:
                tasks.extend(self._create_full_poll_tasks(coin, stagger))

        # PR-3 · _auto_ai_loop 已下线（旧 Trader）。Strategic AI 走独立调度。

        # ── Market Action Analyzer (MAA) 周期循环 ──
        maa_cfg = self._settings.market_action
        if maa_cfg.enabled:
            tasks.append(asyncio.create_task(
                self._auto_market_action_loop(maa_cfg.auto_interval_sec)
            ))
            # Phase 5-A：启动 shadow logger（懒启动也可，但显式启动便于落盘验证）
            try:
                from monitoring.maa_shadow import get_maa_shadow_logger
                get_maa_shadow_logger().start()
            except Exception:
                logger.warning("[MAA-Shadow] start failed", exc_info=True)
            # Phase 5-A：价格 heartbeat 循环（5 分钟一轮，每币去重）
            tasks.append(asyncio.create_task(
                self._auto_maa_heartbeat_loop(interval_sec=300)
            ))
            # Phase 5-B：事后评估循环（30 分钟一轮，按 coin 滚动计算）
            tasks.append(asyncio.create_task(
                self._auto_maa_eval_loop(interval_sec=1800)
            ))
            # P0 增强：funding 8h 历史 + OI 30d hourly 历史（5min / 币一次）
            tasks.append(asyncio.create_task(
                self._auto_maa_history_loop(interval_sec=300)
            ))

        # ── PR-2 · Strategic AI 决策官周期循环（与 MAA 并行，独立配额） ──
        strat_cfg = self._settings.strategic
        if strat_cfg.enabled:
            tasks.append(asyncio.create_task(
                self._auto_strategic_loop(strat_cfg.auto_interval_sec)
            ))

        kl_snap_sec = self._settings.processors.key_level_tracker.get("snapshot_interval_sec", 0)
        if kl_snap_sec > 0:
            tasks.append(asyncio.create_task(self._auto_kl_snapshot_loop(kl_snap_sec)))

        # PR-3 · _digest_email_loop 已下线（依赖旧 Trader 回测）

        # ── D13 · News Intelligence Agent 编排（P1.2b） ──
        try:
            tasks.append(asyncio.create_task(self._news_agent_loop()))
        except Exception:
            logger.debug("[D13] news_agent_loop schedule failed", exc_info=True)

        # ── 滚仓评估循环（每 _roll_eval_interval_sec 秒一轮） ──
        tasks.append(asyncio.create_task(self._roll_eval_loop()))

        await asyncio.gather(*tasks, return_exceptions=True)

    async def _news_agent_loop(self):
        """P1.2b · 启动 News Agent 编排（委托给 news_agent_loop.run_forever）。

        get_context 每次回调提供最新 BTC 价格/历史，供 AI 结构化与价格回填。
        启动前必须注册 D07 新闻源（否则 fetch_all 拿不到任何源，D07 永标 warn）。
        """
        try:
            from processors.news_agent_loop import run_forever
        except Exception:
            logger.warning("[D13] news_agent_loop module unavailable", exc_info=True)
            return

        # 启动时注册 D07 新闻源（yml 不存在时回退到默认 OKX 行业 + 博主两源）
        try:
            from sources.news.registry import get_all as _news_get_all, load_from_yaml as _news_load
            if not _news_get_all():
                yml_path = os.path.join(
                    os.path.dirname(os.path.abspath(__file__)),
                    "config", "news_sources.yml",
                )
                registered = _news_load(yml_path if os.path.exists(yml_path) else None)
                logger.info("[D07] news sources registered count=%d", registered)
        except Exception:
            logger.warning("[D07] news source registration failed", exc_info=True)

        # 启动 Bootstrap Seed：优先从磁盘恢复最近一版简报（重启不丢），
        # 否则写一个 model_used=bootstrap 的种子，让 D09 立即 ok、前端秒显
        # "首轮生成中"，避免 run_news_tick Step 3 串行 enrich 期间用户看到
        # 长时间的"预热中"空白页。
        try:
            from processors.news_brief import ensure_bootstrap_brief
            ensure_bootstrap_brief()
        except Exception:
            logger.warning("[D09] ensure_bootstrap_brief failed", exc_info=True)

        def _get_ctx() -> dict:
            btc_state = self._states.get("BTC")
            price = 0.0
            history: list[dict] = []
            if btc_state is not None:
                if btc_state.ticker is not None:
                    price = float(getattr(btc_state.ticker, "price", 0.0) or 0.0)
                # candle_prices / candle_ts 是 1h/15m K 线收盘价序列（按秒 ts 递增）
                # 可用于新闻价格反应回填（精度足够：±1h / ±2h / ±24h 容差内匹配）
                prices = list(btc_state.candle_prices or [])
                ts_list = list(btc_state.candle_ts or [])
                for t, p in zip(ts_list, prices):
                    try:
                        history.append({"ts": int(t), "price": float(p)})
                    except Exception:
                        continue
            return {
                "running": self._running,
                "current_btc_price": price,
                "price_history": history,
                "target_coins": list(self._active_coins),
            }

        await run_forever(_get_ctx)

    def _create_full_poll_tasks(self, coin: CoinConfig, stagger: float) -> list[asyncio.Task]:
        """为活跃币种创建完整轮询任务集。
        stagger 间隔 0.5s，关键数据优先，8s 内全部启动。
        """
        ccy = coin.ccy
        s = stagger

        # ── Phase B：按币优先级缩放 interval（节流非主力币种） ──
        # priority=1.0 不变；priority=0.5 → interval ×2（节省 50% 配额）
        coin_prio = getattr(self._settings.engine, "coin_priority", None) or {}
        prio = max(0.05, float(coin_prio.get(ccy, 1.0)))
        def _scaled(base: int) -> int:
            return max(1, int(round(base / prio)))

        liq_map_interval = _scaled(self._poll_cfg.get("liquidation_map", 60))
        oi_interval = _scaled(self._poll_cfg.get("oi", 60))
        cvd_interval = _scaled(self._poll_cfg.get("cvd", 60))
        ls_interval = _scaled(self._poll_cfg.get("long_short", 120))
        taker_interval = _scaled(self._poll_cfg.get("taker_volume", 120))
        orderbook_interval = _scaled(self._poll_cfg.get("orderbook", 60))
        liq_history_interval = _scaled(max(self._poll_cfg.get("liquidation_map", 60) * 5, 300))
        large_orders_interval = _scaled(max(self._poll_cfg.get("large_orders", 120), 180))
        spot_large_orders_interval = _scaled(max(
            self._poll_cfg.get("spot_large_orders", self._poll_cfg.get("large_orders", 120)),
            180,
        ))
        heatmap_interval = _scaled(self._poll_cfg.get("liquidation_heatmap", 600))
        heatmap_7d_interval = _scaled(self._poll_cfg.get("liquidation_heatmap_7d", 1800))
        indicators_interval = _scaled(120)
        candles_1d_interval = _scaled(600)
        candles_1w_interval = _scaled(3600)
        candles_4h_interval = _scaled(900)
        candles_15m_interval = _scaled(60)
        oi_rank_interval = _scaled(300)
        net_pos_interval = _scaled(900)
        netflow_interval = _scaled(900)
        td_seq_interval = _scaled(3600)
        footprint_interval = _scaled(self._poll_cfg.get("footprint", 180))
        ob_pressure_interval = _scaled(self._poll_cfg.get("orderbook_pressure", 90))
        spot_ob_pressure_interval = _scaled(self._poll_cfg.get("spot_orderbook_pressure", 120))
        # Phase B：±range 流动性时序（标量时序，不替代 heatmap，180s/coin 足够）
        agg_ask_bids_interval = _scaled(self._poll_cfg.get("aggregated_ask_bids", 180))
        # Phase B+：现货 ±range 流动性时序（与合约对称，180s 同节奏）
        spot_agg_ask_bids_interval = _scaled(
            self._poll_cfg.get("spot_aggregated_ask_bids", 180)
        )
        # Phase C：Coinbase 现货原生订单簿（独立 source，与合约 ob_pressure 同节奏）
        coinbase_orderbook_interval = _scaled(self._settings.coinbase.poll_interval)
        # push_loop 不走 cg API（走内部 _recompute），不应被 priority 节流
        # candles_1h（hard-coded 60s）需要 priority 节流
        candles_1h_interval = _scaled(60)
        basis_interval = _scaled(60)
        # ── Phase B：启动 stagger 跨度从 38s 扩到 ~51s ──
        # cg.FixedIntervalLimiter 是 7s 间隔（rate_limit_per_min: 10）
        # 原 stagger 0.4s 步进会瞬间塞满限速器队列 → 启动 30s 后才"消化完"
        # 现按高/中/低频分组，相邻 stagger ≥ 1.5s，限速器有喘息时间
        return [
            asyncio.create_task(self._poll_loop(
                f"cg_push_{ccy}", self._push_loop, coin, 5, s,
            )),
            # ── 高频组（< 15s）：核心实时数据 ──
            asyncio.create_task(self._poll_loop(
                f"cg_oi_{ccy}", self._poll_oi, coin,
                oi_interval, s + 1.5,
            )),
            asyncio.create_task(self._poll_loop(
                f"cg_liq_{ccy}", self._poll_liquidation_map, coin,
                liq_map_interval, s + 3.0,
            )),
            asyncio.create_task(self._poll_loop(
                f"cg_cvd_{ccy}", self._poll_cvd, coin,
                cvd_interval, s + 4.5,
            )),
            asyncio.create_task(self._poll_loop(
                f"cg_orderbook_{ccy}", self._poll_orderbook_depth, coin,
                orderbook_interval, s + 6.0,
            )),
            asyncio.create_task(self._poll_loop(
                f"cg_taker_{ccy}", self._poll_taker_volume, coin,
                taker_interval, s + 7.5,
            )),
            asyncio.create_task(self._poll_loop(
                f"cg_ls_{ccy}", self._poll_ls_ratio, coin,
                ls_interval, s + 9.0,
            )),
            asyncio.create_task(self._poll_loop(
                f"cg_basis_{ccy}", self._poll_basis, coin,
                basis_interval, s + 10.5,
            )),
            asyncio.create_task(self._poll_loop(
                f"cg_candles_1h_{ccy}", self._poll_candles_1h, coin,
                candles_1h_interval, s + 12.0,
            )),
            asyncio.create_task(self._poll_loop(
                f"cg_candles_15m_{ccy}", self._poll_candles_15m, coin,
                candles_15m_interval, s + 13.5,
            )),
            # ── 中频组（15-30s）：墙观测核心 ──
            asyncio.create_task(self._poll_loop(
                f"cg_orderbook_pressure_{ccy}", self._poll_orderbook_pressure, coin,
                ob_pressure_interval, s + 15.0,
            )),
            # Phase A：现货 5m 深度热力图（双源真支撑/真阻力关键源）
            asyncio.create_task(self._poll_loop(
                f"cg_spot_orderbook_pressure_{ccy}", self._poll_spot_orderbook_pressure, coin,
                spot_ob_pressure_interval, s + 16.5,
            )),
            # Phase B：合约多家聚合 ±range 流动性时序（active_attack 流动性衰竭因子）
            asyncio.create_task(self._poll_loop(
                f"cg_agg_ask_bids_{ccy}", self._poll_aggregated_ask_bids_history, coin,
                agg_ask_bids_interval, s + 17.5,
            )),
            # Phase B+：现货多家聚合 ±range 流动性时序（active_attack 现货优先因子）
            asyncio.create_task(self._poll_loop(
                f"cg_spot_agg_ask_bids_{ccy}", self._poll_spot_aggregated_ask_bids_history, coin,
                spot_agg_ask_bids_interval, s + 19.0,
            )),
            # Phase C：Coinbase 现货原生 orderbook（机构资金独立验证维度，不走 Coinglass）
            asyncio.create_task(self._poll_loop(
                f"cb_orderbook_{ccy}", self._poll_coinbase_orderbook, coin,
                coinbase_orderbook_interval, s + 20.0,
            )),
            asyncio.create_task(self._poll_loop(
                f"cg_indicators_{ccy}", self._poll_indicators, coin,
                indicators_interval, s + 18.0,
            )),
            asyncio.create_task(self._poll_loop(
                f"cg_large_orders_{ccy}", self._poll_large_orders, coin,
                large_orders_interval, s + 19.5,
            )),
            # M2.5：现货大单（与合约大单互补——区分真支撑 vs 清算磁铁）
            asyncio.create_task(self._poll_loop(
                f"cg_spot_large_orders_{ccy}", self._poll_spot_large_orders, coin,
                spot_large_orders_interval, s + 21.0,
            )),
            asyncio.create_task(self._poll_loop(
                f"cg_heatmap_24h_{ccy}", self._poll_liq_heatmap_24h, coin,
                heatmap_interval, s + 22.5,
            )),
            asyncio.create_task(self._poll_loop(
                f"cg_candles_4h_{ccy}", self._poll_candles_4h, coin,
                candles_4h_interval, s + 24.0,
            )),
            asyncio.create_task(self._poll_loop(
                f"cg_oi_rank_{ccy}", self._poll_oi_exchange_rank, coin,
                oi_rank_interval, s + 25.5,
            )),
            asyncio.create_task(self._poll_loop(
                f"cg_liq_history_{ccy}", self._poll_liq_history, coin,
                liq_history_interval, s + 27.0,
            )),
            # ── MAA · Footprint（合约+现货足迹图）──
            asyncio.create_task(self._poll_loop(
                f"cg_footprint_{ccy}", self._poll_footprint, coin,
                footprint_interval, s + 28.5,
            )),
            # ── 低频组（30-51s）：日级 / 周级 / 历史 ──
            asyncio.create_task(self._poll_loop(
                f"cg_candles_1d_{ccy}", self._poll_candles_daily, coin,
                candles_1d_interval, s + 30.0,
            )),
            asyncio.create_task(self._poll_loop(
                f"cg_candles_1w_{ccy}", self._poll_candles_weekly, coin,
                candles_1w_interval, s + 33.0,
            )),
            asyncio.create_task(self._poll_loop(
                f"cg_heatmap_7d_{ccy}", self._poll_liq_heatmap_7d, coin,
                heatmap_7d_interval, s + 36.0,
            )),
            asyncio.create_task(self._poll_loop(
                f"cg_net_pos_{ccy}", self._poll_net_position, coin,
                net_pos_interval, s + 39.0,
            )),
            asyncio.create_task(self._poll_loop(
                f"cg_coin_netflow_{ccy}", self._poll_futures_coin_netflow, coin,
                netflow_interval, s + 42.0,
            )),
            asyncio.create_task(self._poll_loop(
                f"cg_td_seq_{ccy}", self._poll_td_sequential, coin,
                td_seq_interval, s + 45.0,
            )),
        ]

    async def stop(self):
        self._running = False
        await self.trend_service.stop()
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
            self._save_kl_history()
            logger.info("KL history saved to disk before shutdown")
        except Exception:
            logger.error("Failed to save KL history on shutdown", exc_info=True)
        try:
            self._save_market_action_history()
            logger.info("MAA history saved to disk before shutdown")
        except Exception:
            logger.error("Failed to save MAA history on shutdown", exc_info=True)
        try:
            self._save_strategic_history()
            logger.info("Strategic history saved to disk before shutdown")
        except Exception:
            logger.error("Failed to save Strategic history on shutdown", exc_info=True)
        # Phase 5-A · 关停 MAA shadow writer（flush 队列后写盘）
        try:
            from monitoring.maa_shadow import get_maa_shadow_logger
            await get_maa_shadow_logger().stop()
            logger.info("[MAA-Shadow] writer stopped")
        except Exception:
            logger.debug("[MAA-Shadow] stop failed", exc_info=True)
        # 滚仓模块：最后一次落盘（确保关闭前事件全部可恢复）
        try:
            if self.roll_service._initialized:  # type: ignore[attr-defined]
                self.roll_service.persist_store()
                logger.info("[Roll] store persisted before shutdown")
        except Exception:
            logger.error("[Roll] persist on shutdown failed", exc_info=True)
        await self._cg.close()
        await self._bn.close()
        await self._bbx.close()
        await self._cb.close()
        if self._nansen is not None:
            await self._nansen.close()
        if self._looknode is not None:
            await self._looknode.close()
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

    async def _poll_liq_heatmap_24h(self, coin: CoinConfig):
        from polls.liquidation import poll_liq_heatmap
        await poll_liq_heatmap(self._cg, coin, self._states[coin.ccy], ranges=("24h",))

    async def _poll_liq_heatmap_7d(self, coin: CoinConfig):
        from polls.liquidation import poll_liq_heatmap
        await poll_liq_heatmap(self._cg, coin, self._states[coin.ccy], ranges=("7d",))

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

    async def _poll_footprint(self, coin: CoinConfig):
        """MAA · 合约+现货足迹图"""
        from polls.footprint import poll_footprint
        await poll_footprint(self._cg, coin, self._states[coin.ccy])

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Phase 4: 新维度
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    async def _poll_options(self, _coin: CoinConfig):
        from polls.macro import poll_options
        await poll_options(self._cg, self._states, self._settings.supported_coins)

    async def _poll_large_orders(self, coin: CoinConfig):
        from polls.orderflow import poll_large_orders
        await poll_large_orders(self._cg, coin, self._states[coin.ccy])

    async def _poll_spot_large_orders(self, coin: CoinConfig):
        """现货大单 lifecycle（M2.5）。区分真支撑 vs 合约清算磁铁。"""
        from polls.orderflow import poll_spot_large_orders
        await poll_spot_large_orders(self._cg, coin, self._states[coin.ccy])

    async def _poll_orderbook_pressure(self, coin: CoinConfig):
        """挂单压力监测器：拉 /orderbook/history 分价位深度。

        大单 lifecycle 由 _poll_large_orders 写入，本 poll 不重复请求。
        """
        from polls.orderbook_pressure import poll_orderbook_pressure
        await poll_orderbook_pressure(self._cg, coin, self._states[coin.ccy])

    async def _poll_spot_orderbook_pressure(self, coin: CoinConfig):
        """Phase A：现货 5m 深度热力图（/api/spot/orderbook/history）。

        与合约 ``_poll_orderbook_pressure`` 互补：合约源体现杠杆资金，现货源体现真买卖家。
        两者价区共振 → liquidity_wall_engine 标 ``dual_source=True``，
        前端显示"💎 双源高可信墙"（trust_score 阶梯加分最强单一证据）。
        """
        from polls.orderbook_pressure import poll_spot_orderbook_pressure
        await poll_spot_orderbook_pressure(self._cg, coin, self._states[coin.ccy])

    async def _poll_aggregated_ask_bids_history(self, coin: CoinConfig):
        """Phase B：合约多家聚合 ±range 流动性时序。

        ``/api/futures/orderbook/aggregated-ask-bids-history`` 一个端点拿到
        Binance + OKX + Bybit 三家合并 ±2% 内 ask/bid 总 USD 时序。
        与 5m heatmap 互补——后者定位精确价位，本接口反映宏观流动性变化。
        """
        from polls.orderbook_pressure import poll_aggregated_ask_bids_history
        await poll_aggregated_ask_bids_history(self._cg, coin, self._states[coin.ccy])

    async def _poll_spot_aggregated_ask_bids_history(self, coin: CoinConfig):
        """Phase B+：现货多家聚合 ±range 流动性时序。

        ``/api/spot/orderbook/aggregated-ask-bids-history`` 默认聚合
        Binance + OKX + Coinbase（现货流动性最稳的三家）。
        active_attack_score 衰竭因子优先取此源——现货抽流动性是真买卖家撤单。
        """
        from polls.orderbook_pressure import poll_spot_aggregated_ask_bids_history
        await poll_spot_aggregated_ask_bids_history(self._cg, coin, self._states[coin.ccy])

    async def _poll_coinbase_orderbook(self, coin: CoinConfig):
        """Phase C：Coinbase 现货原生订单簿（机构资金独立验证维度）。

        走 Coinbase Exchange 公开 REST（免 auth），独立 rate limiter，不消耗
        Coinglass 配额。墙引擎 ``_augment_zones_with_coinbase`` 消费此源叠加
        ``coinbase_spot_confluence`` → trust_score +0.10 独立加分。
        """
        from polls.coinbase_orderbook import poll_coinbase_orderbook
        await poll_coinbase_orderbook(self._cb, coin, self._states[coin.ccy])

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

    async def _poll_spot_accumulation_fast(self, _coin: CoinConfig):
        await self.spot_accumulation_service.poll_fast(self._cg)

    async def _poll_spot_accumulation_slow(self, _coin: CoinConfig):
        await self.spot_accumulation_service.poll_slow(self._cg)

    async def _poll_spot_accumulation_evaluate(self, _coin: CoinConfig):
        snapshot = self.spot_accumulation_service.evaluate_safe()
        if snapshot is None:
            return
        signature_payload = {
            "scores": snapshot.facts.scores.model_dump(),
            "opportunities": [
                (item.opportunity_id, item.status, item.reserved_usdt, item.filled_usdt)
                for item in snapshot.opportunities
            ],
            "btc": snapshot.portfolio.total_btc,
            "cash": snapshot.portfolio.total_cash_usdt,
        }
        signature = hashlib.sha1(
            json.dumps(signature_payload, sort_keys=True).encode()
        ).hexdigest()
        if signature != self._spot_accumulation_push_signature:
            self._spot_accumulation_push_signature = signature
            await push_to_coin(
                "BTC", "spot_accumulation_update", snapshot.model_dump(mode="json")
            )
        if self._notif_cfg.enabled and self.spot_accumulation_service.config.email_notifications:
            from notifications.email_alert import send_spot_accumulation_email
            for opportunity in self.spot_accumulation_service.pending_email_notifications():
                key = f"spot_accumulation:{opportunity.opportunity_id}"
                if not self._spot_email_dedup.should_send(key):
                    continue
                if await send_spot_accumulation_email(opportunity, snapshot, self._notif_cfg):
                    self._spot_email_dedup.mark_sent(key)
                    self.spot_accumulation_service.mark_email_notification_sent(
                        opportunity.opportunity_id,
                    )

    async def _poll_nansen_perp(self, coin: CoinConfig):
        """SMC · Nansen perp screener（BTC/ETH 15min，确认层）。"""
        if self._nansen is None or coin.ccy not in {"BTC", "ETH"}:
            return
        state = self._states[coin.ccy]
        data = await self._nansen.fetch_perp_screener(coin.ccy)
        if data:
            state.nansen_perp = data
            state.nansen_updated_at["perp_screener"] = int(time.time())
            state.nansen_errors.pop("perp_screener", None)
        else:
            state.nansen_errors["perp_screener"] = self._nansen.last_error or "empty"

    async def _poll_nansen_flow_intelligence(self, coin: CoinConfig):
        """SMC · Nansen TGM flow-intelligence（WBTC/WETH 60min，确认层）。"""
        if self._nansen is None or coin.ccy not in {"BTC", "ETH"}:
            return
        token_map = {
            "BTC": ("WBTC", "0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599"),
            "ETH": ("WETH", "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"),
        }
        _symbol, token_address = token_map[coin.ccy]
        state = self._states[coin.ccy]
        data = await self._nansen.fetch_flow_intelligence(
            chain="ethereum",
            token_address=token_address,
            timeframe="1d",
        )
        if data:
            state.nansen_flow_intelligence = data
            state.nansen_updated_at["flow_intelligence"] = int(time.time())
            state.nansen_errors.pop("flow_intelligence", None)
        else:
            state.nansen_errors["flow_intelligence"] = self._nansen.last_error or "empty"

    async def _poll_nansen_exchange_flows(self, coin: CoinConfig):
        """SMC · Nansen TGM exchange flows（WBTC/WETH 4h，主力资金提醒层）。"""
        if self._nansen is None or coin.ccy not in {"BTC", "ETH"}:
            return
        token_map = {
            "BTC": ("WBTC", "0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599"),
            "ETH": ("WETH", "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"),
        }
        _symbol, token_address = token_map[coin.ccy]
        state = self._states[coin.ccy]
        today = datetime.now(timezone.utc).date()
        start = today - timedelta(days=7)
        rows = await self._nansen.fetch_tgm_flows(
            chain="ethereum",
            token_address=token_address,
            label="exchange",
            from_date=start.isoformat(),
            to_date=today.isoformat(),
            per_page=200,
        )
        if rows:
            state.nansen_exchange_flows = rows
            state.nansen_updated_at["exchange_flows"] = int(time.time())
            state.nansen_errors.pop("exchange_flows", None)
            first = rows[0].get("date") if isinstance(rows[0], dict) else None
            last = rows[-1].get("date") if isinstance(rows[-1], dict) else None
            logger.info(
                "Nansen exchange flows ready | coin=%s rows=%d from=%s to=%s",
                coin.ccy,
                len(rows),
                first,
                last,
            )
        else:
            state.nansen_errors["exchange_flows"] = self._nansen.last_error or "empty"

    async def _poll_nansen_market_breadth(self, _coin: CoinConfig):
        """SMC · low-frequency smart-money market breadth."""
        if self._nansen is None:
            return
        chains = self._settings.nansen.chains
        netflow = await self._nansen.fetch_smart_money_netflow(chains, per_page=50)
        tokens = await self._nansen.fetch_token_screener(chains, per_page=50)
        self._nansen_market_breadth = {
            "ts": int(time.time()),
            "chains": chains,
            "smart_money_netflow": netflow,
            "token_screener": tokens,
            "last_error": self._nansen.last_error,
        }

    # ── 重新计算 ──

    def _recompute(self, ccy: str):
        state = self._states[ccy]
        price = state.ticker.last if state.ticker else 0
        if price <= 0:
            return

        _recompute_t0 = time.time()
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

        # ── Orderbook Pressure Monitor（盘口订单流仪表盘，前置于 KL tracker）──
        # 定位：辅助参考工具，不再产出独立 snipe 信号（已砍 OP-Signal 通道）。
        # 数据全部复用 state（depth/large_orders_history/footprint），不触发新 cg 请求。
        # 前置原因：KL tracker 读取本轮 pressure_snapshot 作 tier-based 共振判定。
        op_cfg = self._settings.processors.orderbook_pressure or {}
        try:
            from processors.orderbook_pressure import compute_pressure_snapshot
            state.orderbook_pressure_snapshot = compute_pressure_snapshot(
                state, cfg_overrides=op_cfg or None,
            )
            # W1-T1：上报 data_quality 给监控（best-effort）
            if state.orderbook_pressure_snapshot is not None:
                try:
                    from processors.liquidity_wall_metrics import get_metrics
                    get_metrics().record_data_quality(
                        ccy,
                        state.orderbook_pressure_snapshot.data_quality or "",
                    )
                except Exception:
                    pass
                # W1-T2：历史落盘（best-effort，推到 thread pool 避免阻塞 event loop）
                try:
                    from processors.liquidity_wall_archiver import get_archiver
                    from processors.liquidity_wall_metrics import get_metrics as _gm
                    archiver = get_archiver()
                    snapshot_ref = state.orderbook_pressure_snapshot
                    source_age = _gm().source_age_by_endpoint()
                    loop = asyncio.get_running_loop()
                    loop.run_in_executor(
                        None, archiver.append, snapshot_ref, source_age,
                    )
                except Exception:
                    logger.debug("[OP] archiver dispatch failed", exc_info=True)
        except Exception:
            logger.debug("[OP] compute_pressure_snapshot failed", exc_info=True)

        # V2 关键位先行：独立于 calculate_levels，产出信号供后者桥接
        self._recompute_key_levels_v2(ccy)

        kl_signals = None
        if state.key_level_snapshot_v2:
            kl_signals = state.key_level_snapshot_v2.signals or None

        self._recompute_range_signal(ccy)

        # ── D01 L2 MarketRegime 识别（轻量，不依赖 AISnapshot）──
        try:
            from processors.market_regime import compute_regime_from_state
            state.regime_snapshot = compute_regime_from_state(
                coin=ccy,
                price=price,
                atr_14=state.atr or 0.0,
                boll_data=state.boll_4h_data or state.boll_data,
                structure=state.market_structure,
                hist_vol_pct=(state.market_index.btc_hist_vol if state.market_index else None),
                ema20=state.ema20_cg,
                prev=state.regime_snapshot,
            )
        except Exception:
            logger.debug("[D01] compute_regime_from_state failed", exc_info=True)

        # ── 趋势衰竭侦测（Phase 1，独立模块，不进硬门，仅作为前端展示 + AI 偏置）──
        try:
            from processors.trend_exhaustion import compute_trend_exhaustion
            state.trend_exhaustion = compute_trend_exhaustion(
                state, prev_signal=state.trend_exhaustion,
            )
        except Exception:
            logger.debug("[TE] compute_trend_exhaustion failed", exc_info=True)

        # ── TE Shadow Logger（P0-A）：异步投递，永不阻塞主链路 ──
        try:
            if state.trend_exhaustion is not None and state.ticker is not None:
                from monitoring.te_shadow import get_te_shadow_logger
                get_te_shadow_logger().record(
                    coin=ccy,
                    signal=state.trend_exhaustion,
                    price=float(state.ticker.last or 0.0),
                    atr=float(state.atr or 0.0),
                )
        except Exception:
            logger.debug("[TE-Shadow] record failed", exc_info=True)

        # ── 规则引擎 8 维方向共识（纯聚合：结构/MTF/动能/箱体/关键位/资金流/仓位/衰竭）──
        # 依赖前置：market_structure（1h/1d/1w）/ rsi_14 / macd_data / range_signal /
        #           key_level_snapshot_v2 / cvd_contract / taker_flow / oi / funding /
        #           net_position_trend / trend_exhaustion —— 本轮 _recompute 均已完成。
        try:
            from processors.direction_vote import compute_direction_vote
            state.direction_vote = compute_direction_vote(state)
        except Exception:
            logger.debug("[DV] compute_direction_vote failed", exc_info=True)

        # PR-3 · L3 SignalBus / L4 Synthesizer / L5 SafetyGate / D04 plan_backtest /
        # divergence_backfill / signal_pnl_tracker / decision_tracker 心跳全部下线
        # （旧 Trader/Fusion/数学引擎链路）。Strategic AI 走独立路径。

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
            # V3-P2-10：1d 清算簇为空时，cascade magnet 回退用 7d 计算
            liq_map_7d=state.liq_maps.get("7d"),
            temperature_score=temp_score,
            candles_4h=state.candles_4h or None,
            candles_15m=state.candles_15m or None,
            # Z · MTF 1h 一致性：传 1h 蜡烛做大级别方向确认
            candles_1h=state.candles_1h or None,
            # V · CVD 背离确认：优先取合约 CVD，其次现货（现货数据更稳）
            cvd=state.cvd_contract or state.cvd_spot or None,
            # OP · 挂单压力监测器：当轮 snapshot 已在 _recompute 前置阶段算好
            pressure_snapshot=state.orderbook_pressure_snapshot,
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
        liq_map_30d = state.liq_maps.get("30d")
        vwap = state.vp.vwap if state.vp else 0

        oi_hist = None
        if state.oi and state.oi.history:
            oi_hist = [{"ts": s.ts, "oi": s.oi_usd} for s in state.oi.history]

        # 吸收带候选（基于 Footprint 派生；替代原"订单墙"软信号，
        # 价位级已成交硬证据，不受 spoof/撤单干扰）
        from processors.absorption_detector import detect_absorption_zones
        absorption = detect_absorption_zones(
            footprint_contract=list(getattr(state, "footprint_contract", []) or []),
            footprint_spot=list(getattr(state, "footprint_spot", []) or []),
            current_price=price,
        )

        # M1: Footprint stacked imbalance 候选（来自 contract latest/prev top_imbalance_zones）
        # 复用 footprint_analyzer.build_snapshot（已在 facts_collector 中使用，是成熟代码路径）
        from processors.market_action.footprint_analyzer import build_snapshot as _build_fp_snap
        footprint_snapshot = _build_fp_snap(
            contract_bars=list(getattr(state, "footprint_contract", []) or []),
            spot_bars=list(getattr(state, "footprint_spot", []) or []),
            coin=ccy,
        )

        # M1: 数据血统/新鲜度 - 用于 score_and_build_snapshot 末段软衰减
        from processors.key_level_freshness import compute_freshness
        freshness = compute_freshness(state)

        discovery = discover_levels(
            current_price=price,
            atr=state.atr,
            candles_4h=state.candles_4h or None,
            candles_1d=state.candles_daily or None,
            candles_1w=state.candles_weekly or None,
            liq_map=liq_map,
            liq_map_7d=liq_map_7d,
            liq_map_30d=liq_map_30d,
            vp=state.vp,
            absorption=absorption,
            footprint_snapshot=footprint_snapshot,
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

        # M2: 提取 CVD 背离方向（用于矛盾扣分）
        # CVDData.has_divergence + trend_1h → "bullish"/"bearish"/""
        cvd_div_dir = ""
        cvd_obj = state.cvd_contract or state.cvd_spot
        if cvd_obj and cvd_obj.has_divergence:
            tr = (cvd_obj.trend_1h or "").lower()
            if tr in ("up", "rising", "bullish"):
                cvd_div_dir = "bullish"
            elif tr in ("down", "falling", "bearish"):
                cvd_div_dir = "bearish"

        # M2: 提取 funding 极值（取 OKX/Binance 中绝对值最大者）
        funding_extreme = 0.0
        if state.funding:
            for r in (state.funding.okx_rate, state.funding.binance_rate):
                if r is not None and abs(r) > abs(funding_extreme):
                    funding_extreme = float(r)

        # M2: OI 高百分位判定（≥10 历史样本时计算 P80）
        oi_high_pct = False
        if state.oi and state.oi.history and len(state.oi.history) >= 10:
            sorted_oi = sorted(s.oi_usd for s in state.oi.history)
            p80_idx = int(len(sorted_oi) * 0.8)
            if state.oi.current_usd >= sorted_oi[p80_idx]:
                oi_high_pct = True

        snapshot = score_and_build_snapshot(
            discovery=discovery,
            current_price=price,
            atr=state.atr,
            prev_levels=state.key_levels_v2 or None,
            boll_data=state.boll_data,
            boll_4h_data=state.boll_4h_data,
            macd_histogram=macd_hist,
            freshness=freshness,
            # M2 新增（向后兼容默认值）
            cvd_divergence=cvd_div_dir,
            funding_rate=funding_extreme,
            oi_high_percentile=oi_high_pct,
            # M3 新增：regime-aware scoring + regime 上下文
            regime_snapshot=getattr(state, "regime_snapshot", None),
        )

        # M1: 独立磁铁通道（max_pain + heatmap top density）
        # 不参与 levels 评分，仅作 UI/AI 参考；与已有 level 距离过近时自动跳过
        from processors.key_level_magnets import discover_magnets
        liq_max_pain_24h = (state.liq_max_pain or {}).get("24h")
        liq_heatmap_24h = (state.liq_heatmaps or {}).get("24h")
        snapshot.magnet_levels = discover_magnets(
            liq_max_pain_24h=liq_max_pain_24h,
            liq_heatmap_24h=liq_heatmap_24h,
            levels=snapshot.levels,
            current_price=price,
            atr=state.atr,
        )

        return snapshot

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
        if state.range_signal:
            payload["range_signal"] = state.range_signal.model_dump()
        if state.key_level_snapshot_v2 and state.key_level_snapshot_v2.levels:
            payload["key_levels_v2"] = state.key_level_snapshot_v2.model_dump()
        # MTF 市场结构（1d / 1w）独立 dict —— 1h 简版仍通过 range_signal.ms_* 暴露，
        # 此处提供完整结构供前端 MarketStructureBadge 做 MTF 对齐度展示
        if state.market_structure_1d:
            payload["market_structure_1d"] = state.market_structure_1d.model_dump()
        if state.market_structure_1w:
            payload["market_structure_1w"] = state.market_structure_1w.model_dump()

        # 新维度
        if state.option_max_pain:
            payload["option_max_pain"] = state.option_max_pain.model_dump()
        if state.option_info:
            payload["option_info"] = state.option_info.model_dump()
        if state.large_orders:
            payload["large_orders"] = state.large_orders.model_dump()
        if state.orderbook_pressure_snapshot:
            payload["orderbook_pressure"] = state.orderbook_pressure_snapshot.model_dump()
        if state.whale_data:
            payload["whale_data"] = {
                "hl_alerts_count": len(state.whale_data.hl_alerts),
                "transfers_count": len(state.whale_data.transfers),
            }
        if state.liq_max_pain:
            # 仅下发当前 coin 通道的痛点，避免把 BTC 通道收到 ETH/SOL items（poll
            # 层多币种共享同一 LiqMaxPainData 引用，原始 items 含全部 supported_coins）
            payload["liq_max_pain"] = {}
            for k, v in state.liq_max_pain.items():
                picked = _pick_max_pain_for_coin(v, coin.ccy)
                if picked is not None:
                    payload["liq_max_pain"][k] = {
                        "ts": int(getattr(v, "ts", 0) or 0),
                        "range": getattr(v, "range", k) or k,
                        "items": [picked.model_dump()],
                    }
        if state.liq_heatmaps:
            payload["liq_heatmaps"] = {k: v.model_dump() for k, v in state.liq_heatmaps.items()}
        if state.rsi_14 is not None:
            payload["rsi_14"] = state.rsi_14
        if state.macd_data:
            payload["macd"] = state.macd_data
        if state.boll_data:
            payload["boll"] = state.boll_data
        if state.trend_exhaustion:
            payload["trend_exhaustion"] = state.trend_exhaustion.model_dump()
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
        liq = state.liq_maps.get("1d") or state.liq_maps.get("24h")
        if liq:
            result["liquidation_1d"] = liq.model_dump()
        return result

    def get_temperature(self, ccy: str) -> Optional[MarketTemperature]:
        return self._states.get(ccy, CoinState(ccy)).temperature

    def get_liquidation_map(self, ccy: str, cycle: str) -> Optional[LiquidationMap]:
        return self._states.get(ccy, CoinState(ccy)).liq_maps.get(cycle)

    def get_liq_heatmap(self, ccy: str, range_: str) -> Optional[HeatmapData]:
        """直读 state.liq_heatmaps[range]（仅 24h / 7d 当前会被填充）。"""
        return self._states.get(ccy, CoinState(ccy)).liq_heatmaps.get(range_)

    def get_waterfall(self, ccy: str) -> Optional[WaterfallData]:
        return self._states.get(ccy, CoinState(ccy)).waterfall

    def get_smc_snapshot(self, ccy: str, horizon: str = "intraday"):
        state = self._states.get(ccy)
        if not state:
            return None
        from processors.smc import build_smc_snapshot
        return build_smc_snapshot(
            state,
            horizon="swing" if horizon == "swing" else "intraday",
            market_breadth=self._nansen_market_breadth,
        )

    def get_smc_facts(self, ccy: str, horizon: str = "intraday") -> dict:
        state = self._states.get(ccy)
        if not state:
            return {}
        from processors.smc import build_smc_facts
        return build_smc_facts(
            state,
            horizon="swing" if horizon == "swing" else "intraday",
            market_breadth=self._nansen_market_breadth,
        )

    def get_smc_market_breadth(self):
        from processors.smc import build_smc_market_breadth
        return build_smc_market_breadth(self._nansen_market_breadth)

    def get_last_ai_ts(self, ccy: str) -> float:
        return self._states.get(ccy, CoinState(ccy)).last_ai_ts

    # PR-3 · get_ai_history / is_ai_running 已下线（旧 Trader 链路）

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

    def _has_news_brief(self) -> bool:
        """软指标：真实新闻简报是否已生成（排除 bootstrap 种子）。

        bootstrap 种子 `model_used="bootstrap"` 且 `based_on_events_count=0`，
        此时 AI 看到的新闻叙事段落是"预热中"占位，会降低首轮分析质量。
        真实 brief 出现后（news_agent_loop 完成首批 events enrich + 调用 news_analyzer）
        `based_on_events_count>0` 或 `model_used!=bootstrap`，即认为已就绪。
        """
        try:
            from processors.news_brief import get_current_brief
            brief = get_current_brief()
            if brief is None:
                return False
            if (brief.model_used or "").lower() == "bootstrap":
                return False
            return (brief.based_on_events_count or 0) > 0
        except Exception:
            return False

    # PR-3 · _auto_ai_loop / _decision_summary_loop 已下线
    # （旧 Trader auto loop + decision_tracker 心跳总结都依赖被删模块）

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

    # PR-3 · _digest_email_loop 已下线（依赖旧 Trader trading_plan_entries 回测统计）
    # PR-3 · fire_ai_analysis / _ai_analysis_task 已下线（旧 Trader 链路）

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Market Action Analyzer · 周期循环 + 触发器
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _get_maa_arbiter(self):
        """懒加载 MAA arbiter（避免启动时 openai 客户端重复初始化）。"""
        if self._maa_arbiter is None:
            try:
                from ai.market_action_arbiter import create_market_action_arbiter
                self._maa_arbiter = create_market_action_arbiter()
            except Exception:
                logger.error("MAA arbiter init failed", exc_info=True)
                self._maa_arbiter = None
        return self._maa_arbiter

    @property
    def market_action_available(self) -> bool:
        arb = self._get_maa_arbiter()
        return bool(arb and getattr(arb, "available", False))

    async def fire_market_action_analysis(self, ccy: str) -> None:
        """对外暴露：手动触发一次 MAA 分析（API / CLI 调用）。"""
        if ccy in self._maa_running:
            raise RuntimeError(f"MAA already running for {ccy}")
        state = self._states.get(ccy)
        if state is None:
            raise RuntimeError(f"coin not supported: {ccy}")
        state.market_action_last_ts = time.time()
        self._maa_running.add(ccy)
        asyncio.create_task(self._market_action_task(ccy))

    async def _market_action_task(self, ccy: str) -> None:
        try:
            from processors.market_action.facts_collector import collect as collect_facts
            state = self._states[ccy]
            facts = collect_facts(state)

            arbiter = self._get_maa_arbiter()
            if arbiter is None:
                logger.warning("MAA arbiter unavailable | coin=%s", ccy)
                return

            # 捕获上一份报告（在覆盖 state.market_action_report 之前），供 AI 做时序对照
            previous_report = state.market_action_report

            t0 = time.time()
            report = await arbiter.analyze(facts, previous_report=previous_report)
            elapsed = time.time() - t0

            state.market_action_report = report
            state.market_action_last_ts = time.time()
            state.market_action_history.append(report)

            cont_stance = (
                report.continuity.stance
                if report.continuity is not None else "n/a"
            )
            stab_str = "n/a"
            if report.stability is not None:
                s = report.stability
                if s.accepted_scenario == s.ai_raw_scenario:
                    stab_str = f"pass(stable_for_runs={s.stable_for_runs})"
                else:
                    stab_str = (
                        f"override({s.override_reason or 'unknown'}) "
                        f"raw={s.ai_raw_scenario}→accepted={s.accepted_scenario} "
                        f"pending={s.pending_switch_to}/{s.pending_runs}"
                    )
            logger.info(
                "MAA ok | coin=%s | %.1fs | scenario=%s phase=%s conf=%d "
                "bias=%s dq=%s evidence=%d continuity=%s stability=%s parse_ok=%s",
                ccy, elapsed, report.scenario, report.market_phase,
                report.confidence,
                report.trading_implications.bias,
                report.data_quality, len(report.evidence_breakdown),
                cont_stance, stab_str,
                bool(report.prompt_debug and report.prompt_debug.parse_ok),
            )

            try:
                await push_to_coin(ccy, "market_action_report", report.model_dump())
            except Exception:
                logger.debug("MAA ws push failed | coin=%s", ccy, exc_info=True)

            try:
                self._save_market_action_history()
            except Exception:
                logger.debug("MAA persist failed | coin=%s", ccy, exc_info=True)

            # ── MAA Shadow Logger（Phase 5-A）：落 report 记录以便事后评估 ──
            try:
                from monitoring.maa_shadow import get_maa_shadow_logger
                price_now = float(state.ticker.last) if state.ticker and state.ticker.last else 0.0
                if price_now > 0:
                    get_maa_shadow_logger().record_report(ccy, report, price_now)
            except Exception:
                logger.debug("[MAA-Shadow] record_report failed | coin=%s", ccy, exc_info=True)

            # ── MAA 邮件提醒：方向切换或强信号触发时发送 ──
            # 节律完全跟随 MAA 主任务（每 10 分钟），无需额外节流；
            # 任何异常都不能影响 MAA 主流程，因此包在独立 try 里
            try:
                if (
                    self._notif_cfg.enabled
                    and getattr(self._notif_cfg, "include_market_action", False)
                ):
                    await self._check_maa_alerts(
                        ccy, report=report, previous_report=previous_report,
                    )
            except Exception:
                logger.debug("MAA alert check failed | coin=%s", ccy, exc_info=True)
        except Exception as e:
            logger.error(
                "MAA background task failed | coin=%s | %s: %s",
                ccy, type(e).__name__, e, exc_info=True,
            )
        finally:
            self._maa_running.discard(ccy)

    async def _check_maa_alerts(
        self,
        ccy: str,
        *,
        report,
        previous_report,
    ) -> None:
        """MAA 信号邮件入口（独立于 _check_alerts，挂在 MAA 任务尾部）。

        与 _check_alerts 的差异：
          1) 触发时机不是每 5 秒 ticker 刷新，而是 MAA 完成（≈每 10 分钟），
             所以无需为防刷屏增加任何额外节流，直接复用 AlertDedup
             即可（普通通道 45 min / 强信号 20 min）。
          2) 复用同一个 SMTP 闸门 + 配置缺失提示静默策略，避免重复打印。
          3) 强信号 dedup_key 中包含 "strong" 后缀，且走 `_maa_strong_dedup`
             这个独立冷却池，避免"普通信号占位 → 同币强信号被压住"。
        """
        try:
            # 复用关键位通道的 SMTP 完整性闸门（同一份 _notif_cfg / _alert_config_warned）
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

            from notifications.signal_monitor import scan_maa_alerts
            from notifications.email_alert import send_alert_email

            state = self._states.get(ccy)
            if state is None:
                return
            price = float(state.ticker.last) if state.ticker and state.ticker.last else 0.0
            if price <= 0:
                return

            prev_scenario = None
            if previous_report is not None:
                if previous_report.stability is not None:
                    prev_scenario = previous_report.stability.accepted_scenario
                else:
                    # 老快照可能没 stability（升级前的 facts），回落到 raw scenario
                    prev_scenario = getattr(previous_report, "scenario", None)

            events = scan_maa_alerts(
                ccy,
                report=report,
                prev_scenario=prev_scenario,
                price=price,
                coin_whitelist=list(self._notif_cfg.market_action_coins or []),
                min_confidence=int(self._notif_cfg.market_action_min_confidence),
                strong_confidence=int(self._notif_cfg.market_action_strong_confidence),
            )

            if not events:
                return

            sent = 0
            cooled = 0
            failed = 0
            for event in events:
                # 强信号走独立 dedup pool，避免被普通通道占位拖累
                dedup = (
                    self._maa_strong_dedup
                    if event.maa_is_strong else self._alert_dedup
                )
                if not dedup.should_send(event.dedup_key):
                    cooled += 1
                    continue
                ok = await send_alert_email(event, self._notif_cfg)
                if ok:
                    dedup.mark_sent(event.dedup_key)
                    sent += 1
                else:
                    failed += 1

            logger.info(
                "[maa-alert] coin=%s scenario=%s strong=%s matched=%d "
                "cooled=%d sent=%d failed=%d prev=%s",
                ccy,
                events[0].maa_scenario if events else "—",
                bool(events and events[0].maa_is_strong),
                len(events),
                cooled,
                sent,
                failed,
                prev_scenario or "—",
            )

            self._alert_dedup.cleanup()
            self._maa_strong_dedup.cleanup()
        except Exception:
            logger.debug("_check_maa_alerts error | coin=%s", ccy, exc_info=True)

    async def _auto_market_action_loop(self, interval_sec: int) -> None:
        """按 interval_sec 节律自动触发每个币种的 MAA 分析（交错启动）。

        - 冷启动等待 180s 让 facts 数据就绪
        - 每个币种间隔 10s 发起，避免 3 币同时抢 DeepSeek 配额
        """
        # 冷启等待 facts 就绪
        await asyncio.sleep(180)
        logger.info(
            "MAA auto loop started | interval=%ds arbiter=%s",
            interval_sec, self.market_action_available,
        )
        while self._running:
            if not self.market_action_available:
                await asyncio.sleep(120)
                continue
            for ccy in self._settings.supported_coins:
                if ccy in self._maa_running:
                    continue
                state = self._states.get(ccy)
                if state is None:
                    continue
                last = state.market_action_last_ts or 0
                elapsed = time.time() - last if last else float("inf")
                if elapsed < interval_sec:
                    continue
                try:
                    await self.fire_market_action_analysis(ccy)
                    logger.info("MAA auto triggered | coin=%s", ccy)
                except Exception:
                    logger.error("MAA auto trigger failed | coin=%s",
                                 ccy, exc_info=True)
                await asyncio.sleep(10)  # 三币之间交错
            await asyncio.sleep(60)

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # PR-2 · Strategic AI 决策官 · 周期循环 + 触发器
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    _STRATEGIC_INIT_BACKOFF_SEC = 300.0

    def _get_strategic_arbiter(self):
        """懒加载 Strategic arbiter（避免启动时 openai 客户端重复初始化）。

        F-15：失败时进入 5min 退避冷却——避免 key 缺失 / 网络中断时被自动循环
        / API 调用反复触发昂贵的 import + client 构造，并刷屏 error 日志。
        """
        if self._strategic_arbiter is not None:
            return self._strategic_arbiter
        now = time.time()
        if (
            self._strategic_arbiter_init_failed_at is not None
            and (now - self._strategic_arbiter_init_failed_at)
            < self._STRATEGIC_INIT_BACKOFF_SEC
        ):
            return None
        try:
            from ai.strategic_arbiter import create_strategic_arbiter
            self._strategic_arbiter = create_strategic_arbiter()
            self._strategic_arbiter_init_failed_at = None
        except Exception:
            logger.error(
                "Strategic arbiter init failed (backoff %.0fs before retry)",
                self._STRATEGIC_INIT_BACKOFF_SEC,
                exc_info=True,
            )
            self._strategic_arbiter = None
            self._strategic_arbiter_init_failed_at = now
        return self._strategic_arbiter

    @property
    def strategic_available(self) -> bool:
        arb = self._get_strategic_arbiter()
        return bool(arb and getattr(arb, "available", False))

    async def fire_strategic_analysis(self, ccy: str) -> None:
        """对外暴露：手动触发一次 Strategic 分析（API / CLI 调用）。"""
        if ccy in self._strategic_running:
            raise RuntimeError(f"Strategic already running for {ccy}")
        state = self._states.get(ccy)
        if state is None:
            raise RuntimeError(f"coin not supported: {ccy}")
        state.strategic_last_ts = time.time()
        self._strategic_running.add(ccy)
        asyncio.create_task(self._strategic_task(ccy))

    async def _push_strategic_error(self, ccy: str, reason: str) -> None:
        """向前端推送 strategic_error 事件，让 AIButton 解锁 loading。

        所有"任务无法执行"的早 return 路径（arbiter 不可用 / snapshot 装配失败 /
        异常）都必须经过本方法，否则前端 strategicLoading 永挂起。
        """
        try:
            await push_to_coin(ccy, "strategic_error", {
                "coin": ccy,
                "reason": reason,
                "ts": int(time.time()),
            })
        except Exception:
            logger.debug("Strategic ws error push failed | coin=%s", ccy, exc_info=True)

    async def _strategic_task(self, ccy: str) -> None:
        """单次 Strategic 分析任务：装配 AISnapshot → arbiter.analyze → 存 state + history。

        关键设计：
          - 复用 `run_ai_analysis` 已实现的"装配 AISnapshot"路径，避免双套数据组装
          - 但 Strategic 的 snapshot 是独立调用 `_build_strategic_snapshot`（不耦合
            旧 Trader 的 `run_ai_analysis`，后者会触发 ExecutionPlan 等待删除模块）
          - 任何异常不影响 MAA / Trader / 引擎主流程
          - 任何"无报告产出"的早 return 都会推 strategic_error 让前端解锁 loading
        """
        try:
            state = self._states[ccy]
            arbiter = self._get_strategic_arbiter()
            if arbiter is None:
                logger.warning("Strategic arbiter unavailable | coin=%s", ccy)
                await self._push_strategic_error(ccy, "arbiter_unavailable")
                return

            snapshot = self._build_strategic_snapshot(ccy)
            if snapshot is None:
                logger.warning("Strategic snapshot build failed | coin=%s", ccy)
                await self._push_strategic_error(ccy, "snapshot_build_failed")
                return

            previous_report = state.strategic_report
            t0 = time.time()
            report = await arbiter.analyze(snapshot, previous_report=previous_report)
            elapsed = time.time() - t0

            state.strategic_report = report
            state.strategic_last_ts = time.time()
            state.strategic_history.append(report)

            logger.info(
                "Strategic ok | coin=%s | %.1fs | decision=%s horizon=%s "
                "bias=%s conf=%.2f data_quality=%s parse_ok=%s",
                ccy, elapsed, report.decision, report.horizon,
                report.bias, report.confidence,
                report.data_quality,
                bool(report.prompt_debug and report.prompt_debug.parse_ok),
            )

            try:
                await push_to_coin(ccy, "strategic_report", report.model_dump())
            except Exception:
                logger.debug("Strategic ws push failed | coin=%s", ccy, exc_info=True)

            try:
                self._save_strategic_history()
            except Exception:
                logger.debug("Strategic persist failed | coin=%s", ccy, exc_info=True)
        except Exception as e:
            logger.error(
                "Strategic background task failed | coin=%s | %s: %s",
                ccy, type(e).__name__, e, exc_info=True,
            )
            await self._push_strategic_error(
                ccy, f"task_exception:{type(e).__name__}",
            )
        finally:
            self._strategic_running.discard(ccy)

    def _build_strategic_snapshot(self, ccy: str):
        """装配 Strategic AI 用的 AISnapshot（复用 build_ai_snapshot 路径）。

        与 `run_ai_analysis` 的装配逻辑保持完全一致：
          - 走相同的 `build_ai_snapshot()`
          - 拉取 trading_brain + market_action_facts（PR-1 已就绪）
          - 失败时 graceful degrade，返回 None 让上层跳过本轮
        """
        try:
            from ai.snapshot import build_ai_snapshot
            from processors.market_action.facts_collector import collect as collect_facts
            from processors.trading_brain_builder import build_trading_brain_snapshot
        except Exception:
            logger.error("Strategic snapshot import failed", exc_info=True)
            return None

        state = self._states.get(ccy)
        if state is None or state.ticker is None or state.ticker.last is None:
            return None

        # MAA Facts（PR-1 已集成，graceful degrade）
        market_action_facts_for_ai = None
        try:
            market_action_facts_for_ai = collect_facts(state)
        except Exception:
            logger.debug("Strategic collect_facts failed | coin=%s", ccy, exc_info=True)

        # TradingBrain Snapshot（PR-1 已集成，graceful degrade）
        # 参数契约 (trading_brain_builder.build_trading_brain_snapshot):
        #   kl: Optional[KeyLevelSnapshotV2]
        #   op: Optional[OrderbookPressureSnapshot]
        #   liq: Optional[LiquidationMap]   ← 单个，不是 dict
        #   cvd_contract_trend / cvd_spot_trend: str
        #   oi_delta_1h_pct: Optional[float]
        #   funding_interpretation: str
        # 注：state.cvd_contract/cvd_spot/oi/funding 是富对象，需要先抽出标量字段。
        liq_for_brain = state.liq_maps.get("1d") or state.liq_maps.get("24h")
        cvd_c_trend = (
            state.cvd_contract.trend_1h if state.cvd_contract else ""
        ) or ""
        cvd_s_trend = (
            state.cvd_spot.trend_1h if state.cvd_spot else ""
        ) or ""
        oi_d1h = state.oi.change_1h_pct if state.oi else None
        fd_interp = (
            state.funding.interpretation if state.funding else ""
        ) or ""

        trading_brain_for_ai = None
        try:
            trading_brain_for_ai = build_trading_brain_snapshot(
                coin=ccy,
                last_price=float(state.ticker.last),
                atr=float(state.atr or state.atr_cg or 0.0),
                kl=state.key_level_snapshot_v2,
                op=state.orderbook_pressure_snapshot,
                liq=liq_for_brain,
                cvd_contract_trend=cvd_c_trend,
                cvd_spot_trend=cvd_s_trend,
                oi_delta_1h_pct=oi_d1h,
                funding_interpretation=fd_interp,
                prev_setup_states=state.brain_setup_states,
            )
        except Exception:
            # 升级到 warning：这条路径曾因参数名 typo 静默失败数周，
            # 表现为 §2 / §4 / §5 全部 None。再静默一次的代价太高。
            logger.warning(
                "Strategic build_trading_brain_snapshot failed | coin=%s",
                ccy, exc_info=True,
            )

        liq_1d = state.liq_maps.get("1d") or state.liq_maps.get("24h")
        liq_7d = state.liq_maps.get("7d")
        liq_30d = state.liq_maps.get("30d")
        pain_block = state.liq_max_pain.get("24h") or state.liq_max_pain.get("1d")
        liq_mp = _pick_max_pain_for_coin(pain_block, ccy)

        cutoff = int(time.time()) - 3600
        recent_sweeps = [e for e in state.liq_sweep_events if e.get("ts", 0) > cutoff]

        temp_score = state.temperature.score if state.temperature else 50
        pin_lv = state.temperature.pin_risk_level if state.temperature else "low"
        atr_val = float(state.atr or state.atr_cg or 0.0)

        w_ct = 0
        if state.whale_data and state.whale_data.transfers:
            w_ct = len(state.whale_data.transfers)

        wf = self._calc_whale_transfer_flows(state.whale_data)
        wdir = ""
        netu = float(wf.get("net_usd") or 0)
        if netu > 1e6:
            wdir = "inflow_exchange"
        elif netu < -1e6:
            wdir = "outflow_exchange"

        cb_prem = 0.0
        cb_trend = ""
        if state.coinbase_premium:
            cb_prem = float(state.coinbase_premium.current_premium or 0)
        sc_mcap = 0.0
        sc_7d = 0.0
        if state.stablecoin_mcap:
            sc_mcap = float(state.stablecoin_mcap.current_total or 0)
            hist = state.stablecoin_mcap.history or []
            if len(hist) >= 8:
                old = hist[-8].total_mcap
                if old and old > 0:
                    sc_7d = round((hist[-1].total_mcap - old) / old * 100, 3)

        opt_mp = None
        opt_ex = ""
        if state.option_max_pain:
            opt_mp = state.option_max_pain.nearest_max_pain
            opt_ex = state.option_max_pain.nearest_expiry or ""
        # F-02: option_call_oi / option_put_oi 已下线（AISnapshot 不再渲染该字段，
        # facts.options 已经覆盖期权 OI 数据）
        iv_atm = pc_oi = None
        if state.option_info:
            iv_atm = state.option_info.iv_atm
            pc_oi = state.option_info.put_call_oi_ratio

        try:
            return build_ai_snapshot(
                coin=ccy,
                price=float(state.ticker.last),
                high_24h=state.ticker.high_24h or 0,
                low_24h=state.ticker.low_24h or 0,
                atr=atr_val,
                market_temp_score=float(temp_score),
                pin_risk_level=pin_lv,
                liq_map=liq_1d,
                liq_map_7d=liq_7d,
                liq_map_30d=liq_30d,
                liq_max_pain_24h=liq_mp,
                vp=state.vp,
                pressure_snapshot=state.orderbook_pressure_snapshot,
                key_level_snapshot_v2=state.key_level_snapshot_v2,
                trading_brain=trading_brain_for_ai,
                market_action_facts=market_action_facts_for_ai,
                global_liq=state.global_liq,
                liq_sweep_events=recent_sweeps,
                market_index=state.market_index,
                etf_flow=state.etf_flow,
                cycle_position=state.cycle_position,
                range_signal=state.range_signal,
                rsi_14=state.rsi_14,
                boll_data=state.boll_data,
                ema20=state.ema20_cg,
                btc_hist_vol=state.market_index.btc_hist_vol if state.market_index else None,
                option_max_pain_price=opt_mp,
                option_nearest_expiry=opt_ex,
                btc_implied_vol=iv_atm,
                btc_put_call_oi=pc_oi,
                poll_failures=state.poll_failures,
                coinbase_premium=cb_prem,
                coinbase_premium_trend=cb_trend,
                stablecoin_total_mcap=sc_mcap,
                stablecoin_7d_change_pct=sc_7d,
                whale_net_direction=wdir,
                whale_transfers_count=w_ct,
                whale_transfer_net_usd=netu,
            )
        except Exception:
            logger.error("Strategic build_ai_snapshot failed | coin=%s", ccy, exc_info=True)
            return None

    async def _auto_strategic_loop(self, interval_sec: int) -> None:
        """按 interval_sec 节律自动触发每个币种的 Strategic 分析（交错启动）。

        与 MAA 调度对称：
          - 冷启动等待 240s（比 MAA 180s 稍长，因 Strategic 依赖 trading_brain）
          - 每个币种间隔 15s 发起（比 MAA 10s 更长，避免与 MAA 抢 DeepSeek 配额）
          - 与 MAA 时序错开：Strategic 慢半拍，避免同时打 LLM
        """
        # 冷启等待 brain 就绪
        await asyncio.sleep(240)
        logger.info(
            "Strategic auto loop started | interval=%ds arbiter=%s",
            interval_sec, self.strategic_available,
        )
        while self._running:
            if not self.strategic_available:
                await asyncio.sleep(120)
                continue
            for ccy in self._settings.supported_coins:
                if ccy in self._strategic_running:
                    continue
                state = self._states.get(ccy)
                if state is None:
                    continue
                last = state.strategic_last_ts or 0
                elapsed = time.time() - last if last else float("inf")
                if elapsed < interval_sec:
                    continue
                try:
                    await self.fire_strategic_analysis(ccy)
                    logger.info("Strategic auto triggered | coin=%s", ccy)
                except Exception:
                    logger.error("Strategic auto trigger failed | coin=%s",
                                 ccy, exc_info=True)
                await asyncio.sleep(15)
            await asyncio.sleep(60)

    async def _auto_maa_heartbeat_loop(self, interval_sec: int = 300) -> None:
        """Phase 5-A · 每 interval_sec 给每个币种落一条价格 heartbeat。

        用途：为 maa_eval 在 T+4h/T+8h/T+24h 寻价提供足够密集的价格锚点。
        shadow logger 内部有 15 分钟去重，interval_sec 设置过小也不会膨胀磁盘。
        """
        await asyncio.sleep(60)  # 冷启稍等一会儿
        try:
            from monitoring.maa_shadow import get_maa_shadow_logger
            shadow = get_maa_shadow_logger()
        except Exception:
            logger.warning("[MAA-Shadow] import failed, heartbeat disabled", exc_info=True)
            return
        logger.info("MAA heartbeat loop started | interval=%ds", interval_sec)
        while self._running:
            for ccy in self._settings.supported_coins:
                state = self._states.get(ccy)
                if state is None or state.ticker is None or not state.ticker.last:
                    continue
                try:
                    shadow.record_heartbeat(ccy, float(state.ticker.last))
                except Exception:
                    logger.debug("[MAA-Shadow] heartbeat failed | coin=%s", ccy, exc_info=True)
            await asyncio.sleep(interval_sec)

    async def _auto_maa_history_loop(self, interval_sec: int = 300) -> None:
        """MAA P0 增强 · 每 interval_sec 拉一次 funding 8h + OI 30d hourly 历史。

        设计：
          - 两个接口（`fetch_fr_oi_weight_history` + `fetch_oi_aggregated_history`）
            均已封装，Coinglass 侧有 30s 级缓存，5min 一轮不会爆 quota
          - 每币 2 次 API ≈ 6 次/5min ≈ 72 次/h，占 3 万日配额不到 0.25%
          - 顺便修复 multi_funding.avg_7d / oi_weighted 这两个历史永远是 0 的 bug
        """
        from polls.derivatives import (
            poll_funding_history_8h,
            poll_oi_hourly_30d,
        )
        await asyncio.sleep(30)  # 冷启稍等，让基础 poll 先跑一轮，避免同时抢 rate limit
        logger.info("MAA history loop started | interval=%ds", interval_sec)
        while self._running:
            for ccy in self._settings.supported_coins:
                coin = self._settings.get_coin(ccy)
                state = self._states.get(ccy)
                if state is None:
                    continue
                try:
                    await poll_funding_history_8h(self._cg, coin, state)
                except Exception:
                    logger.debug("[MAA-hist] funding_history_8h failed | coin=%s",
                                 ccy, exc_info=True)
                try:
                    await poll_oi_hourly_30d(self._cg, coin, state)
                except Exception:
                    logger.debug("[MAA-hist] oi_hourly_30d failed | coin=%s",
                                 ccy, exc_info=True)
                await asyncio.sleep(3)  # 币之间交错避免抢 quota
            await asyncio.sleep(interval_sec)

    async def _auto_maa_eval_loop(self, interval_sec: int = 1800) -> None:
        """Phase 5-B · 每 interval_sec 跑一次 MAA 事后评估，缓存结果到
        `self._maa_eval_summary[coin]`，供 `/api/market-action/eval` 读取。
        """
        # 冷启等 10 分钟，先让 heartbeat/report 积累一些样本
        await asyncio.sleep(600)
        try:
            from monitoring import maa_eval
        except Exception:
            logger.warning("[MAA-Eval] import failed, eval loop disabled", exc_info=True)
            return
        logger.info("MAA eval loop started | interval=%ds", interval_sec)
        while self._running:
            for ccy in self._settings.supported_coins:
                try:
                    summary = maa_eval.evaluate_coin(ccy, window_days=7)
                    self._maa_eval_summary[ccy] = summary.to_dict()
                    self._maa_eval_last_ts = time.time()
                    logger.info(
                        "[MAA-Eval] %s | %s",
                        ccy, maa_eval.summary_headline(summary),
                    )
                except Exception:
                    logger.error("[MAA-Eval] failed | coin=%s", ccy, exc_info=True)
                await asyncio.sleep(2)
            await asyncio.sleep(interval_sec)

    # PR-3 · run_ai_analysis / _build_and_fuse_trader_report 已整体下线
    # 旧 Trader / Fusion / 数学引擎链路被 Strategic AI 全程决策官取代。
    # Strategic 调用栈：_build_strategic_snapshot → StrategicArbiter.analyze。

    @staticmethod
    def _calc_whale_direction(whale):
        from polls.macro import calc_whale_direction
        return calc_whale_direction(whale)

    @staticmethod
    def _calc_whale_transfer_flows(whale):
        from polls.macro import calc_whale_transfer_flows
        return calc_whale_transfer_flows(whale)

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

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 滚仓模块 · 评估调度 + WS 推送
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    async def _roll_eval_loop(self) -> None:
        """定时对所有 active 滚仓持仓做一轮评估。

        触发间隔：`_roll_eval_interval_sec`（默认 10s，可以根据算力微调）。
        跳过条件：
          - 持仓的 coin 不在支持列表
          - 对应 CoinState.ticker 尚未就绪（价格为 0 即无法评估）
          - build_market_context 返回 None
        on_signal 回调已在 __init__ 中注入 → signal 生成即异步推送。
        """
        from processors.roll_market_adapter import build_market_context_from_state

        # 给数据管线一点热身时间
        await asyncio.sleep(15)
        logger.info(
            "[Roll] eval loop started | interval=%ds",
            self._roll_eval_interval_sec,
        )

        # 心跳节流：每 60s 至少 1 条 INFO，便于运维确认 loop 还活着
        heartbeat_interval_sec = 60
        last_heartbeat_ts = 0.0
        last_actions: dict[str, str] = {}     # position_id → 上次 action（用于"action 变化时打日志"）

        while self._running:
            try:
                if self.roll_service._initialized:  # type: ignore[attr-defined]
                    def _provider(coin: str):
                        state = self._states.get(coin)
                        if state is None:
                            return None
                        return build_market_context_from_state(state)

                    signals = self.roll_service.evaluate_all(_provider)
                    now_ts = time.time()
                    actionable_signals = [s for s in (signals or []) if s.action != "hold"]
                    urgent_signals = [s for s in (signals or []) if getattr(s, "urgency", "normal") == "urgent"]

                    # 1) urgent 一律 WARNING（无论是否变化）
                    for s in urgent_signals:
                        # Phase 1 的 urgent 走"刚性规则"（爆仓临近 / SafetyGate / 数据不足），
                        # 并不经过加权评分流水线，confidence_score 保持默认 0.0。直接打 "0"
                        # 会让运维误读为"没信心"，因此 urgent + 非 reduce 的情况统一标记
                        # `confidence=RULE`（确定性规则触发），和 Phase 2/3 的数值分走开。
                        _is_rule_trigger = (
                            s.action in ("close", "hold") and (s.confidence_score or 0) == 0
                        )
                        _conf_text = "RULE" if _is_rule_trigger else f"{s.confidence_score:.0f}"
                        logger.warning(
                            "[Roll][URGENT] pos=%s coin=%s action=%s headline=%s | "
                            "confidence=%s price=%.4f liq_dist=%s%%",
                            s.position_id, s.coin, s.action,
                            (s.headline_cn or "—")[:80],
                            _conf_text,
                            s.current_price,
                            f"{s.distance_to_liq_pct:.2f}" if s.distance_to_liq_pct is not None else "—",
                        )

                    # 2) 非 urgent 但 action 由 hold→其他 / 或 action 改变：INFO
                    for s in actionable_signals:
                        if s.urgency == "urgent":
                            continue   # 已在上一段打过 WARNING
                        prev = last_actions.get(s.position_id)
                        if prev != s.action:
                            logger.info(
                                "[Roll][ACTION] pos=%s coin=%s %s→%s headline=%s | "
                                "confidence=%.0f intensity=%s",
                                s.position_id, s.coin, prev or "—", s.action,
                                (s.headline_cn or "—")[:60],
                                s.confidence_score or 0.0, s.add_intensity,
                            )
                    # 更新 action 缓存（含 hold，便于检测"恢复 hold"）
                    if signals:
                        for s in signals:
                            last_actions[s.position_id] = s.action

                    # 3) 心跳：每 60s 一条 INFO（即便全 hold 也证明 loop 在转）
                    if signals and (now_ts - last_heartbeat_ts >= heartbeat_interval_sec):
                        logger.info(
                            "[Roll][heartbeat] positions=%d actionable=%d urgent=%d",
                            len(signals), len(actionable_signals), len(urgent_signals),
                        )
                        last_heartbeat_ts = now_ts
            except Exception:
                logger.error("[Roll] eval loop error", exc_info=True)
            await asyncio.sleep(self._roll_eval_interval_sec)

    def _on_roll_signal(self, signal) -> None:
        """RollService on_signal 回调：把信号异步推送给订阅的前端。

        本方法在 evaluate_all 的同步调用栈里被触发，需要通过
        asyncio.create_task（或 loop.call_soon_threadsafe）把推送挂回事件循环。
        """
        loop = self._roll_loop_ref
        if loop is None or not loop.is_running():
            return
        try:
            payload = signal.model_dump()
            # 同 loop 内直接 schedule；跨线程调用时用 call_soon_threadsafe 也安全
            if asyncio.get_event_loop_policy().get_event_loop() is loop:
                asyncio.create_task(push_roll_signal(signal.position_id, payload))
            else:
                loop.call_soon_threadsafe(
                    lambda: asyncio.create_task(
                        push_roll_signal(signal.position_id, payload)
                    )
                )
        except Exception:
            logger.debug("[Roll] on_signal push failed", exc_info=True)

    def get_source_health(self) -> list[dict]:
        daily = self._cg.daily_request_count
        limit = self._settings.coinglass.daily_limit
        usage_pct = round(daily / limit * 100, 1) if limit > 0 else 0
        sources = [
            self._cg.health().model_dump(exclude_none=True),
            self._bn.health().model_dump(exclude_none=True),
            {
                "name": "coinglass_daily_usage",
                "status": "degraded" if usage_pct > 80 else "connected",
                "reason": "quota_only_not_connectivity",
                "daily_requests": daily,
                "daily_limit": limit,
                "usage_pct": usage_pct,
                "latency_ms": 0,
            },
        ]
        if self._bbx:
            sources.append(self._bbx.health())
        if self._nansen is not None:
            sources.append(self._nansen.health().model_dump(exclude_none=True))
        else:
            sources.append({
                "name": "nansen",
                "status": "disconnected",
                "latency_ms": 0,
                "last_success_ts": 0,
                "error_count": 0,
                "reason": "NANSEN_API_KEY not set or source disabled",
            })
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

    def get_smc_monitor_status(self) -> dict:
        """SMC rollout monitor for /api/health and the logs page."""
        coins: list[dict] = []
        now = int(time.time())
        for ccy in ("BTC", "ETH"):
            state = self._states.get(ccy)
            if state is None:
                continue
            item = {
                "coin": ccy,
                "intraday": None,
                "swing": None,
                "nansen_perp_age_sec": None,
                "nansen_flow_age_sec": None,
                "nansen_exchange_flow_age_sec": None,
                "nansen_errors": dict(getattr(state, "nansen_errors", {}) or {}),
            }
            updated = getattr(state, "nansen_updated_at", {}) or {}
            if updated.get("perp_screener"):
                item["nansen_perp_age_sec"] = max(0, now - int(updated["perp_screener"]))
            if updated.get("flow_intelligence"):
                item["nansen_flow_age_sec"] = max(0, now - int(updated["flow_intelligence"]))
            if updated.get("exchange_flows"):
                item["nansen_exchange_flow_age_sec"] = max(0, now - int(updated["exchange_flows"]))
            for horizon in ("intraday", "swing"):
                try:
                    snap = self.get_smc_snapshot(ccy, horizon=horizon)
                    if snap is None:
                        continue
                    item[horizon] = {
                        "observation": snap.observation,
                        "setup_state": snap.setup_state,
                        "confidence": snap.confidence,
                        "data_quality": snap.data_quality.status,
                        "data_quality_score": snap.data_quality.score,
                        "missing": snap.data_quality.missing,
                        "degraded": snap.data_quality.degraded,
                        "zones": len(snap.zones),
                        "liquidity_pools": len(snap.liquidity_pools),
                        "fund_flow": {
                            "status": snap.fund_flow.status,
                            "bias": snap.fund_flow.bias,
                            "alerts": len(snap.fund_flow.alerts),
                        },
                    }
                except Exception as exc:
                    item[horizon] = {"error": exc.__class__.__name__}
            coins.append(item)

        breadth = self.get_smc_market_breadth()
        source_status = "connected"
        if any((c.get("intraday") or {}).get("data_quality") == "degraded" for c in coins):
            source_status = "degraded"
        return {
            "status": source_status,
            "nansen_enabled": self._nansen is not None,
            "market_breadth": {
                "status": breadth.status,
                "score": breadth.breadth_score,
                "ts": breadth.ts,
            },
            "coins": coins,
        }
