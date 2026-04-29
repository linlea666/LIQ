"""
Key Level Tracker V2 · M3 桥接（_apply_pressure_alignment 升级版）单元测试

覆盖：
1. 旧路径（PressureWall S/A 级共振 → ob_strong_bid/ask）未被破坏
2. wall_zones 多档 chip：dual_source > spot_only > spot_confluence > trusted（互斥）
3. wall_events 衔接：wall_consumed / wall_removed / wall_strengthened（同价位 30min 内）
4. 风险 warnings：break_through_risk / vacuum_gap_pct / 低 trust + 高 removal
5. 铁律守护：仅追加 confirmations / warnings，不改 score 之外的字段
6. 侧向匹配：多向 ↔ bid，空向 ↔ ask；wait_* 信号忽略

新增 chip key（前端 CONFIRMATION_LABELS 已映射）：
  ob_dual_source_{bid,ask} / ob_spot_only_{bid,ask} /
  ob_spot_confluence_{bid,ask} / ob_trusted_{bid,ask} / ob_wall_strengthened
"""
from __future__ import annotations

import time

from models.key_level import KeyLevelSignal
from models.orderbook_pressure import (
    OrderbookPressureSnapshot,
    PressureWall,
    SweepTarget,
    WallEvent,
    WallZone,
)
from processors.key_level_tracker_v2 import _apply_pressure_alignment


# ─────────────────────────────────────────────────────────────────
# 工具：构造 signal / zone / snapshot
# ─────────────────────────────────────────────────────────────────

def _make_long_signal(price: float = 63000.0, action: str = "snipe_long") -> KeyLevelSignal:
    return KeyLevelSignal(
        level_price=price,
        side="support",
        state="testing",
        action=action,
        confidence="B",
    )


def _make_short_signal(price: float = 63000.0, action: str = "snipe_short") -> KeyLevelSignal:
    return KeyLevelSignal(
        level_price=price,
        side="resistance",
        state="testing",
        action=action,
        confidence="B",
    )


def _make_zone(
    side: str,
    price_mid: float,
    *,
    trust_score: float = 0.5,
    dual_source: bool = False,
    has_spot_confluence: bool = False,
    source: str = "depth_only",
    wall_removal_risk: float = 0.0,
    break_through_risk: float = 0.0,
    next_magnet_price: float | None = None,
    sweep_target: SweepTarget | None = None,
) -> WallZone:
    return WallZone(
        side=side,  # type: ignore[arg-type]
        price_low=price_mid * 0.998,
        price_high=price_mid * 1.002,
        price_mid=price_mid,
        peak_price=price_mid,
        distance_pct=0.0,
        current_usd=2_000_000.0,
        max_usd_1h=2_500_000.0,
        avg_usd_1h=2_000_000.0,
        bin_count=2,
        seen_count=10,
        visible_minutes=50.0,
        persistence_score=0.8,
        source=source,  # type: ignore[arg-type]
        trust_score=trust_score,
        dual_source=dual_source,
        has_spot_confluence=has_spot_confluence,
        wall_removal_risk=wall_removal_risk,
        break_through_risk=break_through_risk,
        next_magnet_price=next_magnet_price,
        sweep_target=sweep_target,
    )


def _make_pressure_wall(side: str, price_mid: float, tier: str = "S") -> PressureWall:
    return PressureWall(
        side=side,  # type: ignore[arg-type]
        price_lo=price_mid * 0.998,
        price_hi=price_mid * 1.002,
        price_mid=price_mid,
        distance_pct=0.0,
        size_usd=20_000_000.0,
        size_base=300.0,
        strength_score=15_000_000.0,
        strength_tier=tier,  # type: ignore[arg-type]
    )


def _make_snapshot(
    *,
    walls: list[PressureWall] | None = None,
    walls_above: list[WallZone] | None = None,
    walls_below: list[WallZone] | None = None,
    events: list[WallEvent] | None = None,
    ts_sec: int | None = None,
) -> OrderbookPressureSnapshot:
    return OrderbookPressureSnapshot(
        coin="BTC",
        ts_sec=ts_sec if ts_sec is not None else int(time.time()),
        last_price=63000.0,
        atr=300.0,
        walls=walls or [],
        walls_above=walls_above or [],
        walls_below=walls_below or [],
        wall_events=events or [],
    )


