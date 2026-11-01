"""Isolated cache worker child process (SIM-001).

Reads typed JSON lines on stdin, writes responses on stdout, logs to
stderr. A daemon scheduler thread processes 4 synthetic jobs/second using
monotonic time, so job processing continues while the main thread waits
for commands. No credentials, shell, code or paths are accepted.
"""
from __future__ import annotations

import json
import sys
import threading
import time

from .cache import JOB_INTERVAL_SECONDS, CacheSimulator
from .protocol import ProtocolError, make_error, make_success, parse_request_line


def _log(message: str) -> None:
    print(f"[aura-worker] {message}", file=sys.stderr, flush=True)


class WorkerApp:
    def __init__(self, cache: CacheSimulator | None = None) -> None:
        self.cache = cache or CacheSimulator()
        self._stop = threading.Event()
        self._scheduler: threading.Thread | None = None

    def _scheduler_loop(self) -> None:
        next_tick = time.monotonic()
        last_rejection_log = float('-inf')
        while not self._stop.is_set():
            next_tick += JOB_INTERVAL_SECONDS
            try:
                accepted = self.cache.process_one_job()
                if not accepted and time.monotonic() - last_rejection_log >= 1.0:
                    status = self.cache.snapshot()
                    _log('job rejected: cache ceiling reached; '
                         f"retained_cache_bytes={status['retained_cache_bytes']}; "
                         f"ceiling_bytes={status['ceiling_bytes']}; "
                         f"eviction_enabled={status['eviction_enabled']}; "
                         f"failed_jobs={status['failed_jobs']}")
                    last_rejection_log = time.monotonic()
            except RuntimeError:
                pass  # not initialized yet; jobs only count after initialize
            except Exception as exc:  # keep scheduler alive, surface on stderr
                _log(f"scheduler job error: {exc!r}")
            delay = next_tick - time.monotonic()
            if delay <= 0:
                next_tick = time.monotonic()
            else:
                self._stop.wait(delay)

    def handle(self, line: str) -> tuple[str, bool]:
        """Handle one line; returns (response_line, should_exit)."""
        try:
            request_id, command, _params = parse_request_line(line)
        except ProtocolError as exc:
            return make_error(exc.request_id, exc.code, exc.message), False
        try:
            if command == "initialize":
                result = self.cache.initialize()
                if self._scheduler is None:
                    self._scheduler = threading.Thread(
                        target=self._scheduler_loop, name="job-scheduler",
                        daemon=True,
                    )
                    self._scheduler.start()
                return make_success(request_id, result), False
            if command == "inject_cache_retention":
                return make_success(
                    request_id, self.cache.inject_cache_retention()), False
            if command == "restore_cache_policy":
                return make_success(
                    request_id, self.cache.restore_cache_policy()), False
            if command == "status":
                return make_success(request_id, self.cache.snapshot()), False
            if command == "shutdown":
                result = self.cache.snapshot()
                return make_success(request_id, result), True
        except RuntimeError as exc:
            code = str(exc) if str(exc) in (
                "already_initialized", "not_initialized") else "worker_error"
            return make_error(request_id, code, str(exc)), False
        except Exception as exc:  # bounded error, framing stays intact
            _log(f"command error: {exc!r}")
            return make_error(request_id, "worker_error", "internal worker error"), False
        return make_error(request_id, "unknown_command", "unknown command"), False

    def shutdown_cleanup(self) -> None:
        self._stop.set()
        if self._scheduler is not None:
            self._scheduler.join(timeout=2.0)
        self.cache.close()


def main() -> int:
    try:
        sys.stdout.reconfigure(line_buffering=True)  # type: ignore[attr-defined]
    except Exception:
        pass
    app = WorkerApp()
    _log("started; waiting for commands on stdin")
    try:
        for raw_line in sys.stdin:
            response, should_exit = app.handle(raw_line)
            sys.stdout.write(response + "\n")
            sys.stdout.flush()
            if should_exit:
                break
    except KeyboardInterrupt:
        pass
    finally:
        app.shutdown_cleanup()
        _log("exiting")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
