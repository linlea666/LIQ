"""新闻智能 Agent · Prompt 库（D12 / D08 Layer 2 / D09）

职责：
  - 集中定义 news_structurer / news_brief 使用的 system + user prompt 模板
  - 所有 prompt 强调 JSON 输出，方便下游解析

重点约束：
  1. Layer 2 三层思维（Fact → Impact → Narrative）
  2. 地缘事件"反复拉扯"降档逻辑（flip_flop_warning + narrative_stage）
  3. 黑天鹅单条单发，保留独立段落 reasoning
  4. 简报层滚动维护：增量更新（保留有效 bullets / 删除过期 / 合并新事件）
"""

from __future__ import annotations

from typing import Any, Iterable


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Layer 2 · 新闻 AI 结构化 · System Prompt
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

NEWS_STRUCTURER_SYSTEM = """你是资深加密市场宏观策略师，擅长把新闻原文翻译为可交易的结构化信号。
严格执行「三层思维」处理每一条输入：

【Layer A · Fact 事实提炼】
- 谁 / 什么 / 何时 / 多少（数字）
- 避免夸张；不复述无关背景

【Layer B · Impact 影响评估】
- first_order_impact：一阶直接影响（<=40 字）
- second_order_impact：二阶传导，可为空（<=40 字）
- impact_score ∈ [-5, +5]（-5 极端利空；+5 极端利多；0 中性）
- horizon ∈ {immediate, short, medium, lasting}
- already_priced_in_pct ∈ [0, 100]：市场已定价百分比
- 对 BTC/ETH/ALT 差异化拆分（不是所有新闻一视同仁；如 ETF 利多 BTC/ETH 但对山寨影响不同）

【Layer C · Narrative 叙事归属】
- narrative_theme：稳定 theme_id（snake_case 英文，如 Fed_Rate_Policy / Middle_East_Iran / ETF_Spot_BTC / Stablecoin_Regulation）
- narrative_stage ∈ {new, continuing, reversal, fading, escalation, de-escalation}
- flip_flop_warning：本事件是否属于同一主题近 24h 的反复拉扯之一（参考输入给出的 theme 近期方向序列）
- 同主题 24h 内反复切换方向 → 设 flip_flop_warning=true 并把 |impact_score| 打 0.7 折扣（黑天鹅级除外）

【风险分类】
risk_type ∈ {none, geopolitical, regulatory, technical, macro_economic, black_swan}
- 战争/冲突/制裁/断交 → geopolitical
- ETF/SEC/立法 → regulatory
- 交易所宕机/协议漏洞 → technical
- CPI/FOMC/利率/PPI → macro_economic
- 极端跳档（0→4 等）→ black_swan

【严格输出协议】
- 必须且仅返回一个合法 JSON 数组：[{...}, {...}, ...]
- 数组顺序与输入 events 顺序一致；每条必须含 event_id（等于输入 external_id）
- 字段名严格与下面 schema 一致，缺失字段使用对应默认值
- 不得返回 markdown、反引号、自然语言解释

schema（每条）：
{
  "event_id": "string",
  "target": "macro|BTC|ETH|sector:DeFi|noise",
  "direction": "bullish|bearish|neutral|potential_reversal",
  "first_order_impact": "<=40字",
  "second_order_impact": "<=40字 或 空串",
  "impact_score": -5~+5 (int),
  "confidence": 0.0~1.0 (float),
  "source_credibility": 0.0~1.0 (float),
  "horizon": "immediate|short|medium|lasting",
  "narrative_theme": "snake_case_theme_id",
  "narrative_stage": "new|continuing|reversal|fading|escalation|de-escalation",
  "flip_flop_warning": true|false,
  "already_priced_in_pct": 0~100 (number),
  "risk_type": "none|geopolitical|regulatory|technical|macro_economic|black_swan",
  "impact_on_assets": [
    {"asset": "BTC|ETH|ALT|oil|usd|gold", "direction": "bullish|bearish|neutral", "magnitude": "low|medium|high"}
  ],
  "rationale_cn": "<=60字为什么是这个方向+幅度",
  "summary_cn": "<=20字",
  "trading_insight": "<=40字操作启示 或 空串",
  "tier": "blackswan|major|normal|minor"
}
"""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Layer 2 · User Prompt 构造
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def build_structurer_user_prompt(
    items: list[dict],
    *,
    active_narratives: list[dict],
    geo_states: list[dict],
    current_btc_price: float,
) -> str:
    """items: [{external_id, title, content, source_author, publish_time_ms, tags, heat_score, tier_hint}]
    active_narratives: [{theme_id, recent_directions[:5], flip_flop_count_24h, avg_abs_reaction_pct}]
    geo_states: [{theme_id, current_level, level_label, flip_flop_count_24h}]
    """
    lines: list[str] = []
    lines.append(f"【当前 BTC 价格】{_fmt_price(current_btc_price)}")
    lines.append("")

    if active_narratives:
        lines.append("【活跃叙事主题（供 flip-flop 判断用）】")
        for n in active_narratives[:10]:
            recent = "→".join(n.get("recent_directions", [])[-5:])
            ff = n.get("flip_flop_count_24h", 0)
            avg = n.get("avg_abs_reaction_pct", 0.0)
            lines.append(
                f"- {n.get('theme_id', '?')}: 最近={recent or '-'} flip_flop_24h={ff} avg_reaction={avg}%"
            )
        lines.append("")

    if geo_states:
        lines.append("【地缘状态】")
        for g in geo_states[:6]:
            lines.append(
                f"- {g.get('theme_id', '?')}: level={g.get('current_level', 0)} "
                f"({g.get('level_label', 'PEACE')}) flip_flop_24h={g.get('flip_flop_count_24h', 0)}"
            )
        lines.append("")

    lines.append("【待分析新闻条目】（按时间升序）")
    for it in items:
        lines.append(_format_one_event(it))

    lines.append("")
    lines.append("请严格按 system 规定输出一个 JSON 数组。")
    return "\n".join(lines)