# ─────────────────────────────────────────────────────────────────
# 1. 旧路径（PressureWall S/A 级 → ob_strong_bid/ask）未被破坏
# ─────────────────────────────────────────────────────────────────

class TestLegacyPathPreserved:
    def test_long_signal_with_sa_bid_wall_yields_ob_strong_bid(self):
        sig = _make_long_signal(63000.0)
        snap = _make_snapshot(walls=[_make_pressure_wall("bid", 63000.0, "S")])
        _apply_pressure_alignment([sig], snap, atr=300.0)
        assert "ob_strong_bid" in sig.confirmations

    def test_short_signal_with_sa_ask_wall_yields_ob_strong_ask(self):
        sig = _make_short_signal(63000.0)
        snap = _make_snapshot(walls=[_make_pressure_wall("ask", 63000.0, "A")])
        _apply_pressure_alignment([sig], snap, atr=300.0)
        assert "ob_strong_ask" in sig.confirmations

    def test_b_tier_wall_does_not_trigger_legacy(self):
        sig = _make_long_signal(63000.0)
        snap = _make_snapshot(walls=[_make_pressure_wall("bid", 63000.0, "B")])
        _apply_pressure_alignment([sig], snap, atr=300.0)
        assert "ob_strong_bid" not in sig.confirmations

    def test_legacy_and_new_path_coexist(self):
        """旧 ob_strong_bid + 新 ob_dual_source_bid 同时出现（不互斥）。"""
        sig = _make_long_signal(63000.0)
        snap = _make_snapshot(
            walls=[_make_pressure_wall("bid", 63000.0, "S")],
            walls_below=[_make_zone("bid", 63000.0, dual_source=True, trust_score=0.9)],
        )
        _apply_pressure_alignment([sig], snap, atr=300.0)
        assert "ob_strong_bid" in sig.confirmations
        assert "ob_dual_source_bid" in sig.confirmations


# ─────────────────────────────────────────────────────────────────
# 2. wall_zones 多档 chip 互斥优先级
# ─────────────────────────────────────────────────────────────────

class TestZoneTrustChips:
    def test_dual_source_takes_precedence_over_spot_confluence(self):
        sig = _make_long_signal(63000.0)
        zone = _make_zone(
            "bid", 63000.0,
            dual_source=True,
            has_spot_confluence=True,
            trust_score=0.9,
        )
        snap = _make_snapshot(walls_below=[zone])
        _apply_pressure_alignment([sig], snap, atr=300.0)
        assert "ob_dual_source_bid" in sig.confirmations
        assert "ob_spot_confluence_bid" not in sig.confirmations
        assert "ob_trusted_bid" not in sig.confirmations

    def test_spot_only_zone_yields_spot_only_chip(self):
        sig = _make_long_signal(63000.0)
        zone = _make_zone("bid", 63000.0, source="spot_only", trust_score=0.7)
        snap = _make_snapshot(walls_below=[zone])
        _apply_pressure_alignment([sig], snap, atr=300.0)
        assert "ob_spot_only_bid" in sig.confirmations

    def test_spot_confluence_without_dual_source(self):
        sig = _make_long_signal(63000.0)
        zone = _make_zone(
            "bid", 63000.0,
            has_spot_confluence=True,
            dual_source=False,
            trust_score=0.7,
        )
        snap = _make_snapshot(walls_below=[zone])
        _apply_pressure_alignment([sig], snap, atr=300.0)
        assert "ob_spot_confluence_bid" in sig.confirmations
        assert "ob_dual_source_bid" not in sig.confirmations

    def test_trusted_chip_when_trust_score_high(self):
        sig = _make_long_signal(63000.0)
        zone = _make_zone("bid", 63000.0, trust_score=0.7)
        snap = _make_snapshot(walls_below=[zone])
        _apply_pressure_alignment([sig], snap, atr=300.0)
        assert "ob_trusted_bid" in sig.confirmations

    def test_low_trust_no_special_chip(self):
        """trust_score < 0.65 且无现货 → 不加新档 chip（旧 ob_strong_* 路径处理）。"""
        sig = _make_long_signal(63000.0)
        zone = _make_zone("bid", 63000.0, trust_score=0.5)
        snap = _make_snapshot(walls_below=[zone])
        _apply_pressure_alignment([sig], snap, atr=300.0)
        assert not any(c.startswith("ob_dual_source") or
                       c.startswith("ob_spot_only") or
                       c.startswith("ob_spot_confluence") or
                       c.startswith("ob_trusted")
                       for c in sig.confirmations)

    def test_short_signal_matches_ask_zones_only(self):
        sig = _make_short_signal(63000.0)
        ask_zone = _make_zone("ask", 63000.0, dual_source=True, trust_score=0.9)
        bid_zone = _make_zone("bid", 63000.0, dual_source=True, trust_score=0.9)
        snap = _make_snapshot(walls_above=[ask_zone], walls_below=[bid_zone])
        _apply_pressure_alignment([sig], snap, atr=300.0)
        assert "ob_dual_source_ask" in sig.confirmations
        assert "ob_dual_source_bid" not in sig.confirmations


