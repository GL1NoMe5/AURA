"""Focused regressions for SIM-001-R1 review corrections.

Covers fresh telemetry, freshness-gated verification, generation
isolation, cancellation cleanup, identity-verified process termination
and strict harness acceptance. Offline; no provider requests.
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import psutil
import pytest

_BACKEND = Path(__file__).resolve().parents[1]
_REPO = _BACKEND.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from aura_worker import (  # noqa: E402
    CEILING_BYTES,
    HEALTHY_CACHE_BYTES,
    ControllerState,
    ProcessIdentity,
    WorkerController,
)
from aura_worker.controller import (  # noqa: E402
    RSS_THRESHOLD_OFFSET_BYTES,
)

_spec = importlib.util.spec_from_file_location(
    "worker_check", _REPO / "tools" / "preflight" / "worker_check.py")
assert _spec is not None and _spec.loader is not None
worker_check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(worker_check)

MIB = 1024 * 1024


def _pid_alive(pid: int) -> bool:
    return psutil.pid_exists(pid) and psutil.Process(pid).is_running()


# ------------------------------------------------------------ unit

class _FakeProc:
    def __init__(self, pid: int, create_time: float):
        self.pid = pid
        self._create_time = create_time
        self.terminate_calls = 0

    def create_time(self) -> float:
        return self._create_time

    def terminate(self) -> None:
        self.terminate_calls += 1


def test_recycled_pid_mismatch_is_never_terminated():
    fake = _FakeProc(pid=4242, create_time=9999.0)
    terminated = WorkerController._terminate_owned_processes(
        [ProcessIdentity(pid=4242, create_time=1111.0)],
        process_factory=lambda pid: fake,
    )
    assert terminated == []
    assert fake.terminate_calls == 0

    matching = _FakeProc(pid=4243, create_time=2222.0)
    terminated = WorkerController._terminate_owned_processes(
        [ProcessIdentity(pid=4243, create_time=2222.0)],
        process_factory=lambda pid: matching,
    )
    assert terminated == [4243]
    assert matching.terminate_calls == 1


def test_old_generation_cannot_append_or_publish():
    controller = WorkerController()
    controller.generation = 2
    assert controller._record_measurement(1, 30 * MIB) is False
    assert controller._record_status(1, {"retained_cache_bytes": 1}) is False
    assert controller._update_baseline_and_detection(30 * MIB, 1) is False
    assert len(controller.measurements) == 0
    assert controller._record_measurement(2, 30 * MIB) is True
    assert len(controller.measurements) == 1
    assert controller.measurements[0].generation == 2


def _fake_verify_controller(monkeypatch, rss_monotonic_offset):
    controller = WorkerController()
    controller.generation = 1
    controller.baseline_rss_bytes = 30 * MIB
    controller._proc = SimpleNamespace(returncode=None)
    controller._set_state(ControllerState.HEALTHY)
    controller._latest_rss_bytes = 30 * MIB
    controller._latest_rss_generation = 1
    controller._latest_rss_monotonic = (
        time.monotonic() + rss_monotonic_offset)
    counter = {"completed": 100}

    async def fake_send(generation, proc, command, timeout=None):
        counter["completed"] += 4
        return {
            "retained_cache_bytes": HEALTHY_CACHE_BYTES,
            "completed_jobs": counter["completed"],
            "failed_jobs": 3,
        }

    monkeypatch.setattr(controller, "_send_command_bound", fake_send)
    return controller


def test_stale_rss_cannot_produce_verified_success(monkeypatch):
    async def run():
        controller = _fake_verify_controller(monkeypatch, -100.0)
        result = await controller.restore_and_verify(timeout=1.2)
        assert result.verified is False
        assert result.reason == "verification_timeout"
        assert controller.state != ControllerState.RECOVERED
    asyncio.run(run())


def test_unavailable_rss_cannot_produce_verified_success(monkeypatch):
    async def run():
        controller = _fake_verify_controller(monkeypatch, 0.0)
        controller._latest_rss_bytes = None
        result = await controller.restore_and_verify(timeout=1.2)
        assert result.verified is False
        assert controller.state != ControllerState.RECOVERED
    asyncio.run(run())


class _FakeStdin:
    def __init__(self):
        self.writes: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.writes.append(data)

    async def drain(self) -> None:
        return None


def test_cancelled_command_future_is_removed_from_pending():
    async def run():
        controller = WorkerController()
        controller.generation = 1
        controller._proc = SimpleNamespace(
            returncode=None, stdin=_FakeStdin(), pid=12345)
        task = asyncio.create_task(controller.send_command("status"))
        await asyncio.sleep(0.1)
        assert controller._pending != {}
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert controller._pending == {}
    asyncio.run(run())


# ------------------------------------------------------------ harness

def _valid_record() -> dict:
    baseline = 40_000_000
    return {
        "error": None,
        "fault_detected": True,
        "ceiling_reached": True,
        "peak_retained_cache_bytes": CEILING_BYTES,
        "healthy_retained_cache_bytes": HEALTHY_CACHE_BYTES,
        "recovered_retained_cache_bytes": HEALTHY_CACHE_BYTES,
        "failed_jobs_at_ceiling": 9,
        "baseline_rss_bytes": baseline,
        "peak_rss_bytes": baseline + RSS_THRESHOLD_OFFSET_BYTES + MIB,
        "recovered_rss_bytes": baseline + MIB,
        "recovery_verified": True,
        "verified_recovery_duration_seconds": 8.0,
        "verification_completed_start": 100,
        "verification_completed_end": 132,
        "verification_failed_start": 9,
        "verification_failed_end": 9,
        "owned_tree": [{"pid": 123, "create_time": 1.0}],
        "surviving_owned_processes": [],
        "child_alive_after_shutdown": False,
    }


def test_harness_accepts_only_complete_cycle():
    assert worker_check.cycle_accepted(_valid_record()) is True


@pytest.mark.parametrize(("key", "value", "check"), [
    ("fault_detected", False, "fault_detected"),
    ("ceiling_reached", False, "ceiling_reached"),
    ("peak_retained_cache_bytes", CEILING_BYTES - 1, "ceiling_reached"),
    ("failed_jobs_at_ceiling", 0, "failed_jobs_positive"),
    ("verified_recovery_duration_seconds", 7.9,
     "verification_at_least_eight_seconds"),
    ("error", "WorkerError: boom", "no_error"),
    ("surviving_owned_processes", [{"pid": 1, "create_time": 1.0}],
     "owned_tree_cleanup"),
    ("child_alive_after_shutdown", True, "owned_tree_cleanup"),
    ("verification_failed_end", 10, "no_new_failures_during_verification"),
    ("verification_completed_end", 100, "verification_job_progress"),
    ("recovered_retained_cache_bytes", HEALTHY_CACHE_BYTES - 1,
     "recovered_cache_exact"),
    ("recovered_rss_bytes", 40_000_000 + RSS_THRESHOLD_OFFSET_BYTES + 1,
     "recovered_rss_within_threshold"),
])
def test_harness_rejects_incomplete_cycles(key, value, check):
    record = _valid_record()
    record[key] = value
    checks = worker_check.evaluate_cycle(record)
    assert checks[check] is False
    assert worker_check.cycle_accepted(record) is False


# ---------------------------------------------------------- integration

async def _start() -> WorkerController:
    controller = WorkerController()
    await asyncio.wait_for(controller.start(), timeout=30)
    return controller


def test_sampling_updates_telemetry_without_status_calls():
    async def run():
        controller = await _start()
        try:
            started = await controller.status()
            assert started["retained_cache_bytes"] == HEALTHY_CACHE_BYTES
            marker = time.monotonic()
            await controller.inject_cache_retention()
            # No status() calls below: the background poller must publish.
            await asyncio.sleep(3.0)
            fresh = [
                m for m in controller.measurements
                if m.monotonic >= marker and m.status_fresh
            ]
            assert len(fresh) >= 3
            completed = [m.completed_jobs for m in fresh
                         if m.completed_jobs is not None]
            retained = [m.retained_cache_bytes for m in fresh
                        if m.retained_cache_bytes is not None]
            assert max(completed) > min(completed)
            assert max(retained) > HEALTHY_CACHE_BYTES
            snapshot = controller.snapshot()
            assert snapshot["worker_status_stale"] is False
            assert snapshot["rss_fresh"] is True
        finally:
            await controller.stop()
    asyncio.run(run())


def test_reset_during_verification_cannot_resolve_or_damage_replacement():
    async def run():
        controller = await _start()
        old_pid = controller.child_pid
        await controller.inject_cache_retention()
        await asyncio.sleep(1.0)
        task = asyncio.create_task(controller.restore_and_verify(timeout=40))
        deadline = time.monotonic() + 5
        while controller.state != ControllerState.VERIFYING:
            assert time.monotonic() < deadline
            await asyncio.sleep(0.05)
        await controller.reset()
        result = await asyncio.wait_for(task, timeout=15)
        assert result.verified is False
        assert controller.generation == 2
        assert controller.child_pid != old_pid
        try:
            assert controller.state == ControllerState.HEALTHY
            status = await controller.status()
            assert status["retained_cache_bytes"] == HEALTHY_CACHE_BYTES
            await asyncio.sleep(1.0)
            # The old verifier must not mutate the replacement afterwards.
            assert controller.state == ControllerState.HEALTHY
            assert controller.generation == 2
        finally:
            await controller.stop()
    asyncio.run(run())


def test_cancellation_while_restore_pending_cleans_owned_tree():
    async def run():
        controller = await _start()
        pid = controller.child_pid
        original_send = controller._send_command_bound

        async def hanging_restore(generation, proc, command, timeout=None):
            if command == "restore_cache_policy":
                await asyncio.sleep(60)
            return await original_send(generation, proc, command, timeout)

        controller._send_command_bound = hanging_restore
        task = asyncio.create_task(controller.restore_and_verify(timeout=40))
        await asyncio.sleep(0.3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert controller.is_running is False
        assert controller._pending == {}
        assert pid is not None and not _pid_alive(pid)
        for identity in controller.owned_process_identities:
            owned_pid = identity["pid"]
            if psutil.pid_exists(owned_pid):
                # Only a recycled PID (different creation time) may exist.
                proc = psutil.Process(owned_pid)
                assert abs(proc.create_time()
                           - identity["create_time"]) > 0.01
    asyncio.run(run())
