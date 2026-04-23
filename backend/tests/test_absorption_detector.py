"""Absorption Detector 单元测试

覆盖要点：
- 基础检测（高 vol + 低 |delta_pct| 命中）
- 阈值过滤（|delta_pct| 超阈值不命中、vol < $500k 不命中）
- 跨 bar 合并（同价位跨多根 bar → bar_count 增加）
- side 判定（相对 current_price 的上下）
- 兜底机制（保守无命中 → 放宽 + fallback_used）
- 现货兜底（合约空 → 走 spot）
- 空数据（双源皆空 → 空 snapshot）
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from processors.absorption_detector import detect_absorption_zones


def _bucket(price_lo: float, price_hi: float, buy_quote: float, sell_quote: float) -> dict:
    return {
        "price_lo": price_lo,
        "price_hi": price_hi,
        "buy_base": 0.0,
        "sell_base": 0.0,
        "buy_quote": buy_quote,
        "sell_quote": sell_quote,
        "buy_trades": 0,
        "sell_trades": 0,
    }


def _bar(ts: int, buckets: list[dict]) -> dict:
    return {"ts": ts, "buckets": buckets}


# ─────────────────────────────────────────────────────────────────────────────
# 1. 基础检测：保守阈值下命中 support + resistance
# ─────────────────────────────────────────────────────────────────────────────

def test_basic_detection_support_and_resistance():
    now = 1_700_000_000
    current_price = 78_000.0

    # 一根 bar，5 个 buckets：
    # - 76_500 (下方) · vol=$12M · |delta|=0.05 → 命中 support
    # - 77_500 (下方) · vol=$2M  · |delta|=0.10 → vol 不够 top 20%（5 个取 top 1）
    # - 78_500 (上方) · vol=$10M · |delta|=0.08 → top 20% 只取 1 个，这个拿不到
    # 调整：只放 3 个候选（top 20% = 1），确保 76_500 入选
    bar = _bar(now, [
        _bucket(76_400, 76_600, 6_000_000, 6_000_000),   # vol=$12M, |delta|=0.0
        _bucket(77_400, 77_600, 1_000_000, 1_000_000),   # vol=$2M
        _bucket(79_400, 79_600, 500_000, 500_000),       # vol=$1M
    ])
    snap = detect_absorption_zones(
        footprint_contract=[bar],
        footprint_spot=None,
        current_price=current_price,
        now_ts=now,
    )
    assert snap.total_zone_count >= 1
    assert len(snap.zones_support) == 1
    assert snap.zones_support[0].price == 76_500.0
    assert snap.zones_support[0].side == "support"
    assert snap.zones_support[0].taker_volume_usd == 12_000_000.0
    assert snap.zones_support[0].delta_pct_abs_avg == 0.0
    assert snap.zones_support[0].bar_count == 1
    assert snap.zones_support[0].source == "contract"
    assert snap.fallback_used is False


# ─────────────────────────────────────────────────────────────────────────────
# 2. 阈值过滤：|delta_pct| 过大 → 不命中（保守 0.20 门槛）
# ─────────────────────────────────────────────────────────────────────────────

def test_strict_threshold_filters_one_sided():
    now = 1_700_000_000
    bar = _bar(now, [
        # vol $10M 但 |delta|=(10-0)/10=1.0，严重单边 → 不算吸收
        _bucket(77_000, 77_100, 10_000_000, 0),
        _bucket(76_000, 76_100, 200_000, 200_000),
    ])
    snap = detect_absorption_zones(
        footprint_contract=[bar],
        footprint_spot=None,
        current_price=78_000,
        now_ts=now,
    )
    # 单边 bucket 被过滤；第二个 vol 过小（$400k < $500k）→ 无命中 → 走 fallback
    # fallback 阈值 0.30，单边 |delta|=1.0 仍超 → 仍无命中
    assert snap.total_zone_count == 0


# ─────────────────────────────────────────────────────────────────────────────
# 3. 绝对 vol 下限：即便 top 20% + 低 delta，vol < $500k 也过滤
# ─────────────────────────────────────────────────────────────────────────────

def test_absolute_min_vol_filter():
    now = 1_700_000_000
    bar = _bar(now, [
        # vol $100k，|delta|=0 完美吸收特征，但 vol 不够
        _bucket(77_000, 77_100, 50_000, 50_000),
    ])
    snap = detect_absorption_zones(
        footprint_contract=[bar],
        footprint_spot=None,
        current_price=78_000,
        now_ts=now,
    )
    assert snap.total_zone_count == 0


# ─────────────────────────────────────────────────────────────────────────────
# 4. 跨 bar 合并：同价位连续 2 根 bar 命中 → bar_count=2，vol 累加
# ─────────────────────────────────────────────────────────────────────────────

def test_cross_bar_merge():
    now = 1_700_000_000
    bar_old = _bar(now - 7200, [   # 2h 前
        _bucket(76_400, 76_600, 3_000_000, 3_000_000),
        _bucket(75_000, 75_100, 200_000, 200_000),
    ])
    bar_new = _bar(now - 3600, [   # 1h 前
        _bucket(76_450, 76_550, 4_000_000, 4_000_000),  # 同价位略偏
        _bucket(75_500, 75_600, 200_000, 200_000),
    ])
    snap = detect_absorption_zones(
        footprint_contract=[bar_old, bar_new],
        footprint_spot=None,
        current_price=78_000,
        now_ts=now,
    )
    assert len(snap.zones_support) == 1
    z = snap.zones_support[0]
    # 两根 bar 合并
    assert z.bar_count == 2
    # vol 累加
    assert z.taker_volume_usd == 14_000_000.0
    # age 取最新（1h）
    assert abs(z.age_hours - 1.0) < 0.01


# ─────────────────────────────────────────────────────────────────────────────
# 5. 兜底机制：保守阈值下无命中，放宽后命中 → fallback_used=True
# ─────────────────────────────────────────────────────────────────────────────

def test_fallback_threshold_triggered():
    now = 1_700_000_000
    # |delta_pct| = (7-3)/10 = 0.40，超过保守 0.20 但低于放宽 0.30？
    # 放宽阈值也是 0.30，所以 0.40 仍然超 → 不行，需要 < 0.30
    # 改成 (6-4)/10 = 0.20（正好保守阈值边界，不命中）
    # 实际判定：d_abs >= delta_pct_abs_max 才不命中；0.20 >= 0.20 → 不命中
    # 放宽 0.30 下 0.20 < 0.30 → 命中
    bar = _bar(now, [
        _bucket(77_400, 77_600, 6_000_000, 4_000_000),   # vol=$10M |delta|=0.20
        _bucket(75_000, 75_100, 500_000, 500_000),
    ])
    snap = detect_absorption_zones(
        footprint_contract=[bar],
        footprint_spot=None,
        current_price=78_000,
        now_ts=now,
    )
    assert snap.fallback_used is True
    assert snap.total_zone_count >= 1
    # 这是 support 侧（77_500 < 78_000）
    assert snap.zones_support[0].price == 77_500.0


# ─────────────────────────────────────────────────────────────────────────────
# 6. 现货兜底：合约为空 → 走 spot
# ─────────────────────────────────────────────────────────────────────────────

def test_spot_fallback_when_contract_empty():
    now = 1_700_000_000
    spot_bar = _bar(now, [
        _bucket(78_900, 79_100, 5_000_000, 5_000_000),   # resistance
        _bucket(77_000, 77_100, 200_000, 200_000),
    ])
    snap = detect_absorption_zones(
        footprint_contract=None,
        footprint_spot=[spot_bar],
        current_price=78_000,
        now_ts=now,
    )
    assert snap.total_zone_count == 1
    assert snap.zones_resistance[0].source == "spot"
    assert snap.zones_resistance[0].side == "resistance"


# ─────────────────────────────────────────────────────────────────────────────
# 7. 双源皆空 → 空 snapshot（但字段结构完整）
# ─────────────────────────────────────────────────────────────────────────────

def test_empty_inputs_returns_empty_snapshot():
    snap = detect_absorption_zones(
        footprint_contract=None,
        footprint_spot=None,
        current_price=78_000,
        now_ts=1_700_000_000,
    )
    assert snap.total_zone_count == 0
    assert snap.zones_support == []
    assert snap.zones_resistance == []
    assert snap.strongest_support is None
    assert snap.strongest_resistance is None
    assert snap.lookback_bars == 0
    assert snap.fallback_used is False


# ─────────────────────────────────────────────────────────────────────────────
# 8. current_price <= 0 防御性返回
# ─────────────────────────────────────────────────────────────────────────────

def test_invalid_current_price_returns_empty():
    bar = _bar(1_700_000_000, [_bucket(77_000, 77_100, 5_000_000, 5_000_000)])
    snap = detect_absorption_zones(
        footprint_contract=[bar],
        footprint_spot=None,
        current_price=0,
        now_ts=1_700_000_000,
    )
    assert snap.total_zone_count == 0


# ─────────────────────────────────────────────────────────────────────────────
# 9. 每侧限量 8 个 zones
# ─────────────────────────────────────────────────────────────────────────────

def test_max_zones_per_side():
    now = 1_700_000_000
    # 12 个分散（间距 $500 > 合并容差 ~$117 @ BTC 78k×0.15%）的 support 价位
    # 每个 vol=$2M，足以越过绝对下限；|delta|=0 符合吸收特征
    # 间距大于容差，确保不会被跨 bucket 合并
    buckets = [
        _bucket(70_000 - i * 500, 70_050 - i * 500, 1_000_000, 1_000_000)
        for i in range(12)
    ]
    # 放 48 个小 vol filler，总 60 个 → top 20% = 12 个正好覆盖全部 support
    fillers = [
        _bucket(60_000 - i * 100, 59_950 - i * 100, 100_000, 100_000)
        for i in range(48)
    ]
    bar = _bar(now, buckets + fillers)
    snap = detect_absorption_zones(
        footprint_contract=[bar],
        footprint_spot=None,
        current_price=78_000,
        now_ts=now,
    )
    # 每侧限量 8 个
    assert len(snap.zones_support) == 8
    assert snap.strongest_support is not None
    assert snap.total_zone_count == 8


# ─────────────────────────────────────────────────────────────────────────────
# 10. strongest_* 正确挑选最高 vol
# ─────────────────────────────────────────────────────────────────────────────

def test_strongest_picks_highest_volume():
    now = 1_700_000_000
    bar = _bar(now, [
        _bucket(76_000, 76_100, 2_000_000, 2_000_000),   # support vol=$4M
        _bucket(76_800, 76_900, 5_000_000, 5_000_000),   # support vol=$10M
        _bucket(79_000, 79_100, 3_000_000, 3_000_000),   # resistance vol=$6M
    ])
    snap = detect_absorption_zones(
        footprint_contract=[bar],
        footprint_spot=None,
        current_price=78_000,
        now_ts=now,
    )
    # top 20% of 3 = ceil(0.6) = 1 → 只取最大 vol 的 bucket（76_850 support vol=$10M）
    assert snap.strongest_support is not None
    assert snap.strongest_support.price == 76_850.0
    assert snap.strongest_support.taker_volume_usd == 10_000_000.0
