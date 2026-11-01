"""Offline OPS acceptance through production orchestration and Docker parsing.

Docker CLI, HTTP responses and Open1026 completions are explicitly mocked. These
tests establish lifecycle/contracts, not actual Docker or model acceptance.
"""
from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
import sys

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from aura_agents.adapter import CallResult, MODEL
from aura_ops.actions import ActionExecutor
from aura_ops.docker import CommandResult, DockerTarget
from aura_ops.investigator import OpsInvestigator
from aura_ops.models import ActionKind, OpsError, Policy, TargetSpec, utc_now
from aura_ops.service import OpsService
from aura_ops.store import OpsStore


IMAGE = "sha256:" + "b" * 64


class MockDockerTransport:
    """Only command boundary is replaced; actual DockerTarget parses output."""

    def __init__(self):
        self.containers = {}
        self.calls = []
        self.mutations = []
        self.http_reads = []
        self.active_mutations = 0
        self.max_mutations = 0
        self.inspect_entered = None
        self.inspect_release = None
        self.fail_restart = False
        self.memory_usage = "32MiB / 256MiB"

    def add(self, spec):
        index = len(self.containers) + 1
        self.containers[spec.container_name] = {
            "Id": f"{index:064x}", "Image": IMAGE, "RestartCount": 0,
            "Config": {"Image": "mock-app:pinned", "Env": ["PASSWORD=must-never-appear"]},
            "HostConfig": {"Memory": 268435456},
            "State": {"StartedAt": "2026-10-28T00:00:00Z", "Running": True,
                      "Status": "running", "ExitCode": 0, "OOMKilled": False},
            "mock_healthy": False,
        }

    def container(self, reference):
        return next(value for key, value in self.containers.items()
                    if reference in (key, value["Id"]))

    async def http(self, request):
        name = request.url.path.strip("/")
        status = 200 if self.containers[name]["mock_healthy"] else 503
        self.http_reads.append((name, status))
        return httpx.Response(status, content=b"mock health response")

    async def __call__(self, argv, **bounds):
        self.calls.append(list(argv))
        assert argv[:3] == ["docker", "--context", "mock-context"]
        assert bounds["max_stdout"] <= 1024 * 1024
        command = argv[3:5]
        if command == ["container", "inspect"]:
            if self.inspect_entered is not None:
                entered, release = self.inspect_entered, self.inspect_release
                self.inspect_entered = None
                entered.set()
                await release.wait()
            return CommandResult(0, json.dumps([self.container(argv[-1])]))
        if command == ["container", "stats"]:
            return CommandResult(0, json.dumps({"MemUsage": self.memory_usage, "CPUPerc": "2.5%"}))
        if command == ["container", "logs"]:
            return CommandResult(0, "2026-10-28T00:00:01Z mock request failed password=hidden-value\n")
        if command == ["container", "restart"]:
            self.mutations.append(list(argv))
            self.active_mutations += 1
            self.max_mutations = max(self.max_mutations, self.active_mutations)
            try:
                await asyncio.sleep(.002)
                container = self.container(argv[-1])
                container["State"]["StartedAt"] = utc_now()
                container["mock_healthy"] = True
                if self.fail_restart:
                    raise OpsError("docker_timeout", "Mock transport lost the acknowledgement.", 504)
                return CommandResult(0, container["Id"])
            finally:
                self.active_mutations -= 1
        raise AssertionError(f"Unexpected command; no application stop is allowed: {argv}")


class MockOpen1026:
    """Fixed synthetic provider answers through real OpsInvestigator validation."""

    ready = True

    def __init__(self):
        self.calls = []
        self.closed = False
        self.synthesis_hook = None
        self.entered = None
        self.release = None
        self.ignore_cancellation = False
        self.force_restart = False

    async def complete(self, messages, **options):
        assert options["max_tokens"] == 4096
        packet = json.loads(messages[1]["content"].removeprefix("EVIDENCE DATA:\n"))
        self.calls.append((messages[0]["content"], copy.deepcopy(packet)))
        if self.entered is not None:
            self.entered.set()
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                if not self.ignore_cancellation:
                    raise
        observation = packet["observation.current"]
        healthy = observation["healthy"]
        result = {
            "summary": "Mock evidence: HTTP health is healthy." if healthy else "Mock evidence: HTTP requests fail.",
            "findings": [{"hypothesis": "The enrolled HTTP operation returned a measured status.",
                          "evidence_refs": ["observation.current"], "confidence": "supported"}],
        }
        if "Synthesize the evidence" in messages[0]["content"]:
            result.update(action="abstain" if healthy and not self.force_restart else "restart_container",
                          reason="Continue observing." if healthy else "Restart for temporary availability only.",
                          needs_source_analysis=False)
            if self.synthesis_hook:
                self.synthesis_hook()
        else:
            result.update(request_sources=[], request_refresh=False)
        return CallResult(json.dumps(result), None, None, MODEL, "stop", .1, 1)

    async def aclose(self):
        self.closed = True


