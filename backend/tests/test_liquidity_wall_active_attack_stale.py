"""W2-T3 单元测试：active_attack_score source-aware stale 降权。

覆盖：
  _stale_weight 工具：
    - age ≤ fresh_max → 1.0
    - age ≥ dead_min → 0.0
    - 中间线性 ramp
    - age=0（无时间戳）→ 1.0（向后兼容）
  _ts_of 工具：
    - obj.ts_sec 优先
    - obj.ts fallback
    - ms 自动转 s
    - None / 缺字段返 0
  active_attack_score 集成：
    - now_sec=None → 不降权（向后兼容旧测试）
    - taker_flow.ts 已 stale → taker 因子清零
    - cvd_spot.series[-1].ts 已 stale → cvd 因子清零
    - drain history[-1].ts_sec 已 stale → drain 因子清零
    - 部分因子 fresh + 部分 stale → 仅 fresh 因子贡献
"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from models.orderbook_pressure import WallZone
from processors.liquidity_wall_engine import (
    ENGINE_DEFAULTS,
    _compute_active_attack_score,
    _stale_weight,
    _ts_of,
)


# ─────────────────────────────────────────────────────────────────
# 1. _stale_weight 工具
# ─────────────────────────────────────────────────────────────────

class TestStaleWeight:
    def test_zero_age_returns_one(self):
        """age=0 视为'无时间戳' → 不降权（向后兼容）。"""
        assert _stale_weight(0) == 1.0
        assert _stale_weight(-5) == 1.0  # 负值也视为无效

    def test_fresh_returns_one(self):
        assert _stale_weight(60) == 1.0
        assert _stale_weight(599) == 1.0
        assert _stale_weight(600) == 1.0

    def test_dead_returns_zero(self):
        assert _stale_weight(900) == 0.0
        assert _stale_weight(1500) == 0.0

    def test_ramp_in_middle(self):
        """600→1.0；900→0.0；中间线性。"""
        assert _stale_weight(750) == pytest.approx(0.5, abs=0.01)
        assert _stale_weight(700) == pytest.approx(0.667, abs=0.01)
        assert _stale_weight(800) == pytest.approx(0.333, abs=0.01)

    def test_custom_thresholds(self):
        # fresh_max=300, dead_min=600
        assert _stale_weight(200, fresh_max=300, dead_min=600) == 1.0
        assert _stale_weight(450, fresh_max=300, dead_min=600) == pytest.approx(0.5, abs=0.01)
        assert _stale_weight(700, fresh_max=300, dead_min=600) == 0.0


# ─────────────────────────────────────────────────────────────────
# 2. _ts_of 工具
# ─────────────────────────────────────────────────────────────────

class TestTsOf:
    def test_ts_sec_priority(self):
        obj = SimpleNamespace(ts_sec=1700000000, ts=1234567890)
        assert _ts_of(obj) == 1700000000

    def test_ts_fallback(self):
        obj = SimpleNamespace(ts=1700000000)
        assert _ts_of(obj) == 1700000000

    def test_ms_auto_converted(self):
        obj = SimpleNamespace(ts_sec=1_700_000_000_000)  # ms
        assert _ts_of(obj) == 1_700_000_000

    def test_none_returns_zero(self):
        assert _ts_of(None) == 0

    def test_no_attrs_returns_zero(self):
        obj = SimpleNamespace(other_field=42)
        assert _ts_of(obj) == 0

    def test_invalid_value_returns_zero(self):
        obj = SimpleNamespace(ts="not a number", ts_sec=None)
        assert _ts_of(obj) == 0


# ─────────────────────────────────────────────────────────────────
# 3. active_attack_score 集成 stale 降权
# ─────────────────────────────────────────────────────────────────

def _make_zone(side: str = "bid") -> WallZone:
    return WallZone(
        side=side, price_low=76050, price_high=76080,
        price_mid=76065, peak_price=76065, distance_pct=-0.30,
        current_usd=2_000_000, max_usd_1h=2_000_000, avg_usd_1h=2_000_000,
        bin_count=3, seen_count=10, visible_minutes=40, persistence_score=0.5,
    )


class TestActiveAttackStaleDownweight:
    def test_now_sec_none_preserves_legacy_behavior(self):
        """now_sec=None → 完全不降权，与 W2-T2 行为一致（向后兼容）。"""
        zone = _make_zone()
        taker = SimpleNamespace(buy_volume_usd=1_000_000, sell_volume_usd=4_000_000)
        cvd = SimpleNamespace(trend_1h="strong_down")
        score = _compute_active_attack_score(zone, taker, cvd, ENGINE_DEFAULTS)
        # 0.40 × 1.0 (sell_ratio=0.8) + 0.30 × 1.0 = 0.70
        assert score == pytest.approx(0.70, abs=0.01)

    def test_fresh_taker_full_weight(self):
        """now=1700000000，taker.ts=1700000060（age=−60s 视为 0）→ 全权重。"""
        zone = _make_zone()
        taker = SimpleNamespace(
            buy_volume_usd=1_000_000, sell_volume_usd=4_000_000,
            ts=1700000000,
        )
        cvd = SimpleNamespace(trend_1h="strong_down", series=[SimpleNamespace(ts=1700000000)])
        score = _compute_active_attack_score(
            zone, taker, cvd, ENGINE_DEFAULTS, now_sec=1700000060,
        )
        # 全权重：0.40 + 0.30 = 0.70
        assert score == pytest.approx(0.70, abs=0.01)

    def test_stale_taker_factor_zeroed(self):
        """taker.ts 比 now 早 1000s（≥ 900s dead）→ taker 因子清零。"""
        zone = _make_zone()
        taker = SimpleNamespace(
            buy_volume_usd=1_000_000, sell_volume_usd=4_000_000,
            ts=1700000000,
        )
        cvd = SimpleNamespace(trend_1h="strong_down", series=[SimpleNamespace(ts=1700001000)])
        # now=1700001000，taker.ts=1700000000 → age=1000 ≥ 900 → taker 清零
        score = _compute_active_attack_score(
            zone, taker, cvd, ENGINE_DEFAULTS, now_sec=1700001000,
        )
        # 仅 cvd 0.30（cvd fresh）；taker 0.40 × 0 = 0
        assert score == pytest.approx(0.30, abs=0.01)

    def test_partial_stale_taker_ramp(self):
        """taker age=750s（介于 600-900）→ ramp 系数 0.5 → taker 0.40 × 1.0 × 0.5 = 0.20"""
        zone = _make_zone()
        taker = SimpleNamespace(
            buy_volume_usd=1_000_000, sell_volume_usd=4_000_000,
            ts=1700000000,
        )
        # cvd 直接给个 fresh 时间戳避免被降权
        cvd = SimpleNamespace(trend_1h="balanced",  # 不同向 → cvd 不贡献
                              series=[SimpleNamespace(ts=1700000750)])
        score = _compute_active_attack_score(
            zone, taker, cvd, ENGINE_DEFAULTS, now_sec=1700000750,
        )
        # cvd 不同向 → 0；taker 0.40 × 1.0 × 0.5 = 0.20
        assert score == pytest.approx(0.20, abs=0.02)

    def test_stale_cvd_factor_zeroed(self):
        """cvd series[-1].ts 已 stale → cvd 因子清零；taker 仍 fresh。"""
        zone = _make_zone()
        taker = SimpleNamespace(
            buy_volume_usd=1_000_000, sell_volume_usd=4_000_000,
            ts=1700001000,  # fresh
        )
        cvd = SimpleNamespace(
            trend_1h="strong_down",
            series=[SimpleNamespace(ts=1700000000)],  # 1000s ago, dead
        )
        score = _compute_active_attack_score(
            zone, taker, cvd, ENGINE_DEFAULTS, now_sec=1700001000,
        )
        # taker 0.40；cvd 0.30 × 0 = 0
        assert score == pytest.approx(0.40, abs=0.01)

    def test_stale_drain_factor_zeroed(self):
        """drain history[-1].ts_sec 已 stale → drain 因子清零。"""
        zone = _make_zone()
        # 构造现货 history：drain 5%（1000M → 950M）但 ts 已 stale
        history = [
            SimpleNamespace(
                aggregated_bids_usd=1_000_000_000, aggregated_asks_usd=1_000_000_000,
                ts_sec=1700000000,
            ),
            SimpleNamespace(
                aggregated_bids_usd=950_000_000, aggregated_asks_usd=1_000_000_000,
                ts_sec=1700000000,  # stale
            ),
        ]
        score = _compute_active_attack_score(
            zone, None, None, ENGINE_DEFAULTS,
            spot_ask_bids_history=history,
            now_sec=1700001500,  # 1500s ago, dead
        )
        # drain 因子清零 → 0
        assert score == 0.0

    def test_fresh_drain_full_weight(self):
        """drain ts fresh → drain 因子全权重生效。"""
        zone = _make_zone()
        history = [
            SimpleNamespace(
                aggregated_bids_usd=1_000_000_000, aggregated_asks_usd=1_000_000_000,
                ts_sec=1700000000,
            ),
            SimpleNamespace(
                aggregated_bids_usd=950_000_000, aggregated_asks_usd=1_000_000_000,
                ts_sec=1700000300,  # fresh (5min ago)
            ),
        ]
        score = _compute_active_attack_score(
            zone, None, None, ENGINE_DEFAULTS,
            spot_ask_bids_history=history,
            now_sec=1700000400,
        )
        # drain 5% → 1.0；0.30 × 1.0 × 1.0 = 0.30
        assert score == pytest.approx(0.30, abs=0.01)

    def test_no_ts_field_falls_back_to_full_weight(self):
        """对象没有 ts / ts_sec 字段（旧夹具）→ age=0 → 全权重（向后兼容）。"""
        zone = _make_zone()
        taker = SimpleNamespace(buy_volume_usd=1_000_000, sell_volume_usd=4_000_000)  # 无 ts
        cvd = SimpleNamespace(trend_1h="strong_down")  # 无 series / ts
        score = _compute_active_attack_score(
            zone, taker, cvd, ENGINE_DEFAULTS, now_sec=1700001000,
        )
        # 都没 ts → 全权重 → 0.70
        assert score == pytest.approx(0.70, abs=0.01)

    def test_taker_flow_data_with_buy_ratio_supported(self):
        """新 TakerFlowData 模型用 buy_ratio / sell_ratio 字段（生产用）。"""
        zone = _make_zone()
        taker = SimpleNamespace(buy_ratio=0.2, sell_ratio=0.8, ts=1700000000)  # bid wall sell 主导
        score = _compute_active_attack_score(
            zone, taker, None, ENGINE_DEFAULTS, now_sec=1700000060,
        )
        # sell_ratio=0.8 → 0.40 × 1.0 = 0.40（线性 0.5→0, 0.6→1.0，>0.6 cap 1.0）
        assert score == pytest.approx(0.40, abs=0.01)


# ─────────────────────────────────────────────────────────────────
# 4. CVD 枚举断裂回归（2026-08 修复）
#    生产端 processors/cvd._calc_trend 输出 rising/declining/flat，
#    旧代码只认 up/down/strong_* → CVD 因子（0.30 权重）永不触发。
# ─────────────────────────────────────────────────────────────────

class TestCvdEnumRegression:
    def test_declining_triggers_bid_side_attack(self):
        """生产枚举 declining（=down）对 bid 墙同向 → 贡献 0.30。"""
        zone = _make_zone(side="bid")
        cvd = SimpleNamespace(trend_1h="declining")
        score = _compute_active_attack_score(zone, None, cvd, ENGINE_DEFAULTS)
        assert score == pytest.approx(0.30, abs=0.01)

    def test_rising_triggers_ask_side_attack(self):
        """生产枚举 rising（=up）对 ask 墙同向 → 贡献 0.30。"""
        zone = _make_zone(side="ask")
        cvd = SimpleNamespace(trend_1h="rising")
        score = _compute_active_attack_score(zone, None, cvd, ENGINE_DEFAULTS)
        assert score == pytest.approx(0.30, abs=0.01)

    def test_rising_does_not_trigger_bid_side(self):
        """rising 对 bid 墙是反向 → 不贡献。"""
        zone = _make_zone(side="bid")
        cvd = SimpleNamespace(trend_1h="rising")
        score = _compute_active_attack_score(zone, None, cvd, ENGINE_DEFAULTS)
        assert score == 0.0

    def test_normalize_trend_aliases(self):
        from processors.cvd import normalize_trend
        assert normalize_trend("rising") == "up"
        assert normalize_trend("strong_up") == "up"
        assert normalize_trend("bullish") == "up"
        assert normalize_trend("declining") == "down"
        assert normalize_trend("falling") == "down"
        assert normalize_trend("strong_down") == "down"
        assert normalize_trend("flat") == "flat"
        assert normalize_trend("balanced") == "flat"
        assert normalize_trend("") == ""
        assert normalize_trend(None) == ""
        assert normalize_trend("garbage") == ""
