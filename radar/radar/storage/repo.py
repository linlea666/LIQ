"""仓储层：领域对象 ↔ SQL。

所有 SQL 集中在这里，业务模块不直接拼 SQL。

INSERT 一律"先构造 列名→值 的字典，再由字典生成 SQL"，
而不是手写一长串列名再手写一长串占位符——后者只要少写一个占位符
就会在运行时炸，而且这类插入语句动辄 60 个字段。
由于字典键顺序稳定，生成的 SQL 文本每次一致，SQLite 仍能命中语句缓存。
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

from ..domain.models import QualityReport, ScoreResult, TokenView
from ..obs.events import RadarEvent
from .db import PRIORITY_CRITICAL, PRIORITY_DROPPABLE, Database, json_dump

logger = logging.getLogger("radar.repo")


def _insert_sql(table: str, row: Mapping[str, Any], *, or_ignore: bool = False) -> str:
    verb = "INSERT OR IGNORE INTO" if or_ignore else "INSERT INTO"
    columns = ",".join(row)
    placeholders = ",".join("?" * len(row))
    return f"{verb} {table} ({columns}) VALUES ({placeholders})"


# ═════════════════════════════════════════════════════════════════════════
# 事件（EventBus 的持久化 sink）
# ═════════════════════════════════════════════════════════════════════════

def make_event_sink(db: Database):
    """构造 EventBus 的落库 sink。

    事件写入用 DROPPABLE 优先级：队列极度紧张时，
    保住警报/Outcome 这类不可再生的数据比保住事件日志更重要，
    而运维日志（JSONL 文件）里始终有一份完整记录。
    但 CRITICAL/ERROR 级事件例外——那正是事后要查的东西。
    """

    def sink(event: RadarEvent) -> None:
        row = {
            "occurred_at": event.occurred_at,
            "event_type": event.event_type.value,
            "category": event.category.value,
            "severity": event.severity.value,
            "importance": event.importance.value,
            "module": event.module,
            "chain_id": event.chain_id,
            "token_id": event.token_id,
            "contract_address": event.contract_address,
            "symbol": event.symbol,
            "correlation_id": event.correlation_id or None,
            "old_state": event.old_state,
            "new_state": event.new_state,
            "snapshot_id": event.snapshot_id,
            "alert_id": event.alert_id,
            "duration_ms": event.duration_ms,
            "strategy_version": event.strategy_version or None,
            "feature_version": event.feature_version or None,
            "config_hash": event.config_hash or None,
            "summary": event.summary[:500] if event.summary else None,
            "payload_json": json_dump(event.payload) if event.payload else None,
        }
        priority = (
            PRIORITY_CRITICAL
            if event.severity.rank >= 40 or event.importance.value == "HIGH"
            else PRIORITY_DROPPABLE
        )
        db.submit(
            _insert_sql("radar_events", row),
            tuple(row.values()),
            priority=priority,
            label=f"event:{event.event_type.value}",
        )

    return sink


# ═════════════════════════════════════════════════════════════════════════
# 代币主表
# ═════════════════════════════════════════════════════════════════════════

async def upsert_token(db: Database, view: TokenView, *,
                       source: str) -> dict[str, Any]:
    """建档或取回已有档案，返回持久化的身份与状态字段。

    首次发现现场（first_seen_*）用 INSERT OR IGNORE 保护：
    这些字段回答"我们在多少市值时发现它"，一旦写入永不允许被覆盖，
    否则重启后会被当时的最新市值改写，领先时间的研究价值直接归零。

    返回整行而不只是 token_id，是因为代币可能因内存压力被淘汰后又重新出现。
    此时若只拿 token_id、其余字段用默认值，这枚币的状态会被悄悄重置回
    DISCOVERED，而数据库里明明记着它是 WATCHING、还是拒绝样本。
    """
    row = {
        "chain_id": view.chain_id,
        "contract_address": view.contract_address,
        "symbol": view.symbol,
        "name": view.name,
        "decimals": view.decimals,
        "launch_time_ms": view.launch_time_ms,
        "creator_address": view.creator_address,
        "launch_platform": view.launch_platform,
        "first_seen_ms": view.first_seen_ms,
        "first_seen_market_cap": view.getf("market_cap"),
        "first_seen_price": view.getf("price"),
        "first_seen_holders": view.geti("holders"),
        "first_seen_source": source,
        "state": view.state.value,
        "state_since_ms": view.state_since_ms or view.first_seen_ms,
        "last_observed_ms": view.last_observed_ms,
    }
    await db.submit_returning(
        _insert_sql("token_master", row, or_ignore=True),
        tuple(row.values()),
        label="token_insert",
    )
    found = await db.fetch_one(
        "SELECT token_id, state, state_since_ms, first_seen_ms, is_reject_sample "
        "FROM token_master WHERE chain_id=? AND contract_address=?",
        (view.chain_id, view.contract_address),
    )
    if not found:
        raise RuntimeError(f"建档失败: {view.chain_id}/{view.contract_address}")
    return found


def update_token_runtime(db: Database, view: TokenView, *,
                         priority: int = PRIORITY_DROPPABLE) -> None:
    """更新可变的运行时字段。不触碰 first_seen_*。

    priority 默认可丢弃（高频路径的常规刷新）；状态变更后的补写必须传
    PRIORITY_CRITICAL——那一次写入承载着"这枚币已经死亡/晋升"的事实，
    丢掉它会让 token_master 永远停在旧状态，重启恢复时把死币复活。
    """
    db.submit(
        "UPDATE token_master SET symbol=COALESCE(?,symbol), name=COALESCE(?,name), "
        "decimals=COALESCE(?,decimals), launch_time_ms=COALESCE(?,launch_time_ms), "
        "creator_address=COALESCE(?,creator_address), "
        "launch_platform=COALESCE(?,launch_platform), "
        "circulating_supply=COALESCE(?,circulating_supply), "
        "total_supply=COALESCE(?,total_supply), max_supply=COALESCE(?,max_supply), "
        "state=?, state_since_ms=?, last_observed_ms=?, last_snapshot_ms=?, "
        "is_reject_sample=?, retention_class=? "
        "WHERE token_id=?",
        (
            view.symbol, view.name, view.decimals, view.launch_time_ms,
            view.creator_address, view.launch_platform,
            view.getf("circulating_supply"), view.getf("total_supply"),
            view.getf("max_supply"),
            view.state.value, view.state_since_ms, view.last_observed_ms,
            view.last_snapshot_ms,
            1 if view.is_reject_sample else 0,
            "important" if view.is_reject_sample or view.state.rank >= 3 else "normal",
            view.token_id,
        ),
        priority=priority,
        label="token_update",
    )


# ═════════════════════════════════════════════════════════════════════════
# 快照
# ═════════════════════════════════════════════════════════════════════════

# 直接来自观测值的快照列（列名 == TokenView.values 的键名）
_SNAPSHOT_VALUE_COLUMNS: tuple[str, ...] = (
    "price", "market_cap", "fdv", "liquidity",
    "interval_high", "interval_low", "interval_volume",
    "price_high_24h", "price_low_24h",
    "bonding_progress", "migrate_status", "binance_score",
    "holders", "kyc_holders",
    "top10_percent", "dev_percent", "sniper_percent", "insider_percent",
    "bundler_percent", "new_wallet_percent", "smart_money_percent",
    "kol_percent", "pro_percent", "sniper_count", "dev_sell_percent",
    "volume_5m", "volume_1h", "volume_4h", "volume_24h",
    "volume_1h_buy", "volume_1h_sell",
    "count_5m", "count_1h", "count_1h_buy", "count_1h_sell",
    "unique_trader_5m", "unique_trader_1h", "unique_trader_24h",
    "pct_change_5m", "pct_change_1h", "pct_change_4h", "pct_change_24h",
    "volume_agg", "count_agg", "count_agg_buy", "count_agg_sell", "pct_change_agg",
    "smart_money_count", "smart_money_traders", "exit_rate", "max_gain",
    "alert_market_cap", "net_inflow",
    "signal_direction", "signal_type", "signal_status",
    "social_hype", "social_hype_cn", "social_hype_en", "kol_count",
    "search_count_24h", "sentiment", "twitter_followers",
    "audit_risk_level", "buy_tax_pct", "sell_tax_pct",
)

# 布尔型观测值需要转成 0/1
_SNAPSHOT_BOOL_COLUMNS: tuple[str, ...] = ("audit_available", "contract_verified")


def build_snapshot_row(
    view: TokenView,
    *,
    observed_at: int,
    stored_at: int,
    endpoint: str,
    latency_ms: int | None,
    response_hash: str | None,
    parser_version: str,
    cohort: Mapping[str, str | None],
    features_json: str | None,
    scores: ScoreResult | None,
    quality: QualityReport | None,
    risk_flags_json: str | None,
    risk_parser_version: str,
    keep_forever: bool,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "token_id": view.token_id,
        # source_at：币安未在响应里给出数据生成时间，因此保持 NULL 而不是
        # 拿 observed_at 顶替——伪造这个字段会让"我们领先多久"的研究失去意义
        "source_at": None,
        "observed_at": observed_at,
        "stored_at": stored_at,
        "endpoint": endpoint,
        "latency_ms": latency_ms,
        "response_hash": response_hash,
        "parser_version": parser_version,
        "cohort_chain": cohort.get("chain"),
        "cohort_age_bucket": cohort.get("age_bucket"),
        "cohort_mc_bucket": cohort.get("mc_bucket"),
        "cohort_stage": cohort.get("stage"),
    }
    for column in _SNAPSHOT_VALUE_COLUMNS:
        row[column] = view.get(column)
    for column in _SNAPSHOT_BOOL_COLUMNS:
        value = view.get(column)
        row[column] = None if value is None else (1 if value else 0)

    row["reported_market_cap"] = view.getf("market_cap")
    row["computed_market_cap"] = quality.computed_market_cap if quality else None
    row["mc_deviation_ratio"] = quality.mc_deviation_ratio if quality else None
    row["features_json"] = features_json
    row["opportunity"] = scores.opportunity if scores else None
    row["confidence"] = scores.confidence if scores else None
    row["data_quality"] = scores.data_quality if scores else None
    row["rug_risk"] = scores.rug_risk if scores else None
    row["distribution"] = scores.distribution if scores else None
    row["dq_json"] = json_dump(quality.as_dict()) if quality else None
    row["risk_flags_json"] = risk_flags_json
    row["risk_parser_version"] = risk_parser_version
    # 年龄以观测时刻为基准，而不是写库时刻：Replay 重放历史快照时
    # 必须复现"当时"的年龄，否则年龄分档条件会与线上判断不一致
    row["token_age_sec"] = view.age_sec(observed_at)
    row["state"] = view.state.value
    row["keep_forever"] = 1 if keep_forever else 0
    return row


async def insert_snapshot(db: Database, row: dict[str, Any]) -> int:
    return await db.submit_returning(
        _insert_sql("snapshots", row), tuple(row.values()), label="snapshot"
    )


def insert_snapshot_nowait(db: Database, row: dict[str, Any]) -> None:
    """不需要 snapshot_id 时走这条路径：避免为每个快照创建 Future。

    绝大多数快照（WATCHING/S0 的常规刷新）不需要 id，
    这条路径能显著降低高频写入的开销。
    """
    db.submit(
        _insert_sql("snapshots", row),
        tuple(row.values()),
        priority=PRIORITY_DROPPABLE,
        label="snapshot",
    )


# ═════════════════════════════════════════════════════════════════════════
# 警报 / 拒绝 / 里程碑
# ═════════════════════════════════════════════════════════════════════════

async def insert_alert(
    db: Database,
    *,
    view: TokenView,
    alert_kind: str,
    is_near_miss: bool,
    created_at: int,
    correlation_id: str,
    snapshot_id: int | None,
    scores: ScoreResult,
    factors_json: str | None,
    trigger_json: str | None,
    prev_scores_json: str | None,
    fingerprint: Mapping[str, str],
) -> int:
    row = {
        "token_id": view.token_id,
        "alert_kind": alert_kind,
        "is_near_miss": 1 if is_near_miss else 0,
        "created_at": created_at,
        "correlation_id": correlation_id or None,
        "snapshot_id": snapshot_id,
        "market_cap": view.getf("market_cap"),
        "price": view.getf("price"),
        "liquidity": view.getf("liquidity"),
        "holders": view.geti("holders"),
        "token_age_sec": view.age_sec(created_at),
        "opportunity": scores.opportunity,
        "confidence": scores.confidence,
        "data_quality": scores.data_quality,
        "rug_risk": scores.rug_risk,
        "distribution": scores.distribution,
        "factors_json": factors_json,
        "trigger_json": trigger_json,
        "prev_scores_json": prev_scores_json,
        "strategy_version": fingerprint.get("strategy_version"),
        "feature_version": fingerprint.get("feature_version"),
        "parser_version": fingerprint.get("parser_version"),
        "config_hash": fingerprint.get("config_hash"),
        "code_commit": fingerprint.get("code_commit"),
    }
    return await db.submit_returning(
        _insert_sql("alerts", row), tuple(row.values()), label=f"alert:{alert_kind}"
    )


def insert_rejection(
    db: Database,
    *,
    view: TokenView,
    occurred_at: int,
    gate: str,
    rule: str,
    actual_value: float | None,
    threshold_value: float | None,
    actual_text: str | None,
    data_quality: float | None,
    snapshot_id: int | None,
    correlation_id: str,
    fingerprint: Mapping[str, str],
) -> None:
    """记录一次拒绝。

    必须同时保存 规则名 / 实际值 / 阈值 / 当时的年龄与市值，
    而不是只存一句 reason。三个月后要回答的问题是
    "被 top10_max 拒掉的币里有多少后来涨了 20 倍"，
    只有结构化的实际值与阈值才能支撑这种反事实查询。
    """
    row = {
        "token_id": view.token_id,
        "occurred_at": occurred_at,
        "gate": gate,
        "rule": rule,
        "actual_value": actual_value,
        "threshold_value": threshold_value,
        "actual_text": actual_text,
        "token_age_sec": view.age_sec(occurred_at),
        "market_cap": view.getf("market_cap"),
        "holders": view.geti("holders"),
        "liquidity": view.getf("liquidity"),
        "data_quality": data_quality,
        "strategy_version": fingerprint.get("strategy_version"),
        "config_hash": fingerprint.get("config_hash"),
        "snapshot_id": snapshot_id,
        "correlation_id": correlation_id or None,
    }
    db.submit(_insert_sql("rejections", row), tuple(row.values()), label=f"reject:{rule}")


def insert_milestone(
    db: Database,
    *,
    view: TokenView,
    milestone_usd: float,
    direction: str,
    sequence: int,
    is_first_upcross: bool,
    occurred_at: int,
    data_quality: float | None,
    mc_source: str,
    snapshot_id: int | None,
) -> None:
    row = {
        "token_id": view.token_id,
        "milestone_usd": milestone_usd,
        "direction": direction,
        "sequence": sequence,
        "is_first_upcross": 1 if is_first_upcross else 0,
        "occurred_at": occurred_at,
        "token_age_sec": view.age_sec(occurred_at),
        "market_cap": view.getf("market_cap"),
        "price": view.getf("price"),
        "liquidity": view.getf("liquidity"),
        "holders": view.geti("holders"),
        "data_quality": data_quality,
        "mc_source": mc_source,
        "snapshot_id": snapshot_id,
        "state": view.state.value,
    }
    # UNIQUE 约束负责幂等：同一里程碑重复上报会被静默忽略
    db.submit(
        _insert_sql("milestones", row, or_ignore=True),
        tuple(row.values()),
        label=f"milestone:{milestone_usd:.0f}",
    )


# ═════════════════════════════════════════════════════════════════════════
# Outcome 与纸面仓位
# ═════════════════════════════════════════════════════════════════════════

# Outcome 是全库最值钱的数据，用 CRITICAL 优先级：
# 队列紧张时宁可丢事件日志，也不能丢"这次判断到底对不对"的答案。
_OUTCOME_COLUMNS = (
    "signal_price", "signal_market_cap", "signal_liquidity",
    "entry_15s", "entry_30s", "entry_60s", "entry_120s",
    "raw_ath_price", "raw_ath_mc", "raw_ath_at",
    "sustained_ath_price", "sustained_ath_mc", "sustained_ath_at",
    "liq_adjusted_multiple", "min_price", "min_price_at",
    "horizons_json", "time_to_2x_sec", "time_to_5x_sec", "time_to_10x_sec",
    "peak_multiple", "current_multiple", "mfe_pct", "mae_pct",
    "outcome_label", "trending_seen_at", "lead_time_sec",
    "last_updated", "is_final",
)


def upsert_outcome(db: Database, *, alert_id: int, token_id: int,
                   signal_at: int, values: Mapping[str, Any]) -> None:
    """写入或更新一条 Outcome。

    用 ON CONFLICT 而非"先查再写"：追踪器每个周期都会更新同一行，
    读改写模式在单写入协程架构下会来回穿越队列，延迟高且有竞态。
    """
    row: dict[str, Any] = {
        "alert_id": alert_id,
        "token_id": token_id,
        "signal_at": signal_at,
    }
    for column in _OUTCOME_COLUMNS:
        row[column] = values.get(column)

    updates = ",".join(f"{c}=excluded.{c}" for c in _OUTCOME_COLUMNS)
    db.submit(
        f"{_insert_sql('outcomes', row)} ON CONFLICT(alert_id) DO UPDATE SET {updates}",
        tuple(row.values()),
        priority=PRIORITY_CRITICAL,
        label="outcome",
    )


def upsert_paper_position(db: Database, *, alert_id: int, token_id: int,
                          size_usd: float, opened_at: int,
                          values: Mapping[str, Any]) -> None:
    columns = (
        "entry_price", "entry_price_source", "est_slippage_pct",
        "effective_entry_price", "peak_value_usd", "current_value_usd",
        "closed_at", "exit_price", "realized_multiple", "status", "last_updated",
    )
    row: dict[str, Any] = {
        "alert_id": alert_id,
        "token_id": token_id,
        "size_usd": size_usd,
        "opened_at": opened_at,
    }
    for column in columns:
        row[column] = values.get(column)

    updates = ",".join(f"{c}=excluded.{c}" for c in columns)
    db.submit(
        f"{_insert_sql('paper_positions', row)} "
        f"ON CONFLICT(alert_id, size_usd) DO UPDATE SET {updates}",
        tuple(row.values()),
        priority=PRIORITY_CRITICAL,
        label="paper_position",
    )


def upsert_kpi_daily(db: Database, *, stat_date: str, strategy_version: str,
                     alert_kind: str, horizon: str, matured_count: int,
                     payload: Mapping[str, Any], created_at: int) -> None:
    row = {
        "stat_date": stat_date,
        "strategy_version": strategy_version,
        "alert_kind": alert_kind,
        "horizon": horizon,
        "matured_count": matured_count,
        "payload_json": json_dump(dict(payload)),
        "created_at": created_at,
    }
    db.submit(
        f"{_insert_sql('kpi_daily', row)} "
        "ON CONFLICT(stat_date, strategy_version, alert_kind, horizon) "
        "DO UPDATE SET matured_count=excluded.matured_count, "
        "payload_json=excluded.payload_json, created_at=excluded.created_at",
        tuple(row.values()),
        label="kpi_daily",
    )


# ═════════════════════════════════════════════════════════════════════════
# 原始归档 / 邮件 / 市场环境 / 配置审计
# ═════════════════════════════════════════════════════════════════════════

def insert_raw_archive(
    db: Database,
    *,
    fetched_at: int,
    endpoint: str,
    chain_id: str | None,
    token_id: int | None,
    kind: str,
    http_status: int,
    latency_ms: int,
    response_hash: str,
    item_count: int,
    payload_gz: bytes | None,
    retention_class: str,
    expires_at: int | None,
) -> None:
    row = {
        "fetched_at": fetched_at,
        "endpoint": endpoint,
        "chain_id": chain_id,
        "token_id": token_id,
        "kind": kind,
        "http_status": http_status,
        "latency_ms": latency_ms,
        "response_hash": response_hash,
        "item_count": item_count,
        "payload_gz": payload_gz,
        "retention_class": retention_class,
        "expires_at": expires_at,
    }
    db.submit(
        _insert_sql("raw_archive", row),
        tuple(row.values()),
        priority=PRIORITY_DROPPABLE,
        label="raw_archive",
    )


async def enqueue_email(
    db: Database,
    *,
    idempotency_key: str,
    kind: str,
    subject: str,
    html: str,
    token_id: int | None,
    alert_id: int | None,
    created_at: int,
) -> None:
    """入队一封邮件。

    幂等键的作用：进程在"警报已落库、邮件未发出"之间崩溃重启后，
    重放不会产生第二封相同邮件；同时 UNIQUE 约束让并发路径也安全。
    """
    row = {
        "idempotency_key": idempotency_key,
        "kind": kind,
        "subject": subject,
        "html": html,
        "token_id": token_id,
        "alert_id": alert_id,
        "status": "pending",
        "retry_count": 0,
        "next_retry_at": created_at,
        "created_at": created_at,
    }
    await db.submit_returning(
        _insert_sql("email_outbox", row, or_ignore=True),
        tuple(row.values()),
        label="email_enqueue",
    )


def mark_email_sent(db: Database, outbox_id: int, sent_at: int) -> None:
    db.submit(
        "UPDATE email_outbox SET status='sent', sent_at=?, last_error=NULL WHERE id=?",
        (sent_at, outbox_id),
        label="email_sent",
    )


def mark_email_failed(db: Database, outbox_id: int, *, error: str,
                      retry_count: int, next_retry_at: int | None,
                      give_up: bool) -> None:
    db.submit(
        "UPDATE email_outbox SET status=?, retry_count=?, next_retry_at=?, last_error=? "
        "WHERE id=?",
        (
            "failed" if give_up else "pending",
            retry_count,
            next_retry_at,
            error[:500],
            outbox_id,
        ),
        label="email_failed",
    )


def insert_market_regime(
    db: Database,
    *,
    # 【预留，当前无调用方】市况分层（S2 确认阈值随大盘冷热调整）需要
    # 一个独立的大盘数据采集器（全链新币数/总成交/总净流入），采集器
    # 尚未实现。表结构与本函数先行落位，接线时无需迁移历史库。
    # 若 V2.x 决定不做市况分层，应连同 schema 的 market_regime 表
    # 与 /market-regime API 一起移除
    recorded_at: int,
    chain_id: str,
    new_token_count: int | None,
    total_volume_usd: float | None,
    total_net_inflow_usd: float | None,
    median_pct_change_1h: float | None,
    trending_activity: float | None,
    regime_label: str | None,
    payload: dict[str, Any] | None,
) -> None:
    row = {
        "recorded_at": recorded_at,
        "chain_id": chain_id,
        "new_token_count": new_token_count,
        "total_volume_usd": total_volume_usd,
        "total_net_inflow_usd": total_net_inflow_usd,
        "median_pct_change_1h": median_pct_change_1h,
        "trending_activity": trending_activity,
        "regime_label": regime_label,
        "payload_json": json_dump(payload),
    }
    db.submit(
        _insert_sql("market_regime", row),
        tuple(row.values()),
        priority=PRIORITY_DROPPABLE,
        label="market_regime",
    )


async def record_config_audit(
    db: Database,
    *,
    recorded_at: int,
    fingerprint: Mapping[str, str],
    config_snapshot: str,
    changes: dict[str, Any] | None,
    operator: str,
) -> bool:
    """记录配置指纹。

    只在指纹变化时写入，因此每次重启不会灌满这张表；
    但一旦阈值变动，旧配置的完整快照会永久保留——
    否则半年后无法回答"当时那套参数到底是什么"。
    返回是否新增了记录。
    """
    latest = await db.fetch_one(
        "SELECT config_hash FROM config_audit ORDER BY id DESC LIMIT 1"
    )
    prev_hash = latest["config_hash"] if latest else None
    if prev_hash == fingerprint.get("config_hash"):
        return False

    row = {
        "recorded_at": recorded_at,
        "config_hash": fingerprint.get("config_hash"),
        "prev_config_hash": prev_hash,
        "strategy_version": fingerprint.get("strategy_version"),
        "feature_version": fingerprint.get("feature_version"),
        "parser_version": fingerprint.get("parser_version"),
        "code_commit": fingerprint.get("code_commit"),
        "changes_json": json_dump(changes),
        "operator": operator,
        "config_snapshot": config_snapshot,
    }
    await db.submit_returning(
        _insert_sql("config_audit", row), tuple(row.values()), label="config_audit"
    )
    return True
