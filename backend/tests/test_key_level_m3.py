"""
Key Level V3 · M3（架构精装：lifecycle + regime-aware）单元测试

覆盖点：
1. make_level_id：稳定性 / ATR 自适应 / 翻转保稳定 / 边界
2. apply_regime_modifier：6 regime × 8 evidence_group 表查询
3. _detect_lifecycle_events：born / strengthening / weakening / tier_upgraded / tier_downgraded / flipped
4. _set_state：状态变更触发 lifecycle event 写入（tracker_v2）
5. score_and_build_snapshot：端到端含 regime → snapshot 头部 + level 字段
6. _merge_with_prev：level_id 继承 + lifecycle_events 不双写
7. _trim_lifecycle_events：超过 20 条自动截断
"""
from __future__ import annotations

import time

from models.key_level import (
    KeyLevelSnapshotV2,
    KeyLevelV2,
    LifecycleEvent,
)
from models.regime import RegimeFeatures, RegimeSnapshot
from processors.confluence_scoring import (
    REGIME_MODIFIER_TABLE,
    REGIME_WEIGHT_VERSION,
    _Cluster,
    _detect_lifecycle_events,
    _merge_with_prev,
    _trim_lifecycle_events,
    apply_regime_modifier,
    make_level_id,
    score_and_build_snapshot,
)
from processors.key_level_tracker_v2 import _set_state
from processors.level_discovery import (
    DiscoveryResult,
    RawCandidate,
    resolve_evidence_group,
)


# ─────────────────────────────────────────────────────────────────
# 1. make_level_id 稳定性
# ─────────────────────────────────────────────────────────────────

class TestMakeLevelId:
    def test_basic_format(self):
        """正常输入 → 12 字符 hex。"""
        lid = make_level_id(63000, 400)
        assert isinstance(lid, str)
        assert len(lid) == 12
        assert all(c in "0123456789abcdef" for c in lid)

    def test_idempotent(self):
        """相同输入 → 相同 ID。"""
        assert make_level_id(63000, 400) == make_level_id(63000, 400)

    def test_atr_bucket_stability(self):
        """同一 level 在 ±0.5×ATR 范围内移动 → 同一 ID。"""
        # bucket_size = max(400*0.5, 63000*0.001) = 200
        # 63000 / 200 = 315.0 → bucket=315
        # 63100 / 200 = 315.5 → round → 316（注意：banker's round）
        # 实际：63100 / 200 = 315.5 → round(315.5)=316（python 偶数舍入）
        # 用更安全的差距：±90 以内同 bucket
        assert make_level_id(63000, 400) == make_level_id(63050, 400)
        assert make_level_id(63000, 400) == make_level_id(62950, 400)

    def test_different_buckets(self):
        """跨 bucket → 不同 ID。"""
        # 63000 vs 65000，bucket_size=200 → 不同 bucket
        assert make_level_id(63000, 400) != make_level_id(65000, 400)

    def test_zero_price_returns_empty(self):
        assert make_level_id(0, 400) == ""
        assert make_level_id(-100, 400) == ""

    def test_zero_atr_uses_price_floor(self):
        """ATR=0 时用 price*0.001 兜底，不崩溃。"""
        lid = make_level_id(63000, 0)
        assert lid != ""
        assert len(lid) == 12

    def test_side_independent(self):
        """level_id 不含 side（M3 设计：side 翻转时 ID 保持稳定）。

        side 翻转的语义由 lifecycle_events 中的 'flipped' 事件承载，
        而不是通过 ID 变化来表达。
        """
        # 同 price + 同 atr → 必同 ID（无论 side）
        lid_a = make_level_id(63000, 400)
        lid_b = make_level_id(63000, 400)
        assert lid_a == lid_b


# ─────────────────────────────────────────────────────────────────
# 2. apply_regime_modifier 表查询
# ─────────────────────────────────────────────────────────────────

