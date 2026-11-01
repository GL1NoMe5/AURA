"""Pinned, isolated linkding evaluator. No LLM calls; default is read-only status."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
LAB = ROOT / ".local" / "ops-lab"
SOURCES = ROOT / "resources" / "ops" / "sources.json"
PROJECT = "aura-ops-lab"
CONTAINER = "aura-ops-lab-linkding"
GATEWAY = "aura-ops-lab-gateway"
BASE_URL = "http://127.0.0.1:18090"
MIB = 1024 * 1024
MAX_OUTPUT = 2 * MIB
IMAGES = {"bad": "aura-ops-linkding:f279015", "reference": "aura-ops-linkding:reference-677ae79",
          "test": "aura-ops-linkding:test-f279015"}
TEST_PATHS = ["bookmarks/tests/test_assets_service.py", "bookmarks/tests/test_bookmark_asset_view.py",
              "bookmarks/tests/test_bookmark_assets_api.py"]
DEPLOYMENT_LAUNCH = {"entrypoint": ["/etc/linkding/.venv/bin/uwsgi"],
                     "command": ["--http", "0.0.0.0:9090", "/etc/linkding/uwsgi.ini"]}


class LabError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def now():
    return datetime.now(timezone.utc).isoformat()


def file_sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(MIB), b""):
            digest.update(chunk)
    return digest.hexdigest()


def requirements_sha(path):
    # Git's Windows autocrlf must not invalidate the exact package/hash pins.
    return hashlib.sha256(Path(path).read_text(encoding="utf-8").encode("utf-8")).hexdigest()


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def safe_extract(archive, destination, expected_prefix):
    """Validate the entire archive before writing; no links/devices/traversal."""
    destination = Path(destination).resolve()
    with tarfile.open(archive, "r:gz") as source:
        entries = []
        total = 0
        for member in source.getmembers():
            relative = PurePosixPath(member.name)
            if relative.is_absolute() or ".." in relative.parts or "\\" in member.name:
                raise LabError("unsafe_archive", "Archive contains an unsafe path")
            if not relative.parts or relative.parts[0] != expected_prefix:
                raise LabError("unsafe_archive", "Unexpected archive root")
            if not member.isdir() and not member.isfile():
                raise LabError("unsafe_archive", "Archive links and special files are forbidden")
            parts = relative.parts[1:]
            if any(":" in p for p in parts):
                raise LabError("unsafe_archive", "Archive contains an invalid path component")
            target = destination.joinpath(*parts).resolve()
            if target != destination and destination not in target.parents:
                raise LabError("unsafe_archive", "Archive path escaped destination")
            total += member.size
            if total > 256 * MIB:
                raise LabError("archive_too_large", "Source archive exceeds extraction bound")
            entries.append((member, target))
        for member, target in entries:
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with source.extractfile(member) as incoming, target.open("wb") as outgoing:
                    shutil.copyfileobj(incoming, outgoing, MIB)


def download_verified(url, destination, digest):
    destination = Path(destination)
    if destination.is_file():
        if file_sha(destination) != digest:
            raise LabError("checksum_mismatch", "Existing download failed its pinned SHA256")
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(url, headers={"User-Agent": "AURA-OPS001/1.0"})
    partial = destination.with_suffix(destination.suffix + ".partial")
    with urllib.request.urlopen(request, timeout=60) as response, partial.open("wb") as output:
        size = 0
        while chunk := response.read(MIB):
            size += len(chunk)
            if size > 128 * MIB:
                raise LabError("download_too_large", "Download exceeds bound")
            output.write(chunk)
    if file_sha(partial) != digest:
        raise LabError("checksum_mismatch", "Download failed its pinned SHA256")
    partial.replace(destination)


def manifest_for(source, revision):
    source = Path(source).resolve()
    return {"source_dir": str(source), "revision": revision,
            "files": {p.relative_to(source).as_posix(): file_sha(p)
                      for p in sorted(source.rglob("*")) if p.is_file()}}


def check_manifest(manifest):
    source = Path(manifest["source_dir"])
    current = manifest_for(source, manifest["revision"])
    if current != manifest:
        raise LabError("source_modified", "Source snapshot differs from recorded manifest")


def prepare_sources(lab=LAB):
    lab = Path(lab)
    config = json.loads(SOURCES.read_text(encoding="utf-8"))["linkding"]
    archive = lab / "downloads" / "linkding.tar.gz"
    patch = lab / "private-reference" / "upstream.patch"
    download_verified(config["archive_url"], archive, config["archive_sha256"])
    download_verified(config["patch_url"], patch, config["patch_sha256"])
    source = lab / "source"
    source_manifest = lab / "source-manifest.json"
    if source_manifest.exists():
        check_manifest(json.loads(source_manifest.read_text(encoding="utf-8")))
    elif source.exists():
        raise LabError("unrecorded_source", "Source directory exists without provenance; inspect it before retrying")
    else:
        safe_extract(archive, source, "linkding-" + config["revision"])
        write_json(source_manifest, manifest_for(source, config["revision"]))
    reference = lab / "private-reference" / "source"
    reference_manifest = lab / "private-reference" / "source-manifest.json"
    if reference_manifest.exists():
        check_manifest(json.loads(reference_manifest.read_text(encoding="utf-8")))
    elif reference.exists():
        raise LabError("unrecorded_reference", "Reference directory exists without provenance; inspect it before retrying")
    else:
        shutil.copytree(source, reference)
        # Stop Git discovering the surrounding AURA checkout. The validated
        # upstream patch applies only inside this private evaluator directory.
        env = dict(os.environ, GIT_CEILING_DIRECTORIES=str(reference.parent.resolve()))
        for extra in (["--check"], []):
            result = subprocess.run(["git", "apply", *extra, str(patch.resolve())],
                                    cwd=reference, env=env, capture_output=True, timeout=20)
            if result.returncode:
                raise LabError("reference_patch_failed", "Pinned reference patch did not apply cleanly")
        write_json(reference_manifest, manifest_for(reference, config["revision"]))
    return {"source_dir": str(source.resolve()), "revision": config["revision"],
            "source_manifest": str(source_manifest.resolve()),
            "reference_is_evaluator_only": True, "runtime_reproduction_verified": False}


def execute(argv, *, timeout=30, input_text=None):
    """Bounded disk-backed output; never print raw Docker/credential output."""
    try:
        with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
            process = subprocess.Popen(argv, stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
                                       stdout=out, stderr=err)
            try:
                process.communicate(input_text.encode("utf-8") if input_text is not None else None, timeout=timeout)
            except (subprocess.TimeoutExpired, KeyboardInterrupt):
                process.kill()
                process.wait(timeout=5)
                raise LabError("command_timeout", "Owned Docker CLI command exceeded its deadline") from None
            out.seek(0); err.seek(0)
            stdout, stderr = out.read(MAX_OUTPUT + 1), err.read(MAX_OUTPUT + 1)
            if len(stdout) > MAX_OUTPUT or len(stderr) > MAX_OUTPUT:
                raise LabError("output_limit", "Command output exceeded its bound")
            return process.returncode, stdout.decode("utf-8", "replace"), stderr.decode("utf-8", "replace")
    except FileNotFoundError:
        raise LabError("docker_missing", "Docker CLI is not installed or not on PATH") from None


class DockerLab:
    def __init__(self, lab=LAB, context="default", runner=execute):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", context):
            raise LabError("invalid_context", "Invalid Docker context")
        self.lab, self.context, self.runner = Path(lab), context, runner
        self.docker = shutil.which("docker") or "docker"

    def command(self, args, timeout=30, input_text=None, check=True):
        code, out, _err = self.runner([self.docker, "--context", self.context, *args],
                                      timeout=timeout, input_text=input_text)
        if code and check:
            raise LabError("docker_command_failed", f"Docker {args[0]} failed (exit {code}); no raw output logged")
        return code, out

    def status(self):
        result = {"timestamp": now(), "docker_context": self.context,
                  "source_prepared": (self.lab / "source-manifest.json").is_file(),
                  "runtime_reproduction_verified": False, "provider_requests": 0}
        try:
            _, out = self.command(["version", "--format", "{{json .Server}}"], timeout=8)
            server = json.loads(out)
            result.update(docker_ready=bool(server), docker_version=server.get("Version") if server else None)
        except (LabError, json.JSONDecodeError) as exc:
            result.update(docker_ready=False, blocker=getattr(exc, "code", "invalid_docker_response"))
        return result

    def inspect_owned(self, *, allow_missing=False, name=CONTAINER):
        code, output = self.command(["inspect", name], check=False)
        if code:
            if allow_missing:
                # A missing container is acceptable only after daemon health
                # is proven, not when permissions/connection also failed.
                if self.status()["docker_ready"]:
                    _, names = self.command(["ps", "-a", "--format", "{{.Names}}"])
                    if name not in names.splitlines():
                        return None
            raise LabError("inspect_failed", "Cannot identify the lab container")
        rows = json.loads(output)
        if len(rows) != 1:
            raise LabError("identity_mismatch", "Expected one lab container")
        row = rows[0]
        labels = row.get("Config", {}).get("Labels", {})
        if labels.get("org.aura.lab") != PROJECT or labels.get("com.docker.compose.project") != PROJECT:
            raise LabError("ownership_mismatch", "Container is not owned by the AURA lab")
        if row.get("Name", "").lstrip("/") != name:
            raise LabError("identity_mismatch", "Unexpected lab container name")
        return row

    def setup(self, source_only=False):
        result = prepare_sources(self.lab)
        if source_only:
            return result
        if not self.status()["docker_ready"]:
            raise LabError("docker_unavailable", "Sources prepared; Linux Docker Engine is unavailable")
        for variant, directory in (("bad", "source"), ("reference", "private-reference/source")):
            self.command(["build", "--platform", "linux/amd64", "--file", str(ROOT / "infra/ops/linkding.Dockerfile"),
                          "--tag", IMAGES[variant], str(self.lab / directory)], timeout=900)
        config = json.loads(SOURCES.read_text(encoding="utf-8"))["linkding"]
        requirements = ROOT / "resources/ops/test-requirements.txt"
        profile = config["test_profile"]
        if requirements_sha(requirements) != profile["requirements_sha256"] or \
                file_sha(self.lab / "source/uv.lock") != profile["lock_sha256"]:
            raise LabError("test_dependency_mismatch", "Pinned test dependencies or upstream lockfile changed")
        context = self.lab / "test-image"
        context.mkdir(parents=True, exist_ok=True)
        (context / "test-requirements.txt").write_bytes(requirements.read_text(encoding="utf-8").encode("utf-8"))
        (context / ".dockerignore").write_text("*\n!test-requirements.txt\n", encoding="utf-8")
        self.command(["build", "--platform", "linux/amd64", "--file", str(ROOT / "infra/ops/linkding-test.Dockerfile"),
                      "--tag", IMAGES["test"], str(context)], timeout=900)
        identities = {variant: self.inspect_image(tag) for variant, tag in IMAGES.items()}
        write_json(self.lab / "images.json", {
            "revision": config["revision"], "images": identities,
            "source_manifest_sha256": file_sha(self.lab / "source-manifest.json"),
            "requirements_sha256": profile["requirements_sha256"],
            "built_at": now(), "runtime_reproduction_verified": False,
        })
        return dict(result, images_built=True, test_image_id=identities["test"])

    def inspect_image(self, reference):
        _, output = self.command(["image", "inspect", reference])
        rows = json.loads(output)
        if len(rows) != 1 or rows[0].get("Os") != "linux" or rows[0].get("Architecture") != "amd64" \
                or not re.fullmatch(r"sha256:[a-f0-9]{64}", rows[0].get("Id", "")):
            raise LabError("image_identity_invalid", "Expected one immutable Linux/amd64 image")
        return rows[0]["Id"]

    def built_images(self):
        path = self.lab / "images.json"
        if not path.is_file():
            raise LabError("images_not_prepared", "Run setup to build and record immutable lab/test images first")
        record = json.loads(path.read_text(encoding="utf-8"))
        config = json.loads(SOURCES.read_text(encoding="utf-8"))["linkding"]
        if record.get("revision") != config["revision"] or \
                record.get("source_manifest_sha256") != file_sha(self.lab / "source-manifest.json") or \
                record.get("requirements_sha256") != config["test_profile"]["requirements_sha256"]:
            raise LabError("build_provenance_changed", "Recorded build/source provenance no longer matches")
        if set(record.get("images", {})) != set(IMAGES) or not all(
                re.fullmatch(r"sha256:[a-f0-9]{64}", value) for value in record["images"].values()):
            raise LabError("image_identity_invalid", "Missing or invalid immutable image IDs")
        return record["images"]

    def target_profile(self, row, credentials, compose_path):
        """Export only public enrollment metadata; reference answers stay private."""
        images = self.built_images()
        if row.get("Image") != images["bad"]:
            raise LabError("source_image_mismatch", "Running image does not match the source snapshot build")
        provenance = json.loads((self.lab / "source-manifest.json").read_text(encoding="utf-8"))
        check_manifest(provenance)
        return {"target_id": "linkding-lab", "name": "Linkding",
            "container_name": CONTAINER, "docker_context": self.context,
            "source_dir": provenance["source_dir"], "source_revision": provenance["revision"],
            "source_manifest_sha256": file_sha(self.lab / "source-manifest.json"),
            "source_image_id": images["bad"], "deployment_root": "/etc/linkding",
            "deployment_launch": DEPLOYMENT_LAUNCH,
            "compose_file": str(Path(compose_path).resolve()), "compose_project": PROJECT, "compose_service": "linkding",
            "probes": [{"url": BASE_URL + "/health"}, {"url": BASE_URL + "/api/bookmarks/",
                        "credential_env": "AURA_LAB_API_TOKEN"}],
            "recovery_checks": [{"url": BASE_URL +
                f"/api/bookmarks/{credentials['bookmark_id']}/assets/{credentials['asset_id']}/download/",
                "credential_env": "AURA_LAB_API_TOKEN", "expected_bytes": credentials["expected_bytes"],
                "expected_sha256": credentials["expected_sha256"], "deadline_seconds": 120},
                {"url": BASE_URL + f"/assets/{credentials['asset_id']}",
                "auth_scheme": "Cookie", "credential_env": "AURA_LAB_SESSION_COOKIE",
                "expected_bytes": credentials["expected_bytes"],
                "expected_sha256": credentials["expected_sha256"], "deadline_seconds": 120}],
            "patch_profile": {"image": images["test"], "test_paths": TEST_PATHS,
                "settings_module": "bookmarks.settings.dev", "timeout_seconds": 180, "memory_mib": 512,
                "scratch_dirs": ["data"]},
            "monitoring": True, "policy": {"automatic_actions": [], "auto_investigate": False}}

    def compose(self, variant, memory_mib):
        if variant not in ("bad", "reference") or memory_mib not in range(128, 513):
            raise LabError("invalid_lab_config", "Use bad/reference and a memory limit of 128–512 MiB")
        return {"services": {"linkding": {
            "container_name": CONTAINER, "image": IMAGES[variant],
            "pull_policy": "never", "depends_on": ["gateway"],
            "mem_limit": f"{memory_mib}m", "memswap_limit": f"{memory_mib}m", "cpus": 1.0,
            "pids_limit": 64, "restart": "no", "labels": {"org.aura.lab": PROJECT},
            "environment": {"LD_DISABLE_BACKGROUND_TASKS": "True", "LD_SERVER_PORT": "9090"},
            "volumes": ["data:/etc/linkding/data"], "networks": ["isolated"],
            "logging": {"driver": "json-file", "options": {"max-size": "2m", "max-file": "2"}},
        }, "gateway": {
            "container_name": GATEWAY, "image": IMAGES["bad"], "pull_policy": "never",
            "entrypoint": ["python"], "command": ["/gateway.py"],
            "ports": ["127.0.0.1:18090:8080"], "networks": ["ingress", "isolated"],
            "volumes": [str(ROOT / "tools/ops/lab_gateway.py") + ":/gateway.py:ro"],
            "read_only": True, "cap_drop": ["ALL"], "security_opt": ["no-new-privileges:true"],
            "mem_limit": "64m", "memswap_limit": "64m", "pids_limit": 32, "cpus": 0.5,
            "restart": "no", "healthcheck": {"disable": True}, "labels": {"org.aura.lab": PROJECT},
            "logging": {"driver": "json-file", "options": {"max-size": "1m", "max-file": "1"}},
        }}, "volumes": {"data": {"labels": {"org.aura.lab": PROJECT}}},
            "networks": {"isolated": {"internal": True, "labels": {"org.aura.lab": PROJECT}},
                         "ingress": {"labels": {"org.aura.lab": PROJECT}}}}

    def start(self, variant="bad", memory_mib=256):
        images = self.built_images()
        if variant not in ("bad", "reference") or self.inspect_image(IMAGES[variant]) != images[variant]:
            raise LabError("image_identity_changed", "Lab image tag changed after setup")
        self.inspect_owned(allow_missing=True)
        self.inspect_owned(allow_missing=True, name=GATEWAY)
        compose_path = self.lab / "compose.json"
        compose = self.compose(variant, memory_mib)
        compose["services"]["linkding"]["image"] = images[variant]
        compose["services"]["gateway"]["image"] = images["bad"]
        write_json(compose_path, compose)
        self.command(["compose", "--project-name", PROJECT, "--file", str(compose_path),
                      "up", "--detach", "--no-build", "linkding"], timeout=120)
        row = self.inspect_owned()
        if row.get("Image") != images[variant]:
            raise LabError("image_identity_changed", "Started container does not use the recorded lab image")
        for _ in range(60):
            try:
                with urllib.request.urlopen(BASE_URL + "/health", timeout=2) as r:
                    if r.status == 200:
                        break
            except (urllib.error.URLError, TimeoutError, OSError):
                time.sleep(1)
        else:
            raise LabError("startup_failed", "Lab application did not become healthy in time")
        seed = (ROOT / "tools/ops/seed_linkding.py").read_text(encoding="utf-8")
        _, out = self.command(["exec", "-i", row["Id"], "python", "manage.py", "shell", "-c",
                               "exec(__import__('sys').stdin.read())"], timeout=120, input_text=seed)
        lines = [line for line in out.splitlines() if line.startswith("AURA_SEED=")]
        if len(lines) != 1:
            raise LabError("seed_failed", "Fixture did not produce one credential record")
        credentials = json.loads(lines[0].partition("=")[2])
        write_json(self.lab / "credentials.json", credentials)
        (self.lab / "credentials.env").write_text(
            "AURA_LAB_API_TOKEN=" + credentials["token"] + "\n" +
            "AURA_LAB_SESSION_COOKIE=" + credentials["session_cookie_name"] + "=" + credentials["sessionid"] + "\n",
            encoding="utf-8")
        metadata = {"variant": variant, "memory_mib": memory_mib, "container_id": row["Id"],
                    "image_id": row["Image"], "started_at": now(), "expected_bytes": credentials["expected_bytes"],
                    "expected_sha256": credentials["expected_sha256"], "compose_sha256": file_sha(compose_path)}
        write_json(self.lab / "active.json", metadata)
        write_json(self.lab / "fault-state.json", {"phase": "inactive"})
        if variant == "bad":
            write_json(self.lab / "target.json", self.target_profile(row, credentials, compose_path))
        return dict(metadata, url=BASE_URL, credentials_saved_locally=True, provider_requests=0)

    def stop(self):
        row = self.inspect_owned(allow_missing=True)
        gateway = self.inspect_owned(allow_missing=True, name=GATEWAY)
        if row is not None:
            self.command(["stop", "--time", "10", row["Id"]], timeout=20)
        if gateway is not None:
            self.command(["stop", "--time", "10", gateway["Id"]], timeout=20)
        return {"stopped": row is not None, "gateway_stopped": gateway is not None, "volumes_preserved": True, "provider_requests": 0}

    def _fault_context(self):
        active = json.loads((self.lab / "active.json").read_text(encoding="utf-8"))
        base = self.lab / "compose.json"
        if not active.get("compose_sha256") or file_sha(base) != active["compose_sha256"]:
            raise LabError("baseline_config_changed", "The enrolled baseline changed; do not replay a stale fixture")
        config = json.loads(base.read_text(encoding="utf-8"))
        if config.get("services", {}).get("linkding", {}).get("image") != active.get("image_id"):
            raise LabError("baseline_image_unpinned", "Restart lab setup/start to enroll an immutable baseline image")
        row = self.inspect_owned()
        if row.get("Image") != active["image_id"]:
            raise LabError("image_identity_changed", "The application image changed; this fixture cannot overwrite it")
        return active, base, row

    def fault(self, kind, memory_mib=96):
        if kind not in ("memory_limit", "port_mismatch") or memory_mib not in (64, 96, 128):
            raise LabError("invalid_fault", "Use memory_limit or port_mismatch, with 64/96/128 MiB fault memory")
        active, base, before = self._fault_context()
        state_path = self.lab / "fault-state.json"
        if state_path.is_file() and json.loads(state_path.read_text()).get("phase") in ("active", "dispatching"):
            raise LabError("fault_already_active", "Reset the current configuration fault before injecting another")
        if kind == "memory_limit" and memory_mib >= active["memory_mib"]:
            raise LabError("invalid_fault", "Fault memory must be lower than the baseline limit")
        service = {**DEPLOYMENT_LAUNCH}
        if kind == "memory_limit":
            service.update(mem_limit=f"{memory_mib}m", memswap_limit=f"{memory_mib}m")
        else:
            service["command"] = ["--http", "0.0.0.0:9091", "/etc/linkding/uwsgi.ini"]
            service["environment"] = {"LD_SERVER_PORT": "9091"}
        override = self.lab / "fault-override.json"
        write_json(override, {"services": {"linkding": service}})
        state = {"phase": "dispatching", "kind": kind, "timestamp": now(),
            "baseline_sha256": active["compose_sha256"], "before_container_id": before["Id"],
            "operator_induced_config_change": True, "provider_requests": 0}
        write_json(state_path, state)
        self.command(["compose", "--project-name", PROJECT, "--file", str(base), "--file", str(override),
            "up", "--detach", "--no-deps", "--no-build", "--pull", "never", "--force-recreate", "linkding"], timeout=120)
        after = self.inspect_owned()
        if after.get("Image") != active["image_id"]:
            raise LabError("image_identity_changed", "Fault recreation changed the expected image")
        state.update(phase="active", container_id=after["Id"], runtime_fault_verified=False)
        write_json(state_path, state)
        return state

    def reset_fault(self):
        active, base, _before = self._fault_context()
        self.command(["compose", "--project-name", PROJECT, "--file", str(base),
            "up", "--detach", "--no-deps", "--no-build", "--pull", "never", "--force-recreate", "linkding"], timeout=120)
        after = self.inspect_owned()
        if after.get("Image") != active["image_id"]:
            raise LabError("image_identity_changed", "Reset did not restore the expected baseline image")
        active["container_id"] = after["Id"]
        write_json(self.lab / "active.json", active)
        write_json(self.lab / "fault-state.json", {"phase": "inactive", "reset_at": now()})
        return {"baseline_reapplied": True, "recovery_verified": False,
                "container_id": after["Id"], "provider_requests": 0}

    def sample(self, container_id):
        # memory.events detects killed child workers even if Docker's init
        # survives. memory.peak retains short peaks missed by sampling.
        script = """import json,pathlib
