"""Disposable Linkding instance. Fixed commands, owned IDs, no visitor arguments."""
from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path

from aura_ops.docker import run_command
from aura_ops.models import OpsError, TargetSpec

LABEL = "org.aura.judging-demo"
LAUNCH = {"entrypoint": ["/etc/linkding/.venv/bin/uwsgi"],
          "command": ["--http", "0.0.0.0:9090", "/etc/linkding/uwsgi.ini"]}


class LinkdingFixture:
    def __init__(self, settings, owner, run_id):
        if not re.fullmatch(r"[a-f0-9]{32}", run_id) or not re.fullmatch(r"[a-f0-9]{32}", owner):
            raise ValueError("Invalid owner identity")
        self.settings, self.run_id = settings, run_id
        self.label = owner + ":" + run_id
        self.base = "aura-judge-" + run_id
        self.folder = settings.data / "runs" / run_id
        self.image_id = None
        self.credential_env = "AURA_DEMO_PROBE_TOKEN"

    async def command(self, *args, timeout=30, missing=False):
        result = await run_command(["docker", "--context", self.settings.context, *args], timeout=timeout)
        if result.returncode:
            if missing and ("no such" in result.stderr.lower() or "not found" in result.stderr.lower()):
                return None
            raise OpsError("demo_docker_failed", "The isolated application could not be prepared or cleaned up.", 503)
        return result.stdout.strip()

    async def inspect(self, kind, name, *, missing=False):
        out = await self.command(kind, "inspect", name, missing=missing)
        if out is None:
            return None
        rows = json.loads(out)
        if not isinstance(rows, list) or len(rows) != 1:
            raise OpsError("demo_identity", "Could not verify the test application identity.", 503)
        return rows[0]

    def owned(self, kind, row):
        labels = (row.get("Config") or {}).get("Labels") if kind == "container" else row.get("Labels")
        if not isinstance(labels, dict) or labels.get(LABEL) != self.label:
            raise OpsError("demo_ownership", "Resource ownership could not be verified.", 409)

    async def app(self):
        row = await self.inspect("container", self.base)
        self.owned("container", row)
        if self.image_id and row["Image"] != self.image_id:
            raise OpsError("demo_image_changed", "Test application image changed.", 409)
        return row

    async def provision(self):
        server = json.loads(await self.command("version", "--format", "{{json .Server}}"))
        if server.get("Os") != "linux":
            raise OpsError("demo_linux_required", "This demonstration requires Linux containers.", 503)
        image = await self.inspect("image", self.settings.image)
        self.image_id = image["Id"]
        self.folder.mkdir(parents=True, exist_ok=False)
        for suffix, flags in (("-internal", ["--internal"]), ("-ingress", [])):
            await self.command("network", "create", "--label", LABEL + "=" + self.label, *flags, self.base + suffix)
        await self.command("volume", "create", "--label", LABEL + "=" + self.label, self.base + "-data")
        # Migrations seed only this new disposable volume, never a connected app.
        common = ["run", "--name", self.base + "-init", "--label", LABEL + "=" + self.label,
                  "--network", "none", "--memory", "512m", "--memory-swap", "512m", "--cpus", "1",
                  "--pids-limit", "64", "--security-opt", "no-new-privileges:true",
                  "--volume", self.base + "-data:/etc/linkding/data", "--entrypoint", "python"]
        await self.command(*common, self.image_id, "manage.py", "migrate", "--noinput", timeout=120)
        await self.remove_container(self.base + "-init")
        seed = str((self.settings.root / "tools/demo/seed.py").resolve())
        output = await self.command(*common, "--volume", seed + ":/aura-seed.py:ro",
                                    self.image_id, "manage.py", "shell", "-c",
                                    "exec(open('/aura-seed.py').read())", timeout=60)
        await self.remove_container(self.base + "-init")
        tokens = [line.removeprefix("AURA_TOKEN=") for line in output.splitlines() if line.startswith("AURA_TOKEN=")]
        if len(tokens) != 1 or not re.fullmatch(r"[A-Za-z0-9]{20,100}", tokens[0]):
            raise OpsError("demo_seed_failed", "Could not initialize test data.", 503)
        os.environ[self.credential_env] = tokens[0]
        compose = self.compose()
        (self.folder / "compose.json").write_text(json.dumps(compose, indent=2), encoding="utf-8")
        (self.folder / "fault.json").write_text(json.dumps({"services": {"linkding": {
            "command": ["--http", "0.0.0.0:9091", "/etc/linkding/uwsgi.ini"],
            "environment": {"LD_SERVER_PORT": "9091"}}}}), encoding="utf-8")
        await self.command("compose", "-p", self.base, "-f", str(self.folder / "compose.json"),
                           "up", "-d", "--no-build", "--pull", "never", timeout=120)
        await self.app()
        return TargetSpec(target_id="linkding", name="Linkding", docker_context=self.settings.context,
                          container_name=self.base, compose_file=str(self.folder / "compose.json"),
                          compose_project=self.base, compose_service="linkding", deployment_launch=LAUNCH,
                          probes=[{"url": f"http://127.0.0.1:{self.settings.port}/health"},
                                  {"url": f"http://127.0.0.1:{self.settings.port}/api/bookmarks/",
                                   "credential_env": self.credential_env}])

    def compose(self):
        limits = {"restart": "no", "labels": {LABEL: self.label}, "pull_policy": "never",
                  "logging": {"driver": "json-file", "options": {"max-size": "1m", "max-file": "1"}}}
        return {"services": {
            "linkding": {**limits, **LAUNCH, "container_name": self.base, "image": self.image_id,
                         "mem_limit": "512m", "memswap_limit": "512m", "cpus": 1, "pids_limit": 64,
                         "security_opt": ["no-new-privileges:true"], "networks": ["internal"],
                         "environment": {"LD_DISABLE_BACKGROUND_TASKS": "True", "LD_SERVER_PORT": "9090"},
                         "volumes": ["data:/etc/linkding/data"]},
            "gateway": {**limits, "container_name": self.base + "-gateway", "image": self.image_id,
                        "entrypoint": ["python"], "command": ["/gateway.py"],
                        "mem_limit": "64m", "memswap_limit": "64m", "cpus": .5, "pids_limit": 32,
                        "read_only": True, "cap_drop": ["ALL"], "security_opt": ["no-new-privileges:true"],
                        "healthcheck": {"disable": True}, "networks": ["internal", "ingress"],
                        "ports": [f"127.0.0.1:{self.settings.port}:8080"],
                        "volumes": [str((self.settings.root / "tools/ops/lab_gateway.py").resolve()) + ":/gateway.py:ro"]}},
            "volumes": {"data": {"external": True, "name": self.base + "-data"}},
            "networks": {name: {"external": True, "name": self.base + "-" + name} for name in ("internal", "ingress")}}

    async def fault(self):
        await self.app()
        await self.command("compose", "-p", self.base, "-f", str(self.folder / "compose.json"),
                           "-f", str(self.folder / "fault.json"), "up", "-d", "--no-deps",
                           "--no-build", "--pull", "never", "--force-recreate", "linkding", timeout=120)
        row = await self.app()
        if "LD_SERVER_PORT=9091" not in row["Config"].get("Env", []) or not row["State"]["Running"]:
            raise OpsError("demo_fault_failed", "The intended test problem could not be confirmed.", 503)

    async def remove_container(self, name):
        row = await self.inspect("container", name, missing=True)
        if row:
            self.owned("container", row)
            await self.command("container", "rm", "-f", row["Id"])

    async def cleanup(self):
        failures = []
        for suffix in ("", "-gateway", "-init"):
            try:
                await self.remove_container(self.base + suffix)
            except Exception:
                failures.append("container" + suffix)
        for kind, suffix in (("network", "-internal"), ("network", "-ingress"), ("volume", "-data")):
            try:
                row = await self.inspect(kind, self.base + suffix, missing=True)
                if row:
                    self.owned(kind, row)
                    await self.command(kind, "rm", row.get("Id") or row["Name"])
            except Exception:
                failures.append(kind)
        os.environ.pop(self.credential_env, None)
        return failures