class TestApplyRegimeModifier:
    def _make_lv(self, evidence_groups: list[str]) -> KeyLevelV2:
        return KeyLevelV2(price=100, side="support", evidence_groups=evidence_groups)

    def test_empty_regime_returns_1(self):
        lv = self._make_lv(["structure_anchor"])
        assert apply_regime_modifier(lv, "") == 1.0
        assert apply_regime_modifier(lv, "unknown_regime") == 1.0

    def test_empty_evidence_groups_returns_1(self):
        lv = self._make_lv([])
        assert apply_regime_modifier(lv, "trend_up") == 1.0

    def test_trend_up_structure_anchor(self):
        """trend_up 下 structure_anchor 应为 1.10。"""
        lv = self._make_lv(["structure_anchor"])
        assert apply_regime_modifier(lv, "trend_up") == 1.10

    def test_range_local_technical_boost(self):
        """range 下 local_technical / liquidation_short / microstructure_local 应为 1.10。"""
        lv = self._make_lv(["local_technical"])
        assert apply_regime_modifier(lv, "range") == 1.10
        lv2 = self._make_lv(["liquidation_short"])
        assert apply_regime_modifier(lv2, "range") == 1.10

    def test_extreme_all_lowered(self):
        """extreme 下所有组都 ×0.85。"""
        for grp in ["structure_anchor", "macro_technical", "liquidation_macro"]:
            lv = self._make_lv([grp])
            assert apply_regime_modifier(lv, "extreme") == 0.85

    def test_high_vol_chop_all_09(self):
        for grp in ["structure_anchor", "flow_dynamic"]:
            lv = self._make_lv([grp])
            assert apply_regime_modifier(lv, "high_vol_chop") == 0.90

    def test_squeeze_all_095(self):
        for grp in ["structure_anchor", "local_technical"]:
            lv = self._make_lv([grp])
            assert apply_regime_modifier(lv, "squeeze") == 0.95

    def test_max_strategy_picks_best_group(self):
        """多组取最大乘子（避免单一弱组拖累共振强位）。

        range 下：structure_anchor=1.05, local_technical=1.10
        → 应取 1.10
        """
        lv = self._make_lv(["structure_anchor", "local_technical"])
        assert apply_regime_modifier(lv, "range") == 1.10

    def test_table_completeness(self):
        """6 种 regime × 8 组都必须有显式条目。"""
        regimes = {"trend_up", "trend_down", "range", "squeeze", "high_vol_chop", "extreme"}
        groups = {
            "structure_anchor", "macro_technical", "local_technical",
            "liquidation_macro", "liquidation_meso", "liquidation_short",
            "microstructure_local", "flow_dynamic",
        }
        assert set(REGIME_MODIFIER_TABLE.keys()) == regimes
        for r in regimes:
            assert set(REGIME_MODIFIER_TABLE[r].keys()) == groups, f"regime {r} 缺组"

    def test_modifier_range(self):
        """所有 modifier ∈ [0.85, 1.10]，避免过度放大或抹零。"""
        for r, table in REGIME_MODIFIER_TABLE.items():
            for g, v in table.items():
                assert 0.85 <= v <= 1.10, f"{r}.{g}={v} 越界"


# ─────────────────────────────────────────────────────────────────
# 3. _detect_lifecycle_events
# ─────────────────────────────────────────────────────────────────

