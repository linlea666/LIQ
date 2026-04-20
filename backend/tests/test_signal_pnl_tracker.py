"""P2.1 · SignalPnLTracker 单测

覆盖：
  - 入账去重（同 sample_id 不重复创建）
  - 非法样本过滤（SL/entry 方向错）
  - pending → entry_filled → tp1_hit / tp2_hit / sl_hit
  - expired（72h 超时，入场前/入场后两种）
  - invalidated
  - MFE / MAE 动态更新
  - 多 origin / tier 聚合统计
  - 持久化 round-trip
  - archive.jsonl 生成
"""

from __future__ import annotations

import json
import os
import time

import pytest

from processors.signal_pnl_tracker import (
    PlanPnLSample,
    SignalPnLTracker,
    _WINDOW_SEC,
    _build_trend_hint,
)


@pytest.fixture
def tmp_tracker(tmp_path):
    data_file = str(tmp_path / "signal_pnl.json")
    archive_file = str(tmp_path / "archive.jsonl")
    return SignalPnLTracker(data_file=data_file, archive_file=archive_file)


class _MockMathPlan:
    def __init__(self, **kw):
        self.coin = kw.get("coin", "BTC")
        self.ts = kw.get("ts", int(time.time()))
        self.priority = 1
        self.action = kw.get("action", "long")
        self.tier_hint = kw.get("tier", "A")
        self.regime = kw.get("regime", "range")
        self.entry_zone_low = kw.get("entry_low", 72000.0)
        self.entry_zone_high = kw.get("entry_high", 72200.0)
        self.stop_loss = kw.get("stop_loss", 71400.0)
        self.tp1 = kw.get("tp1", 73000.0)
        self.tp2 = kw.get("tp2", 74000.0)
        self.rr_ratio = 2.3
        self.current_price = kw.get("current_price", 72500.0)
        self.position_size_pct = 30.0


class _MockAIPlan:
    def __init__(self, **kw):
        self.priority = kw.get("priority", 1)
        self.direction = kw.get("direction", "long")
        self.tier_hint = kw.get("tier", "A")
        self.entry_zone_low = kw.get("entry_low", 72000.0)
        self.entry_zone_high = kw.get("entry_high", 72200.0)
        self.stop_loss = kw.get("stop_loss", 71400.0)
        self.tp1 = kw.get("tp1", 73000.0)
        self.tp2 = kw.get("tp2", 74000.0)
        self.rr_ratio = 2.3
        self.position_suggestion_pct = 25.0


class _MockBrief:
    def __init__(self, action="long", tier_hint="A"):
        self.action = action
        self.tier_hint = tier_hint


