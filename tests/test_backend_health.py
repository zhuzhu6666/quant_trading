"""Verify /api/health responds with expected shape."""
from fastapi.testclient import TestClient

from backend.app import app
from backend.core.auth import create_token

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
    assert body["_fact"]["contract"] == "system.health.v2"
    assert body["_fact"]["state"] in {"known", "unknown", "error"}


def test_health_db_field_present():
    r = client.get("/api/health")
    body = r.json()
    assert body["db"] in ("connected",) or body["db"].startswith("error:")


def test_db_health_requires_auth():
    r = client.get("/api/system/db-health")
    assert r.status_code == 401


def test_db_health_with_auth():
    token = create_token("tester")
    r = client.get("/api/system/db-health", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
