"""Verify /ws/state broadcasts a snapshot on connect + every 1s."""
import json

from fastapi.testclient import TestClient

from backend.app import app

client = TestClient(app)


def test_ws_state_sends_snapshot_on_connect():
    with client.websocket_connect("/ws/state") as ws:
        msg = ws.receive_text()
        snapshot = json.loads(msg)
        assert "equity" in snapshot
        assert "position" in snapshot
        assert "daily" in snapshot
        assert "risk" in snapshot
        assert "server_time" in snapshot


def test_ws_state_sends_followup_after_1s():
    with client.websocket_connect("/ws/state") as ws:
        first = json.loads(ws.receive_text())
        second = json.loads(ws.receive_text())  # wait 1s for next tick
        assert "equity" in second
        # server_time should differ
        assert first["server_time"] != second["server_time"]
