"""联合风险事件级标签、匹配与 OOS 准入统计。

本模块不读取新闻/AI，也不拟合生产阈值。调用方必须只把训练折传给拟合器，
再用返回的冻结 artifact 评估验证折；8 月 19–20 日样本应单独做 forensic regression。
"""
from __future__ import annotations

import math
import random
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Optional

from models.market_risk import CalibrationArtifact, GroundTruthEpisode, MarketRiskMatch


@dataclass(frozen=True)
class PricePoint:
    ts: int
    price: float


@dataclass(frozen=True)
class IncidentSignal:
    incident_id: str
    direction: str
    decision_time: int
    stage: str


@dataclass(frozen=True)
class WalkForwardFold:
    train_indices: tuple[int, ...]
    validation_indices: tuple[int, ...]
    validation_start: int
    validation_end: int
    embargo_sec: int


@dataclass(frozen=True)
class BaselineFeatureRow:
    """同一 decision_time 上的固定基线输入；不得含未来标签字段。"""

    decision_time: int
    price_return_pct: float
    volume_z: float
    spot_cvd_z: float
    oi_change_1h_pct: float
    liquidation_1h_usd: float
    liquidation_direction: str
    full_engine_score: float


@dataclass(frozen=True)
class BaselineScore:
    decision_time: int
    baseline: str
    direction: str
    score: float


_CALIBRATION_FEATURES: dict[str, tuple[str, float, bool]] = {
    # artifact key: (training-row feature, quantile, absolute value)
    "spot_taker_imbalance_early": ("spot_taker_imbalance", 0.90, True),
    "spot_taker_imbalance_extreme": ("spot_taker_imbalance", 0.975, True),
    "spot_min_quote_usd": ("spot_quote_usd", 0.25, False),
    "oi_change_1h_early_pct": ("oi_change_1h_pct", 0.90, True),
    "oi_change_1h_extreme_pct": ("oi_change_1h_pct", 0.975, True),
    "funding_abs_extreme": ("funding_rate", 0.975, True),
    "liquidation_1h_early_usd": ("liquidation_1h_usd", 0.90, False),
    "liquidation_1h_extreme_usd": ("liquidation_1h_usd", 0.975, False),
    "liquidation_density_early_usd": ("liquidation_density_usd", 0.90, False),
    "liquidation_density_extreme_usd": ("liquidation_density_usd", 0.975, False),
    "wall_attack_early": ("wall_attack", 0.90, False),
    "wall_attack_extreme": ("wall_attack", 0.975, False),
    "price_move_5m_feedback_pct": ("price_move_5m_pct", 0.95, True),
    "warning_confidence": ("full_engine_confidence", 0.75, False),
    "critical_confidence": ("full_engine_confidence", 0.90, False),
}

_BASELINE_FEATURES: dict[str, tuple[str, float, bool]] = {
    "price_breakout_abs_pct": ("price_move_5m_pct", 0.95, True),
    "volume_z": ("volume_z", 0.95, False),
    "spot_cvd_abs_z": ("spot_cvd_z", 0.975, True),
    "oi_abs_change_1h_pct": ("oi_change_1h_pct", 0.975, True),
    "liquidation_1h_usd": ("liquidation_1h_usd", 0.975, False),
}

_STATE_TIMERS = {
    "critical_to_warning_sec": 300.0,
    "warning_to_watch_sec": 300.0,
    "quiet_to_cooldown_sec": 600.0,
    "cooldown_to_resolved_sec": 1200.0,
    "resolved_to_normal_sec": 300.0,
    "episode_gap_sec": 900.0,
    "root_direction_dominance_ratio": 2.0,
}


