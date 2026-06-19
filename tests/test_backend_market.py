"""Verify market data API shape + validation."""
from fastapi.testclient import TestClient

from backend.app import app
from backend.core.auth import create_token

_token = create_token("test_user")
client = TestClient(app, headers={"Authorization": f"Bearer {_token}"})


def test_get_bars_default():
    r = client.get("/api/market/bars")
    assert r.status_code == 200
    body = r.json()
    assert "bars" in body
    assert "total" in body
    assert "range" in body


def test_invalid_timeframe_422():
    r = client.get("/api/market/bars?timeframe=INVALID")
    assert r.status_code == 422
