"""
tests/conftest.py — pytest 配置,自动把项目根加到 sys.path

框架审计 2026-06-04 修复计划的统一测试入口。
"""
import os
import sys
import hashlib
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Ensure subprocesses spawned by tests can also import `backend.*`.
os.environ.setdefault("PYTHONPATH", str(PROJECT_ROOT))
os.environ.setdefault("QUANT_JWT_SECRET", "test-jwt-secret-2026-do-not-use-in-prod")
os.environ.setdefault("QUANT_AUTH_USER", "test_user")
os.environ.setdefault("QUANT_PASSWORD_HASH", hashlib.sha256("test_pass_123".encode()).hexdigest())

# ── Auth helper for tests ──
import pytest


@pytest.fixture
def auth_headers():
    """Return Authorization headers with a valid test JWT."""
    from backend.core.auth import create_token
    token = create_token("test_user")
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def auth_client():
    """TestClient with valid JWT in default headers."""
    from backend.app import app
    from backend.core.auth import create_token
    from fastapi.testclient import TestClient
    token = create_token("test_user")
    return TestClient(app, headers={"Authorization": f"Bearer {token}"})


@pytest.fixture(scope="session", autouse=True)
def _isolate_attribution_duckdb(tmp_path_factory):
    """Keep attribution tests from writing into data/trades.duckdb."""
    from alpha import attribution_engine

    original_path = attribution_engine.DUCKDB_TRADES
    original_schema_ready = attribution_engine._TRADES_SCHEMA_READY
    attribution_engine.DUCKDB_TRADES = (
        tmp_path_factory.mktemp("attribution") / "trades.duckdb"
    )
    attribution_engine._TRADES_SCHEMA_READY = False
    try:
        yield
    finally:
        attribution_engine.DUCKDB_TRADES = original_path
        attribution_engine._TRADES_SCHEMA_READY = original_schema_ready


@pytest.fixture(scope="session", autouse=True)
def _shutdown_background_job_loops():
    yield
    try:
        from backend.jobs import shutdown_job_managers_for_tests

        shutdown_job_managers_for_tests()
    except Exception:
        pass
