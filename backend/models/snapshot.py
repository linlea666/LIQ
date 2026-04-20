"""AI 分析用的数据快照 + 因子卡片 + 市场温度"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel


class FactorCard(BaseModel):
    """单张因子卡片数据"""
    id: str             # "D1" - "D8"
    name: str           # "清算失衡"
    value: str          # "+2.4" 主显示值
    direction: str      # "bullish" | "bearish" | "neutral"
    sub_text: str       # "空1.03亿 / 多0.68亿"
    percentile: float   # 0-100 百分位
    summary: str        # "上扫空头" 一句话结论


class MarketTemperature(BaseModel):
    """市场温度计"""
    coin: str
    ts: int
    score: float            # 0-100, 50=中性
    label: str              # "极冷" / "偏冷" / "中性" / "偏热" / "极热"
    pin_risk_level: str     # "low" / "attention" / "high" / "extreme"
    pin_risk_label: str     # "🟢低风险" / "🟡关注" / "🟠中高" / "🔴极高"
    factors: list[FactorCard]


class WaterfallItem(BaseModel):
    """瀑布图单项"""
    factor_id: str
    factor_name: str
    contribution_pct: float  # 正=看多，负=看空
    direction: str           # "bullish" | "bearish"


class WaterfallData(BaseModel):
    """多空归因瀑布图"""
    coin: str
    ts: int
    items: list[WaterfallItem]
    bullish_total: float
    bearish_total: float
    net_bias: float
    net_label: str  # "偏向看多" / "偏向看空" / "多空均衡"


class MacroSnapshot(BaseModel):
    """宏观数据快照 (v2.0 预留)"""
    dxy: Optional[dict] = None
    nasdaq: Optional[dict] = None
    gold: Optional[dict] = None
    oil: Optional[dict] = None
    vix: Optional[dict] = None
    events: list[dict] = []


class SourceHealth(BaseModel):
    """数据源健康状态"""
    name: str
    status: str         # "connected" | "degraded" | "disconnected"
    latency_ms: float = 0
    last_success_ts: int = 0
    error_count: int = 0


class AISnapshot(BaseModel):
    """发送给 AI 的结构化数据快照"""
    coin: str
    ts: int
    price: float
    high_24h: float
    low_24h: float

    liq_clusters_above: list[dict]
    liq_clusters_below: list[dict]
    vacuum_zones: list[dict]
    liq_imbalance_ratio: float

    # 7d 清算地图（远距阶梯策略核心依据）
    liq_clusters_above_7d: list[dict] = []
    liq_clusters_below_7d: list[dict] = []
    vacuum_zones_7d: list[dict] = []
    liq_imbalance_ratio_7d: float = 0

    cvd_contract_trend: str
    cvd_contract_delta_1h: float
    cvd_spot_trend: str
    cvd_spot_delta_1h: float
    cvd_divergence: str

    oi_current_usd: float
    oi_change_1h_pct: float
    oi_change_5m_pct: float
    oi_trend: str

    funding_rate_okx: Optional[float]
    funding_rate_binance: Optional[float]
    funding_interpretation: str
    funding_avg_7d: Optional[float] = None
    funding_exchanges: list[dict] = []

    basis_pct: float

    orderbook_bid_walls: list[dict]
    orderbook_ask_walls: list[dict]
    orderbook_bid_total_usd: float = 0
    orderbook_ask_total_usd: float = 0
    orderbook_spread_pct: float = 0

    recent_liq_24h_long_usd: float = 0
    recent_liq_24h_short_usd: float = 0
    # 向后兼容别名
    @property
    def recent_liq_30m_long_usd(self) -> float:
        return self.recent_liq_24h_long_usd
    @property
    def recent_liq_30m_short_usd(self) -> float:
        return self.recent_liq_24h_short_usd

    volume_profile_poc: float
    value_area_high: float
    value_area_low: float
    vwap: float

    atr_14: float
    market_temperature: float
    pin_risk_level: str

    # Phase 3 新增数据
    ls_ratio: Optional[float] = None
    ls_ratio_interpretation: str = ""
    fear_greed_index: Optional[float] = None
    etf_net_3d: Optional[float] = None
    etf_trend: str = ""
    etf_recent_days: list[dict] = []
    global_liq_long_24h: float = 0
    global_liq_short_24h: float = 0
    global_liq_long_1h: float = 0
    global_liq_short_1h: float = 0
    global_liq_ratio_24h: float = 1.0
    global_liq_largest_single: float = 0
    btc_max_pain: Optional[float] = None
    btc_dvol: Optional[float] = None
    dxy: Optional[float] = None
    dxy_change_pct: Optional[float] = None
    btc_dominance: Optional[float] = None
    taker_buy_ratio: Optional[float] = None
    taker_dominant: str = ""

    # Phase 4: 宏观市场数据
    nasdaq: Optional[float] = None
    nasdaq_change_pct: Optional[float] = None
    gold: Optional[float] = None
    gold_change_pct: Optional[float] = None
    sp500: Optional[float] = None
    sp500_change_pct: Optional[float] = None

    # Phase 5: 链上 + 波动率 + 宏观补充（供 AI 决策 + 阶梯策略）
    btc_mvrv: Optional[float] = None
    btc_hist_vol: Optional[float] = None
    btc_implied_vol: Optional[float] = None
    btc_iv_skew_1m: Optional[float] = None
    exchange_btc_total: Optional[float] = None
    exchange_btc_change_24h: Optional[float] = None
    exchange_btc_change_pct: Optional[float] = None
    ahr999: Optional[float] = None
    stablecoin_dominance: Optional[float] = None
    coinbase_btc_premium: Optional[float] = None
    usdt_otc_premium: Optional[float] = None
    us_10y_yield: Optional[float] = None
    fed_rate: Optional[float] = None

    # Phase 6: 补充指标（BBX已解析，补通至AI管线）
    btc_put_call_oi: Optional[float] = None
    usdt_market_cap: Optional[float] = None
    btc_hashrate: Optional[float] = None
    okx_ls_ratio_btc: Optional[float] = None
    binance_ls_ratio_btc: Optional[float] = None

    # Phase 7: 链上周期评分 (CPS) — BTC 全局状态机
    cycle_position: Optional[dict] = None

    # Phase 8: 流动性扫取检测 (Sweep Detection)
    liq_sweep_above_usd_1h: float = 0
    liq_sweep_below_usd_1h: float = 0
    liq_sweep_events: list[dict] = []

    # Phase 9: 均线箱体信号 (Range Signal)
    range_signal: Optional[dict] = None

    # Phase 10: 关键位状态机 (Key Level State Machine)
    key_levels: Optional[dict] = None

    # Phase 11: 1h 市场结构（Price Action / SMC · BOS/CHoCH 识别）
    # 顶层独立注入，让 AI 能在 §一/§四 阶段优先对齐结构方向，避免逆势误判
    market_structure: Optional[dict] = None

    # 清算热力图摘要（价格-时间维度密度峰值）
    liq_heatmap_hotspots: list[dict] = []  # [{price, total_usd, pct_above}]

    # 30d 清算地图（超远距阶梯参考）
    liq_clusters_above_30d: list[dict] = []
    liq_clusters_below_30d: list[dict] = []
    liq_imbalance_ratio_30d: float = 0

    # Coinglass 技术指标
    rsi_14: Optional[float] = None
    macd_histogram: Optional[float] = None
    macd_above_zero: Optional[bool] = None
    boll_upper: Optional[float] = None
    boll_middle: Optional[float] = None
    boll_lower: Optional[float] = None
    ema20: Optional[float] = None
    ma60_daily: Optional[float] = None
    ma120_daily: Optional[float] = None

    # 期权数据
    option_max_pain_price: Optional[float] = None
    option_nearest_expiry: str = ""
    option_call_oi: Optional[float] = None
    option_put_oi: Optional[float] = None

    # 大单追踪
    large_orders_buy_count: int = 0
    large_orders_sell_count: int = 0
    large_orders_net_usd: float = 0

    # 3 维多空比
    ls_ratio_top_account: Optional[float] = None
    ls_ratio_top_position: Optional[float] = None
    ls_ratio_long_pct: Optional[float] = None
    ls_ratio_short_pct: Optional[float] = None
    ls_ratio_change_24h: Optional[float] = None
    ls_top_acct_long_pct: Optional[float] = None
    ls_top_acct_short_pct: Optional[float] = None
    ls_top_acct_change_24h: Optional[float] = None
    oi_change_24h_pct: Optional[float] = None
    fear_greed_prev: Optional[int] = None

    # 巨鲸追踪
    whale_hl_alerts_count: int = 0
    whale_transfers_count: int = 0
    whale_net_direction: str = ""
    whale_hl_positions: list[dict] = []
    # 巨鲸链上转账 USD 流向（按 to_label/from_label=exchange 聚合）
    whale_transfer_inflow_usd: float = 0
    whale_transfer_outflow_usd: float = 0
    whale_transfer_net_usd: float = 0
    whale_top_transfers: list[dict] = []

    # Coinbase 溢价 + 稳定币 + 交易所OI排名
    coinbase_premium: float = 0
    coinbase_premium_trend: str = ""
    stablecoin_total_mcap: float = 0
    stablecoin_7d_change_pct: float = 0
    oi_exchange_rank: list[dict] = []

    # Phase 4: 规则引擎预计算结果
    rule_supports: list[dict] = []
    rule_resistances: list[dict] = []
    rule_stop_loss: list[dict] = []
    sniper_entries: list[dict] = []
    ladder_plans: list[dict] = []

    # K 线形态检测（4H 最新）
    candlestick_pattern_name: str = ""       # e.g. "锤子线" / "看涨吞没" / ""
    candlestick_pattern_side: str = ""       # "support" / "resistance" / ""
    candlestick_pattern_strength: float = 0  # 0~1

    # Phase 11: 净持仓 + 合约资金流 + TD序列
    net_position_trend: str = ""
    net_position_latest: Optional[float] = None
    net_position_change_24h: Optional[float] = None
    futures_coin_netflow_1h: Optional[float] = None
    futures_coin_netflow_trend: str = ""
    td_sequential_count: Optional[int] = None
    td_sequential_direction: str = ""
    poll_failures: dict[str, str] = {}

    # ── P1.2b · 新闻与地缘注入（optional；AI prompt 在有值时追加板块） ──
    news_brief_text: str = ""                  # Rolling 简报文本（≤3000 chars）
    news_brief_version: int = 0
    news_brief_trigger: str = ""               # "scheduled" / "blackswan" / "user"
    news_brief_updated_at: Optional[int] = None
    geo_overview: Optional[dict] = None        # GeoRiskOverview.model_dump() 精简版
    active_narratives: list[dict] = []          # [{theme_id, name_cn, direction, intensity, flip_flop_24h}]


class SignalSummary(BaseModel):
    """AI 一句话结论的结构化表达"""
    direction: str = ""  # "bullish" | "bearish" | "neutral"
    confidence: str = ""  # "high" | "medium" | "low"
    reason: str = ""
    raw_line: str = ""


class SniperPlan(BaseModel):
    """单个狙击方案的结构化表达"""
    direction: str = ""     # "long" / "short"
    entry: Optional[float] = None
    stop_loss: Optional[float] = None
    tp1: Optional[float] = None
    tp2: Optional[float] = None
    rr: Optional[float] = None
    logic: str = ""
    invalidation: str = ""
    raw_text: str = ""


class TradingPlanEntry(BaseModel):
    """回测可用的结构化交易计划条目"""
    tier: str = ""          # "short" | "mid" | "long"
    direction: str = ""     # "long" | "short"
    entry: Optional[float] = None
    stop_loss: Optional[float] = None
    tp1: Optional[float] = None
    tp2: Optional[float] = None
    rr: Optional[float] = None
    source: str = ""        # "engine" | "ai_inferred"
    logic: str = ""


class BacktestStats(BaseModel):
    """轻量级回测统计摘要（前置声明，供 AISnapshot 引用）"""
    coin: str = ""
    ts: int = 0
    total_signals: int = 0
    triggered: int = 0
    tp1_hit: int = 0
    sl_hit: int = 0
    pending: int = 0
    win_rate: float = 0
    avg_rr: float = 0
    by_tier: dict = {}
    by_direction: dict = {}
    by_source: dict = {}
    recent_signals: list[dict] = []


class AIAnalysisResult(BaseModel):
    """AI 分析输出"""
    coin: str
    ts: int
    price_at_analysis: float
    signal_summary: Optional[SignalSummary] = None
    market_overview: str
    key_levels: list[dict]
    stop_loss_suggestion: dict
    entry_zones: list[dict]
    sniper_setup: str = ""
    sniper_plans: list[SniperPlan] = []
    ladder_plan_text: str = ""
    trading_plan: str = ""
    trading_plan_entries: list[TradingPlanEntry] = []
    risk_warnings: list[str]
    scenario_analysis: list[dict]
    data_quality_feedback: str = ""
    raw_text: str
    user_prompt: str = ""

    # P1.7 · Prompt 升级附录：AI 产出的结构化 Factor Matrix / bias / conviction 等
    # 与 markdown 正文配对输出，供 trader_report_builder 优先采用；
    # 缺失或非法时 builder 自动回退到规则推断（零破坏）
    ai_matrix_json: Optional[dict] = None
