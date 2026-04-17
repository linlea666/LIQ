"""AlertDedup 回归测试

重点覆盖 P1 可靠性缺陷修复：
- should_send 不再产生副作用（纯查询）
- mark_sent 必须显式调用才占据冷却位
- SMTP 失败场景下冷却位不会被静默锁定，下一轮可立即重试
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from notifications.signal_monitor import AlertDedup


def test_should_send_is_pure_query():
    """should_send 必须纯查询，连续调用不产生副作用。"""
    dedup = AlertDedup(cooldown_seconds=60)
    for _ in range(5):
        assert dedup.should_send("k1") is True  # 从未 mark_sent，始终允许


def test_mark_sent_locks_cooldown_window():
    """mark_sent 后 should_send 在冷却窗口内必须返回 False。"""
    dedup = AlertDedup(cooldown_seconds=60)
    assert dedup.should_send("k1") is True
    dedup.mark_sent("k1")
    assert dedup.should_send("k1") is False


def test_cooldown_expires_after_window():
    """冷却窗口过期后 should_send 恢复 True。"""
    dedup = AlertDedup(cooldown_seconds=1)
    dedup.mark_sent("k1")
    assert dedup.should_send("k1") is False
    time.sleep(1.1)
    assert dedup.should_send("k1") is True


def test_different_keys_are_independent():
    dedup = AlertDedup(cooldown_seconds=60)
    dedup.mark_sent("k1")
    assert dedup.should_send("k1") is False
    assert dedup.should_send("k2") is True


def test_smtp_failure_does_not_lock_cooldown_regression():
    """P1 回归：模拟 SMTP 失败场景（调用方 should_send=True 但不 mark_sent），
    下一轮 should_send 必须仍返回 True，不被静默锁定 45 分钟。"""
    dedup = AlertDedup(cooldown_seconds=45 * 60)

    key = "BTC:kl:75993.0:testing:short"
    for attempt in range(10):  # 模拟连续 10 次 SMTP 失败
        assert dedup.should_send(key) is True, (
            f"第 {attempt+1} 次应允许重试，但被冷却了"
        )
        # 关键：send 失败不调用 mark_sent


def test_cleanup_removes_stale_entries():
    """cleanup 清理超出 max_age 的老条目防止内存泄漏。"""
    dedup = AlertDedup(cooldown_seconds=60)
    dedup.mark_sent("old")
    dedup._sent["old"] = time.time() - 8000  # 手动老化到 2.2 小时前
    dedup.mark_sent("fresh")

    dedup.cleanup(max_age=7200)
    assert "old" not in dedup._sent
    assert "fresh" in dedup._sent


def test_cleanup_keeps_recent_entries():
    dedup = AlertDedup(cooldown_seconds=60)
    dedup.mark_sent("k1")
    dedup.mark_sent("k2")
    dedup.cleanup(max_age=7200)
    assert "k1" in dedup._sent
    assert "k2" in dedup._sent
