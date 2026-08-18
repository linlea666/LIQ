"""Bottom Model 快照组装：六因子 → 双层评分 → 四象限 → 类比/反证/质量标注。

historical analog（历史类比）实现说明：
因子引擎是纯函数（序列 in → 分数 out），把序列截断到历史底部日期再跑
同一引擎即得"当年同期因子向量"，与当前向量按共同有效因子求平均绝对差。
早年缺失的指标（OI/ETF/清算等）自动弃权，类比结果附 common_factors
数量供外部 AI 判断置信度——共同因子少的类比参考价值低。
"""

from __future__ import annotations

import logging
import hashlib
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from processors.bottom_model.factors import (
    SeriesIndex,
    Rows,
    build_counter_evidence,
    clamp,
    classify_quadrant,
    compute_confirmation,
    compute_demand_dimensions,
    compute_factors,
    compute_fake_bottom_filter,
    compute_seller_exhaustion,
    compute_stress,
)
from processors.bottom_model.base_rate import compute_base_rate
from processors.bottom_model.correlation import compute_correlation_audit
from processors.bottom_model.metrics import (
    build_registry,
    metric_contract,
    sanitize_series,
)
from storage.bottom_model_store import BottomModelStore

logger = logging.getLogger(__name__)

# v5.1：reclaim_sth_rp 评分方向修复（跌破越深分越高）+ 新增 mvrv_raw（原始
# MVRV 绝对锚定）与 lth_rp_discount（价格 vs LTH 持仓成本折价）两个子信号。
# 版本号进入 replay/审计缓存键，bump 后旧口径缓存自动失效重算。
ALGORITHM_VERSION = "bottom-v5.1"
SCHEMA_VERSION = "2.1"
MODEL_ID = ALGORITHM_VERSION
DATA_POLICY_ID = "pit-final-v2"

# 历史周期底部参考日（公认的周期低点附近，用于因子向量类比）
HISTORICAL_BOTTOMS: tuple[tuple[str, str], ...] = (
    ("2015-01-14", "2013-15 熊市大底"),
    ("2018-12-15", "2018 熊市大底"),
    ("2020-03-13", "COVID 流动性崩盘底"),
    ("2022-11-21", "FTX 崩盘底"),
)

# 数据新鲜度容忍（天）：超过视为 stale 写入 data_quality。
# weekly=14：周线在"上周完整周一"与当前日期间最大自然间隔 13 天，10 会误报
_STALENESS_TOLERANCE = {"daily": 3, "weekly": 14}


def load_all_series(store: BottomModelStore) -> dict[str, Rows]:
    """一次加载全部注册指标序列（~30 指标 × ≤6500 天，内存数 MB 级）。"""
    data: dict[str, Rows] = {}
    for spec in build_registry():
        for metric in spec.metrics:
            data[metric] = sanitize_series(metric, store.series(metric))
    return data


def truncate_asof(data: dict[str, Rows], day: str) -> dict[str, Rows]:
    """把所有序列截断到 day（含）——历史类比复用因子引擎的关键。"""
    return SeriesIndex(data).truncate(day)


def apply_data_policy(data: dict[str, Rows], decision_as_of: str) -> dict[str, Rows]:
    """按决策时点截断并排除未收盘周线；兼容 legacy series 的读时防线。"""
    decision = date.fromisoformat(decision_as_of)
    current_monday = decision - timedelta(days=decision.weekday())
    last_complete_monday = current_monday - timedelta(days=7)
    weekly_metrics = {
        metric for spec in build_registry() if spec.cadence == "weekly"
        for metric in spec.metrics
    }
    out: dict[str, Rows] = {}
    for metric, rows in data.items():
        cutoff = last_complete_monday.isoformat() if metric in weekly_metrics \
            else decision_as_of
        out[metric] = [(day, value) for day, value in rows if day <= cutoff]
    return out


def dataset_fingerprint(data: dict[str, Rows]) -> str:
    digest = hashlib.sha256()
    for metric in sorted(data):
        for day, value in data[metric]:
            digest.update(f"{metric}|{day}|{value:.17g}\n".encode())
    return digest.hexdigest()


