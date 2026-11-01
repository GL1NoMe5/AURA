"""OPS-VERIFY-GUARD-001: incomplete telemetry cannot verify recovery (offline)."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_ops_service import Docker, make_service, observed, target  # noqa: E402
from test_ops_verification import (DATA, Chunks, CgroupRunner, Target,  # noqa: E402
                                   run as run_functional)


# The shared Target fixture already matches DockerTarget.collect()'s complete
# shape (status/health/probes); CompleteTarget keeps these tests explicit.
CompleteTarget = Target


@pytest.mark.parametrize("failure", ["cgroup", "health"])
def test_failure_before_download_does_not_claim_transfer_coverage(failure):
    async def check():
        docker, runner, stream = CompleteTarget(), CgroupRunner(), Chunks()
        if failure == "cgroup":
            runner.unavailable = True
        else:
            docker.mutate = lambda sample, count: sample["runtime"].update(health="unhealthy")
        result, _, _, _ = await run_functional(target=docker, runner=runner, stream=stream)
        assert result["verified"] is False
        assert stream.count == 0
        assert result["in_transfer_samples"] == 0
        assert result["transfer_coverage"] == "no_in_transfer_sample"
        if failure == "cgroup":
            assert result["failed_source"] == "cgroup.memory_events"
    asyncio.run(check())


def healthy_observed(**overrides):
    observation = observed()
    observation["runtime"] = {"running": True, "status": "running", "health": "none"}
    for key, value in overrides.items():
        observation[key] = value
    return observation


@pytest.mark.parametrize("source", ["resources", "logs"])
def test_functional_rejects_partial_collection_errors(source):
    async def check():
        docker = CompleteTarget()
        docker.mutate = lambda sample, count: sample.update(collection_errors={source: "docker_timeout"})
        result, _, _, stream = await run_functional(target=docker)
        assert result["verified"] is False
        assert result["reason"] == "verification_collection_failed"
        assert result["failed_source"] == f"collection_errors.{source}"
        assert stream.count == 0
        assert docker.active == 0
    asyncio.run(check())


def test_functional_rejects_observation_error_field():
    async def check():
        docker = CompleteTarget()
        docker.mutate = lambda sample, count: sample.update(
            error={"code": "docker_unavailable", "message": "Engine gone."})
        result, _, _, _ = await run_functional(target=docker)
        assert result["verified"] is False
        assert result["reason"] == "verification_collection_failed"
        assert result["failed_source"] == "error"
    asyncio.run(check())


@pytest.mark.parametrize("memory", [None, -1, float("inf"), float("nan"), True, "40000000"],
                         ids=["null", "negative", "infinite", "nan", "bool", "string"])
def test_functional_rejects_unusable_memory_bytes(memory):
    async def check():
        docker = CompleteTarget()
        docker.mutate = lambda sample, count: sample["resources"].update(memory_bytes=memory)
        result, _, _, _ = await run_functional(target=docker)
        assert result["verified"] is False
        assert result["reason"] == "verification_observation_incomplete"
        assert result["failed_source"] == "resources.memory_bytes"
        assert docker.active == 0
    asyncio.run(check())


@pytest.mark.parametrize("limit", [None, -5, float("inf"), True, "large"],
                         ids=["missing", "negative", "infinite", "bool", "string"])
def test_functional_rejects_unknown_memory_limit(limit):
    async def check():
        docker = CompleteTarget()
        docker.mutate = lambda sample, count: sample["resources"].update(memory_limit_bytes=limit)
        result, _, _, _ = await run_functional(target=docker)
        assert result["verified"] is False
        assert result["reason"] == "verification_observation_incomplete"
        assert result["failed_source"] == "resources.memory_limit_bytes"
    asyncio.run(check())


def test_functional_accepts_documented_unlimited_limit_zero():
    async def check():
        docker = CompleteTarget()
        docker.mutate = lambda sample, count: sample["resources"].update(memory_limit_bytes=0)
        result, _, _, _ = await run_functional(target=docker, stream=Chunks(delay=.001))
        assert result["verified"] is True
        assert result["failed_source"] is None
    asyncio.run(check())


def test_functional_accepts_missing_cpu_alone():
    async def check():
        docker = CompleteTarget()
        docker.mutate = lambda sample, count: sample["resources"].update(cpu_percent=None)
        result, _, _, _ = await run_functional(target=docker, stream=Chunks(delay=.001))
        assert result["verified"] is True
    asyncio.run(check())


@pytest.mark.parametrize("logs", [None, 42, ["line"]], ids=["null", "number", "list"])
def test_functional_rejects_missing_logs(logs):
    async def check():
        docker = CompleteTarget()
        if logs is None:
            docker.mutate = lambda sample, count: sample.pop("logs", None)
        else:
            docker.mutate = lambda sample, count: sample.update(logs=logs)
        result, _, _, _ = await run_functional(target=docker)
        assert result["verified"] is False
        assert result["reason"] == "verification_observation_incomplete"
        assert result["failed_source"] == "logs"
    asyncio.run(check())


def test_functional_accepts_empty_successfully_collected_logs():
    async def check():
        docker = CompleteTarget()
        docker.mutate = lambda sample, count: sample.update(logs="")
        result, _, _, _ = await run_functional(target=docker, stream=Chunks(delay=.001))
        assert result["verified"] is True
        assert result["new_worker_failures"] == []
        assert result["baseline_worker_failures"] == []
    asyncio.run(check())


@pytest.mark.parametrize("health", ["starting", "unhealthy"])
def test_functional_rejects_non_qualifying_container_health(health):
    async def check():
        docker = CompleteTarget()
        docker.mutate = lambda sample, count: sample["runtime"].update(health=health)
        result, _, _, _ = await run_functional(target=docker)
        assert result["verified"] is False
        assert result["reason"] == "verification_unhealthy"
        assert result["failed_source"] == "runtime.health"
    asyncio.run(check())


@pytest.mark.parametrize("field", ["status", "health"])
def test_functional_rejects_missing_runtime_state(field):
    async def check():
        docker = CompleteTarget()
        docker.mutate = lambda sample, count: sample["runtime"].pop(field, None)
        result, _, _, _ = await run_functional(target=docker)
        assert result["verified"] is False
        assert result["reason"] == "verification_observation_incomplete"
        assert result["failed_source"] == f"runtime.{field}"
    asyncio.run(check())


def test_functional_rejects_paused_runtime():
    async def check():
        docker = CompleteTarget()
        docker.mutate = lambda sample, count: sample["runtime"].update(status="paused")
        result, _, _, _ = await run_functional(target=docker)
        assert result["verified"] is False
        assert result["reason"] == "verification_unhealthy"
        assert result["failed_source"] == "runtime.status"
    asyncio.run(check())


def test_functional_rejects_inconsistent_probes():
    async def check():
        docker = CompleteTarget()
        docker.mutate = lambda sample, count: sample.update(probes=[])
        result, _, _, _ = await run_functional(target=docker)
        assert result["verified"] is False
        assert result["reason"] == "verification_unhealthy"
    asyncio.run(check())


def test_functional_reports_honest_transfer_coverage_without_invented_peak(tmp_path):
    async def check():
        docker = CompleteTarget()
        result, _, _, _ = await run_functional(target=docker, stream=Chunks())
        assert result["verified"] is True
        assert result["in_transfer_samples"] == 0
        assert result["transfer_coverage"] == "no_in_transfer_sample"
        assert result["memory_samples"] == result["sample_count"] >= 3
        assert result["sampled_peak_memory_bytes"] == max(
            sample["memory_bytes"] for sample in result["samples"])
    asyncio.run(check())


def test_functional_counts_in_transfer_samples_on_slow_transfer():
    async def check():
        docker = CompleteTarget()
        result, _, _, _ = await run_functional(target=docker, stream=Chunks(delay=.001))
        assert result["verified"] is True
        assert result["in_transfer_samples"] >= 1
        assert result["transfer_coverage"] == "sampled"
    asyncio.run(check())


def test_functional_distinguishes_baseline_from_new_worker_failures():
    async def check():
        docker = CompleteTarget()
        docker.mutate = lambda sample, count: sample.update(logs="old worker 1 died\n")
        clean, _, _, _ = await run_functional(target=docker, stream=Chunks(delay=.001))
        assert clean["verified"] is True
        assert clean["baseline_worker_failures"] == ["old worker 1 died"]
        assert clean["new_worker_failures"] == []

        failing = CompleteTarget()
        def mutate(sample, count):
            sample.update(logs="old worker 1 died\n")
            if count > 1:
                sample["logs"] += "fresh worker 9 died, killed by signal 9\n"
        failing.mutate = mutate
        result, _, _, _ = await run_functional(target=failing, stream=Chunks(delay=.001))
        assert result["verified"] is False
        assert result["reason"] == "verification_worker_failure"
        assert result["baseline_worker_failures"] == ["old worker 1 died"]
        assert result["new_worker_failures"] == ["fresh worker 9 died, killed by signal 9"]
        assert failing.active == 0
    asyncio.run(check())


def test_functional_rejects_incomplete_mid_transfer_sample_without_leaks():
    async def check():
        docker = CompleteTarget()
        docker.mutate = lambda sample, count: sample["resources"].pop("memory_bytes") if count >= 3 else None
        result, _, _, stream = await run_functional(target=docker, stream=Chunks(delay=.001))
        assert result["verified"] is False
        assert result["reason"] == "verification_observation_incomplete"
        assert result["failed_source"] == "resources.memory_bytes"
        assert stream.closed
        assert docker.active == 0
    asyncio.run(check())


def test_functional_rejects_incomplete_final_observation():
    async def check():
        docker, runner = CompleteTarget(), CgroupRunner()
        runner.on_second = lambda: setattr(
            docker, "mutate", lambda sample, count: sample["resources"].pop("memory_limit_bytes"))
        result, _, _, _ = await run_functional(target=docker, runner=runner)
        assert result["verified"] is False
        assert result["reason"] == "verification_observation_incomplete"
        assert result["failed_source"] == "resources.memory_limit_bytes"
    asyncio.run(check())


def test_functional_full_download_with_missing_telemetry_never_verifies():
    async def check():
        docker, runner = CompleteTarget(), CgroupRunner()
        runner.on_second = lambda: setattr(
            docker, "mutate", lambda sample, count: sample["resources"].update(memory_bytes=None))
        result, _, _, stream = await run_functional(target=docker, runner=runner)
        assert result["checks"][0]["ok"] is True
        assert result["checks"][0]["bytes_received"] == len(DATA)
        assert result["verified"] is False
        assert result["reason"] == "verification_observation_incomplete"
        assert stream.closed
        assert docker.active == 0
    asyncio.run(check())


@pytest.mark.parametrize("defect", ["missing_resources", "usage_null", "limit_missing", "limit_bool"])
def test_availability_requires_complete_memory_before_healthy_interval(tmp_path, defect):
    async def run():
        observation = healthy_observed()
        if defect == "missing_resources":
            observation.pop("resources")
        elif defect == "usage_null":
            observation["resources"]["memory_bytes"] = None
        elif defect == "limit_missing":
            observation["resources"]["memory_limit_bytes"] = None
        else:
            observation["resources"]["memory_limit_bytes"] = True
        service = make_service(tmp_path, docker_factory=Docker(observation))
        try:
            await service.enroll(target())
            result = await service.verify(target(), asyncio.Event())
            assert not result["verified"], defect
            assert result["reason_code"] == "telemetry_incomplete"
            assert "telemetry" in result["reason"]
            assert result["incomplete_samples"] == result["sample_count"] > 0
        finally:
            await service.shutdown()
    asyncio.run(run())


def test_availability_accepts_unlimited_limit_and_keeps_pressure_gate(tmp_path):
    async def run():
        service = make_service(tmp_path, docker_factory=Docker(healthy_observed()))
        try:
            await service.enroll(target())
            service.docker_factory.state["resources"]["memory_limit_bytes"] = 0
            assert (await service.verify(target(), asyncio.Event()))["verified"] is True
            service.docker_factory.state["resources"].update(
                memory_bytes=95_000_000, memory_limit_bytes=100_000_000)
            assert (await service.verify(target(), asyncio.Event()))["verified"] is False
        finally:
            await service.shutdown()
    asyncio.run(run())


@pytest.mark.parametrize("health", ["starting", "unhealthy"])
def test_availability_rejects_non_qualifying_container_health(tmp_path, health):
    async def run():
        observation = healthy_observed()
        observation["runtime"]["health"] = health
        service = make_service(tmp_path, docker_factory=Docker(observation))
        try:
            await service.enroll(target())
            result = await service.verify(target(), asyncio.Event())
            assert result["verified"] is False
            assert result["reason_code"] == "recovery_not_observed"
        finally:
            await service.shutdown()
    asyncio.run(run())


@pytest.mark.parametrize("field", ["status", "health"])
def test_availability_rejects_missing_runtime_state(tmp_path, field):
    async def run():
        observation = healthy_observed()
        observation["runtime"].pop(field)
        service = make_service(tmp_path, docker_factory=Docker(observation))
        try:
            await service.enroll(target())
            result = await service.verify(target(), asyncio.Event())
            assert result["verified"] is False
            assert result["reason_code"] == "telemetry_incomplete"
            assert result["incomplete_samples"] > 0
        finally:
            await service.shutdown()
    asyncio.run(run())


def test_availability_reports_coverage_on_success(tmp_path):
    async def run():
        service = make_service(tmp_path, docker_factory=Docker(healthy_observed()))
        try:
            await service.enroll(target())
            result = await service.verify(target(), asyncio.Event())
            assert result["verified"] is True
            assert result["reason_code"] == "recovery_observed"
            assert result["sample_count"] >= 2
            assert result["incomplete_samples"] == 0
        finally:
            await service.shutdown()
    asyncio.run(run())


def test_availability_recovers_after_transient_incomplete_sample(tmp_path):
    async def run():
        good = healthy_observed()
        bad = healthy_observed()
        bad["resources"].pop("memory_bytes")

        class Flapping:
            reads = 0

            def __call__(self, spec, **kwargs):
                return self

            async def collect(self):
                type(self).reads += 1
                import copy
                return copy.deepcopy(bad if type(self).reads == 1 else good)

        Flapping.reads = 0
        service = make_service(tmp_path, docker_factory=Flapping())
        try:
            await service.enroll(target())
            result = await service.verify(target(), asyncio.Event())
            assert result["verified"] is True
            assert result["incomplete_samples"] == 1
        finally:
            await service.shutdown()
    asyncio.run(run())
