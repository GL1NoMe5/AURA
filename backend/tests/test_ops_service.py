"""Independent ops lifecycle checks using SQLite and bounded in-memory adapters."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import aura_ops.service as service_module
from aura_ops.models import ActionKind, OpsError, Policy, TargetSpec, utc_now
from aura_ops.service import OpsService
from aura_ops.store import OpsStore


def target(name="alpha"):
    return TargetSpec(target_id=name, name=name.title(), container_name=name,
                      probes=[{"url": "http://127.0.0.1:18090/health"}])


def observed(*, healthy=True, image="b", timestamp=None):
    return {"timestamp": timestamp or utc_now(), "healthy": healthy,
            "identity": {"container_id": "a" * 64, "image_id": "sha256:" + image * 64,
                         "image_ref": "app:pinned", "started_at": "2026-10-28T00:00:00Z"},
            "runtime": {"running": True, "status": "running", "health": "none"},
            "resources": {"memory_bytes": 10_000_000, "memory_limit_bytes": 100_000_000},
            "probes": [{"url": "http://127.0.0.1:18090/health", "ok": healthy,
                        "status_code": 200 if healthy else 500, "duration_ms": 2}],
            "logs": "", "config": {}}


class Docker:
    def __init__(self, state=None, error=None):
        self.state = state if state is not None else observed()
        self.error = error
        self.reads = 0
        self.stops = 0

    def __call__(self, spec, **kwargs):
        self.spec = spec
        return self

    async def collect(self):
        self.reads += 1
        if self.error:
            raise self.error
        return deepcopy(self.state)

    async def inspect_identity(self):
        return deepcopy(self.state["identity"])

    async def stop(self):
        self.stops += 1
        raise AssertionError("AURA must never stop an external target during cleanup")


class Investigator:
    ready = True

    def __init__(self, gate=None, late=False):
        self.gate = gate
        self.late = late
        self.entered = asyncio.Event()
        self.calls = []
        self.running = 0
        self.peak = 0
        self.closed = False

    async def diagnose(self, spec, evidence, cancel, emit, refresh=None):
        self.calls.append(spec.target_id)
        self.running += 1
        self.peak = max(self.peak, self.running)
        self.entered.set()
        try:
            if self.gate:
                try:
                    await self.gate.wait()
                except asyncio.CancelledError:
                    if not self.late:
                        raise
            return {"diagnosis": {"summary": "No supported intervention", "findings": [],
                                  "action": "abstain", "reason": "Insufficient evidence",
                                  "needs_source_analysis": False}, "evidence": evidence, "calls": []}
        finally:
            self.running -= 1

    async def close(self):
        self.closed = True


async def until(predicate):
    for _ in range(200):
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("Expected bounded async transition did not occur")


def test_usage_survives_rejected_provider_response(tmp_path):
    from aura_agents.adapter import ProviderOutputError
    async def run():
        usage = {"agent_id": "runtime", "attempts": 1, "usage": {"total_tokens": 42}}
        class Failing(Investigator):
            async def diagnose(self, spec, evidence, cancel, emit, refresh=None):
                await emit("provider_usage", usage)
                raise ProviderOutputError("invalid_ops_json")
        service = make_service(tmp_path, investigator=Failing())
        try:
            await service.enroll(target())
            row = await service.start_investigation("alpha", "Check health")
            await until(lambda: service.investigation(row["investigation_id"])["phase"] == "needs_human")
            result = service.investigation(row["investigation_id"])
            assert result["calls"] == [usage]
            assert result["error"]["code"] == "invalid_ops_json"
        finally:
            await service.shutdown()
    asyncio.run(run())


def make_service(tmp_path, **kwargs):
    return OpsService(tmp_path, store=OpsStore(tmp_path / "store"),
                      investigator=kwargs.pop("investigator", Investigator()),
                      docker_factory=kwargs.pop("docker_factory", Docker()),
                      interval=.005, verification_seconds=.02, verification_timeout=.06,
                      **kwargs)


def test_same_target_deduplicates_and_distinct_targets_queue_sequentially(tmp_path):
    async def run():
        gate = asyncio.Event()
        investigator = Investigator(gate)
        service = make_service(tmp_path, investigator=investigator)
        try:
            await service.enroll(target())
            await service.enroll(target("beta"))
            first = await service.start_investigation("alpha", "Investigate")
            await investigator.entered.wait()
            duplicate = await service.start_investigation("alpha", "Another click")
            second = await service.start_investigation("beta", "Investigate")
            assert first["investigation_id"] == duplicate["investigation_id"]
            assert service.investigation(second["investigation_id"])["phase"] == "queued"
            assert investigator.calls == ["alpha"]
            gate.set()
            await until(lambda: service.investigation(second["investigation_id"])["phase"] == "needs_human")
            assert investigator.calls == ["alpha", "beta"]
            assert investigator.peak == 1
        finally:
            await service.shutdown()
    asyncio.run(run())


def test_missing_provider_never_enqueues_or_dispatches(tmp_path):
    async def run():
        investigator = Investigator()
        investigator.ready = False
        service = make_service(tmp_path, investigator=investigator)
        try:
            await service.enroll(target())
            with pytest.raises(OpsError) as error:
                await service.start_investigation("alpha", "Investigate")
            assert error.value.code == "provider_unavailable"
            assert investigator.calls == []
            assert service.store.list_investigations() == []
        finally:
            await service.shutdown()
    asyncio.run(run())


def test_disabling_automatic_investigation_during_collection_prevents_dispatch(tmp_path, monkeypatch):
    async def run():
        collection_started, release = asyncio.Event(), asyncio.Event()
        class SlowDocker(Docker):
            async def collect(self):
                collection_started.set()
                await release.wait()
                return observed(healthy=False)
        async def available():
            return {"available": True}
        monkeypatch.setattr(service_module, "runtime_info", available)
        investigator = Investigator()
        service = make_service(tmp_path, investigator=investigator, docker_factory=SlowDocker())
        try:
            await service.enroll(target())
            await service.set_policy("alpha", Policy(auto_investigate=True))
            service.bad_streak["alpha"] = 2
            service._monitor_task = asyncio.create_task(service._monitor())
            await collection_started.wait()
            await service.set_policy("alpha", Policy(auto_investigate=False))
            release.set()
            await until(lambda: service.bad_streak["alpha"] >= 3)
            await asyncio.sleep(.02)
            assert investigator.calls == []
            assert service.store.list_investigations() == []
        finally:
            release.set()
            await service.shutdown()
    asyncio.run(run())


def test_queued_monitor_investigation_rechecks_policy_before_model_call(tmp_path):
    async def run():
        gate = asyncio.Event()
        investigator = Investigator(gate)
        service = make_service(tmp_path, investigator=investigator)
        try:
            await service.enroll(target())
            await service.enroll(target("beta"))
            await service.set_policy("beta", Policy(auto_investigate=True))
            await service.start_investigation("alpha", "Manual investigation")
            await investigator.entered.wait()
            queued = await service.start_investigation("beta", "Automatic degradation", trigger="monitor")
            assert queued["phase"] == "queued"
            await service.set_policy("beta", Policy(auto_investigate=False))
            gate.set()
            await until(lambda: service.investigation(queued["investigation_id"])["phase"] != "queued")
            await asyncio.sleep(.01)
            assert investigator.calls == ["alpha"]
        finally:
            gate.set()
            await service.shutdown()
    asyncio.run(run())


def test_immediate_cancel_cannot_requeue_or_dispatch_the_cancelled_investigation(tmp_path):
    async def run():
        investigator = Investigator()
        service = make_service(tmp_path, investigator=investigator)
        try:
            await service.enroll(target())
            row = await service.start_investigation("alpha", "Investigate")
            # Deliberately no event-loop yield between creation and cancellation.
            await service.cancel(row["investigation_id"])
            await asyncio.sleep(.02)
            assert service.investigation(row["investigation_id"])["phase"] == "cancelled"
            assert investigator.calls == []
        finally:
            await service.shutdown()
    asyncio.run(run())


def test_cancel_discards_late_provider_result_and_disconnect_never_stops_target(tmp_path):
    async def run():
        investigator = Investigator(asyncio.Event(), late=True)
        docker = Docker()
        service = make_service(tmp_path, investigator=investigator, docker_factory=docker)
        try:
            await service.enroll(target())
            row = await service.start_investigation("alpha", "Investigate")
            await investigator.entered.wait()
            result = await service.disconnect("alpha")
            final = service.investigation(row["investigation_id"])
            assert final["phase"] == "cancelled"
            assert final["diagnosis"] is None
            assert final["evidence"] == []
            assert service.store.list_proposals() == []
            assert not result["container_stopped"]
            assert not service.target("alpha").monitoring
            assert docker.stops == 0
        finally:
            await service.shutdown()
            assert docker.stops == 0
    asyncio.run(run())


def test_enrollment_cannot_implicitly_grant_automation(tmp_path):
    async def run():
        service = make_service(tmp_path)
        spec = target()
        spec.policy = Policy(automatic_actions=[ActionKind.RESTART])
        try:
            with pytest.raises(OpsError) as error:
                await service.enroll(spec)
            assert error.value.code == "enrollment_observe_only"
            assert service.list_targets() == []
        finally:
            await service.shutdown()
    asyncio.run(run())


def test_startup_reconciles_dispatched_actions_without_reexecution(tmp_path, monkeypatch):
    async def runtime_unavailable():
        raise OpsError("docker_unavailable", "Engine not available", 503)
    monkeypatch.setattr(service_module, "runtime_info", runtime_unavailable)

    async def run():
        service = make_service(tmp_path)
        try:
            await service.enroll(target())
            row = service.store.create_investigation("alpha", "Investigate")
            service.store.update_investigation(row["investigation_id"], phase="executing")
            proposal = service.store.put_proposal({"investigation_id": row["investigation_id"],
                "target_id": "alpha", "status": "dispatching", "action": "restart_container"})
            await service.startup()
            assert service.investigation(row["investigation_id"])["phase"] == "needs_human"
            assert service.store.get_proposal(proposal["proposal_id"])["status"] == "unknown"
            assert service.investigator.calls == []
        finally:
            await service.shutdown()
    asyncio.run(run())


@pytest.mark.parametrize("status,patch_status,phase,action,matched,rebuildable", [
    ("proposed", "tests_passed", "awaiting_approval", "deploy_verified_patch", True, True),
    ("dispatching", "tests_passed", "awaiting_approval", "deploy_verified_patch", True, False),
    ("proposed", "tests_failed", "awaiting_approval", "deploy_verified_patch", True, False),
    ("proposed", "tests_passed", "executing", "deploy_verified_patch", True, False),
    ("proposed", "tests_passed", "awaiting_approval", "restart_container", True, False),
    ("proposed", "tests_passed", "awaiting_approval", "deploy_verified_patch", False, False),
])
def test_restart_preserves_only_never_dispatched_patch_for_new_review(
        tmp_path, monkeypatch, status, patch_status, phase, action, matched, rebuildable):
    async def unavailable():
        raise OpsError("docker_unavailable", "Engine not available", 503)
    monkeypatch.setattr(service_module, "runtime_info", unavailable)

    async def run():
        service = make_service(tmp_path)
        try:
            await service.enroll(target())
            row = service.store.create_investigation("alpha", "Investigate")
            patch = {"patch_id": "validated-patch", "status": patch_status}
            proposal = service.store.put_proposal({
                "investigation_id": row["investigation_id"], "target_id": "alpha",
                "status": status, "action": action,
                "patch_id": patch["patch_id"] if matched else "different-patch"})
            service.store.update_investigation(row["investigation_id"], phase=phase,
                patch=patch, proposal_id=proposal["proposal_id"])
            await service.startup()
            resumed = service.investigation(row["investigation_id"])
            assert resumed["phase"] == ("fix_proposed" if rebuildable else "needs_human")
            assert resumed["patch"] == patch
            assert service.store.get_proposal(proposal["proposal_id"])["status"] == (
                "unknown" if status == "dispatching" else "expired")
            assert service._task is None
            assert service.investigator.calls == []
        finally:
            await service.shutdown()
    asyncio.run(run())


def test_monitor_reports_unavailability_without_inference_or_success(tmp_path, monkeypatch):
    async def unavailable():
        raise OpsError("docker_unavailable", "Engine not available", 503)
    monkeypatch.setattr(service_module, "runtime_info", unavailable)

    async def run():
        docker = Docker(error=OpsError("docker_unavailable", "No engine", 503))
        service = make_service(tmp_path, docker_factory=docker)
        try:
            await service.enroll(target())
            await service.set_policy("alpha", Policy(auto_investigate=True))
            await service.startup()
            await until(lambda: docker.reads > 0)
            assert service.observations["alpha"]["healthy"] is None
            assert not service.docker_status.get("available")
            assert service.docker_status.get("error") or service.docker_status.get("code") or "Checking" not in service.docker_status.get("message", "")
            assert service.investigator.calls == []
        finally:
            await service.shutdown()
    asyncio.run(run())


@pytest.mark.parametrize("defect", ["stale", "missing_identity", "missing_probe", "failed_probe", "memory_pressure"])
def test_verification_rejects_incomplete_stale_or_unhealthy_evidence(tmp_path, defect):
    async def run():
        observation = observed()
        if defect == "stale":
            observation["timestamp"] = "2000-01-01T00:00:00+00:00"
        elif defect == "missing_identity":
            observation.pop("identity")
        elif defect == "missing_probe":
            observation["probes"] = []
        elif defect == "failed_probe":
            observation["probes"][0]["ok"] = False
        else:
            observation["resources"]["memory_bytes"] = 95_000_000
        service = make_service(tmp_path, docker_factory=Docker(observation))
        try:
            await service.enroll(target())
            result = await service.verify(target(), asyncio.Event())
            assert not result["verified"], defect
        finally:
            await service.shutdown()
    asyncio.run(run())


def test_verification_measures_availability_without_claiming_root_cause_fix(tmp_path):
    async def run():
        service = make_service(tmp_path)
        try:
            await service.enroll(target())
            result = await service.verify(target(), asyncio.Event())
            assert result["verified"]
            assert result["duration_seconds"] >= .02
            assert len(result["samples"]) >= 2
            assert result["kind"] == "availability"
            assert result["root_cause_fixed"] is False
        finally:
            await service.shutdown()
    asyncio.run(run())


def test_verification_does_not_certify_a_replaced_image(tmp_path):
    async def run():
        service = make_service(tmp_path, docker_factory=Docker(observed(image="c")))
        try:
            await service.enroll(target())
            result = await service.verify(target(), asyncio.Event(), expected_identity=observed()["identity"])
            assert not result["verified"]
            assert "identity" in result["reason"].lower()
        finally:
            await service.shutdown()
    asyncio.run(run())


@pytest.mark.parametrize("change", ["image", "policy"])
def test_approval_cannot_execute_after_target_or_policy_changed(tmp_path, change):
    async def run():
        docker = Docker()
        service = make_service(tmp_path, docker_factory=docker)
        try:
            spec = target()
            await service.enroll(spec)
            row = service.store.create_investigation("alpha", "Investigate")
            proposal = service.executor.create_proposal(spec, row["investigation_id"],
                {"action": "restart_container", "reason": "Known temporary outage"}, observed())
            service.store.update_investigation(row["investigation_id"], phase="awaiting_approval")
            if change == "image":
                docker.state["identity"]["image_id"] = "sha256:" + "c" * 64
            else:
                await service.set_policy("alpha", Policy(auto_investigate=True))
            await service.decide(proposal["proposal_id"], proposal["proposal_hash"], True)
            await until(lambda: service.investigation(row["investigation_id"])["phase"] == "needs_human")
            final = service.store.get_proposal(proposal["proposal_id"])
            assert final["status"] == "failed"
            assert final["error"] == ("target_changed" if change == "image" else "policy_changed")
        finally:
            await service.shutdown()
    asyncio.run(run())


def test_second_approval_cannot_start_a_parallel_execution(tmp_path):
    async def run():
        gate, entered = asyncio.Event(), asyncio.Event()
        calls = []
        class Executor:
            async def execute(self, proposal, spec, cancel):
                calls.append(spec.target_id)
                entered.set()
                await gate.wait()
                return {"identity": observed()["identity"]}
        service = make_service(tmp_path, executor=Executor())
        try:
            proposals = []
            for name in ("alpha", "beta"):
                await service.enroll(target(name))
                row = service.store.create_investigation(name, "Investigate")
                service.store.update_investigation(row["investigation_id"], phase="awaiting_approval")
                proposals.append(service.store.put_proposal({"target_id": name,
                    "investigation_id": row["investigation_id"], "action": "restart_container",
                    "proposal_hash": "a" * 64}))
            service.store.update_investigation(proposals[0]["investigation_id"],
                error={"code": "interrupted", "message": "Previous attempt was interrupted."})
            await service.decide(proposals[0]["proposal_id"], "a" * 64, True)
            await entered.wait()
            with pytest.raises(OpsError) as error:
                await service.decide(proposals[1]["proposal_id"], "a" * 64, True)
            assert error.value.code == "operation_busy"
            assert calls == ["alpha"]
            gate.set()
            await until(lambda: service.investigation(proposals[0]["investigation_id"])["phase"] == "mitigated")
            assert calls == ["alpha"]
            assert service.investigation(proposals[0]["investigation_id"])["error"] is None
        finally:
            await service.shutdown()
    asyncio.run(run())


def test_diagnosis_of_old_image_cannot_be_rebound_to_new_image(tmp_path):
    async def run():
        gate, entered = asyncio.Event(), asyncio.Event()
        class DelayedDiagnosis(Investigator):
            async def diagnose(self, spec, evidence, cancel, emit, refresh=None):
                entered.set()
                await gate.wait()
                return {"diagnosis": {"summary": "Measured failure", "action": "restart_container",
                    "reason": "Temporary failure in observed version", "needs_source_analysis": False,
                    "findings": [{"hypothesis": "Process requires restart", "confidence": "supported",
                                  "evidence_refs": ["observation.current"]}]}, "evidence": evidence, "calls": []}
        docker = Docker(observed(healthy=False))
        service = make_service(tmp_path, investigator=DelayedDiagnosis(), docker_factory=docker)
        try:
            await service.enroll(target())
            row = await service.start_investigation("alpha", "Investigate")
            await entered.wait()
            docker.state = observed(healthy=True, image="c")
            gate.set()
            await until(lambda: service._task is not None and service._task.done())
            final = service.investigation(row["investigation_id"])
            assert final["phase"] == "needs_human"
            assert service.store.list_proposals(row["investigation_id"]) == []
        finally:
            await service.shutdown()
    asyncio.run(run())
