from processors.market_risk_anomaly import RollingPitAnomalyNormalizer
from storage.market_risk_store import MarketRiskStore


def _fact(value: float, as_of: int) -> dict:
    return {"metric": {"label": "测试", "value": value, "as_of": as_of, "direction": "up"}}


def test_duplicate_as_of_is_not_resampled_and_survives_restart(tmp_path) -> None:
    store = MarketRiskStore(str(tmp_path))
    normalizer = RollingPitAnomalyNormalizer(store, min_samples=30, min_span_sec=3600)
    normalizer.evaluate("BTC", _fact(1.0, 100), 100)
    duplicate = normalizer.evaluate("BTC", _fact(2.0, 100), 101)[0]
    assert duplicate["sample_count"] == 1

    restarted = RollingPitAnomalyNormalizer(store, min_samples=30, min_span_sec=3600)
    after_restart = restarted.evaluate("BTC", _fact(3.0, 200), 200)[0]
    assert after_restart["sample_count"] == 1
    store.close()


def test_future_and_non_monotonic_samples_are_rejected(tmp_path) -> None:
    store = MarketRiskStore(str(tmp_path))
    normalizer = RollingPitAnomalyNormalizer(store)
    assert normalizer.evaluate("BTC", _fact(1.0, 101), 100)[0]["reason"] == "pit_rejected"
    normalizer.evaluate("BTC", _fact(1.0, 100), 100)
    assert normalizer.evaluate("BTC", _fact(1.0, 99), 101)[0]["reason"] == "non_monotonic"
    store.close()


def test_flat_baseline_never_claims_extreme(tmp_path) -> None:
    store = MarketRiskStore(str(tmp_path))
    normalizer = RollingPitAnomalyNormalizer(store, min_samples=30, min_span_sec=3600)
    for index in range(30):
        normalizer.evaluate("BTC", _fact(1.0, 100 + index * 130), 100 + index * 130)
    result = normalizer.evaluate("BTC", _fact(1.0, 4_100), 4_100)[0]
    assert result["status"] == "baseline_flat"
    assert result["robust_z"] is None
    store.close()


def test_non_finite_value_is_rejected(tmp_path) -> None:
    store = MarketRiskStore(str(tmp_path))
    normalizer = RollingPitAnomalyNormalizer(store)
    result = normalizer.evaluate("BTC", _fact(float("nan"), 100), 100)[0]
    assert result["reason"] == "non_finite_value"
    store.close()
