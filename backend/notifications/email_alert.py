"""异步邮件发送 + 卡片式 HTML 模板 + 回测日报。"""

from __future__ import annotations

import asyncio
import logging
import smtplib
import ssl
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
from datetime import datetime, timezone, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from config.settings import EmailNotificationConfig
    from notifications.signal_monitor import AlertEvent

logger = logging.getLogger(__name__)

_BJ_TZ = timezone(timedelta(hours=8))


def _build_html(event: "AlertEvent") -> str:
    """生成卡片式 HTML 邮件正文。"""
    is_long = event.direction == "long"
    color = "#16a34a" if is_long else "#dc2626"
    bg_light = "#f0fdf4" if is_long else "#fef2f2"
    dir_cn = "做多" if is_long else "做空"
    source_cn = "关键位" if event.source == "key_level" else "箱体"

    entry_str = f"${event.entry:,.1f}" if event.entry else "—"
    sl_str = f"${event.stop_loss:,.1f}" if event.stop_loss else "—"
    tp_str = f"${event.tp1:,.1f}" if event.tp1 else "—"
    rr_str = f"{event.rr_ratio:.1f}:1" if event.rr_ratio else "—"
    price_str = f"${event.price:,.2f}"

    level_info = ""
    if event.source == "key_level" and event.level_price:
        state_cn = {
            "swept": "已扫取", "flipped": "已翻转",
            "bounced": "已反弹", "testing": "正测试",
        }.get(event.level_state, event.level_state)
        level_info = f"""
        <tr>
            <td style="padding:8px 16px;color:#6b7280;font-size:13px;">关键位</td>
            <td style="padding:8px 16px;font-size:14px;font-weight:600;">${event.level_price:,.0f}（{state_cn}）</td>
        </tr>"""

    cascade_info = ""
    if event.cascade_risk > 0.5:
        pct = f"{event.cascade_risk:.0%}"
        cascade_info = f"""
        <tr>
            <td colspan="2" style="padding:8px 16px;">
                <div style="background:#fef3c7;border-left:3px solid #f59e0b;padding:8px 12px;border-radius:4px;font-size:13px;color:#92400e;">
                    ⚠ 级联风险 {pct} — 止损须设在清算簇最外层之外
                </div>
            </td>
        </tr>"""

    warnings_html = ""
    if event.warnings:
        items = "".join(f"<li>{w}</li>" for w in event.warnings[:3])
        warnings_html = f"""
        <tr>
            <td colspan="2" style="padding:8px 16px;">
                <div style="background:#fef2f2;border-left:3px solid #ef4444;padding:8px 12px;border-radius:4px;font-size:12px;color:#991b1b;">
                    <ul style="margin:0;padding-left:16px;">{items}</ul>
                </div>
            </td>
        </tr>"""

    ts_str = datetime.fromtimestamp(event.ts, tz=_BJ_TZ).strftime("%Y-%m-%d %H:%M:%S")

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f3f4f6;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'Helvetica Neue',sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="padding:24px 0;">
<tr><td align="center">
<table width="420" cellpadding="0" cellspacing="0" style="background:#ffffff;border-radius:12px;overflow:hidden;box-shadow:0 4px 6px rgba(0,0,0,0.07);">

    <!-- 顶部色带 -->
    <tr><td style="background:{color};padding:20px 24px;">
        <div style="color:#ffffff;font-size:12px;font-weight:500;letter-spacing:1px;text-transform:uppercase;opacity:0.85;">{event.coin} · {source_cn}信号 · {event.signal_tier}级</div>
        <div style="color:#ffffff;font-size:24px;font-weight:700;margin-top:4px;">{dir_cn}机会</div>
    </td></tr>

    <!-- 当前价 -->
    <tr><td style="background:{bg_light};padding:14px 24px;border-bottom:1px solid #e5e7eb;">
        <span style="color:#6b7280;font-size:12px;">当前价</span>
        <span style="float:right;font-size:18px;font-weight:700;color:#111827;">{price_str}</span>
    </td></tr>

    <!-- 交易参数 -->
    <tr><td style="padding:0;">
    <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">
        <tr>
            <td style="padding:12px 16px;color:#6b7280;font-size:13px;border-bottom:1px solid #f3f4f6;">入场价</td>
            <td style="padding:12px 16px;font-size:15px;font-weight:600;color:#111827;text-align:right;border-bottom:1px solid #f3f4f6;">{entry_str}</td>
        </tr>
        <tr>
            <td style="padding:12px 16px;color:#6b7280;font-size:13px;border-bottom:1px solid #f3f4f6;">止损</td>
            <td style="padding:12px 16px;font-size:15px;font-weight:600;color:#dc2626;text-align:right;border-bottom:1px solid #f3f4f6;">{sl_str}</td>
        </tr>
        <tr>
            <td style="padding:12px 16px;color:#6b7280;font-size:13px;border-bottom:1px solid #f3f4f6;">止盈</td>
            <td style="padding:12px 16px;font-size:15px;font-weight:600;color:#16a34a;text-align:right;border-bottom:1px solid #f3f4f6;">{tp_str}</td>
        </tr>
        <tr>
            <td style="padding:12px 16px;color:#6b7280;font-size:13px;border-bottom:1px solid #f3f4f6;">风报比</td>
            <td style="padding:12px 16px;text-align:right;border-bottom:1px solid #f3f4f6;">
                <span style="display:inline-block;background:{color};color:#fff;padding:3px 10px;border-radius:12px;font-size:13px;font-weight:600;">R:R {rr_str}</span>
            </td>
        </tr>{level_info}
    </table>
    </td></tr>

    <!-- 信号理由 -->
    <tr><td style="padding:14px 16px;border-top:1px solid #e5e7eb;">
        <div style="color:#6b7280;font-size:11px;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:6px;">信号理由</div>
        <div style="color:#374151;font-size:13px;line-height:1.5;">{event.reason or '—'}</div>
    </td></tr>

    {cascade_info}
    {warnings_html}

    <!-- 底部免责 -->
    <tr><td style="background:#f9fafb;padding:12px 16px;border-top:1px solid #e5e7eb;">
        <div style="color:#9ca3af;font-size:11px;text-align:center;">{ts_str} · 此为系统信号参考，非投资建议</div>
    </td></tr>