def compute_core(data: dict[str, Rows],
                 as_of: Optional[str] = None) -> dict[str, Any]:
    """纯计算核心：因子 → Stress / Confirmation（含假底惩罚）→ 象限。

    as_of 传给因子/确认层仅用于证据质量的新鲜度乘子；历史类比传入各自的
    截断日，保证当年评估只惩罚"当年就已滞后"的数据。
    """
    factors = compute_factors(data, as_of)
    stress = compute_stress(factors)
    demand_dimensions = compute_demand_dimensions(factors)
    confirmation_raw = compute_confirmation(data, as_of)
    fake_filter = compute_fake_bottom_filter(data)
    seller_exhaustion = compute_seller_exhaustion(data)
    conf_score = confirmation_raw["score"]
    adjusted = (
        round(clamp(conf_score - fake_filter["total_penalty"]), 1)
        if conf_score is not None else None
    )
    stress_score = stress["score"] if stress else None
    return {
        "factors": factors,
        "stress": stress,
        "confirmation": {
            "score": adjusted,
            "score_before_penalty": conf_score,
            "evidence_quality": confirmation_raw.get("evidence_quality"),
            "checks": confirmation_raw["checks"],
        },
        "fake_bottom_filter": fake_filter,
        "quadrant": classify_quadrant(stress_score, adjusted),
        "seller_exhaustion": seller_exhaustion,
        "demand_dimensions": demand_dimensions,
        "counter_evidence": build_counter_evidence(
            factors, fake_filter, confirmation_raw, seller_exhaustion,
            demand_dimensions,
        ),
    }


def _factor_vector(core: dict[str, Any]) -> dict[str, float]:
    return {
        f["key"]: f["score"] for f in core["factors"] if f["score"] is not None
    }


def compute_analogs(data: dict[str, Rows], current_core: dict[str, Any]) -> list[dict[str, Any]]:
    """当前因子向量 vs 历史底部同期向量的相似度（0-100）。"""
    current_vec = _factor_vector(current_core)
    index = SeriesIndex(data)
    analogs: list[dict[str, Any]] = []
    for day, label in HISTORICAL_BOTTOMS:
        try:
            past_core = compute_core(apply_data_policy(index.truncate(day), day), day)
        except Exception:
            logger.warning("BottomModel analog compute failed | day=%s", day, exc_info=True)
            continue
        past_vec = _factor_vector(past_core)
        common = sorted(set(current_vec) & set(past_vec))
        if len(common) < 2:
            analogs.append({
                "day": day, "label": label, "similarity": None,
                "common_factors": common,
                "note": "共同有效因子不足，无法类比",
            })
            continue
        mean_diff = sum(abs(current_vec[k] - past_vec[k]) for k in common) / len(common)
        past_eq = (past_core["stress"] or {}).get("evidence_quality")
        # 相似度取整：共同因子仅 3-5 个时，小数位是虚假精度
        reliability = "low" if len(common) <= 3 else "high" if (
            len(common) >= 5 and (past_eq or 0) >= 60
        ) else "medium"
        analogs.append({
            "day": day,
            "label": label,
            "similarity": round(100.0 - mean_diff),
            "common_factors": common,
            "reliability": reliability,
            "past_scores": {k: past_vec[k] for k in common},
            "past_stress": past_core["stress"]["score"] if past_core["stress"] else None,
            "past_evidence_quality": past_eq,
            "note": f"基于 {len(common)}/6 个共同有效因子" + (
                "（早期数据缺失较多，置信度有限）" if len(common) <= 3 else ""
            ) + (f"，当年证据质量 {past_eq:.0f}" if past_eq is not None else ""),
        })
    return analogs


