"""Verify paper service + REST API surface."""
from unittest.mock import patch, MagicMock
import subprocess

import pytest
from fastapi.testclient import TestClient

from backend.app import app
from backend.core.auth import create_token
from backend.services.paper_service import get_paper_service

_token = create_token("test_user")
client = TestClient(app, headers={"Authorization": f"Bearer {_token}"})


@pytest.fixture(autouse=True)
def reset_paper_service():
    """Reset the singleton before each test."""
    svc = get_paper_service()
    svc._proc = None
    yield


@patch("backend.services.paper_service.subprocess.Popen")
def test_post_paper_start_returns_pid(mock_popen):
    mock_popen.return_value = MagicMock(pid=1234, poll=MagicMock(return_value=None))
    r = client.post("/api/paper/start", json={"symbol": "XAUUSD+", "timeframe": "M15"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "running"
    assert body["pid"] == 1234


@patch("backend.services.paper_service.subprocess.Popen")
def test_double_start_returns_400(mock_popen):
    mock_popen.return_value = MagicMock(pid=1234, poll=MagicMock(return_value=None))
    r1 = client.post("/api/paper/start", json={})
    assert r1.status_code == 200
    r2 = client.post("/api/paper/start", json={})
    assert r2.status_code == 400
    assert r2.json()["detail"]["error"] == "already_running"


@patch("backend.services.paper_service.subprocess.Popen")
def test_stop_returns_stopped(mock_popen):
    mock_proc = MagicMock(pid=1234)
    mock_proc.poll.return_value = None
    mock_popen.return_value = mock_proc
    client.post("/api/paper/start", json={})
    r = client.post("/api/paper/stop", json={"close_positions": False})
    assert r.status_code == 200
    assert r.json()["status"] == "stopped"


@patch("backend.services.paper_service.subprocess.Popen")
def test_emergency_stop_requires_x_confirm(mock_popen):
    mock_proc = MagicMock(pid=1234)
    mock_proc.poll.return_value = None
    mock_popen.return_value = mock_proc
    client.post("/api/paper/start", json={})
    # No header
    r = client.post("/api/paper/emergency-stop", json={})
    assert r.status_code == 403
    # With header
    r2 = client.post("/api/paper/emergency-stop", json={}, headers={"X-Confirm": "emergency"})
    assert r2.status_code == 200
    assert r2.json()["emergency"] is True
