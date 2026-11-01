"""Offline executor tests: policy binding, trustworthy replay, no blind retries."""
from __future__ import annotations

import asyncio
import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import time

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from aura_ops.actions import ActionExecutor
from aura_ops.docker import CommandResult, DockerTarget
from aura_ops.models import ActionKind, OpsError, TargetSpec, utc_now
from aura_ops.store import OpsStore


CID = "a" * 64
IMAGE = "sha256:" + "b" * 64
OTHER_IMAGE = "sha256:" + "c" * 64
REVISION = "d" * 40


def make_spec(tmp_path, *, compose=False, automatic=()):
    values = {"target_id": "bookmarks", "name": "Bookmarks", "container_name": "aura-bookmarks",
              "docker_context": "aura-local", "probes": [{"url": "http://127.0.0.1:9090/"}],
              "policy": {"automatic_actions": list(automatic)}}
    if compose:
        compose_file = tmp_path / "compose.json"
        compose_file.write_text('{"services":{"bookmarks":{"image":"linkding:pinned"}}}', encoding="utf-8")
        values.update(compose_file=str(compose_file.resolve()), compose_project="aura-lab",
                      compose_service="bookmarks", source_dir=str(tmp_path), source_revision=REVISION,
                      deployment_launch={"entrypoint": ["/usr/local/bin/uwsgi"], "command": ["--http", ":9090"]})
    return TargetSpec(**values)


class FakeRunner:
    def __init__(self):
        self.calls = []
        self.mutations = []
        self.container = {"Id": CID, "Image": IMAGE,
                          "Config": {"Image": "linkding:pinned"},
                          "State": {"StartedAt": "2026-10-28T10:00:00Z", "Running": True, "Status": "running"}}
        self.config = {"name": "aura-lab", "services": {"bookmarks": {
            "container_name": "aura-bookmarks", "image": "linkding:pinned",
            "environment": {"PASSWORD": "never-persist-env-secret"}}}}
        self.provenance = True
        self.current_revision = REVISION
        self.config_calls = 0
        self.before_mutation = None
        self.started = None
        self.pause = None
        self.fail_mutation = False
        self.overrides = []

    def observation(self, healthy=True):
        return {"timestamp": utc_now(), "healthy": healthy,
                "identity": {"container_id": self.container["Id"], "image_id": self.container["Image"],
                             "image_ref": self.container["Config"]["Image"],
                             "started_at": self.container["State"]["StartedAt"]},
                "config": {"memory_limit_bytes": 268435456, "server_port": 9090,
                           "published_ports": {"9090/tcp": [{"host_port": "9090"}]},
                           "PASSWORD": "do-not-save-this"}}

    async def __call__(self, argv, **limits):
        self.calls.append(argv)
        assert argv[:3] == ["docker", "--context", "aura-local"]
        assert limits["max_stdout"] <= 1024 * 1024
        if argv[3:5] == ["container", "inspect"]:
            return CommandResult(0, json.dumps([self.container]))
        if argv[3:5] == ["image", "inspect"]:
            labels = {"org.opencontainers.image.revision": self.current_revision,
                      "aura.migration-free": "true" if self.provenance else "false",
                      "password": "do-not-persist-image-secret"}
            return CommandResult(0, json.dumps([{"Id": argv[-1], "Config": {"Labels": labels}}]))
        if argv[3] == "compose" and "config" in argv:
            self.config_calls += 1
            return CommandResult(0, json.dumps(self.config))
        if argv[3:5] == ["container", "restart"] or (argv[3] == "compose" and "up" in argv):
            self.mutations.append(argv)
            if self.before_mutation:
                self.before_mutation()
            if self.started is not None:
                self.started.set()
            if self.pause is not None:
                await self.pause.wait()
            if self.fail_mutation:
                raise OpsError("docker_timeout", "timeout", 504)
            if argv[3] == "compose":
                files = [argv[index + 1] for index, value in enumerate(argv[:-1]) if value == "-f"]
                override = json.loads(Path(files[-1]).read_text(encoding="utf-8"))
                self.overrides.append(override)
                self.container["Image"] = override["services"]["bookmarks"]["image"]
                self.container["Config"]["Image"] = self.container["Image"]
                self.container["Id"] = "e" * 64
            self.container["State"]["StartedAt"] = utc_now()
            return CommandResult(0, self.container["Id"])
        raise AssertionError(argv)


