"""Strategic Trading Decision Officer · DeepSeek 调用器（非思考模式）。

职责：
  - 接 AISnapshot → 组装 prompt → 调用 DeepSeek → 解析 → AIStrategicReport
  - 复用 settings.ai 的 DeepSeek 配置（与 MAA 同一 client / api_key / base_url / model）
  - 完整记录 PromptDebug（system/user/sections/tokens/latency/raw/parse 状态）
  - 解析失败时返回**降级报告**（decision=NO_TRADE + hard_stop_triggered=True），
    让上层流程不中断；前端能看到 parse_error 和 raw_response 做复盘

设计纪律（与 MAA arbiter 模式对齐）：
  1. async 调用 + max_retries 重试
  2. fallback_report 与 prompt_debug 始终成对出现
  3. _payload_to_report 容忍式映射（_coerce_*），任何非法字段不抛异常，退化默认值
  4. 不引入 stability_filter（Strategic 输出是完整计划而非单一方向标签，状态机不适用）
"""

from __future__ import annotations

import logging
import time
import traceback
from typing import Any, Optional

from openai import AsyncOpenAI

from ai.strategic_prompts import (
    build_system_prompt,
    build_user_prompt,
    extract_json_payload,
)
from config.settings import get_settings
from models.common_prompt_debug import PromptDebug
from models.snapshot import AISnapshot
from models.strategic_report import (
    AIStrategicReport,
    AlternativeScenario,
    CurrentZoneAssessment,
    DataSelfCheck,
    Evidence,
    EvidenceMatrix,
    Target,
    TradingPlan,
)

logger = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────────────────────
# 类型白名单（与 strategic_report.py 的 Literal 同步）
# ────────────────────────────────────────────────────────────────────────────

_VALID_DECISIONS = {
    "WAIT", "LONG_OBSERVATION", "SHORT_OBSERVATION",
    "LONG_PLAN", "SHORT_PLAN", "NO_TRADE",
}
_VALID_HORIZONS = {"scalp", "intraday", "swing", "strategic"}
_VALID_BIAS = {"bullish", "bearish", "neutral", "conflicted"}
_VALID_SUPPORTS = {"main", "contrarian", "neutral"}
_VALID_WEIGHT = {"high", "medium", "low"}
_VALID_LEVERAGE = {"low", "medium", "high", "extreme"}
_VALID_DATA_QUALITY = {"ok", "partial", "insufficient"}


# ────────────────────────────────────────────────────────────────────────────
# 降级报告 · 当 AI 不可用 / 解析失败时返回
# ────────────────────────────────────────────────────────────────────────────

def _fallback_report(
    snapshot: AISnapshot,
    *,
    reason: str,
    prompt_debug: Optional[PromptDebug] = None,
) -> AIStrategicReport:
    """构造降级报告。

    设计：
      - decision = NO_TRADE：明确告诉前端"本次不可用，禁止任何方向"
      - hard_stop_triggered = True：data_self_check 标记数据严重不可信
      - confidence_penalty_reason = reason：让前端能展示具体降级原因
    """
    return AIStrategicReport(
        coin=snapshot.coin,
        timestamp=int(time.time()),
        decision="NO_TRADE",
        horizon="intraday",
        bias="neutral",
        confidence=0.0,
        confidence_rationale=f"AI 不可用（{reason}），本次返回降级结果",
        data_self_check=DataSelfCheck(
            hard_stop_triggered=True,
            confidence_penalty_reason=reason,
        ),
        data_quality="insufficient",
        prompt_debug=prompt_debug,
    )


# ────────────────────────────────────────────────────────────────────────────
# Coerce helpers · 把 AI 输出的字段宽容映射到白名单
# ────────────────────────────────────────────────────────────────────────────

def _coerce_decision(v: Any) -> str:
    if isinstance(v, str):
        s = v.strip().upper()
        if s in _VALID_DECISIONS:
            return s
    return "WAIT"


def _coerce_horizon(v: Any) -> str:
    if isinstance(v, str) and v.strip().lower() in _VALID_HORIZONS:
        return v.strip().lower()
    return "intraday"


def _coerce_bias(v: Any) -> str:
    if isinstance(v, str) and v.strip().lower() in _VALID_BIAS:
        return v.strip().lower()
    return "neutral"


def _coerce_supports(v: Any) -> str:
    if isinstance(v, str) and v.strip().lower() in _VALID_SUPPORTS:
        return v.strip().lower()
    return "main"


def _coerce_weight(v: Any) -> str:
    if isinstance(v, str) and v.strip().lower() in _VALID_WEIGHT:
        return v.strip().lower()
    return "medium"


def _coerce_leverage(v: Any) -> str:
    if isinstance(v, str) and v.strip().lower() in _VALID_LEVERAGE:
        return v.strip().lower()
    return "medium"


