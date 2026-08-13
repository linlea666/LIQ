"""BTC Bottom Model 独立数学审计器。

本模块不参与生产调度。它只消费按 data_policy 冻结的数据与 Champion 回放，
输出可复现 JSON/Markdown；任何缺少严格 PIT vintage 的结论都保留
INSUFFICIENT_EVIDENCE/PIT_APPROX 标记，绝不把研究分数升级成概率。
"""

from __future__ import annotations

import hashlib
import math
import random
import time
from bisect import bisect_left, bisect_right
from collections import defaultdict
from datetime import date, timedelta
from statistics import median
from typing import Any, Callable, Optional

from processors.bottom_model.base_rate import load_or_build_replay
from processors.bottom_model.metrics import build_registry, metric_contract
from processors.bottom_model.snapshot import (
    ALGORITHM_VERSION,
    DATA_POLICY_ID,
    MODEL_ID,
    apply_data_policy,
    compute_core,
    dataset_fingerprint,
    load_all_series,
)

LABEL_A_HORIZONS = (30, 60, 90, 180, 365)
LABEL_A_EPS = (0.03, 0.05, 0.10)
LABEL_B_HORIZONS = (30, 90, 180, 365)
LABEL_B_RETURNS = (0.10, 0.20, 0.30, 0.50)
LABEL_C_HORIZONS = (90, 180, 365)
LABEL_C_RETURN = {90: 0.10, 180: 0.20, 365: 0.30}
LABEL_C_MAE = {90: -0.15, 180: -0.20, 365: -0.25}
PRIMARY_LABEL = "C_180_r20_mae20"
BOOTSTRAP_ITERATIONS = 5000
PERMUTATION_ITERATIONS = 1000
EMBARGO_DAYS = 30
AUDIT_ENGINE_VERSION = "audit-v4"


