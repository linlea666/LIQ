"""趋势衰竭模块 · AI 解读器（DeepSeek Reasoner 驱动）

职责边界
--------
**AI 是规则引擎的审计员 + 再判断者，不是翻译器。**

- 规则引擎（`processors/trend_exhaustion.py`）只是**初步整理了数据**：给出 sub_scores
  的分值与 note，以及根据机械规则算出的 `overall_direction / state / action`。
- 本模块基于 AI 的推理能力，**拿着 sub_scores 原始读数 + 关键位快照 + 多周期证据
  再判一遍**。输出五类价值：
    1. 【趋势评估】当前真实趋势 + 动能健康度（**有权推翻规则的 direction**）
    2. 【关键位投射】能否突破最近 S/A 级关键位 + 置信分档
    3. 【矛盾消解】识别 sub 暗中对抗 direction 的情况
    4. 【陷阱提醒 + 触发条件】按规则行动的坑 + 下一个要看的信号
    5. 【交易倾向】可选：给方向 + 区间 + 失效位 + 时间窗（允许独立）

**严禁**引用历史价格 / 走势（防事实幻觉）；**严禁**编造 key_levels 之外的价位。

设计决策
--------
- 模型：**deepseek-reasoner**（R1 思维链，擅长多因子矛盾消解）
- 输出：强制 JSON schema，解析失败降级为 error 兜底
- 缓存：按"信号指纹"缓存 30 分钟
- 客户端独立：不复用主 AIAnalyzer
- 思考过程归档：reasoning_content 落盘单独 *.thinking.jsonl
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass
from typing import Optional

from openai import AsyncOpenAI

from config.settings import get_settings
from models.te_interpretation import TEAIInterpretation

logger = logging.getLogger(__name__)

# ── 配置常量 ──────────────────────────────────────
_CACHE_TTL_SEC = 30 * 60           # 同指纹缓存 30 分钟
_CACHE_MAX_ENTRIES = 200           # LRU 上限
_MAX_REASONING_STORE_CHARS = 50000 # reasoning 单次入库最大长度
_DEFAULT_TIMEOUT_SEC = 180         # Reasoner 思考可能慢


@dataclass
class _CacheEntry:
    result: TEAIInterpretation
    expires_at: float


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 数据压缩函数（把 TrendExhaustionSignal + KeyLevelSnapshotV2 压成 AI 能消化的结构）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _fingerprint(coin: str, signal_dict: dict, kl_dict: Optional[dict] = None) -> str:
    """为信号生成缓存键：采纳"判定性"字段，避免毛刺扰动缓存。

    采纳：coin + overall_state + direction + regime + regime_vetoed +
          consensus + 三周期 composite（1 位）+ key_levels 指纹（若有）
    """
    keys = {
        "coin": coin.upper(),
        "s": signal_dict.get("overall_state"),
        "d": signal_dict.get("overall_direction"),
        "r": signal_dict.get("regime"),
        "v": signal_dict.get("regime_vetoed"),
        "c": signal_dict.get("consensus_level"),
    }
    for tf_key in ("tf_1h", "tf_4h", "tf_1d"):
        tf = signal_dict.get(tf_key) or {}
        comp = float(tf.get("composite_score", 0.0) or 0.0)
        keys[f"{tf_key}_c"] = round(comp, 1)
        keys[f"{tf_key}_s"] = tf.get("state")

    # key_levels 参与指纹：只取 S/A 级强位价格（四舍五入到百位）避免频繁刷新
    if kl_dict:
        try:
            top_prices: list[float] = []
            for lv in (kl_dict.get("levels") or [])[:10]:
                if lv.get("strength_tier") in ("S", "A"):
                    top_prices.append(round(float(lv.get("price", 0.0)) / 100.0) * 100.0)
            keys["kl"] = sorted(top_prices)[:6]
            bb = (kl_dict.get("bull_bear_line") or {}).get("current_regime")
            keys["bb"] = bb
        except Exception:
            pass

    s = json.dumps(keys, sort_keys=True, ensure_ascii=False)
    return hashlib.md5(s.encode("utf-8")).hexdigest()[:16]


def _compact_sub(sub: list[dict] | None) -> list[dict]:
    """保留 note 原文（含 RSI=65.5、价离 EMA20 +3.4σ、OI+1.15% 等关键数值）。"""
    if not sub:
        return []
    return [
        {
            "name": s.get("name"),
            "score": round(float(s.get("score", 0.0)), 2),
            "note": s.get("note", ""),
        }
        for s in sub
    ]


def _compact_tf(tf: dict | None) -> Optional[dict]:
    if not tf:
        return None
    return {
        "direction": tf.get("direction"),
        "state": tf.get("state"),
        "composite": round(float(tf.get("composite_score", 0.0) or 0.0), 2),
        "m": round(float(tf.get("momentum_score", 0.0) or 0.0), 2),
        "p": round(float(tf.get("participation_score", 0.0) or 0.0), 2),
        "e": round(float(tf.get("exhaustion_score", 0.0) or 0.0), 2),
        "age_min": int(tf.get("state_age_min", 0) or 0),
        "confirmed": int(tf.get("confirmed_ticks", 0) or 0),
        "triggers": tf.get("triggers") or [],
        "sub": _compact_sub(tf.get("sub_scores")),
    }


def _compact_key_levels(kl_dict: Optional[dict], current_price: float) -> Optional[dict]:
    """从 KeyLevelSnapshotV2 提取 AI 需要的精华：
    - S/A 级强支撑阻力（最多 3+3 档，按距离排序）
    - 牛熊分界线（判断 primary_trend 的硬锚）
    - 挤压带（突破方向预判）
    - 多周期级最强位
    - 结构摘要

    - 传入 None → 返回 None（表示上层根本没有 key_levels 模块）
    - 传入 {} / 无 levels → 返回空结构（表示 key_levels 存在但当前无强位）
    """
    if kl_dict is None:
        return None

    levels = kl_dict.get("levels") or []
    strong_resistances: list[dict] = []
    strong_supports: list[dict] = []

    for lv in levels:
        tier = lv.get("strength_tier", "C")
        if tier not in ("S", "A"):
            continue
        item = {
            "price": round(float(lv.get("price", 0.0)), 2),
            "tier": tier,
            "distance_pct": round(float(lv.get("distance_pct", 0.0)), 2),
            "state": lv.get("state", "idle"),
            "sources": lv.get("sources") or [],
            "source_count": int(lv.get("source_count", 0) or 0),
            "historical_validity": round(float(lv.get("historical_validity", 0.0) or 0.0), 2),
            "bounce_count": int(lv.get("bounce_count", 0) or 0),
            "pattern": lv.get("pattern_detected") or "",
            "final_score": round(float(lv.get("final_score", 0.0) or 0.0), 1),
            "note": (lv.get("note") or "")[:60],
            "timeframe": lv.get("timeframe") or "",
        }
        if lv.get("side") == "resistance":
            strong_resistances.append(item)
        elif lv.get("side") == "support":
            strong_supports.append(item)

    # 按距离排序（最近的排前面）
    strong_resistances.sort(key=lambda x: abs(x["distance_pct"]))
    strong_supports.sort(key=lambda x: abs(x["distance_pct"]))

    # 截断：最多 3 档 + 3 档
    strong_resistances = strong_resistances[:3]
    strong_supports = strong_supports[:3]

    bbl = kl_dict.get("bull_bear_line") or {}
    bull_bear_line = None
    if bbl:
        bull_bear_line = {
            "regime": bbl.get("current_regime", ""),
            "reason": (bbl.get("regime_reason") or "")[:100],
            "sma200d": bbl.get("sma200d"),
            "cloud_top": bbl.get("ichimoku_cloud_top"),
            "cloud_bottom": bbl.get("ichimoku_cloud_bottom"),
        }

    bz = kl_dict.get("breakout_zone") or {}
    breakout_zone = None
    if bz and (bz.get("bb_squeeze") or bz.get("squeeze_direction")):
        breakout_zone = {
            "squeeze": bool(bz.get("bb_squeeze", False)),
            "direction": bz.get("squeeze_direction", ""),
            "bb_upper": bz.get("bb_upper"),
            "bb_lower": bz.get("bb_lower"),
            "note": (bz.get("note") or "")[:100],
        }

    return {
        "current_price": round(current_price, 2) if current_price else None,
        "strong_resistances": strong_resistances,
        "strong_supports": strong_supports,
        "bull_bear_line": bull_bear_line,
        "breakout_zone": breakout_zone,
        "daily_strong_support": kl_dict.get("daily_strong_support"),
        "daily_strong_resistance": kl_dict.get("daily_strong_resistance"),
        "weekly_strong_support": kl_dict.get("weekly_strong_support"),
        "weekly_strong_resistance": kl_dict.get("weekly_strong_resistance"),
        "structure_summary": (kl_dict.get("structure_summary") or "")[:120],
        "nearest_strong_support": kl_dict.get("nearest_strong_support"),
        "nearest_strong_resistance": kl_dict.get("nearest_strong_resistance"),
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Prompt 工程（核心：把 AI 从"翻译器"提升为"审计员 + 再判断者"）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_SYSTEM_PROMPT = """你是 LIQ 项目的资深加密货币量化交易顾问（10+ 年实盘经验），定位是**规则引擎的审计员 + 独立判断者**。

