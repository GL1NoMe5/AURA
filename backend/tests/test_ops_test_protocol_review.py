"""Codex review regressions for structured test evidence (offline only)."""
from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from aura_ops.models import OpsError
from aura_ops.store import OpsStore
from aura_ops.test_protocol import parse_report, validate_structure
from aura_ops.test_runner import runner_sha256
from test_ops_test_protocol import _base_report, _bindings


@pytest.mark.parametrize("field", ["protocol_version", "runner_version", "attempt"])
@pytest.mark.parametrize("value", [True, 1.0], ids=["boolean", "float"])
def test_protocol_integer_bindings_reject_equal_noninteger(field, value):
    """JSON true/1.0 must not impersonate an integer version or attempt."""
    report = _base_report(**{field: value})
    with pytest.raises(OpsError):
        validate_structure(report, runner_hash=runner_sha256(), **_bindings())


@pytest.mark.parametrize("scope", ["top-level", "session"])
def test_protocol_parser_rejects_duplicate_object_keys(scope):
    raw = json.dumps(_base_report())
    if scope == "top-level":
        raw = raw.replace('"protocol_version": 1',
                          '"protocol_version": 999, "protocol_version": 1', 1)
    else:
        raw = raw.replace('"exit_code": 0', '"exit_code": 1, "exit_code": 0', 1)
    with pytest.raises(OpsError):
        parse_report(raw.encode("utf-8"))


@pytest.mark.parametrize("stage", ["collect", "original-suite"])
def test_real_collection_skip_rejected_even_with_other_passing_tests(tmp_path, stage):
    """An omitted whole test module must not become an apparently complete inventory."""
    from test_ops_test_protocol import _run_real_runner, _validate_real

    files = {
        "tests/test_ok.py": "def test_ok():\n    assert True\n",
        "tests/test_skipped.py": (
            "import pytest\n"
            "pytest.skip('skip whole module', allow_module_level=True)\n\n"
            "def test_hidden():\n    assert False\n"
        ),
    }
    exit_code, stdout, bindings = _run_real_runner(
        tmp_path, files, stage=stage, expected_paths=["tests"])
    assert exit_code == 0
    with pytest.raises(OpsError):
        _validate_real(stdout, exit_code, bindings)


@pytest.mark.parametrize("mutation", [
    "nonce", "profile-hash", "manifest-hash", "rootdir", "duplicate-stage", "unknown-option",
])
def test_deployment_rejects_invocation_report_disagreement(tmp_path, mutation):
    """A durable record must agree with its report, not just look like a runner command."""
    from test_ops_deployment import setup as deploy_setup

    async def check():
        store = OpsStore(tmp_path / "ops")
        try:
            spec, runner, patch, manager = await deploy_setup(tmp_path, store)
            # Establish that the complete fixture is initially deployable.
            manager._validate_patch(spec, patch["investigation_id"], patch)
            changed = copy.deepcopy(patch)
            command = changed["attempts"][-1]["tests"][1]["command"]
            if mutation == "duplicate-stage":
                command.extend(["--stage", "collect"])
            elif mutation == "unknown-option":
                command.extend(["--unrecognized-option", "value"])
            else:
                flag = "--" + mutation
                index = command.index(flag) + 1
                command[index] = {"nonce": "cd" * 16,
                                  "profile-hash": "33" * 32,
                                  "manifest-hash": "44" * 32,
                                  "rootdir": "/elsewhere"}[mutation]
            metadata = changed.pop("metadata_artifact")
            changed["metadata_artifact"] = store.save_artifact(
                metadata["name"], json.dumps(changed, indent=2).encode(), "application/json")
            with pytest.raises(OpsError):
                manager._validate_patch(spec, changed["investigation_id"], changed)
            assert not runner.builds and not runner.app_mutations
        finally:
            store.close()
    asyncio.run(check())
