"""REST API 路由"""

from __future__ import annotations

import logging
import time
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from config.settings import get_settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api")

_engine = None


def set_engine(engine):
    """由 main.py 启动时注入引擎实例"""
    global _engine
    _engine = engine


@router.get("/coins")
async def list_coins():
    """返回支持的币种列表"""
    settings = get_settings()
    return {
        "coins": settings.supported_coins,
        "default": settings.default_coin,
    }


@router.get("/market/{coin}")
async def get_market_data(coin: str):
    """获取指定币种的完整市场数据快照"""
    if not _engine:
        raise HTTPException(503, "Engine not ready")
    coin = coin.upper()
    if coin not in get_settings().supported_coins:
        raise HTTPException(400, f"Unsupported coin: {coin}")

    data = _engine.get_snapshot(coin)
    if not data:
        raise HTTPException(503, f"No data for {coin}")
    return data


@router.get("/factors/{coin}")
async def get_factor_cards(coin: str):
    """获取因子卡片数据"""
    if not _engine:
        raise HTTPException(503, "Engine not ready")
    coin = coin.upper()
    temp = _engine.get_temperature(coin)
    if not temp:
        raise HTTPException(503, f"No temperature data for {coin}")
    return temp.model_dump()


@router.get("/levels/{coin}")
async def get_levels(coin: str):
    """获取关键价位分析"""
    if not _engine:
        raise HTTPException(503, "Engine not ready")
    coin = coin.upper()
    levels = _engine.get_levels(coin)
    if not levels:
        raise HTTPException(503, f"No level data for {coin}")
    return levels.model_dump()


@router.get("/liquidation/{coin}")
async def get_liquidation_map(coin: str, cycle: str = Query("24h")):
    """获取清算地图数据"""
    if not _engine:
        raise HTTPException(503, "Engine not ready")
    coin = coin.upper()
    liq = _engine.get_liquidation_map(coin, cycle)
    if not liq:
        raise HTTPException(503, f"No liquidation data for {coin}")
    return liq.model_dump()


@router.get("/waterfall/{coin}")
async def get_waterfall(coin: str):
    """获取多空归因瀑布图数据"""
    if not _engine:
        raise HTTPException(503, "Engine not ready")
    coin = coin.upper()
    wf = _engine.get_waterfall(coin)
    if not wf:
        raise HTTPException(503, f"No waterfall data for {coin}")
    return wf.model_dump()


@router.post("/ai/analyze/{coin}")
async def trigger_ai_analysis(coin: str):
    """手动触发 AI 分析"""
    if not _engine:
        raise HTTPException(503, "Engine not ready")
    coin = coin.upper()

    if not _engine.ai_available:
        raise HTTPException(503, "AI service not configured")

    cooldown = get_settings().ai.cooldown_sec
    last_ts = _engine.get_last_ai_ts(coin)
    if last_ts and time.time() - last_ts < cooldown:
        remaining = int(cooldown - (time.time() - last_ts))
        raise HTTPException(429, f"AI cooldown: {remaining}s remaining")

    try:
        result = await _engine.run_ai_analysis(coin)
        return result.model_dump()
    except Exception as e:
        logger.error("AI analysis endpoint error | coin=%s", coin, exc_info=True)
        raise HTTPException(500, f"AI analysis failed: {str(e)}")


@router.get("/ai/history/{coin}")
async def get_ai_history(coin: str):
    """获取 AI 分析历史"""
    if not _engine:
        raise HTTPException(503, "Engine not ready")
    coin = coin.upper()
    history = _engine.get_ai_history(coin)
    return {"coin": coin, "analyses": [h.model_dump() for h in history]}


@router.get("/health")
async def health_check():
    """数据源健康状态"""
    if not _engine:
        return {"status": "starting"}
    return {
        "status": "running",
        "sources": _engine.get_source_health(),
        "ai_available": _engine.ai_available,
        "ai_provider": get_settings().ai.active,
    }


@router.get("/logs")
async def get_logs(
    level: Optional[str] = Query(None, description="Filter by level: INFO, WARNING, ERROR"),
    limit: int = Query(200, ge=1, le=500),
    keyword: Optional[str] = Query(None, description="Filter by keyword"),
):
    """获取后端运行日志（内存缓存，最近500条）"""
    from main import log_buffer

    logs = list(log_buffer)

    if level:
        level_upper = level.upper()
        logs = [l for l in logs if l["level"] == level_upper]

    if keyword:
        kw_lower = keyword.lower()
        logs = [l for l in logs if kw_lower in l["msg"].lower() or kw_lower in l["name"].lower()]

    return {"total": len(logs), "logs": logs[-limit:]}
