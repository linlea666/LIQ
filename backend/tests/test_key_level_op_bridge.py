"""
Key Level Tracker V2 · M3 桥接（_apply_pressure_alignment 升级版）单元测试

覆盖：
1. wall_zones 多档 chip：dual_source > spot_only > spot_confluence > trusted（互斥）
2. wall_events 衔接：wall_consumed / wall_removed / wall_strengthened（同价位 30min 内）
3. 风险 warnings：break_through_risk / vacuum_gap_pct / 低 trust + 高 removal
4. 铁律守护：仅追加 confirmations / warnings，不改 score 之外的字段
5. 侧向匹配：多向 ↔ bid，空向 ↔ ask；wait_* 信号忽略

W3-T4-a：删除旧 PressureWall S/A 级 → ob_strong_bid/ask 路径相关测试
（与新 6 类 chip 口径冲突且会把 spoof 嫌疑墙也标成"强墙"）。

新增 chip key（前端 CONFIRMATION_LABELS 已映射）：
  ob_dual_source_{bid,ask} / ob_spot_only_{bid,ask} /
  ob_spot_confluence_{bid,ask} / ob_trusted_{bid,ask} / ob_wall_strengthened
"""
from __future__ import annotations

import time

from models.key_level import KeyLevelSignal
from models.orderbook_pressure import (
    OrderbookPressureSnapshot,
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
    coinbase_spot_confluence: bool = False,
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
        coinbase_spot_confluence=coinbase_spot_confluence,
        wall_removal_risk=wall_removal_risk,
        break_through_risk=break_through_risk,
        next_magnet_price=next_magnet_price,
        sweep_target=sweep_target,
    )


def _make_snapshot(
    *,
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
        walls=[],
        walls_above=walls_above or [],
        walls_below=walls_below or [],
        wall_events=events or [],
    )


# ─────────────────────────────────────────────────────────────────
# W3-T4-a 守护：旧 ob_strong_* 路径已被删除，PressureWall.walls 不再产 chip
# ─────────────────────────────────────────────────────────────────

class TestLegacyPathRemoved:
    """W3-T4-a：保证 pressure_snapshot.walls（PressureWall list）即使非空，
    也不再为 KL 信号追加 ob_strong_bid/ask chip — 完全由 wall_zones 新路径替代。
    """

    def test_legacy_chip_keys_never_emitted(self):
        """即使 walls_below 为空，新路径不产 chip 时也不会回退到 ob_strong_*。"""
        sig = _make_long_signal(63000.0)
        zone = _make_zone("bid", 63000.0, trust_score=0.5)  # 低 trust，新路径不加 chip
        snap = _make_snapshot(walls_below=[zone])
        _apply_pressure_alignment([sig], snap, atr=300.0)
        assert "ob_strong_bid" not in sig.confirmations
        assert "ob_strong_ask" not in sig.confirmations

    def test_pressure_walls_field_no_longer_consumed(self):
        """pressure_snapshot.walls 不应再被 _apply_pressure_alignment 消费。

        构造 only walls=[] + 高 trust zone 时，新路径正常加 chip；
        反向证明：高 trust zone 不存在时，即使 walls 字段保留默认空，
        函数也安全 early-return（不会因为缺 strong_asks/strong_bids 报错）。
        """
        sig = _make_long_signal(63000.0)
        snap = _make_snapshot()  # walls=[], walls_below=[], events=[]
        _apply_pressure_alignment([sig], snap, atr=300.0)
        assert sig.confirmations == []
        assert sig.warnings == []


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
        """trust_score < 0.65 且无现货 → 不加 chip（W3-T4-a 后这类墙不再贡献 chip）。"""
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
        # W3-T3：评分以 0.XX 浮点展示（不再用 X%，避免被误读为概率）
        assert "打穿风险评分" in msgs
        assert "0.75" in msgs
        assert "75%" not in msgs  # 反向断言：不应再出现 X% 形式
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
        # W3-T3：评分以 0.XX 浮点展示（不再 X%）
        assert any("撤单风险评分" in w and "0.70" in w for w in sig.warnings)
        assert all("70%" not in w for w in sig.warnings)

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


# ─────────────────────────────────────────────────────────────────
# 7. W3-T1：Coinbase 共振叠加 chip
# ─────────────────────────────────────────────────────────────────