def _quantile(values: list[float], quantile: float) -> float:
    ordered = sorted(float(value) for value in values if math.isfinite(float(value)))
    if not ordered:
        raise ValueError("cannot fit a quantile from empty/non-finite values")
    if len(ordered) == 1:
        return ordered[0]
    position = max(0.0, min(1.0, quantile)) * (len(ordered) - 1)
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def fit_training_calibration(
    rows: Iterable[Mapping[str, Any]], *,
    forbidden_intervals: Iterable[tuple[int, int]] = (),
    min_samples: int = 50,
    created_at: Optional[str] = None,
) -> CalibrationArtifact:
    """只从训练折拟合并冻结 artifact；任何 forbidden/PIT 行都直接拒绝。

    调用方应把 purged fold 的 ``train_indices`` 对应行传入本函数。函数不会
    接受缺失 decision_time、落入法证留出区间或样本不足的数据，从机制上避免
    8 月 19–20 日样本被人工调参污染。
    """
    training = [dict(row) for row in rows]
    if len(training) < max(1, min_samples):
        raise ValueError(f"training sample too small: {len(training)} < {min_samples}")
    forbidden = [(int(start), int(end)) for start, end in forbidden_intervals]
    decision_times: list[int] = []
    for row in training:
        decision_time = int(row.get("decision_time") or 0)
        if decision_time <= 0:
            raise ValueError("every training row requires a positive decision_time")
        if any(start <= decision_time < end for start, end in forbidden):
            raise ValueError(f"forbidden forensic sample in training: {decision_time}")
        for key, value in row.items():
            if (key == "known_at" or key.endswith("_known_at")) and value is not None:
                if int(value) > decision_time:
                    raise ValueError(
                        f"point-in-time violation: {key}={value} > decision_time={decision_time}"
                    )
        decision_times.append(decision_time)

    def fit(spec: Mapping[str, tuple[str, float, bool]]) -> dict[str, float]:
        result: dict[str, float] = {}
        for output_name, (feature_name, quantile, absolute) in spec.items():
            values: list[float] = []
            for row in training:
                if feature_name not in row or row[feature_name] is None:
                    raise ValueError(f"missing training feature: {feature_name}")
                value = float(row[feature_name])
                values.append(abs(value) if absolute else value)
            fitted = _quantile(values, quantile)
            if not math.isfinite(fitted) or fitted <= 0:
                raise ValueError(f"non-positive fitted threshold: {output_name}={fitted}")
            result[output_name] = float(fitted)
        return result

    thresholds = fit(_CALIBRATION_FEATURES)
    # 单调约束是模型契约，不是人工按某一行情调参。
    for early, extreme in (
        ("spot_taker_imbalance_early", "spot_taker_imbalance_extreme"),
        ("oi_change_1h_early_pct", "oi_change_1h_extreme_pct"),
        ("liquidation_1h_early_usd", "liquidation_1h_extreme_usd"),
        ("liquidation_density_early_usd", "liquidation_density_extreme_usd"),
        ("wall_attack_early", "wall_attack_extreme"),
        ("warning_confidence", "critical_confidence"),
    ):
        if thresholds[extreme] < thresholds[early]:
            raise ValueError(f"calibration monotonicity violated: {early}/{extreme}")
    thresholds.update(_STATE_TIMERS)
    baseline_thresholds = fit(_BASELINE_FEATURES)
    identity = {
        "training_start": min(decision_times),
        "training_end": max(decision_times),
        "sample_count": len(training),
        "thresholds": thresholds,
        "baseline_thresholds": baseline_thresholds,
    }
    digest = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:12]
    return CalibrationArtifact(
        calibration_version=f"market-risk-cal-{digest}",
        created_at=created_at or datetime.now(timezone.utc).isoformat(),
        status="frozen_training",
        admitted_for_production=False,
        training_window={
            "start": min(decision_times), "end": max(decision_times),
            "sample_count": len(training), "fit_scope": "purged_training_fold_only",
        },
        notes="Frozen from training-fold quantiles; requires OOS admission gates before production.",
        thresholds=thresholds,
        baseline_thresholds=baseline_thresholds,
    )


def score_fixed_baselines(
    row: BaselineFeatureRow, thresholds: Mapping[str, float],
) -> dict[str, BaselineScore]:
    """固定四基线打分；阈值必须来自同一训练折冻结 artifact。"""
    required = set(_BASELINE_FEATURES)
    missing = sorted(required - set(thresholds))
    if missing:
        raise ValueError(f"missing baseline thresholds: {missing}")

    def direction(value: float) -> str:
        return "up" if value > 0 else "down" if value < 0 else "unknown"

    price_score = min(
        abs(row.price_return_pct) / thresholds["price_breakout_abs_pct"],
        max(0.0, row.volume_z) / thresholds["volume_z"],
    )
    cvd_score = abs(row.spot_cvd_z) / thresholds["spot_cvd_abs_z"]
    oi_liq_score = min(
        abs(row.oi_change_1h_pct) / thresholds["oi_abs_change_1h_pct"],
        max(0.0, row.liquidation_1h_usd) / thresholds["liquidation_1h_usd"],
    )
    liq_direction = row.liquidation_direction
    if liq_direction not in {"up", "down"}:
        liq_direction = direction(row.oi_change_1h_pct)
    return {
        "price_breakout_volume": BaselineScore(
            row.decision_time, "price_breakout_volume",
            direction(row.price_return_pct), price_score,
        ),
        "spot_cvd_extreme": BaselineScore(
            row.decision_time, "spot_cvd_extreme",
            direction(row.spot_cvd_z), cvd_score,
        ),
        "oi_liquidation": BaselineScore(
            row.decision_time, "oi_liquidation", liq_direction, oi_liq_score,
        ),
        "full_engine": BaselineScore(
            row.decision_time, "full_engine", direction(row.price_return_pct),
            max(0.0, float(row.full_engine_score)),
        ),
    }


