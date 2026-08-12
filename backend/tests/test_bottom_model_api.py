"""Bottom Model 服务层调度逻辑、证据包生成与 REST API 集成测试。"""
from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass, field

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from api.routes_bottom_model import router as bm_router, set_service
from processors.bottom_model.evidence_pack import build_evidence_pack
from processors.bottom_model.service import BottomModelService
from processors.bottom_model.snapshot import build_snapshot
from storage.bottom_model_store import BottomModelStore
from tests.test_bottom_model_factors import _bottomish_data


@dataclass
class _FakeBGCfg:
    enabled: bool = False


@dataclass
class _FakeYahooCfg:
    enabled: bool = False


@dataclass
class _FakeBottomCfg:
    enabled: bool = True
    data_dir: str = "data/bottom_model"
    daily_run_hour_utc: int = 1
    coinglass_spacing_sec: float = 0.0
    snapshot_retention_days: int = 800


@dataclass
class _FakeSettings:
    bottom_model: _FakeBottomCfg = field(default_factory=_FakeBottomCfg)
    bgeometrics: _FakeBGCfg = field(default_factory=_FakeBGCfg)
    yahoo_cme: _FakeYahooCfg = field(default_factory=_FakeYahooCfg)


def _service(tmp_path, seed: bool = True) -> BottomModelService:
    settings = _FakeSettings()
    settings.bottom_model.data_dir = str(tmp_path / "bm")
    svc = BottomModelService(coinglass=object(), settings=settings)
    if seed:
        for metric, rows in _bottomish_data().items():
            svc.store.upsert_series(metric, rows)
    return svc


# ── 调度判定 ──

def test_should_run_when_snapshot_missing_or_old(tmp_path):
    svc = _service(tmp_path)
    # 无快照 → 应运行（daily_run_hour_utc=1，测试时段基本恒真；显式置 0 保证确定性）
    object.__setattr__(svc._cfg, "daily_run_hour_utc", 0)
    assert svc._should_run_now() is True
    # 已有最新快照且无失败 → 不运行
    build_snapshot(svc.store, as_of_day="2099-12-31")
    assert svc._should_run_now() is False


def test_should_retry_failed_fetch_after_cooldown(tmp_path):
    svc = _service(tmp_path)
    object.__setattr__(svc._cfg, "daily_run_hour_utc", 0)
    build_snapshot(svc.store, as_of_day="2099-12-31")
    # 必须用注册表内的 spec key——账本中已停采 spec 的失败旧行不触发自愈
    active_key = svc._collector.registry[0].key
    svc.store.record_fetch(active_key, ok=False, error="http_429")
    # 刚失败：2h 冷却内不重试
    assert svc._should_run_now() is False
    # 手动把账本时间戳回拨 3h → 触发自愈重试
    with svc.store._lock, svc.store._conn:
        svc.store._conn.execute(
            "UPDATE fetch_log SET last_attempt_ts=? WHERE metric=?",
            (int(time.time()) - 3 * 3600, active_key),
        )
    assert svc._should_run_now() is True
    # 注册表外的孤儿失败行：不应触发自愈
    with svc.store._lock, svc.store._conn:
        svc.store._conn.execute("DELETE FROM fetch_log WHERE metric=?", (active_key,))
    svc.store.record_fetch("orphan_metric", ok=False, error="http_429")
    with svc.store._lock, svc.store._conn:
        svc.store._conn.execute(
            "UPDATE fetch_log SET last_attempt_ts=? WHERE metric='orphan_metric'",
            (int(time.time()) - 3 * 3600,),
        )
    assert svc._should_run_now() is False


def test_trigger_run_guard(tmp_path):
    svc = _service(tmp_path)

    class _FakeLockedLock:
        def locked(self):
            return True

    svc._run_lock = _FakeLockedLock()  # type: ignore[assignment]
    assert svc.trigger_run() == {"started": False, "reason": "run_in_progress"}


# ── 证据包 ──

def test_evidence_pack_sections(tmp_path):
    store = BottomModelStore(str(tmp_path / "bm"))
    for metric, rows in _bottomish_data().items():
        store.upsert_series(metric, rows)
    snap = build_snapshot(store)
    pack = build_evidence_pack(snap, store)
    for section in ["§0 分析指令", "§1 模型结论摘要", "§2 六因子明细",
                    "§3 确认信号与假底过滤器", "§4 反证清单", "§5 历史类比",
                    "§6 关键指标原始序列", "§7 数据窗口与局限声明"]:
        assert section in pack, f"缺少分节：{section}"
    assert "Bottom Stress" in pack
    assert "不要跨窗口直接比较分位数" in pack
    # §6 序列确实带数值
    assert "BTC 价格 (USD)" in pack
    store.close()


# ── REST API ──

@pytest.fixture
def client(tmp_path):
    svc = _service(tmp_path)
    build_snapshot(svc.store)
    set_service(svc)
    app = FastAPI()
    app.include_router(bm_router)
    return TestClient(app), svc


def test_api_service_not_ready_503():
    set_service(None)
    app = FastAPI()
    app.include_router(bm_router)
    tc = TestClient(app)
    assert tc.get("/api/bottom-model/snapshot").status_code == 503


def test_api_snapshot_history_health(client):
    tc, svc = client
    snap = tc.get("/api/bottom-model/snapshot")
    assert snap.status_code == 200
    body = snap.json()
    assert body["stress"]["score"] > 0 and len(body["factors"]) == 6

    history = tc.get("/api/bottom-model/history?limit=10")
    assert history.status_code == 200
    assert len(history.json()["items"]) >= 1

    health = tc.get("/api/bottom-model/health")
    assert health.status_code == 200
    assert health.json()["latest_day"] == body["day"]

    pack = tc.get("/api/bottom-model/evidence-pack")
    assert pack.status_code == 200
    assert "§2 六因子明细" in pack.text


def test_api_disabled_503(tmp_path):
    svc = _service(tmp_path, seed=False)
    object.__setattr__(svc._cfg, "enabled", False)
    set_service(svc)
    app = FastAPI()
    app.include_router(bm_router)
    tc = TestClient(app)
    assert tc.get("/api/bottom-model/snapshot").status_code == 503
    # health 允许 disabled 状态下访问
    health = tc.get("/api/bottom-model/health")
    assert health.status_code == 200
    assert health.json()["enabled"] is False
