from processors.spot_accumulation_forward import build_forward_report


DAY = 86_400


def _record(day: int, price: float, status: str = "invalidated") -> dict:
    metrics = {
        name: {"included_in_score": True}
        for name in (
            "etf_flow_5d_usd", "exchange_balance_7d_pct", "spot_netflow_24h_usd",
            "stablecoin_change_7d_pct", "coinbase_premium", "spot_cvd_delta_1h",
            "spot_taker_delta_1h", "footprint_absorption", "persistent_spot_wall",
            "coinbase_confluence", "key_level_reclaimed",
        )
    }
    return {
        "archive_schema_version": 2,
        "record_type": "spot_accumulation_full_fact_snapshot",
        "timestamp": 1_700_000_000 + day * DAY,
        "policy_version": 3,
        "facts": {
            "price": price,
            "metric_facts": metrics,
            "data_quality": {
                "layer_quality": {
                    "capital_flow": {"passed": True},
                    "acceptance": {"passed": True},
                },
            },
        },
        "opportunities": [{
            "opportunity_id": "op-1",
            "stage": "insurance",
            "status": status,
            "policy_version": 3,
            "blocked_by": ["CVD过期"] if status == "invalidated" else [],
        }],
        "blocking_reasons": ["CVD过期"] if status == "invalidated" else [],
    }


def test_forward_report_uses_real_future_windows_and_deduplicates_opportunity():
    records = [{"timestamp": 1_699_000_000, "price": 90, "scores": {}}]
    for day in range(91):
        price = 80 if day == 3 else 100 + day
        status = "eligible" if day == 0 else "invalidated"
        records.append(_record(day, price, status))
    report = build_forward_report(records)
    assert report["label"] == "M/A前向验证"
    assert report["legacy_record_count"] == 1
    assert report["opportunity_count"] == 1
    outcome = report["outcomes"][0]
    assert outcome["return_7d_pct"] == 7.0
    assert outcome["return_30d_pct"] == 30.0
    assert outcome["return_90d_pct"] == 90.0
    assert outcome["max_drawdown_90d_pct"] == -20.0
    assert outcome["terminal_status"] == "invalidated"
    assert outcome["terminal_reason"] == "CVD过期"
    assert outcome["ma_coverage_ratio"] == 1.0
    assert report["ma_layers_ready_rate"] == 1.0
    assert report["invalidation_reasons"] == {"CVD过期": 1}


def test_forward_report_does_not_invent_missing_horizon_prices():
    records = [
        {"timestamp": 1, "facts": "invalid", "opportunities": {}},
        _record(0, 100, "eligible"),
        _record(1, 95, "invalidated"),
    ]
    report = build_forward_report(records)
    outcome = report["outcomes"][0]
    assert outcome["return_7d_pct"] is None
    assert outcome["return_30d_pct"] is None
    assert outcome["return_90d_pct"] is None
    assert report["horizons"]["7d"]["sample_count"] == 0
