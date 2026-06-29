from __future__ import annotations

from types import SimpleNamespace

import aiohttp
import pytest

from sources.coinglass import CoinglassSource
from scripts import preflight_coinglass


class _Response:
    def __init__(self, status: int, payload: dict | None = None) -> None:
        self.status = status
        self._payload = payload or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def raise_for_status(self) -> None:
        if self.status >= 400:
            raise aiohttp.ClientResponseError(
                request_info=SimpleNamespace(real_url="https://proxy.invalid/test"),
                history=(),
                status=self.status,
                message="Unauthorized",
                headers=None,
            )

    async def json(self) -> dict:
        return self._payload


class _Session:
    def __init__(self, response: _Response) -> None:
        self.response = response
        self.calls = 0

    def get(self, *_args, **_kwargs):
        self.calls += 1
        return self.response


@pytest.mark.asyncio
async def test_missing_key_never_sends_request(monkeypatch, tmp_path):
    source = CoinglassSource("https://proxy.invalid", "", rate_per_min=6000)
    source._cache_dir = str(tmp_path)

    async def forbidden_session():
        raise AssertionError("missing key must not create an HTTP session")

    monkeypatch.setattr(source, "get_session", forbidden_session)
    assert await source._request("/api/test") is None
    health = source.health()
    assert health.status == "disconnected"
    assert health.reason == "missing_api_key"
    assert source.daily_request_count == 0


@pytest.mark.asyncio
async def test_401_enters_auth_cooldown_and_suppresses_followup(monkeypatch):
    source = CoinglassSource("https://proxy.invalid", "ks_live_test", rate_per_min=6000)
    session = _Session(_Response(401))

    async def get_session():
        return session

    monkeypatch.setattr(source, "get_session", get_session)
    assert await source._request("/api/first") is None
    assert await source._request("/api/second") is None
    assert session.calls == 1
    health = source.health()
    assert health.status == "disconnected"
    assert health.reason == "unauthorized"
    assert health.last_http_status == 401
    assert health.auth_blocked_until is not None


@pytest.mark.asyncio
async def test_success_clears_previous_auth_diagnostics(monkeypatch):
    source = CoinglassSource("https://proxy.invalid", "ks_live_test", rate_per_min=6000)
    source._health_reason = "unauthorized"
    source._auth_blocked_until = 0
    source._last_http_status = 401
    session = _Session(_Response(200, {"code": 0, "data": [{"ok": True}]}))

    async def get_session():
        return session

    monkeypatch.setattr(source, "get_session", get_session)
    assert await source._request("/api/recovered") == [{"ok": True}]
    health = source.health()
    assert health.status == "connected"
    assert health.reason is None
    assert health.last_http_status == 200


def test_deploy_preflight_rejects_missing_key_before_network(monkeypatch, tmp_path):
    env_path = tmp_path / ".env"
    config_path = tmp_path / "config.yaml"
    env_path.write_text("AI_API_KEY=test\n", encoding="utf-8")
    config_path.write_text(
        'base_url: "https://www.keystore.com.cn/api/v1/proxy/coinglass"\n'
        'api_key_env: "COINGLASS_API_KEY"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(preflight_coinglass, "ENV_PATH", env_path)
    monkeypatch.setattr(preflight_coinglass, "CONFIG_PATH", config_path)

    def forbidden_urlopen(*_args, **_kwargs):
        raise AssertionError("missing key must fail before network")

    monkeypatch.setattr(preflight_coinglass.urllib.request, "urlopen", forbidden_urlopen)
    assert preflight_coinglass.main() == 2


def test_deploy_preflight_rejects_proxy_config_drift(monkeypatch, tmp_path):
    env_path = tmp_path / ".env"
    config_path = tmp_path / "config.yaml"
    env_path.write_text("COINGLASS_API_KEY=ks_live_test\n", encoding="utf-8")
    config_path.write_text('base_url: "https://wrong.invalid"\n', encoding="utf-8")
    monkeypatch.setattr(preflight_coinglass, "ENV_PATH", env_path)
    monkeypatch.setattr(preflight_coinglass, "CONFIG_PATH", config_path)
    assert preflight_coinglass.main() == 1
