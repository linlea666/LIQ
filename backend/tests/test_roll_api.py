"""滚仓 REST API (api/roll_position.py) 集成测试

通过 FastAPI TestClient 走 HTTP 栈，验证：
  1. 路由注册：在未初始化 service 时返回 503
  2. 持仓 CRUD：create / list / get / delete
  3. 事件执行：add / reduce / close / move_sl / override
  4. 模板 CRUD：derive / update / delete
  5. 设置：get / put（含字段校验）
  6. 枚举：/enums
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.roll_position import router as roll_router, set_service
from processors.roll_service import RollService


@pytest.fixture
def client(tmp_path: Path):
    svc = RollService(data_dir=str(tmp_path))
    svc.bootstrap()
    set_service(svc)

    app = FastAPI()
    app.include_router(roll_router)
    return TestClient(app), svc


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 1. service 未初始化时返回 503
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_service_not_ready_returns_503():
    set_service(None)   # type: ignore[arg-type]
    app = FastAPI()
    app.include_router(roll_router)
    c = TestClient(app)
    resp = c.get("/api/roll/positions")
    assert resp.status_code == 503


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 2. 持仓相关
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestPositionsAPI:
    def _create(self, c: TestClient, **overrides):
        payload = {
            "coin": "BTC",
            "side": "long",
            "margin_mode": "isolated",
            "leverage": 10,
            "entry_price": 60000.0,
            "margin_usd": 600.0,
            "template_id": "fatzhai",
            "stop_loss": 55000.0,
        }
        payload.update(overrides)
        return c.post("/api/roll/positions", json=payload)

    def test_create_position_success(self, client):
        c, _ = client
        resp = self._create(c)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["position"]["coin"] == "BTC"
        assert body["plan"]["template_id"] == "fatzhai"
        assert body["position"]["plan_id"] == body["plan"]["id"]

    def test_create_rejects_unknown_template(self, client):
        c, _ = client
        resp = self._create(c, template_id="no-such")
        assert resp.status_code == 400

    def test_create_rejects_excessive_margin(self, client):
        c, _ = client
        resp = self._create(c, margin_usd=6000.0)   # > 50% of default 10k
        assert resp.status_code == 400

    def test_list_positions(self, client):
        c, _ = client
        self._create(c)
        self._create(c, coin="ETH", entry_price=3000.0, margin_usd=300.0)
        resp = c.get("/api/roll/positions")
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == 2

    def test_get_position_detail(self, client):
        c, _ = client
        created = self._create(c).json()
        pid = created["position"]["id"]
        resp = c.get(f"/api/roll/positions/{pid}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["position"]["id"] == pid
        assert body["latest_signal"] is None

    def test_get_position_not_found(self, client):
        c, _ = client
        resp = c.get("/api/roll/positions/pos-missing")
        assert resp.status_code == 404

    def test_delete_position(self, client):
        c, _ = client
        pid = self._create(c).json()["position"]["id"]
        resp = c.delete(f"/api/roll/positions/{pid}")
        assert resp.status_code == 200
        assert resp.json()["deleted"] == pid
        # list 应为空
        assert c.get("/api/roll/positions").json()["count"] == 0

    def test_delete_missing_returns_404(self, client):
        c, _ = client
        resp = c.delete("/api/roll/positions/pos-none")
        assert resp.status_code == 404


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 3. 事件执行
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestExecuteAPI:
    def _create(self, c: TestClient):
        resp = c.post("/api/roll/positions", json={
            "coin": "BTC", "side": "long", "margin_mode": "isolated",
            "leverage": 10, "entry_price": 60000.0, "margin_usd": 600.0,
            "template_id": "fatzhai", "stop_loss": 55000.0,
        })
        return resp.json()["position"]["id"]

    def test_execute_add(self, client):
        c, _ = client
        pid = self._create(c)
        resp = c.post(f"/api/roll/positions/{pid}/execute", json={
            "kind": "add", "price": 61000.0, "margin_delta_usd": 200.0,
        })
        assert resp.status_code == 200
        body = resp.json()
        assert body["position"]["margin_used_usd"] == pytest.approx(800.0)

    def test_execute_add_missing_margin(self, client):
        c, _ = client
        pid = self._create(c)
        resp = c.post(f"/api/roll/positions/{pid}/execute", json={
            "kind": "add", "price": 61000.0,
        })
        assert resp.status_code == 400
        assert "margin_delta_usd" in resp.json()["detail"]

    def test_execute_reduce(self, client):
        c, _ = client
        pid = self._create(c)
        resp = c.post(f"/api/roll/positions/{pid}/execute", json={
            "kind": "reduce", "price": 62000.0, "reduce_pct": 0.4,
        })
        assert resp.status_code == 200
        assert resp.json()["position"]["position_size"] == pytest.approx(0.06)

    def test_execute_reduce_bad_pct(self, client):
        c, _ = client
        pid = self._create(c)
        resp = c.post(f"/api/roll/positions/{pid}/execute", json={
            "kind": "reduce", "price": 62000.0, "reduce_pct": 1.5,
        })
        assert resp.status_code == 400

    def test_execute_close(self, client):
        c, _ = client
        pid = self._create(c)
        resp = c.post(f"/api/roll/positions/{pid}/execute", json={
            "kind": "close", "price": 62000.0, "close_kind": "close_manual",
        })
        assert resp.status_code == 200
        assert resp.json()["position"]["status"] == "closed"

    def test_execute_move_sl_requires_value(self, client):
        c, _ = client
        pid = self._create(c)
        resp = c.post(f"/api/roll/positions/{pid}/execute", json={
            "kind": "move_sl", "price": 60000.0,
        })
        assert resp.status_code == 400

    def test_execute_move_sl_ok(self, client):
        c, _ = client
        pid = self._create(c)
        resp = c.post(f"/api/roll/positions/{pid}/execute", json={
            "kind": "move_sl", "price": 60000.0, "new_sl": 57000.0,
        })
        assert resp.status_code == 200
        assert resp.json()["position"]["stop_loss"] == 57000.0

    def test_override_add(self, client):
        c, _ = client
        pid = self._create(c)
        resp = c.post(f"/api/roll/positions/{pid}/override", json={
            "price": 61000.0, "margin_delta_usd": 100.0, "reason": "gut",
        })
        assert resp.status_code == 200
        # events 最末一条是 user_override_add
        events_resp = c.get(f"/api/roll/positions/{pid}/events")
        events = events_resp.json()["events"]
        assert events[-1]["kind"] == "user_override_add"
        assert events[-1]["user_override"] is True

    def test_events_list(self, client):
        c, _ = client
        pid = self._create(c)
        c.post(f"/api/roll/positions/{pid}/execute", json={
            "kind": "add", "price": 61000.0, "margin_delta_usd": 200.0,
        })
        resp = c.get(f"/api/roll/positions/{pid}/events")
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == 2
        assert [e["kind"] for e in body["events"]] == ["init", "add"]

    def test_signal_not_yet_evaluated(self, client):
        c, _ = client
        pid = self._create(c)
        resp = c.get(f"/api/roll/positions/{pid}/signal")
        assert resp.status_code == 404


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 4. 模板
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestTemplatesAPI:
    def test_list_templates_has_builtins(self, client):
        c, _ = client
        resp = c.get("/api/roll/templates")
        assert resp.status_code == 200
        ids = {t["id"] for t in resp.json()["items"]}
        assert {"fatzhai", "li_fashi", "pyramid", "conservative"}.issubset(ids)

    def test_derive_custom_template(self, client):
        c, _ = client
        resp = c.post("/api/roll/templates", json={
            "source_id": "fatzhai",
            "new_id": "custom:my_fat",
            "new_name": "我的肥仔",
        })
        assert resp.status_code == 200
        assert resp.json()["id"] == "custom:my_fat"
        assert resp.json()["builtin"] is False

    def test_derive_rejects_bad_id(self, client):
        c, _ = client
        resp = c.post("/api/roll/templates", json={
            "source_id": "fatzhai",
            "new_id": "bad-without-prefix",
            "new_name": "x",
        })
        assert resp.status_code == 400

    def test_update_builtin_rejected(self, client):
        c, _ = client
        resp = c.put("/api/roll/templates/fatzhai", json={
            "patch": {"max_add_times": 5},
        })
        assert resp.status_code == 400

    def test_update_custom_ok(self, client):
        c, _ = client
        c.post("/api/roll/templates", json={
            "source_id": "fatzhai",
            "new_id": "custom:m1",
            "new_name": "m1",
        })
        resp = c.put("/api/roll/templates/custom:m1", json={
            "patch": {"max_add_times": 5},
        })
        assert resp.status_code == 200
        assert resp.json()["max_add_times"] == 5

    def test_delete_custom_ok(self, client):
        c, _ = client
        c.post("/api/roll/templates", json={
            "source_id": "fatzhai",
            "new_id": "custom:del",
            "new_name": "del",
        })
        resp = c.delete("/api/roll/templates/custom:del")
        assert resp.status_code == 200
        assert resp.json()["deleted"] == "custom:del"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 5. 全局设置
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestSettingsAPI:
    def test_get_defaults(self, client):
        c, _ = client
        resp = c.get("/api/roll/settings")
        assert resp.status_code == 200
        assert "total_account_usd" in resp.json()

    def test_put_updates(self, client):
        c, _ = client
        resp = c.put("/api/roll/settings", json={
            "total_account_usd": 15000.0,
            "forward_alert_cooldown_min": 45,
        })
        assert resp.status_code == 200
        body = resp.json()
        assert body["total_account_usd"] == 15000.0
        assert body["forward_alert_cooldown_min"] == 45
        assert body["updated_at"] > 0

    def test_put_rejects_bad_hour(self, client):
        c, _ = client
        resp = c.put("/api/roll/settings", json={"quiet_start_utc": 30})
        assert resp.status_code == 400

    def test_put_rejects_invalid_value(self, client):
        c, _ = client
        resp = c.put("/api/roll/settings", json={"total_account_usd": -5})
        assert resp.status_code == 400


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 6. 枚举
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_enums_endpoint(client):
    c, _ = client
    resp = c.get("/api/roll/enums")
    assert resp.status_code == 200
    body = resp.json()
    assert body["sides"] == ["long", "short"]
    assert "isolated" in body["margin_modes"] and "cross" in body["margin_modes"]
    assert "passive_deleveraging" in body["add_modes"]
    assert "min_avg_distance_pct" in body["safety_gates_defaults"]
