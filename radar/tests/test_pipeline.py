"""评估后置流水线测试。

这一层本身逻辑很薄，但它是"采集 → 判断 → 产出 → 追踪"之间唯一的接缝。
接缝出错的表现是：警报正常发出、Outcome 表却是空的，
或者追踪起点比信号时间晚了半个周期——都不报错，只是数据悄悄失真。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from radar.alerts import AlertManager  # noqa: E402
from radar.domain.models import TokenState  # noqa: E402
from radar.notify import EmailRenderer  # noqa: E402
from radar.obs.events import EventBus, bus  # noqa: E402
from radar.pipeline import EvaluationPipeline  # noqa: E402
from radar.storage import repo  # noqa: E402
from radar.storage.db import Database  # noqa: E402
from radar.tracker import OutcomeTracker  # noqa: E402

from test_alerts import ALERT_CONFIG, FINGERPRINT, NOW, make_evaluation, make_view  # noqa: E402
from test_tracker import TRACKER_CONFIG  # noqa: E402

PIPELINE_CONFIG = {**ALERT_CONFIG, **TRACKER_CONFIG}


@pytest.fixture
async def env(tmp_path):
    db = Database(tmp_path / "radar.db")
    await db.start()
    events = EventBus()
    events.configure_fingerprint(FINGERPRINT)
    events.set_sink(repo.make_event_sink(db))
    bus.set_sink(repo.make_event_sink(db))

    manager = AlertManager(db=db, config=PIPELINE_CONFIG, fingerprint=FINGERPRINT,
                           renderer=EmailRenderer(fingerprint=FINGERPRINT))
    tracker = OutcomeTracker(db=db, config=PIPELINE_CONFIG, fingerprint=FINGERPRINT)
    pipeline = EvaluationPipeline(alerts=manager, tracker=tracker)
    try:
        yield pipeline, tracker, db
    finally:
        await db.stop()


@pytest.mark.asyncio
async def test_alert_starts_outcome_tracking(env):
    pipeline, tracker, db = env
    view = make_view()
    await repo.upsert_token(db, view, source="test")

    await pipeline.process(make_evaluation(view=view))
    await db.drain()

    alerts = await db.fetch_all("SELECT alert_id FROM alerts")
    outcomes = await db.fetch_all("SELECT alert_id, signal_at FROM outcomes")
    assert len(alerts) == 1 and len(outcomes) == 1
    assert outcomes[0]["alert_id"] == alerts[0]["alert_id"]
    assert tracker.snapshot()["active"] == 1


@pytest.mark.asyncio
async def test_tracking_starts_at_signal_time_not_wall_clock(env):
    """起点差半个周期，time_to_2x 这类指标就会带上系统性偏移。"""
    pipeline, _, db = env
    view = make_view()
    await repo.upsert_token(db, view, source="test")
    signal_at = NOW - 500_000

    await pipeline.process(make_evaluation(view=view, at=signal_at))
    await db.drain()

    row = (await db.fetch_all("SELECT signal_at FROM outcomes"))[0]
    assert row["signal_at"] == signal_at


@pytest.mark.asyncio
async def test_observation_updates_tracking_without_new_alert(env):
    """不产生警报的评估同样要推进已有 Outcome，否则会漏掉整个周期的价格。"""
    pipeline, _, db = env
    view = make_view()
    await repo.upsert_token(db, view, source="test")
    await pipeline.process(make_evaluation(view=view, at=NOW))

    view.values["price"] = 0.004
    await pipeline.process(make_evaluation(
        view=view, old=TokenState.S1, new=TokenState.S1,
        changed=False, at=NOW + 60_000,
    ))
    await db.drain()

    row = (await db.fetch_all("SELECT * FROM outcomes"))[0]
    assert row["raw_ath_price"] == 0.004
    assert row["peak_multiple"] == pytest.approx(4.0)
    assert len(await db.fetch_all("SELECT * FROM alerts")) == 1


@pytest.mark.asyncio
async def test_tracker_failure_does_not_block_alerting(env, monkeypatch):
    """追踪出问题时警报仍必须发出：错过信号比丢一条统计严重得多。"""
    pipeline, tracker, db = env
    view = make_view()
    await repo.upsert_token(db, view, source="test")

    def boom(*args, **kwargs):
        raise RuntimeError("追踪器炸了")

    monkeypatch.setattr(tracker, "on_observation", boom)
    await pipeline.process(make_evaluation(view=view))
    await db.drain()

    assert len(await db.fetch_all("SELECT * FROM alerts")) == 1
    assert pipeline.stats["errors"] == 1


@pytest.mark.asyncio
async def test_suppressed_alert_does_not_create_outcome(env):
    """被冷却压下的警报没有信号现场，不应产生 Outcome 记录。"""
    pipeline, _, db = env
    view = make_view()
    await repo.upsert_token(db, view, source="test")

    await pipeline.process(make_evaluation(view=view, at=NOW))
    await pipeline.process(make_evaluation(view=view, at=NOW + 60_000))
    await db.drain()

    assert len(await db.fetch_all("SELECT * FROM outcomes")) == 1
    assert pipeline.stats["alerts"] == 1
