"""REST API 路由"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Response

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


@router.get("/liquidation/{coin}")
async def get_liquidation_map(coin: str, cycle: str = Query("1d")):
    """获取清算地图数据（周期: 1d/7d/30d；旧版 3d 已下线）"""
    if not _engine:
        raise HTTPException(503, "Engine not ready")
    coin = coin.upper()
    liq = _engine.get_liquidation_map(coin, cycle)
    if not liq:
        raise HTTPException(503, f"No liquidation data for {coin}")
    return liq.model_dump()


@router.get("/orderflow/{coin}/hourly")
async def get_orderflow_hourly(
    coin: str,
    market: Optional[str] = Query(None, description="spot | futures，缺省返回两者"),
    hours: int = Query(72, ge=1, le=2160, description="回看小时数"),
):
    """订单流小时桶：taker 买卖 USD、净额、大额挂单被动成交、whale 主动成交。

    数据来自本地聚合（P2 orderflow_stats），零 Coinglass 配额；
    coverage_pct < 1 表示该小时数据有断档。
    """
    coin = coin.upper()
    if coin not in get_settings().supported_coins:
        raise HTTPException(400, f"Unsupported coin: {coin}")
    if market is not None and market not in ("spot", "futures"):
        raise HTTPException(400, "market must be 'spot' or 'futures'")
    from processors.orderflow_stats import get_orderflow_store
    now = int(time.time())
    rows = get_orderflow_store().query_hourly(
        coin, market=market, start_ts=now - hours * 3600, limit=hours * 2 + 4,
    )
    return {"coin": coin, "market": market, "hours": hours, "rows": rows}


@router.get("/orderflow/{coin}/daily")
async def get_orderflow_daily(
    coin: str,
    market: Optional[str] = Query(None, description="spot | futures，缺省返回两者"),
    days: int = Query(90, ge=1, le=400, description="回看天数"),
):
    """订单流日桶（UTC+8 日界），字段同小时桶 + hours_covered。"""
    coin = coin.upper()
    if coin not in get_settings().supported_coins:
        raise HTTPException(400, f"Unsupported coin: {coin}")
    if market is not None and market not in ("spot", "futures"):
        raise HTTPException(400, "market must be 'spot' or 'futures'")
    from processors.orderflow_stats import get_orderflow_store
    rows = get_orderflow_store().query_daily(coin, market=market, limit=days * 2 + 4)
    return {"coin": coin, "market": market, "days": days, "rows": rows}


@router.get("/orderflow/{coin}/whales")
async def get_orderflow_whales(
    coin: str,
    hours: int = Query(24, ge=1, le=24, description="回看小时数（deque 只留 24h）"),
    limit: int = Query(100, ge=1, le=500),
):
    """近 24h whale 单笔明细（Binance aggTrade 阈值过滤，内存 deque）。

    进程重启后 deque 清空，属 best-effort 明细流；
    小时/日级累计请用 hourly/daily 端点的 whale_buy_usd/whale_sell_usd。
    """
    coin = coin.upper()
    if coin not in get_settings().supported_coins:
        raise HTTPException(400, f"Unsupported coin: {coin}")
    from sources.binance_trades_ws import get_trades_ws
    ws = get_trades_ws()
    if ws is None:
        return {"coin": coin, "available": False, "rows": []}
    rows = ws.recent_whales(coin, within_sec=hours * 3600)
    rows.sort(key=lambda w: w["ts"], reverse=True)
    return {
        "coin": coin,
        "available": True,
        "stats": ws.stats(),
        "rows": rows[:limit],
    }


@router.get("/orderflow/{coin}/whale-summary")
async def get_orderflow_whale_summary(
    coin: str,
    market: Optional[str] = Query(None, description="spot | futures，缺省合并两市场"),
):
    """鲸鱼多周期滚动汇总（1h/2h/4h/24h）+ 重启安全的近 24h 桶级均价。

    - windows：来自内存 whale deque 的精确滚动窗口（买/卖金额、笔数、
      VWAP、价格区间）。进程重启后从零累积，covered=false 表示
      data_age_sec 尚不足该窗口时长（数值只是下限，不伪造）。
    - h24_bucket：来自 SQLite 小时桶 whale usd/qty 累计列（跨重启保留），
      供"现价 vs 鲸鱼平均买入价"这类参考锚点使用。
    """
    coin = coin.upper()
    if coin not in get_settings().supported_coins:
        raise HTTPException(400, f"Unsupported coin: {coin}")
    if market is not None and market not in ("spot", "futures"):
        raise HTTPException(400, "market must be 'spot' or 'futures'")

    from sources.binance_trades_ws import get_trades_ws
    ws = get_trades_ws()
    now = time.time()
    data_age = ws.data_age_sec() if ws is not None else 0.0

    windows: list[dict] = []
    if ws is not None:
        whales = ws.recent_whales(coin, within_sec=24 * 3600)
        if market:
            whales = [w for w in whales if w.get("market") == market]
        for hours_win in (1, 2, 4, 24):
            cutoff = now - hours_win * 3600
            buy_usd = sell_usd = buy_qty = sell_qty = 0.0
            buy_n = sell_n = 0
            p_min = p_max = 0.0
            for w in whales:
                if w["ts"] < cutoff:
                    continue
                px = float(w.get("price", 0) or 0)
                if px > 0:
                    p_max = max(p_max, px)
                    p_min = px if p_min <= 0 else min(p_min, px)
                if w.get("side") == "buy":
                    buy_usd += float(w.get("usd", 0) or 0)
                    buy_qty += float(w.get("qty", 0) or 0)
                    buy_n += 1
                else:
                    sell_usd += float(w.get("usd", 0) or 0)
                    sell_qty += float(w.get("qty", 0) or 0)
                    sell_n += 1
            windows.append({
                "hours": hours_win,
                "buy_usd": buy_usd,
                "sell_usd": sell_usd,
                "net_usd": buy_usd - sell_usd,
                "buy_count": buy_n,
                "sell_count": sell_n,
                "buy_vwap": buy_usd / buy_qty if buy_qty > 0 else 0.0,
                "sell_vwap": sell_usd / sell_qty if sell_qty > 0 else 0.0,
                "price_min": p_min,
                "price_max": p_max,
                "covered": data_age >= hours_win * 3600,
            })

    # 重启安全的近 24h 桶级汇总（whale qty 列为 P4 新增，旧桶为 0 → vwap=0）
    from processors.orderflow_stats import get_orderflow_store
    h_rows = get_orderflow_store().query_hourly(
        coin, market=market, start_ts=int(now) - 24 * 3600, limit=60,
    )
    b_usd = sum(float(r.get("whale_buy_usd", 0) or 0) for r in h_rows)
    s_usd = sum(float(r.get("whale_sell_usd", 0) or 0) for r in h_rows)
    b_qty = sum(float(r.get("whale_buy_qty", 0) or 0) for r in h_rows)
    s_qty = sum(float(r.get("whale_sell_qty", 0) or 0) for r in h_rows)
    lows = [float(r.get("price_low", 0) or 0) for r in h_rows]
    lows = [v for v in lows if v > 0]
    highs = [float(r.get("price_high", 0) or 0) for r in h_rows]
    h24_bucket = {
        "buy_usd": b_usd,
        "sell_usd": s_usd,
        "net_usd": b_usd - s_usd,
        "buy_vwap": b_usd / b_qty if b_qty > 0 else 0.0,
        "sell_vwap": s_usd / s_qty if s_qty > 0 else 0.0,
        "price_low": min(lows) if lows else 0.0,
        "price_high": max(highs) if highs else 0.0,
    }

    return {
        "coin": coin,
        "market": market,
        "available": ws is not None,
        "data_age_sec": round(data_age, 1),
        "windows": windows,
        "h24_bucket": h24_bucket,
    }


@router.get("/liquidation-heatmap/{coin}")
async def get_liquidation_heatmap(coin: str, range_: str = Query("24h", alias="range")):
    """获取清算热力图（aggregated-heatmap/model1）。

    支持 range：24h / 7d（30d 暂未启用轮询，调用会返回 503）。
    与 `/api/liquidation/{coin}` 的清算地图是两条独立链路；本接口仅供前端
    作为辅助第二视角叠加，不参与 imbalance / clusters 等统计。
    """
    if not _engine:
        raise HTTPException(503, "Engine not ready")
    coin = coin.upper()
    hm = _engine.get_liq_heatmap(coin, range_)
    if not hm:
        raise HTTPException(503, f"No liquidation heatmap for {coin} range={range_}")
    return hm.model_dump()


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


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# V3-M3：V1 vs V2 行为评估对比统计 API
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@router.get("/key-levels/v1v2-stats/{coin}")
async def get_v1v2_stats(
    coin: str,
    window_hours: float = Query(4.0, ge=0.5, le=72.0, description="事后真相窗口（小时）"),
    tolerance_sec: int = Query(600, ge=60, le=3600, description="配对容差（秒）"),
    tier: str = Query("", description="逗号分隔的 tier 过滤，例如 S,A；留空=全部"),
    regime: str = Query("", description="逗号分隔的 regime 过滤，例如 trend_up,range；留空=全部"),
    state: str = Query("", description="逗号分隔的 state 过滤，例如 broken,bounced；留空=全部"),
    truth_atr_mult: float = Query(1.0, ge=0.3, le=3.0, description="真相阈值 ×ATR"),
    ambiguous_band: float = Query(0.3, ge=0.0, le=1.0, description="模糊带 ×ATR"),
    v2_threshold: float = Query(0.5, ge=0.0, le=1.0, description="V2 0-1 二分类阈值"),
    stage_threshold: int = Query(3, ge=1, le=3, description="突破阶段二分类阈值"),
    deduplicate: bool = Query(True, description="按 (level_id,state,state_ts) 去重"),
    require_behavior_eval: bool = Query(True, description="过滤 behavior_eval_available=False 的样本"),
    timeframe_adaptive_window: bool = Query(True, description="future_window 按 lv.timeframe 自适应（1D×24/1W×168）"),
):
    """V1 vs V2 关键位行为对比统计（M3.1 升级：McNemar / Wilson CI / 校准 / 多过滤）。

    数据来源：内存中的 kl_history（由 _auto_kl_snapshot_loop 持续追加）。
    若历史样本不足，返回 sample_size=0 但结构稳定（便于前端容错渲染）。

    Query 参数与 CLI（scripts/behavior_backtest.py）完全对齐，便于前后端复现实验。
    """
    if not _engine:
        raise HTTPException(503, "Engine not ready")
    coin = coin.upper()
    history = _engine.get_kl_history(coin)
    if not history:
        # 仍返回稳定结构，前端可显示"暂无数据"而非崩溃
        return {
            "coin": coin,
            "params": {
                "future_window_sec": int(window_hours * 3600),
                "tolerance_sec": tolerance_sec,
                "truth_atr_mult": truth_atr_mult,
                "ambiguous_band": ambiguous_band,
                "v2_threshold": v2_threshold,
                "breakout_stage_threshold": stage_threshold,
                "deduplicate_events": deduplicate,
                "require_behavior_eval": require_behavior_eval,
                "timeframe_adaptive_window": timeframe_adaptive_window,
            },
            "total_records": 0,
            "tier_filter": [],
            "regime_filter": [],
            "state_filter": [],
            "history_size": 0,
            "stats": {
                "bounce_quality": _empty_stats_dict("bounce_quality"),
                "breakout_stage": _empty_stats_dict("breakout_stage"),
                "fake_break": _empty_stats_dict("fake_break"),
            },
        }

    def _split(s: str) -> list[str] | None:
        if not s.strip():
            return None
        return [t.strip() for t in s.split(",") if t.strip()]

    tier_filter = _split(tier)
    if tier_filter:
        tier_filter = [t.upper() for t in tier_filter]
    regime_filter = _split(regime)
    state_filter = _split(state)

    from processors.behavior_backtest_engine import run_full_comparison
    result = run_full_comparison(
        history,
        coin=coin,
        future_window_sec=int(window_hours * 3600),
        tolerance_sec=tolerance_sec,
        truth_atr_mult=truth_atr_mult,
        ambiguous_band=ambiguous_band,
        v2_threshold=v2_threshold,
        breakout_stage_threshold=stage_threshold,
        tier_filter=tier_filter,
        regime_filter=regime_filter,
        state_filter=state_filter,
        deduplicate_events=deduplicate,
        require_behavior_eval=require_behavior_eval,
        timeframe_adaptive_window=timeframe_adaptive_window,
    )
    result["history_size"] = len(history)
    return result


@router.get("/key-levels/behavior-eval-health")
async def get_behavior_eval_health():
    """V3-M3.1：行为评估模块健康度（运行时观测）。

    暴露 in-memory 计数器（重启即清零，正常）：
      - total_calls / success_calls / input_skip_calls
      - level_eval_total / level_eval_success / level_eval_error
      - error_rate（level 维度）
      - avg_latency_ms（evaluate_behavior 平均耗时）
      - last_error_msg / last_error_ts
      - module_version
    """
    from processors.key_level_behavior_eval import get_health_stats
    return get_health_stats()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# V3-M4 · 切换状态 chip（M4-6）+ rolling 滑窗折线（M4-3）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@router.get("/key-levels/behavior-switch-state")
async def get_behavior_switch_state():
    """V3-M4 P0-6：返回各维度当前生效的 V1/V2 版本（默认全 V1）。

    用途：
      - 前端 v1v2-compare 页顶部 chip 区显示
      - M4-5 CLI 检查当前状态
      - M4-2 审计（切换历史从此值的变化推导）
    """
    from processors.behavior_switch_config import get_switch_state
    return {
        "ready": True,
        "state": get_switch_state(),
        "default_version": "V1",
    }


# rolling 缓存（5min TTL；进程内单例）
# key = (coin, window_days, step_hours, max_anchors, future_window_sec)
# value = (computed_at_ts, result_dict)
_ROLLING_CACHE: dict[tuple, tuple[float, dict]] = {}
_ROLLING_CACHE_TTL_SEC = 300.0  # 5 分钟


@router.get("/key-levels/v1v2-rolling/{coin}")
async def get_v1v2_rolling(
    coin: str,
    window_days: int = Query(7, ge=1, le=30, description="单 anchor 回看窗口（天）"),
    step_hours: int = Query(24, ge=1, le=168, description="anchor 步长（小时）"),
    max_anchors: int = Query(14, ge=2, le=60, description="最多 anchor 数（默认 14 ≈ 2 周）"),
    future_window_sec: int = Query(14400, ge=1800, le=259200, description="判真相窗口（秒，1H 基线）"),
    timeframe_adaptive_window: bool = Query(True),
    no_cache: bool = Query(False, description="跳过缓存（调试用）"),
):
    """V3-M4 P0-3：14 天滑窗 V1/V2 对比指标时间序列（用于前端折线）。

    每个 anchor 的回看窗口 = (anchor_ts - window_days×86400, anchor_ts]，
    步长 = step_hours，最多 max_anchors 个。每个 anchor 内部跑 run_full_comparison。

    性能：单次 ~50-500ms（取决于 history 大小）；启用 5min TTL 内存缓存。
    """
    if not _engine:
        raise HTTPException(503, "Engine not ready")
    coin = coin.upper()
    history = _engine.get_kl_history(coin)
    if not history:
        return {
            "ready": True,
            "coin": coin,
            "params": {
                "window_days": window_days, "step_hours": step_hours,
                "max_anchors": max_anchors,
                "future_window_sec": future_window_sec,
                "timeframe_adaptive_window": timeframe_adaptive_window,
            },
            "anchors": [],
            "history_size": 0,
            "_cache_hit": False,
        }

    cache_key = (
        coin, window_days, step_hours, max_anchors,
        future_window_sec, timeframe_adaptive_window,
    )
    now = time.time()
    if not no_cache:
        cached = _ROLLING_CACHE.get(cache_key)
        if cached and (now - cached[0]) < _ROLLING_CACHE_TTL_SEC:
            result = dict(cached[1])
            result["_cache_hit"] = True
            result["_cache_age_sec"] = round(now - cached[0], 1)
            return result

    from processors.behavior_backtest_engine import compute_rolling_comparison
    result = compute_rolling_comparison(
        history, coin=coin,
        window_days=window_days, step_hours=step_hours, max_anchors=max_anchors,
        future_window_sec=future_window_sec,
        timeframe_adaptive_window=timeframe_adaptive_window,
    )
    result["history_size"] = len(history)
    result["ready"] = True
    result["_cache_hit"] = False
    _ROLLING_CACHE[cache_key] = (now, result)
    return result


def _empty_stats_dict(dimension: str) -> dict:
    """无数据时的占位结构，与 ComparisonStats.to_dict() 字段一致（M3.1 升级）。"""
    empty_cm = {
        "tp": 0, "fp": 0, "tn": 0, "fn": 0,
        "accuracy": 0.0, "precision": 0.0, "recall": 0.0, "f1": 0.0,
        "specificity": 0.0, "balanced_accuracy": 0.0, "mcc": 0.0,
    }
    return {
        "dimension": dimension,
        "sample_size": 0,
        "ambiguous_count": 0,
        "v1": empty_cm,
        "v2": empty_cm,
        "delta_accuracy": 0.0,
        "delta_precision": 0.0,
        "delta_recall": 0.0,
        "delta_f1": 0.0,
        "delta_balanced_accuracy": 0.0,
        "delta_mcc": 0.0,
        "chi_square_stat": 0.0,
        "chi_square_p_value": 1.0,
        "discordant_v1_wrong_v2_right": 0,
        "discordant_v1_right_v2_wrong": 0,
        "mcnemar_stat": 0.0,
        "mcnemar_p_value": 1.0,
        "family_size": 1,
        "mcnemar_p_bonferroni": 1.0,
        "mcnemar_p_fdr": 1.0,
        "accuracy_ci_v1": [0.0, 0.0],
        "accuracy_ci_v2": [0.0, 0.0],
        "is_v2_significantly_better": False,
        "decision_reasons": [],
        "calibration_v2": [],
        "calibration_monotonic": False,
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# M3 · R10：Diff / Lifecycle API（关键位演化追溯）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# 评分阈值常量（与 confluence_scoring._LIFECYCLE_SCORE_DELTA_THRESHOLD 保持一致）
_DIFF_SCORE_THRESHOLD = 5.0


def _summarize_level(lv) -> dict:
    """提取 level 的精简卡片字段供 diff 输出（避免返回完整 KeyLevelV2 拥肿）。"""
    return {
        "level_id": lv.level_id,
        "price": lv.price,
        "side": lv.side,
        "strength_tier": lv.strength_tier,
        "s_class": lv.s_class,
        "final_score": lv.final_score,
        "state": lv.state,
        "category": lv.category,
        "explain_chips": list(lv.explain_chips or []),
    }


@router.get("/key-levels/diff/{coin}")
async def get_kl_diff(
    coin: str,
    from_ts: int = Query(..., description="起始快照时间戳（秒）"),
    to_ts: int = Query(..., description="结束快照时间戳（秒）"),
):
    """对比两个时间点的关键位快照，输出 added/removed/strengthened/weakened/tier_changed/flipped。

    匹配规则：严格按 level_id 配对（M3 R9 引入的稳定 ID）。
    注意（V3-P1-5 修订）：
      - 缺 level_id 的旧快照（M3 上线前归档）将不参与 diff，相应 levels 视为不存在；
        如需对比这类历史快照，请先通过 history API 查看原始数据。
      - 该选择牺牲了对老快照的可视化能力，换取 diff 结果的精准性（避免 price 近似匹配
        在跨周期重置后误判 added/removed）。

    返回结构：
        {
            "coin": "BTC",
            "from_ts": ..., "to_ts": ...,
            "added": [<level summary>...],         # 新出现
            "removed": [<level summary>...],       # 消失
            "strengthened": [{"prev":, "curr":, "delta":}],  # final_score +≥5
            "weakened":   [{"prev":, "curr":, "delta":}],
            "tier_changed": [{"prev":, "curr":, "from":"B", "to":"A"}],
            "flipped":   [{"prev":, "curr":}],     # support↔resistance
        }
    """
    if not _engine:
        raise HTTPException(503, "Engine not ready")
    coin = coin.upper()
    if from_ts >= to_ts:
        raise HTTPException(400, "from_ts 必须早于 to_ts")

    history = _engine.get_kl_history(coin)
    snap_from = next((h for h in history if h.ts == from_ts), None)
    snap_to = next((h for h in history if h.ts == to_ts), None)
    if not snap_from:
        raise HTTPException(404, f"快照未找到：{coin}/{from_ts}")
    if not snap_to:
        raise HTTPException(404, f"快照未找到：{coin}/{to_ts}")

    from_by_id = {lv.level_id: lv for lv in snap_from.levels if lv.level_id}
    to_by_id = {lv.level_id: lv for lv in snap_to.levels if lv.level_id}

    added: list[dict] = []
    removed: list[dict] = []
    strengthened: list[dict] = []
    weakened: list[dict] = []
    tier_changed: list[dict] = []
    flipped: list[dict] = []

    tier_rank = {"S": 4, "A": 3, "B": 2, "C": 1, "": 0}

    for lid, lv_to in to_by_id.items():
        lv_from = from_by_id.get(lid)
        if lv_from is None:
            added.append(_summarize_level(lv_to))
            continue
        delta = lv_to.final_score - lv_from.final_score
        if delta >= _DIFF_SCORE_THRESHOLD:
            strengthened.append({
                "prev": _summarize_level(lv_from),
                "curr": _summarize_level(lv_to),
                "delta": round(delta, 1),
            })
        elif delta <= -_DIFF_SCORE_THRESHOLD:
            weakened.append({
                "prev": _summarize_level(lv_from),
                "curr": _summarize_level(lv_to),
                "delta": round(delta, 1),
            })
        if lv_from.strength_tier != lv_to.strength_tier:
            direction = "upgraded" if (
                tier_rank.get(lv_to.strength_tier, 0) > tier_rank.get(lv_from.strength_tier, 0)
            ) else "downgraded"
            tier_changed.append({
                "prev": _summarize_level(lv_from),
                "curr": _summarize_level(lv_to),
                "from": lv_from.strength_tier,
                "to": lv_to.strength_tier,
                "direction": direction,
            })
        if lv_from.side and lv_to.side and lv_from.side != lv_to.side:
            flipped.append({
                "prev": _summarize_level(lv_from),
                "curr": _summarize_level(lv_to),
            })

    for lid, lv_from in from_by_id.items():
        if lid not in to_by_id:
            removed.append(_summarize_level(lv_from))

    return {
        "coin": coin,
        "from_ts": from_ts,
        "to_ts": to_ts,
        "from_count": len(from_by_id),
        "to_count": len(to_by_id),
        "added": added,
        "removed": removed,
        "strengthened": strengthened,
        "weakened": weakened,
        "tier_changed": tier_changed,
        "flipped": flipped,
        "summary": {
            "added": len(added),
            "removed": len(removed),
            "strengthened": len(strengthened),
            "weakened": len(weakened),
            "tier_changed": len(tier_changed),
            "flipped": len(flipped),
        },
    }


@router.get("/key-levels/lifecycle/{coin}/{level_id}")
async def get_kl_lifecycle(coin: str, level_id: str):
    """返回指定 level_id 的完整生命周期事件流（跨多个 snapshot 合并）。

    设计：
      - 遍历 history snapshots（按 ts 升序）
      - 找出 level_id 命中的所有 levels，提取其 lifecycle_events
      - 按事件 ts 全局去重 + 升序排序（同 ts + 同 event_type 视为同一事件）
      - 输出：first_seen_ts / last_seen_ts / events / latest_state

    用途：
      - 前端"📅 该关键位演化时间线"
      - 复盘"为什么该 S 级支撑突然消失"
      - 外部 AI 审计关键位是否稳定
    """
    if not _engine:
        raise HTTPException(503, "Engine not ready")
    coin = coin.upper()

    history = _engine.get_kl_history(coin)
    if not history:
        raise HTTPException(404, f"无历史数据：{coin}")

    history_sorted = sorted(history, key=lambda h: h.ts)
    # V3-P1-6：去重 key 加入 layer 维度，避免 scoring 层和 tracker 层
    # 在同一秒生成相同 event_type（如同时 flipped）时丢失一条
    seen_keys: set[tuple[int, str, str]] = set()
    merged_events: list[dict] = []
    first_seen_ts: Optional[int] = None
    last_seen_ts: Optional[int] = None
    latest_level_summary: Optional[dict] = None
    snapshot_count = 0

    for snap in history_sorted:
        for lv in snap.levels:
            if lv.level_id != level_id:
                continue
            snapshot_count += 1
            if first_seen_ts is None:
                first_seen_ts = snap.ts
            last_seen_ts = snap.ts
            latest_level_summary = _summarize_level(lv)
            for evt in (lv.lifecycle_events or []):
                key = (evt.ts, evt.event_type, evt.layer)
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                merged_events.append(evt.model_dump())

    if first_seen_ts is None:
        raise HTTPException(404, f"未在任何快照中找到 level_id：{level_id}")

    merged_events.sort(key=lambda e: e["ts"])

    return {
        "coin": coin,
        "level_id": level_id,
        "first_seen_ts": first_seen_ts,
        "last_seen_ts": last_seen_ts,
        "snapshot_count": snapshot_count,
        "event_count": len(merged_events),
        "events": merged_events,
        "latest": latest_level_summary,
    }


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


@router.get("/trading-brain/{coin}")
async def get_trading_brain(
    coin: str,
    max_zones: int = Query(24, ge=1, le=64),
):
    """交易大脑大屏：统一 PriceZone 聚合（只读；无交易指令）。

    无关键位/清算/挂单墙时仍返回 200，依赖 data_quality.notes 说明缺口；
    仅行情 ticker 不可用时 503。
    """
    if not _engine:
        raise HTTPException(503, "Engine not ready")
    coin_u = coin.upper()
    if coin_u not in get_settings().supported_coins:
        raise HTTPException(400, f"Unsupported coin: {coin_u}")
    state = _engine._states.get(coin_u)
    if not state or not state.ticker or not state.ticker.last or state.ticker.last <= 0:
        raise HTTPException(503, f"No ticker for {coin_u}")

    from processors.trading_brain_builder import build_trading_brain_snapshot

    last = float(state.ticker.last)
    atr = float(state.atr or 0.0)
    kl = state.key_level_snapshot_v2
    op = state.orderbook_pressure_snapshot
    from models.liquidation import pick_primary_liq_map
    liq = pick_primary_liq_map(getattr(state, "liq_maps", None))

    from processors.market_read import build_market_read_from_state

    market_context = build_market_read_from_state(state)

    # P1-A 修复：跨帧持久化 setup state，让状态机能真正抵达 confirmed/cooldown/missed
    # （而非每次 build 都被 opportunity_engine 重置为 forming/waiting）。
    # 字典自然按"当帧仍存在的 setup_id"做 GC——本帧聚合不出的 setup 自动清退。
    prev_states = dict(getattr(state, "brain_setup_states", {}) or {})
    snap = build_trading_brain_snapshot(
        coin=coin_u,
        last_price=last,
        atr=atr,
        kl=kl,
        op=op,
        liq=liq,
        cvd_contract_trend=market_context["cvd_contract_trend"],
        cvd_spot_trend=market_context["cvd_spot_trend"],
        oi_delta_1h_pct=market_context["oi_delta_1h_pct"],
        funding_interpretation=market_context["funding_interpretation"],
        funding_rate_8h_pct=market_context["funding_rate_8h_pct"],
        market_read=market_context["market_read"],
        context_sources=market_context["source_meta"],
        max_zones=max_zones,
        prev_setup_states=prev_states,
    )
    state.brain_setup_states = {s.setup_id: s.state for s in snap.opportunities}
    return snap.model_dump()


# PR-3 · /api/execution-plan / /api/signal-bus/stats 已下线
# （ExecutionPlan + signal_bus 都属于已删除的数学引擎 L3-L4 链路）。


@router.get("/liquidity-wall/metrics")
async def get_liquidity_wall_metrics(coin: Optional[str] = Query(None)):
    """流动性墙引擎运行时监控指标（W1-T1）。

    暴露：
      - Coinglass 限速器排队 p50/p95（ms）+ 各端点调用次数 + 各端点错误次数
      - 各端点最近成功距今秒数（source_age）
      - Coinbase 原生 API 延迟 p50/p95 + 30min 错误率
      - 各币 orderbook_pressure 30min stale_ratio + 最近 data_quality 标签

    用途：
      - 上线观察期判定限速器是否在排队（queue_wait_p95 > 8000ms 即不健康）
      - 判定数据是否陈旧（stale_ratio > 0.2 触发降权）
      - 判定 Coinbase 是否可用（error_rate > 0.05 阻塞 trust_score Coinbase 加分）

    参数：
      - coin: 可选。传入则同时返回该币的 stale_ratio + latest data_quality；
              不传则返回 by_coin 字典。
    """
    try:
        from processors.liquidity_wall_metrics import get_metrics
        return get_metrics().snapshot(coin)
    except Exception as e:
        logger.warning("liquidity_wall_metrics snapshot failed: %s", e)
        raise HTTPException(500, f"metrics unavailable: {e}")


# PR-3 · /api/ai-trader-report / /api/final-decision / /api/divergence-stats /
# /api/signal-pnl / /api/ai-quality / /api/plan-backtest 已整体下线
# （依赖 AITraderReport / FinalDecision / divergence_backfill / signal_pnl_tracker /
#  ai_quality_ledger / plan_backtest，全部已被 Strategic AI 取代）。


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


# PR-5 · /api/backtest/stats 已下线（旧 Trader 回测统计无数据源）

@router.get("/news-brief/current")
async def news_brief_current():
    """D09 · 当前滚动新闻简报（供前端人工对证 AI 记忆锚）

    用途：
      - 人工审计 AI prompt 里实际注入了什么新闻
      - 识别熔断态（model_used=skipped_no_events）与故障态（fallback）
      - 对证 bullets / tldr / tracked_themes / diff

    返回契约（前端可据此渲染徽章）：
      - ready=false → 简报尚未生成（首启动 ~60s 内）
      - status=ok / circuit_break / ai_failed / unexpected_empty
      - 其余字段为 NewsBrief model_dump()
    """
    try:
        from processors.news_brief import get_current_brief
        brief = get_current_brief()
    except Exception as e:
        logger.warning("news-brief current endpoint failed: %s", e)
        raise HTTPException(500, f"news_brief unavailable: {e}")

    if brief is None:
        return {
            "ready": False,
            "status": "warming_up",
            "reason": "brief 未生成（启动后 ~60s 或首轮 fetch 未完成）",
        }

    model = (brief.model_used or "").strip()
    if model == "bootstrap":
        ui_status = "bootstrap"
        ui_reason = "首轮简报生成中·通常需 5-15 分钟（等 news_structurer enrich 完成）"
    elif model == "skipped_no_events":
        ui_status = "circuit_break"
        ui_reason = "上游无新闻事件·已熔断（保护 AI 不编造）"
    elif model == "fallback":
        ui_status = "ai_failed"
        ui_reason = "AI 调用失败·沿用上一版本"
    elif sum(len(s.bullets) for s in brief.sections) > 0 or brief.tldr_cn:
        ui_status = "ok"
        ui_reason = ""
    else:
        ui_status = "unexpected_empty"
        ui_reason = "简报为空但非熔断/fallback，需人工排查"

    return {
        "ready": True,
        "status": ui_status,
        "reason": ui_reason,
        "brief": brief.model_dump(),
    }


@router.get("/news-brief/history")
async def news_brief_history(
    limit: int = Query(30, ge=1, le=200),
    since_ts: Optional[int] = None,
):
    """D09 · 滚动新闻简报历史（供前端时间线回溯）

    - limit: 返回条数上限（1~200）
    - since_ts: 若提供，只返回 updated_at > since_ts 的版本

    返回按 version 递增排序（最旧→最新），前端可自行反转或做增量加载。
    单条 item 包含：version / updated_at / trigger / based_on_events_count /
    tldr_cn / sections / tracked_themes / model_used / char_count /
    generation_cost_ms / diff_from_prev_version。
    """
    try:
        from processors.news_brief import load_history
        briefs = load_history(limit=None)  # 先全量读，再按 since_ts + limit 裁剪
    except Exception as e:
        logger.warning("news-brief history endpoint failed: %s", e)
        raise HTTPException(500, f"news_brief history unavailable: {e}")

    if since_ts is not None:
        briefs = [b for b in briefs if int(b.updated_at or 0) > int(since_ts)]
    if limit > 0:
        briefs = briefs[-limit:]

    return {
        "ready": True,
        "count": len(briefs),
        "items": [b.model_dump() for b in briefs],
    }


def _runtime_diagnostics() -> dict:
    """内存、后台任务与现货归档用量；容器内存治理的观测入口。"""
    from utils.process_stats import process_memory_stats

    diagnostics: dict = {"memory": process_memory_stats()}
    try:
        diagnostics["background_tasks"] = len(asyncio.all_tasks())
    except RuntimeError:
        diagnostics["background_tasks"] = None
    if _engine is not None:
        try:
            diagnostics["spot_accumulation_storage"] = (
                _engine.spot_accumulation_service.store.storage_stats()
            )
        except Exception as exc:  # noqa: BLE001
            diagnostics["spot_accumulation_storage_error"] = str(exc)
    return diagnostics


@router.get("/ready")
async def readiness_check(response: Response):
    """就绪探针：核心行情完成暖机（或暖机超时降级）后才返回 200。

    容器健康检查与前端启动依赖此端点，避免暖机期的高负载阶段被判定为可用。
    """
    if not _engine:
        response.status_code = 503
        return {"ready": False, "phase": "initializing"}
    status = _engine.get_startup_status()
    if not status["ready"]:
        response.status_code = 503
    return status


@router.get("/health")
async def health_check():
    """数据源健康状态"""
    build = {
        "commit": os.getenv("APP_GIT_SHA", "unknown"),
        "built_at": os.getenv("APP_BUILD_TIME", "unknown"),
    }
    if not _engine:
        return {"status": "starting", "build": build, "runtime": _runtime_diagnostics()}
    return {
        "status": "running",
        "build": build,
        "startup": _engine.get_startup_status(),
        "runtime": _runtime_diagnostics(),
        "sources": _engine.get_source_health(),
        "smc_monitor": _engine.get_smc_monitor_status(),
        "strategic_available": _engine.strategic_available,
        "ai_provider": get_settings().ai.active,
    }


@router.get("/te/reports")
async def list_te_reports(max_days: int = Query(30, ge=1, le=180)):
    """P0-B · 列出已有的 TrendExhaustion 日报（按日期降序）。"""
    try:
        from monitoring.te_eval import list_reports
        from monitoring.te_shadow import list_available_dates, get_te_shadow_logger
        from monitoring import te_ai_log as ai_log_mod
        reports = list_reports(max_days=max_days)
        shadow_dates = list_available_dates(max_days=max_days)
        logger_stats = get_te_shadow_logger().stats()
        ai_stats = ai_log_mod.stats()
        return {
            "reports": reports,
            "shadow_dates": shadow_dates,
            "logger_stats": logger_stats,
            "ai_log_stats": ai_stats,
        }
    except Exception as e:
        logger.warning("list te reports failed: %s", e)
        raise HTTPException(500, f"te report listing failed: {e}")


@router.post("/te/ai_interpret/{coin}")
async def te_ai_interpret_trigger(coin: str, force: bool = Query(False)):
    """P0-C · 触发趋势衰竭 AI 解读（fire-and-forget，对齐主 AI 架构）

    HTTP 秒回 → 后台任务跑 DeepSeek V4-Flash（非思考模式）→ 结果通过 WebSocket 事件
    `te_ai_result` 推送到订阅该币种的所有客户端；失败 push `te_ai_error`。

    响应 schema：
      - status="cached"     ：缓存命中，结果已同步 push 到 WS；附带 interpretation
      - status="processing" ：已启动后台任务，等 WS 推
      - status="inflight"   ：已有同指纹任务在跑，等 WS 推
      - status="error"      ：AI 未配置等致命错误
    """
    if not _engine:
        raise HTTPException(503, "Engine not ready")
    coin_upper = coin.upper()
    state = _engine._states.get(coin_upper)
    if not state or not state.trend_exhaustion:
        raise HTTPException(503, f"No trend_exhaustion signal for {coin_upper}")

    from ai.te_interpreter import get_te_interpreter
    interpreter = get_te_interpreter()
    if not interpreter.available:
        return {
            "status": "error",
            "coin": coin_upper,
            "message": "AI 未配置 API Key（请检查 DEEPSEEK_API_KEY）",
        }

    signal_dict = state.trend_exhaustion.model_dump()
    price = float(state.ticker.last) if state.ticker else 0.0
    atr = float(state.atr or 0.0)
    # 关键位快照：AI 做 level_projection / trade_bias invalidation 的数据源
    key_levels_dict: Optional[dict] = None
    if state.key_level_snapshot_v2:
        try:
            key_levels_dict = state.key_level_snapshot_v2.model_dump()
        except Exception:
            key_levels_dict = None
    # 扩展上下文：多周期 MS / funding / OI / LS / Liq Map（与 ws.py replay 共享同一收集器）
    from api._ai_helpers import collect_extras
    extras_dict = collect_extras(state)
    fp = interpreter.compute_fingerprint(
        coin_upper, signal_dict, key_levels_dict, extras_dict,
    )

    from api.ws import push_to_coin
    from monitoring.te_ai_log import log_interpretation

    # ── 1. 命中缓存（非 force）→ 同步 push 一次让前端更新，然后秒回 ──
    if not force:
        cached = interpreter.peek_cache(fp)
        if cached is not None:
            await push_to_coin(coin_upper, "te_ai_result", cached.model_dump())
            return {
                "status": "cached",
                "coin": coin_upper,
                "signal_fingerprint": fp,
                "interpretation": cached.model_dump(),
            }

        # ── 2. 已有同指纹任务在跑 → 秒回，等它跑完会自动 WS push ──
        if interpreter.is_inflight(fp):
            return {
                "status": "inflight",
                "coin": coin_upper,
                "signal_fingerprint": fp,
                "message": "AI 已在思考中，结果将通过 WebSocket 推送",
            }

    # ── 3. 启动后台任务 + WS 推送 ───────────────────
    import asyncio

    async def _run_and_push():
        try:
            result = await interpreter.interpret(
                coin=coin_upper, signal_dict=signal_dict,
                price=price, atr=atr,
                key_levels_dict=key_levels_dict,
                extras_dict=extras_dict,
                force=force,
            )
            # WS 推送结果（成功或 AI 自身带 error 都走 te_ai_result）
            try:
                await push_to_coin(
                    coin_upper, "te_ai_result", result.model_dump(),
                )
                logger.info(
                    "[TE-AI] result pushed via WS | coin=%s fp=%s state=%s latency=%dms",
                    coin_upper, fp,
                    "error" if result.error else "done",
                    result.latency_ms,
                )
            except Exception:
                logger.warning("[TE-AI] ws push failed", exc_info=True)
            # shadow log（仅实际调用过 AI 且未出错）
            try:
                if not result.cache_hit and result.error is None:
                    log_interpretation(result, signal_dict, price)
            except Exception:
                logger.debug("[TE-AI] shadow log failed", exc_info=True)
        except Exception as e:
            logger.exception("[TE-AI] bg task crashed | coin=%s fp=%s", coin_upper, fp)
            try:
                await push_to_coin(coin_upper, "te_ai_error", {
                    "coin": coin_upper,
                    "signal_fingerprint": fp,
                    "message": f"{type(e).__name__}: {str(e)[:200]}",
                })
            except Exception:
                pass

    asyncio.create_task(_run_and_push())
    return {
        "status": "processing",
        "coin": coin_upper,
        "signal_fingerprint": fp,
        "message": "AI 已开始思考，结果将通过 WebSocket 推送",
    }


@router.get("/te/ai_interpret/{coin}")
async def te_ai_interpret_peek(coin: str):
    """查询当前缓存的 AI 解读结果（不触发新计算）。

    用途：
      - 页面刷新时拉取最后一次解读（WS replay 也会推，这是兜底）
      - 调试：查看最近一次 AI 的完整输出
    返回：缓存命中则返回 TEAIInterpretation.model_dump()，否则 404。
    """
    if not _engine:
        raise HTTPException(503, "Engine not ready")
    coin_upper = coin.upper()
    state = _engine._states.get(coin_upper)
    if not state or not state.trend_exhaustion:
        raise HTTPException(404, f"No trend_exhaustion signal for {coin_upper}")

    from ai.te_interpreter import get_te_interpreter
    from api._ai_helpers import collect_extras
    interpreter = get_te_interpreter()
    signal_dict = state.trend_exhaustion.model_dump()
    key_levels_dict: Optional[dict] = None
    if state.key_level_snapshot_v2:
        try:
            key_levels_dict = state.key_level_snapshot_v2.model_dump()
        except Exception:
            key_levels_dict = None
    extras_dict = collect_extras(state)
    fp = interpreter.compute_fingerprint(
        coin_upper, signal_dict, key_levels_dict, extras_dict,
    )
    cached = interpreter.peek_cache(fp)
    if cached is None:
        raise HTTPException(404, "No cached interpretation for current signal")
    return cached.model_dump()


@router.get("/te/ai_interpret/{coin}/history")
async def te_ai_interpret_history(
    coin: str,
    limit: int = Query(20, ge=1, le=100),
):
    """读取某币种最近 N 条 AI 解读历史（从新到旧）。

    数据源：`logs/te_ai_interpret/YYYY-MM-DD/{COIN}.jsonl`
    （不含 reasoning，避免文件膨胀；需要时走 /detail 端点）

    Returns:
        {items: [...], total: n, coin: "BTC"}
    """
    from monitoring.te_ai_log import read_history
    coin_upper = coin.upper()
    items = read_history(coin_upper, limit=limit, max_days=30)
    return {
        "coin": coin_upper,
        "items": items,
        "total": len(items),
        "limit": limit,
    }


@router.get("/te/ai_interpret/{coin}/detail/{ts}")
async def te_ai_interpret_detail(coin: str, ts: int):
    """读取单条 AI 解读完整详情（含 reasoning 思考链）。

    Args:
        coin: 币种大写
        ts: 记录 ts（unix 秒，与 history 列表中的 ts 对应）

    Returns:
        完整 jsonl dict + reasoning 字段（若 .thinking.jsonl 有同 fingerprint 记录）
    """
    from monitoring.te_ai_log import read_detail
    coin_upper = coin.upper()
    record = read_detail(coin_upper, ts, with_reasoning=True)
    if record is None:
        raise HTTPException(
            404,
            f"No AI interpretation record found | coin={coin_upper} ts={ts}",
        )
    return record


@router.get("/te/reports/{date}")
async def get_te_report(date: str, regenerate: bool = Query(False)):
    """P0-B · 读取指定日期的 TrendExhaustion 日报（Markdown）。

    若 regenerate=true 或文件不存在，则按需触发一次事后打标。
    """
    try:
        from monitoring.te_eval import read_report, evaluate_day
        if regenerate:
            stats, path = evaluate_day(date)
            md = read_report(date)
            return {
                "date": date,
                "markdown": md,
                "exists": md is not None,
                "stats": {
                    "total_records": stats.total_records,
                    "judged": stats.overall.judged,
                    "correct": stats.overall.correct,
                    "wrong": stats.overall.wrong,
                    "neutral": stats.overall.neutral,
                    "pending": stats.overall.pending,
                    "accuracy": stats.overall.accuracy,
                    "soft_accuracy": stats.overall.soft_accuracy,
                },
                "regenerated": True,
            }
        md = read_report(date)
        if md is None:
            stats, path = evaluate_day(date)
            md = read_report(date)
            if md is None:
                raise HTTPException(404, f"report unavailable for {date}")
            return {
                "date": date,
                "markdown": md,
                "exists": True,
                "regenerated": True,
            }
        return {
            "date": date,
            "markdown": md,
            "exists": True,
            "regenerated": False,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.warning("get te report failed date=%s: %s", date, e)
        raise HTTPException(500, f"te report read failed: {e}")


@router.get("/logs")
async def get_logs(
    level: Optional[str] = Query(None, description="Filter by level: INFO, WARNING, ERROR"),
    limit: int = Query(200, ge=1, le=500),
    keyword: Optional[str] = Query(None, description="Filter by keyword"),
):
    """获取后端运行日志（内存缓存，最近500条）"""
    from monitoring.log_buffer import log_buffer

    logs = list(log_buffer)

    if level:
        level_upper = level.upper()
        logs = [l for l in logs if l["level"] == level_upper]

    if keyword:
        kw_lower = keyword.lower()
        logs = [l for l in logs if kw_lower in l["msg"].lower() or kw_lower in l["name"].lower()]

    return {"total": len(logs), "logs": logs[-limit:]}
