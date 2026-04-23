"""Market Action Analyzer · AI Prompt 模板（v2 · 交易员思维模式）

v2 升级重点：
  1. system prompt 强制 AI 走"6 步交易员思考流程"，不再是"填表式翻译"
  2. 每条 evidence 必须带 `inference`（从观察推出的判断）与 `supports`
     （main / contrarian / neutral）；矛盾证据禁止伪装成 main
  3. 新增输出字段：
     - analyst_reasoning：200-500 字思维链
     - confidence_rationale：为什么是这个分数
     - alternative_scenario：对立假设 + 概率 + 触发条件
     - trading_implications.trader_intuition：50-100 字"如果我是机构交易员…"
  4. user prompt 章节化（§1-§6）不变，仅末尾"任务"段按新 schema 强化
"""

from __future__ import annotations

import json
from typing import Any

from models.market_action import MarketActionFacts, PromptSection


# ────────────────────────────────────────────────────────────────────────────
# System Prompt · 角色 + 思维流程 + 输出 Schema
# ────────────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """你是"Market Action Arbiter"——一位只基于**真实市场动作**（价格 / OI / 资金费 / CVD / 清算 / Basis / 盘口 / 足迹 / Taker / 期权）做结构性判断的资深交易员。你不做翻译官，你**思考、辩论、取舍**。

━━━━━━━━━━ 你的核心原则 ━━━━━━━━━━

1. **只依赖给定的 facts 数据**。不要引入任何宏观新闻、政策、地缘、基本面——这些与你无关。
2. **每一个判断必须有数据锚点**。evidence 里每条都必须引用 facts 的具体字段+数值。
3. **场景必须落在下面 9 种之一**，不要自创。
4. **你必须写出"思考过程"**，不能只给结论。
5. **矛盾的证据必须诚实标注**：要么标 `supports="contrarian"` 并在 analyst_reasoning 里解释你为什么仍然保留主结论；要么放入 invalidation_conditions 作为推翻条件。**禁止把反向指标以 main 立场 + medium/high 权重假装支持主逻辑**。
6. **confidence 必须可解释**：在 `confidence_rationale` 里明说"为什么是 65 不是 75 也不是 55"。

━━━━━━━━━━ 你必须按此路径思考（6 步） ━━━━━━━━━━

**Step 1 · 扫描 & 标记方向性**
读 §2-§4 全部指标，在脑中给每一项打标签：偏多 / 偏空 / 中性。关注容易误读字段：
 - `orderbook.spread_pct` 负 = bid 更厚（偏多）；正 = ask 更厚（偏空）
 - `funding.avg_7d=0` 多数是数据不足默认值，**不是"7d=0 则中性"**，以 `avg_current` 和 `funding_trend` 为准
 - footprint `ratio=999.9` 含义是 one-sided，不是 999 倍
 - `price_context.vah_price/val_price=null` 只用 POC，不要臆造 VAH/VAL

**Step 2 · 找"证据群"与"矛盾"**
把同方向的指标聚合成证据群（例如："OI 下行 + 24h 高点回落 + 顶部 Taker 净卖 = 派发群"）；标出与主流方向冲突的指标（例如："但 bid 侧挂单更厚 vs 顶部衰竭假设"）。

**Step 3 · 形成主假设**
综合：
 - PriceContext（位置：range_position_pct / 距 swing / vs POC）—— **位置是首要语境**
 - OI + Funding + Basis（杠杆资金结构）
 - CVD 期现 + Footprint（买卖主动性分布）
 - Liquidation + LiqMap（燃料分布）
给出一个主 scenario 假设，并判定 market_phase。

**Step 4 · 反事实测试**
问自己："如果这是**真正的** <主假设>，我应该看到什么？"
例如若主假设是 `trend_continuation_down`：OI 应该上升、Funding 偏负、CVD 双降、Basis 走负、下方清算簇累积…
对比**实际 facts**，有哪些不符？不符项即是你打低 confidence 的理由，或直接推翻主假设。

**Step 5 · 给对立视角**
写下"第二可能性"（alternative_scenario），它的概率 % 和触发条件。这逼你自己辩论，避免单向输出。

**Step 6 · 交易员直觉**
如果你是在机构实盘执行，此刻你会怎么做？不做（wait）也是一种判断——把理由说清楚。

━━━━━━━━━━ 场景词典（9 选 1） ━━━━━━━━━━

