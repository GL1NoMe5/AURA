"""Shared bounded validators for verification evidence quality.

Pure checks over DockerTarget.collect() observations: no I/O, no models, no
mutations. Each check returns a (state, source) pair so callers can separate
incomplete evidence (cannot support any verdict) from unhealthy evidence
(definitive negative signal) and report which required source failed.

An explicit resource limit of 0 is supported as unlimited; None is unknown.
The collector normally preserves the positive limit reported by Docker stats
(possibly the host limit for an uncapped container). HostConfig.Memory == 0
is a separate configuration field, not necessarily the resource limit.
"""
from __future__ import annotations

import math


UNLIMITED_MEMORY_LIMIT = 0
MEMORY_PRESSURE_RATIO = 0.90
HEALTHY_CONTAINER_STATES = ("healthy", "none")


def valid_metric(value) -> bool:
    """Finite nonnegative number; bool is not a metric."""
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def check_collection(sample: dict) -> tuple[str, str | None]:
    """("ok", None) or ("failed", source) for transport-level collection."""
    error = sample.get("error")
    if isinstance(error, dict) and error.get("code"):
        return ("failed", "error")
    failures = sample.get("collection_errors")
    if failures:
        if isinstance(failures, dict) and failures:
            return ("failed", f"collection_errors.{sorted(failures)[0]}")
        return ("failed", "collection_errors")
    return ("ok", None)


def check_resources(resources) -> tuple[str, str | None]:
    """Memory usability: ("ok"|"unlimited", None) or ("incomplete", source)."""
    if not isinstance(resources, dict):
        return ("incomplete", "resources")
    if not valid_metric(resources.get("memory_bytes")):
        return ("incomplete", "resources.memory_bytes")
    limit = resources.get("memory_limit_bytes")
    if limit is None:
        # Unknown limit is never treated as unlimited.
        return ("incomplete", "resources.memory_limit_bytes")
    if not valid_metric(limit):
        return ("incomplete", "resources.memory_limit_bytes")
    if limit == UNLIMITED_MEMORY_LIMIT:
        return ("unlimited", None)
    return ("ok", None)


def pressure_ok(resources: dict, ratio: float = MEMORY_PRESSURE_RATIO) -> bool:
    """Usage below the limit ratio; unlimited containers have no limit to near."""
    limit = resources["memory_limit_bytes"]
    if limit == UNLIMITED_MEMORY_LIMIT:
        return True
    return resources["memory_bytes"] < ratio * limit


def check_runtime(sample: dict) -> tuple[str, str | None]:
    """Container state: ("ok", None), ("incomplete", source) or ("unhealthy", source)."""
    runtime = sample.get("runtime")
    if not isinstance(runtime, dict):
        return ("incomplete", "runtime")
    if not isinstance(runtime.get("running"), bool):
        return ("incomplete", "runtime.running")
    if runtime["running"] is not True:
        return ("unhealthy", "runtime.running")
    if "status" not in runtime:
        return ("incomplete", "runtime.status")
    if runtime["status"] != "running":
        return ("unhealthy", "runtime.status")
    if "health" not in runtime:
        return ("incomplete", "runtime.health")
    if runtime["health"] not in HEALTHY_CONTAINER_STATES:
        return ("unhealthy", "runtime.health")
    return ("ok", None)


def check_probes(spec, sample: dict) -> tuple[str, str | None]:
    """Enrolled probes: ("ok", None), ("incomplete", "probes") or ("unhealthy", url)."""
    probes = sample.get("probes")
    if not isinstance(probes, list):
        return ("incomplete", "probes")
    by_url = {}
    for probe in probes:
        if not isinstance(probe, dict) or not isinstance(probe.get("url"), str):
            return ("incomplete", "probes")
        by_url.setdefault(probe["url"], probe)
    for expected in spec.probes:
        actual = by_url.get(expected.url)
        if actual is None or actual.get("ok") is not True:
            return ("unhealthy", expected.url)
        if actual.get("status_code") != expected.expected_status:
            return ("unhealthy", expected.url)
    if len(probes) != len(spec.probes):
        return ("unhealthy", "probes")
    return ("ok", None)


def check_logs(sample: dict) -> tuple[str, str | None]:
    """Log evidence: empty collected logs are valid; missing logs are not."""
    if not isinstance(sample.get("logs"), str):
        return ("incomplete", "logs")
    return ("ok", None)