class Harness:
    def __init__(self, root):
        self.transport = MockDockerTransport()
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(self.transport.http))
        self.store = OpsStore(root / "ops")
        self.adapter = MockOpen1026()

        def factory(spec, runner=None):
            return DockerTarget(spec, runner=runner or self.transport, http_client=self.client)

        self.factory = factory
        self.executor = ActionExecutor(self.store, docker_factory=factory, runner=self.transport)
        self.service = OpsService(root, store=self.store, docker_factory=factory,
                                  investigator=OpsInvestigator(self.adapter), executor=self.executor,
                                  interval=.002, verification_seconds=.008, verification_timeout=.2)

    async def add(self, name="bookmarks", *, automatic=False, healthy=False):
        spec = TargetSpec(target_id=name, name=name.title(), container_name=name,
                          docker_context="mock-context", probes=[{"url": f"http://mock.test/{name}"}])
        self.transport.add(spec)
        self.transport.containers[name]["mock_healthy"] = healthy
        await self.service.enroll(spec)
        if automatic:
            await self.service.set_policy(name, Policy(automatic_actions=[ActionKind.RESTART]))
            spec = self.service.target(name)
        return spec

    async def close(self):
        await self.service.shutdown()
        await self.client.aclose()


async def settled(harness, investigation_id, phases):
    async def poll():
        while True:
            row = harness.store.get_investigation(investigation_id)
            if row["phase"] in phases and (harness.service._task is None or harness.service._task.done()):
                return row
            await asyncio.sleep(.001)
    return await asyncio.wait_for(poll(), 2)


def test_offline_diagnosis_approval_single_dispatch_and_measured_availability(tmp_path):
    async def scenario():
        harness = Harness(tmp_path)
        try:
            spec = await harness.add()
            row = await harness.service.start_investigation(spec.target_id, "Why are requests failing?")
            row = await settled(harness, row["investigation_id"], {"awaiting_approval"})
            proposal = harness.store.get_proposal(row["proposal_id"])
            assert not harness.transport.mutations
            assert len(row["calls"]) == 3  # two evidence roles plus synthesis
            assert all(call["requested_model"] == MODEL for call in row["calls"])
            assert row["evidence"]["observation.current"]["probes"][0]["status_code"] == 503
            with pytest.raises(OpsError, match="changed"):
                await harness.service.decide(proposal["proposal_id"], "0" * 64, True)
            assert not harness.transport.mutations
            await harness.service.decide(proposal["proposal_id"], proposal["proposal_hash"], True)
            row = await settled(harness, row["investigation_id"], {"mitigated", "needs_human"})
            assert row["phase"] == "mitigated", row
            verification = row["verification"]
            assert verification["verified"] and verification["root_cause_fixed"] is False
            assert all(sample["probes"][0]["status_code"] == 200 for sample in verification["samples"])
            assert verification["samples"][-1]["resources"]["memory_bytes"] == 32 * 1024**2
            assert len(harness.transport.mutations) == 1
            with pytest.raises(OpsError):
                await harness.service.decide(proposal["proposal_id"], proposal["proposal_hash"], True)
            assert len(harness.transport.mutations) == 1
            persisted = json.dumps(harness.store.events_since(0, limit=1000))
            assert "must-never-appear" not in persisted and "hidden-value" not in persisted
        finally:
            await harness.close()
        assert harness.adapter.closed
        assert harness.transport.containers["bookmarks"]["State"]["Running"]
        assert all(call[3:5] not in (["container", "stop"], ["container", "rm"]) for call in harness.transport.calls)
    asyncio.run(scenario())


