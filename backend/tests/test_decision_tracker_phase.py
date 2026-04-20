"""D16 / D17 自动聚合 + 上报单测"""

from __future__ import annotations

from utils.decision_tracker import D, DecisionTracker


def _mark_all_others(t: DecisionTracker, status: str = "ok") -> None:
    for did in [
        D.D01_REGIME, D.D02_DUAL_ENGINE, D.D03_SAFETY_GATE,
        D.D04_BACKTEST_LOOP, D.D05_CASCADE_FIX, D.D06_BEGINNER_UI,
        D.D07_NEWS_SOURCES, D.D08_NEWS_PIPELINE, D.D09_NEWS_BRIEF,
        D.D10_FLIP_FLOP, D.D11_GEO_RISK, D.D12_DS_DUAL_TASK,
        D.D13_NEWS_AGENT, D.D14_AI_TRADER, D.D15_FUSION,
        D.D17_FACTOR_MATRIX,
    ]:
        t.mark(did, status=status, log=False)


class TestD16PhaseInference:
    def test_all_ok_yields_p2_done(self):
        t = DecisionTracker()
        _mark_all_others(t, status="ok")
        d = t.get_summary_dict()
        d16 = next(x for x in d["decisions"] if x["id"] == D.D16_PHASED_ROADMAP)
        assert d16["status"] == "ok"
        assert d16["metrics"]["current_phase"] == "P2_done"
        assert d16["metrics"]["pending_count"] == 0

    def test_partial_pending_yields_p2_partial(self):
        """已 mark 前 15 项 ok，仅 D17 pending → 仍是 pending（差一步收官），不是 warn"""
        t = DecisionTracker()
        for did in [
            D.D01_REGIME, D.D02_DUAL_ENGINE, D.D03_SAFETY_GATE,
            D.D04_BACKTEST_LOOP, D.D05_CASCADE_FIX, D.D06_BEGINNER_UI,
            D.D07_NEWS_SOURCES, D.D08_NEWS_PIPELINE, D.D09_NEWS_BRIEF,
            D.D10_FLIP_FLOP, D.D11_GEO_RISK, D.D12_DS_DUAL_TASK,
            D.D13_NEWS_AGENT, D.D14_AI_TRADER, D.D15_FUSION,
        ]:
            t.mark(did, status="ok", log=False)
        d = t.get_summary_dict()
        d16 = next(x for x in d["decisions"] if x["id"] == D.D16_PHASED_ROADMAP)
        assert d16["status"] == "pending"
        assert d16["metrics"]["current_phase"] == "P2_partial"
        assert d16["metrics"]["pending_count"] == 1
        assert "D17" in d16["metrics"]["pending_ids"]

    def test_no_marks_yields_p0_booting_pending(self):
        """启动期：全部 pending 时 D16 应标 pending（不是 warn，避免启动期误报）"""
        t = DecisionTracker()
        d = t.get_summary_dict()
        d16 = next(x for x in d["decisions"] if x["id"] == D.D16_PHASED_ROADMAP)
        assert d16["status"] == "pending"
        assert d16["metrics"]["current_phase"] == "P0_booting"
        assert d16["metrics"]["pending_count"] == 16

    def test_partial_progress_yields_rolling_pending(self):
        """冷启后半程：部分 ok + 部分 pending → 仍算 pending（progress 而非降级）"""
        t = DecisionTracker()
        for did in [
            D.D01_REGIME, D.D02_DUAL_ENGINE, D.D03_SAFETY_GATE,
            D.D04_BACKTEST_LOOP, D.D05_CASCADE_FIX, D.D06_BEGINNER_UI,
            D.D07_NEWS_SOURCES, D.D08_NEWS_PIPELINE, D.D09_NEWS_BRIEF,
        ]:
            t.mark(did, status="ok", log=False)
        d = t.get_summary_dict()
        d16 = next(x for x in d["decisions"] if x["id"] == D.D16_PHASED_ROADMAP)
        assert d16["status"] == "pending"
        assert d16["metrics"]["current_phase"] == "P1_rolling"
        assert d16["metrics"]["pending_count"] == 7

    def test_real_warn_elsewhere_propagates(self):
        """其它项真实 warn 时 D16 跟随 warn（区别于 pending）"""
        t = DecisionTracker()
        _mark_all_others(t, status="ok")
        t.mark(D.D07_NEWS_SOURCES, status="warn", log=False)
        d = t.get_summary_dict()
        d16 = next(x for x in d["decisions"] if x["id"] == D.D16_PHASED_ROADMAP)
        assert d16["status"] == "warn"
        assert d16["metrics"]["observed_warn"] == 1

    def test_any_failed_yields_degraded(self):
        t = DecisionTracker()
        _mark_all_others(t, status="ok")
        t.mark(D.D03_SAFETY_GATE, status="failed", log=False)
        d = t.get_summary_dict()
        d16 = next(x for x in d["decisions"] if x["id"] == D.D16_PHASED_ROADMAP)
        assert d16["status"] == "failed"
        assert d16["metrics"]["current_phase"] == "P2_degraded"
        assert d16["metrics"]["observed_fail"] == 1
