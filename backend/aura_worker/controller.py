"""Asynchronous parent controller for the isolated cache worker.

Suitable for later FastAPI use. Spawns exactly one child with the project
virtualenv interpreter and an argument list (never shell=True), validates
response IDs, enforces finite timeouts, samples real child RSS via psutil
every 500 ms while independently polling worker status, and reaps only its
own identity-verified process tree on shutdown/cancellation. No API
credentials are passed to the worker environment.
"""
from __future__ import annotations

import asyncio
import json
import os
import statistics
import sys
import time
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Callable, Optional

import psutil

from .cache import HEALTHY_CACHE_BYTES
from .protocol import parse_response_line

RSS_SAMPLE_INTERVAL_SECONDS = 0.5
BASELINE_SAMPLE_COUNT = 5
RSS_THRESHOLD_OFFSET_BYTES = 24 * 1024 * 1024
DETECTION_CONSECUTIVE_SAMPLES = 3
VERIFICATION_DURATION_SECONDS = 8.0
DEFAULT_COMMAND_TIMEOUT = 10.0
DEFAULT_STARTUP_TIMEOUT = 20.0
DEFAULT_SHUTDOWN_TIMEOUT = 10.0
DEFAULT_VERIFICATION_TIMEOUT = 40.0
RSS_FRESH_MAX_AGE_SECONDS = 1.5
STATUS_POLL_INTERVAL_SECONDS = 0.5
STATUS_COMMAND_TIMEOUT_SECONDS = 1.0
STATUS_FRESH_MAX_AGE_SECONDS = 2.0
_CREDENTIAL_ENV_DENYLIST = {"BASETEN_API_KEY", "OPENAI_API_KEY"}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ControllerState(str, Enum):
    STOPPED = "stopped"
    STARTING = "starting"
    HEALTHY = "healthy"
    FAULT_DETECTED = "fault_detected"
    VERIFYING = "verifying"
    RECOVERED = "recovered"
    ERROR = "error"
    DEAD = "dead"


class WorkerError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class WorkerDeadError(WorkerError):
    def __init__(self, message: str = "worker process is not running"):
        super().__init__("worker_dead", message)


@dataclass(frozen=True)
class ProcessIdentity:
    """A PID bound to its creation time, so a recycled PID never matches."""

    pid: int
    create_time: float

    def to_dict(self) -> dict:
        return {"pid": self.pid, "create_time": self.create_time}


@dataclass
class Measurement:
    timestamp: str
    monotonic: float
    rss_bytes: Optional[int]
    retained_cache_bytes: Optional[int]
    completed_jobs: Optional[int]
    failed_jobs: Optional[int]
    state: str
    generation: int = 0
    rss_fresh: bool = False
    status_fresh: bool = False
    status_timestamp: Optional[str] = None
    status_age_seconds: Optional[float] = None


@dataclass
class VerificationResult:
    verified: bool
    reason: str
    duration_seconds: float = 0.0
    baseline_rss_bytes: Optional[int] = None
    rss_bytes: Optional[int] = None
    retained_cache_bytes: Optional[int] = None
    completed_jobs: Optional[int] = None
    failed_jobs: Optional[int] = None
    generation: Optional[int] = None
    completed_jobs_start: Optional[int] = None
    failed_jobs_start: Optional[int] = None


def _default_python_executable(backend_dir: Path) -> str:
    venv_python = backend_dir / ".venv" / (
        "Scripts/python.exe" if os.name == "nt" else "bin/python")
    return str(venv_python) if venv_python.exists() else sys.executable


def _worker_env(backend_dir: Path) -> dict:
    env = {
        key: value for key, value in os.environ.items()
        if key not in _CREDENTIAL_ENV_DENYLIST
        and not key.endswith("_API_KEY")
    }
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(backend_dir) + (
        os.pathsep + existing if existing else "")
    env["PYTHONUNBUFFERED"] = "1"
    return env


def _identity_of(proc: psutil.Process) -> Optional[ProcessIdentity]:
    try:
        return ProcessIdentity(pid=proc.pid, create_time=proc.create_time())
    except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
        return None


