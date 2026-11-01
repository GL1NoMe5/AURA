"""Exercise a real Uvicorn WebSocket upgrade, beyond in-process TestClient."""
import asyncio
import json
from pathlib import Path
import socket
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import uvicorn
from websockets.asyncio.client import connect
from aura_api import create_app
from test_api import FakeController


def test_real_uvicorn_accepts_websocket_and_stops():
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(
        create_app(controller_factory=FakeController), log_level='error'))
    thread = threading.Thread(target=server.run, kwargs={'sockets': [listener]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not server.started and time.monotonic() < deadline:
            time.sleep(.01)
        assert server.started
        async def read_snapshot():
            async with connect(f'ws://127.0.0.1:{port}/ws/telemetry', origin=f'http://127.0.0.1:{port}') as ws:
                event = json.loads(await asyncio.wait_for(ws.recv(), 3))
                assert event['type'] == 'snapshot'
                assert event['payload']['phase'] == 'idle'
                assert event['payload']['seq'] < event['seq']
        asyncio.run(read_snapshot())
    finally:
        server.should_exit = True
        thread.join(5)
        listener.close()
    assert not thread.is_alive()
