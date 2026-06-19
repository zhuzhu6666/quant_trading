"""Verify /ws/state broadcasts a snapshot on connect + every 1s."""
import json

from fastapi.testclient import TestClient

from backend.app import app
from backend.core.auth import create_token

client = TestClient(app)


def _ws_token() -> str:
    """Generate a valid JWT for WS testing."""
    return create_token("zhu")


def test_ws_state_sends_snapshot_on_connect():
    token = _ws_token()
    with client.websocket_connect(f"/ws/state?token={token}") as ws:
        msg = ws.receive_text()
        snapshot = json.loads(msg)
        assert "equity" in snapshot
        assert "position" in snapshot
        assert "daily" in snapshot
        assert "risk" in snapshot
        assert "server_time" in snapshot


def test_ws_state_sends_followup_after_1s():
    token = _ws_token()
    with client.websocket_connect(f"/ws/state?token={token}") as ws:
        first = json.loads(ws.receive_text())
        second = json.loads(ws.receive_text())  # wait 1s for next tick
        assert "equity" in second
        # server_time should differ
        assert first["server_time"] != second["server_time"]
