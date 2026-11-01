"""Offline tests for AGENT-001 using httpx mock transports only.

No live provider requests: the AsyncOpenAI client is backed by an
httpx MockTransport. The PNG fixture is test-only chart input.
"""
from __future__ import annotations

import asyncio
import base64
import json
import sys
import time
from pathlib import Path

import httpx
import pytest
from openai import AsyncOpenAI

_BACKEND = Path(__file__).resolve().parents[1]
_REPO = _BACKEND.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from aura_agents import BasetenAdapter, BasetenSwarmOrchestrator  # noqa: E402
from aura_agents.adapter import BASE_URL, MODEL, ProviderCallError  # noqa: E402
from aura_api.orchestration import (  # noqa: E402
    EvidenceBundle,
    OrchestratorUnavailableError,
)

FIXTURE_PNG = (
    _REPO / "tools" / "preflight" / "fixtures" / "vision-probe.png"
).read_bytes()
AGENTS = ("logs", "memory", "config", "vision")


def _evidence(**overrides) -> EvidenceBundle:
    values = dict(
        run_id="run-test", scenario="cache_retention", worker_generation=1,
        measured_at="2026-10-27T00:00:00+00:00", baseline_rss_bytes=40_000_000,
        rss_bytes=70_000_000, rss_fresh=True,
        retained_cache_bytes=60 * 1024 * 1024,
        healthy_cache_bytes=16 * 1024 * 1024,
        ceiling_bytes=96 * 1024 * 1024,
        rss_threshold_bytes=40_000_000 + 24 * 1024 * 1024,
        completed_jobs=140, failed_jobs=3, worker_state="fault_detected",
        recent_measurements=[{"rss_bytes": 70_000_000}],
        worker_config={"eviction_enabled": False, "block_count": 30},
        worker_config_source="worker_status", eviction_enabled=False,
        status_fresh=True, status_timestamp="2026-10-27T00:00:00+00:00",
        sampled_telemetry=[{"rss_bytes": 70_000_000}],
        diagnostic_logs=[], diagnostic_logs_source="unavailable",
        evidence_ids=["telemetry.current", "telemetry.recent",
                      "worker.status", "worker.config", "chart.measured"],
    )
    values.update(overrides)
    return EvidenceBundle(**values)


class FakeSink:
    def __init__(self):
        self.events: list[tuple] = []

    async def agent_started(self, agent_id):
        self.events.append(("started", agent_id))

    async def agent_message(self, agent_id, message, kind="status"):
        self.events.append(("message", agent_id, kind))

    async def agent_finished(self, agent_id, status, summary=None):
        self.events.append(("finished", agent_id, status))


class Recorder:
    def __init__(self):
        self.requests: list[dict] = []
        self.counts: dict[str, int] = {}
        self.in_flight = 0
        self.max_in_flight = 0
        self.completed_order: list[str] = []


def _agent_of(body: dict) -> str:
    for message in body.get("messages", []):
        content = message.get("content")
        texts = []
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):
            texts.extend(
                part.get("text", "") for part in content
                if isinstance(part, dict))
        for text in texts:
            if "SPECIALIST:" in text:
                return text.split("SPECIALIST:", 1)[1].strip().split()[0]
    return "unknown"


def _specialist_json(agent: str) -> str:
    refs = {
        "logs": ["telemetry.current", "diagnostic.logs"],
        "memory": ["telemetry.current", "telemetry.recent"],
        "config": ["worker.config", "worker.status"],
        "vision": ["chart.measured", "telemetry.current"],
    }
    return json.dumps({
        "agent_id": agent, "status": "completed",
        "summary": f"{agent} found the measured fault",
        "findings": [f"{agent} evidence supports cache retention"],
        "evidence_refs": refs[agent],
    })


def _completion(content: str, *, usage=True, reasoning="actual reason",
                finish_reason="stop") -> dict:
    message: dict = {"role": "assistant", "content": content}
    if reasoning is not None:
        message["reasoning_content"] = reasoning
    payload: dict = {
        "id": "chatcmpl-test", "object": "chat.completion", "model": MODEL,
        "choices": [{"index": 0, "message": message,
                     "finish_reason": finish_reason}],
    }
    if usage:
        payload["usage"] = {
            "prompt_tokens": 100, "completion_tokens": 40,
            "total_tokens": 140,
            "completion_tokens_details": {"reasoning_tokens": 10}}
    return payload


