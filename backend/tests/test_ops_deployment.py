"""Reviewed image/deployment pipeline with real patch artifacts and fake Docker."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from pathlib import Path
import shlex
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from aura_ops.actions import ActionExecutor
from aura_ops.deployment import DeploymentManager
from aura_ops.docker import CommandResult
from aura_ops.models import DeploymentLaunch, OpsError, utc_now
from aura_ops.patches import PatchEngine
from aura_ops.store import OpsStore
from test_ops_patches import Adapter, IMAGE, REVISION, Runner, investigation, setup_source, target


BASE_IMAGE = "sha256:" + "c" * 64
CANDIDATE_IMAGE = "sha256:" + "d" * 64
BASE_LAYER = "sha256:" + "e" * 64
CID = "f" * 64


class DeploymentRunner(Runner):
    def __init__(self):
        super().__init__()
        self.images = {IMAGE: {"Id": IMAGE, "Os": "linux"}, BASE_IMAGE: {
            "Id": BASE_IMAGE, "Os": "linux", "RootFS": {"Type": "layers", "Layers": [BASE_LAYER]},
            "Config": {"Entrypoint": ["/bootstrap.sh"], "Cmd": ["server"], "Env": ["APP_ENV=production"],
                       "WorkingDir": "/etc/linkding", "User": "1000", "Volumes": {"/etc/linkding/data": {}},
                       "OnBuild": None, "Labels": {"org.opencontainers.image.revision": REVISION,
                                                    "aura.migration-free": "true"}}}}
        self.app = {"Id": CID, "Image": BASE_IMAGE, "Config": {"Image": "target:base"},
                    "State": {"StartedAt": "2026-10-28T01:00:00Z", "Running": True, "Status": "running"},
                    "Mounts": [{"Destination": "/etc/linkding/data"}]}
        self.compose = {"name": "aura-lab", "services": {"app": {
            "container_name": "test-app", "image": "target:base", "environment": {"PASSWORD": "private-config"}}}}
        self.app_mutations = []
        self.replay_services = []
        self.built_files = {}
        self.builds = []
        self.tag_operations = []
        self.tamper_candidate = None
        self.app_started = None
        self.app_pause = None
        self.bad_deploy_image = False

    def observation(self):
        return {"timestamp": utc_now(), "healthy": True,
                "identity": {"container_id": self.app["Id"], "image_id": self.app["Image"],
                             "image_ref": self.app["Config"]["Image"], "started_at": self.app["State"]["StartedAt"]},
                "config": {"memory_limit_bytes": 268435456, "server_port": 9090, "published_ports": {}}}

    async def __call__(self, argv, **limits):
        command = argv[3:]
        if command[:2] == ["image", "inspect"]:
            self.calls.append((argv, limits))
            image = self.images.get(command[-1])
            return CommandResult(0, json.dumps([image])) if image else CommandResult(1, "", "No such image")
        if command[:2] == ["image", "tag"]:
            self.calls.append((argv, limits))
            self.tag_operations.append(command)
            self.images[command[-1]] = self.images[command[-2]]
            return CommandResult(0, "")
        if command[:2] == ["container", "inspect"] and command[-1] == "test-app":
            self.calls.append((argv, limits))
            return CommandResult(0, json.dumps([self.app]))
        if command[0] == "compose":
            self.calls.append((argv, limits))
            if "config" in command:
                return CommandResult(0, json.dumps(self.compose))
            assert "up" in command
            self.app_mutations.append(argv)
            if self.app_started:
                self.app_started.set()
            if self.app_pause:
                await self.app_pause.wait()
            paths = [command[i + 1] for i, arg in enumerate(command[:-1]) if arg == "-f"]
            service = json.loads(Path(paths[-1]).read_text())["services"]["app"]
            self.replay_services.append(service)
            image_id = service["image"]
            self.app["Image"] = BASE_IMAGE if self.bad_deploy_image else image_id
            self.app["Config"]["Image"] = self.app["Image"]
            self.app["Id"] = f"{len(self.app_mutations) + 10:064x}"
            self.app["State"]["StartedAt"] = utc_now()
            return CommandResult(0, "started")
        if command[0] == "build":
            self.calls.append((argv, limits))
            self.builds.append((command, limits))
            context = Path(command[-1])
            self.built_files = {path.relative_to(context).as_posix(): path.read_bytes()
                                for path in context.rglob("*") if path.is_file()}
            lines = (context / "Dockerfile").read_text().splitlines()
            assert lines[0].startswith("FROM aura-reviewed-base:")
            assert all(line.startswith(("FROM ", "COPY ", "LABEL ")) for line in lines)
            base = self.images[lines[0].split(" ", 1)[1]]
            image = copy.deepcopy(base)
            image["Id"] = CANDIDATE_IMAGE
            image["RootFS"]["Layers"].append("sha256:" + "a" * 64)
            label_values = shlex.split(next(line[6:] for line in lines if line.startswith("LABEL ")))
            image["Config"]["Labels"].update(dict(pair.split("=", 1) for pair in label_values))
            if self.tamper_candidate:
                self.tamper_candidate(image)
            self.images[CANDIDATE_IMAGE] = image
            self.images[command[command.index("--tag") + 1]] = image
            return CommandResult(0, "built")
        return await super().__call__(argv, **limits)


@pytest.fixture
def store(tmp_path):
    instance = OpsStore(tmp_path / "ops")
    yield instance
    instance.close()


async def setup(tmp_path, store):
    source = setup_source(tmp_path)
    compose_file = tmp_path / "compose.json"
    compose_file.write_text('{"services":{"app":{"image":"target:base"}}}', encoding="utf-8")
    manifest_hash = hashlib.sha256((tmp_path / "source-manifest.json").read_bytes()).hexdigest()
    spec = target(source, source_manifest_sha256=manifest_hash, source_image_id=BASE_IMAGE,
                  deployment_root="/etc/linkding", compose_file=str(compose_file.resolve()),
                  deployment_launch={"entrypoint": ["/etc/linkding/.venv/bin/uwsgi"],
                                     "command": ["--http", "0.0.0.0:9090", "/etc/linkding/uwsgi.ini"]},
                  compose_project="aura-lab", compose_service="app")
    store.put_target(spec.model_dump(mode="json"))
    runner = DeploymentRunner()
    async def emit(*args):
        pass
    patch = await PatchEngine(store, Adapter(), store.root, runner).prepare(
        spec, investigation(source), asyncio.Event(), emit)
    assert patch["status"] == "tests_passed"
    manager = DeploymentManager(store, runner=runner)
    await ActionExecutor(store, runner=runner).capture_snapshot(spec, runner.observation())
    return spec, runner, patch, manager


async def proposal(spec, runner, patch, manager):
    return await manager.build_proposal(spec, "test-investigation", patch, runner.observation())


def approve(store, value, *, authorization="user", approved_hash=None):
    return store.transition_proposal(value["proposal_id"], "proposed", "executing",
                                     authorization=authorization, approved_hash=approved_hash or value["proposal_hash"])


async def expect_error(code, operation):
    with pytest.raises(OpsError) as caught:
        await operation
    assert caught.value.code == code


def test_build_uses_reviewed_python_only_and_preserves_runtime_configuration(tmp_path, store):
    async def check():
        spec, runner, patch, manager = await setup(tmp_path, store)
        value = await proposal(spec, runner, patch, manager)
        assert value["action"] == "deploy_verified_patch"
        assert value["candidate_image_id"] == CANDIDATE_IMAGE
        assert value["base_image_id"] == BASE_IMAGE
        assert value["automation_eligible"] is False
        assert value["rollback_on_failure"] is True
        assert not runner.app_mutations
        assert set(runner.built_files) == {"Dockerfile", "payload/app.py"}
        assert runner.built_files["payload/app.py"] == Path(patch["candidate_path"], "app.py").read_bytes()
        assert b"return 42" in runner.built_files["payload/app.py"]
        dockerfile = runner.built_files["Dockerfile"].decode()
        assert 'COPY ["payload/app.py", "/etc/linkding/app.py"]' in dockerfile
        assert "RUN " not in dockerfile
        assert "private-config" not in json.dumps(value)
        command, bounds = runner.builds[0]
        assert command[command.index("--network") + 1] == "none"
        assert "--pull=false" in command and bounds["timeout"] == 120
        assert Path(spec.source_dir, "app.py").read_text() == "def answer():\n    return 0\n"
    asyncio.run(check())


def test_execute_needs_explicit_approval_and_never_claims_runtime_verification(tmp_path, store):
    async def check():
        spec, runner, patch, manager = await setup(tmp_path, store)
        value = await proposal(spec, runner, patch, manager)
        await expect_error("deployment_approval_required", manager.execute(value, spec, asyncio.Event()))
        claimed = approve(store, value)
        result = await manager.execute(claimed, spec, asyncio.Event())
        assert result["verified"] is False and result["command_completed"] is True
        assert result["identity"]["image_id"] == CANDIDATE_IMAGE
        assert runner.replay_services[0] == {"image": CANDIDATE_IMAGE, **spec.deployment_launch.model_dump()}
        assert len(runner.app_mutations) == 1
        dispatched = store.get_proposal(value["proposal_id"])
        assert dispatched["status"] == "dispatching" and dispatched["deployment_dispatched"] is True
        assert dispatched["deployment_identity"] == result["identity"]
        await expect_error("action_not_claimed", manager.execute(claimed, spec, asyncio.Event()))
        argv = runner.app_mutations[0]
        assert argv[-5:] == ["--no-deps", "--no-build", "--pull", "never", "app"]
        assert not list(manager.builds.glob("replay-*"))
    asyncio.run(check())


def test_policy_authorization_cannot_deploy_code(tmp_path, store):
    async def check():
        spec, runner, patch, manager = await setup(tmp_path, store)
        value = approve(store, await proposal(spec, runner, patch, manager), authorization="policy")
        await expect_error("deployment_approval_required", manager.execute(value, spec, asyncio.Event()))
        assert not runner.app_mutations
    asyncio.run(check())


def test_rollback_is_same_approval_one_shot_for_exact_deployed_container(tmp_path, store):
    async def check():
        spec, runner, patch, manager = await setup(tmp_path, store)
        value = approve(store, await proposal(spec, runner, patch, manager))
        await manager.execute(value, spec, asyncio.Event())
        store.update_proposal(value["proposal_id"], status="verification_failed")
        result = await manager.rollback(value, spec, asyncio.Event())
        assert result["identity"]["image_id"] == BASE_IMAGE
        assert result["verified"] is False and len(runner.app_mutations) == 2
        assert runner.replay_services[-1] == {"image": BASE_IMAGE, **spec.deployment_launch.model_dump()}
        assert store.get_proposal(value["proposal_id"])["status"] == "rolling_back"
        await expect_error("rollback_not_authorized", manager.rollback(value, spec, asyncio.Event()))
    asyncio.run(check())


def test_deployment_requires_explicit_no_migration_launch_before_build(tmp_path, store):
    async def check():
        spec, runner, patch, manager = await setup(tmp_path, store)
        spec = spec.model_copy(update={"deployment_launch": None})
        store.put_target(spec.model_dump(mode="json"))
        await expect_error("deployment_launch_required", proposal(spec, runner, patch, manager))
        assert not runner.builds and not runner.tag_operations and not runner.app_mutations
    asyncio.run(check())


def test_launch_change_after_approval_cannot_reuse_deployment(tmp_path, store):
    async def check():
        spec, runner, patch, manager = await setup(tmp_path, store)
        value = approve(store, await proposal(spec, runner, patch, manager))
        changed = spec.model_copy(update={"deployment_launch": DeploymentLaunch(
            entrypoint=["/etc/linkding/.venv/bin/uwsgi"], command=["--http", "0.0.0.0:9091", "uwsgi.ini"])})
        store.put_target(changed.model_dump(mode="json"))
        await expect_error("policy_changed", manager.execute(value, spec, asyncio.Event()))
        assert not runner.app_mutations
    asyncio.run(check())


@pytest.mark.parametrize("entrypoint,command", [
    (["server"], []), (["/bin/sh", "-c"], ["server"]), (["/usr/bin/python3.14"], ["manage.py", "migrate"]),
    (["/app/bootstrap.sh"], []), (["/usr/bin/uwsgi"], ["$SERVER_OPTIONS"]),
    (["/usr/bin/uwsgi"], ["migrate"]), (["/usr/bin/uwsgi"], ["bad\nvalue"]),
    (["/usr/bin/uwsgi"], ["x" * 513]),
])
def test_launch_rejects_implicit_shell_migration_and_unbounded_arguments(entrypoint, command):
    with pytest.raises(ValueError):
        DeploymentLaunch(entrypoint=entrypoint, command=command)


def test_compose_lifecycle_hooks_cannot_run_during_deployment(tmp_path, store):
    async def check():
        spec, runner, patch, manager = await setup(tmp_path, store)
        runner.compose["services"]["app"]["post_start"] = [{"command": "migrate"}]
        await expect_error("compose_lifecycle_unsupported",
                           ActionExecutor(store, runner=runner).capture_snapshot(spec, runner.observation()))
        await expect_error("compose_lifecycle_unsupported", proposal(spec, runner, patch, manager))
        assert not runner.builds and not runner.app_mutations
    asyncio.run(check())


def test_rollback_refuses_a_later_container_even_on_same_candidate_image(tmp_path, store):
    async def check():
        spec, runner, patch, manager = await setup(tmp_path, store)
        value = approve(store, await proposal(spec, runner, patch, manager))
        await manager.execute(value, spec, asyncio.Event())
        store.update_proposal(value["proposal_id"], status="verification_failed")
        runner.app["State"]["StartedAt"] = "2030-01-01T01:00:00Z"
        await expect_error("target_changed", manager.rollback(value, spec, asyncio.Event()))
        assert len(runner.app_mutations) == 1
    asyncio.run(check())


def test_forged_patch_status_cannot_create_a_build(tmp_path, store):
    async def check():
        spec, runner, patch, manager = await setup(tmp_path, store)
        forged = copy.deepcopy(patch)
        forged["edited_hashes"]["app.py"]["after"] = "0" * 64
        await expect_error("deployment_patch_forged", proposal(spec, runner, forged, manager))
        forged = copy.deepcopy(patch)
        forged["attempts"][0]["tests"][0]["exit_code"] = 0
        await expect_error("deployment_patch_forged", proposal(spec, runner, forged, manager))
        assert not runner.tag_operations and not runner.builds and not runner.app_mutations
    asyncio.run(check())


async def setup_secret_like(tmp_path, store):
    source = setup_source(tmp_path)
    extra = {
        "svc/token_service.py": "def helper():\n    return 'token-helper'\n",
        "svc/password_hasher.py": "def hash_pw(value):\n    return 'hashed:' + value\n",
        "templates/create_api_token_modal.html": "<div>token modal</div>\n",
    }
    for name, content in extra.items():
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    manifest_path = tmp_path / "source-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    for name in extra:
        manifest["files"][name] = hashlib.sha256((source / name).read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest))
    compose_file = tmp_path / "compose.json"
    compose_file.write_text('{"services":{"app":{"image":"target:base"}}}', encoding="utf-8")
    manifest_hash = hashlib.sha256((tmp_path / "source-manifest.json").read_bytes()).hexdigest()
    spec = target(source, source_manifest_sha256=manifest_hash, source_image_id=BASE_IMAGE,
                  deployment_root="/etc/linkding", compose_file=str(compose_file.resolve()),
                  deployment_launch={"entrypoint": ["/etc/linkding/.venv/bin/uwsgi"],
                                     "command": ["--http", "0.0.0.0:9090", "/etc/linkding/uwsgi.ini"]},
                  compose_project="aura-lab", compose_service="app")
    store.put_target(spec.model_dump(mode="json"))
    runner = DeploymentRunner()
    async def emit(*args):
        pass
    patch = await PatchEngine(store, Adapter(), store.root, runner).prepare(
        spec, investigation(source), asyncio.Event(), emit)
    assert patch["status"] == "tests_passed"
    manager = DeploymentManager(store, runner=runner)
    await ActionExecutor(store, runner=runner).capture_snapshot(spec, runner.observation())
    return spec, runner, patch, manager, extra


def test_secret_like_filenames_accepted_end_to_end(tmp_path, store):
    async def check():
        spec, runner, patch, manager, extra = await setup_secret_like(tmp_path, store)
        stored = store.get_artifact(patch["metadata_artifact"]["artifact_id"])
        durable_bytes = Path(stored["path"]).read_bytes()
        assert b"[REDACTED]" not in durable_bytes
        durable = json.loads(durable_bytes)
        assert durable == {key: value for key, value in patch.items() if key != "metadata_artifact"}
        for name in extra:
            expected = hashlib.sha256(Path(spec.source_dir, name).read_bytes()).hexdigest()
            assert durable["candidate_manifest"][name] == expected == patch["candidate_manifest"][name]
        value = await proposal(spec, runner, patch, manager)
        assert value["action"] == "deploy_verified_patch"
        assert value["candidate_image_id"] == CANDIDATE_IMAGE
        assert value["base_image_id"] == BASE_IMAGE
        assert set(runner.built_files) == {"Dockerfile", "payload/app.py"}
        assert not runner.app_mutations
    asyncio.run(check())


def test_redacted_manifest_checksums_stay_rejected(tmp_path, store):
    async def check():
        spec, runner, patch, manager = await setup(tmp_path, store)
        tampered = copy.deepcopy(patch)
        name = next(iter(tampered["candidate_manifest"]))
        tampered["candidate_manifest"][name] = "[REDACTED]"
        await expect_error("deployment_patch_forged", proposal(spec, runner, tampered, manager))
        corrupted = {key: value for key, value in tampered.items() if key != "metadata_artifact"}
        redacted_meta = store.save_artifact(f"{tampered['patch_id']}.json",
            json.dumps(corrupted, indent=2).encode("utf-8"), "application/json")
        tampered["metadata_artifact"] = redacted_meta
        await expect_error("deployment_manifest_invalid", proposal(spec, runner, tampered, manager))
        assert not runner.tag_operations and not runner.builds and not runner.app_mutations
    asyncio.run(check())


@pytest.mark.parametrize("part", ["candidate", "baseline", "regression", "test-log", "diff", "source"])
def test_changed_evidence_cannot_be_built(tmp_path, store, part):
    async def check():
        spec, runner, patch, manager = await setup(tmp_path, store)
        if part in ("candidate", "baseline"):
            path = Path(patch["candidate_path" if part == "candidate" else "baseline_path"]) / "app.py"
        elif part == "regression":
            path = Path(patch["candidate_path"]) / "aura_generated_tests/test_aura_regression.py"
        elif part == "source":
            path = Path(spec.source_dir) / "app.py"
        else:
            artifact = patch["artifact"] if part == "diff" else patch["attempts"][0]["tests"][0]["artifact"]
            path = Path(store.get_artifact(artifact["artifact_id"])["path"])
        path.write_bytes(path.read_bytes() + b"\n# changed\n")
        with pytest.raises(OpsError):
            await proposal(spec, runner, patch, manager)
        assert not runner.builds and not runner.app_mutations
    asyncio.run(check())


@pytest.mark.parametrize("change", ["entrypoint", "env", "filesystem", "source-label"])
def test_changed_built_image_configuration_or_base_is_rejected(tmp_path, store, change):
    async def check():
        spec, runner, patch, manager = await setup(tmp_path, store)
        def mutate(image):
            if change == "entrypoint":
                image["Config"]["Entrypoint"] = ["other"]
            elif change == "env":
                image["Config"]["Env"].append("UNAPPROVED=1")
            elif change == "filesystem":
                image["RootFS"]["Layers"][0] = "sha256:" + "0" * 64
            else:
                image["Config"]["Labels"]["org.opencontainers.image.revision"] = "0" * 40
        runner.tamper_candidate = mutate
        await expect_error("deployment_image_changed", proposal(spec, runner, patch, manager))
        assert not runner.app_mutations
    asyncio.run(check())


def test_mounts_and_onbuild_cannot_override_reviewed_code(tmp_path, store):
    async def check():
        spec, runner, patch, manager = await setup(tmp_path, store)
        runner.app["Mounts"].append({"Destination": "/etc/linkding"})
        await expect_error("deployment_mount_conflict", proposal(spec, runner, patch, manager))
        runner.app["Mounts"].pop()
        runner.images[BASE_IMAGE]["Config"]["OnBuild"] = ["RUN touch /unreviewed"]
        await expect_error("deployment_onbuild_forbidden", proposal(spec, runner, patch, manager))
        assert not runner.builds
    asyncio.run(check())


def test_approved_compose_mount_cannot_hide_patch_even_if_current_override_removed_it(tmp_path, store):
    async def check():
        spec, runner, patch, manager = await setup(tmp_path, store)
        runner.compose["services"]["app"]["volumes"] = [{"type": "bind", "source": "/host/code", "target": "/etc/linkding"}]
        await ActionExecutor(store, runner=runner).capture_snapshot(spec, runner.observation())
        await expect_error("deployment_mount_conflict", proposal(spec, runner, patch, manager))
        assert not runner.builds
    asyncio.run(check())


@pytest.mark.parametrize("change", ["proposal", "source", "context", "configuration", "policy", "target"])
def test_revalidation_before_execute_prevents_stale_or_tampered_deployment(tmp_path, store, change):
    async def check():
        spec, runner, patch, manager = await setup(tmp_path, store)
        value = approve(store, await proposal(spec, runner, patch, manager))
        if change == "proposal":
            value["candidate_image_id"] = BASE_IMAGE
        elif change == "source":
            Path(spec.source_dir, "app.py").write_text("modified", encoding="utf-8")
        elif change == "context":
            artifact = store.get_artifact(value["build_artifact_id"])
            build = json.loads(Path(artifact["path"]).read_bytes())
            Path(build["context"], "payload/app.py").write_text("modified", encoding="utf-8")
        elif change == "configuration":
            runner.compose["services"]["app"]["environment"]["CHANGED"] = "1"
        elif change == "policy":
            spec = spec.model_copy(update={"monitoring": False})
        else:
            runner.app["Id"] = "1" * 64
        with pytest.raises(OpsError):
            await manager.execute(value, spec, asyncio.Event())
        assert not runner.app_mutations
    asyncio.run(check())


def test_cancel_before_dispatch_and_unknown_result_after_dispatch(tmp_path, store):
    async def check():
        spec, runner, patch, manager = await setup(tmp_path, store)
        value = approve(store, await proposal(spec, runner, patch, manager))
        event = asyncio.Event()
        event.set()
        with pytest.raises(asyncio.CancelledError):
            await manager.execute(value, spec, event)
        assert not runner.app_mutations
        event.clear()
        runner.app_started, runner.app_pause = asyncio.Event(), asyncio.Event()
        operation = asyncio.create_task(manager.execute(value, spec, event))
        await asyncio.wait_for(runner.app_started.wait(), timeout=2)
        event.set()
        await expect_error("action_outcome_unknown", operation)
        assert len(runner.app_mutations) == 1
        await expect_error("rollback_not_authorized", manager.rollback(value, spec, asyncio.Event()))
        assert not list(manager.builds.glob("replay-*"))
    asyncio.run(check())


def test_missing_enrollment_mapping_is_an_explicit_block(tmp_path, store):
    async def check():
        spec, runner, patch, manager = await setup(tmp_path, store)
        missing_root = spec.model_copy(update={"deployment_root": None})
        store.put_target(missing_root.model_dump(mode="json"))
        await expect_error("deployment_mapping_required", proposal(missing_root, runner, patch, manager))
        missing_image = spec.model_copy(update={"source_image_id": None})
        store.put_target(missing_image.model_dump(mode="json"))
        await expect_error("deployment_source_image_mismatch", proposal(missing_image, runner, patch, manager))
        assert not runner.builds and not runner.app_mutations
    asyncio.run(check())


@pytest.mark.parametrize("revocation", ["disconnect", "policy", "approval", "reenroll"])
def test_revocation_during_last_preflight_prevents_target_mutation(tmp_path, store, revocation):
    async def check():
        spec, runner, patch, manager = await setup(tmp_path, store)
        value = approve(store, await proposal(spec, runner, patch, manager))
        original_snapshot = manager._snapshot
        checks = 0
        async def revoke_during_final_check(current_spec, snapshot_hash):
            nonlocal checks
            result = await original_snapshot(current_spec, snapshot_hash)
            checks += 1
            if checks == 2:
                # This point follows all earlier async preflight reads and is
                # immediately before the proposed Docker compose mutation.
                await asyncio.sleep(0)
                if revocation == "disconnect":
                    store.put_target({**spec.model_dump(mode="json"), "monitoring": False})
                elif revocation == "policy":
                    changed = spec.model_copy(deep=True)
                    changed.policy.auto_investigate = True
                    store.put_target(changed.model_dump(mode="json"))
                elif revocation == "approval":
                    store.update_proposal(value["proposal_id"], authorization="revoked")
                else:
                    store.delete_target(spec.target_id)
                    store.put_target(spec.model_dump(mode="json"))
            return result
        manager._snapshot = revoke_during_final_check
        with pytest.raises(OpsError) as caught:
            await manager.execute(value, spec, asyncio.Event())
        assert caught.value.code in ("target_disconnected", "policy_changed", "deployment_approval_required")
        assert checks == 2
        assert not runner.app_mutations
        assert not store.get_proposal(value["proposal_id"]).get("deployment_dispatched")
    asyncio.run(check())


def test_disconnect_during_build_preflight_does_not_create_owned_image_tag(tmp_path, store):
    async def check():
        spec, runner, patch, manager = await setup(tmp_path, store)
        original_image = manager._image
        async def disconnect_after_image_read(current_spec, reference):
            result = await original_image(current_spec, reference)
            store.put_target({**spec.model_dump(mode="json"), "monitoring": False})
            return result
        manager._image = disconnect_after_image_read
        await expect_error("target_disconnected", proposal(spec, runner, patch, manager))
        assert not runner.tag_operations and not runner.builds and not runner.app_mutations
    asyncio.run(check())
