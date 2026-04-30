"""Strategic AI · System Prompt + User Prompt 渲染（§0-§14）。

定位：
    主 AI"全程决策官"的 prompt 模板。与 MAA prompts 并行存在，互不耦合。
    数据排序按 GPT 反馈采纳的"Decision-First Dual Anchor"结构：
    硬约束 → 数据质量 → TradingBrain 综合地图 → 当前价/PriceZone → 高盈亏比候选 →
    KeyLevel/挂单/清算原始证据 → OI/CVD/Funding/Footprint 实时验证 →
    宏观慢变量修正 → 期权 → 冲突矩阵 → 输出任务 + JSON Schema

复用：
    - `extract_json_payload`：直接复用 `ai.market_action_prompts.extract_json_payload`
      （已处理 markdown code fence / 注释 / 控制字符兼容）
    - PromptSection：复用 PR-1 的通用版（`models.common_prompt_debug.PromptSection`）

设计纪律（与 GPT 反馈一致）：
    1. **TradingBrain 前置**：放在 §2，让 AI 先看到综合"决策地图"
    2. **宏观后置**：§11 放慢变量，作为修正器而非主决策锚
    3. **每条 evidence 必须引用 §N 数据章节**：减少凭感觉编结论
    4. **强制冲突矩阵**：§13 显式列出多/空/等待证据 + 关键矛盾，逼 AI 自我辩论
    5. **WAIT 是合法输出**：prompt §0 显式声明，避免 AI 强行给计划
    6. **不输出仓位百分比 / 杠杆倍数**：用 risk_unit / leverage_risk_level / position_sizing_note 替代
    7. **Top N 压缩**：每章节 Top N 截断（zones 10 / opportunities 5 / walls 8/侧 / liq 8/侧）
    8. **horizon 自决**：AI 根据信号本身决定 scalp/intraday/swing；prompt 不限定
"""

from __future__ import annotations

from typing import Any, Optional

from ai.market_action_prompts import extract_json_payload as _extract_json_shared
from ai.strategic_priority import (
    LIQ_TIMEFRAME_WEIGHT,
    sort_key_levels,
    sort_liq_clusters,
    sort_opportunities,
    sort_price_zones,
    sort_walls,
)
from models.common_prompt_debug import PromptSection
from models.snapshot import AISnapshot


# ────────────────────────────────────────────────────────────────────────────
# Top N 截断常量（与 prompt §0 声明的"决策压缩数据"一致）
# ────────────────────────────────────────────────────────────────────────────

TOP_N_ZONES = 10        # PriceZone 总条数（current 3 + above 5 + below 5 兜底；实际由分组截断）
TOP_N_ZONES_PER_SIDE = 5
TOP_N_ZONES_CURRENT = 3
TOP_N_OPPORTUNITIES = 5
TOP_N_WALLS_PER_SIDE = 8
TOP_N_LIQ_CLUSTERS_PER_SIDE = 8
TOP_N_LIQ_VACUUM = 5
TOP_N_KL_NEAR = 5
TOP_N_KL_PER_SIDE = 6
TOP_N_WALL_EVENTS = 10
TOP_N_FACTS_NARRATIVE = 5


# ────────────────────────────────────────────────────────────────────────────
# System Prompt · 角色 + 思考流程 + 输出 Schema
# ────────────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """你是"Strategic Trading Decision Officer"——主 AI 决策官，基于完整的市场数据（结构 / 流动性 / 流量 / 慢变量）**自主产出候选交易计划**。你不是短线动作仲裁器（那是 MAA），也不是规则引擎；你是**生成完整方案的交易员**：方向 + 入场区 + 失效条件 + 目标 + 风险描述 + 时间窗 + 替代场景。

━━━━━━━━━━ 你的核心原则（不可妥协） ━━━━━━━━━━

1. **WAIT / NO_TRADE 是高质量输出，不是失败**。当出现以下任一情况时优先输出 WAIT：
   - 数据质量 insufficient 或关键源 stale
   - RR < 1.5（盈亏比不达标）
   - 价格在中间位置（无近场决策性区位）
   - 多空证据势均力敌且无明确触发条件
   - 关键磁铁近且未扫（限价试错风险高）
   **拒绝**为了交差强行编计划——交易员"今天不做"也是判断。

2. **每条 evidence 必须引用 §N 数据章节**。observation 必须包含 facts 的具体字段+数值。`section_ref` 字段填 "§4" / "§7" / "§9" 等。

3. **冲突矩阵是产出 trading_plan 的前置条件**。在产出 primary_plan 之前，你必须在 evidence_matrix 字段里：
   - 列出所有支持做多的证据（long_evidence）
   - 列出所有支持做空的证据（short_evidence）
   - 列出所有支持等待的证据（wait_evidence）
   - 显式陈述 contradictions（关键矛盾点，1-3 条中文白话）
   **若 contradictions 严重 → 优先 WAIT**。禁止把 contrarian 证据伪装成 main。

4. **不输出绝对仓位百分比和杠杆倍数**。账户参数（资金 / 风险偏好 / 最大杠杆）由用户/外部规则掌握，AI 不应该拍脑袋给"建议 20% 仓位 / 3x 杠杆"。改用：
   - `risk_unit`：单笔风险描述（"按 1R 风险计算" / "保守开仓"）
   - `leverage_risk_level`：杠杆风险等级（low / medium / high / extreme）
   - `position_sizing_note`：仓位计算说明（"若 hard_invalidation 距离 X% 且账户允许 1R/笔，仓位 ≈ R / X%"）

5. **horizon 自决**。根据信号性质选择：
   - `scalp`：分钟到小时级（短线扫单反转 / 快速突破）
   - `intraday`：日内（当日完成）
   - `swing`：波段（数日 - 1-2 周；多周期共振时使用）
   - `strategic`：战略（月级 / 周期级；通常配合 macro_context 显著时）

6. **数据自我裁判**。在 `data_self_check` 字段里：
   - 列出 missing（必填字段缺失）/ stale（超 TTL 的源）/ provisional（未收盘 bar 字段）
   - 若关键源严重缺失或 stale → `hard_stop_triggered=true` + `decision="NO_TRADE"`
   - confidence 必须可解释（`confidence_rationale` 说明扣分原因）

