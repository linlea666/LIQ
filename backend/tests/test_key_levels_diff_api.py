"""
Key Level V3 · M3 R10 — Diff API + Lifecycle API 集成测试

通过 FastAPI TestClient 验证：
1. 路由注册：未初始化 engine → 503
2. /api/key-levels/diff/{coin}：参数校验、added/removed/strengthened/weakened/tier_changed/flipped
3. /api/key-levels/lifecycle/{coin}/{level_id}：事件流合并 + 去重 + 排序
"""
from __future__ import annotations

from typing import Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routes import router as api_router, set_engine
from models.key_level import KeyLevelSnapshotV2, KeyLevelV2, LifecycleEvent


# ─────────────────────────────────────────────────────────────────
# Mock Engine
# ─────────────────────────────────────────────────────────────────

class _MockEngine:
    """最小 engine：只暴露 get_kl_history。"""
    def __init__(self):
        self._history: dict[str, list[KeyLevelSnapshotV2]] = {}

    def set_history(self, coin: str, snapshots: list[KeyLevelSnapshotV2]):
        self._history[coin.upper()] = snapshots

    def get_kl_history(self, coin: str) -> list[KeyLevelSnapshotV2]:
        return list(self._history.get(coin.upper(), []))


@pytest.fixture
def client():
    eng = _MockEngine()
    set_engine(eng)
    app = FastAPI()
    app.include_router(api_router)
    yield TestClient(app), eng
    set_engine(None)  # 清理 module 级状态


# ─────────────────────────────────────────────────────────────────
# Helper
# ─────────────────────────────────────────────────────────────────

def _make_snapshot(
    ts: int,
    levels: list[KeyLevelV2],
) -> KeyLevelSnapshotV2:
    return KeyLevelSnapshotV2(
        ts=ts,
        current_price=100.0,
        atr=2.0,
        levels=levels,
    )


def _make_level(
    level_id: str,
    price: float = 100,
    side: str = "support",
    final_score: float = 50,
    strength_tier: str = "B",
    state: str = "idle",
    lifecycle_events: Optional[list[LifecycleEvent]] = None,
) -> KeyLevelV2:
    return KeyLevelV2(
        price=price,
        side=side,
        final_score=final_score,
        strength_tier=strength_tier,
        state=state,
        level_id=level_id,
        lifecycle_events=lifecycle_events or [],
    )


# ─────────────────────────────────────────────────────────────────
# 1. 503 when engine not set
# ─────────────────────────────────────────────────────────────────

def test_diff_returns_503_when_engine_not_ready():
    set_engine(None)
    app = FastAPI()
    app.include_router(api_router)
    c = TestClient(app)
    r = c.get("/api/key-levels/diff/BTC?from_ts=100&to_ts=200")
    assert r.status_code == 503


def test_lifecycle_returns_503_when_engine_not_ready():
    set_engine(None)
    app = FastAPI()
    app.include_router(api_router)
    c = TestClient(app)
    r = c.get("/api/key-levels/lifecycle/BTC/abc123def456")
    assert r.status_code == 503


# ─────────────────────────────────────────────────────────────────
# 2. /diff endpoint
# ─────────────────────────────────────────────────────────────────