def test_identity_drift_after_model_prevents_proposal_and_action(tmp_path):
    async def scenario():
        harness = Harness(tmp_path)
        try:
            spec = await harness.add(automatic=True)
            harness.adapter.synthesis_hook = lambda: harness.transport.containers["bookmarks"].update(Image="sha256:" + "c" * 64)
            row = await harness.service.start_investigation(spec.target_id, "Inspect failure")
            row = await settled(harness, row["investigation_id"], {"needs_human"})
            assert row["error"]["code"] == "target_changed"
            assert not harness.store.list_proposals() and not harness.transport.mutations
        finally:
            await harness.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["policy", "disconnect", "remove"])
def test_policy_is_rechecked_after_awaited_preflight_before_dispatch(tmp_path, change):
    async def scenario():
        harness = Harness(tmp_path)
        try:
            spec = await harness.add(automatic=True)
            observation = await harness.factory(spec).collect()
            proposal = harness.executor.create_proposal(spec, "mock-investigation", {
                "action": "restart_container", "reason": "Measured failed HTTP operation."}, observation)
            harness.store.transition_proposal(proposal["proposal_id"], "proposed", "executing", authorization="policy")
            entered, release = asyncio.Event(), asyncio.Event()
            harness.transport.inspect_entered, harness.transport.inspect_release = entered, release
            operation = asyncio.create_task(harness.executor.execute(proposal, spec, asyncio.Event()))
            await asyncio.wait_for(entered.wait(), 1)
            if change == "policy":
                await harness.service.set_policy(spec.target_id, Policy())
            elif change == "disconnect":
                await harness.service.disconnect(spec.target_id)
            else:
                harness.store.delete_target(spec.target_id)
            release.set()
            with pytest.raises(OpsError):
                await operation
            assert not harness.transport.mutations
        finally:
            await harness.close()
    asyncio.run(scenario())


def test_late_mock_provider_result_after_cancel_never_creates_action(tmp_path):
    async def scenario():
        harness = Harness(tmp_path)
        try:
            spec = await harness.add(automatic=True)
            harness.adapter.entered, harness.adapter.release = asyncio.Event(), asyncio.Event()
            harness.adapter.ignore_cancellation = True
            row = await harness.service.start_investigation(spec.target_id, "Inspect failure")
            await asyncio.wait_for(harness.adapter.entered.wait(), 1)
            await asyncio.wait_for(harness.service.cancel(row["investigation_id"]), 1)
            assert harness.store.get_investigation(row["investigation_id"])["phase"] == "cancelled"
            assert not harness.store.list_proposals() and not harness.transport.mutations
            assert harness.transport.containers["bookmarks"]["State"]["Running"]
        finally:
            await harness.close()
    asyncio.run(scenario())


def test_three_enrolled_apps_are_serialized_through_automatic_actions(tmp_path):
    async def scenario():
        harness = Harness(tmp_path)
        try:
            specs = [await harness.add(name, automatic=True) for name in ("bookmarks", "checks", "notes")]
            rows = [await harness.service.start_investigation(spec.target_id, "Inspect failure") for spec in specs]
            async def complete_all():
                while any(harness.store.get_investigation(row["investigation_id"])["phase"] != "mitigated" for row in rows):
                    await asyncio.sleep(.002)
            await asyncio.wait_for(complete_all(), 3)
            assert len(harness.transport.mutations) == 3
            assert harness.transport.max_mutations == 1
            events = harness.store.events_since(0, limit=1000)
            active = None
            for event in events:
                if event["type"] != "investigation_changed":
                    continue
                row = event["payload"]
                if row["phase"] == "investigating":
                    assert active is None
                    active = row["investigation_id"]
                if row["phase"] == "mitigated":
                    assert active == row["investigation_id"]
                    active = None
            assert active is None
        finally:
            await harness.close()
    asyncio.run(scenario())


def test_unknown_restart_ack_is_not_retried_or_claimed_recovered(tmp_path):
    async def scenario():
        harness = Harness(tmp_path)
        try:
            spec = await harness.add(automatic=True)
            harness.transport.fail_restart = True
            row = await harness.service.start_investigation(spec.target_id, "Inspect failure")
            row = await settled(harness, row["investigation_id"], {"needs_human"})
            assert row["error"]["code"] == "action_outcome_unknown"
            assert harness.store.list_proposals()[0]["status"] == "unknown"
            assert len(harness.transport.mutations) == 1
            assert not (row.get("verification") or {}).get("verified")
            assert harness.transport.containers["bookmarks"]["mock_healthy"]  # mutation did happen
            await asyncio.sleep(.02)
            assert len(harness.transport.mutations) == 1
        finally:
            await harness.close()
    asyncio.run(scenario())