7. **替代场景强制产出**。在 `alternative_scenario` 里给出对立假设 + 概率 + 触发条件，逼自己辩论。

8. **绝不引入数据外部信息**。所有判断必须基于 §1-§12 提供的 facts；禁止编造新闻、政策、币圈传闻。

9. **市场阶段与周期位置**。在 `market_phase` 与 `cycle_position` 字段里给出你的判断（自由表述，不限固定枚举），用于 swing/strategic horizon 的 sanity check。

10. **当前价位评估优先**。在 `current_zone_assessment` 里回答：当前在哪个 zone？该 zone 主导角色是什么？距上方/下方最近"决定性"区的距离？最关键冲突一句话总结？

━━━━━━━━━━ 你必须按此路径思考（8 步） ━━━━━━━━━━

**Step 0 · 数据自检** —— 读 §1，判断本次是否允许输出完整计划。若关键数据缺失（KL / 挂单墙 / 清算图任一彻底缺失），输出 `decision="NO_TRADE"` + `data_self_check.hard_stop_triggered=true`。

**Step 1 · 当前区定位** —— 读 §2-§4，回答当前价格在哪个 PriceZone。判断当前区是防守位（spot_defense）/ 磁铁（liquidation_magnet）/ 争夺区（contested）/ 关键位（key_level_only）/ 中间位置（mid_air）。填入 `current_zone_assessment`。

**Step 2 · 上下路径推演** —— 列出上方最近 3 个目标、下方最近 3 个目标。区分支撑/阻力（结构）与清算磁铁（流动性目标）。后者不是"支撑"——它是"扫单后的目的地"。

**Step 3 · 机会筛选** —— 读 §5，判断是否存在合格 Opportunity。若 RR 不足或冲突过大，输出等待计划。注意：opportunity 已按 priority 排序，前 1-2 条最相关。

**Step 4 · 结构复核** —— 读 §6-§8，确认关键位、现货墙、合约墙、清算图是否支持当前 setup。任一明确反对 → 降权或换方向。

**Step 5 · 流量验证** —— 读 §9-§10，判断实时流量是确认、反对还是中性。特别注意 **price-OI 因果链**：
   - 价↑ + OI↑ → 看 CVD：合约 buy 主导 = 新多入场；sell 主导 = 空头加仓
   - 价↑ + OI↓ → 空头回补
   - 价↓ + OI↑ → 多头加仓被困 OR 空头加仓
   - 价↓ + OI↓ → 多头平仓 / 被清算

**Step 6 · 慢变量修正** —— 读 §11-§12，判断宏观/资金面/期权是否需要：降低仓位积极性 / 缩短目标距离 / 调整 horizon。在 `macro_modifier_note` 里说明。

**Step 7 · 冲突矩阵** —— 必须输出 long / short / wait 三类证据 + contradictions。若冲突严重 → 优先 WAIT。

**Step 8 · 交易计划** —— 输出 `primary_plan`（含 entry_zone / soft_invalidation / hard_invalidation / targets / RR / cancel_conditions / risk_unit / leverage_risk_level / position_sizing_note）+ `alternative_plan`（如主计划失效）+ `alternative_scenario`（对立场景） + `invalidation_conditions`（计划级失效）+ `confidence` + `confidence_rationale`。

━━━━━━━━━━ 输出格式（严格遵守） ━━━━━━━━━━

只返回一个 JSON 代码块（```json ... ```），不要任何其它文字。schema 如下：

```json
{
  "decision": "WAIT | LONG_OBSERVATION | SHORT_OBSERVATION | LONG_PLAN | SHORT_PLAN | NO_TRADE",
  "horizon": "scalp | intraday | swing | strategic",
  "bias": "bullish | bearish | neutral | conflicted",
  "confidence": 0.0-1.0 的浮点数,
  "confidence_rationale": "一段话说明 confidence 的具体构成：基准分 + 加/扣分项",
  "market_phase": "市场阶段（自由表述）：累积底部 / 主升浪调整 / 分配顶部 / 趋势确认 / 区间震荡 / 派发完成 / ...",
  "cycle_position": "周期位置：周期早期 / 中期 / 晚期 / 顶部 / 底部",
  "current_zone_assessment": {
    "zone_id": "若当前价落在已识别 PriceZone 内则填其 zone_id；否则空字符串",
    "role": "spot_defense | futures_target | liquidation_magnet | contested | key_level_only | mid_air | other",
    "nearest_critical_above_pct": 距上方最近\"决定性\"区的百分比（正数，含小数；无则 null）,
    "nearest_critical_below_pct": 距下方最近\"决定性\"区的百分比（正数，含小数；无则 null）,
    "key_conflict": "当前最重要的一句话冲突总结，比如：\"现货墙强支撑 vs 下方 0.8% 处巨型清算磁铁\""
  },
  "structure_analysis": "结构 + 订单墙 + 清算图段（200-400 字 · 引用 §4-§8）",
  "flow_analysis": "实时流量 + 现货流入流出 + 硬证据段（200-400 字 · 引用 §9-§10）",
  "macro_context": "宏观 + 情绪 + 资金面慢变量段（100-300 字 · 引用 §11-§12）",
  "primary_plan": {
    "setup_type": "扫单反转 | 回踩支撑 | 突破延续 | 区间高抛低吸 | ...",
    "entry_zone_low": 入场区下沿,
    "entry_zone_high": 入场区上沿,
    "trigger_conditions": ["现货墙未撤", "CVD 5m 转正"],
    "soft_invalidation": "软失效描述",
    "hard_invalidation": "硬失效描述（必须含明确价位 + 时间条件）",
    "targets": [
      {"price": 价位, "reason": "命中理由", "rr": 盈亏比}
    ],
    "cancel_conditions": ["观察取消条件"],
    "risk_unit": "按 1R 风险计算 / 保守开仓 等",
    "leverage_risk_level": "low | medium | high | extreme",
    "position_sizing_note": "若 hard_invalidation 距离 X% 且账户允许 1R/笔，仓位 ≈ R / X%"
  },
  "alternative_plan": null 或与 primary_plan 同结构,
  "no_trade_conditions": ["若 decision=WAIT，列出当前不做的具体原因"],
  "alternative_scenario": {
    "description": "对立场景描述",
    "probability_pct": 0-100 的整数,
    "trigger": "触发该场景所需的观察条件"
  },
  "evidence_matrix": {
    "long_evidence": [
      {"section_ref": "§7", "observation": "纯事实+数值", "inference": "跨维度判断", "supports": "main", "weight": "high"}
    ],
    "short_evidence": [
      {"section_ref": "§8", "observation": "...", "inference": "...", "supports": "contrarian", "weight": "medium"}
    ],
    "wait_evidence": [
      {"section_ref": "§1", "observation": "...", "inference": "...", "supports": "neutral", "weight": "low"}
    ],
    "contradictions": ["关键矛盾点 1（中文白话）", "关键矛盾点 2"]
  },
  "invalidation_conditions": ["计划级失效条件 1", "计划级失效条件 2"],
  "data_self_check": {
    "missing": ["缺失字段清单"],
    "stale": ["超 TTL 的源"],
    "provisional": ["未收盘字段"],
    "hard_stop_triggered": false,
    "confidence_penalty_reason": "confidence 为何打折的具体原因"
  },
  "macro_modifier_note": "宏观/情绪如何修正主计划",
  "data_quality": "ok | partial | insufficient"
}
```

