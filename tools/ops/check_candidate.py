"""Independent HTTP/load check of the pending linkding candidate in a cloned lab.

Does not approve a proposal or modify the registered application's volume/image.
Creates uniquely labelled temporary Docker resources, verifies identity before
cleanup, and preserves evidence. No provider calls or model-produced commands.
"""
from __future__ import annotations
import asyncio
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import uuid
from urllib.parse import urlsplit, urlunsplit

import httpx

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "backend")]
from aura_ops.credentials import load_probe_credentials
from aura_ops.models import TargetSpec
from aura_ops.redaction import redact_text, is_sensitive_key
from aura_ops.verification import verify_functional
from tools.ops.lab import DockerLab

COPY = """
from pathlib import Path
import shutil,sqlite3,hashlib,os,json
source=Path(globals().get('SOURCE','/source')); dest=Path(globals().get('DEST','/dest'))
databases=[p for p in source.rglob('*') if p.suffix in ('.sqlite3','.sqlite')]
def fingerprints(root=source):
    result={}
    for db in root.rglob('*'):
        if db.suffix not in ('.sqlite3','.sqlite'): continue
        for p in (db,Path(str(db)+'-wal')):
            if p.exists():
                if p.is_symlink(): raise RuntimeError('Database symlink rejected')
                h=hashlib.sha256()
                with p.open('rb') as f:
                    for block in iter(lambda:f.read(65536),b''): h.update(block)
                digest=h.hexdigest()
                # An empty WAL contains no committed frames. SQLite may remove
                # it on close; every non-empty WAL and database remains exact.
                if p.name.endswith('-wal') and digest==hashlib.sha256(b'').hexdigest(): continue
                result[str(p.relative_to(root))]=digest
    return result
before=fingerprints()
for p in source.rglob('*'):
    if p.is_symlink(): raise RuntimeError('Source symlink rejected')
    rel=p.relative_to(source); q=dest/rel
    if p.is_dir(): q.mkdir(exist_ok=True)
    elif p.name.endswith('-shm'): continue
    else: shutil.copy2(p,q)
after=fingerprints()
copied=fingerprints(dest)
if not before or before!=after: raise RuntimeError('Source database/WAL changed while copying')
if before!=copied: raise RuntimeError('Cloned database/WAL differs before SQLite validation')
for p in databases:
    db=sqlite3.connect(str(dest/p.relative_to(source)))
    try:
        if db.execute('PRAGMA integrity_check').fetchone()[0]!='ok': raise RuntimeError('Cloned SQLite is inconsistent')
    finally: db.close()
# Normal SQLite validation includes WAL recovery/checkpointing on the COPY.
# Compare raw bytes BEFORE opening it, never after that legitimate transformation.
if before!=fingerprints(): raise RuntimeError('Source database/WAL changed during validation')
for q in [dest,*dest.rglob('*')]:
    p=source/q.relative_to(dest)
    owner=p if p.exists() else p.parent
    stat=owner.stat()
    if p.exists(): os.chmod(q,stat.st_mode & 0o777)
    if hasattr(os,'chown'): os.chown(q,stat.st_uid,stat.st_gid)
print('ACCEPT_BEFORE='+json.dumps(before,sort_keys=True))
print('ACCEPT_AFTER='+json.dumps({'source':after,'dest':copied},sort_keys=True))
"""
FDS = """
from pathlib import Path
count=0
for p in Path('/proc').glob('[0-9]*/fd/*'):
    try:
        if str(p.resolve()).startswith('/etc/linkding/data/assets/'): count+=1
    except OSError: pass
print(count)
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cycles", type=int, choices=(1, 2, 3), default=3)
    options = parser.parse_args()
    run = "aura-candidate-" + uuid.uuid4().hex[:12]
    folder = ROOT / ".local/ops/evidence" / run
    folder.mkdir()
    tempfile.tempdir = str(folder)
    os.environ["PATH"] = str(ROOT / ".local/environment/Docker/resources/bin") + os.pathsep + os.environ.get("PATH", "")
    load_probe_credentials(ROOT)
    lab = DockerLab(folder, "desktop-linux")
    def cmd(*args, timeout=30):
        code, out, err = lab.runner([lab.docker, "--context", lab.context, *args], timeout=timeout)
        if code:
            secrets = [v for k, v in os.environ.items() if is_sensitive_key(k)]
            (folder / "command-error.log").write_text(redact_text(out + err, secrets), encoding="utf-8")
            raise RuntimeError(f"Docker {args[0]} failed; diagnostic saved in {folder.name}")
        return out.strip()
    def inspect(kind, name): return json.loads(cmd(kind, "inspect", name))[0]
    def rewrite(url):
        p = urlsplit(url)
        assert p.hostname == "127.0.0.1" and p.port == 18090
        return urlunsplit((p.scheme, "127.0.0.1:18092", p.path, "", ""))
    with httpx.Client(base_url="http://127.0.0.1:8000", timeout=15) as client:
        proposals = [p for p in client.get("/api/v2/proposals").json()
                     if p["status"] == "proposed" and p["action"] == "deploy_verified_patch"
                     and p["target_id"] == "linkding-lab"]
        assert len(proposals) == 1, "Exactly one pending linkding deployment is required"
        proposal = proposals[0]
        target = client.get("/api/v2/targets/linkding-lab").json()
    original = TargetSpec.model_validate({k: v for k, v in target.items() if k in TargetSpec.model_fields})
    source = inspect("container", original.container_name)
    assert source["Id"] == proposal["expected_identity"]["container_id"]
    assert source["Image"] == proposal["base_image_id"]
    assert source["State"]["StartedAt"] == proposal["expected_identity"]["started_at"]
    assert source["Config"]["Labels"].get("org.aura.lab") == "aura-ops-lab"
    mounts = [m for m in source["Mounts"] if m["Destination"] == "/etc/linkding/data" and m["Type"] == "volume"]
    assert len(mounts) == 1
    source_volume = mounts[0]["Name"]
    assert inspect("volume", source_volume)["Labels"].get("org.aura.lab") == "aura-ops-lab"
    candidate_image = inspect("image", proposal["candidate_image_id"])
    assert candidate_image["Config"]["Labels"]["aura.patch.id"] == proposal["patch_id"]
    label = "org.aura.candidate=" + run
    created = []
    report = {"run": run, "proposal_id": proposal["proposal_id"], "proposal_hash": proposal["proposal_hash"],
              "candidate_image": proposal["candidate_image_id"], "source_identity": proposal["expected_identity"],
              "cycles_requested": options.cycles,
              "provider_requests": 0, "registered_target_mutations": 0, "cycles": [], "passed": False,
              "fixture": "cloned database and assets; synthetic 500 MiB workload", "cleanup": []}
    def create_container(name, args):
        cid = cmd("container", "create", "--name", name, "--label", label, *args)
        created.append(("container", cid))
        return cid
    try:
        internal, ingress, volume = run + "-internal", run + "-ingress", run + "-data"
        for name, extra in ((internal, ["--internal"]), (ingress, [])):
            nid = cmd("network", "create", "--label", label, *extra, name)
            created.append(("network", nid))
        cmd("volume", "create", "--label", label, volume)
        created.append(("volume", volume))
        copier = create_container(run + "-copy", ["--network", "none", "--read-only", "--cap-drop", "ALL",
            "--cap-add", "CHOWN",
            "--security-opt", "no-new-privileges", "--memory", "128m", "--memory-swap", "128m", "--pids-limit", "32",
            "--mount", f"type=volume,source={source_volume},target=/source,readonly",
            "--mount", f"type=volume,source={volume},target=/dest", "--entrypoint", "python",
            proposal["base_image_id"], "-c", COPY])
        cmd("container", "start", "--attach", copier, timeout=120)
        assert inspect("container", copier)["State"]["ExitCode"] == 0
        launch = proposal["deployment_launch"]
        assert len(launch["entrypoint"]) == 1
        app = create_container(run + "-app", ["--network", internal, "--network-alias", "candidate",
            "--read-only", "--tmpfs", "/tmp:rw,nosuid,nodev,size=32m", "--cap-drop", "ALL",
            "--cap-add", "SETUID", "--cap-add", "SETGID",
            "--security-opt", "no-new-privileges", "--memory", "256m", "--memory-swap", "256m",
            "--cpus", "1", "--pids-limit", "64", "--env", "LD_DISABLE_BACKGROUND_TASKS=True",
            "--env", "PYTHONDONTWRITEBYTECODE=1", "--env", "LD_SERVER_PORT=9090",
            "--mount", f"type=volume,source={volume},target=/etc/linkding/data",
            "--entrypoint", launch["entrypoint"][0], proposal["candidate_image_id"], *launch["command"]])
        relay = (ROOT / "tools/ops/lab_gateway.py").read_text("utf-8")
        assert relay.count("'linkding'") == 1
        gateway_file = folder / "gateway.py"
        gateway_file.write_text(relay.replace("'linkding'", "'candidate'"), encoding="utf-8")
        gateway = create_container(run + "-gateway", ["--network", ingress, "--publish", "127.0.0.1:18092:8080",
            "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--memory", "64m",
            "--memory-swap", "64m", "--pids-limit", "32", "--cpus", "0.5",
            "--mount", f"type=bind,source={gateway_file},target=/gateway.py,readonly", "--entrypoint", "python",
            proposal["candidate_image_id"], "/gateway.py"])
        cmd("network", "connect", internal, gateway)
        cmd("container", "start", app, gateway)
        startup_deadline = time.monotonic() + 60
        with httpx.Client(trust_env=False, timeout=3) as client:
            for attempt in range(40):
                if not inspect("container", app)["State"]["Running"]:
                    raise RuntimeError("Candidate exited during startup")
                if time.monotonic() >= startup_deadline: break
                try:
                    if client.get("http://127.0.0.1:18092/health").status_code == 200: break
                except httpx.HTTPError: pass
                time.sleep(1)
            else: raise RuntimeError("Candidate did not become healthy")
            assert time.monotonic() < startup_deadline, "Candidate startup exceeded deadline"
        values = original.model_dump(mode="json")
        values.update(target_id=run, container_name=run + "-app")
        for check in [*values["probes"], *values["recovery_checks"]]: check["url"] = rewrite(check["url"])
        spec = TargetSpec.model_validate(values)
        for cycle in range(options.cycles):
            result = asyncio.run(verify_functional(spec, asyncio.Event()))
            report["cycles"].append(result)
            (folder / "result.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
            print(json.dumps({"cycle": cycle + 1, "verified": result["verified"], "reason": result["reason"],
                              "peak_bytes": result["sampled_peak_memory_bytes"]}), flush=True)
            if not result["verified"]: raise RuntimeError("Independent HTTP/memory check failed")
        before_fds = int(cmd("exec", app, "python", "-c", FDS))
        with httpx.Client(trust_env=False, timeout=15) as client:
            for _ in range(5):
                for check in spec.recovery_checks:
                    secret = os.environ[check.credential_env]
                    headers = {"Cookie": secret} if check.auth_scheme == "Cookie" else {"Authorization": f"{check.auth_scheme} {secret}"}
                    with client.stream("GET", check.url, headers=headers) as response:
                        assert response.status_code == 200
                        next(response.iter_bytes(65536))
            time.sleep(2)
            assert client.get("http://127.0.0.1:18092/health").status_code == 200
        after_fds = int(cmd("exec", app, "python", "-c", FDS))
        report["aborted_downloads"] = {"count": 10, "asset_fds_before": before_fds, "asset_fds_after": after_fds}
        assert after_fds <= before_fds, "Open asset descriptors grew after aborted downloads"
        final_source = inspect("container", original.container_name)
        assert final_source["Id"] == source["Id"] and final_source["Image"] == source["Image"]
        assert final_source["State"]["StartedAt"] == source["State"]["StartedAt"]
        report["passed"] = True
    except Exception as exc:
        report["error_type"] = type(exc).__name__
        for kind, identifier in created:
            if kind == "container":
                code, out, err = lab.runner([lab.docker, "--context", lab.context, "logs", "--tail", "40", identifier], timeout=10)
                secrets = [v for k, v in os.environ.items() if is_sensitive_key(k)]
                (folder / (identifier[:12] + ".log")).write_text(redact_text(out + err, secrets), encoding="utf-8")
        raise
    finally:
        # Containers first, then only this run's labelled networks and cloned volume.
        for kind in ("container", "network", "volume"):
            for resource_kind, identifier in reversed(created):
                if resource_kind != kind: continue
                try:
                    obj = inspect(kind, identifier)
                    labels = obj.get("Config", {}).get("Labels", {}) if kind == "container" else obj.get("Labels", {})
                    assert labels.get("org.aura.candidate") == run, "Cleanup ownership mismatch"
                    cmd(kind, "rm", *(["--force"] if kind == "container" else []), identifier)
                    report["cleanup"].append({"kind": kind, "id": identifier, "removed": True})
                except Exception:
                    report["cleanup"].append({"kind": kind, "id": identifier, "removed": False})
                    report["passed"] = False
        (folder / "result.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps({"passed": report["passed"], "evidence": str(folder / "result.json")}), flush=True)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
