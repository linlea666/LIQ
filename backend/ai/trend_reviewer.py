"""BTC 趋势 AI 复核器。

AI 是严格的单向安全门：只能 accept / downgrade / veto，不能生成方向、升级状态，
也不能接触或生成开仓、止盈止损、仓位参数。
"""

from __future__ import annotations

import json
import logging
from typing import Literal, Optional

from openai import AsyncOpenAI

logger = logging.getLogger(__name__)


class TrendAIReviewer:
    def __init__(self, ai_config):
        self._model = ai_config.model
        self._timeout = ai_config.timeout_sec
        self._client: Optional[AsyncOpenAI] = None
        if ai_config.api_key:
            kwargs = {"api_key": ai_config.api_key}
            if ai_config.api_base:
                kwargs["base_url"] = ai_config.api_base
            self._client = AsyncOpenAI(**kwargs)

    @property
    def available(self) -> bool:
        return self._client is not None

    async def review(self, snapshot) -> tuple[Literal["not_run", "accept", "downgrade", "veto"], str]:
        if (not self._client or not snapshot.core_direction_immutable
                or snapshot.direction not in ("bullish", "bearish")):
            return "not_run", "AI未配置或核心方向无须复核"
        facts = {
            "state": snapshot.state, "direction_locked": snapshot.direction,
            "core_score": snapshot.core_score, "confidence": snapshot.confidence,
            "timeframes": {
                key: {
                    "score": value.score, "price_volume": value.price_volume_score,
                    "orderflow": value.orderflow_score, "oi": value.oi_participation_score,
                    "spot_confirms": value.spot_confirms, "quality": value.quality.valid,
                } for key, value in snapshot.timeframes.items()
            },
            "funding_modifier": snapshot.funding.confidence_modifier,
            "wallet_modifier": snapshot.wallet_flow.confidence_modifier,
            "data_quality": snapshot.data_quality.model_dump(),
        }
        system = (
            "你是BTC趋势数据质量复核器。核心算法方向已经锁定。你只能输出JSON："
            '{"verdict":"accept|downgrade|veto","reason":"简短原因"}。'
            "禁止改变/建议相反方向，禁止升级状态或置信度，禁止给出交易、开仓、止盈止损、仓位建议。"
            "只有证据冲突或质量问题时 downgrade；核心数据不可信时 veto。"
        )
        try:
            response = await self._client.chat.completions.create(
                model=self._model,
                messages=[{"role": "system", "content": system},
                          {"role": "user", "content": json.dumps(facts, ensure_ascii=False)}],
                temperature=0,
                timeout=self._timeout,
                extra_body={"thinking": {"type": "disabled"}},
            )
            text = (response.choices[0].message.content or "").strip()
            if text.startswith("```"):
                text = text.strip("`").removeprefix("json").strip()
            payload = json.loads(text)
            verdict = str(payload.get("verdict", "")).lower()
            if verdict not in {"accept", "downgrade", "veto"}:
                raise ValueError(f"invalid verdict: {verdict}")
            return verdict, str(payload.get("reason", ""))[:500]
        except Exception as exc:
            logger.warning("trend AI review failed: %s", exc)
            return "not_run", f"AI复核失败: {type(exc).__name__}"
