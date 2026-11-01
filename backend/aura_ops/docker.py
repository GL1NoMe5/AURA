"""Docker CLI adapter for explicitly enrolled, externally owned applications.

All CLI invocations select a context explicitly, use argument arrays, and are
bounded. Cancellation kills only the CLI process this module created. Closing
AURA never stops or removes a target container.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import math
import os
import re
import subprocess
import time
from typing import Any, Awaitable, Callable

import httpx

from .models import OpsError, TargetSpec, utc_now
from .redaction import is_sensitive_key, redact_data, redact_text

MAX_STDOUT = 1024 * 1024
MAX_STDERR = 64 * 1024
MAX_LOG_CHARS = 16 * 1024
_CONTAINER_ID = re.compile(r"^[a-f0-9]{64}$")
_IMAGE_ID = re.compile(r"^sha256:[a-f0-9]{64}$")


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str = ""


Runner = Callable[..., Awaitable[CommandResult]]


async def run_command(argv: list[str], *, timeout: float = 15,
                      max_stdout: int = MAX_STDOUT,
                      max_stderr: int = MAX_STDERR) -> CommandResult:
    """Run one owned child, bounding both pipes even before it exits."""
    try:
        process = await asyncio.create_subprocess_exec(
            *argv, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            **({"creationflags": subprocess.CREATE_NO_WINDOW}
               if os.name == "nt" else {}))
    except FileNotFoundError as exc:
        raise OpsError("docker_not_installed", "Docker CLI is not installed or is not on PATH.", 503) from exc
    except OSError as exc:
        raise OpsError("docker_cli_unavailable", "Docker CLI could not be started.", 503) from exc

    async def read_pipe(stream, limit):
        chunks, size = [], 0
        while True:
            chunk = await stream.read(min(65536, limit + 1))
            if not chunk:
                return b"".join(chunks).decode("utf-8", errors="replace")
            size += len(chunk)
            if size > limit:
                raise OpsError("docker_output_limit", "Docker command output exceeded its limit.", 502)
            chunks.append(chunk)

    jobs = [asyncio.create_task(read_pipe(process.stdout, max_stdout)),
            asyncio.create_task(read_pipe(process.stderr, max_stderr)),
            asyncio.create_task(process.wait())]
    combined = asyncio.gather(*jobs)
    try:
        stdout, stderr, returncode = await asyncio.wait_for(
            combined, timeout=timeout)
        return CommandResult(returncode, stdout, stderr)
    except asyncio.TimeoutError as exc:
        raise OpsError("docker_timeout", "Docker command timed out; its result may be unknown.", 504) from exc
    finally:
        # Do not enumerate/kill other processes or stop a Docker container.
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        for job in jobs:
            if not job.done():
                job.cancel()
        # wait_for may lose the aggregate exception in a cancellation race.
        # Drain the aggregate as well as its children before closing the loop.
        await asyncio.gather(combined, *jobs, return_exceptions=True)
        await process.wait()


def _known_secrets(spec: TargetSpec | None = None) -> tuple[str, ...]:
    names = {probe.credential_env for probe in spec.probes if probe.credential_env} if spec else set()
    return tuple(value for key, value in os.environ.items()
                 if value and (key in names or is_sensitive_key(key)))


def _json(text: str) -> Any:
    try:
        return json.loads(text)
    except (TypeError, ValueError) as exc:
        raise OpsError("docker_invalid_output", "Docker returned invalid JSON.", 502) from exc


def _number(value: Any, *, integer=False):
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed) or parsed < 0:
        return None
    return int(parsed) if integer else parsed


def parse_memory(value: str) -> int | None:
    """Parse Docker stats units without treating malformed values as zero."""
    match = re.fullmatch(r"\s*([0-9]+(?:\.[0-9]+)?)\s*(B|[KMGTPE]i?B)\s*", str(value), re.I)
    if not match:
        return None
    unit = match.group(2).upper()
    power = 0 if unit == "B" else "KMGTPE".index(unit[0]) + 1
    scale = (1024 if "I" in unit else 1000) ** power
    amount = float(match.group(1)) * scale
    return int(amount) if math.isfinite(amount) else None


def parse_stats(text: str) -> dict:
    row = _json(text.strip())
    if not isinstance(row, dict):
        raise OpsError("docker_invalid_output", "Docker stats returned an unexpected shape.", 502)
    memory = str(row.get("MemUsage", "")).split("/", 1)
    return {
        "memory_bytes": parse_memory(memory[0]) if memory else None,
        "memory_limit_bytes": parse_memory(memory[1]) if len(memory) == 2 else None,
        "cpu_percent": _number(str(row.get("CPUPerc", "")).rstrip("%")),
    }


def identity_of(inspect: dict) -> dict:
    state = inspect.get("State") or {}
    identity = {
        "container_id": inspect.get("Id"), "image_id": inspect.get("Image"),
        "image_ref": (inspect.get("Config") or {}).get("Image"),
        "started_at": state.get("StartedAt"),
    }
    if not _CONTAINER_ID.fullmatch(str(identity["container_id"])) \
            or not _IMAGE_ID.fullmatch(str(identity["image_id"])) \
            or not isinstance(identity["started_at"], str):
        raise OpsError("docker_invalid_identity", "Docker returned an incomplete container identity.", 502)
    return identity


def _same_identity(expected: dict, actual: dict) -> bool:
    return all(key in expected and expected[key] == actual.get(key)
               for key in ("container_id", "image_id", "started_at"))


class DockerTarget:
    def __init__(self, spec: TargetSpec, runner: Runner | None = None,
                 http_client: httpx.AsyncClient | None = None):
        self.spec = spec
        self.runner = runner or run_command
        self.http_client = http_client

    async def _command(self, *args: str, timeout: float = 15) -> CommandResult:
        result = await self.runner(
            ["docker", "--context", self.spec.docker_context, *args],
            timeout=timeout, max_stdout=MAX_STDOUT, max_stderr=MAX_STDERR)
        if result.returncode:
            message = redact_text(result.stderr.strip(), _known_secrets(self.spec))[:1000]
            code = "docker_unavailable"
            if "no such" in result.stderr.lower() or "not found" in result.stderr.lower():
                code = "target_not_found"
            raise OpsError(code, message or "Docker command failed.", 503)
        return result

    async def _inspect(self, reference: str) -> dict:
        raw = _json((await self._command("container", "inspect", reference)).stdout)
        if not isinstance(raw, list) or len(raw) != 1 or not isinstance(raw[0], dict):
            raise OpsError("docker_invalid_output", "Docker inspect must return exactly one container.", 502)
        identity_of(raw[0])
        return raw[0]

    async def inspect_identity(self) -> dict:
        return identity_of(await self._inspect(self.spec.container_name))

    async def _probe(self, spec, client: httpx.AsyncClient) -> dict:
        started = time.perf_counter()
        result = {"url": spec.url, "ok": False, "status_code": None,
                  "duration_ms": None, "error": None}
        headers = {}
        if spec.credential_env:
            credential = os.environ.get(spec.credential_env, "").strip()
            if not credential:
                result["error"] = "probe_credential_missing"
                result["duration_ms"] = 0.0
                return result
            if spec.auth_scheme == "Cookie":
                headers["Cookie"] = credential
            else:
                headers["Authorization"] = f"{spec.auth_scheme} {credential}"
        async def fetch_headers():
            # Streaming obtains headers without buffering arbitrary response bodies.
            async with client.stream("GET", spec.url, headers=headers,
                                     timeout=spec.timeout_seconds,
                                     follow_redirects=False) as response:
                result["status_code"] = response.status_code
                result["ok"] = response.status_code == spec.expected_status
                if not result["ok"]:
                    result["error"] = "unexpected_http_status"
        try:
            await asyncio.wait_for(fetch_headers(), timeout=spec.timeout_seconds)
        except (httpx.TimeoutException, asyncio.TimeoutError):
            result["error"] = "probe_timeout"
        except (httpx.HTTPError, OSError, ValueError):
            # Exception strings can contain credentials, URLs or response material.
            result["error"] = "probe_unreachable"
        result["duration_ms"] = round((time.perf_counter() - started) * 1000, 3)
        return result

    async def collect(self) -> dict:
        first = await self._inspect(self.spec.container_name)
        identity = identity_of(first)
        container_id = identity["container_id"]
        state = first.get("State") or {}
        running = state.get("Running") is True
        resources = {"memory_bytes": None, "memory_limit_bytes": None, "cpu_percent": None}
        errors: dict[str, str] = {}

        async def read_stats():
            nonlocal resources
            if not running:
                return
            try:
                resources = parse_stats((await self._command(
                    "container", "stats", "--no-stream", "--format", "{{json .}}", container_id)).stdout)
            except OpsError as exc:
                errors["resources"] = exc.code

        async def read_logs():
            try:
                result = await self._command("container", "logs", "--timestamps", "--tail", "100", container_id)
                # Docker writes container stderr to CLI stderr even on exit zero.
                return redact_text(result.stdout + result.stderr, _known_secrets(self.spec))[-MAX_LOG_CHARS:]
            except OpsError as exc:
                errors["logs"] = exc.code
                return ""

        async def observations(client):
            tasks = [asyncio.create_task(read_stats()), asyncio.create_task(read_logs()),
                     *[asyncio.create_task(self._probe(probe, client)) for probe in self.spec.probes]]
            try:
                return await asyncio.gather(*tasks)
            finally:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

        if self.http_client is not None:
            observed = await observations(self.http_client)
        else:
            async with httpx.AsyncClient(trust_env=False) as client:
                observed = await observations(client)
        last = await self._inspect(self.spec.container_name)
        if not _same_identity(identity, identity_of(last)):
            raise OpsError("target_changed", "The target changed while evidence was being collected.", 409)
        # Resolve status from the most recent inspection, not its pre-probe state.
        state = last.get("State") or {}
        health = (state.get("Health") or {}).get("Status", "none")
        host_config = last.get("HostConfig") or {}
        memory_limit = _number(host_config.get("Memory"), integer=True)
        if memory_limit and resources["memory_limit_bytes"] is None:
            resources["memory_limit_bytes"] = memory_limit
        published_ports = {}
        # Configured bindings survive a stopped container. EXPOSE-only entries
        # (null/empty bindings) are not host publication and must not imply drift.
        bindings_map = host_config.get('PortBindings')
        if not isinstance(bindings_map, dict):
            bindings_map = (last.get("NetworkSettings") or {}).get("Ports") or {}
        for key, bindings in bindings_map.items():
            if re.fullmatch(r"[0-9]{1,5}/(?:tcp|udp)", str(key)):
                valid_bindings = [{"host_ip": str(b.get("HostIp", ""))[:64],
                                         "host_port": str(b.get("HostPort", ""))[:5]}
                                        for b in (bindings or [])[:8] if isinstance(b, dict) and b.get('HostPort')]
                if valid_bindings:
                    published_ports[key] = valid_bindings
        server_port = None
        for setting in (last.get("Config") or {}).get("Env") or []:
            if isinstance(setting, str) and setting.startswith("LD_SERVER_PORT="):
                value = setting.split("=", 1)[1]
                server_port = int(value) if value.isdigit() and 0 < int(value) < 65536 else None
        probes = observed[2:]
        return redact_data({
            "timestamp": utc_now(), "identity": identity,
            "runtime": {"running": state.get("Running") is True,
                        "status": state.get("Status", "unknown"),
                        "exit_code": state.get("ExitCode"),
                        "oom_killed": state.get("OOMKilled") is True,
                        "restart_count": _number(last.get("RestartCount"), integer=True),
                        "health": health},
            "resources": resources, "probes": probes, "logs": observed[1],
            "config": {"memory_limit_bytes": memory_limit,
                       "published_ports": published_ports, "server_port": server_port},
            "healthy": state.get("Running") is True and not state.get("Paused")
                       and not state.get("Restarting") and health != "unhealthy"
                       and all(probe["ok"] for probe in probes),
            "collection_errors": errors,
        }, _known_secrets(self.spec))

    async def restart(self, expected_identity: dict,
                      cancel_event: asyncio.Event | None = None) -> dict:
        if cancel_event is not None and cancel_event.is_set():
            raise asyncio.CancelledError()
        current = await self._inspect(self.spec.container_name)
        identity = identity_of(current)
        if not _same_identity(expected_identity, identity):
            raise OpsError("target_changed", "Target identity no longer matches the approved evidence.", 409)
        state = current.get("State") or {}
        if state.get("Paused") or state.get("Restarting") or state.get("Dead") \
                or state.get("Status") not in ("running", "exited"):
            raise OpsError("target_not_restartable", "The target is not in a restartable state.", 409)
        if cancel_event is not None and cancel_event.is_set():
            raise asyncio.CancelledError()
        try:
            await self._command("container", "restart", "--timeout", "10", identity["container_id"], timeout=30)
        except OpsError as exc:
            if exc.code in ("docker_timeout", "docker_unavailable", "docker_output_limit"):
                raise OpsError("action_outcome_unknown", "Restart result is unknown; inspect the target before any retry.", 409) from exc
            raise
        try:
            after = identity_of(await self._inspect(self.spec.container_name))
        except OpsError as exc:
            raise OpsError("action_outcome_unknown", "Restart was dispatched but its resulting target state is unavailable.", 409) from exc
        if after["container_id"] != identity["container_id"] or after["image_id"] != identity["image_id"]:
            raise OpsError("action_outcome_unknown", "The target changed during restart; reconcile its state.", 409)
        return {"action": "restart_container", "command_completed": True,
                "verified": False, "identity": after, "timestamp": utc_now()}

    async def runtime_info(self) -> dict:
        return await runtime_info(self.spec.docker_context, runner=self.runner)


async def runtime_info(docker_context: str = "default", *, runner: Runner | None = None) -> dict:
    """Read engine availability. Never starts Docker or changes its context."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", docker_context):
        raise OpsError("invalid_context", "Invalid Docker context name.")
    result = await (runner or run_command)(
        ["docker", "--context", docker_context, "version", "--format", "{{json .}}"],
        timeout=10, max_stdout=MAX_STDOUT, max_stderr=MAX_STDERR)
    if result.returncode:
        raise OpsError("docker_unavailable", "Docker Engine is not available in the selected context.", 503)
    data = _json(result.stdout)
    if not isinstance(data, dict) or not isinstance(data.get("Server"), dict):
        raise OpsError("docker_unavailable", "Docker Engine did not report a server version.", 503)
    server = data["Server"]
    return {"available": True, "context": docker_context,
            "client_version": (data.get("Client") or {}).get("Version"),
            "server_version": server.get("Version"), "os": server.get("Os"),
            "linux_containers": server.get("Os") == "linux"}