def compute_data_quality(store: BottomModelStore, data: dict[str, Rows],
                         as_of_day: str) -> dict[str, Any]:
    """逐指标新鲜度/窗口标注——证据包防外部 AI 误读的关键。"""
    cadence_by_metric: dict[str, str] = {}
    tolerance_override: dict[str, int] = {}
    active_spec_keys: set[str] = set()
    for spec in build_registry():
        active_spec_keys.add(spec.key)
        for metric in spec.metrics:
            cadence_by_metric[metric] = spec.cadence
            if spec.staleness_days is not None:
                tolerance_override[metric] = spec.staleness_days
    as_of = datetime.strptime(as_of_day, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    missing: list[str] = []
    blocking_missing: list[str] = []
    advisory_missing: list[str] = []
    stale: list[dict[str, Any]] = []
    blocking_stale: list[dict[str, Any]] = []
    advisory_stale: list[dict[str, Any]] = []
    metrics_meta: dict[str, Any] = {}
    for metric, rows in data.items():
        if not rows:
            missing.append(metric)
            role = metric_contract(metric).get("role")
            if role == "model_input":
                blocking_missing.append(metric)
            elif role != "unused":
                advisory_missing.append(metric)
            continue
        first_day, last_day = rows[0][0], rows[-1][0]
        behind = (as_of - datetime.strptime(last_day, "%Y-%m-%d")
                  .replace(tzinfo=timezone.utc)).days
        tolerance = tolerance_override.get(
            metric,
            _STALENESS_TOLERANCE.get(cadence_by_metric.get(metric, "daily"), 3),
        )
        observation = store.observation_meta(metric, as_of_day=as_of_day)
        metrics_meta[metric] = {
            "first_day": first_day, "last_day": last_day,
            "days": len(rows), "behind_days": behind,
            "cadence": cadence_by_metric.get(metric, "daily"),
            "role": metric_contract(metric).get("role"),
            "unit": metric_contract(metric).get("unit"),
            "deprecated": bool(metric_contract(metric).get("deprecated")),
            "availability": observation,
            "pit_status": (observation or {}).get("quality_flag") or "PIT_UNAVAILABLE",
        }
        if behind > tolerance:
            item = {"metric": metric, "last_day": last_day, "behind_days": behind}
            stale.append(item)
            if metric_contract(metric).get("role") == "model_input":
                blocking_stale.append(item)
            elif metric_contract(metric).get("role") != "unused":
                advisory_stale.append(item)
    # 只报告注册表内 spec 的失败——已停采指标的失败旧行不应永久误报
    failed_fetches = {
        key: item["last_error"]
        for key, item in store.fetch_log().items()
        if key in active_spec_keys and not item["last_ok"]
    }
    return {
        "ok": not blocking_missing and not blocking_stale and not failed_fetches,
        "missing": missing,
        "blocking_missing": blocking_missing,
        "advisory_missing": advisory_missing,
        "stale": stale,
        "blocking_stale": blocking_stale,
        "advisory_stale": advisory_stale,
        "failed_fetches": failed_fetches,
        "metrics": metrics_meta,
    }


def _price_context(data: dict[str, Rows]) -> dict[str, Any]:
    def _last(metric: str) -> Optional[float]:
        rows = data.get(metric, [])
        return rows[-1][1] if rows else None

    return {
        "price": _last("btc_price_onchain"),
        "ma_200w": _last("ma_200w"),
        "sth_realized_price": _last("sth_realized_price"),
        "lth_realized_price": _last("lth_realized_price"),
        # 原始 MVRV 比率：抄底估值带用（价格/MVRV ≈ 聚合已实现价格）
        "mvrv_raw": _last("mvrv_raw"),
    }


def compute_delta(store: BottomModelStore, stress: Optional[float],
                  confirmation: Optional[float]) -> dict[str, Any]:
    """与 7d/30d 前快照的评分差（ΔScore 趋势变化率）。"""
    history = store.snapshot_history(limit=45)
    by_day = {item["day"]: item for item in history}

    def _score_at(days_ago: int, field: str) -> Optional[float]:
        target_ts = time.time() - days_ago * 86400
        target_day = time.strftime("%Y-%m-%d", time.gmtime(target_ts))
        candidates = [d for d in by_day if d <= target_day]
        if not candidates:
            return None
        item = by_day[max(candidates)]
        block = item.get(field) or {}
        return block.get("score")

    def _delta(current: Optional[float], past: Optional[float]) -> Optional[float]:
        if current is None or past is None:
            return None
        return round(current - past, 1)

    return {
        "stress_7d": _delta(stress, _score_at(7, "stress")),
        "stress_30d": _delta(stress, _score_at(30, "stress")),
        "confirmation_7d": _delta(confirmation, _score_at(7, "confirmation")),
        "confirmation_30d": _delta(confirmation, _score_at(30, "confirmation")),
    }


def build_snapshot(store: BottomModelStore,
                   as_of_day: Optional[str] = None, *,
                   persist: bool = True) -> dict[str, Any]:
    """组装并落库当日快照。as_of_day 默认 = 数据允许的最新日（价格序列末日）。"""
    data = load_all_series(store)
    if as_of_day is None:
        price_rows = data.get("btc_price_onchain") or data.get("btc_close_1d") or []
        as_of_day = price_rows[-1][0] if price_rows \
            else time.strftime("%Y-%m-%d", time.gmtime())
    data = apply_data_policy(data, as_of_day)
    dataset_id = dataset_fingerprint(data)

    core = compute_core(data, as_of_day)
    stress_score = core["stress"]["score"] if core["stress"] else None
    conf_score = core["confirmation"]["score"]
    stress_eq = (core["stress"] or {}).get("evidence_quality")
    conf_eq = core["confirmation"].get("evidence_quality")
    overall_eq = [eq for eq in (stress_eq, conf_eq) if eq is not None]
    data_quality = compute_data_quality(store, data, as_of_day)
    blockers = []
    if data_quality.get("blocking_missing"):
        blockers.append("MISSING_MODEL_INPUTS")
    if data_quality.get("blocking_stale"):
        blockers.append("STALE_MODEL_INPUTS")
    if data_quality.get("failed_fetches"):
        blockers.append("FETCH_FAILURE")
    if conf_score is None:
        blockers.append("INSUFFICIENT_CONFIRMATION_COVERAGE")
    invalid_data = any(reason in blockers for reason in (
        "MISSING_MODEL_INPUTS", "STALE_MODEL_INPUTS", "FETCH_FAILURE",
    ))
    quality_status = (
        "INVALID_DATA" if invalid_data
        else "ABSTAINED" if stress_score is None or conf_score is None
        else "DEGRADED" if (
            data_quality.get("advisory_missing") or data_quality.get("advisory_stale")
        ) else "OK"
    )
    matched_audit = store.latest_audit_matching(MODEL_ID, DATA_POLICY_ID, dataset_id)
    if matched_audit:
        sample_sizes = matched_audit.get("sample_sizes") or {}
        walk_metrics = ((matched_audit.get("walk_forward") or {}).get("metrics") or {})
        errors = matched_audit.get("errors") or {}
        audit_summary = {
            "status": matched_audit.get("status"),
            "pit_status": matched_audit.get("pit_status"),
            "probability_publishable": matched_audit.get("probability_publishable", False),
            "primary_oos_status": walk_metrics.get("validation_status"),
            "event_n": sample_sizes.get("event_n"),
            "non_overlapping_n_180d": sample_sizes.get("non_overlapping_n_180d"),
            "n_eff": sample_sizes.get("n_eff"),
            "false_positive_dates": [
                item.get("day") for item in errors.get("false_positive_top10", [])[:3]
            ],
            "false_negative_dates": [
                item.get("day") for item in errors.get("false_negative_top10", [])[:3]
            ],
        }
        audit_match = {
            "status": "MATCHED", "audit_id": matched_audit.get("audit_id"),
            "reason": "model_id/data_policy_id/dataset_id exact match",
            "summary": audit_summary,
        }
    else:
        audit_match = {
            "status": "NO_MATCHING_AUDIT", "audit_id": None,
            "reason": "no audit with exact model_id/data_policy_id/dataset_id",
            "summary": None,
        }
    run_id = hashlib.sha256(
        f"{MODEL_ID}|{DATA_POLICY_ID}|{dataset_id}|{as_of_day}|{time.time_ns()}".encode(),
    ).hexdigest()[:24]
    base_rate = compute_base_rate(
        store, data, core, ALGORITHM_VERSION, compute_core,
        data_policy_id=DATA_POLICY_ID, dataset_id=dataset_id,
    )
    current_condition = ((base_rate or {}).get("conditions") or [None])[0]
    time_scales = [
        {
            "days": int(window["weeks"]) * 7,
            "historical_frequency": window.get("hit_rate"),
            "ci95": window.get("hit_rate_ci95"),
            "independent_n": window.get("independent"),
            "median_terminal_return": window.get("median_return"),
            "kind": "historical_frequency",
        }
        for window in ((current_condition or {}).get("windows") or [])
    ]
    snapshot = {
        "day": as_of_day,
        "ts": int(time.time()),
        "algorithm_version": ALGORITHM_VERSION,
        "schema_version": SCHEMA_VERSION,
        "model_id": MODEL_ID,
        "data_policy_id": DATA_POLICY_ID,
        "dataset_id": dataset_id,
        # day 是兼容字段（最后纳入评分的数据日）；as_of 是实际生成/决策时点。
        "as_of": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "validation_status": "INSUFFICIENT_EVIDENCE",
        "audit_id": audit_match["audit_id"],
        "audit_match": audit_match,
        "prediction": {
            "kind": "score", "score": stress_score,
            "probability": None,
            "historical_frequency": "base_rate",
            "time_scales": time_scales,
        },
        "quality_status": quality_status,
        "blocking_reasons": blockers,
        "price_context": _price_context(data),
        **core,
        "evidence_quality": {
            "stress": stress_eq,
            "confirmation": conf_eq,
            "overall": round(sum(overall_eq) / len(overall_eq), 1) if overall_eq else None,
        },
        "correlation_audit": compute_correlation_audit(data),
        "base_rate": base_rate,
        "analogs": compute_analogs(data, core),
        "delta": compute_delta(store, stress_score, conf_score),
        "data_quality": data_quality,
        "metric_roles": {
            metric: metric_contract(metric).get("role") for metric in data
        },
        "frozen_series_id": dataset_id,
        # 新快照的证据包只读取此冻结切片，不再随实时数据库变化。
        "frozen_series": {metric: rows[-120:] for metric, rows in data.items()},
    }
    store.save_model_run({
        "run_id": run_id, "model_id": MODEL_ID,
        "data_policy_id": DATA_POLICY_ID, "dataset_id": dataset_id,
        "decision_as_of": snapshot["as_of"], "quality_status": quality_status,
        "blocking_reasons": blockers,
        "validation_status": snapshot["validation_status"],
        "prediction": snapshot["prediction"],
        "stress": stress_score, "confirmation": conf_score,
        "quadrant": (core.get("quadrant") or {}).get("key"),
        "audit_id": snapshot.get("audit_id"),
    })
    if persist:
        store.save_snapshot(as_of_day, snapshot)
    return snapshot
