from __future__ import annotations

from pathlib import Path


def test_smc_processor_does_not_reference_existing_opinion_layers():
    src = Path(__file__).resolve().parents[1] / "processors" / "smc.py"
    text = src.read_text(encoding="utf-8")
    forbidden = [
        "key_level_snapshot_v2",
        "trading_brain",
        "market_action_report",
        "orderbook_pressure_snapshot",
        "market_structure",
    ]
    for name in forbidden:
        assert name not in text
