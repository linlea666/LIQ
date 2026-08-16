"""运行时配置（配置页后端）测试。

四件容易做错且后果严重的事：
  1. 注册表必须与 config.yaml 完全对齐——暴露一个代码不读的参数
     等于在界面上放一个假开关；
  2. 鉴权必须默认拒绝——未配置令牌时管理接口整体禁用，
     而不是敞开；
  3. 非法值与自相矛盾的组合必须在写盘前被拦下——半套配置落盘
     再重启，服务会带着用户没见过的参数组合运行；
  4. 每次修改必须留下审计记录与新指纹——否则历史警报无法追溯
     "当时是哪套参数"。
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from radar import api, config_schema  # noqa: E402
from radar import settings as settings_mod  # noqa: E402
from radar.obs.events import bus  # noqa: E402
from radar.storage import repo  # noqa: E402
from radar.storage.db import Database  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = REPO_ROOT / "config.yaml"

TOKEN = "test-admin-token"
HEADERS = {"X-Radar-Admin-Token": TOKEN}


def load_defaults() -> dict[str, Any]:
    return yaml.safe_load(DEFAULT_CONFIG.read_bytes())


# ═════════════════════════════════════════════════════════════════════════
# 注册表本身
# ═════════════════════════════════════════════════════════════════════════

def test_every_registered_param_exists_in_default_config():
    """注册表路径必须逐一对应 config.yaml 里真实存在的键。

    路径打错（或 yaml 改名后注册表没跟上）时，配置页会显示
    default=None 的幽灵参数，用户改了也只是在覆盖一个不存在的键。
    """
    defaults = load_defaults()
    missing = [p.path for p in config_schema.PARAMS
               if config_schema.get_path(defaults, p.path, None) is None]
    assert missing == []


def test_default_config_passes_all_validation():
    defaults = load_defaults()
    for param in config_schema.PARAMS:
        value = config_schema.get_path(defaults, param.path)
        _, err = config_schema.validate_value(param, value)
        assert err is None, f"{param.path}: {err}"
    assert config_schema.cross_validate(defaults) == []


def test_check_overrides_rejects_unknown_path():
    _, errors = config_schema.check_overrides(
        {"scheduler": {"no_such_key": 1}}
    )
    assert "scheduler.no_such_key" in errors


def test_check_overrides_rejects_out_of_range():
    _, errors = config_schema.check_overrides(
        {"scheduler": {"target_rpm": 99999}}
    )
    assert "scheduler.target_rpm" in errors


def test_check_overrides_keeps_valid_and_drops_invalid():
    clean, errors = config_schema.check_overrides({
        "scheduler": {"target_rpm": 60},
        "email": {"max_per_hour": -5},
    })
    assert config_schema.get_path(clean, "scheduler.target_rpm") == 60
    assert "email.max_per_hour" in errors
    assert config_schema.get_path(clean, "email.max_per_hour", "absent") == "absent"


def test_deep_merge_patches_chain_list_by_id():
    """chains 是带 id 的字典列表；覆盖层用 {id: {字段}} 局部合并，
    其余链保持原样——整表替换会把另一条链的 name 等字段吃掉。"""
    defaults = load_defaults()
    merged = config_schema.deep_merge(
        defaults, {"chains": {"CT_501": {"enabled": False}}}
    )
    by_id = {c["id"]: c for c in merged["chains"]}
    assert by_id["CT_501"]["enabled"] is False
    assert by_id["CT_501"]["name"] == "Solana"
    assert by_id["56"]["enabled"] is True
    # 原对象不被改动
    assert {c["id"]: c for c in defaults["chains"]}["CT_501"]["enabled"] is True


def test_effective_hash_changes_with_any_override():
    defaults = load_defaults()
    base = config_schema.effective_hash(defaults)
    merged = config_schema.deep_merge(defaults, {"scheduler": {"target_rpm": 60}})
    assert config_schema.effective_hash(merged) != base


def test_cross_validate_catches_broken_hysteresis():
    defaults = load_defaults()
    merged = config_schema.deep_merge(defaults, {
        "state_machine": {"transitions": {"s1": {"enter_opportunity": 60.0}}}
    })
    # 默认 s1 退出阈值 62：进入 60 <= 退出 62，滞回失效
    assert any("S1" in e for e in config_schema.cross_validate(merged))


def test_top10_by_age_validator():
    param = config_schema.PARAMS_BY_PATH[
        "risk.research_gate.top10_max_pct_by_age"
    ]
    ok = [{"max_age_min": 30, "threshold": 65.0},
          {"max_age_min": None, "threshold": 45.0}]
    value, err = config_schema.validate_value(param, ok)
    assert err is None and value[-1]["max_age_min"] is None

    _, err = config_schema.validate_value(param, [
        {"max_age_min": 30, "threshold": 65.0},
    ])
    assert err is not None, "缺少 null 兜底档必须拒绝"

    _, err = config_schema.validate_value(param, [
        {"max_age_min": None, "threshold": 45.0},
        {"max_age_min": 30, "threshold": 65.0},
    ])
    assert err is not None, "null 兜底档不在末尾必须拒绝"


# ═════════════════════════════════════════════════════════════════════════
# settings 加载合并
# ═════════════════════════════════════════════════════════════════════════

@pytest.fixture
def fresh_settings():
    settings_mod.reset_settings_for_tests()
    yield
    settings_mod.reset_settings_for_tests()


def write_tmp_config(tmp_path: Path) -> Path:
    """把真实 config.yaml 复制到临时目录，data_dir 指向临时数据目录。"""
    raw = load_defaults()
    raw["service"]["data_dir"] = str(tmp_path / "data")
    (tmp_path / "data").mkdir(exist_ok=True)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(raw, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return config_path


def test_load_settings_without_overrides_uses_defaults(tmp_path, fresh_settings):
    config_path = write_tmp_config(tmp_path)
    settings = settings_mod.load_settings(config_file=config_path)
    assert settings.raw["scheduler"]["target_rpm"] == 72


def test_load_settings_merges_overrides_and_changes_hash(
    tmp_path, fresh_settings
):
    config_path = write_tmp_config(tmp_path)
    settings_before = settings_mod.load_settings(config_file=config_path)
    hash_before = settings_before.config_hash

    (tmp_path / "data" / settings_mod.OVERRIDES_FILENAME).write_text(
        yaml.safe_dump({"scheduler": {"target_rpm": 60}}), encoding="utf-8"
    )
    settings_mod.reset_settings_for_tests()
    settings_after = settings_mod.load_settings(config_file=config_path)

    assert settings_after.raw["scheduler"]["target_rpm"] == 60
    assert settings_after.config_hash != hash_before


def test_load_settings_drops_invalid_override_entries(tmp_path, fresh_settings):
    config_path = write_tmp_config(tmp_path)
    (tmp_path / "data" / settings_mod.OVERRIDES_FILENAME).write_text(
        yaml.safe_dump({
            "scheduler": {"target_rpm": 99999, "global_rpm": 100},
        }),
        encoding="utf-8",
    )
    settings = settings_mod.load_settings(config_file=config_path)
    # 越界条目被丢弃，合法条目保留
    assert settings.raw["scheduler"]["target_rpm"] == 72
    assert settings.raw["scheduler"]["global_rpm"] == 100


def test_load_settings_discards_all_overrides_on_cross_conflict(
    tmp_path, fresh_settings
):
    """单条各自合法但组合矛盾时整个覆盖层弃用：
    带着自相矛盾的滞回参数运行，状态机会抖动出成片假信号。"""
    config_path = write_tmp_config(tmp_path)
    (tmp_path / "data" / settings_mod.OVERRIDES_FILENAME).write_text(
        yaml.safe_dump({
            "scheduler": {"target_rpm": 60},
            "state_machine": {"transitions": {"s1": {"enter_opportunity": 50.0}}},
        }),
        encoding="utf-8",
    )
    settings = settings_mod.load_settings(config_file=config_path)
    assert settings.raw["scheduler"]["target_rpm"] == 72, "回退出厂默认"


def test_load_settings_survives_corrupt_overrides_file(tmp_path, fresh_settings):
    config_path = write_tmp_config(tmp_path)
    (tmp_path / "data" / settings_mod.OVERRIDES_FILENAME).write_text(
        ": 这不是合法的 yaml :\n  - [", encoding="utf-8"
    )
    settings = settings_mod.load_settings(config_file=config_path)
    assert settings.raw["scheduler"]["target_rpm"] == 72


# ═════════════════════════════════════════════════════════════════════════
# 管理 API
# ═════════════════════════════════════════════════════════════════════════

class AdminFakeSettings:
    """管理路由真正读到的配置表面。"""

    def __init__(self, service_root: Path, data_dir: Path) -> None:
        self.service_root = service_root
        self.data_dir = data_dir
        defaults = yaml.safe_load(
            (service_root / "config.yaml").read_bytes()
        )
        self.config_hash = config_schema.effective_hash(defaults)

    def fingerprint(self) -> dict[str, str]:
        return {
            "strategy_version": "v1.0.0",
            "feature_version": "f1.0.0",
            "parser_version": "p1.0.0",
            "config_hash": self.config_hash,
            "code_commit": "test",
        }


class AdminFakeService:
    def __init__(self, db: Database, service_root: Path, data_dir: Path) -> None:
        self.db = db
        self.settings = AdminFakeSettings(service_root, data_dir)
        self.restart_requested = False

    def request_restart(self) -> None:
        self.restart_requested = True


@pytest.fixture
async def admin_client(tmp_path, monkeypatch):
    monkeypatch.setenv(api.ADMIN_TOKEN_ENV, TOKEN)
    shutil.copy(DEFAULT_CONFIG, tmp_path / "config.yaml")
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    db = Database(tmp_path / "radar.db")
    await db.start()
    bus.set_sink(repo.make_event_sink(db))

    service = AdminFakeService(db, tmp_path, data_dir)
    api.bind_service(service)

    from fastapi import FastAPI

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


@pytest.mark.asyncio
async def test_admin_requires_token(admin_client):
    http, _, _ = admin_client
    assert (await http.get("/api/radar/admin/config")).status_code == 401
    response = await http.get(
        "/api/radar/admin/config", headers={"X-Radar-Admin-Token": "wrong"}
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_admin_disabled_without_env_token(admin_client, monkeypatch):
    """未配置令牌 = 管理接口整体禁用。不安全的默认开启比少一个功能危险。"""
    http, _, _ = admin_client
    monkeypatch.delenv(api.ADMIN_TOKEN_ENV)
    response = await http.get("/api/radar/admin/config", headers=HEADERS)
    assert response.status_code == 503


@pytest.mark.asyncio
async def test_admin_get_returns_registry_driven_groups(admin_client):
    http, _, _ = admin_client
    body = (await http.get("/api/radar/admin/config", headers=HEADERS)).json()
    assert body["restart_pending"] is False
    assert body["override_count"] == 0
    all_params = [p for g in body["groups"] for p in g["params"]]
    assert len(all_params) == len(config_schema.PARAMS)
    assert all(p["value"] == p["default"] for p in all_params)


@pytest.mark.asyncio
async def test_admin_put_saves_override_and_records_audit(admin_client):
    http, service, db = admin_client
    response = await http.put(
        "/api/radar/admin/config", headers=HEADERS,
        json={"changes": {"scheduler.target_rpm": 60}, "remove": []},
    )
    body = response.json()
    assert response.status_code == 200 and body["saved"] is True
    assert body["restart_pending"] is True
    assert body["changed"]["scheduler.target_rpm"] == {"old": 72, "new": 60}

    # 覆盖文件落盘
    overrides_path = service.settings.data_dir / "overrides.yaml"
    saved = yaml.safe_load(overrides_path.read_text(encoding="utf-8"))
    assert saved == {"scheduler": {"target_rpm": 60}}

    # 审计记录带新指纹与逐键 diff
    await db.drain()
    audit = await db.fetch_one(
        "SELECT * FROM config_audit ORDER BY id DESC LIMIT 1"
    )
    assert audit is not None
    assert audit["operator"] == "admin_api"
    assert audit["config_hash"] == body["saved_config_hash"]
    assert audit["config_hash"] != service.settings.config_hash
    assert "target_rpm" in (audit["changes_json"] or "")

    # GET 反映覆盖状态
    state = (await http.get("/api/radar/admin/config", headers=HEADERS)).json()
    assert state["restart_pending"] is True
    assert state["override_count"] == 1
    param = next(p for g in state["groups"] for p in g["params"]
                 if p["path"] == "scheduler.target_rpm")
    assert param["value"] == 60 and param["overridden"] is True


@pytest.mark.asyncio
async def test_admin_put_rejects_invalid_value(admin_client):
    http, service, _ = admin_client
    response = await http.put(
        "/api/radar/admin/config", headers=HEADERS,
        json={"changes": {"scheduler.target_rpm": 99999}, "remove": []},
    )
    assert response.status_code == 400
    assert "scheduler.target_rpm" in response.json()["detail"]["errors"]
    assert not (service.settings.data_dir / "overrides.yaml").exists(), \
        "校验失败时绝不能写盘"


@pytest.mark.asyncio
async def test_admin_put_rejects_cross_field_conflict(admin_client):
    """两个值各自都在范围内，但组合起来滞回失效——必须整体拒绝。"""
    http, service, _ = admin_client
    response = await http.put(
        "/api/radar/admin/config", headers=HEADERS,
        json={"changes": {
            "state_machine.transitions.s1.enter_opportunity": 60.0,
        }, "remove": []},
    )
    assert response.status_code == 400
    assert not (service.settings.data_dir / "overrides.yaml").exists()


@pytest.mark.asyncio
async def test_admin_put_revert_to_default_clears_override(admin_client):
    http, service, _ = admin_client
    await http.put(
        "/api/radar/admin/config", headers=HEADERS,
        json={"changes": {"scheduler.target_rpm": 60}, "remove": []},
    )
    # 改回出厂默认值 = 删除覆盖项；覆盖层清空后文件直接删除
    response = await http.put(
        "/api/radar/admin/config", headers=HEADERS,
        json={"changes": {"scheduler.target_rpm": 72}, "remove": []},
    )
    body = response.json()
    assert body["saved"] is True
    assert "scheduler.target_rpm" in body["removed"]
    assert not (service.settings.data_dir / "overrides.yaml").exists()

    state = (await http.get("/api/radar/admin/config", headers=HEADERS)).json()
    assert state["override_count"] == 0
    assert state["restart_pending"] is False


@pytest.mark.asyncio
async def test_admin_remove_restores_default(admin_client):
    http, _, _ = admin_client
    await http.put(
        "/api/radar/admin/config", headers=HEADERS,
        json={"changes": {"email.max_per_hour": 24}, "remove": []},
    )
    response = await http.put(
        "/api/radar/admin/config", headers=HEADERS,
        json={"changes": {}, "remove": ["email.max_per_hour"]},
    )
    assert response.json()["removed"] == ["email.max_per_hour"]
    state = (await http.get("/api/radar/admin/config", headers=HEADERS)).json()
    assert state["override_count"] == 0


@pytest.mark.asyncio
async def test_admin_restart_delegates_to_service(admin_client):
    http, service, _ = admin_client
    response = await http.post("/api/radar/admin/restart", headers=HEADERS)
    assert response.status_code == 200
    assert response.json()["restarting"] is True
    assert service.restart_requested is True
