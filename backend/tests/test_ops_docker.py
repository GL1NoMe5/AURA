"""Offline Docker-boundary tests; real subprocess tests never invoke Docker."""
from __future__ import annotations

import asyncio
import copy
import json
import os
from pathlib import Path
import sys

import httpx
import psutil
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from aura_ops.docker import (CommandResult, DockerTarget, parse_memory,
                             parse_stats, run_command, runtime_info)
from aura_ops.models import OpsError, TargetSpec
from aura_ops.redaction import REDACTED, redact_data, redact_text

CID = "a" * 64
IMAGE = "sha256:" + "b" * 64


def spec(**overrides):
    values = {"target_id": "bookmarks", "name": "Bookmarks",
              "docker_context": "aura-test", "container_name": "test-linkding",
              "probes": [{"url": "http://127.0.0.1:9080/api/bookmarks/"}]}
    values.update(overrides)
    return TargetSpec(**values)


def inspected():
    return {
        "Id": CID, "Name": "/test-linkding", "Image": IMAGE,
        "Config": {"Image": "linkding:pinned",
                   "Env": ["LD_SERVER_PORT=9090", "DATABASE_PASSWORD=hidden-env-secret"],
                   "Labels": {"private": "hidden-label-secret"}},
        "State": {"Running": True, "Status": "running", "ExitCode": 0,
                  "OOMKilled": False, "StartedAt": "2026-10-28T10:00:00Z",
                  "Health": {"Status": "healthy"}},
        "HostConfig": {"Memory": 256 * 1024 * 1024}, "RestartCount": 0,
        "NetworkSettings": {"Ports": {"9090/tcp": [{"HostIp": "127.0.0.1", "HostPort": "9080"}]}},
    }


class FakeRunner:
    def __init__(self, container=None):
        self.container = container or inspected()
        self.calls = []
        self.inspections = 0
        self.change_on_second = None
        self.stats_error = False
        self.restart_timeout = False
        self.logs = "normal line\npassword=supersecret\nAuthorization: Bearer private-token\n"

    async def __call__(self, argv, **bounds):
        self.calls.append((argv, bounds))
        assert argv[:3] == ["docker", "--context", "aura-test"]
        operation = argv[4]
        if operation == "inspect":
            self.inspections += 1
            if self.inspections == 2 and self.change_on_second:
                self.change_on_second(self.container)
            return CommandResult(0, json.dumps([self.container]))
        if operation == "stats":
            if self.stats_error:
                return CommandResult(1, "", "resource unavailable")
            return CommandResult(0, json.dumps({"MemUsage": "40MiB / 256MiB", "CPUPerc": "12.5%"}))
        if operation == "logs":
            return CommandResult(0, self.logs, "stderr line\n")
        if operation == "restart":
            if self.restart_timeout:
                raise OpsError("docker_timeout", "timed out", 504)
            self.container["State"]["StartedAt"] = "2026-10-28T10:01:00Z"
            return CommandResult(0, CID)
        raise AssertionError(argv)


def client(handler=None):
    return httpx.AsyncClient(transport=httpx.MockTransport(
        handler or (lambda request: httpx.Response(200))), trust_env=False)


@pytest.mark.parametrize("value,expected", [
    ("40MiB", 40 * 1024**2), ("256MiB", 256 * 1024**2),
    ("1.25GiB", int(1.25 * 1024**3)), ("125MB", 125000000),
    ("0B", 0), ("--", None), ("NaN MiB", None), ("-1MB", None),
])
def test_memory_units(value, expected):
    assert parse_memory(value) == expected


def test_redaction_structured_and_unstructured():
    raw = {"apiKey": "one", "nested": {"Authorization": "Bearer two"},
           "message": 'password="multiple secret words" token=three '
                      'https://user:four@example.org/?api_key=five Authorization: Bearer six',
           "known": "An otherwise unlabeled unguessable-secret", "count": 3}
    result = redact_data(raw, ("unguessable-secret",))
    encoded = json.dumps(result)
    for secret in ("one", "Bearer two", "multiple secret words", "three", "four", "five", "six", "unguessable-secret"):
        assert secret not in encoded
    assert result["count"] == 3
    assert raw["apiKey"] == "one"
    assert REDACTED in encoded


