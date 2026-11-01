"""Functional checks use streamed HTTP doubles and read-only Docker doubles."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sys

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from aura_ops.docker import CommandResult
from aura_ops.models import TargetSpec, utc_now
from aura_ops.verification import parse_memory_events, verify_functional

CID = "a" * 64
IMAGE = "sha256:" + "b" * 64
DATA = b"complete measured file\n" * 8192


def spec(**overrides):
    values = {"target_id": "linkding", "name": "Linkding", "container_name": "linkding",
              "docker_context": "aura-test", "probes": [{"url": "http://127.0.0.1:9000/health"}],
              "recovery_checks": [{"url": "http://127.0.0.1:9000/asset", "expected_bytes": len(DATA),
                                   "expected_sha256": hashlib.sha256(DATA).hexdigest(), "deadline_seconds": 5}]}
    values.update(overrides)
    return TargetSpec(**values)


class Target:
    def __init__(self):
        self.calls = 0
        self.active = 0
        self.mutate = None

    async def collect(self):
        self.active += 1
        try:
            self.calls += 1
            await asyncio.sleep(.001)
            value = {"timestamp": utc_now(),
                     "identity": {"container_id": CID, "image_id": IMAGE, "started_at": "2026-10-28T00:00:00Z"},
                     "healthy": True, "runtime": {"running": True, "status": "running", "health": "none",
                                                  "restart_count": 0, "oom_killed": False},
                     "resources": {"memory_bytes": 40000000 + self.calls * 100,
                                   "memory_limit_bytes": 256 * 1024**2, "cpu_percent": 1.0},
                     "probes": [{"url": "http://127.0.0.1:9000/health", "ok": True,
                                 "status_code": 200, "duration_ms": 1.0}],
                     "logs": "old normal line\n2026-10-28T00:00:00Z worker 2 died, killed by signal 9\n"}
            if self.mutate:
                self.mutate(value, self.calls)
            return value
        finally:
            self.active -= 1


class CgroupRunner:
    def __init__(self):
        self.calls = []
        self.before = {"low": 0, "high": 0, "max": 0, "oom": 3, "oom_kill": 2, "oom_group_kill": 0}
        self.after = dict(self.before)
        self.unavailable = False
        self.on_second = None

    async def __call__(self, argv, **bounds):
        self.calls.append(argv)
        assert argv == ["docker", "--context", "aura-test", "container", "exec", CID,
                        "cat", "/sys/fs/cgroup/memory.events"]
        assert bounds["max_stdout"] <= 16 * 1024
        if self.unavailable:
            return CommandResult(1, "", "No such file")
        if len(self.calls) == 2 and self.on_second:
            self.on_second()
        values = self.before if len(self.calls) == 1 else self.after
        return CommandResult(0, "\n".join(f"{key} {value}" for key, value in values.items()))


class Chunks(httpx.AsyncByteStream):
    def __init__(self, data=DATA, delay=0.0, hang=False):
        self.data, self.delay, self.hang = data, delay, hang
        self.closed = False
        self.count = 0
        self.started = asyncio.Event()

    async def __aiter__(self):
        self.started.set()
        if self.hang:
            await asyncio.sleep(100)
        for start in range(0, len(self.data), 4096):
            if self.delay:
                await asyncio.sleep(self.delay)
            self.count += 1
            yield self.data[start:start + 4096]

    async def aclose(self):
        self.closed = True


async def run(*, target=None, runner=None, stream=None, status=200, headers=None, target_spec=None, handler=None):
    target, runner, stream = target or Target(), runner or CgroupRunner(), stream or Chunks()
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            handler or (lambda request: httpx.Response(status, headers=headers, stream=stream)))) as client:
        result = await verify_functional(target_spec or spec(), asyncio.Event(),
                                        docker_factory=lambda _: target, http_client=client,
                                        runner=runner, interval=.01)
    return result, target, runner, stream


def test_streamed_integrity_memory_and_old_oom_do_not_fake_failure():
    async def check():
        result, target, runner, stream = await run(stream=Chunks(delay=.001))
        assert result["verified"] is True
        assert result["reason"] == "functional_checks_passed"
        assert result["checks"][0]["bytes_received"] == len(DATA)
        assert result["checks"][0]["sha256"] == hashlib.sha256(DATA).hexdigest()
        assert result["sample_count"] >= 3
        assert result["sample_count"] <= 300
        assert result["sampled_peak_memory_bytes"] == max(s["memory_bytes"] for s in result["samples"])
        assert result["oom_delta"] == result["oom_kill_delta"] == 0
        assert result["cgroup_before"]["oom_kill"] == 2
        assert not result["new_worker_failures"]
        assert stream.closed and stream.count > 1 and target.active == 0
        assert len(runner.calls) == 2
    asyncio.run(check())


def test_absent_checks_never_claim_fixed_or_invoke_tools():
    async def check():
        def no_target(_):
            pytest.fail("No checks means no collection")
        result = await verify_functional(spec(recovery_checks=[]), asyncio.Event(), docker_factory=no_target)
        assert result["verified"] is False
        assert result["reason"] == "no_functional_checks"
    asyncio.run(check())


def test_unknown_oom_counters_fail_closed_before_transfer():
    async def check():
        runner, stream = CgroupRunner(), Chunks()
        runner.unavailable = True
        result, _, _, _ = await run(runner=runner, stream=stream)
        assert result["verified"] is False
        assert result["reason"] == "oom_evidence_unavailable"
        assert stream.count == 0
    asyncio.run(check())


@pytest.mark.parametrize("counter", ["oom", "oom_kill"])
def test_worker_oom_without_container_death_detected(counter):
    async def check():
        runner = CgroupRunner()
        runner.after[counter] += 1
        result, _, _, _ = await run(runner=runner)
        assert result["verified"] is False
        assert result["reason"] == "verification_oom"
        assert result["cgroup_after"][counter] > result["cgroup_before"][counter]
    asyncio.run(check())


def test_counter_reset_is_not_zero_new_oom():
    async def check():
        runner = CgroupRunner()
        runner.after["oom_kill"] = 0
        result, _, _, _ = await run(runner=runner)
        assert result["verified"] is False
        assert result["reason"] == "verification_cgroup_reset"
    asyncio.run(check())


@pytest.mark.parametrize("field,value", [("container_id", "c" * 64), ("image_id", "sha256:" + "d" * 64),
                                         ("started_at", "2026-10-28T01:00:00Z")])
def test_target_identity_change_invalidates_download(field, value):
    async def check():
        target = Target()
        target.mutate = lambda sample, count: sample["identity"].update({field: value}) if count > 1 else None
        result, _, _, _ = await run(target=target, stream=Chunks(delay=.001))
        assert result["verified"] is False
        assert result["reason"] == "verification_target_changed"
    asyncio.run(check())


def test_identity_change_during_after_cgroup_read_is_detected():
    async def check():
        target, runner = Target(), CgroupRunner()
        runner.on_second = lambda: setattr(target, "mutate", lambda sample, count: sample["identity"].update(started_at="new-generation"))
        result, _, _, _ = await run(target=target, runner=runner)
        assert result["verified"] is False
        assert result["reason"] == "verification_target_changed"
    asyncio.run(check())


def test_new_worker_failure_logs_fail_with_no_container_oom():
    async def check():
        target = Target()
        def mutate(sample, count):
            if count > 1:
                sample["logs"] += "2026-10-28T01:00:00Z worker 3 died, killed by signal 9\n"
        target.mutate = mutate
        result, _, _, _ = await run(target=target, stream=Chunks(delay=.001))
        assert result["verified"] is False
        assert result["reason"] == "verification_worker_failure"
        assert len(result["new_worker_failures"]) == 1
    asyncio.run(check())


@pytest.mark.parametrize("failure", ["health", "restart", "stale"])
def test_bad_observations_invalidate_result(failure):
    async def check():
        target = Target()
        def mutate(sample, count):
            if count < 2:
                return
            if failure == "health":
                sample["healthy"] = False
            elif failure == "restart":
                sample["runtime"]["restart_count"] = 1
            else:
                sample["timestamp"] = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        target.mutate = mutate
        result, _, _, _ = await run(target=target)
        assert result["verified"] is False
        expected = {"health": "verification_unhealthy", "restart": "verification_restarted", "stale": "verification_evidence_stale"}
        assert result["reason"] == expected[failure]
    asyncio.run(check())


@pytest.mark.parametrize("data,reason", [(DATA[:-1], "check_truncated"), (DATA + b"extra", "check_byte_limit"),
                                       (b"x" * len(DATA), "check_hash_mismatch")],
                         ids=["truncated", "oversized", "incorrect-hash"])
def test_transfer_bytes_and_hash_verified(data, reason):
    async def check():
        result, _, _, stream = await run(stream=Chunks(data=data))
        assert result["verified"] is False
        assert result["reason"] == reason
        assert stream.closed
    asyncio.run(check())


@pytest.mark.parametrize("status,headers,reason", [
    (302, {"location": "https://other.example"}, "check_redirect_rejected"),
    (500, {}, "check_http_status"),
    (200, {"content-encoding": "gzip"}, "check_content_encoding"),
    (200, {"content-length": str(len(DATA) + 1)}, "check_content_length"),
])
def test_response_metadata_guards_prevent_reading(status, headers, reason):
    async def check():
        result, _, _, stream = await run(status=status, headers=headers)
        assert result["verified"] is False
        assert result["reason"] == reason
        assert stream.count == 0
    asyncio.run(check())


def test_credentials_used_only_for_exact_request_not_report(monkeypatch):
    monkeypatch.setenv("AURA_TEST_DOWNLOAD_TOKEN", "secret-body-credential")
    requests = []
    target_spec = spec()
    target_spec.recovery_checks[0].credential_env = "AURA_TEST_DOWNLOAD_TOKEN"
    def handler(request):
        requests.append(request)
        assert str(request.url) == "http://127.0.0.1:9000/asset"
        assert request.headers["Authorization"] == "Token secret-body-credential"
        assert request.headers["Accept-Encoding"] == "identity"
        return httpx.Response(200, stream=Chunks())
    async def check():
        result, _, _, _ = await run(target_spec=target_spec, handler=handler)
        assert result["verified"] is True
        assert "secret-body-credential" not in json.dumps(result)
        assert len(requests) == 1
    asyncio.run(check())


def test_missing_credential_does_not_dispatch_download(monkeypatch):
    monkeypatch.delenv("AURA_MISSING_DOWNLOAD_TOKEN", raising=False)
    target_spec = spec()
    target_spec.recovery_checks[0].credential_env = "AURA_MISSING_DOWNLOAD_TOKEN"
    async def check():
        result, _, _, _ = await run(target_spec=target_spec,
            handler=lambda request: pytest.fail("Do not send unauthenticated request"))
        assert result["verified"] is False
        assert result["reason"] == "check_credential_missing"
    asyncio.run(check())


def test_ui_cookie_and_api_token_checks_run_and_both_must_pass(monkeypatch):
    monkeypatch.setenv("AURA_UI_SESSION", "ld_sessionid=private-session-cookie")
    monkeypatch.setenv("AURA_API_TOKEN", "private-api-token")
    target_spec = spec()
    api = target_spec.recovery_checks[0].model_copy(update={
        "url": "http://127.0.0.1:9000/api/asset", "credential_env": "AURA_API_TOKEN"})
    ui = target_spec.recovery_checks[0].model_copy(update={
        "url": "http://127.0.0.1:9000/ui/asset", "credential_env": "AURA_UI_SESSION", "auth_scheme": "Cookie"})
    target_spec.recovery_checks = [api, ui]
    seen = []
    def handler(request):
        seen.append(request.url.path)
        if request.url.path == "/api/asset":
            assert request.headers["Authorization"] == "Token private-api-token"
        else:
            assert request.headers["Cookie"] == "ld_sessionid=private-session-cookie"
            assert "Authorization" not in request.headers
        return httpx.Response(200, stream=Chunks())
    async def check():
        result, _, _, _ = await run(target_spec=target_spec, handler=handler)
        assert result["verified"] is True and len(result["checks"]) == 2
        assert sorted(seen) == ["/api/asset", "/ui/asset"]
        assert "private-session-cookie" not in json.dumps(result)
        assert "private-api-token" not in json.dumps(result)
        seen.clear()
        def fail_ui(request):
            return httpx.Response(500 if request.url.path == "/ui/asset" else 200, stream=Chunks())
        failed, _, _, _ = await run(target_spec=target_spec, handler=fail_ui)
        assert failed["verified"] is False
        assert sum(outcome["ok"] for outcome in failed["checks"]) == 1
    asyncio.run(check())


def test_worker_failure_excerpt_redacts_enrolled_cookie(monkeypatch):
    secret = "ld_sessionid=private-cookie-675"
    monkeypatch.setenv("AURA_UI_SESSION", secret)
    target_spec = spec()
    target_spec.recovery_checks[0].credential_env = "AURA_UI_SESSION"
    target_spec.recovery_checks[0].auth_scheme = "Cookie"
    async def check():
        target = Target()
        def mutate(sample, count):
            if count > 1:
                sample["logs"] += "worker 3 died while processing " + secret + "\n"
        target.mutate = mutate
        result, _, _, _ = await run(target_spec=target_spec, target=target)
        assert result["verified"] is False
        assert result["reason"] == "verification_worker_failure"
        assert "private-cookie-675" not in json.dumps(result)
    asyncio.run(check())


def test_read_exception_is_redacted():
    async def handler(request):
        raise httpx.ReadError("Authorization: Token raw-secret", request=request)
    async def check():
        result, _, _, _ = await run(handler=handler)
        assert result["verified"] is False
        assert result["reason"] == "check_transfer_failed"
        assert "raw-secret" not in json.dumps(result)
    asyncio.run(check())


def test_cancellation_closes_stream_and_polling_without_target_stop():
    async def check():
        stream, target, runner = Chunks(hang=True), Target(), CgroupRunner()
        cancelled = asyncio.Event()
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda request: httpx.Response(200, stream=stream))) as client:
            task = asyncio.create_task(verify_functional(spec(), cancelled,
                docker_factory=lambda _: target, http_client=client, runner=runner, interval=.01))
            await asyncio.wait_for(stream.started.wait(), 2)
            cancelled.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert not client.is_closed
        assert stream.closed
        assert target.active == 0
        assert all("stop" not in argv and "restart" not in argv and "rm" not in argv for argv in runner.calls)
    asyncio.run(check())


def test_overall_download_deadline_not_just_socket_timeout():
    async def check():
        # Bypass only the public minimum for a fast test of the fixed deadline.
        target_spec = spec()
        target_spec.recovery_checks[0].deadline_seconds = .03
        result, _, _, stream = await run(target_spec=target_spec, stream=Chunks(hang=True))
        assert result["verified"] is False
        assert result["reason"] == "check_deadline_exceeded"
        assert stream.closed
    asyncio.run(check())


@pytest.mark.parametrize("text", ["oom 1", "oom 0\noom_kill -1", "oom 0\noom 1\noom_kill 0", "oom x\noom_kill 0"])
def test_cgroup_invalid_values_rejected(text):
    with pytest.raises(Exception) as error:
        parse_memory_events(text)
    assert error.value.code == "oom_evidence_unavailable"
