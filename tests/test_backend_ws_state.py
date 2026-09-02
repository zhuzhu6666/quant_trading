"""Verify /ws/state sends an initial snapshot and event-driven updates."""
import json

from fastapi.testclient import TestClient

from backend.app import app
from backend.core.auth import create_token
from backend.ws.manager import get_connection_manager

client = TestClient(app)


def _ws_token() -> str:
    """Generate a valid JWT for WS testing."""
    return create_token("zhu")


def test_ws_state_sends_snapshot_on_connect():
    token = _ws_token()
    # URL JWTs require QUANT_AUTH_ALLOW_URL_JWT; the endpoint accepts a
    # bearer token in the WS subprotocol (2026-09-02 contract).
    with client.websocket_connect("/ws/state", subprotocols=[token]) as ws:
        msg = ws.receive_text()
        snapshot = json.loads(msg)
        assert "equity" in snapshot
        assert "position" in snapshot
        assert "daily" in snapshot
        assert "risk" in snapshot
        assert "server_time" in snapshot
        assert snapshot["_fact"]["contract"] == "live.state.v2"
        assert snapshot["_fact"]["state"] in {"known", "unknown", "stale", "error"}


def test_ws_state_sends_followup_after_state_change():
    token = _ws_token()
    # URL JWTs require QUANT_AUTH_ALLOW_URL_JWT; the endpoint accepts a
    # bearer token in the WS subprotocol (2026-09-02 contract).
    with client.websocket_connect("/ws/state", subprotocols=[token]) as ws:
        first = json.loads(ws.receive_text())
        get_connection_manager().notify("state")
        second = json.loads(ws.receive_text())
        assert "equity" in second
        # The second snapshot is caused by the explicit state event, not by a
        # per-connection timer.
        assert first["server_time"] != second["server_time"]


def test_http_state_fallback_carries_the_same_fact_contract():
    token = _ws_token()
    response = client.get(
        "/api/state",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    snapshot = response.json()
    assert snapshot["_fact"]["contract"] == "live.state.v2"
    assert snapshot["_fact"]["state"] in {"known", "unknown", "stale", "error"}