@pytest.fixture
def store(tmp_path):
    instance = OpsStore(tmp_path / "ops")
    yield instance
    instance.close()


def create(executor, spec, runner, action=ActionKind.RESTART):
    executor.store.put_target(spec.model_dump(mode="json"))
    class UnitDockerTarget(DockerTarget):
        async def collect(self):
            # The unit transport does not issue HTTP. Full CLI/HTTP parsing is
            # exercised separately by test_ops_acceptance's production adapter.
            return runner.observation(healthy=False)
    executor.docker_factory = UnitDockerTarget
    return executor.create_proposal(spec, "investigation-1", {
        "action": action.value, "reason": "Measured errors support the proposed bounded operation."},
        runner.observation(healthy=False))


def claim(store, proposal, authorization="user"):
    return store.transition_proposal(proposal["proposal_id"], "proposed", "executing",
                                     authorization=authorization,
                                     approved_hash=proposal["proposal_hash"] if authorization == "user" else None)


async def expect_error(code, coroutine):
    with pytest.raises(OpsError) as caught:
        await coroutine
    assert caught.value.code == code


def test_snapshot_is_known_good_bound_and_does_not_store_raw_environment(store, tmp_path):
    async def scenario():
        spec, runner = make_spec(tmp_path, compose=True), FakeRunner()
        (tmp_path / ".env").write_text("PASSWORD=secret-file-value", encoding="utf-8")
        executor = ActionExecutor(store, runner=runner)
        snapshot = await executor.capture_snapshot(spec, runner.observation())
        assert snapshot == store.get_setting("snapshot.bookmarks")
        serialized = json.dumps(snapshot)
        for secret in ("never-persist-env-secret", "secret-file-value", "do-not-save-this", "do-not-persist-image-secret"):
            assert secret not in serialized
        assert len(snapshot["compose"]["compose_sha256"]) == 64
        assert len(snapshot["compose"]["env_sha256"]) == 64
        assert len(snapshot["compose"]["resolved_sha256"]) == 64
        assert snapshot["provenance"]["verified"] is True
        await expect_error("baseline_unhealthy", executor.capture_snapshot(spec, runner.observation(False)))
        old = runner.observation()
        old["timestamp"] = (datetime.now(timezone.utc) - timedelta(minutes=3)).isoformat()
        await expect_error("stale_observation", executor.capture_snapshot(spec, old))
        assert not runner.mutations
    asyncio.run(scenario())


def test_snapshot_rejects_wrong_service_image_and_missing_explicit_container(store, tmp_path):
    async def scenario():
        spec, runner = make_spec(tmp_path, compose=True), FakeRunner()
        executor = ActionExecutor(store, runner=runner)
        runner.config["services"]["bookmarks"]["image"] = "unrelated:latest"
        await expect_error("compose_image_mismatch", executor.capture_snapshot(spec, runner.observation()))
        runner.config["services"]["bookmarks"].pop("container_name")
        await expect_error("compose_target_mismatch", executor.capture_snapshot(spec, runner.observation()))
        assert store.get_setting("snapshot.bookmarks") is None
    asyncio.run(scenario())