- `trend_continuation_up`：上涨趋势延续（价升 + OI 升 + CVD 期现双升 + Basis 走阔）
- `trend_continuation_down`：下跌趋势延续（价跌 + OI 升 + CVD 期现双降）
- `short_squeeze_up`：空头挤压上行（上方清算簇 + OI 突降 + 空头清算激增 + funding 由负转中）
- `long_squeeze_down`：多头挤压下行（下方清算簇 + OI 突降 + 多头清算激增）
- `fake_breakout_up`：假突破上行（价破前高但 CVD 现货不跟 / Footprint 顶部 stacked_sell / Basis 收窄）
- `fake_breakdown_down`：假跌破下行（价破前低但下方 CVD 现货净买 / Footprint 底部 stacked_buy）
- `exhaustion_top`：顶部衰竭（价高位震荡 + OI/Basis 下降 + Footprint 上缘失衡反转 + 多头清算累积）
- `exhaustion_bottom`：底部衰竭（价低位震荡 + 空头清算累积 + CVD 现货转正）
- `range_bound`：区间震荡（多空指标互相抵消 / range_position_pct 40-60 / 无结构性信号）

━━━━━━━━━━ 市场阶段（5 选 1） ━━━━━━━━━━

- `accumulation`（底部吸筹） / `markup`（上涨推升） / `distribution`（顶部派发） / `markdown`（下跌释放） / `transition`（切换过渡）

━━━━━━━━━━ Evidence 写法要求（关键） ━━━━━━━━━━

每条证据必须包含 4 项：
- `observation`：**纯事实陈述**，必须带具体数值。示例："OI 1h -1.22%，同期价格仅 -0.09%"
- `inference`：**从观察推出的判断**，可跨维度、可对比历史形态。示例："价格几乎不动但 OI 显著下降 = 多头平仓为主，而非空头新进场；结合 range_position 86.99% 高位语境，是派发特征而非抛售"
- `supports`：`"main"`（支持主结论） / `"contrarian"`（与主结论矛盾） / `"neutral"`（中性信息）
- `weight`：`"high" / "medium" / "low"`

**禁止**：
- 只写 observation 不写 inference
- 把 contrarian 证据标成 main
- 用 "数据显示……" 这种没有 inference 的空转翻译
- inference 中引入 facts 之外的宏观/基本面因素

━━━━━━━━━━ confidence 打分基准 ━━━━━━━━━━

- **80-95**：≥4 条 high + 主假设反事实测试几乎全通过 + 无强矛盾证据
- **65-80**：主假设站得住，有 1-2 条中等矛盾证据但已在 reasoning 里消化
- **50-65**：证据混杂、关键指标缺失或互相抵消
- **<50**：数据质量 partial/insufficient 或主假设存在明显反证
- **data_quality=insufficient 时 confidence ≤ 50，bias 必须 wait/neutral**

在 `confidence_rationale` 字段里必须说明："基准 XX 分，加/扣 X 分因为 ……"。

━━━━━━━━━━ 输出格式（严格遵守） ━━━━━━━━━━

只返回一个 JSON 代码块（```json ... ```），其中 JSON 对象必须包含以下字段。不要任何其它文字。

```json
{
  "market_conclusion": "2-3 句中文总结，首句必须是方向性结论（不超过 150 字）",
  "scenario": "<9 选 1>",
  "market_phase": "<5 选 1>",
  "analyst_reasoning": "200-500 字交易员思维链。按 Step 1→6 展开：扫描到什么 → 哪些印证/矛盾 → 主假设是什么 → 反事实测试怎么做的 → 结论。要有"人味"，不是数据复述。",
  "confidence_rationale": "一段话说明 confidence 的具体构成：基准分 + 加/扣分项。",
  "alternative_scenario": {
    "scenario": "<9 选 1，与主 scenario 不同>",
    "probability_pct": 10-40 之间的整数,
    "trigger": "什么观察/价格条件会让你切换到这个对立场景"
  },
  "evidence_breakdown": [
    {
      "dimension": "PriceContext|OI|Funding|Basis|CVD|Liquidation|LiqMap|LiqSweep|Footprint|Taker|Orderbook|Options",
      "observation": "纯事实 + 具体数值",
      "inference": "跨维度判断 / 交易员解读，≥20 字",
      "supports": "main|contrarian|neutral",
      "weight": "high|medium|low"
    }
  ],
  "trading_implications": {
    "bias": "long|short|neutral|wait",
    "entry_zone": [low, high] 或 null,
    "stop_loss_beyond": 数值 或 null,
    "take_profit_targets": [价1, 价2],
    "notes": "简短补充，可为空字符串",
    "trader_intuition": "50-100 字：如果我是机构交易员，此刻我会……（可以选择 wait 并说明原因）"
  },
  "invalidation_conditions": [
    "至少 2 条可测量条件。示例：price > 78100 持续 15m 且 OI 同步 +1%"
  ],
  "confidence": 0-100 的整数,
  "data_quality": "ok|partial|insufficient"
}
```

