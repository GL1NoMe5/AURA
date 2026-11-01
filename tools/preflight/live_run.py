"""Bounded opt-in live incident harness for INTEGRATION-001.

DEFAULT IS OFFLINE: without --live this script makes zero provider
requests, starts no incident, and only records configuration readiness.
With --live it runs AT MOST ONE real incident through the shipped
default app (real manager/worker/orchestrator), never retries a whole
run, always shuts down, checks owned-tree cleanup, and saves measured
evidence under .local/integration without any secrets.

Usage (from project root):
    backend\\.venv\\Scripts\\python.exe tools\\preflight\\live_run.py
    backend\\.venv\\Scripts\\python.exe tools\\preflight\\live_run.py --live
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import psutil

_REPO_ROOT = Path(__file__).resolve().parents[2]
_BACKEND = _REPO_ROOT / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

DEFAULT_OUTPUT = _REPO_ROOT / ".local" / "integration" / "live-run.json"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _offline_record(output: Path) -> dict:
    """Zero-request default mode: readiness facts only, no app start."""
    from aura_agents.adapter import load_api_key
    from aura_agents.chart import render_measured_chart  # noqa: F401

    key_present = load_api_key(_REPO_ROOT) is not None
    return {
        "tool": "tools/preflight/live_run.py",
        "task": "INTEGRATION-001",
        "mode": "offline",
        "started_at": _utc_now(),
        "provider_requests_made": 0,
        "provider_configured": key_present,
        "chart_renderer_available": True,
        "orchestrator_ready_when_run": key_present,
        "note": "Default mode makes zero provider requests. "
                "Re-run with --live for at most one real incident.",
    }


def _run_live(output: Path, timeout_seconds: float) -> dict:
    from fastapi.testclient import TestClient

    from aura_api import create_app

    record: dict = {
        "tool": "tools/preflight/live_run.py",
        "task": "INTEGRATION-001",
        "mode": "live",
        "started_at": _utc_now(),
        "provider_requests_made": None,  # filled from actual call metadata
    }
    app = create_app(wire_default_orchestrator=True)
    manager = app.state.manager
    child_pid = None
    owned = []
    started = time.monotonic()
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            child_pid = manager.controller.child_pid
            record["child_pid"] = child_pid
            health = client.get("/health").json()
            record["health"] = health
            if not health.get("orchestrator_ready"):
                # Reject missing key/renderer BEFORE any fault injection.
                record["blocked"] = "orchestrator_not_ready"
                record["incident_started"] = False
                return record
            accepted = client.post(
                "/api/incidents", json={"scenario": "cache_retention"})
            record["incident_status_code"] = accepted.status_code
            record["incident_started"] = accepted.status_code == 202
            if accepted.status_code != 202:
                record["blocked"] = accepted.json()
                return record
            record["run_id"] = accepted.json().get("run_id")
            deadline = time.monotonic() + timeout_seconds
            state = {}
            while time.monotonic() < deadline:
                state = client.get("/api/state").json()
                if state.get("phase") in ("resolved", "error", "cancelled"):
                    break
                time.sleep(0.5)
            record["final_state"] = state
            record["elapsed_seconds"] = round(time.monotonic() - started, 3)
            orchestrator = manager.orchestrator
            metadata = list(getattr(orchestrator, "call_metadata", []))
            record["model_calls"] = len(metadata)
            record["model_attempts"] = sum(
                entry.get("attempts", 0) for entry in metadata)
            usage_totals = {"prompt_tokens": 0, "completion_tokens": 0,
                            "total_tokens": 0}
            usage_available = False
            for entry in metadata:
                usage = entry.get("usage") or {}
                for key in usage_totals:
                    if usage.get(key) is not None:
                        usage_totals[key] += usage[key]
                        usage_available = True
            record["usage_totals"] = usage_totals if usage_available else None
            record["requested_model"] = (
                metadata[0].get("requested_model") if metadata else None)
            verification = state.get("latest_verification") or {}
            record["verification"] = verification
            record["action"] = (state.get("proposal") or {}).get("action")
            record["error"] = state.get("error")
            owned = manager.controller.owned_process_identities
    finally:
        record["child_alive_after_shutdown"] = bool(
            child_pid and psutil.pid_exists(child_pid))
        counts = getattr(manager.orchestrator, "dispatched_counts", {})
        record["provider_requests_made"] = counts.get("attempts", 0)
        record["dispatched_model_calls"] = counts.get("calls", 0)
        record["usage_complete"] = (record.get("model_calls", 0) == counts.get("calls", 0)
                                    and record.get("model_attempts", 0) == counts.get("attempts", 0))
        owned = owned or manager.controller.owned_process_identities
        surviving = []
        for identity in owned:
            try:
                process = psutil.Process(identity['pid'])
                if abs(process.create_time() - identity['create_time']) <= 0.01 and process.is_running():
                    surviving.append(identity)
            except psutil.NoSuchProcess:
                pass
        record['owned_processes'] = owned
        record['surviving_owned_processes'] = surviving
        record["finished_at"] = _utc_now()
    return record


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true",
                        help="run at most one real incident (default: off)")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--timeout", type=float, default=240.0)
    args = parser.parse_args(argv)
    if args.live:
        record = _run_live(args.output, args.timeout)
        success = bool(
            record.get("incident_started")
            and (record.get("final_state") or {}).get("phase")
            == "resolved"
            and (record.get("verification") or {}).get("verified") is True
            and not record.get("child_alive_after_shutdown")
            and not record.get("surviving_owned_processes"))
    else:
        record = _offline_record(args.output)
        success = True
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(record, indent=2), encoding="utf-8")
    print(f"mode={record['mode']} evidence={args.output}")
    if record["mode"] == "offline":
        print(f"provider_configured={record['provider_configured']} "
              "provider_requests_made=0")
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
