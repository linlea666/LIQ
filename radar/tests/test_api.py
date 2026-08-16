"""API 测试。

重点不在"路由能不能返回 200"，而在两件容易做错且后果严重的事：
  1. 评分类响应必须同时带上可信度与依据——只给分数会诱导前端
     做出一个漂亮但危险的界面。
  2. 降级时 /ready 必须返回 503——报告健康的坏服务永远不会被重启。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from radar import api  # noqa: E402
from radar.domain.models import ScoreResult, TokenState, TokenView  # noqa: E402
from radar.obs.events import EventBus, bus  # noqa: E402
from radar.obs.logging_setup import now_ms  # noqa: E402
from radar.storage import repo  # noqa: E402
from radar.storage.db import Database  # noqa: E402

from test_alerts import FINGERPRINT  # noqa: E402

NOW = now_ms()


class FakeSettings:
    """只提供 API 层真正读到的配置。"""

    def __init__(self) -> None:
        self.service = {"host": "0.0.0.0", "port": 8000, "cors_origins": ["*"],
                        "tz_offset_hours": 8}
        self.state_machine = {"transitions": {"s1": {"enter_opportunity": 72}}}
        self.risk = {"execution_blocker": {"honeypot_blocks": True}}
        self.alerts = {"near_miss_margin": 5.0}
        self.raw = {"quality": {"min_for_s1": 55}}
        self.tiers: dict[str, Any] = {}
        self.chains = [_Chain("56", "BSC"), _Chain("CT_501", "Solana")]
        self.strategy_version = "test-1.0.0"

    def fingerprint(self) -> dict[str, str]:
        return dict(FINGERPRINT)


class _Chain:
    def __init__(self, cid: str, name: str) -> None:
        self.id = cid
        self.name = name
        self.enabled = True


class FakeRegistry:
    def __init__(self) -> None:
        self.views: dict[tuple[str, str], TokenView] = {}

    def all_views(self):
        return list(self.views.values())

    def get(self, chain_id: str, contract: str):
        return self.views.get((chain_id, contract))

    def state_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for view in self.views.values():
            counts[view.state.value] = counts.get(view.state.value, 0) + 1
        return counts

    def __len__(self) -> int:
        return len(self.views)


class FakeService:
    """替身服务：只实现 API 需要的表面，避免测试里真的去打币安接口。"""

    def __init__(self, db: Database) -> None:
        self.db = db
        self.settings = FakeSettings()
        self.registry = FakeRegistry()
        self.collector_ok = True
        self.kpi = None

    def health(self) -> dict[str, Any]:
        return {
            "status": "ok" if self.collector_ok else "degraded",
            "running": True,
            "uptime_sec": 100,
            "tokens_in_memory": len(self.registry),
            "rss_mb": 120.0,
            "last_cycle_at": NOW,
            "collector_ok": self.collector_ok,
            "email_usable": True,
            "version": FINGERPRINT,
        }

    def diagnostics(self) -> dict[str, Any]:
        return {"health": self.health(), "scheduler": {}, "collectors": {}}


def make_view(contract: str = "0xaaa", *, state: TokenState = TokenState.S1,
              token_id: int = 1) -> TokenView:
    view = TokenView(chain_id="56", contract_address=contract, token_id=token_id,
                     symbol="PEPE", name="Pepe")
    view.values.update({"price": 0.001, "market_cap": 400_000.0,
                        "liquidity": 60_000.0, "holders": 2000,
                        "top10_percent": 22.0, "smart_money_count": 6})
    view.field_source["market_cap"] = "reported"
    view.launch_time_ms = NOW - 7_200_000
    view.first_seen_ms = NOW - 3_600_000
    view.last_observed_ms = NOW
    view.state = state
    view.state_since_ms = NOW - 600_000
    view.last_scores = {"opportunity": 78.0, "confidence": 71.0,
                        "data_quality": 83.0, "rug_risk": 18.0,
                        "distribution": 12.0}
    return view


@pytest.fixture
async def client(tmp_path):
    """异步客户端 + 单事件循环。

    刻意不用同步的 TestClient：它会在另一个线程里另起一个事件循环，
    而数据库的写入协程绑定在测试的循环上——任何触发写入的路由
    (比如 /review) 都会永远等待一个在死循环上创建的 future。
    这类死锁在生产里同样会发生，只要有人把 db 的启动和使用放在不同循环里。
    """
    from fastapi import FastAPI

    db = Database(tmp_path / "radar.db")
    await db.start()
    events = EventBus()
    events.configure_fingerprint(FINGERPRINT)
    events.set_sink(repo.make_event_sink(db))
    bus.set_sink(repo.make_event_sink(db))

    service = FakeService(db)
    api.bind_service(service)

    app = FastAPI()
    app.include_router(api.router)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport,
                                  base_url="http://radar") as http:
        try:
            yield http, service, db
        finally:
            await db.stop()
            api._service = None


async def seed_alert(db: Database, view: TokenView, *, kind: str = "S1",
                     near_miss: bool = False, created_at: int = NOW) -> int:
    await repo.upsert_token(db, view, source="test")
    return await repo.insert_alert(
        db, view=view, alert_kind=kind, is_near_miss=near_miss,
        created_at=created_at, correlation_id="c1", snapshot_id=None,
        scores=ScoreResult(opportunity=78.0, confidence=71.0, data_quality=83.0,
                           rug_risk=18.0, distribution=12.0),
        factors_json='[{"name":"holder_momentum","score":15}]',
        trigger_json='{"reason":"\\u6676\\u5347 S1"}',
        prev_scores_json=None, fingerprint=FINGERPRINT,
    )


# ─────────────────────────────────────────────────────────────────────────
# 健康检查
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_health_returns_version_fingerprint(client):
    http, _, _ = client
    body = (await http.get("/api/radar/health")).json()
    assert body["status"] == "ok"
    assert body["version"]["config_hash"] == "deadbeef"


@pytest.mark.asyncio
async def test_ready_returns_503_when_degraded(client):
    """报告健康的坏服务永远不会被编排层重启，而那正是最需要重启的时刻。"""
    http, service, _ = client
    assert (await http.get("/api/radar/ready")).status_code == 200

    service.collector_ok = False
    response = await http.get("/api/radar/ready")
    assert response.status_code == 503
    assert response.json()["status"] == "degraded"


@pytest.mark.asyncio
async def test_config_endpoint_exposes_thresholds_not_raw_file(client):
    http, _, _ = client
    body = (await http.get("/api/radar/config")).json()
    assert "thresholds" in body and "fingerprint" in body
    assert "email" not in body, "配置接口不应把邮件段一并暴露"
    assert "smtp_pass" not in str(body)


# ─────────────────────────────────────────────────────────────────────────
# 扫描器
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_token_list_always_carries_confidence_and_quality(client):
    """只返回机会分会诱导前端做出漂亮但危险的界面。"""
    http, service, _ = client
    view = make_view()
    service.registry.views[view.key] = view

    body = (await http.get("/api/radar/tokens")).json()
    assert body["total"] == 1
    scores = body["items"][0]["scores"]
    assert set(scores) == {"opportunity", "confidence", "data_quality",
                           "rug_risk", "distribution"}
    assert body["items"][0]["risk"]["gate_blocked"] is False
    assert body["items"][0]["mc_source"] == "reported"


@pytest.mark.asyncio
async def test_token_list_filters_and_sorts(client):
    http, service, _ = client
    high = make_view("0xaaa", token_id=1)
    low = make_view("0xbbb", state=TokenState.WATCHING, token_id=2)
    low.last_scores["opportunity"] = 30.0
    for view in (high, low):
        service.registry.views[view.key] = view

    assert (await http.get("/api/radar/tokens?state=S1")).json()["total"] == 1
    assert (await http.get(
        "/api/radar/tokens?min_opportunity=50"
    )).json()["total"] == 1
    items = (await http.get("/api/radar/tokens?sort=opportunity")).json()["items"]
    assert items[0]["scores"]["opportunity"] == 78.0


@pytest.mark.asyncio
async def test_unknown_state_is_rejected(client):
    http, _, _ = client
    response = await http.get("/api/radar/tokens?state=NOPE")
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_limit_is_capped(client):
    """一个忘记加 limit 的前端调用不应该能把 512MB 的容器打爆。"""
    http, _, _ = client
    assert (await http.get("/api/radar/tokens?limit=99999")).status_code == 422


@pytest.mark.asyncio
async def test_token_detail_includes_decision_history(client):
    http, service, db = client
    view = make_view()
    service.registry.views[view.key] = view
    await seed_alert(db, view)
    await db.drain()

    body = (await http.get("/api/radar/tokens/56/0xaaa")).json()
    assert body["identity"]["in_memory"] is True
    assert body["live"]["scores"]["confidence"] == 71.0
    assert len(body["alerts"]) == 1
    assert body["quality"]["observation_count"] == 0


@pytest.mark.asyncio
async def test_token_detail_404_for_unknown(client):
    http, _, _ = client
    assert (await http.get("/api/radar/tokens/56/0xnope")).status_code == 404


# ─────────────────────────────────────────────────────────────────────────
# 警报
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_alert_list_excludes_near_miss_by_default(client):
    """Near-Miss 从未发出信号，混进警报列表会让人误以为系统报过。"""
    http, service, db = client
    view = make_view()
    await seed_alert(db, view)
    await seed_alert(db, view, near_miss=True)
    await db.drain()

    assert (await http.get("/api/radar/alerts")).json()["total"] == 1
    assert (await http.get(
        "/api/radar/alerts?include_near_miss=true"
    )).json()["total"] == 2


@pytest.mark.asyncio
async def test_alert_detail_returns_decision_context(client):
    http, service, db = client
    view = make_view()
    alert_id = await seed_alert(db, view)
    await db.drain()

    body = (await http.get(f"/api/radar/alerts/{alert_id}")).json()
    assert body["alert"]["alert_kind"] == "S1"
    # 触发依据必须解析成对象返回，而不是让前端再解一次 JSON 字符串
    assert body["alert"]["trigger"]["reason"]
    assert body["alert"]["factors"][0]["name"] == "holder_momentum"


@pytest.mark.asyncio
async def test_review_state_is_the_only_mutable_field(client):
    """机器判断不可变，人的工作流状态可变，两者必须严格分离。"""
    http, service, db = client
    view = make_view()
    alert_id = await seed_alert(db, view)
    await db.drain()

    response = await http.post(f"/api/radar/alerts/{alert_id}/review?state=REVIEWED")
    assert response.status_code == 200
    body = (await http.get(f"/api/radar/alerts/{alert_id}")).json()
    assert body["alert"]["review_state"] == "REVIEWED"
    # 评分未被人工操作改动
    assert body["alert"]["opportunity"] == 78.0

    assert (await http.post(
        f"/api/radar/alerts/{alert_id}/review?state=GARBAGE"
    )).status_code == 400
    assert (await http.post(
        "/api/radar/alerts/999999/review?state=REVIEWED"
    )).status_code == 404


# ─────────────────────────────────────────────────────────────────────────
# 研究与导出
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_rejections_return_values_not_just_reasons(client):
    """只有实际值、阈值、年龄、市值齐全才能做反事实分析。"""
    http, service, db = client
    view = make_view()
    await repo.upsert_token(db, view, source="test")
    repo.insert_rejection(
        db, view=view, gate="research_gate", rule="top10_max",
        actual_value=68.0, threshold_value=55.0, actual_text="Top10 68%",
        occurred_at=NOW, data_quality=80.0, snapshot_id=None,
        correlation_id="c1", fingerprint=FINGERPRINT,
    )
    await db.drain()

    body = (await http.get("/api/radar/research/rejections")).json()
    assert body["total"] == 1
    item = body["items"][0]
    assert item["actual_value"] == 68.0 and item["threshold_value"] == 55.0
    assert body["by_rule"][0]["rule"] == "top10_max"


@pytest.mark.asyncio
async def test_export_rejects_unknown_dataset(client):
    """把 dataset 拼进 SQL 是最经典的注入面。"""
    http, _, _ = client
    response = await http.get("/api/radar/export/token_master;DROP")
    assert response.status_code == 400
    assert "alerts" in response.json()["detail"]


@pytest.mark.asyncio
async def test_export_includes_fingerprint(client):
    """导出的数据脱离系统后，唯一能说明"这是哪套参数产出的"就是指纹。"""
    http, service, db = client
    view = make_view()
    await seed_alert(db, view)
    await db.drain()

    body = (await http.get("/api/radar/export/alerts")).json()
    assert body["fingerprint"]["config_hash"] == "deadbeef"
    assert body["count"] == 1


@pytest.mark.asyncio
async def test_diagnostics_bundle_gathers_everything(client):
    http, service, db = client
    view = make_view()
    await seed_alert(db, view)
    await db.drain()

    body = (await http.get("/api/radar/diagnostics/bundle")).json()
    assert body["table_sizes"]["alerts"] == 1
    assert "diagnostics" in body and "recent_errors" in body
    assert "pending_emails" in body


@pytest.mark.asyncio
async def test_events_endpoint_parses_payload(client):
    http, service, db = client
    from radar.obs.events import EventType

    bus.emit(EventType.SERVICE_STARTED, module="test", summary="启动",
             payload={"chains": ["56"]})
    await db.drain()

    body = (await http.get("/api/radar/events")).json()
    assert body["total"] >= 1
    assert any(e["payload"] and "chains" in e["payload"] for e in body["items"])
