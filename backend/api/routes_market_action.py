"""Market Action Analyzer · REST API

当前阶段（P1+P2 Debug）：
- GET /api/market-action/facts?coin=BTC         返回 MarketActionFacts（14 字段）
- GET /api/market-action/facts/raw?coin=BTC     返回部分原始 state 调试快照
- GET /api/market-action/footprint?coin=BTC     返回原始 footprint buckets（调试）

AI Arbiter / 报告接口留作 P3 阶段。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/market-action", tags=["market-action"])

_engine = None


def set_engine(engine) -> None:
    global _engine
    _engine = engine


def _get_state(coin: str):
    if _engine is None:
        raise HTTPException(status_code=503, detail="engine not ready")
    state = _engine._states.get(coin.upper())
    if state is None:
        raise HTTPException(status_code=404, detail=f"coin not supported: {coin}")
    return state


@router.get("/facts")
async def get_facts(coin: str = Query("BTC")) -> dict[str, Any]:
    """返回 MarketActionFacts（AI 输入契约，14 字段）"""
    state = _get_state(coin)
    from processors.market_action.facts_collector import collect
    facts = collect(state)
    return facts.model_dump()


@router.get("/facts/summary")
async def get_facts_summary(coin: str = Query("BTC")) -> dict[str, Any]:
    """facts 精简版：只返回顶层字段名 + data_quality + missing，快速检查覆盖度"""
    state = _get_state(coin)
    from processors.market_action.facts_collector import collect
    facts = collect(state)
    dump = facts.model_dump()
    missing_set = set(dump.get("missing", []))
    coverage: dict[str, bool] = {}
    for key in (
        "price", "oi", "funding", "cvd_contract", "cvd_spot", "liquidation_flow",
        "basis", "orderbook", "liq_map_clusters", "liq_sweep_recent",
        "price_context", "footprint", "taker_flow_5m", "options",
    ):
        v = dump.get(key)
        coverage[key] = (v is not None) and (key not in missing_set)
    return {
        "coin": dump.get("coin"),
        "timestamp": dump.get("timestamp"),
        "data_quality": dump.get("data_quality"),
        "missing": dump.get("missing"),
        "coverage": coverage,
        "derived_labels": {
            "oi_price_coherence": dump.get("oi_price_coherence"),
            "spot_contract_coherence": dump.get("spot_contract_coherence"),
            "funding_trend": dump.get("funding_trend"),
        },
    }


@router.get("/footprint")
async def get_footprint(coin: str = Query("BTC")) -> dict[str, Any]:
    """返回 state 上的原始 footprint 数据（调试用）"""
    state = _get_state(coin)
    return {
        "coin": coin.upper(),
        "last_ts": getattr(state, "footprint_last_ts", None),
        "contract": list(getattr(state, "footprint_contract", []) or []),
        "spot": list(getattr(state, "footprint_spot", []) or []),
    }
