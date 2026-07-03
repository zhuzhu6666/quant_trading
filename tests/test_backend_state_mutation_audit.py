from fastapi.testclient import TestClient

from backend.app import app
from backend.core.auth import create_token


def _client() -> TestClient:
    return TestClient(app, headers={"Authorization": f"Bearer {create_token('tester')}"})


def test_job_cancel_records_blocked_audit(monkeypatch):
    calls = []

    class _Mgr:
        def cancel(self, job_id):
            return False

    monkeypatch.setattr("backend.api.jobs.get_job_manager", lambda: _Mgr())
    monkeypatch.setattr("backend.api.jobs.record_api_mutation", lambda **kwargs: calls.append(kwargs) or "audit1")

    r = _client().post("/api/jobs/job123/cancel")

    assert r.status_code == 400
    assert calls and calls[0]["action"] == "cancel_job"
    assert calls[0]["status"] == "blocked"


def test_job_cancel_records_applied_audit(monkeypatch):
    calls = []

    class _Mgr:
        def cancel(self, job_id):
            return True

    monkeypatch.setattr("backend.api.jobs.get_job_manager", lambda: _Mgr())
    monkeypatch.setattr("backend.api.jobs.record_api_mutation", lambda **kwargs: calls.append(kwargs) or "audit1")

    r = _client().post("/api/jobs/job123/cancel")

    assert r.status_code == 200
    assert calls and calls[0]["action"] == "cancel_job"
    assert calls[0]["status"] == "applied"
