"""配置加载与版本指纹。

设计要点：
  - 配置只在进程启动时加载一次（单例），修改需重启生效。
  - 生效配置 = config.yaml（出厂默认，随代码演进）+ data/overrides.yaml
    （配置页写入的覆盖层，存于挂载卷，镜像重建后保留）。
  - config_hash 对**合并后的生效配置**计算：任何阈值调整——无论改的是
    yaml 还是覆盖层——都会改变指纹，历史警报可精确追溯"当时是哪套参数"。
  - 覆盖文件里的非法条目在启动时被丢弃并告警；若合并结果通不过
    跨字段一致性校验，则整个覆盖层弃用、回退出厂默认——
    带着自相矛盾的阈值运行比丢弃用户改动更危险。
  - 凭据只从环境变量读取，永不写入 config.yaml，也永不进入日志。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from . import config_schema
from .domain.features import FEATURE_VERSION
from .sources.parsers import PARSER_VERSION

logger = logging.getLogger("radar.settings")

OVERRIDES_FILENAME = "overrides.yaml"

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


def _data_dir_from(raw: dict[str, Any]) -> Path:
    service = raw.get("service", {}) or {}
    data_dir = Path(str(service.get("data_dir", "data")))
    if not data_dir.is_absolute():
        data_dir = _SERVICE_ROOT / data_dir
    return data_dir


def load_overrides(data_dir: Path) -> dict[str, Any]:
    """读取并校验覆盖层。

    失败模式的处理原则是"宁可回退默认，不可带病运行"：
      - 文件损坏 / 非字典 → 整个覆盖层弃用；
      - 单条非法（写接口上线前的手工编辑、或默认值演进后越界）→ 丢该条；
    每种丢弃都打 error 日志——静默丢弃会让用户以为自己已经改了配置。
    """
    path = data_dir / OVERRIDES_FILENAME
    if not path.exists():
        return {}
    try:
        tree = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        logger.exception("覆盖配置 %s 无法解析，本次启动回退出厂默认", path)
        return {}
    clean, errors = config_schema.check_overrides(tree)
    for bad_path, reason in errors.items():
        logger.error("覆盖配置条目被丢弃 %s: %s", bad_path, reason)
    return clean


def _build(raw: dict[str, Any]) -> Settings:
    data_dir = _data_dir_from(raw)

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
        outbox_max_retries=int(email_raw.get("outbox_max_retries", 5)),
        outbox_retry_backoff_sec=int(email_raw.get("outbox_retry_backoff_sec", 120)),
    )

    scoring = raw.get("scoring", {}) or {}
    return Settings(
        raw=raw,
        config_hash=config_schema.effective_hash(raw),
        code_commit=os.getenv("APP_GIT_SHA", "unknown")[:12],
        build_time=os.getenv("APP_BUILD_TIME", "unknown"),
        strategy_version=str(scoring.get("strategy_version", "v0")),
        # 特征/解析版本是代码属性而非配置：特征公式或字段映射改在代码里，
        # 版本号必须跟着代码走。此前 feature_version 读 config、
        # parser_version 用本文件的独立副本，两处都出现过"代码改了
        # 版本没跟上"的失真——指纹失真会让 KPI 把新旧逻辑混在一组统计
        feature_version=FEATURE_VERSION,
        parser_version=PARSER_VERSION,
        service_root=_SERVICE_ROOT,
        data_dir=data_dir,
        chains=chains,
        tiers=tiers,
        email=email,
    )


_instance: Settings | None = None


def load_settings(config_file: Path | None = None) -> Settings:
    """加载配置（幂等；重复调用返回同一实例）。"""
    global _instance
    if _instance is not None:
        return _instance
    _load_env_file(_ENV_FILE)
    path = config_file or _CONFIG_FILE
    defaults = yaml.safe_load(path.read_bytes()) or {}

    overrides = load_overrides(_data_dir_from(defaults))
    effective = defaults
    if overrides:
        merged = config_schema.deep_merge(defaults, overrides)
        conflicts = config_schema.cross_validate(merged)
        if conflicts:
            # 单条参数各自合法但组合矛盾（如进入阈值 ≤ 退出阈值）时，
            # 整个覆盖层弃用：带着自相矛盾的滞回参数运行，
            # 状态机会抖动出成片的假信号，比丢改动伤害大得多
            logger.error("覆盖配置组合校验失败，回退出厂默认: %s",
                         "; ".join(conflicts))
        else:
            effective = merged
            logger.info("已应用配置覆盖 %d 项",
                        len(config_schema.iter_leaves(overrides)))

    _instance = _build(effective)
    return _instance


def get_settings() -> Settings:
    if _instance is None:
        return load_settings()
    return _instance


def reset_settings_for_tests() -> None:
    """仅供测试：清除单例，允许重新加载不同配置。"""
    global _instance
    _instance = None
