"""Strict shared validator for structured test outcomes.

Used consistently at patch preparation and deployment validation. Console
logs are diagnostic only and never decide acceptance. Reports must be
versioned, bound (runner/stage/attempt/paths/profile/manifest/nonce), complete
and exit-consistent; any missing, malformed, truncated, duplicate or mismatched
report rejects the stage.

This closes protocol/log-parsing defects and named direct bypasses. It does
not make arbitrary hostile Python sharing the test interpreter trustworthy;
see docs/TEST_EVIDENCE_PROTOCOL.md for residual limits.
"""
from __future__ import annotations

import json
import re

from .models import OpsError
from .test_runner import BEGIN_MARK, END_MARK, PROTOCOL_NAME, PROTOCOL_VERSION, RUNNER_VERSION

MAX_REPORT_BYTES = 256 * 1024
MAX_COLLECTED = 2000
MAX_NODEID_CHARS = 500
_HEX64 = re.compile(r"[a-f0-9]{64}")
_HEX32 = re.compile(r"[a-f0-9]{32}")
_STAGES = ("before", "after", "original-suite", "collect")


def _reject(code: str, message: str) -> OpsError:
    return OpsError(code, message, 409)


def extract_report(stdout: str | bytes, nonce: str) -> bytes:
    """Return the single bound report payload, or reject.

    Exactly one envelope for this run's nonce must exist. Anything else
    (missing, duplicate, truncated) fails closed; logs are never parsed.
    """
    if isinstance(stdout, bytes):
        try:
            text = stdout.decode("utf-8")
        except UnicodeError as exc:
            raise _reject("test_report_malformed", "Test output is not valid UTF-8.") from exc
    else:
        text = stdout
    if not isinstance(nonce, str) or not _HEX32.fullmatch(nonce):
        raise _reject("test_report_malformed", "Test run nonce is invalid.")
    begin = f"{BEGIN_MARK} {nonce}\n"
    end = f"\n{END_MARK} {nonce}"
    if text.count(begin) != 1:
        raise _reject("test_report_missing" if begin not in text else "test_report_duplicate",
                      "Structured test report is missing or ambiguous; logs alone prove nothing.")
    start = text.index(begin) + len(begin)
    closing = text.find(end, start)
    if closing < 0:
        raise _reject("test_report_malformed", "Structured test report is truncated.")
    payload = text[start:closing].encode("utf-8")
    if len(payload) > MAX_REPORT_BYTES:
        raise _reject("test_report_too_large", "Structured test report exceeds its transport bound.")
    if text.count(end, start) != 1:
        raise _reject("test_report_duplicate", "Structured test report is ambiguous.")
    return payload


def parse_report(data: bytes) -> dict:
    if not isinstance(data, bytes) or len(data) > MAX_REPORT_BYTES or not data:
        raise _reject("test_report_malformed", "Structured test report has an invalid size.")

    def _no_const(value: str):
        raise ValueError("non-finite")

    def _unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    try:
        report = json.loads(data.decode("utf-8"), parse_constant=_no_const,
                            object_pairs_hook=_unique_object)
    except (ValueError, UnicodeError) as exc:
        raise _reject("test_report_malformed", "Structured test report is not valid JSON.") from exc
    if not isinstance(report, dict):
        raise _reject("test_report_malformed", "Structured test report must be an object.")
    return report


def _is_int(value) -> bool:
    return type(value) is int


