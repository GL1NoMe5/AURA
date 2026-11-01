"""Public demo surface. Deliberately does NOT mount /api/v2 or the local API."""
from __future__ import annotations

from contextlib import asynccontextmanager
import hmac
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse

from aura_ops.models import OpsError
from .config import DemoSettings
from .ledger import DemoLedger
from .runner import DemoRunner

COOKIE = "aura_demo_session"


class InstanceLock:
    def __init__(self, folder):
        folder.mkdir(parents=True, exist_ok=True)
        self.file = (folder / "server.lock").open("a+b")
        self.file.seek(0)
        self.file.write(b"1")
        self.file.flush()
        self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise RuntimeError("The demo already has an active server; use one worker") from None

    def close(self):
        self.file.close()


def create_demo_app(settings=None, *, runner_factory=DemoRunner):
    settings = settings or DemoSettings.from_env()

    @asynccontextmanager
    async def lifespan(app):
        lock = InstanceLock(settings.data)
        ledger = DemoLedger(settings)
        runner = runner_factory(settings, ledger)
        app.state.ledger, app.state.runner = ledger, runner
        try:
            await runner.startup()
            yield
        finally:
            await runner.shutdown()
            ledger.close()
            lock.close()

    app = FastAPI(title="AURA live demonstration", docs_url=None, redoc_url=None,
                  openapi_url=None, lifespan=lifespan)

    def failure(code, message, status):
        return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)

    @app.exception_handler(OpsError)
    async def ops_error(_request, exc):
        return failure(exc.code, exc.message, exc.status)

    @app.middleware("http")
    async def boundary(request, call_next):
        if request.headers.get("host", "").lower() != urlsplit(settings.origin).netloc.lower():
            return failure("host_forbidden", "Host is not allowed.", 400)
        if request.method not in ("GET", "HEAD", "POST", "DELETE"):
            return failure("method_not_allowed", "Method is not allowed.", 405)
        if request.method in ("POST", "DELETE"):
            if request.headers.get("origin") != settings.origin or request.headers.get("x-aura-demo") != "1":
                return failure("origin_forbidden", "Reload this page before continuing.", 403)
            if request.headers.get("content-type", "").split(";")[0] != "application/json":
                return failure("content_type", "JSON is required.", 415)
            # Enforced on streamed bytes, not merely an untrusted Content-Length.
            data = bytearray()
            async for chunk in request.stream():
                if len(data) + len(chunk) > 1024:
                    return failure("body_limit", "Request is too large.", 413)
                data.extend(chunk)
            request.state.body = bytes(data)
        try:
            response = await call_next(request)
        except Exception:
            response = failure("demo_unavailable", "The demonstration is temporarily unavailable.", 503)
        response.headers["Cache-Control"] = "no-store" if request.url.path.startswith("/api/") else "no-cache"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; font-src 'self' data:; connect-src 'self'; media-src 'self' blob:; "
            "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        return response

    def session(request):
        value = app.state.ledger.session(request.cookies.get(COOKIE))
        if not value:
            raise OpsError("sign_in_required", "Enter the demo access code to run a recovery.", 401)
        return value

    def body(request, allowed=()):
        try:
            data = json.loads(request.state.body)
            if not isinstance(data, dict) or set(data) != set(allowed):
                raise ValueError()
            return data
        except (ValueError, TypeError):
            raise OpsError("invalid_body", "Unexpected request fields.", 400) from None

    @app.get("/health")
    async def health():
        return {"status": "ok", "mode": "judging-demo"}

    @app.get("/api/demo/status")
    async def status(request: Request):
        ledger = app.state.ledger
        viewer = ledger.session(request.cookies.get(COOKIE))
        row = ledger.latest(viewer) if viewer else None
        return {"authenticated": bool(viewer), "provider_ready": app.state.runner.ready(),
                "busy": bool(ledger.active()), "run": json.loads(row["report"]) if row else None,
                "limits": {"runs_per_visit": settings.session_runs, "calls_per_run": settings.calls_per_run},
                "mode": "live", "model": "meta-models/muse-spark-open-1026"}

    @app.post("/api/demo/session")
    async def login(request: Request):
        ledger = app.state.ledger
        ledger.login_attempt()
        data = body(request, ("code",))
        code = data["code"]
        if not isinstance(code, str) or not hmac.compare_digest(code.encode(), settings.access_code.encode()):
            raise OpsError("invalid_code", "That access code was not recognized.", 401)
        # An existing authenticated visit keeps its quota rather than gaining a new session.
        token = request.cookies.get(COOKIE)
        if not ledger.session(token):
            token = ledger.new_session()
        response = JSONResponse({"authenticated": True})
        response.set_cookie(COOKIE, token, max_age=43200, httponly=True,
                            secure=settings.origin.startswith("https://"), samesite="strict", path="/")
        return response

    @app.delete("/api/demo/session")
    async def logout(request: Request):
        body(request)
        app.state.ledger.logout(request.cookies.get(COOKIE))
        response = JSONResponse({"authenticated": False})
        response.delete_cookie(COOKIE, path="/")
        return response

    @app.post("/api/demo/runs", status_code=202)
    async def start(request: Request):
        viewer = session(request)
        body(request)
        if not app.state.runner.ready():
            raise OpsError("provider_unavailable", "The model connection is currently unavailable.", 503)
        run_id, created = app.state.ledger.reserve(viewer, request.headers.get("idempotency-key"))
        if created:
            app.state.runner.launch(run_id)
        return {"run_id": run_id, "created": created}

    @app.post("/api/demo/runs/{run_id}/cancel")
    async def cancel(run_id: str, request: Request):
        viewer = session(request)
        body(request)
        row = app.state.ledger.row(run_id)
        if not row or row["session"] != viewer:
            raise OpsError("run_not_found", "Run was not found.", 404)
        if not app.state.runner.cancel_requested:
            await app.state.runner.cancel(run_id)
        return {"stopped": True}

    @app.get("/{path:path}")
    async def static(path: str):
        if not path:
            return RedirectResponse("/try")
        if path in ("try", "try/"):
            path = "index.html"
        elif not path.startswith(("assets/", "portrait/", "audio/")):
            return failure("not_found", "Not found.", 404)
        root = (settings.root / "frontend/dist").resolve()
        file = (root / path).resolve()
        if root not in file.parents or not file.is_file() or file.is_symlink():
            return failure("not_found", "Not found.", 404)
        return FileResponse(file)

    return app
