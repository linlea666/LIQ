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
    """单个币种的跨交易所 symbol 映射"""
    ccy: str
    symbol_okx_swap: str
    symbol_okx_spot: str
    symbol_binance: str
    symbol_bbx: str
    inst_family: str
    default: bool = False


@dataclass(frozen=True)
class BBXSourceConfig:
    base_url: str
    module: str
    poll_interval_sec: int
    timeout_sec: int
    cycles: list[str]


@dataclass(frozen=True)
class OKXSourceConfig:
    rest_base_url: str
    ws_url: str
    timeout_sec: int
    poll_intervals: dict[str, int]
    ws_channels: list[str]


@dataclass(frozen=True)
class BinanceSourceConfig:
    rest_base_url: str
    ws_url: str
    spot_rest_base_url: str
    timeout_sec: int
    enabled: bool
    poll_intervals: dict[str, int]


@dataclass(frozen=True)
class ProcessorsConfig:
    cvd: dict[str, Any]
    percentile: dict[str, Any]
    market_temp: dict[str, Any]
    levels: dict[str, Any]
    orderbook: dict[str, Any]


@dataclass(frozen=True)
class AIConfig:
    provider: str
    model: str
    timeout_sec: int
    max_retries: int
    cooldown_sec: int
    max_history: int
    api_key: str = ""
    api_base: str = ""


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
class Settings:
    coins: dict[str, CoinConfig]
    bbx: BBXSourceConfig
    okx: OKXSourceConfig
    binance: BinanceSourceConfig
    processors: ProcessorsConfig
    ai: AIConfig
    push: PushConfig
    server: ServerConfig
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
            symbol_okx_swap=coin_raw["symbol_okx_swap"],
            symbol_okx_spot=coin_raw["symbol_okx_spot"],
            symbol_binance=coin_raw["symbol_binance"],
            symbol_bbx=coin_raw["symbol_bbx"],
            inst_family=coin_raw["inst_family"],
            default=coin_raw.get("default", False),
        )
        coins[ccy] = cc
        if cc.default:
            default_coin = ccy

    src = raw["sources"]
    bbx = BBXSourceConfig(**src["bbx"])
    okx = OKXSourceConfig(**src["okx"])
    bn = BinanceSourceConfig(**src["binance"])

    processors = ProcessorsConfig(**raw["processors"])

    ai_raw = raw["ai"]
    ai = AIConfig(
        provider=ai_raw["provider"],
        model=ai_raw["model"],
        timeout_sec=ai_raw["timeout_sec"],
        max_retries=ai_raw["max_retries"],
        cooldown_sec=ai_raw["cooldown_sec"],
        max_history=ai_raw["max_history"],
        api_key=os.getenv("AI_API_KEY", ""),
        api_base=os.getenv("AI_API_BASE", ""),
    )

    push = PushConfig(**raw["push"])
    server = ServerConfig(**raw["server"])

    return Settings(
        coins=coins,
        bbx=bbx,
        okx=okx,
        binance=bn,
        processors=processors,
        ai=ai,
        push=push,
        server=server,
        default_coin=default_coin,
    )


def get_settings() -> Settings:
    global _settings_instance
    if _settings_instance is None:
        raw = _load_yaml()
        _settings_instance = _build_settings(raw)
    return _settings_instance
