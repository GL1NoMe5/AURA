"""RELEASE-001 offline reliability tests (no provider requests)."""
from __future__ import annotations

import importlib.util
import socket
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_BACKEND = Path(__file__).resolve().parents[1]
_REPO = _BACKEND.parent
_TESTS = Path(__file__).resolve().parent
for _path in (str(_BACKEND), str(_TESTS)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from test_api import FakeController, _settings  # noqa: E402

from aura_api import create_app  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "aura_runtime", _REPO / "tools" / "runtime" / "aura_runtime.py")
assert _spec is not None and _spec.loader is not None
runtime = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(runtime)

SECRET_MARKER = "AURA-RELEASE-TEST-SECRET"


def _dist(tmp_path: Path) -> Path:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(
        "<html><title>AURA TEST INDEX</title></html>", encoding="utf-8")
    (dist / "assets" / "app.js").write_text(
        "console.log('aura');", encoding="utf-8")
    return dist


def _app(tmp_path, **kwargs):
    fake = FakeController()
    app = create_app(
        controller_factory=lambda: fake, settings=_settings(),
        static_dir=_dist(tmp_path), **kwargs)
    return app, fake


def test_static_root_api_and_ws_still_correct(tmp_path):
    app, fake = _app(tmp_path)
    with TestClient(app) as client:
        root = client.get("/")
        assert root.status_code == 200
        assert "AURA TEST INDEX" in root.text
        asset = client.get("/assets/app.js")
        assert asset.status_code == 200 and "console.log" in asset.text
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["worker_running"] is True
        state = client.get("/api/state")
        assert state.status_code == 200
        assert state.json()["phase"] == "idle"
        with client.websocket_connect("/ws/telemetry") as ws:
            assert ws.receive_json()["type"] == "snapshot"
        spa = client.get("/some/client/route")
        assert spa.status_code == 200 and "AURA TEST INDEX" in spa.text
    assert fake.stop_calls >= 1


def test_unknown_api_route_is_json_404_never_html(tmp_path):
    app, _fake = _app(tmp_path)
    with TestClient(app, raise_server_exceptions=False) as client:
        for path in ("/api/nope", "/api/incidents/extra", "/ws/nope"):
            response = client.get(path)
            assert response.status_code == 404, path
            assert "AURA TEST INDEX" not in response.text
            assert response.json()["error"]["code"] == "not_found"


def test_traversal_and_sensitive_paths_never_served(tmp_path):
    dist = _dist(tmp_path)
    (tmp_path / ".env").write_text(
        f"BASETEN_API_KEY={SECRET_MARKER}", encoding="utf-8")
    fake = FakeController()
    app = create_app(
        controller_factory=lambda: fake, settings=_settings(),
        static_dir=dist)
    with TestClient(app, raise_server_exceptions=False) as client:
        for path in ("/../.env", "/assets/../../.env", "/.env",
                     "/.local/wonder/latest-report.md",
                     "/%2e%2e/.env", "/assets/%2e%2e/%2e%2e/.env"):
            response = client.get(path)
            assert SECRET_MARKER not in response.text, path
            assert response.status_code in (404, 400), path


def test_missing_build_is_clear_but_api_works(tmp_path):
    empty = tmp_path / "empty-dist"
    empty.mkdir()
    fake = FakeController()
    app = create_app(
        controller_factory=lambda: fake, settings=_settings(),
        static_dir=empty)
    with TestClient(app, raise_server_exceptions=False) as client:
        root = client.get("/")
        assert root.status_code == 503
        assert root.json()["error"]["code"] == "frontend_build_missing"
        assert "Start-AURA" in root.json()["error"]["message"]
        assert client.get("/health").status_code == 200


def test_repeated_lifespan_start_stop_leaves_fake_stopped(tmp_path):
    app, fake = _app(tmp_path)
    for _ in range(2):
        with TestClient(app) as client:
            assert client.get("/health").status_code == 200
    assert fake.start_calls >= 2
    assert fake.stop_calls >= 2
    assert fake.is_running is False


# ------------------------------------------------------------ runtime

def test_occupied_port_is_rejected_without_killing(tmp_path):
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    port = blocker.getsockname()[1]
    try:
        with pytest.raises(runtime.RuntimeSetupError) as excinfo:
            runtime.assert_port_free(port)
        assert "already in use" in str(excinfo.value)
        # The unknown listener is untouched and still accepting.
        assert blocker.getsockname()[1] == port
    finally:
        blocker.close()


def test_runtime_prerequisite_errors_are_actionable(tmp_path):
    errors = runtime.check_prerequisites(tmp_path)
    assert any("--setup" in error for error in errors)
    assert runtime.main(
        ["check", "--project-root", str(tmp_path)]) == 2


def test_build_subprocess_env_never_contains_provider_key(monkeypatch):
    monkeypatch.setenv("BASETEN_API_KEY", SECRET_MARKER)
    env = runtime.build_subprocess_env()
    assert "BASETEN_API_KEY" not in env
    assert SECRET_MARKER not in " ".join(env.values())
