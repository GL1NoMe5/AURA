"""INTEGRATION-001 offline integration tests.

Provider responses are mocked through httpx MockTransport; the worker
and the measured chart are real in the full-cycle test. That cycle is a
mock diagnosis, not a live AI incident. Zero live provider requests.
"""
from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_BACKEND = Path(__file__).resolve().parents[1]
_REPO = _BACKEND.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))
_TESTS = Path(__file__).resolve().parent
if str(_TESTS) not in sys.path:
    sys.path.insert(0, str(_TESTS))

from test_agents import (  # noqa: E402
    Recorder,
    _default_handler,
    _make_orchestrator,
)
from test_api import (  # noqa: E402
    DeterministicOrchestrator,
    FakeController,
    _outcome,
    _settings,
)

from aura_api import ApiSettings, create_app  # noqa: E402
from aura_api.manager import ApiError, IncidentManager  # noqa: E402
from aura_api.schemas import Phase  # noqa: E402
from aura_agents.chart import render_measured_chart  # noqa: E402
from aura_worker.cache import CEILING_BYTES, HEALTHY_CACHE_BYTES  # noqa: E402


# ------------------------------------------------------------- imports

@pytest.mark.parametrize("first,second", [
    ("aura_agents", "aura_api"), ("aura_api", "aura_agents")])
def test_both_import_orders(first, second):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(_BACKEND)
    result = subprocess.run(
        [sys.executable, "-c",
         f"import {first}; import {second}; "
         "from aura_api import create_app; print('ok')"],
        capture_output=True, text=True, timeout=60, env=env,
        cwd=str(_BACKEND))
    assert result.returncode == 0, result.stderr[-2000:]
    assert "ok" in result.stdout


# ------------------------------------------------------- default wiring

def test_default_wiring_reads_make_no_dispatch_and_shutdown_closes():
    fake = FakeController()
    app = create_app(
        wire_default_orchestrator=True, controller_factory=lambda: fake,
        settings=_settings())
    orchestrator = app.state.manager.orchestrator
    assert type(orchestrator).__name__ == "BasetenSwarmOrchestrator"
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/api/state").json()["phase"] == "idle"
        with client.websocket_connect("/ws/telemetry") as ws:
            assert ws.receive_json()["type"] == "snapshot"
    # Lifespan closed the provider adapter even without any dispatch.
    assert orchestrator.ready is False


def test_default_wiring_missing_key_503_before_injection(monkeypatch):
    monkeypatch.setattr(
        "aura_agents.adapter.load_api_key", lambda project_root=None: None)
    fake = FakeController()
    app = create_app(
        wire_default_orchestrator=True, controller_factory=lambda: fake,
        provider_configured=False, settings=_settings())
    with TestClient(app) as client:
        health = client.get("/health").json()
        assert health["orchestrator_ready"] is False
        response = client.post(
            "/api/incidents", json={"scenario": "cache_retention"})
        assert response.status_code == 503
    assert fake.inject_calls == 0


def test_no_dispatch_after_reset_with_mocked_swarm():
    async def run():
        recorder = Recorder()

        async def hanging(_count, _body):
            await asyncio.sleep(30)
            return None

        orchestrator = _make_orchestrator(_default_handler(
            recorder, overrides={
                a: hanging for a in
                ("logs", "memory", "config", "vision")}))
        # _default_handler expects Response returns; hanging returns None
        # only if reached, but requests are cancelled while sleeping.
        manager = IncidentManager(
            controller=FakeController(), orchestrator=orchestrator,
            settings=_settings(), provider_configured=True)
        await manager.start_incident("cache_retention")
        deadline = time.monotonic() + 5
        while not recorder.requests:
            assert time.monotonic() < deadline
            await asyncio.sleep(0.02)
        await manager.reset()
        counts_after = dict(recorder.counts)
        await asyncio.sleep(0.3)
        assert recorder.counts == counts_after
        assert recorder.counts.get("synthesis", 0) == 0
        await manager.shutdown()
        await orchestrator.aclose()
    asyncio.run(run())


def test_terminal_failed_run_requires_reset_before_new_run():
    async def run():
        fake = FakeController()
        manager = IncidentManager(
            controller=fake,
            orchestrator=DeterministicOrchestrator(
                _outcome(action="abstain")),
            settings=_settings(), provider_configured=True)
        run = await manager.start_incident("cache_retention")
        await asyncio.wait_for(run.task, timeout=10)
        assert run.phase == Phase.ERROR
        # Worker is still dirty (eviction disabled); stale fault state
        # must not be reused for a new injection.
        with pytest.raises(ApiError) as excinfo:
            await manager.start_incident("cache_retention")
        assert excinfo.value.status_code == 409
        assert excinfo.value.code == "reset_required"
        assert fake.inject_calls == 1
        await manager.reset()
        second = await manager.start_incident("cache_retention")
        await asyncio.wait_for(second.task, timeout=10)
        assert fake.inject_calls == 2
        await manager.shutdown()
    asyncio.run(run())