def _make_orchestrator(handler, *, renderer=None, adapter_kwargs=None,
                       evidence_dir=None):
    recorder_holder = {}
    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler))
    client = AsyncOpenAI(
        api_key="test-key-not-real", base_url=BASE_URL,
        http_client=http_client, max_retries=0, timeout=10.0)
    adapter = BasetenAdapter(
        api_key="test-key-not-real", client=client,
        backoff_seconds=0.01, **(adapter_kwargs or {}))
    orchestrator = BasetenSwarmOrchestrator(
        adapter,
        chart_renderer=renderer or (lambda evidence: FIXTURE_PNG),
        evidence_dir=evidence_dir)
    recorder_holder["client"] = client
    return orchestrator


def _default_handler(recorder: Recorder, *, delay: float = 0.0,
                     overrides: dict | None = None):
    overrides = overrides or {}

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        agent = _agent_of(body)
        recorder.counts[agent] = recorder.counts.get(agent, 0) + 1
        recorder.requests.append(
            {"agent": agent, "url": str(request.url), "body": body})
        recorder.in_flight += 1
        recorder.max_in_flight = max(
            recorder.max_in_flight, recorder.in_flight)
        try:
            if delay:
                await asyncio.sleep(delay)
            behavior = overrides.get(agent)
            if behavior is not None:
                result = behavior(recorder.counts[agent], body)
                if asyncio.iscoroutine(result):
                    result = await result
                return result
            if agent == "synthesis":
                content = json.dumps({
                    "action": "restore_cache_policy",
                    "reason": "all specialists support restore",
                    "evidence_refs": ["telemetry.current"]})
            else:
                content = _specialist_json(agent)
            return httpx.Response(200, json=_completion(content))
        finally:
            recorder.in_flight -= 1
            recorder.completed_order.append(agent)

    return handler


# ---------------------------------------------------------------- tests

def test_five_calls_exact_url_model_and_concurrency():
    async def run():
        recorder = Recorder()
        orchestrator = _make_orchestrator(
            _default_handler(recorder, delay=0.25))
        sink = FakeSink()
        outcome = await orchestrator.diagnose(
            _evidence(), asyncio.Event(), sink)
        await orchestrator.aclose()
        assert [f.agent_id for f in outcome.findings] == list(AGENTS)
        assert outcome.proposal.action == "restore_cache_policy"
        assert recorder.counts == {
            "logs": 1, "memory": 1, "config": 1, "vision": 1,
            "synthesis": 1}
        assert all(r["url"] == f"{BASE_URL}/chat/completions"
                   for r in recorder.requests)
        assert all(r["body"]["model"] == MODEL for r in recorder.requests)
        assert all(r["body"]["stream"] is False for r in recorder.requests)
        assert recorder.max_in_flight >= 3  # real specialist concurrency
        # Synthesis dispatched only after all specialists completed.
        assert recorder.requests[-1]["agent"] == "synthesis"
        first = outcome.findings[0]
        assert first.requested_model == MODEL
        assert first.response_model == MODEL
        assert first.attempts == 1 and first.duration_ms is not None
        assert first.usage is not None and first.usage.total_tokens == 140
        assert first.reasoning == "actual reason"
        kinds = {(e[1], e[2]) for e in sink.events if e[0] == "message"}
        assert ("logs", "reasoning") in kinds
        assert ("logs", "finding") in kinds
    asyncio.run(run())


def test_vision_payload_is_bounded_png_data_url():
    async def run():
        recorder = Recorder()
        orchestrator = _make_orchestrator(_default_handler(recorder))
        await orchestrator.diagnose(_evidence(), asyncio.Event(), FakeSink())
        await orchestrator.aclose()
        vision = next(r for r in recorder.requests if r["agent"] == "vision")
        parts = vision["body"]["messages"][1]["content"]
        image = next(p for p in parts if p["type"] == "image_url")
        url = image["image_url"]["url"]
        assert url.startswith("data:image/png;base64,")
        assert base64.b64decode(url.split(",", 1)[1]) == FIXTURE_PNG
        assert len(json.dumps(vision["body"])) < 200_000
        for request in recorder.requests:
            text = json.dumps(request["body"])
            assert "test-key-not-real" not in text
    asyncio.run(run())


