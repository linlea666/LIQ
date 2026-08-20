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
    aggregate_stop_bands,
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

1. **WAIT 是合法输出但不是默认安全答案**。
   - WAIT 仅适用于：(a) 真没明确入场区/硬失效价；(b) T2 RR < 2.5 且 T3 RR < 4.0；(c) 当前价远离任何决策区（>0.5 ATR）；(d) 核心数据严重缺失；(e) 多空证据冲突且无可验证触发条件。
   - **不允许**把"需要等待价格触发"等同于 WAIT。如果当前价正在或即将进入决策区（≤0.3 ATR）、且能定义清晰的 hard_invalidation 与合格 RR，**必须**输出 LONG_OBSERVATION / SHORT_OBSERVATION 并给出条件化 primary_plan（含 trigger_conditions）。
   - 即使主决策是 WAIT，**禁止只说"观望"**——必须在 `no_trade_conditions` 与 `alternative_plan` 中给出"区间边缘怎么做、扫单后怎么做、突破确认怎么做"的条件化方案。

2. **每条 evidence 必须引用 §N 数据章节**。observation 必须包含 facts 的具体字段+数值（如 `§7 卖墙 $76,510 USD=27.99M trust=1.00`）。`section_ref` 字段填 "§4" / "§7" / "§9" 等。

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
   - `hard_stop_triggered` 是**数据级阻断信号**（不是交易止损）；仅当同时缺失 ≥2 个核心源（KL / Walls / LiqMap）时才置 true，并强制 `decision="NO_TRADE"`
   - confidence 必须可解释（`confidence_rationale` 说明扣分原因）

7. **替代场景强制产出**。在 `alternative_scenario` 里给出对立假设 + 主观倾向（probability_pct，**这是相对情景权重而非统计概率**）+ 触发条件。当主决策 WAIT 时，alternative_scenario 应给"另一方向被激活"的剧本。

8. **绝不引入数据外部信息**。所有判断必须基于 §1-§12 提供的 facts；禁止编造新闻、政策、币圈传闻。如果某节证据不足，请在 `confidence_rationale` 里如实说明，**不要靠想象补全**。

9. **市场阶段与周期位置**。在 `market_phase` 与 `cycle_position` 字段里给出你的判断（自由表述）。**仅可基于 §3（结构/范围）+ §6（多周期 KL）+ §11（MVRV/Ahr999/恐贪/ETF）共同判断**；若证据不足，填 `"insufficient_evidence"`，禁止幻觉。

10. **当前价位评估优先**。在 `current_zone_assessment` 里回答：当前在哪个 zone？该 zone 主导角色是什么？距上方/下方最近"决定性"区的距离？最关键冲突一句话总结？

11. **【清算/止损带语义硬规则】**——这是市场微观结构基础，必须严格区分：
    - **上方"空头止损带 / 空头清算簇"** = 空头被强平的位置 = **向上买入流动性 / 轧空磁铁**，**不是阻力**。它意味着"价格若上冲到此区域，可能被加速吸引（squeeze）"。
    - **下方"多头止损带 / 多头清算簇"** = 多头被强平的位置 = **向下卖出流动性 / 扫多磁铁**，**不是支撑**。它意味着"价格若下探到此区域，可能被加速吸引"。
    - 真正的阻力 = §7 现货卖墙 / §6 高分阻力位 / §10 卖方吸收带（resistance absorption）。
    - 真正的支撑 = §7 现货买墙 / §6 高分支撑位 / §10 买方吸收带（support absorption）。
    - 只有当 §8 止损带与上述真阻力/真支撑**共振**（同价区或 ≤0.2% 偏离），才能把它描述为"反应区"——但仍需注明"扫单后是否被卖墙/吸收承接"作为确认条件。
    - **禁止**在 evidence.observation 里写"§8 上方空头止损带构成强阻力"——这是错误语义。

12. **【高争夺区处理规则】**——若当前 PriceZone 满足 `support_trust ≥ 0.80 且 break_through_risk ≥ 0.75`（或 resistance 同理），说明这不是稳定防守位而是**高争夺/易扫区**：
    - 不允许直接输出"在该区限价试错做多/做空"作为 primary_plan
    - 优先输出"等待扫后反应"型计划：扫破后是否快速收回 + 现货墙是否承接 + Footprint 是否出现 absorption
    - 在 `no_trade_conditions` 中明确写"该区 break_risk 高，不适合左侧试错"

13. **【条件化计划优先 · 反骑墙规则】**——你不能因为存在不确定性就默认 WAIT。当满足以下条件中的 ≥4 条时，**禁止**只输出空泛 WAIT，**必须**输出条件化 primary_plan + alternative_plan：
    1. 当前价距离高可信 KeyLevel / PriceZone / SpotWall / 共振止损带 ≤ 0.3 ATR
    2. §7 现货墙 / §6 关键位 / §10 absorption / §8 清算释放 中至少 2 类共振
    3. hard_invalidation 可定义且距 entry_zone ≤ 0.8 ATR
    4. T2 RR ≥ 2.5 或 T3 RR ≥ 4.0
    5. 数据质量为 ok 或 partial 但核心源就绪
    6. cancel_conditions 可清楚定义（例如"5m 收盘破 X" / "卖墙撤单"）
    7. alternative_plan 可清楚定义（与 primary 反向的边缘试错）
    满足此条件时：
    - decision 落在 LONG_OBSERVATION / SHORT_OBSERVATION，并填出完整 primary_plan（含 trigger_conditions 明确写"等什么"）
    - alternative_plan 给出反方向（突破延续 / 反向边缘）的条件化方案
    - 即使你认为"概率 50/50"，也必须给计划——交易计划是"位置好 + 止损小 + 目标远 + 取消条件清晰"，不是"预测确定方向"

