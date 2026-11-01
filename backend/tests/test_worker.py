"""Offline tests for the SIM-001 isolated cache worker.

Unit cases use production constants (2 MiB blocks, 8 healthy, 96 MiB
ceiling). Integration cases spawn the real worker subprocess with real
timing and measured RSS via psutil. No provider requests are made.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

import psutil
import pytest

_BACKEND = Path(__file__).resolve().parents[1]
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from aura_worker import (  # noqa: E402
    BLOCK_SIZE_BYTES,
    CEILING_BYTES,
    HEALTHY_CACHE_BYTES,
    CacheSimulator,
    ControllerState,
    WorkerController,
    WorkerDeadError,
    WorkerError,
)
from aura_worker.protocol import ProtocolError, parse_request_line  # noqa: E402
from aura_worker.worker import WorkerApp  # noqa: E402

MIB = 1024 * 1024


# ---------------------------------------------------------------- unit

@pytest.mark.parametrize("line,code", [
    ("not json", "malformed_request"),
    ('[]', "malformed_request"),
    ('{"command": "status"}', "malformed_request"),
    ('{"id": 7, "command": "status"}', "malformed_request"),
    ('{"id": "", "command": "status"}', "malformed_request"),
    ('{"id": "a", "command": "status", "extra": 1}', "malformed_request"),
    ('{"id": "a", "command": "bogus"}', "unknown_command"),
    ('{"id": "a", "command": "status", "params": []}', "invalid_params"),
    ('{"id": "a", "command": "status", "params": {"path": "/tmp/x"}}',
     "invalid_params"),
    ('{"id": "a", "command": "status", "params": {"code": "print(1)"}}',
     "invalid_params"),
    ("", "malformed_request"),
])
def test_protocol_strict_validation(line, code):
    with pytest.raises(ProtocolError) as excinfo:
        parse_request_line(line)
    assert excinfo.value.code == code


def test_protocol_worker_app_errors_keep_framing():
    app = WorkerApp()
    bad, should_exit = app.handle('{"id": "x1", "command": "nope"}')
    assert should_exit is False
    payload = json.loads(bad)
    assert payload == {"id": "x1", "ok": False, "error": {
        "code": "unknown_command", "message": "unknown command: nope"}}
    good, _ = app.handle('{"id": "x2", "command": "status"}')
    payload = json.loads(good)
    assert payload["id"] == "x2" and payload["ok"] is True
    assert payload["result"]["retained_cache_bytes"] == 0


def test_healthy_cache_bound_after_many_jobs():
    cache = CacheSimulator()
    try:
        cache.initialize()
        for _ in range(40):
            assert cache.process_one_job() is True
        snap = cache.snapshot()
        assert snap["retained_cache_bytes"] == HEALTHY_CACHE_BYTES
        assert snap["block_count"] == 8
        assert snap["completed_jobs"] == 40
        assert snap["failed_jobs"] == 0
    finally:
        cache.close()


def test_ceiling_rejects_jobs_and_never_exceeds():
    cache = CacheSimulator()
    try:
        cache.initialize()
        cache.inject_cache_retention()
        results = [cache.process_one_job() for _ in range(60)]
        snap = cache.snapshot()
        assert snap["retained_cache_bytes"] == CEILING_BYTES
        assert snap["retained_cache_bytes"] <= CEILING_BYTES
        assert results.count(False) == snap["failed_jobs"] > 0
        assert snap["completed_jobs"] == (
            CEILING_BYTES - HEALTHY_CACHE_BYTES) // BLOCK_SIZE_BYTES
    finally:
        cache.close()


def test_restore_retains_exactly_eight_blocks():
    cache = CacheSimulator()
    try:
        cache.initialize()
        cache.inject_cache_retention()
        for _ in range(20):
            cache.process_one_job()
        assert cache.snapshot()["block_count"] == 28
        cache.restore_cache_policy()
        snap = cache.snapshot()
        assert snap["block_count"] == 8
        assert snap["retained_cache_bytes"] == HEALTHY_CACHE_BYTES
        assert snap["eviction_enabled"] is True
    finally:
        cache.close()


def test_detection_requires_three_consecutive_samples():
    controller = WorkerController()
    controller._set_state(ControllerState.STARTING)
    controller.last_worker_status = {"initialized": True}
    for rss in (30 * MIB, 30 * MIB, 30 * MIB, 30 * MIB, 30 * MIB):
        controller._update_baseline_and_detection(rss)
    assert controller.baseline_rss_bytes == 30 * MIB
    controller._set_state(ControllerState.HEALTHY)
    over = 30 * MIB + 24 * MIB + 1
    controller._update_baseline_and_detection(over)
    controller._update_baseline_and_detection(10 * MIB)  # resets streak
    controller._update_baseline_and_detection(over)
    controller._update_baseline_and_detection(over)
    assert controller.state == ControllerState.HEALTHY
    controller._update_baseline_and_detection(over)
    assert controller.state == ControllerState.FAULT_DETECTED
    assert controller.detected_at is not None


# ---------------------------------------------------------- integration

def _pid_alive(pid: int) -> bool:
    return psutil.pid_exists(pid) and psutil.Process(pid).is_running()


async def _start() -> WorkerController:
    controller = WorkerController()
    await asyncio.wait_for(controller.start(), timeout=30)
    return controller


def test_controller_unknown_command_keeps_framing():
    async def run():
        controller = await _start()
        try:
            with pytest.raises(WorkerError) as excinfo:
                await controller.send_command("bogus_command")
            assert excinfo.value.code == "unknown_command"
            status = await controller.status()
            assert status["retained_cache_bytes"] == HEALTHY_CACHE_BYTES
        finally:
            await controller.stop()
    asyncio.run(run())


def test_real_subprocess_fault_detection_and_recovery():
    async def run():
        controller = await _start()
        pid = controller.child_pid
        try:
            assert controller.baseline_rss_bytes is not None
            healthy = await controller.status()
            assert healthy["retained_cache_bytes"] == HEALTHY_CACHE_BYTES
            await controller.inject_cache_retention()
            assert await controller.wait_for_detection(timeout=30) is True
            deadline = time.monotonic() + 60
            status = healthy
            while time.monotonic() < deadline:
                status = await controller.status()
                if (status["retained_cache_bytes"] == CEILING_BYTES
                        and status["failed_jobs"] > 0):
                    break
                await asyncio.sleep(0.25)
            assert status["retained_cache_bytes"] == CEILING_BYTES
            assert status["failed_jobs"] > 0
            assert controller.peak_rss_bytes is not None
            assert (controller.peak_rss_bytes
                    > controller.baseline_rss_bytes + 24 * MIB)
            result = await controller.restore_and_verify(timeout=40)
            assert result.verified is True, result
            assert result.duration_seconds >= 7.9
            after = await controller.status()
            assert after["retained_cache_bytes"] == HEALTHY_CACHE_BYTES
            assert controller.state == ControllerState.RECOVERED
        finally:
            await controller.stop()
        assert pid is not None and not _pid_alive(pid)
    asyncio.run(run())


def test_stop_idempotent_reset_and_cleanup():
    async def run():
        controller = await _start()
        first_pid = controller.child_pid
        await asyncio.sleep(1.0)
        before = await controller.status()
        assert before["completed_jobs"] > 0
        await controller.reset()
        try:
            assert controller.generation == 2
            assert controller.child_pid != first_pid
            assert first_pid is not None and not _pid_alive(first_pid)
            fresh = await controller.status()
            assert fresh["retained_cache_bytes"] == HEALTHY_CACHE_BYTES
            assert fresh["completed_jobs"] < before["completed_jobs"]
            assert controller.baseline_rss_bytes is not None
        finally:
            await controller.stop()
            await controller.stop()  # idempotent
        assert controller.state == ControllerState.STOPPED
        assert controller.child_pid is not None
        assert not _pid_alive(controller.child_pid)
    asyncio.run(run())


def test_worker_death_is_reported_honestly():
    async def run():
        controller = await _start()
        pid = controller.child_pid
        assert pid is not None
        tree = [pid] + [p.pid for p in
                        psutil.Process(pid).children(recursive=True)]
        psutil.Process(pid).terminate()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if controller.state == ControllerState.DEAD:
                break
            await asyncio.sleep(0.1)
        assert controller.state == ControllerState.DEAD
        with pytest.raises(WorkerDeadError):
            await controller.status()
        await controller.stop()
        for dead_pid in tree:
            assert not _pid_alive(dead_pid)
    asyncio.run(run())


def test_cancellation_during_verification_cleans_child():
    async def run():
        controller = await _start()
        pid = controller.child_pid
        await controller.inject_cache_retention()
        await asyncio.sleep(1.0)
        task = asyncio.create_task(controller.restore_and_verify(timeout=40))
        await asyncio.sleep(0.5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and controller.is_running:
            await asyncio.sleep(0.1)
        assert not controller.is_running
        assert pid is not None and not _pid_alive(pid)
    asyncio.run(run())
