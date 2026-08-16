"""配置加载与版本指纹。

设计要点：
  - 配置只在进程启动时加载一次（单例），修改 config.yaml 需重启生效。
  - config_hash 由文件原始字节计算：任何阈值调整都会改变指纹，
    使得历史警报可以精确追溯到"当时是哪一套参数"。
  - 凭据只从环境变量读取，永不写入 config.yaml，也永不进入日志。
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

_SERVICE_ROOT = Path(__file__).resolve().parent.parent
_CONFIG_FILE = _SERVICE_ROOT / "config.yaml"
_ENV_FILE = _SERVICE_ROOT / "radar.env"


def _load_env_file(path: Path) -> None:
    """极简 .env 解析：容器里 docker compose 已注入环境变量，
    这里只为本地开发提供便利，因此不引入 python-dotenv 依赖。
    已存在的环境变量优先，不被文件覆盖。
    """
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


@dataclass(frozen=True)
class ChainConfig:
    id: str
    name: str
    enabled: bool = True


@dataclass(frozen=True)
class TierConfig:
    """单个调度层的独立配额与目标轮询间隔。"""

    name: str
    max_rpm: float
    interval_sec: int


@dataclass(frozen=True)
class EmailConfig:
    """字段名与主项目 EmailNotificationConfig 的 SMTP 部分保持一致，
    以便 radar 的传输层与主项目行为可直接对照。
    """

    enabled: bool
    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_pass: str
    from_name: str
    to: list[str]
    max_per_hour: int
    digest_on_overflow: bool
    send_s1: bool
    send_s2: bool
    send_distribution: bool
    daily_kpi_hour_local: int
    outbox_max_retries: int
    outbox_retry_backoff_sec: int

    @property
    def usable(self) -> bool:
        return bool(
            self.enabled
            and self.to
            and self.smtp_host
            and self.smtp_port > 0
            and self.smtp_user
            and self.smtp_pass
        )


@dataclass(frozen=True)
class Settings:
    """进程级不可变配置。原始字典保留在 raw 里，供细粒度阈值按需读取。"""

    raw: dict[str, Any]
    config_hash: str
    code_commit: str
    build_time: str
    strategy_version: str
    feature_version: str
    parser_version: str
    service_root: Path
    data_dir: Path
    chains: tuple[ChainConfig, ...]
    tiers: dict[str, TierConfig]
    email: EmailConfig

    # ── 便捷分节访问 ────────────────────────────────────────────────────
    @property
    def service(self) -> dict[str, Any]:
        return self.raw.get("service", {})

    @property
    def scheduler(self) -> dict[str, Any]:
        return self.raw.get("scheduler", {})

    @property
    def collectors(self) -> dict[str, Any]:
        return self.raw.get("collectors", {})

    @property
    def risk(self) -> dict[str, Any]:
        return self.raw.get("risk", {})

    @property
    def features(self) -> dict[str, Any]:
        return self.raw.get("features", {})

    @property
    def scoring(self) -> dict[str, Any]:
        return self.raw.get("scoring", {})

    @property
    def state_machine(self) -> dict[str, Any]:
        return self.raw.get("state_machine", {})

    @property
    def alerts(self) -> dict[str, Any]:
        return self.raw.get("alerts", {})

    @property
    def tracker(self) -> dict[str, Any]:
        return self.raw.get("tracker", {})

    @property
    def storage(self) -> dict[str, Any]:
        return self.raw.get("storage", {})

    @property
    def observability(self) -> dict[str, Any]:
        return self.raw.get("observability", {})

    @property
    def tz_offset_hours(self) -> int:
        return int(self.service.get("tz_offset_hours", 8))

    @property
    def enabled_chains(self) -> tuple[ChainConfig, ...]:
        return tuple(c for c in self.chains if c.enabled)

    def chain_name(self, chain_id: str) -> str:
        for c in self.chains:
            if c.id == chain_id:
                return c.name
        return chain_id

    def tier(self, name: str) -> TierConfig:
        if name not in self.tiers:
            raise KeyError(f"未定义的调度层: {name}")
        return self.tiers[name]

    def fingerprint(self) -> dict[str, str]:
        """写入每条警报/决策事件的可复现指纹。"""
        return {
            "strategy_version": self.strategy_version,
            "feature_version": self.feature_version,
            "parser_version": self.parser_version,
            "config_hash": self.config_hash,
            "code_commit": self.code_commit,
        }


def _build(raw: dict[str, Any], config_bytes: bytes) -> Settings:
    service = raw.get("service", {}) or {}
    data_dir_raw = str(service.get("data_dir", "data"))
    data_dir = Path(data_dir_raw)
    if not data_dir.is_absolute():
        data_dir = _SERVICE_ROOT / data_dir

    chains = tuple(
        ChainConfig(
            id=str(c["id"]),
            name=str(c.get("name", c["id"])),
            enabled=bool(c.get("enabled", True)),
        )
        for c in (raw.get("chains") or [])
    )

    tiers: dict[str, TierConfig] = {}
    for name, cfg in ((raw.get("scheduler", {}) or {}).get("tiers", {}) or {}).items():
        tiers[name] = TierConfig(
            name=name,
            max_rpm=float(cfg.get("max_rpm", 1)),
            interval_sec=int(cfg.get("interval_sec", 300)),
        )

    email_raw = raw.get("email", {}) or {}
    email = EmailConfig(
        enabled=bool(email_raw.get("enabled", False)),
        smtp_host=str(email_raw.get("smtp_host", "")),
        smtp_port=int(email_raw.get("smtp_port", 465)),
        # 凭据只走环境变量
        smtp_user=os.getenv("SMTP_USER", ""),
        smtp_pass=os.getenv("SMTP_PASS", ""),
        from_name=str(email_raw.get("from_name", "LIQ雷达")),
        to=[str(x) for x in (email_raw.get("to") or [])],
        max_per_hour=int(email_raw.get("max_per_hour", 12)),
        digest_on_overflow=bool(email_raw.get("digest_on_overflow", True)),
        send_s1=bool(email_raw.get("send_s1", True)),
        send_s2=bool(email_raw.get("send_s2", True)),
        send_distribution=bool(email_raw.get("send_distribution", True)),
        daily_kpi_hour_local=int(email_raw.get("daily_kpi_hour_local", 9)),
        outbox_max_retries=int(email_raw.get("outbox_max_retries", 5)),
        outbox_retry_backoff_sec=int(email_raw.get("outbox_retry_backoff_sec", 120)),
    )

    scoring = raw.get("scoring", {}) or {}
    return Settings(
        raw=raw,
        config_hash=hashlib.sha256(config_bytes).hexdigest()[:16],
        code_commit=os.getenv("APP_GIT_SHA", "unknown")[:12],
        build_time=os.getenv("APP_BUILD_TIME", "unknown"),
        strategy_version=str(scoring.get("strategy_version", "v0")),
        feature_version=str(scoring.get("feature_version", "f0")),
        parser_version=PARSER_VERSION,
        service_root=_SERVICE_ROOT,
        data_dir=data_dir,
        chains=chains,
        tiers=tiers,
        email=email,
    )


# 解析层版本：币安接口字段映射发生任何变更时必须手动递增，
# 使历史快照能追溯到"当时用哪套解析规则"。
PARSER_VERSION = "p1.0.0"

_instance: Settings | None = None


def load_settings(config_file: Path | None = None) -> Settings:
    """加载配置（幂等；重复调用返回同一实例）。"""
    global _instance
    if _instance is not None:
        return _instance
    _load_env_file(_ENV_FILE)
    path = config_file or _CONFIG_FILE
    config_bytes = path.read_bytes()
    raw = yaml.safe_load(config_bytes) or {}
    _instance = _build(raw, config_bytes)
    return _instance


def get_settings() -> Settings:
    if _instance is None:
        return load_settings()
    return _instance


def reset_settings_for_tests() -> None:
    """仅供测试：清除单例，允许重新加载不同配置。"""
    global _instance
    _instance = None
