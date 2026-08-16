"""HTTP API。

设计取向：**每个返回决策结果的接口都必须同时返回它的可信度与依据**。

只返回"机会分 87"的接口会诱导前端做出一个漂亮但危险的界面——
用户看到一个高分就行动，而不知道这个分数是基于 30% 完整度的数据算出来的。
因此评分类响应一律带上 data_quality、confidence、缺失字段组和风险标注。

所有查询走只读连接，与写入协程互不阻塞。
分页一律有硬上限：一个忘记加 limit 的前端调用不应该能把 512MB 的容器打爆。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .domain.models import TokenState
from .obs.logging_setup import now_ms
from .service import RadarService

logger = logging.getLogger("radar.api")

# 单页硬上限。前端忘记传 limit 时的默认值要小，
# 而允许的最大值要能覆盖导出场景，两者不能是同一个数
MAX_LIMIT = 500

router = APIRouter(prefix="/api/radar")
_service: RadarService | None = None


def bind_service(service: RadarService) -> None:
    global _service
    _service = service


def get_service() -> RadarService:
    if _service is None:
        raise HTTPException(status_code=503, detail="服务尚未就绪")
    return _service


# ═════════════════════════════════════════════════════════════════════════
# 健康与诊断
# ═════════════════════════════════════════════════════════════════════════

@router.get("/health")
async def health() -> dict[str, Any]:
    return get_service().health()


@router.get("/ready")
async def ready() -> JSONResponse:
    """容器健康检查端点。

    降级时返回 503 而不是 200：如果所有接口都在超时却依然报告健康，
    编排层就永远不会重启它，而这恰恰是最需要重启的时刻。
    """
    payload = get_service().health()
    code = 200 if payload["status"] == "ok" else 503
    return JSONResponse(status_code=code, content=payload)


@router.get("/diagnostics")
async def diagnostics() -> dict[str, Any]:
    return get_service().diagnostics()


@router.get("/config")
async def config_fingerprint() -> dict[str, Any]:
    """只暴露指纹与阈值，绝不返回原始配置文件。

    config.yaml 本身不含凭据（凭据只走环境变量），
    但把整个配置开放给前端仍是不必要的攻击面扩大。
    """
    service = get_service()
    return {
        "fingerprint": service.settings.fingerprint(),
        "chains": [
            {"id": c.id, "name": c.name, "enabled": c.enabled}
            for c in service.settings.chains
        ],
        "thresholds": {
            "state_machine": service.settings.state_machine,
            "risk": service.settings.risk,
            "quality": service.settings.raw.get("quality", {}),
            "alerts": service.settings.alerts,
        },
        "tiers": {
            name: {"max_rpm": t.max_rpm, "interval_sec": t.interval_sec}
            for name, t in service.settings.tiers.items()
        },
    }


# ═════════════════════════════════════════════════════════════════════════
# 扫描器：当前内存中的代币
# ═════════════════════════════════════════════════════════════════════════

@router.get("/tokens")
async def list_tokens(
    state: str | None = Query(None, description="按状态过滤"),
    chain_id: str | None = Query(None),
    min_opportunity: float = Query(0.0, ge=0, le=100),
    sort: str = Query("opportunity", pattern="^(opportunity|market_cap|age|holders)$"),
    limit: int = Query(100, ge=1, le=MAX_LIMIT),
) -> dict[str, Any]:
    """内存中的实时代币视图。

    直接读内存而不是查库：这个接口是控制台的主界面，
    每秒都可能被刷新，而它要的正是"此刻系统怎么看这些币"——
    查库反而拿到的是上一次快照，且要多付一次磁盘 IO。
    """
    service = get_service()
    now = now_ms()

    target_state: TokenState | None = None
    if state:
        try:
            target_state = TokenState(state.upper())
        except ValueError:
            raise HTTPException(status_code=400, detail=f"未知状态: {state}") from None

    items: list[dict[str, Any]] = []
    for view in service.registry.all_views():
        if target_state is not None and view.state != target_state:
            continue
        if chain_id and view.chain_id != chain_id:
            continue
        opportunity = float(view.last_scores.get("opportunity", 0.0))
        if opportunity < min_opportunity:
            continue
        items.append(_token_summary(view, now))

    key_map = {
        "opportunity": lambda x: x["scores"]["opportunity"],
        "market_cap": lambda x: x["market_cap"] or 0.0,
        "age": lambda x: -(x["age_sec"] or 0),
        "holders": lambda x: x["holders"] or 0,
    }
    items.sort(key=key_map[sort], reverse=True)
    return {"total": len(items), "items": items[:limit],
            "states": service.registry.state_counts()}


@router.get("/tokens/{chain_id}/{contract_address}")
async def token_detail(chain_id: str, contract_address: str) -> dict[str, Any]:
    """单币完整档案：当前视图 + 历史快照 + 警报 + 里程碑 + Outcome。"""
    service = get_service()
    now = now_ms()

    view = service.registry.get(chain_id, contract_address)
    row = await service.db.fetch_one(
        "SELECT * FROM token_master WHERE chain_id=? AND contract_address=?",
        (chain_id, contract_address),
    )
    if view is None and row is None:
        raise HTTPException(status_code=404, detail="未收录该代币")

    token_id = view.token_id if view is not None else int(row["token_id"])
    snapshots = await service.db.fetch_all(
        "SELECT observed_at, price, market_cap, liquidity, holders, top10_percent, "
        "opportunity, confidence, data_quality, rug_risk, distribution, state, endpoint "
        "FROM snapshots WHERE token_id=? ORDER BY observed_at DESC LIMIT 300",
        (token_id,),
    )
    alerts = await service.db.fetch_all(
        "SELECT alert_id, alert_kind, is_near_miss, created_at, opportunity, "
        "confidence, data_quality, rug_risk, trigger_json, factors_json, review_state "
        "FROM alerts WHERE token_id=? ORDER BY created_at DESC LIMIT 50",
        (token_id,),
    )
    milestones = await service.db.fetch_all(
        "SELECT milestone_usd, occurred_at, market_cap, token_age_sec "
        "FROM milestones WHERE token_id=? ORDER BY occurred_at ASC",
        (token_id,),
    )
    outcomes = await service.db.fetch_all(
        "SELECT * FROM outcomes WHERE token_id=? ORDER BY signal_at DESC LIMIT 20",
        (token_id,),
    )
    rejections = await service.db.fetch_all(
        "SELECT occurred_at, gate, rule, actual_value, threshold_value, actual_text "
        "FROM rejections WHERE token_id=? ORDER BY occurred_at DESC LIMIT 30",
        (token_id,),
    )

    return {
        "identity": _identity(view, row, token_id),
        "live": None if view is None else _token_summary(view, now),
        "quality": None if view is None else {
            "degraded": view.quality_degraded,
            "group_updated_at": view.group_updated_at,
            "field_source": view.field_source,
            "observation_count": view.observation_count,
            "history_depth": view.history_depth,
        },
        "snapshots": [dict(s) for s in reversed(snapshots)],
        "alerts": [_alert_row(a) for a in alerts],
        "milestones": [dict(m) for m in milestones],
        "outcomes": [_outcome_row(o) for o in outcomes],
        "rejections": [dict(r) for r in rejections],
    }


# ═════════════════════════════════════════════════════════════════════════
# 警报
# ═════════════════════════════════════════════════════════════════════════

@router.get("/alerts")
async def list_alerts(
    kind: str | None = Query(None),
    include_near_miss: bool = Query(False),
    since_hours: float = Query(72.0, gt=0, le=24 * 90),
    limit: int = Query(100, ge=1, le=MAX_LIMIT),
) -> dict[str, Any]:
    service = get_service()
    since = now_ms() - int(since_hours * 3_600_000)

    clauses = ["a.created_at >= ?"]
    params: list[Any] = [since]
    if not include_near_miss:
        clauses.append("a.is_near_miss = 0")
    if kind:
        clauses.append("a.alert_kind = ?")
        params.append(kind.upper())
    params.append(limit)

    rows = await service.db.fetch_all(
        "SELECT a.*, t.chain_id, t.contract_address, t.symbol, "
        "o.peak_multiple, o.current_multiple, o.outcome_label, o.is_final "
        "FROM alerts a JOIN token_master t ON t.token_id = a.token_id "
        "LEFT JOIN outcomes o ON o.alert_id = a.alert_id "
        f"WHERE {' AND '.join(clauses)} ORDER BY a.created_at DESC LIMIT ?",
        tuple(params),
    )
    return {"total": len(rows), "items": [_alert_row(r) for r in rows]}


@router.get("/alerts/{alert_id}")
async def alert_detail(alert_id: int) -> dict[str, Any]:
    service = get_service()
    row = await service.db.fetch_one(
        "SELECT a.*, t.chain_id, t.contract_address, t.symbol "
        "FROM alerts a JOIN token_master t ON t.token_id = a.token_id "
        "WHERE a.alert_id = ?",
        (alert_id,),
    )
    if row is None:
        raise HTTPException(status_code=404, detail="警报不存在")

    outcome = await service.db.fetch_one(
        "SELECT * FROM outcomes WHERE alert_id=?", (alert_id,)
    )
    positions = await service.db.fetch_all(
        "SELECT * FROM paper_positions WHERE alert_id=? ORDER BY size_usd", (alert_id,)
    )
    snapshot = None
    if row["snapshot_id"]:
        snapshot = await service.db.fetch_one(
            "SELECT * FROM snapshots WHERE snapshot_id=?", (row["snapshot_id"],)
        )
    return {
        "alert": _alert_row(row),
        # 决策现场：这一帧是判断发生时系统看到的全部输入，
        # 没有它就无法回答"当时到底为什么这么判"
        "decision_snapshot": None if snapshot is None else dict(snapshot),
        "outcome": None if outcome is None else _outcome_row(outcome),
        "paper_positions": [dict(p) for p in positions],
    }


@router.post("/alerts/{alert_id}/review")
async def review_alert(alert_id: int, state: str = Query(...)) -> dict[str, Any]:
    """标记人工复核状态。

    review_state 是 alerts 表里唯一可变的列——机器判断不可变，
    人的工作流状态可变，两者严格分离。混在一起会让"当时机器怎么判的"
    被后来的人工操作覆盖掉。
    """
    allowed = {"NEW", "REVIEWED", "TRACKING", "CLOSED"}
    value = state.upper()
    if value not in allowed:
        raise HTTPException(status_code=400,
                            detail=f"review_state 必须是 {sorted(allowed)} 之一")

    service = get_service()
    exists = await service.db.fetch_one(
        "SELECT alert_id FROM alerts WHERE alert_id=?", (alert_id,)
    )
    if exists is None:
        raise HTTPException(status_code=404, detail="警报不存在")

    service.db.submit(
        "UPDATE alerts SET review_state=?, reviewed_at=? WHERE alert_id=?",
        (value, now_ms(), alert_id), label="alert_review",
    )
    await service.db.drain()
    return {"alert_id": alert_id, "review_state": value}


# ═════════════════════════════════════════════════════════════════════════
# 研究：拒绝样本、Near-Miss、KPI
# ═════════════════════════════════════════════════════════════════════════

@router.get("/research/rejections")
async def list_rejections(
    rule: str | None = Query(None),
    since_hours: float = Query(168.0, gt=0, le=24 * 180),
    limit: int = Query(200, ge=1, le=MAX_LIMIT),
) -> dict[str, Any]:
    """被风险门拦下的记录。

    这是整个系统最重要的研究入口：它回答"我们的阈值错杀了多少赢家"。
    因此返回的不只是原因字符串，还有当时的实际值、阈值、年龄和市值——
    只有这些齐全才能做反事实分析。
    """
    service = get_service()
    since = now_ms() - int(since_hours * 3_600_000)
    clauses = ["r.occurred_at >= ?"]
    params: list[Any] = [since]
    if rule:
        clauses.append("r.rule = ?")
        params.append(rule)
    params.append(limit)

    rows = await service.db.fetch_all(
        "SELECT r.*, t.chain_id, t.contract_address, t.symbol "
        "FROM rejections r JOIN token_master t ON t.token_id = r.token_id "
        f"WHERE {' AND '.join(clauses)} ORDER BY r.occurred_at DESC LIMIT ?",
        tuple(params),
    )
    summary = await service.db.fetch_all(
        "SELECT rule, gate, COUNT(*) AS n FROM rejections WHERE occurred_at >= ? "
        "GROUP BY rule, gate ORDER BY n DESC",
        (since,),
    )
    return {
        "total": len(rows),
        "items": [dict(r) for r in rows],
        "by_rule": [dict(s) for s in summary],
    }


@router.get("/research/near-miss")
async def list_near_miss(
    since_hours: float = Query(168.0, gt=0, le=24 * 180),
    limit: int = Query(200, ge=1, le=MAX_LIMIT),
) -> dict[str, Any]:
    """差一点触发的记录，用于阈值反事实研究。"""
    service = get_service()
    since = now_ms() - int(since_hours * 3_600_000)
    rows = await service.db.fetch_all(
        "SELECT a.*, t.chain_id, t.contract_address, t.symbol, "
        "o.peak_multiple, o.outcome_label "
        "FROM alerts a JOIN token_master t ON t.token_id = a.token_id "
        "LEFT JOIN outcomes o ON o.alert_id = a.alert_id "
        "WHERE a.is_near_miss = 1 AND a.created_at >= ? "
        "ORDER BY a.created_at DESC LIMIT ?",
        (since, limit),
    )
    return {"total": len(rows), "items": [_alert_row(r) for r in rows]}


@router.get("/research/kpi")
async def list_kpi(
    days: int = Query(30, ge=1, le=365),
    horizon: str | None = Query(None),
) -> dict[str, Any]:
    """按成熟队列统计的 KPI。

    matured_count 必须原样返回给前端：一个基于 3 个样本的
    "成功率 67%"和一个基于 300 个样本的"成功率 67%"是完全不同的信息，
    界面上不显示样本量等于在鼓励过度解读。
    """
    service = get_service()
    clauses = ["stat_date >= date('now', ?)"]
    params: list[Any] = [f"-{days} days"]
    if horizon:
        clauses.append("horizon = ?")
        params.append(horizon)

    rows = await service.db.fetch_all(
        f"SELECT * FROM kpi_daily WHERE {' AND '.join(clauses)} "
        "ORDER BY stat_date DESC, alert_kind, horizon",
        tuple(params),
    )
    items = []
    for row in rows:
        item = dict(row)
        item["payload"] = _loads(item.pop("payload_json", None))
        items.append(item)
    return {"total": len(items), "items": items}


@router.post("/research/kpi/rebuild")
async def rebuild_kpi() -> dict[str, Any]:
    service = get_service()
    results = await service.kpi.build()
    await service.db.drain()
    return {"groups": len(results), "items": results}


# ═════════════════════════════════════════════════════════════════════════
# 运维：事件与调度
# ═════════════════════════════════════════════════════════════════════════

@router.get("/events")
async def list_events(
    category: str | None = Query(None),
    severity: str | None = Query(None),
    min_importance: str | None = Query(None),
    since_hours: float = Query(24.0, gt=0, le=24 * 30),
    limit: int = Query(200, ge=1, le=MAX_LIMIT),
) -> dict[str, Any]:
    service = get_service()
    since = now_ms() - int(since_hours * 3_600_000)
    clauses = ["occurred_at >= ?"]
    params: list[Any] = [since]
    for column, value in (("category", category), ("severity", severity),
                          ("importance", min_importance)):
        if value:
            clauses.append(f"{column} = ?")
            params.append(value)
    params.append(limit)

    rows = await service.db.fetch_all(
        f"SELECT * FROM radar_events WHERE {' AND '.join(clauses)} "
        "ORDER BY occurred_at DESC LIMIT ?",
        tuple(params),
    )
    items = []
    for row in rows:
        item = dict(row)
        item["payload"] = _loads(item.pop("payload_json", None))
        items.append(item)
    return {"total": len(items), "items": items}


@router.get("/scheduler")
async def scheduler_snapshot() -> dict[str, Any]:
    return get_service().scheduler.snapshot()


@router.get("/market-regime")
async def market_regime(
    since_hours: float = Query(48.0, gt=0, le=24 * 30),
) -> dict[str, Any]:
    service = get_service()
    since = now_ms() - int(since_hours * 3_600_000)
    rows = await service.db.fetch_all(
        "SELECT * FROM market_regime WHERE recorded_at >= ? ORDER BY recorded_at ASC",
        (since,),
    )
    return {"total": len(rows), "items": [dict(r) for r in rows]}


# ═════════════════════════════════════════════════════════════════════════
# 导出
# ═════════════════════════════════════════════════════════════════════════

@router.get("/export/{dataset}")
async def export_dataset(
    dataset: str,
    since_hours: float = Query(168.0, gt=0, le=24 * 365),
    limit: int = Query(5000, ge=1, le=50_000),
) -> dict[str, Any]:
    """研究数据导出。

    白名单而非拼接表名：把 dataset 直接拼进 SQL 是最经典的注入面，
    而这个接口的全部价值只是让研究脚本少写几行，不值得冒这个风险。
    """
    queries = {
        "alerts": (
            "SELECT a.*, t.chain_id, t.contract_address, t.symbol "
            "FROM alerts a JOIN token_master t ON t.token_id=a.token_id "
            "WHERE a.created_at >= ? ORDER BY a.created_at DESC LIMIT ?"
        ),
        "outcomes": (
            "SELECT o.*, a.alert_kind, a.strategy_version, t.chain_id, "
            "t.contract_address, t.symbol "
            "FROM outcomes o JOIN alerts a ON a.alert_id=o.alert_id "
            "JOIN token_master t ON t.token_id=o.token_id "
            "WHERE o.signal_at >= ? ORDER BY o.signal_at DESC LIMIT ?"
        ),
        "rejections": (
            "SELECT r.*, t.chain_id, t.contract_address, t.symbol "
            "FROM rejections r JOIN token_master t ON t.token_id=r.token_id "
            "WHERE r.occurred_at >= ? ORDER BY r.occurred_at DESC LIMIT ?"
        ),
        "milestones": (
            "SELECT m.*, t.chain_id, t.contract_address, t.symbol "
            "FROM milestones m JOIN token_master t ON t.token_id=m.token_id "
            "WHERE m.occurred_at >= ? ORDER BY m.occurred_at DESC LIMIT ?"
        ),
        "kpi": (
            "SELECT * FROM kpi_daily WHERE created_at >= ? "
            "ORDER BY stat_date DESC LIMIT ?"
        ),
    }
    sql = queries.get(dataset)
    if sql is None:
        raise HTTPException(
            status_code=400,
            detail=f"可导出的数据集: {sorted(queries)}",
        )

    service = get_service()
    since = now_ms() - int(since_hours * 3_600_000)
    rows = await service.db.fetch_all(sql, (since, limit))
    return {
        "dataset": dataset,
        "exported_at": now_ms(),
        "fingerprint": service.settings.fingerprint(),
        "count": len(rows),
        "rows": [dict(r) for r in rows],
    }


@router.get("/diagnostics/bundle")
async def diagnostics_bundle() -> dict[str, Any]:
    """一键诊断包：出问题时要的所有上下文一次性打包。

    存在的理由是排障时的时间成本：分十次调不同接口再手工拼起来，
    每一步都可能拿到不同时刻的状态，最后拼出一幅并不存在的图景。
    """
    service = get_service()
    await service.db.drain()
    recent_errors = await service.db.fetch_all(
        "SELECT * FROM radar_events WHERE severity IN ('error','critical') "
        "AND occurred_at >= ? ORDER BY occurred_at DESC LIMIT 100",
        (now_ms() - 6 * 3_600_000,),
    )
    table_sizes = {}
    for table in ("token_master", "snapshots", "alerts", "outcomes", "rejections",
                  "milestones", "radar_events", "raw_archive", "email_outbox"):
        row = await service.db.fetch_one(f"SELECT COUNT(*) AS n FROM {table}")
        table_sizes[table] = int(row["n"]) if row else 0

    pending_email = await service.db.fetch_all(
        "SELECT id, kind, subject, retry_count, last_error FROM email_outbox "
        "WHERE status='pending' ORDER BY created_at LIMIT 20"
    )
    return {
        "generated_at": now_ms(),
        "diagnostics": service.diagnostics(),
        "table_sizes": table_sizes,
        "db_file_bytes": _file_size(service),
        "recent_errors": [
            {**dict(e), "payload": _loads(dict(e).get("payload_json"))}
            for e in recent_errors
        ],
        "pending_emails": [dict(p) for p in pending_email],
    }


# ═════════════════════════════════════════════════════════════════════════
# 序列化辅助
# ═════════════════════════════════════════════════════════════════════════

def _token_summary(view: Any, now: int) -> dict[str, Any]:
    """代币摘要。

    评分与其可信度必须绑在一起返回。只给分数会诱导前端做出
    一个漂亮但危险的界面：用户看到高分就行动，
    却不知道这个分数是基于 30% 完整度的数据算出来的。
    """
    return {
        "chain_id": view.chain_id,
        "contract_address": view.contract_address,
        "symbol": view.symbol,
        "name": view.name,
        "state": view.state.value,
        "state_since_ms": view.state_since_ms,
        "age_sec": view.age_sec(now),
        "first_seen_ms": view.first_seen_ms,
        "last_observed_ms": view.last_observed_ms,
        "price": view.getf("price"),
        "market_cap": view.getf("market_cap"),
        "mc_source": view.field_source.get("market_cap"),
        "liquidity": view.getf("liquidity"),
        "holders": view.geti("holders"),
        "top10_percent": view.getf("top10_percent"),
        "dev_percent": view.getf("dev_percent"),
        "smart_money_count": view.geti("smart_money_count"),
        "net_inflow": view.getf("net_inflow"),
        "pct_change_1h": view.getf("pct_change_1h"),
        "volume_1h": view.getf("volume_1h"),
        "scores": {
            "opportunity": float(view.last_scores.get("opportunity", 0.0)),
            "confidence": float(view.last_scores.get("confidence", 0.0)),
            "data_quality": float(view.last_scores.get("data_quality", 0.0)),
            "rug_risk": float(view.last_scores.get("rug_risk", 0.0)),
            "distribution": float(view.last_scores.get("distribution", 0.0)),
        },
        "risk": {
            "blocked": view.blocked,
            "block_reason": view.block_reason,
            "gate_blocked": view.gate_blocked,
            "gate_reasons": list(view.gate_reasons),
            "audit_checked": bool(view.audit_checked_at),
        },
        "quality_degraded": view.quality_degraded,
        "is_reject_sample": view.is_reject_sample,
        "tags": sorted(view.tags),
    }


def _identity(view: Any, row: Any, token_id: int) -> dict[str, Any]:
    if view is not None:
        return {
            "token_id": token_id,
            "chain_id": view.chain_id,
            "contract_address": view.contract_address,
            "symbol": view.symbol,
            "name": view.name,
            "launch_time_ms": view.launch_time_ms,
            "launch_platform": view.launch_platform,
            "creator_address": view.creator_address,
            "in_memory": True,
        }
    data = dict(row)
    data["in_memory"] = False
    return data


def _alert_row(row: Any) -> dict[str, Any]:
    item = dict(row)
    for column in ("trigger_json", "factors_json", "prev_scores_json"):
        if column in item:
            item[column.removesuffix("_json")] = _loads(item.pop(column))
    return item


def _outcome_row(row: Any) -> dict[str, Any]:
    item = dict(row)
    item["horizons"] = _loads(item.pop("horizons_json", None))
    return item


def _loads(text: Any) -> Any:
    if not text:
        return None
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return None


def _file_size(service: RadarService) -> int:
    try:
        return service.db.path.stat().st_size
    except OSError:
        return 0


# ═════════════════════════════════════════════════════════════════════════
# 应用装配
# ═════════════════════════════════════════════════════════════════════════

def create_app(service: RadarService) -> FastAPI:
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        try:
            await service.start()
        except Exception:
            # 启动中途失败时数据库 writer 可能已经起来且队列里有东西。
            # 不收尾就直接退出，那些写入连同 WAL 一起悬在半空——
            # 而这恰恰是最需要保住"启动到哪一步炸的"证据的时刻
            await service.stop()
            raise
        bind_service(service)
        try:
            yield
        finally:
            await service.stop()

    app = FastAPI(
        title="LIQ 潜力币雷达",
        version=service.settings.strategy_version,
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=service.settings.service.get("cors_origins", []),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(router)
    return app