class TestDiffEndpoint:
    def test_400_when_from_ge_to(self, client):
        c, _ = client
        r = c.get("/api/key-levels/diff/BTC?from_ts=200&to_ts=100")
        assert r.status_code == 400

    def test_404_when_snapshot_missing(self, client):
        c, eng = client
        eng.set_history("BTC", [_make_snapshot(100, [])])
        r = c.get("/api/key-levels/diff/BTC?from_ts=100&to_ts=200")
        assert r.status_code == 404

    def test_added_level(self, client):
        """新出现 level → added。"""
        c, eng = client
        snap_from = _make_snapshot(100, [_make_level("aaa", price=100)])
        snap_to = _make_snapshot(200, [
            _make_level("aaa", price=100),
            _make_level("bbb", price=110),
        ])
        eng.set_history("BTC", [snap_from, snap_to])

        r = c.get("/api/key-levels/diff/BTC?from_ts=100&to_ts=200")
        assert r.status_code == 200
        data = r.json()
        assert len(data["added"]) == 1
        assert data["added"][0]["level_id"] == "bbb"
        assert data["summary"]["added"] == 1

    def test_removed_level(self, client):
        """消失 level → removed。"""
        c, eng = client
        snap_from = _make_snapshot(100, [
            _make_level("aaa", price=100),
            _make_level("bbb", price=110),
        ])
        snap_to = _make_snapshot(200, [_make_level("aaa", price=100)])
        eng.set_history("BTC", [snap_from, snap_to])

        r = c.get("/api/key-levels/diff/BTC?from_ts=100&to_ts=200")
        data = r.json()
        assert len(data["removed"]) == 1
        assert data["removed"][0]["level_id"] == "bbb"

    def test_strengthened(self, client):
        """final_score +≥5 → strengthened。"""
        c, eng = client
        snap_from = _make_snapshot(100, [_make_level("aaa", final_score=50)])
        snap_to = _make_snapshot(200, [_make_level("aaa", final_score=58)])
        eng.set_history("BTC", [snap_from, snap_to])

        r = c.get("/api/key-levels/diff/BTC?from_ts=100&to_ts=200")
        data = r.json()
        assert len(data["strengthened"]) == 1
        assert data["strengthened"][0]["delta"] == 8.0

    def test_weakened(self, client):
        c, eng = client
        snap_from = _make_snapshot(100, [_make_level("aaa", final_score=70)])
        snap_to = _make_snapshot(200, [_make_level("aaa", final_score=60)])
        eng.set_history("BTC", [snap_from, snap_to])

        r = c.get("/api/key-levels/diff/BTC?from_ts=100&to_ts=200")
        data = r.json()
        assert len(data["weakened"]) == 1
        assert data["weakened"][0]["delta"] == -10.0

    def test_no_event_for_small_score_change(self, client):
        """变化 < 5 分 → 既不算 strengthened 也不算 weakened。"""
        c, eng = client
        snap_from = _make_snapshot(100, [_make_level("aaa", final_score=50)])
        snap_to = _make_snapshot(200, [_make_level("aaa", final_score=53)])
        eng.set_history("BTC", [snap_from, snap_to])

        r = c.get("/api/key-levels/diff/BTC?from_ts=100&to_ts=200")
        data = r.json()
        assert data["strengthened"] == []
        assert data["weakened"] == []

    def test_tier_upgraded(self, client):
        c, eng = client
        snap_from = _make_snapshot(100, [_make_level("aaa", strength_tier="B", final_score=60)])
        snap_to = _make_snapshot(200, [_make_level("aaa", strength_tier="A", final_score=70)])
        eng.set_history("BTC", [snap_from, snap_to])

        r = c.get("/api/key-levels/diff/BTC?from_ts=100&to_ts=200")
        data = r.json()
        assert len(data["tier_changed"]) == 1
        assert data["tier_changed"][0]["direction"] == "upgraded"
        assert data["tier_changed"][0]["from"] == "B"
        assert data["tier_changed"][0]["to"] == "A"

    def test_tier_downgraded(self, client):
        c, eng = client
        snap_from = _make_snapshot(100, [_make_level("aaa", strength_tier="S", final_score=80)])
        snap_to = _make_snapshot(200, [_make_level("aaa", strength_tier="A", final_score=65)])
        eng.set_history("BTC", [snap_from, snap_to])

        r = c.get("/api/key-levels/diff/BTC?from_ts=100&to_ts=200")
        data = r.json()
        assert data["tier_changed"][0]["direction"] == "downgraded"

    def test_flipped(self, client):
        c, eng = client
        snap_from = _make_snapshot(100, [_make_level("aaa", side="support")])
        snap_to = _make_snapshot(200, [_make_level("aaa", side="resistance")])
        eng.set_history("BTC", [snap_from, snap_to])

        r = c.get("/api/key-levels/diff/BTC?from_ts=100&to_ts=200")
        data = r.json()
        assert len(data["flipped"]) == 1
        assert data["flipped"][0]["prev"]["side"] == "support"
        assert data["flipped"][0]["curr"]["side"] == "resistance"

    def test_summary_counts(self, client):
        c, eng = client
        snap_from = _make_snapshot(100, [
            _make_level("a", final_score=50),
            _make_level("b", final_score=70),
        ])
        snap_to = _make_snapshot(200, [
            _make_level("a", final_score=58),  # strengthened
            _make_level("c"),                    # added
        ])
        eng.set_history("BTC", [snap_from, snap_to])

        r = c.get("/api/key-levels/diff/BTC?from_ts=100&to_ts=200")
        data = r.json()
        s = data["summary"]
        assert s["added"] == 1
        assert s["removed"] == 1
        assert s["strengthened"] == 1

    def test_legacy_snapshot_without_level_id(self, client):
        """V3-P1-5：缺 level_id 的旧快照不参与 diff（严格按 ID 配对）。

        旧的 1.0 时代 snapshot 的 lv.level_id 为空字符串，验证此时返回
        from_count/to_count=0，所有列表为空（不会被 price 近似匹配误判）。
        """
        c, eng = client
        # 故意构造无 level_id 的 level（KeyLevelV2.level_id 默认 ""）
        from models.key_level import KeyLevelV2 as _Lv
        legacy_lv_a = _Lv(price=100, side="support", final_score=70)
        legacy_lv_b = _Lv(price=110, side="resistance", final_score=60)
        snap_from = _make_snapshot(100, [legacy_lv_a, legacy_lv_b])
        snap_to = _make_snapshot(200, [legacy_lv_a, legacy_lv_b])
        eng.set_history("BTC", [snap_from, snap_to])

        r = c.get("/api/key-levels/diff/BTC?from_ts=100&to_ts=200")
        assert r.status_code == 200
        data = r.json()
        # 缺 level_id 的 levels 全部被过滤
        assert data["from_count"] == 0
        assert data["to_count"] == 0
        assert data["added"] == []
        assert data["removed"] == []
        assert data["strengthened"] == []
        assert data["weakened"] == []


