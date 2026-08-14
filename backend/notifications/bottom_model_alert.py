"""BTC Bottom Model 首次进入确认区的纯 HTML 邮件模板。"""

from __future__ import annotations

import html
from typing import Any


def _escape(value: Any) -> str:
    return html.escape(str(value))


def _score(value: Any) -> str:
    try:
        return f"{float(value):.1f}"
    except (TypeError, ValueError):
        return "—"


def _list(items: list[str], empty: str) -> str:
    values = items[:8]
    if not values:
        values = [empty]
    return "".join(f"<li>{_escape(item)}</li>" for item in values)


def build_bottom_model_email(
    snapshot: dict[str, Any],
    *,
    page_url: str = "",
) -> tuple[str, str]:
    """返回稳定主题与 HTML；所有内容仅来自已持久化的 OK 日线快照。"""
    subject = "BTC 底部证据进入多项确认区"
    factors = snapshot.get("factors") or []
    factor_rows = "".join(
        "<tr>"
        f"<td style='padding:7px 8px;border-bottom:1px solid #e2e8f0'>{_escape(factor.get('label') or factor.get('key') or '—')}</td>"
        f"<td style='padding:7px 8px;text-align:right;border-bottom:1px solid #e2e8f0'><b>{_score(factor.get('score'))}</b></td>"
        "</tr>"
        for factor in factors
    ) or "<tr><td colspan='2' style='padding:7px 8px'>无可用因子</td></tr>"

    checks = (snapshot.get("confirmation") or {}).get("checks") or []
    satisfied = [
        f"{item.get('label') or item.get('key')}：{item.get('note') or '条件已满足'}"
        for item in checks
        if item.get("ok") is True and item.get("status") != "UNSCORABLE"
    ]
    unmet = [
        f"{item.get('label') or item.get('key')}：{item.get('note') or '尚未满足'}"
        for item in checks
        if item.get("ok") is not True or item.get("status") == "UNSCORABLE"
    ]
    quality = snapshot.get("data_quality") or {}
    quality_notes: list[str] = []
    quality_notes.extend(f"缺失：{item}" for item in quality.get("missing") or [])
    quality_notes.extend(
        f"过期：{item.get('metric')}（落后 {item.get('behind_days')} 天）"
        for item in quality.get("stale") or []
    )
    quality_notes.extend(
        f"采集失败：{key}（{value}）"
        for key, value in (quality.get("failed_fetches") or {}).items()
    )
    link = (
        f"<p style='margin:18px 0'><a href='{_escape(page_url)}' "
        "style='display:inline-block;padding:10px 14px;border-radius:7px;background:#0369a1;color:white;text-decoration:none'>查看底部证据页面</a></p>"
        if page_url else ""
    )
    stress = (snapshot.get("stress") or {}).get("score")
    confirmation = (snapshot.get("confirmation") or {}).get("score")
    quadrant = snapshot.get("quadrant") or {}
    overall_eq = (snapshot.get("evidence_quality") or {}).get("overall")

    body = f"""<!doctype html>
<html><head><meta charset="utf-8"></head>
<body style="margin:0;padding:24px;background:#f1f5f9;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif;color:#0f172a">
  <div style="max-width:700px;margin:auto;background:white;border-radius:12px;overflow:hidden;box-shadow:0 4px 18px rgba(15,23,42,.08)">
    <div style="padding:22px 26px;background:#064e3b;color:white">
      <div style="font-size:13px;opacity:.8">BTC 熊市底部证据与验证模型</div>
      <h2 style="margin:7px 0 0;font-size:22px">进入高压力／多项确认区</h2>
    </div>
    <div style="padding:24px 26px">
      <p style="margin-top:0;color:#334155">数据日 <b>{_escape(snapshot.get('day') or '—')}</b> · 象限 <b>{_escape(quadrant.get('label') or quadrant.get('key') or '—')}</b></p>
      <div style="display:flex;gap:10px;flex-wrap:wrap;margin:14px 0">
        <span style="padding:8px 11px;background:#fff7ed;border:1px solid #fed7aa;border-radius:7px">市场压力 <b>{_score(stress)}</b></span>
        <span style="padding:8px 11px;background:#ecfdf5;border:1px solid #a7f3d0;border-radius:7px">改善确认 <b>{_score(confirmation)}</b></span>
        <span style="padding:8px 11px;background:#f8fafc;border:1px solid #e2e8f0;border-radius:7px">证据质量 <b>{_score(overall_eq)}</b></span>
      </div>
      <h3 style="font-size:15px;margin:20px 0 8px">六项规则分数</h3>
      <table style="width:100%;border-collapse:collapse;font-size:13px">{factor_rows}</table>
      <h3 style="font-size:15px;margin:20px 0 6px;color:#047857">关键满足项</h3>
      <ul style="margin-top:6px;padding-left:20px;line-height:1.65">{_list(satisfied, '确认分已达到当前象限要求')}</ul>
      <h3 style="font-size:15px;margin:20px 0 6px;color:#b45309">尚未满足或不可评分项</h3>
      <ul style="margin-top:6px;padding-left:20px;line-height:1.65">{_list(unmet, '当前确认检查均已满足')}</ul>
      <h3 style="font-size:15px;margin:20px 0 6px">数据质量</h3>
      <ul style="margin-top:6px;padding-left:20px;line-height:1.65">{_list(quality_notes, 'quality_status = OK，无阻断级数据问题')}</ul>
      {link}
      <div style="margin-top:20px;padding:13px 15px;border-radius:8px;background:#fff7ed;color:#9a3412;font-size:12px;line-height:1.6">
        这是日线规则证据首次进入确认区的提醒，不代表已经找到绝对最低点，不是已校准概率，也不是买入指令。
      </div>
      <p style="margin:16px 0 0;color:#94a3b8;font-size:11px">模型 {_escape(snapshot.get('model_id') or snapshot.get('algorithm_version') or '—')} · 数据政策 {_escape(snapshot.get('data_policy_id') or '—')}</p>
    </div>
  </div>
</body></html>"""
    return subject, body