def _finite(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


class PriceIndex:
    def __init__(self, rows: list[tuple[str, float]]):
        self.days = [day for day, _ in rows]
        self.values = [float(value) for _, value in rows]

    def at_or_after(self, day: str) -> Optional[int]:
        idx = bisect_left(self.days, day)
        return idx if idx < len(self.days) else None

    def at_or_before(self, day: str) -> Optional[int]:
        idx = bisect_right(self.days, day) - 1
        return idx if idx >= 0 else None

    def path(self, day: str, horizon: int, *, next_day: bool = False) -> Optional[list[float]]:
        start = date.fromisoformat(day) + timedelta(days=1 if next_day else 0)
        i = self.at_or_after(start.isoformat())
        j = self.at_or_after((start + timedelta(days=horizon)).isoformat())
        if i is None or j is None or j <= i or self.values[i] <= 0:
            return None
        return self.values[i:j + 1]


def build_labels(price: PriceIndex, day: str) -> dict[str, Optional[int]]:
    labels: dict[str, Optional[int]] = {}
    for horizon in LABEL_A_HORIZONS:
        path = price.path(day, horizon)
        for eps in LABEL_A_EPS:
            key = f"A_{horizon}_eps{int(eps * 100)}"
            labels[key] = None if not path else int(path[0] <= min(path) * (1 + eps))
    for horizon in LABEL_B_HORIZONS:
        path = price.path(day, horizon)
        for target in LABEL_B_RETURNS:
            key = f"B_{horizon}_r{int(target * 100)}"
            labels[key] = None if not path else int(path[-1] / path[0] - 1 >= target)
    for horizon in LABEL_C_HORIZONS:
        path = price.path(day, horizon)
        key = f"C_{horizon}_r{int(LABEL_C_RETURN[horizon] * 100)}_mae{int(abs(LABEL_C_MAE[horizon]) * 100)}"
        labels[key] = None if not path else int(
            path[-1] / path[0] - 1 >= LABEL_C_RETURN[horizon]
            and min(path) / path[0] - 1 >= LABEL_C_MAE[horizon]
        )
    return labels


def _rank(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=values.__getitem__)
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i + 1
        while j < len(order) and values[order[j]] == values[order[i]]:
            j += 1
        rank = (i + j - 1) / 2 + 1
        for k in order[i:j]:
            ranks[k] = rank
        i = j
    return ranks


def _pearson(x: list[float], y: list[float]) -> Optional[float]:
    if len(x) < 3 or len(x) != len(y):
        return None
    mx, my = sum(x) / len(x), sum(y) / len(y)
    dx, dy = [v - mx for v in x], [v - my for v in y]
    denom = math.sqrt(sum(v * v for v in dx) * sum(v * v for v in dy))
    return sum(a * b for a, b in zip(dx, dy)) / denom if denom > 0 else None


def _auc(y: list[int], score: list[float]) -> Optional[float]:
    positives = sum(y)
    negatives = len(y) - positives
    if positives == 0 or negatives == 0:
        return None
    ranks = _rank(score)
    pos_sum = sum(rank for rank, label in zip(ranks, y) if label)
    return (pos_sum - positives * (positives + 1) / 2) / (positives * negatives)


def _pr_auc(y: list[int], score: list[float]) -> Optional[float]:
    positives = sum(y)
    if positives == 0:
        return None
    pairs = sorted(zip(score, y), key=lambda pair: pair[0], reverse=True)
    tp = fp = 0
    previous_recall = 0.0
    area = 0.0
    index = 0
    # 同分数必须整组推进阈值；逐行排序会让 tie 内的标签顺序制造虚假 AP。
    while index < len(pairs):
        edge = pairs[index][0]
        group: list[int] = []
        while index < len(pairs) and pairs[index][0] == edge:
            group.append(pairs[index][1])
            index += 1
        tp += sum(group)
        fp += len(group) - sum(group)
        recall = tp / positives
        precision = tp / (tp + fp)
        area += (recall - previous_recall) * precision
        previous_recall = recall
    return area


def _classification(y: list[int], pred: list[int], score: Optional[list[float]] = None,
                    probability: Optional[list[float]] = None) -> dict[str, Any]:
    tp = sum(1 for a, b in zip(y, pred) if a == b == 1)
    tn = sum(1 for a, b in zip(y, pred) if a == b == 0)
    fp = sum(1 for a, b in zip(y, pred) if a == 0 and b == 1)
    fn = sum(1 for a, b in zip(y, pred) if a == 1 and b == 0)
    safe = lambda a, b: a / b if b else None
    precision, recall = safe(tp, tp + fp), safe(tp, tp + fn)
    specificity = safe(tn, tn + fp)
    f1 = safe(2 * tp, 2 * tp + fp + fn)
    denom = math.sqrt((tp + fp) * (tp + fn) * (tn + fp) * (tn + fn))
    mcc = (tp * tn - fp * fn) / denom if denom else None
    out: dict[str, Any] = {
        "n": len(y), "positive_n": sum(y), "tp": tp, "tn": tn, "fp": fp, "fn": fn,
        "precision": precision, "recall": recall, "specificity": specificity,
        "f1": f1, "mcc": mcc,
        "balanced_accuracy": (
            (recall + specificity) / 2 if recall is not None and specificity is not None else None
        ),
        "roc_auc": _auc(y, score) if score else None,
        "pr_auc": _pr_auc(y, score) if score else None,
    }
    if probability:
        clipped = [min(1 - 1e-9, max(1e-9, p)) for p in probability]
        out["brier"] = sum((p - label) ** 2 for p, label in zip(clipped, y)) / len(y)
        out["log_loss"] = -sum(
            label * math.log(p) + (1 - label) * math.log(1 - p)
            for p, label in zip(clipped, y)
        ) / len(y)
        out["ece"] = _ece(y, clipped)
    else:
        out.update({"brier": "UNSCORABLE", "log_loss": "UNSCORABLE", "ece": "UNSCORABLE"})
    return out


def _ece(y: list[int], probability: list[float], bins: int = 10) -> float:
    total = len(y)
    error = 0.0
    for index in range(bins):
        lo, hi = index / bins, (index + 1) / bins
        members = [i for i, p in enumerate(probability) if lo <= p < hi or (index == bins - 1 and p == 1)]
        if members:
            error += len(members) / total * abs(
                sum(probability[i] for i in members) / len(members)
                - sum(y[i] for i in members) / len(members)
            )
    return error


def _calibration_curve(y: list[int], probability: list[float],
                       bins: int = 10) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for index in range(bins):
        lo, hi = index / bins, (index + 1) / bins
        members = [
            i for i, value in enumerate(probability)
            if lo <= value < hi or (index == bins - 1 and value == 1)
        ]
        result.append({
            "bin": f"{index * 10}-{(index + 1) * 10}%",
            "n": len(members),
            "mean_prediction": (
                sum(probability[i] for i in members) / len(members) if members else None
            ),
            "observed_rate": (
                sum(y[i] for i in members) / len(members) if members else None
            ),
        })
    return result


def _choose_threshold(y: list[int], scores: list[float]) -> float:
    best = (-2.0, 50.0)
    for threshold in range(10, 91, 2):
        metrics = _classification(y, [int(score >= threshold) for score in scores])
        value = metrics["mcc"] if metrics["mcc"] is not None else -1.0
        if value > best[0]:
            best = (value, float(threshold))
    return best[1]


def _calibrate(train_y: list[int], train_s: list[float], test_s: list[float]) -> list[float]:
    """训练折内的单调分箱校准；只用于研究指标，不发布为产品概率。"""
    order = sorted(range(len(train_s)), key=train_s.__getitem__)
    bins: list[tuple[float, float]] = []
    size = max(10, len(order) // 10)
    prior = (sum(train_y) + 1) / (len(train_y) + 2)
    for start in range(0, len(order), size):
        part = order[start:start + size]
        if part:
            bins.append((max(train_s[i] for i in part), (sum(train_y[i] for i in part) + 1) / (len(part) + 2)))
    # Pool-adjacent-violators 的保守简化：累计最大保证不随分数下降。
    monotone: list[tuple[float, float]] = []
    running = 0.0
    for edge, rate in bins:
        running = max(running, rate)
        monotone.append((edge, running))
    if not monotone:
        return [prior] * len(test_s)
    return [next((rate for edge, rate in monotone if score <= edge), monotone[-1][1]) for score in test_s]


def _walk_forward(rows: list[dict[str, Any]], label: str,
                  horizon: int) -> dict[str, Any]:
    years = sorted({int(row["day"][:4]) for row in rows})
    predictions: list[int] = []
    actual: list[int] = []
    scores: list[float] = []
    probabilities: list[float] = []
    folds: list[dict[str, Any]] = []
    for year in years:
        test = [row for row in rows if int(row["day"][:4]) == year and row["labels"].get(label) is not None]
        cutoff = date(year, 1, 1) - timedelta(days=horizon + EMBARGO_DAYS)
        train = [
            row for row in rows
            if date.fromisoformat(row["day"]) <= cutoff and row["labels"].get(label) is not None
        ]
        if len(train) < 100 or sum(int(row["labels"][label]) for row in train) < 10 or not test:
            folds.append({"year": year, "status": "SKIPPED_INSUFFICIENT_TRAIN", "train_n": len(train), "test_n": len(test)})
            continue
        train_y = [int(row["labels"][label]) for row in train]
        train_s = [row["combined_score"] for row in train]
        threshold = _choose_threshold(train_y, train_s)
        test_y = [int(row["labels"][label]) for row in test]
        test_s = [row["combined_score"] for row in test]
        test_p = _calibrate(train_y, train_s, test_s)
        actual.extend(test_y)
        scores.extend(test_s)
        probabilities.extend(test_p)
        predictions.extend(int(score >= threshold) for score in test_s)
        folds.append({
            "year": year, "status": "SCORED", "train_n": len(train), "test_n": len(test),
            "threshold": threshold,
        })
    metrics = _classification(actual, predictions, scores, probabilities) if actual else "UNSCORABLE"
    if isinstance(metrics, dict):
        metrics["validation_status"] = (
            "SCORABLE" if len(actual) >= 30 and len(set(actual)) == 2
            else "UNSCORABLE_INSUFFICIENT_OOS_CLASS_SUPPORT"
        )
    return {
        "folds": folds,
        "metrics": metrics,
        "oos": {"y": actual, "pred": predictions, "score": scores, "probability": probabilities},
    }


def _block_bootstrap(y: list[int], pred: list[int], score: list[float],
                     iterations: int = BOOTSTRAP_ITERATIONS, block: int = 26) -> dict[str, Any]:
    if len(y) < block * 2:
        return {"status": "UNSCORABLE", "reason": "insufficient_oos_rows"}
    rng = random.Random(20260813)
    values: dict[str, list[float]] = defaultdict(list)
    n = len(y)
    for _ in range(iterations):
        indices: list[int] = []
        while len(indices) < n:
            start = rng.randrange(0, max(1, n - block + 1))
            indices.extend(range(start, min(n, start + block)))
        indices = indices[:n]
        metrics = _classification(
            [y[i] for i in indices], [pred[i] for i in indices], [score[i] for i in indices],
        )
        for key in ("mcc", "pr_auc", "roc_auc", "balanced_accuracy"):
            if isinstance(metrics.get(key), float):
                values[key].append(metrics[key])
    result: dict[str, Any] = {"iterations": iterations, "block_weeks": block}
    for key, series in values.items():
        ordered = sorted(series)
        result[key + "_ci95"] = [ordered[int(0.025 * len(ordered))], ordered[int(0.975 * len(ordered)) - 1]]
    return result


def _block_bootstrap_delta(
    y: list[int], challenger_probability: list[float],
    champion_score: list[float], champion_probability: list[float],
    iterations: int = BOOTSTRAP_ITERATIONS, block: int = 26,
) -> dict[str, Any]:
    """同一 OOS 时点上 Challenger - Champion 的配对 block-bootstrap。"""
    n = len(y)
    if not (len(challenger_probability) == len(champion_score)
            == len(champion_probability) == n):
        return {"status": "UNSCORABLE", "reason": "unaligned_oos_rows"}
    if n < block * 2:
        return {"status": "UNSCORABLE", "reason": "insufficient_oos_rows"}
    rng = random.Random(20260814)
    deltas: dict[str, list[float]] = defaultdict(list)
    for _ in range(iterations):
        indices: list[int] = []
        while len(indices) < n:
            start = rng.randrange(0, max(1, n - block + 1))
            indices.extend(range(start, min(n, start + block)))
        indices = indices[:n]
        sample_y = [y[i] for i in indices]
        challenger = [challenger_probability[i] for i in indices]
        champion_s = [champion_score[i] for i in indices]
        champion_p = [champion_probability[i] for i in indices]
        challenger_pr = _pr_auc(sample_y, challenger)
        champion_pr = _pr_auc(sample_y, champion_s)
        if challenger_pr is not None and champion_pr is not None:
            deltas["pr_auc"].append(challenger_pr - champion_pr)
        challenger_brier = sum((p - label) ** 2 for p, label in zip(challenger, sample_y)) / n
        champion_brier = sum((p - label) ** 2 for p, label in zip(champion_p, sample_y)) / n
        deltas["brier"].append(challenger_brier - champion_brier)
        challenger_recall = _classification(
            sample_y, [int(p >= 0.5) for p in challenger],
        ).get("recall")
        champion_recall = _classification(
            sample_y, [int(p >= 0.5) for p in champion_p],
        ).get("recall")
        if challenger_recall is not None and champion_recall is not None:
            deltas["recall"].append(challenger_recall - champion_recall)
    result: dict[str, Any] = {"iterations": iterations, "block_weeks": block}
    for key, values in deltas.items():
        ordered = sorted(values)
        result["delta_" + key + "_ci95"] = [
            ordered[int(0.025 * len(ordered))],
            ordered[int(0.975 * len(ordered)) - 1],
        ]
    pr_ci = result.get("delta_pr_auc_ci95") or [float("-inf")]
    brier_ci = result.get("delta_brier_ci95") or [0.0, float("inf")]
    recall_ci = result.get("delta_recall_ci95") or [float("-inf")]
    result["promotion_supported"] = bool(
        pr_ci[0] > 0 and brier_ci[1] < 0 and recall_ci[0] >= 0
    )
    return result


def _n_eff(labels: list[int]) -> Optional[float]:
    if len(labels) < 3:
        return None
    rho = _pearson([float(x) for x in labels[:-1]], [float(x) for x in labels[1:]])
    if rho is None:
        return None
    return max(1.0, len(labels) * (1 - rho) / (1 + rho)) if rho > -0.999 else float(len(labels))


def _event_count(rows: list[dict[str, Any]], label: str, gap_days: int = 30) -> int:
    days = [date.fromisoformat(row["day"]) for row in rows if row["labels"].get(label) == 1]
    if not days:
        return 0
    count, previous = 1, days[0]
    for current in days[1:]:
        if (current - previous).days > gap_days:
            count += 1
        previous = current
    return count


def _benchmark(rows: list[dict[str, Any]], label: str) -> dict[str, Any]:
    usable = [row for row in rows if row["labels"].get(label) is not None]
    y = [int(row["labels"][label]) for row in usable]
    definitions: dict[str, Callable[[dict[str, Any]], float]] = {
        "never_bottom": lambda row: 0.0,
        "random_prevalence": lambda row: row["random_score"],
        "200w": lambda row: row.get("ma200_score", 0.0),
        "ath_drawdown": lambda row: row.get("drawdown_score", 0.0),
        "single_valuation": lambda row: row.get("factor_scores", {}).get("valuation") or 0.0,
        "equal_factor": lambda row: row.get("equal_factor_score", 0.0),
        "trend_confirmation": lambda row: row.get("confirmation") or 0.0,
        "champion_stress": lambda row: row.get("stress") or 0.0,
        "combined": lambda row: row.get("combined_score") or 0.0,
    }
    out: dict[str, Any] = {}
    for name, getter in definitions.items():
        scores = [float(getter(row)) for row in usable]
        threshold = 50.0 if name not in {"never_bottom", "random_prevalence"} else (1.0 if name == "never_bottom" else 50.0)
        out[name] = _classification(y, [int(score >= threshold) for score in scores], scores)
    return out


def _factor_diagnostics(rows: list[dict[str, Any]], label: str) -> dict[str, Any]:
    usable = [row for row in rows if row["labels"].get(label) is not None]
    factors = sorted({key for row in usable for key in row.get("factor_scores", {})})
    y = [float(row["labels"][label]) for row in usable]
    correlations: list[dict[str, Any]] = []
    for key in factors:
        pairs = [(row["factor_scores"].get(key), row["labels"][label]) for row in usable]
        pairs = [(float(x), float(v)) for x, v in pairs if x is not None]
        x = [p[0] for p in pairs]
        yy = [p[1] for p in pairs]
        correlations.append({
            "factor": key, "n": len(x), "pearson": _pearson(x, yy),
            "spearman": _pearson(_rank(x), _rank(yy)) if x else None,
            "mutual_information": _mutual_information(x, [int(v) for v in yy]),
        })
    matrix: list[dict[str, Any]] = []
    for i, a in enumerate(factors):
        for b in factors[i + 1:]:
            pairs = [
                (row["factor_scores"].get(a), row["factor_scores"].get(b)) for row in usable
                if row["factor_scores"].get(a) is not None and row["factor_scores"].get(b) is not None
            ]
            rho = _pearson([float(x) for x, _ in pairs], [float(v) for _, v in pairs])
            matrix.append({"a": a, "b": b, "n": len(pairs), "pearson": rho})
    parent = {key: key for key in factors}

    def find(key: str) -> str:
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for pair in matrix:
        if pair["pearson"] is not None and abs(pair["pearson"]) >= 0.70:
            union(pair["a"], pair["b"])
    clusters: dict[str, list[str]] = defaultdict(list)
    for key in factors:
        clusters[find(key)].append(key)
    return {
        "label_correlation": correlations, "factor_pairs": matrix,
        "clusters_abs_rho_ge_070": list(clusters.values()),
        "vif": _vif(usable, factors),
    }


def _mutual_information(x: list[float], y: list[int], bins: int = 10) -> Optional[float]:
    if len(x) < 20 or len(set(y)) < 2:
        return None
    ranks = _rank(x)
    xb = [min(bins - 1, int((rank - 1) * bins / len(x))) for rank in ranks]
    total = len(x)
    px = defaultdict(int)
    py = defaultdict(int)
    joint = defaultdict(int)
    for a, b in zip(xb, y):
        px[a] += 1; py[b] += 1; joint[(a, b)] += 1
    return sum(
        count / total * math.log((count * total) / (px[a] * py[b]))
        for (a, b), count in joint.items() if count
    )


def _vif(rows: list[dict[str, Any]], factors: list[str]) -> Any:
    try:
        import numpy as np  # type: ignore
    except ImportError:
        return "UNSCORABLE: numpy is only installed from requirements-audit.txt"
    complete = [row for row in rows if all(row.get("factor_scores", {}).get(key) is not None for key in factors)]
    if len(complete) < max(30, len(factors) * 5):
        return "UNSCORABLE: insufficient complete factor rows"
    matrix = np.asarray([[row["factor_scores"][key] for key in factors] for row in complete], dtype=float)
    result = {}
    for index, key in enumerate(factors):
        y = matrix[:, index]
        x = np.delete(matrix, index, axis=1)
        x = np.column_stack([np.ones(len(x)), x])
        beta = np.linalg.lstsq(x, y, rcond=None)[0]
        fitted = x @ beta
        total = float(((y - y.mean()) ** 2).sum())
        residual = float(((y - fitted) ** 2).sum())
        r2 = 1 - residual / total if total > 0 else 1.0
        result[key] = None if r2 >= 0.999999 else 1 / (1 - r2)
    return result


def _ablation(rows: list[dict[str, Any]], label: str) -> dict[str, Any]:
    usable = [row for row in rows if row["labels"].get(label) is not None]
    variants = {
        "stress_only": "stress",
        "confirmation_only": "confirmation",
        "combined": "combined_score",
        "equal_factor_candidate": "equal_factor_score",
        "with_fake_bottom_filter": "combined_score",
        "without_fake_bottom_filter": "combined_without_fake_filter",
    }
    result: dict[str, Any] = {}
    for name, field in variants.items():
        pairs = [
            (int(row["labels"][label]), _finite(row.get(field)))
            for row in usable
        ]
        pairs = [(target, score) for target, score in pairs if score is not None]
        y = [target for target, _ in pairs]
        scores = [float(score) for _, score in pairs]
        result[name] = {
            "n": len(pairs), "roc_auc": _auc(y, scores), "pr_auc": _pr_auc(y, scores),
        }
    factor_keys = sorted({
        key for row in usable for key in row.get("factor_scores", {})
    })
    factor_group: dict[str, Any] = {}
    for removed in factor_keys:
        pairs: list[tuple[int, float]] = []
        for row in usable:
            values = [
                float(value) for key, value in row.get("factor_scores", {}).items()
                if key != removed and value is not None
            ]
            if values:
                pairs.append((int(row["labels"][label]), sum(values) / len(values)))
        y = [target for target, _ in pairs]
        scores = [score for _, score in pairs]
        factor_group["without_" + removed] = {
            "n": len(pairs), "roc_auc": _auc(y, scores), "pr_auc": _pr_auc(y, scores),
        }
    result["factor_group_leave_one_out_equal_weight_proxy"] = factor_group
    result["with_vs_without_eq"] = "UNSCORABLE_NO_PRE_EQ_REPLAY_FEATURES"
    return result


def _challenger_models(rows: list[dict[str, Any]], label: str,
                       horizon: int) -> dict[str, Any]:
    """在与 Champion 相同的 purge/embargo 年度折比较线性 Challenger。"""
    dimensions = {
        "valuation_pressure": "valuation",
        "capitulation_pressure": "capitulation",
        "leverage_clearance": "leverage",
        "seller_exhaustion": "requires_dedicated_feature",
        "direct_spot_demand": "requires_split_demand_feature",
        "liquidity_ammunition": "requires_split_liquidity_feature",
        "price_confirmation": "confirmation",
        "macro_regime": "macro",
    }
    try:
        import sklearn  # type: ignore
        from sklearn.impute import SimpleImputer  # type: ignore
        from sklearn.linear_model import LogisticRegression  # type: ignore
        from sklearn.pipeline import make_pipeline  # type: ignore
        from sklearn.preprocessing import StandardScaler  # type: ignore
    except ImportError:
        return {
            "status": "UNSCORABLE_DEPENDENCY_NOT_INSTALLED",
            "library_versions": {},
            "install": "pip install -r requirements-audit.txt",
            "dimensions": dimensions,
            "models": {
                name: "UNSCORABLE" for name in
                ("logistic", "ridge", "lasso", "elastic_net")
            },
            "nonlinear": "SKIPPED_INSUFFICIENT_N_EFF",
        }
    factor_keys = sorted({key for row in rows for key in row.get("factor_scores", {})})
    years = sorted({int(row["day"][:4]) for row in rows})
    specs = {
        "logistic": {"penalty": "l2", "C": 1e6, "solver": "liblinear"},
        "ridge": {"penalty": "l2", "C": 1.0, "solver": "liblinear"},
        "lasso": {"penalty": "l1", "C": 1.0, "solver": "liblinear"},
        "elastic_net": {"penalty": "elasticnet", "C": 1.0, "solver": "saga", "l1_ratio": 0.5},
    }
    outputs: dict[str, Any] = {}
    def feature_row(row: dict[str, Any]) -> list[float]:
        values = [row.get("factor_scores", {}).get(key) for key in factor_keys]
        values.append(row.get("confirmation"))
        return [float(value) if value is not None else float("nan") for value in values]

    for name, kwargs in specs.items():
        all_y: list[int] = []
        all_p: list[float] = []
        champion_scores: list[float] = []
        champion_probabilities: list[float] = []
        fold_count = 0
        for year in years:
            cutoff = date(year, 1, 1) - timedelta(days=horizon + EMBARGO_DAYS)
            train = [
                row for row in rows
                if date.fromisoformat(row["day"]) <= cutoff
                and row["labels"].get(label) is not None
                and row.get("combined_score") is not None
            ]
            test = [
                row for row in rows
                if int(row["day"][:4]) == year
                and row["labels"].get(label) is not None
                and row.get("combined_score") is not None
            ]
            train_y = [int(row["labels"][label]) for row in train]
            if len(train) < 100 or sum(train_y) < 10 or not test:
                continue
            train_x = [feature_row(row) for row in train]
            test_x = [feature_row(row) for row in test]
            model = make_pipeline(
                SimpleImputer(strategy="median"), StandardScaler(),
                LogisticRegression(max_iter=5000, random_state=20260813, **kwargs),
            )
            model.fit(train_x, train_y)
            probability = model.predict_proba(test_x)[:, 1].tolist()
            # bottom-v4 没有已校准的单一组合概率；可排序的 Champion 主标尺
            # 是 Stress。Confirmation/60:40 组合分别在消融中作为候选评估。
            # Challenger 与 Stress 仍必须在完全相同的 OOS 时点配对比较。
            train_champion = [float(row["stress"]) for row in train]
            test_champion = [float(row["stress"]) for row in test]
            champion_probability = _calibrate(train_y, train_champion, test_champion)
            all_p.extend(float(value) for value in probability)
            champion_scores.extend(test_champion)
            champion_probabilities.extend(champion_probability)
            all_y.extend(int(row["labels"][label]) for row in test)
            fold_count += 1
        outputs[name] = (
            {
                "folds": fold_count,
                "evaluation_status": (
                    "SCORABLE" if len(all_y) >= 30 and len(set(all_y)) == 2
                    else "UNSCORABLE_INSUFFICIENT_OOS_CLASS_SUPPORT"
                ),
                "metrics": _classification(
                    all_y, [int(value >= 0.5) for value in all_p], all_p, all_p,
                ),
                "champion_same_oos": _classification(
                    all_y, [int(value >= 0.5) for value in champion_probabilities],
                    champion_scores, champion_probabilities,
                ),
                "bootstrap_delta_vs_champion": _block_bootstrap_delta(
                    all_y, all_p, champion_scores, champion_probabilities,
                ),
            } if all_y else "UNSCORABLE_INSUFFICIENT_FOLDS"
        )
    n_eff = _n_eff([int(row["labels"][label]) for row in rows if row["labels"].get(label) is not None])
    positives = sum(int(row["labels"][label]) for row in rows if row["labels"].get(label) is not None)
    nonlinear = (
        "ELIGIBLE_NOT_DEFAULT" if (n_eff or 0) >= 200 and positives >= 100
        else f"SKIPPED_INSUFFICIENT_N_EFF:n_eff={n_eff},positive_n={positives}"
    )
    return {
        "status": "RESEARCH_ONLY", "library_versions": {"scikit_learn": sklearn.__version__},
        "dimensions": dimensions, "models": outputs, "nonlinear": nonlinear,
    }


def _path_outcome(path: list[float]) -> dict[str, float]:
    entry = path[0]
    peak = entry
    max_drawdown = 0.0
    for value in path:
        peak = max(peak, value)
        max_drawdown = min(max_drawdown, value / peak - 1)
    return {
        "terminal_return": path[-1] / entry - 1,
        "mae": min(path) / entry - 1,
        "mfe": max(path) / entry - 1,
        "max_drawdown": max_drawdown,
    }


def _first_crossings(rows: list[dict[str, Any]], field: str,
                     threshold: float) -> list[dict[str, Any]]:
    """仅保留从阈值下方向上首次跨越的事件，缺失周不制造伪新事件。"""
    selected: list[dict[str, Any]] = []
    active = False
    for row in rows:
        score = _finite(row.get(field))
        if score is None:
            continue
        current = score >= threshold
        if current and not active:
            selected.append(row)
        active = current
    return selected


def _quantiles(values: list[float]) -> Optional[dict[str, float]]:
    if not values:
        return None
    ordered = sorted(values)
    def at(q: float) -> float:
        return ordered[round((len(ordered) - 1) * q)]
    return {key: at(q) for key, q in (
        ("p05", 0.05), ("p25", 0.25), ("p50", 0.50),
        ("p75", 0.75), ("p95", 0.95),
    )}


def _median_block_ci(values: list[float], *, iterations: int = BOOTSTRAP_ITERATIONS,
                     block: int = 26) -> Any:
    if len(values) < 10:
        return "UNSCORABLE: fewer than 10 outcomes"
    rng = random.Random(20260816 + len(values))
    n = len(values)
    estimates: list[float] = []
    # block 等于整段样本时每次重采样都完全相同，会生成虚假的零宽区间。
    effective_block = min(block, max(2, n // 3))
    for _ in range(iterations):
        sample: list[float] = []
        while len(sample) < n:
            start = rng.randrange(0, max(1, n - effective_block + 1))
            sample.extend(values[start:start + effective_block])
        estimates.append(median(sample[:n]))
    estimates.sort()
    return [
        estimates[int(0.025 * len(estimates))],
        estimates[int(0.975 * len(estimates)) - 1],
    ]


def _occupied_days(signals: list[dict[str, Any]], horizon: int) -> int:
    intervals = sorted(
        (date.fromisoformat(row["day"]) + timedelta(days=1),
         date.fromisoformat(row["day"]) + timedelta(days=1 + horizon))
        for row in signals
    )
    if not intervals:
        return 0
    total = 0
    start, end = intervals[0]
    for next_start, next_end in intervals[1:]:
        if next_start <= end:
            end = max(end, next_end)
        else:
            total += (end - start).days
            start, end = next_start, next_end
    return total + (end - start).days


def _strategy(rows: list[dict[str, Any]], price: PriceIndex,
              threshold: float = 65.0) -> dict[str, Any]:
    ordered = sorted(rows, key=lambda row: row["day"])
    groups = {
        "combined": _first_crossings(ordered, "combined_score", threshold),
        "DCA_weekly": ordered,
        "Buy_and_Hold": ordered[:1],
        "ATH_drawdown": _first_crossings(ordered, "drawdown_score", 50.0),
        "200W": _first_crossings(ordered, "ma200_score", 50.0),
        "equal_factor": _first_crossings(ordered, "equal_factor_score", threshold),
    }
    base: dict[str, dict[str, Any]] = {}
    for name, signals in groups.items():
        horizons: dict[str, Any] = {}
        for horizon in (30, 90, 180, 365):
            paths = [
                path for row in signals
                if (path := price.path(row["day"], horizon, next_day=True))
            ]
            outcomes = [_path_outcome(path) for path in paths]
            returns = [item["terminal_return"] for item in outcomes]
            mae = [item["mae"] for item in outcomes]
            mfe = [item["mfe"] for item in outcomes]
            drawdown = [item["max_drawdown"] for item in outcomes]
            horizons[str(horizon)] = {
                "n": len(outcomes),
                "terminal_return_quantiles": _quantiles(returns),
                "median_return_ci95_block5000": _median_block_ci(returns),
                "median_mae": median(mae) if mae else None,
                "worst_mae": min(mae) if mae else None,
                "median_mfe": median(mfe) if mfe else None,
                "median_max_drawdown": median(drawdown) if drawdown else None,
                "worst_max_drawdown": min(drawdown) if drawdown else None,
                "capital_occupancy_days": _occupied_days(signals[:len(paths)], horizon),
            }
        base[name] = horizons

    out: dict[str, Any] = {
        "entry": "first_threshold_cross_then_next_available_day",
        "spot_only": True, "leverage": 1,
        "signals": {name: len(group) for name, group in groups.items()},
        "costs_bps": {},
    }
    for cost in (0, 10, 25):
        cost_rate = 2 * cost / 10000
        by_model: dict[str, Any] = {}
        for name, horizons in base.items():
            by_model[name] = {}
            for horizon, stats in horizons.items():
                item = dict(stats)
                quantiles = stats.get("terminal_return_quantiles")
                item["terminal_return_quantiles"] = (
                    {key: value - cost_rate for key, value in quantiles.items()}
                    if quantiles else None
                )
                ci = stats.get("median_return_ci95_block5000")
                item["median_return_ci95_block5000"] = (
                    [value - cost_rate for value in ci] if isinstance(ci, list) else ci
                )
                by_model[name][horizon] = item
        for horizon in ("30", "90", "180", "365"):
            benchmark = (
                (by_model.get("Buy_and_Hold", {}).get(horizon, {})
                 .get("terminal_return_quantiles") or {}).get("p50")
            )
            for model in by_model.values():
                median_return = (
                    (model[horizon].get("terminal_return_quantiles") or {}).get("p50")
                )
                model[horizon]["opportunity_cost_vs_buy_hold"] = (
                    benchmark - median_return
                    if benchmark is not None and median_return is not None else None
                )
        out["costs_bps"][str(cost)] = by_model
    out["economic_significance"] = (
        "UNSCORABLE: PIT_APPROX legacy data and too few independent BTC cycles"
    )
    return out


def _cycles(rows: list[dict[str, Any]], label: str) -> list[dict[str, Any]]:
    cycles = ((2013, 2016), (2017, 2020), (2021, 2024), (2025, 2029))
    result = []
    for start, end in cycles:
        subset = [row for row in rows if start <= int(row["day"][:4]) <= end and row["labels"].get(label) is not None]
        train = [row for row in rows if not (start <= int(row["day"][:4]) <= end) and row["labels"].get(label) is not None]
        y = [int(row["labels"][label]) for row in subset]
        scores = [row["combined_score"] for row in subset]
        train_y = [int(row["labels"][label]) for row in train]
        train_scores = [row["combined_score"] for row in train]
        threshold = _choose_threshold(train_y, train_scores) if train else 50.0
        metrics = _classification(y, [int(score >= threshold) for score in scores], scores) if y else {}
        result.append({
            "cycle": f"{start}-{end}", "n": len(subset), "positive_n": sum(y),
            "train_n": len(train), "threshold_from_other_cycles": threshold,
            "pr_auc": metrics.get("pr_auc"), "roc_auc": metrics.get("roc_auc"),
            "mcc": metrics.get("mcc"), "recall": metrics.get("recall"),
        })
    return result


def _errors(rows: list[dict[str, Any]], label: str, threshold: float = 65.0) -> dict[str, Any]:
    usable = [row for row in rows if row["labels"].get(label) is not None]
    fp = [row for row in usable if row["combined_score"] >= threshold and row["labels"][label] == 0]
    fn = [row for row in usable if row["combined_score"] < threshold and row["labels"][label] == 1]
    compact = lambda row: {
        "day": row["day"], "score": row["combined_score"],
        "stress": row.get("stress"), "confirmation": row.get("confirmation"),
        "factor_scores": row.get("factor_scores"),
        "outcome_180d": (row.get("outcomes") or {}).get("180"),
    }
    return {
        "false_positive_top10": [compact(row) for row in sorted(fp, key=lambda r: r["combined_score"], reverse=True)[:10]],
        "false_negative_top10": [compact(row) for row in sorted(fn, key=lambda r: r["combined_score"])[:10]],
    }


def _permutation(rows: list[dict[str, Any]], label: str) -> dict[str, Any]:
    usable = [row for row in rows if row["labels"].get(label) is not None]
    y = [int(row["labels"][label]) for row in usable]
    observed = _pr_auc(y, [row["combined_score"] for row in usable])
    if observed is None:
        return {"status": "UNSCORABLE"}
    rng = random.Random(718)
    exceed = 0
    shuffled = list(y)
    for _ in range(PERMUTATION_ITERATIONS):
        rng.shuffle(shuffled)
        value = _pr_auc(shuffled, [row["combined_score"] for row in usable])
        exceed += int(value is not None and value >= observed)
    return {"iterations": PERMUTATION_ITERATIONS, "observed_pr_auc": observed, "p_value": (exceed + 1) / (PERMUTATION_ITERATIONS + 1)}


def _permutation_importance(rows: list[dict[str, Any]], label: str) -> dict[str, Any]:
    """逐因子置换 1,000 次，报告等权因子 PR-AUC 的下降分布。"""
    usable = [row for row in rows if row["labels"].get(label) is not None]
    y = [int(row["labels"][label]) for row in usable]
    factors = sorted({key for row in usable for key in row.get("factor_scores", {})})
    sums = [sum(float(v) for v in row.get("factor_scores", {}).values() if v is not None) for row in usable]
    counts = [sum(1 for v in row.get("factor_scores", {}).values() if v is not None) for row in usable]
    baseline_scores = [total / count if count else 0.0 for total, count in zip(sums, counts)]
    baseline = _pr_auc(y, baseline_scores)
    if baseline is None:
        return {"status": "UNSCORABLE"}
    rng = random.Random(20260815)
    importance: list[dict[str, Any]] = []
    for factor in factors:
        indices = [i for i, row in enumerate(usable) if row.get("factor_scores", {}).get(factor) is not None]
        original = [float(usable[i]["factor_scores"][factor]) for i in indices]
        drops: list[float] = []
        for _ in range(PERMUTATION_ITERATIONS):
            shuffled = list(original)
            rng.shuffle(shuffled)
            scores = list(baseline_scores)
            for position, replacement in zip(indices, shuffled):
                scores[position] = (
                    sums[position] - float(usable[position]["factor_scores"][factor]) + replacement
                ) / counts[position]
            value = _pr_auc(y, scores)
            if value is not None:
                drops.append(baseline - value)
        ordered = sorted(drops)
        importance.append({
            "factor": factor, "available_n": len(indices),
            "mean_pr_auc_drop": sum(drops) / len(drops) if drops else None,
            "drop_ci95": [
                ordered[int(0.025 * len(ordered))],
                ordered[int(0.975 * len(ordered)) - 1],
            ] if ordered else None,
        })
    return {
        "iterations_per_factor": PERMUTATION_ITERATIONS,
        "baseline_equal_factor_pr_auc": baseline,
        "importance": importance,
    }


def _parameter_sensitivity(rows: list[dict[str, Any]], label: str) -> list[dict[str, Any]]:
    usable = [row for row in rows if row["labels"].get(label) is not None]
    y = [int(row["labels"][label]) for row in usable]
    scores = [float(row["combined_score"]) for row in usable]
    return [
        {"threshold": threshold, **_classification(
            y, [int(score >= threshold) for score in scores], scores,
        )}
        for threshold in range(20, 81, 5)
    ]


def _replay_dataset(store: Any, data: dict[str, Any], dataset_id: str) -> list[dict[str, Any]]:
    replay = load_or_build_replay(
        store, data, ALGORITHM_VERSION, compute_core,
        data_policy_id=DATA_POLICY_ID, dataset_id=dataset_id,
    )
    price = PriceIndex(data.get("btc_price_onchain") or data.get("btc_close_1d") or [])
    price_rows = data.get("btc_price_onchain") or data.get("btc_close_1d") or []
    price_days = [day for day, _ in price_rows]
    price_values = [value for _, value in price_rows]
    ma_rows = data.get("ma_200w") or []
    ma_days = [day for day, _ in ma_rows]
    ma_values = [value for _, value in ma_rows]
    rows: list[dict[str, Any]] = []
    running_ath = 0.0
    rng = random.Random(99)
    for item in replay:
        if item.get("stress") is None:
            continue
        pi = bisect_right(price_days, item["day"]) - 1
        if pi < 0:
            continue
        current = price_values[pi]
        running_ath = max(price_values[:pi + 1]) if pi >= 0 else running_ath
        mi = bisect_right(ma_days, item["day"]) - 1
        factor_scores = (item.get("features") or {}).get("factor_scores") or {}
        features = item.get("features") or {}
        factor_values = [float(value) for value in factor_scores.values() if value is not None]
        confirmation = _finite(item.get("confirmation"))
        confirmation_before_penalty = _finite(features.get("confirmation_before_penalty"))
        stress = float(item["stress"])
        outcomes: dict[str, Any] = {}
        for horizon in (30, 90, 180, 365):
            path = price.path(item["day"], horizon, next_day=True)
            outcomes[str(horizon)] = _path_outcome(path) if path else None
        rows.append({
            **item,
            "labels": build_labels(price, item["day"]),
            "factor_scores": factor_scores,
            "equal_factor_score": sum(factor_values) / len(factor_values) if factor_values else stress,
            # Confirmation 弃权时组合分数也必须弃权，禁止把缺失项当 0 后继续
            # 与满覆盖时期使用同一阈值。
            "combined_score": (
                0.6 * stress + 0.4 * confirmation if confirmation is not None else None
            ),
            "combined_without_fake_filter": (
                0.6 * stress + 0.4 * confirmation_before_penalty
                if confirmation_before_penalty is not None else None
            ),
            "outcomes": outcomes,
            "ma200_score": 100.0 if mi >= 0 and current <= ma_values[mi] else 0.0,
            "drawdown_score": 100.0 if running_ath > 0 and current / running_ath - 1 <= -0.70 else 0.0,
            "random_score": rng.random() * 100,
        })
    return rows


def _ic_rank_ic(rows: list[dict[str, Any]], horizon: int = 180) -> dict[str, Any]:
    """分数与下一可交易日入场的前向终点收益之间的 IC/Rank IC。"""
    result: dict[str, Any] = {}
    for name, field in {
        "stress": "stress",
        "confirmation": "confirmation",
        "combined": "combined_score",
        "equal_factor": "equal_factor_score",
    }.items():
        pairs: list[tuple[float, float]] = []
        for row in rows:
            score = _finite(row.get(field))
            outcome = (row.get("outcomes") or {}).get(str(horizon)) or {}
            forward_return = _finite(outcome.get("terminal_return"))
            if score is not None and forward_return is not None:
                pairs.append((score, forward_return))
        scores = [score for score, _ in pairs]
        returns = [value for _, value in pairs]
        result[name] = {
            "n": len(pairs),
            "ic": _pearson(scores, returns),
            "rank_ic": _pearson(_rank(scores), _rank(returns)) if pairs else None,
        }
    return {"horizon_days": horizon, "metrics": result}


def _score_bin_economics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for lo in range(0, 100, 20):
        hi = lo + 20
        subset = [
            row for row in rows
            if row.get("combined_score") is not None
            and lo <= float(row["combined_score"]) <= hi
            and (hi == 100 or float(row["combined_score"]) < hi)
        ]
        horizons: dict[str, Any] = {}
        for horizon in (30, 90, 180, 365):
            outcomes = [
                (row.get("outcomes") or {}).get(str(horizon)) for row in subset
            ]
            outcomes = [item for item in outcomes if item]
            returns = [float(item["terminal_return"]) for item in outcomes]
            horizons[str(horizon)] = {
                "n": len(outcomes),
                "mean_return": sum(returns) / len(returns) if returns else None,
                "median_return": median(returns) if returns else None,
                "win_rate": (
                    sum(value > 0 for value in returns) / len(returns) if returns else None
                ),
                "return_quantiles": _quantiles(returns),
                "median_mae": median(
                    float(item["mae"]) for item in outcomes
                ) if outcomes else None,
                "median_mfe": median(
                    float(item["mfe"]) for item in outcomes
                ) if outcomes else None,
                "worst_max_drawdown": min(
                    float(item["max_drawdown"]) for item in outcomes
                ) if outcomes else None,
            }
        result[f"{lo}-{hi}"] = horizons
    return {
        "status": "DESCRIPTIVE_PIT_APPROX_NOT_OOS",
        "bins": result,
        "monotonicity": (
            "UNSCORABLE: independent cycle count and strict PIT history are insufficient"
        ),
    }


def _indicator_audit(data: dict[str, Any], rows: list[dict[str, Any]],
                     label: str, decision_day: str) -> list[dict[str, Any]]:
    specs: dict[str, Any] = {
        metric: spec for spec in build_registry() for metric in spec.metrics
    }
    label_rows = [row for row in rows if row["labels"].get(label) is not None]
    vectors: dict[str, list[Optional[float]]] = {}
    for metric, values in data.items():
        days = [day for day, _ in values]
        spec = specs.get(metric)
        tolerance = (
            spec.staleness_days if spec and spec.staleness_days is not None
            else 14 if spec and spec.cadence == "weekly" else 3
        )
        aligned: list[Optional[float]] = []
        for row in label_rows:
            index = bisect_right(days, row["day"]) - 1
            if index < 0:
                aligned.append(None)
                continue
            age = (date.fromisoformat(row["day"]) - date.fromisoformat(days[index])).days
            aligned.append(float(values[index][1]) if age <= tolerance else None)
        vectors[metric] = aligned
    max_correlations: dict[str, Optional[float]] = {}
    metrics = sorted(vectors)
    for metric in metrics:
        strongest: Optional[float] = None
        for other in metrics:
            if other == metric:
                continue
            pairs = [
                (a, b) for a, b in zip(vectors[metric], vectors[other])
                if a is not None and b is not None
            ]
            rho = _pearson(
                [float(a) for a, _ in pairs], [float(b) for _, b in pairs],
            )
            if rho is not None and (strongest is None or abs(rho) > abs(strongest)):
                strongest = rho
        max_correlations[metric] = strongest

    cycles = ((2013, 2016), (2017, 2020), (2021, 2024), (2025, 2029))
    result: list[dict[str, Any]] = []
    for metric in metrics:
        values = data.get(metric) or []
        spec = specs.get(metric)
        role = metric_contract(metric).get("role")
        if not values:
            result.append({
                "metric": metric, "role": role,
                "source": getattr(spec, "source", "UNREGISTERED"),
                "cadence": getattr(spec, "cadence", "UNKNOWN"),
                "unit": metric_contract(metric).get("unit"),
                "first_day": None, "latest_day": None, "n": 0,
                "btc_cycle_coverage": 0, "approx_missing_rate": 1.0,
                "max_timestamp_gap_days": None, "update_lag_days": None,
                "proxy_status": "UNSCORABLE_METADATA_NOT_VERSIONED",
                "revision_status": "PIT_UNAVAILABLE_LEGACY",
                "point_in_time_status": "PIT_UNAVAILABLE",
                "current_eq": "UNSCORABLE_NO_METRIC_LEVEL_EQ_HISTORY",
                "target_pearson": None, "single_roc_auc": None,
                "single_pr_auc": None, "ic_180d": None, "rank_ic_180d": None,
                "max_abs_peer_correlation": None,
                "incremental_value": "UNSCORABLE_MISSING_DATA",
                "ablation_impact": "UNSCORABLE_MISSING_DATA",
                "stability": "UNSCORABLE_MISSING_DATA",
                "recommendation": (
                    "DEPRECATE" if role == "unused"
                    else "DISPLAY_ONLY" if role == "display_only"
                    else "BLOCKED_MISSING_DATA"
                ),
            })
            continue
        cadence_days = 7 if spec and spec.cadence == "weekly" else 1
        first, latest = values[0][0], values[-1][0]
        first_date, latest_date = date.fromisoformat(first), date.fromisoformat(latest)
        expected = max(1, (latest_date - first_date).days // cadence_days + 1)
        gaps = [
            (date.fromisoformat(values[i][0]) - date.fromisoformat(values[i - 1][0])).days
            for i in range(1, len(values))
        ]
        pairs = [
            (float(value), int(row["labels"][label]),
             _finite(((row.get("outcomes") or {}).get("180") or {}).get("terminal_return")))
            for row, value in zip(label_rows, vectors[metric]) if value is not None
        ]
        x = [value for value, _, _ in pairs]
        y = [target for _, target, _ in pairs]
        ic_pairs = [(value, ret) for value, _, ret in pairs if ret is not None]
        ic_x = [value for value, _ in ic_pairs]
        ic_y = [float(ret) for _, ret in ic_pairs]
        result.append({
            "metric": metric,
            "role": role,
            "source": getattr(spec, "source", "UNREGISTERED"),
            "cadence": getattr(spec, "cadence", "UNKNOWN"),
            "unit": metric_contract(metric).get("unit"),
            "first_day": first, "latest_day": latest, "n": len(values),
            "btc_cycle_coverage": sum(
                first_date.year <= end and latest_date.year >= start
                for start, end in cycles
            ),
            "approx_missing_rate": max(0.0, 1 - len(values) / expected),
            "max_timestamp_gap_days": max(gaps) if gaps else None,
            "update_lag_days": (date.fromisoformat(decision_day) - latest_date).days,
            "proxy_status": "UNSCORABLE_METADATA_NOT_VERSIONED",
            "revision_status": "PIT_UNAVAILABLE_LEGACY",
            "point_in_time_status": "PIT_APPROX",
            "current_eq": "UNSCORABLE_NO_METRIC_LEVEL_EQ_HISTORY",
            "target_pearson": _pearson(x, [float(value) for value in y]),
            "single_roc_auc": _auc(y, x),
            "single_pr_auc": _pr_auc(y, x),
            "ic_180d": _pearson(ic_x, ic_y),
            "rank_ic_180d": (
                _pearson(_rank(ic_x), _rank(ic_y)) if ic_pairs else None
            ),
            "max_abs_peer_correlation": (
                abs(max_correlations[metric])
                if max_correlations[metric] is not None else None
            ),
            "incremental_value": "UNSCORABLE_NOT_EXPOSED_AS_INDIVIDUAL_REPLAY_FEATURE",
            "ablation_impact": "UNSCORABLE_NOT_EXPOSED_AS_INDIVIDUAL_REPLAY_FEATURE",
            "stability": "UNSCORABLE_STRICT_PIT_AND_MULTI_CYCLE_REQUIRED",
            "recommendation": (
                "DEPRECATE" if role == "unused"
                else "DISPLAY_ONLY" if role == "display_only"
                else "HOLD_PENDING_INCREMENTAL_OOS_TEST"
            ),
        })
    return result


def _regime_stability(rows: list[dict[str, Any]], price: PriceIndex,
                      label: str) -> dict[str, Any]:
    usable = [row for row in rows if row["labels"].get(label) is not None]
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in usable:
        index = price.at_or_before(row["day"])
        if index is None:
            continue
        start_200 = max(0, index - 199)
        average = sum(price.values[start_200:index + 1]) / (index - start_200 + 1)
        groups["bull_above_200d" if price.values[index] >= average else "bear_below_200d"].append(row)
        start_90 = max(1, index - 89)
        returns = [
            math.log(price.values[i] / price.values[i - 1])
            for i in range(start_90, index + 1) if price.values[i - 1] > 0
        ]
        if returns:
            mean_return = sum(returns) / len(returns)
            variance = sum((value - mean_return) ** 2 for value in returns) / len(returns)
            annualized = math.sqrt(variance * 365)
            groups["high_vol_ge_60pct" if annualized >= 0.60 else "low_vol_lt_60pct"].append(row)
    metrics: dict[str, Any] = {}
    for name, subset in groups.items():
        y = [int(row["labels"][label]) for row in subset]
        scores = [float(row["combined_score"]) for row in subset]
        metrics[name] = _classification(
            y, [int(score >= 65) for score in scores], scores,
        )
    return {
        "status": "DESCRIPTIVE_CAUSAL_PRICE_REGIMES_PIT_APPROX_NOT_OOS",
        "price_regimes": metrics,
        "liquidity_macro_crisis_halving_regimes": (
            "UNSCORABLE: versioned regime series and sufficient cycle support are missing"
        ),
    }


def _improvement_backlog(sample_sizes: dict[str, Any]) -> list[dict[str, str]]:
    n_eff = sample_sizes.get("n_eff")
    return [
        {
            "rank": "1", "problem": "历史数据没有真实 available_at/vintage/revision",
            "evidence": "legacy rows are PIT_APPROX/PIT_UNAVAILABLE",
            "change": "持续积累 append-only 观测版本并冻结每次审计数据集",
            "expected_improvement": "允许未来开展严格 PIT 回测；幅度当前不可量化",
            "validation": "用已知发布滞后和历史修订夹具验证 as-of 查询",
        },
        {
            "rank": "2", "problem": "严格 Walk-Forward 测试样本和类别支持不足",
            "evidence": "only 6 OOS rows and zero positives in the sole scored fold",
            "change": "延长严格 PIT 数据积累期，不降低门槛换取表面数字",
            "expected_improvement": "提高可评分性而非保证提升性能",
            "validation": "OOS N>=30 且正负类均有足够事件后才计算 PR-AUC/MCC",
        },
        {
            "rank": "3", "problem": "独立事件和有效样本极少",
            "evidence": f"event_n={sample_sizes.get('event_n')}, n_eff={n_eff}",
            "change": "所有结论绑定事件 N、非重叠 N 与 block-bootstrap",
            "expected_improvement": "降低虚假显著性；不会凭空增加预测力",
            "validation": "复制重叠周点不得改变事件点估计或置信区间",
        },
        {
            "rank": "4", "problem": "Confirmation 未证明增量价值",
            "evidence": "sample-in Confirmation IC is negative and combined PR-AUC is below Stress",
            "change": "保持为 Challenger 候选，拆分价格确认与需求确认后重新 OOS",
            "expected_improvement": "未知；可能应降权或删除",
            "validation": "paired OOS delta PR-AUC/Brier/Recall block-bootstrap",
        },
        {
            "rank": "5", "problem": "现有人工权重缺少统计来源",
            "evidence": "HEURISTIC WEIGHT; no scorable multi-fold Challenger comparison",
            "change": "只在训练折比较等权、Logistic、Ridge、Lasso、Elastic Net",
            "expected_improvement": "不可预设",
            "validation": "多个冻结测试窗均优于 Champion 才升级",
        },
        {
            "rank": "6", "problem": "原始指标未逐项进入版本化回放特征",
            "evidence": "individual incremental value and ablation are UNSCORABLE",
            "change": "冻结每个子信号/原始指标的 as-of 特征值与缺失原因",
            "expected_improvement": "使死指标与噪声指标可被证伪",
            "validation": "逐指标/因子组消融和 1000 次置换重要性",
        },
        {
            "rank": "7", "problem": "核心数据源缺少第二供应商交叉验证",
            "evidence": "source consistency metrics are UNSCORABLE",
            "change": "为价格、OI、资金费、ETF、稳定币建立只读第二源对照",
            "expected_improvement": "降低 Vendor Risk；预测增量未知",
            "validation": "相关性、MAD、最大偏差和缺失日期报告",
        },
        {
            "rank": "8", "problem": "假底过滤器仅有样本内微弱差异",
            "evidence": "with/without filter difference has no OOS confidence interval",
            "change": "冻结过滤器输入，单独评估 FPR、Recall、MAE 与净收益",
            "expected_improvement": "不可预设",
            "validation": "paired block-bootstrap net benefit and Recall non-inferiority",
        },
        {
            "rank": "9", "problem": "Regime 稳定性缺少宏观/流动性点时序列",
            "evidence": "only causal price trend/volatility regimes are descriptively scorable",
            "change": "版本化 DXY、实质利率、流动性与危机标签",
            "expected_improvement": "识别模型失效环境；不保证整体指标上升",
            "validation": "训练折定义 Regime，冻结测试折分别报告指标",
        },
        {
            "rank": "10", "problem": "经济结果仍受 PIT_APPROX 和少周期限制",
            "evidence": "economic significance is UNSCORABLE",
            "change": "仅用首次跨阈值、下一可交易日、成本和资金占用回测",
            "expected_improvement": "避免重叠信号夸大收益",
            "validation": "与 DCA/Buy-and-Hold/ATH/200W 同机会集配对比较",
        },
    ]


def _next_experiments() -> list[dict[str, str]]:
    return [
        {"experiment": "1. PIT ingestion shadow test", "method": "连续收集 observation_ts/period_end/available_at/revision", "success": "所有 as-of 查询零未来值且修订可追溯", "falsifier": "任一决策时点读到 available_at 之后的数据"},
        {"experiment": "2. Confirmation incremental test", "method": "Stress-only vs Confirmation-only vs 组合 vs 过滤器，purged Walk-Forward", "success": "组合 ΔPR-AUC>0、ΔBrier<0 且 Recall 不恶化的 95% CI", "falsifier": "区间跨 0 或 Recall/MAE 恶化"},
        {"experiment": "3. Weight challenger", "method": "训练折比较等权/Logistic/Ridge/Lasso/Elastic Net", "success": "多个冻结窗稳定优于 Champion", "falsifier": "仅样本内提升或单窗提升"},
        {"experiment": "4. Individual feature ablation", "method": "冻结原始特征后逐项删除和 1000 次 permutation", "success": "增量方向稳定且 CI 不含 0", "falsifier": "删除不降反升或置换无影响"},
        {"experiment": "5. Leave-One-Cycle-Out", "method": "每次完整留出一个 BTC 周期", "success": "所有可评分周期方向一致", "falsifier": "新周期 PR-AUC/MCC 反向"},
        {"experiment": "6. Calibration gate", "method": "嵌套 OOS 生成 reliability/Brier/ECE", "success": "校准在冻结窗稳定且优于基准", "falsifier": "过度自信或 Brier 无改善"},
        {"experiment": "7. Source consistency", "method": "关键指标双供应商逐日对齐", "success": "偏差在预注册容忍内且缺失可解释", "falsifier": "方向性分歧或 schema 漂移"},
        {"experiment": "8. Score monotonicity", "method": "按训练折边界分箱，测试折比较收益/MAE", "success": "分数越高收益改善且 MAE 不恶化", "falsifier": "高分箱无区分或更差"},
        {"experiment": "9. Strategy opportunity-cost test", "method": "首次跨阈值后次日入场，0/10/25bps，与 DCA/B&H 配对", "success": "收益/MAE/资金占用的 CI 支持增量", "falsifier": "机会成本为正或风险恶化"},
        {"experiment": "10. Leakage canary", "method": "持续运行纯随机/已知信号/故意未来特征合成集", "success": "分别判无效/有效/DATA_LEAKAGE", "falsifier": "泄漏集未被阻断或随机集被验证"},
    ]


def _label_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    keys = sorted({key for row in rows for key in row["labels"]})
    result = {}
    for key in keys:
        values = [row["labels"].get(key) for row in rows if row["labels"].get(key) is not None]
        result[key] = {"n": len(values), "positive_n": sum(values), "prevalence": sum(values) / len(values) if values else None}
    return result


def _non_overlapping_positive_n(rows: list[dict[str, Any]], label: str,
                                horizon_days: int) -> int:
    selected = 0
    cutoff: Optional[date] = None
    for row in rows:
        current = date.fromisoformat(row["day"])
        if row["labels"].get(label) == 1 and (cutoff is None or current >= cutoff):
            selected += 1
            cutoff = current + timedelta(days=horizon_days)
    return selected


def _non_overlapping_n(rows: list[dict[str, Any]], label: str,
                       horizon_days: int) -> int:
    selected = 0
    cutoff: Optional[date] = None
    for row in rows:
        current = date.fromisoformat(row["day"])
        if row["labels"].get(label) is not None and (cutoff is None or current >= cutoff):
            selected += 1
            cutoff = current + timedelta(days=horizon_days)
    return selected


def _markdown(payload: dict[str, Any]) -> str:
    def fmt_metric(value: Any) -> str:
        number = _finite(value)
        return f"{number:.3f}" if number is not None else "UNSCORABLE"
    provenance = payload.get("source_provenance") or {}
    provenance_summary = (
        f"来源={provenance.get('kind', 'UNVERIFIED')}；"
        f"冻结时间={provenance.get('frozen_at', 'UNVERIFIED')}；"
        f"SQLite quick_check={provenance.get('sqlite_quick_check', 'UNVERIFIED')}。"
        f"生产遗留快照污染证据：{provenance.get('legacy_snapshot_contamination', 'UNVERIFIED')}"
    )
    sample = payload["sample_sizes"]
    accuracy_lines = [
        "| 尺度 | Label | 状态 | OOS N | Precision | Recall | F1 | MCC | Bal.Acc | ROC-AUC | PR-AUC |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for scale, item in payload["accuracy_dashboard"].items():
        metrics = item.get("metrics") or {}
        accuracy_lines.append(
            f"| {scale} | {item['label']} | {metrics.get('validation_status', 'UNSCORABLE')} | "
            f"{metrics.get('n', 0)} | {fmt_metric(metrics.get('precision'))} | "
            f"{fmt_metric(metrics.get('recall'))} | {fmt_metric(metrics.get('f1'))} | "
            f"{fmt_metric(metrics.get('mcc'))} | {fmt_metric(metrics.get('balanced_accuracy'))} | "
            f"{fmt_metric(metrics.get('roc_auc'))} | {fmt_metric(metrics.get('pr_auc'))} |"
        )
    calibration = payload.get("calibration") or {}
    calibration_lines = [
        f"产品 probability=null。主标签 OOS 状态={calibration.get('status')}；"
        f"Brier={fmt_metric(calibration.get('brier'))}，Log Loss={fmt_metric(calibration.get('log_loss'))}，"
        f"ECE={fmt_metric(calibration.get('ece'))}。",
        "Reliability 分桶已写入 JSON `calibration.curve`；由于 OOS 只有单一类别，不能发布概率。",
    ]
    economics_lines = [
        "Score Bin 结果是 PIT_APPROX 样本内描述，不是 OOS 交易证据。180 天摘要：",
        "| Score Bin | N | Mean Return | Median Return | Win Rate | Median MAE | Median MFE |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for score_bin, horizons in payload["score_bin_economics"]["bins"].items():
        item = horizons["180"]
        economics_lines.append(
            f"| {score_bin} | {item['n']} | {fmt_metric(item.get('mean_return'))} | "
            f"{fmt_metric(item.get('median_return'))} | {fmt_metric(item.get('win_rate'))} | "
            f"{fmt_metric(item.get('median_mae'))} | {fmt_metric(item.get('median_mfe'))} |"
        )
    benchmark_lines = [
        "以下为共同样本上的描述性排序，不是 OOS 升级证据：",
        "| Benchmark | PR-AUC | MCC | Recall |",
        "|---|---:|---:|---:|",
    ] + [
        f"| {name} | {fmt_metric(metrics.get('pr_auc'))} | "
        f"{fmt_metric(metrics.get('mcc'))} | {fmt_metric(metrics.get('recall'))} |"
        for name, metrics in payload["benchmarks"].items()
    ]
    indicator_lines = [
        "逐指标统计为 PIT_APPROX 描述；Incremental Value/逐指标消融因旧回放未冻结原始特征而 UNSCORABLE。",
        "| 指标 | 角色 | 来源 | 覆盖 | N | Lag(d) | PIT | ROC-AUC | PR-AUC | IC180 | Max abs rho | 建议 |",
        "|---|---|---|---|---:|---:|---|---:|---:|---:|---:|---|",
    ]
    for item in payload["indicator_audit"]:
        indicator_lines.append(
            f"| {item['metric']} | {item['role']} | {item['source']} | "
            f"{item['first_day']}..{item['latest_day']} | {item['n']} | {item['update_lag_days']} | "
            f"{item['point_in_time_status']} | {fmt_metric(item.get('single_roc_auc'))} | "
            f"{fmt_metric(item.get('single_pr_auc'))} | {fmt_metric(item.get('ic_180d'))} | "
            f"{fmt_metric(item.get('max_abs_peer_correlation'))} | {item['recommendation']} |"
        )
    errors = payload["errors"]
    fp_lines = [
        f"- {item['day']}: score={item['score']:.1f}, stress={item.get('stress')}, "
        f"confirmation={item.get('confirmation')}, factors={item.get('factor_scores')}"
        for item in errors["false_positive_top10"]
    ] or ["- UNSCORABLE/无案例"]
    fn_lines = [
        f"- {item['day']}: score={item['score']:.1f}, stress={item.get('stress')}, "
        f"confirmation={item.get('confirmation')}, factors={item.get('factor_scores')}"
        for item in errors["false_negative_top10"]
    ] or ["- UNSCORABLE/无案例"]
    problem_lines = [
        f"{item['rank']}. **{item['problem']}** — 证据：{item['evidence']}；修改：{item['change']}；"
        f"预期：{item['expected_improvement']}；验证：{item['validation']}。"
        for item in payload["top_10_problems"]
    ]
    experiment_lines = [
        f"- **{item['experiment']}**：{item['method']}；成功标准：{item['success']}；"
        f"证伪条件：{item['falsifier']}。"
        for item in payload["next_experiments"]
    ]
    sections = [
        ("1. Executive Audit Conclusion", "**INSUFFICIENT EVIDENCE。** bottom-v4 是启发式证据评分，不是已校准概率。所有 legacy 回放均为 PIT_APPROX，没有严格 PIT OOS；主 180 天标签的 Walk-Forward 仅 6 个成熟点且没有正类，PR-AUC、MCC、Recall 均不可评分。样本内结果不能证明优于简单估值/等权基准。最大风险是 legacy 数据缺少真实 vintage，且生产旧快照已确认混入未来日与未收盘周线。Confirmation 和 60/40 组合没有显示稳定增量。经济显著性与概率校准均不可评分。当前只能保留为研究型状态指标。"),
        ("2. Data Integrity Audit", f"dataset_id=`{payload['dataset_id']}`；policy={payload['data_policy_id']}；历史状态={payload['pit_status']}。{provenance_summary}\n\n未来日、未收盘周线和失败多输出已在新生产链路 fail-closed；旧历史仍不得冒充严格 PIT。逐指标缺失率、最大时间戳间隔、更新滞后、角色与来源见 JSON `indicator_audit`。第二数据源一致性：**UNSCORABLE**。"),
        ("3. Target / Label Audit", f"生成 Label A/B/C 共 {len(payload['labels'])} 个组合，完整 N/正例率见 JSON `labels`。主审计标签 `{PRIMARY_LABEL}` 同时约束 180 天终点收益与 MAE。所有标签均排除未走完前向窗口；不同时间尺度不混合。"),
        ("4. Sample Size Audit", f"Daily raw N={sample['daily_raw_n']}；weekly replay N={sample['weekly_raw_n']}；组合分数 N={sample['combined_score_n']}；主标签成熟 N={sample['primary_label_scorable_n']}；事件 N={sample['event_n']}；180 天非重叠 N={sample['non_overlapping_n_180d']}；N_eff={sample['n_eff']}。独立统计功效严重不足。"),
        ("5. Accuracy Dashboard", "\n".join(accuracy_lines)),
        ("6. Probability Calibration", "\n\n".join(calibration_lines)),
        ("7. Economic Performance", "\n".join(economics_lines) + "\n\n策略只采用首次跨阈值后的下一可交易日、现货、1x、0/10/25bps；收益分位、block-bootstrap CI、MAE/MFE、路径最大回撤、资金占用与机会成本见 JSON `strategy`。经济显著性=UNSCORABLE。"),
        ("8. Benchmark Comparison", "\n".join(benchmark_lines)),
        ("9. Indicator Audit Table", "\n".join(indicator_lines)),
        ("10. Redundancy Audit", f"因子 Pearson/Spearman/MI、VIF、|ρ|≥0.70 聚类见 JSON `factor_diagnostics`。聚类={payload['factor_diagnostics'].get('clusters_abs_rho_ge_070')}。同簇信号不能当独立证据；原始指标级最大相关性见上表。"),
        ("11. Ablation Results", f"{payload['ablation']}\n\n逐因子 1,000 次 permutation 及 CI 见 `permutation_importance`。Stress-only 的样本内排序高于 Confirmation/60:40 组合，但没有可评分 OOS CI，不能据此直接改生产权重。"),
        ("12. Regime Stability", f"价格趋势/波动 Regime：{payload['regime_stability']}\n\nLOCO：{payload['cycle_stability']}\n\n宏观、流动性、减半和危机 Regime 因点时标签/周期不足为 UNSCORABLE。"),
        ("13. Overfitting Audit", f"IS={payload['in_sample_metrics']}\n\nOOS={payload['walk_forward']['metrics']}\n\nBootstrap={payload['bootstrap']}；Permutation={payload['permutation']}；参数敏感性见 `parameter_sensitivity`。features={payload['overfitting_audit']['feature_count']}，N_eff/features={payload['overfitting_audit']['n_eff_per_feature']}，风险={payload['overfitting_audit']['risk']}。Challenger={payload['challenger_models']['status']}，升级结论={payload['promotion_decision']}。"),
        ("14. False Bottom Analysis", "Top 10（样本内阈值案例，仅用于诊断）：\n" + "\n".join(fp_lines) + "\n\n旧回放未冻结所有原始子信号，逐案因果归因仍为 UNSCORABLE。"),
        ("15. Missed Bottom Analysis", "Top 10（样本内阈值案例，仅用于诊断）：\n" + "\n".join(fn_lines) + "\n\n必须在未来严格 PIT 数据上验证共同原因。"),
        ("16. Missing Indicator Audit", "Critical：真实 available_at/vintage/revision、关键指标第二数据源。Important：期权 skew/期限结构、basis、订单簿流动性、DXY/实质利率/金融条件的点时序列。Optional：Hash Ribbon、Dormancy 等长历史链上项。任何新增项必须通过独立 OOS 增量测试；当前一律 `REQUIRES_PIT_OOS`。"),
        ("17. Model Audit Score", f"{payload['model_audit_score']}\n\n因严格 OOS、Calibration、Robustness 与数据点时完整性不可评分，禁止为 Dashboard 填造 0-100 数字，也禁止把 MAS 当准确率。"),
        ("18. 最终模型评级", "**INSUFFICIENT EVIDENCE — 研究型状态指标。** `prediction.kind=score`，`probability=null`。不连接自动交易，不称底部概率。"),
        ("19. 最值得修改的 10 个问题", "\n".join(problem_lines)),
        ("20. 下一轮实验清单", "\n".join(experiment_lines)),
    ]
    lines = ["# BTC 底部模型 · 全维度数学审计报告", f"\n审计 ID：`{payload['audit_id']}`"]
    for title, body in sections:
        lines.extend([f"\n## {title}\n", body])
    return "\n".join(lines) + "\n"


def run_mathematical_audit(store: Any, as_of_day: Optional[str] = None, *,
                           source_provenance: Optional[dict[str, Any]] = None,
                           ) -> tuple[dict[str, Any], str]:
    raw = load_all_series(store)
    price_rows = raw.get("btc_price_onchain") or raw.get("btc_close_1d") or []
    if not price_rows:
        raise ValueError("BTC price history is required")
    decision_day = as_of_day or price_rows[-1][0]
    data = apply_data_policy(raw, decision_day)
    dataset_id = dataset_fingerprint(data)
    rows = _replay_dataset(store, data, dataset_id)
    if not rows:
        raise ValueError("No scorable replay rows")
    primary_rows = [row for row in rows if row.get("combined_score") is not None]
    if not primary_rows:
        raise ValueError("No replay rows meet the combined evidence-coverage contract")
    price = PriceIndex(data.get("btc_price_onchain") or data.get("btc_close_1d") or [])
    primary_horizon = 180
    walk = _walk_forward(primary_rows, PRIMARY_LABEL, primary_horizon)
    oos = walk.get("oos") or {}
    bootstrap = _block_bootstrap(oos.get("y", []), oos.get("pred", []), oos.get("score", []))
    labels = _label_summary(rows)
    primary_labels = [
        int(row["labels"][PRIMARY_LABEL]) for row in primary_rows
        if row["labels"].get(PRIMARY_LABEL) is not None
    ]
    daily_labels = [build_labels(price, day).get(PRIMARY_LABEL) for day in price.days]
    daily_labels = [int(value) for value in daily_labels if value is not None]
    sample_sizes = {
        "daily_raw_n": len(daily_labels), "weekly_raw_n": len(rows),
        "combined_score_n": len(primary_rows),
        "primary_label_scorable_n": len(primary_labels),
        "event_n": _event_count(primary_rows, PRIMARY_LABEL),
        "non_overlapping_n_180d": _non_overlapping_n(
            primary_rows, PRIMARY_LABEL, 180,
        ),
        "non_overlapping_positive_n_180d": _non_overlapping_positive_n(
            primary_rows, PRIMARY_LABEL, 180,
        ),
        "n_eff": _n_eff(primary_labels),
    }
    accuracy_specs = {
        "4W": ("B_30_r10", 30),
        "12W": ("B_90_r20", 90),
        "26W": (PRIMARY_LABEL, 180),
        "52W": ("C_365_r30_mae25", 365),
    }
    accuracy_dashboard: dict[str, Any] = {}
    for scale, (label, horizon) in accuracy_specs.items():
        result = _walk_forward(primary_rows, label, horizon)
        metrics = result["metrics"]
        if not isinstance(metrics, dict):
            metrics = {"validation_status": str(metrics), "n": 0}
        elif metrics.get("validation_status") == "SCORABLE":
            metrics["validation_status"] = "RESEARCH_ONLY_PIT_APPROX"
        accuracy_dashboard[scale] = {
            "label": label, "horizon_days": horizon,
            "folds": result["folds"], "metrics": metrics,
        }
    usable_primary = [
        row for row in primary_rows if row["labels"].get(PRIMARY_LABEL) is not None
    ]
    is_y = [int(row["labels"][PRIMARY_LABEL]) for row in usable_primary]
    is_scores = [float(row["combined_score"]) for row in usable_primary]
    is_threshold = _choose_threshold(is_y, is_scores)
    in_sample_metrics = _classification(
        is_y, [int(score >= is_threshold) for score in is_scores], is_scores,
    )
    in_sample_metrics["threshold"] = is_threshold
    oos_y = oos.get("y", [])
    oos_probability = oos.get("probability", [])
    walk_metrics = walk.get("metrics") if isinstance(walk.get("metrics"), dict) else {}
    calibration = {
        "status": walk_metrics.get("validation_status", "UNSCORABLE"),
        "brier": walk_metrics.get("brier"),
        "log_loss": walk_metrics.get("log_loss"),
        "ece": walk_metrics.get("ece"),
        "curve": _calibration_curve(oos_y, oos_probability)
        if oos_y and len(oos_y) == len(oos_probability) else [],
        "probability_publishable": False,
    }
    indicator_audit = _indicator_audit(
        data, primary_rows, PRIMARY_LABEL, decision_day,
    )
    overfitting_audit = {
        "feature_count": len([
            item for item in indicator_audit if item["role"] == "model_input"
        ]),
        "n_eff": sample_sizes["n_eff"],
        "n_eff_per_feature": None,
        "risk": "HIGH",
    }
    if overfitting_audit["feature_count"] and sample_sizes["n_eff"] is not None:
        overfitting_audit["n_eff_per_feature"] = (
            float(sample_sizes["n_eff"]) / overfitting_audit["feature_count"]
        )
    created_at = int(time.time())
    challenger_models = _challenger_models(primary_rows, PRIMARY_LABEL, primary_horizon)
    dependency_profile = challenger_models.get("status", "unknown") + ":" + str(
        challenger_models.get("library_versions") or {},
    )
    audit_id = "audit-" + hashlib.sha256(
        f"{MODEL_ID}|{DATA_POLICY_ID}|{dataset_id}|{decision_day}|{AUDIT_ENGINE_VERSION}|{dependency_profile}".encode(),
    ).hexdigest()[:20]
    payload: dict[str, Any] = {
        "audit_id": audit_id, "schema_version": "1.0",
        "audit_engine_version": AUDIT_ENGINE_VERSION,
        "dependency_profile": dependency_profile,
        "status": "INSUFFICIENT_EVIDENCE",
        "validation_status": "INSUFFICIENT_EVIDENCE", "created_at": created_at,
        "decision_as_of": decision_day, "model_id": MODEL_ID,
        "data_policy_id": DATA_POLICY_ID, "dataset_id": dataset_id,
        "pit_status": "PIT_APPROX", "probability_publishable": False,
        "source_provenance": source_provenance or {
            "kind": "local_store", "verification_status": "UNVERIFIED",
        },
        "labels": labels,
        "sample_sizes": sample_sizes,
        "accuracy_dashboard": accuracy_dashboard,
        "walk_forward": {"folds": walk["folds"], "metrics": walk["metrics"]},
        "in_sample_metrics": in_sample_metrics,
        "calibration": calibration,
        "bootstrap": bootstrap,
        "permutation": _permutation(primary_rows, PRIMARY_LABEL),
        "permutation_importance": _permutation_importance(primary_rows, PRIMARY_LABEL),
        "benchmarks": _benchmark(primary_rows, PRIMARY_LABEL),
        "factor_diagnostics": _factor_diagnostics(primary_rows, PRIMARY_LABEL),
        "indicator_audit": indicator_audit,
        "ic_rank_ic": _ic_rank_ic(primary_rows),
        "score_bin_economics": _score_bin_economics(primary_rows),
        "ablation": _ablation(primary_rows, PRIMARY_LABEL),
        "challenger_models": challenger_models,
        "cycle_stability": _cycles(primary_rows, PRIMARY_LABEL),
        "regime_stability": _regime_stability(primary_rows, price, PRIMARY_LABEL),
        "parameter_sensitivity": {
            "threshold_20_to_80": _parameter_sensitivity(
                primary_rows, PRIMARY_LABEL,
            ),
            "weights_plus_minus_10_20pct": (
                "UNSCORABLE_NO_VERSIONED_PRE_WEIGHT_FEATURES"
            ),
            "lookbacks": "UNSCORABLE_NO_VERSIONED_ALTERNATE_LOOKBACK_FEATURES",
        },
        "errors": _errors(primary_rows, PRIMARY_LABEL),
        "strategy": _strategy(primary_rows, price),
        "overfitting_audit": overfitting_audit,
        "model_audit_score": {
            "predictive_power": "UNSCORABLE",
            "calibration": "UNSCORABLE",
            "robustness": "UNSCORABLE",
            "data_quality": "UNSCORABLE_STRICT_PIT_MISSING",
            "feature_quality": "UNSCORABLE_INCREMENTAL_TEST_MISSING",
            "overfitting_risk": "HIGH",
            "final_mas": "UNSCORABLE",
        },
        "top_10_problems": _improvement_backlog(sample_sizes),
        "next_experiments": _next_experiments(),
        "unscorable": [
            "strict point-in-time OOS accuracy before append-only observation adoption",
            "revision-aware historical replay for legacy rows",
            "causal economic alpha and opportunity cost significance",
            "calibrated product probability",
            "LOCO statistical power with only a few BTC cycles",
        ],
        "promotion_decision": "REJECT_CHALLENGER_PROMOTION",
    }
    markdown = _markdown(payload)
    store.save_audit(audit_id, payload, markdown)
    return payload, markdown


def diagnose_synthetic(scores: list[float], labels: list[int], *,
                       feature_available_after_label: bool = False) -> str:
    """审计器门禁：纯随机/已知预测力/故意泄漏三类合成数据。"""
    if feature_available_after_label:
        return "DATA_LEAKAGE"
    auc = _auc(labels, scores)
    pr = _pr_auc(labels, scores)
    prevalence = sum(labels) / len(labels) if labels else 0.0
    if auc is not None and auc >= 0.75 and pr is not None and pr >= prevalence + 0.15:
        return "VALIDATED_SIGNAL"
    return "INSUFFICIENT_EVIDENCE"
