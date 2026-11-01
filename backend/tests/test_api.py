"""Offline HTTP/WS tests for API-001 with fake controller/orchestrator.

Deterministic fakes exist only here; the shipped default orchestrator is
unavailable. No provider requests are made.
"""
from __future__ import annotations

import asyncio
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_BACKEND = Path(__file__).resolve().parents[1]
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from aura_api import (  # noqa: E402
    ApiSettings,
    OrchestrationOutcome,
    SpecialistFinding,
    SynthesisProposal,
    create_app,
)
from aura_api.manager import ApiError, IncidentManager  # noqa: E402
from aura_api.schemas import Phase  # noqa: E402
from aura_worker import VerificationResult  # noqa: E402
from aura_worker.cache import HEALTHY_CACHE_BYTES  # noqa: E402
from aura_worker.controller import ControllerState  # noqa: E402

MIB = 1024 * 1024
BASELINE = 40 * MIB


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class FakeController:
    """Deterministic controller double with fresh telemetry."""

    def __init__(self):
        self.state = ControllerState.HEALTHY
        self.generation = 1
        self.is_running = True
        self.child_pid = 987654
        self.baseline_rss_bytes = BASELINE
        self.rss_bytes = BASELINE
        self.retained = HEALTHY_CACHE_BYTES
        self.eviction_enabled = True
        self.completed = 100
        self.failed = 0
        self.start_calls = 0
        self.stop_calls = 0
        self.reset_calls = 0
        self.inject_calls = 0
        self.restore_calls = 0
        self.dead = False
        self.verify_delay = 0.05

    def _status(self) -> dict:
        return {
            "initialized": True, "eviction_enabled": self.eviction_enabled,
            "retained_cache_bytes": self.retained,
            "completed_jobs": self.completed, "failed_jobs": self.failed,
        }

    def snapshot(self) -> dict:
        fresh = self.is_running
        return {
            "state": self.state.value, "generation": self.generation,
            "child_running": self.is_running,
            "baseline_rss_bytes": self.baseline_rss_bytes,
            "rss_bytes": self.rss_bytes if fresh else None,
            "rss_fresh": fresh,
            "rss_sample_timestamp": _now() if fresh else None,
            "worker_status": self._status() if fresh else None,
            "worker_status_stale": not fresh,
            "worker_status_timestamp": _now() if fresh else None,
            "retained_cache_bytes": self.retained,
            "completed_jobs": self.completed, "failed_jobs": self.failed,
            "measurements": [],
        }

    async def start(self) -> dict:
        self.start_calls += 1
        self.is_running = True
        self.state = ControllerState.HEALTHY
        return self._status()

    async def stop(self) -> None:
        self.stop_calls += 1
        self.is_running = False
        self.state = ControllerState.STOPPED

    async def reset(self) -> dict:
        self.reset_calls += 1
        self.generation += 1
        self.is_running = True
        self.state = ControllerState.HEALTHY
        self.eviction_enabled = True
        self.retained = HEALTHY_CACHE_BYTES
        self.rss_bytes = BASELINE
        self.completed = 0
        self.failed = 0
        return self._status()

    async def status(self) -> dict:
        return self._status()

    async def inject_cache_retention(self) -> dict:
        self.inject_calls += 1
        self.eviction_enabled = False
        self.retained = 60 * MIB
        self.rss_bytes = BASELINE + 30 * MIB
        self.failed = 2
        if self.dead:
            self.state = ControllerState.DEAD
        else:
            self.state = ControllerState.FAULT_DETECTED
        return self._status()

    async def wait_for_detection(self, timeout: float = 30.0) -> bool:
        return not self.dead

    async def restore_and_verify(self, timeout=None) -> VerificationResult:
        self.restore_calls += 1
        self.state = ControllerState.VERIFYING
        await asyncio.sleep(self.verify_delay)
        self.state = ControllerState.RECOVERED
        self.eviction_enabled = True
        self.retained = HEALTHY_CACHE_BYTES
        self.rss_bytes = BASELINE
        self.completed += 32
        return VerificationResult(
            verified=True, reason="verified", duration_seconds=8.0,
            baseline_rss_bytes=BASELINE, rss_bytes=BASELINE,
            retained_cache_bytes=HEALTHY_CACHE_BYTES,
            completed_jobs=self.completed, failed_jobs=self.failed,
            generation=self.generation, completed_jobs_start=100,
            failed_jobs_start=self.failed)


