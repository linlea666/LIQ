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
from processors.bottom_model import service as service_module
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


def test_should_rebuild_same_day_snapshot_after_model_or_policy_upgrade(tmp_path):
    svc = _service(tmp_path)
    object.__setattr__(svc._cfg, "daily_run_hour_utc", 0)
    svc.store.save_snapshot("2099-12-31", {
        "day": "2099-12-31", "algorithm_version": "bottom-v3",
    })
    assert svc._should_run_now() is True
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


def test_old_snapshot_does_not_bypass_failed_fetch_cooldown(tmp_path):
    """保留旧快照后，失败源仍只能按 2h 冷却重试，不能被每分钟调度打穿。"""
    svc = _service(tmp_path)
    object.__setattr__(svc._cfg, "daily_run_hour_utc", 0)
    svc.store.save_snapshot("2000-01-01", {
        "day": "2000-01-01", "algorithm_version": "bottom-v4",
        "model_id": "bottom-v4", "data_policy_id": "pit-final-v2",
    })
    active_key = svc._collector.registry[0].key
    svc.store.record_fetch(active_key, ok=False, error="http_429")
    assert svc._should_run_now() is False
    with svc.store._lock, svc.store._conn:
        svc.store._conn.execute(
            "UPDATE fetch_log SET last_attempt_ts=? WHERE metric=?",
            (int(time.time()) - 3 * 3600, active_key),
        )
    assert svc._should_run_now() is True


def test_trigger_run_guard(tmp_path):
    svc = _service(tmp_path)

    class _FakeLockedLock:
        def locked(self):
            return True

    svc._run_lock = _FakeLockedLock()  # type: ignore[assignment]
    assert svc.trigger_run() == {"started": False, "reason": "run_in_progress"}


@pytest.mark.asyncio
async def test_invalid_candidate_does_not_overwrite_last_valid_snapshot(tmp_path, monkeypatch):
    svc = _service(tmp_path)
    svc.store.save_snapshot("2026-08-10", {
        "day": "2026-08-10", "quality_status": "OK", "marker": "last-valid",
    })

    async def collect(*, force=False):
        return {"fetched": 1, "skipped_fresh": 0, "failed": 0, "elapsed_sec": 0.1}

    monkeypatch.setattr(svc._collector, "run_once", collect)
    monkeypatch.setattr(service_module, "build_snapshot", lambda store, persist=False: {
        "day": "2026-08-11", "quality_status": "INVALID_DATA",
        "blocking_reasons": ["STALE_MODEL_INPUTS"],
        "stress": {"score": 60.0}, "confirmation": {"score": 40.0},
        "quadrant": {"key": "basing"},
    })
    await svc.run_once()
    assert svc.store.latest_snapshot()["marker"] == "last-valid"
    assert svc.health()["last_run_summary"]["snapshot_persisted"] is False
    assert svc.health()["last_run_summary"]["quality_status"] == "INVALID_DATA"


# ── 证据包 ──

def test_evidence_pack_sections(tmp_path):
    store = BottomModelStore(str(tmp_path / "bm"))
    for metric, rows in _bottomish_data().items():
        store.upsert_series(metric, rows)
    snap = build_snapshot(store)
    pack = build_evidence_pack(snap, store)
    for section in ["§0 分析指令", "§1 模型结论摘要", "§2 六因子明细",
                    "§3 确认信号与假底过滤器", "§4 反证清单", "§5 历史类比",
                    "§6 关键指标原始序列", "§7 数据窗口与局限声明",
                    "§8 相关性与重复计分声明", "§9 滚动上下文"]:
        assert section in pack, f"缺少分节：{section}"
    assert "Bottom Stress" in pack
    # 审计员指令的核心禁止项必须在场
    assert "禁止跨窗口比较分位数" in pack
    assert "禁止重算或\"修正\"模型分数" in pack
    assert "整体证据质量 EQ" in pack
    # §2 表头带证据质量列；§5 类比只描述数据可比性
    assert "| 子信号 | 当前值 | 混合分位 | 得分 | 证据质量 | 备注 |" in pack
    assert "| 历史底部 | 相似度 | 共同因子 | 数据可比性 | 备注 |" in pack
    # §6 序列确实带数值
    assert "BTC 价格 (USD)" in pack
    # v4：§0 指令必须给出历史频率的正确用法、允许弃权，且输出结构收敛为 8 项
    assert "条件分布的观测值，不是预测概率" in pack
    assert "允许弃权" in pack
    assert "8. 模型审计与最终裁决" in pack
    assert "9. " not in pack.split("**输出结构**")[1].split("\n\n")[0]
    for forbidden in ("独立检验", "模型可信", "不是反例"):
        assert forbidden not in pack
    store.close()


def test_evidence_pack_is_reproducible_from_frozen_snapshot(tmp_path):
    store = BottomModelStore(str(tmp_path / "bm"))
    for metric, rows in _bottomish_data().items():
        store.upsert_series(metric, rows)
    snap = build_snapshot(store)
    before = build_evidence_pack(snap, store)
    store.upsert_series("btc_price_onchain", [("2099-01-01", 9e99)])
    assert build_evidence_pack(snap, store) == before
    store.close()


def test_legacy_evidence_pack_never_reads_live_series(tmp_path, monkeypatch):
    store = BottomModelStore(str(tmp_path / "bm"))
    for metric, rows in _bottomish_data().items():
        store.upsert_series(metric, rows)
    snap = build_snapshot(store)
    legacy = dict(snap)
    legacy.pop("frozen_series", None)
    monkeypatch.setattr(
        store, "series",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("live read forbidden")),
    )
    pack = build_evidence_pack(legacy, store, detail="full")
    assert "legacy 快照不含 frozen_series" in pack
    assert "不回读实时数据库" in pack
    store.close()