def test_private_key_block_redacted():
    text = "before\n-----BEGIN RSA PRIVATE KEY-----\nprivate content\n-----END RSA PRIVATE KEY-----\nafter"
    assert redact_text(text) == f"before\n{REDACTED}\nafter"


def test_collect_uses_context_and_resolved_id_redacts_secrets():
    async def check():
        runner = FakeRunner()
        async with client() as http:
            result = await DockerTarget(spec(), runner, http).collect()
        assert result["identity"]["container_id"] == CID
        assert result["resources"]["memory_bytes"] == 40 * 1024**2
        assert result["resources"]["cpu_percent"] == 12.5
        assert result["config"]["server_port"] == 9090
        assert result["healthy"] is True
        assert result["probes"][0]["status_code"] == 200
        assert result["probes"][0]["duration_ms"] >= 0
        encoded = json.dumps(result)
        for value in ("supersecret", "private-token", "hidden-env-secret", "hidden-label-secret"):
            assert value not in encoded
        assert "stderr line" in result["logs"]
        for argv, bounds in runner.calls:
            assert argv[:3] == ["docker", "--context", "aura-test"]
            if argv[4] in ("stats", "logs"):
                assert argv[-1] == CID
            assert bounds["max_stdout"] == 1024 * 1024
    asyncio.run(check())


@pytest.mark.parametrize('stopped', [False, True])
def test_config_ports_keep_real_bindings_but_never_publish_expose_only_entries(stopped):
    async def check():
        container = inspected()
        configured = {'9090/tcp': None, '8000/tcp': [],
                      '8080/tcp': [{'HostIp': '127.0.0.1', 'HostPort': '9080'}]}
        container['HostConfig']['PortBindings'] = configured
        container['NetworkSettings']['Ports'] = {} if stopped else configured
        container['State']['Running'] = not stopped
        container['State']['Status'] = 'exited' if stopped else 'running'
        async with client() as http:
            result = await DockerTarget(spec(), FakeRunner(container), http).collect()
        assert result['config']['published_ports'] == {
            '8080/tcp': [{'host_ip': '127.0.0.1', 'host_port': '9080'}]}
    asyncio.run(check())


@pytest.mark.parametrize("field", ["Id", "Image", "started"])
def test_collect_rejects_identity_change(field):
    async def check():
        runner = FakeRunner()
        def mutate(container):
            if field == "started":
                container["State"]["StartedAt"] = "new-start"
            elif field == "Id":
                container[field] = "c" * 64
            else:
                container[field] = "sha256:" + "d" * 64
        runner.change_on_second = mutate
        async with client() as http:
            with pytest.raises(OpsError) as error:
                await DockerTarget(spec(), runner, http).collect()
        assert error.value.code == "target_changed"
    asyncio.run(check())


def test_failed_metric_has_missing_values_not_fabricated_zeroes():
    async def check():
        runner = FakeRunner()
        runner.stats_error = True
        async with client() as http:
            result = await DockerTarget(spec(), runner, http).collect()
        assert result["resources"]["memory_bytes"] is None
        assert result["resources"]["cpu_percent"] is None
        assert result["collection_errors"]["resources"] == "docker_unavailable"
    asyncio.run(check())


def test_probe_credentials_not_in_evidence_and_redirect_not_followed(monkeypatch):
    monkeypatch.setenv("AURA_LINKDING_TEST_TOKEN", "probe-private-value")
    seen = []
    def handler(request):
        seen.append(request)
        assert request.headers["authorization"] == "Token probe-private-value"
        return httpx.Response(302, headers={"location": "https://other.example/"})
    async def check():
        target = spec(probes=[{"url": "http://127.0.0.1:9080/api/bookmarks/",
                               "credential_env": "AURA_LINKDING_TEST_TOKEN"}])
        async with client(handler) as http:
            result = await DockerTarget(target, FakeRunner(), http).collect()
        assert len(seen) == 1
        assert result["healthy"] is False
        assert "probe-private-value" not in json.dumps(result)
    asyncio.run(check())