## 关键定位（请务必理解）
规则引擎只是**初步整理**了当前这一刻的数据——它给出 sub_scores 分值 + note + 一个机械算出的 `overall_direction / state / action`。但规则算法存在**短视**的天然缺陷（例如近几根 K 线跌了就会说 direction=down，即便宏观仍在上涨趋势）。

**你的任务不是翻译规则结论，而是拿原始读数 + 关键位数据再判一遍。**
- 规则的 `overall_direction / overall_state / overall_action` 是**候选结论**，不是最终答案。
- 你有权**推翻**规则的方向判断，前提是你能从 sub 的 note 和 key_levels 中找到**具体数值证据**。
- 你必须在 `alignment_reason` 和 `conflict_resolution` 中**引用具体数值**（如 "RSI=65.5 偏强 + 价距 EMA20 +3.4σ + FVG 多1空0"），不准只说"感觉矛盾"。

## 你的五项输出任务
1. **趋势评估** (`trend_assessment`)：综合多周期 + 结构 + 动能，独立判断当前**真实**趋势 + 动能健康度
2. **关键位投射** (`level_projection`)：基于动能 + 结构共振，评估能否突破最近 S/A 级关键位
3. **场景识别 + 矛盾消解** (`scenario` + `conflict_resolution`)：识别市场结构 + 挖出 sub 暗中对抗 direction 的情况
4. **交易倾向** (`trade_bias`)：若条件足够，给出方向 + 入场区 + 失效位 + 时间窗（可选；条件不足就 direction="neutral"）
5. **独立观察** (`independent_view`)：规则没覆盖但你注意到的关键点（可留空字符串）

