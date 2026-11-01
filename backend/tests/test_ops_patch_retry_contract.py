"""Frozen-reproducer retry contract: edits-only candidate 2, safe diagnostics.

Offline only: fake adapters/runners and temporary stores (see
test_ops_patches fixtures). No provider, Docker, or registered target is used.
"""
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
from aura_ops.patches import PatchEngine
from aura_ops.store import OpsStore
from test_ops_patches import (REGRESSION, Runner, candidate, investigation,
                              run_engine, setup_source, target)

REGRESSION_PATH = "aura_generated_tests/test_aura_regression.py"
CHANGED_TEST = "from app import answer\n\ndef test_answer():\n    assert answer() == 999\n"
EDITS_ONLY = {"summary": "Correct the return value for the original suite.",
              "edits": [{"path": "app.py", "old": "return 0", "new": "return 42"}]}


def sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


class RawAdapter:
    """Returns exact contents (dicts dumped, strings verbatim); never strips."""

    ready = True

    def __init__(self, contents):
        self.contents = contents
        self.calls = []

    async def complete(self, messages, *, cancel_event, max_tokens, reasoning_effort):
        self.calls.append(messages)
        assert max_tokens == 16384
        assert reasoning_effort == "medium"
        assert cancel_event is not None
        value = self.contents[min(len(self.calls) - 1, len(self.contents) - 1)]
        content = value if isinstance(value, str) else json.dumps(value)
        return SimpleNamespace(content=content, requested_model=MODEL, response_model=MODEL,
                               finish_reason="stop", duration_ms=1.0, attempts=1, usage=None)


def _packet(adapter, index):
    return json.loads(adapter.calls[index][1]["content"].split("EVIDENCE DATA:\n", 1)[1])


def _schema(adapter, index):
    return json.loads(adapter.calls[index][0]["content"].split("Exact JSON Schema:\n", 1)[1])


def _assert_original_unchanged(source):
    assert (source / "app.py").read_text() == "def answer():\n    return 0\n"
    assert (source / "tests/test_app.py").read_text() == "def test_existing():\n    assert True\n"


def test_frozen_reproducer_carried_into_edits_only_candidate_two(tmp_path):
    async def run():
        adapter = RawAdapter([candidate(), dict(EDITS_ONLY)])
        runner = Runner({("original-suite", 1): {
            "mode": "single-fail", "log": "AssertionError: original suite still fails\n"}})
        result, source, _ = await run_engine(tmp_path, adapter=adapter, runner=runner)
        assert result["status"] == "tests_passed"
        assert result["attempts"][0]["reason"] == "candidate_tests_failed"
        # Candidate 2 used the smaller edits-only output contract.
        assert set(_schema(adapter, 1)["properties"]) == {"summary", "edits"}
        packet = _packet(adapter, 1)
        assert packet["retry_contract"] == "edits_only_frozen_regression"
        assert packet["frozen_regression_test"]["sha256"] == sha(REGRESSION.encode())
        assert packet["frozen_regression_test"]["content"] == REGRESSION
        assert packet["previous_candidate"] == candidate()
        # All stages freshly ran for candidate 2 (no reused attempt-1 reports).
        assert len(runner.runs) == 8
        assert [t["stage"] for t in result["attempts"][1]["tests"]] == [
            "2-before", "2-after", "2-original-suite"]
        assert all(t["gate_passed"] for t in result["attempts"][1]["tests"])
        # The exact frozen bytes, not any candidate-2 test text, were mounted.
        for _command, workspace in runner.runs[4:]:
            assert (workspace / REGRESSION_PATH).read_bytes() == REGRESSION.encode()
        assert result["regression_sha256"] == sha(REGRESSION.encode())
        assert result["attempts"][1]["contract"] == "edits_only_frozen_regression"
        assert result["attempts"][1]["frozen_regression_sha256"] == sha(REGRESSION.encode())
        # Context and output-token bounds held on every call.
        for messages in adapter.calls:
            assert len(messages[1]["content"]) <= 160000 + len("EVIDENCE DATA:\n")
        assert all(c["max_tokens"] == 16384 for c in result["calls"])
        _assert_original_unchanged(source)
    asyncio.run(run())


def test_full_contract_remains_when_first_reproducer_does_not_pass_both(tmp_path):
    async def run():
        second_test = REGRESSION.replace("assert answer() == 42",
                                         "assert answer() == 42\n    assert answer() != 0")
        adapter = RawAdapter([candidate(), candidate(regression_test=second_test)])
        # Before stage passes on old code: no reproducer, so no freeze.
        runner = Runner({"before": {"mode": "all-pass"}})
        result, source, _ = await run_engine(tmp_path, adapter=adapter, runner=runner)
        assert result["status"] == "needs_human"
        assert result["reason"] == "reproducer_not_confirmed"
        assert set(_schema(adapter, 1)["properties"]) == {"summary", "edits", "regression_test"}
        packet = _packet(adapter, 1)
        assert packet["retry_contract"] == "full_candidate"
        assert packet["frozen_regression_test"] is None
        # Candidate 2 supplied its own new regression bytes in its workspace.
        assert len(runner.runs) == 4  # collect + before, per attempt
        assert (runner.runs[3][1] / REGRESSION_PATH).read_bytes() == second_test.encode()
        _assert_original_unchanged(source)
    asyncio.run(run())


