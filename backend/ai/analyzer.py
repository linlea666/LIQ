"""AI 分析器：组装快照 → 调用 LLM → 解析输出"""

from __future__ import annotations

import json
import logging
import re
from typing import Optional
import time
import traceback

from openai import AsyncOpenAI

from ai.prompts import (
    build_data_snapshot_prompt,
    build_system_prompt,
    build_user_prompt,
)
from config.settings import get_settings
from models.snapshot import AIAnalysisResult, AISnapshot, SignalSummary, SniperPlan, TradingPlanEntry

logger = logging.getLogger(__name__)


class AIAnalyzer:
    def __init__(self):
        cfg = get_settings().ai
        self._model = cfg.model
        self._timeout = cfg.timeout_sec
        self._max_retries = cfg.max_retries
        self._client: Optional[AsyncOpenAI] = None

        if cfg.api_key:
            kwargs: dict = {"api_key": cfg.api_key}
            if cfg.api_base:
                kwargs["base_url"] = cfg.api_base
            self._client = AsyncOpenAI(**kwargs)

        logger.info(
            "AIAnalyzer init | provider=%s model=%s base=%s timeout=%ds "
            "retries=%d key_set=%s client_ok=%s",
            cfg.active, cfg.model, cfg.api_base or "(default)",
            cfg.timeout_sec, cfg.max_retries,
            bool(cfg.api_key), self._client is not None,
        )

    @property
    def available(self) -> bool:
        return self._client is not None

    async def analyze(self, snapshot: AISnapshot) -> AIAnalysisResult:
        """执行 AI 分析：snapshot → prompt → LLM → 结构化结果"""
        if not self._client:
            raise RuntimeError("AI API key not configured")

        snapshot_dict = snapshot.model_dump()
        user_prompt = build_user_prompt(snapshot_dict)

        system_prompt = build_system_prompt()
        # 2026-04-22 · 跨模型对照切片：生成与 user_prompt 同源但已剥除
        # 规则侧结论 / 指令 / 输出格式要求的纯数据版，供前端"AI 交互过程原文"
        # 卡片第四项展示，让用户可一键复制到其他 AI 做独立方向判断。
        # 本生成是旁路（不影响主链路 API 调用），失败也不影响 AI 分析本身。
        try:
            data_snapshot_prompt = build_data_snapshot_prompt(snapshot_dict)
        except Exception as e:
            logger.warning(
                "build_data_snapshot_prompt failed (non-fatal) | coin=%s err=%s",
                snapshot.coin, e,
            )
            data_snapshot_prompt = ""

        logger.info(
            "AI analysis started | coin=%s price=%.2f | "
            "system_prompt=%d chars | user_prompt=%d chars | "
            "data_snapshot=%d chars | model=%s timeout=%ds",
            snapshot.coin, snapshot.price,
            len(system_prompt), len(user_prompt), len(data_snapshot_prompt),
            self._model, self._timeout,
        )

        is_reasoner = "reasoner" in self._model.lower()

        raw_text = ""
        for attempt in range(1, self._max_retries + 1):
            try:
                logger.info(
                    "AI API call attempt %d/%d | coin=%s model=%s reasoner=%s",
                    attempt, self._max_retries, snapshot.coin, self._model, is_reasoner,
                )
                t0 = time.time()

                api_kwargs: dict = {
                    "model": self._model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    "timeout": self._timeout,
                }
                if not is_reasoner:
                    api_kwargs["temperature"] = 0.3

                response = await self._client.chat.completions.create(**api_kwargs)
                elapsed = time.time() - t0

                msg = response.choices[0].message
                raw_text = msg.content or ""
                tokens_in = response.usage.prompt_tokens if response.usage else 0
                tokens_out = response.usage.completion_tokens if response.usage else 0

                reasoning = getattr(msg, "reasoning_content", None) or ""
                reasoning_tokens = getattr(
                    response.usage, "completion_tokens_details", None
                )
                r_tok = (
                    getattr(reasoning_tokens, "reasoning_tokens", 0)
                    if reasoning_tokens else 0
                )

                logger.info(
                    "AI API call success | coin=%s | %.1fs | "
                    "tokens_in=%d out=%d reasoning_tok=%d | "
                    "response_len=%d reasoning_len=%d chars",
                    snapshot.coin, elapsed, tokens_in, tokens_out, r_tok,
                    len(raw_text), len(reasoning),
                )
                if reasoning:
                    logger.debug(
                        "AI reasoning chain | coin=%s | %.200s...",
                        snapshot.coin, reasoning[:200],
                    )
                break

            except Exception as e:
                elapsed = time.time() - t0
                err_type = type(e).__name__
                logger.warning(
                    "AI API call attempt %d/%d failed | coin=%s | %.1fs | "
                    "err_type=%s err=%s",
                    attempt, self._max_retries, snapshot.coin, elapsed,
                    err_type, str(e),
                )
                if attempt == self._max_retries:
                    logger.error(
                        "AI analysis exhausted all retries | coin=%s | "
                        "err_type=%s\n%s",
                        snapshot.coin, err_type, traceback.format_exc(),
                    )
                    raise

        result = _parse_ai_output(raw_text, snapshot, user_prompt, system_prompt)
        result.data_snapshot_prompt = data_snapshot_prompt
        return result


