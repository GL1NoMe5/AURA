"""Bounded real orchestration with a fake provider; no live requests."""
import asyncio
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from aura_agents.adapter import MODEL, ProviderOutputError
from aura_ops.investigator import OpsInvestigator
from aura_ops.models import TargetSpec


def spec():
    return TargetSpec(target_id="alpha", name="Alpha", container_name="alpha",
                      probes=[{"url": "http://127.0.0.1:18090/health"}])


def answer(value):
    return SimpleNamespace(content=json.dumps(value) if not isinstance(value, str) else value,
        finish_reason="stop", requested_model=MODEL, response_model=MODEL,
        duration_ms=1, attempts=1, usage=None)


def role_output(ref="observation.current", refresh=False):
    return {"summary": "Observed evidence", "findings": [{"hypothesis": "Needs review",
        "confidence": "possible", "evidence_refs": [ref]}], "request_sources": [], "request_refresh": refresh}


def synthesis(action="abstain"):
    return {"summary": "No confirmed correction", "findings": [], "action": action,
            "reason": "Insufficient evidence", "needs_source_analysis": False}


class Adapter:
    ready = True

    def __init__(self, callback=None):
        self.messages = []
        self.callback = callback

    async def complete(self, messages, **kwargs):
        self.messages.append(messages)
        if self.callback:
            return await self.callback(messages, kwargs)
        return answer(synthesis() if "Synthesize the evidence" in messages[0]["content"] else role_output())

    async def aclose(self):
        pass


async def emit(*args):
    pass


def test_citation_schema_lists_exact_available_ids_for_each_round():
    async def run():
        schemas = []
        async def callback(messages, kwargs):
            packet = json.loads(messages[1]['content'].removeprefix('EVIDENCE DATA:\n'))
            declared = json.loads(messages[0]['content'].split('including array/string limits:\n')[1])
            choices = declared['$defs']['Finding']['properties']['evidence_refs']['items']['enum']
            assert choices == sorted(packet)
            assert 'observation.current.probes' not in choices
            schemas.append(choices)
            return answer(synthesis() if 'Synthesize the evidence' in messages[0]['content'] else role_output())
        await OpsInvestigator(Adapter(callback)).diagnose(spec(), {'observation.current': {}}, asyncio.Event(), emit)
        assert schemas[0] == ['observation.current']
        assert 'specialist.findings' in schemas[-1]
    asyncio.run(run())


def test_prompt_exposes_limits_and_rejection_diagnostics_exclude_model_input():
    async def run():
        events = []
        async def record(kind, payload):
            events.append((kind, payload))
        async def callback(messages, kwargs):
            assert '"maxItems":4' in messages[0]["content"]
            value = role_output()
            value["findings"] *= 5
            value["summary"] = "private-canary-that-must-not-be-logged"
            return answer(value)
        with pytest.raises(ProviderOutputError):
            await OpsInvestigator(Adapter(callback)).diagnose(spec(), {"observation.current": {}},
                                                             asyncio.Event(), record)
        rejected = [v for k, v in events if k == "provider_output_rejected"]
        assert rejected and rejected[0]["issues"] == [{"type": "too_long", "field": "findings"}]
        assert "private-canary" not in json.dumps(events)
    asyncio.run(run())


def test_two_followup_rounds_are_bounded_and_measurements_are_preserved():
    async def run():
        async def callback(messages, kwargs):
            assert kwargs["max_tokens"] == 8192
            return answer(synthesis() if "Synthesize the evidence" in messages[0]["content"] else role_output(refresh=True))
        adapter = Adapter(callback)
        refreshes = []
        async def refresh():
            refreshes.append(1)
            return {"sample": len(refreshes)}
        result = await OpsInvestigator(adapter).diagnose(spec(), {"observation.current": {}}, asyncio.Event(), emit, refresh)
        assert len(refreshes) == 2
        assert len(adapter.messages) == 7  # Two roles x three rounds, then synthesis.
        assert len(result["calls"]) == 7
        assert result["evidence"]["observation.followup2"] == {"sample": 2}
        assert "observation.followup3" not in result["evidence"]
    asyncio.run(run())