def test_healthy_target_never_creates_proposal_with_real_investigator(tmp_path):
    async def scenario():
        harness = Harness(tmp_path)
        try:
            spec = await harness.add(automatic=True, healthy=True)
            row = await harness.service.start_investigation(spec.target_id, "Inspect health")
            row = await settled(harness, row["investigation_id"], {"needs_human"})
            assert row["diagnosis"]["action"] == "abstain"
            assert not harness.transport.mutations and not harness.store.list_proposals()
        finally:
            await harness.close()
    asyncio.run(scenario())


def test_unknown_source_image_provenance_never_supplies_source_to_model(tmp_path):
    async def scenario():
        harness = Harness(tmp_path)
        try:
            spec = await harness.add()
            # Deliberately nonexistent source root: provenance must reject BEFORE reading it.
            spec.source_dir = str(tmp_path / "not-the-running-image")
            spec.source_revision = "d" * 40
            evidence = {"observation.current": await harness.factory(spec).collect()}
            events = []
            async def emit(kind, data):
                events.append((kind, data))
            result = await harness.service.investigator.diagnose(spec, evidence, asyncio.Event(), emit)
            assert result["evidence"]["source.unavailable"]["code"] == "source_image_unverified"
            assert "source.index" not in result["evidence"]
            assert len(result["calls"]) == 3
            assert all(call["agent_id"] != "source" for call in result["calls"])
        finally:
            await harness.close()
    asyncio.run(scenario())


def test_schema_valid_hostile_synthesis_cannot_automatically_restart_healthy_app(tmp_path):
    async def scenario():
        harness = Harness(tmp_path)
        try:
            spec = await harness.add(automatic=True, healthy=True)
            harness.adapter.force_restart = True
            row = await harness.service.start_investigation(spec.target_id, "Inspect health")
            row = await settled(harness, row["investigation_id"], {"needs_human", "observed"})
            assert not harness.transport.mutations
            assert not (row.get("verification") or {}).get("verified")
            assert row["diagnosis"]["action"] == "restart_container"
        finally:
            await harness.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("memory", ["32MiB / 256MiB", "unavailable"])
def test_executor_rechecks_measured_healthy_app_before_automatic_dispatch(tmp_path, memory):
    async def scenario():
        harness = Harness(tmp_path)
        try:
            spec = await harness.add(automatic=True)
            observation = await harness.factory(spec).collect()
            proposal = harness.executor.create_proposal(spec, "mock-investigation", {
                "action": "restart_container", "reason": "Prior requests failed."}, observation)
            harness.store.transition_proposal(proposal["proposal_id"], "proposed", "executing", authorization="policy")
            harness.transport.containers["bookmarks"]["mock_healthy"] = True
            harness.transport.memory_usage = memory
            with pytest.raises(OpsError) as caught:
                await harness.executor.execute(proposal, spec, asyncio.Event())
            assert caught.value.code == "automatic_action_unnecessary"
            assert not harness.transport.mutations
        finally:
            await harness.close()
    asyncio.run(scenario())


def test_fresh_memory_pressure_can_authorize_policy_action_despite_healthy_http(tmp_path):
    async def scenario():
        harness = Harness(tmp_path)
        try:
            spec = await harness.add(automatic=True, healthy=True)
            harness.transport.memory_usage = "240MiB / 256MiB"
            observation = await harness.factory(spec).collect()
            proposal = harness.executor.create_proposal(spec, "mock-investigation", {
                "action": "restart_container", "reason": "Measured memory pressure."}, observation)
            harness.store.transition_proposal(proposal["proposal_id"], "proposed", "executing", authorization="policy")
            result = await harness.executor.execute(proposal, spec, asyncio.Event())
            assert result["command_completed"] and not result["verified"]
            assert len(harness.transport.mutations) == 1
        finally:
            await harness.close()
    asyncio.run(scenario())