def _format_one_event(item: dict) -> str:
    ts_ms = int(item.get("publish_time_ms") or item.get("publish_time") or 0)
    sec = ts_ms // 1000 if ts_ms > 0 else 0
    tags = item.get("tags") or item.get("raw_tags") or []
    heat = item.get("heat_score", 0.0)
    tier_hint = item.get("tier_hint") or item.get("tier") or "normal"
    return (
        f"- event_id={item.get('external_id', '')} | tier_hint={tier_hint} | "
        f"publish_ts={sec} heat={heat:.2f} tags={','.join(tags[:6])}\n"
        f"  title: {(item.get('title') or '').strip()[:200]}\n"
        f"  content: {(item.get('content') or '').strip()[:500]}"
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Layer 3c · 滚动新闻简报
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

NEWS_BRIEF_SYSTEM = """你是首席加密研究分析师，负责维护一份「24 小时滚动新闻简报」。
这份简报将被主交易 AI 作为"记忆"注入 prompt，所以必须极其精炼、去重、按重要性排序。

【结构要求】
- 分 4 个板块：macro（宏观）/ regulatory（监管）/ onchain（链上）/ risk（风险）
- 每板块 ≤5 条 bullets，每条 ≤40 个中文字符，按重要性降序
- tldr_cn：全局一句话总结，≤50 字
- tracked_themes：正在追踪的叙事主题（含当前立场 + 24h 反复拉扯次数）

【增量更新规则】
- 输入 prev_brief 存在时：保留仍然有效的 bullets；删除已过期 / 被新事件覆盖的；合并新要点
- 输入为空或 trigger=blackswan 时：基于全部输入事件重写
- 反复拉扯主题的最新一条事件 → 标注"反复"并降低其重要性
- 黑天鹅事件必须出现在 risk 板块首位

【严格输出协议】
必须且仅返回一个合法 JSON 对象：
{
  "tldr_cn": "<=50字",
  "sections": [
    {"section_id": "macro|regulatory|onchain|risk", "section_title_cn": "宏观/监管/链上/风险", "bullets": ["…", "…"]},
    ...
  ],
  "tracked_themes": [
    {"theme_id": "snake_case", "theme_name_cn": "中文名", "current_stance_cn": "<=20字", "flip_flop_count_24h": int, "relevance_score": 0~1}
  ]
}
- 不输出 markdown / 反引号 / 自然语言解释
- 不超过 max_chars 字符（字符数含全部 JSON 内容）
"""


def build_brief_full_user_prompt(
    events: list[dict],
    themes: list[dict],
    geo_overview: dict,
    *,
    max_chars: int,
) -> str:
    lines = [
        f"【目标字符上限】{max_chars}（超出会被截断）",
        "",
        "【地缘风险全局视图】",
        f"- overall_level={geo_overview.get('overall_level', 0)} "
        f"({geo_overview.get('overall_label', 'PEACE')}) "
        f"escalation_24h={geo_overview.get('escalation_count_24h', 0)} "
        f"blackswan_24h={geo_overview.get('has_blackswan_24h', False)}",
        "",
        "【活跃叙事主题】",
    ]
    for t in themes[:12]:
        lines.append(
            f"- {t.get('theme_id', '?')} ({t.get('theme_name_cn', '')}) | "
            f"bias={t.get('current_direction_bias', 'neutral')} "
            f"intensity={t.get('current_intensity', 0)}/5 "
            f"flip_flop_24h={t.get('flip_flop_count_24h', 0)} "
            f"hit_rate={t.get('hit_rate', 0)}"
        )
    lines.append("")

    lines.append("【过去 24h 结构化事件】（按时间升序）")
    for e in events[-80:]:  # 预留空间给 AI
        lines.append(_format_brief_event(e))

    lines.append("")
    lines.append("请按 system 规定返回一个 JSON 对象。")
    return "\n".join(lines)


def build_brief_incremental_user_prompt(
    prev_brief: dict,
    new_events: list[dict],
    themes: list[dict],
    geo_overview: dict,
    *,
    max_chars: int,
) -> str:
    lines = [
        f"【目标字符上限】{max_chars}",
        "",
        "【旧简报（请保留仍然有效的 bullets / 删除过期 / 替换更重要的）】",
        _compact_prev_brief(prev_brief),
        "",
        "【新增结构化事件（本周期）】",
    ]
    if not new_events:
        lines.append("- (无新增)")
    else:
        for e in new_events:
            lines.append(_format_brief_event(e))

    lines.append("")
    lines.append("【当前地缘全局】")
    lines.append(
        f"- overall_level={geo_overview.get('overall_level', 0)} "
        f"({geo_overview.get('overall_label', 'PEACE')}) "
        f"escalation_24h={geo_overview.get('escalation_count_24h', 0)}"
    )
    lines.append("")
    lines.append("【活跃叙事主题】")
    for t in themes[:10]:
        lines.append(
            f"- {t.get('theme_id', '?')} bias={t.get('current_direction_bias', 'neutral')} "
            f"intensity={t.get('current_intensity', 0)}/5 "
            f"flip_flop_24h={t.get('flip_flop_count_24h', 0)}"
        )
    lines.append("")
    lines.append("请返回新版 JSON 简报对象（schema 同 system）。")
    return "\n".join(lines)


def build_brief_shrink_user_prompt(current_json: str, target_chars: int) -> str:
    """当首次输出超限时，让 AI 精简到目标字符"""
    return (
        f"以下 JSON 简报超过 {target_chars} 字符上限，请在保留全部字段前提下精简内容到 <= {target_chars} 字符。"
        f"不得删除字段，只能缩短每条 bullet / rationale / 合并相似要点。\n"
        f"---\n{current_json}\n---\n"
        f"仅返回精简后的 JSON 对象。"
    )


def _format_brief_event(e: dict) -> str:
    """e 可以是 MarketEventSignal.model_dump() 或其精简版"""
    asset_tags = [(a.get("asset", ""), a.get("direction", "")) for a in (e.get("impact_on_assets") or [])[:3]]
    asset_str = ",".join(f"{a}:{d}" for a, d in asset_tags if a)
    eid = str(e.get("event_id") or e.get("external_id") or "")[:30]
    return (
        f"- id={eid} ts={e.get('ts', 0)} tier={e.get('tier', 'normal')} "
        f"risk={e.get('risk_type', 'none')} dir={e.get('direction', 'neutral')} "
        f"impact={e.get('impact_score', 0)} theme={e.get('narrative_theme', '')} "
        f"assets=[{asset_str}]\n"
        f"  summary: {(e.get('summary_cn') or e.get('first_order_impact') or '').strip()[:60]}"
    )


def _compact_prev_brief(pb: dict) -> str:
    """把 prev_brief 压缩为紧凑文本（只保留 sections + tracked_themes）"""
    out_parts: list[str] = [f"tldr: {(pb.get('tldr_cn') or '')[:80]}"]
    sections = pb.get("sections") or []
    for s in sections:
        sec_id = s.get("section_id", "?")
        bullets = s.get("bullets") or []
        if not bullets:
            continue
        out_parts.append(f"[{sec_id}]")
        for b in bullets[:5]:
            out_parts.append(f"  - {str(b)[:60]}")
    tt = pb.get("tracked_themes") or []
    if tt:
        out_parts.append("[themes]")
        for t in tt[:8]:
            out_parts.append(
                f"  - {t.get('theme_id', '?')}: {t.get('current_stance_cn', '')[:30]} "
                f"(flip_flop_24h={t.get('flip_flop_count_24h', 0)})"
            )
    return "\n".join(out_parts)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 辅助
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _fmt_price(p: float) -> str:
    try:
        p = float(p)
    except (TypeError, ValueError):
        return "-"
    if p <= 0:
        return "-"
    if p >= 1000:
        return f"${p:,.0f}"
    if p >= 1:
        return f"${p:,.2f}"
    return f"${p:.4f}"


def digest_items_for_prompt(items: Iterable[Any]) -> list[dict]:
    """把 RawNewsItem 列表转成 prompt 用的 dict（减重）"""
    out: list[dict] = []
    for it in items:
        if hasattr(it, "model_dump"):
            d = it.model_dump()
        elif isinstance(it, dict):
            d = it
        else:
            continue
        out.append({
            "external_id": d.get("external_id", ""),
            "title": d.get("title", ""),
            "content": d.get("content", "") or d.get("translated_content", ""),
            "source_author": d.get("source_author", ""),
            "publish_time_ms": d.get("publish_time", 0),
            "tags": d.get("raw_tags", []),
            "heat_score": d.get("heat_score", 0.0),
        })
    return out
