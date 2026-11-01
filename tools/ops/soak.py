"""Restart baseline benchmark plus 20-minute read-only observation soak.

Section A provisions one disposable Linkding sandbox and measures what a
single typed restart does (and does not) fix for two faulty configurations.
Section B then observes the actual running AURA for a full 1200-second window
without mutating anything. Default invocation is read-only preflight; --run
executes fixtures and the long observation. No provider calls, no production
module edits, no changes to registered applications or the live store.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import statistics
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "backend")]

import httpx
import psutil
from websockets.asyncio.client import connect as ws_connect

from aura_ops.credentials import load_probe_credentials
from aura_ops.docker import DockerTarget
from aura_ops.models import OpsError, TargetSpec
from aura_ops.service import ACTIVE
from tools.ops.acceptance import (
    Acceptance, RunnerError, fault_port_override, linkding_compose, owner_ok,
    pick_ports, port_occupied, BACKEND_HEALTH, CONTEXT, DOCKER, LABEL, LINKDING_IMAGE,
    LINKDING_REVISION, LINKDING_SOURCE, PORTS,
)

WINDOW_SECONDS = 1200.0
TICK_SECONDS = 3.0
WARMUP_SECONDS = 60.0
SECTION_B_BUDGET = 24 * 60
RECONNECT_AT = (400.0, 800.0)
RSS_DRIFT_LIMIT = 32 * 1024 * 1024
COVERAGE_MIN = 0.95
GAP_MAX = 15.0
HTTP_TIMEOUT = 5.0
REMEASURE_SECONDS = 30.0
SCHEDULED_SAMPLES = int(WINDOW_SECONDS // TICK_SECONDS)
STATUS_URL = "http://127.0.0.1:8000/api/v2/status"
WS_URL = "ws://127.0.0.1:8000/ws/ops"
ACTIVE_PROPOSALS = {"executing", "dispatching", "verifying", "rolling_back"}

GATES = {
    "duration": {"window_seconds": WINDOW_SECONDS, "min_sample_span_seconds": 1195.0},
    "coverage": {"scheduled_samples": SCHEDULED_SAMPLES, "min_ratio": COVERAGE_MIN},
    "gaps": {"max_unexplained_gap_seconds": GAP_MAX},
    "health": {"backend_http": 200, "targets_healthy": True, "new_entities": 0,
               "new_provider_calls": 0, "new_dispatches": 0},
    "ws": {"reconnects": len(RECONNECT_AT), "snapshot_timeout_seconds": 15.0,
           "strictly_increasing_per_connection": True, "snapshot_repeat_allowed": True,
           "snapshot_decrease_forbidden": True},
    "rss": {"edge_minutes": 5, "min_edge_samples": 60, "max_drift_bytes": RSS_DRIFT_LIMIT},
    "close": {"clean_teardown": True, "post_healthy": True},
}


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def median_or_none(values):
    return statistics.median(values) if values else None


def coverage_gate(n_valid, scheduled=SCHEDULED_SAMPLES):
    ratio = n_valid / scheduled if scheduled else 0.0
    return {"valid": n_valid, "scheduled": scheduled, "ratio": ratio,
            "passed": ratio >= COVERAGE_MIN}


def gap_gate(spans):
    """spans: ascending sample offsets. Every inter-sample gap needs ticks accounted."""
    ordered = sorted(spans)
    gaps = [b - a for a, b in zip(ordered, ordered[1:])]
    worst = max(gaps) if gaps else 0.0
    return {"max_gap_seconds": worst, "passed": worst <= GAP_MAX,
            "violations": [gap for gap in gaps if gap > GAP_MAX]}


def slope_per_minute(points):
    """Least-squares slope of (offset_seconds, value) in value/minute."""
    n = len(points)
    if n < 2:
        return None
    mean_x = sum(p[0] for p in points) / n
    mean_y = sum(p[1] for p in points) / n
    denom = sum((p[0] - mean_x) ** 2 for p in points)
    if not denom:
        return 0.0
    return sum((p[0] - mean_x) * (p[1] - mean_y) for p in points) / denom * 60.0


def rss_gate(samples):
    """samples: [{offset, rss, handles, full}] with full pid set flagged.

    Compares summed-RSS medians of the first and last five measured minutes.
    Missing metrics block the gate instead of inventing zero.
    """
    full = [s for s in samples if s.get("full") and isinstance(s.get("rss"), int)]
    if not samples:
        return {"status": "blocked", "reason": "no RSS samples measured"}
    edge = 5 * 60.0
    first = [s["rss"] for s in full if s["offset"] < edge]
    last = [s["rss"] for s in full if s["offset"] >= WINDOW_SECONDS - edge]
    detail = {"samples": len(samples), "full_set_samples": len(full),
              "first_edge_n": len(first), "last_edge_n": len(last),
              "missing": len(samples) - len(full)}
    if len(first) < GATES["rss"]["min_edge_samples"] or len(last) < GATES["rss"]["min_edge_samples"]:
        detail.update({"status": "blocked", "reason": "insufficient full-set edge samples"})
        return detail
    first_median, last_median = statistics.median(first), statistics.median(last)
    values = [s["rss"] for s in full]
    handles_first = [s["handles"] for s in full if s["offset"] < edge and isinstance(s.get("handles"), int)]
    handles_last = [s["handles"] for s in full if s["offset"] >= WINDOW_SECONDS - edge and isinstance(s.get("handles"), int)]
    detail.update({
        "status": "pass" if last_median - first_median <= GATES["rss"]["max_drift_bytes"] else "fail",
        "first_median_bytes": first_median, "last_median_bytes": last_median,
        "drift_bytes": last_median - first_median,
        "min_bytes": min(values), "max_bytes": max(values),
        "slope_bytes_per_minute": slope_per_minute([(s["offset"], s["rss"]) for s in full]),
        "handles_first_median": median_or_none(handles_first),
        "handles_last_median": median_or_none(handles_last),
        "handles_max": max([s["handles"] for s in full if isinstance(s.get("handles"), int)] or [None]),
    })
    return detail


class WsTracker:
    """Per-connection ordering validation for /ws/ops evidence."""

    def __init__(self):
        self.connections = []
        self.max_seen = None

    def connect(self):
        self.connections.append({"snapshot_seq": None, "last_seq": None,
                                 "events": 0, "violations": []})
        return len(self.connections) - 1

    def snapshot(self, index, seq):
        conn = self.connections[index]
        conn["snapshot_seq"] = seq
        if self.max_seen is not None and seq < self.max_seen:
            conn["violations"].append(f"stale snapshot seq {seq} below {self.max_seen}")
        conn["last_seq"] = seq
        self.max_seen = seq if self.max_seen is None else max(self.max_seen, seq)

    def event(self, index, seq):
        conn = self.connections[index]
        conn["events"] += 1
        if conn["last_seq"] is None or seq <= conn["last_seq"]:
            conn["violations"].append(f"non-increasing event seq {seq} after {conn['last_seq']}")
        conn["last_seq"] = seq
        self.max_seen = seq if self.max_seen is None else max(self.max_seen, seq)

    def verdict(self):
        violations = [v for conn in self.connections for v in conn["violations"]]
        snapshots = sum(1 for conn in self.connections if conn["snapshot_seq"] is not None)
        return {"connections": len(self.connections), "snapshots": snapshots,
                "violations": violations, "passed": not violations and snapshots == len(self.connections)}


def identity_changes(samples):
    """samples: [{target_id, container_id, image_id, started_at}]."""
    seen = {}
    changes = []
    for sample in samples:
        key = sample["target_id"]
        identity = (sample.get("container_id"), sample.get("image_id"), sample.get("started_at"))
        if key in seen and seen[key] != identity:
            changes.append({"target_id": key, "before": seen[key], "after": identity})
        seen.setdefault(key, identity)
    return changes


class SoakAcceptance(Acceptance):
    def __init__(self, run_id, cmd=None):
        super().__init__(run_id, cmd)
        self.dir = ROOT / ".local" / "ops-soak" / run_id


def fault_mem64_override():
    return {"services": {"linkding": {"mem_limit": "64m", "memswap_limit": "64m"}}}


def app_config(acc, name):
    row = acc.inspect("container", name)
    env = {item.partition("=")[0]: item.partition("=")[2] for item in (row.get("Config") or {}).get("Env") or []}
    state = row.get("State") or {}
    return {"running": state.get("Running") is True, "oom_killed": state.get("OOMKilled"),
            "restart_count": row.get("RestartCount"), "command": (row.get("Config") or {}).get("Cmd") or [],
            "server_port_env": env.get("LD_SERVER_PORT"),
            "memory_bytes": (row.get("HostConfig") or {}).get("Memory"),
            "started_at": state.get("StartedAt"),
            "container_id": row["Id"], "image_id": row["Image"]}


def provision_linkding(acc, port):
    started = time.monotonic()
    acc.dir.mkdir(parents=True, exist_ok=False)
    names = acc.names("linkding")
    for key, extra in (("internal", ["--internal"]), ("ingress", [])):
        nid = acc.docker("network", "create", "--label", f"{LABEL}={acc.run_id}",
                         *extra, names[key], fixture=True)
        acc.created.append(("network", nid))
    acc.docker("volume", "create", "--label", f"{LABEL}={acc.run_id}", names["volume"], fixture=True)
    acc.created.append(("volume", names["volume"]))
    copy_report = acc.copy_volume("aura-ops-lab_data", names["volume"], LINKDING_IMAGE)
    compose_file = acc.dir / "linkding-compose.json"
    compose_file.write_text(json.dumps(linkding_compose(acc.dir, names, port), indent=2), encoding="utf-8")
    (acc.dir / "fault-port.json").write_text(json.dumps(fault_port_override(), indent=2), encoding="utf-8")
    (acc.dir / "fault-mem64.json").write_text(json.dumps(fault_mem64_override(), indent=2), encoding="utf-8")
    acc.compose_containers.update((names["app"], names["gateway"]))
    acc.docker("compose", "--project-name", names["project"], "--file", str(compose_file),
               "up", "--detach", "--no-build", fixture=True, timeout=120)
    for service in ("app", "gateway"):
        row = acc.inspect("container", names[service])
        if not owner_ok("container", row, acc.run_id):
            raise RunnerError("Provisioned container ownership mismatch")
        acc.created.append(("container", row["Id"]))
    return {"names": names, "compose_file": str(compose_file), "port": port,
            "copy": copy_report, "elapsed_seconds": round(time.monotonic() - started, 1)}


def probe_summary(observation):
    return {"healthy": observation.get("healthy"),
            "probes": [{"url": item.get("url"), "ok": item.get("ok"),
                        "status_code": item.get("status_code"), "error": item.get("error")}
                       for item in observation.get("probes", [])]}


async def wait_healthy(target, timeout=120):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            last = await target.collect()
        except OpsError as exc:
            last = {"error": exc.code}
            await asyncio.sleep(2)
            continue
        if last.get("healthy") is True:
            return last
        await asyncio.sleep(2)
    raise RunnerError(f"Sandbox baseline unhealthy: {json.dumps(probe_summary(last or {}))}")


async def restart_case(acc, target, names, case, fault_file, expect_config):
    """One fault, one typed restart, 30s remeasure. Pass = protocol completed."""
    started = time.monotonic()
    actual = {"restarts_invoked": 0}
    acc.docker("compose", "--project-name", names["project"], "--file",
               str(acc.dir / "linkding-compose.json"), "--file", str(acc.dir / fault_file),
               "up", "--detach", "--no-deps", "--no-build", "--pull", "never",
               "--force-recreate", "linkding", timeout=120, fixture=True)
    await asyncio.sleep(3)
    actual["fault_config"] = app_config(acc, names["app"])
    for key, value in expect_config.items():
        if actual["fault_config"].get(key) != value:
            raise RunnerError(f"Fault did not apply: {key}={actual['fault_config'].get(key)!r}")
    try:
        failed = await target.collect()
    except OpsError as exc:
        failed = {"error": exc.code}
    actual["failed_evidence"] = probe_summary(failed) if "error" not in failed else failed
    identity = failed.get("identity") if isinstance(failed, dict) else None
    if not identity:
        raise RunnerError(f"No identity available for restart: {actual['failed_evidence']}")
    row = acc.inspect("container", names["app"])
    if not owner_ok("container", row, acc.run_id) or row["Id"] != (identity or {}).get("container_id"):
        raise RunnerError("Restart identity is not the owned sandbox container")
    restart_started = time.monotonic()
    result = await target.restart(identity)
    actual["restarts_invoked"] = 1
    actual["restart_elapsed_seconds"] = round(time.monotonic() - restart_started, 2)
    actual["restart_identity"] = result["identity"]
    if result["identity"]["container_id"] != identity["container_id"]:
        raise RunnerError("Restart changed the owned container identity")
    samples = []
    deadline = time.monotonic() + REMEASURE_SECONDS
    while time.monotonic() < deadline:
        try:
            observation = await target.collect()
            samples.append({"dt": round(time.monotonic() - restart_started, 1),
                            "healthy": observation.get("healthy"),
                            "probes_ok": [bool(item.get("ok")) for item in observation.get("probes", [])],
                            "server_port": (observation.get("config") or {}).get("server_port"),
                            "memory_sampled_bytes": (observation.get("resources") or {}).get("memory_bytes"),
                            "memory_limit_bytes": (observation.get("resources") or {}).get("memory_limit_bytes")})
        except OpsError as exc:
            samples.append({"dt": round(time.monotonic() - restart_started, 1), "error": exc.code})
        await asyncio.sleep(2)
    actual["remeasure"] = samples
    actual["availability_returned"] = any(s.get("healthy") is True for s in samples)
    actual["final_config"] = app_config(acc, names["app"])
    actual["cause_persists"] = all(actual["final_config"].get(key) == value for key, value in expect_config.items())
    actual["elapsed_seconds"] = round(time.monotonic() - started, 1)
    return actual


async def restore_baseline(acc, target, names):
    acc.docker("compose", "--project-name", names["project"], "--file",
               str(acc.dir / "linkding-compose.json"), "up", "--detach", "--no-deps",
               "--no-build", "--pull", "never", "--force-recreate", "linkding",
               timeout=120, fixture=True)
    observation = await wait_healthy(target)
    config = app_config(acc, names["app"])
    if config["server_port_env"] != "9090" or config["memory_bytes"] != 256 * 1024 * 1024:
        raise RunnerError("Sandbox baseline was not restored")
    return {"healthy": True, "config": config, "probes": probe_summary(observation)}


async def run_benchmark(acc, result, port):
    cases = []
    prov = provision_linkding(acc, port)
    result["benchmark"] = {"provision": prov, "cases": cases}
    load_probe_credentials(ROOT)
    spec = TargetSpec(
        target_id=f"ld-{acc.run_id}", name="Soak Linkding",
        container_name=prov["names"]["app"], docker_context=CONTEXT,
        probes=[{"url": f"http://127.0.0.1:{port}/health"},
                {"url": f"http://127.0.0.1:{port}/api/bookmarks/",
                 "credential_env": "AURA_LAB_API_TOKEN"}])
    target = DockerTarget(spec)
    baseline = await wait_healthy(target)
    base_config = app_config(acc, prov["names"]["app"])
    result["benchmark"]["baseline"] = {"probes": probe_summary(baseline), "config": base_config}
    for case_id, name, fault_file, expect in (
            ("A1", "wrong-port-restart", "fault-port.json",
             {"server_port_env": "9091"}),
            ("A2", "reduced-memory-restart", "fault-mem64.json",
             {"memory_bytes": 64 * 1024 * 1024})):
        try:
            actual = await restart_case(acc, target, prov["names"], name, fault_file, expect)
            restored = await restore_baseline(acc, target, prov["names"])
            cases.append({"case": case_id, "name": name, "passed": True,
                          "expected": "One restart measured against faulty config; protocol completed.",
                          "actual": actual, "restored": restored})
        except (RunnerError, OpsError) as exc:
            cases.append({"case": case_id, "name": name, "passed": False,
                          "expected": "One restart measured against faulty config; protocol completed.",
                          "actual": {}, "error": f"{type(exc).__name__}:{getattr(exc, 'code', exc)}"})
            try:
                await restore_baseline(acc, target, prov["names"])
            except (RunnerError, OpsError):
                pass
    try:
        prior = json.loads((ROOT / ".local/ops-acceptance/acc-65f068e16495/result.json").read_text(encoding="utf-8"))
        result["benchmark"]["prior_aura_run"] = {
            "note": "Different prior run, fixtures and time; NOT a controlled AURA-vs-restart comparison. No timing claim is made.",
            "run_id": "acc-65f068e16495", "model_mode": prior.get("model_mode"),
            "matrix_seconds": prior.get("matrix_seconds"),
            "cases": [{"case": c.get("case"), "name": c.get("name"),
                       "duration_seconds": c.get("duration_seconds")} for c in prior.get("cases", [])]}
    except OSError as exc:
        result["benchmark"]["prior_aura_run"] = {"note": "Prior run evidence unavailable.", "error": str(exc)[:200]}
    return result["benchmark"]


def verify_backend_procs():
    """Verify ONLY backend-owned Python processes: recorded controller PID plus
    descendants running the backend serve command, with port 8000 among them."""
    record = json.loads((ROOT / ".local/ops/evidence/live-controller.json").read_text(encoding="utf-8"))
    recorded = record.get("pid")
    try:
        root = psutil.Process(recorded)
        alive = root.is_running()
    except (psutil.Error, TypeError, ValueError):
        alive = False
    verified = []
    if alive:
        candidates = [root] + root.children(recursive=True)
        for proc in candidates:
            try:
                exe = proc.exe()
                cmdline = " ".join(proc.cmdline())
            except psutil.Error:
                continue
            if "python" not in exe.lower().replace("\\", "/").split("/")[-1]:
                continue
            if "aura_runtime.py" not in cmdline or "serve" not in cmdline:
                continue
            verified.append(proc)
    listeners = set()
    for proc in verified:
        try:
            listeners.update(c.laddr.port for c in proc.net_connections(kind="tcp")
                             if c.status == psutil.CONN_LISTEN)
        except psutil.Error:
            continue
    detail = {"recorded_pid": recorded, "recorded_alive": alive,
              "verified_pids": sorted(p.pid for p in verified),
              "executables": sorted({p.exe() for p in verified}),
              "port_8000_listened": 8000 in listeners}
    if not verified or 8000 not in listeners:
        raise RunnerError(f"Backend process ownership unverifiable: {detail}")
    return [p.pid for p in verified], detail


def sample_procs(pids):
    per_pid, missing = [], []
    for pid in pids:
        try:
            proc = psutil.Process(pid)
            with proc.oneshot():
                per_pid.append({"pid": pid, "rss_bytes": proc.memory_info().rss,
                                "handles": proc.num_handles(), "threads": proc.num_threads()})
        except psutil.Error:
            missing.append(pid)
    return {"per_pid": per_pid, "missing_pids": missing,
            "total_rss_bytes": sum(p["rss_bytes"] for p in per_pid) if per_pid else None,
            "full": not missing and bool(per_pid)}


def sample_sqlite():
    sizes = {}
    for name in ("ops.sqlite3", "ops.sqlite3-wal", "ops.sqlite3-shm"):
        try:
            sizes[name] = (ROOT / ".local/ops" / name).stat().st_size
        except OSError:
            sizes[name] = None
    return sizes


def trim_status(payload):
    targets = {}
    for row in payload.get("targets", []):
        obs = row.get("observation") or {}
        identity = obs.get("identity") or {}
        resources = obs.get("resources") or {}
        targets[row.get("target_id")] = {
            "healthy": obs.get("healthy"), "monitoring": row.get("monitoring"),
            "policy": row.get("policy"), "observation_timestamp": obs.get("timestamp"),
            "container_id": identity.get("container_id"), "image_id": identity.get("image_id"),
            "started_at": identity.get("started_at"),
            "memory_bytes": resources.get("memory_bytes"),
            "memory_limit_bytes": resources.get("memory_limit_bytes"),
            "cpu_percent": resources.get("cpu_percent"),
            "probes": [{"url": p.get("url"), "ok": p.get("ok"),
                        "status_code": p.get("status_code")} for p in obs.get("probes", [])],
            "collection_errors": obs.get("collection_errors")}
    return {"seq": payload.get("seq"), "targets": targets,
            "investigations": [{"id": i.get("investigation_id"), "phase": i.get("phase"),
                                "calls": len(i.get("calls") or [])} for i in payload.get("investigations", [])],
            "proposals": [{"id": p.get("proposal_id"), "status": p.get("status"),
                           "dispatched": p.get("dispatch_attempted_at")} for p in payload.get("proposals", [])],
            "active_id": payload.get("active_id")}


class Sampler:
    def __init__(self, acc, pids, baseline_ops):
        self.acc, self.pids = acc, pids
        self.baseline_ops = baseline_ops
        self.client = httpx.AsyncClient(timeout=HTTP_TIMEOUT)
        self.interference = []
        self.overruns = 0
        self._last_calls = baseline_ops["calls_total"]

    async def close(self):
        await self.client.aclose()

    async def get(self, url):
        started = time.monotonic()
        try:
            response = await self.client.get(url)
            return {"http": response.status_code, "duration_ms": round((time.monotonic() - started) * 1000, 1),
                    "body": response.json() if response.headers.get("content-type", "").startswith("application/json") else None}
        except (httpx.HTTPError, ValueError) as exc:
            return {"http": None, "duration_ms": round((time.monotonic() - started) * 1000, 1),
                    "error": f"{type(exc).__name__}: {str(exc)[:160]}"}

    async def sample(self, tick, offset):
        health = await self.get(BACKEND_HEALTH)
        status = await self.get(STATUS_URL)
        trimmed = None
        if status.get("http") == 200 and isinstance(status.get("body"), dict):
            trimmed = trim_status(status["body"])
            self.check_interference(offset, trimmed)
        procs = sample_procs(self.pids)
        if procs["missing_pids"]:
            self.interference.append({"offset": offset, "kind": "backend_pid_missing",
                                      "detail": procs["missing_pids"]})
        return {"t": utc_now(), "tick": tick, "offset": round(offset, 2),
                "health": {k: v for k, v in health.items() if k != "body"},
                "health_ok": health.get("http") == 200,
                "status_http": status.get("http"),
                "status_duration_ms": status.get("duration_ms"),
                "status_error": status.get("error"),
                "status": trimmed, "procs": procs, "sqlite": sample_sqlite()}

    def check_interference(self, offset, trimmed):
        current_inv = {i["id"]: i["phase"] for i in trimmed["investigations"]}
        current_prop = {p["id"]: (p["status"], p["dispatched"]) for p in trimmed["proposals"]}
        base_inv, base_prop = self.baseline_ops["investigations"], self.baseline_ops["proposals"]
        for iid in sorted(set(current_inv) - set(base_inv)):
            self.interference.append({"offset": offset, "kind": "new_investigation", "detail": iid})
        for pid_ in sorted(set(current_prop) - set(base_prop)):
            self.interference.append({"offset": offset, "kind": "new_proposal", "detail": pid_})
        for pid_, state in sorted(current_prop.items()):
            if pid_ in base_prop and state != base_prop[pid_]:
                self.interference.append({"offset": offset, "kind": "proposal_transition",
                                          "detail": {"id": pid_, "state": state}})
        for target_id, policy in sorted((tid, t.get("policy")) for tid, t in trimmed["targets"].items()):
            if policy != (self.baseline_ops["policies"].get(target_id)):
                self.interference.append({"offset": offset, "kind": "policy_change", "detail": target_id})
        current_calls = sum(i["calls"] for i in trimmed["investigations"])
        if current_calls != self._last_calls:
            self.interference.append({"offset": offset, "kind": "new_provider_calls",
                                      "detail": {"before": self._last_calls, "after": current_calls}})
            self._last_calls = current_calls

    async def run_window(self, path, duration, start_tick=0):
        """Tick loop with no overlap backlog: missed ticks are recorded, never caught up."""
        samples, tick, start = [], start_tick, time.monotonic()
        with open(path, "w", encoding="utf-8") as handle:
            while True:
                target = start + (tick - start_tick) * TICK_SECONDS
                now = time.monotonic()
                if now - start >= duration:
                    break
                if now > target + TICK_SECONDS:
                    skipped = int((now - target) // TICK_SECONDS)
                    for _ in range(skipped):
                        samples.append({"t": utc_now(), "tick": tick, "offset": round(target - start, 2),
                                        "skipped": True, "reason": "overrun"})
                        handle.write(json.dumps(samples[-1]) + "\n")
                        tick += 1
                        target += TICK_SECONDS
                    self.overruns += skipped
                    continue
                if now < target:
                    await asyncio.sleep(target - now)
                try:
                    sample = await self.sample(tick, target - start)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    sample = {"t": utc_now(), "tick": tick, "offset": round(target - start, 2),
                              "skipped": True, "reason": f"sampler:{type(exc).__name__}: {str(exc)[:160]}"}
                samples.append(sample)
                handle.write(json.dumps(sample) + "\n")
                handle.flush()
                tick += 1
        return samples


class WsWatch:
    def __init__(self, path):
        self.path = path
        self.tracker = WsTracker()
        self.connections = []
        self.handle = None

    def record(self, record):
        self.handle.write(json.dumps(record) + "\n")
        self.handle.flush()

    async def one_connection(self, index, until_offset, window_start):
        conn = {"index": index, "connected_at": utc_now(), "snapshot_seq": None,
                "events": 0, "clean_close": False, "investigations": None}
        self.connections.append(conn)
        self.tracker.connect()
        ws = await ws_connect(WS_URL, max_size=16 * 1024 * 1024, open_timeout=10)
        try:
            try:
                raw = await asyncio.wait_for(ws.recv(), 15)
            except (asyncio.TimeoutError, TimeoutError):
                raise RunnerError("WS snapshot not received after connect")
            message = json.loads(raw)
            if message.get("type") != "snapshot":
                raise RunnerError(f"First WS message is not a snapshot: {message.get('type')}")
            seq = message["seq"]
            self.tracker.snapshot(index, seq)
            payload = message.get("payload") or {}
            conn["snapshot_seq"] = seq
            conn["snapshot_hash"] = hashlib.sha256(raw.encode("utf-8")).hexdigest()
            conn["investigations"] = sorted(i.get("investigation_id") for i in payload.get("investigations", []))
            conn["proposals"] = sorted(p.get("proposal_id") for p in payload.get("proposals", []))
            self.record({"t": utc_now(), "connection": index, "type": "snapshot", "seq": seq,
                         "payload_seq": payload.get("seq"), "investigations": conn["investigations"],
                         "proposals": conn["proposals"]})
            while time.monotonic() - window_start < until_offset:
                try:
                    raw = await asyncio.wait_for(ws.recv(), 30)
                except (asyncio.TimeoutError, TimeoutError):
                    continue
                event = json.loads(raw)
                self.tracker.event(index, event["seq"])
                conn["events"] += 1
                self.record({"t": utc_now(), "connection": index, "type": "event",
                             "seq": event["seq"], "kind": event.get("type"),
                             "target_id": event.get("target_id"),
                             "investigation_id": event.get("investigation_id")})
            await ws.close()
            conn["clean_close"] = True
        finally:
            try:
                await ws.close()
            except Exception:
                pass
        return conn

    async def run(self, window_start):
        self.handle = open(self.path, "w", encoding="utf-8")
        try:
            points = [0.0, *RECONNECT_AT, WINDOW_SECONDS]
            for index in range(len(points) - 1):
                await self.one_connection(index, points[index + 1], window_start)
        finally:
            self.handle.close()
            self.handle = None


async def fetch_status(client):
    response = await client.get(STATUS_URL)
    response.raise_for_status()
    return response.json()


async def precheck_soak(acc):
    """Read-only preconditions for the observation window. Never mutates."""
    backend = acc.backend_health()
    if backend != 200:
        raise RunnerError(f"Backend /health is not 200 (got {backend})")
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        payload = await fetch_status(client)
    trimmed = trim_status(payload)
    for target_id, row in trimmed["targets"].items():
        if row.get("healthy") is not True or row.get("monitoring") is not True:
            raise RunnerError(f"Target {target_id} is not healthy/monitored")
        policy = row.get("policy") or {}
        if policy.get("automatic_actions") or policy.get("auto_investigate"):
            raise RunnerError(f"Target {target_id} has automation enabled")
    active = [i for i in trimmed["investigations"] if i["phase"] in ACTIVE]
    if active:
        raise RunnerError(f"Active investigations present: {[i['id'] for i in active]}")
    intervening = [p for p in trimmed["proposals"] if p["status"] in ACTIVE_PROPOSALS]
    if intervening:
        raise RunnerError(f"Active interventions present: {[p['id'] for p in intervening]}")
    pids, proc_detail = verify_backend_procs()
    registered = acc.registered_fingerprints()
    return {"backend": backend, "targets": sorted(trimmed["targets"]),
            "investigations": len(trimmed["investigations"]),
            "proposals": len(trimmed["proposals"]),
            "pending_approval": [p["id"] for p in trimmed["proposals"] if p["status"] == "proposed"],
            "procs": proc_detail, "registered": registered,
            "baseline_ops": {
                "investigations": {i["id"]: i["phase"] for i in trimmed["investigations"]},
                "proposals": {p["id"]: (p["status"], p["dispatched"]) for p in trimmed["proposals"]},
                "policies": {tid: row.get("policy") for tid, row in trimmed["targets"].items()},
                "calls_total": sum(i["calls"] for i in trimmed["investigations"])}}, pids


def evaluate_gates(samples, ws_watch, interference, postcheck, ws_error=None, expected_targets=None):
    valid = [s for s in samples if not s.get("skipped") and s.get("status")]
    offsets = [s["offset"] for s in samples if not s.get("skipped")]
    span = (max(offsets) - min(offsets)) if len(offsets) > 1 else 0.0
    gates = {}
    gates["duration"] = {"status": "pass" if span >= GATES["duration"]["min_sample_span_seconds"] else "fail",
                         "span_seconds": round(span, 1)}
    coverage = coverage_gate(len(valid))
    gates["coverage"] = {"status": "pass" if coverage["passed"] else "fail", **coverage}
    gaps = gap_gate(offsets)
    gates["gaps"] = {"status": "pass" if gaps["passed"] else "fail", **gaps}
    target_ids = set(expected_targets) if expected_targets is not None else (
        set(valid[0]['status']['targets']) if valid else set())
    unhealthy = [s["tick"] for s in valid
                 if not s.get("health_ok") or not target_ids
                 or set(s['status']['targets']) != target_ids
                 or any(not t.get("healthy") for t in s["status"]["targets"].values())]
    identities = [{"target_id": tid, "container_id": t.get("container_id"),
                   "image_id": t.get("image_id"), "started_at": t.get("started_at")}
                  for s in valid for tid, t in sorted(s["status"]["targets"].items())]
    changes = identity_changes(identities)
    blocking = [e for e in interference if e["kind"] in (
        "new_investigation", "new_proposal", "proposal_transition", "new_provider_calls",
        "policy_change", "backend_pid_missing")]
    if blocking:
        gates["health"] = {"status": "blocked", "interference": blocking}
    elif unhealthy or changes:
        gates["health"] = {"status": "fail", "unhealthy_ticks": unhealthy, "identity_changes": changes}
    else:
        gates["health"] = {"status": "pass", "valid_samples": len(valid), "identity_changes": []}
    verdict = ws_watch.tracker.verdict()
    stale_payload = [c["index"] for c in ws_watch.connections
                     if c.get("snapshot_seq") is None]
    inv_sets = [tuple(c.get("investigations") or []) for c in ws_watch.connections]
    prop_sets = [tuple(c.get("proposals") or []) for c in ws_watch.connections]
    reconnect_clean = all(c.get("clean_close") for c in ws_watch.connections) \
        and len(ws_watch.connections) == len(RECONNECT_AT) + 1
    if ws_error:
        gates["ws"] = {"status": "fail", "error": ws_error,
                       **{k: v for k, v in verdict.items() if k != "passed"}}
    elif verdict["passed"] and reconnect_clean and not stale_payload \
            and len(set(inv_sets)) <= 1 and len(set(prop_sets)) <= 1:
        gates["ws"] = {"status": "pass", **{k: v for k, v in verdict.items() if k != "passed"},
                       "reconnects": len(ws_watch.connections) - 1}
    else:
        gates["ws"] = {"status": "fail", **{k: v for k, v in verdict.items() if k != "passed"},
                       "clean_closes": [c.get("clean_close") for c in ws_watch.connections],
                       "snapshot_owners": stale_payload,
                       "investigation_sets": [list(s) for s in inv_sets],
                       "proposal_sets": [list(s) for s in prop_sets]}
    rss_samples = [{"offset": s["offset"], "rss": (s.get("procs") or {}).get("total_rss_bytes"),
                    "handles": sum(p.get("handles", 0) for p in (s.get("procs") or {}).get("per_pid", [])),
                    "full": (s.get("procs") or {}).get("full") is True}
                   for s in valid]
    gates["rss"] = rss_gate(rss_samples)
    gates["close"] = {"status": "pass" if postcheck.get("clean") and postcheck.get("healthy") else "fail",
                      **postcheck}
    return gates


async def run_soak_inner(acc, result):
    section = {"gates": GATES, "started_at": utc_now()}
    result["soak"] = section
    precheck, pids = await precheck_soak(acc)
    section["precheck"] = {k: v for k, v in precheck.items() if k != "baseline_ops"}
    sampler = Sampler(acc, pids, precheck["baseline_ops"])
    ws_watch = WsWatch(str(acc.dir / "ws.jsonl"))
    section["reconnect_at"] = list(RECONNECT_AT)
    print(json.dumps({"phase": "warmup", "seconds": WARMUP_SECONDS}), flush=True)
    warmup = await sampler.run_window(str(acc.dir / "warmup.jsonl"), WARMUP_SECONDS)
    section["warmup"] = {"samples": len([s for s in warmup if not s.get("skipped")]),
                         "skipped": len([s for s in warmup if s.get("skipped")])}
    window_start = time.monotonic()
    print(json.dumps({"phase": "window", "seconds": WINDOW_SECONDS}), flush=True)
    sampler_task = asyncio.create_task(sampler.run_window(str(acc.dir / "samples.jsonl"), WINDOW_SECONDS))
    ws_task = asyncio.create_task(ws_watch.run(window_start))
    ws_error = None
    try:
        progress = 0
        while not sampler_task.done():
            try:
                await asyncio.wait_for(asyncio.shield(sampler_task), timeout=60)
            except (asyncio.TimeoutError, TimeoutError):
                progress += 60
                print(json.dumps({"phase": "window", "elapsed_seconds": progress,
                                  "interference": len(sampler.interference)}), flush=True)
        samples = sampler_task.result()
        try:
            await ws_task
        except Exception as exc:
            ws_error = f"{type(exc).__name__}: {str(exc)[:300]}"
    finally:
        for task in (sampler_task, ws_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(sampler_task, ws_task, return_exceptions=True)
        try:
            await sampler.close()
        except asyncio.CancelledError:
            pass
    section["interference"] = sampler.interference
    section["overruns"] = sampler.overruns
    section["ws"] = ws_watch.connections
    if ws_error:
        section["ws_error"] = ws_error
    postcheck = {"clean": True, "healthy": False}
    try:
        final = acc.backend_health()
        acc.registered_fingerprints()
        postcheck["backend"] = final
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            final_status = trim_status(await fetch_status(client))
        postcheck["registered_running"] = (set(final_status['targets']) == set(precheck['targets'])
            and all(t.get('healthy') is True for t in final_status['targets'].values()))
        postcheck["healthy"] = final == 200 and postcheck['registered_running']
    except Exception as exc:
        postcheck["clean"] = False
        postcheck["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
    section["gates_result"] = evaluate_gates(samples, ws_watch, sampler.interference, postcheck, ws_error,
        expected_targets=precheck['targets'])
    section["samples"] = {"valid": len([s for s in samples if not s.get("skipped")]),
                          "skipped": len([s for s in samples if s.get("skipped")]),
                          "files": ["samples.jsonl", "warmup.jsonl", "ws.jsonl"]}
    return section


async def run_soak(acc, result):
    try:
        await asyncio.wait_for(run_soak_inner(acc, result), timeout=SECTION_B_BUDGET)
    except (asyncio.TimeoutError, TimeoutError):
        section = result.setdefault("soak", {"gates": GATES})
        section["blocked"] = "section exceeded its 24-minute hard deadline"
        section["samples"] = {"files": ["samples.jsonl", "warmup.jsonl", "ws.jsonl"], "partial": True}
        raise RunnerError("Soak section exceeded its 24-minute hard deadline")


async def run_all(acc, result, port):
    await run_benchmark(acc, result, port)
    acc.write_result(result)
    leaks = acc.cleanup()
    result["benchmark_cleanup"] = {"records": list(acc.cleanup_records), "leaks": leaks}
    result["benchmark_registered_after"] = acc.registered_fingerprints()
    acc.cleanup_records.clear()
    acc.created.clear()
    acc.compose_containers.clear()
    acc.write_result(result)
    if leaks:
        raise RunnerError(f"Benchmark leaked owned resources: {leaks}")
    print(json.dumps({"phase": "soak", "note": "benchmark cleaned; starting observation"}), flush=True)
    await run_soak(acc, result)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true",
                        help="Run disposable benchmark and 20-minute observation once.")
    parser.add_argument("--run-id", default="soak-" + uuid.uuid4().hex[:12])
    args = parser.parse_args(argv)
    os.environ["PATH"] = str(ROOT / ".local/environment/Docker/resources/bin") + os.pathsep + os.environ.get("PATH", "")
    acc = SoakAcceptance(args.run_id)
    result = {"run_id": args.run_id, "model_mode": "deterministic_mock", "real_docker": True,
              "provider_requests": 0, "gates": GATES, "passed": False}
    try:
        preflight, ports, _ = acc.preflight()
    except RunnerError as exc:
        print(json.dumps({"preflight": "blocked", "error": str(exc)}))
        return 2
    result["preflight"] = preflight
    print(json.dumps({"preflight": "ok", "run_id": args.run_id, "ports": ports}), flush=True)
    if not args.run:
        print("Offline mode: preflight only, no fixtures created. Pass --run for benchmark and soak.")
        return 0
    try:
        asyncio.run(run_all(acc, result, ports[0]))
    except (RunnerError, OpsError) as exc:
        result["harness_error"] = f"{type(exc).__name__}:{getattr(exc, 'code', exc)}"
    except (asyncio.CancelledError, KeyboardInterrupt):
        result["harness_error"] = "cancelled: partial evidence preserved; full repeat required"
    except Exception as exc:
        result["harness_error"] = f"runner:{type(exc).__name__}: {exc}"
    finally:
        result["mutation_trace"] = {"fixture_commands": acc.fixture_commands}
        try:
            result["leaks"] = acc.cleanup()
        except Exception as exc:
            result["cleanup_error"] = f"{type(exc).__name__}: {exc}"
            result["leaks"] = result.get("leaks", [])
        result["cleanup_records"] = acc.cleanup_records
        try:
            result["registered_after"] = acc.registered_fingerprints()
            result["backend_after"] = acc.backend_health()
        except Exception as exc:
            result["after_check_error"] = f"{type(exc).__name__}: {exc}"
        benchmark_ok = all(c.get("passed") for c in result.get("benchmark", {}).get("cases", [])) \
            and bool(result.get("benchmark", {}).get("cases"))
        soak_gates = result.get("soak", {}).get("gates_result", {})
        soak_ok = bool(soak_gates) and all(g.get("status") == "pass" for g in soak_gates.values())
        cleanup_ok = not result.get('cleanup_error') and not result["leaks"] and all(i.get("removed") for i in acc.cleanup_records)
        registered_ok = result.get("registered_after") == acc.registered_before
        backend_ok = result.get("backend_after") == 200 == result["preflight"]["backend"]
        result["passed"] = benchmark_ok and soak_ok and cleanup_ok and registered_ok and backend_ok \
            and "harness_error" not in result
        path = acc.write_result(result)
        print(json.dumps({"passed": result["passed"], "evidence": path}), flush=True)
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

