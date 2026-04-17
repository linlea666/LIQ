"""_check_alerts 配置健康闸门回归测试

重点覆盖：SMTP 未配置时告警扫描直接跳过，避免"每 5 秒失败一次"的日志刷屏。
相关问题：生产日志中观察到 `[alert] matched=1 sent=0 failed=1` 持续刷屏，
根因是 enabled=True 但 smtp_user/to 为空，scan → send 失败 → 不锁冷却 → 循环。
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from engine import Engine


class _FakeEmailCfg:
    def __init__(self, *, smtp_user: str = "", to: list[str] | None = None):
        self.enabled = True
        self.smtp_user = smtp_user
        self.smtp_pass = ""
        self.smtp_host = "smtp.example.com"
        self.smtp_port = 465
        self.from_name = "LIQ"
        self.to = to or []
        self.min_signal_tier = "A"
        self.include_key_levels = True
        self.include_range = True
        self.cooldown_minutes = 45


def _make_fake_engine(*, smtp_user: str = "", to: list[str] | None = None):
    return SimpleNamespace(
        _notif_cfg=_FakeEmailCfg(smtp_user=smtp_user, to=to),
        _alert_config_warned=False,
        _states={},
        _alert_dedup=SimpleNamespace(),
    )


def test_gate_skips_scan_when_smtp_user_empty(caplog):
    """smtp_user 为空时必须直接 return，不触发 scan_alerts。"""
    fake = _make_fake_engine(smtp_user="", to=["x@y.com"])

    with caplog.at_level(logging.WARNING, logger="engine"):
        asyncio.run(Engine._check_alerts(fake, "BTC"))

    assert fake._alert_config_warned is True
    msgs = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert any("email config incomplete" in m for m in msgs), msgs


def test_gate_skips_scan_when_recipients_empty(caplog):
    """to 列表为空时也必须跳过。"""
    fake = _make_fake_engine(smtp_user="bot@x.com", to=[])

    with caplog.at_level(logging.WARNING, logger="engine"):
        asyncio.run(Engine._check_alerts(fake, "BTC"))

    assert fake._alert_config_warned is True


def test_gate_warning_fires_only_once(caplog):
    """第二次起必须静默，避免日志刷屏（核心需求）。"""
    fake = _make_fake_engine(smtp_user="", to=[])

    with caplog.at_level(logging.WARNING, logger="engine"):
        for _ in range(10):
            asyncio.run(Engine._check_alerts(fake, "BTC"))

    warn_count = sum(
        1 for r in caplog.records
        if r.levelno == logging.WARNING and "email config incomplete" in r.message
    )
    assert warn_count == 1, f"期望仅 1 条 warning，实际 {warn_count} 条"


def test_gate_allows_scan_when_config_complete():
    """配置齐全时不应进入跳过分支（_alert_config_warned 保持 False）。

    因为下游 scan_alerts 需要完整 Engine 状态，我们只验证：配置完整时
    闸门不会阻断——通过观察 `_alert_config_warned` 未被置 True 来证明。
    真实扫描逻辑的正确性由其他测试覆盖（test_signal_monitor_dedup 等）。
    """
    fake = _make_fake_engine(smtp_user="bot@x.com", to=["a@b.com"])
    # _states 为空字典 → KeyError 会被 except 捕获吞掉，这里只关心闸门分支
    asyncio.run(Engine._check_alerts(fake, "BTC"))
    assert fake._alert_config_warned is False, (
        "配置完整时不应触发配置缺失 warning"
    )
