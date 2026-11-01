"""Offline tests for tools/preflight/baseten_probe.py (SETUP-002).

All tests use mocked transports. They require no real key and make no
provider requests; mocked successes are not evidence of account access.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PROBE_PATH = _REPO_ROOT / "tools" / "preflight" / "baseten_probe.py"
_spec = importlib.util.spec_from_file_location("baseten_probe", _PROBE_PATH)
assert _spec is not None and _spec.loader is not None
probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(probe)

SECRET = "sk-TEST-SECRET-DO-NOT-LEAK"


def _text_response(content="AURA preflight OK", reasoning="brief reason",
                   usage=None, model=None):
    message = SimpleNamespace(content=content, reasoning_content=reasoning)
    choice = SimpleNamespace(message=message, finish_reason="stop")
    return SimpleNamespace(
        choices=[choice], usage=usage, model=model or probe.MODEL
    )


def _stream_chunks():
    def chunk(content=None, reasoning=None, usage=None):
        delta = SimpleNamespace(content=content, reasoning_content=reasoning)
        choice = SimpleNamespace(delta=delta, finish_reason=None)
        return SimpleNamespace(choices=[choice], usage=usage, model=probe.MODEL)

    return [
        chunk(reasoning="thinking"),
        chunk(content="1 2 "),
        chunk(content="3 4 5"),
        chunk(usage=SimpleNamespace(
            prompt_tokens=9, completion_tokens=5, total_tokens=14,
            completion_tokens_details=SimpleNamespace(reasoning_tokens=2),
        )),
    ]


class FakeCompletions:
    def __init__(self, error=None):
        self.calls: list[dict] = []
        self._error = error

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        if kwargs.get("stream"):
            return iter(_stream_chunks())
        content = kwargs["messages"][0]["content"]
        if isinstance(content, list):  # vision request
            return _text_response(
                content="top: blue, bottom: amber", reasoning=None
            )
        return _text_response()


class FakeClient:
    def __init__(self, error=None):
        self.completions = FakeCompletions(error=error)
        self.chat = SimpleNamespace(completions=self.completions)


def _tmp_root_with_fixture(tmp_path: Path) -> Path:
    fixture = (
        tmp_path / "tools" / "preflight" / "fixtures" / "vision-probe.png"
    )
    fixture.parent.mkdir(parents=True)
    fixture.write_bytes(b"\x89PNG\r\n\x1a\nfake-fixture-bytes")
    return tmp_path


class FakeAuthError(Exception):
    status_code = 401


class FakeRateLimitError(Exception):
    status_code = 429


class FakeTimeoutError(Exception):
    pass


def test_default_mode_makes_no_network_calls(tmp_path, monkeypatch):
    monkeypatch.delenv("BASETEN_API_KEY", raising=False)
    factory_calls: list[str] = []

    def counting_factory(key):
        factory_calls.append(key)
        raise AssertionError("offline mode must not build a provider client")

    out = tmp_path / "caps.json"
    code = probe.main(
        ["--output", str(out)],
        project_root=tmp_path,
        client_factory=counting_factory,
    )
    assert code == 0
    assert factory_calls == []
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["mode"] == "offline"
    assert data["live_requests_made"] == 0
    assert data["capabilities"]["text"]["status"] == "not_checked"


def test_missing_key_live_makes_no_network_calls(tmp_path, monkeypatch):
    monkeypatch.delenv("BASETEN_API_KEY", raising=False)
    factory_calls: list[str] = []

    def counting_factory(key):
        factory_calls.append(key)
        return FakeClient()

    result = probe.run_live(
        project_root=tmp_path, client_factory=counting_factory, api_key=None
    )
    assert result["status"] == "blocked_missing_key"
    assert result["blocked_code"] == "ONLINE_CHECKS_BLOCKED_MISSING_KEY"
    assert result["live_requests_made"] == 0
    assert factory_calls == []
    assert result["next_command"] == probe.NEXT_LIVE_COMMAND


def test_content_reasoning_separated_and_absent_usage_handled():
    parsed = probe.parse_completion_response(_text_response(usage=None))
    assert parsed["content"] == "AURA preflight OK"
    assert parsed["reasoning"] == "brief reason"
    assert parsed["reasoning_present"] is True
    assert parsed["reasoning_field"] == "reasoning_content"
    # Absent usage stays unavailable; it is never fabricated as zero.
    assert parsed["usage"] is None

    no_reasoning = probe.parse_completion_response(
        _text_response(content="42", reasoning=None)
    )
    assert no_reasoning["content"] == "42"
    assert no_reasoning["reasoning"] is None
    assert no_reasoning["reasoning_present"] is False

    streamed = probe.parse_stream_chunks(_stream_chunks())
    assert streamed["content"] == "1 2 3 4 5"
    assert streamed["reasoning"] == "thinking"
    assert streamed["content_delta_count"] == 2
    assert streamed["usage"] == {
        "prompt_tokens": 9,
        "completion_tokens": 5,
        "total_tokens": 14,
        "reasoning_tokens": 2,
    }
    empty_stream = probe.parse_stream_chunks(
        [SimpleNamespace(choices=[], usage=None, model=probe.MODEL)]
    )
    assert empty_stream["usage"] is None


@pytest.mark.parametrize(
    ("exc", "category"),
    [
        (FakeAuthError(f"bad key {SECRET}"), "auth_error"),
        (FakeRateLimitError(f"slow down {SECRET}"), "rate_limit"),
        (FakeTimeoutError(f"timed out {SECRET}"), "timeout"),
    ],
)
def test_provider_errors_are_sanitized(tmp_path, exc, category):
    sanitized = probe.sanitize_error(exc)
    assert sanitized["category"] == category
    assert SECRET not in json.dumps(sanitized)

    root = _tmp_root_with_fixture(tmp_path)
    client = FakeClient(error=exc)
    result = probe.run_live(
        project_root=root,
        client_factory=lambda key: client,
        api_key="test-key-not-real",
    )
    assert result["live_requests_made"] <= 3
    assert SECRET not in json.dumps(result)
    assert all(e["category"] == category for e in result["errors"])


def test_live_request_count_and_request_limits(tmp_path):
    root = _tmp_root_with_fixture(tmp_path)
    client = FakeClient()
    result = probe.run_live(
        project_root=root,
        client_factory=lambda key: client,
        api_key="test-key-not-real",
    )
    calls = client.completions.calls
    assert len(calls) == 3
    assert result["live_requests_made"] == 3
    assert all(c["model"] == probe.MODEL for c in calls)
    assert all(0 < c["max_tokens"] <= 4096 for c in calls)
    assert sum(1 for c in calls if c.get("stream")) == 1
    assert result["capabilities"]["text"]["status"] == "verified"
    assert result["capabilities"]["streaming"]["status"] == "verified"
    vision = result["capabilities"]["vision"]
    assert vision["status"] == "verified"
    assert vision["actual_answer"] == "top: blue, bottom: amber"
    assert vision["matches_expected"] is True


def test_client_disables_retries_with_finite_timeout(monkeypatch):
    captured: dict = {}

    class FakeOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(probe, "OpenAI", FakeOpenAI)
    probe.build_client("test-key-not-real")
    assert captured["base_url"] == "https://inference.baseten.co/v1"
    assert captured["max_retries"] == 0
    assert 0 < captured["timeout"] <= 60
    assert probe.REQUEST_MAX_TOKENS <= probe.MAX_TOKENS_CEILING <= 4096


def test_vision_answer_matching():
    assert probe.vision_answer_matches("top: blue, bottom: amber") is True
    assert probe.vision_answer_matches("Top blue, bottom orange") is True
    assert probe.vision_answer_matches("top: amber, bottom: blue") is False
    assert probe.vision_answer_matches("it is blue") is False
    assert probe.vision_answer_matches(None) is False


def test_vision_prompt_does_not_leak_expected_colors():
    assert 'blue' not in probe.VISION_PROMPT.lower()
    assert 'amber' not in probe.VISION_PROMPT.lower()
    assert 'orange' not in probe.VISION_PROMPT.lower()


def test_failed_live_probe_exits_nonzero(tmp_path, monkeypatch):
    monkeypatch.setenv('BASETEN_API_KEY', 'test-key-not-real')
    root = _tmp_root_with_fixture(tmp_path)
    code = probe.main(['--live'], project_root=root,
                      client_factory=lambda key: FakeClient(FakeAuthError('secret')))
    assert code == 1
    data = json.loads((root / probe.DEFAULT_OUTPUT_RELATIVE).read_text())
    assert data['status'] == 'partial'


def test_wrong_vision_answer_is_not_verified(tmp_path):
    client = FakeClient()
    original = client.completions.create
    def wrong_vision(**kwargs):
        if isinstance(kwargs['messages'][0]['content'], list):
            return _text_response('top: amber, bottom: blue')
        return original(**kwargs)
    client.completions.create = wrong_vision
    result = probe.run_live(_tmp_root_with_fixture(tmp_path),
                            client_factory=lambda key: client, api_key='test')
    assert result['capabilities']['vision']['status'] != 'verified'
    assert result['status'] == 'partial'
