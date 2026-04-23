"""Market Action Analyzer · AI Prompt 模板

职责：
  - build_system_prompt()：AI 角色 + 场景词典 + 规则 + 输出 JSON Schema
  - build_user_prompt(facts) → (prompt_text, sections)：
      * 把 MarketActionFacts 渲染成 markdown 结构化文本
      * 返回章节锚点列表（前端生成 TOC）

设计要点（与用户拍板一致）：
  1. 9 场景严格枚举，AI 必须落在其中一类
  2. 每条 evidence 必须引用 facts 里的具体数值
  3. prompt 内显式解释**容易误读**的字段语义（spread_pct 负数 / ratio=999.9 等）
  4. 章节使用 `§N 标题` 结构，方便前端生成锚点跳转
  5. 严格 JSON 输出（代码块包裹），避免 AI 自由发挥
"""

from __future__ import annotations

import json
from typing import Any

from models.market_action import MarketActionFacts, PromptSection

# ────────────────────────────────────────────────────────────────────────────
# System Prompt · AI 的角色、规则、输出 Schema
# ────────────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """你是"Market Action Arbiter"——一位只基于**真实市场动作**（价格 / OI / 资金费 / CVD / 清算 / Basis / 盘口 / 足迹 / Taker / 期权）判断行情结构的交易研究员。

━━━━━━━━━━ 你的工作原则 ━━━━━━━━━━

1. **只依赖给定的 facts 数据**。不要引入任何宏观新闻、政策、地缘、项目基本面等"叙事因素"——这些与你无关。
2. **每一个判断必须有数据锚点**。在 evidence 里必须引用 facts 的具体字段+数值（例如 "OI 1h 变化 -1.9%"、"合约 Footprint delta 占比 +17.3%"）。
3. **场景必须落在下面 9 种之一**，不要自创新场景。
4. **confidence 与证据强度一致**：
   - 有 ≥4 条高权重证据且互相印证 → 可给 70-85
   - 证据混杂或互相矛盾 → 40-60
   - 仅有 ≤2 条低权重证据 → 不超过 40
   - 数据质量 `insufficient` → 不超过 50
5. **invalidation_conditions 必须是可测量的价格/指标触发条件**，不是模糊口号。
6. `trading_implications.bias` 与 `scenario` 强一致：
   - `trend_continuation_up` / `short_squeeze_up` → long 或 wait
   - `trend_continuation_down` / `long_squeeze_down` → short 或 wait
   - `exhaustion_top` / `fake_breakout_up` → short 或 wait
   - `exhaustion_bottom` / `fake_breakdown_down` → long 或 wait
   - `range_bound` → wait 或 neutral
   - **数据质量 `insufficient` 时禁止 bias=long/short**，必须 wait 或 neutral。

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

- `accumulation`：底部吸筹
- `markup`：上涨推升
- `distribution`：顶部派发
- `markdown`：下跌释放
- `transition`：切换过渡（信号混杂）

━━━━━━━━━━ facts 字段语义约定（容易误读，必读） ━━━━━━━━━━

1. `orderbook.spread_pct`：**不是买卖价差**，而是 `(ask_total_usd - bid_total_usd) / avg × 100`。
   - 负数 = bid 侧挂单总额更大（潜在买盘更厚）
   - 正数 = ask 侧更厚（潜在卖压更大）
2. `footprint.*.top_imbalance_zones[].ratio = 999.9`：
   - 含义是 **one-sided**（另一边为 0 导致除零被截断），不是 999 倍
3. `funding.avg_7d = 0` 可能是 7d 历史不足的默认值，**不要强行解读为"7 天资金费为零"**，优先看 `avg_current` / `funding_trend`。
4. `price_context.vah_price / val_price` 可能为 null（Volume Profile 只计算了 POC）——**只用 `poc_price` 做参考**，不要臆造 VAH/VAL 水平。
5. `options.*` 仅对 BTC/ETH 生效，SOL 整块为 null，此时不把期权作为证据。
6. `liq_sweep_recent.recent_sweeps_count = 0` 表示近 30min 无连续清算扫单，中性事实。
7. `cvd_*.recent_delta_5m` 是最近 6 个 5m 的净 delta（USD），**不是累计值**。
8. `spot_contract_coherence`：`spot_leads` 表示现货先动、合约跟随；`spot_lags` 反之。

