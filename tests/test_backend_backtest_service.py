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
    # The API enqueues through the canonical PG job manager; unit-test the
    # API contract against a fake manager instead of the shared production
    # queue (2026-09-02: previously hit the real PG from the test process).
    manager = MagicMock()
    manager.list.return_value = []
    manager.submit.return_value = SimpleNamespace(id="job-parity-1", status="queued")
    with patch("backend.api.backtest.get_job_manager", return_value=manager):
        response = client.post(
            "/api/backtest/run",
            json={"symbol": "XAUUSD+", "timeframe": "M5", "max_bars": 5000},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["engine"] == "live_parity_replay_v1"
    assert body["job_id"] == "job-parity-1"
    assert body["status"] == "queued"
    manager.submit.assert_called_once()
    submitted_kind, submitted_params = manager.submit.call_args.args
    assert submitted_kind == "backtest"
    assert submitted_params["symbol"] == "XAUUSD+"
    assert submitted_params["timeframe"] == "M5"
    assert submitted_params["max_bars"] == 5000


def test_backtest_rejects_more_than_twenty_thousand_bars():
    response = client.post("/api/backtest/run", json={"max_bars": 20001})
    assert response.status_code == 422


def test_get_backtest_job():
    job = SimpleNamespace(
        id="job-parity-1",
        status="queued",
        to_dict=lambda: {"id": "job-parity-1", "status": "queued", "kind": "backtest"},
    )
    manager = MagicMock()
    manager.get.return_value = job
    with patch("backend.api.backtest.get_job_manager", return_value=manager):
        response = client.get("/api/backtest/job-parity-1")
    assert response.status_code == 200
    assert response.json()["id"] == "job-parity-1"


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
        "artifact_manifest": {"selected_factor_ids": ["factor"]},
        "components": {"factor_engine": {"verified": True}},
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
    assert "artifact_manifest" not in summary
    assert "components" not in summary
    assert summary["learning_bundle"]["open_sample_count"] == 1
    assert "open_samples" not in summary["learning_bundle"]
    assert "factor_samples" not in summary["learning_bundle"]
