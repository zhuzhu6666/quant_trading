from fastapi.testclient import TestClient

from backend.app import app
from backend.core.auth import create_token


def _client() -> TestClient:
    return TestClient(app, headers={"Authorization": f"Bearer {create_token('tester')}"})


def test_live_start_requires_confirm(monkeypatch):
    calls = []
    monkeypatch.setattr("backend.api.live.record_api_mutation", lambda **kwargs: calls.append(kwargs) or "audit1")
    monkeypatch.setattr("backend.api.live.loop_status", lambda: {"running": False})

    r = _client().post("/api/live/start", json={"broker": "ctrader", "strategy_name": "factor_v4"})

    assert r.status_code == 403
    assert calls and calls[0]["status"] == "blocked"
    assert calls[0]["required_confirm"] == "start-live"


def test_live_start_with_confirm_calls_start_loop(monkeypatch):
    calls = []
    monkeypatch.setattr("backend.api.live.record_api_mutation", lambda **kwargs: calls.append(kwargs) or "audit1")
    monkeypatch.setattr("backend.api.live.loop_status", lambda: {"running": False})
    monkeypatch.setattr(
        "backend.api.live.start_loop",
        lambda broker, strategy_name: {"ok": True, "broker": broker, "strategy_name": strategy_name},
    )

    r = _client().post(
        "/api/live/start",
        json={"broker": "ctrader", "strategy_name": "factor_v4"},
        headers={"X-Confirm": "start-live"},
    )

    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert calls and calls[0]["status"] == "applied"