━━━━━━━━━━ 你必须按此路径思考（8 步） ━━━━━━━━━━

**Step 0 · 数据自检** —— 读 §1，判断本次是否允许输出完整计划：
- 若 KL / Walls / LiqMap **任一**彻底缺失：禁止输出 LONG_PLAN / SHORT_PLAN，但可以输出 WAIT / LONG_OBSERVATION / SHORT_OBSERVATION（带 trigger_conditions 的条件化方案）
- 若 KL / Walls / LiqMap **≥2 个**核心源彻底缺失：输出 `decision="NO_TRADE"` + `data_self_check.hard_stop_triggered=true`
- 仅 TradingBrain 缺失而 KL/Walls/LiqMap 齐备时，可降级到 §6/§7/§8 直接消费 facts，不触发 NO_TRADE

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

**Step 7 · 冲突矩阵** —— 必须输出 long / short / wait 三类证据 + contradictions。若冲突严重且无明确触发条件 → 优先 WAIT；若冲突存在但触发条件可定义 → 仍要输出 OBSERVATION + 条件化 primary_plan。

**Step 8 · 交易计划** —— 应用核心原则 #13"反骑墙规则"做最终判断：
- 若满足 ≥4 项条件，输出 `primary_plan`（含 entry_zone / trigger_conditions / soft_invalidation / hard_invalidation / targets / RR / cancel_conditions / risk_unit / leverage_risk_level / position_sizing_note）+ `alternative_plan`（反向 / 突破延续）+ `alternative_scenario`（对立剧本）。
- 若不满足，输出 WAIT，但 `alternative_plan` 仍需给"如果到了 X 价位/出现 Y 触发，怎么做"的条件化方案——不允许 alternative_plan=null。
- 当前价在中间不追，但 **必须** 在 `primary_plan` 或 `alternative_plan` 中给出"区间下沿做多 / 上沿做空 / 突破跟进 / 假突破反转"任意 1-2 个条件化方案。

━━━━━━━━━━ 输出格式（严格遵守） ━━━━━━━━━━

