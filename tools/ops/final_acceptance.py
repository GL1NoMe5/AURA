"""OPS-FINAL-ACCEPT-001 evidence harness: finite post-guard acceptance.

Two bounded operations, nothing else:

1. ``config-experiment`` -- config-only double-promotion check (Argon 2.5).
   Two nested baselines are produced through the real production promotion
   code (``backend/aura_ops/promotion.py``) with only target
   identity/verification mocked. Resolution is compared with the real
   installed ``docker compose config --format json`` on the base, first and
   second promoted configs. The command never starts containers; the
   fail-closed runner refuses every other Docker verb.
2. ``run-matrix`` -- backend liveness plus exactly one existing real-Docker
   acceptance matrix (``tools/ops/acceptance.py --run-real-docker``) on
   disposable targets, with at most one corrective retry for a demonstrated
   harness-only defect.

No provider requests, installs, image builds/pulls, UI changes, production
code edits or publication. New files stay under the project; registered
fixtures, gateways, volumes and ``.local/ops`` are never touched.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
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

from aura_ops.docker import CommandResult, run_command  # noqa: E402
from aura_ops.models import utc_now  # noqa: E402

TASK_ID = "OPS-FINAL-ACCEPT-001"
CONTEXT = "desktop-linux"
DOCKER = str(ROOT / ".local" / "environment" / "Docker" / "resources" / "bin" / "docker.exe")
BACKEND_HEALTH = "http://127.0.0.1:8000/health"
HEALTH_ENV_VAR = "AURA_ACCEPT_BACKEND_HEALTH"
LINKDING_IMAGE = "sha256:9c1d5a16184bce77a6bedc2d11b54ccb25f21106483e6b6c5bd670b2613c6178"
MATRIX_PORTS = (18093, 18094, 18095, 18096)

# Harmless invented fixture values. No real paths, hosts, credentials or
# application data. Safe to persist in evidence.
FIXTURE = {
    "target_id": "finalaccept-config",
    "container_name": "finalaccept-config-fixture",
    "compose_project": "finalaccept-config",
    "compose_service": "app",
    "network_name": "finalaccept-front",
    "volume_name": "finalaccept-appdata",
    "source_revision": "0f1e2d3c4b5a69780f1e2d3c4b5a69780f1e2d3c",
    "container_id": "ab" * 32,
    "started_at": "2026-10-30T00:00:00Z",
    "env": {"FIXTURE_GREETING": "hello-final-accept", "FIXTURE_EXTRA": "extra-value"},
    "direct_env": "direct-value",
    "base_command": ["fixture-base-command", "--port", "9090"],
    "launch": {"entrypoint": ["/usr/local/bin/fixture-server"],
               "command": ["--serve", "127.0.0.1:9090"]},
    "probe_url": "http://127.0.0.1:18099/health",
}


def project_python() -> str:
    candidate = ROOT / "backend" / ".venv" / "Scripts" / "python.exe"
    if candidate.is_file():
        return str(candidate)
    return sys.executable


def ensure_docker_path() -> str:
    """Prefer the project Docker binary; never install or download one."""
    if Path(DOCKER).is_file():
        directory = str(Path(DOCKER).parent)
        os.environ["PATH"] = directory + os.pathsep + os.environ.get("PATH", "")
        return DOCKER
    return "docker"


# ---------------------------------------------------------------------------
# Pure helpers (offline-testable; no Docker, no network).
# ---------------------------------------------------------------------------

def extract_service(resolved: dict, service: str) -> dict:
    """Pick the compared fields out of ``docker compose config`` JSON output."""
    services = resolved.get("services") or {}
    if service not in services:
        raise KeyError(f"service {service!r} missing from resolved config")
    entry = services[service]

    def _cmd(value):
        if value is None or isinstance(value, list):
            return value
        return [value]

    binds = {}
    named = {}
    for item in entry.get("volumes") or []:
        if isinstance(item, dict):
            kind = item.get("type")
            source, target = item.get("source"), item.get("target")
            if kind == "bind" and source and target:
                binds[target] = source
            elif kind == "volume" and source and target:
                named[target] = source
        elif isinstance(item, str):
            match = re.fullmatch(r"((?:[A-Za-z]:)?[^:]+):([^:]+)(?::.*)?", item)
            if match:
                source, target = match.group(1), match.group(2)
                if "/" in source or "\\" in source or source.startswith("."):
                    binds[target] = source
                else:
                    named[target] = source
    networks = sorted((entry.get("networks") or {}).keys()) if isinstance(
        entry.get("networks"), dict) else sorted(entry.get("networks") or [])
    top_networks = {key: (value or {}).get("name", key)
                    for key, value in (resolved.get("networks") or {}).items()}
    top_volumes = {key: (value or {}).get("name", key)
                   for key, value in (resolved.get("volumes") or {}).items()}
    raw_env = entry.get("environment") or {}
    if isinstance(raw_env, list):
        environment = {}
        for item in raw_env:
            key, sep, value = item.partition("=")
            if sep:
                environment[key] = value
    else:
        environment = dict(raw_env)
    env_files = []
    for item in entry.get("env_file") or []:
        if isinstance(item, dict):
            env_files.append(item.get("path"))
        elif isinstance(item, str):
            env_files.append(item)
    return {
        "image": entry.get("image"),
        "entrypoint": _cmd(entry.get("entrypoint")),
        "command": _cmd(entry.get("command")),
        "container_name": entry.get("container_name"),
        "environment": environment,
        "env_file": sorted(path for path in env_files if path),
        "binds": binds,
        "named_volumes": named,
        "service_networks": networks,
        "top_networks": top_networks,
        "top_volumes": top_volumes,
    }


def compare_resolved(base: dict, first: dict, second: dict, *, service: str,
                     expected_image: str, expected_launch: dict) -> dict:
    """Compare three resolved configs; only image/launch may change."""
    got = [extract_service(resolved, service) for resolved in (base, first, second)]
    names = ("base", "first", "second")
    aspects = {}
    for key in ("binds", "environment", "env_file", "service_networks", "top_networks",
                "top_volumes", "named_volumes", "container_name"):
        values = [item[key] for item in got]
        aspects[key] = {"equal": values[0] == values[1] == values[2],
                        "values": values if len({json.dumps(v, sort_keys=True, default=str) for v in values}) > 1 else [values[0]]}
    images = [item["image"] for item in got]
    aspects["image"] = {"equal": all(image == expected_image for image in images),
                        "values": images}
    launches = [(item["entrypoint"], item["command"]) for item in got]
    aspects["launch"] = {
        "equal": launches[1] == launches[2] == (expected_launch["entrypoint"], expected_launch["command"]),
        "values": [{"entrypoint": entry, "command": cmd} for entry, cmd in launches]}
    stable_keys = ("binds", "environment", "env_file", "service_networks", "top_networks",
                   "top_volumes", "named_volumes", "container_name")
    normalized = []
    for document in (base, first, second):
        document = copy.deepcopy(document)
        for key in ("image", "entrypoint", "command"):
            document["services"][service].pop(key, None)
        normalized.append(document)
    aspects["complete_config"] = {"equal": normalized[0] == normalized[1] == normalized[2]}
    passed = all(aspects[key]["equal"] for key in (*stable_keys, "image", "launch", "complete_config"))
    detail = {f"{names[i]}=={names[i + 1]}":
              {key: got[i][key] == got[i + 1][key] for key in (*stable_keys, "image")}
              for i in range(2)}
    return {"passed": passed, "aspects": aspects, "pairwise": detail}


_SECRET_PATTERNS = (
    re.compile(r"(?i)secret[_-]?key\s*[:=]"),
    re.compile(r"(?i)api[_-]?token\s*[:=]"),
    re.compile(r"(?i)authorization\s*[:=]\s*(bearer|token)\s+\S"),
    re.compile(r"(?i)\bcookie\b\s*[:=].*[;=\w-]{8,}"),
    re.compile(r"(?i)baseten[_-]?api[_-]?key"),
    re.compile(r"AURA_TARGET_HEALTHCHECKS_COOKIE\s*[:=]\s*\S"),
    re.compile(r"AURA_LAB_API_TOKEN\s*[:=]\s*\S"),
)


def scan_for_secrets(text: str) -> list[str]:
    """Return redacted match contexts; empty means no credential pattern."""
    findings = []
    for pattern in _SECRET_PATTERNS:
        for match in pattern.finditer(text):
            start = max(0, match.start() - 24)
            findings.append(text[start:match.end() + 8].replace("\n", " "))
            if len(findings) >= 8:
                return findings
    return findings


def matrix_slot(evidence_dir: Path, retry_note: str | None) -> dict:
    """Enforce at most one matrix run plus one harness-defect retry."""
    record_path = evidence_dir / "matrix-runs.json"
    runs = json.loads(record_path.read_text(encoding="utf-8")) if record_path.is_file() else []
    if any(run.get("passed") for run in runs):
        return {"allowed": False, "reason": "matrix already passed; no repeat runs",
                "runs": runs}
    if len(runs) >= 2:
        return {"allowed": False, "reason": "attempt budget exhausted (one run plus one retry)",
                "runs": runs}
    if runs and not (retry_note or "").strip():
        return {"allowed": False,
                "reason": "first attempt recorded; a retry needs --retry-harness-defect with the demonstrated harness-only defect",
                "runs": runs}
    if runs and len((retry_note or "").strip()) < 24:
        return {"allowed": False,
                "reason": "retry note must describe the demonstrated harness-only defect",
                "runs": runs}
    return {"allowed": True, "runs": runs, "attempt": len(runs) + 1}


def http_status(url: str, timeout: float = 10.0) -> int | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status
    except Exception:
        return None


def pick_temp_port() -> int:
    """Bind an ephemeral localhost port to prove it is free, then release it."""
    probe = socket.socket()
    try:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    finally:
        probe.close()
    if port in MATRIX_PORTS or port == 8000:
        raise RuntimeError(f"ephemeral port {port} collides with a reserved port")
    return port


SERVER_BOOT = (
    "import sys;"
    "sys.path.insert(0, r'{backend}');"
    "from aura_api.app import create_app;"
    "import uvicorn;"
    "app = create_app(provider_configured=False, wire_default_orchestrator=False,"
    " enable_demo=False, enable_ops=False);"
    "uvicorn.run(app, host='127.0.0.1', port={port}, log_level='warning')"
)


# ---------------------------------------------------------------------------
# Config-only double-promotion experiment (Argon 2.5).
# ---------------------------------------------------------------------------

BASE_COMPOSE = """\
services:
  {service}:
    container_name: {container_name}
    image: "{image}"
    command: ["fixture-base-command", "--port", "9090"]
    env_file: [".env"]
    environment:
      FIXTURE_DIRECT: "{direct}"
      FIXTURE_FROM_ENV: "${{FIXTURE_GREETING}}"
    volumes:
      - ./rel-data:/data:ro
      - {volume_key}:/var/lib/fixture
    networks: [front]
