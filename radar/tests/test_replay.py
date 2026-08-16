"""Replay 引擎测试。

回测最致命的失效方式不是算错，而是**算得跟线上不一样却没人发现**。
因此测试的重点是三条安全边界（不写生产库、不发邮件、不读墙上时钟），
以及"换一套阈值确实会改变产出"这个最基本的有效性验证。
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from radar.domain.models import TokenObservation  # noqa: E402
from radar.obs.events import EventBus, bus  # noqa: E402
from radar.registry import TokenRegistry  # noqa: E402
from radar.replay import (  # noqa: E402
    NullTransport,
    ReplayEngine,
    _observation_from_snapshot,
    _parse_time,
    build_parser,
)
from radar.storage import repo  # noqa: E402
from radar.storage.db import Database  # noqa: E402

from test_registry import CONFIG, FINGERPRINT  # noqa: E402
from test_tracker import TRACKER_CONFIG  # noqa: E402

NOW = 1_800_000_000_000
HOUR = 3_600_000

REPLAY_CONFIG: dict[str, Any] = {
    **CONFIG,
    **{k: v for k, v in TRACKER_CONFIG.items() if k in ("tracker", "service")},
    "registry": {**CONFIG["registry"], "max_tokens_in_memory": 500},
    "storage": {"downsample_after_hours": 48},
    "email": {"enabled": False, "max_per_hour": 100, "digest_on_overflow": False,
              "send_s1": True, "send_s2": True, "send_distribution": True},
    "alerts": {
        "near_miss_margin": 5.0, "near_miss_cooldown_sec": 600,
        "cooldown_sec": {"s1": 3600, "s2": 1800, "distribution": 3600},
        "anomaly": {"warmup_hours": 48, "baseline_window_hours": 168,
                    "deviation_multiple": 8.0},
    },
}


def obs(contract: str, at: int, **overrides: Any) -> TokenObservation:
    """构造一枚各项指标都很健康的币，让它能真正走到 S1。"""
    values: dict[str, Any] = {
        "price": 0.001, "market_cap": 400_000.0, "liquidity": 60_000.0,
        "holders": 2000, "top10_percent": 20.0, "dev_percent": 2.0,
        "sniper_percent": 3.0, "insider_percent": 2.0, "bundler_percent": 1.0,
        "new_wallet_percent": 10.0, "smart_money_percent": 8.0,
        "volume_1h": 300_000.0, "volume_5m": 40_000.0, "volume_24h": 900_000.0,
        "volume_1h_buy": 200_000.0, "volume_1h_sell": 100_000.0,
        "count_1h": 900, "count_1h_buy": 600, "count_1h_sell": 300,
        "unique_trader_1h": 500, "unique_trader_24h": 1500,
        "pct_change_1h": 40.0, "pct_change_5m": 8.0, "pct_change_24h": 120.0,
        "smart_money_count": 8, "net_inflow": 90_000.0, "exit_rate": 8.0,
        "social_hype": 70.0, "search_count_24h": 900,
        "audit_available": True, "audit_risk_level": 1,
        "buy_tax_pct": 0.0, "sell_tax_pct": 0.0, "honeypot": False,
        "contract_verified": True,
        "circulating_supply": 400_000_000.0, "total_supply": 400_000_000.0,
    }
    values.update(overrides)
    return TokenObservation(
        chain_id="56", contract_address=contract, endpoint="trending",
        observed_at=at, parser_version="p1.0.0", symbol="PEPE",
        launch_time_ms=at - 2 * HOUR, seen_on_trending=True, **values,
    )


async def build_source(path: Path, *, points: int = 8) -> None:
    """造一份带真实快照序列的源库。"""
    db = Database(path)
    await db.start()
    events = EventBus()
    events.configure_fingerprint(FINGERPRINT)
    events.set_sink(repo.make_event_sink(db))
    bus.set_sink(repo.make_event_sink(db))

    registry = TokenRegistry(db=db, events=events, config=REPLAY_CONFIG,
                             fingerprint=FINGERPRINT)
    for i in range(points):
        at = NOW + i * 90_000
        views = await registry.ingest([
            obs("0xaaa", at, holders=2000 + i * 300,
                market_cap=400_000.0 + i * 90_000.0,
                price=0.001 * (1 + i * 0.2)),
        ])
        await registry.evaluate(views[0], at, endpoint="trending")
    await db.drain()
    await db.stop()


# ─────────────────────────────────────────────────────────────────────────
# 快照 → 观测的还原
# ─────────────────────────────────────────────────────────────────────────

def test_derived_columns_are_not_fed_back_as_input():
    """把 opportunity / features_json 当输入喂回去，回测就成了自证循环。"""
    row = {
        "chain_id": "56", "contract_address": "0xaaa", "endpoint": "trending",
        "observed_at": NOW, "source_at": None, "parser_version": "p1.0.0",
        "symbol": "PEPE", "name": None, "decimals": None,
        "launch_time_ms": NOW - HOUR, "creator_address": None,
        "launch_platform": None, "circulating_supply": None,
        "total_supply": None, "max_supply": None,
        "price": 0.002, "holders": 1500, "audit_available": 1,
        "opportunity": 99.0, "features_json": '{"fake": 1}',
        "data_quality": 100.0, "state": "S2",
    }
    observation = _observation_from_snapshot(row)
    assert observation is not None
    assert observation.price == 0.002
    assert observation.holders == 1500
    assert not hasattr(observation, "opportunity")
    assert "features_json" not in observation.__slots__


def test_audit_available_false_is_distinct_from_unknown():
    """False（币安明确说没有审计）与 None（我们没查过）混淆会放行一批币。"""
    base = {
        "chain_id": "56", "contract_address": "0xaaa", "endpoint": "detail",
        "observed_at": NOW, "source_at": None, "parser_version": "p",
        "symbol": None, "name": None, "decimals": None, "launch_time_ms": None,
        "creator_address": None, "launch_platform": None,
        "circulating_supply": None, "total_supply": None, "max_supply": None,
    }
    assert _observation_from_snapshot({**base, "audit_available": 0}).audit_available is False
    assert _observation_from_snapshot({**base, "audit_available": 1}).audit_available is True
    assert _observation_from_snapshot({**base, "audit_available": None}).audit_available is None


def test_snapshot_without_identity_is_skipped():
    assert _observation_from_snapshot({
        "chain_id": None, "contract_address": "0xaaa", "observed_at": NOW,
    }) is None


# ─────────────────────────────────────────────────────────────────────────
# 时间解析与 CLI 参数
# ─────────────────────────────────────────────────────────────────────────

def test_parse_relative_and_absolute_time():
    now = _parse_time("0d")
    assert abs(now - _parse_time("0h")) < 5000
    week_ago = _parse_time("7d")
    assert 6.9 * 86_400_000 < now - week_ago < 7.1 * 86_400_000
    assert _parse_time("1800000000000") == 1_800_000_000_000
    assert _parse_time("2026-08-16") > 0


def test_cli_requires_explicit_output():
    parser = build_parser()
    args = parser.parse_args(["--db", "a.db", "--config", "c.yaml", "--out", "b.db"])
    assert args.start == "7d"
    with pytest.raises(SystemExit):
        parser.parse_args(["--db", "a.db", "--config", "c.yaml"])


# ─────────────────────────────────────────────────────────────────────────
# 安全边界
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_null_transport_never_sends():
    transport = NullTransport()
    await transport.send(subject="任何主题", html="<p>任何内容</p>")
    assert transport.blocked == 1


@pytest.mark.asyncio
async def test_readonly_database_rejects_writes(tmp_path):
    """靠"约定不写"是不够的：一次误写生产库造成的污染无法回滚。"""
    path = tmp_path / "source.db"
    await build_source(path, points=2)

    ro = Database(path, read_only=True)
    await ro.start()
    try:
        rows = await ro.fetch_all("SELECT COUNT(*) AS n FROM snapshots")
        assert rows[0]["n"] > 0, "只读连接仍必须能正常查询"
        with pytest.raises(RuntimeError, match="只读"):
            ro.submit("DELETE FROM snapshots")
        with pytest.raises(RuntimeError, match="只读"):
            await ro.submit_returning("DELETE FROM snapshots")
    finally:
        await ro.stop()


@pytest.mark.asyncio
async def test_replay_does_not_modify_source(tmp_path):
    source = tmp_path / "source.db"
    await build_source(source)
    before = source.read_bytes()
    counts_before = await _table_counts(source)

    engine = ReplayEngine(source_db=source, output_db=tmp_path / "out.db",
                          config=REPLAY_CONFIG, fingerprint=FINGERPRINT)
    await engine.run(start_ms=NOW - HOUR, end_ms=NOW + 24 * HOUR)

    assert await _table_counts(source) == counts_before
    assert source.read_bytes() == before, "源库字节级不变"


async def _table_counts(path: Path) -> dict[str, int]:
    db = Database(path, read_only=True)
    await db.start()
    try:
        result = {}
        for table in ("snapshots", "alerts", "outcomes", "token_master",
                      "radar_events", "email_outbox"):
            row = await db.fetch_one(f"SELECT COUNT(*) AS n FROM {table}")
            result[table] = int(row["n"])
        return result
    finally:
        await db.stop()


# ─────────────────────────────────────────────────────────────────────────
# 重放有效性
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_replay_reproduces_evaluations(tmp_path):
    source = tmp_path / "source.db"
    await build_source(source, points=8)

    engine = ReplayEngine(source_db=source, output_db=tmp_path / "out.db",
                          config=REPLAY_CONFIG, fingerprint=FINGERPRINT)
    report = await engine.run(start_ms=NOW - HOUR, end_ms=NOW + 24 * HOUR)

    assert report.snapshots_read > 0
    assert report.evaluations == report.snapshots_read
    assert report.tokens == 1
    assert (tmp_path / "out.db").exists()


@pytest.mark.asyncio
async def test_replay_is_deterministic(tmp_path):
    """同样的输入跑两遍必须完全一致。

    任何一处漏用墙上时钟，两次结果就会不同——而这种不一致
    在单次运行里是看不出来的，只会让人对着一份不可复现的回测调参。
    """
    source = tmp_path / "source.db"
    await build_source(source, points=8)

    reports = []
    for i in range(2):
        engine = ReplayEngine(source_db=source, output_db=tmp_path / f"out{i}.db",
                              config=REPLAY_CONFIG, fingerprint=FINGERPRINT)
        reports.append(await engine.run(start_ms=NOW - HOUR, end_ms=NOW + 24 * HOUR))

    first, second = reports
    assert first.snapshots_read == second.snapshots_read
    assert first.evaluations == second.evaluations
    assert first.alerts == second.alerts
    assert first.alerts_by_kind == second.alerts_by_kind
    assert first.near_miss == second.near_miss


@pytest.mark.asyncio
async def test_looser_thresholds_produce_more_alerts(tmp_path):
    """这是回测存在的全部意义：换阈值必须真的改变产出。"""
    source = tmp_path / "source.db"
    await build_source(source, points=10)

    strict = copy.deepcopy(REPLAY_CONFIG)
    strict["state_machine"]["transitions"]["s1"]["enter_opportunity"] = 99.0
    strict["state_machine"]["transitions"]["s2"]["enter_opportunity"] = 99.5

    loose = copy.deepcopy(REPLAY_CONFIG)
    loose["state_machine"]["transitions"]["s1"]["enter_opportunity"] = 20.0
    loose["state_machine"]["transitions"]["s1"]["min_confidence"] = 0.0
    loose["state_machine"]["transitions"]["s1"]["min_data_quality"] = 0.0

    strict_report = await ReplayEngine(
        source_db=source, output_db=tmp_path / "strict.db",
        config=strict, fingerprint=FINGERPRINT,
    ).run(start_ms=NOW - HOUR, end_ms=NOW + 24 * HOUR)

    loose_report = await ReplayEngine(
        source_db=source, output_db=tmp_path / "loose.db",
        config=loose, fingerprint=FINGERPRINT,
    ).run(start_ms=NOW - HOUR, end_ms=NOW + 24 * HOUR)

    assert loose_report.alerts > strict_report.alerts


@pytest.mark.asyncio
async def test_replay_records_baseline_for_comparison(tmp_path):
    """没有对照组的回测只能回答"会报多少个"，回答不了"比现在多还是少"。"""
    source = tmp_path / "source.db"
    await build_source(source, points=10)

    db = Database(source)
    await db.start()
    from radar.domain.models import ScoreResult, TokenView

    view = TokenView(chain_id="56", contract_address="0xaaa", token_id=1)
    await repo.insert_alert(
        db, view=view, alert_kind="S1", is_near_miss=False, created_at=NOW,
        correlation_id="c", snapshot_id=None,
        scores=ScoreResult(opportunity=80.0, confidence=70.0, data_quality=80.0,
                           rug_risk=20.0, distribution=10.0),
        factors_json=None, trigger_json=None, prev_scores_json=None,
        fingerprint=FINGERPRINT,
    )
    await db.drain()
    await db.stop()

    report = await ReplayEngine(
        source_db=source, output_db=tmp_path / "out.db",
        config=REPLAY_CONFIG, fingerprint=FINGERPRINT,
    ).run(start_ms=NOW - HOUR, end_ms=NOW + 24 * HOUR)

    assert report.baseline_alerts == 1
    assert report.baseline_by_kind == {"S1": 1}


@pytest.mark.asyncio
async def test_empty_window_is_reported_not_silently_passed(tmp_path):
    source = tmp_path / "source.db"
    await build_source(source, points=3)

    report = await ReplayEngine(
        source_db=source, output_db=tmp_path / "out.db",
        config=REPLAY_CONFIG, fingerprint=FINGERPRINT,
    ).run(start_ms=NOW - 100 * HOUR, end_ms=NOW - 90 * HOUR)

    assert report.snapshots_read == 0
    assert any("没有任何快照" in w for w in report.warnings)


@pytest.mark.asyncio
async def test_downsampled_window_is_flagged(tmp_path):
    """基于稀疏数据的结论带着数字的权威感，比不做回测更危险。"""
    source = tmp_path / "source.db"
    await build_source(source, points=3)

    report = await ReplayEngine(
        source_db=source, output_db=tmp_path / "out.db",
        config=REPLAY_CONFIG, fingerprint=FINGERPRINT,
    ).run(start_ms=NOW - 200 * HOUR, end_ms=NOW + 24 * HOUR)

    assert report.downsampled_ratio > 0
    assert any("降采样" in w for w in report.warnings)