class TestDetectLifecycleEvents:
    def _make_lv(self, **kw) -> KeyLevelV2:
        defaults = {
            "price": 100, "side": "support",
            "final_score": 50, "strength_tier": "B",
            "state": "idle",
        }
        defaults.update(kw)
        return KeyLevelV2(**defaults)

    def test_born_when_no_prev(self):
        lv = self._make_lv(final_score=60, strength_tier="A")
        evts = _detect_lifecycle_events(None, lv, now=1000)
        assert len(evts) == 1
        assert evts[0].event_type == "born"
        assert evts[0].ts == 1000
        assert "60" in evts[0].detail
        assert evts[0].tier_after == "A"

    def test_strengthening_score_up_5(self):
        prev = self._make_lv(final_score=50, strength_tier="B")
        new = self._make_lv(final_score=56, strength_tier="B")
        evts = _detect_lifecycle_events(prev, new, now=1000)
        types = [e.event_type for e in evts]
        assert "strengthening" in types

    def test_weakening_score_down_5(self):
        prev = self._make_lv(final_score=70, strength_tier="A")
        new = self._make_lv(final_score=63, strength_tier="A")
        evts = _detect_lifecycle_events(prev, new, now=1000)
        types = [e.event_type for e in evts]
        assert "weakening" in types

    def test_no_event_for_small_change(self):
        """变化 < 5 分 → 不记录 strengthen/weaken（噪声过滤）。"""
        prev = self._make_lv(final_score=50, strength_tier="B")
        new = self._make_lv(final_score=52, strength_tier="B")
        evts = _detect_lifecycle_events(prev, new, now=1000)
        types = [e.event_type for e in evts]
        assert "strengthening" not in types
        assert "weakening" not in types

    def test_tier_upgraded(self):
        prev = self._make_lv(final_score=60, strength_tier="B")
        new = self._make_lv(final_score=70, strength_tier="A")
        evts = _detect_lifecycle_events(prev, new, now=1000)
        types = [e.event_type for e in evts]
        assert "tier_upgraded" in types
        # 同时也会有 strengthening
        assert "strengthening" in types

    def test_tier_downgraded(self):
        prev = self._make_lv(final_score=80, strength_tier="S")
        new = self._make_lv(final_score=65, strength_tier="A")
        evts = _detect_lifecycle_events(prev, new, now=1000)
        types = [e.event_type for e in evts]
        assert "tier_downgraded" in types

    def test_flipped(self):
        """side 翻转 → flipped 事件。"""
        prev = self._make_lv(side="support", final_score=60)
        new = self._make_lv(side="resistance", final_score=60)
        evts = _detect_lifecycle_events(prev, new, now=1000)
        types = [e.event_type for e in evts]
        assert "flipped" in types
        flip_evt = next(e for e in evts if e.event_type == "flipped")
        assert "支撑" in flip_evt.detail
        assert "阻力" in flip_evt.detail


# ─────────────────────────────────────────────────────────────────
# 4. _set_state lifecycle event push（tracker）
# ─────────────────────────────────────────────────────────────────

class TestSetStateLifecycle:
    def test_idle_to_approaching_no_event(self):
        """idle → approaching 不记录（噪声状态过渡）。"""
        lv = KeyLevelV2(price=100, side="support", state="idle")
        _set_state(lv, "approaching", now=1000)
        assert lv.lifecycle_events == []
        assert lv.state == "approaching"

    def test_idle_to_testing_records_event(self):
        lv = KeyLevelV2(price=100, side="support", state="idle")
        _set_state(lv, "testing", now=1000)
        assert len(lv.lifecycle_events) == 1
        assert lv.lifecycle_events[0].event_type == "tested"
        assert lv.lifecycle_events[0].ts == 1000

    def test_testing_to_bounced_records_reacted(self):
        lv = KeyLevelV2(price=100, side="support", state="testing")
        _set_state(lv, "bounced", now=2000)
        assert len(lv.lifecycle_events) == 1
        assert lv.lifecycle_events[0].event_type == "reacted"
        assert lv.bounce_count == 1

    def test_testing_to_broken_records_event(self):
        lv = KeyLevelV2(price=100, side="support", state="testing")
        _set_state(lv, "broken", now=3000)
        assert len(lv.lifecycle_events) == 1
        assert lv.lifecycle_events[0].event_type == "broken"

    def test_broken_to_fake_break_records_event(self):
        lv = KeyLevelV2(price=100, side="support", state="broken")
        _set_state(lv, "fake_break", now=4000)
        assert len(lv.lifecycle_events) == 1
        assert lv.lifecycle_events[0].event_type == "fake_break"

    def test_no_double_record_same_state(self):
        """同状态重复 set 不重复记录。"""
        lv = KeyLevelV2(price=100, side="support", state="testing")
        _set_state(lv, "testing", now=1000)
        assert len(lv.lifecycle_events) == 0

    def test_max_20_events_kept(self):
        """累积超过 20 条自动截断。"""
        lv = KeyLevelV2(price=100, side="support", state="idle")
        for i in range(25):
            # 在 testing 与 bounced 之间反复切换以触发新事件
            target = "testing" if i % 2 == 0 else "bounced"
            _set_state(lv, target, now=1000 + i)
        assert len(lv.lifecycle_events) == 20


