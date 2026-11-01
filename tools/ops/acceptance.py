"""Bounded real-Docker acceptance with deterministic offline diagnostics.

Default invocation runs read-only preflight only. Pass --run-real-docker to
provision disposable sandbox fixtures and run the lifecycle matrix once. Real
Docker/HTTP and actual OpsService/ActionExecutor/DockerTarget/OpsStore are used;
only model completion bytes are canned (model_mode=deterministic_mock) through
the real OpsInvestigator validation. No provider calls, downloads, frontend
changes, or mutations to registered fixtures, the live store, or the backend.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "backend")]

from aura_agents.adapter import CallResult
from aura_ops.docker import DockerTarget
from aura_ops.investigator import OpsInvestigator
from aura_ops.models import ActionKind, OpsError, TargetSpec
from aura_ops.service import OpsService
from aura_ops.store import OpsStore
from tools.ops.check_candidate import COPY
from tools.ops.lab import DEPLOYMENT_LAUNCH

CONTEXT = "desktop-linux"
DOCKER = str(ROOT / ".local" / "environment" / "Docker" / "resources" / "bin" / "docker.exe")
BACKEND_HEALTH = "http://127.0.0.1:8000/health"
PORTS = (18093, 18094, 18095, 18096)
LABEL = "org.aura.accept"
LINKDING_IMAGE = "sha256:9c1d5a16184bce77a6bedc2d11b54ccb25f21106483e6b6c5bd670b2613c6178"
LINKDING_REVISION = "f279015767b734c8f9a17c402adaa68bcd9f8d90"
LINKDING_SOURCE = str(ROOT / ".local" / "ops-lab" / "source")
MATRIX_BUDGET = 8 * 60
PROVISION_BUDGET = 10 * 60
MODEL_MODE = "deterministic_mock"

# COPY emits pre-validation fingerprints, then checks SQLite including its WAL.
# Comparing the post-checkpoint destination bytes would reject valid clones.
FUSED = COPY


def backend_health_url():
    """Explicit health-URL injection for one acceptance invocation.

    OPS-FINAL-ACCEPT-001 may point a single run at a temporary localhost
    backend via AURA_ACCEPT_BACKEND_HEALTH. The default URL is unchanged and
    all gates (HTTP 200 required before and after) are preserved.
    """
    override = os.environ.get("AURA_ACCEPT_BACKEND_HEALTH", "").strip()
    return override or BACKEND_HEALTH


class RunnerError(Exception):
    """Local runner defect; never a product verdict."""


class CaseFail(Exception):
    def __init__(self, actual, error):
        super().__init__(error)
        self.actual, self.error = actual, error


def execute(argv, timeout=30, input_text=None):
    try:
        completed = subprocess.run(argv, input=input_text.encode("utf-8") if input_text else None,
                                   capture_output=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise RunnerError(f"Missing executable: {argv[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise RunnerError(f"Command exceeded {timeout}s: {argv[3] if len(argv) > 3 else argv[0]}") from exc
    out, err = completed.stdout.decode("utf-8", "replace"), completed.stderr.decode("utf-8", "replace")
    if len(out) > 2 * 1024 * 1024 or len(err) > 256 * 1024:
        raise RunnerError("Command output exceeded its bound")
    return completed.returncode, out, err


class DeterministicAdapter:
    """Canned completion bytes through the real investigator validation.

    The decision is scripted per scenario; supporting facts embedded in the
    canned text are measured values from the evidence packet. Labelled
    deterministic_mock in every call record. No network use.
    """

    ready = True

    def __init__(self, mode="abstain"):
        if mode not in ("abstain", "restore", "restart"):
            raise RunnerError(f"Unknown deterministic mode: {mode}")
        self.mode = mode
        self.calls = []
        self.closed = False

    async def complete(self, messages, **options):
        packet = json.loads(messages[1]["content"].removeprefix("EVIDENCE DATA:\n"))
        current = packet.get("observation.current", {})
        measured = "; ".join(
            f"{item.get('url')} -> {item.get('status_code')} ok={item.get('ok')}"
            for item in current.get("probes", [])) or "no probes measured"
        identity = current.get("identity", {})
        started = identity.get("started_at", "unknown-start")
        synthesis = "Synthesize the evidence" in messages[0]["content"]
        if not synthesis:
            result = {"summary": f"Deterministic role note; measured: {measured}.",
                      "findings": [{"hypothesis": "Recorded observation reflects measured sandbox state.",
                                    "evidence_refs": ["observation.current"], "confidence": "supported"}],
                      "request_sources": [], "request_refresh": False}
        elif self.mode == "abstain":
            result = {"summary": "Deterministic control: no fault signature asserted.",
                      "findings": [], "action": ActionKind.ABSTAIN.value,
                      "reason": f"Deterministic control abstains; measured: {measured}.",
                      "needs_source_analysis": False}
        elif self.mode == "restore":
            result = {"summary": "Deterministic decision: restore the known-good configuration.",
                      "findings": [{"hypothesis": "Measured state differs from the captured healthy baseline.",
                                    "evidence_refs": ["observation.current", "target.baseline"],
                                    "confidence": "supported"}],
                      "action": ActionKind.RESTORE.value,
                      "reason": f"Restore enrolled config; measured: {measured}; started {started}.",
                      "needs_source_analysis": False}
        else:
            result = {"summary": "Deterministic decision: restart the enrolled container.",
                      "findings": [{"hypothesis": "Measured observation supports a reviewed restart.",
                                    "evidence_refs": ["observation.current"], "confidence": "supported"}],
                      "action": ActionKind.RESTART.value,
                      "reason": f"Restart enrolled container; measured: {measured}; started {started}.",
                      "needs_source_analysis": False}
        self.calls.append({"mode": self.mode, "synthesis": synthesis, "measured": measured})
        return CallResult(json.dumps(result), None, None, MODEL_MODE, "stop", 0.0, 1,
                          requested_model=MODEL_MODE)

    async def aclose(self):
        self.closed = True


def port_occupied(port):
    probe = socket.socket()
    probe.settimeout(1)
    try:
        return probe.connect_ex(("127.0.0.1", port)) == 0
    finally:
        probe.close()


def pick_ports(occupied, needed=2):
    free = [port for port in PORTS if port not in occupied]
    if len(free) < needed:
        raise RunnerError(f"Only {len(free)} free ports in {PORTS}; refusing occupied ports")
    return free[:needed]


def owner_ok(kind, obj, run_id):
    if kind == "container":
        labels = (obj.get("Config") or {}).get("Labels") or {}
    elif kind in ("network", "volume"):
        labels = obj.get("Labels") or {}
    else:
        raise RunnerError(f"Unknown resource kind: {kind}")
    return labels.get(LABEL) == run_id


def linkding_compose(run_dir, names, port, image=LINKDING_IMAGE):
    return {"services": {
        "linkding": {
            "container_name": names["app"], "image": image, "pull_policy": "never",
            "entrypoint": DEPLOYMENT_LAUNCH["entrypoint"], "command": DEPLOYMENT_LAUNCH["command"],
            "mem_limit": "256m", "memswap_limit": "256m", "cpus": 1.0, "pids_limit": 64,
            "restart": "no", "labels": {LABEL: names["run"]},
            "environment": {"LD_DISABLE_BACKGROUND_TASKS": "True", "LD_SERVER_PORT": "9090"},
            "volumes": ["data:/etc/linkding/data"], "networks": ["internal"],
            "logging": {"driver": "json-file", "options": {"max-size": "2m", "max-file": "2"}}},
        "gateway": {
            "container_name": names["gateway"], "image": image, "pull_policy": "never",
            "entrypoint": ["python"], "command": ["/gateway.py"],
            "ports": [f"127.0.0.1:{port}:8080"], "networks": ["ingress", "internal"],
            "volumes": [str(ROOT / "tools/ops/lab_gateway.py") + ":/gateway.py:ro"],
            "read_only": True, "cap_drop": ["ALL"], "security_opt": ["no-new-privileges:true"],
            "mem_limit": "64m", "memswap_limit": "64m", "pids_limit": 32, "cpus": 0.5,
            "restart": "no", "healthcheck": {"disable": True}, "labels": {LABEL: names["run"]},
            "logging": {"driver": "json-file", "options": {"max-size": "1m", "max-file": "1"}}}},
        "volumes": {"data": {"external": True, "name": names["volume"]}},
        "networks": {"internal": {"external": True, "name": names["internal"]},
                     "ingress": {"external": True, "name": names["ingress"]}}}


def healthchecks_compose(names, port, image, secret_env_file):
    return {"services": {
        "healthchecks": {
            "container_name": names["app"], "image": image, "pull_policy": "never",
            "mem_limit": "512m", "memswap_limit": "512m", "cpus": 1.0, "pids_limit": 96,
            "restart": "no", "labels": {LABEL: names["run"]},
            "env_file": [secret_env_file],
            "environment": {"DB": "sqlite", "DB_NAME": "/data/hc.sqlite", "DEBUG": "False",
                "SITE_ROOT": f"http://127.0.0.1:{port}", "ALLOWED_HOSTS": "127.0.0.1,localhost",
                "REGISTRATION_OPEN": "False", "EMAIL_HOST": "", "GUNICORN_WORKERS": "1",
                "GUNICORN_PORT": "8000", "DEFAULT_FROM_EMAIL": "aura@example.invalid",
                "APPRISE_ENABLED": "False", "SHELL_ENABLED": "False", "USE_PAYMENTS": "False"},
            "volumes": ["data:/data"], "networks": ["internal"],
            "logging": {"driver": "json-file", "options": {"max-size": "2m", "max-file": "2"}}},
        "gateway": {
            "container_name": names["gateway"], "image": image, "pull_policy": "never",
            "entrypoint": ["python"], "command": ["/gateway.py"],
            "ports": [f"127.0.0.1:{port}:8080"], "networks": ["ingress", "internal"],
            "volumes": [str(ROOT / "tools/ops/healthchecks_gateway.py") + ":/gateway.py:ro"],
            "read_only": True, "cap_drop": ["ALL"], "security_opt": ["no-new-privileges:true"],
            "mem_limit": "64m", "memswap_limit": "64m", "pids_limit": 32, "cpus": 0.5,
            "restart": "no", "healthcheck": {"disable": True}, "labels": {LABEL: names["run"]},
            "logging": {"driver": "json-file", "options": {"max-size": "1m", "max-file": "1"}}}},
        "volumes": {"data": {"external": True, "name": names["volume"]}},
        "networks": {"internal": {"external": True, "name": names["internal"]},
                     "ingress": {"external": True, "name": names["ingress"]}}}


def fault_port_override():
    return {"services": {"linkding": {
        "command": ["--http", "0.0.0.0:9091", "/etc/linkding/uwsgi.ini"],
        "environment": {"LD_SERVER_PORT": "9091"}}}}


def fault_mem_override():
    return {"services": {"linkding": {"mem_limit": "96m", "memswap_limit": "96m"}}}


class Acceptance:
    def __init__(self, run_id, cmd=None):
        self.run_id = run_id
        self.cmd = cmd or execute
        self.dir = ROOT / ".local" / "ops-acceptance" / run_id
        self.created = []
        self.compose_containers = set()
        self.fixture_commands = []
        self.cases = []
        self.cleanup_records = []
        self.registered_before = {}
        self.started_at = time.time()

    def docker(self, *args, timeout=30, input_text=None, fixture=False):
        argv = [DOCKER, "--context", CONTEXT, *args]
        if fixture:
            self.fixture_commands.append(argv)
        code, out, err = self.cmd(argv, timeout, input_text)
        if code:
            raise RunnerError(f"Docker {' '.join(args[:2])} failed (exit {code})")
        return out.strip()

    def inspect(self, kind, ref):
        rows = json.loads(self.docker(kind, "inspect", ref))
        if not isinstance(rows, list) or len(rows) != 1:
            raise RunnerError(f"Expected one {kind} for {ref}")
        return rows[0]

    def names(self, suffix):
        base = f"aura-accept-{self.run_id}-{suffix}"
        return {"run": self.run_id, "app": base, "gateway": base + "-gw",
                "volume": base + "-data", "internal": base + "-internal",
                "ingress": base + "-ingress", "project": f"aura-accept-{self.run_id}-{suffix}"}

    def backend_health(self):
        try:
            with urllib.request.urlopen(backend_health_url(), timeout=10) as response:
                return response.status
        except Exception:
            return None

    def registered_fingerprints(self):
        return {name: {
            "container_id": row["Id"], "image_id": row["Image"],
            "started_at": (row.get("State") or {}).get("StartedAt")}
            for name, row in (
                ("aura-ops-lab-linkding", self.inspect("container", "aura-ops-lab-linkding")),
                ("aura-ops-lab-gateway", self.inspect("container", "aura-ops-lab-gateway")),
                ("aura-healthchecks-holdout", self.inspect("container", "aura-healthchecks-holdout")),
                ("aura-healthchecks-gateway", self.inspect("container", "aura-healthchecks-gateway")))}

    def preflight(self):
        report = {"docker": None, "images": {}, "ports": {}, "registered": {},
                  "backend": None, "provenance": {}}
        if not Path(DOCKER).is_file():
            raise RunnerError("Project Docker executable is missing")
        server = json.loads(self.docker("version", "--format", "{{json .Server}}"))
        if not server or server.get("Os") != "linux":
            raise RunnerError("A Linux Docker Engine is required")
        report["docker"] = {"version": server.get("Version"), "os": server.get("Os")}
        linkding = self.inspect("image", LINKDING_IMAGE)
        if linkding.get("Id") != LINKDING_IMAGE:
            raise RunnerError("Linkding base image identity mismatch")
        pinned = json.loads((ROOT / "resources/ops/healthchecks.json").read_text(encoding="utf-8"))
        healthchecks = self.inspect("image", pinned["image"])
        report["images"] = {"linkding": linkding["Id"], "healthchecks": healthchecks["Id"],
                            "healthchecks_ref": pinned["image"]}
        occupied = {port for port in PORTS if port_occupied(port)}
        published = self.docker("ps", "--format", "{{.Ports}}")
        for port in PORTS:
            if f":{port}->" in published or f":{port}/" in published:
                occupied.add(port)
        report["ports"] = {str(port): ("occupied" if port in occupied else "free") for port in PORTS}
        ports = pick_ports(occupied)
        manifest = json.loads((ROOT / ".local/ops-lab/source-manifest.json").read_text(encoding="utf-8"))
        if manifest.get("revision") != LINKDING_REVISION:
            raise RunnerError("Linkding source revision mismatch")
        for rel in (".local/ops-lab/credentials.env", ".local/ops-healthchecks/credentials.env",
                    ".local/ops-healthchecks/.env"):
            path = ROOT / rel
            if not path.is_file():
                raise RunnerError(f"Missing fixture credential file: {rel}")
            report["provenance"][rel] = {"present": True, "bytes": path.stat().st_size}
        report["provenance"]["linkding_revision"] = manifest["revision"]
        report["provenance"]["linkding_files"] = len(manifest["files"])
        self.registered_before = self.registered_fingerprints()
        report["registered"] = self.registered_before
        report["backend"] = self.backend_health()
        if report["backend"] != 200:
            raise RunnerError(f"Backend /health is not 200 (got {report['backend']})")
        return report, ports, pinned["image"]

    def _create_container(self, name, args, timeout=30):
        cid = self.docker("container", "create", "--name", name, "--label", f"{LABEL}={self.run_id}",
                          *args, timeout=timeout, fixture=True)
        self.created.append(("container", cid))
        return cid

    def _remove_owned(self, kind, identifier):
        obj = self.inspect(kind, identifier)
        if not owner_ok(kind, obj, self.run_id):
            raise RunnerError(f"Cleanup ownership mismatch for {kind} {identifier[:12]}")
        if kind == "container":
            self.docker("container", "rm", "--force", identifier, fixture=True)
        elif kind == "network":
            self.docker("network", "rm", identifier, fixture=True)
        else:
            self.docker("volume", "rm", identifier, fixture=True)
        self.cleanup_records.append({"kind": kind, "id": identifier, "removed": True,
                                     "label_verified": True})

    def start_attached(self, cid, timeout):
        argv = [DOCKER, "--context", CONTEXT, "container", "start", "--attach", cid]
        self.fixture_commands.append(argv)
        code, out, err = self.cmd(argv, timeout, None)
        if code:
            detail = (out.strip() + "\n" + err.strip()).strip()[-2000:]
            raise RunnerError(f"Container {cid[:12]} exited {code}: {detail}")
        return out.strip()

    def _copy_once(self, source_volume, dest_volume, image):
        name = f"aura-accept-{self.run_id}-copy-{uuid.uuid4().hex[:6]}"
        cid = self._create_container(name, ["--network", "none", "--read-only", "--user", "0:0", "--cap-drop", "ALL",
            "--cap-add", "CHOWN", "--cap-add", "DAC_OVERRIDE", "--security-opt", "no-new-privileges", "--memory", "128m",
            "--memory-swap", "128m", "--pids-limit", "32",
            "--mount", f"type=volume,source={source_volume},target=/source,readonly",
            "--mount", f"type=volume,source={dest_volume},target=/dest",
            "--entrypoint", "python", image, "-c", FUSED])
        try:
            out = self.start_attached(cid, 180)
            if self.inspect("container", cid)["State"]["ExitCode"] != 0:
                raise RunnerError("Read-only data copy failed")
        finally:
            self._remove_owned("container", cid)
            self.created.remove(("container", cid))
        lines = {}
        for line in out.splitlines():
            if line.startswith("ACCEPT_BEFORE="):
                lines["before"] = json.loads(line.partition("=")[2])
            elif line.startswith("ACCEPT_AFTER="):
                lines.update(json.loads(line.partition("=")[2]))
        if set(lines) != {"before", "source", "dest"} or not lines["before"]:
            raise RunnerError("Copy evidence is incomplete")
        return lines

    def copy_volume(self, source_volume, dest_volume, image):
        last = None
        for attempt in (1, 2, 3, 4):
            try:
                lines = self._copy_once(source_volume, dest_volume, image)
            except RunnerError as exc:
                last = exc
                continue
            if lines["before"] == lines["source"] == lines["dest"]:
                return {"fingerprints": lines["dest"], "files": len(lines["dest"]), "attempts": attempt}
            last = RunnerError("SQLite/WAL fingerprints differ across copy")
        raise RunnerError(f"Stable copy failed after 4 attempts: {last}")

    def provision(self, ports, hc_image):
        started = time.monotonic()
        self.dir.mkdir(parents=True, exist_ok=False)
        linkding, hc = self.names("linkding"), self.names("hc")
        for names in (linkding, hc):
            for key, extra in (("internal", ["--internal"]), ("ingress", [])):
                nid = self.docker("network", "create", "--label", f"{LABEL}={self.run_id}",
                                  *extra, names[key], fixture=True)
                self.created.append(("network", nid))
            self.docker("volume", "create", "--label", f"{LABEL}={self.run_id}", names["volume"], fixture=True)
            self.created.append(("volume", names["volume"]))
        linkding_copy = self.copy_volume("aura-ops-lab_data", linkding["volume"], LINKDING_IMAGE)
        hc_copy = self.copy_volume("aura-healthchecks-holdout_data", hc["volume"], hc_image)
        linkding_file = self.dir / "linkding-compose.json"
        linkding_file.write_text(json.dumps(linkding_compose(self.dir, linkding, ports[0]), indent=2),
                                 encoding="utf-8")
        secret = None
        for line in (ROOT / ".local/ops-healthchecks/.env").read_text(encoding="utf-8").splitlines():
            if line.startswith("SECRET_KEY="):
                secret = line.partition("=")[2].strip()
        if not secret:
            raise RunnerError("Holdout SECRET_KEY is unavailable")
        hc_env = self.dir / "hc" / ".env"
        hc_env.parent.mkdir(exist_ok=True)
        hc_env.write_text(f"SECRET_KEY={secret}\n", encoding="utf-8")
        hc_file = self.dir / "hc-compose.json"
        hc_file.write_text(json.dumps(healthchecks_compose(hc, ports[1], hc_image, str(hc_env)), indent=2),
                           encoding="utf-8")
        (self.dir / "fault-port.json").write_text(json.dumps(fault_port_override(), indent=2), encoding="utf-8")
        (self.dir / "fault-mem.json").write_text(json.dumps(fault_mem_override(), indent=2), encoding="utf-8")
        for names, path in ((linkding, linkding_file), (hc, hc_file)):
            self.compose_containers.update((names['app'], names['gateway']))
            self.docker("compose", "--project-name", names["project"], "--file", str(path),
                        "up", "--detach", "--no-build", fixture=True, timeout=120)
            for service in ("app", "gateway"):
                row = self.inspect("container", names[service])
                if not owner_ok("container", row, self.run_id):
                    raise RunnerError("Provisioned container ownership mismatch")
                self.created.append(("container", row["Id"]))
        elapsed = time.monotonic() - started
        if elapsed > PROVISION_BUDGET:
            raise RunnerError("Provisioning exceeded its bound")
        return {"linkding": linkding, "hc": hc, "linkding_file": str(linkding_file),
                "hc_file": str(hc_file), "ports": ports, "elapsed_seconds": round(elapsed, 1),
                "linkding_copy": linkding_copy, "hc_copy": hc_copy}

    def cleanup(self):
        if self.compose_containers:
            # Compose restoration recreates containers. Discover only exact names
            # reserved by this run, then verify ownership of each current ID.
            rows = [json.loads(line) for line in self.docker(
                'container', 'ls', '--all', '--no-trunc', '--format', '{{json .}}').splitlines() if line]
            live_ids = {row['ID'] for row in rows}
            for row in rows:
                if row.get('Names') not in self.compose_containers:
                    continue
                obj = self.inspect('container', row['ID'])
                if not owner_ok('container', obj, self.run_id):
                    self.cleanup_records.append({'kind': 'container', 'id': row['ID'],
                        'removed': False, 'error': 'Expected sandbox name has foreign ownership'})
                    continue
                if ('container', row['ID']) not in self.created:
                    self.created.append(('container', row['ID']))
            for kind, identifier in list(self.created):
                if kind == 'container' and identifier not in live_ids:
                    self.created.remove((kind, identifier))
                    self.cleanup_records.append({'kind': kind, 'id': identifier,
                        'removed': True, 'already_absent': True})
        for kind in ("container", "network", "volume"):
            for resource_kind, identifier in reversed(self.created):
                if resource_kind != kind:
                    continue
                try:
                    self._remove_owned(kind, identifier)
                except Exception as exc:
                    self.cleanup_records.append({"kind": kind, "id": identifier, "removed": False,
                                                 "error": f"{type(exc).__name__}: {exc}"})
        leaks = []
        queries = (("container", ["ps", "-a", "--filter", f"label={LABEL}={self.run_id}",
                                  "--format", "{{.ID}}"]),
                   ("network", ["ls", "--filter", f"label={LABEL}={self.run_id}", "--format", "{{.ID}}"]),
                   ("volume", ["ls", "--filter", f"label={LABEL}={self.run_id}", "--format", "{{.Name}}"]))
        for kind, args in queries:
            for line in self.docker(kind, *args).splitlines():
                if line.strip():
                    leaks.append({"kind": kind, "id": line.strip()})
        return leaks

    def write_result(self, result):
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self.dir / "result.json"
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)
        return str(path)


async def wait_phase(store, service, investigation_id, phases, timeout):
    deadline = time.monotonic() + timeout
    while True:
        row = store.get_investigation(investigation_id)
        task = service._task
        if row["phase"] in phases and (task is None or task.done()):
            return row
        if time.monotonic() > deadline:
            raise CaseFail({"phase": row["phase"]}, f"Timed out waiting for {sorted(phases)}")
        await asyncio.sleep(0.5)


def trimmed(observation):
    return {key: value for key, value in observation.items() if key != "logs"}


def dispatch_records(store):
    records = []
    for proposal in store.list_proposals():
        records.append({"proposal_id": proposal.get("proposal_id"), "action": proposal.get("action"),
                        "status": proposal.get("status"), "target_id": proposal.get("target_id"),
                        "investigation_id": proposal.get("investigation_id"),
                        "dispatch_attempted_at": proposal.get("dispatch_attempted_at"),
                        "authorization": proposal.get("authorization")})
    return records


def validate_cases(cases):
    selected = list(cases)
    if not selected or selected[0] != 1 or selected != sorted(set(selected)) \
            or any(type(case) is not int or case not in range(1, 7) for case in selected):
        raise RunnerError("Cases must be sorted unique IDs from 1 to 6 and include enrollment case 1.")
    return selected


def check_cooldown_block(row, proposal, before, after, previous_cooldown, current_cooldown):
    """Accept only a proven early cooldown rejection with no second dispatch."""
    actual = {"phase": row.get("phase"), "status": proposal.get("status")}
    if row.get("phase") != "needs_human" or (row.get("error") or {}).get("code") != "restart_cooldown" \
            or proposal.get("status") != "blocked" or proposal.get("blocked_reason") != "restart_cooldown" \
            or proposal.get("action") != ActionKind.RESTART.value \
            or proposal.get("dispatch_attempted_at") or proposal.get("authorization"):
        raise CaseFail(actual, "Second restart was not blocked before dispatch by the cooldown")
    if not previous_cooldown or previous_cooldown != current_cooldown \
            or any(before.get(key) != after.get(key) for key in ("Id", "Image")) \
            or not (before.get("State") or {}).get("StartedAt") \
            or before["State"]["StartedAt"] != (after.get("State") or {}).get("StartedAt"):
        raise CaseFail(actual, "Second restart changed the container or its durable cooldown")


async def run_case(acc, result, case_id, name, expected, coro):
    started = time.monotonic()
    try:
        deadline = getattr(acc, 'matrix_deadline', None)
        if deadline is None:
            actual = await coro()
        else:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CaseFail({}, 'Matrix deadline exceeded')
            actual = await asyncio.wait_for(coro(), timeout=remaining)
        acc.cases.append({"case": case_id, "name": name, "expected": expected,
                          "actual": actual, "passed": True,
                          "duration_seconds": round(time.monotonic() - started, 1)})
    except CaseFail as exc:
        acc.cases.append({"case": case_id, "name": name, "expected": expected,
                          "actual": exc.actual, "passed": False, "error": str(exc),
                          "duration_seconds": round(time.monotonic() - started, 1)})
    except OpsError as exc:
        acc.cases.append({"case": case_id, "name": name, "expected": expected,
                          "actual": {"product_gate": exc.code}, "passed": False,
                          "blocked": True, "error": exc.message,
                          "duration_seconds": round(time.monotonic() - started, 1)})
    except Exception as exc:
        acc.cases.append({"case": case_id, "name": name, "expected": expected,
                          "actual": {}, "passed": False,
                          "error": f"runner:{type(exc).__name__}: {exc}",
                          "duration_seconds": round(time.monotonic() - started, 1)})
    result["cases"] = acc.cases
    acc.write_result(result)


class Matrix:
    def __init__(self, acc, result, prov, hc_image):
        self.acc, self.result, self.prov = acc, result, prov
        self.ports = prov["ports"]
        self.hc_image = hc_image
        self.store = OpsStore(acc.dir / "store")
        self.adapter = DeterministicAdapter()
        self.service = OpsService(ROOT, store=self.store,
                                  investigator=OpsInvestigator(self.adapter))
        self.linkding_id = f"accept-ld-{acc.run_id}"
        self.hc_id = f"accept-hc-{acc.run_id}"
        self.linkding_spec = None
        self.hc_spec = None
        self.snapshots = {}
        self.linkding_healthy = False
        self.deadline = None

    def ensure_budget(self):
        if time.monotonic() > self.deadline:
            raise CaseFail({"deadline_seconds": MATRIX_BUDGET}, "Matrix deadline exceeded")

    async def collect(self, spec):
        return await DockerTarget(spec).collect()

    async def baseline(self):
        checks_path = None
        holdout = json.loads((ROOT / ".local/ops-healthchecks/target.json").read_text(encoding="utf-8"))
        for probe in holdout["probes"]:
            if probe.get("credential_env") == "AURA_TARGET_HEALTHCHECKS_COOKIE":
                checks_path = probe["url"].split("18091", 1)[1]
        if not checks_path:
            raise RunnerError("Holdout checks path is unavailable")
        self.linkding_spec = TargetSpec(
            target_id=self.linkding_id, name="Acceptance Linkding",
            container_name=self.prov["linkding"]["app"], docker_context=CONTEXT,
            probes=[{"url": f"http://127.0.0.1:{self.ports[0]}/health"},
                    {"url": f"http://127.0.0.1:{self.ports[0]}/api/bookmarks/",
                     "credential_env": "AURA_LAB_API_TOKEN"}],
            source_dir=LINKDING_SOURCE, source_revision=LINKDING_REVISION,
            source_image_id=LINKDING_IMAGE, deployment_launch=DEPLOYMENT_LAUNCH,
            compose_file=self.prov["linkding_file"],
            compose_project=self.prov["linkding"]["project"], compose_service="linkding")
        self.hc_spec = TargetSpec(
            target_id=self.hc_id, name="Acceptance Healthchecks",
            container_name=self.prov["hc"]["app"], docker_context=CONTEXT,
            probes=[{"url": f"http://127.0.0.1:{self.ports[1]}/health"},
                    {"url": f"http://127.0.0.1:{self.ports[1]}{checks_path}",
                     "auth_scheme": "Cookie", "credential_env": "AURA_TARGET_HEALTHCHECKS_COOKIE"}])
        deadline = time.monotonic() + 120
        linkding_ok = hc_ok = False
        while time.monotonic() < deadline and not (linkding_ok and hc_ok):
            if not linkding_ok:
                try:
                    linkding_ok = (await self.collect(self.linkding_spec)).get("healthy") is True
                except OpsError:
                    linkding_ok = False
            if not hc_ok:
                try:
                    hc_ok = (await self.collect(self.hc_spec)).get("healthy") is True
                except OpsError:
                    hc_ok = False
            await asyncio.sleep(2)
        cookie_refreshed = False
        if not hc_ok:
            seed = (ROOT / "tools/ops/seed_healthchecks.py").read_text(encoding="utf-8")
            row = self.acc.inspect("container", self.prov["hc"]["app"])
            out = self.acc.docker("exec", "-i", row["Id"], "python", "/opt/healthchecks/manage.py",
                                  "shell", "-c", "exec(__import__('sys').stdin.read())",
                                  timeout=60, input_text=seed, fixture=True)
            records = [line.partition("=")[2] for line in out.splitlines() if line.startswith("AURA_HEALTHCHECKS=")]
            if len(records) != 1:
                raise RunnerError("Sandbox session seed failed")
            os.environ["AURA_TARGET_HEALTHCHECKS_COOKIE"] = json.loads(records[0])["cookie"]
            cookie_refreshed = True
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline and not hc_ok:
                try:
                    hc_ok = (await self.collect(self.hc_spec)).get("healthy") is True
                except OpsError:
                    hc_ok = False
                await asyncio.sleep(2)
        if not linkding_ok or not hc_ok:
            raise RunnerError(f"Sandbox baselines unhealthy: linkding={linkding_ok} healthchecks={hc_ok}")
        await self.service.startup()
        return {"cookie_refreshed": cookie_refreshed}

    async def case_enroll_snapshot(self):
        self.ensure_budget()
        actual = {}
        for target_id, spec in ((self.linkding_id, self.linkding_spec), (self.hc_id, self.hc_spec)):
            row = await self.service.enroll(spec)
            if row["policy"]["automatic_actions"] or row["policy"]["auto_investigate"]:
                raise CaseFail(actual, "Sandbox enrolled with automation permission")
            observation = await self.collect(self.service.target(target_id))
            if observation.get("healthy") is not True:
                raise CaseFail(actual, f"{target_id} observation unhealthy")
            snapshot = await self.service.capture_snapshot(target_id)
            if snapshot["target_id"] != target_id \
                    or snapshot["identity"]["image_id"] != observation["identity"]["image_id"]:
                raise CaseFail(actual, "Snapshot is not bound to target/image")
            self.snapshots[target_id] = snapshot
            actual[target_id] = {"observation": trimmed(observation), "snapshot_id": snapshot["snapshot_id"],
                                 "image_id": snapshot["identity"]["image_id"]}
        compose = self.snapshots[self.linkding_id].get("compose") or {}
        if not compose.get("resolved_sha256") or compose.get("project") != self.prov["linkding"]["project"]:
            raise CaseFail(actual, "Linkding snapshot lacks compose binding")
        self.linkding_healthy = True
        return actual

    async def case_healthy_control(self):
        self.ensure_budget()
        before = self.acc.inspect("container", self.prov["linkding"]["app"])
        self.adapter.mode = "abstain"
        row = await self.service.start_investigation(self.linkding_id, "Acceptance control: is the healthy sandbox left alone?")
        row = await wait_phase(self.store, self.service, row["investigation_id"], {"needs_human"}, 90)
        if row["phase"] != "needs_human" or (row.get("diagnosis") or {}).get("action") != "abstain":
            raise CaseFail({"phase": row["phase"]}, "Control did not abstain")
        if self.store.list_proposals(row["investigation_id"]):
            raise CaseFail({}, "Control created a proposal")
        after = self.acc.inspect("container", self.prov["linkding"]["app"])
        unchanged = after["Id"] == before["Id"] and after["State"]["StartedAt"] == before["State"]["StartedAt"]
        if not unchanged or self.store.get_setting(f"lastrestart.{self.linkding_id}") is not None:
            raise CaseFail({}, "Control mutated the sandbox")
        return {"phase": "needs_human", "diagnosis_action": "abstain", "proposals": 0,
                "identity_unchanged": True, "decision_source": MODEL_MODE,
                "note": "Mocked abstention exercises orchestration only, not model accuracy."}

    def fault(self, override_file):
        names = self.prov["linkding"]
        self.linkding_healthy = False
        self.acc.docker("compose", "--project-name", names["project"], "--file", self.prov["linkding_file"],
                        "--file", str(self.acc.dir / override_file), "up", "--detach", "--no-deps",
                        "--no-build", "--pull", "never", "--force-recreate", "linkding",
                        timeout=120, fixture=True)

    def app_config(self):
        row = self.acc.inspect("container", self.prov["linkding"]["app"])
        env = {item.partition("=")[0]: item.partition("=")[2] for item in (row.get("Config") or {}).get("Env") or []}
        return {"running": (row.get("State") or {}).get("Running") is True,
                "command": (row.get("Config") or {}).get("Cmd") or [],
                "server_port_env": env.get("LD_SERVER_PORT"),
                "memory_bytes": (row.get("HostConfig") or {}).get("Memory"),
                "started_at": (row.get("State") or {}).get("StartedAt"),
                "container_id": row["Id"], "image_id": row["Image"]}

    async def restore_flow(self, target_id, question):
        self.adapter.mode = "restore"
        row = await self.service.start_investigation(target_id, question)
        row = await wait_phase(self.store, self.service, row["investigation_id"], {"awaiting_approval", "needs_human"}, 90)
        if row["phase"] != "awaiting_approval":
            raise CaseFail({"phase": row["phase"], "error": row.get("error")}, "Restore did not reach approval")
        proposal = self.store.get_proposal(row["proposal_id"])
        if proposal["action"] != ActionKind.RESTORE.value or proposal.get("blocked_reason"):
            raise CaseFail({"action": proposal["action"]}, "Restore proposal blocked or wrong action")
        decision = await self.service.decide(proposal["proposal_id"], proposal["proposal_hash"], True)
        if decision.get("status") != "accepted":
            raise CaseFail({}, "Fixture approval was not accepted")
        row = await wait_phase(self.store, self.service, row["investigation_id"],
                               {"mitigated", "needs_human"}, 240)
        return row, proposal

    async def case_wrong_port(self):
        self.ensure_budget()
        if not self.linkding_healthy:
            raise CaseFail({}, "Blocked: baseline was not restored")
        self.fault("fault-port.json")
        await asyncio.sleep(3)
        config = self.app_config()
        if not config["running"]:
            raise CaseFail(config, "Fault stopped the process instead of misconfiguring it")
        observation = await self.collect(self.service.target(self.linkding_id))
        health = next(item for item in observation["probes"] if item["url"].endswith("/health"))
        if health.get("ok"):
            raise CaseFail(config, "Expected probe still passes after port fault")
        if config["server_port_env"] != "9091" or not any("9091" in part for part in config["command"]):
            raise CaseFail(config, "No measured port difference")
        actual = {"fault": config, "health_probe": health,
                  "baseline_port": "9090", "observed_port": config["server_port_env"]}
        row, proposal = await self.restore_flow(self.linkding_id, "Acceptance: sandbox serves the wrong port.")
        if row["phase"] != "mitigated":
            raise CaseFail(actual, f"Restore ended in {row['phase']}: {(row.get('error') or {})}")
        verification = row.get("verification") or {}
        if verification.get("verified") is not True or verification.get("root_cause_fixed") is not False:
            raise CaseFail(actual, "Restore verification is not mitigated/false-fix")
        restored = self.app_config()
        if restored["server_port_env"] != "9090" or restored["image_id"] != LINKDING_IMAGE:
            raise CaseFail(actual, "Restored config or image is wrong")
        probes = await self.collect(self.service.target(self.linkding_id))
        if probes.get("healthy") is not True or any(not item.get("ok") for item in probes["probes"]):
            raise CaseFail(actual, "Probes unhealthy after restore")
        self.linkding_healthy = True
        actual.update({"proposal_id": proposal["proposal_id"], "phase": "mitigated",
                       "verified": True, "root_cause_fixed": False, "restored": restored,
                       "started_changed": restored["started_at"] != config["started_at"]})
        return actual

    async def case_reduced_memory(self):
        self.ensure_budget()
        if not self.linkding_healthy:
            raise CaseFail({}, "Blocked: baseline was not restored")
        self.fault("fault-mem.json")
        await asyncio.sleep(2)
        config = self.app_config()
        baseline_limit = self.snapshots[self.linkding_id]["configuration"]["memory_limit_bytes"]
        if config["memory_bytes"] != 96 * 1024 * 1024 or baseline_limit != 256 * 1024 * 1024:
            raise CaseFail(config, "No measured memory-limit difference")
        actual = {"fault_limit_bytes": config["memory_bytes"], "baseline_limit_bytes": baseline_limit}
        row, proposal = await self.restore_flow(self.linkding_id, "Acceptance: sandbox memory limit reduced.")
        if row["phase"] != "mitigated":
            raise CaseFail(actual, f"Restore ended in {row['phase']}")
        restored = self.app_config()
        probes = await self.collect(self.service.target(self.linkding_id))
        if restored["memory_bytes"] != 256 * 1024 * 1024 or probes.get("healthy") is not True:
            raise CaseFail(actual, "Memory limit or HTTP availability not restored")
        self.linkding_healthy = True
        actual.update({"proposal_id": proposal["proposal_id"], "phase": "mitigated",
                       "restored_limit_bytes": restored["memory_bytes"], "probes_healthy": True})
        return actual

    async def case_restart_cooldown(self):
        self.ensure_budget()
        self.adapter.mode = "restart"
        before = self.acc.inspect("container", self.prov["hc"]["app"])
        row = await self.service.start_investigation(self.hc_id, "Acceptance: reviewed restart of sandbox.")
        row = await wait_phase(self.store, self.service, row["investigation_id"], {"awaiting_approval"}, 90)
        proposal = self.store.get_proposal(row["proposal_id"])
        if proposal["action"] != ActionKind.RESTART.value:
            raise CaseFail({"action": proposal["action"]}, "Restart proposal has the wrong action")
        await self.service.decide(proposal["proposal_id"], proposal["proposal_hash"], True)
        row = await wait_phase(self.store, self.service, row["investigation_id"],
                               {"mitigated", "needs_human"}, 240)
        if row["phase"] != "mitigated":
            raise CaseFail({}, f"Restart ended in {row['phase']}")
        after = self.acc.inspect("container", self.prov["hc"]["app"])
        if after["Id"] != before["Id"] or after["Image"] != before["Image"]:
            raise CaseFail({}, "Restart changed container/image identity")
        if after["State"]["StartedAt"] == before["State"]["StartedAt"]:
            raise CaseFail({}, "Restart did not change StartedAt")
        probes = await self.collect(self.service.target(self.hc_id))
        if probes.get("healthy") is not True:
            raise CaseFail({}, "Probes unhealthy after restart")
        actual = {"proposal_id": proposal["proposal_id"], "phase": "mitigated",
                  "container_unchanged": True, "image_unchanged": True,
                  "started_before": before["State"]["StartedAt"],
                  "started_after": after["State"]["StartedAt"]}
        previous_cooldown = self.store.get_setting(f"lastrestart.{self.hc_id}")
        second = await self.service.start_investigation(self.hc_id, "Acceptance: immediate second restart.")
        second = await wait_phase(self.store, self.service, second["investigation_id"], {"needs_human"}, 90)
        second_proposal = self.store.get_proposal(second["proposal_id"])
        try:
            await self.service.decide(second_proposal["proposal_id"], second_proposal["proposal_hash"], True)
        except OpsError as exc:
            if exc.code != "proposal_not_pending":
                raise CaseFail(actual, f"Blocked approval rejected for unexpected reason: {exc.code}") from exc
        else:
            raise CaseFail(actual, "Blocked cooldown proposal accepted a manual approval")
        final = self.acc.inspect("container", self.prov["hc"]["app"])
        check_cooldown_block(second, self.store.get_proposal(second_proposal["proposal_id"]),
                             after, final, previous_cooldown,
                             self.store.get_setting(f"lastrestart.{self.hc_id}"))
        actual.update({"second_rejected": "restart_cooldown", "second_status": "blocked",
                       "no_second_dispatch": True, "approval_rejected": "proposal_not_pending",
                       "cooldown_unchanged": True})
        return actual

    async def case_reopen(self):
        self.ensure_budget()
        before_investigations = [(row["investigation_id"], row["phase"])
                                 for row in self.store.list_investigations(limit=100)]
        before_proposals = [(item["proposal_id"], item.get("status"))
                            for item in self.store.list_proposals()]
        apps_before = {key: self.acc.inspect("container", self.prov[key]["app"])["State"]["StartedAt"]
                       for key in ("linkding", "hc")}
        await self.service.shutdown()
        reopened = OpsStore(self.acc.dir / "store")
        service = None
        try:
            service = OpsService(ROOT, store=reopened, investigator=OpsInvestigator(DeterministicAdapter()))
            await service.startup()
            after_investigations = [(row["investigation_id"], row["phase"])
                                    for row in reopened.list_investigations(limit=100)]
            after_proposals = [(item["proposal_id"], item.get("status"))
                               for item in reopened.list_proposals()]
            if after_investigations != before_investigations or after_proposals != before_proposals:
                raise CaseFail({}, "History did not survive close/reopen")
            for key in ("linkding", "hc"):
                row = self.acc.inspect("container", self.prov[key]["app"])
                if row["State"].get("Running") is not True \
                        or row["State"]["StartedAt"] != apps_before[key]:
                    raise CaseFail({}, "Fixture app did not stay alive across reopen")
            for target_id in (self.linkding_id, self.hc_id):
                result = await service.disconnect(target_id)
                if result.get("container_stopped") is False and result.get("monitoring") is False:
                    pass
                else:
                    raise CaseFail({}, "Disconnect reported an unexpected result")
            for key in ("linkding", "hc"):
                if self.acc.inspect("container", self.prov[key]["app"])["State"].get("Running") is not True:
                    raise CaseFail({}, "Disconnect stopped a fixture app")
        finally:
            if service is not None:
                await service.shutdown()
        self.service = service
        self.store = reopened
        return {"investigations": len(after_investigations), "proposals": len(after_proposals),
                "history_survived": True, "apps_alive_after_reopen": True,
                "apps_alive_after_disconnect": True}


async def run_matrix(matrix, acc, result, cases=(1, 2, 3, 4, 5, 6)):
    selected = validate_cases(cases)
    baseline = await asyncio.wait_for(matrix.baseline(), timeout=180)
    result["provisioning"]["baseline"] = baseline
    acc.write_result(result)
    matrix.deadline = time.monotonic() + MATRIX_BUDGET
    acc.matrix_deadline = matrix.deadline
    matrix_start = time.monotonic()
    checks = [(1, "enroll-observe-snapshot",
                   "Observe-only enrollment, healthy evidence, bound snapshots.",
                   matrix.case_enroll_snapshot),
              (2, "healthy-control-abstains",
                   "Deterministic abstention, no proposal, no mutation.",
                   matrix.case_healthy_control),
              (3, "wrong-port-restore",
                   "Measured port fault restored via approval; mitigated, root cause open.",
                   matrix.case_wrong_port),
              (4, "reduced-memory-restore",
                   "Measured 96MiB fault restored to 256MiB via approval.",
                   matrix.case_reduced_memory),
              (5, "restart-then-cooldown",
                   "One restart verified; immediate second rejected by cooldown.",
                   matrix.case_restart_cooldown)]
    for case_id, name, expected, check in checks:
        if case_id in selected:
            await run_case(acc, result, case_id, name, expected, check)
    result["mutation_trace"]["aura_dispatches"] = dispatch_records(matrix.store)
    if 6 in selected:
        await run_case(acc, result, 6, "reopen-survives",
                       "History survives close/reopen; shutdown/disconnect stop nothing.",
                       matrix.case_reopen)
    result["matrix_seconds"] = round(time.monotonic() - matrix_start, 1)
    await matrix.service.shutdown()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-real-docker", action="store_true",
                        help="Provision disposable fixtures and run the matrix once.")
    parser.add_argument("--run-id", default="acc-" + uuid.uuid4().hex[:12])
    parser.add_argument("--cases", default="1,2,3,4,5,6",
                        help="Explicit subset on newly provisioned fixtures, e.g. 1,4,5,6; never resets cooldown.")
    args = parser.parse_args(argv)
    try:
        selected = validate_cases([int(case) for case in args.cases.split(",")])
    except (ValueError, RunnerError) as exc:
        parser.error(str(exc))
    if not re.fullmatch(r'acc-[a-z0-9]{6,20}', args.run_id):
        parser.error('run-id must be acc- followed by 6 to 20 lowercase letters/digits')
    os.environ["PATH"] = str(ROOT / ".local/environment/Docker/resources/bin") + os.pathsep + os.environ.get("PATH", "")
    acc = Acceptance(args.run_id)
    result = {"run_id": args.run_id, "model_mode": MODEL_MODE, "real_docker": True,
              "scope": "full" if selected == [1, 2, 3, 4, 5, 6] else "selected",
              "selected_cases": selected,
              "provider_requests": 0, "cases": [], "mutation_trace": {"fixture_commands": [], "aura_dispatches": []},
              "cleanup": [], "leaks": [], "passed": False}
    try:
        preflight, ports, hc_image = acc.preflight()
    except RunnerError as exc:
        result["preflight"] = {"blocked": str(exc)}
        path = acc.write_result(result)
        print(json.dumps({"preflight": "blocked", "error": str(exc), "evidence": path}))
        return 2
    result["preflight"] = preflight
    print(json.dumps({"preflight": "ok", "run_id": args.run_id, "ports": ports}), flush=True)
    if not args.run_real_docker:
        print("Offline mode: preflight only, no fixtures created. Pass --run-real-docker to run the matrix.")
        return 0
    matrix = None
    try:
        prov = acc.provision(ports, hc_image)
        result["provisioning"] = prov
        matrix = Matrix(acc, result, prov, hc_image)
        async def bounded_matrix():
            try:
                await asyncio.wait_for(run_matrix(matrix, acc, result, selected), timeout=MATRIX_BUDGET + 180)
            finally:
                await matrix.service.shutdown()
        asyncio.run(bounded_matrix())
    except (RunnerError, OpsError) as exc:
        acc.cases.append({"case": 0, "name": "harness", "expected": "Matrix completes.",
                          "actual": {}, "passed": False,
                          "error": f"{type(exc).__name__}:{getattr(exc, 'code', exc)}"})
    except Exception as exc:
        acc.cases.append({"case": 0, "name": "harness", "expected": "Matrix completes.",
                          "actual": {}, "passed": False, "error": f"runner:{type(exc).__name__}: {exc}"})
    finally:
        if matrix is not None:
            try:
                matrix.store.close()
            except Exception:
                pass
        result["mutation_trace"]["fixture_commands"] = [
            [part if not part.startswith("SECRET_KEY=") else "SECRET_KEY=[REDACTED]" for part in argv]
            for argv in acc.fixture_commands]
        result["cleanup"] = acc.cleanup_records
        try:
            result["leaks"] = acc.cleanup()
        except Exception as exc:
            result["cleanup_error"] = f"{type(exc).__name__}: {exc}"
            result["leaks"] = result.get("leaks", [])
        try:
            result["registered_after"] = acc.registered_fingerprints()
            result["backend_after"] = acc.backend_health()
        except Exception as exc:
            result["after_check_error"] = f"{type(exc).__name__}: {exc}"
        result["cases"] = acc.cases
        registered_ok = result.get("registered_after") == acc.registered_before
        backend_ok = result.get("backend_after") == 200 == result["preflight"]["backend"]
        result["passed"] = [case['case'] for case in acc.cases] == selected \
            and all(case["passed"] for case in acc.cases) and not result.get('cleanup_error') \
            and not result["leaks"] and all(item.get("removed") for item in acc.cleanup_records) \
            and registered_ok and backend_ok
        result["duration_seconds"] = round(time.time() - acc.started_at, 1)
        path = acc.write_result(result)
        print(json.dumps({"passed": result["passed"], "evidence": path,
                          "cases": [(case["case"], case["passed"]) for case in acc.cases]}), flush=True)
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

