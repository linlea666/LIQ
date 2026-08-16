"""警报与邮件测试。

这一层的失效方式很特别：技术上完全正常，但每小时发四十封邮件，
于是用户在第三天把它拉进垃圾箱，从此错过真正重要的那一封。
因此测试重点全部放在**抑制机制**和**投递可靠性**上。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from radar.alerts import (  # noqa: E402
    KIND_DISTRIBUTION,
    KIND_S1,
    AlertManager,
    AnomalyDetector,
    EmailOutboxWorker,
    RateLimiter,
)
from radar.domain.models import (  # noqa: E402
    FactorScore,
    QualityReport,
    ScoreResult,
    TokenState,
    TokenView,
)
from radar.domain.risk_gate import GATE_RESEARCH, RiskDecision, Violation  # noqa: E402
from radar.domain.states import Requirement, StateDecision  # noqa: E402
from radar.notify import EmailRenderer  # noqa: E402
from radar.obs.events import EventBus, bus  # noqa: E402
from radar.registry import Evaluation  # noqa: E402
from radar.storage import repo  # noqa: E402
from radar.storage.db import Database  # noqa: E402

from radar.obs.logging_setup import now_ms  # noqa: E402

# 邮件 worker 按墙上时钟判断"哪些邮件到期该发"——它是 I/O 环节，
# 不属于需要确定性重放的决策路径。因此测试的时间基准也必须是真实时间，
# 否则测的是"未来才到期的邮件不发"，而不是投递逻辑本身。
NOW = now_ms()

FINGERPRINT = {
    "strategy_version": "test-1.0.0", "feature_version": "f1.0.0",
    "parser_version": "p1.0.0", "config_hash": "deadbeef", "code_commit": "testsha",
}

ALERT_CONFIG: dict[str, Any] = {
    "alerts": {
        "near_miss_margin": 5.0,
        "near_miss_cooldown_sec": 600,
        "cooldown_sec": {"s1": 3600, "s2": 1800, "distribution": 3600},
        "anomaly": {"warmup_hours": 48, "baseline_window_hours": 168,
                    "deviation_multiple": 8.0},
    },
    "email": {
        "enabled": True, "max_per_hour": 3, "digest_on_overflow": True,
        "send_s1": True, "send_s2": True, "send_distribution": True,
        "outbox_max_retries": 3, "outbox_retry_backoff_sec": 60,
    },
}


def make_view(contract: str = "0xaaa", token_id: int = 1) -> TokenView:
    view = TokenView(chain_id="56", contract_address=contract, token_id=token_id,
                     symbol="PEPE")
    view.values.update({"price": 0.001, "market_cap": 250_000.0,
                        "liquidity": 40_000.0, "holders": 1200})
    view.launch_time_ms = NOW - 3_600_000
    view.last_observed_ms = NOW
    return view


def make_evaluation(
    *,
    view: TokenView | None = None,
    old: TokenState = TokenState.S0,
    new: TokenState = TokenState.S1,
    changed: bool = True,
    near_miss: bool = False,
    blocked_state: TokenState | None = None,
    at: int = NOW,
    opportunity: float = 78.0,
) -> Evaluation:
    view = view or make_view()
    state = StateDecision(old_state=old, new_state=new, changed=changed,
                          reason=f"晋升 {new.value}")
    state.requirements[new.value] = [
        Requirement(name="opportunity", label="机会分", passed=True,
                    actual=opportunity, threshold=72.0),
        Requirement(name="liquidity", label="流动性", passed=True,
                    actual=40_000.0, threshold=12_000.0, is_score=False),
    ]
    if near_miss:
        state.near_miss = True
        state.blocked_state = blocked_state or TokenState.S1
        state.blocked_by = [
            Requirement(name="opportunity", label="机会分", passed=False,
                        actual=69.0, threshold=72.0, gap=3.0)
        ]

    scores = ScoreResult(
        opportunity=opportunity, confidence=74.0, data_quality=82.0,
        rug_risk=18.0, distribution=12.0,
        factors=[
            FactorScore(name="holder_momentum", label="持有人动量",
                        score=15.0, max_score=20.0, detail="持有人 1200"),
            FactorScore(name="capital_flow", label="资金流入",
                        score=12.0, max_score=20.0, detail="净流入 $30,000"),
        ],
    )
    risk = RiskDecision(audit_unknown=False)
    quality = QualityReport(score=82.0)
    return Evaluation(
        view=view, quality=quality, scores=scores, risk=risk, state=state,
        features_json="{}", evaluated_at=at, snapshot_id=1,
        market_cap=250_000.0, mc_source="reported",
    )


class RecordingTransport:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.fail_times = 0

    async def send(self, *, subject: str, html: str) -> None:
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("SMTP 连接超时")
        self.sent.append((subject, html))


@pytest.fixture
async def env(tmp_path):
    db = Database(tmp_path / "radar.db")
    await db.start()
    events = EventBus()
    events.configure_fingerprint(FINGERPRINT)
    events.set_sink(repo.make_event_sink(db))
    bus.set_sink(repo.make_event_sink(db))

    renderer = EmailRenderer(tz_offset_hours=8, fingerprint=FINGERPRINT)
    manager = AlertManager(db=db, config=ALERT_CONFIG,
                           fingerprint=FINGERPRINT, renderer=renderer)
    try:
        yield manager, db, renderer
    finally:
        await db.stop()


# ─────────────────────────────────────────────────────────────────────────
# 限速器与异常检测（纯逻辑）
# ─────────────────────────────────────────────────────────────────────────

def test_rate_limiter_caps_per_hour():
    limiter = RateLimiter(max_per_hour=3)
    assert [limiter.allow() for _ in range(5)] == [True, True, True, False, False]
    assert limiter.remaining() == 0


def test_anomaly_detector_silent_during_warmup():
    """冷启动期做倍数比较必然误报，而误报会让人很快学会忽略这类告警。"""
    detector = AnomalyDetector({"warmup_hours": 48, "baseline_window_hours": 168,
                                "deviation_multiple": 8.0})
    for i in range(200):
        anomalous, _, _ = detector.record(KIND_S1, NOW + i * 1000)
        assert not anomalous


def test_anomaly_detector_flags_rate_explosion():
    detector = AnomalyDetector({"warmup_hours": 0, "baseline_window_hours": 168,
                                "deviation_multiple": 8.0})
    # 先铺一周的低速基线：每小时 1 个
    base = NOW - 168 * 3_600_000
    for hour in range(168):
        detector.record(KIND_S1, base + hour * 3_600_000)

    # 再在最后一小时内塞进 80 个
    flagged = False
    for i in range(80):
        anomalous, _, _ = detector.record(KIND_S1, NOW + i * 1000)
        flagged = flagged or anomalous
    assert flagged, "产出速率暴涨必须被发现"


def test_anomaly_alarm_is_itself_rate_limited():
    detector = AnomalyDetector({"warmup_hours": 0, "baseline_window_hours": 168,
                                "deviation_multiple": 2.0})
    base = NOW - 168 * 3_600_000
    for hour in range(168):
        detector.record(KIND_S1, base + hour * 3_600_000)

    alarms = sum(1 for i in range(200) if detector.record(KIND_S1, NOW + i * 1000)[0])
    assert alarms <= 1, "异常告警本身刷屏会淹没它要提示的问题"


# ─────────────────────────────────────────────────────────────────────────
# 警报生成
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_promotion_creates_alert_and_queues_email(env):
    manager, db, _ = env
    record = await manager.handle(make_evaluation())

    assert record is not None
    assert record.kind == KIND_S1
    await db.drain()

    alerts = await db.fetch_all("SELECT * FROM alerts")
    assert len(alerts) == 1
    assert alerts[0]["alert_kind"] == KIND_S1
    assert alerts[0]["is_near_miss"] == 0
    # 触发依据必须落库：事后要回答"当时为什么报警、依据哪几个数"
    assert alerts[0]["trigger_json"] and alerts[0]["factors_json"]
    assert alerts[0]["strategy_version"] == "test-1.0.0"

    outbox = await db.fetch_all("SELECT * FROM email_outbox")
    assert len(outbox) == 1
    assert outbox[0]["status"] == "pending"


@pytest.mark.asyncio
async def test_no_alert_when_state_unchanged(env):
    """维持在 S1 不应反复报警，只有"进入"才报。"""
    manager, db, _ = env
    assert await manager.handle(
        make_evaluation(old=TokenState.S1, new=TokenState.S1, changed=False)
    ) is None
    await db.drain()
    assert await db.fetch_all("SELECT * FROM alerts") == []


@pytest.mark.asyncio
async def test_no_alert_on_downgrade(env):
    """降级不发信：既不可操作，数量也远多于晋升。"""
    manager, db, _ = env
    assert await manager.handle(
        make_evaluation(old=TokenState.S2, new=TokenState.S1)
    ) is None
    await db.drain()
    assert await db.fetch_all("SELECT * FROM alerts") == []


@pytest.mark.asyncio
async def test_cooldown_suppresses_repeat_alert(env):
    manager, db, _ = env
    assert await manager.handle(make_evaluation(at=NOW)) is not None
    # 冷却期内的第二次晋升（降级后又升回来）不应再通知
    assert await manager.handle(make_evaluation(at=NOW + 600_000)) is None
    assert manager.stats.suppressed_cooldown == 1

    # 超过冷却窗口后恢复通知
    assert await manager.handle(make_evaluation(at=NOW + 3_700_000)) is not None


@pytest.mark.asyncio
async def test_failed_alert_write_does_not_start_cooldown(env, monkeypatch):
    """写库失败等于这次判断从未发生过，不能顺带把这枚币静默一小时。

    冷却若在落库之前起算，那一小时里既没有警报、没有邮件、
    也没有 Outcome 追踪，而且没有任何重试机会。
    """
    manager, db, _ = env

    async def boom(*args, **kwargs):
        raise RuntimeError("磁盘满了")

    monkeypatch.setattr(repo, "insert_alert", boom)
    assert await manager.handle(make_evaluation(at=NOW)) is None

    # 恢复后同一枚币必须还能报出来
    monkeypatch.undo()
    record = await manager.handle(make_evaluation(at=NOW + 60_000))
    assert record is not None, "写库失败后冷却被误起算，该币被静默"
    assert manager.stats.suppressed_cooldown == 0


@pytest.mark.asyncio
async def test_cooldown_is_per_kind_and_per_token(env):
    manager, _, _ = env
    await manager.handle(make_evaluation(at=NOW))
    # 同一枚币的不同警报类型互不影响
    assert await manager.handle(
        make_evaluation(old=TokenState.S1, new=TokenState.S2, at=NOW + 1000)
    ) is not None
    # 另一枚币也不受影响
    other = make_view(contract="0xbbb", token_id=2)
    assert await manager.handle(make_evaluation(view=other, at=NOW + 2000)) is not None


@pytest.mark.asyncio
async def test_distribution_alert_kind(env):
    manager, _, _ = env
    record = await manager.handle(
        make_evaluation(old=TokenState.S0, new=TokenState.DISTRIBUTION)
    )
    assert record is not None and record.kind == KIND_DISTRIBUTION


# ─────────────────────────────────────────────────────────────────────────
# DISTRIBUTION 发信门槛
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_distribution_mail_requires_prior_s1(env):
    """从未发过 S1/S2 的币谈不上"你可能持有"，派发邮件没有可操作价值。

    警报本身必须照常落库——研究价值保留，只抑制邮件。
    """
    manager, db, _ = env
    record = await manager.handle(
        make_evaluation(old=TokenState.S0, new=TokenState.DISTRIBUTION)
    )
    assert record is not None
    await db.drain()
    assert len(await db.fetch_all("SELECT * FROM alerts")) == 1
    assert await db.fetch_all("SELECT * FROM email_outbox") == []


@pytest.mark.asyncio
async def test_distribution_mail_sent_when_prior_s1_and_healthy(env):
    manager, db, _ = env
    view = make_view()
    await manager.handle(make_evaluation(view=view))     # 先有一条真实 S1
    record = await manager.handle(make_evaluation(
        view=view, old=TokenState.S0, new=TokenState.DISTRIBUTION,
        at=NOW + 60_000,
    ))
    assert record is not None
    await db.drain()
    outbox = await db.fetch_all("SELECT kind FROM email_outbox ORDER BY id")
    assert [r["kind"] for r in outbox] == ["alert_s1", "alert_distribution"]


@pytest.mark.asyncio
async def test_distribution_mail_blocked_for_dust_token(env):
    """实盘事故：NTDA 拔池 4 分钟后才发派发邮件，市值 $39M→$1,382。"""
    manager, db, _ = env
    view = make_view()
    await manager.handle(make_evaluation(view=view))     # 先有一条真实 S1

    ev = make_evaluation(view=view, old=TokenState.S0,
                         new=TokenState.DISTRIBUTION, at=NOW + 60_000)
    ev.market_cap = 1_382.0                              # 已崩盘成尘埃
    record = await manager.handle(ev)
    assert record is not None, "警报照常落库"
    await db.drain()
    outbox = await db.fetch_all("SELECT kind FROM email_outbox")
    assert [r["kind"] for r in outbox] == ["alert_s1"], "尘埃币不应发派发邮件"


# ─────────────────────────────────────────────────────────────────────────
# Near-Miss
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_near_miss_is_recorded_but_not_mailed(env):
    """Near-Miss 是反事实研究的数据，不是给人看的通知。"""
    manager, db, _ = env
    result = await manager.handle(
        make_evaluation(changed=False, near_miss=True, opportunity=69.0)
    )
    assert result is None
    await db.drain()

    alerts = await db.fetch_all("SELECT * FROM alerts WHERE is_near_miss=1")
    assert len(alerts) == 1
    assert alerts[0]["alert_kind"] == "S1"
    assert await db.fetch_all("SELECT * FROM email_outbox") == []


@pytest.mark.asyncio
async def test_near_miss_has_cooldown(env):
    """分数在阈值附近震荡的币每个周期都会命中，不加限制会淹没这张表。"""
    manager, db, _ = env
    for i in range(6):
        await manager.handle(
            make_evaluation(changed=False, near_miss=True, at=NOW + i * 60_000)
        )
    await db.drain()
    rows = await db.fetch_all("SELECT * FROM alerts WHERE is_near_miss=1")
    assert len(rows) == 1, f"Near-Miss 被重复记录 {len(rows)} 次"


# ─────────────────────────────────────────────────────────────────────────
# 全局限速与摘要
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_rate_limit_diverts_overflow_to_digest(env):
    """行情狂热时几十枚币同时达标是常态，逐封发等于自毁通知渠道。"""
    manager, db, _ = env
    for i in range(6):
        view = make_view(contract=f"0x{i:03d}", token_id=i + 1)
        await manager.handle(make_evaluation(view=view, at=NOW + i * 1000))

    await db.drain()
    outbox = await db.fetch_all("SELECT * FROM email_outbox")
    assert len(outbox) == 3, "超出每小时上限的邮件不应逐封发出"
    assert manager.stats.suppressed_rate_limit == 3

    # 但警报本身全部落库，一条都不能少
    alerts = await db.fetch_all("SELECT * FROM alerts")
    assert len(alerts) == 6

    assert await manager.flush_digest() is True
    await db.drain()
    digest = await db.fetch_all("SELECT * FROM email_outbox WHERE kind='alert_digest'")
    assert len(digest) == 1
    assert "3 条警报" in digest[0]["subject"]


@pytest.mark.asyncio
async def test_flush_digest_is_noop_when_empty(env):
    manager, _, _ = env
    assert await manager.flush_digest() is False


@pytest.mark.asyncio
async def test_digest_throttles_itself(env):
    """摘要不走 max_per_hour，因此必须自己限速。

    维护循环每 30 秒调一次 flush_digest。不加间隔的话，行情狂热时
    一小时能发 120 封摘要——恰好在每小时上限最该起作用的时刻把它架空。
    """
    manager, db, _ = env

    async def overflow(base: int, start: int) -> None:
        for i in range(6):
            view = make_view(contract=f"0x{base + i:03d}", token_id=base + i + 1)
            await manager.handle(make_evaluation(view=view, at=start + i * 1000))

    await overflow(0, NOW)
    assert await manager.flush_digest() is True, "第一封应立即发出"

    # 30 秒后又攒了一批：真实的维护循环就是这个节奏
    await overflow(100, NOW + 30_000)
    assert await manager.flush_digest() is False, "间隔内不应再发"
    # 队列必须保留，攒到下次一起发，而不是被丢掉
    assert manager.snapshot()["digest_pending"] > 0

    await db.drain()
    digests = await db.fetch_all(
        "SELECT * FROM email_outbox WHERE kind='alert_digest'"
    )
    assert len(digests) == 1


@pytest.mark.asyncio
async def test_email_idempotency_key_prevents_duplicates(env):
    """崩溃重启后重放不能产生第二封相同邮件。"""
    manager, db, renderer = env
    ev = make_evaluation()
    record = await manager.handle(ev)
    assert record is not None

    subject, html = renderer.render_alert(record, ev)
    await repo.enqueue_email(
        db, idempotency_key=f"alert:{record.alert_id}", kind="alert_s1",
        subject=subject, html=html, token_id=1, alert_id=record.alert_id,
        created_at=NOW,
    )
    await db.drain()
    rows = await db.fetch_all("SELECT * FROM email_outbox")
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_disabled_kind_is_not_mailed(env, tmp_path):
    db = Database(tmp_path / "off.db")
    await db.start()
    try:
        config = {**ALERT_CONFIG,
                  "email": {**ALERT_CONFIG["email"], "send_s1": False}}
        manager = AlertManager(db=db, config=config, fingerprint=FINGERPRINT,
                               renderer=EmailRenderer())
        record = await manager.handle(make_evaluation())
        assert record is not None, "关闭邮件不影响警报落库"
        await db.drain()
        assert await db.fetch_all("SELECT * FROM email_outbox") == []
    finally:
        await db.stop()


# ─────────────────────────────────────────────────────────────────────────
# 邮件投递
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_outbox_worker_sends_and_marks(env):
    manager, db, _ = env
    await manager.handle(make_evaluation())
    await db.drain()

    transport = RecordingTransport()
    worker = EmailOutboxWorker(db=db, transport=transport, config=ALERT_CONFIG)
    assert await worker.process_once() == 1
    await db.drain()

    assert len(transport.sent) == 1
    subject, html = transport.sent[0]
    assert "PEPE" in subject
    assert "机会分" in html
    rows = await db.fetch_all("SELECT status, sent_at FROM email_outbox")
    assert rows[0]["status"] == "sent"
    assert rows[0]["sent_at"] is not None


@pytest.mark.asyncio
async def test_failed_delivery_is_retried_with_backoff(env):
    """SMTP 抖动时直接发信会永久丢失警报，outbox 的全部意义就在这里。"""
    manager, db, _ = env
    await manager.handle(make_evaluation())
    await db.drain()

    transport = RecordingTransport()
    transport.fail_times = 1
    worker = EmailOutboxWorker(db=db, transport=transport, config=ALERT_CONFIG)

    assert await worker.process_once() == 0
    await db.drain()
    rows = await db.fetch_all(
        "SELECT status, retry_count, next_retry_at, last_error FROM email_outbox"
    )
    assert rows[0]["status"] == "pending", "可重试的失败不能标记为最终失败"
    assert rows[0]["retry_count"] == 1
    assert rows[0]["next_retry_at"] > NOW
    assert "SMTP" in rows[0]["last_error"]


@pytest.mark.asyncio
async def test_delivery_gives_up_after_max_retries(env):
    manager, db, _ = env
    await manager.handle(make_evaluation())
    await db.drain()

    transport = RecordingTransport()
    transport.fail_times = 99
    worker = EmailOutboxWorker(db=db, transport=transport, config=ALERT_CONFIG)

    for _ in range(ALERT_CONFIG["email"]["outbox_max_retries"]):
        # 每轮把 next_retry_at 拨回过去，避免在测试里真的等退避时间
        await db.submit_returning(
            "UPDATE email_outbox SET next_retry_at=0 WHERE status='pending'"
        )
        await worker.process_once()
    await db.drain()

    rows = await db.fetch_all("SELECT status, retry_count FROM email_outbox")
    assert rows[0]["status"] == "failed"
    assert worker.stats.gave_up == 1


@pytest.mark.asyncio
async def test_worker_skips_entries_not_yet_due(env):
    manager, db, _ = env
    await manager.handle(make_evaluation())
    await db.drain()
    await db.submit_returning(
        "UPDATE email_outbox SET next_retry_at=? WHERE status='pending'",
        (NOW + 10 * 86_400_000,),
    )
    await db.drain()

    worker = EmailOutboxWorker(db=db, transport=RecordingTransport(),
                               config=ALERT_CONFIG)
    assert await worker.process_once() == 0


# ─────────────────────────────────────────────────────────────────────────
# 邮件渲染
# ─────────────────────────────────────────────────────────────────────────

def test_renderer_escapes_token_names():
    """代币名称直接来自链上，注入 HTML 是常见的污染手段。"""
    renderer = EmailRenderer(fingerprint=FINGERPRINT)
    view = make_view()
    view.symbol = "<script>alert(1)</script>"
    ev = make_evaluation(view=view)
    from radar.alerts import AlertRecord

    record = AlertRecord(alert_id=1, kind=KIND_S1, token_key=view.key,
                         symbol=view.symbol, created_at=NOW,
                         scores=ev.scores.as_scores_dict())
    _, html = renderer.render_alert(record, ev)
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_renderer_marks_unknown_audit_explicitly():
    """人天然会把"没提示风险"读成"没有风险"，必须显式写出未知。"""
    renderer = EmailRenderer(fingerprint=FINGERPRINT)
    ev = make_evaluation()
    ev.risk = RiskDecision(audit_unknown=True)
    from radar.alerts import AlertRecord

    record = AlertRecord(alert_id=1, kind=KIND_S1, token_key=ev.view.key,
                         symbol="PEPE", created_at=NOW,
                         scores=ev.scores.as_scores_dict())
    _, html = renderer.render_alert(record, ev)
    assert "审计结果未知" in html
    assert "不等于安全" in html


def test_renderer_marks_computed_market_cap():
    """不同口径的市值放在一起比较毫无意义，必须标注来源。"""
    renderer = EmailRenderer(fingerprint=FINGERPRINT)
    ev = make_evaluation()
    ev.mc_source = "computed"
    from radar.alerts import AlertRecord

    record = AlertRecord(alert_id=1, kind=KIND_S1, token_key=ev.view.key,
                         symbol="PEPE", created_at=NOW,
                         scores=ev.scores.as_scores_dict())
    _, html = renderer.render_alert(record, ev)
    assert "反算" in html


def test_renderer_shows_research_gate_violations():
    renderer = EmailRenderer(fingerprint=FINGERPRINT)
    ev = make_evaluation()
    ev.risk = RiskDecision(gate_blocked=True, audit_unknown=False, violations=[
        Violation(gate=GATE_RESEARCH, rule="top10_max", actual_value=68.0,
                  threshold_value=55.0, detail="Top10 68.0%"),
    ])
    from radar.alerts import AlertRecord

    record = AlertRecord(alert_id=1, kind=KIND_S1, token_key=ev.view.key,
                         symbol="PEPE", created_at=NOW,
                         scores=ev.scores.as_scores_dict())
    _, html = renderer.render_alert(record, ev)
    assert "Top10 68.0%" in html