class _MockFinal:
    """模拟 FinalDecision：recommended_action ∈ {execute/reduce_size/wait/avoid}
    方向通过 math_brief.action / ai_brief.action 推导
    """
    def __init__(self, **kw):
        self.coin = kw.get("coin", "BTC")
        self.ts = kw.get("ts", int(time.time()))
        # recommended_action 用 rec_action 区分（保留 action kwarg 兼容老测试）
        self.recommended_action = kw.get("rec_action", "execute")
        self.current_price = kw.get("current_price", 72500.0)
        # 方向通过 brief 传递
        direction = kw.get("action", "long")
        if direction in {"wait", "avoid"}:
            # 老用法：action=wait → 等价于 recommended_action=wait
            self.recommended_action = direction
            self.math_brief = _MockBrief(action="wait", tier_hint="C")
            self.ai_brief = _MockBrief(action="wait", tier_hint="C")
        else:
            self.math_brief = _MockBrief(action=direction, tier_hint=kw.get("tier", "A"))
            self.ai_brief = _MockBrief(action=direction, tier_hint=kw.get("tier", "A"))
        self.entry_zone_low = kw.get("entry_low", 72000.0)
        self.entry_zone_high = kw.get("entry_high", 72200.0)
        self.stop_loss = kw.get("stop_loss", 71400.0)
        self.tp1 = kw.get("tp1", 73000.0)
        self.tp2 = kw.get("tp2", 74000.0)
        self.rr_ratio = 2.3
        self.recommended_position_pct = 30.0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 入账 & 去重
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestRecord:
    def test_record_math_plan(self, tmp_tracker):
        sid = tmp_tracker.record_math_plan(_MockMathPlan(), current_price=72500)
        assert sid is not None
        assert len(tmp_tracker.get_recent("BTC")) == 1

    def test_dedup_same_plan(self, tmp_tracker):
        p = _MockMathPlan()
        sid1 = tmp_tracker.record_math_plan(p, 72500)
        sid2 = tmp_tracker.record_math_plan(p, 72500)
        assert sid1 is not None
        assert sid2 is None
        assert len(tmp_tracker.get_recent("BTC")) == 1

    def test_reject_wait_action(self, tmp_tracker):
        sid = tmp_tracker.record_math_plan(
            _MockMathPlan(action="wait"), 72500,
        )
        assert sid is None

    def test_reject_long_with_sl_above_entry(self, tmp_tracker):
        sid = tmp_tracker.record_math_plan(
            _MockMathPlan(action="long", entry_low=72000, stop_loss=73000),
            72500,
        )
        assert sid is None

    def test_reject_short_with_sl_below_entry(self, tmp_tracker):
        sid = tmp_tracker.record_math_plan(
            _MockMathPlan(action="short", entry_low=72000, stop_loss=71000),
            72500,
        )
        assert sid is None

    def test_record_multiple_ai_plans(self, tmp_tracker):
        plans = [
            _MockAIPlan(priority=1, direction="long",
                        entry_low=72000, stop_loss=71400),
            _MockAIPlan(priority=2, direction="short",
                        entry_low=72800, stop_loss=73500,
                        tp1=71500, tp2=70500),
        ]
        ids = tmp_tracker.record_ai_plans(
            coin="BTC", trading_plans=plans, current_price=72500,
        )
        assert len(ids) == 2
        assert len(tmp_tracker.get_recent("BTC")) == 2

    def test_record_final_skips_wait(self, tmp_tracker):
        sid = tmp_tracker.record_final_decision(
            _MockFinal(action="wait"), 72500,
        )
        assert sid is None

    def test_record_final_execute_long(self, tmp_tracker):
        """融合 execute + math_brief.action=long 应被记录"""
        sid = tmp_tracker.record_final_decision(
            _MockFinal(action="long", rec_action="execute"), 72500,
        )
        assert sid is not None
        samples = tmp_tracker.get_recent("BTC")
        assert len(samples) == 1
        assert samples[0]["origin"] == "final"
        assert samples[0]["action"] == "long"
        assert samples[0]["tier"] == "A"
        assert samples[0]["entry_low"] == 72000.0
        assert samples[0]["position_pct"] == 30.0

    def test_record_final_reduce_size_short(self, tmp_tracker):
        """reduce_size 也应被记录（只是仓位较小）"""
        sid = tmp_tracker.record_final_decision(
            _MockFinal(
                action="short", rec_action="reduce_size",
                entry_low=72800, stop_loss=73500,
                tp1=71500, tp2=70500,
            ),
            72500,
        )
        assert sid is not None
        assert tmp_tracker.get_recent("BTC")[0]["action"] == "short"

    def test_record_final_skips_no_direction(self, tmp_tracker):
        """recommended_action=execute 但两个 brief 都 wait → 没方向 → 跳过"""
        final = _MockFinal(action="long", rec_action="execute")
        final.math_brief = _MockBrief(action="wait")
        final.ai_brief = _MockBrief(action="wait")
        sid = tmp_tracker.record_final_decision(final, 72500)
        assert sid is None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Tick 状态机
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestTickTransitions:
    def test_long_entry_fills_then_tp1_hit(self, tmp_tracker):
        tmp_tracker.record_math_plan(_MockMathPlan(), 72500)
        # 价格跌入 entry_zone（72200）
        tmp_tracker.tick("BTC", 72100, now_ts=int(time.time()) + 60)
        rec = tmp_tracker.get_recent("BTC")[0]
        assert rec["outcome"] == "entry_filled"
        assert rec["entry_filled_ts"] is not None
        # 价格涨到 tp1 73000
        tmp_tracker.tick("BTC", 73050, now_ts=int(time.time()) + 120)
        rec = tmp_tracker.get_recent("BTC")[0]
        assert rec["outcome"] == "tp1_hit"
        assert rec["r_multiple"] is not None and rec["r_multiple"] > 0

    def test_long_tp2_hit(self, tmp_tracker):
        tmp_tracker.record_math_plan(_MockMathPlan(), 72500)
        tmp_tracker.tick("BTC", 72100, now_ts=int(time.time()) + 60)
        tmp_tracker.tick("BTC", 74200, now_ts=int(time.time()) + 120)
        rec = tmp_tracker.get_recent("BTC")[0]
        assert rec["outcome"] == "tp2_hit"

    def test_long_sl_hit(self, tmp_tracker):
        tmp_tracker.record_math_plan(_MockMathPlan(), 72500)
        tmp_tracker.tick("BTC", 72100, now_ts=int(time.time()) + 60)
        tmp_tracker.tick("BTC", 71300, now_ts=int(time.time()) + 120)
        rec = tmp_tracker.get_recent("BTC")[0]
        assert rec["outcome"] == "sl_hit"
        assert rec["r_multiple"] == -1.0

    def test_short_flow_tp_hit(self, tmp_tracker):
        tmp_tracker.record_math_plan(
            _MockMathPlan(
                action="short", entry_low=72800, entry_high=73000,
                stop_loss=73600, tp1=72000, tp2=71000,
            ),
            72500,
        )
        # 反弹到 72900 触发入场
        tmp_tracker.tick("BTC", 72900, now_ts=int(time.time()) + 60)
        rec = tmp_tracker.get_recent("BTC")[0]
        assert rec["outcome"] == "entry_filled"
        # 跌到 71900 → tp1
        tmp_tracker.tick("BTC", 71900, now_ts=int(time.time()) + 120)
        rec = tmp_tracker.get_recent("BTC")[0]
        assert rec["outcome"] == "tp1_hit"
        assert rec["r_multiple"] > 0

    def test_short_sl_hit(self, tmp_tracker):
        tmp_tracker.record_math_plan(
            _MockMathPlan(
                action="short", entry_low=72800, entry_high=73000,
                stop_loss=73600, tp1=72000, tp2=71000,
            ),
            72500,
        )
        tmp_tracker.tick("BTC", 72900, now_ts=int(time.time()) + 60)
        tmp_tracker.tick("BTC", 73700, now_ts=int(time.time()) + 120)
        rec = tmp_tracker.get_recent("BTC")[0]
        assert rec["outcome"] == "sl_hit"

    def test_expired_before_entry(self, tmp_tracker):
        start = int(time.time())
        tmp_tracker.record_math_plan(_MockMathPlan(), 72500)
        # 72h+1 后 tick，价格还在 entry_zone 之上
        future = start + _WINDOW_SEC + 60
        tmp_tracker.tick("BTC", 72800, now_ts=future)
        rec = tmp_tracker.get_recent("BTC")[0]
        assert rec["outcome"] == "expired"
        # 入场从未触发
        assert rec["entry_filled_ts"] is None

    def test_expired_after_entry(self, tmp_tracker):
        start = int(time.time())
        tmp_tracker.record_math_plan(_MockMathPlan(), 72500)
        tmp_tracker.tick("BTC", 72100, now_ts=start + 60)
        # 入场后 72h+1 价格未触 SL/TP
        tmp_tracker.tick("BTC", 72500, now_ts=start + _WINDOW_SEC + 120)
        rec = tmp_tracker.get_recent("BTC")[0]
        assert rec["outcome"] == "expired"
        assert rec["entry_filled_ts"] is not None

    def test_mark_invalidated(self, tmp_tracker):
        tmp_tracker.record_math_plan(_MockMathPlan(), 72500)
        tmp_tracker.record_math_plan(
            _MockMathPlan(
                action="short", entry_low=72800, entry_high=73000,
                stop_loss=73600, tp1=72000, tp2=71000,
            ),
            72500,
        )
        n = tmp_tracker.mark_invalidated("BTC", reason="blackswan")
        assert n == 2
        for rec in tmp_tracker.get_recent("BTC"):
            assert rec["outcome"] == "invalidated"
            assert rec["invalidation_reason"] == "blackswan"

    def test_mfe_and_mae_tracked(self, tmp_tracker):
        tmp_tracker.record_math_plan(_MockMathPlan(), 72500)
        tmp_tracker.tick("BTC", 72100, now_ts=int(time.time()) + 60)   # entry
        tmp_tracker.tick("BTC", 72800, now_ts=int(time.time()) + 120)  # MFE 上升
        tmp_tracker.tick("BTC", 71900, now_ts=int(time.time()) + 180)  # MAE 下降
        rec = tmp_tracker.get_recent("BTC")[0]
        # 还没触发 SL/TP
        assert rec["outcome"] == "entry_filled"
        assert rec["max_favorable_r"] > 0
        assert rec["max_adverse_r"] < 0

    def test_terminal_sample_not_reprocessed(self, tmp_tracker):
        tmp_tracker.record_math_plan(_MockMathPlan(), 72500)
        tmp_tracker.tick("BTC", 72100, now_ts=int(time.time()) + 60)
        tmp_tracker.tick("BTC", 71300, now_ts=int(time.time()) + 120)  # SL
        rec_before = tmp_tracker.get_recent("BTC")[0]
        tmp_tracker.tick("BTC", 80000, now_ts=int(time.time()) + 180)
        rec_after = tmp_tracker.get_recent("BTC")[0]
        # 终态后不再变化
        assert rec_after["outcome"] == rec_before["outcome"]
        assert rec_after["outcome_price"] == rec_before["outcome_price"]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 聚合统计
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestStats:
    def test_stats_empty(self, tmp_tracker):
        s = tmp_tracker.get_stats("BTC")
        assert s["sample_size"] == 0
        assert s["win_rate"] == 0.0
        assert "暂无" in s["trend_hint_cn"]

    def test_stats_win_rate(self, tmp_tracker):
        start = int(time.time())
        # 3 win (long, 不同 entry 使 dedup 不命中), 2 lose (short)
        long_entries = [(72000, 71400), (71800, 71200), (71600, 71000)]
        for el, sl in long_entries:
            p = _MockMathPlan(
                entry_low=float(el), entry_high=72300.0,
                stop_loss=float(sl), tp1=73000, tp2=74000,
            )
            p.ts = start
            tmp_tracker.record_math_plan(p, 72500)
        short_entries = [(73000, 73800), (73100, 74000)]
        for el, sl in short_entries:
            p = _MockMathPlan(
                action="short", entry_low=float(el), entry_high=73300.0,
                stop_loss=float(sl), tp1=72000, tp2=71000,
            )
            p.ts = start + 100
            tmp_tracker.record_math_plan(p, 72500)

        # 所有 long entry_high 最高是 72200，先 tick 72100 让 3 条 long 入场
        tmp_tracker.tick("BTC", 72100, now_ts=start + 60)
        # 涨到 73050 → 3 条 long 命中 tp1
        tmp_tracker.tick("BTC", 73050, now_ts=start + 120)
        # 让 2 条 short 入场（反弹到 73150 触 entry_low=73000/73100）
        tmp_tracker.tick("BTC", 73150, now_ts=start + 180)
        # 继续上涨 74100 → 2 条 short 依次触 SL（73800 / 74000）
        tmp_tracker.tick("BTC", 74100, now_ts=start + 240)

        s = tmp_tracker.get_stats("BTC")
        assert s["sample_size"] == 5
        assert s["tp_hits"] == 3
        assert s["sl_hits"] == 2
        assert s["win_rate"] == 0.6

    def test_origin_breakdown(self, tmp_tracker):
        start = int(time.time())
        m = _MockMathPlan()
        m.ts = start
        tmp_tracker.record_math_plan(m, 72500)
        tmp_tracker.record_ai_plans(
            "BTC",
            [_MockAIPlan(entry_low=72050.0, entry_high=72250.0,
                         stop_loss=71350.0)],
            72500, created_ts=start,
        )
        bd = tmp_tracker.get_origin_breakdown("BTC")
        assert len(bd) == 3
        origins = {b["origin"] for b in bd}
        assert origins == {"math", "ai", "final"}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 持久化 & 归档
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestPersistence:
    def test_round_trip(self, tmp_path):
        data_file = str(tmp_path / "pnl.json")
        arch_file = str(tmp_path / "arch.jsonl")
        t1 = SignalPnLTracker(data_file=data_file, archive_file=arch_file)
        t1.record_math_plan(_MockMathPlan(), 72500)
        # 触发 persist：持续记录到 _PERSIST_EVERY_N_CHANGES
        for i in range(20):
            p = _MockAIPlan(entry_low=72100 + i, entry_high=72300, stop_loss=71500)
            t1.record_ai_plans("BTC", [p], 72500, created_ts=int(time.time()) + i)
        assert os.path.exists(data_file)
        t2 = SignalPnLTracker(data_file=data_file, archive_file=arch_file)
        # 至少有一条数学 plan + 1 条 AI plan（后续 AI entry_low 有些可能被合成同 id 去重）
        assert len(t2.get_recent("BTC")) >= 2

    def test_corrupt_file_tolerated(self, tmp_path):
        data_file = str(tmp_path / "pnl.json")
        arch_file = str(tmp_path / "arch.jsonl")
        with open(data_file, "w") as f:
            f.write("not json")
        t = SignalPnLTracker(data_file=data_file, archive_file=arch_file)
        assert t.get_recent("BTC") == []
        t.record_math_plan(_MockMathPlan(), 72500)
        assert len(t.get_recent("BTC")) == 1


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Sample dataclass
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestSampleSerialization:
    def test_from_dict_to_dict(self):
        s = PlanPnLSample(
            sample_id="BTC|math|p1|long|72000-72200|sl71400",
            coin="BTC", origin="math",
            action="long", tier="A",
            entry_low=72000, entry_high=72200, stop_loss=71400,
            tp1=73000, tp2=74000,
            created_ts=1700000000, price_at_create=72500,
        )
        d = s.to_dict()
        s2 = PlanPnLSample.from_dict(d)
        assert s2.sample_id == s.sample_id
        assert s2.tp1 == 73000

    def test_from_dict_missing_fields(self):
        s = PlanPnLSample.from_dict({"coin": "ETH"})
        assert s.coin == "ETH"
        assert s.outcome == "pending"