def test_evidence_pack_compact_and_full_share_frozen_lineage(tmp_path):
    store = BottomModelStore(str(tmp_path / "bm"))
    for metric, rows in _bottomish_data().items():
        store.upsert_series(metric, rows)
    snap = build_snapshot(store)
    full = build_evidence_pack(snap, store, detail="full")
    compact = build_evidence_pack(snap, store, detail="compact")
    assert len(compact) < len(full)
    for value in (
        snap["schema_version"], snap["model_id"], snap["data_policy_id"],
        snap["dataset_id"], snap["frozen_series_id"],
    ):
        assert value in full and value in compact
    # compact 的每个原始序列最多保留最近 7 个冻结点。
    assert "detail=compact" in compact
    store.upsert_series("btc_price_onchain", [("2099-01-01", 9e99)])
    assert build_evidence_pack(snap, store, detail="compact") == compact
    with pytest.raises(ValueError):
        build_evidence_pack(snap, store, detail="invalid")
    store.close()


def _fake_window(weeks: int, reliable: bool) -> dict:
    return {
        "weeks": weeks, "points": 40 if reliable else 2,
        "independent": 12 if reliable else 1, "segments": 9 if reliable else 1,
        "hit_rate": 62.5 if reliable else None,
        "median_return": 33.4 if reliable else None,
        "worst_return": -41.2, "reliable": reliable,
    }


def _fake_base_rate() -> dict:
    windows = [_fake_window(13, True), _fake_window(26, True), _fake_window(52, False)]
    return {
        "algorithm_version": "bottom-v4", "hit_threshold_pct": 30.0,
        "forward_weeks": [13, 26, 52], "min_independent": 5,
        "replay": {"points": 600, "first_day": "2013-12-10",
                   "last_day": "2026-08-11", "step_days": 7},
        "baseline": {"label": "全样本基准", "description": "对照组",
                     "points": 600, "windows": windows, "reliable": True},
        "conditions": [{"label": "当前象限 · 筑底改善", "description": "同象限时点",
                        "points": 42, "windows": windows, "reliable": True}],
        "stress_ladder": [{"threshold": 55.0, "points": 300, "windows": windows}],
        "confirmation_ladder": [{"threshold": 35.0, "points": 400, "windows": windows}],
        "caveats": ["终点收益口径，窗口未走完的时点已排除"],
    }


def test_evidence_pack_base_rate_section(tmp_path):
    """§10 只在快照带 base_rate 时出现，且不得改动 §0-§9 的编号。"""
    store = BottomModelStore(str(tmp_path / "bm"))
    for metric, rows in _bottomish_data().items():
        store.upsert_series(metric, rows)
    snap = build_snapshot(store)

    # v2/v3 历史快照没有 base_rate：章节仍在（§0 指令引用了它），但内容改为说明
    without = build_evidence_pack({**snap, "base_rate": None}, store)
    assert "## §10 历史频率层" in without
    assert "本次快照不含历史频率层" in without
    assert "| 条件 | 窗口 | 时点数 |" not in without

    # 真实回放产出的结构也要能渲染（合成数据够 100 个周级时点）
    assert snap["base_rate"]["algorithm_version"] == snap["algorithm_version"]
    assert "§10 历史频率层" in build_evidence_pack(snap, store)

    snap["base_rate"] = _fake_base_rate()
    pack = build_evidence_pack(snap, store)
    assert "§10 历史频率层" in pack
    assert "§9 滚动上下文" in pack and "§8 相关性与重复计分声明" in pack
    assert "全样本基准" in pack and "当前象限 · 筑底改善" in pack
    # 不可靠窗口不得渲染出百分比
    assert "样本不足（不给频率）" in pack
    assert "62.5%" in pack
    # §1 摘要处的交叉引用，避免读者漏掉末尾章节
    assert "历史频率对照" in pack and "完整分档与口径见 §10" in pack
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


def test_api_snapshot_history_health(client, monkeypatch):
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

    bundled = tc.get("/api/bottom-model/audit/latest")
    assert bundled.status_code == 200
    assert bundled.json()["status"] == "INSUFFICIENT_EVIDENCE"
    assert bundled.json()["audit_engine_version"] == "audit-v5"
    assert bundled.json()["markdown"].count("\n## ") == 20
    svc.store.save_audit("audit-test", {
        "audit_id": "audit-test", "model_id": "bottom-v4",
        "data_policy_id": "pit-final-v1", "dataset_id": "data-test",
        "status": "INSUFFICIENT_EVIDENCE",
    }, "# report")
    latest = tc.get("/api/bottom-model/audit/latest")
    assert latest.status_code == 200 and latest.json()["audit_id"] == "audit-test"
    assert tc.get("/api/bottom-model/audit/audit-test").json()["markdown"] == "# report"

    compact = tc.get("/api/bottom-model/evidence-pack?detail=compact")
    assert compact.status_code == 200 and "detail=compact" in compact.text
    assert tc.get("/api/bottom-model/evidence-pack?detail=invalid").status_code == 422

    monkeypatch.setattr(svc, "trigger_run", lambda force=False: {
        "started": True, "force": force,
    })
    triggered = tc.post("/api/bottom-model/run?force=true")
    assert triggered.status_code == 200
    assert triggered.json() == {"started": True, "force": True}


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
