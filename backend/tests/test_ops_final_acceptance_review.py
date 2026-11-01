"""Codex regressions for final acceptance evidence, with no external calls."""
import copy
import json
from pathlib import Path
import subprocess
import sys

import pytest

sys.path[:0] = [str(Path(__file__).resolve().parents[2]), str(Path(__file__).resolve().parents[1]),
               str(Path(__file__).resolve().parent)]
from tools.ops import final_acceptance as harness
from test_ops_final_acceptance import resolved, BASE_CMD, IMAGE, LAUNCH


@pytest.mark.parametrize("location", ["port", "mount-mode", "network-option", "extra-service"])
def test_complete_resolved_config_not_only_selected_fields(location):
    base = resolved(command=BASE_CMD)
    first = resolved(command=LAUNCH["command"], entrypoint=LAUNCH["entrypoint"])
    second = copy.deepcopy(first)
    if location == "port":
        second["services"]["app"]["ports"] = [{"target": 9090, "published": "18099"}]
    elif location == "mount-mode":
        second["services"]["app"]["volumes"][0]["read_only"] = False
    elif location == "network-option":
        second["networks"]["front"]["internal"] = True
    else:
        second["services"]["extra"] = {"image": IMAGE}
    assert not harness.compare_resolved(base, first, second, service="app",
                                       expected_image=IMAGE, expected_launch=LAUNCH)["passed"]


def setup_matrix(monkeypatch, tmp_path, exit_code=0, *, temporary=False):
    monkeypatch.setattr(harness, "ROOT", tmp_path)
    monkeypatch.setattr(harness, "http_status", lambda *a, **kw: None if temporary else 200)
    result_path = tmp_path / ".local/ops-acceptance/acc-review12/result.json"
    result_path.parent.mkdir(parents=True)
    result = {"passed": True, "cases": [{"case": i, "name": str(i), "passed": True} for i in range(1, 7)],
              "cleanup": [{"removed": True}], "leaks": [], "preflight": {"registered": {"id": "stable"}},
              "registered_after": {"id": "stable"}, "backend_after": 200,
              "model_mode": "deterministic_mock", "provider_requests": 0}
    result_path.write_text(json.dumps(result))
    completed = subprocess.CompletedProcess([], exit_code, b"", b"")
    monkeypatch.setattr(harness.subprocess, "run", lambda *a, **kw: completed)
    # New wrapper uses a separately mockable child transport.
    monkeypatch.setattr(harness, "_run_matrix_child", lambda *a, **kw: completed, raising=False)
    if temporary:
        class Child:
            pid = 12345
            def poll(self):
                return None
        monkeypatch.setattr(harness, "pick_temp_port", lambda: 19999)
        monkeypatch.setattr(harness, "launch_temp_backend", lambda *a: Child())
        monkeypatch.setattr(harness, "wait_healthy", lambda *a: True)
        monkeypatch.setattr(harness, "stop_child", lambda *a: {"pid": 12345, "exit_code": 0,
                            "port_free": False, "process_tree_gone": True})
    return tmp_path / "evidence"


def test_nonzero_exit_cannot_pass_using_successful_result_file(monkeypatch, tmp_path):
    evidence = setup_matrix(monkeypatch, tmp_path, 1)
    assert not harness.run_matrix(evidence, run_id="acc-review12", retry_note=None)["passed"]


def test_unconfirmed_backend_shutdown_cannot_pass(monkeypatch, tmp_path):
    evidence = setup_matrix(monkeypatch, tmp_path, temporary=True)
    assert not harness.run_matrix(evidence, run_id="acc-review12", retry_note=None)["passed"]


def test_timeout_is_durable_failed_attempt_and_liveness_saved(monkeypatch, tmp_path):
    evidence = setup_matrix(monkeypatch, tmp_path)
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(["owned-matrix"], 1800)
    monkeypatch.setattr(harness.subprocess, "run", timeout)
    monkeypatch.setattr(harness, "_run_matrix_child", timeout, raising=False)
    try:
        harness.run_matrix(evidence, run_id="acc-review12", retry_note=None)
    except subprocess.TimeoutExpired:
        pass
    runs = json.loads((evidence / "matrix-runs.json").read_text())
    assert len(runs) == 1 and runs[0]["passed"] is False
    assert runs[0]["error"]["type"] == "TimeoutExpired"
    assert (evidence / "backend-liveness.json").is_file()


def test_config_command_returns_failure_for_confirmed_problem(monkeypatch, tmp_path):
    async def failed(*args):
        return {"verdict": "confirmed-problem", "comparison": {"passed": False}, "seconds": 0}
    monkeypatch.setattr(harness, "run_config_experiment", failed)
    assert harness.main(["config-experiment", "--evidence-dir", str(tmp_path)]) == 1


def test_selected_case_scope_is_forwarded_and_saved(monkeypatch, tmp_path):
    evidence = setup_matrix(monkeypatch, tmp_path)
    result_path = tmp_path / ".local/ops-acceptance/acc-review12/result.json"
    result = json.loads(result_path.read_text())
    result["cases"] = [case for case in result["cases"] if case["case"] in (1, 4, 5, 6)]
    result_path.write_text(json.dumps(result))
    calls = []
    def run(argv, env, timeout):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, b"", b"")
    monkeypatch.setattr(harness, "_run_matrix_child", run)
    summary = harness.run_matrix(evidence, run_id="acc-review12", retry_note=None, cases=[1, 4, 5, 6])
    assert summary["passed"] is True and summary["scope"] == "selected"
    assert calls[0][calls[0].index("--cases") + 1] == "1,4,5,6"
    stored = json.loads((evidence / "matrix-run.json").read_text())["latest"]
    assert stored["selected_cases"] == [1, 4, 5, 6] and stored["scope"] == "selected"


def test_stop_child_rechecks_transient_port_without_killing_listener(monkeypatch):
    seen = []
    monkeypatch.setattr(harness, "_stop_owned_tree", lambda proc, timeout:
                        {"pid": 123, "process_tree_gone": True, "exit_code": 1})
    probes = iter([False, True])
    monkeypatch.setattr(harness, "port_free", lambda port: seen.append(port) or next(probes))
    monkeypatch.setattr(harness.time, "sleep", lambda seconds: None)
    result = harness.stop_child(object(), 19999)
    assert result["port_free"] is True and seen == [19999, 19999]


def test_matrix_timeout_cleans_own_popen_before_propagating(monkeypatch):
    class Child:
        pid = 123
        def communicate(self, timeout):
            raise subprocess.TimeoutExpired(["matrix"], timeout)
    proc = Child()
    monkeypatch.setattr(harness.subprocess, "Popen", lambda *a, **kw: proc)
    monkeypatch.setattr(harness, "_remember_process", lambda owned: None)
    stopped = []
    monkeypatch.setattr(harness, "_stop_owned_tree", lambda owned:
                        stopped.append(owned) or {"process_tree_gone": True})
    with pytest.raises(subprocess.TimeoutExpired) as error:
        harness._run_matrix_child(["matrix"], {}, 1800)
    assert stopped == [proc]
    assert error.value.owned_process_cleanup["process_tree_gone"] is True