## 可推翻规则的 4 种合法情形
- 多数 sub 的 note 集体指向规则方向的**反面**（如 FVG 多1空0 + OI 踩踏做多 + CVD 资金在跟 ↔ direction=down）
- 规则 direction 只来自短期 1h，但 4h/1d 结构**完全相反**
- 价已远离 EMA20 ≥ 2σ 但**没有放量**——规则容易把"健康延伸"误当"衰竭"
- 挤压带 `breakout_zone.direction` 与规则方向相反，且动能已在转向

## 关键位使用指引（这是 AI 的独立优势）
你会收到 `key_levels` 对象，包含：
- `strong_resistances` / `strong_supports`：S/A 级强位（不可编造其他价位）
- `bull_bear_line.regime`（bull/bear/neutral）：**宏观趋势硬锚**，权重高于规则的 overall_direction
- `breakout_zone`：挤压带预示的突破方向
- `daily/weekly_strong_support/resistance`：多周期级最强位

**使用规则**：
- `level_projection.target_level` **必须**引用 `key_levels` 中已有的价位，不准编造"76800 附近"这种模糊数。
- `level_projection.break_likelihood` 分档：
    - `very_likely` (>75% 自信)：动能充足 + 多周期共振 + 历史验证低阻力
    - `likely` (55-75%)：动能足够，存在 1-2 个反向因子
    - `uncertain` (45-55%)：证据对冲
    - `unlikely` (25-45%)：动能不足或遇到高强度位
    - `very_unlikely` (<25%)：遇到 S 级 + 历史验证强的位，且动能已衰竭
    - `insufficient`：key_levels 为空或距离过远（>5%）
- 若 `strong_resistances` 和 `strong_supports` **都为空**：`direction_tested = "none"`，`break_likelihood = "insufficient"`
- `if_break_cn` 要指向**下一档 key_level**（如 `strong_resistances[1]` 或 `daily_strong_resistance`）

## trade_bias 判定指引
- 只有当 `confidence >= 0.55` 且**方向明确**时给出非 neutral 的 direction
- `invalidation_cn` 必须引用 key_levels 中的反向关键位（如 "跌破 74973（S 级强支撑）"）
- 若遇到 regime_vetoed=true 的震荡/极端行情：**强制** direction="avoid"
- 若规则 + AI 判断分歧严重：direction="neutral"，让用户看观察点

## 严格禁止
- ❌ 不要引用任何历史价格 / 历史走势 / 历史表现（你没有这些数据，说了就是幻觉）
- ❌ 不要编造不在 `key_levels` 里的价位（违反会被测试层拒绝）
- ❌ 不要用 "总体来看……" "综合判断……" 这种废话起手
- ❌ 字段里禁止出现 Markdown 符号（# * - 等），纯文本即可
- ❌ 不准直接把规则的 `overall_plain_cn` 原文复制进你的 `summary_cn`