def _finding(agent_id: str) -> SpecialistFinding:
    return SpecialistFinding(
        agent_id=agent_id, status="completed",
        summary=f"{agent_id} simulated finding",
        findings=[f"{agent_id} observed the measured fault"],
        evidence_refs=["telemetry"])


def _outcome(action="restore_cache_policy", agents=("logs", "memory", "config", "vision")):
    return OrchestrationOutcome(
        findings=[_finding(agent_id) for agent_id in agents],
        proposal=SynthesisProposal(
            action=action, reason="simulated diagnosis (test only)",
            evidence_refs=["telemetry"]))


class DeterministicOrchestrator:
    """Test-only orchestrator injected through the Python app factory."""

    ready = True
    simulated = True

    def __init__(self, outcome=None, *, block: bool = False,
                 late: bool = False):
        self.calls = 0
        self._outcome = outcome if outcome is not None else _outcome()
        self._block = block
        self._late = late
        self.release = asyncio.Event()

    async def diagnose(self, evidence, cancel_event, event_sink):
        self.calls += 1
        for agent_id in ("logs", "memory", "config", "vision"):
            await event_sink.agent_started(agent_id)
        if self._late:
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                return self._outcome  # late result; manager must drop it
        if self._block:
            await self.release.wait()
        return self._outcome


def _settings(**overrides) -> ApiSettings:
    values = dict(
        detection_timeout_seconds=5.0, orchestration_timeout_seconds=5.0,
        verification_timeout_seconds=10.0, telemetry_interval_seconds=0.05,
        subscriber_queue_size=5, journal_maxlen=20)
    values.update(overrides)
    return ApiSettings(**values)


def _app(fake=None, orchestrator=None, **kwargs):
    fake = fake or FakeController()
    app = create_app(
        orchestrator=orchestrator, controller_factory=lambda: fake,
        settings=_settings(), **kwargs)
    return app, fake


def _wait_phase(client: TestClient, phase: str, timeout: float = 10.0) -> dict:
    deadline = time.monotonic() + timeout
    state = {}
    while time.monotonic() < deadline:
        state = client.get("/api/state").json()
        if state["phase"] == phase:
            return state
        time.sleep(0.05)
    raise AssertionError(f"phase {phase} not reached; last={state}")


# ---------------------------------------------------------------- HTTP

def test_health_state_and_default_503_before_injection():
    app, fake = _app(provider_configured=True)  # key exists, still 503
    with TestClient(app) as client:
        health = client.get("/health")
        assert health.status_code == 200
        body = health.json()
        assert body["provider_configured"] is True
        assert body["orchestrator_ready"] is False
        assert "BASETEN" not in health.text
        state = client.get("/api/state").json()
        assert state["phase"] == "idle" and state["run_id"] is None
        assert state["telemetry"]["rss_bytes"] == BASELINE
        response = client.post(
            "/api/incidents", json={"scenario": "cache_retention"})
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "orchestrator_unavailable"
        assert fake.inject_calls == 0  # rejected BEFORE any injection
    assert fake.stop_calls >= 1  # lifespan shutdown cleanup


def test_invalid_scenario_rejected_without_injection():
    orchestrator = DeterministicOrchestrator()
    app, fake = _app(orchestrator=orchestrator, provider_configured=False)
    with TestClient(app) as client:
        assert client.post(
            "/api/incidents", json={"scenario": "traffic_spike"}
        ).status_code == 422
        assert client.post(
            "/api/incidents", json={"scenario": "cache_retention", "x": 1}
        ).status_code == 422
    assert fake.inject_calls == 0
    assert orchestrator.calls == 0


