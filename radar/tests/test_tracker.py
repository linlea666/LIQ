"""追踪器测试。

这一层的失效方式是最隐蔽的：不崩溃、不报错，只是给出一个
系统性偏乐观或偏悲观的结论，然后我们据此把阈值越调越错。
因此测试重点全部放在**口径正确性**上：右删失、三种 ATH 的区分、
滑点方向、以及重启不能抹掉历史极值。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from radar.domain.models import TokenView  # noqa: E402
from radar.obs.events import EventBus, bus  # noqa: E402
from radar.obs.logging_setup import now_ms  # noqa: E402
from radar.storage import repo  # noqa: E402
from radar.storage.db import Database  # noqa: E402
from radar.tracker import (  # noqa: E402
    HorizonWindow,
    KpiReporter,
    OutcomeTracker,
    _label_for,
)

NOW = now_ms()
HOUR = 3_600_000

FINGERPRINT = {
    "strategy_version": "test-1.0.0", "feature_version": "f1.0.0",
    "parser_version": "p1.0.0", "config_hash": "deadbeef", "code_commit": "testsha",
}

TRACKER_CONFIG: dict[str, Any] = {
    "service": {"tz_offset_hours": 8},
    "tracker": {
        "milestones_usd": [100_000, 1_000_000, 10_000_000],
        "milestone_hysteresis_pct": 3.0,
        "outcome_horizons_hours": [1, 4, 24],
        "entry_delay_sec": [15, 30, 60, 120],
        "sustained_min_hold_sec": 300,
        "sustained_min_volume_usd": 2000.0,
        "paper_position_sizes": [100, 1000],
        "liquidity_slippage_factor": 2.0,
    },
}


def make_view(*, price: float = 0.001, market_cap: float = 100_000.0,
              liquidity: float = 50_000.0, token_id: int = 1,
              contract: str = "0xaaa") -> TokenView:
    view = TokenView(chain_id="56", contract_address=contract,
                     token_id=token_id, symbol="PEPE")
    view.values.update({"price": price, "market_cap": market_cap,
                        "liquidity": liquidity, "volume_5m": 8000.0})
    view.launch_time_ms = NOW - HOUR
    view.last_observed_ms = NOW
    return view


def move(view: TokenView, *, price: float | None = None,
         market_cap: float | None = None, volume_5m: float | None = None,
         interval_high: float | None = None,
         interval_low: float | None = None,
         interval_seen_at: int = 0) -> TokenView:
    if price is not None:
        view.values["price"] = price
    if market_cap is not None:
        view.values["market_cap"] = market_cap
    if volume_5m is not None:
        view.values["volume_5m"] = volume_5m
    view.values["interval_high"] = interval_high
    view.values["interval_low"] = interval_low
    if interval_seen_at:
        view.interval_seen_at = interval_seen_at
    return view


@pytest.fixture
async def env(tmp_path):
    db = Database(tmp_path / "radar.db")
    await db.start()
    events = EventBus()
    events.configure_fingerprint(FINGERPRINT)
    events.set_sink(repo.make_event_sink(db))
    bus.set_sink(repo.make_event_sink(db))
    tracker = OutcomeTracker(db=db, config=TRACKER_CONFIG, fingerprint=FINGERPRINT)
    try:
        yield tracker, db
    finally:
        await db.stop()


async def seed_alert(db: Database, view: TokenView, *, kind: str = "S1",
                     created_at: int = NOW) -> int:
    """建一条真实的 alerts 记录，让 Outcome 的外键关联可用。"""
    from radar.domain.models import ScoreResult

    await repo.upsert_token(db, view, source="test")
    return await repo.insert_alert(
        db, view=view, alert_kind=kind, is_near_miss=False,
        created_at=created_at, correlation_id="c1", snapshot_id=None,
        scores=ScoreResult(opportunity=80.0, confidence=70.0, data_quality=80.0,
                           rug_risk=20.0, distribution=10.0),
        factors_json=None, trigger_json=None, prev_scores_json=None,
        fingerprint=FINGERPRINT,
    )


# ─────────────────────────────────────────────────────────────────────────
# 时间窗口（纯逻辑）
# ─────────────────────────────────────────────────────────────────────────

def test_horizon_freezes_after_maturity():
    """24 小时内最大浮盈不能被第 5 天的行情污染。"""
    window = HorizonWindow(label="1h", horizon_ms=HOUR)
    window.update(2.0, 0)
    window.update(3.0, HOUR // 2)
    window.update(99.0, HOUR * 5)

    assert window.matured
    assert window.max_price == 3.0
    assert window.as_dict(1.0)["mfe_pct"] == 200.0


def test_unmatured_horizon_is_marked():
    """未到期不等于收益为零——这个区别决定了 KPI 会不会被系统性拖低。"""
    window = HorizonWindow(label="24h", horizon_ms=24 * HOUR)
    window.update(1.5, 60_000)
    assert window.as_dict(1.0) == {"mfe_pct": 50.0, "mae_pct": 50.0, "matured": False}


def test_outcome_label_prefers_rug_over_peak():
    """先涨 12 倍再归零，本质上是 RUG 而不是 MOON。"""
    assert _label_for(12.0, -95.0) == "RUG"
    assert _label_for(12.0, -30.0) == "MOON"
    assert _label_for(1.05, -10.0) == "FLAT"
    assert _label_for(None, None) is None


# ─────────────────────────────────────────────────────────────────────────
# 基础追踪
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_track_records_signal_snapshot(env):
    tracker, db = env
    view = make_view()
    alert_id = await seed_alert(db, view)

    tracked = tracker.track(alert_id=alert_id, alert_kind="S1", view=view, at_ms=NOW)
    assert tracked is not None
    await db.drain()

    rows = await db.fetch_all("SELECT * FROM outcomes")
    assert len(rows) == 1
    assert rows[0]["signal_price"] == 0.001
    assert rows[0]["signal_market_cap"] == 100_000.0
    assert rows[0]["is_final"] == 0


@pytest.mark.asyncio
async def test_duplicate_track_is_ignored(env):
    tracker, db = env
    view = make_view()
    alert_id = await seed_alert(db, view)
    assert tracker.track(alert_id=alert_id, alert_kind="S1", view=view, at_ms=NOW)
    assert tracker.track(alert_id=alert_id, alert_kind="S1", view=view, at_ms=NOW) is None


@pytest.mark.asyncio
async def test_mfe_and_mae_are_tracked_incrementally(env):
    """峰谷必须在观测发生的当下记录：快照 48 小时后会被抽稀，事后补不回。"""
    tracker, db = env
    view = make_view(price=0.001)
    alert_id = await seed_alert(db, view)
    tracker.track(alert_id=alert_id, alert_kind="S1", view=view, at_ms=NOW)

    tracker.on_observation(move(view, price=0.005), NOW + 60_000)
    tracker.on_observation(move(view, price=0.0002), NOW + 120_000)
    tracker.on_observation(move(view, price=0.001), NOW + 180_000)
    await db.drain()

    row = (await db.fetch_all("SELECT * FROM outcomes"))[0]
    assert row["raw_ath_price"] == 0.005
    assert row["min_price"] == 0.0002
    assert row["mfe_pct"] == pytest.approx(400.0)
    assert row["mae_pct"] == pytest.approx(-80.0)
    assert row["peak_multiple"] == pytest.approx(5.0)
    assert row["current_multiple"] == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_interval_extremes_fill_polling_gaps(env):
    """30 秒采样下，短时间内暴涨又暴跌的币只能靠区间极值抓到。

    极值窗口必须完全落在信号之后（seen_at - 回看窗口 >= signal_at），
    因此把观测放在信号后 5 分钟。
    """
    tracker, db = env
    view = make_view(price=0.001)
    alert_id = await seed_alert(db, view)
    tracker.track(alert_id=alert_id, alert_kind="S1", view=view, at_ms=NOW)

    at = NOW + 300_000
    tracker.on_observation(
        move(view, price=0.0011, interval_high=0.009, interval_low=0.0009,
             interval_seen_at=at),
        at,
    )
    await db.drain()

    row = (await db.fetch_all("SELECT * FROM outcomes"))[0]
    assert row["raw_ath_price"] == 0.009, "轮询瞬间的价格远低于区间最高价"
    assert row["min_price"] == 0.0009


@pytest.mark.asyncio
async def test_pre_signal_interval_extremes_are_ignored(env):
    """信号之前留下的区间极值绝不能算进信号之后的收益。

    实盘事故：合并视图永久携带崩盘前的 interval_high（拉盘顶），
    追踪器把它当作当前观测，伪造出 30204 倍的假 MOON。
    """
    tracker, db = env
    view = make_view(price=0.001)
    # 崩盘前的旧极值：seen_at 早于信号
    view.values["interval_high"] = 0.03
    view.values["interval_low"] = 0.0005
    view.interval_seen_at = NOW - 60_000

    alert_id = await seed_alert(db, view)
    tracker.track(alert_id=alert_id, alert_kind="S1", view=view, at_ms=NOW)

    # 之后的观测没有新极值，但视图仍带着旧值
    view.values["price"] = 0.0012
    tracker.on_observation(view, NOW + 60_000)
    await db.drain()

    row = (await db.fetch_all("SELECT * FROM outcomes"))[0]
    assert row["raw_ath_price"] == pytest.approx(0.0012), \
        "信号前的拉盘顶被算成了信号后的收益"


@pytest.mark.asyncio
async def test_interval_extremes_too_close_to_signal_are_ignored(env):
    """信号后立刻到达的极值窗口仍盖住信号之前，必须等窗口完全越过信号。"""
    tracker, db = env
    view = make_view(price=0.001)
    alert_id = await seed_alert(db, view)
    tracker.track(alert_id=alert_id, alert_kind="S1", view=view, at_ms=NOW)

    # 信号后 60 秒：回看 180 秒的窗口仍包含信号前 120 秒
    tracker.on_observation(
        move(view, price=0.0011, interval_high=0.05,
             interval_seen_at=NOW + 60_000),
        NOW + 60_000,
    )
    await db.drain()

    row = (await db.fetch_all("SELECT * FROM outcomes"))[0]
    assert row["raw_ath_price"] == pytest.approx(0.0011)


@pytest.mark.asyncio
async def test_time_to_multiples_recorded_once(env):
    tracker, db = env
    view = make_view(price=0.001)
    alert_id = await seed_alert(db, view)
    tracker.track(alert_id=alert_id, alert_kind="S1", view=view, at_ms=NOW)

    tracker.on_observation(move(view, price=0.002), NOW + 30_000)
    tracker.on_observation(move(view, price=0.011), NOW + 90_000)
    # 再次到达 2 倍不应覆盖首次到达时间
    tracker.on_observation(move(view, price=0.002), NOW + 300_000)
    await db.drain()

    row = (await db.fetch_all("SELECT * FROM outcomes"))[0]
    assert row["time_to_2x_sec"] == 30
    assert row["time_to_10x_sec"] == 90
    assert row["time_to_5x_sec"] == 90


# ─────────────────────────────────────────────────────────────────────────
# 延迟入场
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_delayed_entry_prices_are_recorded(env):
    """报警价不是可成交价——这是纸面收益与可能收益之间最大的一道折扣。"""
    tracker, db = env
    view = make_view(price=0.001)
    alert_id = await seed_alert(db, view)
    tracker.track(alert_id=alert_id, alert_kind="S1", view=view, at_ms=NOW)

    tracker.on_observation(move(view, price=0.0012), NOW + 20_000)
    tracker.on_observation(move(view, price=0.0018), NOW + 65_000)
    await db.drain()

    row = (await db.fetch_all("SELECT * FROM outcomes"))[0]
    assert row["entry_15s"] == 0.0012, "第 20 秒的观测在 15 秒点的容差内"
    assert row["entry_60s"] == 0.0018, "第 65 秒的观测在 60 秒点的容差内"
    assert row["entry_30s"] is None, \
        "第 65 秒的价格不能冒充 30 秒入场价，那恰好毁掉这个指标的意义"
    assert row["entry_120s"] is None, "尚未采到 120 秒后的价格，不能编造"


@pytest.mark.asyncio
async def test_dense_sampling_fills_every_entry_point(env):
    """警报后进入 25 秒高频窗口，正常采样密度下四个延迟点都应有值。"""
    tracker, db = env
    view = make_view(price=0.001)
    alert_id = await seed_alert(db, view)
    tracker.track(alert_id=alert_id, alert_kind="S1", view=view, at_ms=NOW)

    for second in range(0, 150, 25):
        tracker.on_observation(move(view, price=0.001), NOW + second * 1000)
    await db.drain()

    row = (await db.fetch_all("SELECT * FROM outcomes"))[0]
    assert all(row[f"entry_{d}s"] is not None for d in (15, 30, 60, 120))


# ─────────────────────────────────────────────────────────────────────────
# 三种 ATH 口径
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_spike_does_not_count_as_sustained_ath(env):
    """插针到 10 倍再瞬间砸回，不是"能卖出去的价格"。"""
    tracker, db = env
    view = make_view(price=0.001)
    alert_id = await seed_alert(db, view)
    tracker.track(alert_id=alert_id, alert_kind="S1", view=view, at_ms=NOW)

    tracker.on_observation(move(view, price=0.001), NOW + 30_000)
    tracker.on_observation(move(view, price=0.010), NOW + 60_000)
    tracker.on_observation(move(view, price=0.001), NOW + 90_000)
    for i in range(6):
        tracker.on_observation(move(view, price=0.001), NOW + 120_000 + i * 60_000)
    await db.drain()

    row = (await db.fetch_all("SELECT * FROM outcomes"))[0]
    assert row["raw_ath_price"] == 0.010, "屏幕最高价确实到过 10 倍"
    assert row["sustained_ath_price"] == pytest.approx(0.001), \
        "但可持续价格不应把插针算进去"


@pytest.mark.asyncio
async def test_sustained_ath_recognises_held_level(env):
    tracker, db = env
    view = make_view(price=0.001)
    alert_id = await seed_alert(db, view)
    tracker.track(alert_id=alert_id, alert_kind="S1", view=view, at_ms=NOW)

    # 站上 3 倍并稳住超过 5 分钟
    for i in range(10):
        tracker.on_observation(move(view, price=0.003), NOW + (i + 1) * 60_000)
    await db.drain()

    row = (await db.fetch_all("SELECT * FROM outcomes"))[0]
    assert row["sustained_ath_price"] == pytest.approx(0.003)


@pytest.mark.asyncio
async def test_sustained_ath_requires_tradable_volume(env):
    """成交清淡时的高价没有意义：挂单卖不出去。"""
    tracker, db = env
    view = make_view(price=0.001)
    alert_id = await seed_alert(db, view)
    tracker.track(alert_id=alert_id, alert_kind="S1", view=view, at_ms=NOW)

    for i in range(10):
        tracker.on_observation(
            move(view, price=0.003, volume_5m=50.0), NOW + (i + 1) * 60_000
        )
    await db.drain()

    row = (await db.fetch_all("SELECT * FROM outcomes"))[0]
    assert row["sustained_ath_price"] is None


@pytest.mark.asyncio
async def test_sustained_ath_needs_full_window_coverage(env):
    """刚开始追踪时只有两个相邻采样点，不能当作"持续了 5 分钟"。"""
    tracker, db = env
    view = make_view(price=0.001)
    alert_id = await seed_alert(db, view)
    tracker.track(alert_id=alert_id, alert_kind="S1", view=view, at_ms=NOW)

    tracker.on_observation(move(view, price=0.005), NOW + 10_000)
    tracker.on_observation(move(view, price=0.005), NOW + 20_000)
    await db.drain()

    row = (await db.fetch_all("SELECT * FROM outcomes"))[0]
    assert row["sustained_ath_price"] is None


# ─────────────────────────────────────────────────────────────────────────
# 纸面仓位与滑点
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_larger_position_suffers_more_slippage(env):
    """策略能承载多少钱，只有并列三档仓位才看得出来。"""
    tracker, db = env
    view = make_view(price=0.001, liquidity=20_000.0)
    alert_id = await seed_alert(db, view)
    tracker.track(alert_id=alert_id, alert_kind="S1", view=view, at_ms=NOW)
    tracker.on_observation(move(view, price=0.010), NOW + 60_000)
    await db.drain()

    rows = {r["size_usd"]: r
            for r in await db.fetch_all("SELECT * FROM paper_positions")}
    assert set(rows) == {100.0, 1000.0}
    assert rows[1000.0]["est_slippage_pct"] > rows[100.0]["est_slippage_pct"]
    assert rows[1000.0]["peak_value_usd"] < rows[100.0]["peak_value_usd"] * 10


@pytest.mark.asyncio
async def test_unknown_liquidity_uses_worst_case_slippage(env):
    """未知不等于没有滑点。低估滑点会让我们据以放大仓位。"""
    tracker, db = env
    view = make_view(price=0.001)
    view.values["liquidity"] = None
    alert_id = await seed_alert(db, view)
    tracker.track(alert_id=alert_id, alert_kind="S1", view=view, at_ms=NOW)
    await db.drain()

    rows = await db.fetch_all("SELECT est_slippage_pct FROM paper_positions")
    assert all(r["est_slippage_pct"] == 50.0 for r in rows)


@pytest.mark.asyncio
async def test_liq_adjusted_multiple_uses_largest_position(env):
    """这个数字用来判断策略能承载多少钱，乐观那一档没有决策价值。"""
    tracker, db = env
    view = make_view(price=0.001, liquidity=20_000.0)
    alert_id = await seed_alert(db, view)
    tracker.track(alert_id=alert_id, alert_kind="S1", view=view, at_ms=NOW)
    tracker.on_observation(move(view, price=0.010), NOW + 60_000)
    await db.drain()

    outcome = (await db.fetch_all("SELECT * FROM outcomes"))[0]
    largest = (await db.fetch_all(
        "SELECT * FROM paper_positions ORDER BY size_usd DESC LIMIT 1"
    ))[0]
    assert outcome["liq_adjusted_multiple"] == pytest.approx(
        largest["peak_value_usd"] / largest["size_usd"], rel=1e-3
    )
    assert outcome["liq_adjusted_multiple"] < outcome["peak_multiple"]


# ─────────────────────────────────────────────────────────────────────────
# 里程碑
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_milestone_recorded_once_with_hysteresis(env):
    """市值在阈值上下抖动的币，不加滞回会在几分钟内产生几十条记录。"""
    tracker, db = env
    view = make_view(market_cap=90_000.0)
    await seed_alert(db, view)

    tracker.on_observation(view, NOW)
    tracker.on_observation(move(view, market_cap=101_000.0), NOW + 60_000)
    tracker.on_observation(move(view, market_cap=104_000.0), NOW + 120_000)
    tracker.on_observation(move(view, market_cap=99_000.0), NOW + 180_000)
    tracker.on_observation(move(view, market_cap=105_000.0), NOW + 240_000)
    await db.drain()

    rows = await db.fetch_all("SELECT * FROM milestones")
    assert len(rows) == 1, f"里程碑被重复记录 {len(rows)} 次"
    assert rows[0]["milestone_usd"] == 100_000.0
    assert rows[0]["is_first_upcross"] == 1


@pytest.mark.asyncio
async def test_milestone_hysteresis_blocks_marginal_cross(env):
    tracker, db = env
    view = make_view(market_cap=101_000.0)
    await seed_alert(db, view)
    # 101_000 < 100_000 × 1.03，尚未真正站稳
    tracker.on_observation(view, NOW)
    await db.drain()
    assert await db.fetch_all("SELECT * FROM milestones") == []


@pytest.mark.asyncio
async def test_multiple_milestones_crossed_at_once(env):
    """一次观测跨过多档时，每一档都要留下记录。"""
    tracker, db = env
    view = make_view(market_cap=50_000.0)
    await seed_alert(db, view)
    # 第一帧建立基线（50k 未越过任何档），第二帧一口气跨过三档
    tracker.on_observation(view, NOW)
    tracker.on_observation(move(view, market_cap=15_000_000.0), NOW + 60_000)
    await db.drain()

    rows = await db.fetch_all("SELECT milestone_usd FROM milestones ORDER BY milestone_usd")
    assert [r["milestone_usd"] for r in rows] == [100_000.0, 1_000_000.0, 10_000_000.0]


@pytest.mark.asyncio
async def test_first_sighting_does_not_fabricate_past_crossings(env):
    """发现时已经很大的币，它跨越低档位的时刻发生在我们看到它之前。

    把它们记成"刚刚上穿"不只是噪音，而是直接伪造了这张表的核心用途：
    「多久涨到 100 万」会退化成「我们发现它时它已经多大了」。
    """
    tracker, db = env
    view = make_view(market_cap=5_000_000.0)
    await seed_alert(db, view)

    tracker.on_observation(view, NOW)
    await db.drain()
    assert await db.fetch_all("SELECT * FROM milestones") == []
    assert tracker.snapshot()["milestones_seeded"] == 2, "100k 与 1M 应记为基线"

    # 但此后真正见证的跨越必须记下来
    tracker.on_observation(move(view, market_cap=11_000_000.0), NOW + 60_000)
    await db.drain()
    rows = await db.fetch_all("SELECT milestone_usd FROM milestones")
    assert [r["milestone_usd"] for r in rows] == [10_000_000.0]


@pytest.mark.asyncio
async def test_downtime_gap_does_not_fabricate_crossings(env):
    """停机期间涨上去的币，重启后同样不该把那些跨越记成刚刚发生。

    这是首次发现问题的同一类错误：判据是"我们有没有亲眼看到"，
    而不是"数据库里有没有记录过"。
    """
    tracker, db = env
    view = make_view(market_cap=90_000.0)
    await seed_alert(db, view)
    tracker.on_observation(view, NOW)
    tracker.on_observation(move(view, market_cap=200_000.0), NOW + 60_000)
    await db.drain()
    assert len(await db.fetch_all("SELECT * FROM milestones")) == 1

    # 重启：停机期间从 20 万涨到 5000 万
    fresh = OutcomeTracker(db=db, config=TRACKER_CONFIG, fingerprint=FINGERPRINT)
    await fresh.restore()
    fresh.on_observation(move(view, market_cap=50_000_000.0), NOW + 7_200_000)
    await db.drain()

    rows = await db.fetch_all("SELECT milestone_usd FROM milestones")
    assert [r["milestone_usd"] for r in rows] == [100_000.0], (
        "停机期间的跨越未被见证，不应落库"
    )


@pytest.mark.asyncio
async def test_seeded_milestones_are_not_reported_again(env):
    tracker, db = env
    view = make_view(market_cap=5_000_000.0)
    await seed_alert(db, view)
    tracker.seed_milestones(view.key, [100_000.0, 1_000_000.0])
    tracker.on_observation(view, NOW)
    await db.drain()
    assert await db.fetch_all("SELECT * FROM milestones") == []


# ─────────────────────────────────────────────────────────────────────────
# 定案与恢复
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_outcome_finalized_after_longest_horizon(env):
    tracker, db = env
    view = make_view(price=0.001)
    alert_id = await seed_alert(db, view)
    tracker.track(alert_id=alert_id, alert_kind="S1", view=view, at_ms=NOW)

    tracker.on_observation(move(view, price=0.003), NOW + 25 * HOUR)
    await db.drain()

    row = (await db.fetch_all("SELECT * FROM outcomes"))[0]
    assert row["is_final"] == 1
    horizons = json.loads(row["horizons_json"])
    assert all(h["matured"] for h in horizons.values())
    assert tracker.snapshot()["active"] == 0

    positions = await db.fetch_all("SELECT status FROM paper_positions")
    assert all(p["status"] == "closed" for p in positions)


@pytest.mark.asyncio
async def test_sweep_finalizes_tokens_that_stopped_updating(env):
    """币变成 DEAD 后不再有观测，这条记录会永久占内存且永远算不进 KPI。"""
    tracker, db = env
    view = make_view()
    alert_id = await seed_alert(db, view)
    tracker.track(alert_id=alert_id, alert_kind="S1", view=view, at_ms=NOW - 30 * HOUR)

    assert tracker.snapshot()["active"] == 1
    assert tracker.sweep(NOW) == 1
    assert tracker.snapshot()["active"] == 0
    await db.drain()
    assert (await db.fetch_all("SELECT is_final FROM outcomes"))[0]["is_final"] == 1


@pytest.mark.asyncio
async def test_sweep_leaves_active_tracking_alone(env):
    tracker, db = env
    view = make_view()
    alert_id = await seed_alert(db, view)
    tracker.track(alert_id=alert_id, alert_kind="S1", view=view, at_ms=NOW - HOUR)
    assert tracker.sweep(NOW) == 0
    assert tracker.snapshot()["active"] == 1


@pytest.mark.asyncio
async def test_restore_preserves_historical_extremes(env):
    """重启抹掉历史最高价，会让涨了 8 倍随后回落的币被记成从未涨过。"""
    tracker, db = env
    view = make_view(price=0.001)
    alert_id = await seed_alert(db, view)
    tracker.track(alert_id=alert_id, alert_kind="S1", view=view, at_ms=NOW)
    tracker.on_observation(move(view, price=0.008), NOW + 60_000)
    tracker.on_observation(move(view, price=0.0009), NOW + 120_000)
    await db.drain()

    fresh = OutcomeTracker(db=db, config=TRACKER_CONFIG, fingerprint=FINGERPRINT)
    assert await fresh.restore() == 1

    fresh.on_observation(move(view, price=0.002), NOW + 180_000)
    await db.drain()
    row = (await db.fetch_all("SELECT * FROM outcomes"))[0]
    assert row["raw_ath_price"] == 0.008
    assert row["min_price"] == 0.0009


@pytest.mark.asyncio
async def test_restore_finalizes_records_expired_during_downtime(env):
    tracker, db = env
    view = make_view()
    alert_id = await seed_alert(db, view, created_at=NOW - 40 * HOUR)
    tracker.track(alert_id=alert_id, alert_kind="S1", view=view,
                  at_ms=NOW - 40 * HOUR)
    await db.drain()

    fresh = OutcomeTracker(db=db, config=TRACKER_CONFIG, fingerprint=FINGERPRINT)
    assert await fresh.restore() == 0
    await db.drain()
    assert (await db.fetch_all("SELECT is_final FROM outcomes"))[0]["is_final"] == 1


@pytest.mark.asyncio
async def test_restore_reloads_milestones(env):
    """重启后不得把已记录的档位再记一遍。"""
    tracker, db = env
    view = make_view(market_cap=50_000.0)
    await seed_alert(db, view)
    tracker.on_observation(view, NOW)
    tracker.on_observation(move(view, market_cap=15_000_000.0), NOW + 60_000)
    await db.drain()
    before = await db.fetch_all("SELECT milestone_usd FROM milestones")
    assert len(before) == 3

    fresh = OutcomeTracker(db=db, config=TRACKER_CONFIG, fingerprint=FINGERPRINT)
    await fresh.restore()
    fresh.on_observation(view, NOW + 120_000)
    fresh.on_observation(move(view, market_cap=16_000_000.0), NOW + 180_000)
    await db.drain()
    assert len(await db.fetch_all("SELECT * FROM milestones")) == len(before)


# ─────────────────────────────────────────────────────────────────────────
# KPI
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_kpi_excludes_unmatured_samples(env):
    """把昨天刚发的警报计入 7 天窗口，等于用未到期样本把成功率拖向零。"""
    tracker, db = env
    # 一条已成熟（5 小时前）、一条未成熟（10 分钟前）
    for idx, offset in enumerate((5 * HOUR, 600_000)):
        view = make_view(price=0.001, token_id=idx + 1, contract=f"0x{idx}")
        alert_id = await seed_alert(db, view, created_at=NOW - offset)
        tracker.track(alert_id=alert_id, alert_kind="S1", view=view,
                      at_ms=NOW - offset)
        tracker.on_observation(move(view, price=0.003), NOW - offset + 60_000)
    await db.drain()

    reporter = KpiReporter(db=db, config=TRACKER_CONFIG, fingerprint=FINGERPRINT)
    results = await reporter.build(NOW)
    by_horizon = {r["horizon"]: r for r in results}

    assert by_horizon["1h"]["matured_count"] == 1, "只有 5 小时前那条跨过了 1h 窗口"
    assert "4h" in by_horizon and by_horizon["4h"]["matured_count"] == 1
    assert "24h" not in by_horizon, "24 小时窗口尚无成熟样本，不应产出统计"


@pytest.mark.asyncio
async def test_kpi_computes_hit_ratios_and_medians(env):
    tracker, db = env
    peaks = [1.1, 2.5, 3.0, 12.0]
    for idx, peak in enumerate(peaks):
        view = make_view(price=0.001, token_id=idx + 1, contract=f"0x{idx}")
        alert_id = await seed_alert(db, view, created_at=NOW - 5 * HOUR)
        tracker.track(alert_id=alert_id, alert_kind="S1", view=view,
                      at_ms=NOW - 5 * HOUR)
        tracker.on_observation(move(view, price=0.001 * peak),
                               NOW - 5 * HOUR + 60_000)
    await db.drain()

    reporter = KpiReporter(db=db, config=TRACKER_CONFIG, fingerprint=FINGERPRINT)
    results = {r["horizon"]: r for r in await reporter.build(NOW)}
    stats = results["1h"]

    assert stats["matured_count"] == 4
    assert stats["hit_2x_ratio"] == 0.75
    assert stats["hit_10x_ratio"] == 0.25
    # 中位数而非均值：12 倍那个样本会让均值完全失去意义
    assert stats["median_peak_multiple"] == pytest.approx(2.75)


@pytest.mark.asyncio
async def test_kpi_is_persisted_and_upserted(env):
    tracker, db = env
    view = make_view(price=0.001)
    alert_id = await seed_alert(db, view, created_at=NOW - 5 * HOUR)
    tracker.track(alert_id=alert_id, alert_kind="S1", view=view, at_ms=NOW - 5 * HOUR)
    tracker.on_observation(move(view, price=0.003), NOW - 5 * HOUR + 60_000)
    await db.drain()

    reporter = KpiReporter(db=db, config=TRACKER_CONFIG, fingerprint=FINGERPRINT)
    await reporter.build(NOW)
    await reporter.build(NOW)
    await db.drain()

    rows = await db.fetch_all("SELECT * FROM kpi_daily WHERE horizon='1h'")
    assert len(rows) == 1, "同日同口径重复生成必须覆盖而不是追加"
    assert json.loads(rows[0]["payload_json"])["hit_2x_ratio"] == 1.0


@pytest.mark.asyncio
async def test_kpi_excludes_near_miss_records(env):
    """Near-Miss 从未发出信号，计入 KPI 会让统计口径失去意义。"""
    tracker, db = env
    from radar.domain.models import ScoreResult

    view = make_view()
    await repo.upsert_token(db, view, source="test")
    await repo.insert_alert(
        db, view=view, alert_kind="S1", is_near_miss=True,
        created_at=NOW - 5 * HOUR, correlation_id="c1", snapshot_id=None,
        scores=ScoreResult(opportunity=69.0, confidence=60.0, data_quality=70.0,
                           rug_risk=25.0, distribution=10.0),
        factors_json=None, trigger_json=None, prev_scores_json=None,
        fingerprint=FINGERPRINT,
    )
    await db.drain()

    reporter = KpiReporter(db=db, config=TRACKER_CONFIG, fingerprint=FINGERPRINT)
    assert await reporter.build(NOW) == []


@pytest.mark.asyncio
async def test_kpi_summarize_reports_window_quality(env):
    """周报/看板口径：total 含未成熟样本，分组只统计已到期的。

    区分"没发"和"还没到期"是看板最重要的诚实性要求——
    把未成熟样本埋掉会让刚上线的策略版本看起来一条推送都没有。
    """
    tracker, db = env
    # 已成熟（5 小时前，冲到 3x）+ 未成熟（10 分钟前）
    matured = make_view(price=0.001, token_id=1, contract="0xm")
    alert_id = await seed_alert(db, matured, created_at=NOW - 5 * HOUR)
    tracker.track(alert_id=alert_id, alert_kind="S1", view=matured,
                  at_ms=NOW - 5 * HOUR)
    tracker.on_observation(move(matured, price=0.003), NOW - 5 * HOUR + 60_000)

    fresh = make_view(price=0.001, token_id=2, contract="0xf")
    fresh_id = await seed_alert(db, fresh, created_at=NOW - 600_000)
    tracker.track(alert_id=fresh_id, alert_kind="S1", view=fresh,
                  at_ms=NOW - 600_000)
    await db.drain()

    reporter = KpiReporter(db=db, config=TRACKER_CONFIG, fingerprint=FINGERPRINT)
    summary = await reporter.summarize(window_days=7, at_ms=NOW)

    assert summary["total_alerts"] == 2, "总数必须含未成熟样本"
    by_horizon = {g["horizon"]: g for g in summary["groups"]}
    assert by_horizon["1h"]["matured_count"] == 1
    assert by_horizon["1h"]["hit_2x_ratio"] == 1.0
    assert "24h" not in by_horizon, "24h 窗口无成熟样本不应产出分组"

    # 不写库：summarize 是纯查询，重复调用不能污染 kpi_daily
    rows = await db.fetch_all("SELECT * FROM kpi_daily")
    assert rows == []