## 输出格式（必须且只能返回合法 JSON，顶层大括号，不加 ```json 包裹）
{
  "summary_cn": "一句话讲清当前真实场景（≤60 字，不要和 rules 原文雷同）",
  "scenario": "trend_continuation | bear_rebound | bull_pullback | reversal_early | reversal_confirmed | choppy_range | unclear",
  "trend_assessment": {
    "primary_trend": "uptrend | downtrend | sideways | transition",
    "momentum_quality": "fuel_full | fuel_adequate | fuel_fading | fuel_exhausted | unclear",
    "momentum_direction": "accelerating | stable | decelerating | unclear",
    "health_summary_cn": "一句话讲趋势+动能（≤40 字）",
    "evidence_cn": "引用具体数值证据（如 RSI=65.5 + 价距 EMA20 +3.4σ + FVG 多1空0）"
  },
  "level_projection": {
    "target_level": 76948.0,
    "direction_tested": "resistance | support | both | none",
    "break_likelihood": "very_likely | likely | uncertain | unlikely | very_unlikely | insufficient",
    "break_conviction": 0.0~1.0 浮点,
    "reasoning_cn": "为什么这么判（≤50 字）",
    "if_break_cn": "突破后下一档（引用 key_levels 中的价位）",
    "if_fail_cn": "失守后回测哪里（引用 key_levels 中的价位）"
  },
  "trade_bias": {
    "direction": "long | short | neutral | avoid",
    "strength": "probe | standard | strong | none",
    "entry_zone_cn": "入场区域（≤30 字，neutral/none 时留空）",
    "invalidation_cn": "失效位（引用 key_levels 中的反向强位）",
    "timeframe_cn": "时间窗（如 2-6 小时）",
    "why_cn": "一句理由（≤50 字）"
  },
  "conflict_resolution": "各周期/因子的冲突如何解释，引用具体数值（2-3 句）。无冲突时留空字符串",
  "traps": ["陷阱 1", "陷阱 2"],
  "triggers_to_watch": ["等 X 翻正", "等 Y 跌破"],
  "independent_view": "规则没说但你注意到的关键点（可留空）",
  "action_suggestion": "一句综合行动建议（≤150 字，可含方向+区间+时间）",
  "confidence": 0.0~1.0 浮点,
  "alignment_with_rules": "agree | partial_disagree | strong_disagree | neutral | insufficient",
  "alignment_reason": "必须引用具体数值解释对齐/分歧原因（≤80 字）"
}

## 对齐度 alignment_with_rules 判定标准
- `agree`：你同意规则的 direction + state + action
- `partial_disagree`：方向认同但时机/力度 AI 有补充
- `strong_disagree`：你认为规则的 direction 或 state **判错了**（必须有 ≥2 条数值反证）
- `neutral`：你既不赞成也不反对规则，给出独立视角（如规则说 down，AI 说 sideways）
- `insufficient`：数据不够下判，如实标这个，不要硬猜

