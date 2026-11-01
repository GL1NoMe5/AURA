"""One real-worker API integration cycle with simulated diagnosis.

The diagnosis is a deterministic test double injected through the Python
app factory. It is NOT real AI; the worker fault, RSS growth and
verification are real. Offline; no provider requests.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import psutil
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
from aura_worker.cache import HEALTHY_CACHE_BYTES  # noqa: E402


class SimulatedDiagnosisOrchestrator:
    """Deterministic test-only diagnosis; clearly not a model call."""

    ready = True
    simulated = True

    def __init__(self):
        self.calls = 0

    async def diagnose(self, evidence, cancel_event, event_sink):
        self.calls += 1
        findings = []
        for agent_id in ("logs", "memory", "config", "vision"):
            await event_sink.agent_started(agent_id)
            findings.append(SpecialistFinding(
                agent_id=agent_id, status="completed",
                summary=f"{agent_id}: simulated diagnosis (test only)",
                findings=["retained cache grew beyond the healthy bound"],
                evidence_refs=["telemetry", "worker_status"]))
        return OrchestrationOutcome(
            findings=findings,
            proposal=SynthesisProposal(
                action="restore_cache_policy",
                reason="simulated diagnosis (test only)",
                evidence_refs=["telemetry"]))


def test_real_worker_cycle_with_simulated_diagnosis():
    orchestrator = SimulatedDiagnosisOrchestrator()
    app = create_app(
        orchestrator=orchestrator,
        settings=ApiSettings(
            detection_timeout_seconds=30.0,
            orchestration_timeout_seconds=10.0,
            verification_timeout_seconds=45.0,
            telemetry_interval_seconds=0.5),
    )
    manager = app.state.manager
    with TestClient(app) as client:
        health = client.get("/health").json()
        assert health["orchestrator_ready"] is True
        assert health["worker_running"] is True
        child_pid = manager.controller.child_pid
        assert child_pid is not None
        accepted = client.post(
            "/api/incidents", json={"scenario": "cache_retention"})
        assert accepted.status_code == 202
        deadline = time.monotonic() + 90
        state = {}
        while time.monotonic() < deadline:
            state = client.get("/api/state").json()
            if state["phase"] in ("resolved", "error"):
                break
            time.sleep(0.25)
        assert state["phase"] == "resolved", state
        assert state["simulated_diagnosis"] is True
        verification = state["latest_verification"]
        assert verification["verified"] is True
        assert verification["duration_seconds"] >= 7.9
        assert verification["retained_cache_bytes"] == HEALTHY_CACHE_BYTES
        assert state["telemetry"]["retained_cache_bytes"] == HEALTHY_CACHE_BYTES
        assert orchestrator.calls == 1
        types = [event["type"] for event in manager.journal]
        assert "action_applied" in types and "verification" in types
    # Lifespan shutdown reaped the owned worker tree.
    assert child_pid is not None and not psutil.pid_exists(child_pid)