def _coerce_data_quality(v: Any) -> str:
    if isinstance(v, str) and v.strip().lower() in _VALID_DATA_QUALITY:
        return v.strip().lower()
    return "partial"


def _coerce_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _coerce_confidence(v: Any) -> float:
    f = _coerce_float(v)
    if f is None:
        return 0.0
    if f < 0.0:
        return 0.0
    if f > 1.0:
        # 兼容 AI 输出 0-100 整数（Strategic schema 要求 0-1）
        return min(1.0, f / 100.0) if f <= 100.0 else 1.0
    return f


def _coerce_str_list(v: Any, max_len: int = 8, item_max: int = 200) -> list[str]:
    if not isinstance(v, list):
        return []
    out: list[str] = []
    for x in v:
        s = str(x).strip()
        if not s:
            continue
        out.append(s[:item_max])
        if len(out) >= max_len:
            break
    return out


def _coerce_evidence_list(raw: Any, max_len: int = 12) -> list[Evidence]:
    if not isinstance(raw, list):
        return []
    out: list[Evidence] = []
    for e in raw[:max_len]:
        if not isinstance(e, dict):
            continue
        section_ref = str(e.get("section_ref") or "").strip()[:20]
        observation = str(e.get("observation") or "").strip()[:400]
        if not observation:
            continue
        inference = str(e.get("inference") or "").strip()[:500]
        out.append(Evidence(
            section_ref=section_ref or "§?",
            observation=observation,
            inference=inference,
            supports=_coerce_supports(e.get("supports")),  # type: ignore[arg-type]
            weight=_coerce_weight(e.get("weight")),  # type: ignore[arg-type]
        ))
    return out


def _coerce_targets(raw: Any) -> list[Target]:
    if not isinstance(raw, list):
        return []
    out: list[Target] = []
    for t in raw[:6]:
        if not isinstance(t, dict):
            continue
        price = _coerce_float(t.get("price"))
        if price is None or price <= 0:
            continue
        rr = _coerce_float(t.get("rr"))
        if rr is None:
            rr = 0.0
        out.append(Target(
            price=price,
            reason=str(t.get("reason") or "")[:200],
            rr=rr,
        ))
    return out


def _coerce_trading_plan(raw: Any) -> Optional[TradingPlan]:
    if not isinstance(raw, dict):
        return None
    setup_type = str(raw.get("setup_type") or "").strip()
    hard_inv = str(raw.get("hard_invalidation") or "").strip()
    if not setup_type or not hard_inv:
        # 关键字段缺失 → 视为 plan 不存在（避免下游误用半成品计划）
        return None
    el = _coerce_float(raw.get("entry_zone_low"))
    eh = _coerce_float(raw.get("entry_zone_high"))
    if el is None or eh is None:
        return None
    if el > eh:
        el, eh = eh, el  # 容忍 AI 倒置
    return TradingPlan(
        setup_type=setup_type[:80],
        entry_zone_low=el,
        entry_zone_high=eh,
        trigger_conditions=_coerce_str_list(raw.get("trigger_conditions"), max_len=6, item_max=200),
        soft_invalidation=str(raw.get("soft_invalidation") or "")[:300],
        hard_invalidation=hard_inv[:300],
        targets=_coerce_targets(raw.get("targets")),
        cancel_conditions=_coerce_str_list(raw.get("cancel_conditions"), max_len=5, item_max=200),
        risk_unit=str(raw.get("risk_unit") or "")[:200],
        leverage_risk_level=_coerce_leverage(raw.get("leverage_risk_level")),  # type: ignore[arg-type]
        position_sizing_note=str(raw.get("position_sizing_note") or "")[:300],
    )


def _coerce_alternative_scenario(raw: Any) -> Optional[AlternativeScenario]:
    if not isinstance(raw, dict):
        return None
    desc = str(raw.get("description") or "").strip()[:400]
    trigger = str(raw.get("trigger") or "").strip()[:300]
    if not desc or not trigger:
        return None
    try:
        prob = int(float(raw.get("probability_pct") or 0))
        prob = max(0, min(100, prob))
    except (TypeError, ValueError):
        prob = 0
    return AlternativeScenario(
        description=desc,
        probability_pct=prob,
        trigger=trigger,
    )


def _coerce_current_zone(raw: Any) -> CurrentZoneAssessment:
    if not isinstance(raw, dict):
        return CurrentZoneAssessment()
    return CurrentZoneAssessment(
        zone_id=str(raw.get("zone_id") or "")[:80],
        role=str(raw.get("role") or "")[:40],
        nearest_critical_above_pct=_coerce_float(raw.get("nearest_critical_above_pct")),
        nearest_critical_below_pct=_coerce_float(raw.get("nearest_critical_below_pct")),
        key_conflict=str(raw.get("key_conflict") or "")[:300],
    )


