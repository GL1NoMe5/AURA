"""Bounded read-only smoke for the Healthchecks holdout. No mutations, no secrets in output."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.ops.healthchecks import BASE_URL, GATEWAY, HealthchecksLab
from tools.ops.lab import LabError, download, now, write_json

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", default="default")
    args = parser.parse_args(argv)
    lab = HealthchecksLab(context=args.context)
    try:
        from aura_ops.models import TargetSpec
        target = TargetSpec.model_validate(json.loads((lab.lab / "target.json").read_text(encoding="utf-8")))
        credentials = json.loads((lab.lab / "credentials.json").read_text(encoding="utf-8"))
        cookie = credentials.get("cookie", "")
        if target.target_id != "healthchecks-holdout" or not cookie:
            raise LabError("smoke_not_enrolled", "Holdout target or local credentials are missing")
        app = lab.inspect_owned()
        gateway = lab.inspect_owned(name=GATEWAY)
        health = download(BASE_URL + "/health", {}, max_bytes=65536,
                          timeout_seconds=5, deadline_seconds=10)
        checks = download(BASE_URL + credentials["checks_path"], {"Cookie": cookie},
                          max_bytes=2 * 1024 * 1024, timeout_seconds=10, deadline_seconds=20)
        healthy = (health["status_code"] == 200 and not health["error"]
                   and checks["status_code"] == 200 and not checks["error"]
                   and (app.get("State") or {}).get("Running") is True
                   and (gateway.get("State") or {}).get("Running") is True)
        result = {"timestamp": now(), "target_id": target.target_id,
                  "docker_context": target.docker_context, "url": BASE_URL,
                  "container_id": app["Id"], "image_id": app["Image"],
                  "gateway_container_id": gateway["Id"], "gateway_image_id": gateway["Image"],
                  "app_running": (app.get("State") or {}).get("Running") is True,
                  "gateway_running": (gateway.get("State") or {}).get("Running") is True,
                  "health_probe": health, "authenticated_probe": checks,
                  "healthy": healthy, "provider_requests": 0}
        if cookie and cookie in json.dumps(result):
            raise LabError("smoke_leak", "Smoke output must never carry credentials")
        write_json(lab.lab / "evidence/smoke.json", result)
        print(json.dumps(result, indent=2))
        return 0 if healthy else 2
    except (LabError, OSError, ValueError) as exc:
        result = {"status": "blocked", "code": getattr(exc, "code", "smoke_error"),
                  "message": str(exc) if isinstance(exc, LabError) else type(exc).__name__,
                  "provider_requests": 0, "healthy": False}
        print(json.dumps(result, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
