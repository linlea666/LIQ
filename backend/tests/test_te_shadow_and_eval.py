"""P0-A/B · TE Shadow Logger 与 Outcome Labeling 单元测试。

覆盖要点：
    1. Shadow Logger 的**去重策略**（state 不变 + 时间 < 1h 应跳过）
    2. Shadow Logger 的**heartbeat**（超 1h 即便不变也落盘）
    3. Shadow Logger 的**score drift**（composite 变化大即记录）
    4. 打标逻辑：healthy_continuation / momentum_fading / exhaustion_warn / structural_reversal
    5. regime_vetoed 信号应被 skip
    6. Markdown 日报能生成 + AI prompt 段可抽取
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

import pytest

# 调整 sys.path 以加载 backend 包
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from monitoring import te_shadow as shadow_mod
from monitoring import te_eval as eval_mod
from monitoring.te_eval import LabeledRecord, _label_one, evaluate_day


# ─────────────────────── 辅助 ───────────────────────

def _make_signal(
    state: str = "healthy_continuation",
    direction: str = "up",
    action: str = "hold",
    regime: str = "trend_up",
    regime_vetoed: bool = False,
    consensus: str = "strong_agree",
    composite_1h: float = 0.4,
    composite_4h: float = 0.5,
    composite_1d: float = 0.3,
) -> dict:
    """构造一份 model_dump 风格的 TrendExhaustionSignal 字典。"""

    def _tf(tf: str, comp: float) -> dict:
        return {
            "tf": tf,
            "direction": direction,
            "momentum_score": comp * 0.8,
            "participation_score": comp * 0.6,
            "exhaustion_score": -comp * 0.3,
            "composite_score": comp,
            "state": state,
            "state_age_min": 15,
            "confirmed_ticks": 2,
            "triggers": ["macd_2d_pos", "cvd_up"],
            "sub_scores": [
                {"key": "m1_macd_2d", "name": "MACD", "score": 0.5, "note": "柱体走强", "value": 0.1},
                {"key": "p1_cvd", "name": "CVD", "score": 0.3, "note": "CVD 同步", "value": None},
            ],
            "action_hint": action,
            "reason_cn": "动能健康",
        }

    return {
        "coin": "BTC",
        "ts": int(time.time()),
        "tf_1h": _tf("1h", composite_1h),
        "tf_4h": _tf("4h", composite_4h),
        "tf_1d": _tf("1d", composite_1d),
        "consensus_level": consensus,
        "overall_direction": direction,
        "overall_state": state,
        "overall_action": action,
        "overall_position_pct": 0.6,
        "overall_plain_cn": "还在涨，动能健康",
        "overall_tip_cn": "顺势持有",
        "overall_reason_cn": "4h/1d 共振",
        "regime": regime,
        "regime_vetoed": regime_vetoed,
        "data_quality": "ok",
        "missing_inputs": [],
    }


@pytest.fixture
def tmp_shadow_root(tmp_path, monkeypatch):
    """把 shadow_log_root 重定向到临时目录。"""
    monkeypatch.setattr(shadow_mod, "shadow_log_root", lambda: str(tmp_path))
    # 同时替换 te_eval 里的引用（它 from monitoring.te_shadow import shadow_log_root）
    monkeypatch.setattr(eval_mod, "shadow_log_root", lambda: str(tmp_path))
    # 重建单例，确保拿到新 root
    shadow_mod._singleton = None
    yield tmp_path
    shadow_mod._singleton = None


# ─────────────────────── Shadow Logger 测试 ───────────────────────

@pytest.mark.asyncio
async def test_shadow_writes_first_record(tmp_shadow_root):
    logger_instance = shadow_mod.get_te_shadow_logger()
    logger_instance.start()

    sig = _make_signal()
    logger_instance.record("BTC", sig, price=72000, atr=400)

    # 等 writer flush
    await asyncio.sleep(4.0)
    await logger_instance.stop()

    # 找生成的 jsonl
    jsonls = list(Path(tmp_shadow_root).rglob("BTC.jsonl"))
    assert jsonls, "应生成 BTC.jsonl"
    lines = jsonls[0].read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1, "首次应写入 1 条"
    rec = json.loads(lines[0])
    assert rec["coin"] == "BTC"
    assert rec["overall"]["state"] == "healthy_continuation"
    assert rec["reason"] == "state_change"
    assert rec["tf"]["4h"]["c"] == pytest.approx(0.5)


@pytest.mark.asyncio
async def test_shadow_dedupes_identical_within_hour(tmp_shadow_root):
    logger_instance = shadow_mod.get_te_shadow_logger()
    logger_instance.start()
    sig = _make_signal()
    logger_instance.record("BTC", sig, price=72000, atr=400)
    # 连续 5 次相同信号，都应被去重
    for _ in range(5):
        logger_instance.record("BTC", sig, price=72005, atr=400)
    await asyncio.sleep(3.5)
    await logger_instance.stop()

    jsonls = list(Path(tmp_shadow_root).rglob("BTC.jsonl"))
    lines = jsonls[0].read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1, f"相同信号应去重，实际 {len(lines)} 条"


@pytest.mark.asyncio
async def test_shadow_state_change_triggers_write(tmp_shadow_root):
    logger_instance = shadow_mod.get_te_shadow_logger()
    logger_instance.start()
    logger_instance.record("BTC", _make_signal(state="healthy_continuation"), price=72000, atr=400)
    logger_instance.record("BTC", _make_signal(state="exhaustion_warn", action="reduce"), price=72100, atr=400)
    await asyncio.sleep(3.5)
    await logger_instance.stop()

    jsonls = list(Path(tmp_shadow_root).rglob("BTC.jsonl"))
    lines = jsonls[0].read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[1])["overall"]["state"] == "exhaustion_warn"


@pytest.mark.asyncio
async def test_shadow_score_drift_triggers_write(tmp_shadow_root):
    logger_instance = shadow_mod.get_te_shadow_logger()
    logger_instance.start()
    # 第一条 composite=0.4，第二条 composite=0.8（drift > 阈值 0.25）
    logger_instance.record("BTC", _make_signal(composite_1h=0.4, composite_4h=0.4, composite_1d=0.4), 72000, 400)
    logger_instance.record("BTC", _make_signal(composite_1h=0.8, composite_4h=0.8, composite_1d=0.8), 72000, 400)
    await asyncio.sleep(3.5)
    await logger_instance.stop()

    jsonls = list(Path(tmp_shadow_root).rglob("BTC.jsonl"))
    lines = jsonls[0].read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[1])["reason"] == "score_drift"


# ─────────────────────── Labeling 测试 ───────────────────────

def _base_record(**kw) -> LabeledRecord:
    defaults = dict(
        ts=1700000000,
        coin="BTC",
        price=72000,
        atr=500,
        regime="trend_up",
        regime_vetoed=False,
        consensus="strong_agree",
        overall_state="healthy_continuation",
        overall_action="hold",
        overall_direction="up",
        position_pct=0.6,
        data_quality="ok",
        sub_triggers=["1h:macd_2d_pos"],
    )
    defaults.update(kw)
    return LabeledRecord(**defaults)


def test_label_healthy_continuation_correct():
    rec = _base_record(r_12h=0.5, r_24h=1.0)  # up + 同向 +0.5σ → 预期对
    _label_one(rec)
    assert rec.label == "correct"


def test_label_healthy_continuation_wrong():
    rec = _base_record(r_12h=-0.8, r_24h=-1.0)  # up 但反向跌 -0.8σ → 错
    _label_one(rec)
    assert rec.label == "wrong"


def test_label_healthy_continuation_neutral():
    rec = _base_record(r_12h=0.1, r_24h=0.2)
    _label_one(rec)
    assert rec.label == "neutral"


def test_label_momentum_fading_correct_on_stall():
    rec = _base_record(overall_state="momentum_fading", r_12h=0.05, r_24h=-0.2)
    _label_one(rec)
    assert rec.label == "correct"  # 动能确实没再放大


def test_label_momentum_fading_wrong_on_strong_continue():
    rec = _base_record(overall_state="momentum_fading", r_12h=1.5, r_24h=2.0)
    _label_one(rec)
    assert rec.label == "wrong"  # 动能没减反而更强 → 判错


def test_label_exhaustion_warn_correct_on_reverse():
    rec = _base_record(overall_state="exhaustion_warn", r_12h=-0.4, r_24h=-0.6)
    _label_one(rec)
    assert rec.label == "correct"


def test_label_structural_reversal_wrong_on_continuation():
    rec = _base_record(overall_state="structural_reversal", r_12h=1.0, r_24h=1.2)
    _label_one(rec)
    assert rec.label == "wrong"


def test_label_regime_vetoed_always_skip():
    rec = _base_record(regime_vetoed=True, r_12h=0.5, r_24h=1.0)
    _label_one(rec)
    assert rec.label == "skip"
    assert "regime_vetoed" in rec.skip_reason


def test_label_pending_when_no_future_price():
    rec = _base_record(r_12h=None, r_24h=None)
    _label_one(rec)
    assert rec.label == "pending"


def test_label_down_direction_correct():
    rec = _base_record(overall_direction="down", r_12h=-1.0, r_24h=-1.2)  # down + 同向下跌 → 对
    _label_one(rec)
    assert rec.label == "correct"


# ─────────────────────── 端到端：shadow → eval → report ───────────────────────

@pytest.mark.asyncio
async def test_end_to_end_eval_pipeline(tmp_shadow_root, monkeypatch):
    """真实写若干 shadow 记录，跑 evaluate_day，检查 report 可生成。"""
    # report 根也重定向
    report_dir = tmp_shadow_root.parent / "te_eval"
    monkeypatch.setattr(eval_mod, "report_root", lambda: str(report_dir))

    from datetime import datetime, timedelta
    from monitoring.te_shadow import _BJ_TZ

    # 构造"昨天"作为 target_date
    target_dt = datetime.now(_BJ_TZ) - timedelta(days=1)
    target_slug = target_dt.strftime("%Y-%m-%d")

    # 手动写 2 条 shadow + 1 条 24h 后的价格记录
    day_dir = Path(tmp_shadow_root) / target_slug
    day_dir.mkdir(parents=True, exist_ok=True)
    next_dir = Path(tmp_shadow_root) / (target_dt + timedelta(days=1)).strftime("%Y-%m-%d")
    next_dir.mkdir(parents=True, exist_ok=True)

    base_ts = int(target_dt.replace(hour=10, minute=0, second=0, microsecond=0).timestamp())

    def _record(ts: int, price: float, state: str, dirn: str = "up") -> dict:
        return {
            "ts": ts,
            "coin": "BTC",
            "price": price,
            "atr": 500.0,
            "regime": "trend_up",
            "regime_vetoed": False,
            "consensus_level": "strong_agree",
            "data_quality": "ok",
            "missing_inputs": [],
            "overall": {
                "state": state,
                "action": "hold",
                "direction": dirn,
                "position_pct": 0.6,
                "plain_cn": "",
                "tip_cn": "",
                "reason_cn": "",
            },
            "tf": {
                "1h": {
                    "state": state, "direction": dirn,
                    "m": 0.4, "p": 0.3, "e": -0.1, "c": 0.3,
                    "age_min": 15, "confirmed": 2,
                    "triggers": ["macd_2d_pos"], "sub": [],
                },
                "4h": None, "1d": None,
            },
            "reason": "state_change",
        }

    # 当天 10:00 一条 healthy_continuation@72000
    # 当天 11:00 一条 exhaustion_warn@73000
    # 次日 10:00（24h 后）价格 75000（说明 healthy 对、exhaustion 错）
    with (day_dir / "BTC.jsonl").open("w", encoding="utf-8") as f:
        f.write(json.dumps(_record(base_ts, 72000, "healthy_continuation")) + "\n")
        f.write(json.dumps(_record(base_ts + 3600, 73000, "exhaustion_warn")) + "\n")
    with (next_dir / "BTC.jsonl").open("w", encoding="utf-8") as f:
        f.write(json.dumps(_record(base_ts + 24 * 3600, 75000, "healthy_continuation")) + "\n")
        f.write(json.dumps(_record(base_ts + 25 * 3600, 75500, "healthy_continuation")) + "\n")

    stats, path = evaluate_day(target_slug)
    assert stats.total_records == 2
    # healthy_continuation@72000 → 24h 后 75000，up +6σ → correct
    # exhaustion_warn@73000 → 24h 后 75500，up +5σ（没反向，反向走强）→ wrong
    assert stats.overall.correct >= 1
    assert stats.overall.wrong >= 1
    assert path is not None
    md = Path(path).read_text(encoding="utf-8")
    assert "准确率日报" in md
    assert "发给 AI 复核用 Prompt" in md
    assert "```" in md
