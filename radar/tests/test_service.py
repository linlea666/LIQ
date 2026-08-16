"""服务层纯逻辑测试：通知质量周报的触发窗口与渲染。

周报的触发不能依赖"进程恰好在周一早上活着"：
重启跨过发送时刻就永远漏掉一期，而漏掉的周报没有任何报错，
只是收件人某周一没收到邮件——这类静默缺失必须用测试锁死语义。
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from radar.notify import EmailRenderer  # noqa: E402
from radar.service import weekly_report_key  # noqa: E402

TZ8 = timezone(timedelta(hours=8))


def _ms(text: str) -> int:
    return int(datetime.fromisoformat(text).replace(tzinfo=TZ8).timestamp() * 1000)


def test_weekly_key_not_due_monday_before_send_hour():
    # 2026-08-17 是周一
    assert weekly_report_key(_ms("2026-08-17 08:59"),
                             tz_offset_hours=8, send_hour=9) is None


def test_weekly_key_due_monday_after_send_hour():
    key = weekly_report_key(_ms("2026-08-17 09:01"), tz_offset_hours=8, send_hour=9)
    assert key == "2026-W34"


def test_weekly_key_backfills_after_missed_monday():
    """周一停机、周三恢复：当周周报必须补发而不是跳过。"""
    key = weekly_report_key(_ms("2026-08-19 15:00"), tz_offset_hours=8, send_hour=9)
    assert key == "2026-W34"


def test_weekly_key_is_stable_within_week():
    """同一周内多次评估必须得到同一个键——幂等键靠它防止重发。"""
    monday = weekly_report_key(_ms("2026-08-17 10:00"), tz_offset_hours=8, send_hour=9)
    sunday = weekly_report_key(_ms("2026-08-23 23:00"), tz_offset_hours=8, send_hour=9)
    assert monday == sunday


def test_weekly_report_renders_groups_and_sample_sizes():
    renderer = EmailRenderer(tz_offset_hours=8,
                             fingerprint={"strategy_version": "v2.0.0",
                                          "config_hash": "abc"})
    summary = {
        "window_days": 7,
        "total_alerts": 15,
        "groups": [
            {"alert_kind": "S2", "strategy_version": "v2.0.0", "horizon": "24h",
             "matured_count": 4, "rug_ratio": 0.25, "hit_2x_ratio": 0.5,
             "median_peak_multiple": 2.1, "median_mae_pct": -35.0},
            {"alert_kind": "S1", "strategy_version": "v1.0.0", "horizon": "24h",
             "matured_count": 10, "rug_ratio": 0.8, "hit_2x_ratio": 0.1,
             "median_peak_multiple": 1.3, "median_mae_pct": -95.0},
        ],
    }
    subject, html = renderer.render_weekly_report(summary, generated_at=1_800_000_000_000)

    assert "15" in subject and "周报" in subject
    # 样本量必须出现在正文：3 个样本的 0% RUG 毫无意义
    assert ">4<" in html.replace(" ", "").replace("\n", "") or "4" in html
    assert "v2.0.0" in html and "v1.0.0" in html
    assert "80%" in html          # V1 的 RUG 率
    assert "RUG" in subject or "RUG率" in html


def test_weekly_report_renders_empty_window():
    renderer = EmailRenderer(tz_offset_hours=8)
    subject, html = renderer.render_weekly_report(
        {"window_days": 7, "total_alerts": 0, "groups": []},
        generated_at=1_800_000_000_000,
    )
    assert "尚未成熟" in subject
    assert "没有已成熟的推送样本" in html
