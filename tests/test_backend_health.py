"""Verify /api/health responds with expected shape."""
from fastapi.testclient import TestClient

from backend.app import app

client = TestClient(app)


def test_health_ok_or_degraded():
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] in ("ok", "degraded")
    assert "db" in body
    assert "server_time" in body
    assert "uptime_seconds" in body
    assert body["uptime_seconds"] >= 0


def test_health_db_field_present():
    r = client.get("/api/health")
    body = r.json()
    assert body["db"] in ("connected",) or body["db"].startswith("error:")
