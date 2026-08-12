"""启动入口回归：禁止重复导入 main，就绪探针必须反映核心暖机状态。

根因背景：`uvicorn.run("main:socket_app")` 会二次导入入口模块，Engine 与全部
历史数据被初始化两次，后端常驻内存直接翻倍。
"""

from __future__ import annotations

import ast
import os
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routes import router, set_engine

MAIN_PATH = Path(__file__).resolve().parents[1] / "main.py"


def _uvicorn_run_call() -> ast.Call:
    tree = ast.parse(MAIN_PATH.read_text(encoding="utf-8"), str(MAIN_PATH))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "run"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "uvicorn"
        ):
            return node
    raise AssertionError("main.py 必须调用 uvicorn.run 启动服务")


def test_uvicorn_target_is_an_object_not_an_import_string():
    call = _uvicorn_run_call()
    assert call.args, "uvicorn.run 必须显式传入应用参数"
    target = call.args[0]
    assert not isinstance(target, ast.Constant), (
        "uvicorn.run 不得使用 'main:socket_app' 字符串目标：会二次导入 main 并重复初始化 Engine"
    )
    assert isinstance(target, ast.Name) and target.id == "socket_app"


def test_api_layer_never_imports_the_entrypoint_module():
    """API 层从 main 导入共享状态同样会触发入口模块二次执行。"""
    api_dir = MAIN_PATH.parent / "api"
    offenders = [
        path.name for path in api_dir.glob("*.py")
        if "from main import" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []


def test_duplicate_module_import_guard_raises(monkeypatch):
    import monitoring.log_buffer  # noqa: F401  确保被提取的日志缓冲模块可用

    module = type(sys)("main")
    module.__file__ = str(MAIN_PATH)
    monkeypatch.setitem(sys.modules, "main", module)

    definition = next(
        node for node in ast.parse(MAIN_PATH.read_text(encoding="utf-8")).body
        if isinstance(node, ast.FunctionDef) and node.name == "_guard_duplicate_module_import"
    )
    namespace: dict = {
        "__name__": "__main__", "__file__": str(MAIN_PATH), "sys": sys, "os": os,
    }
    exec(  # noqa: S102  只执行入口守卫函数定义，避免导入 main 产生副作用
        compile(ast.Module(body=[definition], type_ignores=[]), str(MAIN_PATH), "exec"),
        namespace,
    )
    with pytest.raises(RuntimeError, match="重复导入"):
        namespace["_guard_duplicate_module_import"]()


class _StubEngine:
    def __init__(self, ready: bool, phase: str, degraded: bool = False) -> None:
        self._ready = ready
        self._phase = phase
        self._degraded = degraded

    def get_startup_status(self) -> dict:
        return {
            "phase": self._phase,
            "core_ready": self._ready and not self._degraded,
            "ready": self._ready,
            "degraded": self._degraded,
            "default_coin": "BTC",
            "core_warmup_timeout_sec": 600,
            "started_at": 1_700_000_000,
            "uptime_sec": 12,
        }


def _client(engine) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    set_engine(engine)
    return TestClient(app)


def test_ready_endpoint_reports_503_before_core_warmup_completes():
    client = _client(_StubEngine(ready=False, phase="core_warmup"))
    response = client.get("/api/ready")
    assert response.status_code == 503
    assert response.json()["phase"] == "core_warmup"


def test_ready_endpoint_reports_200_after_core_warmup():
    client = _client(_StubEngine(ready=True, phase="running"))
    response = client.get("/api/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["ready"] is True
    assert body["core_ready"] is True


def test_ready_endpoint_releases_traffic_in_degraded_mode():
    """核心行情长时间不可用时仍需放行，否则一次上游故障会让前端也起不来。"""
    client = _client(_StubEngine(ready=True, phase="running", degraded=True))
    response = client.get("/api/ready")
    assert response.status_code == 200
    assert response.json()["degraded"] is True


def test_ready_endpoint_without_engine_is_not_ready():
    client = _client(None)
    response = client.get("/api/ready")
    assert response.status_code == 503
    assert response.json() == {"ready": False, "phase": "initializing"}