━━━━━━━━━━ 输出格式（严格遵守） ━━━━━━━━━━

只返回一个 JSON 代码块，外面用 ```json ... ``` 包裹，不要任何其它解释文字。
Schema：

```json
{
  "market_conclusion": "2-3 句中文总结（不超过 150 字，首句必须是方向性结论）",
  "scenario": "<9 选 1>",
  "market_phase": "<5 选 1>",
  "evidence_breakdown": [
    {
      "dimension": "CVD 期现|OI|Funding|Liquidation|Basis|Orderbook|LiqMap|LiqSweep|Footprint|Taker|Options|PriceContext",
      "observation": "中文描述，必须引用 facts 里的具体数值",
      "weight": "high|medium|low"
    }
  ],
  "trading_implications": {
    "bias": "long|short|neutral|wait",
    "entry_zone": [low, high] 或 null,
    "stop_loss_beyond": 价格数值 或 null,
    "take_profit_targets": [价格1, 价格2],
    "notes": "简短补充，可为空字符串"
  },
  "invalidation_conditions": [
    "至少 2 条可测量条件，例如：price < 77400 持续 15m；或 OI 1h 变化 > +2%"
  ],
  "confidence": 0-100 的整数,
  "data_quality": "ok|partial|insufficient"
}
```

返回纯 JSON，禁止任何额外文字、标题、emoji。"""


# ────────────────────────────────────────────────────────────────────────────
# User Prompt · 把 facts 渲染成带章节锚点的 markdown
# ────────────────────────────────────────────────────────────────────────────

_SCENARIO_CN = {
    "trend_continuation_up": "上涨趋势延续",
    "trend_continuation_down": "下跌趋势延续",
    "short_squeeze_up": "空头挤压上行",
    "long_squeeze_down": "多头挤压下行",
    "fake_breakout_up": "假突破上行",
    "fake_breakdown_down": "假跌破下行",
    "exhaustion_top": "顶部衰竭",
    "exhaustion_bottom": "底部衰竭",
    "range_bound": "区间震荡",
}


def _fmt(v: Any, unit: str = "", nd: int = 2, default: str = "—") -> str:
    """格式化数值，None / NaN → 默认占位符。"""
    if v is None:
        return default
    try:
        fv = float(v)
        if fv != fv:  # NaN
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
    """格式化单条 Footprint 失衡价位。"""
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
    lines.append(
        f"- 近 6×5m delta：{cvd_c.get('recent_delta_5m')}"
    )

    lines.append(f"\n### CVD 现 · 现货")
    lines.append(
        f"- 1h delta：${_fmt(cvd_s.get('delta_1h'))} "
        f"| 趋势：`{cvd_s.get('trend_1h', '—')}` "
        f"| 背离：{cvd_s.get('has_divergence')}"
    )
    lines.append(
        f"- 近 6×5m delta：{cvd_s.get('recent_delta_5m')}"
    )

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
    _header("§6", "你的任务")
    lines.append(
        "请严格按 system prompt 里的 JSON Schema 输出**单一 json 代码块**，"
        "不要附加任何解释、标题或 markdown 前后缀。"
    )
    lines.append(
        "提醒：每条 evidence 必须引用 §2-§4 里的具体数值；"
        "invalidation_conditions 必须是可测量的价格/指标触发条件。"
    )
    lines.append(
        f"如果数据质量是 `insufficient`，`bias` 必须是 `wait` 或 `neutral`，禁止 `long/short`。"
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
    # ```json ... ```
    m = re.search(r"```(?:json|JSON)?\s*\n(.*?)\n```", raw, re.DOTALL)
    if m:
        block = m.group(1).strip()
    else:
        # 兜底：取第一个 { 到最后一个 } 之间的内容
        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("no json object found")
        block = raw[start:end + 1]

    # 容错：去除 // 行注释和尾随逗号
    block = re.sub(r"(^|\s)//[^\n]*", "", block)
    block = re.sub(r",(\s*[}\]])", r"\1", block)
    payload = json.loads(block)
    if not isinstance(payload, dict):
        raise ValueError(f"json root is not dict: {type(payload)}")
    return payload