def _parse_ai_output(raw_text: str, snapshot: AISnapshot, user_prompt: str = "", system_prompt: str = "") -> AIAnalysisResult:
    """
    解析 AI 输出文本为结构化结果。
    即使解析部分失败，也保留 raw_text 作为降级展示。
    使用模糊匹配 section headers，兼容 AI 输出的细微格式差异。
    """
    sections: dict[str, str] = {}
    current_section = ""
    current_lines: list[str] = []

    for line in raw_text.split("\n"):
        stripped = line.strip()
        if stripped.startswith("##") and not stripped.startswith("###"):
            if current_section:
                sections[current_section] = "\n".join(current_lines).strip()
            current_section = stripped.lstrip("#").strip()
            current_lines = []
        else:
            current_lines.append(line)

    if current_section:
        sections[current_section] = "\n".join(current_lines).strip()

    def _find_section(*keywords: str) -> str:
        for key, val in sections.items():
            for kw in keywords:
                if kw in key:
                    return val
        return ""

    def _find_sniper_section() -> str:
        for key, val in sections.items():
            if "狙击" in key:
                return val
        return ""

    def _find_ladder_section() -> str:
        for key, val in sections.items():
            if "阶梯" in key or "多层" in key:
                return val
        return ""

    signal = _parse_signal_summary(_find_section("一句话结论", "结论", "Summary"))
    if signal is None:
        overview_text = _find_section("格局", "总览", "Overview")
        signal = _parse_signal_summary_from_overview(overview_text)
    if signal is None:
        signal = _parse_signal_summary_from_overview(raw_text)

    sniper_text = _find_sniper_section()
    trading_plan_text = _find_section("交易计划", "Trading Plan")

    trading_plan_entries = _parse_trading_plan_entries(trading_plan_text)
    if not trading_plan_entries and sniper_text:
        for sp in _parse_sniper_plans(sniper_text):
            trading_plan_entries.append(TradingPlanEntry(
                tier="short", direction=sp.direction, entry=sp.entry,
                stop_loss=sp.stop_loss, tp1=sp.tp1, tp2=sp.tp2,
                rr=sp.rr, source="engine", logic=sp.logic,
            ))

    # P1.7：提取附录中的结构化 JSON（若模型按新 prompt 产出）
    matrix_json = _extract_matrix_json(raw_text)

    return AIAnalysisResult(
        coin=snapshot.coin,
        ts=int(time.time()),
        price_at_analysis=snapshot.price,
        signal_summary=signal,
        market_overview=_find_section("格局", "总览", "Overview"),
        key_levels=_parse_levels_table(_find_section("价位", "图谱", "Level")),
        stop_loss_suggestion={"raw": _find_section("止损", "Stop")},
        entry_zones=_parse_entry_zones(_find_section("入场", "观察区", "Entry")),
        sniper_setup=sniper_text,
        sniper_plans=_parse_sniper_plans(sniper_text),
        ladder_plan_text=_find_ladder_section(),
        trading_plan=trading_plan_text,
        trading_plan_entries=trading_plan_entries,
        risk_warnings=_parse_list(_find_section("风险提示", "Risk")),
        scenario_analysis=_parse_scenarios(_find_section("场景", "推演", "Scenario")),
        data_quality_feedback=_find_section("数据质量", "自检", "Data Quality"),
        raw_text=raw_text,
        user_prompt=user_prompt,
        system_prompt=system_prompt,
        ai_matrix_json=matrix_json,
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# P1.7 · AITRADER_MATRIX_JSON 提取
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# 匹配 ```AITRADER_MATRIX_JSON ... ``` 代码块
# 宽容处理：标签大小写不敏感、允许空白和换行、允许 json/JSON 作为语言标记兜底
_MATRIX_BLOCK_PATTERNS = [
    re.compile(
        r"```\s*AITRADER[_\s-]*MATRIX[_\s-]*JSON\s*\n(.*?)\n```",
        re.DOTALL | re.IGNORECASE,
    ),
    # 兜底：若模型忘记标签，取最后一个 ```json ... ``` 块尝试解析
    re.compile(r"```json\s*\n(.*?)\n```", re.DOTALL | re.IGNORECASE),
]


def _extract_matrix_json(raw_text: str) -> Optional[dict]:
    """从 AI 原始输出中提取 AITRADER_MATRIX_JSON 附录。

    策略：
      1. 优先匹配 `AITRADER_MATRIX_JSON` 语言标签（强信号）
      2. 若无，尝试最后一个 `json` 标签块，且必须能解析为 dict 且含 "sections" 键
      3. 任何解析异常 → 返回 None（下游 builder 自动回退规则路径）

    不在此处做 schema 严格校验（留给 trader_report_builder 宽容处理），
    只保证"能 json.loads 成 dict"这条底线。
    """
    if not raw_text:
        return None

    for pat in _MATRIX_BLOCK_PATTERNS:
        matches = pat.findall(raw_text)
        for block in reversed(matches):  # 取最后一个（正文之后的附录）
            payload = _try_parse_json_block(block)
            if payload is None:
                continue
            # 兜底 json 块必须显式含 sections 才算 matrix，避免误认其他 json
            if pat is _MATRIX_BLOCK_PATTERNS[1] and "sections" not in payload:
                continue
            return payload
    return None


def _try_parse_json_block(block: str) -> Optional[dict]:
    """尝试把一个 JSON 代码块文本解析为 dict。

    容错处理：
      - 去除可能残留的行内注释 `// ...`
      - 去除尾随逗号 `, }` / `, ]`
      - 仅接受顶层为 dict 的 payload
    """
    text = (block or "").strip()
    if not text:
        return None

    # 去行内 // 注释（仅行首/空格后的 //，避免误伤 URL）
    text = re.sub(r"(^|\s)//[^\n]*", "", text)
    # 去尾随逗号
    text = re.sub(r",(\s*[}\]])", r"\1", text)

    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None

    if not isinstance(payload, dict):
        return None
    return payload


def _parse_signal_summary(text: str) -> SignalSummary | None:
    """解析 AI 一句话结论为结构化交易信号。

    预期格式: **看空（置信度：中）**——理由...
    也兼容变体: 偏多/偏空/中性偏多/中性偏空/中性/观望 等
    """
    if not text or not text.strip():
        return None
    raw = text.strip().split("\n")[0].strip()
    direction = ""
    confidence = ""
    reason = ""

    cleaned = raw.replace("**", "").replace("*", "").replace("📝", "").strip()

    _BULLISH = ("看多", "偏多", "做多", "多头", "bullish", "中性偏多")
    _BEARISH = ("看空", "偏空", "做空", "空头", "bearish", "中性偏空")
    _NEUTRAL = ("震荡", "中性", "观望", "盘整", "neutral", "区间")

    for kw in _BULLISH:
        if kw in cleaned:
            direction = "bullish"
            break
    if not direction:
        for kw in _BEARISH:
            if kw in cleaned:
                direction = "bearish"
                break
    if not direction:
        for kw in _NEUTRAL:
            if kw in cleaned:
                direction = "neutral"
                break

    conf_match = re.search(r"置信度[：:]\s*(高|中|低|high|medium|low)", cleaned, re.IGNORECASE)
    if conf_match:
        _map = {"高": "high", "中": "medium", "低": "low",
                "high": "high", "medium": "medium", "low": "low"}
        confidence = _map.get(conf_match.group(1).lower(), "medium")

    for sep in ("——", "—", "--", "：", ":"):
        idx = cleaned.find(sep)
        if idx >= 0:
            candidate = cleaned[idx + len(sep):].strip()
            if candidate and len(candidate) > 3:
                reason = candidate
                break

    if not direction:
        return None

    return SignalSummary(
        direction=direction,
        confidence=confidence or "medium",
        reason=reason[:100],
        raw_line=raw[:200],
    )


def _parse_signal_summary_from_overview(text: str) -> SignalSummary | None:
    """从市场格局总览的开头几行提取白话总结（fallback）。

    预期格式: 📝 **看多（置信度：高）**——理由...
    也兼容 AI 把白话总结放在前5行任意位置的情况。
    """
    if not text:
        return None
    _ALL_KEYWORDS = ("看多", "看空", "震荡", "偏多", "偏空",
                     "做多", "做空", "中性", "观望", "盘整", "📝")
    for line in text.strip().split("\n")[:5]:
        line = line.strip().lstrip("> ")
        if any(kw in line for kw in _ALL_KEYWORDS):
            result = _parse_signal_summary(line)
            if result:
                return result
    return None


def _parse_levels_table(text: str) -> list[dict]:
    """尝试从 markdown 表格解析价位"""
    levels: list[dict] = []
    for line in text.split("\n"):
        line = line.strip()
        if not line or line.startswith("|--") or line.startswith("| 类型") or ":---" in line or "---" == line.replace("|", "").replace(" ", "").replace("-", ""):
            continue
        if line.startswith("|"):
            parts = [p.strip() for p in line.strip("|").split("|")]
            if len(parts) >= 3:
                levels.append({
                    "type": parts[0],
                    "price": parts[1],
                    "strength": parts[2] if len(parts) > 2 else "",
                    "reason": parts[3] if len(parts) > 3 else "",
                })
    return levels


def _parse_entry_zones(text: str) -> list[dict]:
    zones: list[dict] = []
    current: dict = {}
    for line in text.split("\n"):
        line = line.strip()
        if "观察区" in line:
            if current:
                zones.append(current)
            direction = "long" if "多" in line else "short"
            current = {"direction": direction, "raw": line, "details": []}
        elif line.startswith("-") and current:
            current["details"].append(line.lstrip("- "))
    if current:
        zones.append(current)
    return zones


def _parse_list(text: str) -> list[str]:
    items: list[str] = []
    for line in text.split("\n"):
        line = line.strip()
        if line and (line.startswith("-") or line[0].isdigit()):
            items.append(line.lstrip("-0123456789. "))
    return items


def _parse_scenarios(text: str) -> list[dict]:
    scenarios: list[dict] = []
    current: dict = {}
    for line in text.split("\n"):
        line = line.strip()
        clean = line.lstrip("*#").strip()
        if clean.startswith("场景"):
            if current:
                scenarios.append(current)
            sep = "：" if "：" in clean else ":"
            parts = clean.split(sep, 1)
            label = parts[0].replace("**", "").strip()
            desc = parts[1].replace("**", "").strip() if len(parts) > 1 else ""
            current = {"label": label, "description": desc}
        elif clean.startswith("当前数据偏向"):
            if current:
                scenarios.append(current)
                current = {}
        elif line and current:
            current["description"] = current.get("description", "") + " " + line
    if current:
        scenarios.append(current)
    return scenarios


def _parse_trading_plan_entries(text: str) -> list[TradingPlanEntry]:
    """从新版"交易计划（三档结构）"章节提取回测可用的结构化条目。

    按档位（短线/中线/远线）分段，从 markdown 表格行或自由文本中提取
    方向、入场价、止损、止盈、R:R。
    """
    if not text or len(text) < 30:
        return []

    entries: list[TradingPlanEntry] = []
    current_tier = ""

    _TIER_MAP = {
        "短线": "short", "short": "short",
        "中线": "mid", "mid": "mid",
        "远线": "long", "long": "long",
    }

    def _detect_tier(line: str) -> str:
        lower = line.lower()
        for kw, tier in _TIER_MAP.items():
            if kw in lower:
                return tier
        return ""

    def _extract_price(pattern: str, block: str) -> Optional[float]:
        m = re.search(pattern, block, re.IGNORECASE)
        if m:
            raw = m.group(1).replace(",", "").replace("$", "").replace(" ", "").strip()
            try:
                return float(raw)
            except ValueError:
                pass
        return None

    def _detect_direction(text_block: str) -> str:
        lower = text_block[:100].lower()
        if any(kw in lower for kw in ("多单", "做多", "long", "多头")):
            return "long"
        if any(kw in lower for kw in ("空单", "做空", "short", "空头")):
            return "short"
        return ""

    def _detect_source(text_block: str) -> str:
        if any(kw in text_block for kw in ("AI推断", "⚡", "自主构建", "AI自主")):
            return "ai_inferred"
        return "engine"

    table_row_re = re.compile(
        r"\|\s*(?P<dir>[^|]+)\s*\|\s*\$?(?P<entry>[\d,.]+)\s*\|\s*\$?(?P<sl>[\d,.]+)\s*\|"
        r"\s*\$?(?P<tp1>[\d,.]+)\s*(?:\(.*?(?P<rr1>[\d.]+)\))?\s*\|"
        r"(?:\s*\$?(?P<tp2>[\d,.]+)\s*(?:\(.*?(?P<rr2>[\d.]+)\))?\s*\|)?"
    )

    _next_is_ai = False

    for line in text.split("\n"):
        stripped = line.strip()
        if not stripped:
            continue

        new_tier = _detect_tier(stripped)
        if new_tier and ("**" in stripped or "###" in stripped or "档" in stripped):
            current_tier = new_tier
            _next_is_ai = False
            continue

        if stripped.startswith("|") and "---" not in stripped and "方向" not in stripped and "类型" not in stripped:
            if ("AI推断" in stripped or "⚡" in stripped) and not re.search(r"\$[\d,]+", stripped):
                _next_is_ai = True
                continue
            m = table_row_re.match(stripped)
            if m:
                direction = _detect_direction(m.group("dir"))
                if not direction:
                    continue
                def _safe_float(s: str | None) -> Optional[float]:
                    if not s:
                        return None
                    try:
                        return float(s.replace(",", "").replace("$", "").strip())
                    except (ValueError, AttributeError):
                        return None
                entry_val = _safe_float(m.group("entry"))
                sl_val = _safe_float(m.group("sl"))
                tp1_val = _safe_float(m.group("tp1"))
                tp2_val = _safe_float(m.group("tp2"))
                rr_val = _safe_float(m.group("rr1"))
                source = "ai_inferred" if _next_is_ai else _detect_source(stripped)
                _next_is_ai = False
                if entry_val or sl_val or tp1_val:
                    entries.append(TradingPlanEntry(
                        tier=current_tier or "short",
                        direction=direction, entry=entry_val,
                        stop_loss=sl_val, tp1=tp1_val, tp2=tp2_val,
                        rr=rr_val, source=source,
                    ))
                continue

    if not entries:
        chunks = re.split(
            r"(?=\*\*(?:多单|空单|做多|做空|Long|Short|多头|空头))", text
        )
        for chunk in chunks:
            if not chunk.strip():
                continue
            direction = _detect_direction(chunk)
            if not direction:
                continue
            tier = current_tier
            for ln in chunk.split("\n")[:3]:
                t = _detect_tier(ln)
                if t:
                    tier = t
                    break
            entry_val = _extract_price(
                r"(?:挂单价|入场|entry)[^$\d]*\$?([\d,]+\.?\d*)", chunk
            )
            sl_val = _extract_price(
                r"(?:止损|stop.?loss|SL)[^$\d]*\$?([\d,]+\.?\d*)", chunk
            )
            tp1_val = _extract_price(
                r"(?:止盈1|TP1|目标1|take.?profit.?1)[^$\d]*\$?([\d,]+\.?\d*)", chunk
            )
            tp2_val = _extract_price(
                r"(?:止盈2|TP2|目标2|take.?profit.?2)[^$\d]*\$?([\d,]+\.?\d*)", chunk
            )
            rr_match = re.search(r"R[：:]\s*R\s*[=≈≥]\s*([\d.]+)", chunk, re.IGNORECASE)
            if not rr_match:
                rr_match = re.search(r"1[：:]([\d.]+)", chunk)
            rr_val = float(rr_match.group(1)) if rr_match else None
            source = _detect_source(chunk)

            if entry_val or sl_val or tp1_val:
                entries.append(TradingPlanEntry(
                    tier=tier or "short", direction=direction, entry=entry_val,
                    stop_loss=sl_val, tp1=tp1_val, tp2=tp2_val,
                    rr=rr_val, source=source,
                ))

    return entries


def _parse_sniper_plans(text: str) -> list[SniperPlan]:
    """从狙击方案 markdown 文本中提取结构化挂单数据。

    尝试识别每个方向（多单/空单）的入场价、止损、止盈、R:R。
    采用宽松正则匹配，兼容 AI 的各种表述变体。
    """
    if not text or len(text) < 20:
        return []

    plans: list[SniperPlan] = []
    chunks = re.split(r"(?=\*\*(?:多单|空单|做多|做空|Long|Short))", text)

    def _extract_price(pattern: str, block: str) -> Optional[float]:
        m = re.search(pattern, block, re.IGNORECASE)
        if m:
            raw = m.group(1).replace(",", "").replace("$", "").strip()
            try:
                return float(raw)
            except ValueError:
                pass
        return None

    for chunk in chunks:
        if not chunk.strip():
            continue
        direction = ""
        lower = chunk[:80].lower()
        if any(kw in lower for kw in ("多单", "做多", "long", "多头埋伏")):
            direction = "long"
        elif any(kw in lower for kw in ("空单", "做空", "short", "空头埋伏")):
            direction = "short"

        if not direction:
            continue

        entry = _extract_price(
            r"(?:挂单价|入场|entry)[^$\d]*\$?([\d,]+\.?\d*)", chunk
        )
        sl = _extract_price(
            r"(?:止损|stop.?loss|SL)[^$\d]*\$?([\d,]+\.?\d*)", chunk
        )
        tp1 = _extract_price(
            r"(?:止盈1|TP1|目标1|take.?profit.?1)[^$\d]*\$?([\d,]+\.?\d*)", chunk
        )
        tp2 = _extract_price(
            r"(?:止盈2|TP2|目标2|take.?profit.?2)[^$\d]*\$?([\d,]+\.?\d*)", chunk
        )
        rr_match = re.search(r"R[：:]\s*R\s*[=≈≥]\s*([\d.]+)", chunk, re.IGNORECASE)
        if not rr_match:
            rr_match = re.search(r"1[：:]([\d.]+)", chunk)
        rr = float(rr_match.group(1)) if rr_match else None

        inv_match = re.search(r"(?:失效|无效|作废)[^：:]*[：:](.+?)(?:\n|$)", chunk)
        invalidation = inv_match.group(1).strip() if inv_match else ""

        logic_match = re.search(
            r"(?:逻辑|理由|依据|logic|reason)[^：:]*[：:](.+?)(?:\n|$)", chunk, re.IGNORECASE
        )
        logic = logic_match.group(1).strip() if logic_match else ""

        if entry or sl or tp1:
            plans.append(SniperPlan(
                direction=direction,
                entry=entry,
                stop_loss=sl,
                tp1=tp1,
                tp2=tp2,
                rr=rr,
                logic=logic[:200],
                invalidation=invalidation[:200],
                raw_text=chunk.strip()[:500],
            ))

    return plans


def create_analyzer() -> AIAnalyzer:
    return AIAnalyzer()
