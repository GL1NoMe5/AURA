"""OPS-PATCH-GUARD-001 regressions: trusted patch workspace boundary.

Offline only: real temporary filesystem state with the fake Docker
runner/provider. Command-shape assertions check mount arguments; they
are not proof of Linux mount enforcement (real container validation is
a separate reviewed step).
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from aura_ops.actions import ActionExecutor
from aura_ops.deployment import DeploymentManager, _protected_edit
from aura_ops.models import OpsError, PatchProfile
from aura_ops.patches import PatchEngine, _assert_workspace, _editable
from aura_ops.store import OpsStore
from test_ops_deployment import BASE_IMAGE, DeploymentRunner
from test_ops_patches import (Adapter, IMAGE, Runner, candidate, investigation,
                              setup_source, sha, target)

REVISION = "b" * 40


def setup_profile_source(tmp_path):
    source = tmp_path / "source"
    contents = {
        "app.py": "def answer():\n    return 0\n",
        "tests/test_app.py": "def test_existing():\n    assert True\n",
        "checks/suite.py": "def test_suite():\n    assert True\n",
        "settings.py": "DEBUG = True\n",
        "pkg/__init__.py": "",
        "pkg/settings.py": "DEBUG = True\n",
        "pkg/helpers.py": "def helper():\n    return 1\n",
        "pytest.ini": "[pytest]\n",
        "license.txt": "Test fixture\n",
        ".env": "BASETEN_API_KEY=should-never-be-mounted\n",
        "credentials.json": '{"token":"private"}',
    }
    for name, content in contents.items():
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    files = {p.relative_to(source).as_posix(): sha(p.read_bytes())
             for p in source.rglob("*") if p.is_file()}
    (tmp_path / "source-manifest.json").write_text(json.dumps(
        {"source_dir": str(source), "revision": REVISION,
         "files": files}), encoding="utf-8")
    (source / "untracked_secret.py").write_text(
        "API_KEY = 'local-only'\n", encoding="utf-8")
    return source


def profile_investigation(source, *paths):
    return {"target_id": "test-target",
            "investigation_id": "test-investigation",
            "diagnosis": {"summary": "The application returns a wrong result."},
            "evidence": {f"source:{name}": {
                "path": name, "sha256": sha((source / name).read_bytes())}
                for name in paths}}


async def run_profile_engine(tmp_path, *, adapter=None, runner=None,
                             profile, evidence_paths):
    source = setup_profile_source(tmp_path)
    spec = target(source, patch_profile=profile)
    store = OpsStore(tmp_path / "ops")
    events = []

    async def emit(kind, payload):
        events.append((kind, payload))

    try:
        engine = PatchEngine(store, adapter or Adapter(), tmp_path / "ops",
                             runner or Runner())
        result = await engine.prepare(
            spec, profile_investigation(source, *evidence_paths),
            asyncio.Event(), emit)
        return result, source, events
    finally:
        store.close()


def test_mount_is_readonly_with_scratch_retained(tmp_path):
    async def check():
        source = setup_source(tmp_path)
        spec = target(source, patch_profile={
            "image": IMAGE, "test_paths": ["tests"],
            "settings_module": "app.settings", "memory_mib": 512,
            "scratch_dirs": ["data"]})
        store = OpsStore(tmp_path / "ops")
        runner = Runner()

        async def emit(*args):
            pass

        try:
            result = await PatchEngine(store, Adapter(), tmp_path / "ops",
                                       runner).prepare(
                spec, investigation(source), asyncio.Event(), emit)
        finally:
            store.close()
        assert result["status"] == "tests_passed"
        assert len(runner.runs) == 4
        for command, workspace in runner.runs:
            # runc cannot create this mountpoint inside a read-only bind.
            assert (workspace / "data").is_dir()
            assert not any((workspace / "data").iterdir())
            mount = command[command.index("--mount") + 1]
            assert mount.endswith(",readonly"), mount
            tmpfs = [command[i + 1] for i, value in enumerate(command)
                     if value == "--tmpfs"]
            assert "/tmp:rw,nosuid,nodev,size=128m" in tmpfs
            assert "/workspace/data:rw,nosuid,nodev,noexec,size=64m" in tmpfs
            assert "--read-only" in command
            assert command[command.index("--network") + 1] == "none"
    asyncio.run(check())


@pytest.mark.parametrize("plant", ["evil.ini", "pycache"])
def test_planted_non_python_file_fails_validation(tmp_path, plant):
    async def check():
        runner = Runner()
        if plant == "evil.ini":
            runner.mutate = lambda ws: (ws / "evil.ini").write_text(
                "planted\n", encoding="utf-8")
        else:
            def plant_pyc(ws):
                cache = ws / "__pycache__"
                cache.mkdir(exist_ok=True)
                (cache / "app.cpython-310.pyc").write_bytes(
                    b"\x42\x0d\x0d\x0a planted bytecode")
            runner.mutate = plant_pyc

        async def emit(*args):
            pass

        source = setup_source(tmp_path)
        store = OpsStore(tmp_path / "ops")
        try:
            result = await PatchEngine(store, Adapter(), tmp_path / "ops",
                                       runner).prepare(
                target(source), investigation(source), asyncio.Event(), emit)
        finally:
            store.close()
        assert result["status"] == "needs_human"
        assert result["reason"] == "patch_workspace_modified"
        assert len(runner.runs) == 1
    asyncio.run(check())



def _inventory(tmp_path, name):
    root = tmp_path / name
    (root / "tests").mkdir(parents=True)
    (root / "app.py").write_text("def answer():\n    return 0\n", encoding="utf-8")
    (root / "tests/test_app.py").write_text(
        "def test_existing():\n    assert True\n", encoding="utf-8")
    (root / "license.txt").write_text("Test fixture\n", encoding="utf-8")
    hashes = {p.relative_to(root).as_posix(): sha(p.read_bytes())
              for p in root.rglob("*") if p.is_file()}
    return root, hashes


def _raises_inventory(root, hashes, **kwargs):
    with pytest.raises(OpsError) as caught:
        _assert_workspace(root, hashes, **kwargs)
    assert caught.value.code == "patch_workspace_modified"


def test_workspace_inventory_is_exact(tmp_path):
    root, hashes = _inventory(tmp_path, "clean")
    _assert_workspace(root, hashes)

    extra, extra_hashes = _inventory(tmp_path, "config")
    (extra / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    _raises_inventory(extra, extra_hashes)

    cached, cached_hashes = _inventory(tmp_path, "bytecode")
    cache = cached / "__pycache__"
    cache.mkdir()
    (cache / "app.cpython-310.pyc").write_bytes(b"\x42\x0d\x0d\x0a planted")
    _raises_inventory(cached, cached_hashes)

    notes, notes_hashes = _inventory(tmp_path, "notes")
    (notes / "notes.txt").write_text("planted\n", encoding="utf-8")
    _raises_inventory(notes, notes_hashes)

    stray, stray_hashes = _inventory(tmp_path, "stray-dir")
    (stray / "empty-dir").mkdir()
    _raises_inventory(stray, stray_hashes)

    tampered, tampered_hashes = _inventory(tmp_path, "tampered")
    (tampered / "app.py").write_text("def answer():\n    return 1\n",
                                     encoding="utf-8")
    _raises_inventory(tampered, tampered_hashes)

    scratch, scratch_hashes = _inventory(tmp_path, "scratch")
    (scratch / "data").mkdir()
    _raises_inventory(scratch, scratch_hashes)
    _assert_workspace(scratch, scratch_hashes, scratch_dirs=["data"])
    (scratch / "data" / "leftover.txt").write_text("x\n", encoding="utf-8")
    _raises_inventory(scratch, scratch_hashes, scratch_dirs=["data"])


def test_workspace_link_is_rejected(tmp_path):
    root, hashes = _inventory(tmp_path, "linked")
    link = root / "linked.py"
    try:
        link.symlink_to(root / "app.py")
    except OSError:
        pytest.skip("Symlink creation needs privileges on this machine.")
    _raises_inventory(root, hashes)


def test_profile_protection_matrix():
    profile = PatchProfile(image=IMAGE, test_paths=["checks"],
                           settings_module="pkg.settings")
    assert not _editable("checks/suite.py", profile)
    assert not _editable("checks/deep/more.py", profile)
    assert not _editable("pkg/settings.py", profile)
    assert not _editable("pkg/__init__.py", profile)
    # Transitively imported helpers are not guessed or protected.
    assert _editable("pkg/helpers.py", profile)
    assert _editable("app.py", profile)
    assert _editable("checks_utils.py", profile)
    single = PatchProfile(image=IMAGE, test_paths=["tests"],
                          settings_module="settings")
    assert not _editable("settings.py", single)
    assert not _editable("settings/__init__.py", single)
    assert _editable("settings.py", None) is True
    # "app.settings" protects app/settings.py and the package
    # initializer, never the sibling application module app.py itself.
    dotted = PatchProfile(image=IMAGE, test_paths=["tests"],
                          settings_module="app.settings")
    assert not _editable("app/settings.py", dotted)
    assert not _editable("app/__init__.py", dotted)
    assert _editable("app.py", dotted)


def test_custom_test_path_edit_never_runs(tmp_path):
    async def check():
        runner = Runner()
        adapter = Adapter([candidate(edits=[{
            "path": "checks/suite.py", "old": "assert True",
            "new": "assert 2 + 2 == 4"}])])
        result, _, _ = await run_profile_engine(
            tmp_path, adapter=adapter, runner=runner,
            profile={"image": IMAGE, "test_paths": ["checks"]},
            evidence_paths=("app.py", "checks/suite.py"))
        assert result["status"] == "needs_human"
        assert result["reason"] == "patch_edit_forbidden"
        assert not runner.runs
        assert len(adapter.calls) == 2
    asyncio.run(check())


def test_settings_module_edit_never_runs(tmp_path):
    async def check():
        runner = Runner()
        adapter = Adapter([candidate(edits=[{
            "path": "settings.py", "old": "True", "new": "False"}])])
        result, _, _ = await run_profile_engine(
            tmp_path, adapter=adapter, runner=runner,
            profile={"image": IMAGE, "test_paths": ["checks"],
                     "settings_module": "settings"},
            evidence_paths=("app.py", "settings.py"))
        assert result["status"] == "needs_human"
        assert result["reason"] == "patch_edit_forbidden"
        assert not runner.runs
        assert len(adapter.calls) == 2
    asyncio.run(check())


def test_valid_app_edit_still_accepted_with_profile(tmp_path):
    async def check():
        result, _, _ = await run_profile_engine(
            tmp_path,
            profile={"image": IMAGE, "test_paths": ["checks"],
                     "settings_module": "pkg.settings"},
            evidence_paths=("app.py",))
        assert result["status"] == "tests_passed"
    asyncio.run(check())


def test_deployment_protection_helper():
    profile = PatchProfile(image=IMAGE, test_paths=["checks"],
                           settings_module="pkg.settings")
    assert not _protected_edit("checks/suite.py", profile)
    assert not _protected_edit("pkg/settings.py", profile)
    assert not _protected_edit("pkg/__init__.py", profile)
    assert _protected_edit("app.py", profile)
    assert _protected_edit("checks_utils.py", profile)


def test_deployment_refuses_protected_candidate_edit(tmp_path):
    async def check():
        source = setup_profile_source(tmp_path)
        compose_file = tmp_path / "compose.json"
        compose_file.write_text(
            '{"services":{"app":{"image":"target:base"}}}', encoding="utf-8")
        manifest_hash = hashlib.sha256(
            (tmp_path / "source-manifest.json").read_bytes()).hexdigest()
        profile = {"image": IMAGE, "test_paths": ["checks"],
                   "settings_module": "pkg.settings"}
        spec = target(
            source, patch_profile=profile,
            source_manifest_sha256=manifest_hash,
            source_image_id=BASE_IMAGE, deployment_root="/etc/linkding",
            compose_file=str(compose_file.resolve()),
            deployment_launch={"entrypoint": ["/etc/linkding/.venv/bin/uwsgi"],
                               "command": ["--http", "0.0.0.0:9090",
                                           "/etc/linkding/uwsgi.ini"]},
            compose_project="aura-lab", compose_service="app")
        store = OpsStore(tmp_path / "ops")
        runner = DeploymentRunner()

        async def emit(*args):
            pass

        try:
            store.put_target(spec.model_dump(mode="json"))
            patch = await PatchEngine(store, Adapter(), store.root,
                                      runner).prepare(
                spec, profile_investigation(source, "app.py"),
                asyncio.Event(), emit)
            assert patch["status"] == "tests_passed"
            manager = DeploymentManager(store, runner=runner)
            await ActionExecutor(store, runner=runner).capture_snapshot(
                spec, runner.observation())
            tampered = copy.deepcopy(patch)
            suite_bytes = (source / "checks/suite.py").read_bytes()
            edited_bytes = suite_bytes.replace(b"assert True",
                                               b"assert 2 + 2 == 4")
            tampered["edits"] = [{
                "path": "checks/suite.py", "old": "assert True",
                "new": "assert 2 + 2 == 4"}]
            tampered["edited_hashes"] = {"checks/suite.py": {
                "before": sha(suite_bytes), "after": sha(edited_bytes)}}
            tampered["candidate_manifest"]["checks/suite.py"] = sha(edited_bytes)
            corrupted = {key: value for key, value in tampered.items()
                         if key != "metadata_artifact"}
            meta = store.save_artifact(
                f"{tampered['patch_id']}.json",
                json.dumps(corrupted, indent=2).encode("utf-8"),
                "application/json")
            tampered["metadata_artifact"] = meta
            with pytest.raises(OpsError) as caught:
                await manager.build_proposal(
                    spec, "test-investigation", tampered,
                    runner.observation())
            assert caught.value.code == "deployment_edit_forbidden"
            assert not runner.builds
        finally:
            store.close()
    asyncio.run(check())


def test_deployment_workspace_inventory_is_exact(tmp_path):
    async def check():
        source = setup_source(tmp_path)
        compose_file = tmp_path / "compose.json"
        compose_file.write_text(
            '{"services":{"app":{"image":"target:base"}}}', encoding="utf-8")
        manifest_hash = hashlib.sha256(
            (tmp_path / "source-manifest.json").read_bytes()).hexdigest()
        spec = target(
            source, source_manifest_sha256=manifest_hash,
            source_image_id=BASE_IMAGE, deployment_root="/etc/linkding",
            compose_file=str(compose_file.resolve()),
            deployment_launch={"entrypoint": ["/etc/linkding/.venv/bin/uwsgi"],
                               "command": ["--http", "0.0.0.0:9090",
                                           "/etc/linkding/uwsgi.ini"]},
            compose_project="aura-lab", compose_service="app")
        store = OpsStore(tmp_path / "ops")
        runner = DeploymentRunner()

        async def emit(*args):
            pass

        try:
            store.put_target(spec.model_dump(mode="json"))
            patch = await PatchEngine(store, Adapter(), store.root,
                                      runner).prepare(
                spec, investigation(source), asyncio.Event(), emit)
            assert patch["status"] == "tests_passed"
            (Path(patch["candidate_path"]) / "evil.ini").write_text(
                "planted\n", encoding="utf-8")
            manager = DeploymentManager(store, runner=runner)
            await ActionExecutor(store, runner=runner).capture_snapshot(
                spec, runner.observation())
            with pytest.raises(OpsError) as caught:
                await manager.build_proposal(
                    spec, "test-investigation", patch, runner.observation())
            assert caught.value.code == "patch_workspace_modified"
            assert not runner.builds
        finally:
            store.close()
    asyncio.run(check())


def test_deployment_accepts_only_declared_empty_scratch(tmp_path):
    async def check():
        from test_ops_deployment import setup
        from aura_ops.patches import _digest

        store = OpsStore(tmp_path / "ops")
        try:
            spec, runner, patch, manager = await setup(tmp_path, store)
            # Rebuild validation with the same approved source and a scratch profile.
            spec.patch_profile.scratch_dirs = ["data"]
            store.put_target(spec.model_dump(mode="json"))
            async def emit(*args):
                pass
            fresh_runner = DeploymentRunner()
            patch = await PatchEngine(store, Adapter(), store.root, fresh_runner).prepare(
                spec, investigation(Path(spec.source_dir)), asyncio.Event(), emit)
            assert patch["status"] == "tests_passed"
            for key in ("baseline_path", "candidate_path"):
                Path(patch[key], "data").mkdir(exist_ok=True)
            assert patch["profile_hash"] == _digest(spec.patch_profile.model_dump())
            checked = manager._validate_patch(spec, "test-investigation", patch)
            assert checked["record"]["patch_id"] == patch["patch_id"]
            Path(patch["candidate_path"], "data", "planted.cfg").write_text("tamper")
            with pytest.raises(OpsError) as error:
                manager._validate_patch(spec, "test-investigation", patch)
            assert error.value.code == "patch_workspace_modified"
        finally:
            store.close()
    asyncio.run(check())