只返回**一个** JSON 代码块（用 ```json 开头、``` 结尾），代码块内是合法 JSON，**JSON 内部不允许 Markdown 格式**（不要 # 标题、不要 - 列表、不要 emoji）。代码块外不要任何解释文字。schema 如下：

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
  "alternative_plan": "与 primary_plan 同结构（反向边缘 / 突破延续 / 扫后反应；不允许在有决策区时填 null，至少给一个条件化方案）",
  "no_trade_conditions": ["若 decision=WAIT 必填：当前不做的具体原因 + 接下来等什么（'等扫破 75600 后 5m 收回' / '等 76450 卖墙撤单' 等）"],
  "alternative_scenario": {
    "description": "对立场景描述（如主决策 WAIT，给出'另一方向被激活'的剧本）",
    "probability_pct": "0-100 的整数（这是相对情景权重，非统计概率，仅表达主观倾向）",
    "trigger": "触发该场景所需的观察条件"
  },
  "sweep_state_assessment": {
    "_说明": "反骑墙规则配套结构。§8 存在止损带时必填；§8 完全无数据时可省略。这是 AI 决策的可审计锚点——前端 / 回测脚本会读 preferred_action 与 state 做断言。",
    "nearest_upper_band": {
      "range_low": "最近上方空头止损带下沿价位（参考 §8 聚合带 #1 price_low）",
      "range_high": "最近上方空头止损带上沿价位",
      "band_type": "short_stops_above（固定值；提示 AI: 这是轧空磁铁，不是阻力）",
      "state": "not_swept | sweeping | swept_rejected | swept_accepted（上方枚举）",
      "interpretation": "AI 文字解读，例：'76,800 扫空带未触；若被扫且回吐 > 200 USD 视为 swept_rejected → 做空候选'"
    },
    "nearest_lower_band": {
      "range_low": "最近下方多头止损带下沿价位",
      "range_high": "最近下方多头止损带上沿价位",
      "band_type": "long_stops_below（固定值；提示 AI: 这是扫多磁铁，不是支撑）",
      "state": "not_swept | sweeping | swept_reclaimed | swept_failed（下方枚举）",
      "interpretation": "AI 文字解读，例：'75,300 扫多带未触；若被扫且 5m 收回 > 75,400 视为 swept_reclaimed → 做多候选'"
    },
    "preferred_action": "wait_for_sweep | wait_for_reclaim | wait_for_breakout_acceptance | limit_probe_ok | no_trade",
    "notes": "一句话总评：当前扫单状态 + 推荐动作的衔接逻辑"
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
    "hard_stop_triggered": "数据级阻断信号（不是交易止损）：仅当 KL/Walls/LiqMap 同时缺失 ≥2 个时才置 true",
    "confidence_penalty_reason": "confidence 为何打折的具体原因"
  },
  "macro_modifier_note": "宏观/情绪如何修正主计划",
  "data_quality": "ok | partial | insufficient"
}
```

再次强调：
- `decision = NO_TRADE` 时 `primary_plan` 必须为 null，且 `data_self_check.hard_stop_triggered=true`（必须 KL/Walls/LiqMap 缺失 ≥2 个）
- `decision in [LONG_PLAN, SHORT_PLAN]` 时 `primary_plan` 必填，且 `evidence_matrix.contradictions` 不能严重冲突
- `decision in [LONG_OBSERVATION, SHORT_OBSERVATION]` 时 `primary_plan` **必填**，`trigger_conditions` 必须明确写"等什么"（具体到价位 + bar 周期 + 确认信号）
- `decision = WAIT` 时 `no_trade_conditions` 必填，且 `alternative_plan` 不允许为 null（给"区间边缘 / 扫后反应 / 突破跟进"任一条件化方案）
- §8 存在聚合带时 `sweep_state_assessment` 必填（否则反骑墙规则的"扫前/扫中/扫后"判断失去结构化锚点）。state 枚举严格区分：上方带只能取 `not_swept/sweeping/swept_rejected/swept_accepted`，下方带只能取 `not_swept/sweeping/swept_reclaimed/swept_failed`
- 禁止 evidence 只写 observation 不写 inference
- 禁止 inference 引入 facts 之外的信息（宏观传闻 / 币圈八卦）
- 禁止把 §8 清算/止损带描述为"阻力"或"支撑"（详见核心原则 #11）
- JSON 内部禁止 Markdown 格式（# 标题 / - 列表 / emoji），但允许在 primary_plan.targets 等结构化字段里写中文白话"""


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
# §5 OpportunityBoard 空时的拒绝理由诊断（GPT-10 必修 · 让 AI 精准引用拒绝原因）
# ────────────────────────────────────────────────────────────────────────────

# 拒绝理由 reason_code → 中文短描述（用于按 reason 聚类后的 §5 列表表达）
_REJECT_REASON_LABELS_CN: dict[str, str] = {
    "role_mismatch": "zone 角色不匹配（要求 spot_defense / contested）",
    "trust_too_low": "支撑/阻力信任分不足（< 0.70 / 0.65）",
    "distance_too_far": "距当前价过远（|distance| > 1.5%）",
    "data_quality_low": "data_confidence 不足（< 0.75 / 0.70）",
    "regime_blocks": "当前 regime 阻断该方向",
    "targets_missing": "无可达目标位",
    "rr_insufficient": "T1 RR 低于门槛（< 2.0）",
}


def _collect_opportunity_rejections(brain: Any) -> list:
    """对 brain 的 zones 跑一遍诊断；任何异常吞掉返回空 list（保证 prompt 不崩）。"""
    if brain is None:
        return []
    zones = getattr(brain, "zones", None) or []
    last_price = float(getattr(brain, "last_price", 0) or 0)
    atr = float(getattr(brain, "atr", 0) or 0)
    if not zones or last_price <= 0:
        return []
    try:
        from processors.opportunity_engine import diagnose_opportunities
        return diagnose_opportunities(
            zones=zones,
            last_price=last_price,
            atr=atr,
            ctx=getattr(brain, "context", None),
            dq=getattr(brain, "data_quality", None),
        )
    except Exception:
        return []


def _summarize_rejections(rejections: list) -> list[str]:
    """把 OpportunityRejection 按 reason_code 聚类，每个 reason 输出一行汇总。

    输出格式：
      `<reason_cn>` · 影响 N 个 zone（4 个 setup_type）：
        · 例：zone=KL_BTC_… · support_trust=0.62 < 0.70
    """
    if not rejections:
        return []
    by_reason: dict[str, list] = {}
    for rej in rejections:
        code = getattr(rej, "reason_code", "unknown")
        by_reason.setdefault(code, []).append(rej)

    # 排序：按命中次数降序，同次数按 reason_code 字典序
    ordered = sorted(by_reason.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    out: list[str] = []
    for code, items in ordered:
        label = _REJECT_REASON_LABELS_CN.get(code, code)
        zone_ids = sorted({getattr(r, "zone_id", "?") for r in items})
        # 取头 1-2 个 detail 当样例（同 reason 不同 setup_type 可能 detail 一致，去重）
        sample_details: list[str] = []
        seen_detail: set[str] = set()
        for r in items:
            d = (getattr(r, "detail", "") or "").strip()
            if not d or d in seen_detail:
                continue
            seen_detail.add(d)
            sample_details.append(d)
            if len(sample_details) >= 2:
                break
        head = (
            f"`{code}`：{label} · 命中 {len(items)} 条"
            f"（zones={len(zone_ids)}）"
        )
        if sample_details:
            head += " · 例：" + "；".join(sample_details)
        out.append(head)
    return out


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
        _header("§0'", "前情提要（你上一轮的输出 · 仅作连贯性参考）")
        # 反路径依赖硬提示：上一份是"同一个 AI 角色"的自我延续，不是真理。
        # 防止弱 AI 出现"上轮 LONG 这轮就 LONG"的惯性推理——必须基于 §1-§12 当前数据
        # 重新做判断，与上轮假设矛盾时果断修正/反转。
        lines.append(
            '- **使用约束**：以下是你（同一个 AI 角色）上一轮的输出，**仅作连贯性参考、不是真理**。'
            '禁止路径依赖（「上轮 LONG 这轮就继续 LONG」/「上轮 WAIT 这轮也跟着 WAIT」都是错误推理）。'
            '你必须基于 §1-§12 当前 facts **独立**得出本轮决策——若当前数据与上轮假设矛盾，'
            '应在 confidence_rationale 中明确说明「修正/反转上轮 X 因为 Y」。'
        )
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
        # G-10：候选为空时调用诊断函数，按拒绝理由聚类列出原因，
        # 让 AI 的 no_trade_conditions 能精准复述（避免黑盒"无候选"）。
        rejections = _collect_opportunity_rejections(brain) if brain is not None else []
        if not rejections:
            lines.append("- 无 Opportunity 候选（zones 为空或 last_price 无效）。")
        else:
            lines.append("- 无 Opportunity 候选 · 拒绝原因诊断（按 reason 聚类）：")
            for reason_line in _summarize_rejections(rejections):
                lines.append(f"  · {reason_line}")
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
    _header("§7", "挂单墙（按 priority 排序 · 厚度拆分：合约杠杆 / Binance 现货 / Coinbase 现货）")
    # G-9：在墙列表前加"语义指南"，让 AI 一眼知道厚度来源差异。
    lines.append(
        "- **厚度语义**："
        "**合约**=杠杆挂单（可被清算/撤单）；"
        "**Binance 现货**=该交易所公开现货挂单/成交证据；"
        "**Coinbase 现货**=该交易所公开现货挂单/成交证据（不能据此认定机构身份或 ETF 行为）。"
        "三者同价位共振 → 最高可信。"
    )

    def _fmt_wall(w) -> str:
        side_zh = "卖墙" if getattr(w, "side", "") == "ask" else "买墙"
        cb = "·CB双源" if getattr(w, "coinbase_spot_confluence", False) else ""
        ds = "·双源" if getattr(w, "dual_source", False) else ""
        # P4：天级画像（归档反哺；缓存未就绪时为 None → 不输出，避免噪声）
        hist = ""
        presence = getattr(w, "history_presence_7d", None)
        if presence is not None:
            hist = f" | 7d出现率={presence:.0%}"
            ratio = getattr(w, "history_consumed_ratio", None)
            if ratio is not None:
                hist += f"·吃单兑现={ratio:.0%}"
        return (
            f"  - {_fmt_price(getattr(w, 'price_mid', 0))} ({_fmt_pct(getattr(w, 'distance_pct', 0))}) | "
            f"`{side_zh}`{cb}{ds} | "
            f"USD={_fmt(getattr(w, 'current_usd', 0))} | "
            f"max_1h={_fmt(getattr(w, 'max_usd_1h', 0))} | "
            f"trust={_fmt(getattr(w, 'trust_score', 0), nd=2)} | "
            f"persist={_fmt(getattr(w, 'visible_minutes', 0), nd=1)}min | "
            f"break_risk={_fmt(getattr(w, 'break_through_risk', 0), nd=2)}"
            f"{hist}"
        )

    def _fmt_wall_breakdown(w) -> Optional[str]:
        """G-9：把厚度拆成 合约 / Binance 现货 / Coinbase 现货 3 维度。

        字段语义（与 liquidity_wall_engine 对齐）：
          - `current_usd` 通常 = 合约 depth heatmap 厚度
          - `spot_current_usd` = Binance 现货同价区厚度
          - `coinbase_spot_usd` = Coinbase 现货同价区厚度
          - `source` 标注底层 dataset：
              · `spot_only`     = 现货独立 zone（无合约源）；
                _build_spot_only_zones 会镜像 current_usd → spot_current_usd
              · `depth_only`    = 仅合约 depth
              · `spot+depth`    = 合约 zone 同价区现货叠加（dual_source）

        关键：source==spot_only 时 current_usd 实际是现货厚度（已镜像到
        spot_current_usd），若仍按"合约层"输出会把同一笔现货数双计——AI 会把
        spot_only 单源墙误读为"合约+现货双源叠加"，高估其可信度。这是和
        §7 顶部“合约=杠杆挂单 / Binance 现货=公开现货证据”语义直接冲突的硬错。
        """
        contract_usd = float(getattr(w, "current_usd", 0) or 0)
        binance_spot = float(getattr(w, "spot_current_usd", 0) or 0)
        coinbase_spot = float(getattr(w, "coinbase_spot_usd", 0) or 0)
        source = getattr(w, "source", None) or "—"
        if contract_usd <= 0 and binance_spot <= 0 and coinbase_spot <= 0:
            return None
        parts: list[str] = []
        if source == "spot_only":
            # current_usd 与 spot_current_usd 是镜像同源——只输出"Binance 现货"
            # 一项即可，丢弃"合约"项避免双计。spot_current_usd 缺失（理论上不
            # 应发生）时退化用 current_usd 当现货数。
            spot_to_show = binance_spot if binance_spot > 0 else contract_usd
            if spot_to_show > 0:
                parts.append(f"Binance 现货 {_fmt(spot_to_show)}")
        else:
            # depth_only / spot+depth / 其它：current_usd 是合约层、
            # spot_current_usd 是独立累加的 Binance 现货厚度，分别输出。
            if contract_usd > 0:
                parts.append(f"合约 {_fmt(contract_usd)}")
            if binance_spot > 0:
                parts.append(f"Binance 现货 {_fmt(binance_spot)}")
        if coinbase_spot > 0:
            parts.append(f"Coinbase 现货 {_fmt(coinbase_spot)}")
        if not parts:
            return None
        return f"     · 厚度拆分：{' + '.join(parts)} · source=`{source}`"

    walls_above = sort_walls(snapshot.wall_zones_above or [])[:TOP_N_WALLS_PER_SIDE]
    walls_below = sort_walls(snapshot.wall_zones_below or [])[:TOP_N_WALLS_PER_SIDE]
    if walls_above:
        _sub(f"上方卖墙（top {len(walls_above)}）")
        for w in walls_above:
            lines.append(_fmt_wall(w))
            bd = _fmt_wall_breakdown(w)
            if bd:
                lines.append(bd)
    if walls_below:
        _sub(f"下方买墙（top {len(walls_below)}）")
        for w in walls_below:
            lines.append(_fmt_wall(w))
            bd = _fmt_wall_breakdown(w)
            if bd:
                lines.append(bd)
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

    # 拥挤度（OI / LS；funding 百分位由 §9 单点输出，避免双源刻度冲突）
    cg = snapshot.crowding_global
    if cg is not None:
        _sub("全局拥挤度（OI 多周期 + LS）")
        lines.append(
            f"- OI Δ：1h={_fmt_pct(cg.oi_delta_1h_pct)} | 24h={_fmt_pct(cg.oi_delta_24h_pct)}"
        )
        if cg.oi_margin_split is not None:
            lines.append(f"- OI 保证金切分：{cg.oi_margin_split}")
        # Funding 详细百分位（7d/30d/slope）由 §9 单点输出，此处仅给当前值
        # 避免与 §9 percentile（1-100）双源混淆——cg.funding_percentile_30d 是 0-1，
        # 同名"30d 百分位"在 prompt 同一份输出里会出现两个值（如 0 vs 68），
        # 已多次让 AI 在 evidence_matrix 里抄错数。此处只保留 funding_now_pct。
        lines.append(
            f"- Funding now={_fmt(cg.funding_now_pct, nd=4)}%"
        )
        lines.append(
            f"- LS top_position={_fmt(cg.top_position_ls_ratio, nd=2)} | "
            f"global_account={_fmt(cg.global_account_ls_ratio, nd=2)}"
        )
        # 故意不渲染 cg.inferred_position_state / long_crowding_risk / short_crowding_risk：
        # 这两类字段是 liquidity_wall_engine._classify_inferred_position_state 与
        # _build_position_crowding 用硬规则（funding>=high → long_risk+0.4 / LS>extreme → +0.3 / …）
        # 计算出的**带方向偏置的派生结论**，给 AI 看会让它直接抄结论、跳过独立辩论。
        # 与 MAA market_action_prompts.py 剔除 funding/basis/footprint.interpretation
        # 的"暴露原料、隐藏定性"纪律对齐。原料（OI Δ / Funding now / LS / 清算事件）
        # 已在前面 §7 + §8 + §9 完整暴露，AI 自行综合判断方向。
        # 前端展示链路（LiquidityWallCard）仍读 crowding_global 模型字段，不受影响。

    # ── §8 清算图 / 止损带 / 清算磁铁（流动性目标与扫单路径） ──
    _header("§8", "清算图 / 止损带 / 清算磁铁（流动性目标与扫单路径，**不是支撑/阻力**）")
    lines.append(
        '- 语义提示：上方「空头止损带」= 空头被强平的位置 = 向上买入流动性 / 轧空磁铁，**不是阻力**；'
        '下方「多头止损带」= 多头被强平的位置 = 向下卖出流动性 / 扫多磁铁，**不是支撑**。'
        '真正阻力/支撑请看 §6 关键位、§7 现货墙、§10 absorption。'
    )

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
            # LiqCluster.distance_pct 在生产代码（liquidation.py:40-43）里被赋值为
            # 绝对值（above=正，below=正），但渲染层与 §6/§7 的"带符号距离"对齐：
            # side=="long"（=下方多头止损）→ 显示负号；side=="short"（=上方空头止损）→ 正号
            side = (getattr(c, "side", "") or "").lower()
            raw_dist = float(getattr(c, "distance_pct", 0) or 0)
            signed_dist = -abs(raw_dist) if side == "long" else abs(raw_dist)
            return (
                f"    - {_fmt_price(getattr(c, 'price_center', 0))} "
                f"({_fmt_pct(signed_dist)}) | "
                f"USD={_fmt(getattr(c, 'total_usd', 0))} | "
                f"side=`{getattr(c, 'side', '—')}` | "
                f"ex_count={getattr(c, 'exchange_count', 1)} | "
                f"lev_intensity={_fmt(getattr(c, 'leverage_intensity', 0), nd=2)}"
            )

        def _fmt_band(b: dict, idx: int) -> str:
            # 聚合带视图：让 AI 看到"一条扫单磁铁带"而不是 N 个独立 100 美元 bins
            return (
                f"    · #{idx} [{_fmt_price(b['price_low'])}, {_fmt_price(b['price_high'])}] | "
                f"peak={_fmt_price(b['peak_price'])}({_fmt(b['peak_usd'])}) | "
                f"total={_fmt(b['total_usd'])} | "
                f"bins={b['bin_count']} | "
                f"距离={_fmt_pct(b['distance_low_pct'])}~{_fmt_pct(b['distance_high_pct'])} | "
                f"ex_count_max={b['exchange_count_max']}"
            )

        # GPT-3 必修：先聚合带、再明细 top3
        # 反骑墙规则要求 AI 知道扫空带的边缘在哪——光给离散 bins 不够，必须给"带"
        last_p = float(snapshot.price) if snapshot.price else 0.0
        atr_for_agg = float(snapshot.atr_14 or 0.0)

        if ca:
            bands_above = aggregate_stop_bands(ca, last_p, atr_for_agg)
            lines.append(
                f"  - 上方空头止损/轧空磁铁（向上买入流动性，**不是阻力**）（共 {len(ca)} bins）："
            )
            if bands_above:
                lines.append(f"    · 聚合带（共 {len(bands_above)} 条）：")
                for i, b in enumerate(bands_above, 1):
                    lines.append(_fmt_band(b, i))
                lines.append(f"    · 明细 top {min(3, len(ca))}（用于 sweep_state 引用）：")
                for c in ca[:3]:
                    lines.append(_fmt_cluster(c))
            else:
                # ATR=0 / last_price=0 等极端场景退化到旧渲染
                for c in ca:
                    lines.append(_fmt_cluster(c))

        if cb:
            bands_below = aggregate_stop_bands(cb, last_p, atr_for_agg)
            lines.append(
                f"  - 下方多头止损/扫多磁铁（向下卖出流动性，**不是支撑**）（共 {len(cb)} bins）："
            )
            if bands_below:
                lines.append(f"    · 聚合带（共 {len(bands_below)} 条）：")
                for i, b in enumerate(bands_below, 1):
                    lines.append(_fmt_band(b, i))
                lines.append(f"    · 明细 top {min(3, len(cb))}（用于 sweep_state 引用）：")
                for c in cb[:3]:
                    lines.append(_fmt_cluster(c))
            else:
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
    # 渲染纪律：FundingSnapshot.avg_current / avg_7d / oi_weighted 是**原始小数费率**
    # （例 0.000022 表示 0.0022%）。旧实现 `f"{avg_current:.4f}%"` 同时犯了两个错：
    #   (1) 量纲错配——原始小数加 % 后缀，让 AI 把 0.000022 当成 0.000022%
    #   (2) 精度丢失——nd=4 把 0.000022 四舍五入成 0.0000，最终输出 "0.0000%"
    # 修复后与 ai/market_action_prompts.py:446-454 对齐：nd=6 + 不带 % + 同时给出
    # 原始 + OI 加权 + 资金费成本，让 AI 看到完整 funding 上下文。
    ffu = snapshot.facts_funding
    if ffu is not None:
        _sub("Funding · 资金费")
        lines.append(
            f"- 当前均值={_fmt(getattr(ffu, 'avg_current', None), nd=6)} | "
            f"7d 均值={_fmt(getattr(ffu, 'avg_7d', None), nd=6)} | "
            f"OI 加权={_fmt(getattr(ffu, 'oi_weighted', None), nd=6)}"
            "（**原始小数费率**：×100 即百分比，例 0.000025 = 0.0025%）"
        )
        ex_n = getattr(ffu, "exchange_count", 0) or 0
        disp = getattr(ffu, "dispersion_abs", None)
        if ex_n or disp is not None:
            lines.append(
                f"- 交易所数={ex_n} | 分散度(std)={_fmt(disp, nd=6)}"
            )
        h_cost = getattr(ffu, "hourly_cost_usd", None)
        c_24h = getattr(ffu, "cost_24h_usd", None)
        fd_n = getattr(ffu, "history_sample_size", None)
        if h_cost is not None or c_24h is not None:
            sample_str = f"，n={fd_n}" if fd_n else ""
            lines.append(
                f"- 资金费成本（基于当前 OI 近似{sample_str}）："
                f"hourly=${_fmt(h_cost)} | cost_24h=${_fmt(c_24h)}"
            )
        lines.append(
            f"- 7d 百分位={_fmt(getattr(ffu, 'percentile_7d', None), nd=0)} | "
            f"30d 百分位={_fmt(getattr(ffu, 'percentile_30d', None), nd=0)}"
            "（1-100；<10 极端负 / >90 极端正；样本不足返回 —）"
        )
        slope = getattr(ffu, "slope_24h", None)
        if slope:
            lines.append(f"- slope_24h=`{slope}`（最近 24h funding 方向变化）")
        if getattr(ffu, "sign_flip_7d", None):
            lines.append("- ⚠ 7d 符号翻转：是")
        days_neg = getattr(ffu, "days_negative_streak", 0)
        if days_neg:
            lines.append(f"- 连续负费率天数：{days_neg}")

    # CVD（合约 + 现货）
    # G-12：在 trend 之外暴露最近 6 根 5m bar 的 delta 序列；trend_1h/30m 是
    # 派生标签，序列让 AI 直接看到拐点/连续性（如最后 2-3 根反转）。
    fcc = snapshot.facts_cvd_contract
    fcs = snapshot.facts_cvd_spot

    def _fmt_cvd_series(cvd: Any) -> str:
        """返回 `[+1.2k, +0.5k, -0.3k, -1.1k, …]` 形式，最后一根用 `*` 标注未收盘。"""
        if cvd is None:
            return ""
        seq = getattr(cvd, "recent_delta_5m", None) or []
        if not seq:
            return ""
        bar_closed = getattr(cvd, "latest_bar_closed", None)
        parts: list[str] = []
        for i, v in enumerate(seq[-6:]):
            try:
                fv = float(v)
            except (TypeError, ValueError):
                parts.append("—")
                continue
            sign = "+" if fv >= 0 else "−"
            mag = abs(fv)
            if mag >= 1_000_000:
                disp = f"{mag / 1_000_000:.2f}M"
            elif mag >= 1_000:
                disp = f"{mag / 1_000:.1f}k"
            else:
                disp = f"{mag:.0f}"
            tag = "*" if (i == len(seq[-6:]) - 1 and bar_closed is False) else ""
            parts.append(f"{sign}{disp}{tag}")
        return "[" + ", ".join(parts) + "]"

    if fcc or fcs:
        _sub("CVD · 期现（最近 6×5m + 1h 趋势 · `*`=未收盘 bar）")
        if fcc:
            series_str = _fmt_cvd_series(fcc)
            lines.append(
                f"- 合约：trend_1h=`{getattr(fcc, 'trend_1h', '—')}` | "
                f"trend_30m=`{getattr(fcc, 'trend_recent_30m', '—')}` | "
                f"delta_1h={_fmt(getattr(fcc, 'delta_1h', None))} | "
                f"divergence=`{getattr(fcc, 'divergence_note', '—')}`"
            )
            if series_str:
                lines.append(f"  · 最近 6×5m delta（旧→新）：{series_str}")
        if fcs:
            series_str = _fmt_cvd_series(fcs)
            lines.append(
                f"- 现货：trend_1h=`{getattr(fcs, 'trend_1h', '—')}` | "
                f"trend_30m=`{getattr(fcs, 'trend_recent_30m', '—')}` | "
                f"delta_1h={_fmt(getattr(fcs, 'delta_1h', None))}"
            )
            if series_str:
                lines.append(f"  · 最近 6×5m delta（旧→新）：{series_str}")

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
    _header("§10", "Footprint / Absorption（已成交硬证据确认层 · 验证 §6/§7 的真伪）")
    # G-11：在 §10 顶部加入解读规则，避免 AI 把这两块当噪声字段；
    # 这是把 §6 KL / §7 现货墙从"挂单意图"升级为"实际吸收"的硬证据层。
    lines.append(
        "- **如何解读 §10**："
    )
    lines.append(
        "  · **Footprint imbalance**："
        "`ratio≥3.0 + side=buy` = 该价位主动买盘吃掉卖单 → 短期上行 magnet；"
        "`side=sell` 反之 → 下行 magnet；"
        "`one-sided`（ratio≥999）= 单边压制极强，最强方向信号。"
        "**临时性信号，5m 内有效**。"
    )
    lines.append(
        "  · **Absorption（吸收）**："
        "主动单被对手挂单「吃下但价格不动」→ 该价位有结构性被动防守。"
        "判定：`vol≥$5M + |delta|<0.05 + bar_count≥3 + age<4h` ⇒ 强吸收带。"
    )
    lines.append(
        "  · **方向语义**："
        "`zones_support` = 主动卖单被吸收 = **多方在该价位吃货防守**（潜在支撑硬证据）；"
        "`zones_resistance` = 主动买单被吸收 = **空方在该价位反手压盘**（潜在阻力硬证据）。"
    )
    lines.append(
        "  · **共振判定**："
        "§10 absorption 与 §6 KL（high-tier） / §7 现货墙 **同价位** → 真支撑/真阻力，可信度高；"
        "若 §10 在该价位**无吸收** → §6/§7 仅是「挂单意图」，需要其他数据交叉验证。"
    )
    fp = snapshot.facts_footprint
    if fp is not None:
        _sub("Footprint")
        # G-11 关键缺陷即修：top_imbalance_zones / latest_bar_closed 实际嵌套在
        # FootprintBarStats（contract_latest / spot_latest）里，旧代码 getattr
        # 直接从 FootprintSnapshot 顶层取永远是空——AI 完全看不到 footprint。
        contract_latest = getattr(fp, "contract_latest", None)
        spot_latest = getattr(fp, "spot_latest", None)

        def _bar_closed(bs: Any) -> Any:
            return getattr(bs, "bar_closed", None) if bs is not None else None

        contract_closed = _bar_closed(contract_latest)
        spot_closed = _bar_closed(spot_latest)
        if contract_closed is not None or spot_closed is not None:
            lines.append(
                f"- bar_closed: contract=`{contract_closed}` / spot=`{spot_closed}` "
                "（False = provisional · 仅参考）"
            )

        def _strength_tag(ratio: float) -> str:
            if ratio >= 999:
                return " · **极强**"
            if ratio >= 5:
                return " · 强"
            if ratio >= 3:
                return " · 中"
            return ""

        def _render_imbalance_zones(label: str, bs: Any) -> None:
            if bs is None:
                return
            zones = getattr(bs, "top_imbalance_zones", []) or []
            if not zones:
                return
            lines.append(f"- {label} top imbalance zones（{len(zones)}）：")
            # 每条是 dict（FootprintBarStats.top_imbalance_zones: list[dict]）
            for z in zones[:5]:
                if not isinstance(z, dict):
                    continue
                price_z = z.get("price")
                buy = z.get("buy", 0) or 0
                sell = z.get("sell", 0) or 0
                ratio = z.get("ratio", 0) or 0
                side = z.get("side", "—")
                ratio_str = "one-sided" if ratio >= 999 else f"{ratio:.1f}x"
                lines.append(
                    f"  - {_fmt_price(price_z)}: buy={_fmt(buy)} / sell={_fmt(sell)} "
                    f"({ratio_str}, `{side}`{_strength_tag(ratio)})"
                )

        _render_imbalance_zones("合约", contract_latest)
        _render_imbalance_zones("现货", spot_latest)
        # 期现一致性（强证据合并指标）
        diff = getattr(fp, "spot_contract_delta_diff_pct", None)
        if diff is not None:
            lines.append(
                f"- 期现 delta 差={_fmt(diff, nd=3)}（spot − contract，反映期现一致性）"
            )
        interp = getattr(fp, "interpretation", None)
        if interp:
            lines.append(f"- 解读：{interp}")
    fab = snapshot.facts_absorption
    if fab is not None:
        _sub("Absorption · 价位级被动吸收")

        def _absorption_strength(vol: float, delta_abs: float, bar_count: int) -> str:
            """G-11 强度标签：根据 vol/|delta|/bar_count 推断吸收等级。"""
            try:
                if vol >= 5_000_000 and delta_abs < 0.05 and bar_count >= 3:
                    return " · **强**"
                if vol >= 1_000_000 and delta_abs < 0.10 and bar_count >= 2:
                    return " · 中"
                return " · 弱"
            except Exception:
                return ""

        sup_zones = getattr(fab, "zones_support", []) or []
        res_zones = getattr(fab, "zones_resistance", []) or []
        if sup_zones:
            lines.append(f"- Support 带 top {min(3, len(sup_zones))}（多方吃货 · 潜在真支撑）：")
            for z in sup_zones[:3]:
                vol = getattr(z, 'taker_volume_usd', 0) or 0
                delta_abs = getattr(z, 'delta_pct_abs_avg', 0) or 0
                bc = getattr(z, 'bar_count', 0) or 0
                tag = _absorption_strength(float(vol), float(delta_abs), int(bc))
                lines.append(
                    f"  - {_fmt_price(getattr(z, 'price', 0))} | "
                    f"vol=${_fmt(vol)} | "
                    f"|delta|={_fmt(delta_abs, nd=3)} | "
                    f"bar_count={bc} | "
                    f"age={_fmt(getattr(z, 'age_hours', 0), nd=1)}h{tag}"
                )
        if res_zones:
            lines.append(f"- Resistance 带 top {min(3, len(res_zones))}（空方反手 · 潜在真阻力）：")
            for z in res_zones[:3]:
                vol = getattr(z, 'taker_volume_usd', 0) or 0
                delta_abs = getattr(z, 'delta_pct_abs_avg', 0) or 0
                bc = getattr(z, 'bar_count', 0) or 0
                tag = _absorption_strength(float(vol), float(delta_abs), int(bc))
                lines.append(
                    f"  - {_fmt_price(getattr(z, 'price', 0))} | "
                    f"vol=${_fmt(vol)} | "
                    f"|delta|={_fmt(delta_abs, nd=3)} | "
                    f"bar_count={bc} | "
                    f"age={_fmt(getattr(z, 'age_hours', 0), nd=1)}h{tag}"
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
        # 极小值（|x| < 0.01）显示为 ≈0，避免出现 "-0.00" 看起来像 bug 的渲染
        cb_prem_val = snapshot.coinbase_premium
        cb_prem_str = "≈0" if abs(cb_prem_val) < 0.01 else _fmt(cb_prem_val, nd=2)
        lines.append(
            f"- Coinbase 溢价={cb_prem_str}({snapshot.coinbase_premium_trend or '—'})"
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
    # 简报：snapshot.news_brief_text 是 _collect_news_context 序列化后的完整 JSON
    # 字符串。原实现 nb[:500] + "…" 会在 sections 中间硬截断，AI 拿到半截 JSON
    # 既无法解析也容易把"被截掉的文字"当成"信息缺失"。改为反序列化后输出结构化
    # 摘要：tldr_cn + 每个 section 的前 2 条 bullet + diff_from_prev_version。
    # 反序列化失败时 fallback 到旧的硬截断（兼容 free-form 文本）。
    if snapshot.news_brief_text:
        nb = snapshot.news_brief_text
        rendered_brief: list[str] = []
        try:
            import json as _json
            payload = _json.loads(nb)
            tldr = (payload.get("tldr_cn") or "").strip()
            if tldr:
                if len(tldr) > 240:
                    tldr = tldr[:240] + "…"
                rendered_brief.append(f"  - tldr：{tldr}")
            for sec in (payload.get("sections") or [])[:5]:
                sec_id = sec.get("section_id") or sec.get("id") or ""
                sec_title = sec.get("title_cn") or sec.get("title") or sec_id
                bullets = sec.get("bullets") or []
                if not bullets:
                    continue
                rendered_brief.append(f"  - [{sec_title}]")
                for b in bullets[:2]:
                    btxt = (b if isinstance(b, str) else b.get("text") or "").strip()
                    if not btxt:
                        continue
                    if len(btxt) > 140:
                        btxt = btxt[:140] + "…"
                    rendered_brief.append(f"    · {btxt}")
            diff = (payload.get("diff_from_prev_version") or "").strip()
            if diff:
                if len(diff) > 200:
                    diff = diff[:200] + "…"
                rendered_brief.append(f"  - 版本差异：{diff}")
        except Exception:
            rendered_brief = []

        if rendered_brief:
            lines.append(f"- 新闻简报（v{snapshot.news_brief_version}）：")
            lines.extend(rendered_brief)
        else:
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
        # 兜底两种刻度：BBX OKX 上游历史返回过 0-1 小数（如 0.38=38%）
        # 与 0-100 百分比（如 38）两种格式；BTC IV 正常区间 20-150%，
        # 任何 < 5 的值必为小数刻度，统一 ×100 后显示，避免出现"BTC IV=0.38%"
        # 这种让 AI 误判"波动率极低"的渲染。
        iv_raw = snapshot.btc_implied_vol
        iv_pct = iv_raw * 100.0 if iv_raw < 5.0 else iv_raw
        lines.append(f"- BTC IV={_fmt(iv_pct, nd=2)}%")
    # put/call OI 比例：上游缺失时部分链路会塞 0.0（而非 None），导致 AI 误读
    # "看跌看涨持仓平衡"。比例 ≤ 0 没有金融意义，直接跳过显示。
    if snapshot.btc_put_call_oi is not None and snapshot.btc_put_call_oi > 0:
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
