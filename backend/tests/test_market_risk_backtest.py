from __future__ import annotations

from processors.market_risk_backtest import (
    BaselineFeatureRow,
    IncidentSignal,
    PricePoint,
    admission_gates,
    fit_training_calibration,
    label_market_episodes,
    match_incidents_once,
    paired_bootstrap_delta_ci,
    purged_walk_forward,
    select_at_equal_alert_burden,
    stratified_binary_report,
)

import pytest


def test_episode_label_uses_onset_and_threshold_not_peak_for_lead() -> None:
    points = [
        PricePoint(0, 100), PricePoint(600, 101.3), PricePoint(1200, 105),
        PricePoint(1800, 110), PricePoint(2400, 107), PricePoint(3000, 105),
        PricePoint(3600, 105), PricePoint(9000, 105),
    ]
    events = label_market_episodes(
        points, coin="BTC", threshold_pct=5, forward_window_sec=3600,
        quiet_split_sec=7200,
    )
    assert len(events) == 1
    event = events[0]
    assert event.onset == 600
    assert event.threshold_time == 1200
    assert event.peak == 1800
    assert event.end >= 3000  # 50% 回撤完成后才切分

    matches = match_incidents_once([
        IncidentSignal("inc", "up", 300, "warning"),
        IncidentSignal("inc", "up", 400, "critical"),
    ], events)
    assert len(matches) == 1
    assert matches[0].lead_to_onset_sec == 300
    assert matches[0].lead_to_threshold_sec == 900


def test_purge_removes_overlapping_training_labels_and_applies_embargo() -> None:
    events = label_market_episodes([
        PricePoint(0, 100), PricePoint(100, 106), PricePoint(200, 100),
        PricePoint(1000, 100), PricePoint(1100, 106), PricePoint(1200, 100),
        PricePoint(2000, 100), PricePoint(2100, 106), PricePoint(2200, 100),
    ], coin="BTC", threshold_pct=5, forward_window_sec=150, quiet_split_sec=100)
    folds = purged_walk_forward(
        events, validation_windows=[(1000, 1300)], embargo_sec=900,
    )
    assert len(folds) == 1
    assert folds[0].validation_indices
    assert all(events[index].end < 100 for index in folds[0].train_indices)


def test_incremental_gate_requires_ci_strictly_above_zero() -> None:
    delta = paired_bootstrap_delta_ci([1] * 60, [0] * 60, samples=500)
    assert delta[1] > 0
    result = admission_gates(
        warning_tp=40, warning_total=60,
        critical_tp=25, critical_total=32,
        recall_hits=24, qualifying_events=35,
        false_warning_per_day=0.5, false_critical_per_14d=1.0,
        core_coverage=0.95, incremental_delta_ci=delta,
    )
    assert result["admitted"] is True


def _training_rows(count: int = 60) -> list[dict[str, float]]:
    return [
        {
            "decision_time": 1_700_000_000 + index * 60,
            "spot_taker_imbalance": 0.01 + index / 1000,
            "spot_quote_usd": 1_000_000 + index * 10_000,
            "oi_change_1h_pct": 0.1 + index / 100,
            "funding_rate": 0.0001 + index / 1_000_000,
            "liquidation_1h_usd": 1_000_000 + index * 100_000,
            "liquidation_density_usd": 5_000_000 + index * 200_000,
            "wall_attack": 0.1 + index / 1000,
            "price_move_5m_pct": 0.1 + index / 100,
            "full_engine_confidence": 0.2 + index / 1000,
            "volume_z": 0.5 + index / 100,
            "spot_cvd_z": 0.5 + index / 100,
        }
        for index in range(count)
    ]


def test_training_calibration_is_frozen_versioned_and_pit_guarded() -> None:
    rows = _training_rows()
    artifact = fit_training_calibration(rows, min_samples=50, created_at="2026-01-01T00:00:00Z")
    assert artifact.status == "frozen_training"
    assert artifact.admitted_for_production is False
    assert artifact.calibration_version.startswith("market-risk-cal-")
    assert artifact.training_window["sample_count"] == 60
    assert artifact.thresholds["spot_taker_imbalance_extreme"] >= artifact.thresholds[
        "spot_taker_imbalance_early"
    ]
    assert set(artifact.baseline_thresholds) == {
        "price_breakout_abs_pct", "volume_z", "spot_cvd_abs_z",
        "oi_abs_change_1h_pct", "liquidation_1h_usd",
    }

    leaked = _training_rows()
    leaked[0]["entity_known_at"] = leaked[0]["decision_time"] + 1
    with pytest.raises(ValueError, match="point-in-time violation"):
        fit_training_calibration(leaked, min_samples=50)
    with pytest.raises(ValueError, match="forbidden forensic sample"):
        fit_training_calibration(
            rows, min_samples=50,
            forbidden_intervals=[(rows[0]["decision_time"], rows[0]["decision_time"] + 1)],
        )


def test_fixed_baselines_use_equal_alert_burden() -> None:
    thresholds = {
        "price_breakout_abs_pct": 1.0, "volume_z": 2.0,
        "spot_cvd_abs_z": 2.0, "oi_abs_change_1h_pct": 1.0,
        "liquidation_1h_usd": 10.0,
    }
    rows = [
        BaselineFeatureRow(
            decision_time=100 + index, price_return_pct=2.0 + index,
            volume_z=3.0, spot_cvd_z=3.0, oi_change_1h_pct=2.0,
            liquidation_1h_usd=20.0, liquidation_direction="up",
            full_engine_score=1.5,
        )
        for index in range(3)
    ]
    selected = select_at_equal_alert_burden(rows, thresholds, alerts_per_baseline=2)
    assert set(selected) == {
        "price_breakout_volume", "spot_cvd_extreme", "oi_liquidation", "full_engine",
    }
    assert all(len(signals) == 2 for signals in selected.values())


def test_stratified_report_preserves_required_dimensions() -> None:
    report = stratified_binary_report([
        {
            "volatility_regime": "high", "market_regime": "trend",
            "session": "US", "data_quality": "normal", "hit": True,
        },
        {
            "volatility_regime": "high", "market_regime": "range",
            "session": "Asia", "data_quality": "data_degraded", "hit": False,
        },
    ], outcome_key="hit")
    assert report["volatility_regime"]["high"] == {"count": 2.0, "rate": 0.5}
    assert report["session"]["US"]["rate"] == 1.0
