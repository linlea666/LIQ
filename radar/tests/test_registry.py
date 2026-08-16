"""注册表集成测试。

用真实 SQLite（临时文件）跑完整流水线，因此同时验证：
schema 列名与仓储层字典是否对齐、单写入协程是否正常、
领域层各模块的接线是否正确。

纯 mock 的测试在这里价值有限——最容易出的错恰恰是
"snapshots 表少一列" 这种只有真跑一次才会暴露的问题。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from radar.domain.models import (  # noqa: E402
    QualityReport,
    ScoreResult,
    TokenObservation,
    TokenState,
)
from radar.domain.risk_gate import RiskDecision  # noqa: E402
from radar.domain.states import StateDecision  # noqa: E402
from radar.obs.events import EventBus, EventType  # noqa: E402
from radar.registry import Evaluation, TokenRegistry  # noqa: E402
from radar.storage import repo  # noqa: E402
from radar.storage.db import Database  # noqa: E402

NOW = 1_800_000_000_000

CONFIG = {
    "registry": {"max_tokens_in_memory": 6, "evict_batch": 2,
                 "restore_limit": 100, "restore_max_age_hours": 48},
    "quality": {
        "freshness": {
            "market": {"fresh_sec": 300, "stale_sec": 900},
            "holders": {"fresh_sec": 300, "stale_sec": 1200},
            "distribution": {"fresh_sec": 600, "stale_sec": 2400},
            "smart_money": {"fresh_sec": 900, "stale_sec": 3600},
            "social": {"fresh_sec": 3600, "stale_sec": 14400},
            "audit": {"fresh_sec": 86400, "stale_sec": 259200},
            "supply": {"fresh_sec": 86400, "stale_sec": 604800},
        },
        "missing_penalty": {"market": 35, "holders": 15, "distribution": 20,
                            "smart_money": 10, "social": 3, "audit": 8, "supply": 6},
        "stale_penalty": {"market": 25, "holders": 12, "distribution": 15,
                          "smart_money": 10, "social": 2, "audit": 5, "supply": 4},
        "mc_deviation_warn": 0.15, "mc_deviation_conflict": 0.35,
        "mc_conflict_penalty": 15, "min_for_s1": 55, "min_for_s2": 70,
    },
    "features": {
        "min_market_cap_denominator": 30000.0,
        "min_liquidity_denominator": 3000.0,
        "min_holder_denominator": 30.0,
        "winsorize_growth_cap": 5.0,
        "lookback_windows_sec": [300, 900, 3600],
        "snapshot_min_interval_sec": 55,
        "snapshot_min_interval_by_state": {"WATCHING": 900, "DISCOVERED": 1800},
    },
    "risk": {
        "execution_blocker": {"audit_risk_level_min": 4, "sell_tax_max_pct": 10.0,
                              "buy_tax_max_pct": 10.0, "honeypot_blocks": True},
        "research_gate": {
            "top10_max_pct_by_age": [
                {"max_age_min": 10, "threshold": 85.0},
                {"max_age_min": 60, "threshold": 70.0},
                {"max_age_min": None, "threshold": 55.0},
            ],
            "combined_concentration_max_pct": 55.0,
            "dev_max_pct": 15.0,
            "min_liquidity_usd": 5000.0,
            "min_liquidity_mc_ratio": 0.01,
            "wash_trading_tags": ["DEV_WASH_TRADING", "INSIDER_WASH_TRADING"],
        },
        "audit": {"min_liquidity_usd": 3000.0, "recheck_before_s1": True, "ttl_sec": 86400},
    },
    "scoring": {
        "strategy_version": "test-1.0.0",
        "opportunity_weights": {
            "holder_momentum": 20, "capital_flow": 20, "smart_money": 15,
            "liquidity_quality": 15, "distribution_health": 15,
            "social_momentum": 10, "valuation_upside": 5,
        },
        "valuation_scenarios": {"base_mc": 3_000_000},
    },
    "state_machine": {
        "min_dwell_sec": 180, "exit_confirm_cycles": 2,
        "transitions": {
            "s0": {"enter_opportunity": 55, "exit_opportunity": 45,
                   "max_rug_risk": 70, "min_data_quality": 40},
            "s1": {"enter_opportunity": 72, "exit_opportunity": 62,
                   "max_rug_risk": 45, "min_data_quality": 60,
                   "min_liquidity_usd": 12000.0, "min_confidence": 50},
            "s2": {"enter_opportunity": 85, "exit_opportunity": 75,
                   "max_rug_risk": 35, "min_data_quality": 70,
                   "min_confidence": 65, "min_smart_money_count": 3},
        },
        "distribution": {"enter_score": 60, "exit_score": 45},
        "dormant_after_stale_sec": 21600,
        "dead_min_liquidity_usd": 500.0,
    },
    "alerts": {"near_miss_margin": 5.0},
}

FINGERPRINT = {
    "strategy_version": "test-1.0.0",
    "feature_version": "f1.0.0",
    "parser_version": "p1.0.0",
    "config_hash": "deadbeef",
    "code_commit": "testsha",
}


@pytest.fixture
async def env(tmp_path):
    db = Database(tmp_path / "radar.db")
    await db.start()
    events = EventBus()
    events.configure_fingerprint(FINGERPRINT)
    events.set_sink(repo.make_event_sink(db))
    registry = TokenRegistry(db=db, events=events, config=CONFIG,
                             fingerprint=FINGERPRINT)
    try:
        yield registry, db, events
    finally:
        await db.stop()


def obs(contract: str, *, endpoint: str = "trending", observed_at: int = NOW,
        **fields) -> TokenObservation:
    defaults = dict(
        symbol="TEST", price=0.001, market_cap=150_000.0, liquidity=40_000.0,
        holders=900, circulating_supply=150_000_000.0,
        launch_time_ms=observed_at - 1_800_000,
    )
    defaults.update(fields)
    return TokenObservation(
        chain_id="56", contract_address=contract, endpoint=endpoint,
        observed_at=observed_at, parser_version="p1.0.0", **defaults,
    )


# ─────────────────────────────────────────────────────────────────────────
# 建档与合并
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ingest_creates_token_and_persists_first_seen(env):
    registry, db, _ = env
    views = await registry.ingest([obs("0xaaa")])
    assert len(views) == 1
    view = views[0]
    assert view.token_id is not None

    row = await db.fetch_one(
        "SELECT * FROM token_master WHERE contract_address=?", ("0xaaa",)
    )
    assert row is not None
    # 首次发现现场必须带真实数值，否则"我们在多少市值时发现它"永远无法回答
    assert row["first_seen_market_cap"] == pytest.approx(150_000.0)
    assert row["first_seen_holders"] == 900
    assert row["first_seen_source"] == "trending"


@pytest.mark.asyncio
async def test_first_seen_is_never_overwritten(env):
    """这是整个系统里最不能被覆盖的字段：领先时间研究的唯一锚点。"""
    registry, db, _ = env
    await registry.ingest([obs("0xaaa", market_cap=50_000.0)])
    await registry.ingest([obs("0xaaa", market_cap=900_000.0,
                               observed_at=NOW + 60_000)])
    row = await db.fetch_one(
        "SELECT first_seen_market_cap FROM token_master WHERE contract_address=?",
        ("0xaaa",),
    )
    assert row["first_seen_market_cap"] == pytest.approx(50_000.0)


@pytest.mark.asyncio
async def test_repeated_ingest_reuses_same_token_id(env):
    registry, _, _ = env
    first = (await registry.ingest([obs("0xaaa")]))[0].token_id
    second = (await registry.ingest([obs("0xaaa", observed_at=NOW + 1000)]))[0].token_id
    assert first == second


@pytest.mark.asyncio
async def test_observation_without_contract_is_skipped(env):
    registry, _, _ = env
    assert await registry.ingest([obs("")]) == []
    assert len(registry) == 0


# ─────────────────────────────────────────────────────────────────────────
# 评估流水线：真正落库
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_evaluate_writes_snapshot_with_all_columns(env):
    """跑通一次真实写库，专门用来暴露 schema 与仓储层字典不对齐。"""
    registry, db, _ = env
    view = (await registry.ingest([obs("0xaaa")]))[0]
    ev = await registry.evaluate(view, NOW, force_snapshot=True)

    assert ev.snapshot_id is not None
    row = await db.fetch_one("SELECT * FROM snapshots WHERE snapshot_id=?",
                             (ev.snapshot_id,))
    assert row is not None
    assert row["price"] == pytest.approx(0.001)
    assert row["holders"] == 900
    assert row["opportunity"] is not None
    assert row["features_json"] and row["dq_json"] and row["risk_flags_json"]
    # source_at 必须保持 NULL：币安没给数据生成时间，伪造会毁掉领先时间研究
    assert row["source_at"] is None
    assert row["token_age_sec"] == 1800


@pytest.mark.asyncio
async def test_snapshot_interval_is_relaxed_for_low_states(env):
    """低状态代币不该和 S1 同频写快照。

    单行快照约 4KB，若 4000 枚币一律每分钟一帧，磁盘增长约 23GB/天，
    而抽稀要 48 小时后才启动——磁盘会先被填满，连带拖垮同机的主后端。
    """
    registry, db, _ = env
    # 刻意选一个各项都平庸的币：贴近阈值的币会触发 Near-Miss，
    # 而 Near-Miss 是决策现场、本就该绕过间隔限制
    weak = dict(market_cap=60_000.0, liquidity=6_000.0, holders=60,
                circulating_supply=60_000_000.0)
    view = (await registry.ingest([obs("0xaaa", **weak)]))[0]
    ev = await registry.evaluate(view, NOW, force_snapshot=True)
    assert not ev.state.near_miss, "测试前提：该币不应贴近任何阈值"
    await db.drain()
    base = len(await db.fetch_all("SELECT * FROM snapshots"))

    # WATCHING 档间隔 900 秒：3 分钟后再评估不应产生新快照
    assert view.state.value in {"DISCOVERED", "WATCHING"}
    await registry.ingest([obs("0xaaa", observed_at=NOW + 180_000, **weak)])
    await registry.evaluate(view, NOW + 180_000)
    await db.drain()
    assert len(await db.fetch_all("SELECT * FROM snapshots")) == base

    # 但超过该状态的间隔后必须恢复记录，否则历史会彻底断掉
    await registry.ingest([obs("0xaaa", observed_at=NOW + 2_000_000, **weak)])
    await registry.evaluate(view, NOW + 2_000_000)
    await db.drain()
    assert len(await db.fetch_all("SELECT * FROM snapshots")) > base


@pytest.mark.asyncio
async def test_unconfigured_state_falls_back_to_dense_interval(env):
    """默认值选错的代价必须是多花磁盘，而不是丢失决策历史。"""
    registry, _, _ = env
    assert registry._snapshot_interval_ms(TokenState.S2) == 55_000
    assert registry._snapshot_interval_ms(TokenState.WATCHING) == 900_000


@pytest.mark.asyncio
async def test_scores_are_all_within_range(env):
    registry, _, _ = env
    view = (await registry.ingest([obs("0xaaa")]))[0]
    ev = await registry.evaluate(view, NOW)
    for name, value in ev.scores.as_scores_dict().items():
        assert 0.0 <= value <= 100.0, f"{name} 越界: {value}"


@pytest.mark.asyncio
async def test_history_is_pushed_after_evaluation_not_before(env):
    """顺序错了会让所有速度类特征恒等于 0——不报错，只是信号消失。"""
    registry, _, _ = env
    view = (await registry.ingest([obs("0xaaa")]))[0]
    await registry.evaluate(view, NOW)
    assert len(view.history) == 1

    await registry.ingest([obs("0xaaa", holders=1400, observed_at=NOW + 300_000)])
    ev = await registry.evaluate(view, NOW + 300_000)
    growth = ev.view.last_features.get("holder_growth_5m")
    assert growth is not None and growth > 0, "第二轮应能算出持有人增长"


@pytest.mark.asyncio
async def test_honeypot_blocks_and_emits_event(env):
    registry, db, events = env
    view = (await registry.ingest([
        obs("0xbad", endpoint="audit", honeypot=True, audit_available=True),
    ]))[0]
    ev = await registry.evaluate(view, NOW)

    assert ev.risk.blocked
    assert view.state == TokenState.BLOCKED
    assert ev.scores.rug_risk == 100.0
    assert events.counts().get(EventType.HONEYPOT_DETECTED.value) == 1

    await db.drain()
    rows = await db.fetch_all(
        "SELECT rule, gate FROM rejections WHERE token_id=?", (view.token_id,)
    )
    assert any(r["rule"] == "honeypot" for r in rows)


@pytest.mark.asyncio
async def test_state_change_is_persisted_immediately(env):
    """状态变更必须在同一次评估内落库为新状态。

    历史 bug：update_token_runtime 在状态机结论应用之前执行，
    数据库永远存"变更前"的状态。对终局评估（如 S1→DEAD 后再无观测）
    这意味着 token_master 永远停在 S1，重启恢复时把死币复活。
    """
    registry, db, _ = env
    view = (await registry.ingest([obs("0xzombie")]))[0]
    view.state = TokenState.S1
    view.state_since_ms = NOW - 600_000

    # 流动性被抽干 → 本次评估应转 DEAD，且 DB 立刻是 DEAD
    await registry.ingest([obs("0xzombie", liquidity=10.0,
                               observed_at=NOW + 60_000)])
    ev = await registry.evaluate(view, NOW + 60_000)
    assert ev.state.new_state == TokenState.DEAD
    assert view.state == TokenState.DEAD

    await db.drain()
    row = await db.fetch_one(
        "SELECT state FROM token_master WHERE contract_address=?", ("0xzombie",)
    )
    assert row["state"] == "DEAD", "终局状态未落库——重启后死币会被复活"


@pytest.mark.asyncio
async def test_rejections_are_not_rewritten_every_cycle(env):
    """同一枚币每轮重复写拒绝记录，几小时就能把表灌满。"""
    registry, db, _ = env
    view = (await registry.ingest([obs("0xthin", liquidity=1000.0)]))[0]
    for i in range(4):
        await registry.evaluate(view, NOW + i * 60_000)

    await db.drain()
    rows = await db.fetch_all(
        "SELECT rule FROM rejections WHERE token_id=?", (view.token_id,)
    )
    assert 0 < len(rows) <= 3, f"拒绝记录被重复写入: {len(rows)} 条"


@pytest.mark.asyncio
async def test_quality_degradation_alerts_only_on_transition(env):
    """每轮重发同一条数据质量告警，一天就是几万条噪声，真警报会被埋掉。"""
    registry, _, events = env
    # 只提供市场与持有人数据，筹码/聪明钱/审计三组全缺 → 数据质量必然不达标
    view = (await registry.ingest([obs("0xdq")]))[0]
    for i in range(5):
        view.last_observed_ms = NOW + i * 60_000
        await registry.evaluate(view, NOW + i * 60_000)

    degraded = events.counts().get(EventType.DATA_QUALITY_DEGRADED.value, 0)
    assert degraded <= 1, f"数据质量告警重复了 {degraded} 次"


@pytest.mark.asyncio
async def test_quality_recovery_emits_event(env):
    registry, _, events = env
    view = (await registry.ingest([obs("0xdq2")]))[0]
    await registry.evaluate(view, NOW)
    if not view.quality_degraded:
        pytest.skip("该样本未进入降级状态，无法验证恢复路径")

    # 补齐缺失的字段组后应发出恢复事件，而不是悄悄转好
    await registry.ingest([obs(
        "0xdq2", endpoint="detail", observed_at=NOW + 60_000,
        top10_percent=25.0, dev_percent=1.0, sniper_percent=2.0,
        insider_percent=1.0, bundler_percent=1.0, new_wallet_percent=20.0,
        smart_money_count=5, exit_rate=10.0, net_inflow=30_000.0,
        social_hype=5000.0, search_count_24h=200,
        audit_available=True, audit_risk_level=0, honeypot=False,
        buy_tax_pct=0.0, sell_tax_pct=0.0,
    )])
    await registry.evaluate(view, NOW + 60_000)
    assert not view.quality_degraded
    assert events.counts().get(EventType.DATA_QUALITY_RECOVERED.value, 0) == 1


@pytest.mark.asyncio
async def test_market_cap_cross_check_records_deviation(env):
    registry, _, _ = env
    # price × supply = 300K，但上报市值 100K，偏离 200%
    view = (await registry.ingest([
        obs("0xmc", price=0.002, circulating_supply=150_000_000.0, market_cap=100_000.0),
    ]))[0]
    ev = await registry.evaluate(view, NOW)
    assert ev.quality.mc_deviation_ratio is not None
    assert ev.quality.mc_deviation_ratio > 0.35
    assert "mc_conflict" in ev.quality.penalties
    # 里程碑必须用上报口径，且明确标注来源，否则不同币之间不可比
    assert ev.mc_source == "reported"
    assert ev.market_cap == pytest.approx(100_000.0)


@pytest.mark.asyncio
async def test_missing_market_cap_falls_back_to_computed(env):
    registry, _, _ = env
    view = (await registry.ingest([
        obs("0xmc2", market_cap=None, price=0.002, circulating_supply=100_000_000.0),
    ]))[0]
    ev = await registry.evaluate(view, NOW)
    assert ev.mc_source == "computed"
    assert ev.market_cap == pytest.approx(200_000.0)


# ─────────────────────────────────────────────────────────────────────────
# 内存淘汰
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_eviction_respects_limit(env):
    registry, _, _ = env
    for i in range(10):
        await registry.ingest([obs(f"0x{i:03d}")])
    assert len(registry) <= CONFIG["registry"]["max_tokens_in_memory"]
    assert registry.stats.evicted > 0


@pytest.mark.asyncio
async def test_eviction_never_drops_promoted_tokens(env):
    """S0 及以上是长期研究价值最高的部分，宁可少收新币也不能丢。"""
    registry, _, _ = env
    keeper = (await registry.ingest([obs("0xkeep")]))[0]
    keeper.state = TokenState.S1
    keeper.state_since_ms = NOW

    reject_sample = (await registry.ingest([obs("0xsample")]))[0]
    reject_sample.is_reject_sample = True

    for i in range(20):
        await registry.ingest([obs(f"0xnew{i:03d}")])

    assert registry.get("56", "0xkeep") is not None
    assert registry.get("56", "0xsample") is not None


@pytest.mark.asyncio
async def test_evicted_token_keeps_its_state_when_it_returns(env):
    """被淘汰的币重新出现时，状态必须从数据库回填。

    否则它会被当成全新的币从 DISCOVERED 重来，
    还会把 token_master 里真实的状态覆盖掉——
    在榜单频繁进出的币身上，这等于状态永远无法稳定。
    """
    registry, db, _ = env
    view = (await registry.ingest([obs("0xback")]))[0]
    view.state = TokenState.WATCHING
    view.state_since_ms = NOW - 600_000
    original_id = view.token_id
    repo.update_token_runtime(db, view)
    await db.drain()

    # 模拟内存淘汰
    registry._views.pop(("56", "0xback"))
    assert registry.get("56", "0xback") is None

    revived = (await registry.ingest([obs("0xback", observed_at=NOW + 60_000)]))[0]
    assert revived.token_id == original_id
    assert revived.state == TokenState.WATCHING
    assert revived.state_since_ms == NOW - 600_000


@pytest.mark.asyncio
async def test_returning_token_keeps_reject_sample_flag(env):
    registry, db, _ = env
    view = (await registry.ingest([obs("0xrs")]))[0]
    view.is_reject_sample = True
    repo.update_token_runtime(db, view)
    await db.drain()

    registry._views.pop(("56", "0xrs"))
    revived = (await registry.ingest([obs("0xrs", observed_at=NOW + 60_000)]))[0]
    assert revived.is_reject_sample, "拒绝样本标记丢失会让反事实对照组悄悄缩水"


@pytest.mark.asyncio
async def test_eviction_warns_when_everything_is_protected(env):
    registry, _, events = env
    for i in range(8):
        view = (await registry.ingest([obs(f"0xp{i:03d}")]))[0]
        view.state = TokenState.S1
    await registry.ingest([obs("0xtrigger")])
    # 触发淘汰但无可淘汰对象时必须告警，而不是静默让内存继续涨
    assert events.counts().get(EventType.MEMORY_WARNING.value, 0) >= 1


# ─────────────────────────────────────────────────────────────────────────
# 重启恢复
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_restore_recovers_state_and_prevents_duplicate_promotion(env, tmp_path):
    """不恢复状态会导致重启后把已是 S1 的币重新晋升一次，用户收到重复警报。"""
    registry, db, events = env
    view = (await registry.ingest([obs("0xaaa")]))[0]
    view.state = TokenState.S1
    view.state_since_ms = NOW
    view.last_observed_ms = NOW
    await registry.evaluate(view, NOW, force_snapshot=True)
    repo.update_token_runtime(db, view)
    await db.drain()

    fresh = TokenRegistry(db=db, events=events, config=CONFIG,
                          fingerprint=FINGERPRINT)
    restored = await fresh.restore(NOW + 60_000)
    assert restored == 1
    recovered = fresh.get("56", "0xaaa")
    assert recovered is not None
    assert recovered.state == TokenState.S1
    assert recovered.token_id == view.token_id
    assert recovered.state_since_ms == NOW


@pytest.mark.asyncio
async def test_restore_rebuilds_history_for_promoted_tokens(env):
    registry, db, events = env
    view = (await registry.ingest([obs("0xaaa")]))[0]
    for i in range(4):
        ts = NOW + i * 120_000
        view.values["holders"] = 900 + i * 100
        view.last_observed_ms = ts
        await registry.evaluate(view, ts, force_snapshot=True)
    # 写完快照后再标记为 S1：本用例隔离"恢复历史"这一个行为，
    # 而不是同时考验这枚测试币能否真的达到 S1 的评分门槛
    view.state = TokenState.S1
    repo.update_token_runtime(db, view)
    await db.drain()

    fresh = TokenRegistry(db=db, events=events, config=CONFIG,
                          fingerprint=FINGERPRINT)
    await fresh.restore(NOW + 600_000)
    recovered = fresh.get("56", "0xaaa")
    assert recovered is not None
    assert recovered.history_depth >= 4, "S1 代币重启后应恢复历史，避免速度特征归零"


@pytest.mark.asyncio
async def test_restore_skips_dead_tokens(env):
    registry, db, events = env
    view = (await registry.ingest([obs("0xdead")]))[0]
    view.state = TokenState.DEAD
    repo.update_token_runtime(db, view)
    await db.drain()

    fresh = TokenRegistry(db=db, events=events, config=CONFIG,
                          fingerprint=FINGERPRINT)
    assert await fresh.restore(NOW) == 0


# ─────────────────────────────────────────────────────────────────────────
# S1 锚点生命周期（S2 确认制的基准数据）
# ─────────────────────────────────────────────────────────────────────────

def _state_eval(view, old: TokenState, new: TokenState) -> Evaluation:
    """构造一次"状态机已给出结论"的评估，用于隔离测试状态变更副作用。"""
    return Evaluation(
        view=view,
        quality=QualityReport(score=90.0),
        scores=ScoreResult(opportunity=75, confidence=80, data_quality=90,
                           rug_risk=10, distribution=5),
        risk=RiskDecision(),
        state=StateDecision(old_state=old, new_state=new, changed=(old != new)),
        features_json="{}",
        evaluated_at=NOW,
        market_cap=view.getf("market_cap"),
    )


@pytest.mark.asyncio
async def test_s1_promotion_records_anchor_and_demotion_clears_it(env):
    """进入 S1 记录现场锚点；跌回 S1 以下清除；转 DISTRIBUTION 保留。

    锚点是 S2 确认全部行为条件（回撤、LP 抽离、dev 减仓）的基准，
    记录/清除时机错了，确认制整体失效。
    """
    registry, _, _ = env
    view = (await registry.ingest([obs("0xanchor")]))[0]

    registry._apply_state_change(_state_eval(view, TokenState.S0, TokenState.S1), NOW)
    assert view.s1_anchor is not None
    assert view.s1_anchor["price"] == pytest.approx(0.001)
    assert view.s1_anchor["liquidity"] == pytest.approx(40_000.0)
    assert view.s1_anchor["holders"] == 900
    assert view.s1_peak_price == pytest.approx(0.001)
    assert not view.s1_inflow_dipped

    # 转派发观察：锚点仍有对照价值，必须保留
    registry._apply_state_change(
        _state_eval(view, TokenState.S1, TokenState.DISTRIBUTION), NOW)
    assert view.s1_anchor is not None

    # 真正跌回观察级：清除，下次晋升重新记录
    registry._apply_state_change(
        _state_eval(view, TokenState.DISTRIBUTION, TokenState.WATCHING), NOW)
    assert view.s1_anchor is None
    assert view.s1_peak_price is None


@pytest.mark.asyncio
async def test_restore_recovers_s1_anchor_from_latest_real_alert(env):
    """重启后 S1+ 代币必须带回锚点，否则 S2 确认永远无法通过；
    且只认非 near-miss 警报——near-miss 不是真实的晋升现场。"""
    registry, db, events = env
    view = (await registry.ingest([obs("0xanchor")]))[0]
    view.state = TokenState.S1
    view.state_since_ms = NOW
    view.last_observed_ms = NOW
    scores = ScoreResult(opportunity=75, confidence=80, data_quality=90,
                         rug_risk=10, distribution=5)
    await repo.insert_alert(
        db, view=view, alert_kind="S1", is_near_miss=False, created_at=NOW,
        correlation_id="c1", snapshot_id=None, scores=scores,
        factors_json=None, trigger_json=None, prev_scores_json=None,
        fingerprint=FINGERPRINT,
    )
    # 更晚的 near-miss 记录带着另一个价格，不得被当成锚点
    view.values["price"] = 0.005
    await repo.insert_alert(
        db, view=view, alert_kind="S1", is_near_miss=True, created_at=NOW + 10_000,
        correlation_id="c2", snapshot_id=None, scores=scores,
        factors_json=None, trigger_json=None, prev_scores_json=None,
        fingerprint=FINGERPRINT,
    )
    repo.update_token_runtime(db, view)
    await db.drain()

    fresh = TokenRegistry(db=db, events=events, config=CONFIG,
                          fingerprint=FINGERPRINT)
    await fresh.restore(NOW + 60_000)
    recovered = fresh.get("56", "0xanchor")
    assert recovered is not None
    assert recovered.s1_anchor is not None
    assert recovered.s1_anchor["price"] == pytest.approx(0.001)
    assert recovered.s1_anchor["at"] == NOW
    assert recovered.s1_peak_price == pytest.approx(0.001)


@pytest.mark.asyncio
async def test_revived_s1_token_reloads_anchor(env):
    """被内存淘汰后复活的 S1 币也要带回锚点（单币建档路径）。"""
    registry, db, _ = env
    view = (await registry.ingest([obs("0xanchor")]))[0]
    view.state = TokenState.S1
    view.state_since_ms = NOW
    scores = ScoreResult(opportunity=75, confidence=80, data_quality=90,
                         rug_risk=10, distribution=5)
    await repo.insert_alert(
        db, view=view, alert_kind="S1", is_near_miss=False, created_at=NOW,
        correlation_id="c1", snapshot_id=None, scores=scores,
        factors_json=None, trigger_json=None, prev_scores_json=None,
        fingerprint=FINGERPRINT,
    )
    repo.update_token_runtime(db, view)
    await db.drain()

    registry._views.pop(("56", "0xanchor"))
    revived = (await registry.ingest([obs("0xanchor", observed_at=NOW + 60_000)]))[0]
    assert revived.state == TokenState.S1
    assert revived.s1_anchor is not None
    assert revived.s1_anchor["price"] == pytest.approx(0.001)


@pytest.mark.asyncio
async def test_restore_keeps_reject_samples_even_when_stale(env):
    """拒绝样本是反事实研究的对照组，不能因为长期没更新就丢掉。"""
    registry, db, events = env
    view = (await registry.ingest([obs("0xsample")]))[0]
    view.is_reject_sample = True
    view.last_observed_ms = NOW - 30 * 24 * 3_600_000
    repo.update_token_runtime(db, view)
    await db.drain()

    fresh = TokenRegistry(db=db, events=events, config=CONFIG,
                          fingerprint=FINGERPRINT)
    assert await fresh.restore(NOW) == 1
