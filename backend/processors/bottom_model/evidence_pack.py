"""Bottom Model 证据包生成器：§ 分节 markdown，供一键复制给外部 AI 分析。

设计原则（copy-only 模式）：
- 头部内置分析指令模板，外部 AI 无需额外上下文即可开始工作
- 模型自身评分仅作"§1 摘要"供校对——指令明确要求 AI 独立评估而非复述
- 每指标标注实际历史窗口（§7），防止跨窗口误比分位数
- 附近 30 天原始序列（§6），让 AI 能核验因子层的计算而非盲信
"""

from __future__ import annotations

from typing import Any, Optional

from storage.bottom_model_store import BottomModelStore

# §6 原始序列：指标 → (显示名, 尾部天数, 数值格式)
_SERIES_SPEC: tuple[tuple[str, str, int, str], ...] = (
    ("btc_price_onchain", "BTC 价格 (USD)", 30, "{:.0f}"),
    ("sth_realized_price", "STH Realized Price", 30, "{:.0f}"),
    ("ma_200w", "200 周均线", 30, "{:.0f}"),
    ("mvrv_zscore", "MVRV Z-Score", 30, "{:.3f}"),
    ("nupl", "NUPL", 30, "{:.4f}"),
    ("sopr", "aSOPR", 30, "{:.4f}"),
    ("realized_loss", "已实现亏损 (USD)", 30, "{:.3e}"),
    ("oi_agg_usd", "聚合 OI (USD)", 30, "{:.3e}"),
    ("cme_oi_usd", "CME OI (USD)", 30, "{:.3e}"),
    ("funding_oiw", "OI 加权资金费", 30, "{:.5f}"),
    ("liq_long_usd", "多头清算 (USD)", 30, "{:.3e}"),
    ("etf_flow_usd", "ETF 日净流 (USD)", 30, "{:.3e}"),
    ("stablecoin_total_mcap", "稳定币总市值 (USD)", 30, "{:.4e}"),
    ("fear_greed", "恐惧贪婪指数", 30, "{:.0f}"),
    ("cme_vol_1w", "CME 周成交量 (张)", 12, "{:.0f}"),
)

_INSTRUCTIONS = """\
你是一名加密市场周期研究员。以下是一份 BTC 熊市底部评估的结构化证据包，\
由确定性规则引擎生成。请**独立**完成分析，不要简单复述 §1 的模型结论：

1. 逐因子（§2）审查证据强弱，指出内部矛盾与最薄弱的环节；
2. 将 §4 反证清单与 §3 假底过滤器作为对抗性检验，回答"如果这不是底，\
最可能的原因是什么"；
3. 参考 §5 历史类比，但注意共同因子数量（少于 4 个的类比置信度有限）；
4. §7 声明了各指标的历史窗口差异——**不要跨窗口直接比较分位数**\
（如清算数据仅 2023-11 起，其"99 分位"含金量低于 2010 起的链上指标）；
5. 输出：a) 当前处于底部区域的定性概率（低/中低/中/中高/高）及依据；\
b) 未来 4-12 周最值得跟踪的 3 个确认/否定触发条件；c) 你与模型结论的分歧点。
"""


def _fmt(value: Any, fmt: str = "{:.4g}") -> str:
    if value is None:
        return "—"
    try:
        return fmt.format(value)
    except (ValueError, TypeError):
        return str(value)