def test_http_run_resolves_with_fake_worker_and_events():
    orchestrator = DeterministicOrchestrator()
    app, fake = _app(orchestrator=orchestrator)
    with TestClient(app) as client:
        accepted = client.post(
            "/api/incidents", json={"scenario": "cache_retention"})
        assert accepted.status_code == 202
        run_id = accepted.json()["run_id"]
        state = _wait_phase(client, "resolved")
        assert state["run_id"] == run_id
        assert state["latest_verification"]["verified"] is True
        assert state["simulated_diagnosis"] is True
        assert all(a["status"] == "completed" for a in state["agents"])
        events = app.state.manager.journal
        types = [e["type"] for e in events]
        assert "action_proposed" in types and "action_applied" in types
        assert types.index("action_applied") > types.index("action_proposed")
        assert [e["seq"] for e in events] == sorted(e["seq"] for e in events)
    assert fake.restore_calls == 1  # restore never called twice for events


def test_websocket_snapshot_monotonic_and_reconnect_no_duplicate():
    orchestrator = DeterministicOrchestrator()
    app, fake = _app(orchestrator=orchestrator)
    with TestClient(app) as client:
        with client.websocket_connect("/ws/telemetry") as ws:
            snapshot = ws.receive_json()
            assert snapshot["type"] == "snapshot"
            assert snapshot["payload"]["phase"] == "idle"
            first_seq = snapshot["seq"]
            telemetry = ws.receive_json()
            assert telemetry["seq"] > first_seq
        client.post("/api/incidents", json={"scenario": "cache_retention"})
        _wait_phase(client, "resolved")
        assert orchestrator.calls == 1
        with client.websocket_connect("/ws/telemetry") as ws:
            snapshot = ws.receive_json()
            assert snapshot["type"] == "snapshot"
            assert snapshot["seq"] > first_seq
            assert snapshot["payload"]["phase"] == "resolved"
        assert orchestrator.calls == 1  # reconnect starts no inference


def test_forbidden_and_allowed_origins():
    orchestrator = DeterministicOrchestrator()
    app, fake = _app(orchestrator=orchestrator)
    evil = {"Origin": "https://evil.example"}
    local = {"Origin": "http://127.0.0.1:5173"}
    with TestClient(app, raise_server_exceptions=False) as client:
        assert client.post(
            "/api/incidents", json={"scenario": "cache_retention"},
            headers=evil).status_code == 403
        assert client.post("/api/reset", headers=evil).status_code == 403
        assert fake.inject_calls == 0
        accepted = client.post(
            "/api/incidents", json={"scenario": "cache_retention"},
            headers=local)
        assert accepted.status_code == 202
        assert accepted.headers.get(
            "access-control-allow-origin") == "http://127.0.0.1:5173"
        _wait_phase(client, "resolved")
        with pytest.raises(Exception):
            with client.websocket_connect(
                    "/ws/telemetry", headers=evil):
                pass
        with client.websocket_connect("/ws/telemetry", headers=local) as ws:
            assert ws.receive_json()["type"] == "snapshot"


# ------------------------------------------------------- manager (async)

def _manager(fake=None, orchestrator=None, **settings_overrides):
    return IncidentManager(
        controller=fake or FakeController(),
        orchestrator=orchestrator or DeterministicOrchestrator(),
        settings=_settings(**settings_overrides),
        provider_configured=False)


async def _await_run_task(manager: IncidentManager, timeout: float = 10.0):
    run = manager._current_run
    assert run is not None and run.task is not None
    await asyncio.wait_for(run.task, timeout=timeout)
    return run


def test_concurrent_starts_one_202_rest_409():
    async def run():
        orchestrator = DeterministicOrchestrator(block=True)
        manager = _manager(orchestrator=orchestrator)
        results = await asyncio.gather(
            manager.start_incident("cache_retention"),
            manager.start_incident("cache_retention"),
            return_exceptions=True)
        created = [r for r in results if not isinstance(r, Exception)]
        errors = [r for r in results if isinstance(r, ApiError)]
        assert len(created) == 1
        assert len(errors) == 1 and errors[0].status_code == 409
        orchestrator.release.set()
        await _await_run_task(manager)
        assert manager.snapshot().phase == Phase.RESOLVED
        await manager.shutdown()
    asyncio.run(run())