p=pathlib.Path('/sys/fs/cgroup')
d={}
for n in ['memory.current','memory.peak','memory.max','memory.events']:
 try:d[n]=p.joinpath(n).read_text().strip()
 except OSError:d[n]=None
print(json.dumps(d))"""
        try:
            _, out = self.command(["exec", container_id, "python", "-c", script], timeout=4)
            data = json.loads(out)
            return {"timestamp": now(), "cgroup": data}
        except (LabError, json.JSONDecodeError):
            return {"timestamp": now(), "sample_error": "cgroup_unavailable"}

    def reproduce(self):
        fault_state = self.lab / "fault-state.json"
        if fault_state.is_file() and json.loads(fault_state.read_text()).get("phase") in ("active", "dispatching"):
            raise LabError("config_fault_active", "Reset the configuration fault before evaluating the historical buffering defect")
        row = self.inspect_owned()
        credentials = json.loads((self.lab / "credentials.json").read_text())
        active = json.loads((self.lab / "active.json").read_text())
        if active["container_id"] != row["Id"] or active["image_id"] != row["Image"]:
            raise LabError("identity_mismatch", "Lab instance changed since fixture setup")
        before = self.sample(row["Id"])
        started = now()
        report = {"timestamp": started, "variant": active["variant"], "source_revision":
            json.loads(SOURCES.read_text())["linkding"]["revision"],
            "container_id": row["Id"], "image_id": row["Image"], "memory_limit_mib": active["memory_mib"],
            "provider_requests": 0, "synthetic_workload": True, "historical_oss_regression": True,
            "samples": [before], "downloads": []}
        health = download(BASE_URL + "/api/bookmarks/", {"Authorization": "Token " + credentials["token"]}, max_bytes=MIB)
        report["before_probe"] = health
        if health["status_code"] != 200 or health["error"]:
            raise LabError("baseline_unhealthy", "Authenticated bookmarks request must succeed before reproduction")
        stop_sampling = threading.Event()

        def collect_samples():
            while not stop_sampling.wait(0.5):
                if len(report["samples"]) < 180:
                    report["samples"].append(self.sample(row["Id"]))

        sampler = threading.Thread(target=collect_samples, daemon=True)
        sampler.start()
        try:
            paths = [("ui", f"/assets/{credentials['asset_id']}",
                      {"Cookie": credentials["session_cookie_name"] + "=" + credentials["sessionid"]}),
                     ("api", f"/api/bookmarks/{credentials['bookmark_id']}/assets/{credentials['asset_id']}/download/",
                      {"Authorization": "Token " + credentials["token"]})]
            for route, path, headers in paths:
                result = download(BASE_URL + path, headers, max_bytes=credentials["expected_bytes"])
                result["route"] = route
                result["complete"] = (result["status_code"] == 200 and result["error"] is None
                    and result["bytes"] == credentials["expected_bytes"]
                    and result["sha256"] == credentials["expected_sha256"])
                report["downloads"].append(result)
                time.sleep(2)  # Observe actual worker recovery before the next request.
        finally:
            stop_sampling.set()
            sampler.join(timeout=6)
        after = self.sample(row["Id"])
        report["samples"].append(after)
        report["after_probe"] = download(BASE_URL + "/api/bookmarks/", {"Authorization": "Token " + credentials["token"]}, max_bytes=MIB)
        _, stdout, stderr = self.runner([self.docker, "--context", self.context,
            "logs", "--since", started, "--tail", "120", row["Id"]], timeout=10, input_text=None)
        logs = stdout + stderr
        for secret in (credentials["token"], credentials["sessionid"]):
            logs = logs.replace(secret, "[REDACTED]")
        report["logs"] = logs[-20000:]
        report.update(classify_result(before, after, report["downloads"], logs, report["after_probe"]))
        report["accepted"] = (report["reproduced_memory_failure"] if active["variant"] == "bad"
                              else report["verified_complete_transfer"])
        output = self.lab / "evidence" / (time.strftime("%Y%m%d-%H%M%S") + "-" + active["variant"] + ".json")
        write_json(output, report)
        return {"evidence": str(output), "accepted": report["accepted"], "variant": active["variant"],
                "reproduced_memory_failure": report["reproduced_memory_failure"],
                "verified_complete_transfer": report["verified_complete_transfer"], "provider_requests": 0}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def download(url, headers, max_bytes, *, timeout_seconds=30, deadline_seconds=60):
    digest, size, status, error = hashlib.sha256(), 0, None, None
    started = time.monotonic()
    try:
        request = urllib.request.Request(url, headers=headers)
        with urllib.request.build_opener(NoRedirect).open(request, timeout=timeout_seconds) as response:
            status = response.status
            while chunk := response.read(64 * 1024):
                if time.monotonic() - started > deadline_seconds:
                    raise TimeoutError()
                size += len(chunk)
                if size > max_bytes:
                    raise ValueError("response_size_limit")
                digest.update(chunk)
    except urllib.error.HTTPError as exc:
        status, error = exc.code, "http_error"
    except Exception as exc:
        # Never store URLs/headers/cookies or exception reprs containing them.
        error = type(exc).__name__
    return {"status_code": status, "bytes": size, "sha256": digest.hexdigest(),
            "duration_seconds": round(time.monotonic() - started, 3), "error": error}


def oom_count(sample):
    text = sample.get("cgroup", {}).get("memory.events")
    if text is None:
        return None
    try:
        counts = dict(line.split() for line in text.splitlines())
        return int(counts["oom_kill"])
    except (KeyError, ValueError):
        return None


def classify_result(before, after, downloads, logs, after_probe):
    first, last = oom_count(before), oom_count(after)
    oom_delta = last - first if first is not None and last is not None else None
    failed = any(not result.get("complete", False) for result in downloads)
    # SIGKILL alone does not prove an OOM. Require an independent cgroup delta.
    reproduced = bool(failed and oom_delta is not None and oom_delta > 0)
    verified = bool(len(downloads) == 2 and all(d.get("complete") for d in downloads)
                    and oom_delta == 0 and after_probe.get("status_code") == 200
                    and not after_probe.get("error"))
    return {"oom_kill_delta": oom_delta,
            "worker_sigkill_log_seen": bool(re.search(r"killed by signal 9|OOM|out of memory", logs, re.I)),
            "reproduced_memory_failure": reproduced, "verified_complete_transfer": verified}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("status", "setup", "start", "reproduce", "fault", "reset", "stop"), nargs="?", default="status")
    parser.add_argument("--context", default="default")
    parser.add_argument("--source-only", action="store_true")
    parser.add_argument("--variant", choices=("bad", "reference"), default="bad")
    parser.add_argument("--memory-mib", type=int, default=256)
    parser.add_argument("--kind", choices=("memory_limit", "port_mismatch"))
    parser.add_argument("--fault-memory-mib", type=int, choices=(64, 96, 128), default=96)
    args = parser.parse_args(argv)
    lab = DockerLab(context=args.context)
    try:
        if args.command == "status":
            result = lab.status()
        elif args.command == "setup":
            result = lab.setup(args.source_only)
        elif args.command == "start":
            result = lab.start(args.variant, args.memory_mib)
        elif args.command == "reproduce":
            result = lab.reproduce()
        elif args.command == "fault":
            result = lab.fault(args.kind, args.fault_memory_mib)
        elif args.command == "reset":
            result = lab.reset_fault()
        else:
            result = lab.stop()
        print(json.dumps(result, indent=2))
        return 0 if result.get("accepted", True) else 1
    except (LabError, OSError, ValueError, urllib.error.URLError) as exc:
        result = {"status": "blocked", "code": getattr(exc, "code", "lab_error"),
                  "message": str(exc) if isinstance(exc, LabError) else type(exc).__name__,
                  "provider_requests": 0, "runtime_reproduction_verified": False}
        if args.command != "status":
            write_json(lab.lab / "evidence" / "last-blocker.json", result)
        print(json.dumps(result, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
