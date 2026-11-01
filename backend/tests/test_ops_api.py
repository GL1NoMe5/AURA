"""HTTP/WS operations tests with real persistence and no external services."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from aura_api.app import create_app
from aura_ops.service import OpsService
from aura_ops.models import OpsError
import aura_ops.service as service_module


class OfflineInvestigator:
    ready = False
    async def close(self):
        pass


class MissingDocker:
    def __init__(self, spec, **kwargs):
        pass
    async def collect(self):
        raise OpsError("docker_not_installed", "Docker is unavailable.", 503)


@pytest.fixture
def app(tmp_path, monkeypatch):
    async def unavailable():
        raise OpsError("docker_not_installed", "Docker is unavailable.", 503)
    monkeypatch.setattr(service_module, "runtime_info", unavailable)
    return create_app(enable_ops=True, enable_demo=False, provider_configured=False,
                      ops_factory=lambda: OpsService(tmp_path, docker_factory=MissingDocker,
                          investigator=OfflineInvestigator(), interval=.01))


def target(name="example"):
    return {"target_id": name, "name": name, "container_name": name,
            "probes": [{"url": "http://127.0.0.1:18090/"}]}


def test_reads_and_connection_do_not_start_demo_worker_or_inference(app):
    with TestClient(app) as client:
        status = client.get("/api/v2/status").json()
        assert status["targets"] == [] and status["provider_ready"] is False
        assert not app.state.manager.controller.is_running
        assert client.post("/api/incidents", json={"scenario": "cache_retention"}).status_code == 410
        assert client.post("/api/reset").status_code == 410
        assert not app.state.manager.controller.is_running
        with client.websocket_connect("/ws/ops") as ws:
            snapshot = ws.receive_json()
            assert snapshot["type"] == "snapshot"
            assert snapshot["payload"]["targets"] == []


def test_enroll_observe_only_policy_is_explicit_and_disconnect_keeps_record(app):
    with TestClient(app) as client:
        response = client.post("/api/v2/targets", json=target())
        assert response.status_code == 201
        assert response.json()["policy"]["automatic_actions"] == []
        assert client.post("/api/v2/targets", json=target()).status_code == 409
        automatic = {**target("automated"), "policy": {"auto_investigate": True}}
        assert client.post("/api/v2/targets", json=automatic).status_code == 400
        assert client.post("/api/v2/targets/example/policy", json={"automatic_actions": ["restart_container"]}).status_code == 200
        assert client.post("/api/v2/targets/example/disconnect").json()["container_stopped"] is False
        assert client.get("/api/v2/targets/example").json()["monitoring"] is False
        assert client.post("/api/v2/targets/example/reconnect").json()["monitoring"] is True
        assert client.post("/api/v2/investigations", json={"target_id": "example"}).status_code == 503
        assert client.get("/api/v2/investigations").json() == []


def test_cross_origin_mutations_are_rejected_and_reconnect_reads_are_safe(app):
    with TestClient(app) as client:
        response = client.post("/api/v2/targets", json=target(), headers={"Origin": "https://untrusted.example"})
        assert response.status_code == 403
        assert client.get("/api/v2/targets").json() == []
        assert client.post("/api/v2/targets", json=target(), headers={"Origin": "http://127.0.0.1:8000"}).status_code == 201
        with client.websocket_connect("/ws/ops") as ws:
            snapshot = ws.receive_json()
            client.post("/api/v2/targets/example/disconnect")
            event = ws.receive_json()
            assert event["seq"] > snapshot["seq"]
        assert client.get("/api/v2/investigations").json() == []


def test_artifact_download_is_checked_and_application_never_serves_arbitrary_files(app):
    with TestClient(app) as client:
        artifact = app.state.ops.store.save_artifact("review.patch", b"test patch", "text/plain")
        response = client.get("/api/v2/artifacts/" + artifact["artifact_id"])
        assert response.status_code == 200 and response.content == b"test patch"
        assert response.headers["x-content-type-options"] == "nosniff"
        path = app.state.ops.store.get_artifact(artifact["artifact_id"])["path"]
        Path(path).write_text("tampered")
        assert client.get("/api/v2/artifacts/" + artifact["artifact_id"]).status_code != 200
        assert client.get("/api/v2/artifacts/not-present").status_code == 400
        assert client.get("/api/v2/artifacts/" + "f" * 32).status_code == 404


def test_store_survives_http_lifespan_restart(tmp_path, monkeypatch):
    async def unavailable():
        raise OpsError("docker_not_installed", "Docker is unavailable.", 503)
    monkeypatch.setattr(service_module, "runtime_info", unavailable)
    def new_app():
        return create_app(enable_ops=True, enable_demo=False,
            ops_factory=lambda: OpsService(tmp_path, investigator=OfflineInvestigator(), docker_factory=MissingDocker))
    with TestClient(new_app()) as client:
        client.post("/api/v2/targets", json=target())
        before = client.get("/api/v2/status").json()["seq"]
    with TestClient(new_app()) as client:
        assert client.get("/api/v2/targets").json()[0]["target_id"] == "example"
        client.post("/api/v2/targets/example/disconnect")
        assert client.get("/api/v2/status").json()["seq"] > before
