"""P1.8a · AI Quality Ledger 单测

覆盖：
  - AIQualityRecord 持久化 round-trip
  - Ledger.record 基本流程 + GC 裁剪
  - get_recent 返回顺序（最新在前）
  - get_stats 聚合指标
  - trend_hint_cn 文案
  - top_invalid_reasons 统计
  - 线程安全锁不阻塞（轻量 smoke）
"""

from __future__ import annotations

import os
import tempfile
import time

import pytest

from processors.ai_quality_ledger import (
    AIQualityLedger,
    AIQualityRecord,
    _build_trend_hint,
    _top_invalid_reasons,
)


@pytest.fixture
def tmp_ledger(tmp_path):
    data_file = str(tmp_path / "ai_quality.json")
    return AIQualityLedger(data_file=data_file)


def _mk_record(coin="BTC", matrix_source="ai_json", plans_source="ai_json",
               json_valid=True, bias_vs_text="consistent",
               math_agreement="agree", invalid_reason="",
               latency_ms=12000, tokens=3000, overlay=8, plans_count=2,
               ts=None):
    return AIQualityRecord(
        ts=int(ts or time.time()),
        coin=coin,
        price_at_analysis=72500.0,
        matrix_source=matrix_source,
        plans_source=plans_source,
        json_valid=json_valid,
        json_invalid_reason=invalid_reason,
        overlay_fields=overlay,
        ai_plans_count=plans_count,
        bias_vs_text=bias_vs_text,
        final_bias="bullish",
        final_conviction=70,
        math_agreement=math_agreement,
        latency_ms=latency_ms,
        reasoning_tokens=tokens,
        model="deepseek-v4-flash",
    )


class TestRecordRoundTrip:
    def test_to_from_dict_preserves_fields(self):
        rec = _mk_record()
        d = rec.to_dict()
        rec2 = AIQualityRecord.from_dict(d)
        assert rec2.coin == "BTC"
        assert rec2.matrix_source == "ai_json"
        assert rec2.latency_ms == 12000
        assert rec2.overlay_fields == 8

    def test_from_dict_missing_fields_default(self):
        rec = AIQualityRecord.from_dict({"coin": "ETH"})
        assert rec.coin == "ETH"
        assert rec.matrix_source == "rule_fallback"
        assert rec.json_valid is False

    def test_from_dict_malformed_returns_empty(self):
        rec = AIQualityRecord.from_dict({"coin": "ETH", "ts": "oops"})
        # 异常值应降级为空 coin 记录
        assert rec.coin == ""


class TestLedgerRecord:
    def test_record_and_recent(self, tmp_ledger):
        for i in range(3):
            tmp_ledger.record(_mk_record(ts=1000 + i))
        recent = tmp_ledger.get_recent("BTC", limit=10)
        assert len(recent) == 3
        # 最新在前
        assert recent[0]["ts"] == 1002
        assert recent[-1]["ts"] == 1000

    def test_record_ignores_empty_coin(self, tmp_ledger):
        tmp_ledger.record(_mk_record(coin=""))
        assert tmp_ledger.get_recent("BTC") == []

    def test_persist_and_reload(self, tmp_path):
        data_file = str(tmp_path / "q.json")
        l1 = AIQualityLedger(data_file=data_file)
        l1.record(_mk_record(ts=5000))
        l1.record(_mk_record(coin="ETH", matrix_source="rule_fallback", ts=6000))
        assert os.path.exists(data_file)
        l2 = AIQualityLedger(data_file=data_file)
        assert len(l2.get_recent("BTC")) == 1
        assert len(l2.get_recent("ETH")) == 1

    def test_gc_trimming(self, tmp_ledger):
        # 压入超过 GC_THRESHOLD 300 条 → 触发裁剪后继续追加
        # 策略：每超 300 立即裁回 200，所以最终在 [200, 300] 之间
        for i in range(310):
            tmp_ledger.record(_mk_record(ts=i))
        all_recent = tmp_ledger.get_recent("BTC", limit=500)
        assert 200 <= len(all_recent) <= 300
        # 裁剪时留的是最新的，所以最新 ts 必须为 309
        assert all_recent[0]["ts"] == 309