class TestTrendHint:
    def test_low_sample(self):
        assert "样本不足" in _build_trend_hint(0.5, 0.1, 2, 0.5)

    def test_strong_perf(self):
        h = _build_trend_hint(0.65, 0.8, 30, 0.8)
        assert "稳健" in h
        assert "正收益" in h

    def test_weak_perf(self):
        h = _build_trend_hint(0.35, -0.3, 30, 0.8)
        assert "偏低" in h
        assert "亏损" in h

    def test_low_entry_fill_warn(self):
        h = _build_trend_hint(0.6, 0.4, 20, 0.3)
        assert "入场触发率" in h


class TestArchive:
    def test_archive_jsonl_generated(self, tmp_path):
        import processors.signal_pnl_tracker as mod
        data_file = str(tmp_path / "pnl.json")
        arch = str(tmp_path / "arch.jsonl")
        t = SignalPnLTracker(data_file=data_file, archive_file=arch)

        # 把 GC 门槛临时调低
        monkey_threshold = 3
        old = mod._GC_THRESHOLD
        mod._GC_THRESHOLD = monkey_threshold
        mod._MAX_RECORDS_PER_COIN = 2
        try:
            now = int(time.time())
            # 先压 3 条并让它们全部终态 + 已超 72h
            for i in range(3):
                p = _MockMathPlan(entry_low=72000 + i, entry_high=72200)
                p.ts = now - _WINDOW_SEC - 10000
                sid = t.record_math_plan(p, 72500)
                if sid is None:
                    continue
                bucket = t._samples["BTC"]
                for s in bucket:
                    if s.sample_id == sid:
                        s.outcome = "tp1_hit"
                        s.outcome_ts = now - _WINDOW_SEC - 5000
            # 触发一次添加以触 GC
            for i in range(3):
                p = _MockMathPlan(entry_low=75000 + i, entry_high=75200,
                                  stop_loss=74500)
                t.record_math_plan(p, 75100)
            assert os.path.exists(arch)
            with open(arch) as f:
                lines = [l for l in f if l.strip()]
            assert len(lines) >= 1
            # 每行能解析回来
            for line in lines:
                d = json.loads(line)
                PlanPnLSample.from_dict(d)
        finally:
            mod._GC_THRESHOLD = old
