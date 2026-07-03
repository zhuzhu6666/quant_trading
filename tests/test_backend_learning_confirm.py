from fastapi.testclient import TestClient

from backend.app import app
from backend.core.auth import create_token


def _client() -> TestClient:
    return TestClient(app, headers={"Authorization": f"Bearer {create_token('tester')}"})


def test_parameter_template_apply_requires_governance_confirm(monkeypatch):
    calls = []
    monkeypatch.setattr("backend.api.learning.record_api_mutation", lambda **kwargs: calls.append(kwargs) or "audit1")

    r = _client().post(
        "/api/learning/parameter-templates/apply-switch",
        json={"factor_id": "f1", "template_id": "t1"},
    )

    assert r.status_code == 403
    assert calls and calls[0]["status"] == "blocked"
    assert calls[0]["required_confirm"] == "governance-change"


def test_parameter_template_apply_with_confirm_reaches_service(monkeypatch):
    calls = []
    monkeypatch.setattr("backend.api.learning.record_api_mutation", lambda **kwargs: calls.append(kwargs) or "audit1")

    class _Service:
        def activate_template(self, **kwargs):
            return {"ok": True, "blocked": False, "kwargs": kwargs}

    monkeypatch.setattr("backend.api.learning.ParameterTemplateService", lambda: _Service())

    r = _client().post(
        "/api/learning/parameter-templates/apply-switch",
        json={"factor_id": "f1", "template_id": "t1", "regime_key": "r1"},
        headers={"X-Confirm": "governance-change"},
    )

    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert calls and calls[0]["status"] == "applied"
