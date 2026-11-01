"""Explicit real-Docker check of only newly created demo resources. No model calls."""
import argparse
import asyncio
import json
from pathlib import Path
import secrets
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
from aura_demo.config import DemoSettings
from aura_demo.fixture import LinkdingFixture
from aura_ops.docker import DockerTarget
from aura_ops.service import OpsService
from aura_ops.store import OpsStore


async def check(context):
    rid = uuid.uuid4().hex
    settings = DemoSettings(root=ROOT, data=ROOT / ".local/demo-checks" / rid,
                            origin="http://127.0.0.1:8019", access_code=secrets.token_urlsafe(32), context=context)
    fixture = LinkdingFixture(settings, secrets.token_hex(16), rid)
    result = {"provider_requests": 0, "passed": False, "run_id": rid}
    service = None
    try:
        spec = await fixture.provision()
        deadline = time.monotonic() + 75
        while True:
            observed = await DockerTarget(spec).collect()
            result["last_observation"] = observed
            if observed.get("healthy"):
                break
            if time.monotonic() > deadline:
                raise RuntimeError("baseline_unhealthy")
            await asyncio.sleep(2)
        service = OpsService(ROOT, store=OpsStore(fixture.folder / "ops"))
        await service.enroll(spec)
        await service.capture_snapshot(spec.target_id)
        result["before"] = {"healthy": observed["healthy"], "probes": observed["probes"]}
        await fixture.fault()
        observed = await DockerTarget(spec).collect()
        result["fault"] = {"healthy": observed["healthy"], "probes": observed["probes"], "config": observed["config"]}
        result["passed"] = observed["healthy"] is False and observed["config"]["server_port"] == 9091
    except Exception as exc:
        result["error"] = getattr(exc, "code", type(exc).__name__)
        if isinstance(exc, RuntimeError) and str(exc) == "baseline_unhealthy":
            result["error"] = "baseline_unhealthy"
    finally:
        if service:
            await service.shutdown()
        result["cleanup_failures"] = await fixture.cleanup()
        result["passed"] = result["passed"] and not result["cleanup_failures"]
        settings.data.mkdir(parents=True, exist_ok=True)
        (settings.data / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps({"passed": result["passed"], "error": result.get("error"),
                      "cleanup_failures": result["cleanup_failures"], "evidence": str(settings.data / "result.json")}))
    return result["passed"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="Actually create and remove a disposable Linkding")
    parser.add_argument("--context", default="default")
    args = parser.parse_args()
    if not args.run:
        parser.error("Use --run to explicitly authorize the disposable fixture check")
    raise SystemExit(0 if asyncio.run(check(args.context)) else 1)