def select_at_equal_alert_burden(
    rows: Iterable[BaselineFeatureRow], thresholds: Mapping[str, float], *,
    alerts_per_baseline: int,
) -> dict[str, list[BaselineScore]]:
    """每个基线取同样数量且 score>=1 的告警；不足即拒绝比较。"""
    if alerts_per_baseline <= 0:
        raise ValueError("alerts_per_baseline must be positive")
    buckets: dict[str, list[BaselineScore]] = {}
    for row in rows:
        for name, score in score_fixed_baselines(row, thresholds).items():
            if score.score >= 1.0:
                buckets.setdefault(name, []).append(score)
    selected: dict[str, list[BaselineScore]] = {}
    for name in ("price_breakout_volume", "spot_cvd_extreme", "oi_liquidation", "full_engine"):
        candidates = sorted(
            buckets.get(name, []), key=lambda item: (-item.score, item.decision_time),
        )
        if len(candidates) < alerts_per_baseline:
            raise ValueError(
                f"insufficient qualifying alerts for equal burden: {name} "
                f"{len(candidates)} < {alerts_per_baseline}"
            )
        selected[name] = candidates[:alerts_per_baseline]
    return selected


def stratified_binary_report(
    rows: Iterable[Mapping[str, Any]], *, outcome_key: str,
    dimensions: tuple[str, ...] = (
        "volatility_regime", "market_regime", "session", "data_quality",
    ),
) -> dict[str, dict[str, dict[str, float]]]:
    """按波动、趋势/震荡、时区和质量层报告样本数与命中率。"""
    materialized = [dict(row) for row in rows]
    report: dict[str, dict[str, dict[str, float]]] = {}
    for dimension in dimensions:
        groups: dict[str, list[int]] = {}
        for row in materialized:
            label = str(row.get(dimension) or "unknown")
            groups.setdefault(label, []).append(1 if bool(row.get(outcome_key)) else 0)
        report[dimension] = {
            label: {"count": float(len(values)), "rate": sum(values) / len(values)}
            for label, values in sorted(groups.items()) if values
        }
    return report


def _crossing_time(points: list[PricePoint], start_idx: int, end_idx: int, target: float,
                   direction: str) -> int:
    for point in points[start_idx:end_idx + 1]:
        if (direction == "up" and point.price >= target) or (
            direction == "down" and point.price <= target
        ):
            return point.ts
    return points[end_idx].ts