# ─────────────────────────────────────────────────────────────────
# 3. wall_events 衔接（同价位 + 30min 内）
# ─────────────────────────────────────────────────────────────────

class TestWallEvents:
    def _make_event(self, side: str, price_mid: float, event_type: str,
                    age_sec: int, snap_ts: int) -> WallEvent:
        return WallEvent(
            ts_sec=snap_ts - age_sec,
            side=side,  # type: ignore[arg-type]
            price_mid=price_mid,
            event_type=event_type,  # type: ignore[arg-type]
            confidence=0.8,
        )

    def test_wall_consumed_yields_warning(self):
        snap_ts = int(time.time())
        sig = _make_long_signal(63000.0)
        ev = self._make_event("bid", 63000.0, "wall_consumed", 600, snap_ts)
        snap = _make_snapshot(events=[ev], ts_sec=snap_ts)
        _apply_pressure_alignment([sig], snap, atr=300.0)
        assert any("被吃" in w for w in sig.warnings)
        assert any("10min" in w for w in sig.warnings)

    def test_wall_removed_yields_warning(self):
        snap_ts = int(time.time())
        sig = _make_long_signal(63000.0)
        ev = self._make_event("bid", 63000.0, "wall_removed", 300, snap_ts)
        snap = _make_snapshot(events=[ev], ts_sec=snap_ts)
        _apply_pressure_alignment([sig], snap, atr=300.0)
        assert any("撤单" in w for w in sig.warnings)

    def test_wall_strengthened_yields_confirmation(self):
        snap_ts = int(time.time())
        sig = _make_long_signal(63000.0)
        ev = self._make_event("bid", 63000.0, "wall_strengthened", 300, snap_ts)
        snap = _make_snapshot(events=[ev], ts_sec=snap_ts)
        _apply_pressure_alignment([sig], snap, atr=300.0)
        assert "ob_wall_strengthened" in sig.confirmations

    def test_event_older_than_30min_ignored(self):
        snap_ts = int(time.time())
        sig = _make_long_signal(63000.0)
        ev = self._make_event("bid", 63000.0, "wall_consumed", 1900, snap_ts)
        snap = _make_snapshot(events=[ev], ts_sec=snap_ts)
        _apply_pressure_alignment([sig], snap, atr=300.0)
        assert not any("被吃" in w for w in sig.warnings)

    def test_event_at_far_price_ignored(self):
        snap_ts = int(time.time())
        sig = _make_long_signal(63000.0)
        ev = self._make_event("bid", 64000.0, "wall_consumed", 300, snap_ts)
        snap = _make_snapshot(events=[ev], ts_sec=snap_ts)
        _apply_pressure_alignment([sig], snap, atr=300.0)
        assert not any("被吃" in w for w in sig.warnings)

    def test_event_opposite_side_ignored(self):
        """做多信号忽略 ask 侧事件（即使同价位）。"""
        snap_ts = int(time.time())
        sig = _make_long_signal(63000.0)
        ev = self._make_event("ask", 63000.0, "wall_consumed", 300, snap_ts)
        snap = _make_snapshot(events=[ev], ts_sec=snap_ts)
        _apply_pressure_alignment([sig], snap, atr=300.0)
        assert not any("被吃" in w for w in sig.warnings)

    def test_strengthened_dedup_in_one_run(self):
        """同信号同价位多条 strengthened 只加一次 ob_wall_strengthened。"""
        snap_ts = int(time.time())
        sig = _make_long_signal(63000.0)
        ev1 = self._make_event("bid", 63000.0, "wall_strengthened", 300, snap_ts)
        ev2 = self._make_event("bid", 63000.0, "wall_strengthened", 600, snap_ts)
        snap = _make_snapshot(events=[ev1, ev2], ts_sec=snap_ts)
        _apply_pressure_alignment([sig], snap, atr=300.0)
        assert sig.confirmations.count("ob_wall_strengthened") == 1


