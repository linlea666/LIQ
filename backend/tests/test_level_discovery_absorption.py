"""
Level Discovery · Absorption 分支单元测试

覆盖点：
1. `discover_levels` 签名只接受 `absorption`，**不再**接受 `orderbook`。
2. AbsorptionSnapshot.zones_support / zones_resistance → RawCandidate
   落在 `capital_flow` 维度，source_tag 为 `absorption_zone_*`。
3. 评分：bar_count 更多、volume 更大、delta 更纯粹 → 分更高；fallback_used → 折扣。
4. age_hours 衰减生效。
5. 距离现价过远（>15%）的 zone 被丢弃。
6. 空 absorption 不产生候选，也不报错。
"""
from __future__ import annotations

import inspect

import pytest

from models.market_action import AbsorptionSnapshot, AbsorptionZone
from processors.level_discovery import discover_levels


def _make_snap(
    supports=None,
    resistances=None,
    fallback_used: bool = False,
) -> AbsorptionSnapshot:
    return AbsorptionSnapshot(
        zones_support=list(supports or []),
        zones_resistance=list(resistances or []),
        total_zone_count=(len(supports or []) + len(resistances or [])),
        window_hours=3.0,
        lookback_bars=3,
        fallback_used=fallback_used,
    )


def _zone(
    price: float,
    side: str = "support",
    vol: float = 10_000_000,
    d: float = 0.10,
    bars: int = 1,
    age: float = 0.5,
) -> AbsorptionZone:
    return AbsorptionZone(
        price=price,
        side=side,  # type: ignore[arg-type]
        taker_volume_usd=vol,
        delta_pct_abs_avg=d,
        bar_count=bars,
        age_hours=age,
        source="contract",
    )


def test_signature_no_orderbook_has_absorption():
    sig = inspect.signature(discover_levels)
    assert "absorption" in sig.parameters
    assert "orderbook" not in sig.parameters


def test_absorption_zones_become_capital_flow_candidates():
    snap = _make_snap(
        supports=[_zone(76500, "support", vol=12_000_000, d=0.05, bars=2, age=0.5)],
        resistances=[_zone(79500, "resistance", vol=8_000_000, d=0.10, bars=1, age=1.0)],
    )
    r = discover_levels(current_price=78000, atr=500, absorption=snap)
    tags = {c.source_tag for c in r.candidates}
    assert "absorption_zone_support" in tags
    assert "absorption_zone_resistance" in tags
    # 不应再出现订单墙 tag
    assert "orderbook_bid_wall" not in tags
    assert "orderbook_ask_wall" not in tags


def test_absorption_scoring_bar_count_and_purity():
    """bar_count=3 + 纯 delta 应该比 bar_count=1 + 粗 delta 得分高。"""
    strong = _make_snap(supports=[_zone(76500, vol=10_000_000, d=0.02, bars=3, age=0.1)])
    weak = _make_snap(supports=[_zone(76500, vol=10_000_000, d=0.25, bars=1, age=0.1)])

    r_strong = discover_levels(current_price=78000, atr=500, absorption=strong)
    r_weak = discover_levels(current_price=78000, atr=500, absorption=weak)

    s_strong = next(c.base_score for c in r_strong.candidates if c.source_tag == "absorption_zone_support")
    s_weak = next(c.base_score for c in r_weak.candidates if c.source_tag == "absorption_zone_support")
    assert s_strong > s_weak


def test_absorption_fallback_applies_discount():
    z = _zone(76500, vol=10_000_000, d=0.05, bars=2, age=0.2)
    normal = _make_snap(supports=[z], fallback_used=False)
    fb = _make_snap(supports=[z], fallback_used=True)

    r_norm = discover_levels(current_price=78000, atr=500, absorption=normal)
    r_fb = discover_levels(current_price=78000, atr=500, absorption=fb)

    s_norm = next(c.base_score for c in r_norm.candidates if c.source_tag == "absorption_zone_support")
    s_fb = next(c.base_score for c in r_fb.candidates if c.source_tag == "absorption_zone_support")
    # 0.7 折扣 ± 浮点容差
    assert s_fb == pytest.approx(s_norm * 0.7, rel=1e-3)


def test_absorption_age_decay():
    fresh = _make_snap(supports=[_zone(76500, vol=10_000_000, d=0.05, bars=1, age=0.0)])
    old = _make_snap(supports=[_zone(76500, vol=10_000_000, d=0.05, bars=1, age=2.5)])
    r_fresh = discover_levels(current_price=78000, atr=500, absorption=fresh)
    r_old = discover_levels(current_price=78000, atr=500, absorption=old)
    s_fresh = next(c.base_score for c in r_fresh.candidates if c.source_tag == "absorption_zone_support")
    s_old = next(c.base_score for c in r_old.candidates if c.source_tag == "absorption_zone_support")
    assert s_fresh > s_old


def test_absorption_far_zone_dropped():
    # 现价 78000, 远离 15% 的 zone（60000）应被丢弃
    snap = _make_snap(supports=[_zone(60000, vol=20_000_000, d=0.03, bars=3, age=0.1)])
    r = discover_levels(current_price=78000, atr=500, absorption=snap)
    tags = [c.source_tag for c in r.candidates]
    assert "absorption_zone_support" not in tags


def test_absorption_none_is_safe():
    r = discover_levels(current_price=78000, atr=500, absorption=None)
    tags = [c.source_tag for c in r.candidates]
    assert "absorption_zone_support" not in tags
    assert "absorption_zone_resistance" not in tags


def test_absorption_empty_snapshot_safe():
    snap = _make_snap()
    r = discover_levels(current_price=78000, atr=500, absorption=snap)
    tags = [c.source_tag for c in r.candidates]
    assert "absorption_zone_support" not in tags
    assert "absorption_zone_resistance" not in tags
