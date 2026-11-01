"""Independent Healthchecks holdout; no model calls, default is read-only status."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import secrets
import sys
import time

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.ops.lab import DockerLab, LabError, ROOT, download, now, write_json

DIRECTORY = ROOT / ".local" / "ops-healthchecks"
PROVENANCE = ROOT / "resources/ops/healthchecks.json"
PROJECT = "aura-healthchecks-holdout"
CONTAINER = "aura-healthchecks-holdout"
GATEWAY = "aura-healthchecks-gateway"
BASE_URL = "http://127.0.0.1:18091"


class HealthchecksLab(DockerLab):
    def __init__(self, lab=DIRECTORY, context="default", **kwargs):
        super().__init__(lab, context, **kwargs)
        self.provenance = json.loads(PROVENANCE.read_text(encoding="utf-8"))

    def status(self):
        result = super().status()
        result.pop("source_prepared", None)
        result.pop("runtime_reproduction_verified", None)
        return {**result, "application": "Healthchecks", "portability_verified": False,
                "image": self.provenance["image"], "local_setup_recorded": (self.lab / "image.json").is_file()}

    def compose(self):
        # Engine 29 does not publish a port whose container sits only on an
        # internal network, so a fixed-destination sidecar carries localhost
        # ingress while the app keeps no published ports and no egress.
        return {"services": {"healthchecks": {
            "container_name": CONTAINER, "image": self.provenance["image"],
            "platform": "linux/amd64", "pull_policy": "never",
            "depends_on": ["gateway"], "restart": "no",
            "mem_limit": "512m", "memswap_limit": "512m", "cpus": 1.0, "pids_limit": 96,
            "labels": {"org.aura.holdout": PROJECT},
            "env_file": [str((self.lab / ".env").resolve())],
            "environment": {"DB": "sqlite", "DB_NAME": "/data/hc.sqlite", "DEBUG": "False",
                "SITE_ROOT": BASE_URL, "ALLOWED_HOSTS": "127.0.0.1,localhost", "REGISTRATION_OPEN": "False",
                "EMAIL_HOST": "", "GUNICORN_WORKERS": "1", "GUNICORN_PORT": "8000",
                "DEFAULT_FROM_EMAIL": "aura@example.invalid", "APPRISE_ENABLED": "False",
                "SHELL_ENABLED": "False", "USE_PAYMENTS": "False"},
            "volumes": ["data:/data"], "networks": ["isolated"],
            "logging": {"driver": "json-file", "options": {"max-size": "2m", "max-file": "2"}},
        }, "gateway": {
            "container_name": GATEWAY, "image": self.provenance["image"],
            "platform": "linux/amd64", "pull_policy": "never",
            "entrypoint": ["python"], "command": ["/gateway.py"],
            "ports": ["127.0.0.1:18091:8080"], "networks": ["ingress", "isolated"],
            "volumes": [str((ROOT / "tools/ops/healthchecks_gateway.py").resolve()) + ":/gateway.py:ro"],
            "read_only": True, "cap_drop": ["ALL"], "security_opt": ["no-new-privileges:true"],
            "mem_limit": "64m", "memswap_limit": "64m", "pids_limit": 32, "cpus": 0.5,
            "restart": "no", "healthcheck": {"disable": True}, "labels": {"org.aura.holdout": PROJECT},
            "logging": {"driver": "json-file", "options": {"max-size": "1m", "max-file": "1"}},
        }}, "volumes": {"data": {"labels": {"org.aura.holdout": PROJECT}}},
            "networks": {"isolated": {"internal": True, "labels": {"org.aura.holdout": PROJECT}},
                         "ingress": {"labels": {"org.aura.holdout": PROJECT}}}}

    def setup(self):
        if not self.status()["docker_ready"]:
            raise LabError("docker_unavailable", "A Linux Docker Engine is required for the Healthchecks holdout")
        self.lab.mkdir(parents=True, exist_ok=True)
        env = self.lab / ".env"
        if not env.exists():
            env.write_text("SECRET_KEY=" + secrets.token_urlsafe(48) + "\n", encoding="utf-8")
        elif not re.fullmatch(r"SECRET_KEY=[A-Za-z0-9_-]{32,100}\s*", env.read_text(encoding="utf-8")):
            raise LabError("invalid_local_secret", "Existing holdout environment file is not the expected local secret")
        self.command(["pull", "--platform", "linux/amd64", self.provenance["image"]], timeout=900)
        image_id = self.inspect_image(self.provenance["image"])
        write_json(self.lab / "image.json", {"image": self.provenance["image"], "image_id": image_id,
            "source_revision": self.provenance["revision"], "prepared_at": now()})
        write_json(self.lab / "compose.json", self.compose())
        return {"image_prepared": True, "image_id": image_id, "started": False,
                "provider_requests": 0, "portability_verified": False}

    def inspect_owned(self, *, allow_missing=False, name=CONTAINER):
        code, output = self.command(["inspect", name], check=False)
        if code:
            if allow_missing and self.status()["docker_ready"]:
                _, names = self.command(["ps", "-a", "--format", "{{.Names}}"])
                if name not in names.splitlines():
                    return None
            raise LabError("inspect_failed", "Cannot establish holdout container ownership")
        rows = json.loads(output)
        row = rows[0] if len(rows) == 1 else {}
        labels = row.get("Config", {}).get("Labels", {})
        if labels.get("org.aura.holdout") != PROJECT or labels.get("com.docker.compose.project") != PROJECT \
                or row.get("Name", "").lstrip("/") != name \
                or not re.fullmatch(r"[a-f0-9]{64}", row.get("Id", "")):
            raise LabError("ownership_mismatch", "Refusing to modify a container not owned by this holdout")
        return row

    def target_profile(self, credentials):
        return {"target_id": "healthchecks-holdout", "name": "Healthchecks",
            "container_name": CONTAINER, "docker_context": self.context,
            "compose_file": str((self.lab / "compose.json").resolve()),
            "compose_project": PROJECT, "compose_service": "healthchecks",
            "probes": [{"url": BASE_URL + "/health"},
                {"url": BASE_URL + credentials["checks_path"], "auth_scheme": "Cookie",
                 "credential_env": "AURA_TARGET_HEALTHCHECKS_COOKIE"}],
            "monitoring": True, "policy": {"automatic_actions": [], "auto_investigate": False}}

    def start(self):
        record_path = self.lab / "image.json"
        if not record_path.is_file():
            raise LabError("setup_required", "Run Healthchecks setup before start")
        record = json.loads(record_path.read_text(encoding="utf-8"))
        if record.get("image") != self.provenance["image"] or \
                self.inspect_image(self.provenance["image"]) != record.get("image_id"):
            raise LabError("image_identity_changed", "Holdout image differs from setup provenance")
        self.inspect_owned(allow_missing=True)
        self.inspect_owned(allow_missing=True, name=GATEWAY)
        write_json(self.lab / "compose.json", self.compose())
        self.command(["compose", "--project-name", PROJECT, "--file", str(self.lab / "compose.json"),
                      "up", "--detach", "--no-build", "healthchecks", "gateway"], timeout=120)
        row = self.inspect_owned()
        gateway = self.inspect_owned(name=GATEWAY)
        if row.get("Image") != record["image_id"]:
            raise LabError("image_identity_changed", "Started holdout uses an unexpected image")
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            probe = download(BASE_URL + "/health", {}, max_bytes=65536, timeout_seconds=2, deadline_seconds=3)
            if probe["status_code"] == 200 and not probe["error"]:
                break
            time.sleep(1)
        else:
            raise LabError("startup_failed", "Healthchecks did not pass its database health probe")
        seed = (ROOT / "tools/ops/seed_healthchecks.py").read_text(encoding="utf-8")
        _, output = self.command(["exec", "-i", row["Id"], "python", "/opt/healthchecks/manage.py", "shell", "-c",
            "exec(__import__('sys').stdin.read())"], timeout=45, input_text=seed)
        records = [line.partition("=")[2] for line in output.splitlines() if line.startswith("AURA_HEALTHCHECKS=")]
        if len(records) != 1:
            raise LabError("seed_failed", "Holdout did not produce one local credential record")
        credentials = json.loads(records[0])
        if not re.fullmatch(r"/projects/[a-f0-9-]{36}/checks/", credentials.get("checks_path", "")):
            raise LabError("seed_invalid", "Holdout project URL was invalid")
        write_json(self.lab / "credentials.json", credentials)
        (self.lab / "credentials.env").write_text(
            "AURA_TARGET_HEALTHCHECKS_COOKIE=" + credentials["cookie"] + "\n", encoding="utf-8")
        target = self.target_profile(credentials)
        check = download(target["probes"][1]["url"], {"Cookie": credentials["cookie"]}, max_bytes=2 * 1024 * 1024)
        if check["status_code"] != 200 or check["error"]:
            raise LabError("authenticated_probe_failed", "Healthchecks project view could not be verified")
        write_json(self.lab / "target.json", target)
        evidence = {"timestamp": now(), "container_id": row["Id"], "image_id": row["Image"],
            "gateway_container_id": gateway["Id"], "gateway_image_id": gateway["Image"],
            "health_probe": probe, "authenticated_probe": check, "provider_requests": 0,
            "connected_application_verified": True, "restart_verified": False,
            "portability_verified": False}
        write_json(self.lab / "evidence/start.json", evidence)
        return {"url": BASE_URL, "target_file": str(self.lab / "target.json"),
                "provider_requests": 0, "connected_application_verified": True,
                "restart_verified": False, "credentials_saved_locally": True}

    def stop(self):
        row = self.inspect_owned(allow_missing=True)
        gateway = self.inspect_owned(allow_missing=True, name=GATEWAY)
        if row:
            self.command(["stop", "--time", "10", row["Id"]], timeout=20)
        if gateway:
            self.command(["stop", "--time", "10", gateway["Id"]], timeout=20)
        return {"stopped": bool(row), "gateway_stopped": bool(gateway),
                "volumes_preserved": True, "provider_requests": 0}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("status", "setup", "start", "stop"), nargs="?", default="status")
    parser.add_argument("--context", default="default")
    args = parser.parse_args(argv)
    lab = HealthchecksLab(context=args.context)
    try:
        print(json.dumps(getattr(lab, args.command)(), indent=2))
        return 0
    except (LabError, OSError, ValueError) as exc:
        result = {"status": "blocked", "code": getattr(exc, "code", "holdout_error"),
            "message": str(exc) if isinstance(exc, LabError) else type(exc).__name__,
            "provider_requests": 0, "portability_verified": False}
        if args.command != "status":
            write_json(lab.lab / "evidence/last-blocker.json", result)
        print(json.dumps(result, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