# ─────────────────────────────────────────────────────────────────
# 3. /lifecycle endpoint
# ─────────────────────────────────────────────────────────────────

class TestLifecycleEndpoint:
    def test_404_when_no_history(self, client):
        c, eng = client
        # 该 coin 完全没有 history
        r = c.get("/api/key-levels/lifecycle/BTC/aaa")
        assert r.status_code == 404

    def test_404_when_level_id_not_in_any_snapshot(self, client):
        c, eng = client
        snap = _make_snapshot(100, [_make_level("xxx")])
        eng.set_history("BTC", [snap])
        r = c.get("/api/key-levels/lifecycle/BTC/aaa")
        assert r.status_code == 404

    def test_returns_events_from_single_snapshot(self, client):
        c, eng = client
        evts = [
            LifecycleEvent(ts=99, event_type="born"),
            LifecycleEvent(ts=100, event_type="strengthening"),
        ]
        snap = _make_snapshot(100, [_make_level("aaa", lifecycle_events=evts)])
        eng.set_history("BTC", [snap])

        r = c.get("/api/key-levels/lifecycle/BTC/aaa")
        assert r.status_code == 200
        data = r.json()
        assert data["level_id"] == "aaa"
        assert data["first_seen_ts"] == 100
        assert data["last_seen_ts"] == 100
        assert data["snapshot_count"] == 1
        assert len(data["events"]) == 2
        assert data["events"][0]["event_type"] == "born"

    def test_merges_events_across_snapshots(self, client):
        """多个 snapshot 中事件合并 + 时间排序 + 去重。"""
        c, eng = client
        snap1 = _make_snapshot(100, [_make_level(
            "aaa",
            lifecycle_events=[LifecycleEvent(ts=99, event_type="born")],
        )])
        snap2 = _make_snapshot(200, [_make_level(
            "aaa",
            lifecycle_events=[
                LifecycleEvent(ts=99, event_type="born"),  # 重复
                LifecycleEvent(ts=150, event_type="strengthening"),
            ],
        )])
        snap3 = _make_snapshot(300, [_make_level(
            "aaa",
            lifecycle_events=[
                LifecycleEvent(ts=99, event_type="born"),
                LifecycleEvent(ts=150, event_type="strengthening"),
                LifecycleEvent(ts=250, event_type="tested"),
            ],
        )])
        eng.set_history("BTC", [snap1, snap2, snap3])

        r = c.get("/api/key-levels/lifecycle/BTC/aaa")
        data = r.json()
        assert data["snapshot_count"] == 3
        assert data["first_seen_ts"] == 100
        assert data["last_seen_ts"] == 300
        # 3 个不同事件（去重后）
        assert len(data["events"]) == 3
        # 升序
        ts_list = [e["ts"] for e in data["events"]]
        assert ts_list == sorted(ts_list)

    def test_dedup_same_ts_and_event_type(self, client):
        """同 ts + 同 event_type + 同 layer → 视为同事件去重。"""
        c, eng = client
        snap1 = _make_snapshot(100, [_make_level(
            "aaa",
            lifecycle_events=[
                LifecycleEvent(ts=99, event_type="born", layer="scoring"),
                LifecycleEvent(ts=99, event_type="born", layer="scoring"),  # 同 ts/类型/层
            ],
        )])
        eng.set_history("BTC", [snap1])
        r = c.get("/api/key-levels/lifecycle/BTC/aaa")
        data = r.json()
        assert len(data["events"]) == 1

    def test_no_dedup_when_layer_differs(self, client):
        """V3-P1-6：同 ts + 同 event_type 但不同 layer → 保留两条。

        典型场景：同一秒内 confluence_scoring 检测到 flipped（评分翻转），
        tracker_v2 状态机也检测到 flipped（state 转移），二者反映不同维度，
        都应在 lifecycle API 中可见。
        """
        c, eng = client
        snap1 = _make_snapshot(100, [_make_level(
            "aaa",
            lifecycle_events=[
                LifecycleEvent(ts=99, event_type="flipped", layer="scoring"),
                LifecycleEvent(ts=99, event_type="flipped", layer="tracker"),
            ],
        )])
        eng.set_history("BTC", [snap1])
        r = c.get("/api/key-levels/lifecycle/BTC/aaa")
        data = r.json()
        assert len(data["events"]) == 2
        layers = {e["layer"] for e in data["events"]}
        assert layers == {"scoring", "tracker"}