class TestCoinbaseConfluenceChip:
    def test_coinbase_alone_yields_ob_coinbase_bid(self):
        """coinbase_spot_confluence + 普通合约（trust < 0.65）墙：
           主路径不会加 dual/spot/trusted chip，仅追加 ob_coinbase_bid 叠加 chip。"""
        sig = _make_long_signal(63000.0)
        zone = _make_zone(
            "bid", 63000.0,
            trust_score=0.5,  # 普通
            coinbase_spot_confluence=True,
        )
        snap = _make_snapshot(walls_below=[zone])
        _apply_pressure_alignment([sig], snap, atr=300.0)
        # Coinbase chip 必须被追加
        assert "ob_coinbase_bid" in sig.confirmations
        # 互斥优先级路径无任何 chip（trust < 0.65 且无现货共振）
        assert "ob_dual_source_bid" not in sig.confirmations
        assert "ob_trusted_bid" not in sig.confirmations

    def test_coinbase_layers_on_top_of_dual_source(self):
        """关键不变量：双源墙 + Coinbase 共振时，两个 chip 同时出现（叠加，不互斥）。"""
        sig = _make_long_signal(63000.0)
        zone = _make_zone(
            "bid", 63000.0,
            trust_score=0.95,
            dual_source=True,
            coinbase_spot_confluence=True,
        )
        snap = _make_snapshot(walls_below=[zone])
        _apply_pressure_alignment([sig], snap, atr=300.0)
        assert "ob_dual_source_bid" in sig.confirmations
        assert "ob_coinbase_bid" in sig.confirmations

    def test_coinbase_layers_on_top_of_trusted(self):
        """trust ≥ 0.65 + Coinbase 共振 → ob_trusted + ob_coinbase 同时。"""
        sig = _make_long_signal(63000.0)
        zone = _make_zone(
            "bid", 63000.0,
            trust_score=0.70,
            coinbase_spot_confluence=True,
        )
        snap = _make_snapshot(walls_below=[zone])
        _apply_pressure_alignment([sig], snap, atr=300.0)
        assert "ob_trusted_bid" in sig.confirmations
        assert "ob_coinbase_bid" in sig.confirmations

    def test_no_coinbase_no_chip(self):
        """coinbase_spot_confluence=False → 不加 ob_coinbase_*"""
        sig = _make_long_signal(63000.0)
        zone = _make_zone("bid", 63000.0, dual_source=True, trust_score=0.9)
        snap = _make_snapshot(walls_below=[zone])
        _apply_pressure_alignment([sig], snap, atr=300.0)
        assert "ob_coinbase_bid" not in sig.confirmations

    def test_short_signal_with_ask_coinbase_yields_ob_coinbase_ask(self):
        """空向信号 + ask wall + Coinbase 共振 → ob_coinbase_ask"""
        sig = _make_short_signal(63000.0)
        zone = _make_zone(
            "ask", 63000.0,
            trust_score=0.85,
            dual_source=True,
            coinbase_spot_confluence=True,
        )
        snap = _make_snapshot(walls_above=[zone])
        _apply_pressure_alignment([sig], snap, atr=300.0)
        assert "ob_coinbase_ask" in sig.confirmations
        assert "ob_dual_source_ask" in sig.confirmations


# ─────────────────────────────────────────────────────────────────
# W3-T3 文案统一：风险评分 X% → 0.XX
# ─────────────────────────────────────────────────────────────────

