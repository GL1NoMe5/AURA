"""Independent bounded verification of enrolled functional download checks.

Only measured HTTP bytes and Docker/cgroup observations determine the result.
The verifier never changes, restarts, or owns the application under test.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import hashlib
import math
import os
import re
import time

import httpx

from .docker import DockerTarget, run_command
from .evidence_quality import (check_collection, check_logs, check_probes,
                               check_resources, check_runtime, valid_metric)
from .models import OpsError, TargetSpec, utc_now
from .redaction import redact_text

MAX_SAMPLES = 300
CHUNK_BYTES = 64 * 1024
_IDENTITY_FIELDS = ("container_id", "image_id", "started_at")
_WORKER_FAILURE = re.compile(
    r"(?:\bout of memory\b|\boom[-_ ]kill(?:ed)?\b|killed by signal\s+(?:6|9)\b|"
    r"\bworker\b[^\n]{0,150}\b(?:died|segfault|exited abnormally)\b|\bsegmentation fault\b)", re.I)


async def _cancellable(awaitable, cancel_event: asyncio.Event, timeout: float):
    task = asyncio.ensure_future(asyncio.wait_for(awaitable, timeout=timeout))
    cancel = asyncio.create_task(cancel_event.wait())
    try:
        done, _ = await asyncio.wait({task, cancel}, return_when=asyncio.FIRST_COMPLETED)
        if cancel in done and cancel_event.is_set():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise asyncio.CancelledError()
        return await task
    finally:
        task.cancel()
        cancel.cancel()
        await asyncio.gather(task, cancel, return_exceptions=True)


def _failure_lines(sample: dict) -> set[str]:
    return {line.strip() for line in str(sample.get("logs", "")).splitlines()
            if _WORKER_FAILURE.search(line)}


def _identity(sample):
    value = sample.get("identity")
    if not isinstance(value, dict) or not all(isinstance(value.get(key), str) and value[key]
                                             for key in _IDENTITY_FIELDS):
        raise OpsError("verification_identity_missing", "Container identity is incomplete.", 409)
    if not re.fullmatch(r"[a-f0-9]{64}", value["container_id"]):
        raise OpsError("verification_identity_missing", "Container identity is invalid.", 409)
    return {key: value[key] for key in _IDENTITY_FIELDS}


def _fresh(sample):
    try:
        measured = datetime.fromisoformat(sample["timestamp"].replace("Z", "+00:00"))
        if measured.tzinfo is None:
            return False
        age = (datetime.now(timezone.utc) - measured).total_seconds()
        return -5 <= age <= 30
    except (KeyError, TypeError, AttributeError, ValueError):
        return False


def _integer(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def parse_memory_events(text: str) -> dict[str, int]:
    result = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) != 2 or not re.fullmatch(r"[a-z_]+", parts[0]) \
                or not re.fullmatch(r"[0-9]{1,20}", parts[1]) or parts[0] in result:
            raise OpsError("oom_evidence_unavailable", "Container memory events are not readable.", 409)
        result[parts[0]] = int(parts[1])
    if "oom" not in result or "oom_kill" not in result:
        raise OpsError("oom_evidence_unavailable", "Container memory events lack OOM counters.", 409)
    return result


async def _memory_events(spec, identity, cancel_event, runner):
    try:
        result = await _cancellable(runner(
            ["docker", "--context", spec.docker_context, "container", "exec",
             identity["container_id"], "cat", "/sys/fs/cgroup/memory.events"],
            timeout=5, max_stdout=16 * 1024, max_stderr=8 * 1024), cancel_event, timeout=6)
    except (OpsError, asyncio.TimeoutError, OSError) as exc:
        raise OpsError("oom_evidence_unavailable", "Cannot independently read the target's OOM counters.", 409) from exc
    if result.returncode:
        raise OpsError("oom_evidence_unavailable", "The target does not expose readable cgroup v2 OOM counters.", 409)
    return parse_memory_events(result.stdout)


async def _download(check, client, cancel_event):
    started = time.monotonic()
    digest = hashlib.sha256()
    result = {"url": check.url, "ok": False, "status_code": None,
              "bytes_received": 0, "expected_bytes": check.expected_bytes,
              "sha256": None, "expected_sha256": check.expected_sha256,
              "error": None, "duration_seconds": None}
    headers = {"Accept-Encoding": "identity"}
    if check.credential_env:
        credential = os.environ.get(check.credential_env, "").strip()
        if not credential:
            result.update(error="check_credential_missing", duration_seconds=0.0)
            return result
        if check.auth_scheme == "Cookie":
            headers["Cookie"] = credential
        else:
            headers["Authorization"] = f"{check.auth_scheme} {credential}"

    async def consume():
        async with client.stream("GET", check.url, headers=headers,
                                 timeout=check.timeout_seconds, follow_redirects=False) as response:
            result["status_code"] = response.status_code
            if 300 <= response.status_code < 400:
                result["error"] = "check_redirect_rejected"
                return
            if response.status_code != check.expected_status:
                result["error"] = "check_http_status"
                return
            if response.headers.get("content-encoding", "identity").lower().strip() not in ("", "identity"):
                # Raw bytes are bounded even for a malicious compressed response.
                result["error"] = "check_content_encoding"
                return
            length = response.headers.get("content-length")
            if length is not None and (not length.isdigit() or int(length) != check.expected_bytes):
                result["error"] = "check_content_length"
                return
            async for chunk in response.aiter_raw(chunk_size=CHUNK_BYTES):
                if cancel_event.is_set():
                    raise asyncio.CancelledError()
                result["bytes_received"] += len(chunk)
                if result["bytes_received"] > check.expected_bytes:
                    result["error"] = "check_byte_limit"
                    return
                digest.update(chunk)
            result["sha256"] = digest.hexdigest()
            if result["bytes_received"] != check.expected_bytes:
                result["error"] = "check_truncated"
            elif result["sha256"] != check.expected_sha256:
                result["error"] = "check_hash_mismatch"
            else:
                result["ok"] = True

    try:
        await _cancellable(consume(), cancel_event, timeout=check.deadline_seconds)
    except asyncio.TimeoutError:
        result["error"] = "check_deadline_exceeded"
    except httpx.TimeoutException:
        result["error"] = "check_http_timeout"
    except (httpx.HTTPError, OSError):
        # Never persist exception text: it can contain authorization or body data.
        result["error"] = "check_transfer_failed"
    result["duration_seconds"] = round(time.monotonic() - started, 4)
    return result


async def verify_functional(spec: TargetSpec, cancel_event: asyncio.Event, *,
                            docker_factory=DockerTarget, http_client=None,
                            interval: float = .5, runner=run_command) -> dict:
    """Verify downloaded content, stable health/identity and absence of new OOMs.