def test_truncated_synthesis_is_rejected_even_when_json_would_be_valid():
    async def run():
        events = []
        async def record(kind, payload):
            events.append((kind, payload))
        async def callback(messages, kwargs):
            assert kwargs['max_tokens'] == 8192
            result = answer(synthesis() if 'Synthesize the evidence' in messages[0]['content'] else role_output())
            if 'Synthesize the evidence' in messages[0]['content']:
                result.finish_reason = 'length'
            return result
        adapter = Adapter(callback)
        with pytest.raises(ProviderOutputError, match='incomplete_response'):
            await OpsInvestigator(adapter).diagnose(spec(), {'observation.current': {}}, asyncio.Event(), record)
        assert len(adapter.messages) == 3  # No unbounded retries or substitute decision.
        assert [v for k, v in events if k == 'provider_usage'][-1]['finish_reason'] == 'length'
        assert [v for k, v in events if k == 'provider_output_rejected'][-1]['finish_reason'] == 'length'
        assert not any(k == 'agent_finished' and v['agent_id'] == 'synthesis' for k, v in events)
    asyncio.run(run())


@pytest.mark.parametrize("bad", ["not JSON", '{"summary":123}', "unknown_reference", "deploy", "uncited_action"])
def test_invalid_provider_data_never_authorizes_action(bad):
    async def run():
        async def callback(messages, kwargs):
            is_synthesis = "Synthesize the evidence" in messages[0]["content"]
            if bad == "deploy":
                return answer(synthesis("deploy_verified_patch") if is_synthesis else role_output())
            if bad == "uncited_action":
                return answer(synthesis("restart_container") if is_synthesis else role_output())
            if bad == "unknown_reference":
                return answer(role_output("invented.hidden.truth"))
            return answer(bad)
        with pytest.raises(ProviderOutputError):
            await OpsInvestigator(Adapter(callback)).diagnose(spec(), {"observation.current": {}}, asyncio.Event(), emit)
    asyncio.run(run())


def test_failure_cancels_and_awaits_parallel_provider_sibling():
    async def run():
        sibling_started, sibling_cancelled = asyncio.Event(), asyncio.Event()
        never = asyncio.Event()
        async def callback(messages, kwargs):
            if "Assess resources" in messages[0]["content"]:
                await sibling_started.wait()
                raise ProviderOutputError("invalid_ops_json")
            sibling_started.set()
            try:
                await never.wait()
            except asyncio.CancelledError:
                sibling_cancelled.set()
                raise
        try:
            with pytest.raises(ProviderOutputError):
                await OpsInvestigator(Adapter(callback)).diagnose(spec(), {"observation.current": {}}, asyncio.Event(), emit)
            assert sibling_cancelled.is_set(), "Sibling provider call survived terminal investigation failure"
        finally:
            never.set()
            await asyncio.sleep(0)
    asyncio.run(run())


def test_precancelled_investigation_dispatches_nothing():
    async def run():
        adapter = Adapter()
        cancel = asyncio.Event()
        cancel.set()
        with pytest.raises(asyncio.CancelledError):
            await OpsInvestigator(adapter).diagnose(spec(), {"observation.current": {}}, cancel, emit)
        assert adapter.messages == []
    asyncio.run(run())


@pytest.mark.parametrize("source_image,observed_image,should_read", [
    (None, "sha256:" + "b" * 64, False),
    ("sha256:" + "a" * 64, "sha256:" + "b" * 64, False),
    ("sha256:" + "b" * 64, "sha256:" + "b" * 64, True),
])
def test_source_inspection_requires_binding_to_observed_image(monkeypatch, tmp_path,
                                                            source_image, observed_image, should_read):
    import aura_ops.investigator as module
    reads = []
    class Index:
        files = {"app.py": "c" * 64}
        def __init__(self, target):
            reads.append("index")
        def inventory(self):
            return {"revision": "a" * 40, "files": ["app.py"]}
        def read(self, path):
            reads.append(path)
            return {"path": path, "content": "def health(): return True"}
    monkeypatch.setattr(module, "SourceIndex", Index)
    async def run():
        target = spec().model_copy(update={"source_dir": str(tmp_path),
            "source_revision": "a" * 40, "source_image_id": source_image})
        async def callback(messages, kwargs):
            if "Synthesize the evidence" in messages[0]["content"]:
                return answer(synthesis())
            output = role_output()
            output["request_sources"] = ["app.py"]
            return answer(output)
        result = await OpsInvestigator(Adapter(callback)).diagnose(target,
            {"observation.current": {"identity": {"image_id": observed_image}}}, asyncio.Event(), emit)
        if should_read:
            assert reads == ["index", "app.py"]
            assert "source:app.py" in result["evidence"]
        else:
            assert reads == []
            assert "source.index" not in result["evidence"]
            assert result["evidence"]["source.unavailable"]["code"] == "source_image_unverified"
    asyncio.run(run())
