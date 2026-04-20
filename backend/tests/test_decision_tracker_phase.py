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

    def test_partial_warn_yields_p2_partial(self):
        t = DecisionTracker()
        # 只 mark 前 15 项 ok，D17 留 pending
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
        assert d16["status"] == "warn"
        assert d16["metrics"]["current_phase"] == "P2_partial"
        assert d16["metrics"]["pending_count"] == 1
        assert "D17" in d16["metrics"]["pending_ids"]

    def test_no_marks_yields_p0_warn(self):
        t = DecisionTracker()
        d = t.get_summary_dict()
        d16 = next(x for x in d["decisions"] if x["id"] == D.D16_PHASED_ROADMAP)
        assert d16["status"] == "warn"
        assert d16["metrics"]["current_phase"] == "P0"
        assert d16["metrics"]["pending_count"] == 16

    def test_any_failed_yields_degraded(self):
        t = DecisionTracker()
        _mark_all_others(t, status="ok")
        t.mark(D.D03_SAFETY_GATE, status="failed", log=False)
        d = t.get_summary_dict()
        d16 = next(x for x in d["decisions"] if x["id"] == D.D16_PHASED_ROADMAP)
        assert d16["status"] == "failed"
        assert d16["metrics"]["current_phase"] == "P2_degraded"
        assert d16["metrics"]["observed_fail"] == 1
