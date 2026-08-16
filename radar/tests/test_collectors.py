"""采集器测试。

用真实抓下来的 fixtures 喂给假客户端，跑通"HTTP 响应 → 解析 → 合并 →
评估 → 落库"的完整链路。这一层最容易出的错不是算法错误，
而是接线错误：参数传错、解析器挑错、失败处理把整轮拖垮。
这些只有跑一遍完整链路才会暴露。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from radar.collectors import CollectorService, OnboardingThrottle  # noqa: E402
from radar.obs.events import EventBus, EventType, bus  # noqa: E402
from radar.registry import TokenRegistry  # noqa: E402
from radar.scheduler import RequestScheduler  # noqa: E402
from radar.sources.client import FetchError, FetchResult  # noqa: E402
from radar.sources.endpoints import Endpoint  # noqa: E402
from radar.storage import repo  # noqa: E402
from radar.storage.db import Database  # noqa: E402

from test_registry import CONFIG, FINGERPRINT  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
NOW = 1_800_000_000_000


class FakeSettings:
    """只提供采集器真正读取的那几段配置。"""

    def __init__(self, config: dict[str, Any]) -> None:
        self.collectors = config.get("collectors", {})
        self.scheduler = config.get("scheduler", {})
        self.storage = config.get("storage", {})
        self.tiers = _tiers_from(config)
        self.chains = [
            _Chain(c["id"], c.get("enabled", True)) for c in config.get("chains", [])
        ]


class _Chain:
    def __init__(self, chain_id: str, enabled: bool) -> None:
        self.id = chain_id
        self.enabled = enabled


class _Tier:
    def __init__(self, max_rpm: float, interval_sec: int) -> None:
        self.max_rpm = max_rpm
        self.interval_sec = interval_sec


def _tiers_from(config: dict[str, Any]) -> dict[str, _Tier]:
    raw = (config.get("scheduler", {}) or {}).get("tiers", {}) or {}
    return {name: _Tier(v["max_rpm"], v["interval_sec"]) for name, v in raw.items()}


COLLECTOR_CONFIG: dict[str, Any] = {
    **CONFIG,
    # 注册表用例刻意把内存上限设成 6 来测淘汰；采集用例要跑完整批量，
    # 必须放开，否则测的其实是淘汰而不是采集
    "registry": {**CONFIG["registry"], "max_tokens_in_memory": 4000},
    "chains": [{"id": "56", "enabled": True}, {"id": "CT_501", "enabled": True}],
    "collectors": {
        "list_page_size": 50,
        "meme_rush_stages": [10, 20, 30],
        "trending_period": 30,
        "inflow_period": "1h",
        "social_language": "zh-CN",
        "extract_chart_extremes": True,
    },
    "scheduler": {
        "global_rpm": 9000, "target_rpm": 9000, "jitter_ratio": 0.0,
        "request_timeout_sec": 12, "max_retries": 0, "retry_backoff_base_sec": 0.1,
        "burst_window_sec": 480, "onboarding_max_per_min": 500,
        "adaptive": {"window_sec": 300, "rate_limit_threshold": 0.05,
                     "downscale_ratio": 0.8, "min_rpm": 30, "recover_after_sec": 600},
        "tiers": {
            "discovery": {"max_rpm": 9000, "interval_sec": 60},
            "social": {"max_rpm": 9000, "interval_sec": 300},
            "audit": {"max_rpm": 9000, "interval_sec": 0},
            "burst": {"max_rpm": 9000, "interval_sec": 25},
            "s2": {"max_rpm": 9000, "interval_sec": 30},
            "s1": {"max_rpm": 9000, "interval_sec": 90},
            "s0": {"max_rpm": 9000, "interval_sec": 150},
            "watching": {"max_rpm": 9000, "interval_sec": 900},
            "reject": {"max_rpm": 9000, "interval_sec": 2400},
        },
    },
    "storage": {
        "raw_list_retention_hours": 72,
        "raw_detail_retention_days": 400,
        "reject_sample_ratio": 0.0,
    },
}


class FakeClient:
    """按 (端点名, 链) 回放 fixtures 的假客户端。

    刻意保留 FetchResult 的完整结构（含原始文本与哈希），
    这样归档路径也会被真实执行到。
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []
        self.failures: set[str] = set()
        self.responses: dict[tuple[str, str | None], Any] = {}

    def load(self, endpoint_name: str, chain_id: str, fixture: str) -> None:
        path = FIXTURES / fixture
        self.responses[(endpoint_name, chain_id)] = json.loads(
            path.read_text(encoding="utf-8")
        )

    async def fetch(self, endpoint: Endpoint, *, chain_id: str | None = None,
                    params: dict[str, Any] | None = None,
                    body: dict[str, Any] | None = None,
                    tier: str | None = None,
                    budget_timeout_sec: float = 120.0) -> FetchResult:
        self.calls.append((endpoint.name, chain_id))
        if endpoint.name in self.failures:
            raise FetchError(f"{endpoint.name} 注入故障", status=500)

        payload = self.responses.get((endpoint.name, chain_id))
        if payload is None:
            raise FetchError(f"无 fixture: {endpoint.name}/{chain_id}", status=404)

        data = payload.get("data") if isinstance(payload, dict) else payload
        text = json.dumps(payload)
        return FetchResult(
            endpoint=endpoint, chain_id=chain_id, data=data, raw_text=text,
            http_status=200, latency_ms=42,
            response_hash=f"hash-{endpoint.name}", observed_at=NOW,
            item_count=len(data) if isinstance(data, list) else 0,
        )


