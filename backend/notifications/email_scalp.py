"""短线信号邮件 · 独立通道，复用 EmailNotificationConfig（SMTP）

设计原则（dev-constraints #3 复用决策）：
  - SMTP 配置 + 异步发送框架 → 直接复用 email_alert.py 的模式（线程池执行 SMTP）
  - 但**不修改** send_alert_email：那是 MAA / key_level 专属，模板与字段都不同
  - 这里独立一份，复用 EmailNotificationConfig 实例（共享配置/凭据）

只发送 created 事件（结算事件不发邮件，UI 上看历史足矣，避免邮件刷屏）
test_mode 永远 True，标题前缀 [测试]，避免误以为实盘信号
"""

from __future__ import annotations

import asyncio
import logging
import smtplib
import ssl
from datetime import datetime, timedelta, timezone
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
from typing import TYPE_CHECKING

from models.scalp_signal import ScalpSignal

if TYPE_CHECKING:
    from config.settings import EmailNotificationConfig

logger = logging.getLogger(__name__)

_BJ_TZ = timezone(timedelta(hours=8))

_warned_missing_config = False


def _direction_cn(direction: str) -> tuple[str, str]:
    """方向 → (中文, 颜色)"""
    if direction == "up":
        return "看涨 ↑", "#16a34a"
    return "看跌 ↓", "#dc2626"


def _strategy_cn(strategy_value: str) -> str:
    return {
        "A_sweep_reclaim": "A · 扫单回归",
        "B_cvd_divergence": "B · 现合 CVD 背离",
        "C_range_edge_fade": "C · 区间边缘回归",
    }.get(strategy_value, strategy_value)


def _build_subject(signal: ScalpSignal, *, prefix: str = "[测试]") -> str:
    dir_cn, _ = _direction_cn(signal.direction)
    strat = _strategy_cn(signal.strategy.value)
    return (
        f"{prefix} 短线 {signal.horizon_min}min · {signal.coin} {dir_cn} "
        f"@ ${signal.reference_price:,.0f} · 置信 {signal.confidence}"
    )


def _build_html(signal: ScalpSignal, *, test_mode_banner: str = "测试模式 · 不构成投资建议") -> str:
    dir_cn, color = _direction_cn(signal.direction)
    strat = _strategy_cn(signal.strategy.value)
    ts_cn = datetime.fromtimestamp(signal.created_at, tz=_BJ_TZ).strftime("%Y-%m-%d %H:%M:%S")
    expiry_cn = datetime.fromtimestamp(signal.expiry_ts, tz=_BJ_TZ).strftime("%H:%M:%S")

    # 5 因子表
    fb = signal.factor_breakdown
    factors_html = "".join([
        _factor_row("核心信号", fb.core_signal_strength, fb.weights["core_signal_strength"]),
        _factor_row("多周期对齐", fb.multi_tf_alignment, fb.weights["multi_tf_alignment"]),
        _factor_row("关键位质量", fb.key_level_quality, fb.weights["key_level_quality"]),
        _factor_row("数据新鲜度", fb.data_freshness, fb.weights["data_freshness"]),
        _factor_row("历史命中率", fb.historical_winrate, fb.weights["historical_winrate"]),
    ])

    # Evidence 列表
    ev_html = ""
    for ev in signal.evidence[:6]:  # 最多 6 条避免过长
        weight_color = {"high": "#dc2626", "medium": "#d97706", "low": "#6b7280"}.get(ev.weight, "#6b7280")
        ev_html += (
            f'<li style="margin-bottom:6px;">'
            f'<span style="color:{weight_color};font-weight:600;">[{ev.dimension}]</span> '
            f'{ev.observation}'
            f'</li>'
        )

    return f"""
<div style="font-family: -apple-system, sans-serif; max-width:560px; padding:16px; background:#fafafa;">
  <div style="background:#fef3c7; border-left:4px solid #f59e0b; padding:8px 12px; margin-bottom:12px; font-size:13px; color:#92400e;">
    ⚠ {test_mode_banner}
  </div>

  <div style="background:white; border-radius:8px; padding:16px; box-shadow:0 1px 3px rgba(0,0,0,0.06);">
    <div style="display:flex; align-items:center; justify-content:space-between; margin-bottom:8px;">
      <div style="font-size:14px; color:#6b7280;">{strat}</div>
      <div style="font-size:11px; color:#9ca3af;">{ts_cn}</div>
    </div>

    <div style="font-size:24px; font-weight:700; color:{color}; margin-bottom:4px;">
      {signal.coin} {dir_cn} · {signal.horizon_min}min
    </div>

    <div style="font-size:14px; color:#374151; margin-bottom:12px;">
      参考价 <strong>${signal.reference_price:,.2f}</strong>
      &nbsp;·&nbsp; 到期 {expiry_cn}
      &nbsp;·&nbsp; 置信 <strong>{signal.confidence}</strong> / 100
      &nbsp;·&nbsp; 命中预期 <strong>{
          (str(int(signal.hit_probability * 100)) + '% (校准 N=' + str(signal.calibration_sample_size) + ')')
          if signal.hit_probability is not None
          else '未校准（样本不足，仅供参考）'
      }</strong>
    </div>

    <div style="font-size:12px; color:#6b7280; padding:6px 10px; background:#f3f4f6; border-radius:4px; margin-bottom:12px;">
      Regime: <strong>{signal.regime}</strong> &nbsp;|&nbsp;
      多周期偏置: <strong>{signal.bias_score:+.2f}</strong>
    </div>

    <div style="font-size:12px; color:#374151; font-weight:600; margin-bottom:6px;">5 因子分解</div>
    <table style="width:100%; font-size:11px; border-collapse:collapse; margin-bottom:12px;">
      <thead><tr style="background:#f9fafb;">
        <th style="text-align:left; padding:4px 8px;">因子</th>
        <th style="text-align:right; padding:4px 8px;">分数</th>
        <th style="text-align:right; padding:4px 8px;">权重</th>
      </tr></thead>
      <tbody>{factors_html}</tbody>
    </table>

    <div style="font-size:12px; color:#374151; font-weight:600; margin-bottom:6px;">证据链</div>
    <ul style="font-size:12px; color:#4b5563; padding-left:18px; margin:0 0 12px 0;">
      {ev_html}
    </ul>

    <div style="font-size:11px; color:#9ca3af; padding-top:8px; border-top:1px solid #e5e7eb;">
      Signal ID: {signal.signal_id} &nbsp;|&nbsp; LIQ Scalp Engine V1
    </div>
  </div>
</div>
"""