def test_nullable_usage_and_reasoning_stay_null():
    async def run():
        recorder = Recorder()

        def no_usage(_count, _body):
            agent = _agent_of(_body)
            content = (_specialist_json(agent) if agent != "synthesis"
                       else json.dumps({
                           "action": "abstain", "reason": "uncertain",
                           "evidence_refs": []}))
            return httpx.Response(
                200, json=_completion(content, usage=False, reasoning=None))

        orchestrator = _make_orchestrator(
            _default_handler(recorder,
                             overrides={a: no_usage for a in
                                        AGENTS + ("synthesis",)}))
        outcome = await orchestrator.diagnose(
            _evidence(), asyncio.Event(), FakeSink())
        await orchestrator.aclose()
        assert all(f.usage is None and f.reasoning is None
                   for f in outcome.findings)
        assert outcome.proposal.action == "abstain"
        assert outcome.proposal.usage is None
    asyncio.run(run())


@pytest.mark.parametrize(("agent", "content", "finish"), [
    ("memory", "not json at all", "stop"),
    ("memory", json.dumps({
        "agent_id": "logs", "status": "completed", "summary": "s",
        "findings": ["f"], "evidence_refs": []}), "stop"),
    ("memory", json.dumps({
        "agent_id": "memory", "status": "completed", "summary": "s",
        "findings": ["f"], "evidence_refs": ["made.up.ref"]}), "stop"),
    ("memory", json.dumps({
        "agent_id": "memory", "status": "incomplete", "summary": "s",
        "findings": [], "evidence_refs": []}), "stop"),
    ("memory", _specialist_json("memory"), "length"),
    ("memory", "", "stop"),
])
def test_strict_specialist_output_fails_closed(agent, content, finish):
    async def run():
        recorder = Recorder()

        def bad(_count, _body):
            return httpx.Response(200, json=_completion(
                content, finish_reason=finish))

        orchestrator = _make_orchestrator(
            _default_handler(recorder, overrides={agent: bad}))
        with pytest.raises(Exception):
            await orchestrator.diagnose(
                _evidence(), asyncio.Event(), FakeSink())
        await orchestrator.aclose()
        assert recorder.counts.get("synthesis", 0) == 0
        assert recorder.counts[agent] == 1  # no schema retry / repair loop
    asyncio.run(run())


def test_unknown_synthesis_action_fails_closed():
    async def run():
        recorder = Recorder()

        def bad_synthesis(_count, _body):
            return httpx.Response(200, json=_completion(json.dumps({
                "action": "restart_everything", "reason": "x",
                "evidence_refs": []})))

        orchestrator = _make_orchestrator(_default_handler(
            recorder, overrides={"synthesis": bad_synthesis}))
        with pytest.raises(Exception):
            await orchestrator.diagnose(
                _evidence(), asyncio.Event(), FakeSink())
        await orchestrator.aclose()
        assert recorder.counts["synthesis"] == 1
    asyncio.run(run())


def test_missing_key_and_missing_renderer_not_ready(monkeypatch):
    monkeypatch.delenv("BASETEN_API_KEY", raising=False)
    no_key = BasetenSwarmOrchestrator(
        BasetenAdapter(api_key="", client=object()),
        chart_renderer=lambda evidence: FIXTURE_PNG)
    assert no_key.ready is False
    no_renderer = BasetenSwarmOrchestrator(
        BasetenAdapter(api_key="test-key", client=object()),
        chart_renderer=None)
    assert no_renderer.ready is False

    async def run():
        recorder = Recorder()
        orchestrator = _make_orchestrator(
            _default_handler(recorder),
            renderer=lambda evidence: (_ for _ in ()).throw(
                RuntimeError("renderer exploded")))
        with pytest.raises(Exception):
            await orchestrator.diagnose(
                _evidence(), asyncio.Event(), FakeSink())
        await orchestrator.aclose()
        assert recorder.requests == []  # failure before any dispatch

        not_png = _make_orchestrator(
            _default_handler(recorder), renderer=lambda evidence: b"nope")
        with pytest.raises(Exception):
            await not_png.diagnose(_evidence(), asyncio.Event(), FakeSink())
        await not_png.aclose()
        assert recorder.requests == []
    asyncio.run(run())