def _check_bindings(report: dict, *, stage: str, attempt: int, expected_paths: list,
                    profile_hash: str, manifest_hash: str, runner_hash: str,
                    nonce: str | None, actual_exit: int) -> None:
    if report.get("protocol") != PROTOCOL_NAME \
            or not _is_int(report.get("protocol_version")) \
            or not _is_int(report.get("runner_version")) \
            or report.get("protocol_version") != PROTOCOL_VERSION \
            or report.get("runner_version") != RUNNER_VERSION:
        raise _reject("test_report_binding", "Test report protocol/runner version mismatch.")
    if not isinstance(report.get("runner_sha256"), str) \
            or report["runner_sha256"] != runner_hash:
        raise _reject("test_report_binding", "Test report runner identity mismatch.")
    if report.get("stage") != stage or not _is_int(report.get("attempt")) \
            or report.get("attempt") != attempt \
            or report.get("expected_paths") != expected_paths \
            or report.get("profile_hash") != profile_hash \
            or report.get("manifest_hash") != manifest_hash:
        raise _reject("test_report_binding", "Test report bindings do not match this run.")
    seen_nonce = report.get("nonce")
    if not isinstance(seen_nonce, str) or not _HEX32.fullmatch(seen_nonce):
        raise _reject("test_report_malformed", "Test report nonce is invalid.")
    if nonce is not None and seen_nonce != nonce:
        raise _reject("test_report_binding", "Test report does not belong to this run.")
    session = report.get("session")
    if not isinstance(session, dict) or not _is_int(session.get("exit_code")):
        raise _reject("test_report_malformed", "Test report session is invalid.")
    if session["exit_code"] != actual_exit:
        raise _reject("test_report_exit_mismatch",
                      "Test execution exit code disagrees with its report.")


def _check_common_shape(report: dict, expected_paths: list) -> tuple[list, list, list]:
    collected = report.get("collected")
    deselected = report.get("deselected")
    collection_errors = report.get("collection_errors")
    tests = report.get("tests")
    session = report.get("session")
    if not isinstance(collected, list) or len(collected) > MAX_COLLECTED \
            or not isinstance(deselected, list) or len(deselected) > MAX_COLLECTED \
            or not isinstance(collection_errors, list) or len(collection_errors) > MAX_COLLECTED \
            or not isinstance(tests, list) or len(tests) > 3 * MAX_COLLECTED \
            or not isinstance(session, dict) or session.get("completed") is not True \
            or session.get("stopped_early") is not False \
            or report.get("collection_complete") is not True:
        raise _reject("test_report_malformed", "Test report structure is invalid.")
    for nodeid in collected:
        if not isinstance(nodeid, str) or not nodeid or len(nodeid) > MAX_NODEID_CHARS \
                or "\n" in nodeid or "\x00" in nodeid or not _under_paths(nodeid, expected_paths):
            raise _reject("test_report_malformed", "Test report collects an unexpected node.")
    if len(set(collected)) != len(collected):
        raise _reject("test_report_malformed", "Test report collection is not unique.")
    return collected, deselected, collection_errors


def _under_paths(nodeid: str, expected_paths: list) -> bool:
    for path in expected_paths:
        if not isinstance(path, str) or not path:
            continue
        if nodeid == path or nodeid.startswith(path + "/") or nodeid.startswith(path + "::"):
            return True
    return False


def _phase_map(tests: list) -> dict[tuple[str, str], dict]:
    phases: dict[tuple[str, str], dict] = {}
    for entry in tests:
        if not isinstance(entry, dict):
            raise _reject("test_report_malformed", "Test phase entry is invalid.")
        nodeid, when, outcome = entry.get("nodeid"), entry.get("when"), entry.get("outcome")
        if not isinstance(nodeid, str) or when not in ("setup", "call", "teardown") \
                or outcome not in ("passed", "failed", "skipped"):
            raise _reject("test_report_malformed", "Test phase entry is invalid.")
        for key in ("exc_type", "exc_mod"):
            value = entry.get(key)
            if value is not None and (not isinstance(value, str) or not value or len(value) > 100):
                raise _reject("test_report_malformed", "Test failure class is invalid.")
        if entry.get("wasxfail") is not None:
            # xfail/xpass substitution never counts; validated per stage.
            if not isinstance(entry.get("wasxfail"), str):
                raise _reject("test_report_malformed", "Test xfail marker is invalid.")
        if (nodeid, when) in phases:
            raise _reject("test_report_malformed", "Duplicate test phase entry.")
        phases[(nodeid, when)] = entry
    return phases