# ─────────────────────────────────────────────────────────────────
# 4. 风险 warnings（break_through / vacuum / 低 trust + 高 removal）
# ─────────────────────────────────────────────────────────────────

class TestRiskWarnings:
    def test_high_break_through_with_magnet_long_side(self):
        sig = _make_long_signal(63000.0)
        zone = _make_zone(
            "bid", 63000.0,
            trust_score=0.7,
            break_through_risk=0.75,
            next_magnet_price=61500.0,
        )
        snap = _make_snapshot(walls_below=[zone])
        _apply_pressure_alignment([sig], snap, atr=300.0)
        msgs = " ".join(sig.warnings)
        # W1-T3：必须是"打穿风险评分"（强调评分性质，不是统计概率）
        assert "打穿风险评分" in msgs
        assert "75%" in msgs
        assert "下方磁铁" in msgs

    def test_warning_uses_score_phrasing_not_probability(self):
        """W1-T3：所有相关 warning 必须用"评分"措辞，不得出现误导性"概率""可能性"等词。"""
        sig = _make_long_signal(63000.0)
        zone = _make_zone(
            "bid", 63000.0,
            trust_score=0.4,
            wall_removal_risk=0.7,         # 触发"撤单风险评分"
            break_through_risk=0.65,       # 触发"打穿风险评分"
            next_magnet_price=61500.0,
        )
        snap = _make_snapshot(walls_below=[zone])
        _apply_pressure_alignment([sig], snap, atr=300.0)
        msgs = " ".join(sig.warnings)
        # 正向断言："评分"措辞必现
        assert "撤单风险评分" in msgs
        assert "打穿风险评分" in msgs
        # 反向断言：禁用概率/可能性措辞（仅在 KL warning 范围内）
        # 注意：磁铁价位描述与"概率"无关，本检查仅针对 zone 风险 warning
        assert "概率" not in msgs
        assert "可能性" not in msgs

    def test_high_break_through_short_side_uses_above(self):
        sig = _make_short_signal(63000.0)
        zone = _make_zone(
            "ask", 63000.0,
            trust_score=0.7,
            break_through_risk=0.65,
            next_magnet_price=64500.0,
        )
        snap = _make_snapshot(walls_above=[zone])
        _apply_pressure_alignment([sig], snap, atr=300.0)
        msgs = " ".join(sig.warnings)
        assert "上方磁铁" in msgs

    def test_low_trust_high_removal_yields_warning(self):
        sig = _make_long_signal(63000.0)
        zone = _make_zone(
            "bid", 63000.0,
            trust_score=0.4,
            wall_removal_risk=0.7,
        )
        snap = _make_snapshot(walls_below=[zone])
        _apply_pressure_alignment([sig], snap, atr=300.0)
        assert any("撤单风险" in w and "70" in w for w in sig.warnings)

    def test_vacuum_gap_warning(self):
        sig = _make_long_signal(63000.0)
        sweep = SweepTarget(
            direction="below",
            magnet_price=61000.0,
            magnet_amount_usd=50_000_000.0,
            distance_pct=-3.2,
            vacuum_gap_pct=2.5,
        )
        zone = _make_zone(
            "bid", 63000.0,
            trust_score=0.7,
            break_through_risk=0.8,
            sweep_target=sweep,
        )
        snap = _make_snapshot(walls_below=[zone])
        _apply_pressure_alignment([sig], snap, atr=300.0)
        msgs = " ".join(sig.warnings)
        assert "真空跨度" in msgs
        assert "2.5%" in msgs

    def test_no_warning_when_break_through_below_threshold(self):
        sig = _make_long_signal(63000.0)
        zone = _make_zone("bid", 63000.0, trust_score=0.7, break_through_risk=0.5)
        snap = _make_snapshot(walls_below=[zone])
        _apply_pressure_alignment([sig], snap, atr=300.0)
        assert not any("打穿" in w for w in sig.warnings)