def test_transient_429_retried_once_then_succeeds():
    async def run():
        recorder = Recorder()

        def flaky(count, _body):
            if count == 1:
                return httpx.Response(429, json={"error": {"message": "slow"}})
            return httpx.Response(
                200, json=_completion(_specialist_json("logs")))

        orchestrator = _make_orchestrator(_default_handler(
            recorder, overrides={"logs": flaky}))
        outcome = await orchestrator.diagnose(
            _evidence(), asyncio.Event(), FakeSink())
        await orchestrator.aclose()
        assert recorder.counts["logs"] == 2
        logs = next(f for f in outcome.findings if f.agent_id == "logs")
        assert logs.attempts == 2
    asyncio.run(run())


def test_auth_error_never_retried_and_no_synthesis():
    async def run():
        recorder = Recorder()

        def denied(_count, _body):
            return httpx.Response(401, json={"error": {"message": "bad key"}})

        orchestrator = _make_orchestrator(_default_handler(
            recorder, overrides={a: denied for a in AGENTS}))
        with pytest.raises(ProviderCallError) as excinfo:
            await orchestrator.diagnose(
                _evidence(), asyncio.Event(), FakeSink())
        await orchestrator.aclose()
        assert excinfo.value.category == "auth_error"
        assert max(recorder.counts.values()) == 1
        assert recorder.counts.get("synthesis", 0) == 0
        assert "test-key" not in str(excinfo.value)
    asyncio.run(run())


def test_partial_failure_cancels_siblings_no_synthesis():
    async def run():
        recorder = Recorder()

        async def slow(_count, _body):
            await asyncio.sleep(10)
            return httpx.Response(
                200, json=_completion(_specialist_json("memory")))

        def bad(_count, _body):
            return httpx.Response(500, json={"error": {"message": "boom"}})

        orchestrator = _make_orchestrator(_default_handler(
            recorder, overrides={"memory": slow, "logs": bad}),
            adapter_kwargs={})
        started = time.monotonic()
        with pytest.raises(ProviderCallError):
            await orchestrator.diagnose(
                _evidence(), asyncio.Event(), FakeSink())
        await orchestrator.aclose()
        assert time.monotonic() - started < 8  # siblings were cancelled
        assert recorder.counts.get("synthesis", 0) == 0
    asyncio.run(run())


def test_cancellation_during_requests_and_no_new_dispatch():
    async def run():
        recorder = Recorder()

        async def hanging(_count, _body):
            await asyncio.sleep(30)
            return httpx.Response(200, json=_completion("{}"))

        orchestrator = _make_orchestrator(_default_handler(
            recorder, overrides={a: hanging for a in AGENTS}))
        cancel = asyncio.Event()
        task = asyncio.create_task(orchestrator.diagnose(
            _evidence(), cancel, FakeSink()))
        deadline = time.monotonic() + 5
        while not recorder.requests:
            assert time.monotonic() < deadline
            await asyncio.sleep(0.02)
        cancel.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5)
        counts_after = dict(recorder.counts)
        await asyncio.sleep(0.2)
        assert recorder.counts == counts_after  # no new dispatch
        assert recorder.counts.get("synthesis", 0) == 0
        await orchestrator.aclose()
    asyncio.run(run())


class _StatusError(Exception):
    status_code = 429


class _FakeCompletions:
    def __init__(self):
        self.calls = 0

    async def create(self, **_kwargs):
        self.calls += 1
        raise _StatusError("rate limited")


class _FakeClient:
    def __init__(self):
        self.completions = _FakeCompletions()
        self.chat = type("Chat", (), {"completions": self.completions})()
        self.closed = False

    async def close(self):
        self.closed = True


def test_cancellation_during_retry_backoff():
    async def run():
        client = _FakeClient()
        adapter = BasetenAdapter(
            api_key="test-key", client=client, backoff_seconds=30.0)
        cancel = asyncio.Event()
        task = asyncio.create_task(adapter.complete(
            [{"role": "user", "content": "x"}], cancel_event=cancel))
        deadline = time.monotonic() + 5
        while client.completions.calls == 0:
            assert time.monotonic() < deadline
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.1)  # now inside the 30 s backoff
        cancel.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5)
        assert client.completions.calls == 1  # retry never dispatched
        await adapter.aclose()
        assert client.closed is True  # client cleanup on shutdown
    asyncio.run(run())


def test_default_client_disables_sdk_retries(monkeypatch):
    captured = {}

    class FakeAsyncOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr("openai.AsyncOpenAI", FakeAsyncOpenAI)
    adapter = BasetenAdapter(api_key="test-key")
    adapter._ensure_client()
    assert captured["base_url"] == BASE_URL
    assert captured["max_retries"] == 0
    assert captured["timeout"] > 0


