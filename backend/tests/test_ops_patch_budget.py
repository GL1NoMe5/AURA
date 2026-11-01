"""Output budgeting keeps a truncated model answer outside the execution path."""
import asyncio
import json

import httpx
import pytest
from openai import AsyncOpenAI

from test_ops_patches import Adapter, Runner, run_engine, candidate
from aura_agents.adapter import BASE_URL, MODEL, BasetenAdapter
from aura_ops.docker import CommandResult
from aura_ops.patches import _test_diagnostic_feedback
from aura_ops.test_runner import BEGIN_MARK, END_MARK


def test_large_report_does_not_displace_failure_feedback():
    nonce = "a" * 32
    diagnostic = "AssertionError: 200 != 404\nFAILED test_missing_file\n"
    private_value = "private-fixture-marker"
    payload = json.dumps({"rows": ["passed phase"] * 4000})
    stdout = diagnostic + f"{BEGIN_MARK} {nonce}\n{payload}\n{END_MARK} {nonce}\n"
    result = _test_diagnostic_feedback(CommandResult(1, stdout, private_value),
                                       ["--nonce", nonce], [private_value])
    assert diagnostic in result
    assert BEGIN_MARK not in result and "passed phase" not in result
    assert private_value not in result
    assert len(result) <= 8000


def test_retry_sees_failed_candidate_and_traceback(tmp_path):
    async def run():
        adapter = Adapter()
        runner = Runner({("original-suite", 1): {
            "mode": "single-fail", "log": "AssertionError: missing-file response changed\n"}})
        result, _, _ = await run_engine(tmp_path, adapter=adapter, runner=runner)
        assert result["status"] == "tests_passed"
        assert result["attempts"][0]["reason"] == "candidate_tests_failed"
        assert len(adapter.calls) == 2
        packet = json.loads(adapter.calls[1][1]["content"].split("EVIDENCE DATA:\n", 1)[1])
        assert packet["previous_candidate"] == candidate()
        assert "missing-file response changed" in packet["previous_attempt"]
        assert "failing stage: original-suite" in packet["previous_attempt"]
        assert "passed stages: 1-before, 1-after" in packet["previous_attempt"]
        assert BEGIN_MARK not in packet["previous_attempt"]
        assert packet["reference_tests"] == [{"path": "tests/test_app.py",
            "content": (tmp_path / "source/tests/test_app.py").read_bytes().decode("utf-8")}]
        schema = json.loads(adapter.calls[0][0]["content"].split("Exact JSON Schema:\n", 1)[1])
        assert schema["$defs"]["Edit"]["properties"]["path"]["enum"] == ["app.py"]
    asyncio.run(run())


@pytest.mark.parametrize("effort", [None, "medium"])
def test_effort_reaches_wire_without_changing_default(effort):
    async def run():
        bodies = []
        def handler(request):
            body = json.loads(request.content)
            bodies.append(body)
            return httpx.Response(200, json={"id": "fixture", "model": MODEL, "choices": [
                {"index": 0, "message": {"role": "assistant", "content": "{}"}, "finish_reason": "stop"}]})
        offline_auth = "fixture"
        client = AsyncOpenAI(api_key=offline_auth, base_url=BASE_URL,
                             http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), max_retries=0)
        adapter = BasetenAdapter(api_key=offline_auth, client=client)
        try:
            await adapter.complete([{"role": "user", "content": "test"}], max_tokens=16384,
                                   reasoning_effort=effort)
            assert len(bodies) == 1
            assert bodies[0]["model"] == MODEL
            assert bodies[0]["max_tokens"] == 16384
            if effort is None:
                assert "reasoning_effort" not in bodies[0]
            else:
                assert bodies[0]["reasoning_effort"] == effort
            with pytest.raises(ValueError):
                await adapter.complete([], reasoning_effort="max")
            assert len(bodies) == 1
        finally:
            await adapter.aclose()
    asyncio.run(run())


def test_schema_feedback_identifies_field_without_echoing_values(tmp_path):
    async def run():
        bad = candidate(summary=123)
        bad["untrusted-field-name"] = "untrusted-field-value"
        adapter = Adapter([bad, candidate()])
        result, _, events = await run_engine(tmp_path, adapter=adapter, runner=Runner())
        assert result["status"] == "tests_passed"
        issues = result["attempts"][0]["validation_issues"]
        assert {"type": "string_type", "field": "summary"} in issues
        assert {"type": "extra_forbidden", "field": "response"} in issues
        feedback = json.loads(adapter.calls[1][1]["content"].split("EVIDENCE DATA:\n", 1)[1])["previous_attempt"]
        assert "summary" in feedback and "string_type" in feedback
        combined = json.dumps(result) + json.dumps(events) + feedback
        assert "untrusted-field-name" not in combined
        assert "untrusted-field-value" not in combined
    asyncio.run(run())


def test_complete_looking_but_truncated_candidate_never_runs_tests(tmp_path):
    class Truncated(Adapter):
        async def complete(self, *args, **kwargs):
            result = await super().complete(*args, **kwargs)
            # Even valid-looking JSON is rejected if the provider reports length.
            result.finish_reason = "length"
            return result

    async def run():
        adapter, runner = Truncated(), Runner()
        result, source, _ = await run_engine(tmp_path, adapter=adapter, runner=runner)
        assert result["status"] == "needs_human"
        assert result["reason"] == "patch_response_truncated"
        assert len(adapter.calls) == 2
        assert runner.runs == []
        assert (source / "app.py").read_text() == "def answer():\n    return 0\n"
        assert "Return a smaller complete candidate" in adapter.calls[1][1]["content"]
        assert all(c["max_tokens"] == 16384 and c["reasoning_effort"] == "medium" for c in result["calls"])
    asyncio.run(run())