</table>
</td></tr>
</table>
</body></html>"""


def _build_subject(event: "AlertEvent") -> str:
    dir_cn = "做多" if event.direction == "long" else "做空"
    source_cn = "关键位" if event.source == "key_level" else "箱体"
    return f"[{event.signal_tier}级{dir_cn}] {event.coin} {source_cn}信号 ${event.price:,.0f}"


async def send_alert_email(
    event: "AlertEvent",
    config: "EmailNotificationConfig",
) -> bool:
    """异步发送信号通知邮件。在线程池中执行 SMTP 操作，不阻塞事件循环。"""
    if not config.to or not config.smtp_user:
        logger.warning("Email notification skipped: no recipients or smtp_user configured")
        return False

    subject = _build_subject(event)
    html = _build_html(event)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = formataddr((str(Header(config.from_name, "utf-8")), config.smtp_user))
    msg["To"] = ", ".join(config.to)
    msg.attach(MIMEText(html, "html", "utf-8"))

    def _send():
        try:
            ctx = ssl.create_default_context()
            with smtplib.SMTP_SSL(config.smtp_host, config.smtp_port, context=ctx, timeout=15) as server:
                server.login(config.smtp_user, config.smtp_pass)
                server.sendmail(config.smtp_user, config.to, msg.as_string())
            return True
        except Exception:
            logger.error("Failed to send alert email", exc_info=True)
            return False

    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(None, _send)
    if result:
        logger.info("Alert email sent | %s | %s", event.coin, subject)
    return result


def _build_digest_html(stats_map: dict[str, Any], period: str = "日报") -> str:
    """生成回测统计摘要邮件 HTML。"""
    ts_str = datetime.now(tz=_BJ_TZ).strftime("%Y-%m-%d %H:%M")
    rows_html = ""
    for coin, st in stats_map.items():
        total = st.get("total_signals", 0)
        tp1 = st.get("tp1_hit", 0)
        sl = st.get("sl_hit", 0)
        wr = st.get("win_rate", 0)
        avg_rr = st.get("avg_rr", 0)
        triggered = st.get("triggered", 0)
        wr_color = "#16a34a" if wr >= 50 else "#dc2626" if wr > 0 else "#6b7280"
        rows_html += f"""
        <tr style="border-bottom:1px solid #e5e7eb;">
            <td style="padding:10px 12px;font-weight:600;">{coin}</td>
            <td style="padding:10px 12px;text-align:center;">{total}</td>
            <td style="padding:10px 12px;text-align:center;">{triggered}</td>
            <td style="padding:10px 12px;text-align:center;color:{wr_color};font-weight:700;">{wr}%</td>
            <td style="padding:10px 12px;text-align:center;">{tp1}胜 / {sl}负</td>
            <td style="padding:10px 12px;text-align:center;color:#d97706;">1:{avg_rr}</td>
        </tr>"""

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;background:#f3f4f6;font-family:-apple-system,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="padding:24px 0;">
<tr><td align="center">
<table width="520" cellpadding="0" cellspacing="0" style="background:#fff;border-radius:12px;overflow:hidden;box-shadow:0 4px 6px rgba(0,0,0,0.07);">
    <tr><td style="background:#1e40af;padding:20px 24px;">
        <div style="color:#fff;font-size:20px;font-weight:700;">📊 LIQ 信号回测{period}</div>
        <div style="color:rgba(255,255,255,0.8);font-size:12px;margin-top:4px;">{ts_str}</div>
    </td></tr>
    <tr><td style="padding:16px;">
        <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;font-size:13px;">
            <tr style="background:#f9fafb;border-bottom:2px solid #e5e7eb;">
                <th style="padding:8px 12px;text-align:left;">币种</th>
                <th style="padding:8px 12px;text-align:center;">信号数</th>
                <th style="padding:8px 12px;text-align:center;">触发</th>
                <th style="padding:8px 12px;text-align:center;">胜率</th>
                <th style="padding:8px 12px;text-align:center;">战绩</th>
                <th style="padding:8px 12px;text-align:center;">Avg R:R</th>
            </tr>
            {rows_html}
        </table>
    </td></tr>
    <tr><td style="padding:12px 16px;background:#f9fafb;border-top:1px solid #e5e7eb;">
        <div style="color:#9ca3af;font-size:11px;text-align:center;">此为系统自动生成的回测统计，仅供参考</div>
    </td></tr>
</table>
</td></tr></table>
</body></html>"""


async def send_backtest_digest(
    stats_map: dict[str, Any],
    config: "EmailNotificationConfig",
    period: str = "日报",
) -> bool:
    """发送回测统计摘要邮件。"""
    if not config.to or not config.smtp_user:
        return False
    if not stats_map:
        return False

    subject = f"[LIQ] 信号回测{period} - {datetime.now(tz=_BJ_TZ).strftime('%Y-%m-%d')}"
    html = _build_digest_html(stats_map, period)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = formataddr((str(Header(config.from_name, "utf-8")), config.smtp_user))
    msg["To"] = ", ".join(config.to)
    msg.attach(MIMEText(html, "html", "utf-8"))

    def _send():
        try:
            ctx = ssl.create_default_context()
            with smtplib.SMTP_SSL(config.smtp_host, config.smtp_port, context=ctx, timeout=15) as server:
                server.login(config.smtp_user, config.smtp_pass)
                server.sendmail(config.smtp_user, config.to, msg.as_string())
            return True
        except Exception:
            logger.error("Failed to send digest email", exc_info=True)
            return False

    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(None, _send)
    if result:
        logger.info("Backtest digest sent | period=%s coins=%s", period, list(stats_map.keys()))
    return result
