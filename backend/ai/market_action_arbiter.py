"""Market Action Arbiter · DeepSeek V4-Flash 调用器（非思考模式）

职责：
  - 接 MarketActionFacts → 组装 prompt → 调用 DeepSeek → 解析 → MarketActionReport
  - 复用 settings.ai 的 DeepSeek 配置（api_key/base_url/model/timeout/retries）
  - 完整记录 PromptDebug（system/user/sections/tokens/latency/raw/parse 状态）
  - 解析失败时返回**降级报告**（range_bound + wait + data_quality=insufficient），
    让上层流程不中断，前端能看到 parse_error 和 raw_response 做复盘

模型：deepseek-v4-flash，thinking=disabled（reasoning_tokens 恒为 0）。
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
    AlternativeScenario,
    ContinuityVerdict,
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
    previous_snapshot: Optional[dict] = None,
) -> MarketActionReport:
    # fallback 也保留 continuity（first_run 或保留上版参考，但 stance=first_run 表示"本次不可信"）
    continuity = ContinuityVerdict(
        stance="first_run",
        previous_scenario=(previous_snapshot or {}).get("scenario") if previous_snapshot else None,
        previous_ts=(previous_snapshot or {}).get("timestamp") if previous_snapshot else None,
        note=f"本次为降级报告（{reason}），未进行时序对照；如需连续性判断请待 AI 恢复后重新分析。",
    )
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
        continuity=continuity,
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

    async def analyze(
        self,
        facts: MarketActionFacts,
        previous_report: Optional[MarketActionReport] = None,
    ) -> MarketActionReport:
        """执行一次 AI 分析，返回完整的 MarketActionReport（含 PromptDebug）。

        Args:
            facts: 本轮市场事实数据
            previous_report: 可选 · 同币种上一份 MarketActionReport，若提供则渲染 §0 前情提要，
                             供 AI 做"延续 / 细节修正 / 方向反转"判断
        """
        system_prompt = build_system_prompt()
        user_prompt, sections = build_user_prompt(facts, previous_report=previous_report)

        # 提取 prev 快照，供 fallback 与 continuity 回填使用
        prev_snapshot: Optional[dict] = None
        if previous_report is not None:
            try:
                prev_snapshot = (
                    previous_report.model_dump()
                    if hasattr(previous_report, "model_dump")
                    else dict(previous_report)
                )
            except Exception:
                prev_snapshot = None

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
                previous_snapshot=prev_snapshot,
            )

        logger.info(
            "MAA analyze start | coin=%s | sys=%d chars | user=%d chars | "
            "model=%s thinking=disabled timeout=%ds",
            facts.coin, len(system_prompt), len(user_prompt),
            self._model, self._timeout,
        )

        raw_text = ""
        reasoning_text = ""
        tokens_in = tokens_out = tokens_reasoning = 0
        t0 = time.time()

        for attempt in range(1, self._max_retries + 1):
            try:
                # v4-flash 非思考模式：低温 + thinking=disabled，保证 JSON 稳定且响应快
                api_kwargs: dict = {
                    "model": self._model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    "timeout": self._timeout,
                    "temperature": 0.2,
                    "extra_body": {"thinking": {"type": "disabled"}},
                }

                resp = await self._client.chat.completions.create(**api_kwargs)
                msg = resp.choices[0].message
                raw_text = msg.content or ""
                # v4-flash 非思考模式下 reasoning_content 恒为空；字段保留向后兼容 R1/reasoner
                reasoning_text = getattr(msg, "reasoning_content", None) or ""

                if resp.usage:
                    tokens_in = resp.usage.prompt_tokens or 0
                    tokens_out = resp.usage.completion_tokens or 0
                    details = getattr(resp.usage, "completion_tokens_details", None)
                    tokens_reasoning = getattr(details, "reasoning_tokens", 0) if details else 0

                logger.info(
                    "MAA analyze ok | coin=%s | attempt=%d | %.1fs | "
                    "tok_in=%d out=%d reasoning=%d | resp=%d chars | cot=%d chars",
                    facts.coin, attempt, time.time() - t0,
                    tokens_in, tokens_out, tokens_reasoning,
                    len(raw_text), len(reasoning_text),
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
                        previous_snapshot=prev_snapshot,
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
            ai_reasoning_content=reasoning_text or None,
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
                previous_snapshot=prev_snapshot,
            )

        try:
            report = _payload_to_report(payload, facts, prompt_debug, prev_snapshot)
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
                previous_snapshot=prev_snapshot,
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
_VALID_SUPPORTS = {"main", "contrarian", "neutral"}
_VALID_CONTINUITY = {"continuation", "refinement", "reversal", "first_run"}

# dimension 白名单（与 prompts 里 §Evidence 写法要求保持一致）
_VALID_DIMENSIONS = {
    "PriceContext", "OI", "Funding", "Basis", "CVD",
    "Liquidation", "LiqMap", "LiqSweep", "Footprint",
    "Taker", "Orderbook", "Options",
}
# 常见变体 → 白名单的归一映射（AI 偶尔会写成中文或连字符形式）
_DIMENSION_ALIASES = {
    "price": "PriceContext",
    "pricecontext": "PriceContext",
    "position": "PriceContext",
    "oi": "OI",
    "open_interest": "OI",
    "openinterest": "OI",
    "funding": "Funding",
    "fundingrate": "Funding",
    "basis": "Basis",
    "cvd": "CVD",
    "cvd_contract": "CVD",
    "cvd_spot": "CVD",
    "liquidation": "Liquidation",
    "liq": "Liquidation",
    "liqmap": "LiqMap",
    "liq_map": "LiqMap",
    "liquidationmap": "LiqMap",
    "liqsweep": "LiqSweep",
    "liq_sweep": "LiqSweep",
    "sweep": "LiqSweep",
    "footprint": "Footprint",
    "taker": "Taker",
    "taker_flow": "Taker",
    "orderbook": "Orderbook",
    "depth": "Orderbook",
    "options": "Options",
    "option": "Options",
}


def _coerce_dimension(v) -> str:
    """把 AI 输出的 dimension 归一到白名单；无法识别时保底 PriceContext。"""
    if not isinstance(v, str):
        return "PriceContext"
    s = v.strip()
    if s in _VALID_DIMENSIONS:
        return s
    key = s.lower().replace("-", "_").replace(" ", "_")
    if key in _DIMENSION_ALIASES:
        return _DIMENSION_ALIASES[key]
    # 宽松匹配大小写
    for d in _VALID_DIMENSIONS:
        if d.lower() == key:
            return d
    logger.debug("MAA dimension fallback | received=%r → PriceContext", v)
    return "PriceContext"


def _coerce_continuity_stance(v) -> str:
    if isinstance(v, str) and v in _VALID_CONTINUITY:
        return v
    return "first_run"


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


def _coerce_supports(v) -> str:
    if isinstance(v, str) and v in _VALID_SUPPORTS:
        return v
    return "main"


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
    previous_snapshot: Optional[dict] = None,
) -> MarketActionReport:
    evidence_raw = payload.get("evidence_breakdown") or []
    evidence: list[EvidenceItem] = []
    if isinstance(evidence_raw, list):
        for e in evidence_raw[:12]:  # 防过量
            if not isinstance(e, dict):
                continue
            dim = _coerce_dimension(e.get("dimension"))
            obs = str(e.get("observation") or "")[:400]
            if not obs:
                continue
            inf_raw = e.get("inference")
            inference = str(inf_raw)[:500] if inf_raw else None
            evidence.append(EvidenceItem(
                dimension=dim,
                observation=obs,
                inference=inference,
                supports=_coerce_supports(e.get("supports")),
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
        trader_intuition=(str(ti_raw.get("trader_intuition") or "")[:400]) or None,
    )

    # 推理层新字段（向后兼容：缺失时为 None）
    analyst_reasoning_raw = payload.get("analyst_reasoning")
    analyst_reasoning = str(analyst_reasoning_raw)[:2500] if analyst_reasoning_raw else None

    conf_rationale_raw = payload.get("confidence_rationale")
    confidence_rationale = str(conf_rationale_raw)[:600] if conf_rationale_raw else None

    alt_raw = payload.get("alternative_scenario")
    alternative_scenario = None
    if isinstance(alt_raw, dict):
        alt_sc = alt_raw.get("scenario")
        if isinstance(alt_sc, str) and alt_sc in _VALID_SCENARIOS:
            try:
                prob = int(float(alt_raw.get("probability_pct") or 0))
                prob = max(0, min(100, prob))
                alternative_scenario = AlternativeScenario(
                    scenario=alt_sc,
                    probability_pct=prob,
                    trigger=str(alt_raw.get("trigger") or "")[:300],
                )
            except (TypeError, ValueError):
                alternative_scenario = None

    inv_raw = payload.get("invalidation_conditions") or []
    if isinstance(inv_raw, list):
        invalidations = [str(x)[:200] for x in inv_raw if str(x).strip()][:6]
    else:
        invalidations = []

    conclusion = str(payload.get("market_conclusion") or "")[:600]
    if not conclusion:
        conclusion = "（AI 未给出总结）"

    # ── continuity · 时序连续性 ──
    # 若无上一份报告：无论 AI 输出什么，都强制 first_run
    # 若有上一份：尊重 AI 的 stance 判断，但 previous_scenario / previous_ts 由后端强制回填
    continuity: Optional[ContinuityVerdict] = None
    cont_raw = payload.get("continuity")
    if previous_snapshot is None:
        continuity = ContinuityVerdict(
            stance="first_run",
            previous_scenario=None,
            previous_ts=None,
            note=(
                str((cont_raw or {}).get("note") or "")[:300]
                or "首次分析，无历史参考。"
            ),
        )
    else:
        prev_scenario = previous_snapshot.get("scenario")
        prev_ts = previous_snapshot.get("timestamp")
        if isinstance(cont_raw, dict):
            stance = _coerce_continuity_stance(cont_raw.get("stance"))
            # 若 AI 给出 first_run 但实际有历史，视为 continuation（保守）
            if stance == "first_run":
                stance = "continuation"
            note = str(cont_raw.get("note") or "")[:300]
        else:
            # AI 忘了填 continuity 字段：按 scenario 对比兜底
            current_scenario = _coerce_scenario(payload.get("scenario"))
            if current_scenario == prev_scenario:
                stance, note = "continuation", "AI 未给出 continuity，后端按 scenario 相同判为 continuation。"
            else:
                stance, note = "refinement", "AI 未给出 continuity，后端按 scenario 变化判为 refinement。"
        continuity = ContinuityVerdict(
            stance=stance,
            previous_scenario=prev_scenario if isinstance(prev_scenario, str) else None,
            previous_ts=int(prev_ts) if isinstance(prev_ts, (int, float)) else None,
            note=note or f"本次相对上一份判为 {stance}。",
        )

    return MarketActionReport(
        coin=facts.coin,
        timestamp=int(time.time()),
        market_conclusion=conclusion,
        scenario=_coerce_scenario(payload.get("scenario")),
        market_phase=_coerce_phase(payload.get("market_phase")),
        analyst_reasoning=analyst_reasoning,
        confidence_rationale=confidence_rationale,
        alternative_scenario=alternative_scenario,
        continuity=continuity,
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