# ─────────────────────────────────────────────────────────────────
# 5. _trim_lifecycle_events
# ─────────────────────────────────────────────────────────────────

class TestTrimLifecycleEvents:
    def test_under_20_no_trim(self):
        evts = [LifecycleEvent(ts=i, event_type="tested") for i in range(10)]
        out = _trim_lifecycle_events(evts)
        assert len(out) == 10

    def test_over_20_keeps_last_20(self):
        evts = [LifecycleEvent(ts=i, event_type="tested") for i in range(30)]
        out = _trim_lifecycle_events(evts)
        assert len(out) == 20
        assert out[0].ts == 10  # 最旧的被丢
        assert out[-1].ts == 29


# ─────────────────────────────────────────────────────────────────
# 6. _merge_with_prev：level_id 继承
# ─────────────────────────────────────────────────────────────────

class TestMergeWithPrevLevelId:
    def test_inherits_level_id_when_matched(self):
        prev = KeyLevelV2(price=100, side="support", level_id="abc123def456")
        new = KeyLevelV2(price=100.05, side="support")
        result = _merge_with_prev([new], [prev], price=100, atr=2)
        assert result[0].level_id == "abc123def456"

    def test_no_inheritance_when_not_matched(self):
        prev = KeyLevelV2(price=100, side="support", level_id="abc123def456")
        new = KeyLevelV2(price=200, side="support")
        result = _merge_with_prev([new], [prev], price=100, atr=2)
        # 200 与 100 距离过远 → 不匹配 → 保持原 level_id（默认 ""，由主流程后续填充）
        assert result[0].level_id == ""


# ─────────────────────────────────────────────────────────────────
# 7. score_and_build_snapshot 端到端 + regime
# ─────────────────────────────────────────────────────────────────

def _make_candidate(
    price: float,
    side: str,
    source_tag: str,
    base_score: float = 30,
    dimension: str = "math_indicator",
) -> RawCandidate:
    c = RawCandidate(
        price=price, side=side, dimension=dimension,
        source=f"src_{source_tag}", source_tag=source_tag,
        base_score=base_score, timeframe="1D",
    )
    c.evidence_group = resolve_evidence_group(source_tag)
    return c


def _make_regime_snapshot(regime: str, conf: float = 0.7) -> RegimeSnapshot:
    return RegimeSnapshot(
        coin="BTC",
        ts=int(time.time()),
        regime=regime,
        confidence=conf,
        features=RegimeFeatures(),
        description_cn=f"测试 {regime}",
    )


