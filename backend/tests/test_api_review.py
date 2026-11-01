"""Regressions from Codex API-001 review; no provider calls."""
import asyncio

from fastapi.testclient import TestClient
from starlette.websockets import WebSocket

from test_api import FakeController
from aura_api import create_app
from aura_api.manager import IncidentManager


def test_fresh_fault_telemetry_is_not_healthy():
    async def run():
        controller = FakeController()
        manager = IncidentManager(controller=controller)
        assert manager.snapshot().worker_health == 'healthy'
        await controller.inject_cache_retention()
        assert manager.snapshot().telemetry.rss_fresh
        assert manager.snapshot().worker_health == 'degraded'
    asyncio.run(run())


def test_events_queued_during_accept_do_not_follow_newer_snapshot(monkeypatch):
    app = create_app(controller_factory=FakeController)
    manager = app.state.manager
    original_accept = WebSocket.accept

    async def accept_with_pending_event(self, *args, **kwargs):
        await original_accept(self, *args, **kwargs)
        await manager._emit('telemetry', {'marker': 'before_snapshot'}, None)

    monkeypatch.setattr(WebSocket, 'accept', accept_with_pending_event)
    with TestClient(app) as client:
        with client.websocket_connect('/ws/telemetry') as ws:
            snapshot = ws.receive_json()
            following = ws.receive_json()
            assert snapshot['type'] == 'snapshot'
            assert following['seq'] > snapshot['seq']
            assert following['payload'].get('marker') != 'before_snapshot'