def _same_identity(proc: psutil.Process, identity: ProcessIdentity) -> bool:
    if proc.pid != identity.pid:
        return False
    try:
        return abs(proc.create_time() - identity.create_time) <= 0.01
    except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
        return False


class WorkerController:
    """Owns one worker child process for one generation at a time."""

    def __init__(
        self,
        python_executable: str | None = None,
        backend_dir: Path | None = None,
        command_timeout: float = DEFAULT_COMMAND_TIMEOUT,
        startup_timeout: float = DEFAULT_STARTUP_TIMEOUT,
        shutdown_timeout: float = DEFAULT_SHUTDOWN_TIMEOUT,
        rss_interval: float = RSS_SAMPLE_INTERVAL_SECONDS,
        verification_timeout: float = DEFAULT_VERIFICATION_TIMEOUT,
        status_poll_interval: float = STATUS_POLL_INTERVAL_SECONDS,
        status_timeout: float = STATUS_COMMAND_TIMEOUT_SECONDS,
    ) -> None:
        self._backend_dir = backend_dir or Path(__file__).resolve().parents[1]
        self._python = python_executable or _default_python_executable(
            self._backend_dir)
        self.command_timeout = command_timeout
        self.startup_timeout = startup_timeout
        self.shutdown_timeout = shutdown_timeout
        self.rss_interval = rss_interval
        self.verification_timeout = verification_timeout
        self.status_poll_interval = status_poll_interval
        self.status_timeout = status_timeout
        self.state = ControllerState.STOPPED
        self.state_history: list[dict] = []
        self.generation = 0
        self.baseline_rss_bytes: Optional[int] = None
        self.measurements: deque[Measurement] = deque(maxlen=4000)
        self.stderr_lines: deque[str] = deque(maxlen=200)
        self.last_worker_status: Optional[dict] = None
        self.detected_at: Optional[str] = None
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._child_pid: Optional[int] = None
        self._root_identity: Optional[ProcessIdentity] = None
        self._owned_identities: dict[int, ProcessIdentity] = {}
        self._seq = 0
        self._pending: dict[str, tuple[int, asyncio.Future]] = {}
        self._reader_task: Optional[asyncio.Task] = None
        self._stderr_task: Optional[asyncio.Task] = None
        self._sampler_task: Optional[asyncio.Task] = None
        self._status_task: Optional[asyncio.Task] = None
        self._baseline_samples: list[int] = []
        self._consecutive_over = 0
        self._stopping = False
        self._lifecycle_lock: Optional[asyncio.Lock] = None
        # Freshness-tracked latest samples (never silently re-timestamped).
        self._latest_rss_bytes: Optional[int] = None
        self._latest_rss_monotonic: Optional[float] = None
        self._latest_rss_timestamp: Optional[str] = None
        self._latest_rss_generation: Optional[int] = None
        self._latest_status: Optional[dict] = None
        self._latest_status_monotonic: Optional[float] = None
        self._latest_status_timestamp: Optional[str] = None
        self._latest_status_generation: Optional[int] = None
        self._status_available = False
        self._status_error: Optional[str] = None

    # -- properties / freshness -------------------------------------------
    @property
    def child_pid(self) -> Optional[int]:
        return self._child_pid

    @property
    def is_running(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    @property
    def owned_process_identities(self) -> list[dict]:
        return [identity.to_dict()
                for identity in self._owned_identities.values()]

    @property
    def latest_rss_bytes(self) -> Optional[int]:
        """Last measured RSS value, regardless of age (see fresh_rss_bytes)."""
        return self._latest_rss_bytes

    @property
    def rss_age_seconds(self) -> Optional[float]:
        if self._latest_rss_monotonic is None:
            return None
        return max(0.0, time.monotonic() - self._latest_rss_monotonic)

    @property
    def fresh_rss_bytes(self) -> Optional[int]:
        return self._fresh_rss_bytes_for(self.generation)

    @property
    def status_age_seconds(self) -> Optional[float]:
        if self._latest_status_monotonic is None:
            return None
        return max(0.0, time.monotonic() - self._latest_status_monotonic)

    @property
    def fresh_worker_status(self) -> Optional[dict]:
        if (
            self._latest_status is None
            or self._latest_status_generation != self.generation
            or not self._status_available
            or self.status_age_seconds is None
            or self.status_age_seconds > STATUS_FRESH_MAX_AGE_SECONDS
        ):
            return None
        return self._latest_status

    @property
    def peak_rss_bytes(self) -> Optional[int]:
        values = [m.rss_bytes for m in self.measurements
                  if m.rss_bytes is not None]
        return max(values) if values else None

    def _fresh_rss_bytes_for(self, generation: int) -> Optional[int]:
        if (
            self._latest_rss_bytes is None
            or self._latest_rss_generation != generation
            or self._latest_rss_monotonic is None
            or time.monotonic() - self._latest_rss_monotonic
            > RSS_FRESH_MAX_AGE_SECONDS
        ):
            return None
        return self._latest_rss_bytes

    def _set_state(self, new_state: ControllerState) -> None:
        if self.state != new_state:
            self.state = new_state
            self.state_history.append(
                {"timestamp": _utc_now_iso(), "state": new_state.value,
                 "generation": self.generation})

    def _lock(self) -> asyncio.Lock:
        if self._lifecycle_lock is None:
            self._lifecycle_lock = asyncio.Lock()
        return self._lifecycle_lock

    # -- freshness-tracked recording (generation-checked) ------------------
    def _record_status(self, generation: int, status: dict) -> bool:
        if generation != self.generation:
            return False
        self._latest_status = dict(status)
        self._latest_status_monotonic = time.monotonic()
        self._latest_status_timestamp = _utc_now_iso()
        self._latest_status_generation = generation
        self._status_available = True
        self._status_error = None
        self.last_worker_status = self._latest_status
        return True

    def _mark_status_unavailable(self, generation: int, reason: str) -> bool:
        if generation != self.generation:
            return False
        self._status_available = False
        self._status_error = reason[:256]
        return True

    def _record_measurement(
        self, generation: int, rss: int,
        identities: tuple[ProcessIdentity, ...] = (),
    ) -> bool:
        """Append one RSS measurement; stale generations cannot append."""
        if generation != self.generation:
            return False
        now_mono = time.monotonic()
        self._latest_rss_bytes = rss
        self._latest_rss_monotonic = now_mono
        self._latest_rss_timestamp = _utc_now_iso()
        self._latest_rss_generation = generation
        for identity in identities:
            self._owned_identities[identity.pid] = identity
        status = self.fresh_worker_status
        status_fresh = status is not None
        self.measurements.append(Measurement(
            timestamp=self._latest_rss_timestamp, monotonic=now_mono,
            rss_bytes=rss,
            retained_cache_bytes=(
                status.get("retained_cache_bytes") if status_fresh else None),
            completed_jobs=(
                status.get("completed_jobs") if status_fresh else None),
            failed_jobs=status.get("failed_jobs") if status_fresh else None,
            state=self.state.value,
            generation=generation,
            rss_fresh=True,
            status_fresh=status_fresh,
            status_timestamp=(
                self._latest_status_timestamp if status_fresh else None),
            status_age_seconds=(
                round(self.status_age_seconds, 3) if status_fresh else None),
        ))
        return True

    # -- lifecycle (serialized; internal helpers assume the lock is held) ---
    async def start(self) -> dict:
        """Spawn a fresh generation, establish baseline, return fresh status."""
        async with self._lock():
            return await self._start_locked()

    async def _start_locked(self) -> dict:
        if self.is_running:
            return await self.status()
        self.generation += 1
        generation = self.generation
        self._fail_all_pending(WorkerDeadError("generation replaced"))
        self.measurements.clear()
        self.baseline_rss_bytes = None
        self._baseline_samples = []
        self._consecutive_over = 0
        self.detected_at = None
        self.last_worker_status = None
        self._latest_status = None
        self._latest_status_generation = None
        self._status_available = False
        self._latest_rss_bytes = None
        self._latest_rss_generation = None
        self._latest_rss_monotonic = None
        self._stopping = False
        self._root_identity = None
        self._owned_identities = {}
        self._set_state(ControllerState.STARTING)
        try:
            self._proc = await asyncio.create_subprocess_exec(
                self._python, "-m", "aura_worker",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self._backend_dir),
                env=_worker_env(self._backend_dir),
            )
            proc = self._proc
            self._child_pid = proc.pid
            self._root_identity = await asyncio.to_thread(
                lambda: _identity_of(psutil.Process(proc.pid)))
            if self._root_identity is not None:
                self._owned_identities[proc.pid] = self._root_identity
            self._reader_task = asyncio.create_task(
                self._reader_loop(generation, proc))
            self._stderr_task = asyncio.create_task(
                self._stderr_loop(generation, proc))
            result = await asyncio.wait_for(
                self.send_command("initialize"), timeout=self.startup_timeout)
            if generation != self.generation:
                raise WorkerDeadError("generation replaced during startup")
            self._sampler_task = asyncio.create_task(
                self._sampler_loop(generation, proc))
            self._status_task = asyncio.create_task(
                self._status_poll_loop(generation, proc))
            await asyncio.wait_for(
                self._wait_for_baseline(generation),
                timeout=self.startup_timeout)
            if generation != self.generation:
                raise WorkerDeadError("generation replaced during startup")
            current = await asyncio.wait_for(
                self.status(), timeout=self.startup_timeout)
            if generation != self.generation:
                raise WorkerDeadError("generation replaced during startup")
            self._set_state(ControllerState.HEALTHY)
            return current
        except asyncio.CancelledError:
            await self._force_cleanup()
            self._set_state(ControllerState.STOPPED)
            raise
        except Exception:
            await self._force_cleanup()
            if not self._stopping:
                self._set_state(ControllerState.ERROR)
            raise

    async def _wait_for_baseline(self, generation: int) -> None:
        while self.baseline_rss_bytes is None:
            if generation != self.generation or not self.is_running:
                raise WorkerDeadError("worker died during baseline sampling")
            await asyncio.sleep(0.05)

    async def stop(self) -> None:
        async with self._lock():
            await self._stop_locked()

    async def _stop_if_generation(self, generation: int) -> bool:
        """Stop only if the caller's generation is still the current one."""
        async with self._lock():
            if generation != self.generation:
                return False
            await self._stop_locked()
            return True

    async def _stop_locked(self) -> None:
        if self._proc is None and self.state == ControllerState.STOPPED:
            return
        self._stopping = True
        proc = self._proc
        if proc is not None and proc.returncode is None:
            try:
                await asyncio.wait_for(
                    self.send_command("shutdown"), timeout=3.0)
            except Exception:
                pass
        await self._force_cleanup()
        self._set_state(ControllerState.STOPPED)
        self._stopping = False

    async def reset(self) -> dict:
        """Stop and start a fresh generation with reset counters/baseline."""
        async with self._lock():
            await self._stop_locked()
            return await self._start_locked()

    # -- identity-verified cleanup -----------------------------------------
    @staticmethod
    def _terminate_owned_processes(
        identities,
        process_factory: Callable = psutil.Process,
    ) -> list[int]:
        """Terminate only processes whose PID + creation time still match.

        A remembered PID whose current creation time differs is a recycled
        PID belonging to someone else and is never signalled.
        """
        verified: list = []
        terminated_pids: list[int] = []
        for identity in identities:
            try:
                proc = process_factory(identity.pid)
            except Exception:
                continue
            if not _same_identity(proc, identity):
                continue
            try:
                proc.terminate()
            except Exception:
                continue
            verified.append(proc)
            terminated_pids.append(identity.pid)
        if verified and process_factory is psutil.Process:
            _gone, alive = psutil.wait_procs(verified, timeout=3.0)
            for proc in alive:
                try:
                    proc.kill()
                except Exception:
                    pass
            if alive:
                psutil.wait_procs(alive, timeout=3.0)
        return terminated_pids

    @classmethod
    def _kill_owned_tree_sync(
        cls, root_identity: Optional[ProcessIdentity], identities,
    ) -> None:
        candidates: dict[int, ProcessIdentity] = {
            identity.pid: identity for identity in identities}
        if root_identity is not None:
            candidates[root_identity.pid] = root_identity
            try:
                root = psutil.Process(root_identity.pid)
                if _same_identity(root, root_identity):
                    for child in root.children(recursive=True):
                        identity = _identity_of(child)
                        if identity is not None:
                            candidates[identity.pid] = identity
            except Exception:
                pass
        cls._terminate_owned_processes(list(candidates.values()))

    async def _force_cleanup(self) -> None:
        proc = self._proc
        self._fail_all_pending(WorkerDeadError("worker is stopping"))
        current = asyncio.current_task()
        tasks = [
            task for task in (
                self._sampler_task, self._status_task,
                self._reader_task, self._stderr_task)
            if task is not None and task is not current
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            # Await cancelled background tasks before any replacement starts,
            # so an old generation can never write into the new one.
            await asyncio.gather(*tasks, return_exceptions=True)
        self._sampler_task = None
        self._status_task = None
        self._reader_task = None
        self._stderr_task = None
        if self._root_identity is not None or self._owned_identities:
            await asyncio.to_thread(
                self._kill_owned_tree_sync, self._root_identity,
                tuple(self._owned_identities.values()))
        if proc is not None:
            try:
                if proc.stdin is not None and not proc.stdin.is_closing():
                    proc.stdin.close()
            except Exception:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    pass
        if self._proc is proc:
            self._proc = None

    # -- protocol -----------------------------------------------------------
    def _fail_all_pending(self, exc: Exception) -> None:
        pending, self._pending = self._pending, {}
        for _gen, future in pending.values():
            if not future.done():
                future.set_exception(exc)

    async def send_command(self, command: str,
                           timeout: float | None = None) -> dict:
        return await self._send_command_bound(
            self.generation, self._proc, command, timeout)

    async def _send_command_bound(
        self, generation: int, proc, command: str,
        timeout: float | None = None,
    ) -> dict:
        if (
            generation != self.generation or proc is None
            or proc is not self._proc or proc.returncode is not None
            or proc.stdin is None
        ):
            raise WorkerDeadError()
        self._seq += 1
        request_id = f"g{generation}-{self._seq}"
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = (generation, future)
        payload = (
            json.dumps({"id": request_id, "command": command, "params": {}})
            + "\n"
        ).encode("utf-8")
        effective_timeout = timeout or self.command_timeout
        try:
            proc.stdin.write(payload)
            await asyncio.wait_for(proc.stdin.drain(),
                                   timeout=effective_timeout)
            response = await asyncio.wait_for(future,
                                              timeout=effective_timeout)
        except asyncio.CancelledError:
            # Cancelled futures must never linger in _pending.
            self._pending.pop(request_id, None)
            if not future.done():
                future.cancel()
            raise
        except asyncio.TimeoutError:
            self._pending.pop(request_id, None)
            raise WorkerError(
                "command_timeout", f"command {command} timed out") from None
        except (ConnectionResetError, BrokenPipeError):
            self._pending.pop(request_id, None)
            raise WorkerDeadError("worker pipe closed") from None
        if response.get("ok") is True:
            result = response["result"]
            if command in ("status", "initialize", "inject_cache_retention",
                           "restore_cache_policy"):
                # Only a still-current generation may publish status.
                self._record_status(generation, result)
            return result
        error = response.get("error") or {}
        raise WorkerError(str(error.get("code", "worker_error")),
                          str(error.get("message", "worker error")))

    async def status(self) -> dict:
        return await self.send_command("status")

    async def inject_cache_retention(self) -> dict:
        return await self.send_command("inject_cache_retention")

    async def restore_cache_policy(self) -> dict:
        return await self.send_command("restore_cache_policy")

    async def _reader_loop(self, generation: int, proc) -> None:
        assert proc.stdout is not None
        try:
            while True:
                raw = await proc.stdout.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    response = parse_response_line(line)
                except Exception:
                    if generation == self.generation:
                        self.stderr_lines.append(
                            f"discarded malformed worker line: {line[:120]}")
                    continue
                entry = self._pending.pop(str(response.get("id")), None)
                if entry is None:
                    continue  # stale/unknown generation reply: ignore
                reply_generation, future = entry
                if reply_generation != generation or future.done():
                    continue
                future.set_result(response)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        if generation == self.generation and not self._stopping:
            self._handle_worker_death(generation)

    async def _stderr_loop(self, generation: int, proc) -> None:
        assert proc.stderr is not None
        try:
            while True:
                raw = await proc.stderr.readline()
                if not raw:
                    break
                if generation == self.generation:
                    self.stderr_lines.append(
                        raw.decode("utf-8", errors="replace").rstrip()[:512])
        except (asyncio.CancelledError, Exception):
            pass

    def _handle_worker_death(self, generation: int) -> None:
        if generation != self.generation or self._stopping:
            return
        if self.state != ControllerState.STOPPED:
            self._set_state(ControllerState.DEAD)
        self._fail_all_pending(WorkerDeadError("worker process exited"))

    # -- RSS sampling + independent status polling --------------------------
    async def _read_child_rss(
        self, pid: int,
    ) -> tuple[Optional[int], tuple[ProcessIdentity, ...]]:
        def _read() -> tuple[Optional[int], tuple[ProcessIdentity, ...]]:
            # The venv python.exe is a small launcher shim whose real
            # interpreter runs as its child; measure the owned tree so the
            # sampled value is the worker's actual RSS, not the shim's.
            try:
                root = psutil.Process(pid)
                processes = [root] + root.children(recursive=True)
            except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
                return None, ()
            identities = tuple(
                identity for identity in
                (_identity_of(proc) for proc in processes)
                if identity is not None)
            total = 0
            alive = False
            for proc in processes:
                try:
                    total += proc.memory_info().rss
                    alive = True
                except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
                    continue
            return (total if alive else None), identities
        return await asyncio.to_thread(_read)

    async def _sampler_loop(self, generation: int, proc) -> None:
        pid = proc.pid
        next_tick = time.monotonic()
        try:
            while generation == self.generation and not self._stopping:
                rss, identities = await self._read_child_rss(pid)
                if generation != self.generation or self._stopping:
                    return  # old sampler must never append into a new gen
                if rss is None:
                    self._handle_worker_death(generation)
                    return
                if self._record_measurement(generation, rss, identities):
                    self._update_baseline_and_detection(rss, generation)
                next_tick += self.rss_interval
                delay = next_tick - time.monotonic()
                if delay <= 0:
                    next_tick = time.monotonic()
                    continue
                await asyncio.sleep(delay)
        except asyncio.CancelledError:
            raise

    async def _status_poll_loop(self, generation: int, proc) -> None:
        """Poll status independently of API consumers, with bounded timeout.

        A hung status request times out and marks status unavailable; the
        RSS sampler runs in its own task and keeps its 500 ms cadence.
        """
        next_tick = time.monotonic()
        try:
            while generation == self.generation and not self._stopping:
                try:
                    await self._send_command_bound(
                        generation, proc, "status",
                        timeout=self.status_timeout)
                except asyncio.CancelledError:
                    raise
                except WorkerError as exc:
                    self._mark_status_unavailable(
                        generation, f"{exc.code}: {exc.message}")
                    if exc.code == "worker_dead":
                        self._handle_worker_death(generation)
                        return
                if generation != self.generation or self._stopping:
                    return
                next_tick += self.status_poll_interval
                delay = next_tick - time.monotonic()
                if delay <= 0:
                    next_tick = time.monotonic()
                    continue
                await asyncio.sleep(delay)
        except asyncio.CancelledError:
            raise

    def _update_baseline_and_detection(
        self, rss: int, generation: Optional[int] = None,
    ) -> bool:
        if generation is not None and generation != self.generation:
            return False
        if self.baseline_rss_bytes is None:
            warmed = bool(
                (self.last_worker_status or {}).get("initialized"))
            if self.state == ControllerState.STARTING and warmed:
                self._baseline_samples.append(rss)
                if len(self._baseline_samples) >= BASELINE_SAMPLE_COUNT:
                    self.baseline_rss_bytes = int(statistics.median(
                        self._baseline_samples))
            return True
        threshold = self.baseline_rss_bytes + RSS_THRESHOLD_OFFSET_BYTES
        if rss > threshold:
            self._consecutive_over += 1
            if (self._consecutive_over >= DETECTION_CONSECUTIVE_SAMPLES
                    and self.state in (ControllerState.HEALTHY,
                                       ControllerState.RECOVERED)):
                self.detected_at = _utc_now_iso()
                self._set_state(ControllerState.FAULT_DETECTED)
        else:
            self._consecutive_over = 0
        return True

    async def wait_for_detection(self, timeout: float = 30.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.state == ControllerState.FAULT_DETECTED:
                return True
            if self.state in (ControllerState.DEAD, ControllerState.ERROR,
                              ControllerState.STOPPED):
                return False
            await asyncio.sleep(0.1)
        return self.state == ControllerState.FAULT_DETECTED

    # -- independent recovery verification (generation-bound) ---------------
    def _stale_result(self, generation: int, started: float,
                      last_status: Optional[dict]) -> VerificationResult:
        status = last_status or {}
        return VerificationResult(
            False, "stale_generation", time.monotonic() - started,
            self.baseline_rss_bytes, self._fresh_rss_bytes_for(generation),
            status.get("retained_cache_bytes"), status.get("completed_jobs"),
            status.get("failed_jobs"), generation=generation)

    async def restore_and_verify(
        self, timeout: float | None = None
    ) -> VerificationResult:
        """Restore policy, then independently verify for 8 stable seconds.

        Bound to the generation captured at entry: a concurrent reset/stop
        aborts the old verifier, which never mutates or stops the
        replacement. Only fresh RSS (<=1.5 s old, same generation) and fresh
        worker status can satisfy the window; stale/unavailable data resets
        it. The acknowledgement alone never proves recovery.
        """
        generation = self.generation
        proc = self._proc
        overall_timeout = timeout or self.verification_timeout
        started = time.monotonic()
        last_status: Optional[dict] = None
        try:
            restored = await self._send_command_bound(
                generation, proc, "restore_cache_policy")
        except asyncio.CancelledError:
            # Cleanup only the generation this verifier belongs to.
            await asyncio.shield(self._stop_if_generation(generation))
            raise
        except WorkerError as exc:
            if generation != self.generation:
                return self._stale_result(generation, started, last_status)
            return VerificationResult(
                False, f"restore_failed:{exc.code}", 0.0,
                self.baseline_rss_bytes, self.fresh_rss_bytes,
                generation=generation)
        if generation != self.generation or proc is not self._proc:
            return self._stale_result(generation, started, restored)
        last_status = restored
        failed_at_restore = int(restored.get("failed_jobs", 0))
        completed_at_restore = int(restored.get("completed_jobs", 0))
        baseline = self.baseline_rss_bytes
        self._set_state(ControllerState.VERIFYING)
        window_start: Optional[float] = None
        window_completed_start: Optional[int] = None
        last_completed: Optional[int] = None
        last_progress_at = time.monotonic()
        try:
            while time.monotonic() - started < overall_timeout:
                if generation != self.generation or proc is not self._proc:
                    return self._stale_result(
                        generation, started, last_status)
                if not self.is_running:
                    self._handle_worker_death(generation)
                    return VerificationResult(
                        False, "worker_dead", time.monotonic() - started,
                        baseline, self._fresh_rss_bytes_for(generation),
                        (last_status or {}).get("retained_cache_bytes"),
                        (last_status or {}).get("completed_jobs"),
                        (last_status or {}).get("failed_jobs"),
                        generation=generation,
                        completed_jobs_start=completed_at_restore,
                        failed_jobs_start=failed_at_restore)
                try:
                    last_status = await self._send_command_bound(
                        generation, proc, "status")
                except WorkerDeadError:
                    if generation != self.generation:
                        return self._stale_result(
                            generation, started, last_status)
                    return VerificationResult(
                        False, "worker_dead", time.monotonic() - started,
                        baseline, self._fresh_rss_bytes_for(generation),
                        generation=generation,
                        completed_jobs_start=completed_at_restore,
                        failed_jobs_start=failed_at_restore)
                except WorkerError as exc:
                    if generation != self.generation:
                        return self._stale_result(
                            generation, started, last_status)
                    return VerificationResult(
                        False, f"status_failed:{exc.code}",
                        time.monotonic() - started, baseline,
                        self._fresh_rss_bytes_for(generation),
                        generation=generation,
                        completed_jobs_start=completed_at_restore,
                        failed_jobs_start=failed_at_restore)
                if generation != self.generation or proc is not self._proc:
                    return self._stale_result(
                        generation, started, last_status)
                rss = self._fresh_rss_bytes_for(generation)
                completed = int(last_status.get("completed_jobs", 0))
                failed = int(last_status.get("failed_jobs", 0))
                if last_completed is None or completed > last_completed:
                    last_progress_at = time.monotonic()
                if completed < (last_completed or 0):
                    window_start = None
                last_completed = completed
                conditions_ok = (
                    baseline is not None
                    and rss is not None  # stale/unavailable RSS never verifies
                    and last_status.get("retained_cache_bytes")
                    == HEALTHY_CACHE_BYTES
                    and rss <= baseline + RSS_THRESHOLD_OFFSET_BYTES
                    and failed == failed_at_restore
                )
                if not conditions_ok:
                    window_start = None
                    window_completed_start = None
                else:
                    if window_start is None:
                        window_start = time.monotonic()
                        window_completed_start = completed
                        last_progress_at = time.monotonic()
                    elif time.monotonic() - last_progress_at > 2.0:
                        window_start = None
                        window_completed_start = None
                    elif (time.monotonic() - window_start
                          >= VERIFICATION_DURATION_SECONDS
                          and window_completed_start is not None
                          and completed > window_completed_start):
                        if generation != self.generation:
                            return self._stale_result(
                                generation, started, last_status)
                        self._set_state(ControllerState.RECOVERED)
                        return VerificationResult(
                            True, "verified", time.monotonic() - window_start,
                            baseline, rss,
                            last_status.get("retained_cache_bytes"),
                            completed, failed, generation=generation,
                            completed_jobs_start=window_completed_start,
                            failed_jobs_start=failed_at_restore)
                await asyncio.sleep(0.25)
                if generation != self.generation or proc is not self._proc:
                    return self._stale_result(
                        generation, started, last_status)
        except asyncio.CancelledError:
            await asyncio.shield(self._stop_if_generation(generation))
            raise
        if generation != self.generation or proc is not self._proc:
            return self._stale_result(generation, started, last_status)
        if self.is_running:
            self._set_state(ControllerState.FAULT_DETECTED)
        return VerificationResult(
            False, "verification_timeout", time.monotonic() - started,
            baseline, self._fresh_rss_bytes_for(generation),
            (last_status or {}).get("retained_cache_bytes"),
            (last_status or {}).get("completed_jobs"),
            (last_status or {}).get("failed_jobs"), generation=generation,
            completed_jobs_start=completed_at_restore,
            failed_jobs_start=failed_at_restore)

    def snapshot(self) -> dict:
        fresh_status = self.fresh_worker_status
        status = fresh_status or {}
        return {
            "state": self.state.value,
            "generation": self.generation,
            "child_pid": self._child_pid,
            "child_running": self.is_running,
            "baseline_rss_bytes": self.baseline_rss_bytes,
            "rss_bytes": self.fresh_rss_bytes,
            "last_rss_bytes": self.latest_rss_bytes,
            "rss_fresh": self.fresh_rss_bytes is not None,
            "rss_age_seconds": (
                round(self.rss_age_seconds, 3)
                if self.rss_age_seconds is not None else None),
            "rss_sample_timestamp": self._latest_rss_timestamp,
            "peak_rss_bytes": self.peak_rss_bytes,
            "worker_status": fresh_status,
            "worker_status_stale": fresh_status is None,
            "worker_status_error": self._status_error,
            "worker_status_timestamp": self._latest_status_timestamp,
            "worker_status_age_seconds": (
                round(self.status_age_seconds, 3)
                if self.status_age_seconds is not None else None),
            "retained_cache_bytes": status.get("retained_cache_bytes"),
            "completed_jobs": status.get("completed_jobs"),
            "failed_jobs": status.get("failed_jobs"),
            "detected_at": self.detected_at,
            "owned_processes": self.owned_process_identities,
            "measurements": [asdict(m) for m in self.measurements],
            "state_history": list(self.state_history),
        }