def _factor_section(factors: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for factor in factors:
        score = _fmt(factor["score"], "{:.1f}") if factor["score"] is not None else "弃权"
        lines.append(
            f"\n### {factor['label']}（权重 {factor['weight']:.0%}）"
            f" — 得分 {score}，子信号覆盖率 {factor['coverage']:.0%}\n"
        )
        lines.append("| 子信号 | 当前值 | 混合分位 | 得分 | 备注 |")
        lines.append("|---|---|---|---|---|")
        for sub in factor["sub_signals"]:
            lines.append(
                f"| {sub['label']} | {_fmt(sub['value'])} | "
                f"{_fmt(sub['percentile'], '{:.1f}')} | "
                f"{_fmt(sub['score'], '{:.0f}')} | {sub['note'] or '—'} |"
            )
    return lines


def build_evidence_pack(snapshot: dict[str, Any],
                        store: Optional[BottomModelStore] = None) -> str:
    stress = snapshot.get("stress") or {}
    confirmation = snapshot.get("confirmation") or {}
    quadrant = snapshot.get("quadrant") or {}
    delta = snapshot.get("delta") or {}
    price_ctx = snapshot.get("price_context") or {}
    exhaustion = snapshot.get("seller_exhaustion")
    dq = snapshot.get("data_quality") or {}

    lines: list[str] = [
        "# BTC 熊市底部概率模型 · 证据包",
        f"\n数据日：{snapshot.get('day')} ｜ 算法：{snapshot.get('algorithm_version')} ｜ "
        "生成方式：确定性规则引擎（无 AI 参与）",
        "\n## §0 分析指令\n",
        _INSTRUCTIONS,
        "\n## §1 模型结论摘要（供校对，非最终答案）\n",
        f"- **Bottom Stress（市场压力）**：{_fmt(stress.get('score'), '{:.1f}')} / 100"
        f"（有效因子权重 {_fmt(stress.get('active_weight'), '{:.0%}')}，"
        f"弃权因子：{', '.join(stress.get('abstained') or []) or '无'}）",
        f"- **Bottom Confirmation（改善确认）**：{_fmt(confirmation.get('score'), '{:.1f}')} / 100"
        f"（假底惩罚前 {_fmt(confirmation.get('score_before_penalty'), '{:.1f}')}）",
        f"- **四象限状态**：{quadrant.get('label', '—')} — {quadrant.get('note', '')}",
        f"- **卖方衰竭指数**：{_fmt((exhaustion or {}).get('score'), '{:.1f}')}"
        + (f"（{exhaustion['components']}）" if exhaustion else ""),
        f"- **ΔStress**：7d {_fmt(delta.get('stress_7d'), '{:+.1f}')} / "
        f"30d {_fmt(delta.get('stress_30d'), '{:+.1f}')}；"
        f"**ΔConfirmation**：7d {_fmt(delta.get('confirmation_7d'), '{:+.1f}')} / "
        f"30d {_fmt(delta.get('confirmation_30d'), '{:+.1f}')}",
        f"- 价格上下文：现价 {_fmt(price_ctx.get('price'), '{:.0f}')}，"
        f"200W 均线 {_fmt(price_ctx.get('ma_200w'), '{:.0f}')}，"
        f"STH 成本 {_fmt(price_ctx.get('sth_realized_price'), '{:.0f}')}，"
        f"LTH 成本 {_fmt(price_ctx.get('lth_realized_price'), '{:.0f}')}",
        "\n## §2 六因子明细\n",
        "评分方向：0-100，**越高越符合历史底部特征**。分位为 3y/5y/全历史混合"
        "（窗口不足自动退化，见 §7）。",
    ]
    lines.extend(_factor_section(snapshot.get("factors") or []))

    lines.append("\n## §3 确认信号与假底过滤器\n")
    lines.append("确认信号（快变量，与压力层分离）：\n")
    for check in confirmation.get("checks") or []:
        lines.append(
            f"- {check['label']}：{_fmt(check['score'], '{:.0f}')}"
            f"{'（' + check['note'] + '）' if check.get('note') else ''}"
        )
    triggers = (snapshot.get("fake_bottom_filter") or {}).get("triggers") or []
    if triggers:
        lines.append("\n假底过滤器触发（对 Confirmation 施加惩罚）：\n")
        for trigger in triggers:
            lines.append(f"- **{trigger['label']}**（-{trigger['penalty']}）：{trigger['note']}")
    else:
        lines.append("\n假底过滤器：无触发。")

    ce = snapshot.get("counter_evidence") or {}
    lines.append("\n## §4 反证清单（对抗性检验）\n")
    lines.append("**支持底部的证据**：\n")
    lines.extend(f"- {item}" for item in ce.get("supporting") or ["（无）"])
    lines.append("\n**反对底部的证据**：\n")
    lines.extend(f"- {item}" for item in ce.get("opposing") or ["（无）"])

    lines.append("\n## §5 历史类比\n")
    lines.append("| 历史底部 | 相似度 | 共同因子 | 备注 |")
    lines.append("|---|---|---|---|")
    for analog in snapshot.get("analogs") or []:
        lines.append(
            f"| {analog['day']} {analog['label']} | "
            f"{_fmt(analog['similarity'], '{:.1f}')} | "
            f"{len(analog.get('common_factors') or [])}/6 | {analog.get('note', '')} |"
        )

    if store is not None:
        lines.append("\n## §6 关键指标原始序列（近段尾部，升序）\n")
        for metric, label, days, fmt in _SERIES_SPEC:
            rows = store.series(metric, limit=days)
            if not rows:
                continue
            body = ", ".join(fmt.format(v) for _, v in rows)
            lines.append(f"- **{label}**（{rows[0][0]} → {rows[-1][0]}）：{body}")

    lines.append("\n## §7 数据窗口与局限声明\n")
    metrics_meta = dq.get("metrics") or {}
    if metrics_meta:
        lines.append("| 指标 | 起始日 | 最新日 | 样本天数 |")
        lines.append("|---|---|---|---|")
        for metric in sorted(metrics_meta):
            meta = metrics_meta[metric]
            lines.append(
                f"| {metric} | {meta['first_day']} | {meta['last_day']} | {meta['days']} |"
            )
    if dq.get("missing"):
        lines.append(f"\n缺失指标：{', '.join(dq['missing'])}")
    if dq.get("stale"):
        stale_desc = ", ".join(
            f"{item['metric']}（滞后 {item['behind_days']} 天）" for item in dq["stale"]
        )
        lines.append(f"\n滞后指标：{stale_desc}")
    lines.append(
        "\n固有局限：清算/资金费仅 2023-11 起（一个周期都不到）；OI 2021-02 起；"
        "ETF 流 2024-01 起；BGeometrics 链上指标仅 4 年；CME 周量为 BTC=F 前月"
        "标准合约（5 BTC/张）代理指标，不含 Micro/期权。全球 M2 上游滞后约 1 个月。"
        "历史大底样本仅 4 个——任何'概率'表述都应保持谦逊。"
    )
    return "\n".join(lines) + "\n"