# ─────────────────────────────────────────────────────────────────
# 5. 铁律守护（V3：只追加 confirmations / warnings，不改其他）
# ─────────────────────────────────────────────────────────────────

class TestIronLawGuard:
    def test_score_recomputed_but_other_fields_preserved(self):
        """score 由 _compute_score 重算（合理），其他字段一字不变。"""
        sig = _make_long_signal(63000.0)
        sig.entry_price = 62800.0
        sig.stop_loss = 62500.0
        sig.tp1 = 63800.0
        sig.tp2 = 64200.0
        sig.rr_ratio = 3.0
        original_state = sig.state
        original_action = sig.action
        original_side = sig.side
        original_entry = sig.entry_price
        original_sl = sig.stop_loss
        original_tp1 = sig.tp1
        original_tp2 = sig.tp2

        zone = _make_zone("bid", 63000.0, dual_source=True, trust_score=0.9)
        snap = _make_snapshot(walls_below=[zone])
        _apply_pressure_alignment([sig], snap, atr=300.0)

        assert sig.state == original_state
        assert sig.action == original_action
        assert sig.side == original_side
        assert sig.entry_price == original_entry
        assert sig.stop_loss == original_sl
        assert sig.tp1 == original_tp1
        assert sig.tp2 == original_tp2

    def test_empty_snapshot_no_change(self):
        sig = _make_long_signal(63000.0)
        confirms_before = list(sig.confirmations)
        warnings_before = list(sig.warnings)
        snap = _make_snapshot()
        _apply_pressure_alignment([sig], snap, atr=300.0)
        assert sig.confirmations == confirms_before
        assert sig.warnings == warnings_before


# ─────────────────────────────────────────────────────────────────
# 6. 侧向匹配 + wait 信号忽略
# ─────────────────────────────────────────────────────────────────

class TestSideMatchingAndWaitIgnore:
    def test_wait_approach_signal_ignored(self):
        sig = _make_long_signal(63000.0, action="wait_approach")
        zone = _make_zone("bid", 63000.0, dual_source=True, trust_score=0.9)
        snap = _make_snapshot(walls_below=[zone])
        _apply_pressure_alignment([sig], snap, atr=300.0)
        assert sig.confirmations == []
        assert sig.warnings == []

    def test_wait_sweep_signal_ignored(self):
        sig = _make_long_signal(63000.0, action="wait_sweep")
        zone = _make_zone("bid", 63000.0, dual_source=True, trust_score=0.9)
        snap = _make_snapshot(walls_below=[zone])
        _apply_pressure_alignment([sig], snap, atr=300.0)
        assert sig.confirmations == []

    def test_far_price_zone_not_matched(self):
        """zone 与 signal 价位 > 0.5×ATR → 不匹配。"""
        sig = _make_long_signal(63000.0)
        zone = _make_zone("bid", 64000.0, dual_source=True, trust_score=0.9)
        snap = _make_snapshot(walls_below=[zone])
        _apply_pressure_alignment([sig], snap, atr=300.0)
        assert "ob_dual_source_bid" not in sig.confirmations

    def test_within_atr_tolerance_matched(self):
        """zone 在 ±0.5×ATR 内 → 匹配。"""
        sig = _make_long_signal(63000.0)
        zone = _make_zone("bid", 63100.0, dual_source=True, trust_score=0.9)
        snap = _make_snapshot(walls_below=[zone])
        _apply_pressure_alignment([sig], snap, atr=300.0)
        assert "ob_dual_source_bid" in sig.confirmations
