"""Patch validation boundaries with real filesystem/store and offline runners."""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from aura_agents.adapter import MODEL
from aura_ops.docker import CommandResult
from aura_ops.models import OpsError, TargetSpec
from aura_ops.patches import PatchEngine, _redact_record
from aura_ops.store import OpsStore

IMAGE = "sha256:" + "a" * 64
REVISION = "b" * 40
REGRESSION_PATH = "aura_generated_tests/test_aura_regression.py"
REGRESSION = "from app import answer\n\ndef test_answer():\n    assert answer() == 42\n"
FAILED = "F\nE AssertionError: assert 0 == 42\nFAILED aura_generated_tests/test_aura_regression.py::test_answer - AssertionError\n1 failed in 0.01s\n"


def sha(content):
    return hashlib.sha256(content).hexdigest()


def setup_source(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "tests").mkdir()
    (source / "app.py").write_text("def answer():\n    return 0\n", encoding="utf-8")
    (source / "tests/test_app.py").write_text("def test_existing():\n    assert True\n", encoding="utf-8")
    (source / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (source / "license.txt").write_text("Test fixture\n", encoding="utf-8")
    (source / ".env").write_text("BASETEN_API_KEY=should-never-be-mounted\n", encoding="utf-8")
    (source / "credentials.json").write_text('{"token":"private"}', encoding="utf-8")
    files = {p.relative_to(source).as_posix(): sha(p.read_bytes()) for p in source.rglob("*") if p.is_file()}
    (tmp_path / "source-manifest.json").write_text(json.dumps(
        {"source_dir": str(source), "revision": REVISION, "files": files}), encoding="utf-8")
    # Untracked/local files never become part of the test container mount.
    (source / "untracked_secret.py").write_text("API_KEY = 'local-only'\n", encoding="utf-8")
    return source


def target(source, **overrides):
    values = {"target_id": "test-target", "name": "Test", "container_name": "test-app",
              "docker_context": "test-context", "probes": [{"url": "http://127.0.0.1:9000/"}],
              "source_dir": str(source), "source_revision": REVISION,
              "patch_profile": {"image": IMAGE, "test_paths": ["tests"], "timeout_seconds": 10}}
    values.update(overrides)
    return TargetSpec(**values)


def investigation(source):
    return {"target_id": "test-target", "investigation_id": "test-investigation",
            "diagnosis": {"summary": "The application returns a wrong result."},
            "evidence": {"source:app.py": {"path": "app.py", "sha256": sha((source / "app.py").read_bytes())}}}


def candidate(**overrides):
    values = {"summary": "Correct the return value.", "edits": [{"path": "app.py", "old": "return 0", "new": "return 42"}],
              "regression_test": REGRESSION}
    values.update(overrides)
    return values


class Adapter:
    ready = True
    def __init__(self, candidates=None, on_call=None):
        self.candidates = candidates or [candidate()]
        self.calls = []
        self.on_call = on_call

    async def complete(self, messages, *, cancel_event, max_tokens, reasoning_effort):
        self.calls.append(messages)
        assert max_tokens == 16384
        assert reasoning_effort == "medium"
        assert "disjoint anchors in ORIGINAL source" in messages[0]["content"]
        assert '"maxItems":3' in messages[0]["content"]
        assert cancel_event is not None
        if self.on_call:
            self.on_call()
        value = self.candidates[min(len(self.calls) - 1, len(self.candidates) - 1)]
        # Comply with the requested contract: an edits-only retry schema
        # (no regression_test property) gets only summary/edits back.
        if isinstance(value, dict):
            schema = json.loads(messages[0]["content"].split("Exact JSON Schema:\n", 1)[1])
            if "regression_test" not in schema.get("properties", {}):
                value = {key: item for key, item in value.items() if key in ("summary", "edits")}
        return SimpleNamespace(content=json.dumps(value), requested_model=MODEL, response_model=MODEL,
                               finish_reason="stop", duration_ms=1.0, attempts=1, usage=None)


REGRESSION_NODE = "aura_generated_tests/test_aura_regression.py::test_answer"
SUITE_NODE = "tests/test_app.py::test_existing"


def _suite_nodes_for(expected_paths):
    if any(path == "checks" or path.startswith("checks/") for path in expected_paths):
        return ["checks/suite.py::test_suite"]
    return [SUITE_NODE]


def protocol_result(*, stage, attempt, nonce, expected_paths, profile_hash,
                    manifest_hash, mode="valid", log=None, no_report=False,
                    report_override=None, duplicate=False):
    """Build a fake-Docker CommandResult with a bound structured report."""
    from aura_ops.test_runner import (BEGIN_MARK, END_MARK, PROTOCOL_NAME,
                                      PROTOCOL_VERSION, RUNNER_VERSION,
                                      runner_sha256)
    suite_nodes = _suite_nodes_for(list(expected_paths))
    if stage == "collect":
        if mode not in ("valid", "empty", "collect-error"):
            raise AssertionError(f"unknown mode {mode}")
        collected = [] if mode == "empty" else list(suite_nodes)
        phases = []
        errors = ["tests/test_app.py"] if mode == "collect-error" else []
        exit_code = 2 if mode == "collect-error" else (5 if mode == "empty" else 0)
        default_log = {("valid"): "1 test collected in 0.01s\n",
                       "empty": "no tests collected in 0.01s\n",
                       "collect-error": "ERROR collecting tests/test_app.py\n"}[mode]
        report = {"protocol": PROTOCOL_NAME, "protocol_version": PROTOCOL_VERSION,
                  "runner_version": RUNNER_VERSION, "runner_sha256": runner_sha256(),
                  "stage": stage, "attempt": attempt, "nonce": nonce,
                  "expected_paths": list(expected_paths), "profile_hash": profile_hash,
                  "manifest_hash": manifest_hash, "collected": collected,
                  "collection_complete": True, "deselected": [],
                  "collection_errors": errors,
                  "tests": phases,
                  "session": {"completed": True, "exit_code": exit_code, "stopped_early": False}}
        if report_override:
            report.update(report_override)
        payload = json.dumps(report, sort_keys=True, separators=(",", ":"))
        text = (log if log is not None else default_log)
        if not no_report:
            envelope = f"{BEGIN_MARK} {report['nonce']}\n{payload}\n{END_MARK} {report['nonce']}\n"
            text += envelope
            if duplicate:
                text += envelope
        return CommandResult(exit_code, text)
    elif stage in ("before", "after"):
        collected = [REGRESSION_NODE]
        if (stage == "before" and mode == "valid") or mode == "assert-fail":
            phases = [
                {"nodeid": REGRESSION_NODE, "when": "setup", "outcome": "passed",
                 "exc_type": None, "exc_mod": None, "wasxfail": None},
                {"nodeid": REGRESSION_NODE, "when": "call", "outcome": "failed",
                 "exc_type": "AssertionError", "exc_mod": "builtins", "wasxfail": None},
                {"nodeid": REGRESSION_NODE, "when": "teardown", "outcome": "passed",
                 "exc_type": None, "exc_mod": None, "wasxfail": None},
            ]
            exit_code, default_log = 1, FAILED
        elif mode == "all-pass" or (stage == "after" and mode == "valid"):
            phases = [
                {"nodeid": REGRESSION_NODE, "when": when, "outcome": "passed",
                 "exc_type": None, "exc_mod": None, "wasxfail": None}
                for when in ("setup", "call", "teardown")]
            exit_code, default_log = 0, "1 passed in 0.01s\n"
        elif mode == "value-fail":
            phases = [
                {"nodeid": REGRESSION_NODE, "when": "setup", "outcome": "passed",
                 "exc_type": None, "exc_mod": None, "wasxfail": None},
                {"nodeid": REGRESSION_NODE, "when": "call", "outcome": "failed",
                 "exc_type": "ValueError", "exc_mod": "builtins", "wasxfail": None},
                {"nodeid": REGRESSION_NODE, "when": "teardown", "outcome": "passed",
                 "exc_type": None, "exc_mod": None, "wasxfail": None},
            ]
            exit_code, default_log = 1, "1 failed in 0.01s\n"
        elif mode == "skip":
            phases = [
                {"nodeid": REGRESSION_NODE, "when": "setup", "outcome": "passed",
                 "exc_type": None, "exc_mod": None, "wasxfail": None},
                {"nodeid": REGRESSION_NODE, "when": "call", "outcome": "skipped",
                 "exc_type": "Skipped", "exc_mod": "builtins", "wasxfail": None},
                {"nodeid": REGRESSION_NODE, "when": "teardown", "outcome": "passed",
                 "exc_type": None, "exc_mod": None, "wasxfail": None},
            ]
            exit_code, default_log = 0, "1 skipped in 0.01s\n"
        elif mode == "setup-error":
            phases = [
                {"nodeid": REGRESSION_NODE, "when": "setup", "outcome": "failed",
                 "exc_type": "ValueError", "exc_mod": "builtins", "wasxfail": None},
                {"nodeid": REGRESSION_NODE, "when": "call", "outcome": "skipped",
                 "exc_type": None, "exc_mod": None, "wasxfail": None},
                {"nodeid": REGRESSION_NODE, "when": "teardown", "outcome": "passed",
                 "exc_type": None, "exc_mod": None, "wasxfail": None},
            ]
            exit_code, default_log = 1, "1 error in 0.01s\n"
        elif mode == "system-exit":
            phases = [
                {"nodeid": REGRESSION_NODE, "when": "setup", "outcome": "passed",
                 "exc_type": None, "exc_mod": None, "wasxfail": None},
                {"nodeid": REGRESSION_NODE, "when": "call", "outcome": "failed",
                 "exc_type": "SystemExit", "exc_mod": "builtins", "wasxfail": None},
                {"nodeid": REGRESSION_NODE, "when": "teardown", "outcome": "passed",
                 "exc_type": None, "exc_mod": None, "wasxfail": None},
            ]
            exit_code, default_log = 1, "1 failed in 0.01s\n"
        elif mode == "empty":
            collected, phases = [], []
            exit_code, default_log = 5, "no tests ran in 0.01s\n"
        else:
            raise AssertionError(f"unknown mode {mode}")
    else:  # original-suite
        collected = list(suite_nodes)
        node = suite_nodes[0]
        if mode in ("valid", "all-pass"):
            phases = [
                {"nodeid": node, "when": when, "outcome": "passed",
                 "exc_type": None, "exc_mod": None, "wasxfail": None}
                for when in ("setup", "call", "teardown")]
            exit_code, default_log = 0, "1 passed in 0.02s\n"
        elif mode == "single-fail":
            phases = [
                {"nodeid": node, "when": "setup", "outcome": "passed",
                 "exc_type": None, "exc_mod": None, "wasxfail": None},
                {"nodeid": node, "when": "call", "outcome": "failed",
                 "exc_type": "AssertionError", "exc_mod": "builtins", "wasxfail": None},
                {"nodeid": node, "when": "teardown", "outcome": "passed",
                 "exc_type": None, "exc_mod": None, "wasxfail": None},
            ]
            exit_code, default_log = 1, "1 failed in 0.02s\n"
        elif mode == "skip":
            phases = [
                {"nodeid": node, "when": "setup", "outcome": "passed",
                 "exc_type": None, "exc_mod": None, "wasxfail": None},
                {"nodeid": node, "when": "call", "outcome": "skipped",
                 "exc_type": "Skipped", "exc_mod": "builtins", "wasxfail": None},
                {"nodeid": node, "when": "teardown", "outcome": "passed",
                 "exc_type": None, "exc_mod": None, "wasxfail": None},
            ]
            exit_code, default_log = 0, "1 skipped in 0.02s\n"
        else:
            raise AssertionError(f"unknown mode {mode}")
    report = {"protocol": PROTOCOL_NAME, "protocol_version": PROTOCOL_VERSION,
              "runner_version": RUNNER_VERSION, "runner_sha256": runner_sha256(),
              "stage": stage, "attempt": attempt, "nonce": nonce,
              "expected_paths": list(expected_paths), "profile_hash": profile_hash,
              "manifest_hash": manifest_hash, "collected": collected,
              "collection_complete": True, "deselected": [], "collection_errors": [],
              "tests": phases,
              "session": {"completed": True, "exit_code": exit_code, "stopped_early": False}}
    if report_override:
        report.update(report_override)
    payload = json.dumps(report, sort_keys=True, separators=(",", ":"))
    text = (log if log is not None else default_log)
    if not no_report:
        envelope = f"{BEGIN_MARK} {report['nonce']}\n{payload}\n{END_MARK} {report['nonce']}\n"
        text += envelope
        if duplicate:
            text += envelope
    return CommandResult(exit_code, text)


class Runner:
    def __init__(self, outcomes=None):
        self.calls = []
        self.containers = {}
        self.runs = []
        self.removed = []
        # Stage-keyed report scenarios; None/{} means a valid stage report.
        # Keys are stage suffixes ("collect", "before", "after",
        # "original-suite"), (stage, attempt) tuples, or "*" for all stages.
        self.outcomes = outcomes
        self.foreign_label = False
        self.hang = False
        self.started = asyncio.Event()
        self.mutate = None
        self.image_missing = False

    def _scenario(self, stage, attempt):
        if not self.outcomes:
            return {}
        if isinstance(self.outcomes, dict):
            if (stage, attempt) in self.outcomes:
                return self.outcomes[(stage, attempt)]
            if stage in self.outcomes:
                return self.outcomes[stage]
            return self.outcomes.get("*", {})
        return self.outcomes[min(len(self.runs) - 1, len(self.outcomes) - 1)]

    async def __call__(self, argv, **limits):
        self.calls.append((argv, limits))
        assert argv[:3] == ["docker", "--context", "test-context"]
        command = argv[3:]
        if command[:2] == ["image", "inspect"]:
            if self.image_missing:
                return CommandResult(1, "", "No such image")
            return CommandResult(0, json.dumps([{"Id": IMAGE, "Os": "linux"}]))
        if command[0] == "run":
            name = command[command.index("--name") + 1]
            labels = dict(command[i + 1].split("=", 1) for i, value in enumerate(command) if value == "--label")
            if self.foreign_label:
                labels["aura.owner"] = "someone-else"
            cid = f"{len(self.runs) + 1:064x}"
            self.containers[name] = {"Id": cid, "Name": "/" + name, "Config": {"Labels": labels}}
            mounts = [command[i + 1] for i, value in enumerate(command) if value == "--mount"]
            assert any("target=/workspace" in mount and "readonly" in mount for mount in mounts)
            assert any("target=/aura/test_runner.py" in mount and "readonly" in mount for mount in mounts)
            workspace = Path(mounts[0].split("source=", 1)[1].split(",target=", 1)[0])
            assert not (workspace / ".env").exists()
            assert not (workspace / "credentials.json").exists()
            assert not (workspace / "untracked_secret.py").exists()
            assert not (workspace / ".git").exists()
            assert command[command.index("--entrypoint") + 1] == "python"
            assert "/aura/test_runner.py" in command
            stage = command[command.index("--stage") + 1]
            attempt = int(command[command.index("--attempt") + 1])
            nonce = command[command.index("--nonce") + 1]
            expected_paths = json.loads(command[command.index("--expected-paths") + 1])
            profile_hash = command[command.index("--profile-hash") + 1]
            manifest_hash = command[command.index("--manifest-hash") + 1]
            self.runs.append((command, workspace))
            if self.mutate:
                self.mutate(workspace)
            self.started.set()
            if self.hang:
                await asyncio.sleep(100)
            scenario = self._scenario(stage, attempt)
            return protocol_result(stage=stage, attempt=attempt, nonce=nonce,
                                    expected_paths=expected_paths, profile_hash=profile_hash,
                                    manifest_hash=manifest_hash, **scenario)
        if command[:2] == ["container", "inspect"]:
            row = self.containers.get(command[-1])
            return CommandResult(0, json.dumps([row])) if row else CommandResult(1, "", "No such container")
        if command[:3] == ["container", "rm", "--force"]:
            cid = command[-1]
            matching = [name for name, row in self.containers.items() if row["Id"] == cid]
            assert len(matching) == 1
            self.removed.append(cid)
            del self.containers[matching[0]]
            return CommandResult(0, cid)
        raise AssertionError(command)


async def run_engine(tmp_path, *, adapter=None, runner=None, spec_override=None):
    source = setup_source(tmp_path)
    spec = target(source, **(spec_override or {}))
    store = OpsStore(tmp_path / "ops")
    events = []
    async def emit(kind, payload):
        events.append((kind, payload))
    try:
        engine = PatchEngine(store, adapter or Adapter(), tmp_path / "ops", runner or Runner())
        result = await engine.prepare(spec, investigation(source), asyncio.Event(), emit)
        return result, source, events
    finally:
        store.close()


def test_patch_before_after_suite_provenance_and_original_untouched(tmp_path):
    async def check():
        runner = Runner()
        adapter = Adapter()
        result, source, events = await run_engine(tmp_path, adapter=adapter, runner=runner)
        assert result["status"] == "tests_passed"
        assert result["runtime_verified"] is False
        assert result["deployment_approved"] is False
        assert (source / "app.py").read_text() == "def answer():\n    return 0\n"
        assert Path(result["candidate_path"], "app.py").read_text() == "def answer():\n    return 42\n"
        assert len(runner.runs) == 4
        assert len(runner.removed) == 4
        assert not runner.containers
        assert len(adapter.calls) == 1
        assert result["attempts"][0]["tests"][0]["gate_passed"] is True
        assert result["attempts"][0]["tests"][0]["passed"] is False
        assert result["edited_hashes"]["app.py"]["before"] == sha((source / "app.py").read_bytes())
        store = OpsStore(tmp_path / "ops")
        try:
            artifact = store.get_artifact(result["artifact"]["artifact_id"])
            diff = Path(artifact["path"]).read_text()
            assert "-    return 0" in diff and "+    return 42" in diff
            assert "test_aura_regression.py" in diff
        finally:
            store.close()
        assert events[-1][0] == "patch_finished"
    asyncio.run(check())


def test_docker_isolation_profile_and_no_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("BASETEN_API_KEY", "secret-env-value")
    async def check():
        runner = Runner()
        result, _, _ = await run_engine(tmp_path, runner=runner,
            spec_override={"patch_profile": {"image": IMAGE, "test_paths": ["tests"],
                                             "settings_module": "app.settings", "memory_mib": 512,
                                             "scratch_dirs": ["data"]}})
        assert result["status"] == "tests_passed"
        for command, _ in runner.runs:
            for option, value in [("--network", "none"), ("--cap-drop", "ALL"),
                                  ("--security-opt", "no-new-privileges"), ("--pids-limit", "128"),
                                  ("--memory", "512m"), ("--memory-swap", "512m"), ("--cpus", "1"),
                                  ("--entrypoint", "python"), ("--pull", "never")]:
                assert command[command.index(option) + 1] == value
            assert "--read-only" in command
            assert "--privileged" not in command
            assert "DJANGO_SETTINGS_MODULE=app.settings" in command
            assert "PYTHONDONTWRITEBYTECODE=1" in command
            assert "/workspace/data:rw,nosuid,nodev,noexec,size=64m" in command
            assert not any("secret-env-value" in part or "docker.sock" in part or "BASETEN" in part for part in command)
            assert IMAGE in command
    asyncio.run(check())


@pytest.mark.parametrize("path", ["../escape", "/absolute", "a/b", "a:b", "a,b", "a b", ".", ""])
def test_scratch_path_cannot_escape_or_inject_mount_options(path):
    from pydantic import ValidationError
    from aura_ops.models import PatchProfile
    with pytest.raises(ValidationError):
        PatchProfile(image=IMAGE, test_paths=["tests"], scratch_dirs=[path])


def test_scratch_cannot_hide_trusted_source_or_tests(tmp_path):
    async def check():
        runner = Runner()
        result, _, _ = await run_engine(tmp_path, runner=runner,
            spec_override={"patch_profile": {"image": IMAGE, "test_paths": ["tests"],
                                             "scratch_dirs": ["tests"]}})
        assert result["status"] == "needs_human"
        assert result["reason"] == "patch_scratch_conflict"
        assert not runner.runs
    asyncio.run(check())


@pytest.mark.parametrize("path", ["../escape.py", "/absolute.py", "C:/escape.py", "app.py/../../evil.py",
                                 "tests/test_app.py", "conftest.py", "setup.py", "untracked_secret.py", ".env"])
def test_unauthorized_edit_never_runs_tests(tmp_path, path):
    async def check():
        runner = Runner()
        adapter = Adapter([candidate(edits=[{"path": path, "old": "return 0", "new": "return 42"}])])
        result, _, _ = await run_engine(tmp_path, adapter=adapter, runner=runner)
        assert result["status"] == "needs_human"
        assert not runner.runs
        assert len(adapter.calls) == 2
        assert result["reason"] == "patch_edit_forbidden"
    asyncio.run(check())


def test_schema_extra_fields_rejected(tmp_path):
    async def check():
        runner = Runner()
        result, _, _ = await run_engine(tmp_path, runner=runner,
            adapter=Adapter([candidate(shell_command="do anything")]))
        assert result["status"] == "needs_human"
        assert result["reason"] == "patch_schema_invalid"
        assert not runner.runs
    asyncio.run(check())


def test_non_unique_source_anchor_rejected(tmp_path):
    async def check():
        runner = Runner()
        result, _, _ = await run_engine(tmp_path, runner=runner,
            adapter=Adapter([candidate(edits=[{"path": "app.py", "old": "no such source", "new": "return 42"}])]))
        assert result["status"] == "needs_human"
        assert result["reason"] == "patch_anchor_invalid"
        assert not runner.runs
    asyncio.run(check())


@pytest.mark.parametrize("mode", [
    "all-pass",
    "setup-error",
    "skip",
    "value-fail",
    "empty",
])
def test_baseline_requires_assertion_not_setup_failure(tmp_path, mode):
    async def check():
        runner = Runner({"before": {"mode": mode}})
        result, _, _ = await run_engine(tmp_path, runner=runner)
        assert result["status"] == "needs_human"
        assert result["reason"] == "reproducer_not_confirmed"
        assert len(runner.runs) == 4  # collect + baseline per bounded candidate
        assert len(runner.removed) == 4
    asyncio.run(check())


def test_original_suite_failure_never_produces_passed_candidate(tmp_path):
    async def check():
        runner = Runner({"original-suite": {"mode": "single-fail"}})
        result, _, _ = await run_engine(tmp_path, runner=runner)
        assert result["status"] == "needs_human"
        assert result["reason"] == "candidate_tests_failed"
        assert "artifact" not in result
        assert len(runner.runs) == 8
    asyncio.run(check())


def test_trusted_tests_cannot_be_modified_by_generated_test(tmp_path):
    async def check():
        runner = Runner()
        runner.mutate = lambda workspace: (workspace / "tests/test_app.py").write_text("def test_existing():\n    pass\n")
        result, _, _ = await run_engine(tmp_path, runner=runner)
        assert result["status"] == "needs_human"
        assert result["reason"] == "patch_workspace_modified"
        assert len(runner.runs) == 1
        assert len(runner.removed) == 1
    asyncio.run(check())


def test_source_change_during_model_call_rejects_candidate(tmp_path):
    async def check():
        source = setup_source(tmp_path)
        store = OpsStore(tmp_path / "ops")
        runner = Runner()
        adapter = Adapter(on_call=lambda: (source / "app.py").write_text("def answer():\n    return 99\n"))
        async def emit(*args):
            pass
        try:
            result = await PatchEngine(store, adapter, tmp_path / "ops", runner).prepare(
                target(source), investigation(source), asyncio.Event(), emit)
            assert result["status"] == "needs_human"
            assert result["reason"] == "source_changed"
            assert not runner.runs
        finally:
            store.close()
    asyncio.run(check())


def test_foreign_container_never_removed(tmp_path):
    async def check():
        runner = Runner()
        runner.foreign_label = True
        result, _, _ = await run_engine(tmp_path, runner=runner)
        assert result["status"] == "needs_human"
        assert result["reason"] == "patch_cleanup_identity_mismatch"
        assert not runner.removed
    asyncio.run(check())


def test_cancellation_removes_owned_container_not_just_cli(tmp_path):
    async def check():
        source = setup_source(tmp_path)
        store = OpsStore(tmp_path / "ops")
        runner = Runner()
        runner.hang = True
        cancelled = asyncio.Event()
        async def emit(*args):
            pass
        try:
            task = asyncio.create_task(PatchEngine(store, Adapter(), tmp_path / "ops", runner).prepare(
                target(source), investigation(source), cancelled, emit))
            await asyncio.wait_for(runner.started.wait(), 3)
            cancelled.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert len(runner.removed) == 1
            assert not runner.containers
        finally:
            store.close()
    asyncio.run(check())


def test_missing_image_dispatches_no_model_call(tmp_path):
    async def check():
        source = setup_source(tmp_path)
        store = OpsStore(tmp_path / "ops")
        runner, adapter = Runner(), Adapter()
        runner.image_missing = True
        async def emit(*args):
            pass
        try:
            with pytest.raises(OpsError) as error:
                await PatchEngine(store, adapter, tmp_path / "ops", runner).prepare(
                    target(source), investigation(source), asyncio.Event(), emit)
            assert error.value.code == "patch_image_unavailable"
            assert not adapter.calls
        finally:
            store.close()
    asyncio.run(check())


def test_configured_secret_in_tracked_code_blocks_workspace_and_provider(tmp_path, monkeypatch):
    async def check():
        source = setup_source(tmp_path)
        secret = "private-credential-4782b96b"
        monkeypatch.setenv("BASETEN_API_KEY", secret)
        code = (source / "app.py").read_text() + "\nSENSITIVE = " + repr(secret) + "\n"
        (source / "app.py").write_text(code)
        manifest_path = tmp_path / "source-manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["files"]["app.py"] = sha((source / "app.py").read_bytes())
        manifest_path.write_text(json.dumps(manifest))
        store, adapter, runner = OpsStore(tmp_path / "ops"), Adapter(), Runner()
        async def emit(*args):
            pass
        try:
            with pytest.raises(OpsError) as error:
                await PatchEngine(store, adapter, tmp_path / "ops", runner).prepare(
                    target(source), investigation(source), asyncio.Event(), emit)
            assert error.value.code == "patch_source_contains_secret"
            assert secret not in error.value.message
            assert not adapter.calls and not runner.runs
        finally:
            store.close()
    asyncio.run(check())


SECRET_LIKE_FILES = {
    "svc/token_service.py": "def answer():\n    return 0\n",
    "svc/password_hasher.py": "def hash_pw(value):\n    return 'hashed:' + value\n",
    "templates/create_api_token_modal.html": "<div>token modal</div>\n",
    "tests/test_app.py": "def test_existing():\n    assert True\n",
    "pytest.ini": "[pytest]\n",
}


def setup_secret_like_source(tmp_path):
    """Benign tracked files whose names contain secret-like words (no secrets inside)."""
    source = tmp_path / "source"
    for name, content in SECRET_LIKE_FILES.items():
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    files = {p.relative_to(source).as_posix(): sha(p.read_bytes()) for p in source.rglob("*") if p.is_file()}
    (tmp_path / "source-manifest.json").write_text(json.dumps(
        {"source_dir": str(source), "revision": REVISION, "files": files}), encoding="utf-8")
    return source


def secret_like_investigation(source):
    return {"target_id": "test-target", "investigation_id": "test-investigation",
            "diagnosis": {"summary": "The token service returns a wrong result."},
            "evidence": {"source:svc/token_service.py": {"path": "svc/token_service.py",
                        "sha256": sha((source / "svc/token_service.py").read_bytes())}}}


def test_secret_like_filenames_survive_persistence_identically(tmp_path):
    async def check():
        source = setup_secret_like_source(tmp_path)
        spec = target(source)
        store = OpsStore(tmp_path / "ops")
        async def emit(*args):
            pass
        try:
            engine = PatchEngine(store, Adapter([candidate(
                summary="Correct the service return value.",
                edits=[{"path": "svc/token_service.py", "old": "return 0", "new": "return 42"}])]),
                tmp_path / "ops", Runner())
            result = await engine.prepare(spec, secret_like_investigation(source), asyncio.Event(), emit)
            assert result["status"] == "tests_passed"
            stored = store.get_artifact(result["metadata_artifact"]["artifact_id"])
            durable = json.loads(Path(stored["path"]).read_bytes())
            returned = {key: value for key, value in result.items() if key != "metadata_artifact"}
            assert durable == returned
            assert "[REDACTED]" not in json.dumps(durable)
            manifest = durable["candidate_manifest"]
            assert set(manifest) == set(SECRET_LIKE_FILES) | {REGRESSION_PATH}
            candidate_bytes = Path(result["candidate_path"], "svc/token_service.py").read_bytes()
            assert b"return 42" in candidate_bytes and b"return 0" not in candidate_bytes
            assert manifest["svc/token_service.py"] == sha(candidate_bytes)
            assert manifest["svc/password_hasher.py"] == sha((source / "svc/password_hasher.py").read_bytes())
            assert manifest["templates/create_api_token_modal.html"] == sha(
                (source / "templates/create_api_token_modal.html").read_bytes())
            assert durable["edited_hashes"] == {"svc/token_service.py": {
                "before": sha((source / "svc/token_service.py").read_bytes()),
                "after": sha(candidate_bytes)}}
            assert durable["edits"] == [{"path": "svc/token_service.py", "old": "return 0", "new": "return 42"}]
            assert result["summary"] == "Correct the service return value."
        finally:
            store.close()
    asyncio.run(check())


def test_configured_secret_redacted_from_result_text_while_manifests_stay_exact(tmp_path, monkeypatch):
    async def check():
        secret = "live-credential-9f3c2a77"
        monkeypatch.setenv("AURA_PATCH_TEST_TOKEN", secret)
        source = setup_secret_like_source(tmp_path)
        spec = target(source)
        store = OpsStore(tmp_path / "ops")
        events = []
        async def emit(kind, payload):
            events.append((kind, payload))
        try:
            engine = PatchEngine(store, Adapter([candidate(
                summary=f"Correct the token service return value (ref {secret}).",
                edits=[{"path": "svc/token_service.py", "old": "return 0", "new": "return 42"}])]),
                tmp_path / "ops", Runner())
            result = await engine.prepare(spec, secret_like_investigation(source), asyncio.Event(), emit)
            assert result["status"] == "tests_passed"
            assert secret not in json.dumps(result)
            assert secret not in json.dumps(events)
            assert "[REDACTED]" in result["summary"]
            stored = store.get_artifact(result["metadata_artifact"]["artifact_id"])
            durable_bytes = Path(stored["path"]).read_bytes()
            assert secret.encode() not in durable_bytes
            durable = json.loads(durable_bytes)
            assert durable == {key: value for key, value in result.items() if key != "metadata_artifact"}
            assert durable["candidate_manifest"]["svc/password_hasher.py"] == sha(
                (source / "svc/password_hasher.py").read_bytes())
            diff = Path(store.get_artifact(result["artifact"]["artifact_id"])["path"]).read_bytes()
            assert secret.encode() not in diff
        finally:
            store.close()
    asyncio.run(check())


@pytest.mark.parametrize("field", ["new", "regression", "pattern"])
def test_unsanitizable_candidate_rejected_explicitly_without_runs_or_leaks(tmp_path, monkeypatch, field):
    async def check():
        secret = "embedded-credential-77aa1d"
        monkeypatch.setenv("AURA_PATCH_TEST_TOKEN", secret)
        source = setup_source(tmp_path)
        store = OpsStore(tmp_path / "ops")
        events = []
        async def emit(kind, payload):
            events.append((kind, payload))
        if field == "new":
            values = candidate(edits=[{"path": "app.py", "old": "return 0", "new": f"return 0  # {secret}"}])
        elif field == "regression":
            values = candidate(regression_test=REGRESSION + f"\n# {secret}\n")
        else:
            values = candidate(edits=[{"path": "app.py", "old": "return 0",
                                        "new": 'return 0  # password = "hunter2"'}])
        runner, adapter = Runner(), Adapter([values])
        try:
            result = await PatchEngine(store, adapter, tmp_path / "ops", runner).prepare(
                target(source), investigation(source), asyncio.Event(), emit)
            assert result["status"] == "needs_human"
            assert result["reason"] == "patch_candidate_redaction_conflict"
            assert not runner.runs
            assert len(adapter.calls) == 2
            assert secret not in json.dumps(result)
            assert secret not in json.dumps(events)
            for path in (tmp_path / "ops").rglob("*"):
                if path.is_file():
                    assert secret.encode() not in path.read_bytes()
        finally:
            store.close()
    asyncio.run(check())


@pytest.mark.parametrize("value", ["[REDACTED]", "not-a-hash", "", "private-credential-4782b96b",
                                   "a" * 63, "g" * 64, None, 42])
def test_manifest_like_keys_reject_arbitrary_values_without_exemption(value):
    with pytest.raises(OpsError) as error:
        _redact_record({"candidate_manifest": {"svc/token_service.py": value}},
                       ("private-credential-4782b96b",))
    assert error.value.code == "patch_metadata_invalid"


@pytest.mark.parametrize("record", [
    {"candidate_manifest": {"../escape.py": "a" * 64}},
    {"candidate_manifest": {}},
    {"edited_hashes": {"app.py": {"before": "a" * 64}}},
    {"edited_hashes": {"app.py": {"before": "a" * 64, "after": "[REDACTED]"}}},
    {"edits": [{"path": "app.py", "old": "x"}]},
    {"profile": {"image": IMAGE}},
    {"source_manifest_hash": "short"},
    {"source_revision": "z" * 40},
])
def test_malformed_validation_metadata_rejected_explicitly(record):
    with pytest.raises(OpsError) as error:
        _redact_record(record, ())
    assert error.value.code == "patch_metadata_invalid"
