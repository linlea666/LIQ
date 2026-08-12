"""Bottom Model 快照组装：六因子 → 双层评分 → 四象限 → 类比/反证/质量标注。

historical analog（历史类比）实现说明：
因子引擎是纯函数（序列 in → 分数 out），把序列截断到历史底部日期再跑
同一引擎即得"当年同期因子向量"，与当前向量按共同有效因子求平均绝对差。
早年缺失的指标（OI/ETF/清算等）自动弃权，类比结果附 common_factors
数量供外部 AI 判断置信度——共同因子少的类比参考价值低。
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Optional

from processors.bottom_model.factors import (
    Rows,
    build_counter_evidence,
    clamp,
    classify_quadrant,
    compute_confirmation,
    compute_factors,
    compute_fake_bottom_filter,
    compute_seller_exhaustion,
    compute_stress,
)
from processors.bottom_model.correlation import compute_correlation_audit
from processors.bottom_model.metrics import build_registry, sanitize_series
from storage.bottom_model_store import BottomModelStore

logger = logging.getLogger(__name__)

ALGORITHM_VERSION = "bottom-v3"

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
    return {
        metric: [(d, v) for d, v in rows if d <= day]
        for metric, rows in data.items()
    }


def compute_core(data: dict[str, Rows],
                 as_of: Optional[str] = None) -> dict[str, Any]:
    """纯计算核心：因子 → Stress / Confirmation（含假底惩罚）→ 象限。

    as_of 传给因子/确认层仅用于证据质量的新鲜度乘子；历史类比传入各自的
    截断日，保证当年评估只惩罚"当年就已滞后"的数据。
    """
    factors = compute_factors(data, as_of)
    stress = compute_stress(factors)
    confirmation_raw = compute_confirmation(data, as_of)
    fake_filter = compute_fake_bottom_filter(data)
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
        "seller_exhaustion": compute_seller_exhaustion(data),
        "counter_evidence": build_counter_evidence(factors, fake_filter),
    }


def _factor_vector(core: dict[str, Any]) -> dict[str, float]:
    return {
        f["key"]: f["score"] for f in core["factors"] if f["score"] is not None
    }


def compute_analogs(data: dict[str, Rows], current_core: dict[str, Any]) -> list[dict[str, Any]]:
    """当前因子向量 vs 历史底部同期向量的相似度（0-100）。"""
    current_vec = _factor_vector(current_core)
    analogs: list[dict[str, Any]] = []
    for day, label in HISTORICAL_BOTTOMS:
        try:
            past_core = compute_core(truncate_asof(data, day), day)
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
    stale: list[dict[str, Any]] = []
    metrics_meta: dict[str, Any] = {}
    for metric, rows in data.items():
        if not rows:
            missing.append(metric)
            continue
        first_day, last_day = rows[0][0], rows[-1][0]
        behind = (as_of - datetime.strptime(last_day, "%Y-%m-%d")
                  .replace(tzinfo=timezone.utc)).days
        tolerance = tolerance_override.get(
            metric,
            _STALENESS_TOLERANCE.get(cadence_by_metric.get(metric, "daily"), 3),
        )
        metrics_meta[metric] = {
            "first_day": first_day, "last_day": last_day,
            "days": len(rows), "behind_days": behind,
        }
        if behind > tolerance:
            stale.append({"metric": metric, "last_day": last_day, "behind_days": behind})
    # 只报告注册表内 spec 的失败——已停采指标的失败旧行不应永久误报
    failed_fetches = {
        key: item["last_error"]
        for key, item in store.fetch_log().items()
        if key in active_spec_keys and not item["last_ok"]
    }
    return {
        "ok": not missing and not stale,
        "missing": missing,
        "stale": stale,
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
                   as_of_day: Optional[str] = None) -> dict[str, Any]:
    """组装并落库当日快照。as_of_day 默认 = 数据允许的最新日（价格序列末日）。"""
    data = load_all_series(store)
    if as_of_day is None:
        price_rows = data.get("btc_price_onchain") or data.get("btc_close_1d") or []
        as_of_day = price_rows[-1][0] if price_rows \
            else time.strftime("%Y-%m-%d", time.gmtime())
    else:
        data = truncate_asof(data, as_of_day)

    core = compute_core(data, as_of_day)
    stress_score = core["stress"]["score"] if core["stress"] else None
    conf_score = core["confirmation"]["score"]
    stress_eq = (core["stress"] or {}).get("evidence_quality")
    conf_eq = core["confirmation"].get("evidence_quality")
    overall_eq = [eq for eq in (stress_eq, conf_eq) if eq is not None]
    snapshot = {
        "day": as_of_day,
        "ts": int(time.time()),
        "algorithm_version": ALGORITHM_VERSION,
        "price_context": _price_context(data),
        **core,
        "evidence_quality": {
            "stress": stress_eq,
            "confirmation": conf_eq,
            "overall": round(sum(overall_eq) / len(overall_eq), 1) if overall_eq else None,
        },
        "correlation_audit": compute_correlation_audit(data),
        "analogs": compute_analogs(data, core),
        "delta": compute_delta(store, stress_score, conf_score),
        "data_quality": compute_data_quality(store, data, as_of_day),
    }
    store.save_snapshot(as_of_day, snapshot)
    return snapshot
