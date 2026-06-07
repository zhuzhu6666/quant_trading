"""Verify backtest service + API surface (in-process runner mocked).

Phase 4.7: backtest_service no longer shells out to main.py subprocess; the
service calls the in-process run_backtest_sweep from backtest_runner instead.
These tests mock the in-process runner so the API surface can be exercised
without running an actual backtest.
"""
from unittest.mock import patch, MagicMock

import pytest
from fastapi.testclient import TestClient

from backend.app import app

client = TestClient(app)


def test_post_backtest_returns_job_id():
    with patch("backend.services.backtest_runner.run_backtest_sweep") as mock_sweep:
        mock_sweep.return_value = {
            "rows": [],
            "total_runs": 12,
            "elapsed_seconds": 0.01,
            "report_path": None,
            "note": "mocked",
        }
        r = client.post("/api/backtest/run", json={"symbol": "XAUUSD+", "timeframe": "M15"})
        assert r.status_code == 200
        body = r.json()
        assert "job_id" in body
        assert body["status"] in ("queued", "running", "done", "error")


def test_get_backtest_job():
    with patch("backend.services.backtest_runner.run_backtest_sweep") as mock_sweep:
        mock_sweep.return_value = {
            "rows": [],
            "total_runs": 12,
            "elapsed_seconds": 0.01,
            "report_path": None,
            "note": "mocked",
        }
        r = client.post("/api/backtest/run", json={})
        job_id = r.json()["job_id"]
        r2 = client.get(f"/api/backtest/{job_id}")
        assert r2.status_code == 200
        assert r2.json()["id"] == job_id


def test_get_nonexistent_job_404():
    r = client.get("/api/backtest/nonexistent_id_xx")
    assert r.status_code == 404
