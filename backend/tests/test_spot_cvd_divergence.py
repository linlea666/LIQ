from models.flow import CVDData, CVDPoint
from processors.cvd import compute_cvd_price_divergence, detect_cvd_price_divergence


def test_pure_divergence_helper_does_not_mutate_input_and_wrapper_remains_compatible():
    base = 1_700_000_000_000
    points = [
        CVDPoint(ts=base + i * 300_000, buy_vol=1, sell_vol=1, delta=0, cvd=100 - i)
        for i in range(24)
    ]
    cvd = CVDData(coin="BTC", inst_type="SPOT", series=points)
    prices = [100, 99, 98, 95]
    timestamps = [base, base + 1_800_000, base + 3_600_000, base + 6_900_000]

    result = compute_cvd_price_divergence(cvd, prices, timestamps)
    assert cvd.has_divergence is False
    assert result.has_divergence is False  # CVD同步创新低，不构成底背离

    for i, point in enumerate(points[12:]):
        point.cvd = 90 + i
    result = compute_cvd_price_divergence(cvd, prices, timestamps)
    assert result.has_divergence is True
    assert "底背离" in result.note
    assert cvd.has_divergence is False

    mutated = detect_cvd_price_divergence(cvd, prices, timestamps)
    assert mutated is cvd
    assert cvd.has_divergence is True