class TestScoreAndBuildSnapshotWithRegime:
    def _make_discovery(self, candidates: list[RawCandidate]) -> DiscoveryResult:
        return DiscoveryResult(
            candidates=candidates,
            sma200d=None,
            bmsa=None,
            ichimoku=None,
            keltner=None,
            fib_swing_high=0,
            fib_swing_low=0,
            fib_levels=[],
            fib_direction="",
        )

    def test_no_regime_no_modifier(self):
        """未传 regime → snapshot.regime / lv.regime_at_score 均为空。"""
        cands = [
            _make_candidate(100, "support", "vwap", base_score=50),
            _make_candidate(100, "support", "liq_cluster_below_7d", base_score=50),
            _make_candidate(100, "support", "footprint_stacked", base_score=50),
        ]
        snap = score_and_build_snapshot(
            discovery=self._make_discovery(cands),
            current_price=100,
            atr=2,
        )
        assert snap.regime == ""
        assert snap.regime_weight_version == ""
        if snap.levels:
            assert snap.levels[0].regime_at_score == ""
            assert snap.levels[0].regime_modifier_applied == 1.0  # 默认值

    def test_regime_extreme_lowers_score(self):
        """extreme regime → 所有组 ×0.85，score 降 15%。"""
        cands = [_make_candidate(100, "support", "vwap", base_score=80)]
        snap_no = score_and_build_snapshot(
            discovery=self._make_discovery(cands),
            current_price=100, atr=2,
        )
        baseline_score = snap_no.levels[0].final_score if snap_no.levels else 0

        cands2 = [_make_candidate(100, "support", "vwap", base_score=80)]
        snap_extreme = score_and_build_snapshot(
            discovery=self._make_discovery(cands2),
            current_price=100, atr=2,
            regime_snapshot=_make_regime_snapshot("extreme"),
        )
        assert snap_extreme.regime == "extreme"
        assert snap_extreme.regime_weight_version == REGIME_WEIGHT_VERSION
        if snap_extreme.levels:
            lv = snap_extreme.levels[0]
            assert lv.regime_at_score == "extreme"
            assert lv.regime_modifier_applied == 0.85
            # final_score 应较 baseline 下降（容许 1.0 浮点误差）
            assert lv.final_score <= baseline_score * 0.86 + 1.0

    def test_regime_range_boosts_local_technical(self):
        """range regime + local_technical → ×1.10。"""
        cands = [_make_candidate(100, "support", "vwap", base_score=50)]  # local_technical
        snap = score_and_build_snapshot(
            discovery=self._make_discovery(cands),
            current_price=100, atr=2,
            regime_snapshot=_make_regime_snapshot("range"),
        )
        if snap.levels:
            lv = snap.levels[0]
            assert lv.regime_modifier_applied == 1.10
            assert lv.regime_at_score == "range"

    def test_level_id_assigned(self):
        """所有 level 都应有 level_id（12 字符 hex）。"""
        cands = [_make_candidate(100, "support", "vwap", base_score=50)]
        snap = score_and_build_snapshot(
            discovery=self._make_discovery(cands),
            current_price=100, atr=2,
        )
        for lv in snap.levels:
            assert lv.level_id != ""
            assert len(lv.level_id) == 12

    def test_lifecycle_born_on_first_snapshot(self):
        """首次 snapshot（无 prev）→ 每个 level 都有 born 事件。"""
        cands = [_make_candidate(100, "support", "vwap", base_score=50)]
        snap = score_and_build_snapshot(
            discovery=self._make_discovery(cands),
            current_price=100, atr=2,
        )
        for lv in snap.levels:
            assert any(e.event_type == "born" for e in lv.lifecycle_events)

    def test_lifecycle_inheritance_across_snapshots(self):
        """第二次 snapshot 应继承 prev 的 lifecycle_events。"""
        cands1 = [_make_candidate(100, "support", "vwap", base_score=50)]
        snap1 = score_and_build_snapshot(
            discovery=self._make_discovery(cands1),
            current_price=100, atr=2,
        )
        prev_levels = list(snap1.levels)
        prev_event_count = len(prev_levels[0].lifecycle_events) if prev_levels else 0
        prev_id = prev_levels[0].level_id if prev_levels else ""

        # 第二次 snapshot：score 显著上升 → 应触发 strengthening
        cands2 = [
            _make_candidate(100, "support", "vwap", base_score=80),
            _make_candidate(100, "support", "liq_cluster_below_7d", base_score=80),
            _make_candidate(100, "support", "footprint_stacked", base_score=80),
        ]
        snap2 = score_and_build_snapshot(
            discovery=self._make_discovery(cands2),
            current_price=100, atr=2,
            prev_levels=prev_levels,
        )
        # 同 level_id 应被找到并继承
        same_lv = next((lv for lv in snap2.levels if lv.level_id == prev_id), None)
        assert same_lv is not None
        # 事件数应增加（prev events + new events）
        assert len(same_lv.lifecycle_events) > prev_event_count
        # 最后一条应是新增（strengthening 或 tier_upgraded）
        last_types = [e.event_type for e in same_lv.lifecycle_events[-3:]]
        assert ("strengthening" in last_types) or ("tier_upgraded" in last_types)

    def test_regime_snapshot_metadata(self):
        """snapshot 头部应正确写入 regime / confidence / description / version。"""
        cands = [_make_candidate(100, "support", "vwap", base_score=50)]
        snap = score_and_build_snapshot(
            discovery=self._make_discovery(cands),
            current_price=100, atr=2,
            regime_snapshot=_make_regime_snapshot("trend_up", conf=0.85),
        )
        assert snap.regime == "trend_up"
        assert snap.regime_confidence == 0.85
        assert snap.regime_description == "测试 trend_up"
        assert snap.regime_weight_version == REGIME_WEIGHT_VERSION