def test_restart_requires_claim_and_authorization_then_persists_dispatch_and_cooldown(store, tmp_path):
    async def scenario():
        spec, runner = make_spec(tmp_path, automatic=[ActionKind.RESTART]), FakeRunner()
        executor = ActionExecutor(store, runner=runner)
        proposal = create(executor, spec, runner)
        assert proposal["automation_eligible"] is True
        await expect_error("action_not_claimed", executor.execute(proposal, spec, asyncio.Event()))
        claimed = claim(store, proposal, "policy")
        result = await executor.execute(claimed, spec, asyncio.Event())
        assert result["command_completed"] is True and result["verified"] is False
        assert len(runner.mutations) == 1
        assert runner.mutations[0][-1] == CID
        assert store.get_proposal(proposal["proposal_id"])["status"] == "dispatching"
        assert store.get_setting("lastrestart.bookmarks") <= time.time()
        await expect_error("action_not_claimed", executor.execute(claimed, spec, asyncio.Event()))
        second = claim(store, create(executor, spec, runner), "policy")
        await expect_error("restart_cooldown", executor.execute(second, spec, asyncio.Event()))
        assert len(runner.mutations) == 1
    asyncio.run(scenario())


@pytest.mark.parametrize("authorization,approved_hash", [("policy", None), ("user", "0" * 64), (None, None)])
def test_empty_policy_cannot_be_bypassed_by_claim_only(store, tmp_path, authorization, approved_hash):
    async def scenario():
        spec, runner = make_spec(tmp_path), FakeRunner()
        executor = ActionExecutor(store, runner=runner)
        proposal = create(executor, spec, runner)
        assert proposal["automation_eligible"] is False
        claimed = store.transition_proposal(proposal["proposal_id"], "proposed", "executing",
                                            authorization=authorization, approved_hash=approved_hash)
        await expect_error("action_not_authorized", executor.execute(claimed, spec, asyncio.Event()))
        assert not runner.mutations
    asyncio.run(scenario())


def test_proposal_hash_policy_and_target_identity_are_revalidated(store, tmp_path):
    async def scenario():
        spec, runner = make_spec(tmp_path), FakeRunner()
        executor = ActionExecutor(store, runner=runner)
        proposal = claim(store, create(executor, spec, runner))
        changed = copy.deepcopy(proposal)
        changed["expected_identity"]["container_id"] = "f" * 64
        await expect_error("proposal_changed", executor.execute(changed, spec, asyncio.Event()))
        changed_spec = spec.model_copy(update={"monitoring": False})
        await expect_error("policy_changed", executor.execute(proposal, changed_spec, asyncio.Event()))
        runner.container["State"]["StartedAt"] = utc_now()
        await expect_error("target_changed", executor.execute(proposal, spec, asyncio.Event()))
        assert not runner.mutations
    asyncio.run(scenario())


def test_configuration_restore_replays_only_trusted_base_and_immutable_image(store, tmp_path):
    async def scenario():
        spec, runner = make_spec(tmp_path, compose=True, automatic=[ActionKind.RESTORE]), FakeRunner()
        executor = ActionExecutor(store, runner=runner)
        await executor.capture_snapshot(spec, runner.observation())
        proposal = claim(store, create(executor, spec, runner, ActionKind.RESTORE), "policy")
        result = await executor.execute(proposal, spec, asyncio.Event())
        assert result["verified"] is False
        assert result["identity"]["image_id"] == IMAGE
        assert len(runner.mutations) == 1
        argv = runner.mutations[0]
        assert ["--no-deps", "--no-build", "--pull", "never"] == argv[-5:-1]
        assert argv[-1] == "bookmarks"
        assert spec.compose_file in argv
        assert runner.overrides == [{"services": {"bookmarks": {
            "image": IMAGE, **spec.deployment_launch.model_dump(mode="json")}}}]
        assert not list(executor.work_dir.glob("override-*"))
    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["base", "env", "resolved"])
def test_configuration_drift_never_dispatches_a_mutation(store, tmp_path, change):
    async def scenario():
        spec, runner = make_spec(tmp_path, compose=True), FakeRunner()
        executor = ActionExecutor(store, runner=runner)
        await executor.capture_snapshot(spec, runner.observation())
        proposal = claim(store, create(executor, spec, runner, ActionKind.RESTORE))
        if change == "base":
            Path(spec.compose_file).write_text("different config", encoding="utf-8")
        elif change == "env":
            (tmp_path / ".env").write_text("PORT=9091", encoding="utf-8")
        else:
            # Included files/host env can change the resolved result even when
            # the enrolled base file and adjacent .env did not change.
            runner.config["services"]["bookmarks"]["environment"]["PORT"] = "9091"
        await expect_error("config_changed", executor.execute(proposal, spec, asyncio.Event()))
        assert not runner.mutations
    asyncio.run(scenario())


