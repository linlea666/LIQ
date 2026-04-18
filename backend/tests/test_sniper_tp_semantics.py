"""验证 Sniper TP1/TP2 语义交换：TP1=近目标、TP2=远目标。

对应的 prompt 层 "R:R 达标判定以 TP2 为准" 语义约束也在 test_ai_prompt_sniper_tp.py 里独立测试。
"""
from __future__ import annotations

import pytest

from models.liquidation import LiquidationMap, LiqCluster, VacuumZone
from models.levels import PriceLevel
from processors.levels import _calc_sniper_entries


def _build_liq_map(entry_long_cluster: float, above_cluster: float) -> LiquidationMap:
    """构造做多 sniper 场景：下方 long 清算簇 + 上方 short 清算簇 + 紧邻真空区。"""
    return LiquidationMap(
        coin="BTC",
        ts=0,
        cycle="1d",
        leverage_groups=[],
        clusters_below=[
            LiqCluster(
                price_center=entry_long_cluster,
                price_from=entry_long_cluster - 100,
                price_to=entry_long_cluster + 100,
                total_usd=50_000_000,
                side="long",
                distance_pct=1.5,
            ),
        ],
        clusters_above=[
            LiqCluster(
                price_center=above_cluster,
                price_from=above_cluster - 100,
                price_to=above_cluster + 100,
                total_usd=40_000_000,
                side="short",
                distance_pct=2.0,
            ),
        ],
        vacuum_zones=[
            # 紧邻 cluster 下方的小真空区，使 risk 较小
            VacuumZone(
                price_from=entry_long_cluster - 600,
                price_to=entry_long_cluster - 200,
            ),
        ],
    )


def test_long_sniper_tp1_is_nearer_than_tp2():
    """做多方向：TP1（近目标）必须比 TP2（远目标）更接近 entry。"""
    current_price = 77000
    liq_map = _build_liq_map(entry_long_cluster=76500, above_cluster=77200)
    atr = 100.0  # 小 ATR → 小 risk → 近目标 RR 也能 ≥ 1.5

    entries = _calc_sniper_entries(
        current_price=current_price,
        liq_map=liq_map,
        atr=atr,
        supports=[],
        resistances=[],
        vp=None,
    )
    long_entries = [e for e in entries if e.direction == "long"]
    assert long_entries, "至少应生成一个做多狙击方案"

    for se in long_entries:
        assert se.entry_price < se.take_profit_1 < se.take_profit_2, (
            f"TP1 必须在 TP2 之前（近目标更接近 entry）"
            f"但收到 entry={se.entry_price} TP1={se.take_profit_1} TP2={se.take_profit_2}"
        )
        # TP2 = 远目标 = 必须满足 min_sniper_rr (2.5) 硬约束
        assert se.rr_ratio_2 >= 2.5, f"TP2 远目标 RR 应 ≥ 2.5，收到 {se.rr_ratio_2}"
        # TP1 = 近目标 = 至少 1.5 质量门
        assert se.rr_ratio_1 >= 1.5, f"TP1 近目标 RR 应 ≥ 1.5，收到 {se.rr_ratio_1}"
        # 近目标 RR 不应大于远目标 RR（如果相等则说明兜底生效）
        assert se.rr_ratio_1 <= se.rr_ratio_2 + 0.1, "近目标 RR 不应超过远目标 RR"


def test_short_sniper_tp1_is_nearer_than_tp2():
    """做空方向：TP1（近目标）价格 > TP2（远目标）价格。因为做空方向 TP 在下方。"""
    current_price = 77000
    liq_map = LiquidationMap(
        coin="BTC", ts=0, cycle="1d",
        leverage_groups=[],
        clusters_above=[
            LiqCluster(
                price_center=77500, price_from=77400, price_to=77600,
                total_usd=50_000_000, side="short", distance_pct=0.7,
            ),
        ],
        clusters_below=[
            LiqCluster(
                price_center=76800, price_from=76700, price_to=76900,
                total_usd=40_000_000, side="long", distance_pct=0.3,
            ),
        ],
        vacuum_zones=[
            VacuumZone(price_from=77800, price_to=78200),
        ],
    )
    entries = _calc_sniper_entries(
        current_price=current_price,
        liq_map=liq_map,
        atr=100,
        supports=[],
        resistances=[],
        vp=None,
    )
    short_entries = [e for e in entries if e.direction == "short"]
    assert short_entries, "至少应生成一个做空狙击方案"

    for se in short_entries:
        assert se.entry_price > se.take_profit_1 > se.take_profit_2, (
            f"做空方向 TP1 必须比 TP2 更接近 entry（在 entry 下方更近的一档）"
            f"但收到 entry={se.entry_price} TP1={se.take_profit_1} TP2={se.take_profit_2}"
        )
        assert se.rr_ratio_2 >= 2.5
        assert se.rr_ratio_1 >= 1.5
