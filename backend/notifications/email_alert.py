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

# 避免 "Email notification skipped" 每 tick 刷屏：配置缺失仅首次告警一次
_warned_missing_config = False


# ─────────────────────────────────────────────────────────────────────────────
# MAA 专属邮件模板（与现有关键位/箱体模板并列，避免互相影响）
# ─────────────────────────────────────────────────────────────────────────────

# scenario → 中文标签（覆盖 9 大场景；缺省回落英文）
_MAA_SCENARIO_CN = {
    "trend_continuation_up": "趋势延续 · 多头",
    "trend_continuation_down": "趋势延续 · 空头",
    "exhaustion_top": "顶部衰竭",
    "exhaustion_bottom": "底部衰竭",
    "trap_top": "假突破 · 多陷阱",
    "trap_bottom": "假跌破 · 空陷阱",
    "range_bound": "区间震荡",
    "squeeze_pending": "挤压待发",
    "fakeout_then_continue": "假突 + 继续主方向",
}

_MAA_PHASE_CN = {
    "trend": "趋势",
    "range": "区间",
    "squeeze": "挤压",
    "transition": "过渡",
    "exhaustion": "衰竭",
}

_MAA_CONTINUITY_CN = {
    "continuation": "延续",
    "refinement": "修正",
    "reversal": "反转",
    "first_run": "首次",
}


def _maa_color_pack(event: "AlertEvent") -> tuple[str, str, str]:
    """返回 (主色, 浅底色, 顶部 tag 文本)。

    强信号（reversal + 高 conf）走紫/橙醒目色带，与日常普通通道区分；
    普通通道走绿/红常规色带（与方向一致）。
    """
    is_long = event.direction == "long"
    if event.maa_is_strong:
        color = "#7c3aed" if is_long else "#ea580c"      # 紫 / 橙
        bg_light = "#f5f3ff" if is_long else "#fff7ed"
        tag = "⚡强信号"
    else:
        color = "#16a34a" if is_long else "#dc2626"      # 绿 / 红
        bg_light = "#f0fdf4" if is_long else "#fef2f2"
        tag = "方向更新"
    return color, bg_light, tag


