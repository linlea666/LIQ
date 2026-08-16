"""邮件渲染与 SMTP 传输。

**为什么不复用主项目的 email_alert.py**：
那个模块的绝大部分是主项目自己的业务模板，radar 一行都用不上；
而把它整体拷进来会引入两个真实问题——Docker 构建上下文要跨目录，
以及日后主项目改动模板时两份代码悄悄分叉。
这里只实现最小的 SMTP 传输，语义与主项目保持一致（SSL、UTF-8、显式超时）。

**邮件内容的设计原则**：收件人看到一封警报时要在十秒内回答三个问题——
这是什么币、为什么现在报警、我该有多信它。
因此正文必须同时呈现：触发原因与阈值、各因子的正负贡献、
以及数据质量与风险的明确标注。
只给一个"机会分 87"的邮件是没法据以行动的。
"""

from __future__ import annotations

import asyncio
import html as html_escape
import logging
import smtplib
import ssl
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
from typing import Any, Sequence

from .alerts import KIND_DISTRIBUTION, KIND_S1, KIND_S2, AlertRecord
from .registry import Evaluation
from .settings import EmailConfig

logger = logging.getLogger("radar.notify")

_KIND_LABEL = {
    KIND_S1: "S1 候选",
    KIND_S2: "S2 强信号",
    KIND_DISTRIBUTION: "派发预警",
}

_KIND_COLOR = {
    KIND_S1: "#2563eb",
    KIND_S2: "#dc2626",
    KIND_DISTRIBUTION: "#d97706",
}


class SmtpTransport:
    """SMTP 发送。

    smtplib 是阻塞的，必须放进线程池：容器只有 0.6 核，
    一次 SMTP 握手加投递可能耗时几秒，直接在事件循环里跑
    会让采集器和 API 在这几秒内完全停摆。
    """

    def __init__(self, config: EmailConfig, *, timeout_sec: float = 20.0) -> None:
        self._config = config
        self._timeout = timeout_sec

    @property
    def usable(self) -> bool:
        return self._config.usable

    async def send(self, *, subject: str, html: str) -> None:
        if not self._config.usable:
            raise RuntimeError("SMTP 未配置完整（检查 SMTP_USER / SMTP_PASS 环境变量）")
        await asyncio.to_thread(self._send_sync, subject, html)

    def _send_sync(self, subject: str, html: str) -> None:
        cfg = self._config
        message = MIMEMultipart("alternative")
        message["Subject"] = subject
        message["From"] = formataddr((cfg.from_name, cfg.smtp_user))
        message["To"] = ", ".join(cfg.to)
        message.attach(MIMEText(html, "html", "utf-8"))

        context = ssl.create_default_context()
        if cfg.smtp_port == 465:
            with smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port,
                                  timeout=self._timeout, context=context) as server:
                server.login(cfg.smtp_user, cfg.smtp_pass)
                server.sendmail(cfg.smtp_user, cfg.to, message.as_string())
        else:
            with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port,
                              timeout=self._timeout) as server:
                server.starttls(context=context)
                server.login(cfg.smtp_user, cfg.smtp_pass)
                server.sendmail(cfg.smtp_user, cfg.to, message.as_string())