A verified result proves the complete enrolled operation plus actual OOM and
health checks under this measured workload. It is not a continuous memory
profile: the peak is the maximum of the recorded samples, and a transfer that
finishes between polls reports no in-transfer sample instead of inventing one.
The caller still performs its availability window and decides application state.
"""
    started = time.monotonic()
    report = {"verified": False, "reason": "verification_incomplete", "started_at": utc_now(),
              "checks": [], "samples": [], "sample_count": 0,
              "sampled_peak_memory_bytes": None, "memory_samples": 0,
              "in_transfer_samples": 0, "transfer_coverage": "no_in_transfer_sample",
              "failed_source": None, "identity": None,
              "cgroup_before": None, "cgroup_after": None,
              "oom_delta": None, "oom_kill_delta": None,
              "baseline_worker_failures": [], "new_worker_failures": []}
    if cancel_event.is_set():
        raise asyncio.CancelledError()
    if not spec.recovery_checks:
        report.update(reason="no_functional_checks", completed_at=utc_now(), duration_seconds=0.0)
        return report
    if not isinstance(interval, (int, float)) or not math.isfinite(interval) or interval <= 0:
        raise ValueError("interval must be finite and positive")
    effective_interval = max(float(interval), max(check.deadline_seconds for check in spec.recovery_checks) / (MAX_SAMPLES - 4))
    target = docker_factory(spec)
    pinned = None
    baseline_failures = set()
    baseline_restart_count = None
    baseline_oom_killed = None
    credential_names = {check.credential_env for check in [*spec.probes, *spec.recovery_checks]
                        if check.credential_env}
    known_credentials = tuple(os.environ[name] for name in credential_names if os.environ.get(name))
    owned_client = http_client is None
    client = http_client or httpx.AsyncClient(trust_env=False, limits=httpx.Limits(max_connections=3))
    active = []

    def reject(code, message, source):
        report["failed_source"] = source
        raise OpsError(code, message, 409)

    async def observe():
        try:
            sample = await _cancellable(target.collect(), cancel_event, timeout=25)
        except asyncio.TimeoutError as exc:
            report["failed_source"] = "collection"
            raise OpsError("verification_observation_timeout", "Independent observation timed out.", 409) from exc
        if not isinstance(sample, dict) or not _fresh(sample):
            reject("verification_evidence_stale", "Independent observations must be fresh.", "timestamp")
        try:
            current_identity = _identity(sample)
        except OpsError:
            report["failed_source"] = "identity"
            raise
        if pinned is not None and current_identity != pinned:
            reject("verification_target_changed", "The target changed during functional verification.", "identity")
        collection_state, collection_source = check_collection(sample)
        if collection_state != "ok":
            reject("verification_collection_failed",
                   "Essential collection failed; partial telemetry cannot verify recovery.",
                   collection_source)
        resources_state, resources_source = check_resources(sample.get("resources"))
        if resources_state == "incomplete":
            reject("verification_observation_incomplete", "Complete memory measurements are required.",
                   resources_source)
        logs_state, logs_source = check_logs(sample)
        if logs_state != "ok":
            reject("verification_observation_incomplete", "Complete log evidence is required.", logs_source)
        runtime_state, runtime_source = check_runtime(sample)
        if runtime_state == "incomplete":
            reject("verification_observation_incomplete", "Complete runtime state is required.", runtime_source)
        probes_state, probes_source = check_probes(spec, sample)
        if probes_state == "incomplete":
            reject("verification_observation_incomplete", "Complete probe results are required.", probes_source)
        runtime = sample.get("runtime") or {}
        restart_count = runtime.get("restart_count")
        if not _integer(restart_count):
            reject("verification_observation_incomplete", "Container restart counters are missing.",
                   "runtime.restart_count")
        compact = {"timestamp": sample["timestamp"], "observed_at": utc_now(),
                   "elapsed_seconds": round(time.monotonic() - started, 4),
                   "healthy": sample.get("healthy") is True,
                   "memory_bytes": (sample.get("resources") or {}).get("memory_bytes"),
                   "memory_limit_bytes": (sample.get("resources") or {}).get("memory_limit_bytes"),
                   "cpu_percent": (sample.get("resources") or {}).get("cpu_percent"),
                   "restart_count": restart_count}
        if len(report["samples"]) >= MAX_SAMPLES:
            reject("verification_sample_limit", "Functional verification exceeded its observation bound.",
                   "samples")
        report["samples"].append(compact)
        report["sample_count"] = len(report["samples"])
        memory = compact["memory_bytes"]
        if valid_metric(memory):
            report["memory_samples"] += 1
            report["sampled_peak_memory_bytes"] = max(report["sampled_peak_memory_bytes"] or 0, memory)
        if runtime_state != "ok":
            reject("verification_unhealthy", "Application health failed during the functional check.",
                   runtime_source)
        if probes_state != "ok":
            reject("verification_unhealthy", "Enrolled probe results did not succeed.", probes_source)
        if sample.get("healthy") is not True:
            reject("verification_unhealthy", "Application health failed during the functional check.", "healthy")
        if baseline_restart_count is not None and restart_count != baseline_restart_count:
            reject("verification_restarted", "The application restarted during the functional check.",
                   "runtime.restart_count")
        if baseline_oom_killed is False and runtime.get("oom_killed") is True:
            reject("verification_oom", "The container reported a new OOM kill.", "runtime.oom_killed")
        if pinned is not None:
            new_failures = _failure_lines(sample) - baseline_failures
            if new_failures:
                report["new_worker_failures"] = [redact_text(line, known_credentials)[:800]
                                                  for line in sorted(new_failures)[:8]]
                reject("verification_worker_failure",
                       "New worker failure logs appeared during verification.", "logs")
        return sample

    async def poll():
        while True:
            await asyncio.sleep(effective_interval)
            await observe()

    pre_transfer_samples = None
    post_transfer_start = None
    try:
        initial = await observe()
        pinned = _identity(initial)
        report["identity"] = pinned
        baseline_failures = _failure_lines(initial)
        # Historical lines stay visible separately from lines added by the workload.
        report["baseline_worker_failures"] = [redact_text(line, known_credentials)[:800]
                                              for line in sorted(baseline_failures)[:8]]
        baseline_restart_count = initial["runtime"]["restart_count"]
        baseline_oom_killed = initial["runtime"].get("oom_killed")
        report["cgroup_before"] = await _memory_events(spec, pinned, cancel_event, runner)

        async def transfer_all():
            transfers = [asyncio.create_task(_download(check, client, cancel_event)) for check in spec.recovery_checks]
            try:
                return await asyncio.gather(*transfers)
            finally:
                for task in transfers:
                    task.cancel()
                await asyncio.gather(*transfers, return_exceptions=True)

        pre_transfer_samples = len(report["samples"])
        transfers = asyncio.create_task(transfer_all())
        monitor = asyncio.create_task(poll())
        cancel = asyncio.create_task(cancel_event.wait())
        active = [transfers, monitor, cancel]
        done, _ = await asyncio.wait(set(active), return_when=asyncio.FIRST_COMPLETED)
        if cancel in done and cancel_event.is_set():
            raise asyncio.CancelledError()
        if monitor in done:
            await monitor  # propagate an observation failure, never ignore it
        report["checks"] = await transfers
        post_transfer_start = len(report["samples"])
        monitor.cancel()
        await asyncio.gather(monitor, return_exceptions=True)
        await observe()
        report["cgroup_after"] = await _memory_events(spec, pinned, cancel_event, runner)
        # A final identity check catches changes during cgroup collection itself.
        await observe()
        deltas = {output: report["cgroup_after"][counter] - report["cgroup_before"][counter]
                  for counter, output in (("oom", "oom_delta"), ("oom_kill", "oom_kill_delta"))}
        report.update(deltas)
        for delta in deltas.values():
            if delta < 0:
                report["failed_source"] = "cgroup.memory_events"
                raise OpsError("verification_cgroup_reset", "Container memory counters reset during verification.", 409)
            if delta:
                report["failed_source"] = "cgroup.memory_events"
                raise OpsError("verification_oom", "New OOM events appeared during the functional check.", 409)
        failed = next((check for check in report["checks"] if not check["ok"]), None)
        if failed:
            report["reason"] = failed["error"] or "functional_check_failed"
            report["failed_source"] = failed.get("url")
        else:
            report.update(verified=True, reason="functional_checks_passed")
    except OpsError as exc:
        report["reason"] = exc.code
        if report["failed_source"] is None:
            report["failed_source"] = "cgroup.memory_events" if exc.code == "oom_evidence_unavailable" else "collection"
    except (httpx.HTTPError, OSError):
        report["reason"] = "verification_transport_error"
        report["failed_source"] = "transport"
    finally:
        for task in active:
            task.cancel()
        if active:
            await asyncio.gather(*active, return_exceptions=True)
        if owned_client:
            await client.aclose()
        end = post_transfer_start if post_transfer_start is not None else len(report["samples"])
        report["in_transfer_samples"] = 0 if pre_transfer_samples is None else max(0, end - pre_transfer_samples)
        report["transfer_coverage"] = "sampled" if report["in_transfer_samples"] else "no_in_transfer_sample"
        report["completed_at"] = utc_now()
        report["duration_seconds"] = round(time.monotonic() - started, 4)
    return report
