"""Market Action Arbiter · DeepSeek 调用器

职责：
  - 接 MarketActionFacts → 组装 prompt → 调用 DeepSeek → 解析 → MarketActionReport
  - 复用 settings.ai 的 DeepSeek 配置（api_key/base_url/model/timeout/retries）
  - 完整记录 PromptDebug（system/user/sections/tokens/latency/raw/parse 状态）
  - 解析失败时返回**降级报告**（range_bound + wait + data_quality=insufficient），
    让上层流程不中断，前端能看到 parse_error 和 raw_response 做复盘
"""

from __future__ import annotations

import logging
import time
import traceback
from typing import Optional

from openai import AsyncOpenAI

from ai.market_action_prompts import (
    build_system_prompt,
    build_user_prompt,
    extract_json_payload,
)
from config.settings import get_settings
from models.market_action import (
    EvidenceItem,
    MarketActionFacts,
    MarketActionReport,
    PromptDebug,
    TradingImplications,
)

logger = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────────────────────
# 降级报告 · 当 AI 不可用/解析失败时返回，保持前端/下游不崩
# ────────────────────────────────────────────────────────────────────────────

def _fallback_report(
    facts: MarketActionFacts,
    *,
    reason: str,
    prompt_debug: Optional[PromptDebug] = None,
) -> MarketActionReport:
    return MarketActionReport(
        coin=facts.coin,
        timestamp=int(time.time()),
        market_conclusion=f"AI 分析不可用（{reason}），本次返回降级结果；请结合 facts 原始数据自行判断。",
        scenario="range_bound",
        market_phase="transition",
        evidence_breakdown=[],
        trading_implications=TradingImplications(bias="wait", notes=f"fallback: {reason}"),
        invalidation_conditions=["AI 可用后重新分析"],
        confidence=0,
        data_quality="insufficient" if facts.data_quality != "ok" else facts.data_quality,
        stale_minutes=0,
        facts_snapshot=facts,
        prompt_debug=prompt_debug,
    )


# ────────────────────────────────────────────────────────────────────────────
# Arbiter 主体
# ────────────────────────────────────────────────────────────────────────────