class EmailRenderer:
    def __init__(self, *, tz_offset_hours: int = 8,
                 fingerprint: dict[str, str] | None = None) -> None:
        self._tz = timezone(timedelta(hours=tz_offset_hours))
        self._fingerprint = fingerprint or {}

    # ── 单条警报 ────────────────────────────────────────────────────────
    def render_alert(self, record: AlertRecord, ev: Evaluation) -> tuple[str, str]:
        view = ev.view
        symbol = record.symbol
        label = _KIND_LABEL.get(record.kind, record.kind)
        color = _KIND_COLOR.get(record.kind, "#334155")
        mc = _money(ev.market_cap)

        subject = f"[雷达·{label}] {symbol} · 市值 {mc} · 机会分 {ev.scores.opportunity:.0f}"

        sections = [
            self._header(symbol, view, label, color, ev),
            self._scores_block(ev),
            self._trigger_block(ev),
            self._factors_block(ev),
            self._risk_block(ev),
            self._quality_block(ev),
            self._footer(record.created_at),
        ]
        return subject, _wrap("".join(sections))

    def _header(self, symbol: str, view: Any, label: str,
                color: str, ev: Evaluation) -> str:
        chain = "BSC" if view.chain_id == "56" else "Solana"
        age = _duration(view.age_sec(ev.evaluated_at))
        return f"""
<div style="border-left:4px solid {color};padding:12px 16px;background:#f8fafc;">
  <div style="font-size:13px;color:{color};font-weight:600;">{_e(label)}</div>
  <div style="font-size:22px;font-weight:700;color:#0f172a;margin:4px 0;">{_e(symbol)}</div>
  <div style="font-size:12px;color:#64748b;">
    {chain} · 上线 {_e(age)} · 合约 <code style="font-size:11px;">{_e(view.contract_address)}</code>
  </div>
</div>
<table style="width:100%;border-collapse:collapse;margin-top:12px;font-size:13px;">
  <tr>
    {_cell("市值", _money(ev.market_cap) + _mc_note(ev))}
    {_cell("流动性", _money(view.getf("liquidity")))}
    {_cell("持有人", _count(view.geti("holders")))}
  </tr>
</table>
"""

    def _scores_block(self, ev: Evaluation) -> str:
        s = ev.scores
        # 把把握程度和数据质量与机会分并列展示，而不是折叠在角落：
        # "机会分 88 但把握 31" 与 "机会分 72 把握 89" 是完全不同的两件事，
        # 收件人必须一眼看到这个区别
        items = [
            ("机会分", s.opportunity, "#2563eb"),
            ("把握程度", s.confidence, _confidence_color(s.confidence)),
            ("数据质量", s.data_quality, _quality_color(s.data_quality)),
            ("归零风险", s.rug_risk, _risk_color(s.rug_risk)),
            ("派发迹象", s.distribution, _risk_color(s.distribution)),
        ]
        cells = "".join(
            f"""<td style="padding:10px 6px;text-align:center;border:1px solid #e2e8f0;">
                  <div style="font-size:11px;color:#64748b;">{_e(name)}</div>
                  <div style="font-size:19px;font-weight:700;color:{color};">{value:.0f}</div>
                </td>"""
            for name, value, color in items
        )
        return (
            '<table style="width:100%;border-collapse:collapse;margin-top:14px;">'
            f"<tr>{cells}</tr></table>"
        )

    def _trigger_block(self, ev: Evaluation) -> str:
        rows = []
        requirements = ev.state.as_dict()["requirements"].get(
            ev.state.new_state.value, []
        )
        for req in requirements:
            actual = req["actual"]
            threshold = req["threshold"]
            if actual is None or threshold is None:
                continue
            rows.append(
                f"""<tr>
                  <td style="padding:5px 8px;color:#475569;">{_e(req['label'])}</td>
                  <td style="padding:5px 8px;text-align:right;font-weight:600;color:#0f172a;">
                    {actual:.1f}</td>
                  <td style="padding:5px 8px;text-align:right;color:#94a3b8;">
                    阈值 {threshold:.1f}</td>
                </tr>"""
            )
        table = (
            '<table style="width:100%;border-collapse:collapse;font-size:12px;">'
            + "".join(rows) + "</table>"
        ) if rows else ""
        return _section("触发原因", f"<div style='color:#334155;'>{_e(ev.state.reason)}</div>{table}")

    def _factors_block(self, ev: Evaluation) -> str:
        rows = []
        for factor in sorted(ev.scores.factors, key=lambda f: -f.score):
            ratio = factor.score / factor.max_score if factor.max_score else 0.0
            bar_color = "#16a34a" if ratio >= 0.6 else ("#f59e0b" if ratio >= 0.35 else "#cbd5e1")
            width = max(2, int(ratio * 100))
            rows.append(
                f"""<tr>
                  <td style="padding:4px 8px;width:90px;color:#475569;">{_e(factor.label)}</td>
                  <td style="padding:4px 8px;">
                    <div style="background:#f1f5f9;height:8px;border-radius:4px;">
                      <div style="background:{bar_color};width:{width}%;height:8px;border-radius:4px;"></div>
                    </div>
                  </td>
                  <td style="padding:4px 8px;text-align:right;width:60px;color:#0f172a;">
                    {factor.score:.0f}/{factor.max_score:.0f}</td>
                  <td style="padding:4px 8px;color:#94a3b8;font-size:11px;">{_e(factor.detail)}</td>
                </tr>"""
            )
        return _section(
            "评分构成",
            '<table style="width:100%;border-collapse:collapse;font-size:12px;">'
            + "".join(rows) + "</table>",
        )

    def _risk_block(self, ev: Evaluation) -> str:
        parts: list[str] = []
        if ev.risk.audit_unknown:
            # 这一句必须显眼：未知不等于安全，而人天然会把"没提示风险"读成"没有风险"
            parts.append(
                '<div style="color:#b45309;">⚠ 合约审计结果未知（不等于安全）</div>'
            )
        for violation in ev.risk.research_violations:
            parts.append(
                f'<div style="color:#b45309;">· {_e(violation.detail or violation.rule)}</div>'
            )
        for reason in ev.scores.distribution_reasons:
            parts.append(f'<div style="color:#b45309;">· {_e(reason)}</div>')
        if not parts:
            parts.append('<div style="color:#16a34a;">未命中研究风险门规则</div>')
        return _section("风险提示", "".join(parts))

    def _quality_block(self, ev: Evaluation) -> str:
        q = ev.quality
        notes: list[str] = []
        if q.missing_groups:
            notes.append(f"缺失数据: {_e('、'.join(q.missing_groups))}")
        if q.stale_groups:
            notes.append(f"数据过期: {_e('、'.join(q.stale_groups))}")
        for conflict in q.conflicts:
            notes.append(_e(conflict))
        if not notes:
            return ""
        return _section(
            "数据说明",
            "".join(f'<div style="color:#64748b;font-size:12px;">· {n}</div>' for n in notes),
        )

    # ── 摘要邮件 ────────────────────────────────────────────────────────
    def render_digest(self, records: Sequence[AlertRecord]) -> tuple[str, str]:
        counts: dict[str, int] = {}
        for record in records:
            counts[record.kind] = counts.get(record.kind, 0) + 1
        summary = "、".join(
            f"{_KIND_LABEL.get(k, k)} {n} 个" for k, n in sorted(counts.items())
        )
        subject = f"[雷达·摘要] {len(records)} 条警报 · {summary}"

        rows = "".join(
            f"""<tr>
              <td style="padding:6px 8px;border-bottom:1px solid #e2e8f0;">
                <span style="color:{_KIND_COLOR.get(r.kind, '#334155')};font-weight:600;">
                  {_e(_KIND_LABEL.get(r.kind, r.kind))}</span></td>
              <td style="padding:6px 8px;border-bottom:1px solid #e2e8f0;font-weight:600;">
                {_e(r.symbol)}</td>
              <td style="padding:6px 8px;border-bottom:1px solid #e2e8f0;text-align:right;">
                机会 {r.scores.get('opportunity', 0):.0f}</td>
              <td style="padding:6px 8px;border-bottom:1px solid #e2e8f0;text-align:right;color:#64748b;">
                把握 {r.scores.get('confidence', 0):.0f}</td>
              <td style="padding:6px 8px;border-bottom:1px solid #e2e8f0;text-align:right;color:#64748b;">
                {_time(r.created_at, self._tz)}</td>
            </tr>"""
            for r in records
        )
        body = f"""
<div style="padding:12px 16px;background:#f8fafc;border-left:4px solid #334155;">
  <div style="font-size:18px;font-weight:700;color:#0f172a;">警报摘要</div>
  <div style="font-size:12px;color:#64748b;margin-top:4px;">
    邮件已达每小时上限，以下 {len(records)} 条警报合并送达。
    完整决策依据请在雷达控制台查看。
  </div>
</div>
<table style="width:100%;border-collapse:collapse;margin-top:12px;font-size:13px;">{rows}</table>
{self._footer(records[-1].created_at if records else 0)}
"""
        return subject, _wrap(body)

    def _footer(self, at_ms: int) -> str:
        fp = self._fingerprint
        return f"""
<div style="margin-top:16px;padding-top:10px;border-top:1px solid #e2e8f0;
            font-size:11px;color:#94a3b8;line-height:1.6;">
  {_time(at_ms, self._tz)} · 策略 {_e(fp.get('strategy_version', '-'))}
  · 配置 {_e(fp.get('config_hash', '-'))}<br>
  本邮件为研究性信号，不构成投资建议。所有阈值均为待回测初始值。
</div>
"""


