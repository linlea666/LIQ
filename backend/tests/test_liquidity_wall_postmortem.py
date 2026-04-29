"""W4-T1 流动性墙后验报告 单元测试。

覆盖（按 dev-constraints 第 8 条 a/b/c/d）：
1. 输入解析：JSONL 反序列化、ZoneRecord/EventRecord 提取、字段缺失兜底
2. K 线工具：Binance 元素解析、范围筛选、错误数据跳过
3. 判定函数：consumed/break_through/removal 各 outcome 分支（hit/miss/partial/
   pending/insufficient）+ side 对称性 + 边界条件
4. 统计聚合：HitRateStats 计算、trust 桶、dominant_role 分组、wall_zone_id 去重
5. 报告组装：build_report 端到端、空数据安全、notes 提示
6. 渲染：report_to_dict / report_to_markdown 关键字段存在性
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from processors.liquidity_wall_postmortem import (
    EventRecord,
    HitRateStats,
    JudgedOutcome,
    KlinePoint,
    PostmortemReport,
    ZoneRecord,
    aggregate_outcomes,
    build_report,
    compute_dominant_role_stats,
    compute_trust_buckets,
    deduplicate_zones_by_first_high_score,
    extract_event_records,
    extract_zone_records,
    judge_break_through_outcome,
    judge_consumed_outcome,
    judge_removal_outcome,
    klines_in_range,
    parse_archived_snapshots,
    parse_binance_klines,
    report_to_dict,
    report_to_markdown,
)


# ─────────────────────────────────────────────────────────────────
# 工具：构造测试数据
# ─────────────────────────────────────────────────────────────────

def _make_zone_dict(
    *, side="bid", price_mid=63000.0, ts_in_summary=False, **kw,
) -> dict:
    base = {
        "wall_zone_id": kw.get("wall_zone_id", "z_001"),
        "side": side,
        "source": "depth_only",
        "dual_source": False,
        "price_mid": price_mid,
        "price_low": price_mid * 0.998,
        "price_high": price_mid * 1.002,
        "current_usd": 2_000_000.0,
        "max_usd_1h": 2_500_000.0,
        "trust_score": 0.7,
        "raw_trust_score": 0.7,
        "trust_components": {},
        "support_resistance_trust_score": 0.5,
        "sweep_attractiveness_score": 0.4,
        "break_through_risk": 0.0,
        "active_attack_score": 0.0,
        "wall_removal_risk": 0.0,
        "wall_consumed_confidence": 0.0,
        "persistence_score": 0.7,
        "persistence_min": 30.0,
        "exchange_count": 1,
        "has_spot_confluence": False,
        "spot_current_usd": 0,
        "coinbase_spot_confluence": False,
        "coinbase_spot_usd": 0,
        "coinbase_num_orders": 0,
        "trend": "",
        "dominant_role": "ordinary",
    }
    base.update(kw)
    return base


def _make_snapshot_dict(
    *, ts=1700_000_000, coin="BTC",
    walls_above=None, walls_below=None, events=None,
) -> dict:
    return {
        "ts": ts,
        "coin": coin,
        "last_price": 63000.0,
        "atr": 300.0,
        "data_quality": "ok",
        "walls_above": walls_above or [],
        "walls_below": walls_below or [],
        "wall_events": events or [],
        "crowding_global": None,
        "top_sweep_targets": [],
        "source_age": {},
    }


def _make_event_dict(
    *, ts, wall_zone_id="z_001", event_type="wall_removed",
    side="bid", price_mid=63000.0, confidence=0.7,
    executed_usd_value=None,
) -> dict:
    return {
        "wall_zone_id": wall_zone_id,
        "event_type": event_type,
        "side": side,
        "price_mid": price_mid,
        "ts": ts,
        "size_before_usd": 2_000_000.0,
        "size_after_usd": 0.0,
        "executed_usd_value": executed_usd_value,
        "confidence": confidence,
    }


def _make_kline(ts: int, *, o=63000.0, h=63100.0, l=62900.0, c=63050.0) -> KlinePoint:
    return KlinePoint(ts=ts, open=o, high=h, low=l, close=c)


def _make_zone_record(
    *, ts=1700_000_000, side="bid", price_mid=63000.0,
    wall_zone_id="z_001", **kw,
) -> ZoneRecord:
    base = dict(
        coin="BTC", ts=ts, wall_zone_id=wall_zone_id, side=side,
        price_low=price_mid * 0.998, price_high=price_mid * 1.002,
        price_mid=price_mid, current_usd=2_000_000.0,
        trust_score=0.7, break_through_risk=0.0, wall_removal_risk=0.0,
        wall_consumed_confidence=0.0,
        support_resistance_trust_score=0.5, sweep_attractiveness_score=0.4,
        active_attack_score=0.0,
        persistence_score=0.7, persistence_min=30.0, dominant_role="ordinary",
    )
    base.update(kw)
    return ZoneRecord(**base)


# ═════════════════════════════════════════════════════════════════
# 1. 输入解析
# ═════════════════════════════════════════════════════════════════

class TestParseArchivedSnapshots:
    def test_parses_valid_jsonl(self):
        lines = [
            json.dumps({"ts": 100, "coin": "BTC"}),
            json.dumps({"ts": 200, "coin": "BTC"}),
        ]
        out = parse_archived_snapshots(lines)
        assert len(out) == 2
        assert out[0]["ts"] == 100

    def test_skips_empty_lines(self):
        lines = ["", "   ", json.dumps({"ts": 1, "coin": "BTC"}), "\n"]
        out = parse_archived_snapshots(lines)
        assert len(out) == 1

    def test_skips_invalid_json(self):
        lines = [
            "not json",
            json.dumps({"ts": 1, "coin": "BTC"}),
            "{broken",
        ]
        out = parse_archived_snapshots(lines)
        assert len(out) == 1

    def test_skips_non_dict_top_level(self):
        lines = [
            json.dumps([1, 2, 3]),
            json.dumps({"ts": 1, "coin": "BTC"}),
        ]
        out = parse_archived_snapshots(lines)
        assert len(out) == 1


class TestExtractZoneRecords:
    def test_extracts_above_and_below(self):
        snap = _make_snapshot_dict(
            walls_above=[_make_zone_dict(side="ask", price_mid=64000)],
            walls_below=[_make_zone_dict(side="bid", price_mid=62000)],
        )
        recs = extract_zone_records([snap])
        assert len(recs) == 2
        sides = {r.side for r in recs}
        assert sides == {"ask", "bid"}

    def test_skips_invalid_snapshot(self):
        # 缺 coin / ts
        snaps = [
            {"ts": 0, "coin": "BTC", "walls_above": [_make_zone_dict()]},
            {"ts": 100, "coin": "", "walls_above": [_make_zone_dict()]},
            _make_snapshot_dict(walls_below=[_make_zone_dict()]),
        ]
        recs = extract_zone_records(snaps)
        assert len(recs) == 1

    def test_propagates_optional_fields(self):
        snap = _make_snapshot_dict(walls_below=[_make_zone_dict(
            wall_zone_id="abc",
            dominant_role="dual_battleground",
            next_magnet_price=61000.0,
            dual_source=True,
            coinbase_spot_confluence=True,
        )])
        rec = extract_zone_records([snap])[0]
        assert rec.wall_zone_id == "abc"
        assert rec.dominant_role == "dual_battleground"
        assert rec.next_magnet_price == 61000.0
        assert rec.dual_source is True
        assert rec.coinbase_spot_confluence is True

    def test_handles_missing_fields_with_defaults(self):
        # 极简 wall（仅必要字段），其余应回退默认值
        minimal = {
            "side": "bid",
            "price_low": 62000, "price_high": 62100, "price_mid": 62050,
            "current_usd": 1_000_000,
        }
        snap = _make_snapshot_dict(walls_below=[minimal])
        recs = extract_zone_records([snap])
        assert len(recs) == 1
        assert recs[0].dominant_role == "ordinary"
        assert recs[0].trust_score == 0.0


class TestExtractEventRecords:
    def test_extracts_events_from_snapshots(self):
        snap = _make_snapshot_dict(events=[
            _make_event_dict(ts=1000, event_type="wall_removed"),
            _make_event_dict(ts=2000, event_type="wall_consumed",
                             executed_usd_value=500_000),
        ])
        evs = extract_event_records([snap])
        assert len(evs) == 2
        types = {e.event_type for e in evs}
        assert types == {"wall_removed", "wall_consumed"}

    def test_skips_invalid_ts(self):
        snap = _make_snapshot_dict(events=[
            _make_event_dict(ts=0),
            _make_event_dict(ts=1000),
        ])
        evs = extract_event_records([snap])
        assert len(evs) == 1


# ═════════════════════════════════════════════════════════════════
# 2. K 线工具
# ═════════════════════════════════════════════════════════════════

class TestParseBinanceKlines:
    def test_parses_standard_format(self):
        # Binance 格式：[openTime_ms, o, h, l, c, v, closeTime_ms, ...]
        raw = [
            [1_700_000_000_000, "63000", "63100", "62900", "63050", "10", 1_700_000_300_000],
            [1_700_000_300_000, "63050", "63200", "63000", "63150", "12", 1_700_000_600_000],
        ]
        out = parse_binance_klines(raw)
        assert len(out) == 2
        assert out[0].ts == 1_700_000_000
        assert out[0].open == 63000.0
        assert out[1].close == 63150.0

    def test_sorts_by_ts(self):
        raw = [
            [1_700_000_300_000, "1", "2", "1", "2", "1", 0],
            [1_700_000_000_000, "1", "2", "1", "2", "1", 0],
        ]
        out = parse_binance_klines(raw)
        assert out[0].ts < out[1].ts

    def test_skips_invalid_rows(self):
        raw = [
            [1_700_000_000_000, "1", "2", "1", "2", "1", 0],
            ["bad", "data"],
            [1_700_000_300_000, "abc", "2", "1", "2", "1", 0],  # 非数字
        ]
        out = parse_binance_klines(raw)
        assert len(out) == 1


class TestKlinesInRange:
    def test_filters_inside_range(self):
        kl = [_make_kline(ts) for ts in (100, 200, 300, 400)]
        out = klines_in_range(kl, start_ts=150, end_ts=350)
        assert [k.ts for k in out] == [200, 300]

    def test_invalid_range_returns_empty(self):
        kl = [_make_kline(100)]
        assert klines_in_range(kl, 200, 100) == []
        assert klines_in_range(kl, 100, 100) == []


# ═════════════════════════════════════════════════════════════════
# 3. 判定函数
# ═════════════════════════════════════════════════════════════════

class TestJudgeConsumedOutcome:
    def test_below_threshold_marked_miss(self):
        rec = _make_zone_record(wall_consumed_confidence=0.3)
        out = judge_consumed_outcome(rec, [], window_sec=1800)
        assert out.outcome == "miss"
        assert "below threshold" in out.note

    def test_bid_wall_hit_when_close_below(self):
        rec = _make_zone_record(
            ts=100, side="bid", price_mid=63000.0,
            wall_consumed_confidence=0.7,
        )
        kl = [_make_kline(200, c=62800.0, l=62700.0, h=63100.0)]
        out = judge_consumed_outcome(rec, kl, window_sec=1800)
        assert out.outcome == "hit"

    def test_bid_wall_partial_when_only_low_below(self):
        rec = _make_zone_record(
            ts=100, side="bid", price_mid=63000.0,
            wall_consumed_confidence=0.7,
        )
        # close 在 price_low 之上，但 low 探到 price_low 之下
        kl = [_make_kline(200, c=63100.0, l=62800.0, h=63200.0)]
        out = judge_consumed_outcome(rec, kl, window_sec=1800)
        assert out.outcome == "partial"

    def test_bid_wall_miss_when_held(self):
        rec = _make_zone_record(
            ts=100, side="bid", price_mid=63000.0,
            wall_consumed_confidence=0.7,
        )
        kl = [_make_kline(200, c=63200.0, l=63100.0, h=63300.0)]
        out = judge_consumed_outcome(rec, kl, window_sec=1800)
        assert out.outcome == "miss"

    def test_ask_wall_hit_when_close_above(self):
        rec = _make_zone_record(
            ts=100, side="ask", price_mid=63000.0,
            wall_consumed_confidence=0.7,
        )
        kl = [_make_kline(200, c=63500.0, l=62900.0, h=63600.0)]
        out = judge_consumed_outcome(rec, kl, window_sec=1800)
        assert out.outcome == "hit"

    def test_pending_when_window_not_closed(self):
        rec = _make_zone_record(
            ts=10_000, side="bid", price_mid=63000.0,
            wall_consumed_confidence=0.7,
        )
        out = judge_consumed_outcome(rec, [], window_sec=1800, now_ts=10_500)
        assert out.outcome == "pending"

    def test_insufficient_when_no_klines(self):
        rec = _make_zone_record(
            ts=100, side="bid", price_mid=63000.0,
            wall_consumed_confidence=0.7,
        )
        out = judge_consumed_outcome(rec, [], window_sec=1800)
        assert out.outcome == "insufficient_data"


class TestJudgeBreakThroughOutcome:
    def test_below_threshold_marked_miss(self):
        rec = _make_zone_record(break_through_risk=0.3)
        out = judge_break_through_outcome(rec, [], window_sec=7200)
        assert out.outcome == "miss"

    def test_bid_hit_when_low_reaches_magnet(self):
        rec = _make_zone_record(
            ts=100, side="bid", price_mid=63000.0,
            break_through_risk=0.7, next_magnet_price=61000.0,
        )
        kl = [_make_kline(500, c=62000.0, l=60900.0, h=62500.0)]
        out = judge_break_through_outcome(rec, kl, window_sec=7200)
        assert out.outcome == "hit"
        assert out.metric > 0

    def test_bid_partial_when_breaks_wall_but_not_magnet(self):
        rec = _make_zone_record(
            ts=100, side="bid", price_mid=63000.0,
            break_through_risk=0.7, next_magnet_price=61000.0,
        )
        # close 在 price_low 之下，但 low 没到 magnet
        kl = [_make_kline(500, c=62800.0, l=62500.0, h=63100.0)]
        out = judge_break_through_outcome(rec, kl, window_sec=7200)
        assert out.outcome == "partial"

    def test_bid_miss_when_wall_holds(self):
        rec = _make_zone_record(
            ts=100, side="bid", price_mid=63000.0,
            break_through_risk=0.7, next_magnet_price=61000.0,
        )
        kl = [_make_kline(500, c=63200.0, l=63100.0, h=63300.0)]
        out = judge_break_through_outcome(rec, kl, window_sec=7200)
        assert out.outcome == "miss"

    def test_ask_hit_when_high_reaches_magnet(self):
        rec = _make_zone_record(
            ts=100, side="ask", price_mid=63000.0,
            break_through_risk=0.7, next_magnet_price=64500.0,
        )
        kl = [_make_kline(500, c=64000.0, l=63500.0, h=64600.0)]
        out = judge_break_through_outcome(rec, kl, window_sec=7200)
        assert out.outcome == "hit"

    def test_falls_back_to_breakout_when_no_magnet(self):
        rec = _make_zone_record(
            ts=100, side="bid", price_mid=63000.0,
            break_through_risk=0.7, next_magnet_price=None,
        )
        kl = [_make_kline(500, c=62800.0, l=62500.0, h=63100.0)]
        out = judge_break_through_outcome(rec, kl, window_sec=7200)
        # close < price_low → 退化逻辑下视为 hit
        assert out.outcome == "hit"


class TestJudgeRemovalOutcome:
    def test_below_threshold_marked_miss(self):
        rec = _make_zone_record(wall_removal_risk=0.3)
        out = judge_removal_outcome(rec, [], window_sec=1800)
        assert out.outcome == "miss"

    def test_hit_on_wall_removed_event(self):
        rec = _make_zone_record(
            ts=100, wall_removal_risk=0.7, wall_zone_id="zid",
        )
        events = [EventRecord(
            coin="BTC", ts=500, wall_zone_id="zid",
            event_type="wall_removed", side="bid",
            price_mid=63000.0, confidence=0.7,
        )]
        out = judge_removal_outcome(rec, events, window_sec=1800)
        assert out.outcome == "hit"

    def test_hit_on_consumed_and_removed_event(self):
        rec = _make_zone_record(
            ts=100, wall_removal_risk=0.7, wall_zone_id="zid",
        )
        events = [EventRecord(
            coin="BTC", ts=500, wall_zone_id="zid",
            event_type="wall_consumed_and_removed", side="bid",
            price_mid=63000.0, confidence=0.75,
        )]
        out = judge_removal_outcome(rec, events, window_sec=1800)
        assert out.outcome == "hit"

    def test_partial_on_consumed_only(self):
        rec = _make_zone_record(
            ts=100, wall_removal_risk=0.7, wall_zone_id="zid",
        )
        events = [EventRecord(
            coin="BTC", ts=500, wall_zone_id="zid",
            event_type="wall_consumed", side="bid",
            price_mid=63000.0, confidence=0.7,
        )]
        out = judge_removal_outcome(rec, events, window_sec=1800)
        assert out.outcome == "partial"

    def test_miss_when_no_related_event(self):
        rec = _make_zone_record(
            ts=100, wall_removal_risk=0.7, wall_zone_id="zid",
        )
        # 不同 wall_zone_id 的事件
        events = [EventRecord(
            coin="BTC", ts=500, wall_zone_id="other",
            event_type="wall_removed", side="bid",
            price_mid=63000.0, confidence=0.7,
        )]
        out = judge_removal_outcome(rec, events, window_sec=1800)
        assert out.outcome == "miss"

    def test_insufficient_when_missing_zone_id(self):
        rec = _make_zone_record(
            ts=100, wall_removal_risk=0.7, wall_zone_id="",
        )
        out = judge_removal_outcome(rec, [], window_sec=1800)
        assert out.outcome == "insufficient_data"

    def test_pending_when_window_not_closed(self):
        rec = _make_zone_record(
            ts=10_000, wall_removal_risk=0.7, wall_zone_id="zid",
        )
        out = judge_removal_outcome(rec, [], window_sec=1800, now_ts=10_500)
        assert out.outcome == "pending"


# ═════════════════════════════════════════════════════════════════
# 4. 统计聚合
# ═════════════════════════════════════════════════════════════════

class TestAggregateOutcomes:
    def test_basic_counts(self):
        outcomes = [
            JudgedOutcome("hit"), JudgedOutcome("hit"),
            JudgedOutcome("miss"), JudgedOutcome("partial"),
            JudgedOutcome("insufficient_data"), JudgedOutcome("pending"),
        ]
        s = aggregate_outcomes(outcomes, threshold=0.6)
        assert s.hits == 2
        assert s.misses == 1
        assert s.partials == 1
        assert s.insufficient == 1
        assert s.pending == 1
        assert s.evaluated == 4
        assert s.hit_rate == round(2 / 4, 3)

    def test_excludes_below_threshold_misses(self):
        # 低于阈值的样本算 miss + 特殊 note，应被 sample_count 排除
        outcomes = [
            JudgedOutcome("miss",
                          note="below threshold (not in evaluation set)"),
            JudgedOutcome("hit"),
        ]
        s = aggregate_outcomes(outcomes, threshold=0.6)
        assert s.sample_count == 1
        assert s.hit_rate == 1.0

    def test_zero_evaluated_returns_zero_rate(self):
        s = aggregate_outcomes([JudgedOutcome("pending")], threshold=0.6)
        assert s.evaluated == 0
        assert s.hit_rate == 0.0

    def test_partial_or_hit_rate(self):
        outcomes = [
            JudgedOutcome("hit"), JudgedOutcome("partial"),
            JudgedOutcome("miss"), JudgedOutcome("miss"),
        ]
        s = aggregate_outcomes(outcomes, threshold=0.6)
        assert s.partial_or_hit_rate == 0.5  # (1+1) / 4


class TestComputeTrustBuckets:
    def test_bucket_assignment(self):
        recs = [
            _make_zone_record(trust_score=0.6, persistence_min=20),
            _make_zone_record(trust_score=0.7, persistence_min=40),
            _make_zone_record(trust_score=0.8, persistence_min=60),
            _make_zone_record(trust_score=0.5),  # 低于 0.55 排除
        ]
        out = compute_trust_buckets(recs)
        labels = {b.bucket_label: b.sample_count for b in out}
        assert labels["0.55-0.65"] == 1
        assert labels["0.65-0.75"] == 1
        assert labels["0.75-1.00"] == 1

    def test_empty_buckets_return_zero(self):
        out = compute_trust_buckets([])
        assert all(b.sample_count == 0 for b in out)


class TestComputeDominantRoleStats:
    def test_groups_by_role(self):
        recs = [
            _make_zone_record(dominant_role="dual_battleground", trust_score=0.9),
            _make_zone_record(dominant_role="dual_battleground", trust_score=0.85),
            _make_zone_record(dominant_role="ordinary", trust_score=0.5),
        ]
        stats = compute_dominant_role_stats(recs)
        by_role = {s.role: s for s in stats}
        assert by_role["dual_battleground"].sample_count == 2
        assert by_role["ordinary"].sample_count == 1
        assert by_role["dual_battleground"].avg_trust > 0.8

    def test_empty_input(self):
        assert compute_dominant_role_stats([]) == []


class TestDeduplicateZonesByFirstHighScore:
    def test_keeps_first_occurrence_per_id(self):
        rec1 = _make_zone_record(ts=100, wall_zone_id="z1",
                                 break_through_risk=0.7)
        rec2 = _make_zone_record(ts=200, wall_zone_id="z1",
                                 break_through_risk=0.8)
        rec3 = _make_zone_record(ts=300, wall_zone_id="z2",
                                 break_through_risk=0.7)
        out = deduplicate_zones_by_first_high_score(
            [rec3, rec1, rec2],
            score_attr="break_through_risk",
            threshold=0.6,
        )
        # 应只有 z1 最早一帧 + z2
        ids_to_ts = {r.wall_zone_id: r.ts for r in out}
        assert ids_to_ts == {"z1": 100, "z2": 300}

    def test_filters_below_threshold(self):
        rec = _make_zone_record(break_through_risk=0.3, wall_zone_id="z1")
        out = deduplicate_zones_by_first_high_score(
            [rec], score_attr="break_through_risk", threshold=0.6,
        )
        assert out == []

    def test_handles_missing_wall_zone_id(self):
        rec1 = _make_zone_record(ts=100, wall_zone_id="",
                                 break_through_risk=0.7)
        rec2 = _make_zone_record(ts=100, wall_zone_id="",
                                 break_through_risk=0.7,
                                 price_mid=64000.0)
        out = deduplicate_zones_by_first_high_score(
            [rec1, rec2], score_attr="break_through_risk", threshold=0.6,
        )
        # 不同 price_mid 应作为不同记录保留
        assert len(out) == 2


# ═════════════════════════════════════════════════════════════════
# 5. 报告组装（端到端）
# ═════════════════════════════════════════════════════════════════

class TestBuildReport:
    def test_empty_input_safe(self):
        rep = build_report("BTC", [], [])
        assert rep.coin == "BTC"
        assert rep.total_snapshots == 0
        assert rep.total_zone_records == 0
        assert rep.consumed_stats.evaluated == 0
        assert "no zone records" in " ".join(rep.notes)

    def test_full_pipeline_smoke(self):
        snap_t = 100
        snap = _make_snapshot_dict(
            ts=snap_t,
            walls_below=[_make_zone_dict(
                wall_zone_id="z_a", side="bid", price_mid=63000.0,
                wall_consumed_confidence=0.8,
                break_through_risk=0.7,
                next_magnet_price=61000.0,
                wall_removal_risk=0.7,
                trust_score=0.8,
                dominant_role="institutional_footprint",
            )],
            events=[_make_event_dict(
                ts=snap_t + 600, wall_zone_id="z_a",
                event_type="wall_removed", side="bid", price_mid=63000.0,
            )],
        )
        # 让 break_through hit：close 跌到 magnet 之下
        klines = [_make_kline(snap_t + 1000, c=60800.0, l=60500.0, h=61500.0)]

        rep = build_report("BTC", [snap], klines, now_ts=snap_t + 10_000)

        assert rep.total_zone_records == 1
        assert rep.total_events == 1
        # 三类后验都应至少评估到该样本
        assert rep.consumed_stats.evaluated >= 1
        assert rep.break_through_stats.evaluated >= 1
        assert rep.removal_stats.hits >= 1
        # dominant_role 包含 institutional_footprint
        roles = {r.role for r in rep.dominant_role_stats}
        assert "institutional_footprint" in roles

    def test_pending_classification_when_now_inside_window(self):
        snap_t = 100
        snap = _make_snapshot_dict(
            ts=snap_t,
            walls_below=[_make_zone_dict(
                wall_zone_id="z_b", side="bid", price_mid=63000.0,
                wall_consumed_confidence=0.7,
            )],
        )
        rep = build_report("BTC", [snap], [], now_ts=snap_t + 100)
        # window_sec=1800，now=200 → 仍 pending
        assert rep.consumed_stats.pending >= 1


# ═════════════════════════════════════════════════════════════════
# 6. 渲染
# ═════════════════════════════════════════════════════════════════

class TestRendering:
    def _make_smoke_report(self) -> PostmortemReport:
        snap = _make_snapshot_dict(
            ts=100,
            walls_below=[_make_zone_dict(
                wall_zone_id="z_a", break_through_risk=0.7,
                next_magnet_price=61000.0,
                trust_score=0.8, dominant_role="dual_battleground",
            )],
        )
        kl = [_make_kline(500, c=60800.0, l=60500.0, h=61500.0)]
        return build_report("BTC", [snap], kl, now_ts=10_000)

    def test_report_to_dict_keys_present(self):
        rep = self._make_smoke_report()
        d = report_to_dict(rep)
        for key in ("coin", "totals", "consumed_stats", "break_through_stats",
                    "removal_stats", "trust_buckets", "dominant_role_stats",
                    "high_sr_hit_rate", "high_sa_hit_rate", "notes"):
            assert key in d
        assert d["coin"] == "BTC"
        # JSON-serializable
        json.dumps(d)

    def test_report_to_markdown_contains_sections(self):
        rep = self._make_smoke_report()
        md = report_to_markdown(rep)
        # 关键章节标题
        assert "# 流动性墙后验报告" in md
        assert "wall_consumed_confidence" in md
        assert "break_through_risk" in md
        assert "wall_removal_risk" in md
        assert "trust_score 区分度" in md
        assert "dominant_role 分布" in md
        # W3-T3 口径：明确"事后命中率/不是统计概率"
        assert "事后命中率" in md
        assert "不是统计概率" in md

    def test_markdown_lists_dominant_roles(self):
        rep = self._make_smoke_report()
        md = report_to_markdown(rep)
        # 至少应包含 dual_battleground 角色行
        assert "dual_battleground" in md

    def test_markdown_handles_empty_report(self):
        rep = build_report("BTC", [], [])
        md = report_to_markdown(rep)
        # 不应崩溃，且应包含"无样本"提示
        assert "评估样本为 0" in md or "没有" in md or "no zone records" in md
