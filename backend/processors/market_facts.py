"""Neutral facade for deterministic market facts shared outside the AI analyzer.

The implementation remains single-source with Market Action's mature builders;
consumers import this module so they do not depend on reports, prompts or AI state.
"""

from __future__ import annotations

from processors.market_action.facts_collector import (
    build_cvd_snapshot,
    build_funding_snapshot,
    build_oi_snapshot,
    build_price_snapshot,
)

__all__ = [
    "build_cvd_snapshot",
    "build_funding_snapshot",
    "build_oi_snapshot",
    "build_price_snapshot",
]
