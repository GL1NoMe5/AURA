"""OPS-TEST-PROTOCOL-001: structured outcomes, not console text (offline).

Failing-before probes for Argon 5.3 console/alias defects, then full protocol
coverage with the real runner in short local child processes (installed pytest,
no Docker/network) plus fake-Docker preparation/deployment integration.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from aura_ops.docker import CommandResult


def test_console_spoof_must_not_establish_success(tmp_path):
    """Fake FAILED/AssertionError lines + print('1 passed') must never pass."""
    from test_ops_patches import Runner, run_engine

    async def check():
        fake_fail = ("FAILED aura_generated_tests/test_aura_regression.py::test_answer - AssertionError\n"
                     "AssertionError: fake\n1 failed in 0.01s\n")
        runner = Runner({"*": {"no_report": True, "log": fake_fail}})
        result, _, _ = await run_engine(tmp_path, runner=runner)
        assert result["status"] == "needs_human"
    asyncio.run(check())


def test_valid_assertion_with_ordinary_error_log_must_pass(tmp_path):
    """Ordinary ERROR log lines must not reject a real AssertionError reproducer."""
    from test_ops_patches import FAILED, Runner, run_engine

    async def check():
        runner = Runner({"before": {"log": FAILED + "ERROR ordinary application log line\n"}})
        result, _, _ = await run_engine(tmp_path, runner=runner)
        assert result["status"] == "tests_passed"
    asyncio.run(check())


@pytest.mark.parametrize("snippet", [
    "from pytest import skip as s\ndef test_a():\n    s()\n    assert True\n",
    "import pytest as p\ndef test_a():\n    p.xfail()\n    assert True\n",
    "from sys import exit as e\ndef test_a():\n    e(0)\n    assert True\n",
    "def test_a():\n    raise SystemExit(0)\n    assert True\n",
    "from pytest import importorskip as io\nio('nope')\ndef test_a():\n    assert True\n",
    "import pytest\ndef test_a():\n    pytest.exit('stop')\n    assert True\n",
    "import os as o\ndef test_a():\n    o._exit(0)\n    assert True\n",
    "from builtins import eval as e\ndef test_a():\n    e('1')\n    assert True\n",
    "import sys\ndef test_a():\n    sys.settrace(None)\n    assert True\n",
    "def pytest_collection_modifyitems(items):\n    pass\ndef test_a():\n    assert True\n",
    "import _pytest.config\ndef test_a():\n    assert True\n",
    "import inspect\ndef test_a():\n    assert True\n",
    "import os\ndef test_a():\n    os.write(1, b'x')\n    assert True\n",
    "import sys\ndef test_a():\n    sys.stdout.write('x')\n    assert True\n",
    "def test_a():\n    assert 'x' in open('app.py').read()\n",
    "from x import *\ndef test_a():\n    assert True\n",
])
def test_aliased_test_escapes_rejected(tmp_path, snippet):
    from aura_ops.patches import Candidate, _validate_candidate
    from aura_ops.models import OpsError
    payloads = {"app.py": b"def answer():\n    return 0\n"}
    cand = Candidate(summary="x", edits=[{"path": "app.py", "old": "return 0", "new": "return 42"}],
                     regression_test=snippet)
    with pytest.raises(OpsError) as err:
        _validate_candidate(cand, payloads, {"app.py"})
    assert err.value.code == "patch_test_invalid"


def test_legitimate_regression_with_fixtures_accepted():
    """gzip/tmp_path/mock/override_settings/read-wrapper/routes stay allowed."""
    from aura_ops.patches import Candidate, _validate_candidate
    payloads = {"app.py": b"def answer():\n    return 0\n"}
    snippet = (
        "import gzip\n"
        "from unittest import mock\n"
        "from django.test import override_settings\n"
        "from app import answer\n"
        "def _read_all(stream):\n"
        "    chunks = []\n"
        "    while True:\n"
        "        piece = stream.read(5)\n"
        "        if not piece:\n"
        "            break\n"
        "        chunks.append(piece)\n"
        "    return b''.join(chunks)\n"
        "@override_settings(DEBUG=True)\n"
        "def test_answer(tmp_path):\n"
        "    target = tmp_path / 'payload.bin'\n"
        "    target.write_bytes(gzip.compress(b'hello'))\n"
        "    with mock.patch('app.fetch_route', return_value=b'data') as fetched:\n"
        "        assert answer() == 42\n"
        "        fetched.assert_called_once()\n"
        "    assert _read_all(open(target, 'rb')) != b''\n"
        "def test_download_routes():\n"
        "    assert answer() != 0\n"
    )
    cand = Candidate(summary="x", edits=[{"path": "app.py", "old": "return 0", "new": "return 42"}],
                     regression_test=snippet)
    changes = _validate_candidate(cand, payloads, {"app.py"})
    assert changes["app.py"].endswith(b"return 42\n")


# --- Real runner in short local child processes (installed pytest) ---

def _run_real_runner(tmp_path, files, *, stage, expected_paths, attempt=1):
    import json as _json
    import secrets as _secrets
    import subprocess as _subprocess
    from aura_ops.test_runner import runner_path
    work = Path(tmp_path) / "ws"
    work.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        target = work / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    nonce = _secrets.token_hex(16)
    profile_hash, manifest_hash = "11" * 32, "22" * 32
    argv = [sys.executable, str(runner_path()), "--stage", stage, "--attempt", str(attempt),
           "--nonce", nonce, "--expected-paths", _json.dumps(list(expected_paths)),
           "--profile-hash", profile_hash, "--manifest-hash", manifest_hash,
           "--rootdir", str(work)]
    proc = _subprocess.run(argv, capture_output=True, text=True, cwd=str(work), timeout=60)
    bindings = {"stage": stage, "attempt": attempt, "expected_paths": list(expected_paths),
                "profile_hash": profile_hash, "manifest_hash": manifest_hash, "nonce": nonce}
    return proc.returncode, proc.stdout, bindings


def _validate_real(stdout, exit_code, bindings):
    from aura_ops.test_protocol import extract_report, parse_report, validate_structure
    from aura_ops.test_runner import runner_sha256
    payload = extract_report(stdout, bindings["nonce"])
    report = parse_report(payload)
    collected, phases = validate_structure(
        report, stage=bindings["stage"], attempt=bindings["attempt"],
        expected_paths=bindings["expected_paths"], profile_hash=bindings["profile_hash"],
        manifest_hash=bindings["manifest_hash"], runner_hash=runner_sha256(),
        nonce=bindings["nonce"], actual_exit=exit_code)
    return report, collected, phases


def test_real_before_after_and_suite_cycle(tmp_path):
    from aura_ops.test_protocol import check_after, check_before, check_suite
    app_v0 = "def answer():\n    return 0\n"
    reg = "from app import answer\ndef test_answer():\n    assert answer() == 42\n"
    suite = "def test_existing():\n    assert True\n"
    files_v0 = {"app.py": app_v0, "test_reg.py": reg, "tests/test_app.py": suite}
    exit0, out0, bind0 = _run_real_runner(tmp_path, files_v0, stage="before", expected_paths=["test_reg.py"])
    _, before_nodes, before_phases = _validate_real(out0, exit0, bind0)
    assert check_before(before_nodes, before_phases, exit_code=exit0) is True
    files_v1 = {"app.py": "def answer():\n    return 42\n", "test_reg.py": reg,
                "tests/test_app.py": suite}
    exit1, out1, bind1 = _run_real_runner(tmp_path, files_v1, stage="after", expected_paths=["test_reg.py"])
    _, after_nodes, after_phases = _validate_real(out1, exit1, bind1)
    assert check_after(after_nodes, after_phases, exit_code=exit1, before_nodes=before_nodes) is True
    exit2, out2, bind2 = _run_real_runner(tmp_path, files_v1, stage="original-suite",
                                          expected_paths=["tests"])
    _, suite_nodes, suite_phases = _validate_real(out2, exit2, bind2)
    assert check_suite(suite_nodes, suite_phases, exit_code=exit2,
                       baseline_nodes=suite_nodes) is True
    assert suite_nodes == ["tests/test_app.py::test_existing"]


def test_real_error_log_does_not_reject_assertion(tmp_path):
    from aura_ops.test_protocol import check_before
    reg = ("import logging\nfrom app import answer\n"
           "def test_answer():\n"
           "    logging.error('ordinary ERROR line')\n"
           "    assert answer() == 42\n")
    files = {"app.py": "def answer():\n    return 0\n", "test_reg.py": reg}
    exit_code, stdout, bindings = _run_real_runner(tmp_path, files, stage="before",
                                                   expected_paths=["test_reg.py"])
    assert "ERROR" in stdout
    _, nodes, phases = _validate_real(stdout, exit_code, bindings)
    assert check_before(nodes, phases, exit_code=exit_code) is True


@pytest.mark.parametrize("kind,failure", [
    ("value", "def test_a():\n    raise ValueError('bad')\n"),
    ("skip", "import pytest\ndef test_a():\n    pytest.skip('nope')\n"),
    ("xfail", "import pytest\n@pytest.mark.xfail\ndef test_a():\n    assert True\n"),
    ("exit", "import sys\ndef test_a():\n    sys.exit(0)\n"),
    ("setup", "import pytest\n@pytest.fixture\ndef fix():\n    raise ValueError('s')\n"
              "def test_a(fix):\n    assert True\n"),
    ("teardown", "import pytest\n@pytest.fixture\ndef fix():\n    yield 1\n    raise ValueError('t')\n"
                 "def test_a(fix):\n    assert True\n"),
    ("empty", "X = 1\n"),
])
def test_real_wrong_outcomes_fail_before_gate(tmp_path, kind, failure):
    from aura_ops.test_protocol import check_before
    files = {"test_reg.py": failure}
    exit_code, stdout, bindings = _run_real_runner(tmp_path, files, stage="before",
                                                   expected_paths=["test_reg.py"])
    _, nodes, phases = _validate_real(stdout, exit_code, bindings)
    assert check_before(nodes, phases, exit_code=exit_code) is False


@pytest.mark.parametrize("kind,body", [
    ("syntax", "def test_a(:\n    pass\n"),
    ("import", "import no_such_module_xyz\ndef test_a():\n    assert True\n"),
])
def test_real_collection_failures_reject(tmp_path, kind, body):
    import pytest as _pt
    from aura_ops.models import OpsError
    from aura_ops.test_protocol import extract_report, parse_report, validate_structure
    from aura_ops.test_runner import runner_sha256
    files = {"test_reg.py": body}
    exit_code, stdout, bindings = _run_real_runner(tmp_path, files, stage="before",
                                                   expected_paths=["test_reg.py"])
    payload = extract_report(stdout, bindings["nonce"])
    report = parse_report(payload)
    with _pt.raises(OpsError) as err:
        validate_structure(report, stage=bindings["stage"], attempt=bindings["attempt"],
                           expected_paths=bindings["expected_paths"],
                           profile_hash=bindings["profile_hash"],
                           manifest_hash=bindings["manifest_hash"], runner_hash=runner_sha256(),
                           nonce=bindings["nonce"], actual_exit=exit_code)
    assert err.value.code == "test_collection_failed"


def test_real_printed_summary_cannot_forge_pass(tmp_path):
    from aura_ops.test_protocol import check_after
    reg = ("def test_a():\n"
           "    print('1 passed')\n"
           "    print('FAILED test_reg.py::test_a - AssertionError')\n"
           "    assert True\n")
    files = {"test_reg.py": reg}
    exit_code, stdout, bindings = _run_real_runner(tmp_path, files, stage="after",
                                                   expected_paths=["test_reg.py"])
    _, nodes, phases = _validate_real(stdout, exit_code, bindings)
    assert check_after(nodes, phases, exit_code=exit_code, before_nodes=nodes) is True
    bad = ("def test_a():\n"
           "    print('1 passed')\n"
           "    assert False\n")
    exit_bad, out_bad, bind_bad = _run_real_runner(tmp_path, {"test_reg.py": bad},
                                                   stage="after", expected_paths=["test_reg.py"])
    _, nodes_bad, phases_bad = _validate_real(out_bad, exit_bad, bind_bad)
    assert check_after(nodes_bad, phases_bad, exit_code=exit_bad,
                       before_nodes=nodes_bad) is False


# --- Validator transport/binding/node-set unit checks ---

def _base_report(**overrides):
    from aura_ops.test_runner import runner_sha256
    report = {"protocol": "aura-test-report", "protocol_version": 1, "runner_version": 1,
              "runner_sha256": runner_sha256(), "stage": "after", "attempt": 1,
              "nonce": "ab" * 16, "expected_paths": ["test_reg.py"],
              "profile_hash": "11" * 32, "manifest_hash": "22" * 32,
              "collected": ["test_reg.py::test_a"], "collection_complete": True,
              "deselected": [], "collection_errors": [],
              "tests": [{"nodeid": "test_reg.py::test_a", "when": when, "outcome": "passed",
                         "exc_type": None, "exc_mod": None, "wasxfail": None}
                        for when in ("setup", "call", "teardown")],
              "session": {"completed": True, "exit_code": 0, "stopped_early": False}}
    report.update(overrides)
    return report


def _bindings(**overrides):
    base = {"stage": "after", "attempt": 1, "expected_paths": ["test_reg.py"],
            "profile_hash": "11" * 32, "manifest_hash": "22" * 32,
            "nonce": "ab" * 16, "actual_exit": 0}
    base.update(overrides)
    return base


def test_validator_accepts_bound_report():
    from aura_ops.test_protocol import check_after, validate_structure
    from aura_ops.test_runner import runner_sha256
    collected, phases = validate_structure(_base_report(), runner_hash=runner_sha256(),
                                           **_bindings())
    assert check_after(collected, phases, exit_code=0, before_nodes=collected) is True


@pytest.mark.parametrize("field,value", [
    ("stage", "before"), ("attempt", 2), ("expected_paths", ["other.py"]),
    ("profile_hash", "33" * 32), ("manifest_hash", "44" * 32),
    ("runner_sha256", "55" * 32), ("nonce", "cd" * 16),
    ("protocol_version", 2), ("protocol", "other"), ("runner_version", 99),
])
def test_validator_rejects_wrong_binding(field, value):
    import pytest as _pt
    from aura_ops.models import OpsError
    from aura_ops.test_protocol import validate_structure
    from aura_ops.test_runner import runner_sha256
    report = _base_report(**{field: value})
    bindings = _bindings()
    if field == "runner_sha256":
        runner_hash, report_hash = runner_sha256(), value
    else:
        runner_hash, report_hash = runner_sha256(), report["runner_sha256"]
    assert report_hash is not None
    with _pt.raises(OpsError) as err:
        validate_structure(report, runner_hash=runner_hash, **bindings)
    assert err.value.code == "test_report_binding"


def test_validator_rejects_exit_mismatch_and_bad_shapes():
    import pytest as _pt
    from aura_ops.models import OpsError
    from aura_ops.test_protocol import validate_structure
    from aura_ops.test_runner import runner_sha256
    with _pt.raises(OpsError) as err:
        validate_structure(_base_report(), runner_hash=runner_sha256(), **_bindings(actual_exit=1))
    assert err.value.code == "test_report_exit_mismatch"
    for bad in ({"deselected": ["test_reg.py::test_a"]},
                {"collection_errors": ["test_reg.py"]},
                {"collected": ["elsewhere.py::test_x"]},
                {"collected": ["test_reg.py::test_a", "test_reg.py::test_a"]}):
        with _pt.raises(OpsError):
            validate_structure(_base_report(**bad), runner_hash=runner_sha256(), **_bindings())


def test_envelope_requires_exact_single_nonce_match():
    import json as _json
    import pytest as _pt
    from aura_ops.models import OpsError
    from aura_ops.test_protocol import extract_report
    from aura_ops.test_runner import BEGIN_MARK, END_MARK
    payload = _json.dumps({"ok": True})
    good = f"logs\n{BEGIN_MARK} {'ab' * 16}\n{payload}\n{END_MARK} {'ab' * 16}\n"
    assert extract_report(good, "ab" * 16) == payload.encode()
    with _pt.raises(OpsError):
        extract_report("no envelope here", "ab" * 16)
    with _pt.raises(OpsError):
        extract_report(good + good, "ab" * 16)
    truncated = f"{BEGIN_MARK} {'ab' * 16}\n{payload}\n"
    with _pt.raises(OpsError):
        extract_report(truncated, "ab" * 16)
    wrong_nonce = good.replace("ab" * 16, "cd" * 16)
    with _pt.raises(OpsError):
        extract_report(wrong_nonce, "ab" * 16)


def test_node_set_must_match_exactly():
    from aura_ops.test_protocol import check_after, check_suite, validate_structure
    from aura_ops.test_runner import runner_sha256
    collected, phases = validate_structure(_base_report(), runner_hash=runner_sha256(),
                                           **_bindings())
    assert check_after(collected, phases, exit_code=0, before_nodes=["other.py::test_z"]) is False
    assert check_suite(collected, phases, exit_code=0, baseline_nodes=[]) is False
    assert check_suite(collected, phases, exit_code=0, baseline_nodes=collected) is True


# --- Preparation/deployment integration for tampering and legacy ---

@pytest.mark.parametrize("scripts,reason", [
    ({"before": {"mode": "system-exit"}}, "reproducer_not_confirmed"),
    ({"collect": {"mode": "empty"}}, "baseline_collection_failed"),
    ({"collect": {"mode": "collect-error"}}, "test_collection_failed"),
    ({"before": {"duplicate": True}}, "test_report_duplicate"),
    ({"before": {"report_override": {"manifest_hash": "00" * 32}}}, "test_report_binding"),
    ({"before": {"no_report": True, "log": "1 failed\n"}}, "test_report_missing"),
])
def test_preparation_rejects_protocol_failures(tmp_path, scripts, reason):
    from test_ops_patches import Runner, run_engine

    async def check():
        runner = Runner(scripts)
        result, _, _ = await run_engine(tmp_path, runner=runner)
        assert result["status"] == "needs_human"
        assert result.get("reason") == reason
    asyncio.run(check())


def test_deployment_rejects_legacy_log_only_candidate(tmp_path):
    import copy as _copy
    import json as _json
    from aura_ops.models import OpsError
    from aura_ops.store import OpsStore
    from test_ops_deployment import setup as deploy_setup

    async def check():
        store = OpsStore(tmp_path / "ops")
        try:
            spec, runner, patch, manager = await deploy_setup(tmp_path, store)
            legacy = _copy.deepcopy(patch)
            attempt = legacy["attempts"][-1]
            attempt.pop("baseline_collection", None)
            for entry in attempt["tests"]:
                entry.pop("report", None)
            metadata = dict(legacy.pop("metadata_artifact"))
            raw = _json.dumps(legacy, indent=2).encode()
            meta = store.save_artifact(metadata["name"], raw, "application/json")
            legacy["metadata_artifact"] = meta
            with pytest.raises(OpsError) as err:
                await manager.build_proposal(spec, legacy["investigation_id"], legacy,
                                             runner.observation())
            assert err.value.code == "deployment_protocol_missing"
        finally:
            store.close()
    asyncio.run(check())


def test_deployment_detects_tampered_report_artifact(tmp_path):
    from aura_ops.models import OpsError
    from aura_ops.store import OpsStore
    from test_ops_deployment import setup as deploy_setup

    async def check():
        store = OpsStore(tmp_path / "ops")
        try:
            spec, runner, patch, manager = await deploy_setup(tmp_path, store)
            entry = patch["attempts"][-1]["tests"][1]
            meta = store.get_artifact(entry["report"]["artifact_id"])
            path = Path(meta["path"])
            raw = path.read_bytes().replace(b'"exit_code":0', b'"exit_code":1', 1)
            assert raw != path.read_bytes()
            path.write_bytes(raw)
            with pytest.raises(OpsError) as err:
                await manager.build_proposal(spec, patch["investigation_id"], patch,
                                             runner.observation())
            assert err.value.code in ("artifact_corrupt", "artifact_missing",
                                      "deployment_tests_missing", "test_report_malformed",
                                      "test_report_exit_mismatch", "test_report_binding")
        finally:
            store.close()
    asyncio.run(check())


def test_deployment_ignores_cached_booleans(tmp_path):
    import copy as _copy
    import json as _json
    from aura_ops.store import OpsStore
    from test_ops_deployment import setup as deploy_setup

    async def check():
        store = OpsStore(tmp_path / "ops")
        try:
            spec, runner, patch, manager = await deploy_setup(tmp_path, store)
            flipped = _copy.deepcopy(patch)
            for entry in flipped["attempts"][-1]["tests"]:
                entry["gate_passed"] = False
                entry["passed"] = False
            flipped["attempts"][-1]["baseline_collection"]["gate_passed"] = False
            metadata = dict(flipped.pop("metadata_artifact"))
            raw = _json.dumps(flipped, indent=2).encode()
            meta = store.save_artifact(metadata["name"], raw, "application/json")
            flipped["metadata_artifact"] = meta
            proposal = await manager.build_proposal(spec, flipped["investigation_id"], flipped,
                                                    runner.observation())
            assert proposal["action"] == "deploy_verified_patch"
        finally:
            store.close()
    asyncio.run(check())