def label_market_episodes(
    points: Iterable[PricePoint], *, coin: str,
    threshold_pct: float = 5.0, forward_window_sec: int = 4 * 3600,
    retrace_fraction: float = 0.5, quiet_split_sec: int = 120 * 60,
) -> list[GroundTruthEpisode]:
    """把重叠阳性分钟合并成 market event/episode。

    onset 是从事件起价首次完成最终阈值 25% 的时刻；peak 是 MFE 极值时刻，
    只用于描述，任何领先时间都不得相对 peak 计算。
    """
    series = sorted(
        {int(point.ts): PricePoint(int(point.ts), float(point.price)) for point in points
         if point.price > 0}.values(),
        key=lambda point: point.ts,
    )
    if len(series) < 2 or threshold_pct <= 0 or forward_window_sec <= 0:
        return []
    threshold = threshold_pct / 100.0
    candidates: list[dict] = []
    right = 1
    for idx, start in enumerate(series[:-1]):
        right = max(right, idx + 1)
        while right + 1 < len(series) and series[right + 1].ts - start.ts <= forward_window_sec:
            right += 1
        window = series[idx + 1:right + 1]
        if not window:
            continue
        max_point = max(window, key=lambda point: point.price)
        min_point = min(window, key=lambda point: point.price)
        up_move = max_point.price / start.price - 1.0
        down_move = 1.0 - min_point.price / start.price
        if max(up_move, down_move) < threshold:
            continue
        direction = "up" if up_move >= down_move else "down"
        extreme = max_point if direction == "up" else min_point
        candidates.append({
            "start_idx": idx,
            "end_idx": series.index(extreme, idx + 1, right + 1),
            "direction": direction,
            "last_qualifying": start.ts,
        })

    groups: list[dict] = []
    for candidate in candidates:
        if not groups:
            groups.append(dict(candidate))
            continue
        current = groups[-1]
        cand_start = series[candidate["start_idx"]]
        group_start = series[current["start_idx"]]
        through = series[current["start_idx"]:candidate["start_idx"] + 1]
        if current["direction"] == "up":
            peak = max(point.price for point in through)
            retraced = cand_start.price <= peak - retrace_fraction * (peak - group_start.price)
        else:
            trough = min(point.price for point in through)
            retraced = cand_start.price >= trough + retrace_fraction * (group_start.price - trough)
        overlaps = cand_start.ts <= series[current["end_idx"]].ts
        within_quiet = cand_start.ts - current["last_qualifying"] <= quiet_split_sec
        if candidate["direction"] == current["direction"] and (overlaps or within_quiet) and not retraced:
            current["end_idx"] = max(current["end_idx"], candidate["end_idx"])
            current["last_qualifying"] = cand_start.ts
        else:
            groups.append(dict(candidate))

    episodes: list[GroundTruthEpisode] = []
    for number, group in enumerate(groups, 1):
        start_idx, end_idx = group["start_idx"], group["end_idx"]
        start = series[start_idx]
        direction = group["direction"]
        qualifying_segment = series[start_idx:end_idx + 1]
        provisional_extreme = (
            max(qualifying_segment, key=lambda point: point.price)
            if direction == "up" else min(qualifying_segment, key=lambda point: point.price)
        )
        extreme_idx = series.index(provisional_extreme, start_idx, end_idx + 1)
        # episode 在完成 50% 回撤或最后阳性分钟后连续 120min 无 qualifying 时结束。
        final_end_idx = end_idx
        split_price = (
            provisional_extreme.price - retrace_fraction * (provisional_extreme.price - start.price)
            if direction == "up"
            else provisional_extreme.price + retrace_fraction * (start.price - provisional_extreme.price)
        )
        for scan_idx in range(extreme_idx + 1, len(series)):
            point = series[scan_idx]
            retraced = (
                point.price <= split_price if direction == "up" else point.price >= split_price
            )
            quiet = point.ts - group["last_qualifying"] >= quiet_split_sec
            final_end_idx = scan_idx
            if retraced or quiet:
                break
        end_idx = max(end_idx, final_end_idx)
        segment = series[start_idx:end_idx + 1]
        if direction == "up":
            extreme = max(segment, key=lambda point: point.price)
            adverse = min(segment, key=lambda point: point.price)
            mfe = (extreme.price / start.price - 1.0) * 100
            mae = (adverse.price / start.price - 1.0) * 100
            onset_target = start.price * (1 + threshold * 0.25)
            threshold_target = start.price * (1 + threshold)
        else:
            extreme = min(segment, key=lambda point: point.price)
            adverse = max(segment, key=lambda point: point.price)
            mfe = (1.0 - extreme.price / start.price) * 100
            mae = (1.0 - adverse.price / start.price) * 100
            onset_target = start.price * (1 - threshold * 0.25)
            threshold_target = start.price * (1 - threshold)
        onset = _crossing_time(series, start_idx, end_idx, onset_target, direction)
        threshold_time = _crossing_time(series, start_idx, end_idx, threshold_target, direction)
        event_id = f"{coin.upper()}_{direction}_{start.ts}_{number}"
        episodes.append(GroundTruthEpisode(
            event_id=event_id, coin=coin.upper(), direction=direction,
            event_start=start.ts, onset=onset, threshold_time=threshold_time,
            peak=extreme.ts, end=series[end_idx].ts,
            mfe_pct=round(mfe, 6), mae_pct=round(mae, 6),
            duration_sec=max(0, series[end_idx].ts - start.ts),
        ))
    return episodes