def test_probe_body_never_consumed():
    class NeverRead(httpx.AsyncByteStream):
        async def __aiter__(self):
            raise AssertionError("Probe must not read an unbounded body")
            yield b"unreachable"
    async def check():
        async with client(lambda request: httpx.Response(200, stream=NeverRead())) as http:
            result = await DockerTarget(spec(), FakeRunner(), http).collect()
        assert result["healthy"]
    asyncio.run(check())


def test_missing_probe_credential_does_not_send_request(monkeypatch):
    monkeypatch.delenv("AURA_MISSING_TEST_TOKEN", raising=False)
    async def check():
        target = spec(probes=[{"url": "http://127.0.0.1:9080/api/bookmarks/",
                               "credential_env": "AURA_MISSING_TEST_TOKEN"}])
        async with client(lambda request: pytest.fail("No credential means no request")) as http:
            result = await DockerTarget(target, FakeRunner(), http).collect()
        assert result["probes"][0]["error"] == "probe_credential_missing"
        assert result["healthy"] is False
    asyncio.run(check())


def test_probe_timeout_is_not_healthy():
    def handler(request):
        raise httpx.ReadTimeout("secret-token-in-exception", request=request)
    async def check():
        async with client(handler) as http:
            result = await DockerTarget(spec(), FakeRunner(), http).collect()
        assert result["probes"][0]["error"] == "probe_timeout"
        assert result["healthy"] is False
        assert "secret-token-in-exception" not in json.dumps(result)
    asyncio.run(check())


def test_probe_has_overall_deadline_even_if_transport_hangs():
    async def handler(request):
        await asyncio.sleep(10)
        return httpx.Response(200)
    async def check():
        target = spec(probes=[{"url": "http://127.0.0.1:9080/", "timeout_seconds": 0.1}])
        async with client(handler) as http:
            result = await DockerTarget(target, FakeRunner(), http).collect()
        assert result["probes"][0]["error"] == "probe_timeout"
        assert result["healthy"] is False
    asyncio.run(check())


def test_restart_uses_exact_identity_and_does_not_claim_recovery():
    async def check():
        runner = FakeRunner()
        target = DockerTarget(spec(), runner)
        before = await target.inspect_identity()
        result = await target.restart(before)
        assert result["command_completed"] is True
        assert result["verified"] is False
        assert result["identity"]["started_at"] != before["started_at"]
        restart = [argv for argv, _ in runner.calls if argv[4] == "restart"]
        assert restart == [["docker", "--context", "aura-test", "container", "restart", "--timeout", "10", CID]]
    asyncio.run(check())


def test_restart_stale_identity_dispatches_nothing():
    async def check():
        runner = FakeRunner()
        target = DockerTarget(spec(), runner)
        before = await target.inspect_identity()
        before["image_id"] = "sha256:" + "0" * 64
        with pytest.raises(OpsError) as error:
            await target.restart(before)
        assert error.value.code == "target_changed"
        assert not any(argv[4] == "restart" for argv, _ in runner.calls)
    asyncio.run(check())


@pytest.mark.parametrize("field,value", [("Paused", True), ("Restarting", True), ("Dead", True), ("Status", "removing")])
def test_restart_rejects_unstable_state(field, value):
    async def check():
        runner = FakeRunner()
        runner.container["State"][field] = value
        target = DockerTarget(spec(), runner)
        identity = await target.inspect_identity()
        with pytest.raises(OpsError) as error:
            await target.restart(identity)
        assert error.value.code == "target_not_restartable"
        assert not any(argv[4] == "restart" for argv, _ in runner.calls)
    asyncio.run(check())


def test_restart_cancelled_before_dispatch():
    async def check():
        runner = FakeRunner()
        cancelled = asyncio.Event()
        cancelled.set()
        with pytest.raises(asyncio.CancelledError):
            await DockerTarget(spec(), runner).restart({}, cancelled)
        assert runner.calls == []
    asyncio.run(check())


def test_restart_timeout_reports_unknown_outcome():
    async def check():
        runner = FakeRunner()
        runner.restart_timeout = True
        target = DockerTarget(spec(), runner)
        with pytest.raises(OpsError) as error:
            await target.restart(await target.inspect_identity())
        assert error.value.code == "action_outcome_unknown"
        assert len([argv for argv, _ in runner.calls if argv[4] == "restart"]) == 1
    asyncio.run(check())


