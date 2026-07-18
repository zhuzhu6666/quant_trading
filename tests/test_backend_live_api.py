from fastapi.testclient import TestClient

from backend.app import app
from backend.core.auth import create_token
from backend.services.live_safety_state import safety_outbox_path


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


def test_emergency_result_survives_mutation_audit_failure(monkeypatch, tmp_path):
    from backend.api import live as live_api

    monkeypatch.setenv("QUANT_SAFETY_STATE_DIR", str(tmp_path / "safety"))
    result = {
        "schema_version": "live_emergency_close.v2",
        "status": "completed",
        "ok": True,
        "emergency_id": "emergency_test",
        "attempted": 1,
        "closed": 1,
        "failed": 0,
        "remaining_position_ids": [],
        "resume_required": True,
    }
    monkeypatch.setattr("backend.api.live.emergency_close", lambda *_args, **_kwargs: result)
    monkeypatch.setattr(
        "backend.api.live.record_api_mutation",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("postgres unavailable")),
    )

    response = live_api.emergency(
        _user={"sub": "tester"},
        req=live_api.EmergencyCloseRequest(broker="ctrader"),
        x_confirm="emergency",
    )

    assert response["status"] == "completed"
    assert "emergency_api_mutation_audit_failed" in safety_outbox_path().read_text(encoding="utf-8")