def _factor_row(name: str, score: float, weight: float) -> str:
    pct = max(0, min(100, int(score * 100)))
    bar_color = "#16a34a" if pct >= 70 else "#d97706" if pct >= 50 else "#9ca3af"
    return (
        f'<tr>'
        f'<td style="padding:4px 8px; color:#374151;">{name}</td>'
        f'<td style="padding:4px 8px; text-align:right;">'
        f'  <span style="display:inline-block; width:60px; height:6px; background:#e5e7eb; border-radius:3px; vertical-align:middle; margin-right:4px;">'
        f'    <span style="display:block; width:{pct}%; height:6px; background:{bar_color}; border-radius:3px;"></span>'
        f'  </span>'
        f'  <span style="color:#374151;">{score:.2f}</span>'
        f'</td>'
        f'<td style="padding:4px 8px; text-align:right; color:#9ca3af;">×{weight:.2f}</td>'
        f'</tr>'
    )


async def send_scalp_signal_email(
    signal: ScalpSignal,
    config: "EmailNotificationConfig",
    *,
    test_mode_subject_prefix: str = "[测试]",
) -> bool:
    """异步发送短线信号邮件（线程池中执行 SMTP，不阻塞事件循环）

    Args:
        signal: ScalpSignal 实例
        config: EmailNotificationConfig（复用现有 SMTP 配置）
        test_mode_subject_prefix: 标题前缀（默认 [测试] 提醒非实盘）

    Returns:
        True 表示发送成功，False 失败/跳过（不抛错）
    """
    global _warned_missing_config
    if not config.to or not config.smtp_user:
        if not _warned_missing_config:
            logger.warning(
                "scalp email skipped: no recipients/smtp_user (suppressing further warnings)"
            )
            _warned_missing_config = True
        return False
    _warned_missing_config = False

    subject = _build_subject(signal, prefix=test_mode_subject_prefix)
    html = _build_html(signal)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = formataddr((str(Header(config.from_name, "utf-8")), config.smtp_user))
    msg["To"] = ", ".join(config.to)
    msg.attach(MIMEText(html, "html", "utf-8"))

    def _send():
        try:
            ctx = ssl.create_default_context()
            with smtplib.SMTP_SSL(
                config.smtp_host, config.smtp_port, context=ctx, timeout=15
            ) as server:
                server.login(config.smtp_user, config.smtp_pass)
                server.sendmail(config.smtp_user, config.to, msg.as_string())
            return True
        except Exception:  # noqa: BLE001
            logger.error("scalp email failed", exc_info=True)
            return False

    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(None, _send)
    if result:
        logger.info(
            "scalp email sent | %s %s %s @ %s confidence=%d",
            signal.coin, signal.direction, signal.strategy.value,
            signal.signal_id, signal.confidence,
        )
    return result
