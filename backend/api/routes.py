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
async def get_liquidation_map(coin: str, cycle: str = Query("1d")):
    """获取清算地图数据（周期: 1d/3d/7d/30d）"""
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
    """触发 AI 分析（fire-and-forget），结果通过 WebSocket 推送。"""
    coin_raw = coin
    logger.info("AI endpoint request received | coin=%s", coin_raw)

    if not _engine:
        logger.warning("AI endpoint rejected: engine not ready | coin=%s", coin_raw)
        raise HTTPException(503, "Engine not ready")
    coin = coin.upper()

    if not _engine.ai_available:
        logger.warning("AI endpoint rejected: AI not configured (no API key?) | coin=%s", coin)
        raise HTTPException(503, "AI service not configured")

    if _engine.is_ai_running(coin):
        logger.info("AI endpoint rejected: already running | coin=%s", coin)
        raise HTTPException(429, "AI analysis already in progress")

    cooldown = get_settings().ai.cooldown_sec
    last_ts = _engine.get_last_ai_ts(coin)
    if last_ts and time.time() - last_ts < cooldown:
        remaining = int(cooldown - (time.time() - last_ts))
        logger.info("AI endpoint rejected: cooldown | coin=%s remaining=%ds", coin, remaining)
        raise HTTPException(429, f"AI cooldown: {remaining}s remaining")

    try:
        await _engine.fire_ai_analysis(coin)
        logger.info("AI endpoint dispatched (async) | coin=%s", coin)
        return {"status": "processing", "coin": coin}
    except Exception as e:
        logger.error(
            "AI endpoint dispatch error | coin=%s | %s: %s",
            coin, type(e).__name__, str(e), exc_info=True,
        )
        raise HTTPException(500, f"AI analysis failed: {str(e)}")


@router.get("/ai/history/{coin}")
async def get_ai_history(coin: str, limit: int = Query(5, ge=1, le=50)):
    """获取 AI 分析历史（按时间倒序，默认最近 5 条）"""
    if not _engine:
        raise HTTPException(503, "Engine not ready")
    coin = coin.upper()
    history = _engine.get_ai_history(coin)
    history.sort(key=lambda h: h.ts, reverse=True)
    return {"coin": coin, "analyses": [h.model_dump() for h in history[:limit]]}


@router.get("/ai/detail/{coin}/{ts}")
async def get_ai_detail(coin: str, ts: int):
    """按时间戳精确查询单条 AI 分析结果"""
    if not _engine:
        raise HTTPException(503, "Engine not ready")
    coin = coin.upper()
    for h in _engine.get_ai_history(coin):
        if h.ts == ts:
            return h.model_dump()
    raise HTTPException(404, f"Analysis not found: {coin}/{ts}")


@router.get("/key-levels/history/{coin}")
async def get_kl_history(coin: str, limit: int = Query(5, ge=1, le=50)):
    """获取关键位历史快照（按时间倒序，默认最近 5 条）"""
    if not _engine:
        raise HTTPException(503, "Engine not ready")
    coin = coin.upper()
    history = _engine.get_kl_history(coin)
    history.sort(key=lambda h: h.ts, reverse=True)
    return {"coin": coin, "snapshots": [h.model_dump() for h in history[:limit]]}


@router.get("/key-levels/detail/{coin}/{ts}")
async def get_kl_detail(coin: str, ts: int):
    """按时间戳精确查询单条关键位快照"""
    if not _engine:
        raise HTTPException(503, "Engine not ready")
    coin = coin.upper()
    for h in _engine.get_kl_history(coin):
        if h.ts == ts:
            return h.model_dump()
    raise HTTPException(404, f"KL snapshot not found: {coin}/{ts}")


@router.get("/key-levels/{coin}")
async def get_key_levels_v2(coin: str):
    """获取 V2 关键位完整快照（详情页 + 大屏）"""
    if not _engine:
        raise HTTPException(503, "Engine not ready")
    coin = coin.upper()
    if coin not in get_settings().supported_coins:
        raise HTTPException(400, f"Unsupported coin: {coin}")

    state = _engine._states.get(coin)
    if not state or not state.key_level_snapshot_v2:
        raise HTTPException(503, f"No key level data for {coin}")
    return state.key_level_snapshot_v2.model_dump()