class _DelayedCeilingController(FakeController):
    def __init__(self):
        super().__init__()
        self._injected_at = None

    async def inject_cache_retention(self):
        result = await super().inject_cache_retention()
        self._injected_at = time.monotonic()
        self.retained = HEALTHY_CACHE_BYTES
        self.failed = 0
        return result

    def _status(self):
        status = super()._status()
        status["ceiling_bytes"] = CEILING_BYTES
        if self._injected_at is not None and \
                time.monotonic() - self._injected_at > 0.7:
            self.retained = CEILING_BYTES
            self.failed = 3
            status["retained_cache_bytes"] = CEILING_BYTES
            status["failed_jobs"] = 3
        return status


class _RecordingOrchestrator:
    ready = True
    simulated = True

    def __init__(self):
        self.evidence = None
        self.called_at = None

    async def diagnose(self, evidence, cancel_event, event_sink):
        self.evidence = evidence
        self.called_at = time.monotonic()
        return _outcome()


def test_failure_counter_gating_waits_for_ceiling():
    async def run():
        fake = _DelayedCeilingController()
        orchestrator = _RecordingOrchestrator()
        manager = IncidentManager(
            controller=fake, orchestrator=orchestrator,
            settings=_settings(), provider_configured=True)
        run = await manager.start_incident("cache_retention")
        await asyncio.wait_for(run.task, timeout=15)
        assert run.phase == Phase.RESOLVED
        assert orchestrator.evidence is not None
        assert orchestrator.evidence.retained_cache_bytes == CEILING_BYTES
        assert (orchestrator.evidence.failed_jobs or 0) >= 1
        await manager.shutdown()
    asyncio.run(run())


def test_live_harness_default_mode_makes_zero_requests(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "live_run", _REPO / "tools" / "preflight" / "live_run.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    output = tmp_path / "live-run.json"
    assert module.main(["--output", str(output)]) == 0
    record = json.loads(output.read_text(encoding="utf-8"))
    assert record["mode"] == "offline"
    assert record["provider_requests_made"] == 0


def test_real_worker_cycle_mocked_provider_real_chart():
    """Real worker + real Codex chart; provider responses are mocked.

    This is a mock diagnosis cycle, not a live AI incident.
    """
    recorder = Recorder()
    orchestrator = _make_orchestrator(
        _default_handler(recorder), renderer=render_measured_chart)
    app = create_app(
        orchestrator=orchestrator,
        settings=ApiSettings(
            detection_timeout_seconds=30.0,
            failure_evidence_timeout_seconds=60.0,
            orchestration_timeout_seconds=30.0,
            verification_timeout_seconds=45.0,
            telemetry_interval_seconds=0.5),
    )
    manager = app.state.manager
    with TestClient(app) as client:
        accepted = client.post(
            "/api/incidents", json={"scenario": "cache_retention"})
        assert accepted.status_code == 202
        deadline = time.monotonic() + 120
        state = {}
        while time.monotonic() < deadline:
            state = client.get("/api/state").json()
            if state["phase"] in ("resolved", "error"):
                break
            time.sleep(0.25)
        assert state["phase"] == "resolved", state
        assert state["latest_verification"]["verified"] is True
        assert state["telemetry"]["retained_cache_bytes"] \
            == HEALTHY_CACHE_BYTES
        agents = {a["agent_id"]: a for a in state["agents"]}
        assert all(agents[a]["status"] == "completed"
                   for a in ("logs", "memory", "config", "vision",
                             "synthesis"))
        assert agents["logs"]["usage"]["total_tokens"] == 140
        assert agents["logs"]["attempts"] == 1
    assert recorder.counts.get("synthesis") == 1
    assert len(orchestrator.call_metadata) == 5
    vision = next(r for r in recorder.requests if r["agent"] == "vision")
    parts = vision["body"]["messages"][1]["content"]
    image = next(p for p in parts if p["type"] == "image_url")
    chart_bytes = base64.b64decode(
        image["image_url"]["url"].split(",", 1)[1])
    assert chart_bytes.startswith(b"\x89PNG\r\n\x1a\n")
    assert len(chart_bytes) > 5000  # real 1000x600 measured chart