def _coerce_data_self_check(raw: Any) -> DataSelfCheck:
    if not isinstance(raw, dict):
        return DataSelfCheck()
    return DataSelfCheck(
        missing=_coerce_str_list(raw.get("missing"), max_len=12, item_max=80),
        stale=_coerce_str_list(raw.get("stale"), max_len=12, item_max=80),
        provisional=_coerce_str_list(raw.get("provisional"), max_len=12, item_max=80),
        hard_stop_triggered=bool(raw.get("hard_stop_triggered") or False),
        confidence_penalty_reason=str(raw.get("confidence_penalty_reason") or "")[:300],
    )


def _coerce_evidence_matrix(raw: Any) -> EvidenceMatrix:
    if not isinstance(raw, dict):
        return EvidenceMatrix()
    return EvidenceMatrix(
        long_evidence=_coerce_evidence_list(raw.get("long_evidence"), max_len=8),
        short_evidence=_coerce_evidence_list(raw.get("short_evidence"), max_len=8),
        wait_evidence=_coerce_evidence_list(raw.get("wait_evidence"), max_len=6),
        contradictions=_coerce_str_list(raw.get("contradictions"), max_len=4, item_max=300),
    )


# ────────────────────────────────────────────────────────────────────────────
# payload → AIStrategicReport
# ────────────────────────────────────────────────────────────────────────────

def _payload_to_report(
    payload: dict,
    snapshot: AISnapshot,
    prompt_debug: PromptDebug,
) -> AIStrategicReport:
    """把 AI 输出 payload 映射到 AIStrategicReport。

    设计纪律：
      - 任何字段缺失 / 非法 → coerce 默认值，不抛异常
      - decision 与 primary_plan 的一致性强约束：
          · decision in [LONG_PLAN, SHORT_PLAN] 但 primary_plan=None → 降级为 LONG_OBSERVATION/SHORT_OBSERVATION
          · decision = NO_TRADE 时强制清空 primary_plan / alternative_plan
      - hard_stop_triggered=True 时强制 decision=NO_TRADE（防 AI 自相矛盾）
    """
    decision = _coerce_decision(payload.get("decision"))
    horizon = _coerce_horizon(payload.get("horizon"))
    bias = _coerce_bias(payload.get("bias"))
    confidence = _coerce_confidence(payload.get("confidence"))

    primary_plan = _coerce_trading_plan(payload.get("primary_plan"))
    alternative_plan = _coerce_trading_plan(payload.get("alternative_plan"))
    alternative_scenario = _coerce_alternative_scenario(payload.get("alternative_scenario"))
    current_zone = _coerce_current_zone(payload.get("current_zone_assessment"))
    data_self_check = _coerce_data_self_check(payload.get("data_self_check"))
    evidence_matrix = _coerce_evidence_matrix(payload.get("evidence_matrix"))

    # 一致性约束 1：hard_stop_triggered → 强制 NO_TRADE
    if data_self_check.hard_stop_triggered and decision != "NO_TRADE":
        logger.info(
            "Strategic decision force NO_TRADE due to hard_stop_triggered | "
            "coin=%s ai_raw_decision=%s", snapshot.coin, decision,
        )
        decision = "NO_TRADE"

    # 一致性约束 2：NO_TRADE → 清空 plan
    if decision == "NO_TRADE":
        primary_plan = None
        alternative_plan = None

    # 一致性约束 3：LONG_PLAN/SHORT_PLAN 但 plan 缺失 → 降级到 OBSERVATION
    if decision in ("LONG_PLAN", "SHORT_PLAN") and primary_plan is None:
        logger.info(
            "Strategic decision degrade %s → OBSERVATION due to missing primary_plan | coin=%s",
            decision, snapshot.coin,
        )
        decision = "LONG_OBSERVATION" if decision == "LONG_PLAN" else "SHORT_OBSERVATION"

    return AIStrategicReport(
        coin=snapshot.coin,
        timestamp=int(time.time()),
        decision=decision,  # type: ignore[arg-type]
        horizon=horizon,  # type: ignore[arg-type]
        bias=bias,  # type: ignore[arg-type]
        confidence=confidence,
        confidence_rationale=str(payload.get("confidence_rationale") or "")[:600],
        market_phase=str(payload.get("market_phase") or "")[:100],
        cycle_position=str(payload.get("cycle_position") or "")[:100],
        current_zone_assessment=current_zone,
        structure_analysis=str(payload.get("structure_analysis") or "")[:1500],
        flow_analysis=str(payload.get("flow_analysis") or "")[:1500],
        macro_context=str(payload.get("macro_context") or "")[:1000],
        primary_plan=primary_plan,
        alternative_plan=alternative_plan,
        no_trade_conditions=_coerce_str_list(payload.get("no_trade_conditions"), max_len=6, item_max=200),
        alternative_scenario=alternative_scenario,
        evidence_matrix=evidence_matrix,
        invalidation_conditions=_coerce_str_list(payload.get("invalidation_conditions"), max_len=6, item_max=200),
        data_self_check=data_self_check,
        macro_modifier_note=str(payload.get("macro_modifier_note") or "")[:400],
        data_quality=_coerce_data_quality(payload.get("data_quality")),  # type: ignore[arg-type]
        prompt_debug=prompt_debug,
    )