@pytest.mark.parametrize("field", ["regression_test", "test_content", "regression"])
def test_invented_or_changed_test_fields_in_edits_only_contract_rejected(tmp_path, field):
    async def run():
        payload = dict(EDITS_ONLY)
        payload[field] = CHANGED_TEST
        adapter = RawAdapter([candidate(), payload])
        runner = Runner({("original-suite", 1): {"mode": "single-fail"}})
        result, source, _ = await run_engine(tmp_path, adapter=adapter, runner=runner)
        assert result["status"] == "needs_human"
        assert result["reason"] == "patch_schema_invalid"
        assert result["attempts"][1]["contract"] == "edits_only_frozen_regression"
        assert any(i["type"] == "extra_forbidden" for i in result["attempts"][1]["validation_issues"])
        assert len(runner.runs) == 4  # candidate 2 never reached test execution
        assert CHANGED_TEST not in json.dumps(result)
        _assert_original_unchanged(source)
    asyncio.run(run())


@pytest.mark.parametrize("payload,reason", [
    ({"summary": "Missing edits entirely."}, "patch_schema_invalid"),
    ({"summary": "Empty edits.", "edits": []}, "patch_schema_invalid"),
    ({"summary": "Incomplete edit.", "edits": [{"path": "app.py", "old": "return 0"}]},
     "patch_schema_invalid"),
    ({"summary": "Duplicate edits.", "edits": [
        {"path": "app.py", "old": "return 0", "new": "return 42"},
        {"path": "app.py", "old": "return 0", "new": "return 43"}]},
     "patch_duplicate_edit"),
])
def test_missing_or_duplicate_edits_stay_rejected_in_edits_only_contract(tmp_path, payload, reason):
    async def run():
        adapter = RawAdapter([candidate(), payload])
        runner = Runner({("original-suite", 1): {"mode": "single-fail"}})
        result, source, _ = await run_engine(tmp_path, adapter=adapter, runner=runner)
        assert result["status"] == "needs_human"
        assert result["reason"] == reason
        assert len(runner.runs) == 4
        _assert_original_unchanged(source)
    asyncio.run(run())


def test_failed_fresh_candidate_two_stages_are_not_discarded(tmp_path):
    async def run():
        adapter = RawAdapter([candidate(), dict(EDITS_ONLY)])
        runner = Runner({("original-suite", 1): {"mode": "single-fail"},
                         ("after", 2): {"mode": "value-fail"}})
        result, source, _ = await run_engine(tmp_path, adapter=adapter, runner=runner)
        assert result["status"] == "needs_human"
        assert result["reason"] == "candidate_tests_failed"
        assert "artifact" not in result
        stages = result["attempts"][1]["tests"]
        assert [t["stage"] for t in stages] == ["2-before", "2-after"]
        assert stages[-1]["gate_passed"] is False
        assert len(runner.runs) == 7  # 4 for attempt 1, collect+before+after for attempt 2
        _assert_original_unchanged(source)
    asyncio.run(run())


def test_malformed_json_diagnostics_are_safe_and_bounded(tmp_path):
    async def run():
        secret = "ADVERSARIAL_SECRET_VALUE_9f3c"
        field_name = "evil_field_name_9f3c"
        raw = ('{\n  "summary": "' + secret + '",\n  "' + field_name + '": "' + secret + '",\n'
               '  "edits": [}\n')
        adapter = RawAdapter([raw, raw])
        result, source, events = await run_engine(tmp_path, adapter=adapter, runner=Runner())
        assert result["status"] == "needs_human"
        assert result["reason"] == "patch_schema_invalid"
        diag = result["attempts"][0]["output_diagnostic"]
        assert diag["category"] == "json_invalid"
        assert isinstance(diag["json_line"], int) and diag["json_line"] >= 2
        assert isinstance(diag["json_column"], int) and diag["json_column"] >= 1
        assert diag["response_chars"] == len(raw)
        assert diag["response_sha256"] == sha(raw.encode())
        combined = json.dumps(result) + json.dumps(events)
        feedback = _packet(adapter, 1)["previous_attempt"]
        assert "json_invalid" in feedback
        for leaked in (secret, field_name):
            assert leaked not in combined
            assert leaked not in feedback
            assert leaked not in json.dumps(result["attempts"][0]["validation_issues"])
        for path in (tmp_path / "ops").rglob("*"):
            if path.is_file():
                assert secret.encode() not in path.read_bytes()
                assert field_name.encode() not in path.read_bytes()
        _assert_original_unchanged(source)
    asyncio.run(run())


def test_cancellation_still_removes_owned_container(tmp_path):
    async def run():
        source = setup_source(tmp_path)
        store = OpsStore(tmp_path / "ops")
        runner = Runner()
        runner.hang = True
        cancelled = asyncio.Event()

        async def emit(*args):
            pass
        try:
            task = asyncio.create_task(PatchEngine(
                store, RawAdapter([candidate()]), tmp_path / "ops", runner).prepare(
                target(source), investigation(source), cancelled, emit))
            await asyncio.wait_for(runner.started.wait(), 3)
            cancelled.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert len(runner.removed) == 1
            assert not runner.containers
            _assert_original_unchanged(source)
        finally:
            store.close()
    asyncio.run(run())