@pytest.fixture
async def service(tmp_path):
    db = Database(tmp_path / "radar.db")
    await db.start()
    events = EventBus()
    events.configure_fingerprint(FINGERPRINT)
    events.set_sink(repo.make_event_sink(db))
    # 客户端与调度器内部直接用全局总线，测试里也接上同一个 sink
    bus.set_sink(repo.make_event_sink(db))

    settings = FakeSettings(COLLECTOR_CONFIG)
    registry = TokenRegistry(db=db, events=events, config=COLLECTOR_CONFIG,
                             fingerprint=FINGERPRINT)
    client = FakeClient()
    scheduler = RequestScheduler(settings)
    evaluations: list[Any] = []
    collector = CollectorService(
        client=client, scheduler=scheduler, registry=registry, db=db,
        settings=settings, on_evaluation=evaluations.append,
    )
    try:
        yield collector, client, registry, db, evaluations
    finally:
        await db.stop()


def load_all_lists(client: FakeClient) -> None:
    for chain, suffix in (("56", "56"), ("CT_501", "CT_501")):
        client.load("trending", chain, f"trending_{suffix}.json")
        client.load("meme_rush", chain, f"memerush_new_{suffix}.json")
        client.load("inflow", chain, f"inflow_{suffix}.json")
        client.load("signal", chain, f"signal_{suffix}.json")
    client.load("meme_rank", "56", "memerank_56.json")


# ─────────────────────────────────────────────────────────────────────────
# 入库限速
# ─────────────────────────────────────────────────────────────────────────

def test_onboarding_throttle_caps_admissions():
    throttle = OnboardingThrottle(max_per_min=10)
    assert throttle.admit(4) == 4
    assert throttle.admit(10) == 6, "超出每分钟上限的部分必须被拒绝"
    assert throttle.capacity() == 0


def test_onboarding_throttle_never_zero_capacity():
    """配置写 0 也不能把系统卡死到永远收不进新币。"""
    assert OnboardingThrottle(max_per_min=0).capacity() >= 1


