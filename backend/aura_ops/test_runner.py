"""AURA-owned pytest runner/reporter for patch validation.

Supplied from outside the candidate workspace and mounted read-only; the
candidate cannot edit this file or choose the runner. Emits one versioned
JSON report envelope to stdout; console logs stay diagnostic only.

Same-interpreter limits (see docs/TEST_EVIDENCE_PROTOCOL.md): candidate code
and pytest share this interpreter, so a hostile test can read argv/env (nonce),
write raw stdout, or terminate the process. The nonce blocks accidental
collisions and offline precomputation, not in-process theft. Duplicate or
missing envelopes, exit mismatches and binding mismatches fail closed, but no
JSON report proves arbitrary hostile Python trustworthy.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

RUNNER_VERSION = 1
PROTOCOL_VERSION = 1
PROTOCOL_NAME = "aura-test-report"

BEGIN_MARK = "AURA_TEST_REPORT_BEGIN"
END_MARK = "AURA_TEST_REPORT_END"


def runner_path() -> Path:
    return Path(__file__).resolve()


def runner_sha256() -> str:
    return hashlib.sha256(runner_path().read_bytes()).hexdigest()


class _State:
    def __init__(self):
        self.collected = None
        self.deselected: list[str] = []
        self.collect_errors: list[str] = []
        self.phases: dict[tuple[str, str], dict] = {}
        self.stopped_early = False


_STATE = _State()


def pytest_collection_finish(session):
    try:
        _STATE.collected = [str(item.nodeid) for item in session.items]
    except Exception:
        _STATE.collected = []


def pytest_deselected(items):
    try:
        _STATE.deselected.extend(str(item.nodeid) for item in items)
    except Exception:
        _STATE.deselected.append("<unrepresentable>")


def pytest_collectreport(report):
    try:
        # A skipped module can hide every test it contains before collection.
        # Treat it as incomplete evidence, just like a collection failure.
        if report.failed or report.skipped:
            _STATE.collect_errors.append(str(report.nodeid))
    except Exception:
        _STATE.collect_errors.append("<unrepresentable>")


def _record_exc(item, call, rep):
    try:
        nodeid = str(item.nodeid)
    except Exception:
        nodeid = "<unrepresentable>"
    try:
        when = str(call.when)
    except Exception:
        when = "<unknown>"
    try:
        outcome = str(rep.outcome)
    except Exception:
        outcome = "<unknown>"
    exc_type = exc_mod = None
    try:
        exc = call.excinfo
        if exc is not None and exc.type is not None:
            exc_type = str(exc.type.__name__)
            exc_mod = str(getattr(exc.type, "__module__", ""))
    except Exception:
        exc_type, exc_mod = "<unrepresentable>", ""
    try:
        wasxfail = getattr(rep, "wasxfail", None)
        wasxfail = None if wasxfail is None else str(wasxfail)[:200]
    except Exception:
        wasxfail = "<unrepresentable>"
    _STATE.phases[(nodeid, when)] = {
        "nodeid": nodeid, "when": when, "outcome": outcome,
        "exc_type": exc_type, "exc_mod": exc_mod, "wasxfail": wasxfail,
    }


def _pytest_major() -> int:
    try:
        import pytest
        return int(str(pytest.__version__).split(".")[0])
    except Exception:
        return 0


def _install_makereport_hook():
    import pytest
    if _pytest_major() >= 7:
        @pytest.hookimpl(wrapper=True, tryfirst=True)
        def pytest_runtest_makereport(item, call):
            rep = yield
            _record_exc(item, call, rep)
            return rep
    else:
        @pytest.hookimpl(hookwrapper=True, tryfirst=True)
        def pytest_runtest_makereport(item, call):
            outcome = yield
            rep = outcome.get_result()
            _record_exc(item, call, rep)
            outcome.force_result(rep)
    return pytest_runtest_makereport


pytest_runtest_makereport = _install_makereport_hook()


def pytest_sessionfinish(session, exitstatus):
    try:
        if getattr(session, "shouldstop", False) or getattr(session, "shouldfail", False):
            _STATE.stopped_early = True
    except Exception:
        _STATE.stopped_early = True


def _build_report(args, exit_code: int, completed: bool) -> dict:
    tests = []
    for (nodeid, when), rec in sorted(_STATE.phases.items()):
        tests.append(rec)
    return {
        "protocol": PROTOCOL_NAME,
        "protocol_version": PROTOCOL_VERSION,
        "runner_version": RUNNER_VERSION,
        "runner_sha256": runner_sha256(),
        "stage": args.stage,
        "attempt": args.attempt,
        "nonce": args.nonce,
        "expected_paths": args.expected_paths,
        "profile_hash": args.profile_hash,
        "manifest_hash": args.manifest_hash,
        "collected": _STATE.collected if _STATE.collected is not None else [],
        "collection_complete": _STATE.collected is not None,
        "deselected": list(_STATE.deselected),
        "collection_errors": list(_STATE.collect_errors),
        "tests": tests,
        "session": {
            "completed": bool(completed),
            "exit_code": int(exit_code),
            "stopped_early": bool(_STATE.stopped_early),
        },
    }


def _parse_args(argv):
    parser = argparse.ArgumentParser(prog="aura-test-runner")
    parser.add_argument("--stage", required=True,
                        choices=["before", "after", "original-suite", "collect"])
    parser.add_argument("--attempt", required=True, type=int)
    parser.add_argument("--nonce", required=True)
    parser.add_argument("--expected-paths", required=True)
    parser.add_argument("--profile-hash", required=True)
    parser.add_argument("--manifest-hash", required=True)
    parser.add_argument("--rootdir", default="/workspace")
    parsed = parser.parse_args(argv)
    try:
        expected = json.loads(parsed.expected_paths)
    except (ValueError, TypeError):
        parser.error("expected-paths must be JSON")
    parsed.expected_paths = expected
    return parsed


def main(argv=None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    # Running an external script puts /aura, not /workspace, on sys.path.
    # Preserve the former `python -m pytest` application import semantics,
    # including tests under aura_generated_tests and Django plugin discovery.
    sys.path.insert(0, str(Path(args.rootdir).resolve(strict=True)))
    import pytest
    pytest_args = ["-p", "no:cacheprovider", "-q", "--rootdir", args.rootdir]
    if args.stage == "collect":
        pytest_args.append("--collect-only")
    pytest_args.extend(args.expected_paths)
    plugin = sys.modules[__name__]
    try:
        exit_code = int(pytest.main(pytest_args, plugins=[plugin]))
        completed = True
    except SystemExit as exc:
        # pytest.exit() or raw SystemExit escaping the session: incomplete.
        code = exc.code
        exit_code = int(code) if isinstance(code, int) else 2
        completed = False
    except BaseException:
        exit_code, completed = 2, False
    report = _build_report(args, exit_code, completed)
    payload = json.dumps(report, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, allow_nan=False)
    sys.stdout.write(f"{BEGIN_MARK} {args.nonce}\n{payload}\n{END_MARK} {args.nonce}\n")
    sys.stdout.flush()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
