"""
全局配置加载：YAML + 环境变量。
整个后端通过 get_settings() 获取唯一配置实例。
"""

from __future__ import annotations

import os
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml
from dotenv import load_dotenv

_CONFIG_DIR = Path(__file__).resolve().parent
_CONFIG_FILE = _CONFIG_DIR / "config.yaml"
_ENV_FILE = _CONFIG_DIR.parent / ".env"

# 始终从 backend/.env 加载，避免启动目录不同导致密钥配置被静默跳过。
load_dotenv(_ENV_FILE)


@dataclass(frozen=True)
class CoinConfig:
    """单个币种的 Coinglass symbol 映射"""
    ccy: str
    symbol_cg: str
    symbol_cg_pair: str
    exchange_primary: str
    ct_val: float = 1.0
    contract_type: str = "linear"
    contract_size: float = 1.0
    margin_asset: str = "USDT"
    default: bool = False
    # Phase C：Coinbase 现货原生 API product_id（如 "BTC-USD"）
    #   - None / 缺省 → polls.coinbase_orderbook 自动派生 f"{ccy}-USD"
    #   - 空字符串 ""    → 显式禁用该币种 Coinbase 拉取（如小币 Coinbase 不上架）
    #   - 显式字符串 "X-Y" → 直接使用该 product_id
    symbol_coinbase: Optional[str] = None


@dataclass(frozen=True)
class CoinglassSourceConfig:
    """Coinglass 统一数据源配置"""
    base_url: str
    api_key_env: str
    api_key_default: str
    timeout_sec: int
    rate_limit_per_min: int
    provider_limit_per_min: int
    operational_limit_per_min: int
    daily_limit: int
    poll_intervals: dict[str, int]


@dataclass(frozen=True)
class BBXSourceConfig:
    """BBX 市场指数数据源配置"""
    url: str = "https://bbx.com/api/pc?module=v1/market/index"
    cache_ttl: int = 120
    timeout_sec: int = 15
    poll_interval: int = 120


@dataclass(frozen=True)
class BinanceSourceConfig:
    """Binance Futures 公共数据源配置"""
    base_url: str
    timeout_sec: int = 10
    use_for_ticker: bool = True
    use_for_klines: bool = True
    use_for_basis: bool = True
    ws_enabled: bool = True
    ws_url: str = "wss://fstream.binance.com/ws/!ticker@arr"
    ws_reconnect_min_sec: int = 2
    ws_reconnect_max_sec: int = 30


@dataclass(frozen=True)
class CoinbaseSourceConfig:
    """Coinbase Exchange 公开 REST 数据源配置（Phase C，仅 orderbook，免 auth）。

    速率限制：Coinbase 公开端点上限 10 req/s = 600/min。
    rate_per_min 默认 60（1s 间隔），4 币 × 1/90s ≈ 0.04 req/s 远低于上限。
    """
    base_url: str = "https://api.exchange.coinbase.com"
    timeout_sec: int = 15
    rate_per_min: int = 60
    poll_interval: int = 90


@dataclass(frozen=True)
class NansenSourceConfig:
    """Nansen API source config for the SMC confirmation layer.

    The key is resolved only from ``api_key_env`` at source construction time.
    """

    enabled: bool = True
    base_url: str = "https://api.nansen.ai"
    api_key_env: str = "NANSEN_API_KEY"
    timeout_sec: int = 20
    rate_limit_per_min: int = 30
    poll_intervals: dict[str, int] = field(default_factory=lambda: {
        "perp_screener": 900,
        "flow_intelligence": 3600,
        "exchange_flows": 14400,
        "market_breadth": 14400,
    })
    chains: list[str] = field(default_factory=lambda: ["ethereum", "base", "solana"])


@dataclass(frozen=True)
class LooknodeSourceConfig:
    """Looknode seven-exchange BTC transfer-flow source."""

    enabled: bool = True
    base_url: str = "https://www.looknode.com"
    timeout_sec: int = 15
    cache_ttl_sec: int = 21600
    stale_after_sec: int = 216000
    history_days: int = 730
    alert_daily_percentile: float = 99.0
    alert_multiday_percentile: float = 98.0
    crosscheck_abs_percentile: float = 75.0
    crosscheck_min_net_ratio: float = 0.03


@dataclass(frozen=True)
class BGeometricsSourceConfig:
    """BGeometrics（bitcoin-data.com）链上指标源。

    免费档硬限：8 次/小时、15 次/天（按 IP/token 计），近 4 年日级历史。
    专供 Bottom Model 模块补充 Coinglass 缺失的 MVRV-Z / Realized Loss 等指标；
    key 可选（无 key 也可用免费档），仅从 ``api_key_env`` 环境变量读取。
    """

    enabled: bool = True
    base_url: str = "https://bitcoin-data.com"
    api_key_env: str = "BGEOMETRICS_API_KEY"
    timeout_sec: int = 20
    hourly_limit: int = 8
    daily_limit: int = 15


@dataclass(frozen=True)
class YahooCMESourceConfig:
    """Yahoo Finance CME 比特币期货（BTC=F）周线量价源。

    非官方公开接口，无需 key；仅供 Bottom Model 的 CME 恐慌周量子信号。
    失败时该子信号缺省（fail-open），不影响其他因子。
    """

    enabled: bool = True
    base_url: str = "https://query1.finance.yahoo.com"
    timeout_sec: int = 20
    symbol: str = "BTC=F"


@dataclass(frozen=True)
class BottomModelConfig:
    """BTC 熊市底部证据与验证模型模块配置。

    - daily_run_hour_utc：每日自动采集+评分小时（UTC）。默认 1（北京 09:00），
      此时 Coinglass 链上指标（T-1 日更）与 BGeometrics 均已更新完整。
    - coinglass_spacing_sec：模块内相邻 Coinglass 请求的最小间隔，
      避免每日一轮采集瞬时挤占常规轮询的限流配额。
    - snapshot_retention_days：每日评分快照保留天数（有界存储纪律）。
    """

    enabled: bool = True
    data_dir: str = "data/bottom_model"
    daily_run_hour_utc: int = 1
    coinglass_spacing_sec: float = 10.0
    snapshot_retention_days: int = 800


