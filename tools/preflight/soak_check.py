"""Offline backend acceptance harness: 20-minute soak, zero provider calls.

Two roles in one file:
  serve  - child: real Uvicorn + real worker + test-only mock-provider
           swarm (MockTransport; SIMULATED diagnosis, never a real call).
  run    - parent: drives HTTP/WS fault-matrix scenarios on an
           ephemeral 127.0.0.1 port, samples RSS/queues/tasks, then
           requests graceful owned-group shutdown and verifies cleanup.

Default run is the full 1200 s soak (warmup excluded). Evidence goes to
.local/acceptance/soak.json. Never contacts any provider, never touches
the user's servers, never kills unrelated processes.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

SIMULATED_LABEL = "simulated diagnosis (soak mock transport, not a provider call)"
MOCK_API_KEY = "soak-mock-key-not-real"
HOST = "127.0.0.1"
SOAK_SECONDS = 1200.0
SAMPLE_SECONDS = 2.0
MAX_MEASUREMENTS = 1200

_REPO = Path(__file__).resolve().parents[2]
_BACKEND = _REPO / "backend"
_ACCEPT = _REPO / ".local" / "acceptance"
sys.path.insert(0, str(_BACKEND))


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------- shared

def read_control(control_path: Path) -> dict:
    try:
        return json.loads(control_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"mode": "success", "token": 0}


def write_control(control_path: Path, mode: str, token: int) -> None:
    control_path.parent.mkdir(parents=True, exist_ok=True)
    control_path.write_text(
        json.dumps({"mode": mode, "token": token}), encoding="utf-8")


def _specialist_json(agent: str, refs: dict) -> str:
    return json.dumps({
        "agent_id": agent, "status": "completed",
        "summary": f"{agent}: {SIMULATED_LABEL}",
        "findings": [f"{agent} cites measured soak evidence"],
        "evidence_refs": refs[agent],
    })


def _completion(content: str, model: str, *, finish_reason="stop",
                reasoning="soak mock reasoning") -> dict:
    return {
        "id": "chatcmpl-soak", "object": "chat.completion", "model": model,
        "choices": [{"index": 0,
                     "message": {"role": "assistant", "content": content,
                                 "reasoning_content": reasoning},
                     "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 50, "completion_tokens": 20,
                  "total_tokens": 70,
                  "completion_tokens_details": {"reasoning_tokens": 5}},
    }


def build_swarm_handler(control_path: Path, model: str):
    """MockTransport handler whose behavior follows the control file."""
    import httpx

    counts: dict[tuple[int, str], int] = {}
    refs = {
        "logs": ["telemetry.current", "diagnostic.logs"],
        "memory": ["telemetry.current", "telemetry.recent"],
        "config": ["worker.config", "worker.status"],
        "vision": ["chart.measured", "telemetry.current"],
    }

    def agent_of(body: dict) -> str:
        for message in body.get("messages", []):
            content = message.get("content")
            texts = [content] if isinstance(content, str) else [
                part.get("text", "") for part in content
                if isinstance(part, dict)]
            for text in texts:
                if "SPECIALIST:" in text:
                    return text.split("SPECIALIST:", 1)[1].strip().split()[0]
        return "unknown"

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        agent = agent_of(body)
        control = read_control(control_path)
        token = int(control.get("token", 0))
        mode = str(control.get("mode", "success"))
        key = (token, agent)
        counts[key] = counts.get(key, 0) + 1
        attempt = counts[key]
        if mode == "hang":
            await asyncio.sleep(90.0)
        if mode == "always429":
            return httpx.Response(429, json={"error": {"message": "slow"}})
        if mode == "always500":
            return httpx.Response(500, json={"error": {"message": "boom"}})
        if mode == "retry429" and attempt == 1:
            return httpx.Response(429, json={"error": {"message": "slow"}})
        if agent == "synthesis":
            if mode == "abstain":
                content = json.dumps({"action": "abstain",
                                      "reason": SIMULATED_LABEL,
                                      "evidence_refs": []})
            elif mode == "unknown_action":
                content = json.dumps({"action": "restart_everything",
                                      "reason": "x", "evidence_refs": []})
            else:
                content = json.dumps(
                    {"action": "restore_cache_policy",
                     "reason": SIMULATED_LABEL,
                     "evidence_refs": ["telemetry.current"]})
            return httpx.Response(200, json=_completion(content, model))
        if mode == "malformed":
            content = "this is not json"
        elif mode == "truncated":
            return httpx.Response(200, json=_completion(
                _specialist_json(agent, refs), model, finish_reason="length"))
        elif mode == "empty":
            content = ""
        else:
            content = _specialist_json(agent, refs)
        return httpx.Response(200, json=_completion(content, model))

    return handler


def make_mock_swarm(control_path: Path, evidence_dir: Path | None = None):
    """Test-only real-swarm orchestrator backed by MockTransport."""
    import httpx
    from openai import AsyncOpenAI

    from aura_agents.adapter import BASE_URL, MODEL, BasetenAdapter
    from aura_agents.chart import render_measured_chart
    from aura_agents.orchestrator import BasetenSwarmOrchestrator

    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            build_swarm_handler(control_path, MODEL)))
    client = AsyncOpenAI(
        api_key=MOCK_API_KEY, base_url=BASE_URL, http_client=http_client,
        max_retries=0, timeout=10.0)
    adapter = BasetenAdapter(api_key=MOCK_API_KEY, client=client)
    return BasetenSwarmOrchestrator(
        adapter, chart_renderer=render_measured_chart,
        evidence_dir=evidence_dir)


# ------------------------------------------------------------- analysis

def healthy_trend(samples: list[dict]) -> dict:
    """Least-squares slope over warmed healthy RSS samples (bytes/sec)."""
    points = [(s["t"], s["worker_rss"]) for s in samples
              if s.get("healthy") and s.get("worker_rss")]
    if len(points) < 10:
        return {"slope_bytes_per_sec": None, "n": len(points),
                "verdict": "insufficient_healthy_samples"}
    n = len(points)
    mean_t = sum(t for t, _ in points) / n
    mean_r = sum(r for _, r in points) / n
    denom = sum((t - mean_t) ** 2 for t, _ in points)
    slope = (sum((t - mean_t) * (r - mean_r) for t, r in points) / denom
             if denom else 0.0)
    values = sorted(r for _, r in points)
    first_half = values[: n // 2]
    second_half = values[n // 2:]
    start_median = first_half[len(first_half) // 2]
    end_median = second_half[len(second_half) // 2]
    drift = end_median - start_median
    verdict = ("continuous_growth_flagged"
               if slope > 0 and drift > 64 * 1024 * 1024
               else "stable_or_bounded")
    return {"slope_bytes_per_sec": round(slope, 3), "n": n,
            "start_median": start_median, "end_median": end_median,
            "drift_bytes": drift, "min": values[0], "max": values[-1],
            "verdict": verdict}


def check_watermark(http_seq, ws_snapshot_seq, event_seqs: list[int]) -> dict:
    """HTTP snapshot seq watermark vs authoritative WS sequence."""
    ordered = all(b > a for a, b in zip(event_seqs, event_seqs[1:]))
    return {
        "http_seq": http_seq, "ws_snapshot_seq": ws_snapshot_seq,
        "events_monotonic": ordered,
        "snapshot_not_older_than_http": (
            None if http_seq is None or ws_snapshot_seq is None
            else ws_snapshot_seq >= http_seq),
    }


def find_secret_markers(text: str) -> list[str]:
    """Banned tokens that must never appear in evidence (values/headers)."""
    banned = ["BASETEN_API_KEY=", "sk-", "Authorization:", "authorization",
              "X-Stainless", "x-stainless"]
    return sorted({token for token in banned if token in text})


# ------------------------------------------------------------ serve mode

def _install_graceful_handler(server) -> None:
    def _handler(signum, _frame):
        print(f"graceful-shutdown-signal received (sig={signum})", flush=True)
        server.should_exit = True

    signal.signal(signal.SIGINT, _handler)
    sigbreak = getattr(signal, "SIGBREAK", None)
    if sigbreak is not None:
        signal.signal(sigbreak, _handler)


async def _metrics_loop(manager, path: Path, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            controller = manager.controller
            queues = [q.qsize() for q in manager._subscribers]
            row = {
                "timestamp": _utc_now(),
                "journal_len": len(manager.journal),
                "journal_maxlen": manager.journal.maxlen,
                "subscribers": len(manager._subscribers),
                "subscriber_queue_max": max(queues) if queues else 0,
                "pending_requests": len(controller._pending),
                "asyncio_tasks": len(asyncio.all_tasks()),
                "controller_state": controller.state.value,
                "worker_generation": controller.generation,
                "seq": manager._seq,
                "dropped_slow": getattr(
                    manager, "dropped_for_slow_subscribers", None),
            }
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(row), encoding="utf-8")
            tmp.replace(path)
        except Exception as exc:  # metrics must never break serving
            print(f"metrics-loop error: {exc!r}", flush=True)
        await asyncio.sleep(SAMPLE_SECONDS)


def cmd_serve(args) -> int:
    os.environ.pop("BASETEN_API_KEY", None)  # harness never holds a key
    import uvicorn

    from aura_api import ApiSettings, create_app
    from aura_worker import WorkerController

    metrics_path = Path(args.metrics)
    metrics_path.parent.mkdir(parents=True, exist_ok=True)
    control_path = Path(args.control)
    orchestrator = make_mock_swarm(
        control_path, evidence_dir=_ACCEPT / "agent-evidence")
    app = create_app(
        orchestrator=orchestrator,
        controller_factory=WorkerController,
        provider_configured=False,
        settings=ApiSettings(subscriber_queue_size=20),
    )
    manager = app.state.manager
    config = uvicorn.Config(
        app, host=HOST, port=args.port, log_level="warning", lifespan="on")
    server = uvicorn.Server(config)
    _install_graceful_handler(server)
    stop = asyncio.Event()

    async def _amain() -> None:
        metrics_task = asyncio.create_task(
            _metrics_loop(manager, metrics_path, stop))
        try:
            await server.serve()
        finally:
            stop.set()
            await asyncio.gather(metrics_task, return_exceptions=True)

    print(f"soak serve on http://{HOST}:{args.port} (mock swarm, real worker)",
          flush=True)
    asyncio.run(_amain())
    return 0


# ------------------------------------------------------------- run mode

def _free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind((HOST, 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class Driver:
    def __init__(self, base_url: str, control_path: Path, run_dir: Path):
        import httpx

        self.base = base_url
        self.control_path = control_path
        self.run_dir = run_dir
        self.http = httpx.Client(base_url=base_url, timeout=10.0)
        self.token = 0
        self.events_log: list[dict] = []

    def set_mode(self, mode: str) -> int:
        self.token += 1
        write_control(self.control_path, mode, self.token)
        return self.token

    def state(self) -> dict:
        return self.http.get("/api/state").json()

    def health(self) -> dict:
        return self.http.get("/health").json()

    def start_incident(self) -> dict:
        response = self.http.post(
            "/api/incidents", json={"scenario": "cache_retention"})
        return {"status": response.status_code, "body": response.json()}

    def reset(self) -> dict:
        response = self.http.post("/api/reset")
        return {"status": response.status_code, "body": response.json()}

    def wait_phase(self, phases: set[str], timeout: float,
                   poll: float = 1.0) -> dict:
        deadline = time.monotonic() + timeout
        last: dict = {}
        while time.monotonic() < deadline:
            last = self.state()
            if last.get("phase") in phases:
                return last
            time.sleep(poll)
        return last

    def note(self, scenario: str, text: str) -> None:
        self.events_log.append(
            {"t": round(time.monotonic(), 1), "scenario": scenario,
             "text": text[:300]})
        print(f"SOAK [{scenario}] {text}", flush=True)


def _tree_identities(root_pid: int) -> list[dict]:
    import psutil

    try:
        root = psutil.Process(root_pid)
        members = [root] + root.children(recursive=True)
    except psutil.Error:
        return []
    out = []
    for proc in members:
        try:
            cmdline = " ".join(proc.cmdline())
            out.append({"pid": proc.pid,
                        "create_time": proc.create_time(),
                        "rss": proc.memory_info().rss,
                        "role": ("worker" if "aura_worker" in cmdline
                                 else "server")})
        except psutil.Error:
            continue
    return out


class Sampler(threading.Thread):
    def __init__(self, driver: Driver, child_pid: int, metrics_path: Path):
        super().__init__(daemon=True)
        self.driver = driver
        self.child_pid = child_pid
        self.metrics_path = metrics_path
        self.samples: list[dict] = []
        self.state_polls: list[dict] = []
        self.stop_event = threading.Event()
        self.last_phase = "unknown"

    def run(self) -> None:
        tick = 0
        while not self.stop_event.is_set():
            tick += 1
            try:
                members = _tree_identities(self.child_pid)
                server_rss = sum(m["rss"] for m in members
                                 if m["role"] == "server")
                worker_rss = sum(m["rss"] for m in members
                                 if m["role"] == "worker")
                try:
                    metrics = json.loads(
                        self.metrics_path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    metrics = {}
                if tick % 3 == 1:  # light /api/state cadence (~6 s)
                    try:
                        polled = self.driver.state()
                        self.last_phase = polled.get("phase", "unknown")
                        self.state_polls.append({
                            "t": round(time.monotonic(), 1),
                            "phase": self.last_phase,
                            "seq": polled.get("seq"),
                            "failed_jobs": (polled.get("telemetry") or {})
                            .get("failed_jobs"),
                        })
                    except Exception:
                        pass
                self.samples.append({
                    "t": round(time.monotonic(), 1),
                    "phase": self.last_phase,
                    "server_rss": server_rss or None,
                    "worker_rss": worker_rss or None,
                    "identities": [(m["pid"], round(m["create_time"], 3))
                                   for m in members],
                    "journal_len": metrics.get("journal_len"),
                    "subscriber_queue_max": metrics.get(
                        "subscriber_queue_max"),
                    "pending_requests": metrics.get("pending_requests"),
                    "dropped_slow": metrics.get("dropped_slow"),
                })
            except Exception as exc:  # sampling must never kill the run
                print(f"SOAK sampler error: {exc!r}", flush=True)
            self.stop_event.wait(SAMPLE_SECONDS)


TERMINAL = {"resolved", "error", "cancelled"}


async def _ws_session(url: str, read_seconds: float,
                      read_messages: bool = True) -> dict:
    """One real-Uvicorn WebSocket session; returns seq observations."""
    from websockets.asyncio.client import connect

    result: dict = {"snapshot": None, "seqs": [], "gaps": [],
                    "run_ids": set()}
    async with connect(url, max_size=4 * 1024 * 1024) as ws:
        raw = await asyncio.wait_for(ws.recv(), timeout=10.0)
        snapshot = json.loads(raw)
        result["snapshot"] = snapshot
        last = snapshot.get("seq")
        result["seqs"].append(last)
        deadline = time.monotonic() + read_seconds
        if read_messages:
            while time.monotonic() < deadline:
                try:
                    raw = await asyncio.wait_for(
                        ws.recv(), timeout=max(
                            0.5, deadline - time.monotonic()))
                except asyncio.TimeoutError:
                    break
                event = json.loads(raw)
                seq = event.get("seq")
                result["seqs"].append(seq)
                result["run_ids"].add(event.get("run_id"))
                if isinstance(seq, int) and isinstance(last, int):
                    if seq <= last:
                        result["gaps"].append(("rollback", last, seq))
                    elif seq > last + 1:
                        result["gaps"].append(("gap", last, seq))
                last = seq
        else:
            await asyncio.sleep(read_seconds)  # slow: hold, never read
    result["run_ids"] = sorted(result["run_ids"], key=str)
    return result


def ws_churn(driver: Driver, rounds: int, read_seconds: float) -> dict:
    url = driver.base.replace("http://", "ws://") + "/ws/telemetry"
    out = {"rounds": [], "reconnects": 0, "monotonic_ok": True,
           "watermarks": []}
    for _ in range(rounds):
        http_seq = driver.state().get("seq")
        session = asyncio.run(_ws_session(url, read_seconds))
        out["reconnects"] += 1
        out["watermarks"].append(check_watermark(
            http_seq, (session["snapshot"] or {}).get("seq"),
            [s for s in session["seqs"] if isinstance(s, int)]))
        if session["gaps"]:
            out["monotonic_ok"] = False
        out["rounds"].append({
            "snapshot_seq": (session["snapshot"] or {}).get("seq"),
            "events": len(session["seqs"]) - 1, "gaps": session["gaps"][:5]})
    return out


def run_incident(driver: Driver, mode: str, name: str,
                 terminal_timeout: float = 150.0) -> dict:
    """One incident to a terminal phase; records NEW failure counters."""
    token = driver.set_mode(mode)
    before = driver.state()
    failed_before = (before.get("telemetry") or {}).get("failed_jobs")
    accepted = driver.start_incident()
    started = time.monotonic()
    if accepted["status"] != 202:
        return {"name": name, "mode": mode, "token": token,
                "accepted": False, "status": accepted["status"],
                "body": accepted["body"],
                "duration_s": round(time.monotonic() - started, 1)}
    failed_at_diagnosing = None
    final = {}
    deadline = time.monotonic() + terminal_timeout
    while time.monotonic() < deadline:
        final = driver.state()
        phase = final.get("phase")
        if phase == "diagnosing" and failed_at_diagnosing is None:
            failed_at_diagnosing = (final.get("telemetry") or {}).get(
                "failed_jobs")
        if phase in TERMINAL:
            break
        time.sleep(1.0)
    telemetry = final.get("telemetry") or {}
    return {
        "name": name, "mode": mode, "token": token, "accepted": True,
        "phase": final.get("phase"),
        "run_id": final.get("run_id"),
        "failed_before": failed_before,
        "failed_at_diagnosing": failed_at_diagnosing,
        "failed_after": telemetry.get("failed_jobs"),
        "new_rejections_seen": (
            (failed_at_diagnosing or 0) > (failed_before or 0)
            if failed_before is not None and failed_at_diagnosing is not None
            else None),
        "verification": final.get("latest_verification"),
        "error": final.get("error"),
        "duration_s": round(time.monotonic() - started, 1),
    }


def expect_reset_required(driver: Driver) -> dict:
    """After a terminal failure the worker is dirty: new runs need Reset."""
    accepted = driver.start_incident()
    body = accepted.get("body") or {}
    error = body.get("error") or {}
    return {"status": accepted["status"], "code": error.get("code"),
            "required": accepted["status"] == 409
            and error.get("code") == "reset_required"}


def do_reset(driver: Driver, timeout: float = 60.0) -> dict:
    started = time.monotonic()
    response = driver.reset()
    state = driver.wait_phase({"idle"}, timeout=timeout)
    return {"status": response["status"], "phase": state.get("phase"),
            "duration_s": round(time.monotonic() - started, 1)}


def reset_during(driver: Driver, mode: str, name: str,
                 at_phases: set[str], settle_seconds: float = 6.0) -> dict:
    """Reset mid-flight; late results must be dropped, no resolve/restore."""
    token = driver.set_mode(mode)
    accepted = driver.start_incident()
    started = time.monotonic()
    if accepted["status"] != 202:
        return {"name": name, "accepted": False, "status": accepted["status"],
                "duration_s": round(time.monotonic() - started, 1)}
    run_id = accepted["body"].get("run_id")
    driver.wait_phase(at_phases, timeout=120.0)
    reset_outcome = do_reset(driver)
    time.sleep(settle_seconds)  # late results would land here if dropped wrong
    state = driver.state()
    return {
        "name": name, "mode": mode, "token": token, "accepted": True,
        "run_id": run_id, "reset": reset_outcome,
        "phase_after_settle": state.get("phase"),
        "run_after_settle": state.get("run_id"),
        "verification_after_settle": state.get("latest_verification"),
        "no_resolve": state.get("phase") == "idle"
        and state.get("run_id") is None,
        "duration_s": round(time.monotonic() - started, 1),
    }


def worker_death(driver: Driver, child_pid: int,
                 terminal_timeout: float = 120.0) -> dict:
    """Terminate the OWNED worker mid-diagnosis; run must fail, not resolve."""
    import psutil

    token = driver.set_mode("hang")
    accepted = driver.start_incident()
    started = time.monotonic()
    if accepted["status"] != 202:
        return {"name": "worker_death", "accepted": False,
                "status": accepted["status"],
                "duration_s": round(time.monotonic() - started, 1)}
    driver.wait_phase({"diagnosing"}, timeout=120.0)
    killed = []
    for member in _tree_identities(child_pid):
        if member["role"] != "worker":
            continue
        try:
            proc = psutil.Process(member["pid"])
            if abs(proc.create_time() - member["create_time"]) > 0.01:
                continue  # recycled PID: never touch
            proc.terminate()
            killed.append(member["pid"])
        except psutil.Error:
            continue
    final = driver.wait_phase(TERMINAL, timeout=terminal_timeout)
    outcome = {
        "name": "worker_death", "token": token, "accepted": True,
        "killed_owned_worker_pids": killed,
        "phase": final.get("phase"), "error": final.get("error"),
        "duration_s": round(time.monotonic() - started, 1),
    }
    outcome["reset"] = do_reset(driver)
    return outcome


def slow_subscriber(driver: Driver, hold_seconds: float) -> dict:
    url = driver.base.replace("http://", "ws://") + "/ws/telemetry"

    async def _hold() -> dict:
        held = await _ws_session(url, hold_seconds, read_messages=False)
        fresh = await _ws_session(url, 4.0, read_messages=True)
        return {"held_snapshot_seq": (held["snapshot"] or {}).get("seq"),
                "fresh_snapshot_seq": (fresh["snapshot"] or {}).get("seq"),
                "fresh_events": len(fresh["seqs"]) - 1,
                "fresh_gaps": fresh["gaps"][:5]}

    started = time.monotonic()
    result = asyncio.run(_hold())
    result["duration_s"] = round(time.monotonic() - started, 1)
    return result


def idle_observe(driver: Driver, seconds: float, name: str = "idle") -> dict:
    """Idle telemetry window with seq-monotonic reads (no incident)."""
    url = driver.base.replace("http://", "ws://") + "/ws/telemetry"
    started = time.monotonic()
    session = asyncio.run(_ws_session(url, seconds, read_messages=True))
    seqs = [s for s in session["seqs"] if isinstance(s, int)]
    return {
        "name": name, "duration_s": round(time.monotonic() - started, 1),
        "events": len(seqs) - 1, "monotonic": not session["gaps"],
        "gaps": session["gaps"][:5],
        "snapshot_seq": (session["snapshot"] or {}).get("seq"),
    }


def _spawn_server(port: int, control_path: Path, metrics_path: Path,
                  server_log: Path) -> subprocess.Popen:
    env = dict(os.environ)
    env.pop("BASETEN_API_KEY", None)
    kwargs: dict = {
        "cwd": str(_REPO),
        "env": env,
        "stdout": server_log.open("ab"),
        "stderr": subprocess.STDOUT,
    }
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(
        [sys.executable, "-u", str(Path(__file__).resolve()), "serve",
         "--port", str(port), "--control", str(control_path),
         "--metrics", str(metrics_path)],
        **kwargs)


def _graceful_stop(proc: subprocess.Popen, server_log: Path,
                   timeout: float = 30.0) -> dict:
    """Group-scoped control signal: graceful path, never a broadcast."""
    outcome: dict = {"signal": None, "log_confirms_graceful": False,
                     "exited": False}
    try:
        if os.name == "nt":
            os.kill(proc.pid, signal.CTRL_BREAK_EVENT)
            outcome["signal"] = "CTRL_BREAK_EVENT"
        else:
            os.killpg(proc.pid, signal.SIGINT)
            outcome["signal"] = "SIGINT"
    except (OSError, ProcessLookupError) as exc:
        outcome["signal_error"] = repr(exc)
        return outcome
    try:
        proc.wait(timeout=timeout)
        outcome["exited"] = True
    except subprocess.TimeoutExpired:
        return outcome
    try:
        tail = server_log.read_bytes()[-20000:].decode(
            "utf-8", errors="replace")
    except OSError:
        tail = ""
    outcome["log_confirms_graceful"] = (
        "graceful-shutdown-signal" in tail
        and ("Shutting down" in tail or "shutdown" in tail.lower()))
    return outcome


def _identities_gone(identities: list[dict]) -> list[dict]:
    import psutil

    survivors = []
    for identity in identities:
        pid = identity["pid"]
        try:
            proc = psutil.Process(pid)
            if identity.get("create_time") is None:
                survivors.append({"pid": pid, "unverified": True})
            elif abs(proc.create_time() - identity["create_time"]) > 0.01:
                continue  # recycled PID belongs to someone else
            else:
                survivors.append({"pid": pid})
        except psutil.Error:
            continue
    return survivors


def _mark_healthy(samples: list[dict]) -> None:
    for sample in samples:
        sample["healthy"] = sample.get("phase") in ("idle",)


def cmd_run(args) -> int:
    import psutil

    run_dir = _ACCEPT
    run_dir.mkdir(parents=True, exist_ok=True)
    control_path = run_dir / "control.json"
    metrics_path = run_dir / "child-metrics.json"
    server_log = run_dir / "server.log"
    server_log.write_bytes(b"")  # fresh log per run; no stale lines
    write_control(control_path, "success", 0)
    port = _free_port()
    base = f"http://{HOST}:{port}"
    print(f"SOAK starting isolated server on {base} (mock swarm only)",
          flush=True)
    proc = _spawn_server(port, control_path, metrics_path, server_log)
    try:
        server_identity = {
            "pid": proc.pid,
            "create_time": psutil.Process(proc.pid).create_time(),
        }
    except psutil.Error:
        server_identity = {"pid": proc.pid, "create_time": None}
    driver = Driver(base, control_path, run_dir)
    record: dict = {
        "tool": "tools/preflight/soak_check.py", "task": "ACCEPTANCE-001",
        "mode": "offline-soak", "started_at": _utc_now(), "port": port,
        "provider_requests_made": 0, "diagnosis_label": SIMULATED_LABEL,
        "requested_soak_seconds": args.duration, "scenarios": [],
        "coverage": {}, "criteria": {}, "notes": driver.events_log,
    }
    soak_started: float | None = None
    sampler = None
    try:
        # Warmup (excluded from soak time): worker healthy + baseline.
        warmup_started = time.monotonic()
        healthy = False
        while time.monotonic() - warmup_started < args.warmup_timeout:
            try:
                health = driver.health()
                if health.get("worker_running") \
                        and (driver.state().get("phase") == "idle"):
                    healthy = True
                    break
            except Exception:
                pass
            if proc.poll() is not None:
                print("SOAK server exited during warmup", flush=True)
                record["criteria"]["server_started"] = False
                return 2
            time.sleep(1.0)
        record["warmup_seconds"] = round(
            time.monotonic() - warmup_started, 1)
        if not healthy:
            print("SOAK warmup timed out", flush=True)
            record["criteria"]["server_started"] = False
            return 2
        record["server_pid"] = proc.pid
        record["server_identity"] = server_identity

        sampler = Sampler(driver, proc.pid, metrics_path)
        sampler.start()
        soak_started = time.monotonic()
        record["soak_started_at"] = _utc_now()

        def elapsed() -> float:
            return time.monotonic() - soak_started

        def progress(tag: str) -> None:
            print(f"SOAK t={elapsed():.0f}s {tag}", flush=True)

        def terminal_incident(mode: str, name: str) -> dict:
            outcome = run_incident(driver, mode, name)
            driver.note(name,
                        f"mode={mode} phase={outcome.get('phase')} "
                        f"{outcome.get('duration_s')}s")
            record["scenarios"].append(outcome)
            return outcome

        def error_cycle(mode: str, name: str) -> None:
            outcome = terminal_incident(mode, name)
            if outcome.get("phase") == "error":
                # Dirty worker after a terminal failure: Reset is required.
                gate = expect_reset_required(driver)
                outcome["reset_required_gate"] = gate
                if not gate.get("required"):
                    driver.note(name, "EXPECTED reset_required gate MISSED")
            else:
                # Resolved runs restore a healthy worker, so a new run
                # without Reset is correct application behavior.
                outcome["reset_required_gate"] = {"required": True,
                                                  "not_applicable": True}
            reset_outcome = do_reset(driver)
            outcome["reset"] = reset_outcome

        # Schedule: interleave real recoveries, the fault matrix,
        # cancellations and WS churn with idle telemetry windows.
        plan = [
            ("idle", lambda: idle_observe(driver, 90.0, "idle-1")),
            ("ws", lambda: ws_churn(driver, 3, 5.0)),
            ("success", lambda: terminal_incident(
                "success", "recovery-1")),
            ("idle", lambda: idle_observe(driver, 60.0, "idle-2")),
            ("error", lambda: error_cycle("retry429", "retry-429-success")),
            ("error", lambda: error_cycle("always429", "retry-limit-429")),
            ("error", lambda: error_cycle("always500", "server-500")),
            ("idle", lambda: idle_observe(driver, 75.0, "idle-3")),
            ("error", lambda: error_cycle("malformed", "malformed-output")),
            ("error", lambda: error_cycle("truncated", "truncated-output")),
            ("error", lambda: error_cycle("empty", "empty-output")),
            ("error", lambda: error_cycle("abstain", "synthesis-abstain")),
            ("error", lambda: error_cycle(
                "unknown_action", "unknown-action")),
            ("idle", lambda: idle_observe(driver, 60.0, "idle-4")),
            ("cancel", lambda: record["scenarios"].append(reset_during(
                driver, "success", "reset-at-fault",
                {"fault_injected"}))),
            ("cancel", lambda: record["scenarios"].append(reset_during(
                driver, "hang", "reset-during-diagnosis",
                {"diagnosing"}))),
            ("slow", lambda: record.setdefault(
                "slow_subscriber", slow_subscriber(driver, 25.0))),
            ("death", lambda: record["scenarios"].append(
                worker_death(driver, proc.pid,
                             terminal_timeout=args.death_wait))),
            ("cancel", lambda: record["scenarios"].append(reset_during(
                driver, "success", "reset-during-verification",
                {"verifying", "remediating"}))),
            ("success", lambda: terminal_incident(
                "success", "recovery-2")),
            ("idle", lambda: idle_observe(driver, 90.0, "idle-5")),
            ("ws", lambda: record.setdefault(
                "ws_churn_2", ws_churn(driver, 5, 4.0))),
            ("success", lambda: terminal_incident(
                "success", "recovery-3")),
            ("error", lambda: error_cycle("hang", "diagnosis-timeout")),
        ]
        index = 0
        last_progress = 0.0
        if args.only:
            # Targeted evidence run (NOT a soak): run named steps once.
            wanted = {name.strip() for name in args.only.split(",")}
            for kind, step in plan:
                label = getattr(step, "__name__", "")
                if kind in wanted or label in wanted:
                    try:
                        step()
                    except Exception as exc:
                        driver.note("schedule-error", repr(exc)[:200])
            record["targeted_only"] = sorted(wanted)
            record["soak_seconds"] = round(elapsed(), 1)
        while not args.only and elapsed() < args.duration:
            kind, step = plan[index % len(plan)]
            index += 1
            try:
                result = step()
                if kind in ("idle", "ws") and isinstance(result, dict):
                    record["scenarios"].append(
                        {"name": result.get("name", kind), **result})
            except Exception as exc:
                driver.note("schedule-error", repr(exc)[:200])
                record["scenarios"].append(
                    {"name": f"{kind}-step", "schedule_error": repr(exc)})
            if elapsed() - last_progress >= 60.0:
                last_progress = elapsed()
                progress(f"running ({index} steps)")
            remaining = args.duration - elapsed()
            if 0 < remaining < 45:
                # Fill exactly to the requested duration, then stop.
                idle_observe(driver, remaining, "idle-fill")
                break
        record["soak_seconds"] = round(elapsed(), 1)

        # Final idle state before shutdown.
        final_state = driver.state()
        if final_state.get("phase") not in ("idle",):
            do_reset(driver)
        record["final_state_phase"] = driver.state().get("phase")
        sampler.stop_event.set()
        sampler.join(timeout=10.0)

        _mark_healthy(sampler.samples)
        bounded = sampler.samples[-MAX_MEASUREMENTS:]
        for sample in bounded:
            sample["identities"] = [
                list(identity) for identity in sample["identities"]]
        record["measurements"] = bounded
        record["state_polls"] = sampler.state_polls[-400:]

        trend = healthy_trend(sampler.samples)
        record["memory"] = trend
        journals = [s["journal_len"] for s in sampler.samples
                    if s.get("journal_len") is not None]
        queues = [s["subscriber_queue_max"] for s in sampler.samples
                  if s.get("subscriber_queue_max") is not None]
        pendings = [s["pending_requests"] for s in sampler.samples
                    if s.get("pending_requests") is not None]
        record["bounds"] = {
            "journal_max": max(journals) if journals else None,
            "journal_cap": 500,
            "subscriber_queue_max": max(queues) if queues else None,
            "subscriber_queue_cap": 20,
            "pending_max": max(pendings) if pendings else None,
            "pending_final": pendings[-1] if pendings else None,
            "dropped_slow_final": next(
                (s["dropped_slow"] for s in reversed(sampler.samples)
                 if s.get("dropped_slow") is not None), None),
        }
        ws_ok = all(
            s.get("monotonic", s.get("monotonic_ok", True))
            for s in record["scenarios"]
            if s.get("name", "").startswith(("idle", "ws")))
        record["coverage"] = {
            "recoveries": sum(
                1 for s in record["scenarios"]
                if s.get("phase") == "resolved"),
            "error_modes": sorted({
                s.get("mode") for s in record["scenarios"]
                if s.get("phase") == "error"}),
            "ws_monotonic": ws_ok,
        }
        criteria = {
            "soak_duration_met": record["soak_seconds"] >= args.duration,
            "recoveries_at_least_3": record["coverage"]["recoveries"] >= 3,
            "error_matrix_complete": all(
                mode in record["coverage"]["error_modes"] for mode in (
                    "always429", "always500", "malformed", "truncated",
                    "empty", "abstain", "unknown_action", "hang")),
            "reset_gates": all(
                s.get("reset_required_gate", {}).get("required", True)
                for s in record["scenarios"]
                if "reset_required_gate" in s),
            "journal_bounded": (record["bounds"]["journal_max"] or 0) <= 500,
            "queues_bounded": (
                (record["bounds"]["subscriber_queue_max"] or 0) <= 20),
            "no_pending_retained": record["bounds"]["pending_final"] == 0,
            "ws_monotonic": ws_ok,
            "memory_no_continuous_growth": (
                trend.get("verdict") != "continuous_growth_flagged"),
        }
        record["criteria"] = criteria
    except KeyboardInterrupt:
        record["interrupted"] = True
        record["soak_seconds"] = round(
            time.monotonic() - soak_started, 1) \
            if soak_started is not None else 0.0
        print("SOAK interrupted by operator; shutting down gracefully",
              flush=True)
    except Exception as exc:
        record["harness_error"] = repr(exc)[:500]
        print(f"SOAK harness error: {exc!r}", flush=True)

    # Snapshot owned identities BEFORE shutdown: the tree is gone after
    # a graceful exit, so post-exit enumeration would prove nothing.
    pre_shutdown_owned = _tree_identities(proc.pid)
    record["owned_before_shutdown"] = pre_shutdown_owned

    # Graceful owned-group shutdown, then identity-verified cleanup check.
    shutdown = _graceful_stop(proc, server_log)
    record["shutdown"] = shutdown
    try:
        tail = server_log.read_bytes()[-6000:].decode(
            "utf-8", errors="replace")
    except OSError:
        tail = ""
    record["server_log_tail"] = tail[-3000:]
    owned = pre_shutdown_owned
    survivors = _identities_gone(
        [{"pid": m["pid"], "create_time": m["create_time"]}
         for m in owned] + [server_identity])
    if not shutdown.get("exited"):
        # Honest fallback: stop only still-owned identities, then fail.
        for member in owned:
            try:
                target = psutil.Process(member["pid"])
                if abs(target.create_time()
                       - member["create_time"]) <= 0.01:
                    target.terminate()
            except psutil.Error:
                pass
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            pass
        survivors = _identities_gone(
            [{"pid": m["pid"], "create_time": m["create_time"]}
             for m in owned])
    record["cleanup"] = {
        "owned_identities_seen": len(owned),
        "survivors": survivors,
        "all_gone": not survivors,
    }
    record["criteria"]["graceful_shutdown"] = bool(
        shutdown.get("exited") and shutdown.get("log_confirms_graceful"))
    record["criteria"]["cleanup_all_gone"] = not survivors
    record["finished_at"] = _utc_now()
    text = json.dumps(record, indent=2, default=str)
    markers = find_secret_markers(text)
    record["secret_markers"] = markers
    record["criteria"]["no_secrets_in_evidence"] = not markers
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(record, indent=2, default=str),
                      encoding="utf-8")
    failed = [name for name, passed in record["criteria"].items()
              if not passed]
    print(f"SOAK RESULT {'PASS' if not failed else 'FAIL ' + ','.join(failed)}"
          f" soak={record.get('soak_seconds')}s evidence={output}",
          flush=True)
    return 0 if not failed else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", nargs="?", default="run",
                        choices=("run", "serve"))
    parser.add_argument("--duration", type=float, default=SOAK_SECONDS)
    parser.add_argument("--only", type=str, default="",
                        help="targeted evidence run: comma-separated plan "
                             "kinds (e.g. death); not a soak")
    parser.add_argument("--death-wait", type=float, default=120.0)
    parser.add_argument("--warmup-timeout", type=float, default=180.0)
    parser.add_argument("--output", type=Path,
                        default=_ACCEPT / "soak.json")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--control", type=Path,
                        default=_ACCEPT / "control.json")
    parser.add_argument("--metrics", type=Path,
                        default=_ACCEPT / "child-metrics.json")
    args = parser.parse_args(argv)
    if args.mode == "serve":
        return cmd_serve(args)
    return cmd_run(args)


if __name__ == "__main__":
    raise SystemExit(main())

