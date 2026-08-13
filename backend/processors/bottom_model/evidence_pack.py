"""Bottom Model 证据包生成器：§ 分节 markdown，供一键复制给外部 AI 分析。

设计原则（copy-only 模式）：
- 头部内置分析指令模板，外部 AI 无需额外上下文即可开始工作
- 模型自身评分仅作"§1 摘要"供校对——指令明确要求 AI 独立审计而非复述
- 每个分数都附证据质量 EQ（§2/§3），防止把短窗口满分当成强证据
- 每指标标注实际历史窗口（§7），防止跨窗口误比分位数
- 显式声明指标间相关性与跨层重复（§8），防止把一份证据数成多份
- 附原始序列（§6）与滚动上下文（§9），让 AI 能核验计算而非盲信
- 历史频率层（§10）把模型读数对照实测前向结果，作为对规则引擎的独立检验；
  新章节一律追加在末尾——现有单测按 § 编号断言，编号必须稳定
"""

from __future__ import annotations

from bisect import bisect_right
from datetime import date, timedelta
from typing import Any, Optional

from processors.bottom_model.factors import percentile_rank, values_of
from processors.bottom_model.metrics import sanitize_series
from storage.bottom_model_store import BottomModelStore

# §6 原始序列：指标 → (显示名, 尾部天数, 数值格式)
_SERIES_SPEC: tuple[tuple[str, str, int, str], ...] = (
    ("btc_price_onchain", "BTC 价格 (USD)", 30, "{:.0f}"),
    ("sth_realized_price", "STH Realized Price", 30, "{:.0f}"),
    ("ma_200w", "200 周均线", 30, "{:.0f}"),
    ("mvrv_zscore", "MVRV Z-Score", 30, "{:.3f}"),
    ("nupl", "NUPL", 30, "{:.4f}"),
    ("sopr", "aSOPR", 30, "{:.4f}"),
    ("sth_sopr", "STH-SOPR", 30, "{:.4f}"),
    ("puell_multiple", "Puell Multiple", 30, "{:.3f}"),
    ("realized_loss", "已实现亏损 (USD)", 30, "{:.3e}"),
    ("realized_profit", "已实现利润 (USD)", 30, "{:.3e}"),
    ("oi_agg_usd", "聚合 OI (USD)", 30, "{:.3e}"),
    ("cme_oi_usd", "CME OI (USD)", 30, "{:.3e}"),
    ("funding_oiw", "OI 加权资金费 (%/8h)", 30, "{:.5f}"),
    ("liq_long_usd", "多头清算 (USD)", 30, "{:.3e}"),
    ("etf_flow_usd", "ETF 日净流 (USD)", 30, "{:.3e}"),
    ("coinbase_premium_rate", "Coinbase 溢价率 (%)", 30, "{:.3f}"),
    ("spot_net_taker_usd", "现货净 taker 买入 (USD)", 30, "{:.3e}"),
    ("stablecoin_total_mcap", "稳定币总市值 (USD)", 30, "{:.4e}"),
    ("fear_greed", "恐惧贪婪指数", 30, "{:.0f}"),
    ("cme_close_1w", "CME 周收盘 (USD)", 12, "{:.0f}"),
    ("cme_vol_1w", "CME 周成交量 (张)", 12, "{:.0f}"),
)

# §9 滚动上下文：指标 → (显示名, 数值格式, 变化口径, 序列频率)
# mode="rel" 用百分比变化（水平量），"abs" 用绝对差（本身已是比率/指数/流量）
_CONTEXT_SPEC: tuple[tuple[str, str, str, str, str], ...] = (
    ("btc_price_onchain", "BTC 价格", "{:.0f}", "rel", "daily"),
    ("mvrv_zscore", "MVRV Z-Score", "{:.2f}", "abs", "daily"),
    ("nupl", "NUPL", "{:.3f}", "abs", "daily"),
    ("reserve_risk", "Reserve Risk", "{:.5f}", "abs", "daily"),
    ("puell_multiple", "Puell Multiple", "{:.2f}", "abs", "daily"),
    ("sopr", "aSOPR", "{:.4f}", "abs", "daily"),
    ("sth_sopr", "STH-SOPR", "{:.4f}", "abs", "daily"),
    ("realized_loss", "已实现亏损（负值）", "{:.3e}", "abs", "daily"),
    ("sth_supply", "STH 供应", "{:.4e}", "rel", "daily"),
    ("oi_agg_usd", "聚合 OI", "{:.3e}", "rel", "daily"),
    ("cme_oi_usd", "CME OI", "{:.3e}", "rel", "daily"),
    ("funding_oiw", "OI 加权资金费 (%/8h)", "{:.5f}", "abs", "daily"),
    ("etf_flow_usd", "ETF 日净流", "{:.3e}", "abs", "daily"),
    ("coinbase_premium_rate", "Coinbase 溢价率 (%)", "{:.3f}", "abs", "daily"),
    ("spot_net_taker_usd", "现货净 taker 买入", "{:.3e}", "abs", "daily"),
    ("stablecoin_total_mcap", "稳定币总市值", "{:.4e}", "rel", "daily"),
    ("exchange_balance_btc", "交易所余额", "{:.4e}", "rel", "daily"),
    ("fear_greed", "恐惧贪婪", "{:.0f}", "abs", "daily"),
    ("global_m2_yoy", "全球 M2 同比", "{:.2f}", "abs", "weekly"),
)

