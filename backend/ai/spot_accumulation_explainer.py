"""现货抄底规则结果解释器；只生成文字，永不参与评分或资金状态。"""

from __future__ import annotations

import logging
from typing import Optional

from openai import AsyncOpenAI

from config.settings import get_settings
from models.spot_accumulation import SpotAccumulationSnapshot

logger = logging.getLogger(__name__)


class SpotAccumulationExplainer:
    def __init__(self) -> None:
        cfg = get_settings().ai
        self._model = cfg.model
        self._timeout = cfg.timeout_sec
        self._client: Optional[AsyncOpenAI] = None
        if cfg.api_key:
            kwargs = {"api_key": cfg.api_key}
            if cfg.api_base:
                kwargs["base_url"] = cfg.api_base
            self._client = AsyncOpenAI(**kwargs)

    @property
    def available(self) -> bool:
        return self._client is not None

    @staticmethod
    def fallback(snapshot: SpotAccumulationSnapshot, reason: str = "") -> str:
        scores = snapshot.facts.scores
        eligible = [item for item in snapshot.opportunities if item.status == "eligible"]
        action = (
            f"当前仅有 {eligible[0].stage} 达标，规则上限 {eligible[0].allocation_usdt:.0f} U。"
            if eligible else "当前没有满足三层门槛的资金释放机会。"
        )
        suffix = f" AI说明不可用：{reason}。" if reason else ""
        return (
            f"估值V={scores.valuation:.1f}，资金M={scores.capital_flow:.1f}，"
            f"承接A={scores.acceptance:.1f}。{action}{suffix}"
        )

    async def explain(self, snapshot: SpotAccumulationSnapshot) -> str:
        if self._client is None:
            return self.fallback(snapshot, "未配置模型")
        compact = {
            "price": snapshot.facts.price,
            "cycle_ath": snapshot.facts.cycle_ath,
            "drawdown_pct": snapshot.facts.drawdown_pct,
            "scores": snapshot.facts.scores.model_dump(),
            "evidence": snapshot.facts.evidence,
            "vetoes": snapshot.facts.hard_vetoes,
            "quality": snapshot.facts.data_quality.model_dump(),
            "opportunities": [
                {
                    "stage": item.stage,
                    "status": item.status,
                    "allocation_usdt": item.allocation_usdt,
                    "blocked_by": item.blocked_by,
                }
                for item in snapshot.opportunities
            ],
        }
        prompt = (
            "你是BTC现货抄底模块的只读解释员。资金金额和机会状态已由确定性规则决定，"
            "你不得更改、追加或否定规则金额，也不得声称确定底部。请用中文简洁说明："
            "1) 三层证据；2) 最大风险或冲突；3) 为什么当前释放或不释放资金。\n"
            f"规则快照：{compact}"
        )
        try:
            response = await self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": "只解释确定性规则，不做独立资金决策。"},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.1,
                timeout=self._timeout,
                extra_body={"thinking": {"type": "disabled"}},
            )
            text = response.choices[0].message.content or ""
            return text.strip()[:3000] or self.fallback(snapshot, "模型返回空内容")
        except Exception as exc:  # noqa: BLE001
            logger.warning("spot accumulation explanation failed: %s", exc)
            return self.fallback(snapshot, type(exc).__name__)