def test_snapshot_changes_and_image_changes_require_a_new_proposal(store, tmp_path):
    async def scenario():
        spec, runner = make_spec(tmp_path, compose=True), FakeRunner()
        executor = ActionExecutor(store, runner=runner)
        await executor.capture_snapshot(spec, runner.observation())
        proposal = claim(store, create(executor, spec, runner, ActionKind.RESTORE))
        await executor.capture_snapshot(spec, runner.observation())
        await expect_error("snapshot_changed", executor.execute(proposal, spec, asyncio.Event()))
        runner.container["Image"] = OTHER_IMAGE
        changed_image = claim(store, create(executor, spec, runner, ActionKind.RESTORE))
        assert changed_image["blocked_reason"] == "restore_cannot_change_image"
        await expect_error("restore_cannot_change_image", executor.execute(changed_image, spec, asyncio.Event()))
        assert not runner.mutations
    asyncio.run(scenario())


def test_rollback_requires_matching_source_and_migration_free_provenance(store, tmp_path):
    async def scenario():
        spec, runner = make_spec(tmp_path, compose=True), FakeRunner()
        executor = ActionExecutor(store, runner=runner)
        await executor.capture_snapshot(spec, runner.observation())
        runner.container["Image"] = OTHER_IMAGE
        runner.container["Config"]["Image"] = "linkding:new-build"
        proposal = claim(store, create(executor, spec, runner, ActionKind.ROLLBACK))
        runner.current_revision = "f" * 40
        await expect_error("rollback_provenance_required", executor.execute(proposal, spec, asyncio.Event()))
        assert not runner.mutations
        runner.current_revision = REVISION
        runner.provenance = False
        await expect_error("rollback_provenance_required", executor.execute(proposal, spec, asyncio.Event()))
        runner.provenance = True
        result = await executor.execute(proposal, spec, asyncio.Event())
        assert result["identity"]["image_id"] == IMAGE
        assert result["verified"] is False
        assert runner.overrides[-1]["services"]["bookmarks"]["entrypoint"] == ["/usr/local/bin/uwsgi"]
        assert runner.overrides[-1]["services"]["bookmarks"]["command"] == ["--http", ":9090"]
    asyncio.run(scenario())


def test_cancel_before_dispatch_never_changes_application_or_cooldown(store, tmp_path):
    async def scenario():
        spec, runner = make_spec(tmp_path), FakeRunner()
        executor = ActionExecutor(store, runner=runner)
        proposal = claim(store, create(executor, spec, runner))
        cancellation = asyncio.Event()
        cancellation.set()
        with pytest.raises(asyncio.CancelledError):
            await executor.execute(proposal, spec, cancellation)
        assert not runner.calls
        assert store.get_setting("lastrestart.bookmarks") is None
    asyncio.run(scenario())


@pytest.mark.parametrize("action", [ActionKind.RESTORE, ActionKind.ROLLBACK])
def test_source_bound_compose_replay_requires_explicit_nonmigration_launch(store, tmp_path, action):
    async def scenario():
        spec, runner = make_spec(tmp_path, compose=True), FakeRunner()
        spec.deployment_launch = None
        executor = ActionExecutor(store, runner=runner)
        await executor.capture_snapshot(spec, runner.observation())
        proposal = create(executor, spec, runner, action)
        assert proposal["blocked_reason"] == "deployment_launch_required"
        assert proposal["automation_eligible"] is False
        await expect_error("deployment_launch_required", executor.execute(claim(store, proposal), spec, asyncio.Event()))
        assert not runner.mutations and not runner.overrides
    asyncio.run(scenario())