# ────────────────────────────────────────────────────────────────────────────
# StrategicArbiter 主体
# ────────────────────────────────────────────────────────────────────────────

class StrategicArbiter:
    """调用 DeepSeek 完成全程战略决策（输出完整候选交易计划）。"""

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
            "StrategicArbiter init | provider=%s model=%s base=%s "
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
        snapshot: AISnapshot,
        previous_report: Optional[AIStrategicReport] = None,
    ) -> AIStrategicReport:
        """执行一次 Strategic AI 分析，返回完整的 AIStrategicReport（含 PromptDebug）。

        Args:
            snapshot: 本轮 AISnapshot（PR-1 装配，含强类型 trading_brain / facts_*）
            previous_report: 可选 · 同币种上一份 AIStrategicReport，
                若提供则在 prompt §0' 渲染前情提要供 AI 做"延续 / 修正 / 反转"判断
        """
        system_prompt = build_system_prompt()
        user_prompt, sections = build_user_prompt(snapshot, previous_report=previous_report)

        if self._client is None:
            return _fallback_report(
                snapshot,
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

        logger.info(
            "Strategic analyze start | coin=%s | sys=%d chars | user=%d chars | "
            "model=%s thinking=disabled timeout=%ds",
            snapshot.coin, len(system_prompt), len(user_prompt),
            self._model, self._timeout,
        )

        raw_text = ""
        reasoning_text = ""
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
                    "temperature": 0.2,
                    "extra_body": {"thinking": {"type": "disabled"}},
                }
                resp = await self._client.chat.completions.create(**api_kwargs)
                msg = resp.choices[0].message
                raw_text = msg.content or ""
                reasoning_text = getattr(msg, "reasoning_content", None) or ""

                if resp.usage:
                    tokens_in = resp.usage.prompt_tokens or 0
                    tokens_out = resp.usage.completion_tokens or 0
                    details = getattr(resp.usage, "completion_tokens_details", None)
                    tokens_reasoning = getattr(details, "reasoning_tokens", 0) if details else 0

                logger.info(
                    "Strategic analyze ok | coin=%s | attempt=%d | %.1fs | "
                    "tok_in=%d out=%d reasoning=%d | resp=%d chars",
                    snapshot.coin, attempt, time.time() - t0,
                    tokens_in, tokens_out, tokens_reasoning, len(raw_text),
                )
                break
            except Exception as e:
                logger.warning(
                    "Strategic analyze attempt %d/%d failed | coin=%s | err=%s: %s",
                    attempt, self._max_retries, snapshot.coin,
                    type(e).__name__, str(e),
                )
                if attempt == self._max_retries:
                    logger.error(
                        "Strategic analyze exhausted retries | coin=%s\n%s",
                        snapshot.coin, traceback.format_exc(),
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
                        snapshot, reason="AI API exhausted retries",
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
            ai_reasoning_content=reasoning_text or None,
        )

        # ── 解析 JSON ──
        try:
            payload = extract_json_payload(raw_text)
        except Exception as e:
            logger.warning(
                "Strategic parse failed | coin=%s | err=%s | raw head=%r",
                snapshot.coin, e, raw_text[:300],
            )
            prompt_debug.parse_ok = False
            prompt_debug.parse_error = f"json_extract: {type(e).__name__}: {e}"
            return _fallback_report(
                snapshot, reason="AI output parse failed",
                prompt_debug=prompt_debug,
            )

        try:
            report = _payload_to_report(payload, snapshot, prompt_debug)
            return report
        except Exception as e:
            logger.warning(
                "Strategic schema validate failed | coin=%s | err=%s",
                snapshot.coin, e, exc_info=True,
            )
            prompt_debug.parse_ok = False
            prompt_debug.parse_error = f"schema: {type(e).__name__}: {e}"
            return _fallback_report(
                snapshot, reason="AI output schema invalid",
                prompt_debug=prompt_debug,
            )


def create_strategic_arbiter() -> StrategicArbiter:
    return StrategicArbiter()