def test_evidence_record_saved_bounded_without_secrets(tmp_path):
    async def run():
        recorder = Recorder()
        orchestrator = _make_orchestrator(
            _default_handler(recorder), evidence_dir=tmp_path)
        await orchestrator.diagnose(_evidence(), asyncio.Event(), FakeSink())
        await orchestrator.aclose()
        saved = list(tmp_path.glob("run-*.json"))
        assert len(saved) == 1
        text = saved[0].read_text(encoding="utf-8")
        assert "test-key-not-real" not in text
        assert "Authorization" not in text
        data = json.loads(text)
        assert data["proposal"]["action"] == "restore_cache_policy"
        assert len(data["call_metadata"]) == 5
    asyncio.run(run())


# ------------------------------------------------------- INTEGRATION-001

def test_bounded_packet_remains_valid_json_with_huge_evidence():
    from aura_agents.prompts import (
        MAX_PACKET_CHARS,
        build_specialist_messages,
        build_synthesis_messages,
    )
    from aura_api.orchestration import SpecialistFinding

    rows = [{
        "timestamp": "t", "monotonic": float(i), "rss_bytes": 70_000_000,
        "retained_cache_bytes": 60 * 1024 * 1024, "completed_jobs": i,
        "failed_jobs": 1, "rss_fresh": True, "status_fresh": True,
        "blob": "x" * 5000,
    } for i in range(200)]
    evidence = _evidence(
        sampled_telemetry=rows, recent_measurements=rows,
        diagnostic_logs=[{"source": "worker_stderr", "text": "y" * 5000}
                         for _ in range(50)])
    for agent_id in AGENTS:
        messages, _refs = build_specialist_messages(evidence, agent_id)
        packet_text = messages[1]["content"].split(
            "EVIDENCE PACKET (JSON): ", 1)[1]
        parsed = json.loads(packet_text)  # must never be sliced mid-object
        assert isinstance(parsed, dict)
        assert len(packet_text) <= MAX_PACKET_CHARS
    findings = [SpecialistFinding(
        agent_id=a, status="completed", summary="s", findings=["f"],
        evidence_refs=["telemetry.current"]) for a in AGENTS]
    synth_messages, allowed = build_synthesis_messages(evidence, findings)
    assert "ALLOWED EVIDENCE REFS:" in synth_messages[1]["content"]
    assert "finding:logs" in synth_messages[1]["content"]
    assert allowed >= {"telemetry.current", "finding:logs"}


def test_long_reasoning_truncated_with_flag_not_rejected():
    async def run():
        recorder = Recorder()

        def long_reasoning(_count, body):
            agent = _agent_of(body)
            content = (_specialist_json(agent) if agent != "synthesis"
                       else json.dumps({
                           "action": "abstain", "reason": "done",
                           "evidence_refs": []}))
            return httpx.Response(200, json=_completion(
                content, reasoning="r" * 9000))

        orchestrator = _make_orchestrator(_default_handler(
            recorder, overrides={a: long_reasoning
                                 for a in AGENTS + ("synthesis",)}))
        outcome = await orchestrator.diagnose(
            _evidence(), asyncio.Event(), FakeSink())
        await orchestrator.aclose()
        assert all(len(f.reasoning) == 8000 and f.reasoning_truncated
                   for f in outcome.findings)
        assert outcome.proposal.reasoning_truncated is True
    asyncio.run(run())


def test_attempt_wall_clock_deadline_bounds_hanging_call():
    async def run():
        recorder = Recorder()

        async def hanging(_count, _body):
            await asyncio.sleep(10)
            return httpx.Response(200, json=_completion("{}"))

        http_client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                _default_handler(recorder, overrides={"logs": hanging})))
        client = AsyncOpenAI(
            api_key="test-key", base_url=BASE_URL,
            http_client=http_client, max_retries=0, timeout=10.0)
        adapter = BasetenAdapter(
            api_key="test-key", client=client, backoff_seconds=0.01,
            attempt_deadline_seconds=0.2)
        started = time.monotonic()
        with pytest.raises(ProviderCallError) as excinfo:
            await adapter.complete(
                [{"role": "user", "content": "SPECIALIST: logs\nx"}])
        assert excinfo.value.category == "timeout"
        assert excinfo.value.attempts == 2  # one bounded retry, then stop
        assert time.monotonic() - started < 5
        await adapter.aclose()
    asyncio.run(run())
