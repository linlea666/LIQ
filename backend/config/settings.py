"""
全局配置加载：YAML + 环境变量。
整个后端通过 get_settings() 获取唯一配置实例。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml
from dotenv import load_dotenv

load_dotenv()

_CONFIG_DIR = Path(__file__).resolve().parent
_CONFIG_FILE = _CONFIG_DIR / "config.yaml"


@dataclass(frozen=True)
class CoinConfig:
    """单个币种的 Coinglass symbol 映射"""
    ccy: str
    symbol_cg: str
    symbol_cg_pair: str
    exchange_primary: str
    ct_val: float = 1.0
    default: bool = False


@dataclass(frozen=True)
class CoinglassSourceConfig:
    """Coinglass 统一数据源配置"""
    base_url: str
    api_key_env: str
    api_key_default: str
    timeout_sec: int
    rate_limit_per_min: int
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
class ProcessorsConfig:
    cvd: dict[str, Any]
    percentile: dict[str, Any]
    market_temp: dict[str, Any]
    levels: dict[str, Any]
    orderbook: dict[str, Any]
    range_signal: dict[str, Any] = field(default_factory=dict)
    key_level_tracker: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AIProviderConfig:
    """单个 AI 提供商的配置"""
    name: str
    model: str
    api_base: str
    env_key: str


@dataclass(frozen=True)
class AINewsAgentConfig:
    """D12 · 新闻智能 Agent 配置（共享主 AI 的 key，独立轻量模型/预算）"""
    model: str = "deepseek-chat"
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


@dataclass(frozen=True)
class NotificationsConfig:
    email: EmailNotificationConfig = field(default_factory=EmailNotificationConfig)


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
            default=coin_raw.get("default", False),
        )
        coins[ccy] = cc
        if cc.default:
            default_coin = ccy

    src = raw["sources"]
    coinglass = CoinglassSourceConfig(**src["coinglass"])
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
        model=str(news_agent_raw.get("model") or "deepseek-chat"),
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
    engine_cfg = EngineConfig(
        inactive_poll_sec=eng_raw.get("inactive_poll_sec", 120),
        grace_period_sec=eng_raw.get("grace_period_sec", 60),
    )

    notif_raw = raw.get("notifications", {})
    email_raw = notif_raw.get("email", {})
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
    )
    notifications_cfg = NotificationsConfig(email=email_cfg)

    return Settings(
        coins=coins,
        coinglass=coinglass,
        binance=binance,
        bbx=bbx,
        processors=processors,
        ai=ai,
        push=push,
        server=server,
        engine=engine_cfg,
        notifications=notifications_cfg,
        default_coin=default_coin,
    )


def get_settings() -> Settings:
    global _settings_instance
    if _settings_instance is None:
        raw = _load_yaml()
        _settings_instance = _build_settings(raw)
    return _settings_instance