@dataclass(frozen=True)
class ProcessorsConfig:
    cvd: dict[str, Any]
    percentile: dict[str, Any]
    market_temp: dict[str, Any]
    levels: dict[str, Any]
    orderbook: dict[str, Any]
    range_signal: dict[str, Any] = field(default_factory=dict)
    key_level_tracker: dict[str, Any] = field(default_factory=dict)
    orderbook_pressure: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AIProviderConfig:
    """单个 AI 提供商的配置"""
    name: str
    model: str
    api_base: str
    env_key: str


@dataclass(frozen=True)
class AINewsAgentConfig:
    """D12 · 新闻智能 Agent 配置（共享主 AI 的 key，可配置独立模型/预算）"""
    model: str = "deepseek-v4-flash"
    timeout_sec: int = 60
    max_retries: int = 2
    temperature: float = 0.2
    max_tokens_structurer: int = 2500
    max_tokens_brief: int = 1800
    batch_size_blackswan: int = 1
    batch_size_major: int = 3
    batch_size_normal: int = 5
    api_base: str = ""      # 空=沿用主 AI
    api_key: str = ""       # 空=沿用主 AI
    env_key: str = ""       # 若指定，优先从该环境变量读取 key
    # ── P1.2b · 编排循环节律 ──
    fetch_interval_sec: int = 600        # 新闻拉取 + 滤波 + 结构化（秒）
    brief_interval_sec: int = 3600       # Rolling Brief 重写周期
    backfill_interval_sec: int = 900     # 价格回填扫描周期
    decay_interval_sec: int = 1800       # Narrative/Geo decay 周期
    ledger_max_events: int = 500         # 账本最大条目
    ledger_max_age_sec: int = 172800     # 账本最长保留 48h
    blackswan_rewrite_brief: bool = True # 黑天鹅立即重写简报


@dataclass(frozen=True)
class AIConfig:
    active: str
    model: str
    timeout_sec: int
    max_retries: int
    cooldown_sec: int
    max_history: int
    auto_interval_sec: int = 0
    api_key: str = ""
    api_base: str = ""
    providers: dict = field(default_factory=dict)
    news_agent: AINewsAgentConfig = field(default_factory=AINewsAgentConfig)


@dataclass(frozen=True)
class EngineConfig:
    inactive_poll_sec: int = 120
    grace_period_sec: int = 60
    # ── Phase B：按币 poll 频率乘子（节流非主力币种，省 rate_limit/min 配额）──
    # interval_actual = base_interval / coin_priority[ccy]
    # 取值约定：1.0 = 满频；0.5 = 间隔翻倍；0.0 / 缺失 = 完全不 poll（对应 allow_coins 移除）
    # 缺省：BTC/ETH 1.0，SOL 0.5（实测主要节流目标）
    coin_priority: dict[str, float] = field(default_factory=lambda: {
        "BTC": 1.0, "ETH": 1.0, "SOL": 0.5,
    })


@dataclass(frozen=True)
class PushConfig:
    ticker_interval_ms: int
    factor_cards_interval_ms: int
    liq_map_interval_ms: int
    cvd_oi_interval_ms: int
    orderbook_interval_ms: int


@dataclass(frozen=True)
class ServerConfig:
    host: str
    port: int
    cors_origins: list[str]


@dataclass(frozen=True)
class EmailNotificationConfig:
    enabled: bool = False
    smtp_host: str = "smtp.163.com"
    smtp_port: int = 465
    smtp_user: str = ""
    smtp_pass: str = ""
    from_name: str = "LIQ监控"
    to: list[str] = field(default_factory=list)
    min_signal_tier: str = "A"
    cooldown_minutes: int = 45
    include_range: bool = True
    include_key_levels: bool = True
    # ── MAA（Market Action Analyzer）邮件通道（独立于关键位/箱体）──
    # 触发口径：accepted_scenario 切换 + bias∈{long,short} + conf≥min + 非 range_bound + dq=ok
    # 强信号通道（confidence≥strong + continuity.stance=reversal）走独立 dedup_key + 短 cooldown
    include_market_action: bool = True
    # Bottom Model 日线确认区提醒。独立于短线信号通道，但仍受 enabled 总开关控制。
    include_bottom_model: bool = True
    include_market_risk: bool = True
    market_action_min_confidence: int = 75
    market_action_strong_confidence: int = 85
    market_action_strong_cooldown_minutes: int = 20
    market_action_coins: list[str] = field(default_factory=lambda: ["BTC", "ETH"])


@dataclass(frozen=True)
class NotificationsConfig:
    email: EmailNotificationConfig = field(default_factory=EmailNotificationConfig)


@dataclass(frozen=True)
class MarketActionConfig:
    """Market Action Analyzer (MAA) 配置

    - enabled：是否启用 MAA 周期 AI 分析
    - auto_interval_sec：两次调用之间的最小间隔（秒）
    - max_history：历史报告保留条数（per coin）
    - include_prompt_in_api：默认接口是否返回完整 prompt_debug（节省带宽可关闭）
    """
    enabled: bool = True
    auto_interval_sec: int = 600
    max_history: int = 200
    include_prompt_in_api: bool = True