class TestW3T3RiskScoreFormat:
    """W3-T3：风险评分（break_through_risk / wall_removal_risk）展示统一为 0.XX 浮点。

    动机：
      - X% 形式容易让 AI / 用户误读为"统计概率"
      - 改为 0.XX 与 trust_score / confidence / SR/SA / active_attack_score 同口径
      - 反向断言全程禁止 X% 形式
    """

    def test_break_through_warning_uses_two_decimal_float(self):
        sig = _make_long_signal(63000.0)
        zone = _make_zone(
            "bid", 63000.0,
            trust_score=0.7,
            break_through_risk=0.78,
            next_magnet_price=61500.0,
        )
        snap = _make_snapshot(walls_below=[zone])
        _apply_pressure_alignment([sig], snap, atr=300.0)
        msgs = " ".join(sig.warnings)
        assert "打穿风险评分0.78" in msgs
        assert "78%" not in msgs
        assert "78.00%" not in msgs

    def test_break_through_warning_two_decimal_for_round_score(self):
        """0.6 边界 → 显示 0.60（保留两位小数，不省略尾零）。"""
        sig = _make_long_signal(63000.0)
        zone = _make_zone(
            "bid", 63000.0,
            trust_score=0.7,
            break_through_risk=0.6,
        )
        snap = _make_snapshot(walls_below=[zone])
        _apply_pressure_alignment([sig], snap, atr=300.0)
        msgs = " ".join(sig.warnings)
        assert "打穿风险评分0.60" in msgs
        assert "60%" not in msgs

    def test_break_through_warning_handles_max_score(self):
        """评分 = 1.0 → 显示 1.00。"""
        sig = _make_long_signal(63000.0)
        zone = _make_zone(
            "bid", 63000.0,
            trust_score=0.7,
            break_through_risk=1.0,
        )
        snap = _make_snapshot(walls_below=[zone])
        _apply_pressure_alignment([sig], snap, atr=300.0)
        msgs = " ".join(sig.warnings)
        assert "打穿风险评分1.00" in msgs
        assert "100%" not in msgs

    def test_removal_risk_warning_uses_two_decimal_float(self):
        sig = _make_long_signal(63000.0)
        zone = _make_zone(
            "bid", 63000.0,
            trust_score=0.4,
            wall_removal_risk=0.83,
        )
        snap = _make_snapshot(walls_below=[zone])
        _apply_pressure_alignment([sig], snap, atr=300.0)
        msgs = " ".join(sig.warnings)
        assert "撤单风险评分0.83" in msgs
        assert "83%" not in msgs

    def test_both_risks_no_percent_sign_in_score(self):
        """同时触发两类 warning，断言「评分」字样之后紧跟的是 0.XX 浮点（非 X%）。"""
        import re

        sig = _make_long_signal(63000.0)
        zone = _make_zone(
            "bid", 63000.0,
            trust_score=0.4,
            wall_removal_risk=0.7,
            break_through_risk=0.65,
            next_magnet_price=61500.0,
        )
        snap = _make_snapshot(walls_below=[zone])
        _apply_pressure_alignment([sig], snap, atr=300.0)
        # 至少各触发一条
        score_warnings = [w for w in sig.warnings if "评分" in w]
        assert len(score_warnings) >= 2

        # 关键正则：「评分」后必须紧跟 0.XX 或 1.00（两位小数浮点），禁止 \d+%
        score_float_pattern = re.compile(r"评分(0\.\d{2}|1\.00)")
        score_pct_pattern = re.compile(r"评分\d+(\.\d+)?%")
        for w in score_warnings:
            assert score_float_pattern.search(w), (
                f"评分必须以 0.XX 浮点展示，发现非合规 warning: {w!r}"
            )
            assert not score_pct_pattern.search(w), (
                f"评分禁止以 X% 展示，发现: {w!r}"
            )

    def test_warning_score_and_magnet_combination(self):
        """评分 + 磁铁价位 同时出现时，评分仍是 0.XX，磁铁仍是 $X。"""
        sig = _make_long_signal(63000.0)
        zone = _make_zone(
            "bid", 63000.0,
            trust_score=0.7,
            break_through_risk=0.72,
            next_magnet_price=61500.0,
        )
        snap = _make_snapshot(walls_below=[zone])
        _apply_pressure_alignment([sig], snap, atr=300.0)
        msgs = " ".join(sig.warnings)
        assert "打穿风险评分0.72" in msgs
        assert "下方磁铁$61,500" in msgs

    def test_no_probability_phrasing(self):
        """禁用"概率""可能性""可能 X%"等概率暗示词。"""
        sig = _make_long_signal(63000.0)
        zone = _make_zone(
            "bid", 63000.0,
            trust_score=0.4,
            wall_removal_risk=0.7,
            break_through_risk=0.65,
            next_magnet_price=61500.0,
        )
        snap = _make_snapshot(walls_below=[zone])
        _apply_pressure_alignment([sig], snap, atr=300.0)
        msgs = " ".join(sig.warnings)
        for word in ("概率", "可能性"):
            assert word not in msgs, f"warning 不得包含概率词「{word}」: {msgs!r}"
