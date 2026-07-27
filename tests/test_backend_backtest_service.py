"""Canonical Parity backtest API contract."""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from backend.app import app
from backend.core.auth import create_token
from backend.services.backtest_service import _job_result_summary

client = TestClient(
    app,
    headers={"Authorization": f"Bearer {create_token('test_user')}"},
)

REPORT = {
    "schema_version": "parity_replay_report.v1",
    "engine": "live_parity_replay_v1",
    "status": "diagnostic_only",
    "metrics": {"bar_count": 10, "independent_trade_count": 1},
    "learning_bundle": {"trainable": False, "blockers": ["fixture"]},
}


def test_post_backtest_returns_parity_job_id():
    with patch("backend.services.parity_replay.ParityReplayService.run", return_value=REPORT):
        response = client.post(
            "/api/backtest/run",
            json={"symbol": "XAUUSD+", "timeframe": "M5", "max_bars": 5000},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["engine"] == "live_parity_replay_v1"
    assert body["job_id"]
    assert body["status"] in {"queued", "pending", "running", "done", "error"}


def test_backtest_rejects_more_than_twenty_thousand_bars():
    response = client.post("/api/backtest/run", json={"max_bars": 20001})
    assert response.status_code == 422


def test_get_backtest_job():
    with patch("backend.services.parity_replay.ParityReplayService.run", return_value=REPORT):
        job_id = client.post("/api/backtest/run", json={}).json()["job_id"]
    response = client.get(f"/api/backtest/{job_id}")
    assert response.status_code == 200
    assert response.json()["id"] == job_id


def test_get_nonexistent_job_404():
    assert client.get("/api/backtest/nonexistent_id_xx").status_code == 404


def test_cancel_backtest_uses_existing_job_manager_contract():
    manager = MagicMock()
    manager.get.return_value = SimpleNamespace(id="job-cancel")
    manager.cancel.return_value = True
    with patch("backend.api.backtest.get_job_manager", return_value=manager):
        response = client.post("/api/backtest/job-cancel/cancel")
    assert response.status_code == 200
    assert response.json() == {"job_id": "job-cancel", "cancelled": True}
    manager.cancel.assert_called_once_with("job-cancel")


def test_completed_job_report_is_returned_without_legacy_parser():
    manager = MagicMock()
    manager.get.return_value = SimpleNamespace(
        id="job-report",
        status="done",
        result=REPORT,
    )
    with patch("backend.api.backtest.get_job_manager", return_value=manager):
        response = client.get("/api/backtest/job-report/report")
    assert response.status_code == 200
    assert response.json()["report"]["engine"] == "live_parity_replay_v1"


def test_job_result_keeps_counts_but_not_full_trade_or_sample_payloads():
    summary = _job_result_summary({
        **REPORT,
        "trades": [{"trade_id": "one"}],
        "events": [{"event": "opened"}],
        "learning_bundle": {
            "trainable": True,
            "open_sample_count": 1,
            "factor_sample_count": 1,
            "open_samples": [{"sample_id": "open"}],
            "factor_samples": [{"sample_id": "factor"}],
        },
    })

    assert "trades" not in summary
    assert "events" not in summary
    assert summary["learning_bundle"]["open_sample_count"] == 1
    assert "open_samples" not in summary["learning_bundle"]
    assert "factor_samples" not in summary["learning_bundle"]