def _build_maa_html(event: "AlertEvent") -> str:
    """MAA（动作分析模块）专属卡片：
    场景 / 阶段 / 立场 + 交易计划 + 失效条件 + 对立场景 + 推理摘要。
    """
    color, bg_light, tag_cn = _maa_color_pack(event)
    is_long = event.direction == "long"
    dir_cn = "做多" if is_long else "做空"

    scenario_cn = _MAA_SCENARIO_CN.get(event.maa_scenario, event.maa_scenario or "—")
    phase_cn = _MAA_PHASE_CN.get(event.maa_phase, event.maa_phase or "—")
    continuity_cn = _MAA_CONTINUITY_CN.get(event.maa_continuity, event.maa_continuity or "")

    price_str = f"${event.price:,.2f}"
    entry_str = f"${event.entry:,.1f}" if event.entry else "—"
    sl_str = f"${event.stop_loss:,.1f}" if event.stop_loss else "—"
    rr_str = f"{event.rr_ratio:.1f}:1" if event.rr_ratio else "—"

    # 多目标 TP（最多展示 3 个，避免邮件过长）
    if event.maa_tp_targets:
        tps = event.maa_tp_targets[:3]
        tp_str = " / ".join(f"${t:,.1f}" for t in tps)
    elif event.tp1:
        tp_str = f"${event.tp1:,.1f}"
    else:
        tp_str = "—"

    # 滤波修正提示（accepted ≠ ai_raw）
    overridden_html = ""
    if event.maa_stability_overridden:
        overridden_html = (
            '<span style="display:inline-block;margin-left:8px;padding:2px 8px;'
            'border-radius:10px;background:#fef3c7;color:#92400e;'
            'font-size:11px;font-weight:600;">已防抖</span>'
        )

    continuity_html = ""
    if continuity_cn:
        cont_color = "#7c3aed" if event.maa_continuity == "reversal" else "#475569"
        cont_bg = "#f5f3ff" if event.maa_continuity == "reversal" else "#f1f5f9"
        continuity_html = (
            f'<span style="display:inline-block;margin-left:8px;padding:2px 10px;'
            f'border-radius:10px;background:{cont_bg};color:{cont_color};'
            f'font-size:11px;font-weight:600;">立场 · {continuity_cn}</span>'
        )

    invalidation_html = ""
    if event.maa_invalidation_top:
        invalidation_html = f"""
    <tr><td style="padding:12px 16px;border-top:1px solid #e5e7eb;">
        <div style="color:#6b7280;font-size:11px;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:6px;">失效条件（命中即放弃）</div>
        <div style="background:#fef2f2;border-left:3px solid #dc2626;padding:8px 12px;border-radius:4px;font-size:12px;color:#991b1b;line-height:1.5;">{event.maa_invalidation_top}</div>
    </td></tr>"""

    alternative_html = ""
    if event.maa_alternative:
        alternative_html = f"""
    <tr><td style="padding:10px 16px;border-top:1px solid #f3f4f6;">
        <div style="color:#6b7280;font-size:11px;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:4px;">对立场景</div>
        <div style="color:#374151;font-size:12px;line-height:1.5;">{event.maa_alternative}</div>
    </td></tr>"""

    reasoning_html = ""
    if event.maa_reasoning_short:
        reasoning_html = f"""
    <tr><td style="padding:12px 16px;border-top:1px solid #e5e7eb;">
        <div style="color:#6b7280;font-size:11px;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:6px;">分析摘要</div>
        <div style="color:#374151;font-size:13px;line-height:1.6;">{event.maa_reasoning_short}</div>
    </td></tr>"""

    ts_str = datetime.fromtimestamp(event.ts, tz=_BJ_TZ).strftime("%Y-%m-%d %H:%M:%S")

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f3f4f6;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'Helvetica Neue',sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="padding:24px 0;">
<tr><td align="center">
<table width="460" cellpadding="0" cellspacing="0" style="background:#ffffff;border-radius:12px;overflow:hidden;box-shadow:0 4px 6px rgba(0,0,0,0.07);">

    <!-- 顶部色带 -->
    <tr><td style="background:{color};padding:20px 24px;">
        <div style="color:#ffffff;font-size:12px;font-weight:500;letter-spacing:1px;text-transform:uppercase;opacity:0.85;">{event.coin} · 动作分析 · {tag_cn}</div>
        <div style="color:#ffffff;font-size:24px;font-weight:700;margin-top:4px;">{dir_cn} · {scenario_cn}</div>
        <div style="color:rgba(255,255,255,0.85);font-size:12px;margin-top:6px;">阶段 {phase_cn} · 置信度 {event.maa_confidence}{overridden_html}{continuity_html}</div>
    </td></tr>

    <!-- 当前价 -->
    <tr><td style="background:{bg_light};padding:14px 24px;border-bottom:1px solid #e5e7eb;">
        <span style="color:#6b7280;font-size:12px;">当前价</span>
        <span style="float:right;font-size:18px;font-weight:700;color:#111827;">{price_str}</span>
    </td></tr>

    <!-- 交易计划 -->
    <tr><td style="padding:0;">
    <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">
        <tr>
            <td style="padding:12px 16px;color:#6b7280;font-size:13px;border-bottom:1px solid #f3f4f6;">入场参考</td>
            <td style="padding:12px 16px;font-size:15px;font-weight:600;color:#111827;text-align:right;border-bottom:1px solid #f3f4f6;">{entry_str}</td>
        </tr>
        <tr>
            <td style="padding:12px 16px;color:#6b7280;font-size:13px;border-bottom:1px solid #f3f4f6;">止损</td>
            <td style="padding:12px 16px;font-size:15px;font-weight:600;color:#dc2626;text-align:right;border-bottom:1px solid #f3f4f6;">{sl_str}</td>
        </tr>
        <tr>
            <td style="padding:12px 16px;color:#6b7280;font-size:13px;border-bottom:1px solid #f3f4f6;">止盈目标</td>
            <td style="padding:12px 16px;font-size:14px;font-weight:600;color:#16a34a;text-align:right;border-bottom:1px solid #f3f4f6;">{tp_str}</td>
        </tr>
        <tr>
            <td style="padding:12px 16px;color:#6b7280;font-size:13px;border-bottom:1px solid #f3f4f6;">风报比</td>
            <td style="padding:12px 16px;text-align:right;border-bottom:1px solid #f3f4f6;">
                <span style="display:inline-block;background:{color};color:#fff;padding:3px 10px;border-radius:12px;font-size:13px;font-weight:600;">R:R {rr_str}</span>
            </td>
        </tr>
    </table>
    </td></tr>

    {reasoning_html}
    {invalidation_html}
    {alternative_html}

    <!-- 底部免责 -->
    <tr><td style="background:#f9fafb;padding:12px 16px;border-top:1px solid #e5e7eb;">
        <div style="color:#9ca3af;font-size:11px;text-align:center;">{ts_str} · 动作分析仅作参考，进场前需结合关键位与执行节奏</div>
    </td></tr>

