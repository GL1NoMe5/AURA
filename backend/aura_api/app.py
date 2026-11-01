"""FastAPI application factory for the AURA local API (API-001).

Run locally (single uvicorn process, no reload, so the lifespan owns
exactly one worker controller):

    backend\\.venv\\Scripts\\python.exe -m uvicorn aura_api:app --host 127.0.0.1 --port 8000

Tests inject deterministic orchestrators/controllers only through this
Python factory; there is no HTTP/header/environment selection switch.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Callable, Optional

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse

from .config import ApiSettings, detect_provider_configured, is_origin_allowed
from .manager import ApiError, IncidentManager
from .orchestration import Orchestrator
from .schemas import (
    ErrorResponse,
    HealthResponse,
    IncidentAccepted,
    IncidentRequest,
)

_MUTATION_PATHS = frozenset({"/api/incidents", "/api/reset"})


def _error_response(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=ErrorResponse(
            error={"code": code, "message": message}).model_dump(),
    )


def _build_default_orchestrator(project_root: Path):
    """Real Baseten swarm + Codex chart renderer, locally configured.

    Imported lazily inside the factory to avoid aura_api/aura_agents
    import cycles. No provider request happens at construction, import,
    startup, health, state or reconnect; a missing key simply leaves
    the orchestrator unready (503 before injection).
    """
    from aura_agents.adapter import BasetenAdapter
    from aura_agents.chart import render_measured_chart
    from aura_agents.orchestrator import BasetenSwarmOrchestrator

    adapter = BasetenAdapter(project_root=project_root)
    return BasetenSwarmOrchestrator(
        adapter, chart_renderer=render_measured_chart,
        evidence_dir=project_root / ".local" / "agents")


def create_app(
    *,
    orchestrator: Optional[Orchestrator] = None,
    controller_factory: Optional[Callable] = None,
    provider_configured: Optional[bool] = None,
    settings: Optional[ApiSettings] = None,
    wire_default_orchestrator: bool = False,
    static_dir: Optional[Path] = None,
    enable_ops: bool = False,
    enable_demo: bool = True,
    ops_factory: Optional[Callable] = None,
) -> FastAPI:
    project_root = Path(__file__).resolve().parents[2]
    resolved_static_dir = (
        static_dir if static_dir is not None
        else project_root / "frontend" / "dist")
    controller = controller_factory() if controller_factory else None
    if orchestrator is None and wire_default_orchestrator:
        orchestrator = _build_default_orchestrator(project_root)
    manager = IncidentManager(
        controller=controller,
        orchestrator=orchestrator,
        settings=settings,
        provider_configured=(
            detect_provider_configured(project_root)
            if provider_configured is None else provider_configured),
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            if enable_demo:
                await manager.startup()
            if enable_ops:
                from aura_ops.service import OpsService
                _app.state.ops = ops_factory() if ops_factory else OpsService(project_root)
                await _app.state.ops.startup()
            yield
        finally:
            # Close worker and provider adapter even on startup failure.
            await manager.shutdown()
            if getattr(_app.state, "ops", None) is not None:
                await _app.state.ops.shutdown()

    app = FastAPI(title="AURA local API", lifespan=lifespan)
    app.state.manager = manager
    app.state.incident_manager = manager

    @app.middleware("http")
    async def local_origin_guard(request: Request, call_next):
        origin = request.headers.get("origin")
        if request.method == "OPTIONS" and origin is not None:
            if not is_origin_allowed(origin):
                return _error_response(
                    403, "origin_forbidden", "Origin is not allowed.")
            return PlainTextResponse(
                status_code=204,
                headers={
                    "Access-Control-Allow-Origin": origin,
                    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                    "Access-Control-Allow-Headers": "content-type",
                    "Vary": "Origin",
                })
        if request.method == "POST" and (request.url.path in _MUTATION_PATHS or request.url.path.startswith("/api/v2/")) \
                and not is_origin_allowed(origin):
            return _error_response(
                403, "origin_forbidden", "Origin is not allowed.")
        response = await call_next(request)
        if origin is not None and is_origin_allowed(origin):
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Vary"] = "Origin"
        return response

    @app.exception_handler(ApiError)
    async def api_error_handler(_request: Request, exc: ApiError):
        return _error_response(exc.status_code, exc.code, exc.message)

    @app.exception_handler(Exception)
    async def unhandled_error_handler(_request: Request, _exc: Exception):
        return _error_response(
            500, "internal_error", "Internal error.")

    @app.get("/health", response_model=HealthResponse)
    async def health():
        controller_state = getattr(manager.controller.state, "value",
                                   str(manager.controller.state))
        worker_running = bool(manager.controller.is_running)
        return HealthResponse(
            status="ok" if worker_running or enable_ops else "degraded",
            provider_configured=manager.provider_configured,
            orchestrator_ready=manager.orchestrator_ready,
            worker_state=str(controller_state),
            worker_running=worker_running,
            server_instance_id=manager.server_instance_id,
        )

    @app.get("/api/state")
    async def state():
        return manager.snapshot()

    @app.post("/api/incidents", status_code=202,
              response_model=IncidentAccepted)
    async def create_incident(body: IncidentRequest):
        if not enable_demo:
            return _error_response(410, "demo_disabled", "The demo worker is disabled. Enroll an application in Operations.")
        run = await manager.start_incident(body.scenario)
        return IncidentAccepted(
            run_id=run.run_id, phase=run.phase, scenario=run.scenario)

    @app.post("/api/reset")
    async def reset():
        if not enable_demo:
            return _error_response(410, "demo_disabled", "Reset applies only to the demonstration worker.")
        return await manager.reset()

    @app.websocket("/ws/telemetry")
    async def telemetry_socket(websocket: WebSocket):
        origin = websocket.headers.get("origin")
        if not is_origin_allowed(origin):
            await websocket.close(code=4403)
            return
        queue = manager.subscribe()
        try:
            await websocket.accept()
            snapshot = manager.create_snapshot_event()
            await websocket.send_json(snapshot)

            async def sender() -> None:
                last_seq = snapshot["seq"]
                while True:
                    event = await queue.get()
                    # Events queued during accept may predate the snapshot.
                    if event["seq"] <= last_seq:
                        continue
                    await websocket.send_json(event)
                    last_seq = event["seq"]

            async def receiver() -> None:
                while True:
                    await websocket.receive_text()

            send_task = asyncio.create_task(sender())
            receive_task = asyncio.create_task(receiver())
            try:
                done, _pending = await asyncio.wait(
                    {send_task, receive_task},
                    return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    if task.exception() is not None and not isinstance(
                            task.exception(), WebSocketDisconnect):
                        raise task.exception()
            finally:
                send_task.cancel()
                receive_task.cancel()
                await asyncio.gather(
                    send_task, receive_task, return_exceptions=True)
        except WebSocketDisconnect:
            pass
        finally:
            manager.unsubscribe(queue)

    if enable_ops:
        from aura_ops.models import OpsError
        from aura_ops.router import router as ops_router
        @app.exception_handler(OpsError)
        async def ops_error_handler(_request: Request, exc: OpsError):
            return _error_response(exc.status, exc.code, exc.message)
        app.include_router(ops_router)

    # Static frontend serving is registered AFTER all API/WS routes.
    # Unknown API paths stay JSON 404s; only files physically inside the
    # built frontend/dist tree can ever be served.
    @app.get("/{full_path:path}", include_in_schema=False)
    async def serve_frontend(full_path: str):
        if full_path == "api" or full_path.startswith("api/") \
                or full_path == "ws" or full_path.startswith("ws/"):
            return _error_response(404, "not_found", "API route not found.")
        static_root = resolved_static_dir.resolve()
        index_file = static_root / "index.html"
        if not index_file.is_file():
            return _error_response(
                503, "frontend_build_missing",
                "Frontend build is missing. Run Start-AURA.cmd --setup.")
        parts = [part for part in full_path.split("/") if part not in ("", ".")]
        if any(part == ".." or part.startswith(".") for part in parts):
            return _error_response(404, "not_found", "File not found.")
        if not parts:
            return FileResponse(index_file)
        candidate = static_root.joinpath(*parts).resolve()
        if candidate != static_root and static_root not in candidate.parents:
            return _error_response(404, "not_found", "File not found.")
        if candidate.is_file():
            return FileResponse(candidate)
        if Path(parts[-1]).suffix:
            return _error_response(404, "not_found", "File not found.")
        return FileResponse(index_file)  # SPA fallback for client routes

    return app


# Shipped default app: real worker plus the real Baseten swarm and Codex
# chart renderer when locally configured. Without a local key the
# orchestrator is unready and incidents return 503 before injection.
# Construction/import/startup/reads make no provider requests.
app = create_app(enable_ops=True, enable_demo=False)

# Uvicorn's "aura_api:app" target must resolve to this FastAPI instance,
# not to the aura_api.app submodule; publish it on the parent package.
import sys as _sys

_sys.modules[__package__].__dict__["app"] = app
