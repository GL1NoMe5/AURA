"""Fast offline tests for the ACCEPTANCE-001 harness components.

No server, no 20-minute run here: mode handler behavior, trend math,
watermark checks and evidence sanitation. The full soak runs separately
via tools/preflight/soak_check.py.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path

import httpx

_BACKEND = Path(__file__).resolve().parents[1]
_REPO = _BACKEND.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from aura_agents.adapter import MODEL  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "soak_check", _REPO / "tools" / "preflight" / "soak_check.py")
assert _spec is not None and _spec.loader is not None
soak = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(soak)


def _request(agent: str) -> httpx.Request:
    return httpx.Request(
        "POST", "https://inference.baseten.co/v1/chat/completions",
        json={"model": MODEL, "messages": [
            {"role": "system", "content": "s"},
            {"role": "user", "content": f"SPECIALIST: {agent}\n evidence"}]})


def _respond(handler, agent: str) -> httpx.Response:
    return asyncio.run(handler(_request(agent)))


def test_control_modes_drive_handler(tmp_path):
    control = tmp_path / "control.json"
    handler = soak.build_swarm_handler(control, MODEL)
    soak.write_control(control, "success", 1)
    body = json.loads(_respond(handler, "logs").content)
    assert json.loads(
        body["choices"][0]["message"]["content"])["status"] == "completed"
    soak.write_control(control, "malformed", 2)
    body = json.loads(_respond(handler, "memory").content)
    assert body["choices"][0]["message"]["content"] == "this is not json"
    soak.write_control(control, "abstain", 3)
    body = json.loads(_respond(handler, "synthesis").content)
    assert json.loads(
        body["choices"][0]["message"]["content"])["action"] == "abstain"
    soak.write_control(control, "unknown_action", 4)
    body = json.loads(_respond(handler, "synthesis").content)
    assert "restart_everything" in body["choices"][0]["message"]["content"]
    # Retry is scoped per control token: a new run retries fresh.
    soak.write_control(control, "retry429", 5)
    assert _respond(handler, "logs").status_code == 429
    assert _respond(handler, "logs").status_code == 200
    soak.write_control(control, "retry429", 6)
    assert _respond(handler, "logs").status_code == 429


def test_mock_swarm_end_to_end_success(tmp_path):
    from test_agents import _evidence  # reuse: no live calls

    from aura_api.orchestration import AgentEventSink  # noqa

    class Sink:
        async def agent_started(self, agent_id):
            pass

        async def agent_message(self, agent_id, message, kind="status"):
            pass

        async def agent_finished(self, agent_id, status, summary=None):
            pass

    async def run():
        control = tmp_path / "control.json"
        soak.write_control(control, "success", 1)
        orch = soak.make_mock_swarm(control)
        rows = [{"monotonic": 100.0 + i, "rss_bytes": 70_000_000,
                 "retained_cache_bytes": 60 * 1024 * 1024}
                for i in range(4)]
        evidence = _evidence(sampled_telemetry=rows,
                             recent_measurements=rows)
        outcome = await orch.diagnose(
            evidence, asyncio.Event(), Sink())
        await orch.aclose()
        return outcome

    outcome = asyncio.run(run())
    assert [f.agent_id for f in outcome.findings] == [
        "logs", "memory", "config", "vision"]
    assert outcome.proposal.action == "restore_cache_policy"
    assert "soak mock" in outcome.findings[0].summary


def test_healthy_trend_flags_only_continuous_growth():
    stable = [{"t": float(i * 2), "healthy": True,
               "worker_rss": 40_000_000 + (i % 5) * 1000}
              for i in range(60)]
    assert soak.healthy_trend(stable)["verdict"] == "stable_or_bounded"
    growing = [{"t": float(i * 2), "healthy": True,
                "worker_rss": 40_000_000 + i * 3_000_000}
               for i in range(60)]
    flagged = soak.healthy_trend(growing)
    assert flagged["verdict"] == "continuous_growth_flagged"
    assert flagged["slope_bytes_per_sec"] > 0
    assert soak.healthy_trend(stable[:5])["verdict"] == \
        "insufficient_healthy_samples"


def test_watermark_check():
    ok = soak.check_watermark(100, 105, [105, 106, 107])
    assert ok["snapshot_not_older_than_http"] is True
    assert ok["events_monotonic"] is True
    bad = soak.check_watermark(100, 105, [105, 103, 107])
    assert bad["events_monotonic"] is False
    unknown = soak.check_watermark(None, 105, [105])
    assert unknown["snapshot_not_older_than_http"] is None


def test_secret_markers_detected():
    assert soak.find_secret_markers('{"a": 1}') == []
    assert "sk-" in soak.find_secret_markers("key sk-abc here")
    assert "Authorization:" in soak.find_secret_markers("Authorization: x")