@router.get("/execution-plan/{coin}")
async def get_execution_plan(coin: str):
    """数学引擎 L4 输出：ExecutionPlan（action / 仓位 / 红绿灯 / 分数 / 贡献源）

    前端 ExecutionPlanCard（D06）与 AI 双引擎融合（D15）消费此接口。
    数据可能为 None（首轮 _recompute 尚未生成），返回 {"ready": false}。
    """
    if not _engine:
        raise HTTPException(503, "Engine not ready")
    coin = coin.upper()
    state = _engine._states.get(coin) if hasattr(_engine, "_states") else None
    # D06：每次被前端 ExecutionPlanCard 消费时静默上报
    try:
        from utils.decision_tracker import D, get_tracker
        get_tracker().mark(
            D.D06_BEGINNER_UI,
            status="ok" if (state and state.execution_plan) else "warn",
            log=False,
            coin=coin,
            plan_ready=bool(state and state.execution_plan),
        )
    except Exception:
        logger.debug("[D06] tracker mark failed", exc_info=True)

    if state is None or state.execution_plan is None:
        return {"ready": False, "coin": coin}
    return {"ready": True, "plan": state.execution_plan.model_dump()}


@router.get("/signal-bus/stats")
async def get_signal_bus_stats():
    """信号总线运行统计（调试 / D02 可视化）"""
    try:
        from processors.signal_bus import get_bus
        return get_bus().stats()
    except Exception as e:
        logger.warning("signal_bus stats failed: %s", e)
        raise HTTPException(500, f"signal_bus unavailable: {e}")


@router.get("/ai-trader-report/{coin}")
async def get_ai_trader_report(coin: str):
    """D14：AI 引擎 L7 输出 —— AITraderReport。

    包含：7 板块 AIFactorMatrix / AIKeyLevelInterpretation / AITradingPlan
    / agreement_with_math_engine / narrative_impact 等完整结构化报告。

    数据可能为 None（首轮 AI 尚未运行或 build 失败），返回 {"ready": false}。
    """
    if not _engine:
        raise HTTPException(503, "Engine not ready")
    coin = coin.upper()
    state = _engine._states.get(coin) if hasattr(_engine, "_states") else None
    if state is None or getattr(state, "ai_trader_report", None) is None:
        return {"ready": False, "coin": coin}
    return {"ready": True, "report": state.ai_trader_report.model_dump()}


@router.get("/final-decision/{coin}")
async def get_final_decision(coin: str):
    """D15：双引擎融合层 L7.5 输出 —— FinalDecision（对外主视图）。

    - consensus_level / consensus_stars / traffic_light / final_score
    - math_brief + ai_brief 两引擎并列
    - recommended_action + 入场/止损/止盈融合参数
    - divergence_summary（conflict 时）
    """
    if not _engine:
        raise HTTPException(503, "Engine not ready")
    coin = coin.upper()
    state = _engine._states.get(coin) if hasattr(_engine, "_states") else None
    if state is None or getattr(state, "final_decision", None) is None:
        return {"ready": False, "coin": coin}
    return {"ready": True, "decision": state.final_decision.model_dump()}


@router.get("/divergence-stats/{coin}")
async def get_divergence_stats(coin: str):
    """D04 扩展：双引擎分歧回测闭环统计。

    返回：
      - stats: list[DivergenceStats]（按 divergence_type 聚合，样本多的在前）
      - recent_samples: 最近 20 条分歧样本原始轨迹（调试用）

    当 consensus=conflict 时，融合层会查询此仓产出 historical_divergence
    显示到 FinalDecisionCard。样本 <10 视为参考性低。
    """
    coin = coin.upper()
    try:
        from processors.divergence_backfill import get_divergence_store
        store = get_divergence_store()
        stats = [s.model_dump() for s in store.get_stats_list(coin)]
        full = store.snapshot_dict(coin).get(coin, [])
        recent = sorted(full, key=lambda s: s.get("created_ts", 0), reverse=True)[:20]
        return {
            "ready": True,
            "coin": coin,
            "stats": stats,
            "total_samples": len(full),
            "recent_samples": recent,
        }
    except Exception as e:
        logger.warning("divergence_stats route failed | coin=%s err=%s", coin, e)
        return {"ready": False, "coin": coin, "error": str(e)}