@dataclass(frozen=True)
class StrategicConfig:
    """Strategic AI 决策官配置（PR-2 新增）。

    设计纪律：
      - 与 MAA 共享 `settings.ai` 的 DeepSeek 配置（同一 api_key / model / timeout / retries）
      - 周期独立：默认 900s（15min），比 MAA 600s 更稀释——Strategic 是中线决策官
      - max_history 100 / 币（中线报告体积更大，比 MAA 200 减半）
      - include_prompt_in_api：与 MAA 同语义，默认 True 便于前端 PromptDebug 复用

    字段：
      - enabled：是否启用 Strategic 周期 AI 分析（false = 不调度，但手动 fire 接口仍可工作）
      - auto_interval_sec：两次调用最小间隔（秒），最小值 300（避免暴打 LLM）
      - max_history：历史报告保留条数（per coin）
      - include_prompt_in_api：API 返回时是否带完整 prompt_debug
    """
    enabled: bool = True
    auto_interval_sec: int = 900
    max_history: int = 100
    include_prompt_in_api: bool = True


# PR-3 · NOFXConfig 已下线（NOFX 外部 AI 接口随数学引擎一并删除）


@dataclass(frozen=True)
class ScalpSignalConfig:
    """短线预测合约信号引擎 · 启动级配置（运行参数）

    策略本身的启用 / 阈值 / 通知等用户可调项走 JSON（ScalpConfig）由前端配置面板管理，
    yaml 中只配启动开关、主循环间隔、数据目录等"系统层"参数。
    """
    enabled: bool = True
    tick_interval_sec: float = 30.0
    data_dir: str = "data/scalp_signal"  # 相对 backend/ 工作目录


@dataclass(frozen=True)
class TrendMonitorConfig:
    """BTC 原生趋势与资金流模块；只读分析，不含任何交易执行语义。"""
    enabled: bool = True
    evaluation_interval_sec: int = 300
    data_dir: str = "data/trend"
    algorithm_version: str = "btc-native-v3"
    email_enabled: bool = True
    footprint_enabled: bool = True
    ai_review_enabled: bool = True
    component_weights: dict[str, tuple[float, float, float]] = field(default_factory=lambda: {
        "15m": (0.35, 0.45, 0.20), "1h": (0.40, 0.40, 0.20),
        "4h": (0.50, 0.30, 0.20), "1d": (0.55, 0.25, 0.20),
    })
    core_weights: tuple[float, float, float] = (0.30, 0.50, 0.20)
    direction_threshold: float = 25.0
    confirm_4h_threshold: float = 45.0
    confirm_1h_threshold: float = 25.0
    strong_opposite_1d_threshold: float = 45.0
    confirmation_bars: int = 3
    modifier_cap: float = 12.0
    wallet_modifier_scale_btc: float = 5000.0
    snapshot_retention_days: int = 400
    active_flow_1h_percentile: float = 99.0
    active_flow_24h_percentile: float = 97.5
    active_flow_1h_min_ratio: float = 0.10
    behavior_futures_net_ratio_min: float = 0.005
    behavior_spot_net_ratio_confirm: float = 0.003
    behavior_price_atr_min: float = 0.15
    behavior_oi_change_1h_min: float = 0.001
    behavior_oi_change_4h_min: float = 0.0025
    behavior_subbar_share_min: float = 7 / 12