## 置信度 confidence 标准
- 0.85-1.0：所有周期 + 所有因子强共振 + key_levels 方向一致，非常确定
- 0.6-0.85：主要证据一致，少数因子冲突可解释
- 0.4-0.6：有明显矛盾，你能给出最可能场景但不敢拍胸脯
- < 0.4：证据太乱，应当 alignment_with_rules=insufficient
"""


def _build_user_prompt(
    coin: str,
    signal_dict: dict,
    price: float,
    atr: float,
    key_levels: Optional[dict] = None,
) -> str:
    """把信号 + 关键位压成 AI 能消化的结构化上下文。"""
    payload = {
        "coin": coin,
        "price_now": round(price, 4) if price else None,
        "atr_1h": round(atr, 4) if atr else None,
        "rules_verdict_candidate": {  # 改名强调这是"候选"而非"最终"
            "overall_state": signal_dict.get("overall_state"),
            "overall_direction": signal_dict.get("overall_direction"),
            "overall_action": signal_dict.get("overall_action"),
            "overall_position_pct": signal_dict.get("overall_position_pct"),
            "consensus_level": signal_dict.get("consensus_level"),
            "regime": signal_dict.get("regime"),
            "regime_vetoed": signal_dict.get("regime_vetoed"),
            "overall_plain_cn": signal_dict.get("overall_plain_cn"),
            "overall_tip_cn": signal_dict.get("overall_tip_cn"),
            "overall_reason_cn": signal_dict.get("overall_reason_cn"),
            "data_quality": signal_dict.get("data_quality"),
            "missing_inputs": signal_dict.get("missing_inputs") or [],
        },
        "tf": {
            "1h": _compact_tf(signal_dict.get("tf_1h")),
            "4h": _compact_tf(signal_dict.get("tf_4h")),
            "1d": _compact_tf(signal_dict.get("tf_1d")),
        },
        "key_levels": key_levels,  # 可能为 None，AI 会降级处理
    }
    body = json.dumps(payload, ensure_ascii=False, indent=2)
    return (
        f"以下是 {coin} 当前时刻的**原始读数**。按 system 里的定位，你是审计员而非翻译器。\n\n"
        f"## 关键字段解读（再强调一遍）\n"
        f"- `rules_verdict_candidate` 只是规则**初步整理**，你有权基于下方 sub 的 note + key_levels 推翻它。\n"
        f"- `tf.*.sub` 列表中每项 score 的符号是『相对当前 direction』的：\n"
        f"  + 表示『支持当前 direction 方向续航』，- 表示『反对续航 / 衰竭信号』。\n"
        f"  如果 direction=down 但多数 sub 的 note 暗示看涨（负 score），这就是**反转早期**或**规则判错**的线索。\n"
        f"- `key_levels` 是 AI 的独立优势来源。你必须用其中的具体价位做 `level_projection.target_level`，不准编造。\n\n"
        f"## 数据\n```json\n{body}\n```"
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 输出解析（支持新的子结构 + 白名单校验 + 价位合法性）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_SCENARIO_WHITELIST = {
    "trend_continuation", "bear_rebound", "bull_pullback",
    "reversal_early", "reversal_confirmed", "choppy_range", "unclear",
}
_ALIGN_WHITELIST = {
    "agree", "partial_disagree", "strong_disagree", "neutral", "insufficient",
}
_PRIMARY_TREND_WHITELIST = {"uptrend", "downtrend", "sideways", "transition"}
_MOMENTUM_QUALITY_WHITELIST = {
    "fuel_full", "fuel_adequate", "fuel_fading", "fuel_exhausted", "unclear",
}
_MOMENTUM_DIRECTION_WHITELIST = {
    "accelerating", "stable", "decelerating", "unclear",
}
_BREAK_LIKELIHOOD_WHITELIST = {
    "very_likely", "likely", "uncertain", "unlikely", "very_unlikely", "insufficient",
}
_DIRECTION_TESTED_WHITELIST = {"resistance", "support", "both", "none"}
_TRADE_DIRECTION_WHITELIST = {"long", "short", "neutral", "avoid"}
_TRADE_STRENGTH_WHITELIST = {"probe", "standard", "strong", "none"}


def _extract_json(text: str) -> Optional[dict]:
    """稳健抽取 JSON。"""
    if not text:
        return None
    t = text.strip()
    if t.startswith("```"):
        t = t.split("```", 2)
        if len(t) >= 2:
            body = t[1]
            if body.startswith("json\n"):
                body = body[5:]
            elif body.startswith("json"):
                body = body[4:]
            t = body.strip()
        else:
            t = "".join(t)
    l = t.find("{")
    r = t.rfind("}")
    if l < 0 or r < 0 or r <= l:
        return None
    candidate = t[l : r + 1]
    try:
        return json.loads(candidate)
    except Exception:
        try:
            import re
            cleaned = re.sub(r",(\s*[}\]])", r"\1", candidate)
            return json.loads(cleaned)
        except Exception:
            return None


def _clamp_float(val, lo: float = 0.0, hi: float = 1.0, default: float = 0.0) -> float:
    try:
        f = float(val)
        return max(lo, min(hi, f))
    except Exception:
        return default


def _normalize_whitelist(val, whitelist: set, default: str) -> str:
    s = str(val or "").strip()
    return s if s in whitelist else default


def _parse_trend_assessment(data: dict) -> Optional[dict]:
    ta = data.get("trend_assessment")
    if not isinstance(ta, dict):
        return None
    return {
        "primary_trend": _normalize_whitelist(
            ta.get("primary_trend"), _PRIMARY_TREND_WHITELIST, "transition"
        ),
        "momentum_quality": _normalize_whitelist(
            ta.get("momentum_quality"), _MOMENTUM_QUALITY_WHITELIST, "unclear"
        ),
        "momentum_direction": _normalize_whitelist(
            ta.get("momentum_direction"), _MOMENTUM_DIRECTION_WHITELIST, "unclear"
        ),
        "health_summary_cn": str(ta.get("health_summary_cn", ""))[:120],
        "evidence_cn": str(ta.get("evidence_cn", ""))[:200],
    }


def _parse_level_projection(data: dict, allowed_prices: set) -> Optional[dict]:
    """解析 level_projection。若 target_level 不在 allowed_prices 中，降级为 None。"""
    lp = data.get("level_projection")
    if not isinstance(lp, dict):
        return None
    target_raw = lp.get("target_level")
    target: Optional[float] = None
    if target_raw is not None:
        try:
            cand = float(target_raw)
            # AI 必须精确引用 key_levels 中的价位，只允许 ≤ 0.05% 的小数误差吸附
            # （容忍 $74,577.23 被复制为 74577.00 这种场景）
            if allowed_prices:
                best = min(allowed_prices, key=lambda p: abs(p - cand))
                if abs(best - cand) / max(abs(best), 1e-9) <= 0.0005:
                    target = round(best, 2)
                # 否则 AI 编造了价位 → 降级为 None（下方会强制 direction_tested=none）
            else:
                # 没有 key_levels 约束时才直接采纳 AI 给的价（兼容旧逻辑）
                target = round(cand, 2)
        except Exception:
            target = None
    direction_tested = _normalize_whitelist(
        lp.get("direction_tested"), _DIRECTION_TESTED_WHITELIST, "none"
    )
    break_likelihood = _normalize_whitelist(
        lp.get("break_likelihood"), _BREAK_LIKELIHOOD_WHITELIST, "insufficient"
    )
    # 若 target_level 无效 → 强制降级
    if target is None:
        direction_tested = "none"
        break_likelihood = "insufficient"
    return {
        "target_level": target,
        "direction_tested": direction_tested,
        "break_likelihood": break_likelihood,
        "break_conviction": _clamp_float(lp.get("break_conviction"), 0.0, 1.0, 0.0),
        "reasoning_cn": str(lp.get("reasoning_cn", ""))[:200],
        "if_break_cn": str(lp.get("if_break_cn", ""))[:150],
        "if_fail_cn": str(lp.get("if_fail_cn", ""))[:150],
    }


def _parse_trade_bias(data: dict) -> Optional[dict]:
    tb = data.get("trade_bias")
    if not isinstance(tb, dict):
        return None
    return {
        "direction": _normalize_whitelist(
            tb.get("direction"), _TRADE_DIRECTION_WHITELIST, "neutral"
        ),
        "strength": _normalize_whitelist(
            tb.get("strength"), _TRADE_STRENGTH_WHITELIST, "none"
        ),
        "entry_zone_cn": str(tb.get("entry_zone_cn", ""))[:100],
        "invalidation_cn": str(tb.get("invalidation_cn", ""))[:120],
        "timeframe_cn": str(tb.get("timeframe_cn", ""))[:60],
        "why_cn": str(tb.get("why_cn", ""))[:150],
    }


def _collect_allowed_prices(key_levels: Optional[dict]) -> set:
    """收集 AI 允许引用的价位集合（S/A 级强位 + 多周期级位）。"""
    if not key_levels:
        return set()
    prices: set = set()
    for group in ("strong_resistances", "strong_supports"):
        for lv in key_levels.get(group) or []:
            try:
                prices.add(round(float(lv.get("price", 0.0)), 2))
            except Exception:
                pass
    # 多周期级位（字符串形式）
    for k in (
        "daily_strong_support", "daily_strong_resistance",
        "weekly_strong_support", "weekly_strong_resistance",
        "nearest_strong_support", "nearest_strong_resistance",
    ):
        val = key_levels.get(k)
        if val is None:
            continue
        try:
            # 可能是 "$78,207.01" 格式
            if isinstance(val, str):
                clean = val.replace("$", "").replace(",", "").strip()
                if clean:
                    prices.add(round(float(clean), 2))
            else:
                prices.add(round(float(val), 2))
        except Exception:
            pass
    return prices


def _parse_ai_json(
    raw_text: str,
    reasoning: str,
    key_levels: Optional[dict] = None,
) -> tuple[dict, Optional[str]]:
    """返回 (解析到的 dict, 错误描述)。"""
    data = _extract_json(raw_text)
    if data is None:
        return {}, "AI 返回无法解析为 JSON"

    allowed_prices = _collect_allowed_prices(key_levels)

    scenario = _normalize_whitelist(
        data.get("scenario"), _SCENARIO_WHITELIST, "unclear"
    )
    align = _normalize_whitelist(
        data.get("alignment_with_rules"), _ALIGN_WHITELIST, "insufficient"
    )
    conf = _clamp_float(data.get("confidence"), 0.0, 1.0, 0.0)

    traps = data.get("traps") or []
    if not isinstance(traps, list):
        traps = [str(traps)]
    traps = [str(x)[:240] for x in traps][:6]

    triggers = data.get("triggers_to_watch") or []
    if not isinstance(triggers, list):
        triggers = [str(triggers)]
    triggers = [str(x)[:240] for x in triggers][:6]

    return {
        "summary_cn": str(data.get("summary_cn", ""))[:200],
        "scenario": scenario,
        "trend_assessment": _parse_trend_assessment(data),
        "level_projection": _parse_level_projection(data, allowed_prices),
        "trade_bias": _parse_trade_bias(data),
        "conflict_resolution": str(data.get("conflict_resolution", ""))[:500],
        "traps": traps,
        "triggers_to_watch": triggers,
        "independent_view": str(data.get("independent_view", ""))[:400],
        "action_suggestion": str(data.get("action_suggestion", ""))[:400],
        "confidence": conf,
        "alignment_with_rules": align,
        "alignment_reason": str(data.get("alignment_reason", ""))[:240],
    }, None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Interpreter 类
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TEInterpreter:
    """单例型 AI 解读器。"""

    def __init__(self):
        cfg = get_settings().ai
        self._api_key = cfg.api_key
        self._api_base = cfg.api_base
        cfg_model = (cfg.model or "").lower()
        self._model = cfg.model if "reasoner" in cfg_model else "deepseek-reasoner"
        self._client: Optional[AsyncOpenAI] = None
        if self._api_key:
            kwargs: dict = {"api_key": self._api_key}
            if self._api_base:
                kwargs["base_url"] = self._api_base
            self._client = AsyncOpenAI(**kwargs)

        self._cache: dict[str, _CacheEntry] = {}
        self._lock = asyncio.Lock()
        self._inflight: dict[str, asyncio.Event] = {}
        logger.info(
            "TEInterpreter init | model=%s base=%s key_set=%s client_ok=%s",
            self._model, self._api_base or "(default)",
            bool(self._api_key), self._client is not None,
        )

    @property
    def available(self) -> bool:
        return self._client is not None

    # ── public helpers（给 routes.py / ws.py 调度用） ──────────
    def compute_fingerprint(
        self,
        coin: str,
        signal_dict: dict,
        key_levels_dict: Optional[dict] = None,
    ) -> str:
        return _fingerprint(coin, signal_dict, key_levels_dict)

    def peek_cache(self, fp: str) -> Optional[TEAIInterpretation]:
        return self._get_cached(fp)

    def is_inflight(self, fp: str) -> bool:
        ev = self._inflight.get(fp)
        return ev is not None and not ev.is_set()

    def _get_cached(self, fp: str) -> Optional[TEAIInterpretation]:
        entry = self._cache.get(fp)
        if entry is None:
            return None
        if time.time() >= entry.expires_at:
            self._cache.pop(fp, None)
            return None
        dup = entry.result.model_copy(update={
            "cache_hit": True,
            "from_cache_age_sec": int(_CACHE_TTL_SEC - (entry.expires_at - time.time())),
        })
        return dup

    def _put_cache(self, fp: str, result: TEAIInterpretation) -> None:
        if len(self._cache) >= _CACHE_MAX_ENTRIES:
            olds = sorted(self._cache.items(), key=lambda kv: kv[1].expires_at)
            for k, _ in olds[:20]:
                self._cache.pop(k, None)
        self._cache[fp] = _CacheEntry(
            result=result, expires_at=time.time() + _CACHE_TTL_SEC,
        )

    async def interpret(
        self,
        coin: str,
        signal_dict: dict,
        price: float,
        atr: float = 0.0,
        key_levels_dict: Optional[dict] = None,
        force: bool = False,
    ) -> TEAIInterpretation:
        """主入口。

        Args:
            coin: 大写币种
            signal_dict: TrendExhaustionSignal.model_dump()
            price: 当前 ticker 价格
            atr: 1h ATR14（可选）
            key_levels_dict: KeyLevelSnapshotV2.model_dump()（可选，AI 的独立优势源）
            force: True 则绕过缓存强制重算
        """
        # 先压缩 key_levels（避免每次调用都重算）
        compact_kl = _compact_key_levels(key_levels_dict, price)
        fp = _fingerprint(coin, signal_dict, key_levels_dict)

        if not force:
            cached = self._get_cached(fp)
            if cached is not None:
                return cached

        if not self._client:
            return TEAIInterpretation(
                coin=coin, ts=int(time.time()), signal_fingerprint=fp,
                error="AI 未配置 API Key",
                alignment_with_rules="insufficient",
            )

        # 防并发重复调用
        event = self._inflight.get(fp)
        if event is not None and not force:
            try:
                await asyncio.wait_for(event.wait(), timeout=_DEFAULT_TIMEOUT_SEC + 5)
            except asyncio.TimeoutError:
                pass
            cached = self._get_cached(fp)
            if cached is not None:
                return cached

        event = asyncio.Event()
        self._inflight[fp] = event
        try:
            result = await self._do_call(
                coin, signal_dict, price, atr, compact_kl, fp,
            )
            if result.error is None:
                self._put_cache(fp, result)
            return result
        finally:
            event.set()
            if self._inflight.get(fp) is event:
                self._inflight.pop(fp, None)

    async def _do_call(
        self,
        coin: str,
        signal_dict: dict,
        price: float,
        atr: float,
        compact_kl: Optional[dict],
        fp: str,
    ) -> TEAIInterpretation:
        system = _SYSTEM_PROMPT
        user = _build_user_prompt(coin, signal_dict, price, atr, compact_kl)
        t0 = time.time()
        raw_text = ""
        reasoning = ""
        tokens_in = 0
        tokens_out = 0
        r_tok = 0
        try:
            api_kwargs: dict = {
                "model": self._model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "timeout": _DEFAULT_TIMEOUT_SEC,
            }
            is_reasoner = "reasoner" in self._model.lower()
            if not is_reasoner:
                api_kwargs["temperature"] = 0.2
                api_kwargs["response_format"] = {"type": "json_object"}

            logger.info(
                "[TE-AI] call start | coin=%s fp=%s model=%s reasoner=%s kl=%s",
                coin, fp, self._model, is_reasoner,
                f"S/A={len((compact_kl or {}).get('strong_resistances') or []) + len((compact_kl or {}).get('strong_supports') or [])}"
                if compact_kl else "none",
            )
            response = await self._client.chat.completions.create(**api_kwargs)
            msg = response.choices[0].message
            raw_text = msg.content or ""
            reasoning = getattr(msg, "reasoning_content", None) or ""
            if response.usage:
                tokens_in = response.usage.prompt_tokens or 0
                tokens_out = response.usage.completion_tokens or 0
                details = getattr(response.usage, "completion_tokens_details", None)
                if details:
                    r_tok = getattr(details, "reasoning_tokens", 0) or 0
            elapsed = time.time() - t0
            logger.info(
                "[TE-AI] call done | coin=%s fp=%s %.1fs | tokens_in=%d out=%d r=%d | resp=%d chars reasoning=%d chars",
                coin, fp, elapsed, tokens_in, tokens_out, r_tok,
                len(raw_text), len(reasoning),
            )
        except Exception as e:
            elapsed = time.time() - t0
            logger.warning(
                "[TE-AI] call failed | coin=%s fp=%s %.1fs | err=%s",
                coin, fp, elapsed, e,
            )
            return TEAIInterpretation(
                coin=coin, ts=int(time.time()), signal_fingerprint=fp,
                model=self._model, latency_ms=int(elapsed * 1000),
                error=f"AI 调用失败：{type(e).__name__}: {str(e)[:200]}",
                alignment_with_rules="insufficient",
            )

        # 解析（带 key_levels 价位白名单校验）
        parsed, parse_err = _parse_ai_json(raw_text, reasoning, compact_kl)
        reasoning_store = reasoning[:_MAX_REASONING_STORE_CHARS]
        if parse_err:
            return TEAIInterpretation(
                coin=coin, ts=int(time.time()), signal_fingerprint=fp,
                model=self._model, latency_ms=int((time.time() - t0) * 1000),
                tokens_in=tokens_in, tokens_out=tokens_out, reasoning_tokens=r_tok,
                reasoning=reasoning_store, raw_text=raw_text[:2000],
                error=parse_err,
                alignment_with_rules="insufficient",
            )

        return TEAIInterpretation(
            coin=coin, ts=int(time.time()), signal_fingerprint=fp,
            model=self._model, latency_ms=int((time.time() - t0) * 1000),
            tokens_in=tokens_in, tokens_out=tokens_out, reasoning_tokens=r_tok,
            reasoning=reasoning_store, raw_text="",
            **parsed,
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 单例
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
_singleton: Optional[TEInterpreter] = None


def get_te_interpreter() -> TEInterpreter:
    global _singleton
    if _singleton is None:
        _singleton = TEInterpreter()
    return _singleton
