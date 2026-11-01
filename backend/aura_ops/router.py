"""Local HTTP/WS interface for enrolled external applications."""
from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse

from aura_api.config import is_origin_allowed
from .models import (ApprovalRequest, DeleteTargetRequest, InvestigationRequest, OpsError,
                     PatchRequest, Policy, ReconciliationConfirmRequest, TargetSpec,
                     UpdateTargetRequest)


router = APIRouter()


def service(request):
    instance = getattr(request.app.state, "ops", None)
    if instance is None:
        raise OpsError("ops_unavailable", "Operations service is not enabled.", 503)
    return instance


def _require_allowed_origin(request: Request) -> None:
    if not is_origin_allowed(request.headers.get("origin")):
        raise OpsError("origin_forbidden", "Origin is not allowed.", 403)


@router.get("/api/v2/status")
async def status(request: Request):
    return service(request).snapshot()


@router.get("/api/v2/targets")
async def targets(request: Request):
    return service(request).list_targets()


@router.post("/api/v2/targets", status_code=201)
async def enroll(body: TargetSpec, request: Request):
    return await service(request).enroll(body)


@router.get("/api/v2/targets/{target_id}")
async def target(target_id: str, request: Request):
    return service(request).target_view(target_id)


@router.put("/api/v2/targets/{target_id}")
async def update_target(target_id: str, body: UpdateTargetRequest, request: Request):
    _require_allowed_origin(request)
    return await service(request).update_target(target_id, body.expected_revision, body.spec)


@router.delete("/api/v2/targets/{target_id}")
async def remove_target(target_id: str, body: DeleteTargetRequest, request: Request):
    _require_allowed_origin(request)
    return await service(request).remove_target(target_id, body.expected_revision)


@router.post("/api/v2/targets/{target_id}/reconciliations", status_code=201)
async def collect_reconciliation(target_id: str, request: Request):
    return await service(request).collect_reconciliation(target_id)


@router.get("/api/v2/targets/{target_id}/reconciliations/{review_id}")
async def get_reconciliation(target_id: str, review_id: str, request: Request):
    return service(request).get_reconciliation(target_id, review_id)


@router.post("/api/v2/targets/{target_id}/reconciliations/{review_id}/confirm")
async def confirm_reconciliation(target_id: str, review_id: str,
                                 body: ReconciliationConfirmRequest, request: Request):
    return await service(request).confirm_reconciliation(
        target_id, review_id, review_hash=body.review_hash,
        attestation=body.attestation, operator_note=body.operator_note)


@router.post("/api/v2/targets/{target_id}/policy")
async def policy(target_id: str, body: Policy, request: Request):
    return await service(request).set_policy(target_id, body)


@router.post("/api/v2/targets/{target_id}/disconnect")
async def disconnect(target_id: str, request: Request):
    return await service(request).disconnect(target_id)


@router.post("/api/v2/targets/{target_id}/reconnect")
async def reconnect(target_id: str, request: Request):
    return await service(request).reconnect(target_id)


@router.post("/api/v2/targets/{target_id}/snapshot")
async def capture(target_id: str, request: Request):
    return await service(request).capture_snapshot(target_id)


@router.get("/api/v2/investigations")
async def investigations(request: Request, target_id: str | None = None):
    return service(request).store.list_investigations(target_id)


@router.post("/api/v2/investigations", status_code=202)
async def investigate(body: InvestigationRequest, request: Request):
    return await service(request).start_investigation(body.target_id, body.question)


@router.get("/api/v2/investigations/{investigation_id}")
async def investigation(investigation_id: str, request: Request):
    return service(request).investigation(investigation_id)


@router.post("/api/v2/investigations/{investigation_id}/cancel")
async def cancel(investigation_id: str, request: Request):
    return await service(request).cancel(investigation_id)


@router.get("/api/v2/proposals")
async def proposals(request: Request):
    return service(request).store.list_proposals()


@router.post("/api/v2/proposals/{proposal_id}/decision", status_code=202)
async def decision(proposal_id: str, body: ApprovalRequest, request: Request):
    return await service(request).decide(proposal_id, body.proposal_hash, body.approve)


@router.post("/api/v2/patches", status_code=202)
async def patch(body: PatchRequest, request: Request):
    return await service(request).prepare_patch(body.investigation_id)


@router.get("/api/v2/events")
async def events(request: Request, after: int = 0):
    return service(request).store.events_since(max(0, after))


@router.post("/api/v2/deployments", status_code=202)
async def deployment(body: PatchRequest, request: Request):
    return await service(request).prepare_deployment(body.investigation_id)


@router.post("/api/v2/deployments/{proposal_id}/promote")
async def promote_deployment(proposal_id: str, request: Request):
    return await service(request).promote_deployment(proposal_id)


@router.get("/api/v2/artifacts/{artifact_id}")
async def artifact(artifact_id: str, request: Request):
    meta = service(request).store.get_artifact(artifact_id)
    if meta is None:
        raise OpsError("artifact_not_found", "Artifact was not found.", 404)
    return FileResponse(meta["path"], media_type=meta["media_type"], filename=meta["name"],
                        headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"})


@router.websocket("/ws/ops")
async def socket(websocket: WebSocket):
    if not is_origin_allowed(websocket.headers.get("origin")):
        await websocket.close(code=4403)
        return
    ops = service(websocket)
    queue = ops.subscribe()
    try:
        await websocket.accept()
        snapshot = ops.snapshot()
        await websocket.send_json({"type": "snapshot", "seq": snapshot["seq"], "payload": snapshot})
        async def send():
            watermark = snapshot["seq"]
            while True:
                event = await queue.get()
                if event["seq"] > watermark:
                    await websocket.send_json(event)
                    watermark = event["seq"]
        async def receive():
            while True:
                await websocket.receive_text()
        jobs = [asyncio.create_task(send()), asyncio.create_task(receive())]
        try:
            done, _ = await asyncio.wait(jobs, return_when=asyncio.FIRST_COMPLETED)
            for job in done:
                job.result()
        finally:
            for job in jobs:
                job.cancel()
            await asyncio.gather(*jobs, return_exceptions=True)
    except (WebSocketDisconnect, RuntimeError, asyncio.CancelledError):
        pass
    finally:
        ops.unsubscribe(queue)
