"""Market Action Analyzer · REST API

端点总览：
  Debug / 数据侧
    GET  /api/market-action/facts               返回 MarketActionFacts（14 字段）
    GET  /api/market-action/facts/summary       返回覆盖度 + 派生标签
    GET  /api/market-action/footprint           返回原始 footprint buckets

  AI 报告
    GET  /api/market-action/report              最新一份 MarketActionReport
    GET  /api/market-action/report/history      最近 N 份报告（时间倒序）
    GET  /api/market-action/report/all          三币最新报告聚合
    POST /api/market-action/run                 手动触发一次 AI 分析

默认会携带 prompt_debug（含 system/user/raw_response），前端用于展示"本轮喂给 AI
的原始数据"。如需精简可加参数 ?slim=1 剥离 prompt_debug 与 facts_snapshot。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/market-action", tags=["market-action"])

_engine = None


def set_engine(engine) -> None:
    global _engine
    _engine = engine


def _require_engine():
    if _engine is None:
        raise HTTPException(status_code=503, detail="engine not ready")
    return _engine


def _get_state(coin: str):
    engine = _require_engine()
    state = engine._states.get(coin.upper())
    if state is None:
        raise HTTPException(status_code=404, detail=f"coin not supported: {coin}")
    return state


def _dump_report(report, *, slim: bool = False, include_prompt: bool = True) -> Optional[dict]:
    """序列化 Report → dict，按需剥离大字段。

    - slim=True：同时去掉 prompt_debug 和 facts_snapshot（列表 API 默认）
    - include_prompt=False：保留 facts_snapshot 但剥离 prompt_debug
    - 默认完整：保留全部，含 CoT 字段（v4-flash 非思考模式下恒为空，保留兼容 R1 时代快照）
    """
    if report is None:
        return None
    d = report.model_dump()
    if slim or not include_prompt:
        d.pop("prompt_debug", None)
    if slim:
        d.pop("facts_snapshot", None)
    return d


def _staleness_minutes(ts: int | float | None) -> int:
    if not ts:
        return -1
    try:
        return max(0, int((time.time() - float(ts)) / 60))
    except (TypeError, ValueError):
        return -1


# ────────────────────────────────────────────────────────────────────────────
# Debug · Facts / Footprint
# ────────────────────────────────────────────────────────────────────────────

@router.get("/facts")
async def get_facts(coin: str = Query("BTC")) -> dict[str, Any]:
    """返回 MarketActionFacts（AI 输入契约，14 字段）。"""
    state = _get_state(coin)
    from processors.market_action.facts_collector import collect
    facts = collect(state)
    return facts.model_dump()


@router.get("/facts/summary")
async def get_facts_summary(coin: str = Query("BTC")) -> dict[str, Any]:
    """facts 精简版：只返回顶层字段名 + data_quality + missing。"""
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
    """返回 state 上的原始 footprint 数据（调试用）。"""
    state = _get_state(coin)
    return {
        "coin": coin.upper(),
        "last_ts": getattr(state, "footprint_last_ts", None),
        "contract": list(getattr(state, "footprint_contract", []) or []),
        "spot": list(getattr(state, "footprint_spot", []) or []),
    }


# ────────────────────────────────────────────────────────────────────────────
# AI 报告
# ────────────────────────────────────────────────────────────────────────────

def _default_include_prompt() -> bool:
    if _engine is None:
        return True
    try:
        return bool(_engine._settings.market_action.include_prompt_in_api)
    except Exception:
        return True


@router.get("/report")
async def get_report(
    coin: str = Query("BTC"),
    slim: int = Query(0, ge=0, le=1, description="1=去除 prompt_debug + facts_snapshot"),
    include_prompt: Optional[int] = Query(
        None, ge=0, le=1, description="显式控制 prompt_debug（覆盖全局默认）",
    ),
) -> dict[str, Any]:
    """返回该币种最新一份 MarketActionReport。

    - 返回 404 情况：引擎还没跑过 MAA 分析
    - `stale_minutes` 字段由服务端实时计算覆盖（方便前端渲染）
    """
    state = _get_state(coin)
    report = getattr(state, "market_action_report", None)
    if report is None:
        raise HTTPException(
            status_code=404,
            detail=f"no market action report for {coin} yet",
        )
    inc = bool(include_prompt) if include_prompt is not None else _default_include_prompt()
    dumped = _dump_report(report, slim=bool(slim), include_prompt=inc)
    assert dumped is not None  # type narrowing
    dumped["stale_minutes"] = _staleness_minutes(
        getattr(state, "market_action_last_ts", None) or report.timestamp,
    )
    return dumped


@router.get("/report/history")
async def get_report_history(
    coin: str = Query("BTC"),
    limit: int = Query(20, ge=1, le=200),
    slim: int = Query(1, ge=0, le=1, description="默认 1：history 不返回 prompt_debug，节省带宽"),
) -> dict[str, Any]:
    """最近 N 份报告（时间倒序）。history 默认 slim=1 避免响应过大。"""
    state = _get_state(coin)
    hist = list(getattr(state, "market_action_history", []) or [])
    hist.reverse()
    hist = hist[:limit]
    items: list[dict] = []
    include_prompt = not bool(slim)
    for r in hist:
        d = _dump_report(r, slim=bool(slim), include_prompt=include_prompt)
        if d is not None:
            items.append(d)
    return {
        "coin": coin.upper(),
        "count": len(items),
        "items": items,
    }


@router.get("/report/all")
async def get_report_all(
    slim: int = Query(1, ge=0, le=1, description="默认 1：聚合接口 slim 返回"),
) -> dict[str, Any]:
    """三币最新报告聚合（SOL 可能未配置期权等字段）。"""
    engine = _require_engine()
    out: dict[str, Any] = {}
    for ccy in engine._settings.supported_coins:
        state = engine._states.get(ccy)
        if state is None:
            out[ccy] = None
            continue
        report = getattr(state, "market_action_report", None)
        if report is None:
            out[ccy] = None
            continue
        d = _dump_report(report, slim=bool(slim), include_prompt=not bool(slim))
        if d is not None:
            d["stale_minutes"] = _staleness_minutes(
                getattr(state, "market_action_last_ts", None) or report.timestamp,
            )
        out[ccy] = d
    return {
        "coins": out,
        "generated_at": int(time.time()),
    }


# ────────────────────────────────────────────────────────────────────────────
# Phase 5 · 事后评估（T+4h/8h/24h 兑现率 + Confidence 校准）
# ────────────────────────────────────────────────────────────────────────────

@router.get("/eval")
async def get_eval_summary(
    coin: str = Query("BTC"),
    refresh: int = Query(0, ge=0, le=1, description="1=强制重算（忽略缓存，会阻塞请求数秒）"),
    window_days: int = Query(7, ge=1, le=30),
) -> dict[str, Any]:
    """返回 MAA 事后评估 summary：
      - 各 horizon (4h/8h/24h) 的命中率
      - Confidence 分桶校准
      - Per-scenario 准确率
      - 最近 20 条样本

    默认读 engine 缓存（每 30 分钟刷新一次）；refresh=1 强制重算。
    """
    engine = _require_engine()
    ccy = coin.upper()
    if ccy not in engine._settings.supported_coins:
        raise HTTPException(status_code=404, detail=f"coin not supported: {coin}")

    cache = getattr(engine, "_maa_eval_summary", {}) or {}
    cached = cache.get(ccy)

    need_compute = bool(refresh) or cached is None or cached.get("window_days") != window_days
    if need_compute:
        try:
            from monitoring import maa_eval
            summary = maa_eval.evaluate_coin(ccy, window_days=window_days)
            payload = summary.to_dict()
            if not refresh:  # 只有默认 window 时才更新全局缓存
                cache[ccy] = payload
        except Exception as e:
            logger.error("[MAA-Eval] on-demand failed | coin=%s", ccy, exc_info=True)
            return {
                "ready": False,
                "coin": ccy,
                "error": f"{type(e).__name__}: {e}",
            }
    else:
        payload = cached

    return {
        "ready": True,
        "coin": ccy,
        "summary": payload,
        "last_eval_ts": int(getattr(engine, "_maa_eval_last_ts", 0) or 0),
    }


@router.post("/run")
async def run_once(coin: str = Query("BTC")) -> dict[str, Any]:
    """手动触发一次 AI 分析（异步，返回后任务仍在后台执行）。

    - 如果该币种已有任务运行中，返回 409
    - 不等待结果；通过 `/report` 轮询或 WS `market_action_report` 事件拿结果
    """
    engine = _require_engine()
    ccy = coin.upper()
    if ccy not in engine._settings.supported_coins:
        raise HTTPException(status_code=404, detail=f"coin not supported: {coin}")
    if not engine.market_action_available:
        raise HTTPException(status_code=503, detail="MAA arbiter not available")
    try:
        await engine.fire_market_action_analysis(ccy)
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return {
        "coin": ccy,
        "status": "dispatched",
        "started_at": int(time.time()),
    }
