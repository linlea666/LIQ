from __future__ import annotations

import pytest

from api.routes_smc import _probe_flow_summary
from scripts.nansen_flow_probe import _flow_summary


ROWS_WITH_SIGNED_OUTFLOWS = [
    {
        "date": "2026-05-15T01:00:00",
        "price_usd": 100.0,
        "total_inflows_cex": 10.0,
        "total_outflows_cex": -3.0,
        "total_inflows_dex": 5.0,
        "total_outflows_dex": -2.0,
    },
    {
        "date": "2026-05-15T02:00:00",
        "price_usd": 110.0,
        "total_inflows_cex": 2.0,
        "total_outflows_cex": -15.0,
        "total_inflows_dex": 4.0,
        "total_outflows_dex": -1.0,
    },
]


@pytest.mark.parametrize("summary_fn", [_probe_flow_summary, _flow_summary])
def test_nansen_probe_summary_handles_signed_outflows(summary_fn):
    summary = summary_fn(ROWS_WITH_SIGNED_OUTFLOWS)

    assert summary["cex_in_token"] == 12.0
    assert summary["cex_out_token_raw"] == -18.0
    assert summary["cex_out_token_abs"] == 18.0
    assert summary["cex_net_token"] == -6.0
    assert summary["cex_net_usd_approx"] == -660.0
    assert summary["dex_net_token"] == 6.0
    assert summary["dex_net_usd_approx"] == 660.0


@pytest.mark.parametrize("summary_fn", [_probe_flow_summary, _flow_summary])
def test_nansen_probe_summary_handles_positive_outflow_magnitudes(summary_fn):
    rows = [
        {
            "date": "2026-05-15T01:00:00",
            "price_usd": 100.0,
            "total_inflows_cex": 10.0,
            "total_outflows_cex": 3.0,
        }
    ]

    summary = summary_fn(rows)

    assert summary["cex_out_token_raw"] == 3.0
    assert summary["cex_net_token"] == 7.0
    assert summary["cex_net_usd_approx"] == 700.0