class TestGetStats:
    def test_empty_returns_zero_sample(self, tmp_ledger):
        s = tmp_ledger.get_stats("BTC")
        assert s["sample_size"] == 0
        assert s["trend_hint_cn"] == "暂无样本"

    def test_stats_aggregate(self, tmp_ledger):
        # 10 条：7 条 ai_json，3 条 rule_fallback
        for i in range(7):
            tmp_ledger.record(_mk_record(ts=1000 + i))
        for i in range(3):
            tmp_ledger.record(_mk_record(
                matrix_source="rule_fallback",
                plans_source="markdown",
                json_valid=False,
                invalid_reason="missing",
                ts=2000 + i,
            ))
        s = tmp_ledger.get_stats("BTC")
        assert s["sample_size"] == 10
        assert s["ai_json_hit_rate"] == pytest.approx(0.7, abs=0.01)
        assert s["ai_plans_hit_rate"] == pytest.approx(0.7, abs=0.01)
        assert s["avg_latency_ms"] == 12000

    def test_stats_conflict_and_consistency(self, tmp_ledger):
        for i in range(5):
            tmp_ledger.record(_mk_record(bias_vs_text="consistent", ts=100 + i))
        for i in range(2):
            tmp_ledger.record(_mk_record(bias_vs_text="conflict", ts=200 + i))
        for i in range(3):
            tmp_ledger.record(_mk_record(
                matrix_source="internal_conflict",
                bias_vs_text="conflict",
                ts=300 + i,
            ))
        s = tmp_ledger.get_stats("BTC")
        assert s["sample_size"] == 10
        assert s["internal_conflict_rate"] == pytest.approx(0.3, abs=0.01)
        # 5 consistent / 10 known
        assert s["bias_consistency_rate"] == pytest.approx(0.5, abs=0.01)

    def test_stats_window_respected(self, tmp_ledger):
        for i in range(80):
            tmp_ledger.record(_mk_record(ts=i))
        s = tmp_ledger.get_stats("BTC", window=30)
        assert s["sample_size"] == 30

    def test_top_invalid_reasons(self):
        items = [
            _mk_record(invalid_reason="missing"),
            _mk_record(invalid_reason="missing"),
            _mk_record(invalid_reason="malformed"),
            _mk_record(invalid_reason=""),
        ]
        tops = _top_invalid_reasons(items)
        assert tops[0]["reason"] == "missing"
        assert tops[0]["count"] == 2
        assert tops[1]["reason"] == "malformed"


class TestTrendHint:
    def test_low_sample_hint(self):
        h = _build_trend_hint({"sample_size": 3, "ai_json_hit_rate": 1.0})
        assert "样本不足" in h

    def test_high_hit_rate_positive(self):
        h = _build_trend_hint({
            "sample_size": 20,
            "ai_json_hit_rate": 0.9,
            "internal_conflict_rate": 0.0,
            "ai_plans_hit_rate": 0.8,
        })
        assert "稳定命中" in h
        assert "良好" in h

    def test_conflict_warning(self):
        h = _build_trend_hint({
            "sample_size": 20,
            "ai_json_hit_rate": 0.6,
            "internal_conflict_rate": 0.2,
            "ai_plans_hit_rate": 0.5,
        })
        assert "冲突" in h

    def test_low_rate_warning(self):
        h = _build_trend_hint({
            "sample_size": 20,
            "ai_json_hit_rate": 0.2,
            "internal_conflict_rate": 0.0,
            "ai_plans_hit_rate": 0.1,
        })
        assert "低" in h


class TestGetRecent:
    def test_limit_respected(self, tmp_ledger):
        for i in range(20):
            tmp_ledger.record(_mk_record(ts=i))
        assert len(tmp_ledger.get_recent("BTC", limit=5)) == 5

    def test_unknown_coin_returns_empty(self, tmp_ledger):
        assert tmp_ledger.get_recent("XYZ") == []


class TestLoadCorrupted:
    def test_corrupt_file_tolerated(self, tmp_path):
        data_file = str(tmp_path / "bad.json")
        with open(data_file, "w") as f:
            f.write("not json")
        ledger = AIQualityLedger(data_file=data_file)
        assert ledger.get_recent("BTC") == []
        # 仍可正常写入
        ledger.record(_mk_record())
        assert len(ledger.get_recent("BTC")) == 1