class MarketActionArbiter:
    """调用 DeepSeek 完成市场动作结构化分析。"""

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
            "MarketActionArbiter init | provider=%s model=%s base=%s "
            "timeout=%ds retries=%d key_set=%s client_ok=%s",
            cfg.active, cfg.model, cfg.api_base or "(default)",
            cfg.timeout_sec, cfg.max_retries,
            bool(cfg.api_key), self._client is not None,
        )

    @property
    def available(self) -> bool:
        return self._client is not None

    async def analyze(self, facts: MarketActionFacts) -> MarketActionReport:
        """执行一次 AI 分析，返回完整的 MarketActionReport（含 PromptDebug）。"""
        system_prompt = build_system_prompt()
        user_prompt, sections = build_user_prompt(facts)

        if self._client is None:
            return _fallback_report(
                facts,
                reason="AI key not configured",
                prompt_debug=PromptDebug(
                    system=system_prompt,
                    user=user_prompt,
                    chars=len(user_prompt),
                    sections=sections,
                    model=self._model,
                    generated_at=int(time.time()),
                    parse_ok=False,
                    parse_error="ai_client_unavailable",
                ),
            )

        is_reasoner = "reasoner" in self._model.lower()

        logger.info(
            "MAA analyze start | coin=%s | sys=%d chars | user=%d chars | "
            "model=%s reasoner=%s timeout=%ds",
            facts.coin, len(system_prompt), len(user_prompt),
            self._model, is_reasoner, self._timeout,
        )

        raw_text = ""
        tokens_in = tokens_out = tokens_reasoning = 0
        t0 = time.time()

        for attempt in range(1, self._max_retries + 1):
            try:
                api_kwargs: dict = {
                    "model": self._model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    "timeout": self._timeout,
                }
                if not is_reasoner:
                    api_kwargs["temperature"] = 0.2

                resp = await self._client.chat.completions.create(**api_kwargs)
                msg = resp.choices[0].message
                raw_text = msg.content or ""

                if resp.usage:
                    tokens_in = resp.usage.prompt_tokens or 0
                    tokens_out = resp.usage.completion_tokens or 0
                    details = getattr(resp.usage, "completion_tokens_details", None)
                    tokens_reasoning = getattr(details, "reasoning_tokens", 0) if details else 0

                logger.info(
                    "MAA analyze ok | coin=%s | attempt=%d | %.1fs | "
                    "tok_in=%d out=%d reasoning=%d | resp=%d chars",
                    facts.coin, attempt, time.time() - t0,
                    tokens_in, tokens_out, tokens_reasoning, len(raw_text),
                )
                break
            except Exception as e:
                logger.warning(
                    "MAA analyze attempt %d/%d failed | coin=%s | err=%s: %s",
                    attempt, self._max_retries, facts.coin,
                    type(e).__name__, str(e),
                )
                if attempt == self._max_retries:
                    logger.error(
                        "MAA analyze exhausted retries | coin=%s\n%s",
                        facts.coin, traceback.format_exc(),
                    )
                    prompt_debug = PromptDebug(
                        system=system_prompt,
                        user=user_prompt,
                        chars=len(user_prompt),
                        sections=sections,
                        model=self._model,
                        latency_ms=int((time.time() - t0) * 1000),
                        generated_at=int(time.time()),
                        parse_ok=False,
                        parse_error=f"api_error: {type(e).__name__}: {str(e)[:200]}",
                    )
                    return _fallback_report(
                        facts, reason="AI API exhausted retries",
                        prompt_debug=prompt_debug,
                    )

        latency_ms = int((time.time() - t0) * 1000)
        prompt_debug = PromptDebug(
            system=system_prompt,
            user=user_prompt,
            chars=len(user_prompt),
            sections=sections,
            model=self._model,
            tokens_prompt=tokens_in or None,
            tokens_completion=tokens_out or None,
            tokens_reasoning=tokens_reasoning or None,
            latency_ms=latency_ms,
            generated_at=int(time.time()),
            ai_raw_response=raw_text,
        )

        # ── 解析 JSON ──
        try:
            payload = extract_json_payload(raw_text)
        except Exception as e:
            logger.warning(
                "MAA parse failed | coin=%s | err=%s | raw head=%r",
                facts.coin, e, raw_text[:300],
            )
            prompt_debug.parse_ok = False
            prompt_debug.parse_error = f"json_extract: {type(e).__name__}: {e}"
            return _fallback_report(
                facts, reason="AI output parse failed",
                prompt_debug=prompt_debug,
            )

        try:
            report = _payload_to_report(payload, facts, prompt_debug)
            return report
        except Exception as e:
            logger.warning(
                "MAA schema validate failed | coin=%s | err=%s",
                facts.coin, e,
            )
            prompt_debug.parse_ok = False
            prompt_debug.parse_error = f"schema: {type(e).__name__}: {e}"
            return _fallback_report(
                facts, reason="AI output schema invalid",
                prompt_debug=prompt_debug,
            )


# ────────────────────────────────────────────────────────────────────────────
# payload → MarketActionReport · 宽容处理，把 AI 输出映射到 Pydantic 模型
# ────────────────────────────────────────────────────────────────────────────

_VALID_SCENARIOS = {
    "trend_continuation_up", "trend_continuation_down",
    "short_squeeze_up", "long_squeeze_down",
    "fake_breakout_up", "fake_breakdown_down",
    "exhaustion_top", "exhaustion_bottom",
    "range_bound",
}
_VALID_PHASES = {"accumulation", "markup", "distribution", "markdown", "transition"}
_VALID_BIAS = {"long", "short", "neutral", "wait"}
_VALID_WEIGHT = {"high", "medium", "low"}