networks:
  front:
    name: {network_name}
volumes:
  {volume_key}:
    name: {volume_name}
"""

APP_SOURCE = '"""Invented fixture module for the promotion config experiment."""\n\n\ndef answer():\n    return 42\n'
REGRESSION_SOURCE = 'def test_answer():\n    assert 42 == 42\n'


def write_base_fixture(root: Path) -> dict:
    """Write the invented base tree: compose, env, bind dir, enrolled source."""
    base = (root / "base").resolve()
    (base / "rel-data").mkdir(parents=True, exist_ok=True)
    (base / "rel-data" / "fixture.txt").write_text("invented bind content\n", encoding="utf-8")
    (base / "compose.yaml").write_text(BASE_COMPOSE.format(
        service=FIXTURE["compose_service"], container_name=FIXTURE["container_name"],
        image=LINKDING_IMAGE, direct=FIXTURE["direct_env"],
        volume_key="appdata", network_name=FIXTURE["network_name"],
        volume_name=FIXTURE["volume_name"]), encoding="utf-8")
    (base / ".env").write_text("".join(f"{key}={value}\n" for key, value in FIXTURE["env"].items()),
                               encoding="utf-8")
    source = base / "source"
    source.mkdir(exist_ok=True)
    (source / "app.py").write_text(APP_SOURCE, encoding="utf-8")
    (source / "tests").mkdir(exist_ok=True)
    (source / "tests" / "test_app.py").write_text("def test_existing():\n    assert True\n",
                                                  encoding="utf-8")
    files = {path.relative_to(source).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
             for path in sorted(source.rglob("*")) if path.is_file()}
    manifest = {"source_dir": str(source), "revision": FIXTURE["source_revision"], "files": files}
    manifest_bytes = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
    (base / "source-manifest.json").write_bytes(manifest_bytes)
    return {"base": str(base), "compose": str(base / "compose.yaml"), "env_file": str(base / ".env"),
            "source_dir": str(source), "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest()}


class ConfigOnlyRunner:
    """Fail-closed Docker boundary: read-only config/image inspection only.

    ``compose config`` and ``image inspect`` run against the real installed
    binaries (no containers are started). Every other verb raises instead of
    executing, and every call is recorded for evidence.
    """

    def __init__(self, calls: list):
        self.calls = calls

    async def __call__(self, argv, **limits):
        self.calls.append({"argv": list(argv), "limits": {key: limits.get(key) for key in
                                                          ("timeout", "max_stdout", "max_stderr")}})
        words = set(argv)
        if "compose" in words and "config" in words:
            if words & {"up", "run", "create", "start", "restart", "stop", "rm", "down", "exec", "build", "push", "pull"}:
                raise RuntimeError(f"refused mutating compose verb: {argv}")
            return await run_command(list(argv), **limits)
        if argv[3:5] == ["image", "inspect"]:
            return await run_command(list(argv), **limits)
        raise RuntimeError(f"refused non-config docker call: {argv}")


def _mock_identity() -> dict:
    return {"container_id": FIXTURE["container_id"], "image_id": LINKDING_IMAGE,
            "image_ref": LINKDING_IMAGE, "started_at": FIXTURE["started_at"]}


def _mock_container_row() -> dict:
    return {"Id": FIXTURE["container_id"], "Image": LINKDING_IMAGE,
            "Config": {"Image": LINKDING_IMAGE}, "State": {"StartedAt": FIXTURE["started_at"]}}


def _mock_observation(identity: dict) -> dict:
    return {"timestamp": utc_now(), "healthy": True, "identity": dict(identity),
            "config": {"memory_limit_bytes": 268435456, "published_ports": {},
                       "server_port": 9090}}


class MockIdentityDocker:
    """Mocked target identity/verification boundary for the experiment."""

    def __init__(self, identity: dict):
        self._identity = identity

    def __call__(self, *args, **kwargs):
        identity = self._identity
        parent = self

        class _Target:
            async def collect(self):
                return _mock_observation(identity)

            async def inspect_identity(self):
                return dict(identity)

        parent.calls.append({"args": [str(arg) for arg in args][:2]})
        return _Target()

    calls: list = []


async def _build_proposal_record(store, spec, snapshot_hash: str, investigation_id: str,
                                 identity: dict, patch_id: str) -> dict:
    from aura_ops.actions import _hash, _spec_hash
    from aura_ops.deployment import _binding
    from aura_ops.models import ActionKind
    target_row = store.get_target(spec.target_id)
    proposal = {
        "version": 1, "action": ActionKind.DEPLOY.value, "target_id": spec.target_id,
        "investigation_id": investigation_id, "expected_identity": dict(identity),
        "spec_hash": _spec_hash(spec),
        "policy_hash": _hash(spec.policy.model_dump(mode="json")),
        "enrollment_created_at": target_row["created_at"],
        "snapshot_hash": snapshot_hash, "patch_id": patch_id,
        "patch_metadata_id": "00" * 16, "patch_metadata_sha256": "11" * 32,
        "source_manifest_hash": "22" * 32, "profile_hash": "33" * 32,
        "candidate_manifest_hash": "44" * 32, "edited_hashes": {},
        "regression_sha256": "55" * 32,
        "base_image_id": LINKDING_IMAGE, "candidate_image_id": LINKDING_IMAGE,
        "deployment_root": "/app", "deployment_launch": dict(FIXTURE["launch"]),
        "build_artifact_id": "66" * 16, "build_artifact_sha256": "77" * 32,
        "rollback_on_failure": True, "reason": "final-acceptance fixture",
    }
    proposal["proposal_hash"] = _binding(proposal)
    proposal["approved_hash"] = proposal["proposal_hash"]
    proposal["authorization"] = "user"
    proposal["status"] = "verified"
    proposal["verification"] = {"verified": True, "root_cause_fixed": True,
                                "functional": {"verified": True}}
    proposal["deployment_identity"] = dict(identity)
    return store.put_proposal(proposal)


async def run_config_experiment(evidence_dir: Path) -> dict:
    """Produce two nested promotions through production code; compare configs."""
    from aura_ops.actions import _hash
    from aura_ops.deployment import DeploymentManager
    from aura_ops.models import TargetSpec
    from aura_ops.patches import _hash as file_hash, _REGRESSION, _source_files
    from aura_ops.promotion import promote
    from aura_ops.source import SourceIndex

    started = time.time()
    evidence_dir = Path(evidence_dir).resolve()
    if (evidence_dir / "config-experiment.json").exists():
        raise RuntimeError("config experiment already recorded in this evidence dir")
    evidence_dir.mkdir(parents=True, exist_ok=True)
    docker_path = ensure_docker_path()
    calls: list = []
    MockIdentityDocker.calls = []
    identity = _mock_identity()
    outcome_versions = collect_versions()
    outcome: dict = {"task": TASK_ID, "experiment": "double-promotion-config",
                     "cwd": os.getcwd(), "versions": outcome_versions,
                     "mocked": ["target identity", "verification"],
                     "production_paths": ["promotion.promote x2", "actions.capture_snapshot x3",
                                          "actions._check_snapshot x2", "store.promote_baseline x2",
                                          "docker compose config --format json x3 (read-only)"]}

    fixture = write_base_fixture(evidence_dir / "config-fixture")
    outcome["fixture"] = {**fixture, "values": {"env": FIXTURE["env"],
             "direct_env": FIXTURE["direct_env"], "launch": FIXTURE["launch"],
             "image": LINKDING_IMAGE, "network": FIXTURE["network_name"],
             "volume": FIXTURE["volume_name"]}}
    store_dir = evidence_dir / "config-store"
    store = None
    try:
        from aura_ops.store import OpsStore
        store = OpsStore(store_dir)
        spec = TargetSpec(
            target_id=FIXTURE["target_id"], name="Final acceptance config fixture",
            container_name=FIXTURE["container_name"], docker_context=CONTEXT,
            probes=[{"url": FIXTURE["probe_url"]}], source_dir=fixture["source_dir"],
            source_revision=FIXTURE["source_revision"],
            source_manifest_sha256=fixture["manifest_sha256"], source_image_id=LINKDING_IMAGE,
            deployment_root="/app", deployment_launch=dict(FIXTURE["launch"]),
            compose_file=fixture["compose"], compose_project=FIXTURE["compose_project"],
            compose_service=FIXTURE["compose_service"],
            patch_profile={"image": LINKDING_IMAGE, "test_paths": ["tests"],
                           "timeout_seconds": 60})
        store.put_target(spec.model_dump(mode="json"))
        # Real enrollment validation of the invented base source.
        _source_files(spec, SourceIndex(spec))
        factory = MockIdentityDocker(identity)
        manager = DeploymentManager(store, docker_factory=factory,
                                    runner=ConfigOnlyRunner(calls))

        async def _container(current):
            return copy.deepcopy(_mock_container_row())

        candidate_dir = evidence_dir / "config-fixture" / "candidate"
        (candidate_dir / "aura_generated_tests").mkdir(parents=True, exist_ok=True)
        (candidate_dir / "app.py").write_text(APP_SOURCE, encoding="utf-8")
        (candidate_dir / _REGRESSION).write_text(REGRESSION_SOURCE, encoding="utf-8")
        manifest = {name: file_hash((candidate_dir / name).read_bytes())
                    for name in ("app.py", _REGRESSION)}
        checked = {"record": {"candidate_manifest": manifest,
                              "candidate_path": str(candidate_dir)}}
        base_image = {"Id": LINKDING_IMAGE}
        candidate_image = {"Id": LINKDING_IMAGE}

        async def _revalidate(proposal, current):
            return checked, base_image, candidate_image, None

        manager._container = _container  # mocked identity only
        manager._revalidate = _revalidate  # mocked verification only

        async def _promote_once(current_spec, label):
            snapshot = await manager.actions.capture_snapshot(
                current_spec, _mock_observation(identity))
            investigation = store.create_investigation(
                current_spec.target_id, f"Final acceptance config fixture ({label})")
            record = await _build_proposal_record(
                store, current_spec, _hash(snapshot), investigation["investigation_id"],
                identity, "patch-" + label)
            store.update_investigation(investigation["investigation_id"],
                                       phase="verified_fixed", proposal_id=record["proposal_id"])
            result = await promote(manager, current_spec, record["proposal_id"])
            enrolled = TargetSpec.model_validate(
                {key: value for key, value in store.get_target(current_spec.target_id).items()
                 if key in TargetSpec.model_fields})
            return result, enrolled, snapshot, record["proposal_id"]

        first_record, first_spec, base_snapshot, _ = await _promote_once(spec, "v1")
        second_record, second_spec, first_snapshot, _ = await _promote_once(first_spec, "v2")
        second_snapshot = store.get_setting("snapshot." + spec.target_id)

        async def _resolved(current_spec):
            files = manager.actions._compose_files(current_spec)
            argv = manager.actions._compose_argv(current_spec, files) + ["config", "--format", "json"]
            calls.append({"argv": list(argv),
                          "limits": {"timeout": 15, "max_stdout": 1048576, "max_stderr": 65536},
                          "comparison": True})
            result = await run_command(list(argv), timeout=15,
                                       max_stdout=1024 * 1024, max_stderr=65536)
            if result.returncode:
                raise RuntimeError(f"compose config failed: {result.stderr[-500:]}")
            return files, argv, json.loads(result.stdout)

        base_files, base_argv, base_cfg = await _resolved(spec)
        first_files, first_argv, first_cfg = await _resolved(first_spec)
        second_files, second_argv, second_cfg = await _resolved(second_spec)
        comparison = compare_resolved(base_cfg, first_cfg, second_cfg,
                                      service=FIXTURE["compose_service"],
                                      expected_image=LINKDING_IMAGE,
                                      expected_launch=FIXTURE["launch"])
        full_match = (json.dumps(first_cfg, sort_keys=True) == json.dumps(second_cfg, sort_keys=True))
        outcome.update({
            "promotions": [
                {"label": "v1", "compose": first_record["snapshot_hash"],
                 "spec_compose_file": first_spec.compose_file},
                {"label": "v2", "compose": second_record["snapshot_hash"],
                 "spec_compose_file": second_spec.compose_file}],
            "compose_files": {"base": base_files, "first": first_files, "second": second_files},
            "commands": {"base": base_argv, "first": first_argv, "second": second_argv},
            "snapshot_hashes": {"base": _hash(base_snapshot), "first": _hash(first_snapshot),
                                "second": _hash(second_snapshot)},
            "resolved_sha256": {"base": base_snapshot["compose"]["resolved_sha256"],
                                "first": first_snapshot["compose"]["resolved_sha256"],
                                "second": second_snapshot["compose"]["resolved_sha256"]},
            "comparison": comparison,
            "first_second_full_match": full_match,
            "docker_calls": len(calls),
            "verdict": "pass" if comparison["passed"] else "confirmed-problem",
            "seconds": round(time.time() - started, 1),
        })
    finally:
        if store is not None:
            store.close()
    (evidence_dir / "compose-base.json").write_text(
        json.dumps(base_cfg, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (evidence_dir / "compose-first.json").write_text(
        json.dumps(first_cfg, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (evidence_dir / "compose-second.json").write_text(
        json.dumps(second_cfg, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (evidence_dir / "compose-runner-calls.json").write_text(
        json.dumps(calls, indent=2) + "\n", encoding="utf-8")
    (evidence_dir / "docker-path.txt").write_text(docker_path + "\n", encoding="utf-8")
    (evidence_dir / "config-experiment.json").write_text(
        json.dumps(outcome, indent=2) + "\n", encoding="utf-8")
    findings = scan_for_secrets((evidence_dir / "config-experiment.json").read_text(encoding="utf-8"))
    if findings:
        raise RuntimeError(f"credential pattern in config evidence: {findings[:2]}")
    return outcome


# ---------------------------------------------------------------------------
# Backend liveness plus a single real-Docker acceptance matrix.
# ---------------------------------------------------------------------------

def launch_temp_backend(port: int, log_path: Path) -> subprocess.Popen:
    """Start a temporary production app instance (API liveness only).

    Exact flags from the task: provider_configured=False,
    wire_default_orchestrator=False, enable_demo=False, enable_ops=False.
    No targets are registered; the matrix uses its own isolated OpsService.
    """
    code = SERVER_BOOT.format(backend=str(ROOT / "backend"), port=port)
    log_handle = log_path.open("wb")
    try:
        proc = subprocess.Popen(
            [project_python(), "-c", code], cwd=str(ROOT),
            stdin=subprocess.DEVNULL, stdout=log_handle, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        _remember_process(proc)
    except Exception:
        log_handle.close()
        raise
    log_handle.close()
    return proc


def wait_healthy(url: str, timeout: float = 45.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if http_status(url, timeout=5) == 200:
            return True
        time.sleep(1.0)
    return False


def port_free(port: int) -> bool:
    probe = socket.socket()
    probe.settimeout(2)
    try:
        return probe.connect_ex(("127.0.0.1", port)) != 0
    finally:
        probe.close()


def _remember_process(proc):
    import psutil
    try:
        proc._aura_created_at = psutil.Process(proc.pid).create_time()
    except psutil.NoSuchProcess:
        proc._aura_created_at = None


def _stop_owned_tree(proc: subprocess.Popen, timeout: float = 30.0) -> dict:
    """Stop the owned Popen and its identity-checked descendants, never listeners."""
    import psutil
    children = []
    if proc.poll() is None:
        root = psutil.Process(proc.pid)
        if root.create_time() != getattr(proc, "_aura_created_at", None):
            raise RuntimeError("Owned process identity cannot be confirmed")
        children = root.children(recursive=True)
        for child in reversed(children):
            try:
                if child.is_running():  # psutil checks PID reuse against create_time.
                    child.terminate()
            except psutil.NoSuchProcess:
                pass
        proc.terminate()  # Popen retains its owned OS process handle.
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=timeout)
    _, alive = psutil.wait_procs(children, timeout=timeout)
    for child in alive:
        try:
            if child.is_running():
                child.kill()
        except psutil.NoSuchProcess:
            pass
    _, alive = psutil.wait_procs(alive, timeout=timeout)
    return {"pid": proc.pid, "exit_code": proc.returncode,
            "owned_descendants": [child.pid for child in children],
            "process_tree_gone": proc.poll() is not None and not alive}


def stop_child(proc: subprocess.Popen, port: int, timeout: float = 30.0) -> dict:
    result = _stop_owned_tree(proc, timeout)
    deadline = time.monotonic() + min(timeout, 5.0)
    free = port_free(port)
    while not free and time.monotonic() < deadline:
        time.sleep(0.1)
        free = port_free(port)
    result["port_free"] = free
    return result


def _run_matrix_child(argv, env, timeout):
    proc = subprocess.Popen(argv, cwd=str(ROOT), env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    _remember_process(proc)
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        return subprocess.CompletedProcess(argv, proc.returncode, stdout, stderr)
    except BaseException as error:
        error.owned_process_cleanup = _stop_owned_tree(proc)
        raise


def collect_versions() -> dict:
    ensure_docker_path()
    versions = {"task": TASK_ID, "python": sys.version.split()[0]}
    try:
        import pytest  # noqa: PLC0415
        versions["pytest"] = pytest.__version__
    except Exception:
        versions["pytest"] = None
    for name, argv in (("docker", ["docker", "--version"]),
                      ("compose", ["docker", "compose", "version"]),
                      ("node", ["node", "--version"])):
        try:
            completed = subprocess.run(argv, capture_output=True, timeout=30, cwd=str(ROOT))
            versions[name] = (completed.stdout.decode("utf-8", "replace").strip()
                              or completed.stderr.decode("utf-8", "replace").strip())
        except Exception as exc:
            versions[name] = f"unavailable: {exc}"
    return versions


def run_matrix(evidence_dir: Path, *, run_id: str, retry_note: str | None,
               cases: list[int] | None = None) -> dict:
    """Run the existing acceptance matrix once against a healthy backend."""
    evidence_dir = Path(evidence_dir).resolve()
    evidence_dir.mkdir(parents=True, exist_ok=True)
    if not re.fullmatch(r"acc-[a-z0-9]{6,20}", run_id):
        raise RuntimeError("run-id must be acc- followed by 6 to 20 lowercase letters/digits")
    selected = list(range(1, 7)) if cases is None else list(cases)
    if not selected or 1 not in selected or selected != sorted(set(selected)) \
            or any(type(case) is not int or not 1 <= case <= 6 for case in selected):
        raise RuntimeError("cases must be sorted unique IDs from 1..6, including enrollment case 1")
    slot = matrix_slot(evidence_dir, retry_note)
    if not slot["allowed"]:
        raise RuntimeError(slot["reason"])
    liveness: dict = {"backend_8000": http_status(BACKEND_HEALTH, timeout=10)}
    env = dict(os.environ)
    temp: subprocess.Popen | None = None
    temp_port: int | None = None
    matrix_seconds: float | None = None
    summary = {"attempt": slot["attempt"], "run_id": run_id,
               "retry_note": retry_note if slot["attempt"] > 1 else None,
               "selected_cases": selected, "scope": "full" if selected == list(range(1, 7)) else "selected",
               "passed": False, "cases": [], "exit_code": None,
               "cleanup_all_removed": False, "registered_unchanged": False}
    try:
        if liveness["backend_8000"] == 200:
            liveness["mode"] = "reused-8000"
            env[HEALTH_ENV_VAR] = BACKEND_HEALTH
        else:
            temp_port = pick_temp_port()
            url = f"http://127.0.0.1:{temp_port}/health"
            log_path = evidence_dir / "temp-backend.log"
            temp = launch_temp_backend(temp_port, log_path)
            liveness.update({"mode": "temporary-localhost", "port": temp_port,
                             "pid": temp.pid, "url": url,
                             "flags": {"provider_configured": False,
                                       "wire_default_orchestrator": False,
                                       "enable_demo": False, "enable_ops": False}})
            if temp.poll() is not None or not wait_healthy(url):
                raise RuntimeError("temporary backend did not become healthy")
            liveness["healthy"] = True
            env[HEALTH_ENV_VAR] = url
        argv = [project_python(), str(ROOT / "tools" / "ops" / "acceptance.py"),
               "--run-real-docker", "--run-id", run_id,
               "--cases", ",".join(map(str, selected))]
        started = time.monotonic()
        completed = _run_matrix_child(argv, env, timeout=1800)
        matrix_seconds = round(time.monotonic() - started, 1)
        stdout = completed.stdout.decode("utf-8", "replace")
        stderr = completed.stderr.decode("utf-8", "replace")[-4000:]
        result_path = ROOT / ".local" / "ops-acceptance" / run_id / "result.json"
        result = json.loads(result_path.read_text(encoding="utf-8")) if result_path.is_file() else None
        summary.update({
                   "exit_code": completed.returncode, "matrix_seconds": matrix_seconds,
                   "result_path": str(result_path) if result else None,
                   "stdout_tail": stdout[-2000:], "stderr_tail": stderr,
                   "passed": bool(completed.returncode == 0 and result and result.get("passed") is True),
                   "cases": [(case.get("case"), case.get("name"), case.get("passed"))
                             for case in (result or {}).get("cases", [])] if result else [],
                   "model_mode": (result or {}).get("model_mode"),
                   "provider_requests": (result or {}).get("provider_requests"),
                   "leaks": (result or {}).get("leaks", []),
                   "cleanup_error": (result or {}).get("cleanup_error"),
                   "cleanup_all_removed": bool((result or {}).get("cleanup")) and all(
                       item.get("removed") is True for item in (result or {}).get("cleanup", [])),
                   "registered_unchanged": bool((result or {}).get("preflight", {}).get("registered"))
                   and (result or {}).get("registered_after")
                   == (result or {}).get("preflight", {}).get("registered"),
                   "backend_after": (result or {}).get("backend_after")})
        summary["passed"] = (summary["passed"] and summary["cleanup_all_removed"]
                             and summary["registered_unchanged"] and not summary["leaks"]
                             and not summary["cleanup_error"] and summary["backend_after"] == 200
                             and [case[0] for case in summary["cases"]] == selected
                             and all(case[2] is True for case in summary["cases"]))
    except BaseException as error:
        summary["passed"] = False
        summary["error"] = {"type": type(error).__name__, "message": "Matrix wrapper did not complete."}
        summary["owned_process_cleanup"] = getattr(error, "owned_process_cleanup", None)
    finally:
        if temp is not None:
            try:
                liveness["shutdown"] = stop_child(temp, temp_port or 0)
                if not (liveness["shutdown"].get("port_free") is True
                        and liveness["shutdown"].get("process_tree_gone") is True):
                    summary["passed"] = False
            except BaseException as error:
                summary["passed"] = False
                liveness["shutdown"] = {"error": type(error).__name__, "process_tree_gone": False}
    (evidence_dir / "backend-liveness.json").write_text(
        json.dumps(liveness, indent=2) + "\n", encoding="utf-8")
    runs = slot["runs"] + [summary]
    (evidence_dir / "matrix-runs.json").write_text(json.dumps(runs, indent=2) + "\n",
                                                   encoding="utf-8")
    (evidence_dir / "matrix-run.json").write_text(
        json.dumps({"latest": summary, "liveness": liveness}, indent=2) + "\n", encoding="utf-8")
    for name in ("matrix-run.json", "matrix-runs.json", "backend-liveness.json"):
        findings = scan_for_secrets((evidence_dir / name).read_text(encoding="utf-8"))
        if findings:
            raise RuntimeError(f"credential pattern in {name}: {findings[:2]}")
    summary["liveness_mode"] = liveness.get("mode")
    summary["seconds"] = matrix_seconds
    return summary


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    config = sub.add_parser("config-experiment", help="Run the config-only double-promotion check.")
    config.add_argument("--evidence-dir", required=True)
    matrix = sub.add_parser("run-matrix", help="Run the real-Docker acceptance matrix once.")
    matrix.add_argument("--evidence-dir", required=True)
    matrix.add_argument("--run-id", default="acc-" + uuid.uuid4().hex[:12])
    matrix.add_argument("--retry-harness-defect", default=None)
    matrix.add_argument("--cases", default=None, help="Comma-separated case IDs, including 1; default all six.")
    versions = sub.add_parser("versions", help="Record installed tool versions.")
    versions.add_argument("--evidence-dir", required=True)
    args = parser.parse_args(argv)
    if args.command == "config-experiment":
        outcome = asyncio.run(run_config_experiment(Path(args.evidence_dir)))
        print(json.dumps({"verdict": outcome["verdict"],
                          "passed": outcome["comparison"]["passed"],
                          "seconds": outcome["seconds"]}))
        return 0 if outcome["comparison"]["passed"] else 1
    if args.command == "run-matrix":
        summary = run_matrix(Path(args.evidence_dir), run_id=args.run_id,
                             retry_note=args.retry_harness_defect,
                             cases=[int(value) for value in args.cases.split(",")] if args.cases else None)
        print(json.dumps({"passed": summary["passed"], "run_id": summary["run_id"],
                          "cases": summary["cases"],
                          "liveness": summary["liveness_mode"]}))
        return 0 if summary["passed"] else 1
    if args.command == "versions":
        evidence_dir = Path(args.evidence_dir).resolve()
        evidence_dir.mkdir(parents=True, exist_ok=True)
        payload = collect_versions()
        (evidence_dir / "versions.json").write_text(json.dumps(payload, indent=2) + "\n",
                                                    encoding="utf-8")
        print(json.dumps(payload, indent=2))
        return 0
    parser.error("unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())