def test_non_source_config_restore_keeps_administrator_startup_behavior(store, tmp_path):
    async def scenario():
        spec, runner = make_spec(tmp_path, compose=True), FakeRunner()
        spec.source_dir = None
        spec.source_revision = None
        spec.deployment_launch = None
        executor = ActionExecutor(store, runner=runner)
        await executor.capture_snapshot(spec, runner.observation())
        proposal = claim(store, create(executor, spec, runner, ActionKind.RESTORE))
        await executor.execute(proposal, spec, asyncio.Event())
        assert runner.overrides == [{"services": {"bookmarks": {"image": IMAGE}}}]
    asyncio.run(scenario())


def test_restart_of_source_bound_target_does_not_require_launch_profile(store, tmp_path):
    async def scenario():
        spec, runner = make_spec(tmp_path, compose=True), FakeRunner()
        spec.deployment_launch = None
        executor = ActionExecutor(store, runner=runner)
        proposal = claim(store, create(executor, spec, runner, ActionKind.RESTART))
        await executor.execute(proposal, spec, asyncio.Event())
        assert len(runner.mutations) == 1
        assert runner.mutations[0][3:5] == ["container", "restart"]
        assert not runner.overrides
    asyncio.run(scenario())


def test_cancel_after_dispatch_is_unknown_and_is_not_retried(store, tmp_path):
    async def scenario():
        spec, runner = make_spec(tmp_path), FakeRunner()
        runner.started, runner.pause = asyncio.Event(), asyncio.Event()
        executor = ActionExecutor(store, runner=runner)
        proposal = claim(store, create(executor, spec, runner))
        cancellation = asyncio.Event()
        operation = asyncio.create_task(executor.execute(proposal, spec, cancellation))
        await asyncio.wait_for(runner.started.wait(), timeout=1)
        cancellation.set()
        await expect_error("action_outcome_unknown", operation)
        assert len(runner.mutations) == 1
        assert store.get_setting("lastrestart.bookmarks") is not None
        assert store.get_proposal(proposal["proposal_id"])["status"] == "dispatching"
        await expect_error("action_not_claimed", executor.execute(proposal, spec, asyncio.Event()))
    asyncio.run(scenario())


def test_transport_timeout_after_dispatch_retains_cooldown(store, tmp_path):
    async def scenario():
        spec, runner = make_spec(tmp_path), FakeRunner()
        runner.fail_mutation = True
        executor = ActionExecutor(store, runner=runner)
        proposal = claim(store, create(executor, spec, runner))
        await expect_error("action_outcome_unknown", executor.execute(proposal, spec, asyncio.Event()))
        assert len(runner.mutations) == 1
        assert store.get_setting("lastrestart.bookmarks") is not None
    asyncio.run(scenario())


def test_concurrent_execution_of_same_proposal_dispatches_once(store, tmp_path):
    async def scenario():
        spec, runner = make_spec(tmp_path), FakeRunner()
        executor = ActionExecutor(store, runner=runner)
        proposal = claim(store, create(executor, spec, runner))
        results = await asyncio.gather(executor.execute(proposal, spec, asyncio.Event()),
                                       executor.execute(proposal, spec, asyncio.Event()), return_exceptions=True)
        assert sum(isinstance(result, dict) for result in results) == 1
        assert len(runner.mutations) == 1
        failure = next(result for result in results if isinstance(result, BaseException))
        assert isinstance(failure, OpsError)
        assert failure.code in ("restart_cooldown", "action_not_claimed", "target_changed", "action_already_claimed")
    asyncio.run(scenario())


def test_unverified_deployment_and_missing_snapshot_fail_closed(store, tmp_path):
    async def scenario():
        spec, runner = make_spec(tmp_path), FakeRunner()
        executor = ActionExecutor(store, runner=runner)
        deploy = claim(store, create(executor, spec, runner, ActionKind.DEPLOY))
        assert deploy["automation_eligible"] is False
        await expect_error("verified_patch_required", executor.execute(deploy, spec, asyncio.Event()))
        restore = claim(store, create(executor, spec, runner, ActionKind.RESTORE))
        await expect_error("snapshot_required", executor.execute(restore, spec, asyncio.Event()))
        assert not runner.mutations
    asyncio.run(scenario())