def purged_walk_forward(
    episodes: list[GroundTruthEpisode], *, validation_windows: list[tuple[int, int]],
    embargo_sec: int,
) -> list[WalkForwardFold]:
    """只用验证窗之前的数据训练，并 purge 标签区间重叠 + embargo。"""
    folds: list[WalkForwardFold] = []
    for valid_start, valid_end in validation_windows:
        validation = tuple(
            idx for idx, event in enumerate(episodes)
            if event.event_start <= valid_end and event.end >= valid_start
        )
        forbidden_start = valid_start - max(0, embargo_sec)
        forbidden_end = valid_end + max(0, embargo_sec)
        train = tuple(
            idx for idx, event in enumerate(episodes)
            if event.end < valid_start
            and not (event.event_start <= forbidden_end and event.end >= forbidden_start)
        )
        folds.append(WalkForwardFold(
            train_indices=train, validation_indices=validation,
            validation_start=valid_start, validation_end=valid_end,
            embargo_sec=max(0, embargo_sec),
        ))
    return folds


def match_incidents_once(
    signals: Iterable[IncidentSignal], episodes: Iterable[GroundTruthEpisode], *,
    max_lead_sec: int = 6 * 3600,
) -> list[MarketRiskMatch]:
    """一个 incident 对同一 GT event 最多命中一次；只认 warning/critical。"""
    eligible = sorted(
        (signal for signal in signals if signal.stage in {"warning", "critical"}),
        key=lambda signal: signal.decision_time,
    )
    results: list[MarketRiskMatch] = []
    used_pairs: set[tuple[str, str]] = set()
    for event in sorted(episodes, key=lambda item: item.event_start):
        for signal in eligible:
            pair = (signal.incident_id, event.event_id)
            if pair in used_pairs or signal.direction != event.direction:
                continue
            if event.event_start - max_lead_sec <= signal.decision_time <= event.threshold_time:
                used_pairs.add(pair)
                results.append(MarketRiskMatch(
                    incident_id=signal.incident_id, event_id=event.event_id,
                    lead_to_onset_sec=event.onset - signal.decision_time,
                    lead_to_threshold_sec=event.threshold_time - signal.decision_time,
                ))
                break
    return results


def wilson_interval(successes: int, total: int, z: float = 1.959963984540054) -> tuple[float, float]:
    if total <= 0:
        return 0.0, 1.0
    p = successes / total
    denominator = 1 + z * z / total
    center = (p + z * z / (2 * total)) / denominator
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * total)) / total) / denominator
    return max(0.0, center - margin), min(1.0, center + margin)


def paired_bootstrap_delta_ci(
    full_hits: list[int], baseline_hits: list[int], *, samples: int = 10_000,
    seed: int = 17,
) -> tuple[float, float, float]:
    """相同 episode/alert burden 下 Full Engine - baseline 的配对命中率区间。"""
    if len(full_hits) != len(baseline_hits) or not full_hits:
        raise ValueError("paired outcomes must be non-empty and equal length")
    deltas = [int(full) - int(base) for full, base in zip(full_hits, baseline_hits)]
    rng = random.Random(seed)
    distribution = sorted(
        sum(deltas[rng.randrange(len(deltas))] for _ in deltas) / len(deltas)
        for _ in range(max(100, samples))
    )
    lo = distribution[int(0.025 * (len(distribution) - 1))]
    hi = distribution[int(0.975 * (len(distribution) - 1))]
    return sum(deltas) / len(deltas), lo, hi


def admission_gates(
    *, warning_tp: int, warning_total: int, critical_tp: int, critical_total: int,
    recall_hits: int, qualifying_events: int, false_warning_per_day: float,
    false_critical_per_14d: float, core_coverage: float,
    incremental_delta_ci: Optional[tuple[float, float, float]],
) -> dict[str, object]:
    warning_lb = wilson_interval(warning_tp, warning_total)[0]
    critical_lb = wilson_interval(critical_tp, critical_total)[0]
    recall_lb = wilson_interval(recall_hits, qualifying_events)[0]
    checks = {
        "warning": warning_total >= 50 and warning_tp / max(1, warning_total) >= 0.55 and warning_lb >= 0.45,
        "critical": critical_total >= 30 and critical_tp / max(1, critical_total) >= 0.70 and critical_lb >= 0.55,
        "recall": qualifying_events >= 30 and recall_hits / max(1, qualifying_events) >= 0.60 and recall_lb >= 0.45,
        "false_warning": false_warning_per_day <= 1.0,
        "false_critical": false_critical_per_14d <= 1.0,
        "coverage": core_coverage >= 0.90,
        "incremental": bool(incremental_delta_ci and incremental_delta_ci[1] > 0),
    }
    return {
        "admitted": all(checks.values()), "checks": checks,
        "wilson_lower": {
            "warning_precision": warning_lb,
            "critical_precision": critical_lb,
            "qualifying_event_recall": recall_lb,
        },
    }