_RELIABILITY_LABELS = {"high": "高", "medium": "中", "low": "低"}

_INSTRUCTIONS = """\
你的角色是**第二层审计员**，不是分析员。下面是一份 BTC 熊市底部评估证据包，\
由确定性规则引擎生成（无 AI 参与）。你的任务是审计这份证据、指出模型看不见的\
东西，而不是复述 §1 的结论。

**硬性禁止**
1. 禁止重算或"修正"模型分数。分数是既成事实，你审计的是它的**证据基础**。
2. 禁止把高分当成高预测力。分位 99 只说明"历史同期极少更极端"，不说明\
"接下来会涨"。
3. 禁止跨窗口比较分位数。清算/资金费仅 2023-11 起（不足一个周期），其 99 分位\
的含金量远低于 2010 年起的链上指标——每个子信号已给出证据质量 EQ（0-100），\
EQ 低于 50 的必须显式降权说明。
4. 禁止把高相关指标当作独立证据。§8 列出了实测相关系数，估值簇内多个指标\
本质是同一份证据的不同写法。
5. §10 的历史频率是**条件分布的观测值，不是预测概率**。禁止把"52 周胜率 77%"\
转写成"上涨概率 77%"——周级时点的前向窗口高度重叠，有效样本量看 independent\
（不重叠观测数）而非 points；independent < 5 的格子已不输出百分比，你也不要\
自己去凑一个。除 §10 已给出的频率外，其余判断只允许使用定性档位：\
低 / 中低 / 中 / 中高 / 高。
6. 禁止在历史类比中使用当年不可得的信息（模型已做点对点截断，你也要遵守）。
7. 必须区分"数据事实"（证据包里的数字）与"你的推断"（你的解读），\
在文中显式标注。

**必须应用的领域判据**
- 极端 ≠ 底部：极端读数可以持续数月，且可以在更低的价格上再次出现。
- 抬高低点 ≠ 趋势反转：需要看是否收复成本线、是否出现更高高点（见 §3 结构阶段）。
- 资金费回正必须结合 OI：若 OI 同步快速回堆，是杠杆重建而非健康修复。
- ETF 单周流入不算需求回归，要看持续性与规模相对市值。
- Coinbase 溢价为负 = 美国现货资金在净卖出，需求判断不能只看成交总量；\
现货净 taker 买入要看**连续性与相对市值规模**，单日转正没有意义。
- 稳定币增长是**流动性弹药**，不等于已经进场的买盘。
- 卖方衰竭要看"亏损兑现衰减"而非"价格不跌"。

**历史频率（§10）的用法**
- 它是对模型的**独立检验**，不是模型的背书。若频率与模型分数方向矛盾，\
必须显式指出矛盾，并判断哪一个更可信、理由是什么。
- **任何结论必须绑定时间尺度**。同一状态在 13 周与 52 周的胜率可能相反，\
只说"接近底部"而不说在哪个时间尺度上，等于没有结论。
- 注意 Confirmation 分档表：若它在各档之间几乎没有区分度，说明确认层是\
趋势跟随指标而非底部指标，你应据此下调对确认分的权重。

**允许弃权**
若证据不足以支撑判断，你应当在第 8 项直接给出"证据不足"，并列出还缺什么\
（哪个指标、什么读数、持续多久）才能判断。给一个勉强的裁决比说不知道更糟。

**输出结构**（简洁，能用一句说清就不要三句）
1. 执行摘要（3-5 句，必须带时间尺度）
2. 最强的 5 项支持证据（每项注明 EQ 与你的可信度）
3. 最强的 5 项反对证据
4. 市场状态诊断（一节写完，不要拆成四段）：卖方衰竭到哪一步 / 结构处于 §3 的\
哪一阶段 / 需求是弹药还是真实买盘 / 当前由现货还是杠杆驱动
5. 历史频率对照：模型读数与 §10 实测频率是否一致；不一致时你信哪一个、为什么
6. 假底风险与"证明本判断错误"的失效条件
7. 未来 4-12 周的 3 个可观测触发条件（要可证伪、带阈值）
8. 模型审计与最终裁决：模型高估了什么、低估了什么、遗漏了什么；底部区域的\
定性档位 + 绑定时间尺度，或明确弃权并说明缺什么证据
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
        eq = factor.get("evidence_quality")
        lines.append(
            f"\n### {factor['label']}（权重 {factor['weight']:.0%}）"
            f" — 得分 {score}，子信号覆盖率 {factor['coverage']:.0%}"
            + (f"，证据质量 {eq:.0f}" if eq is not None else "") + "\n"
        )
        lines.append("| 子信号 | 当前值 | 混合分位 | 得分 | 证据质量 | 备注 |")
        lines.append("|---|---|---|---|---|---|")
        for sub in factor["sub_signals"]:
            sub_eq = sub.get("evidence_quality")
            eq_cell = f"{sub_eq:.0f}" if sub_eq is not None else "—"
            if sub.get("eq_note"):
                eq_cell += f"<br/><sub>{sub['eq_note']}</sub>"
            lines.append(
                f"| {sub['label']} | {_fmt(sub['value'])} | "
                f"{_fmt(sub['percentile'], '{:.1f}')} | "
                f"{_fmt(sub['score'], '{:.0f}')} | {eq_cell} | {sub['note'] or '—'} |"
            )
    return lines


def _rolling_context_section(data: dict[str, list[list[Any]]]) -> list[str]:
    """§9 自然日上下文；不再把稀疏序列的 N 行误称为 N 天。"""
    lines = [
        "| 指标 | 当前值 | Δ7天 | Δ30天 | Δ90天 | 冻结窗口分位 | 30天前分位 | 冻结区间 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for metric, label, fmt, mode, cadence in _CONTEXT_SPEC:
        rows = sanitize_series(metric, data.get(metric) or [])
        if len(rows) < 2:
            continue
        days = [day for day, _ in rows]
        current = rows[-1][1]
        vals = values_of(rows)

        def _index_days_ago(periods: int) -> int:
            target = (date.fromisoformat(rows[-1][0]) - timedelta(days=periods)).isoformat()
            return bisect_right(days, target) - 1

        def _delta(periods: int) -> str:
            idx = _index_days_ago(periods)
            if idx < 0:
                return "—"
            past = rows[idx][1]
            if mode == "rel":
                return f"{(current - past) / abs(past):+.1%}" if abs(past) > 1e-12 else "—"
            return f"{current - past:+.4g}"

        prev_idx = _index_days_ago(30)
        prev_pct = percentile_rank(vals, rows[prev_idx][1]) if prev_idx >= 0 else None
        lines.append(
            f"| {label} | {_fmt(current, fmt)} | {_delta(7)} | {_delta(30)} | "
            f"{_delta(90)} | {percentile_rank(vals, current):.0f} | "
            f"{_fmt(prev_pct, '{:.0f}')} | "
            # 区间跨越多个数量级（如价格 0.056 → 124753），固定格式会把极小值
            # 显示成 0，这里统一用有效数字格式
            f"{_fmt(min(vals), '{:.6g}')} ~ {_fmt(max(vals), '{:.6g}')} |"
        )
    return lines


def _correlation_section(audit: dict[str, Any]) -> list[str]:
    lines = [
        f"\n以下相关系数在最近 {audit.get('window_days', 1095)} 天的重叠交易日上实测。"
        "**|ρ| ≥ 0.70 的两个指标不构成独立证据**，把它们分别计入"
        "\"支持底部的证据\"会重复计分。\n",
    ]
    for group in audit.get("groups") or []:
        lines.append(f"\n**{group['label']}** — {group['note']}\n")
        lines.append("| 指标 A | 指标 B | ρ | 重叠样本 |")
        lines.append("|---|---|---|---|")
        for pair in group["pairs"]:
            lines.append(f"| {pair['a']} | {pair['b']} | {pair['rho']:+.2f} | {pair['n']} |")
    redundancies = audit.get("structural_redundancies") or []
    if redundancies:
        lines.append("\n**由定义决定的冗余（不给相关系数，因为那只是同义反复）**\n")
        for item in redundancies:
            lines.append(
                f"- {item['topic']}：{item['basis']} → {item['conclusion']}"
            )
    overlaps = audit.get("cross_layer_overlaps") or []
    if overlaps:
        lines.append("\n**跨层重复使用清单（同一现象在不同层的不同用法）**\n")
        for item in overlaps:
            lines.append(f"- **{item['topic']}**：{item['usage']}。{item['note']}")
    return lines


def _hit_seq(windows: list[dict[str, Any]]) -> str:
    return " / ".join(
        f"{w['weeks']}周 {w['hit_rate']:.0f}%" if w.get("reliable")
        else f"{w['weeks']}周 样本不足"
        for w in windows
    )


def _base_rate_pointer(base_rate: Optional[dict[str, Any]]) -> str:
    """§1 摘要里的一行交叉引用，避免阅读顺序上漏掉末尾的 §10。"""
    if not base_rate:
        return "本次快照未含历史频率层（回放样本不足）"
    conditions = base_rate.get("conditions") or []
    baseline = (base_rate.get("baseline") or {}).get("windows") or []
    parts = [f"全样本基准 {_hit_seq(baseline)}"] if baseline else []
    if conditions:
        first = conditions[0]
        parts.insert(0, f"{first.get('label')} {_hit_seq(first.get('windows') or [])}")
    threshold = base_rate.get("hit_threshold_pct", 30)
    return (
        f"{'；'.join(parts)}（胜率 = N 周后涨幅 ≥ {threshold:.0f}%）"
        "——完整分档与口径见 §10，结论必须绑定时间尺度"
    )


def _base_rate_windows(label: str, windows: list[dict[str, Any]]) -> list[str]:
    rows: list[str] = []
    for w in windows:
        if w.get("reliable"):
            hit = f"{w['hit_rate']:.1f}%"
            med = f"{w['median_return']:+.1f}%"
        else:
            hit = med = f"样本不足（不给频率）"
        rows.append(
            f"| {label} | {w['weeks']} 周 | {w['points']} | {w['independent']} | "
            f"{hit} | {med} | {_fmt(w.get('worst_return'), '{:+.1f}')}% |"
        )
    return rows


def _base_rate_section(base_rate: dict[str, Any]) -> list[str]:
    """§10 历史频率层：模型读数 vs 实测前向结果。"""
    replay = base_rate.get("replay") or {}
    threshold = base_rate.get("hit_threshold_pct", 30)
    lines = [
        f"\n按 {base_rate.get('algorithm_version')} 算法回放 "
        f"{replay.get('first_day')} ~ {replay.get('last_day')} 的 "
        f"{replay.get('points')} 个周级时点，统计各条件下**前向终点收益**的分布。"
        f"胜率 = N 周后收盘涨幅 ≥ {threshold:.0f}% 的比例。\n",
        "\n**这是对模型的独立检验，不是模型的背书。**"
        "先看全样本基准，任何条件频率只有相对基准的超额才有意义。\n",
        "| 条件 | 窗口 | 时点数 | 不重叠观测 | 胜率 | 中位收益 | 最差一次 |",
        "|---|---|---|---|---|---|---|",
    ]
    baseline = base_rate.get("baseline") or {}
    if baseline:
        lines.extend(_base_rate_windows(
            f"**{baseline.get('label')}**", baseline.get("windows") or [],
        ))
    for cond in base_rate.get("conditions") or []:
        lines.extend(_base_rate_windows(cond.get("label", "—"), cond.get("windows") or []))
    for cond in base_rate.get("conditions") or []:
        if cond.get("description"):
            lines.append(f"\n- {cond['label']}：{cond['description']}")

    def _ladder_table(title: str, ladder: list[dict[str, Any]], note: str) -> None:
        if not ladder:
            return
        lines.append(f"\n**{title}**\n")
        lines.append(f"{note}\n")
        lines.append("| 门槛 | 时点数 | " + " | ".join(
            f"{w['weeks']}周胜率（不重叠 n）" for w in ladder[0]["windows"]
        ) + " |")
        lines.append("|---|---|" + "---|" * len(ladder[0]["windows"]))
        for step in ladder:
            cells = []
            for w in step["windows"]:
                cells.append(
                    f"{w['hit_rate']:.1f}%（{w['independent']}）"
                    if w.get("reliable") else f"样本不足（{w['independent']}）"
                )
            lines.append(
                f"| ≥ {step['threshold']:.0f} | {step['points']} | " + " | ".join(cells) + " |"
            )

    _ladder_table(
        "Stress 分档单调性", base_rate.get("stress_ladder") or [],
        "压力越高、前向胜率越高，说明压力维度本身是有效的——这是模型可信的"
        "正面证据。最高档常因不重叠观测过少而弃权，那是样本问题，不是反例。",
    )
    _ladder_table(
        "Confirmation 分档单调性", base_rate.get("confirmation_ladder") or [],
        "若各档胜率几乎相同，说明确认层**不是**底部指标而是趋势跟随指标："
        "牛市途中它长期维持高位。这是模型的已知弱点，请据此调整权重。",
    )
    caveats = base_rate.get("caveats") or []
    if caveats:
        lines.append("\n**口径与局限**\n")
        lines.extend(f"- {item}" for item in caveats)
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
    eq_summary = snapshot.get("evidence_quality") or {}

    lines: list[str] = [
        "# BTC 熊市底部证据与验证模型 · 日常证据包",
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
        f"- **整体证据质量 EQ**：{_fmt(eq_summary.get('overall'), '{:.0f}')} / 100"
        f"（压力层 {_fmt(eq_summary.get('stress'), '{:.0f}')}，"
        f"确认层 {_fmt(eq_summary.get('confirmation'), '{:.0f}')}）"
        "——EQ 由历史跨度、可用分位窗口、数据新鲜度、代理关系四项推导，"
        "已作为子信号聚合的权重乘子；EQ 越低，同样的分数越不该被当真",
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
        f"- **历史频率对照**：{_base_rate_pointer(snapshot.get('base_rate'))}",
        "\n## §2 六因子明细\n",
        "评分方向：0-100，**越高越符合历史底部特征**。分位为 3y/5y/全历史混合"
        "（窗口不足自动退化，见 §7）。证据质量 EQ = 跨度 × 可用窗口 × 新鲜度 × "
        "代理折扣，其中跨度以两个 BTC 周期（8 年）为满分锚点——因此 2023 年才"
        "开始的清算/资金费即便读数极端，EQ 也只有 30 出头。",
    ]
    lines.extend(_factor_section(snapshot.get("factors") or []))

    lines.append("\n## §3 确认信号与假底过滤器\n")
    lines.append("确认信号（快变量，与压力层分离，按 EQ 加权汇总）：\n")
    for check in confirmation.get("checks") or []:
        check_eq = check.get("evidence_quality")
        lines.append(
            f"- {check['label']}：{_fmt(check['score'], '{:.0f}')}"
            + (f"｜EQ {check_eq:.0f}" if check_eq is not None else "")
            + (f"（{check['note']}）" if check.get("note") else "")
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
    lines.append(
        "相似度为整数（共同因子仅 3-6 个，小数位是假精度）。可信度由共同因子"
        "数量与当年数据的证据质量共同决定，低可信度的类比只能当线索。\n"
    )
    lines.append("| 历史底部 | 相似度 | 共同因子 | 可信度 | 备注 |")
    lines.append("|---|---|---|---|---|")
    for analog in snapshot.get("analogs") or []:
        reliability = _RELIABILITY_LABELS.get(analog.get("reliability", ""), "—")
        lines.append(
            f"| {analog['day']} {analog['label']} | "
            f"{_fmt(analog['similarity'], '{:.0f}')} | "
            f"{len(analog.get('common_factors') or [])}/6 | {reliability} | "
            f"{analog.get('note', '')} |"
        )

    frozen = snapshot.get("frozen_series") or {}
    if frozen or store is not None:
        lines.append("\n## §6 关键指标原始序列（近段尾部，升序）\n")
        for metric, label, days, fmt in _SERIES_SPEC:
            source_rows = frozen.get(metric) if frozen else store.series(metric, limit=days)
            rows = sanitize_series(metric, source_rows or [])[-days:]
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

    audit = snapshot.get("correlation_audit")
    if audit:
        lines.append("\n## §8 相关性与重复计分声明\n")
        lines.extend(_correlation_section(audit))

    if frozen or store is not None:
        lines.append("\n## §9 滚动上下文（变化速度与所处分位）\n")
        lines.append(
            "变化窗口按自然日定位；分位与区间只基于快照冻结切片，保证证据包可复现。\n"
        )
        context_data = frozen or {
            metric: store.series(metric, limit=120) for metric, *_ in _CONTEXT_SPEC
        }
        lines.extend(_rolling_context_section(context_data))

    # 章节无条件出现：§0 的指令引用了 §10，缺席会让外部 AI 找一个不存在的编号
    lines.append("\n## §10 历史频率层（模型读数的实测对照）\n")
    base_rate = snapshot.get("base_rate")
    if base_rate:
        lines.extend(_base_rate_section(base_rate))
    else:
        lines.append(
            "本次快照不含历史频率层：回放样本不足（刚部署、历史序列未补齐，"
            "或该快照由 v4 之前的算法生成）。§0 中涉及 §10 的规则本次不适用——"
            "**不要凭空构造频率数字**，此时只能使用定性档位。"
        )

    return "\n".join(lines) + "\n"
