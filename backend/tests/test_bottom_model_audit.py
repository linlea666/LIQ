"""离线数学审计器的合成数据门禁。"""

from __future__ import annotations

import os
import random
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from processors.bottom_model.audit import _first_crossings, _pr_auc, diagnose_synthetic


def test_audit_rejects_random_data():
    rng = random.Random(7)
    labels = [rng.randrange(2) for _ in range(500)]
    scores = [rng.random() for _ in labels]
    assert diagnose_synthetic(scores, labels) == "INSUFFICIENT_EVIDENCE"


def test_audit_accepts_known_predictive_signal():
    labels = [0, 1] * 250
    scores = [0.1 if label == 0 else 0.9 for label in labels]
    assert diagnose_synthetic(scores, labels) == "VALIDATED_SIGNAL"


def test_audit_detects_declared_future_feature():
    labels = [0, 1] * 100
    assert diagnose_synthetic([float(x) for x in labels], labels, feature_available_after_label=True) == "DATA_LEAKAGE"


def test_pr_auc_constant_score_equals_prevalence():
    labels = [0, 1, 0, 0, 1]
    assert _pr_auc(labels, [0.0] * len(labels)) == sum(labels) / len(labels)


def test_strategy_events_use_first_threshold_crossing_only():
    rows = [
        {"day": f"2026-01-{day:02d}", "score": score}
        for day, score in enumerate((50, 70, 80, None, 60, 70), start=1)
    ]
    selected = _first_crossings(rows, "score", 65.0)
    assert [row["day"] for row in selected] == ["2026-01-02", "2026-01-06"]
