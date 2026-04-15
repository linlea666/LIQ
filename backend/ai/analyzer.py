"""AI 分析器：组装快照 → 调用 LLM → 解析输出"""

from __future__ import annotations

import logging
import re
from typing import Optional
import time
import traceback

from openai import AsyncOpenAI

from ai.prompts import build_system_prompt, build_user_prompt
from config.settings import get_settings
from models.snapshot import AIAnalysisResult, AISnapshot, SignalSummary, SniperPlan

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
        logger.info(
            "AI analysis started | coin=%s price=%.2f | "
            "system_prompt=%d chars | user_prompt=%d chars | model=%s timeout=%ds",
            snapshot.coin, snapshot.price,
            len(system_prompt), len(user_prompt),
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

        result = _parse_ai_output(raw_text, snapshot, user_prompt)
        return result


def _parse_ai_output(raw_text: str, snapshot: AISnapshot, user_prompt: str = "") -> AIAnalysisResult:
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
        risk_warnings=_parse_list(_find_section("风险提示", "Risk")),
        scenario_analysis=_parse_scenarios(_find_section("场景", "推演", "Scenario")),
        raw_text=raw_text,
        user_prompt=user_prompt,
    )


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