@router.get("/signal-pnl/{coin}")
async def get_signal_pnl(
    coin: str,
    origin: str = "all",
    tier: str = "all",
    limit: int = 50,
    window: int = 200,
):
    """P2.1 · 信号 PnL 回放（72h 价格回放）

    返回：
      - stats: 当前筛选条件下的聚合统计
      - origin_breakdown: math / ai / final 三引擎并列对比
      - tier_breakdown: A / B / C 档分档对比（已筛选 origin 后）
      - recent: 最近 N 条样本轨迹（含 entry_filled / outcome / MFE / MAE）

    参数：
      - origin: all | math | ai | final
      - tier:   all | A | B | C
      - limit:  最近明细条数，1-200
      - window: 统计窗口（最近 N 条已 resolved 样本），1-500
    """
    coin = coin.upper()
    origin = origin.strip().lower() if origin else "all"
    tier_u = tier.strip().upper() if tier else "all"
    origin_filter = None if origin == "all" else origin
    tier_filter = None if tier_u == "all" else tier_u
    limit = max(1, min(200, int(limit or 50)))
    window = max(1, min(500, int(window or 200)))
    try:
        from processors.signal_pnl_tracker import get_signal_pnl_tracker
        tracker = get_signal_pnl_tracker()
        stats = tracker.get_stats(
            coin, origin=origin_filter, tier=tier_filter, window=window,
        )
        origin_breakdown = tracker.get_origin_breakdown(coin)
        tier_breakdown = tracker.get_tier_breakdown(coin, origin=origin_filter)
        recent = tracker.get_recent(coin, limit=limit, origin=origin_filter)
        return {
            "ready": True,
            "coin": coin,
            "stats": stats,
            "origin_breakdown": origin_breakdown,
            "tier_breakdown": tier_breakdown,
            "recent": recent,
        }
    except Exception as e:
        logger.warning("signal_pnl route failed | coin=%s err=%s", coin, e)
        return {"ready": False, "coin": coin, "error": str(e)}


@router.get("/ai-quality/{coin}")
async def get_ai_quality(coin: str, limit: int = 50):
    """P1.8a · AI 分析质量监控

    返回：
      - stats: 聚合统计（命中率、冲突率、平均延迟、趋势提示）
      - recent: 最近 N 条原始记录（调试用，N ≤ 50）

    统计窗口：默认最近 50 次分析；`limit` 可 1-200 之间。

    指标说明：
      - ai_json_hit_rate: AI 附录成功覆写 matrix 的比率
      - ai_plans_hit_rate: AI 附录直出 trading_plans 的比率
      - bias_consistency_rate: AI JSON bias 与 markdown signal 一致率
      - internal_conflict_rate: 内部矛盾率（冲突越高说明 prompt 需要迭代）
      - math_agreement_rate: 与数学引擎一致的比率
    """
    coin = coin.upper()
    limit = max(1, min(200, int(limit or 50)))
    try:
        from processors.ai_quality_ledger import get_ai_quality_ledger
        ledger = get_ai_quality_ledger()
        stats = ledger.get_stats(coin)
        recent = ledger.get_recent(coin, limit=limit)
        return {
            "ready": True,
            "coin": coin,
            "stats": stats,
            "recent": recent,
        }
    except Exception as e:
        logger.warning("ai_quality route failed | coin=%s err=%s", coin, e)
        return {"ready": False, "coin": coin, "error": str(e)}


@router.get("/plan-backtest/{coin}")
async def get_plan_backtest(coin: str):
    """D04：数学引擎 ExecutionPlan 的实盘回测统计。

    返回 BacktestStats（total/triggered/tp1_hit/sl_hit/win_rate/avg_rr …）
    和最近 20 笔交易的原始轨迹，用于前端"历史胜率"提示与调试。
    """
    try:
        from processors.plan_backtest import get_plan_backtest_store
        store = get_plan_backtest_store()
        coin_u = coin.upper()
        stats = store.get_stats(coin_u)
        return {
            "stats": stats.model_dump(),
            "trades": store.snapshot_dict(coin_u).get(coin_u, []),
        }
    except Exception as e:
        logger.warning("plan_backtest query failed: %s", e)
        raise HTTPException(500, f"plan_backtest unavailable: {e}")