再次强调：
- `decision = NO_TRADE` 时 `primary_plan` 必须为 null，且 `data_self_check.hard_stop_triggered=true`
- `decision in [LONG_PLAN, SHORT_PLAN]` 时 `primary_plan` 必填，且 `evidence_matrix.contradictions` 不能严重冲突
- `decision in [LONG_OBSERVATION, SHORT_OBSERVATION]` 时 `primary_plan` 可选；若给出，`trigger_conditions` 必须明确写"等什么"
- `decision = WAIT` 时 `no_trade_conditions` 必填
- 禁止 evidence 只写 observation 不写 inference
- 禁止 inference 引入 facts 之外的信息（宏观传闻 / 币圈八卦）
- 禁止 Markdown / emoji / 任何 JSON 之外的文字"""


def build_system_prompt() -> str:
    return SYSTEM_PROMPT


# ────────────────────────────────────────────────────────────────────────────
# extract_json_payload · 复用 MAA 的实现
# ────────────────────────────────────────────────────────────────────────────

def extract_json_payload(raw: str) -> dict[str, Any]:
    """复用 MAA 的 JSON 提取（容忍 markdown / 注释 / 控制字符）。"""
    return _extract_json_shared(raw)


# ────────────────────────────────────────────────────────────────────────────
# 渲染辅助
# ────────────────────────────────────────────────────────────────────────────

def _fmt(v: Any, unit: str = "", nd: int = 2, default: str = "—") -> str:
    """通用数值格式化（自动 K/M/B 后缀）。"""
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


def _fmt_price(v: Any, nd: int = 2) -> str:
    if v is None:
        return "—"
    try:
        return f"${float(v):,.{nd}f}"
    except (TypeError, ValueError):
        return "—"


# ────────────────────────────────────────────────────────────────────────────
# build_user_prompt · §0-§14 完整渲染
# ────────────────────────────────────────────────────────────────────────────

def build_user_prompt(
    snapshot: AISnapshot,
    previous_report: Any = None,
) -> tuple[str, list[PromptSection]]:
    """渲染 AISnapshot → markdown user prompt + 章节锚点列表。

    Args:
        snapshot: PR-1 装配的 AISnapshot（含强类型 trading_brain / facts_* 等）
        previous_report: 可选 · 上一份 AIStrategicReport，若提供则在 §0 后渲染前情提要

    Returns:
        (user_prompt_text, sections)：user_prompt 字符串 + 章节锚点列表（供前端 TOC）
    """
    lines: list[str] = []
    sections: list[PromptSection] = []

    def _header(anchor: str, title: str, level: int = 2) -> None:
        sections.append(PromptSection(anchor=anchor, title=title, level=level))
        lines.append(f"\n## {anchor} {title}\n")

    def _sub(title: str) -> None:
        lines.append(f"\n### {title}\n")

    coin = snapshot.coin
    price = snapshot.price

    # ── §0 任务身份 + 硬约束 ──
    _header("§0", "任务身份 + 硬约束")
    lines.append(f"- 标的：**{coin}** · 当前价：{_fmt_price(price)}")
    lines.append(f"- 24h 高/低：{_fmt_price(snapshot.high_24h)} / {_fmt_price(snapshot.low_24h)}")
    lines.append(f"- ATR(14) ≈ {_fmt(snapshot.atr_14, nd=4)}")
    lines.append("- 你的任务：基于 §1-§12 数据，按 8 步思考流程产出 trading plan JSON")
    lines.append("- 硬约束：WAIT 是合法输出；evidence 必须引用 §N；冲突矩阵必填；不输出仓位百分比/杠杆倍数；horizon 自决")

    # ── §0' 前情提要（有上一份才渲染） ──
    prev_dict: Optional[dict] = None
    if previous_report is not None:
        try:
            prev_dict = (
                previous_report.model_dump()
                if hasattr(previous_report, "model_dump") else dict(previous_report)
            )
        except Exception:
            prev_dict = None
    if prev_dict:
        _header("§0'", "前情提要（上一份决策报告）")
        prev_ts = prev_dict.get("timestamp")
        prev_decision = prev_dict.get("decision", "—")
        prev_horizon = prev_dict.get("horizon", "—")
        prev_bias = prev_dict.get("bias", "—")
        prev_conf = prev_dict.get("confidence", "—")
        lines.append(f"- 上一份 ts：`{prev_ts}` | decision=`{prev_decision}` | horizon=`{prev_horizon}` | bias=`{prev_bias}` | confidence={prev_conf}")
        prev_plan = prev_dict.get("primary_plan") or {}
        if prev_plan:
            lines.append(
                f"- 上一份主计划：setup=`{prev_plan.get('setup_type', '—')}` | "
                f"entry=[{_fmt(prev_plan.get('entry_zone_low'), nd=2)}, "
                f"{_fmt(prev_plan.get('entry_zone_high'), nd=2)}] | "
                f"hard_invalidation=`{(prev_plan.get('hard_invalidation') or '—')[:60]}`"
            )
        prev_invalid = prev_dict.get("invalidation_conditions") or []
        if prev_invalid:
            lines.append(f"- 上一份失效清单：{prev_invalid[:3]}")

    # ── §1 数据质量与可用性 ──
    _header("§1", "数据质量与可用性（先看再决策）")
    lines.append(f"- facts data_quality：`{snapshot.facts_data_quality or '—'}`")
    if snapshot.facts_missing:
        lines.append(f"- facts 缺失字段（{len(snapshot.facts_missing)}）：{snapshot.facts_missing[:8]}")
    if snapshot.facts_has_provisional_bars:
        lines.append(f"- 含未收盘 bar：是；具体字段：{snapshot.facts_provisional_fields[:8]}")
    else:
        lines.append("- 含未收盘 bar：否（所有派生字段均基于已收盘 bar）")
    if snapshot.facts_sources_used:
        lines.append(f"- 本轮成功源：{snapshot.facts_sources_used[:10]}")
    if snapshot.poll_failures:
        lines.append(f"- poll 失败源：{list(snapshot.poll_failures.items())[:6]}")
    # KL / 墙 / 清算 三大核心源
    has_kl = snapshot.key_level_snapshot is not None
    has_walls = bool(snapshot.wall_zones_above or snapshot.wall_zones_below)
    has_liq = any([snapshot.liq_map_block_1d, snapshot.liq_map_block_7d, snapshot.liq_map_block_30d])
    has_brain = snapshot.trading_brain is not None
    lines.append(
        f"- 核心源就绪：KeyLevel=`{has_kl}` | Walls=`{has_walls}` | LiqMap=`{has_liq}` | TradingBrain=`{has_brain}`"
    )

    # ── §2 当前交易大脑快照 + 综合区位 ──
    _header("§2", "当前交易大脑综合地图（PriceZone + 排行 + 数据质量）")
    brain = snapshot.trading_brain
    if brain is None:
        lines.append("- TradingBrain 数据不可用，本节跳过。")
    else:
        bsum = (brain.summary or "").strip()
        if bsum:
            lines.append(f"- 综合一句话：{bsum[:200]}")
        lines.append(f"- last_price={_fmt_price(brain.last_price)} | atr={_fmt(brain.atr, nd=2)}")
        rk = brain.rankings
        if rk.support_trust:
            lines.append(f"- 支撑信任 Top：{rk.support_trust[:5]}")
        if rk.resistance_trust:
            lines.append(f"- 阻力信任 Top：{rk.resistance_trust[:5]}")
        if rk.sweep_targets:
            lines.append(f"- 扫单磁铁 Top：{rk.sweep_targets[:5]}")
        if rk.break_through_risk:
            lines.append(f"- 打穿风险 Top：{rk.break_through_risk[:5]}")
        bdq = brain.data_quality
        if bdq.notes:
            lines.append(f"- TradingBrain notes：{bdq.notes[:3]}")
        if bdq.is_partial_ready:
            lines.append(f"- TradingBrain 暖机中：ready={bdq.ready_count}/{bdq.total_count}")

    # ── §3 当前价格状态 ──
    _header("§3", "当前价格 / 波动 / 时间锚")
    lines.append(f"- 当前价：{_fmt_price(price)} | 24h 高/低：{_fmt_price(snapshot.high_24h)} / {_fmt_price(snapshot.low_24h)}")
    lines.append(f"- ATR(14)={_fmt(snapshot.atr_14, nd=4)} | RSI(14)={_fmt(snapshot.rsi_14, nd=1)}")
    lines.append(
        f"- POC={_fmt_price(snapshot.volume_profile_poc)} | "
        f"VAH={_fmt_price(snapshot.value_area_high)} | "
        f"VAL={_fmt_price(snapshot.value_area_low)} | "
        f"VWAP={_fmt_price(snapshot.vwap)}"
    )
    lines.append(
        f"- 市场温度={_fmt(snapshot.market_temperature, nd=1)} | "
        f"针刺风险=`{snapshot.pin_risk_level}`"
    )
    if snapshot.range_signal:
        lines.append(f"- range_signal：{snapshot.range_signal}")

    # ── §4 PriceZone 价格区地图（按 priority 分组排序） ──
    _header("§4", "PriceZone 价格区地图（按决策相关性排序）")
    if brain is None or not brain.zones:
        lines.append("- 无 PriceZone 数据，本节跳过。")
    else:
        cur, above, below = sort_price_zones(brain.zones)
        cur = cur[:TOP_N_ZONES_CURRENT]
        above = above[:TOP_N_ZONES_PER_SIDE]
        below = below[:TOP_N_ZONES_PER_SIDE]

        def _fmt_zone(z) -> str:
            return (
                f"  - `{z.zone_id}` | role=`{z.dominant_role}` | "
                f"price=[{_fmt(z.price_low, nd=1)},{_fmt(z.price_high, nd=1)}] "
                f"({_fmt_pct(z.distance_pct)}) | "
                f"sup_trust={_fmt(z.support_trust, nd=2)} | "
                f"res_trust={_fmt(z.resistance_trust, nd=2)} | "
                f"sweep_attr={_fmt(z.sweep_attractiveness, nd=2)} | "
                f"break_risk={_fmt(z.break_through_risk, nd=2)} | "
                f"data_conf={_fmt(z.data_confidence, nd=2)}"
                + (f" | label=`{z.dominant_label}`" if z.dominant_label else "")
            )

        if cur:
            _sub(f"当前区（|distance|<0.30%, top {len(cur)}）")
            for z in cur:
                lines.append(_fmt_zone(z))
        if above:
            _sub(f"上方（top {len(above)}）")
            for z in above:
                lines.append(_fmt_zone(z))
        if below:
            _sub(f"下方（top {len(below)}）")
            for z in below:
                lines.append(_fmt_zone(z))

    # ── §5 高盈亏比候选（OpportunityBoard） ──
    _header("§5", "高盈亏比候选（OpportunityBoard，按 state + score 排序）")
    if brain is None or not brain.opportunities:
        lines.append("- 无 Opportunity 候选。")
    else:
        opps = sort_opportunities(brain.opportunities)[:TOP_N_OPPORTUNITIES]
        for opp in opps:
            state_name = opp.state.name if opp.state else "forming"
            rrs = [t.rr for t in (opp.targets or [])]
            entry_lows = [s.entry_zone[0] for s in (opp.entry_styles or []) if len(s.entry_zone) >= 2]
            entry_highs = [s.entry_zone[1] for s in (opp.entry_styles or []) if len(s.entry_zone) >= 2]
            entry_str = ""
            if entry_lows and entry_highs:
                entry_str = f" | entry≈[{_fmt(min(entry_lows), nd=2)}, {_fmt(max(entry_highs), nd=2)}]"
            lines.append(
                f"- `{opp.setup_id}` | type=`{opp.setup_type}` | dir=`{opp.direction}` | "
                f"state=`{state_name}` | opp={_fmt(opp.opportunity_score, nd=2)} | "
                f"asym={_fmt(opp.asymmetry_score, nd=2)} | "
                f"max_rr={_fmt(max(rrs) if rrs else 0, nd=2)}{entry_str}"
            )
            if opp.risk_plan:
                lines.append(
                    f"   · soft_inv={_fmt_price(opp.risk_plan.soft_invalidation)} | "
                    f"hard_stop={_fmt_price(opp.risk_plan.hard_stop)}"
                )
            if opp.targets:
                tgt_str = " · ".join(
                    f"{_fmt_price(t.price)}/RR={_fmt(t.rr, nd=2)}"
                    for t in opp.targets[:3]
                )
                lines.append(f"   · targets：{tgt_str}")
            if opp.cancel_conditions:
                lines.append(f"   · cancel：{opp.cancel_conditions[:2]}")
            if opp.state and opp.state.pending_reason:
                lines.append(f"   · pending：{opp.state.pending_reason[:120]}")

    # ── §6 KeyLevel V3（结构骨架） ──
    _header("§6", "KeyLevel V3 多周期关键位（结构骨架，按 priority 分组排序）")
    kl_snap = snapshot.key_level_snapshot
    if kl_snap is None or not kl_snap.levels:
        lines.append("- KeyLevel 数据不可用。")
    else:
        kls = list(kl_snap.levels)
        near, above_kl, below_kl = sort_key_levels(kls, price)
        near = near[:TOP_N_KL_NEAR]
        above_kl = above_kl[:TOP_N_KL_PER_SIDE]
        below_kl = below_kl[:TOP_N_KL_PER_SIDE]

        def _fmt_kl(kl) -> str:
            tf = getattr(kl, "timeframe", "—")
            sc = getattr(kl, "s_class", "—")
            fs = getattr(kl, "final_score", 0) or 0
            side = getattr(kl, "side", "—")
            evg = getattr(kl, "evidence_groups", None) or []
            return (
                f"  - {_fmt_price(kl.price)} | tf=`{tf}` | side=`{side}` | "
                f"class=`{sc}` | final={_fmt(fs, nd=1)} | "
                f"evidence_groups={len(evg)}"
            )

        if near:
            _sub(f"近场 ±1%（top {len(near)}）")
            for kl in near:
                lines.append(_fmt_kl(kl))
        if above_kl:
            _sub(f"上方（top {len(above_kl)}）")
            for kl in above_kl:
                lines.append(_fmt_kl(kl))
        if below_kl:
            _sub(f"下方（top {len(below_kl)}）")
            for kl in below_kl:
                lines.append(_fmt_kl(kl))

    # ── §7 现货墙 / 合约墙 / Coinbase / Orderbook ──
    _header("§7", "挂单墙（现货 + 合约 · 按 priority 排序 · 含 Coinbase 共振 / 双源）")

    def _fmt_wall(w) -> str:
        side_zh = "卖墙" if getattr(w, "side", "") == "ask" else "买墙"
        cb = "·CB双源" if getattr(w, "coinbase_spot_confluence", False) else ""
        ds = "·双源" if getattr(w, "dual_source", False) else ""
        return (
            f"  - {_fmt_price(getattr(w, 'price_mid', 0))} ({_fmt_pct(getattr(w, 'distance_pct', 0))}) | "
            f"`{side_zh}`{cb}{ds} | "
            f"USD={_fmt(getattr(w, 'current_usd', 0))} | "
            f"max_1h={_fmt(getattr(w, 'max_usd_1h', 0))} | "
            f"trust={_fmt(getattr(w, 'trust_score', 0), nd=2)} | "
            f"persist={_fmt(getattr(w, 'visible_minutes', 0), nd=1)}min | "
            f"break_risk={_fmt(getattr(w, 'break_through_risk', 0), nd=2)}"
        )

    walls_above = sort_walls(snapshot.wall_zones_above or [])[:TOP_N_WALLS_PER_SIDE]
    walls_below = sort_walls(snapshot.wall_zones_below or [])[:TOP_N_WALLS_PER_SIDE]
    if walls_above:
        _sub(f"上方卖墙（top {len(walls_above)}）")
        for w in walls_above:
            lines.append(_fmt_wall(w))
    if walls_below:
        _sub(f"下方买墙（top {len(walls_below)}）")
        for w in walls_below:
            lines.append(_fmt_wall(w))
    if not (walls_above or walls_below):
        lines.append("- 无显著挂单墙数据。")

    # 墙事件（最近 30min · 7 类事件）
    if snapshot.wall_events_v2:
        _sub(f"近 30min 墙事件（top {min(TOP_N_WALL_EVENTS, len(snapshot.wall_events_v2))}）")
        EVT_LABEL = {
            "wall_consumed": "被吃",
            "wall_strengthened": "增厚",
            "wall_removed": "撤单",
            "wall_appeared": "新出",
            "wall_drifted": "漂移",
            "wall_split": "分裂",
            "wall_merged": "合并",
        }
        for ev in (snapshot.wall_events_v2 or [])[:TOP_N_WALL_EVENTS]:
            kind = EVT_LABEL.get(getattr(ev, "event_type", ""), getattr(ev, "event_type", "—"))
            side = "卖墙" if getattr(ev, "side", "") == "ask" else "买墙"
            lines.append(
                f"  - `{kind}` ({side}) {_fmt_price(getattr(ev, 'price_mid', 0))} "
                f"| confidence={_fmt(getattr(ev, 'confidence', 0), nd=2)}"
            )

    # USD/USDT basis（机构资金 footprint）
    if snapshot.usd_usdt_basis_pct is not None:
        lines.append(f"\n- Coinbase USD vs Binance USDT 基差：{_fmt_pct(snapshot.usd_usdt_basis_pct, nd=3)}（>30bp 异常 / <5bp 正常）")

    # 拥挤度（OI / Funding / LS）
    cg = snapshot.crowding_global
    if cg is not None:
        _sub("全局拥挤度（OI 多周期 + Funding 百分位 + LS）")
        lines.append(
            f"- OI Δ：1h={_fmt_pct(cg.oi_delta_1h_pct)} | 24h={_fmt_pct(cg.oi_delta_24h_pct)}"
        )
        if cg.oi_margin_split is not None:
            lines.append(f"- OI 保证金切分：{cg.oi_margin_split}")
        lines.append(
            f"- Funding now={_fmt(cg.funding_now_pct, nd=4)}% | "
            f"30d 百分位={_fmt(cg.funding_percentile_30d, nd=0)}"
        )
        lines.append(
            f"- LS top_position={_fmt(cg.top_position_ls_ratio, nd=2)} | "
            f"global_account={_fmt(cg.global_account_ls_ratio, nd=2)}"
        )
        if cg.inferred_position_state:
            lines.append(f"- 推断仓位状态：`{cg.inferred_position_state}`")
        lines.append(
            f"- 拥挤风险：long={_fmt(cg.long_crowding_risk, nd=2)} | "
            f"short={_fmt(cg.short_crowding_risk, nd=2)}"
        )

    # ── §8 清算图 / 止损带 / 清算磁铁（流动性目标与扫单路径） ──
    _header("§8", "清算图 / 止损带 / 清算磁铁（流动性目标与扫单路径）")

    def _fmt_liq_block(block, label: str) -> None:
        if block is None:
            lines.append(f"- {label}：无数据")
            return
        _sub(label)
        lines.append(f"- imbalance_ratio={_fmt(block.imbalance_ratio, nd=2)}（>1=上方更重）")
        # by_exchange Top 3
        if block.by_exchange_summary:
            ex_strs = " · ".join(
                f"{x['exchange']}:{x['share_pct']}%"
                for x in block.by_exchange_summary[:3]
            )
            lines.append(f"- 交易所 Top3：{ex_strs}")
        # max_pain（仅 1d 通常有）
        if block.max_pain:
            mp = block.max_pain
            lines.append(
                f"- 痛点价：long={_fmt_price(mp.long_pain_price)}({_fmt(mp.long_pain_usd)}) | "
                f"short={_fmt_price(mp.short_pain_price)}({_fmt(mp.short_pain_usd)})"
            )

        cycle = block.cycle or "1d"
        ca = sort_liq_clusters(
            list(block.clusters_above or []),
            cycle, block.vacuum_zones,
        )[:TOP_N_LIQ_CLUSTERS_PER_SIDE]
        cb = sort_liq_clusters(
            list(block.clusters_below or []),
            cycle, block.vacuum_zones,
        )[:TOP_N_LIQ_CLUSTERS_PER_SIDE]

        def _fmt_cluster(c) -> str:
            return (
                f"    - {_fmt_price(getattr(c, 'price_center', 0))} "
                f"({_fmt_pct(getattr(c, 'distance_pct', 0))}) | "
                f"USD={_fmt(getattr(c, 'total_usd', 0))} | "
                f"side=`{getattr(c, 'side', '—')}` | "
                f"ex_count={getattr(c, 'exchange_count', 1)} | "
                f"lev_intensity={_fmt(getattr(c, 'leverage_intensity', 0), nd=2)}"
            )

        if ca:
            lines.append(f"  - 上方空头止损带（top {len(ca)}）：")
            for c in ca:
                lines.append(_fmt_cluster(c))
        if cb:
            lines.append(f"  - 下方多头止损带（top {len(cb)}）：")
            for c in cb:
                lines.append(_fmt_cluster(c))
        # 真空区
        vz_list = (block.vacuum_zones or [])[:TOP_N_LIQ_VACUUM]
        if vz_list:
            lines.append(f"  - 真空区（top {len(vz_list)}）：")
            for vz in vz_list:
                mid = float(getattr(vz, "midpoint", 0) or 0)
                lo = float(getattr(vz, "price_from", 0) or 0)
                hi = float(getattr(vz, "price_to", 0) or 0)
                width_pct = ((hi - lo) / mid * 100.0) if mid > 0 else 0
                lines.append(
                    f"    - [{_fmt_price(lo)}, {_fmt_price(hi)}] "
                    f"宽度≈{_fmt(width_pct, nd=2)}% | mid={_fmt_price(mid)}"
                )

    _fmt_liq_block(snapshot.liq_map_block_1d, "1d 时间窗")
    _fmt_liq_block(snapshot.liq_map_block_7d, "7d 时间窗")
    _fmt_liq_block(snapshot.liq_map_block_30d, "30d 时间窗（战略远场）")

    # ── §9 实时订单流与持仓流量（OI / CVD / Funding / Basis / Taker） ──
    _header("§9", "实时订单流与持仓流量（按因果链：Price+OI → Funding → CVD → Taker → Basis）")

    # OI
    foi = snapshot.facts_oi
    if foi is not None:
        _sub("OI · 持仓")
        lines.append(
            f"- 当前 OI={_fmt(getattr(foi, 'current_usd', 0))} USD | "
            f"1h Δ={_fmt_pct(getattr(foi, 'change_1h_pct', None))} | "
            f"24h Δ={_fmt_pct(getattr(foi, 'change_24h_pct', None))}"
        )
        pct30 = getattr(foi, "percentile_30d_hourly", None)
        if pct30 is not None:
            lines.append(f"- 30d 百分位={_fmt(pct30, nd=0)}")
        if getattr(foi, "is_near_local_high_7d", False):
            lines.append("- ⚠ 接近 7d 局部最高（≥98%）")
        venue = getattr(foi, "venue_split", None) or []
        if venue:
            split_str = " · ".join(
                f"{getattr(v, 'venue', '?')}:{_fmt_pct(getattr(v, 'change_1h_pct', None))}"
                for v in venue[:3]
            )
            lines.append(f"- 头部 venue 1h Δ：{split_str}")

    # Funding
    ffu = snapshot.facts_funding
    if ffu is not None:
        _sub("Funding · 资金费")
        lines.append(
            f"- avg_current={_fmt(getattr(ffu, 'avg_current', None), nd=4)}% | "
            f"7d 百分位={_fmt(getattr(ffu, 'percentile_7d', None), nd=0)} | "
            f"30d 百分位={_fmt(getattr(ffu, 'percentile_30d', None), nd=0)}"
        )
        slope = getattr(ffu, "slope_24h", None)
        if slope:
            lines.append(f"- slope_24h=`{slope}`")
        if getattr(ffu, "sign_flip_7d", None):
            lines.append("- ⚠ 7d 符号翻转：是")
        days_neg = getattr(ffu, "days_negative_streak", 0)
        if days_neg:
            lines.append(f"- 连续负费率天数：{days_neg}")

    # CVD（合约 + 现货）
    fcc = snapshot.facts_cvd_contract
    fcs = snapshot.facts_cvd_spot
    if fcc or fcs:
        _sub("CVD · 期现")
        if fcc:
            lines.append(
                f"- 合约：trend_1h=`{getattr(fcc, 'trend_1h', '—')}` | "
                f"trend_30m=`{getattr(fcc, 'trend_recent_30m', '—')}` | "
                f"delta_1h={_fmt(getattr(fcc, 'delta_1h', None))} | "
                f"divergence=`{getattr(fcc, 'divergence_note', '—')}`"
            )
        if fcs:
            lines.append(
                f"- 现货：trend_1h=`{getattr(fcs, 'trend_1h', '—')}` | "
                f"trend_30m=`{getattr(fcs, 'trend_recent_30m', '—')}` | "
                f"delta_1h={_fmt(getattr(fcs, 'delta_1h', None))}"
            )

    # Basis
    fb = snapshot.facts_basis
    if fb is not None:
        _sub("Basis · 期现溢价")
        lines.append(
            f"- basis_pct={_fmt_pct(getattr(fb, 'basis_pct', None))} | "
            f"trend=`{getattr(fb, 'trend', '—')}`"
        )

    # Taker Flow
    ft = snapshot.facts_taker_flow
    if ft is not None:
        _sub("Taker Flow · 5m 期现")
        lines.append(
            f"- 最新 5m 合约 delta={_fmt(getattr(ft, 'latest_contract_delta_usd', None))} | "
            f"现货 delta={_fmt(getattr(ft, 'latest_spot_delta_usd', None))}"
        )
        if getattr(ft, "spot_vs_contract_divergence", None):
            lines.append(f"- 期现背离：`{ft.spot_vs_contract_divergence}`")

    # Liquidation flow
    lines.append(
        f"- 24h 全网清算：long=${_fmt(snapshot.global_liq_long_24h)} | "
        f"short=${_fmt(snapshot.global_liq_short_24h)} | "
        f"ratio={_fmt(snapshot.global_liq_ratio_24h, nd=2)}"
    )
    if snapshot.liq_sweep_events:
        lines.append(
            f"- 近 1h 扫单事件：上方 ${_fmt(snapshot.liq_sweep_above_usd_1h)} | "
            f"下方 ${_fmt(snapshot.liq_sweep_below_usd_1h)} ({len(snapshot.liq_sweep_events)} 起)"
        )

    # ── §10 Footprint / Absorption（硬证据确认层） ──
    _header("§10", "Footprint / Absorption（已成交硬证据确认层）")
    fp = snapshot.facts_footprint
    if fp is not None:
        _sub("Footprint")
        lines.append(f"- bar_closed=`{getattr(fp, 'latest_bar_closed', None)}`（False = provisional）")
        zones = getattr(fp, "top_imbalance_zones", []) or []
        if zones:
            lines.append(f"- top imbalance zones（{len(zones)}）：")
            for z in zones[:5]:
                price_z = getattr(z, "price", None)
                buy = getattr(z, "buy", 0) or 0
                sell = getattr(z, "sell", 0) or 0
                ratio = getattr(z, "ratio", 0) or 0
                side = getattr(z, "side", "—")
                ratio_str = "one-sided" if ratio >= 999 else f"{ratio:.1f}x"
                lines.append(
                    f"  - {_fmt_price(price_z)}: buy={_fmt(buy)} / sell={_fmt(sell)} "
                    f"({ratio_str}, `{side}`)"
                )
    fab = snapshot.facts_absorption
    if fab is not None:
        _sub("Absorption · 价位级被动吸收")
        sup_zones = getattr(fab, "zones_support", []) or []
        res_zones = getattr(fab, "zones_resistance", []) or []
        if sup_zones:
            lines.append(f"- Support 带 top {min(3, len(sup_zones))}：")
            for z in sup_zones[:3]:
                lines.append(
                    f"  - {_fmt_price(getattr(z, 'price', 0))} | "
                    f"vol=${_fmt(getattr(z, 'taker_volume_usd', 0))} | "
                    f"|delta|={_fmt(getattr(z, 'delta_pct_abs_avg', 0), nd=3)} | "
                    f"bar_count={getattr(z, 'bar_count', 0)} | "
                    f"age={_fmt(getattr(z, 'age_hours', 0), nd=1)}h"
                )
        if res_zones:
            lines.append(f"- Resistance 带 top {min(3, len(res_zones))}：")
            for z in res_zones[:3]:
                lines.append(
                    f"  - {_fmt_price(getattr(z, 'price', 0))} | "
                    f"vol=${_fmt(getattr(z, 'taker_volume_usd', 0))} | "
                    f"|delta|={_fmt(getattr(z, 'delta_pct_abs_avg', 0), nd=3)} | "
                    f"bar_count={getattr(z, 'bar_count', 0)} | "
                    f"age={_fmt(getattr(z, 'age_hours', 0), nd=1)}h"
                )

    # ── §11 宏观 / 情绪 / ETF / 稳定币 / 资金面慢变量（修正器，非主决策锚） ──
    _header("§11", "宏观 / 情绪 / ETF / 稳定币 / 资金面慢变量（修正器）")
    macro_lines: list[str] = []
    if snapshot.dxy is not None:
        macro_lines.append(f"DXY={_fmt(snapshot.dxy, nd=2)} ({_fmt_pct(snapshot.dxy_change_pct)})")
    if snapshot.btc_dominance is not None:
        macro_lines.append(f"BTC.D={_fmt(snapshot.btc_dominance, nd=2)}%")
    if snapshot.us_10y_yield is not None:
        macro_lines.append(f"US10Y={_fmt(snapshot.us_10y_yield, nd=3)}")
    if snapshot.fed_rate is not None:
        macro_lines.append(f"FedRate={_fmt(snapshot.fed_rate, nd=2)}%")
    if snapshot.nasdaq is not None:
        macro_lines.append(f"NDX={_fmt(snapshot.nasdaq)} ({_fmt_pct(snapshot.nasdaq_change_pct)})")
    if snapshot.gold is not None:
        macro_lines.append(f"Gold={_fmt(snapshot.gold)} ({_fmt_pct(snapshot.gold_change_pct)})")
    if macro_lines:
        lines.append("- 宏观：" + " | ".join(macro_lines))
    sentiment_lines: list[str] = []
    if snapshot.fear_greed_index is not None:
        sentiment_lines.append(f"恐贪={snapshot.fear_greed_index}")
    if snapshot.ahr999 is not None:
        sentiment_lines.append(f"Ahr999={_fmt(snapshot.ahr999, nd=4)}")
    if snapshot.btc_mvrv is not None:
        sentiment_lines.append(f"MVRV={_fmt(snapshot.btc_mvrv, nd=2)}")
    if sentiment_lines:
        lines.append("- 情绪：" + " | ".join(sentiment_lines))
    if snapshot.etf_net_3d is not None or snapshot.etf_trend:
        lines.append(
            f"- ETF：3d 净流入={_fmt(snapshot.etf_net_3d)} | trend=`{snapshot.etf_trend or '—'}`"
        )
    if snapshot.stablecoin_total_mcap:
        lines.append(
            f"- 稳定币市值={_fmt(snapshot.stablecoin_total_mcap)} | "
            f"7d Δ={_fmt_pct(snapshot.stablecoin_7d_change_pct)}"
        )
    if snapshot.coinbase_premium is not None:
        lines.append(
            f"- Coinbase 溢价={_fmt(snapshot.coinbase_premium, nd=2)}({snapshot.coinbase_premium_trend or '—'})"
        )
    # 现货流入流出 + 鲸鱼
    if snapshot.whale_net_direction:
        lines.append(
            f"- 鲸鱼方向：`{snapshot.whale_net_direction}` | "
            f"transfers={snapshot.whale_transfers_count} | "
            f"net_usd={_fmt(snapshot.whale_transfer_net_usd)}"
        )
    if snapshot.exchange_btc_total is not None:
        lines.append(
            f"- 交易所 BTC 总余额={_fmt(snapshot.exchange_btc_total)} | "
            f"24h Δ={_fmt_pct(snapshot.exchange_btc_change_pct)}"
        )
    # narratives 主题
    if snapshot.active_narratives:
        nar_strs = []
        for n in snapshot.active_narratives[:TOP_N_FACTS_NARRATIVE]:
            nar_strs.append(
                f"`{n.get('theme_name_cn') or n.get('theme_id')}`"
                f"({n.get('current_direction_bias', '—')}/i={n.get('current_intensity', 0)})"
            )
        lines.append(f"- 活跃主题：{' · '.join(nar_strs)}")
    # 简报
    if snapshot.news_brief_text:
        nb = snapshot.news_brief_text
        if len(nb) > 500:
            nb = nb[:500] + "…"
        lines.append(f"- 新闻简报（v{snapshot.news_brief_version}）：{nb}")

    # ── §12 期权（max pain / IV / put-call OI） ──
    _header("§12", "期权 · max pain / IV / put-call OI")
    fop = snapshot.facts_options
    if fop is not None:
        _sub("MAA Facts Options")
        mp = getattr(fop, "max_pain_price", None)
        if mp is not None:
            lines.append(f"- max_pain={_fmt_price(mp)}")
        ne = getattr(fop, "nearest_expiry", None)
        if ne:
            lines.append(f"- nearest_expiry={ne}")
        coi = getattr(fop, "call_oi", None)
        poi = getattr(fop, "put_oi", None)
        if coi is not None or poi is not None:
            lines.append(f"- call_oi={_fmt(coi)} | put_oi={_fmt(poi)}")
    # AISnapshot 自带的期权字段
    if snapshot.option_max_pain_price is not None:
        lines.append(f"- option_max_pain_price={_fmt_price(snapshot.option_max_pain_price)}")
    if snapshot.option_nearest_expiry:
        lines.append(f"- option_nearest_expiry={snapshot.option_nearest_expiry}")
    if snapshot.btc_implied_vol is not None:
        lines.append(f"- BTC IV={_fmt(snapshot.btc_implied_vol, nd=2)}%")
    if snapshot.btc_put_call_oi is not None:
        lines.append(f"- BTC put/call OI={_fmt(snapshot.btc_put_call_oi, nd=2)}")

    # ── §13 冲突矩阵自检（必填提示，AI 真正在 evidence_matrix 字段输出） ──
    _header("§13", "冲突矩阵自检（必填）")
    lines.append("**你必须在输出 JSON 的 `evidence_matrix` 字段里**显式列出：")
    lines.append("- `long_evidence[]`：所有支持做多/反弹的证据（含 §N 引用）")
    lines.append("- `short_evidence[]`：所有支持做空/破位的证据")
    lines.append("- `wait_evidence[]`：所有支持等待的证据（含数据冲突 / RR 不足 / 中间位置）")
    lines.append("- `contradictions[]`：1-3 条关键矛盾（中文白话，列出哪两条互相打架）")
    lines.append("- 若 contradictions 严重 → 优先输出 `decision=WAIT`")

    # ── §14 输出任务 + JSON Schema 提示 ──
    _header("§14", "输出任务（按 8 步思考流程产出 JSON）")
    lines.append(f"- 标的：**{coin}** · 当前价：{_fmt_price(price)}")
    lines.append("- 严格按 system prompt 给定的 JSON Schema 输出，禁止任何 Markdown / 解释 / emoji")
    lines.append("- decision 必须落在 6 选 1：WAIT / LONG_OBSERVATION / SHORT_OBSERVATION / LONG_PLAN / SHORT_PLAN / NO_TRADE")
    lines.append("- 如果数据严重不足，请输出 `decision=NO_TRADE` + `data_self_check.hard_stop_triggered=true`")
    lines.append("- evidence 中每条都必须引用 `section_ref`（§1-§12 中的一个）")
    lines.append("- horizon 自决：scalp / intraday / swing / strategic")

    return "\n".join(lines), sections