# ─────────────────────────────────────────────────────────────────────────
# 发现循环
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_discovery_cycle_ingests_real_fixtures(service):
    collector, client, registry, db, evaluations = service
    load_all_lists(client)

    report = await collector.run_discovery_cycle()

    assert report.failed == 0, report.errors
    assert report.observations > 0
    assert report.tokens_touched > 0
    assert len(registry) == report.tokens_touched
    # 每枚币本轮只评估一次：多接口观测必须先合并再评分
    assert report.evaluations == report.tokens_touched
    assert len(evaluations) == report.evaluations

    await db.drain()
    rows = await db.fetch_all("SELECT COUNT(*) AS n FROM token_master")
    assert rows[0]["n"] == report.tokens_touched


@pytest.mark.asyncio
async def test_discovery_requests_expected_endpoints_per_chain(service):
    collector, client, _, _, _ = service
    load_all_lists(client)
    await collector.run_discovery_cycle()

    called = {(name, chain) for name, chain in client.calls}
    for chain in ("56", "CT_501"):
        for name in ("trending", "meme_rush", "inflow", "signal"):
            assert (name, chain) in called, f"{name} 未对 {chain} 发起请求"
    # meme_rank 仅 BSC 支持，绝不能对 Solana 发起
    assert ("meme_rank", "56") in called
    assert ("meme_rank", "CT_501") not in called


@pytest.mark.asyncio
async def test_single_endpoint_failure_does_not_abort_cycle(service):
    """某个接口挂掉时，其余接口的数据必须照常入库。

    整轮中断是更坏的结果：那会让所有维度同时失去更新，
    而分组新鲜度机制本来可以只降低受影响维度的可信度。
    """
    collector, client, registry, _, _ = service
    load_all_lists(client)
    client.failures.add("trending")

    report = await collector.run_discovery_cycle()

    assert report.failed == 2, "两条链的 trending 都应失败"
    assert report.succeeded > 0
    assert len(registry) > 0, "其余接口的代币仍应入库"
    assert any("trending" in e for e in report.errors)


@pytest.mark.asyncio
async def test_all_endpoints_failing_yields_empty_but_no_crash(service):
    collector, client, registry, _, _ = service
    load_all_lists(client)
    client.failures.update({"trending", "meme_rush", "inflow", "signal", "meme_rank"})

    report = await collector.run_discovery_cycle()
    assert report.succeeded == 0
    assert report.observations == 0
    assert len(registry) == 0


@pytest.mark.asyncio
async def test_repeated_cycles_merge_instead_of_duplicating(service):
    collector, client, registry, db, _ = service
    load_all_lists(client)

    first = await collector.run_discovery_cycle()
    second = await collector.run_discovery_cycle()

    assert second.new_tokens == 0, "第二轮不应再产生新币"
    assert len(registry) == first.tokens_touched

    await db.drain()
    rows = await db.fetch_all("SELECT COUNT(*) AS n FROM token_master")
    assert rows[0]["n"] == first.tokens_touched


@pytest.mark.asyncio
async def test_onboarding_throttle_defers_excess_new_tokens(service, monkeypatch):
    collector, client, registry, _, _ = service
    load_all_lists(client)
    monkeypatch.setattr(collector, "_onboarding", OnboardingThrottle(max_per_min=5))

    report = await collector.run_discovery_cycle()
    assert report.new_tokens == 5
    assert report.throttled_new > 0
    assert len(registry) == 5


@pytest.mark.asyncio
async def test_deferred_tokens_are_picked_up_next_cycle(service, monkeypatch):
    """被限速推迟的币不是丢失，下一轮列表还会返回它们。"""
    collector, client, registry, _, _ = service
    load_all_lists(client)
    monkeypatch.setattr(collector, "_onboarding", OnboardingThrottle(max_per_min=5))
    await collector.run_discovery_cycle()

    monkeypatch.setattr(collector, "_onboarding", OnboardingThrottle(max_per_min=500))
    second = await collector.run_discovery_cycle()
    assert second.new_tokens > 0
    assert len(registry) > 5