@router.get("/range-signal/{coin}")
async def get_range_signal(coin: str):
    """获取箱体信号完整数据（详情页）"""
    if not _engine:
        raise HTTPException(503, "Engine not ready")
    coin = coin.upper()
    if coin not in get_settings().supported_coins:
        raise HTTPException(400, f"Unsupported coin: {coin}")

    state = _engine._states.get(coin)
    if not state or not state.range_signal:
        raise HTTPException(503, f"No range signal data for {coin}")
    return state.range_signal.model_dump()


@router.get("/backtest/stats/{coin}")
async def get_backtest_stats(coin: str):
    """获取轻量级回测统计摘要"""
    if not _engine:
        raise HTTPException(503, "Engine not ready")
    coin = coin.upper()
    return _engine.compute_backtest_stats(coin)


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


@router.get("/health/decisions")
async def decisions_health():
    """D1-D17 架构决策落地状态。

    返回 17 个架构决策点（L2 Regime / L3 Signal Bus / L4 Synthesizer /
    L5 Safety Gate / L6 News Pipeline / L7 AI Trader / L7.5 Fusion 等）
    的 pending / ok / warn / fail 分布与运行时指标。
    用于开发阶段对"每一个架构步骤是否真的在跑、效果如何"进行溯源。
    """
    try:
        from utils.decision_tracker import get_tracker
        return get_tracker().get_summary_dict()
    except Exception as e:
        logger.warning("decisions health endpoint failed: %s", e)
        raise HTTPException(500, f"decision_tracker unavailable: {e}")


@router.get("/replay/list")
async def replay_list(
    coin: Optional[str] = None,
    since_ts: Optional[int] = None,
    until_ts: Optional[int] = None,
    limit: int = 200,
):
    """P2.4 · 历史快照列表（按 coin + 时间区间）

    只返回帧头（ts/coin/price/brief/is_present flags），不返回全量内容；
    详情请用 `/api/replay/frame`。
    """
    try:
        from processors.snapshot_archiver import get_snapshot_archiver
        arch = get_snapshot_archiver()
        items = arch.read_range(
            coin=coin.upper() if coin else None,
            since_ts=since_ts, until_ts=until_ts, limit=int(limit or 200),
        )
        return {"ready": True, "count": len(items), "items": items}
    except Exception as e:
        logger.warning("replay list failed: %s", e)
        return {"ready": False, "items": [], "error": str(e)}


@router.get("/replay/frame")
async def replay_frame(coin: str, ts: int):
    """P2.4 · 按 (coin, ts) 精确读取某一帧的完整四件套"""
    try:
        from processors.snapshot_archiver import get_snapshot_archiver
        arch = get_snapshot_archiver()
        frame = arch.read_frame(coin.upper(), int(ts))
        if frame is None:
            raise HTTPException(404, f"frame not found coin={coin} ts={ts}")
        return {"ready": True, "frame": frame}
    except HTTPException:
        raise
    except Exception as e:
        logger.warning("replay frame failed coin=%s ts=%s err=%s", coin, ts, e)
        raise HTTPException(500, str(e))


@router.get("/health/summary")
async def decisions_health_summary():
    """P2.2 · Decision Health 聚合摘要（供前端徽章条 / 告警弹窗使用）

    返回：
      - ts, overall: ok|warn|fail（最坏状态）
      - green/yellow/red: 17 项分色计数
      - degraded: warn/fail 项列表（含 id / title / status / duration / metrics）
      - events: 最近 50 条降级事件（从 pending 变色时记录）
    """
    try:
        from monitoring.health_aggregator import get_health_aggregator
        return get_health_aggregator().summary()
    except Exception as e:
        logger.warning("health summary endpoint failed: %s", e)
        raise HTTPException(500, f"health aggregator unavailable: {e}")


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