</table>
</td></tr>
</table>
</body></html>"""


def _build_maa_subject(event: "AlertEvent") -> str:
    dir_cn = "做多" if event.direction == "long" else "做空"
    scenario_cn = _MAA_SCENARIO_CN.get(event.maa_scenario, event.maa_scenario or "")
    tag = "⚡强信号" if event.maa_is_strong else "方向更新"
    scen_part = f" · {scenario_cn}" if scenario_cn else ""
    return (
        f"[动作{tag}·{dir_cn}] "
        f"{event.coin} 置信度{event.maa_confidence}{scen_part} ${event.price:,.0f}"
    )


def _build_html(event: "AlertEvent") -> str:
    """生成卡片式 HTML 邮件正文。

    分流：
      - source="market_action" → 走 MAA 专属卡片（强信号紫橙色 / 普通蓝绿）
      - 其他（key_level / range）→ 走原有交易参数表卡片
    """
    if event.source == "market_action":
        return _build_maa_html(event)

    is_long = event.direction == "long"
    is_scalp = event.is_scalp
    # scalp 用更醒目的紫/橙色带，与 snipe/flip 区分
    if is_scalp:
        color = "#7c3aed" if is_long else "#ea580c"
        bg_light = "#f5f3ff" if is_long else "#fff7ed"
    else:
        color = "#16a34a" if is_long else "#dc2626"
        bg_light = "#f0fdf4" if is_long else "#fef2f2"
    dir_cn = "做多" if is_long else "做空"
    if event.source == "key_level":
        source_cn = "日内⚡" if is_scalp else "关键位"
    else:
        source_cn = "箱体"

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

    # Commit 6: 1h 市场结构 + 对齐度条（与前端顶部徽章等效）
    structure_bar = ""
    ms_dir = (event.ms_direction or "").lower()
    ms_align = (event.ms_alignment or "").lower()
    if ms_dir:
        dir_map = {
            "bullish": ("🟢 上升结构", "#16a34a", "#f0fdf4"),
            "bearish": ("🔴 下降结构", "#dc2626", "#fef2f2"),
            "ranging": ("⚪ 震荡结构", "#6b7280", "#f3f4f6"),
            "transitioning": ("🟡 结构转换中", "#ca8a04", "#fefce8"),
        }
        label, chip_fg, chip_bg = dir_map.get(ms_dir, ("", "#6b7280", "#f3f4f6"))
        if label:
            align_chip = ""
            if ms_align == "aligned":
                align_chip = (
                    '<span style="display:inline-block;margin-left:8px;padding:2px 10px;'
                    'border-radius:10px;background:#dcfce7;color:#15803d;'
                    'font-size:11px;font-weight:600;">⚡ 短线顺势</span>'
                )
            elif ms_align == "conflict":
                # 中性色调（蓝）而非警示色（橙）——多维博弈不等于危险
                align_chip = (
                    '<span style="display:inline-block;margin-left:8px;padding:2px 10px;'
                    'border-radius:10px;background:#dbeafe;color:#1e40af;'
                    'font-size:11px;font-weight:600;">🔄 多维博弈</span>'
                )
            structure_bar = f"""
    <tr><td style="background:#ffffff;padding:10px 24px;border-bottom:1px solid #e5e7eb;">
        <span style="color:#6b7280;font-size:11px;">1h 结构</span>
        <span style="display:inline-block;margin-left:8px;padding:2px 10px;border-radius:10px;background:{chip_bg};color:{chip_fg};font-size:11px;font-weight:600;">{label}</span>
        {align_chip}
    </td></tr>"""

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
    {structure_bar}
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
    if event.source == "market_action":
        return _build_maa_subject(event)

    dir_cn = "做多" if event.direction == "long" else "做空"
    if event.source == "key_level":
        source_cn = "日内⚡" if event.is_scalp else "关键位"
    else:
        source_cn = "箱体"

    # Commit 6.5: 结构对齐度写进标题，但强调这只是"与 1h 短线结构的关系"
    #   aligned  → ⚡短线顺势（方向与 1h 小时级动量一致）
    #   conflict → 🔄多维博弈（方案基于多维综合判断，与 1h 结构相反）
    #   neutral/unknown/"" → 不加标签（结构不明时不打扰）
    # 关键：逆 1h 结构的多维共振方案往往是高 R:R 机会（如分发末端反弹），
    #       不应用"⚠逆势"这种暗示"差/危险"的措辞
    align = (event.ms_alignment or "").lower()
    if align == "aligned":
        align_tag = " ⚡短线顺势"
    elif align == "conflict":
        align_tag = " 🔄多维博弈"
    else:
        align_tag = ""

    return (
        f"[{event.signal_tier}级{dir_cn}{align_tag}] "
        f"{event.coin} {source_cn}信号 ${event.price:,.0f}"
    )


async def send_alert_email(
    event: "AlertEvent",
    config: "EmailNotificationConfig",
) -> bool:
    """异步发送信号通知邮件。在线程池中执行 SMTP 操作，不阻塞事件循环。"""
    global _warned_missing_config
    if not config.to or not config.smtp_user:
        if not _warned_missing_config:
            logger.warning("Email notification skipped: no recipients or smtp_user configured (suppressing further warnings)")
            _warned_missing_config = True
        return False
    # 配置已恢复，重置抑制标志，确保未来再次丢失时能提醒
    _warned_missing_config = False

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