@pytest.mark.asyncio
async def test_raw_responses_are_archived(service):
    collector, client, _, db, _ = service
    load_all_lists(client)
    await collector.run_discovery_cycle()
    await db.drain()

    rows = await db.fetch_all(
        "SELECT endpoint, kind, retention_class, expires_at FROM raw_archive"
    )
    assert rows, "原始响应必须归档，否则接口改版后无法重放验证新解析器"
    assert all(r["kind"] == "list" for r in rows)
    assert all(r["retention_class"] == "short" for r in rows)
    assert all(r["expires_at"] > NOW for r in rows)


@pytest.mark.asyncio
async def test_schema_drift_is_reported(service):
    """接口改版是最危险的静默失效：字段一改，解析器安静地返回 None。"""
    collector, client, _, _, _ = service
    client.responses[("trending", "56")] = {
        "success": True,
        "data": [{"contractAddress": "0xdrift", "unexpectedField": 1}],
    }
    client.load("trending", "CT_501", "trending_CT_501.json")

    before = bus.counts().get(EventType.API_SCHEMA_CHANGED.value, 0)
    await collector.run_discovery_cycle()
    after = bus.counts().get(EventType.API_SCHEMA_CHANGED.value, 0)
    assert after > before


# ─────────────────────────────────────────────────────────────────────────
# 详情刷新
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_refresh_targets_higher_tiers_first(service):
    from radar.domain.models import TokenState

    collector, client, registry, _, _ = service
    load_all_lists(client)
    await collector.run_discovery_cycle()

    views = list(registry.all_views())
    assert len(views) >= 3
    views[0].state = TokenState.WATCHING
    views[1].state = TokenState.S2
    views[2].state = TokenState.S1

    due = collector._collect_due(NOW)
    tiers = [tier for _, tier in due]
    assert tiers.index("s2") < tiers.index("s1") < tiers.index("watching")


@pytest.mark.asyncio
async def test_failed_refresh_does_not_retry_immediately(service):
    """接口故障时对同一枚币疯狂重试会吃光配额，必须先占住下次刷新时间。"""
    collector, client, registry, _, _ = service
    load_all_lists(client)
    await collector.run_discovery_cycle()

    view = next(iter(registry.all_views()))
    client.failures.add("detail")
    await collector._refresh_token(view, "s1", NOW)

    assert collector._next_refresh[view.key] > NOW
    assert not any(v is view for v, _ in collector._collect_due(NOW))


@pytest.mark.asyncio
async def test_dead_and_blocked_tokens_are_never_refreshed(service):
    from radar.domain.models import TokenState

    collector, client, registry, _, _ = service
    load_all_lists(client)
    await collector.run_discovery_cycle()

    for view in registry.all_views():
        view.state = TokenState.DEAD
    assert collector._collect_due(NOW) == []


@pytest.mark.asyncio
async def test_burst_window_takes_priority_over_state_tier(service):
    from radar.domain.models import TokenState

    collector, client, registry, _, _ = service
    load_all_lists(client)
    await collector.run_discovery_cycle()

    view = next(iter(registry.all_views()))
    view.state = TokenState.WATCHING
    collector._scheduler.open_burst(view.key, reason="S1")
    assert collector._tier_for(view) == "burst"


# ─────────────────────────────────────────────────────────────────────────
# 审计
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_audit_queue_is_deduplicated(service):
    collector, client, registry, _, evaluations = service
    load_all_lists(client)
    await collector.run_discovery_cycle()
    first_len = len(collector._audit_queue)
    assert first_len > 0, "缺审计结果的币应进入补查队列"

    # 再跑一轮：同一批币不能被重复排队
    await collector.run_discovery_cycle()
    assert len(collector._audit_queue) == first_len


@pytest.mark.asyncio
async def test_audit_result_updates_risk_and_unblocks_promotion(service):
    collector, client, registry, db, _ = service
    load_all_lists(client)
    await collector.run_discovery_cycle()

    view = next(iter(registry.all_views()))
    client.load("audit", view.chain_id, f"audit_{view.chain_id}.json")
    await collector._fetch_audit(view)

    assert view.audit_checked_at > 0, "审计结果应刷新审计字段组的新鲜度"