def _coerce_scenario(v) -> str:
    if isinstance(v, str) and v in _VALID_SCENARIOS:
        return v
    return "range_bound"


def _coerce_phase(v) -> str:
    if isinstance(v, str) and v in _VALID_PHASES:
        return v
    return "transition"


def _coerce_bias(v) -> str:
    if isinstance(v, str) and v in _VALID_BIAS:
        return v
    return "wait"


def _coerce_weight(v) -> str:
    if isinstance(v, str) and v in _VALID_WEIGHT:
        return v
    return "medium"


def _coerce_int_confidence(v) -> int:
    try:
        n = int(float(v))
        return max(0, min(100, n))
    except (TypeError, ValueError):
        return 0


def _coerce_float(v) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _coerce_float_list(v) -> list[float]:
    if not isinstance(v, list):
        return []
    out: list[float] = []
    for x in v:
        f = _coerce_float(x)
        if f is not None:
            out.append(f)
    return out


def _payload_to_report(
    payload: dict,
    facts: MarketActionFacts,
    prompt_debug: PromptDebug,
) -> MarketActionReport:
    evidence_raw = payload.get("evidence_breakdown") or []
    evidence: list[EvidenceItem] = []
    if isinstance(evidence_raw, list):
        for e in evidence_raw[:12]:  # 防过量
            if not isinstance(e, dict):
                continue
            dim = str(e.get("dimension") or "")[:60] or "Unknown"
            obs = str(e.get("observation") or "")[:400]
            if not obs:
                continue
            evidence.append(EvidenceItem(
                dimension=dim,
                observation=obs,
                weight=_coerce_weight(e.get("weight")),
            ))

    ti_raw = payload.get("trading_implications") or {}
    bias = _coerce_bias(ti_raw.get("bias"))

    # insufficient 数据质量时强制 bias 不允许 long/short
    dq = payload.get("data_quality") or facts.data_quality
    if dq == "insufficient" and bias in ("long", "short"):
        bias = "wait"

    entry_zone = ti_raw.get("entry_zone")
    if isinstance(entry_zone, list) and len(entry_zone) >= 2:
        zone = [_coerce_float(entry_zone[0]), _coerce_float(entry_zone[1])]
        entry_zone_out = [z for z in zone if z is not None] if all(z is not None for z in zone) else None
    else:
        entry_zone_out = None

    trading_impl = TradingImplications(
        bias=bias,
        entry_zone=entry_zone_out if entry_zone_out and len(entry_zone_out) == 2 else None,
        stop_loss_beyond=_coerce_float(ti_raw.get("stop_loss_beyond")),
        take_profit_targets=_coerce_float_list(ti_raw.get("take_profit_targets"))[:4],
        notes=(str(ti_raw.get("notes") or "")[:300]) or None,
    )

    inv_raw = payload.get("invalidation_conditions") or []
    if isinstance(inv_raw, list):
        invalidations = [str(x)[:200] for x in inv_raw if str(x).strip()][:6]
    else:
        invalidations = []

    conclusion = str(payload.get("market_conclusion") or "")[:600]
    if not conclusion:
        conclusion = "（AI 未给出总结）"

    return MarketActionReport(
        coin=facts.coin,
        timestamp=int(time.time()),
        market_conclusion=conclusion,
        scenario=_coerce_scenario(payload.get("scenario")),
        market_phase=_coerce_phase(payload.get("market_phase")),
        evidence_breakdown=evidence,
        trading_implications=trading_impl,
        invalidation_conditions=invalidations,
        confidence=_coerce_int_confidence(payload.get("confidence")),
        data_quality=dq if dq in ("ok", "partial", "insufficient") else facts.data_quality,
        stale_minutes=0,
        facts_snapshot=facts,
        prompt_debug=prompt_debug,
    )


def create_market_action_arbiter() -> MarketActionArbiter:
    return MarketActionArbiter()