@dataclass(frozen=True)
class MarketRiskConfig:
    """联合风险预警系统启动配置。

    阈值不放在 YAML；它们只能来自版本化 calibration artifact。这样配置项
    不会看似可调、实际却绕过回测准入。未知键在启动时直接报错。
    """

    enabled: bool = True
    coins: tuple[str, ...] = ("BTC",)
    mode: str = "shadow"
    shadow_mode: bool = True
    tick_interval_sec: int = 30
    data_dir: str = "data/market_risk"
    calibration_artifact: str = "config/market_risk_calibration_v1.json"
    email_enabled: bool = False
    raw_event_store_enabled: bool = True
    raw_event_queue_max: int = 20_000
    raw_event_batch_size: int = 2_000
    raw_event_segment_sec: int = 300
    raw_event_max_lateness_sec: int = 120
    raw_event_max_total_bytes: int = 50 * 1024 * 1024 * 1024
    raw_event_min_free_bytes: int = 10 * 1024 * 1024 * 1024
    raw_event_min_free_inodes: int = 200_000
    source_max_age_sec: dict[str, int] = field(default_factory=lambda: {
        "spot_demand": 180,
        "leveraged_positioning": 360,
        # Coinglass 聚合清算图最小缓存 300s；加 90s 轮询和 30s 请求预算。
        "liquidation_risk": 420,
        "liquidity_structure": 360,
        "market_response": 180,
        "context": 86_400,
    })

    def effective_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "coins": list(self.coins),
            "mode": self.mode,
            "shadow_mode": self.shadow_mode,
            "tick_interval_sec": self.tick_interval_sec,
            "data_dir": self.data_dir,
            "calibration_artifact": self.calibration_artifact,
            "email_enabled": self.email_enabled,
            "raw_event_store_enabled": self.raw_event_store_enabled,
            "raw_event_queue_max": self.raw_event_queue_max,
            "raw_event_batch_size": self.raw_event_batch_size,
            "raw_event_segment_sec": self.raw_event_segment_sec,
            "raw_event_max_lateness_sec": self.raw_event_max_lateness_sec,
            "raw_event_max_total_bytes": self.raw_event_max_total_bytes,
            "raw_event_min_free_bytes": self.raw_event_min_free_bytes,
            "raw_event_min_free_inodes": self.raw_event_min_free_inodes,
            "source_max_age_sec": dict(self.source_max_age_sec),
        }

    @staticmethod
    def consumer_registry() -> dict[str, str]:
        """配置消费方登记；契约测试防止新增参数沦为无效旋钮。"""
        return {
            "enabled": "MarketRiskEngine.start + market-risk API gate",
            "coins": "MarketRiskEngine loop and route scope",
            "mode": "MarketRiskEngine mode and notification/readiness gates",
            "shadow_mode": "notification eligibility and UI",
            "tick_interval_sec": "MarketRiskEngine._run_loop",
            "data_dir": "MarketRiskStore RawEventStore OnchainEntityStore",
            "calibration_artifact": "MarketRiskEngine._load_calibration",
            "email_enabled": "MarketRiskEngine outbox gate",
            "raw_event_store_enabled": "RawEventStore construction",
            "raw_event_queue_max": "RawEventStore queue hard bound",
            "raw_event_batch_size": "RawEventStore batch hard bound",
            "raw_event_segment_sec": "RawEventStore atomic segment boundary",
            "raw_event_max_lateness_sec": "RawEventStore late-event calibration boundary",
            "raw_event_max_total_bytes": "RawEventStore storage admission gate",
            "raw_event_min_free_bytes": "RawEventStore disk free-space gate",
            "raw_event_min_free_inodes": "RawEventStore inode free-space gate",
            "source_max_age_sec": "MarketRiskEngine SourceQuality gates",
        }

    @property
    def version_hash(self) -> str:
        payload = json.dumps(self.effective_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()[:16]


@dataclass(frozen=True)
class RetentionConfig:
    """数据保留策略（P0 · 统一配置入口）。

    只集中"部署者需要调整"的保留天数；各模块内部固定策略
    （sweep_watch 7d、facts_raw 48h、trend/bottom_model 400/800d 等）
    仍由模块自治，登记见 config.yaml retention 段注释。
    环境变量 LIQUIDITY_WALL_KEEP_DAYS 优先于 yaml（运维应急杆）。
    """
    liquidity_wall_days: int = 30
    orderflow_hourly_days: int = 180
    orderflow_daily_days: int = 400


@dataclass(frozen=True)
class Settings:
    coins: dict[str, CoinConfig]
    coinglass: CoinglassSourceConfig
    binance: BinanceSourceConfig
    bbx: BBXSourceConfig
    processors: ProcessorsConfig
    ai: AIConfig
    push: PushConfig
    server: ServerConfig
    engine: EngineConfig = field(default_factory=EngineConfig)
    notifications: NotificationsConfig = field(default_factory=NotificationsConfig)
    # PR-3 · nofx 字段已下线
    market_action: MarketActionConfig = field(default_factory=MarketActionConfig)
    strategic: StrategicConfig = field(default_factory=StrategicConfig)
    scalp_signal: ScalpSignalConfig = field(default_factory=ScalpSignalConfig)
    trend_monitor: TrendMonitorConfig = field(default_factory=TrendMonitorConfig)
    coinbase: CoinbaseSourceConfig = field(default_factory=CoinbaseSourceConfig)
    nansen: NansenSourceConfig = field(default_factory=NansenSourceConfig)
    looknode: LooknodeSourceConfig = field(default_factory=LooknodeSourceConfig)
    bgeometrics: BGeometricsSourceConfig = field(default_factory=BGeometricsSourceConfig)
    yahoo_cme: YahooCMESourceConfig = field(default_factory=YahooCMESourceConfig)
    bottom_model: BottomModelConfig = field(default_factory=BottomModelConfig)
    retention: RetentionConfig = field(default_factory=RetentionConfig)
    market_risk: MarketRiskConfig = field(default_factory=MarketRiskConfig)
    default_coin: str = "BTC"

    def get_coin(self, ccy: str) -> CoinConfig:
        ccy_upper = ccy.upper()
        if ccy_upper not in self.coins:
            raise ValueError(f"Unsupported coin: {ccy_upper}. Available: {list(self.coins.keys())}")
        return self.coins[ccy_upper]

    @property
    def supported_coins(self) -> list[str]:
        return list(self.coins.keys())


_settings_instance: Optional[Settings] = None


def _load_yaml() -> dict:
    with open(_CONFIG_FILE, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _build_settings(raw: dict) -> Settings:
    coins: dict[str, CoinConfig] = {}
    default_coin = "BTC"
    for ccy, coin_raw in raw["coins"].items():
        cc = CoinConfig(
            ccy=coin_raw["ccy"],
            symbol_cg=coin_raw["symbol_cg"],
            symbol_cg_pair=coin_raw["symbol_cg_pair"],
            exchange_primary=coin_raw["exchange_primary"],
            ct_val=float(coin_raw.get("ct_val", 1.0)),
            contract_type=str(coin_raw.get("contract_type", "linear") or "linear").lower(),
            contract_size=max(1e-12, float(coin_raw.get("contract_size", 1.0))),
            margin_asset=str(coin_raw.get("margin_asset", "USDT") or "USDT").upper(),
            default=coin_raw.get("default", False),
            symbol_coinbase=coin_raw.get("symbol_coinbase"),
        )
        coins[ccy] = cc
        if cc.default:
            default_coin = ccy

    src = raw["sources"]
    cg_raw = src["coinglass"]
    operational_limit = int(
        cg_raw.get("operational_limit_per_min", cg_raw.get("rate_limit_per_min", 10))
    )
    provider_limit = int(cg_raw.get("provider_limit_per_min", 11))
    if operational_limit < 1 or provider_limit < 1 or operational_limit > provider_limit:
        raise ValueError(
            "sources.coinglass requires 1 <= operational_limit_per_min <= provider_limit_per_min"
        )
    coinglass = CoinglassSourceConfig(
        base_url=cg_raw["base_url"],
        api_key_env=cg_raw["api_key_env"],
        api_key_default=cg_raw.get("api_key_default", ""),
        timeout_sec=int(cg_raw.get("timeout_sec", 15)),
        rate_limit_per_min=operational_limit,
        provider_limit_per_min=provider_limit,
        operational_limit_per_min=operational_limit,
        daily_limit=int(cg_raw.get("daily_limit", 50000)),
        poll_intervals=dict(cg_raw.get("poll_intervals", {})),
    )
    bbx_raw = src.get("bbx", {})
    bbx = BBXSourceConfig(
        url=bbx_raw.get("url", "https://bbx.com/api/pc?module=v1/market/index"),
        cache_ttl=int(bbx_raw.get("cache_ttl", 120)),
        timeout_sec=int(bbx_raw.get("timeout_sec", 15)),
        poll_interval=int(bbx_raw.get("poll_interval", 120)),
    )
    bn_raw = src.get("binance", {})
    binance = BinanceSourceConfig(
        base_url=bn_raw.get("base_url", "https://fapi.binance.com"),
        timeout_sec=int(bn_raw.get("timeout_sec", 10)),
        use_for_ticker=bool(bn_raw.get("use_for_ticker", True)),
        use_for_klines=bool(bn_raw.get("use_for_klines", True)),
        use_for_basis=bool(bn_raw.get("use_for_basis", True)),
        ws_enabled=bool(bn_raw.get("ws_enabled", True)),
        ws_url=bn_raw.get("ws_url", "wss://fstream.binance.com/ws/!ticker@arr"),
        ws_reconnect_min_sec=int(bn_raw.get("ws_reconnect_min_sec", 2)),
        ws_reconnect_max_sec=int(bn_raw.get("ws_reconnect_max_sec", 30)),
    )

    cb_raw = src.get("coinbase", {})
    coinbase = CoinbaseSourceConfig(
        base_url=cb_raw.get("base_url", "https://api.exchange.coinbase.com"),
        timeout_sec=int(cb_raw.get("timeout_sec", 15)),
        rate_per_min=int(cb_raw.get("rate_per_min", 60)),
        poll_interval=int(cb_raw.get("poll_interval", 90)),
    )

    nsn_raw = src.get("nansen", {}) or {}
    nsn_intervals = nsn_raw.get("poll_intervals") or {}
    nansen = NansenSourceConfig(
        enabled=bool(nsn_raw.get("enabled", True)),
        base_url=nsn_raw.get("base_url", "https://api.nansen.ai"),
        api_key_env=nsn_raw.get("api_key_env", "NANSEN_API_KEY"),
        timeout_sec=int(nsn_raw.get("timeout_sec", 20)),
        rate_limit_per_min=int(nsn_raw.get("rate_limit_per_min", 30)),
        poll_intervals={
            "perp_screener": int(nsn_intervals.get("perp_screener", 900)),
            "flow_intelligence": int(nsn_intervals.get("flow_intelligence", 3600)),
            "exchange_flows": int(nsn_intervals.get("exchange_flows", 14400)),
            "market_breadth": int(nsn_intervals.get("market_breadth", 14400)),
        },
        chains=[
            str(chain) for chain in nsn_raw.get(
                "chains", ["ethereum", "base", "solana"],
            )
        ],
    )

    looknode_raw = src.get("looknode", {}) or {}
    looknode = LooknodeSourceConfig(
        enabled=bool(looknode_raw.get("enabled", True)),
        base_url=str(looknode_raw.get("base_url", "https://www.looknode.com")).rstrip("/"),
        timeout_sec=max(3, int(looknode_raw.get("timeout_sec", 15))),
        cache_ttl_sec=max(3600, int(looknode_raw.get("cache_ttl_sec", 21600))),
        stale_after_sec=max(86400, int(looknode_raw.get("stale_after_sec", 216000))),
        history_days=max(400, int(looknode_raw.get("history_days", 730))),
        alert_daily_percentile=max(
            90.0, min(100.0, float(looknode_raw.get("alert_daily_percentile", 99.0))),
        ),
        alert_multiday_percentile=max(
            90.0, min(100.0, float(looknode_raw.get("alert_multiday_percentile", 98.0))),
        ),
        crosscheck_abs_percentile=max(
            50.0, min(100.0, float(looknode_raw.get("crosscheck_abs_percentile", 75.0))),
        ),
        crosscheck_min_net_ratio=max(
            0.0, min(1.0, float(looknode_raw.get("crosscheck_min_net_ratio", 0.03))),
        ),
    )

    bg_raw = src.get("bgeometrics", {}) or {}
    bgeometrics = BGeometricsSourceConfig(
        enabled=bool(bg_raw.get("enabled", True)),
        base_url=str(bg_raw.get("base_url", "https://bitcoin-data.com")).rstrip("/"),
        api_key_env=str(bg_raw.get("api_key_env", "BGEOMETRICS_API_KEY")),
        timeout_sec=max(5, int(bg_raw.get("timeout_sec", 20))),
        hourly_limit=max(1, int(bg_raw.get("hourly_limit", 8))),
        daily_limit=max(1, int(bg_raw.get("daily_limit", 15))),
    )

    yc_raw = src.get("yahoo_cme", {}) or {}
    yahoo_cme = YahooCMESourceConfig(
        enabled=bool(yc_raw.get("enabled", True)),
        base_url=str(yc_raw.get("base_url", "https://query1.finance.yahoo.com")).rstrip("/"),
        timeout_sec=max(5, int(yc_raw.get("timeout_sec", 20))),
        symbol=str(yc_raw.get("symbol", "BTC=F")),
    )

    processors = ProcessorsConfig(**raw["processors"])

    ai_raw = raw["ai"]
    active_provider = ai_raw.get("active", "openai")
    providers_raw = ai_raw.get("providers", {})
    providers: dict[str, AIProviderConfig] = {}
    for name, p in providers_raw.items():
        providers[name] = AIProviderConfig(
            name=name,
            model=p["model"],
            api_base=p["api_base"],
            env_key=p["env_key"],
        )

    if active_provider not in providers:
        raise ValueError(
            f"AI active provider '{active_provider}' not found in providers: {list(providers.keys())}"
        )

    active = providers[active_provider]
    api_key = os.getenv(active.env_key, "") or os.getenv("AI_API_KEY", "")

    news_agent_raw = ai_raw.get("news_agent", {}) or {}
    news_env_key = str(news_agent_raw.get("env_key") or "").strip()
    news_api_key = ""
    if news_env_key:
        news_api_key = os.getenv(news_env_key, "")
    if not news_api_key:
        news_api_key = api_key  # 沿用主 AI key（默认行为）
    news_agent_cfg = AINewsAgentConfig(
        model=str(news_agent_raw.get("model") or "deepseek-v4-flash"),
        timeout_sec=int(news_agent_raw.get("timeout_sec") or 60),
        max_retries=int(news_agent_raw.get("max_retries") or 2),
        temperature=float(news_agent_raw.get("temperature") or 0.2),
        max_tokens_structurer=int(news_agent_raw.get("max_tokens_structurer") or 2500),
        max_tokens_brief=int(news_agent_raw.get("max_tokens_brief") or 1800),
        batch_size_blackswan=int(news_agent_raw.get("batch_size_blackswan") or 1),
        batch_size_major=int(news_agent_raw.get("batch_size_major") or 3),
        batch_size_normal=int(news_agent_raw.get("batch_size_normal") or 5),
        api_base=str(news_agent_raw.get("api_base") or "").strip() or active.api_base,
        api_key=news_api_key,
        env_key=news_env_key,
        fetch_interval_sec=int(news_agent_raw.get("fetch_interval_sec") or 600),
        brief_interval_sec=int(news_agent_raw.get("brief_interval_sec") or 3600),
        backfill_interval_sec=int(news_agent_raw.get("backfill_interval_sec") or 900),
        decay_interval_sec=int(news_agent_raw.get("decay_interval_sec") or 1800),
        ledger_max_events=int(news_agent_raw.get("ledger_max_events") or 500),
        ledger_max_age_sec=int(news_agent_raw.get("ledger_max_age_sec") or 172800),
        blackswan_rewrite_brief=bool(news_agent_raw.get("blackswan_rewrite_brief", True)),
    )

    ai = AIConfig(
        active=active_provider,
        model=active.model,
        timeout_sec=ai_raw["timeout_sec"],
        max_retries=ai_raw["max_retries"],
        cooldown_sec=ai_raw["cooldown_sec"],
        max_history=ai_raw["max_history"],
        auto_interval_sec=ai_raw.get("auto_interval_sec", 0),
        api_key=api_key,
        api_base=active.api_base,
        providers=providers,
        news_agent=news_agent_cfg,
    )

    push = PushConfig(**raw["push"])
    server = ServerConfig(**raw["server"])

    eng_raw = raw.get("engine", {})
    coin_prio_raw = eng_raw.get("coin_priority") or {}
    coin_prio: dict[str, float] = {}
    for ccy, val in coin_prio_raw.items() if isinstance(coin_prio_raw, dict) else []:
        try:
            v = float(val)
            if v > 0:
                coin_prio[str(ccy).upper()] = v
        except (TypeError, ValueError):
            continue
    if not coin_prio:
        coin_prio = {"BTC": 1.0, "ETH": 1.0, "SOL": 0.5}
    engine_cfg = EngineConfig(
        inactive_poll_sec=eng_raw.get("inactive_poll_sec", 120),
        grace_period_sec=eng_raw.get("grace_period_sec", 60),
        coin_priority=coin_prio,
    )

    notif_raw = raw.get("notifications", {})
    email_raw = notif_raw.get("email", {})
    # MAA 币种白名单：缺省时只发 BTC/ETH（与 dataclass 默认一致）
    maa_coins_raw = email_raw.get("market_action_coins")
    if isinstance(maa_coins_raw, list) and maa_coins_raw:
        maa_coins = [str(c).upper() for c in maa_coins_raw]
    else:
        maa_coins = ["BTC", "ETH"]
    email_cfg = EmailNotificationConfig(
        enabled=email_raw.get("enabled", False),
        smtp_host=email_raw.get("smtp_host", "smtp.163.com"),
        smtp_port=email_raw.get("smtp_port", 465),
        smtp_user=os.getenv("SMTP_USER", email_raw.get("smtp_user", "")),
        smtp_pass=os.getenv("SMTP_PASS", email_raw.get("smtp_pass", "")),
        from_name=email_raw.get("from_name", "LIQ监控"),
        to=email_raw.get("to", []),
        min_signal_tier=email_raw.get("min_signal_tier", "A"),
        cooldown_minutes=email_raw.get("cooldown_minutes", 45),
        include_range=email_raw.get("include_range", True),
        include_key_levels=email_raw.get("include_key_levels", True),
        # MAA 通道（默认开 · 阈值与 dataclass 默认一致）
        include_market_action=email_raw.get("include_market_action", True),
        include_bottom_model=email_raw.get("include_bottom_model", True),
        include_market_risk=email_raw.get("include_market_risk", True),
        market_action_min_confidence=int(email_raw.get("market_action_min_confidence", 75) or 75),
        market_action_strong_confidence=int(email_raw.get("market_action_strong_confidence", 85) or 85),
        market_action_strong_cooldown_minutes=int(
            email_raw.get("market_action_strong_cooldown_minutes", 20) or 20
        ),
        market_action_coins=maa_coins,
    )
    notifications_cfg = NotificationsConfig(email=email_cfg)

    # PR-3 · NOFXConfig 装配已下线（接口随数学引擎删除）

    maa_raw = raw.get("market_action", {}) or {}
    maa_cfg = MarketActionConfig(
        enabled=bool(maa_raw.get("enabled", True)),
        auto_interval_sec=max(60, int(maa_raw.get("auto_interval_sec", 600) or 600)),
        max_history=max(10, int(maa_raw.get("max_history", 200) or 200)),
        include_prompt_in_api=bool(maa_raw.get("include_prompt_in_api", True)),
    )

    # PR-2 · Strategic AI 决策官（中线 15min 周期）
    strat_raw = raw.get("strategic", {}) or {}
    strat_cfg = StrategicConfig(
        enabled=bool(strat_raw.get("enabled", True)),
        auto_interval_sec=max(300, int(strat_raw.get("auto_interval_sec", 900) or 900)),
        max_history=max(10, int(strat_raw.get("max_history", 100) or 100)),
        include_prompt_in_api=bool(strat_raw.get("include_prompt_in_api", True)),
    )

    # 短线预测合约信号引擎（30s 周期，独立模块）
    scalp_raw = raw.get("scalp_signal", {}) or {}
    scalp_cfg = ScalpSignalConfig(
        enabled=bool(scalp_raw.get("enabled", True)),
        tick_interval_sec=max(5.0, float(scalp_raw.get("tick_interval_sec", 30.0) or 30.0)),
        data_dir=str(scalp_raw.get("data_dir", "data/scalp_signal") or "data/scalp_signal"),
    )

    trend_raw = raw.get("trend_monitor", {}) or {}
    default_component_weights = {
        "15m": (0.35, 0.45, 0.20), "1h": (0.40, 0.40, 0.20),
        "4h": (0.50, 0.30, 0.20), "1d": (0.55, 0.25, 0.20),
    }
    component_weights = {}
    for tf, fallback in default_component_weights.items():
        values = (trend_raw.get("component_weights", {}) or {}).get(tf, fallback)
        parsed = tuple(float(value) for value in values)
        if len(parsed) != 3 or abs(sum(parsed) - 1.0) > 1e-6 or min(parsed) < 0:
            raise ValueError(f"trend_monitor.component_weights.{tf} must contain 3 non-negative values summing to 1")
        component_weights[tf] = parsed
    core_weights = tuple(float(value) for value in trend_raw.get("core_weights", (0.30, 0.50, 0.20)))
    if len(core_weights) != 3 or abs(sum(core_weights) - 1.0) > 1e-6 or min(core_weights) < 0:
        raise ValueError("trend_monitor.core_weights must contain 3 non-negative values summing to 1")
    trend_cfg = TrendMonitorConfig(
        enabled=bool(trend_raw.get("enabled", True)),
        evaluation_interval_sec=max(
            300, int(trend_raw.get("evaluation_interval_sec", 300) or 300),
        ),
        data_dir=str(trend_raw.get("data_dir", "data/trend") or "data/trend"),
        algorithm_version=str(
            trend_raw.get("algorithm_version", "btc-native-v3") or "btc-native-v3"
        ),
        email_enabled=bool(trend_raw.get("email_enabled", True)),
        footprint_enabled=bool(trend_raw.get("footprint_enabled", True)),
        ai_review_enabled=bool(trend_raw.get("ai_review_enabled", True)),
        component_weights=component_weights,
        core_weights=core_weights,
        direction_threshold=max(1.0, float(trend_raw.get("direction_threshold", 25.0))),
        confirm_4h_threshold=max(1.0, float(trend_raw.get("confirm_4h_threshold", 45.0))),
        confirm_1h_threshold=max(1.0, float(trend_raw.get("confirm_1h_threshold", 25.0))),
        strong_opposite_1d_threshold=max(1.0, float(trend_raw.get("strong_opposite_1d_threshold", 45.0))),
        confirmation_bars=max(1, int(trend_raw.get("confirmation_bars", 3))),
        modifier_cap=max(0.0, min(12.0, float(trend_raw.get("modifier_cap", 12.0)))),
        wallet_modifier_scale_btc=max(1.0, float(trend_raw.get("wallet_modifier_scale_btc", 5000.0))),
        snapshot_retention_days=max(30, int(trend_raw.get("snapshot_retention_days", 400))),
        active_flow_1h_percentile=max(90.0, min(100.0, float(trend_raw.get("active_flow_1h_percentile", 99.0)))),
        active_flow_24h_percentile=max(90.0, min(100.0, float(trend_raw.get("active_flow_24h_percentile", 97.5)))),
        active_flow_1h_min_ratio=max(0.0, min(1.0, float(trend_raw.get("active_flow_1h_min_ratio", 0.10)))),
        behavior_futures_net_ratio_min=max(
            1e-6, min(1.0, float(trend_raw.get("behavior_futures_net_ratio_min", 0.005))),
        ),
        behavior_spot_net_ratio_confirm=max(
            1e-6, min(1.0, float(trend_raw.get("behavior_spot_net_ratio_confirm", 0.003))),
        ),
        behavior_price_atr_min=max(
            0.01, float(trend_raw.get("behavior_price_atr_min", 0.15)),
        ),
        behavior_oi_change_1h_min=max(
            1e-6, float(trend_raw.get("behavior_oi_change_1h_min", 0.001)),
        ),
        behavior_oi_change_4h_min=max(
            1e-6, float(trend_raw.get("behavior_oi_change_4h_min", 0.0025)),
        ),
        behavior_subbar_share_min=max(
            0.5, min(1.0, float(trend_raw.get("behavior_subbar_share_min", 7 / 12))),
        ),
    )

    bottom_raw = raw.get("bottom_model", {}) or {}
    bottom_model_cfg = BottomModelConfig(
        enabled=bool(bottom_raw.get("enabled", True)),
        data_dir=str(bottom_raw.get("data_dir", "data/bottom_model") or "data/bottom_model"),
        daily_run_hour_utc=max(0, min(23, int(bottom_raw.get("daily_run_hour_utc", 1)))),
        coinglass_spacing_sec=max(1.0, float(bottom_raw.get("coinglass_spacing_sec", 10.0))),
        snapshot_retention_days=max(90, int(bottom_raw.get("snapshot_retention_days", 800))),
    )

    retention_raw = raw.get("retention", {}) or {}
    retention_cfg = RetentionConfig(
        liquidity_wall_days=max(3, min(90, int(retention_raw.get("liquidity_wall_days", 30)))),
        orderflow_hourly_days=max(7, min(3650, int(retention_raw.get("orderflow_hourly_days", 180)))),
        orderflow_daily_days=max(30, min(3650, int(retention_raw.get("orderflow_daily_days", 400)))),
    )

    market_risk_raw = raw.get("market_risk", {}) or {}
    if not isinstance(market_risk_raw, dict):
        raise ValueError("market_risk must be a mapping")
    market_risk_allowed = {
        "enabled", "coins", "mode", "shadow_mode", "tick_interval_sec", "data_dir",
        "calibration_artifact", "email_enabled", "raw_event_store_enabled",
        "raw_event_queue_max", "raw_event_batch_size", "raw_event_segment_sec",
        "raw_event_max_lateness_sec",
        "raw_event_max_total_bytes", "raw_event_min_free_bytes",
        "raw_event_min_free_inodes", "source_max_age_sec",
    }
    unknown_market_risk = sorted(set(market_risk_raw) - market_risk_allowed)
    if unknown_market_risk:
        raise ValueError(
            "unknown market_risk config keys: " + ", ".join(unknown_market_risk)
        )
    risk_coins_raw = market_risk_raw.get("coins", ["BTC"])
    if not isinstance(risk_coins_raw, (list, tuple)) or not risk_coins_raw:
        raise ValueError("market_risk.coins must be a non-empty list")
    risk_coins = tuple(str(value).upper() for value in risk_coins_raw)
    unsupported_risk_coins = sorted(set(risk_coins) - set(coins))
    if unsupported_risk_coins:
        raise ValueError(
            "market_risk.coins contains unsupported coins: "
            + ", ".join(unsupported_risk_coins)
        )
    default_risk_ages = MarketRiskConfig().source_max_age_sec
    risk_ages_raw = market_risk_raw.get("source_max_age_sec", {}) or {}
    if not isinstance(risk_ages_raw, dict):
        raise ValueError("market_risk.source_max_age_sec must be a mapping")
    unknown_risk_ages = sorted(set(risk_ages_raw) - set(default_risk_ages))
    if unknown_risk_ages:
        raise ValueError(
            "unknown market_risk.source_max_age_sec keys: " + ", ".join(unknown_risk_ages)
        )
    legacy_shadow = bool(market_risk_raw.get("shadow_mode", True))
    risk_mode = str(
        market_risk_raw.get(
            "mode", "shadow" if legacy_shadow else "production_read_only",
        )
    )
    if risk_mode not in {"shadow", "production_read_only", "production_alerting"}:
        raise ValueError("market_risk.mode must be shadow, production_read_only, or production_alerting")
    if "mode" in market_risk_raw and "shadow_mode" in market_risk_raw:
        if legacy_shadow != (risk_mode == "shadow"):
            raise ValueError("market_risk.mode conflicts with legacy shadow_mode")
    market_risk_cfg = MarketRiskConfig(
        enabled=bool(market_risk_raw.get("enabled", True)),
        coins=risk_coins,
        mode=risk_mode,
        shadow_mode=risk_mode == "shadow",
        tick_interval_sec=max(5, int(market_risk_raw.get("tick_interval_sec", 30))),
        data_dir=str(market_risk_raw.get("data_dir", "data/market_risk") or "data/market_risk"),
        calibration_artifact=str(
            market_risk_raw.get(
                "calibration_artifact", "config/market_risk_calibration_v1.json",
            ) or "config/market_risk_calibration_v1.json"
        ),
        email_enabled=bool(market_risk_raw.get("email_enabled", False)),
        raw_event_store_enabled=bool(market_risk_raw.get("raw_event_store_enabled", True)),
        raw_event_queue_max=max(1_000, min(200_000, int(
            market_risk_raw.get("raw_event_queue_max", 20_000),
        ))),
        raw_event_batch_size=max(100, min(10_000, int(
            market_risk_raw.get("raw_event_batch_size", 2_000),
        ))),
        raw_event_segment_sec=max(60, min(3600, int(
            market_risk_raw.get("raw_event_segment_sec", 300),
        ))),
        raw_event_max_lateness_sec=max(0, min(1800, int(
            market_risk_raw.get("raw_event_max_lateness_sec", 120),
        ))),
        raw_event_max_total_bytes=max(1024**3, int(
            market_risk_raw.get("raw_event_max_total_bytes", 50 * 1024**3),
        )),
        raw_event_min_free_bytes=max(1024**3, int(
            market_risk_raw.get("raw_event_min_free_bytes", 10 * 1024**3),
        )),
        raw_event_min_free_inodes=max(10_000, int(
            market_risk_raw.get("raw_event_min_free_inodes", 200_000),
        )),
        source_max_age_sec={
            key: max(420 if key == "liquidation_risk" else 5, int(risk_ages_raw.get(key, value)))
            for key, value in default_risk_ages.items()
        },
    )

    return Settings(
        coins=coins,
        coinglass=coinglass,
        binance=binance,
        bbx=bbx,
        coinbase=coinbase,
        nansen=nansen,
        looknode=looknode,
        bgeometrics=bgeometrics,
        yahoo_cme=yahoo_cme,
        bottom_model=bottom_model_cfg,
        processors=processors,
        ai=ai,
        push=push,
        server=server,
        engine=engine_cfg,
        notifications=notifications_cfg,
        market_action=maa_cfg,
        strategic=strat_cfg,
        scalp_signal=scalp_cfg,
        trend_monitor=trend_cfg,
        retention=retention_cfg,
        market_risk=market_risk_cfg,
        default_coin=default_coin,
    )


def get_settings() -> Settings:
    global _settings_instance
    if _settings_instance is None:
        raw = _load_yaml()
        _settings_instance = _build_settings(raw)
    return _settings_instance