# ═════════════════════════════════════════════════════════════════════════
# 渲染工具
# ═════════════════════════════════════════════════════════════════════════

def _wrap(body: str) -> str:
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="margin:0;padding:16px;background:#f1f5f9;
             font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;">
<div style="max-width:640px;margin:0 auto;background:#ffffff;padding:16px;
            border-radius:8px;">{body}</div>
</body></html>"""


def _section(title: str, content: str) -> str:
    return f"""
<div style="margin-top:16px;">
  <div style="font-size:12px;font-weight:600;color:#0f172a;margin-bottom:6px;
              padding-bottom:4px;border-bottom:1px solid #e2e8f0;">{_e(title)}</div>
  {content}
</div>"""


def _cell(label: str, value: str) -> str:
    return f"""<td style="padding:8px;border:1px solid #e2e8f0;">
      <div style="font-size:11px;color:#64748b;">{_e(label)}</div>
      <div style="font-size:15px;font-weight:600;color:#0f172a;">{value}</div>
    </td>"""


def _e(text: Any) -> str:
    """HTML 转义。

    代币名称直接来自链上，包含 <script> 或引号是常见的攻击/污染手段。
    虽然收件人是自己，但邮件客户端渲染未转义内容仍可能出现显示错乱。
    """
    return html_escape.escape(str(text if text is not None else "—"))


def _mc_note(ev: Evaluation) -> str:
    # 反算市值必须标注：不同口径的市值放在一起比较毫无意义
    if ev.mc_source == "computed":
        return '<span style="font-size:10px;color:#b45309;"> (反算)</span>'
    return ""


def _money(value: float | None) -> str:
    if value is None:
        return "—"
    if value >= 1_000_000:
        return f"${value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"${value / 1_000:.1f}K"
    return f"${value:.2f}"


def _count(value: int | None) -> str:
    return "—" if value is None else f"{value:,}"


def _duration(seconds: int | None) -> str:
    if seconds is None:
        return "时间未知"
    minutes = seconds / 60
    if minutes < 60:
        return f"{minutes:.0f} 分钟"
    hours = minutes / 60
    if hours < 24:
        return f"{hours:.1f} 小时"
    return f"{hours / 24:.1f} 天"


def _time(at_ms: int, tz: timezone) -> str:
    if not at_ms:
        return "—"
    return datetime.fromtimestamp(at_ms / 1000, tz).strftime("%m-%d %H:%M:%S")


def _confidence_color(value: float) -> str:
    return "#16a34a" if value >= 70 else ("#f59e0b" if value >= 50 else "#dc2626")


def _quality_color(value: float) -> str:
    return "#16a34a" if value >= 75 else ("#f59e0b" if value >= 55 else "#dc2626")


def _risk_color(value: float) -> str:
    return "#16a34a" if value < 30 else ("#f59e0b" if value < 55 else "#dc2626")
