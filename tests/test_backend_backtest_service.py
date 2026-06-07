"""Verify backtest service + API surface (mocked subprocess)."""
from unittest.mock import patch, MagicMock

import pytest
from fastapi.testclient import TestClient

from backend.app import app

client = TestClient(app)


def test_post_backtest_returns_job_id():
    with patch("backend.services.backtest_service.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="OK", stderr="")
        with patch("backend.services.backtest_service._find_latest_backtest_report", return_value=None):
            r = client.post("/api/backtest/run", json={"symbol": "XAUUSD+", "timeframe": "M15"})
            assert r.status_code == 200
            body = r.json()
            assert "job_id" in body
            assert body["status"] in ("queued", "running", "done", "error")


def test_get_backtest_job():
    with patch("backend.services.backtest_service.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="OK", stderr="")
        with patch("backend.services.backtest_service._find_latest_backtest_report", return_value=None):
            r = client.post("/api/backtest/run", json={})
            job_id = r.json()["job_id"]
            r2 = client.get(f"/api/backtest/{job_id}")
            assert r2.status_code == 200
            assert r2.json()["id"] == job_id


def test_get_nonexistent_job_404():
    r = client.get("/api/backtest/nonexistent_id_xx")
    assert r.status_code == 404