def test_restart_reconciliation_failure_is_unknown_not_success():
    async def check():
        runner = FakeRunner()
        original = runner.__call__
        restarted = False
        async def disconnect_after_restart(argv, **bounds):
            nonlocal restarted
            if restarted and argv[4] == "inspect":
                return CommandResult(1, "", "daemon unreachable")
            result = await original(argv, **bounds)
            restarted = restarted or argv[4] == "restart"
            return result
        target = DockerTarget(spec(), disconnect_after_restart)
        with pytest.raises(OpsError) as error:
            await target.restart(await target.inspect_identity())
        assert error.value.code == "action_outcome_unknown"
        assert len([argv for argv, _ in runner.calls if argv[4] == "restart"]) == 1
    asyncio.run(check())


def test_runtime_info_selects_context_and_requires_server():
    async def check():
        async def runner(argv, **bounds):
            assert argv == ["docker", "--context", "aura-test", "version", "--format", "{{json .}}"]
            return CommandResult(0, json.dumps({"Client": {"Version": "test"}, "Server": {"Version": "test-server", "Os": "linux"}}))
        info = await runtime_info("aura-test", runner=runner)
        assert info["linux_containers"] is True
        async def offline(argv, **bounds):
            return CommandResult(1, "", "unreachable")
        with pytest.raises(OpsError) as error:
            await runtime_info("aura-test", runner=offline)
        assert error.value.code == "docker_unavailable"
    asyncio.run(check())


def test_invalid_json_not_converted_to_success():
    with pytest.raises(OpsError) as error:
        parse_stats("not JSON")
    assert error.value.code == "docker_invalid_output"
    data = parse_stats('{"MemUsage":"--","CPUPerc":"NaN%"}')
    assert all(value is None for value in data.values())


def test_real_child_output_limit():
    async def check():
        with pytest.raises(OpsError) as error:
            await run_command([sys.executable, "-c", "print('x' * 10000)"], max_stdout=100, timeout=5)
        assert error.value.code == "docker_output_limit"
    asyncio.run(check())


def test_real_child_timeout():
    async def check():
        with pytest.raises(OpsError) as error:
            await run_command([sys.executable, "-c", "import time; time.sleep(10)"], timeout=0.1)
        assert error.value.code == "docker_timeout"
    asyncio.run(check())


def test_real_child_cancellation_cleans_owned_process(tmp_path):
    pid_file = tmp_path / "owned-pid.txt"
    async def check():
        source = "import os,time,pathlib; pathlib.Path(" + repr(str(pid_file)) + ").write_text(str(os.getpid())); time.sleep(10)"
        task = asyncio.create_task(run_command([sys.executable, "-c", source], timeout=15))
        for _ in range(200):
            if pid_file.exists():
                break
            await asyncio.sleep(0.01)
        assert pid_file.exists()
        pid = int(pid_file.read_text())
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not psutil.pid_exists(pid)
    asyncio.run(check())


def test_missing_executable_reports_prerequisite():
    async def check():
        with pytest.raises(OpsError) as error:
            await run_command(["aura-nonexistent-docker-executable-8fc15"], timeout=1)
        assert error.value.code == "docker_not_installed"
    asyncio.run(check())


def test_cancelled_waiter_drains_combined_pipe_future(monkeypatch):
    """Cancellation while wait_for is detaching must retrieve aggregate errors."""
    import gc
    async def check():
        loop = asyncio.get_running_loop()
        errors = []
        loop.set_exception_handler(lambda _, context: errors.append(context))
        async def detached_waiter(awaitable, *, timeout):
            await asyncio.sleep(0)
            raise asyncio.CancelledError()
        monkeypatch.setattr(asyncio, "wait_for", detached_waiter)
        with pytest.raises(asyncio.CancelledError):
            await run_command([sys.executable, "-c", "import time; time.sleep(10)"])
        await asyncio.sleep(0)
        gc.collect()
        await asyncio.sleep(0)
        assert not errors, errors
    asyncio.run(check())
