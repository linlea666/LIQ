"""AI 分析器：组装快照 → 调用 LLM → 解析输出"""

from __future__ import annotations

import logging
from typing import Optional
import time
import traceback

from openai import AsyncOpenAI

from ai.prompts import build_system_prompt, build_user_prompt
from config.settings import get_settings
from models.snapshot import AIAnalysisResult, AISnapshot

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

        raw_text = ""
        for attempt in range(1, self._max_retries + 1):
            try:
                logger.info(
                    "AI API call attempt %d/%d | coin=%s model=%s",
                    attempt, self._max_retries, snapshot.coin, self._model,
                )
                t0 = time.time()
                response = await self._client.chat.completions.create(
                    model=self._model,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    temperature=0.3,
                    timeout=self._timeout,
                )
                elapsed = time.time() - t0

                raw_text = response.choices[0].message.content or ""
                tokens_in = response.usage.prompt_tokens if response.usage else 0
                tokens_out = response.usage.completion_tokens if response.usage else 0

                logger.info(
                    "AI API call success | coin=%s | %.1fs | "
                    "tokens_in=%d out=%d | response_len=%d chars",
                    snapshot.coin, elapsed, tokens_in, tokens_out, len(raw_text),
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

    return AIAnalysisResult(
        coin=snapshot.coin,
        ts=int(time.time()),
        price_at_analysis=snapshot.price,
        market_overview=_find_section("格局", "总览", "Overview"),
        key_levels=_parse_levels_table(_find_section("价位", "图谱", "Level")),
        stop_loss_suggestion={"raw": _find_section("止损", "Stop")},
        entry_zones=_parse_entry_zones(_find_section("入场", "观察区", "Entry")),
        sniper_setup=_find_sniper_section(),
        ladder_plan_text=_find_ladder_section(),
        risk_warnings=_parse_list(_find_section("风险提示", "Risk")),
        scenario_analysis=_parse_scenarios(_find_section("场景", "推演", "Scenario")),
        raw_text=raw_text,
        user_prompt=user_prompt,
    )


def _parse_levels_table(text: str) -> list[dict]:
    """尝试从 markdown 表格解析价位"""
    levels: list[dict] = []
    for line in text.split("\n"):
        line = line.strip()
        if not line or line.startswith("|--") or line.startswith("| 类型"):
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
        if line.startswith("场景"):
            if current:
                scenarios.append(current)
            current = {"label": line.split("：")[0] if "：" in line else line.split(":")[0],
                        "description": line.split("：", 1)[-1] if "：" in line else line.split(":", 1)[-1]}
        elif line and current:
            current["description"] = current.get("description", "") + " " + line
    if current:
        scenarios.append(current)
    return scenarios


def create_analyzer() -> AIAnalyzer:
    return AIAnalyzer()
