"""滚仓持久化层 (storage/roll_storage.py) 单元测试

覆盖要点：
  1. 路径计算：roll_dir / positions_path / events_path / settings_path
  2. RollStoreData CRUD：upsert/get/active/delete
  3. save_store / load_store 往返一致
  4. load_store 容错：文件缺失 / JSON 损坏 / 字段损坏
  5. append_event / load_events 追加顺序与 position_id 过滤
  6. rebuild_position_events：冷启动回填内嵌 events
  7. load_settings / save_settings：含 updated_at 自动更新
  8. bootstrap：创建目录 + 加载 + 回填事件
  9. 原子写入：tmp 残留不污染（写入成功后不残留 .tmp）
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from models.roll_position import (
    RollEvent,
    RollGlobalSettings,
    RollPlan,
    SafetyGates,
    UserPosition,
)
from storage.roll_storage import (
    RollStoreData,
    append_event,
    bootstrap,
    events_path,
    load_events,
    load_settings,
    load_store,
    positions_path,
    rebuild_position_events,
    roll_dir,
    save_settings,
    save_store,
    settings_path,
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Fixtures
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.fixture
def data_dir(tmp_path: Path) -> str:
    return str(tmp_path)


def _mk_position(pid: str = "pos-1", plan_id: str = "plan-1") -> UserPosition:
    now = int(time.time())
    return UserPosition(
        id=pid,
        coin="BTC",
        side="long",
        margin_mode="isolated",
        leverage=10,
        entry_price=60000.0,
        position_size=0.1,
        position_value_usd=6000.0,
        margin_used_usd=600.0,
        total_account_usd=10000.0,
        stop_loss=58000.0,
        initial_stop_loss=58000.0,
        plan_id=plan_id,
        created_at=now,
        updated_at=now,
    )


def _mk_plan(plan_id: str = "plan-1", position_id: str = "pos-1") -> RollPlan:
    return RollPlan(
        id=plan_id,
        position_id=position_id,
        template_id="fatzhai",
        add_mode="passive_deleveraging",
        target_leverage=10.0,
        gates=SafetyGates(),
        created_at=int(time.time()),
    )


def _mk_event(kind: str = "add", ts: int | None = None) -> RollEvent:
    return RollEvent(
        ts=ts or int(time.time()),
        kind=kind,   # type: ignore[arg-type]
        price=60100.0,
        margin_delta_usd=100.0,
        size_delta=0.01,
        avg_price_after=60050.0,
        leverage_after=9.5,
        liq_price_after=54000.0,
        sl_after=58000.0,
        reason="test",
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 1. 路径计算
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_path_helpers(data_dir: str):
    assert roll_dir(data_dir).name == "roll"
    assert positions_path(data_dir).name == "positions.json"
    assert events_path(data_dir).name == "events.jsonl"
    assert settings_path(data_dir).name == "settings.json"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 2. RollStoreData in-memory 行为
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestStoreDataInMemory:
    def test_upsert_and_get(self):
        store = RollStoreData()
        pos = _mk_position()
        plan = _mk_plan()
        store.upsert_position(pos)
        store.upsert_plan(plan)

        assert store.get_position("pos-1") is pos
        assert store.get_plan("plan-1") is plan
        assert store.plan_for_position("pos-1") is plan

    def test_active_positions_filters_closed(self):
        store = RollStoreData()
        active = _mk_position("pos-a")
        closed = _mk_position("pos-b")
        closed.status = "closed"
        store.upsert_position(active)
        store.upsert_position(closed)

        active_list = store.active_positions()
        assert len(active_list) == 1
        assert active_list[0].id == "pos-a"

    def test_delete_position_also_removes_plan(self):
        store = RollStoreData()
        store.upsert_position(_mk_position())
        store.upsert_plan(_mk_plan())

        store.delete_position("pos-1")
        assert store.get_position("pos-1") is None
        assert store.get_plan("plan-1") is None

    def test_delete_nonexistent_is_noop(self):
        store = RollStoreData()
        store.delete_position("not-exist")   # 不应抛

    def test_plan_for_position_missing(self):
        store = RollStoreData()
        assert store.plan_for_position("no-such") is None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 3. save_store / load_store 往返
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestStoreRoundtrip:
    def test_roundtrip_preserves_data(self, data_dir: str):
        store = RollStoreData()
        store.upsert_position(_mk_position())
        store.upsert_plan(_mk_plan())
        save_store(data_dir, store)

        loaded = load_store(data_dir)
        assert "pos-1" in loaded.positions
        assert "plan-1" in loaded.plans
        assert loaded.positions["pos-1"].entry_price == 60000.0
        assert loaded.plans["plan-1"].template_id == "fatzhai"

    def test_load_nonexistent_returns_empty(self, data_dir: str):
        store = load_store(data_dir)
        assert store.positions == {}
        assert store.plans == {}

    def test_load_corrupted_json_returns_empty(self, data_dir: str):
        path = positions_path(data_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not valid json", encoding="utf-8")
        store = load_store(data_dir)
        assert store.positions == {}
        assert store.plans == {}

    def test_load_skips_bad_position_keeps_others(self, data_dir: str):
        path = positions_path(data_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        good = _mk_position("pos-good")
        payload = {
            "version": 1,
            "updated_at": 0,
            "positions": {
                "pos-good": good.model_dump(),
                "pos-bad":  {"id": "pos-bad", "coin": "BTC"},  # 缺必填
            },
            "plans": {},
        }
        path.write_text(json.dumps(payload), encoding="utf-8")

        store = load_store(data_dir)
        assert "pos-good" in store.positions
        assert "pos-bad" not in store.positions

    def test_save_atomic_no_tmp_left(self, data_dir: str):
        store = RollStoreData()
        store.upsert_position(_mk_position())
        save_store(data_dir, store)

        tmp_path = positions_path(data_dir).with_suffix(".json.tmp")
        assert not tmp_path.exists()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 4. Events append / load / 过滤
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestEvents:
    def test_append_and_load_events(self, data_dir: str):
        e1 = _mk_event("init", ts=1000)
        e2 = _mk_event("add", ts=2000)
        append_event(data_dir, "pos-1", e1)
        append_event(data_dir, "pos-1", e2)

        loaded = load_events(data_dir)
        assert len(loaded) == 2
        assert loaded[0][0] == "pos-1" and loaded[0][1].kind == "init"
        assert loaded[1][0] == "pos-1" and loaded[1][1].kind == "add"

    def test_load_events_filter_by_position(self, data_dir: str):
        append_event(data_dir, "pos-A", _mk_event("init", ts=1))
        append_event(data_dir, "pos-B", _mk_event("init", ts=2))
        append_event(data_dir, "pos-A", _mk_event("add", ts=3))

        only_a = load_events(data_dir, position_id="pos-A")
        assert len(only_a) == 2
        assert all(pid == "pos-A" for pid, _ in only_a)

    def test_load_events_skips_corrupted_line(self, data_dir: str):
        e1 = _mk_event("init", ts=1)
        append_event(data_dir, "pos-1", e1)
        path = events_path(data_dir)
        with open(path, "a", encoding="utf-8") as f:
            f.write("{\"bad line}\n")
        append_event(data_dir, "pos-1", _mk_event("add", ts=2))

        loaded = load_events(data_dir)
        assert len(loaded) == 2  # 跳过坏行，但 OK 的行仍能加载

    def test_load_events_nonexistent(self, data_dir: str):
        assert load_events(data_dir) == []


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 5. rebuild_position_events
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestRebuild:
    def test_rebuild_fills_events(self, data_dir: str):
        store = RollStoreData()
        store.upsert_position(_mk_position())

        append_event(data_dir, "pos-1", _mk_event("init", ts=1))
        append_event(data_dir, "pos-1", _mk_event("add", ts=2))
        rebuild_position_events(data_dir, store)

        assert len(store.positions["pos-1"].events) == 2
        assert store.positions["pos-1"].events[0].kind == "init"

    def test_rebuild_ignores_events_without_position(self, data_dir: str):
        store = RollStoreData()   # empty
        append_event(data_dir, "pos-missing", _mk_event("init"))
        rebuild_position_events(data_dir, store)
        # 不抛异常即通过；store 保持空
        assert store.positions == {}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 5b. 闭仓位持久化回归 —— 平仓后 size/value/margin=0 必须能被重载
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestClosedPositionRoundtrip:
    """回归：曾因 position_size/value/margin Field(..., gt=0) 导致
    closed position（全部字段为 0）在 load_store 时被校验失败
    静默丢弃，进而破坏复盘功能。约束已放宽为 ge=0。"""

    def test_closed_position_roundtrip(self, data_dir: str):
        store = RollStoreData()
        pos = _mk_position(pid="pos-closed")
        plan = _mk_plan(plan_id="plan-closed", position_id="pos-closed")
        pos.status = "closed"
        pos.closed_at = int(time.time())
        # 模拟 execute_close 后的归零状态
        pos.position_size = 0.0
        pos.position_value_usd = 0.0
        pos.margin_used_usd = 0.0
        store.upsert_position(pos)
        store.upsert_plan(plan)
        save_store(data_dir, store)

        reloaded = load_store(data_dir)
        assert "pos-closed" in reloaded.positions, "闭仓位不得在重载时被丢弃"
        rpos = reloaded.positions["pos-closed"]
        assert rpos.status == "closed"
        assert rpos.position_size == 0.0
        assert rpos.margin_used_usd == 0.0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 6. Settings 读写
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestSettings:
    def test_load_defaults_when_missing(self, data_dir: str):
        s = load_settings(data_dir)
        assert isinstance(s, RollGlobalSettings)
        assert s.total_account_usd > 0   # 默认值

    def test_save_and_load_custom(self, data_dir: str):
        custom = RollGlobalSettings(total_account_usd=12345.0, forward_alert_cooldown_min=45)
        save_settings(data_dir, custom)
        loaded = load_settings(data_dir)
        assert loaded.total_account_usd == 12345.0
        assert loaded.forward_alert_cooldown_min == 45

    def test_save_updates_timestamp(self, data_dir: str):
        s = RollGlobalSettings(updated_at=0)
        save_settings(data_dir, s)
        loaded = load_settings(data_dir)
        assert loaded.updated_at > 0

    def test_load_corrupted_settings_returns_defaults(self, data_dir: str):
        path = settings_path(data_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not json", encoding="utf-8")
        loaded = load_settings(data_dir)
        assert isinstance(loaded, RollGlobalSettings)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 7. bootstrap 组合
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestBootstrap:
    def test_bootstrap_creates_dir(self, data_dir: str):
        store, settings = bootstrap(data_dir)
        assert roll_dir(data_dir).exists()
        assert isinstance(store, RollStoreData)
        assert isinstance(settings, RollGlobalSettings)

    def test_bootstrap_loads_existing_and_rebuilds_events(self, data_dir: str):
        init = RollStoreData()
        init.upsert_position(_mk_position())
        init.upsert_plan(_mk_plan())
        save_store(data_dir, init)

        append_event(data_dir, "pos-1", _mk_event("init", ts=1))
        append_event(data_dir, "pos-1", _mk_event("add", ts=2))

        store, settings = bootstrap(data_dir)
        assert "pos-1" in store.positions
        # events 被回填
        assert len(store.positions["pos-1"].events) == 2