@pytest.mark.parametrize(("outcome", "code"), [
    (_outcome(action="abstain"), "synthesis_abstained"),
    (_outcome(agents=("logs", "memory", "config")),
     "incomplete_specialist_findings"),
    ({"findings": [], "proposal": {"action": "delete_everything",
                                   "reason": "x", "evidence_refs": []}},
     "invalid_orchestration_output"),
])
def test_abstain_incomplete_unknown_never_restore(outcome, code):
    async def run():
        fake = FakeController()
        manager = _manager(
            fake=fake, orchestrator=DeterministicOrchestrator(outcome))
        await manager.start_incident("cache_retention")
        finished = await _await_run_task(manager)
        assert finished.phase == Phase.ERROR
        assert finished.error is not None and finished.error.code == code
        assert fake.restore_calls == 0
        await manager.shutdown()
    asyncio.run(run())


def test_reset_during_diagnosis_rejects_late_result():
    async def run():
        fake = FakeController()
        orchestrator = DeterministicOrchestrator(late=True)
        manager = _manager(fake=fake, orchestrator=orchestrator)
        await manager.start_incident("cache_retention")
        deadline = time.monotonic() + 5
        while orchestrator.calls == 0:
            assert time.monotonic() < deadline
            await asyncio.sleep(0.02)
        snapshot = await manager.reset()
        assert snapshot.phase == Phase.IDLE and snapshot.run_id is None
        await asyncio.sleep(0.2)  # late outcome must stay dropped
        assert manager.snapshot().phase == Phase.IDLE
        assert fake.restore_calls == 0
        assert fake.reset_calls == 1
        await manager.shutdown()
    asyncio.run(run())


def test_reset_during_verification_rejects_late_success():
    async def run():
        fake = FakeController()
        fake.verify_delay = 5.0
        manager = _manager(fake=fake)
        await manager.start_incident("cache_retention")
        deadline = time.monotonic() + 5
        while fake.restore_calls == 0:
            assert time.monotonic() < deadline
            await asyncio.sleep(0.02)
        snapshot = await manager.reset()
        assert snapshot.phase == Phase.IDLE
        types = [e["type"] for e in manager.journal]
        assert "verification" not in types[-3:]
        assert manager.snapshot().latest_verification is None
        await manager.shutdown()
    asyncio.run(run())


def test_worker_death_becomes_error_not_resolved():
    async def run():
        fake = FakeController()
        fake.dead = True
        manager = _manager(fake=fake)
        await manager.start_incident("cache_retention")
        finished = await _await_run_task(manager)
        assert finished.phase == Phase.ERROR
        assert finished.error is not None
        assert finished.error.code == "worker_dead"
        assert manager.snapshot().worker_health == "dead"
        await manager.shutdown()
    asyncio.run(run())


def test_slow_subscriber_bounded_queue_and_journal():
    async def run():
        manager = _manager(subscriber_queue_size=3, journal_maxlen=5)
        queue = manager.subscribe()
        for index in range(12):
            await manager._emit(
                "telemetry", {"index": index}, run_id=None)
        assert queue.qsize() <= 3
        assert manager.dropped_for_slow_subscribers > 0
        assert len(manager.journal) <= 5
        seqs = [event["seq"] for event in manager.journal]
        assert seqs == sorted(seqs)
        manager.unsubscribe(queue)
        await manager.shutdown()
    asyncio.run(run())


def test_simultaneous_resets_are_serialized():
    async def run():
        fake = FakeController()
        manager = _manager(fake=fake)
        first, second = await asyncio.gather(
            manager.reset(), manager.reset())
        assert first.phase == Phase.IDLE and second.phase == Phase.IDLE
        assert fake.reset_calls == 2
        await manager.shutdown()
    asyncio.run(run())