def validate_structure(report: dict, *, stage: str, attempt: int, expected_paths: list,
                       profile_hash: str, manifest_hash: str, runner_hash: str,
                       nonce: str | None, actual_exit: int) -> tuple[list, dict]:
    """Raise on any protocol/binding defect; return (collected, phases)."""
    if stage not in _STAGES or type(attempt) is not int or attempt not in (1, 2) \
            or not isinstance(expected_paths, list) or not expected_paths \
            or not all(isinstance(p, str) and p for p in expected_paths) \
            or not _HEX64.fullmatch(profile_hash or "") or not _HEX64.fullmatch(manifest_hash or "") \
            or not _HEX64.fullmatch(runner_hash or "") or type(actual_exit) is not int:
        raise _reject("test_report_malformed", "Test validation bindings are invalid.")
    _check_bindings(report, stage=stage, attempt=attempt, expected_paths=expected_paths,
                    profile_hash=profile_hash, manifest_hash=manifest_hash,
                    runner_hash=runner_hash, nonce=nonce, actual_exit=actual_exit)
    collected, deselected, collection_errors = _check_common_shape(report, expected_paths)
    if deselected or collection_errors:
        raise _reject("test_collection_failed",
                      "Test collection errors or deselection reject the stage.")
    phases = _phase_map(report["tests"])
    return collected, phases


def _node_records(phases: dict, nodeid: str):
    setup = phases.get((nodeid, "setup"))
    call = phases.get((nodeid, "call"))
    teardown = phases.get((nodeid, "teardown"))
    return setup, call, teardown


def _clean(record: dict | None) -> bool:
    return record is not None and record["outcome"] == "passed" \
        and record.get("exc_type") is None and record.get("wasxfail") is None


def check_collect(collected: list, phases: dict, *, exit_code: int) -> bool:
    """Collection-only evidence: nonempty inventory, nothing executed."""
    if exit_code != 0 or not collected or phases:
        return False
    return True


def check_before(collected: list, phases: dict, *, exit_code: int) -> bool:
    """At least one call-phase AssertionError, nothing else failing."""
    if exit_code != 1 or not collected:
        return False
    if len(phases) != 3 * len(collected):
        return False
    failures = 0
    for nodeid in collected:
        setup, call, teardown = _node_records(phases, nodeid)
        if setup is None or teardown is None or call is None:
            return False
        if not _clean(setup) or not _clean(teardown):
            return False
        if call.get("wasxfail") is not None or call["outcome"] == "skipped":
            return False
        if call["outcome"] == "failed":
            if call.get("exc_type") != "AssertionError" or call.get("exc_mod") != "builtins":
                return False
            failures += 1
        elif call["outcome"] != "passed" or call.get("exc_type") is not None:
            return False
    return failures >= 1


def _all_passed(collected: list, phases: dict, *, exit_code: int,
                required_nodes: list | None) -> bool:
    if exit_code != 0 or not collected:
        return False
    if required_nodes is not None and list(collected) != list(required_nodes):
        return False
    if len(phases) != 3 * len(collected):
        return False
    for nodeid in collected:
        setup, call, teardown = _node_records(phases, nodeid)
        if setup is None or call is None or teardown is None:
            return False
        if not _clean(setup) or not _clean(call) or not _clean(teardown):
            return False
    return True


def check_after(collected: list, phases: dict, *, exit_code: int, before_nodes: list) -> bool:
    if not before_nodes:
        return False
    return _all_passed(collected, phases, exit_code=exit_code, required_nodes=before_nodes)


def check_suite(collected: list, phases: dict, *, exit_code: int, baseline_nodes: list) -> bool:
    if not baseline_nodes:
        return False
    return _all_passed(collected, phases, exit_code=exit_code, required_nodes=baseline_nodes)


def redact_report_text(report: dict, secrets) -> dict:
    """Redact only free-form text; structural IDs/hashes/nodes stay exact."""
    if not secrets:
        return report
    import copy
    from .redaction import redact_text
    safe = copy.deepcopy(report)
    for entry in safe.get("tests") or []:
        if isinstance(entry, dict) and isinstance(entry.get("wasxfail"), str):
            entry["wasxfail"] = redact_text(entry["wasxfail"], secrets)
    return safe