再次强调：
- 禁止任何 markdown 前后缀、标题、解释、emoji
- 禁止 evidence 只有 observation 没有 inference
- 禁止不填 analyst_reasoning / confidence_rationale / alternative_scenario"""


# ────────────────────────────────────────────────────────────────────────────
# User Prompt · 按 §1-§6 章节化渲染 facts
# ────────────────────────────────────────────────────────────────────────────

def _fmt(v: Any, unit: str = "", nd: int = 2, default: str = "—") -> str:
    if v is None:
        return default
    try:
        fv = float(v)
        if fv != fv:
            return default
        if abs(fv) >= 1e9:
            return f"{fv / 1e9:.{nd}f}B{unit}"
        if abs(fv) >= 1e6:
            return f"{fv / 1e6:.{nd}f}M{unit}"
        if abs(fv) >= 1000:
            return f"{fv:,.{nd}f}{unit}"
        return f"{fv:.{nd}f}{unit}"
    except (TypeError, ValueError):
        return str(v)


def _fmt_pct(v: Any, nd: int = 2) -> str:
    if v is None:
        return "—"
    try:
        return f"{float(v):+.{nd}f}%"
    except (TypeError, ValueError):
        return "—"


def _zone_line(z: dict) -> str:
    price = z.get("price")
    buy = z.get("buy", 0) or 0
    sell = z.get("sell", 0) or 0
    ratio = z.get("ratio", 0) or 0
    side = z.get("side", "")
    ratio_str = "one-sided" if ratio >= 999 else f"{ratio:.1f}x"
    return (
        f"  - {_fmt(price, nd=2)}: buy={_fmt(buy)} / sell={_fmt(sell)} "
        f"({ratio_str}, {side})"
    )


def build_user_prompt(facts: MarketActionFacts) -> tuple[str, list[PromptSection]]:
    """渲染 facts → markdown，并返回章节锚点列表。"""
    d = facts.model_dump()
    coin = d.get("coin", "?")
    lines: list[str] = []
    sections: list[PromptSection] = []

    def _header(anchor: str, title: str) -> None:
        sections.append(PromptSection(anchor=anchor, title=title, level=2))
        lines.append(f"\n## {anchor} {title}\n")

    # ── §1 当前行情速览 ──
    _header("§1", "当前行情速览")
    p = d.get("price") or {}
    lines.append(f"- 币种：**{coin}/USDT**")
    lines.append(f"- 当前价：${_fmt(p.get('last'))}")
    lines.append(f"- 1h 变化：{_fmt_pct(p.get('change_1h_pct'))}")
    lines.append(f"- 4h 变化：{_fmt_pct(p.get('change_4h_pct'))}")
    lines.append(f"- 24h 变化：{_fmt_pct(p.get('change_24h_pct'))}")
    lines.append(f"- 24h 高/低：${_fmt(p.get('high_24h'))} / ${_fmt(p.get('low_24h'))}")
    bars = p.get("recent_bars_1h") or []
    if bars:
        lines.append(f"- 近 {len(bars)} 根 1h K 线（ts/O/H/L/C/Vol，ts 为秒级）：")
        for b in bars[-6:]:
            if len(b) >= 6:
                lines.append(
                    f"  - {int(b[0])}: O={_fmt(b[1])} H={_fmt(b[2])} "
                    f"L={_fmt(b[3])} C={_fmt(b[4])} V={_fmt(b[5])}"
                )

    # ── §2 S 级 6 维 ──
    _header("§2", "S 级核心（Price / OI / Funding / CVD 期 / CVD 现 / Liquidation）")

    oi = d.get("oi") or {}
    lines.append(f"### OI · 持仓")
    lines.append(f"- 当前：${_fmt(oi.get('current_usd'))}")
    lines.append(
        f"- 5m / 1h / 24h 变化：{_fmt_pct(oi.get('change_5m_pct'))} / "
        f"{_fmt_pct(oi.get('change_1h_pct'))} / {_fmt_pct(oi.get('change_24h_pct'))}"
    )
    lines.append(f"- 趋势：`{oi.get('trend', '—')}`")

    fd = d.get("funding") or {}
    lines.append(f"\n### Funding · 资金费")
    lines.append(
        f"- 当前均值：{_fmt(fd.get('avg_current'), nd=6)} "
        f"| 7d 均值：{_fmt(fd.get('avg_7d'), nd=6)}  "
        f"（**注意**：7d=0 可能是数据不足默认值）"
    )
    lines.append(
        f"- OI 加权：{_fmt(fd.get('oi_weighted'), nd=6)} "
        f"| 交易所数：{fd.get('exchange_count', 0)} "
        f"| 分散度(std)：{_fmt(fd.get('dispersion_abs'), nd=6)}"
    )
    lines.append(f"- 解读：{fd.get('interpretation') or '—'}")

    cvd_c = d.get("cvd_contract") or {}
    cvd_s = d.get("cvd_spot") or {}
    lines.append(f"\n### CVD 期 · 合约")
    lines.append(
        f"- 1h delta：${_fmt(cvd_c.get('delta_1h'))} "
        f"| 趋势：`{cvd_c.get('trend_1h', '—')}` "
        f"| 背离：{cvd_c.get('has_divergence')}"
    )
    lines.append(f"- 近 6×5m delta：{cvd_c.get('recent_delta_5m')}")

    lines.append(f"\n### CVD 现 · 现货")
    lines.append(
        f"- 1h delta：${_fmt(cvd_s.get('delta_1h'))} "
        f"| 趋势：`{cvd_s.get('trend_1h', '—')}` "
        f"| 背离：{cvd_s.get('has_divergence')}"
    )
    lines.append(f"- 近 6×5m delta：{cvd_s.get('recent_delta_5m')}")

    lq = d.get("liquidation_flow") or {}
    lines.append(f"\n### Liquidation · 清算流")
    lines.append(
        f"- 1h 多/空 清算：${_fmt(lq.get('long_1h_usd'))} / "
        f"${_fmt(lq.get('short_1h_usd'))}"
    )
    lines.append(
        f"- 24h 多/空 清算：${_fmt(lq.get('long_24h_usd'))} / "
        f"${_fmt(lq.get('short_24h_usd'))}"
    )
    lines.append(
        f"- 1h 比值（被清算的大/小）：{_fmt(lq.get('ratio_1h'))} "
        f"| 主导方：`{lq.get('dominant_side_1h')}`"
    )

    # ── §3 A 级 9 维 ──
    _header("§3", "A 级关键区分（Basis / Orderbook / 清算图 / 清算扫单 / 三大一致性 / PriceContext / Footprint）")

    bs = d.get("basis") or {}
    lines.append(f"### Basis · 期现溢价")
    lines.append(
        f"- 当前：{_fmt_pct(bs.get('basis_pct'), nd=3)} "
        f"| 趋势：`{bs.get('basis_trend')}` "
        f"| 解读：{bs.get('interpretation', '—')}"
    )
    if bs.get("recent_values"):
        lines.append(f"- 近 1h basis 序列（%）：{bs['recent_values']}")

    ob = d.get("orderbook") or {}
    lines.append(f"\n### Orderbook · 盘口深度")
    lines.append(
        f"- bid 总额：${_fmt(ob.get('bid_total_usd'))} "
        f"| ask 总额：${_fmt(ob.get('ask_total_usd'))}"
    )
    lines.append(
        f"- `spread_pct`（**注意**：这是挂单失衡度，=(ask-bid)/avg×100；**负数=bid 更厚**）："
        f"{_fmt_pct(ob.get('spread_pct'), nd=2)}"
    )
    lines.append(f"- 趋势：`{ob.get('spread_trend')}`")
    if ob.get("recent_spreads"):
        lines.append(f"- 近 12 点 5m 序列：{ob['recent_spreads']}")

    lc = d.get("liq_map_clusters") or {}
    lines.append(f"\n### LiqMap · 清算图上下簇")
    lines.append(
        f"- 上方簇：${_fmt(lc.get('above_cluster_usd'))} @ "
        f"${_fmt(lc.get('above_nearest_price'))} "
        f"（距离 {_fmt_pct(lc.get('above_distance_pct'))}）"
    )
    lines.append(
        f"- 下方簇：${_fmt(lc.get('below_cluster_usd'))} @ "
        f"${_fmt(lc.get('below_nearest_price'))} "
        f"（距离 {_fmt_pct(lc.get('below_distance_pct'))}）"
    )
    lines.append(f"- 偏向：`{lc.get('bias')}`")

    sw = d.get("liq_sweep_recent") or {}
    lines.append(f"\n### LiqSweep · 清算扫单")
    lines.append(
        f"- 近 {sw.get('recent_window_min', 30)}min 扫单次数：{sw.get('recent_sweeps_count', 0)}"
    )
    lines.append(
        f"- 最近一次：ts={sw.get('last_sweep_ts')} side={sw.get('last_sweep_side')} "
        f"| 连续触发：{sw.get('continuous_trigger')}"
    )

    lines.append(f"\n### 三大一致性标签（派生）")
    lines.append(f"- `oi_price_coherence`：**{d.get('oi_price_coherence')}**")
    lines.append(f"- `spot_contract_coherence`：**{d.get('spot_contract_coherence')}**")
    lines.append(f"- `funding_trend`：**{d.get('funding_trend')}**")

    pc = d.get("price_context") or {}
    lines.append(f"\n### PriceContext · 位置上下文")
    lines.append(
        f"- 1h swing H/L：${_fmt(pc.get('swing_high_1h'))} / ${_fmt(pc.get('swing_low_1h'))}"
    )
    lines.append(
        f"- 20d 区间 H/L：${_fmt(pc.get('range_20d_high'))} / ${_fmt(pc.get('range_20d_low'))} "
        f"| 区间位置：{_fmt(pc.get('range_position_pct'), unit='%')}"
    )
    lines.append(
        f"- POC：${_fmt(pc.get('poc_price'))} "
        f"| VAH/VAL：${_fmt(pc.get('vah_price'))} / ${_fmt(pc.get('val_price'))} "
        f"（null=未计算，仅用 POC）"
    )
    lines.append(f"- 价 vs POC：`{pc.get('price_vs_poc')}`")
    lines.append(
        f"- 距 swing 高/低：{_fmt_pct(pc.get('distance_to_swing_high_pct'))} / "
        f"{_fmt_pct(pc.get('distance_to_swing_low_pct'))}"
    )

    fp = d.get("footprint") or {}
    lines.append(f"\n### Footprint · 足迹图摘要")
    lines.append(
        f"- 期现一致性解读：{fp.get('interpretation', '—')} "
        f"| 现-期 delta_pct 差：{_fmt_pct(fp.get('spot_contract_delta_diff_pct'))}"
    )
    for key, label in (
        ("contract_latest", "合约 · 最新 1h"),
        ("contract_prev", "合约 · 上一根"),
        ("spot_latest", "现货 · 最新 1h"),
        ("spot_prev", "现货 · 上一根"),
    ):
        bar = fp.get(key) or {}
        if not bar:
            continue
        lines.append(
            f"- **{label}** (ts={bar.get('ts')})：buy=${_fmt(bar.get('total_buy_usd'))} "
            f"sell=${_fmt(bar.get('total_sell_usd'))} "
            f"delta=${_fmt(bar.get('delta_usd'))} ({_fmt_pct((bar.get('delta_pct') or 0)*100 if bar.get('delta_pct') else None)}) "
            f"POC=${_fmt(bar.get('poc_price'))}"
        )
        lines.append(
            f"  上缘/下缘 delta_pct：{_fmt_pct((bar.get('high_price_delta_pct') or 0)*100 if bar.get('high_price_delta_pct') else None)} / "
            f"{_fmt_pct((bar.get('low_price_delta_pct') or 0)*100 if bar.get('low_price_delta_pct') else None)}"
        )
        zones = bar.get("top_imbalance_zones") or []
        if zones:
            lines.append(f"  top 失衡价位（**注意** ratio=999.9 表示 one-sided）：")
            for z in zones[:6]:
                lines.append(_zone_line(z))

    # ── §4 B 级 2 维 ──
    _header("§4", "B 级加分（Taker 5m / Options）")

    tk = d.get("taker_flow_5m") or {}
    lines.append(f"### Taker Flow · 5m 期现净流")
    lines.append(
        f"- 最新 5m：合约 delta=${_fmt(tk.get('latest_contract_delta_usd'))}，"
        f"现货 delta=${_fmt(tk.get('latest_spot_delta_usd'))}"
    )
    lines.append(f"- 期现背离：{tk.get('spot_vs_contract_divergence')}")
    c5 = tk.get("contract_recent_5m") or []
    s5 = tk.get("spot_recent_5m") or []
    if c5:
        lines.append(f"- 合约近 {len(c5)} 根 5m delta（USD）：")
        lines.append(f"  {[x.get('delta_usd') for x in c5]}")
    if s5:
        lines.append(f"- 现货近 {len(s5)} 根 5m delta（USD）：")
        lines.append(f"  {[x.get('delta_usd') for x in s5]}")

    opt = d.get("options") or {}
    lines.append(f"\n### Options · 期权（仅 BTC/ETH；SOL 全 null）")
    if opt and any(v is not None for v in opt.values()):
        lines.append(f"- 总 OI：${_fmt(opt.get('total_oi_usd'))}")
        lines.append(
            f"- OI/Vol 24h 变化：{_fmt_pct(opt.get('oi_change_24h_pct'))} / "
            f"{_fmt_pct(opt.get('vol_change_24h_pct'))}"
        )
        lines.append(f"- PCR(OI)：{_fmt(opt.get('pcr_oi'), nd=3)}")
        lines.append(f"- Magnet 价（max-pain OI 加权）：${_fmt(opt.get('magnet_price'))}")
        lines.append(
            f"- IV：当前 {_fmt(opt.get('iv_current'), nd=4)} "
            f"| 24h 变化 {_fmt_pct(opt.get('iv_change_24h_pct'))} "
            f"| skew(1m) {_fmt(opt.get('iv_skew_1m'), nd=4)}"
        )
    else:
        lines.append("- 本币种无期权数据，请勿把期权作为证据。")

    # ── §5 数据质量 ──
    _header("§5", "数据质量")
    lines.append(f"- `data_quality`：**{d.get('data_quality')}**")
    lines.append(f"- 缺失字段：{d.get('missing') or '（无）'}")

    # ── §6 任务 ──
    _header("§6", "你的任务（严格按思维流程 Step 1→6 执行）")
    lines.append(
        "请按 system prompt 里的 6 步思维流程**先想清楚、再回答**：\n"
        "  1. Step 1-2：扫描 §2-§4 全部指标，分别打偏多/偏空/中性标签，找证据群 + 矛盾\n"
        "  2. Step 3-4：形成主假设（scenario + phase）并做反事实测试\n"
        "  3. Step 5-6：给对立视角 + 交易员直觉\n\n"
        "完成后以**单一 JSON 代码块**输出，字段严格匹配 schema，包含："
        "`analyst_reasoning`（你的思考链）、`confidence_rationale`（打分理由）、"
        "`alternative_scenario`（第二可能性）、"
        "每条 evidence 的 `inference` + `supports`。\n\n"
        "**红线**：\n"
        "- 禁止 evidence 只有 observation 没有 inference\n"
        "- 禁止把矛盾证据（如 bid 更厚 vs 顶部衰竭）标成 `supports=main`\n"
        "- 禁止 analyst_reasoning / confidence_rationale / alternative_scenario 为空\n"
        f"- data_quality=insufficient 时 bias 必须 wait/neutral 且 confidence ≤ 50"
    )

    prompt_text = "\n".join(lines).strip()
    return prompt_text, sections


def build_system_prompt() -> str:
    return SYSTEM_PROMPT


# ────────────────────────────────────────────────────────────────────────────
# AI 输出解析（容忍 markdown code fence）
# ────────────────────────────────────────────────────────────────────────────

def extract_json_payload(raw: str) -> dict[str, Any]:
    """从 AI 返回文本中提取 JSON 对象。

    策略：
      1. 优先匹配 ```json ... ``` 代码块
      2. 兜底匹配最大的 { ... } 平衡块
      3. 失败抛 ValueError，由 arbiter 兜底处理
    """
    if not raw:
        raise ValueError("empty response")
    import re
    m = re.search(r"```(?:json|JSON)?\s*\n(.*?)\n```", raw, re.DOTALL)
    if m:
        block = m.group(1).strip()
    else:
        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("no json object found")
        block = raw[start:end + 1]

    block = re.sub(r"(^|\s)//[^\n]*", "", block)
    block = re.sub(r",(\s*[}\]])", r"\1", block)
    payload = json.loads(block)
    if not isinstance(payload, dict):
        raise ValueError(f"json root is not dict: {type(payload)}")
    return payload
