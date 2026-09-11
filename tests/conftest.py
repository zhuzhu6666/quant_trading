"""
tests/conftest.py — pytest 配置,自动把项目根加到 sys.path

框架审计 2026-06-04 修复计划的统一测试入口。
"""
import os
import sys
import hashlib
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Ensure subprocesses spawned by tests can also import `backend.*`.
os.environ.setdefault("PYTHONPATH", str(PROJECT_ROOT))
os.environ.setdefault("QUANT_JWT_SECRET", "test-jwt-secret-2026-do-not-use-in-prod")
os.environ.setdefault("QUANT_AUTH_USER", "test_user")
os.environ.setdefault("QUANT_PASSWORD_HASH", "$argon2id$v=19$m=65536,t=3,p=4$t0JQgZ/oFmjgr3eDUmkPeQ$ApsE7RuwK9h8kw4Qeeipekzt+XHALgKjEyW2VlaMgF8")
os.environ.setdefault("QUANT_AUTH_ALLOW_STATELESS_STEP_UP", "1")
os.environ.setdefault("QUANT_AUTH_SESSION_STORE", "memory")
os.environ.setdefault("QUANT_AUTH_INSECURE_COOKIE", "1")
# Release-time feature flags are production deployment state, not test
# defaults.  Keep the broad compatibility suite deterministic; v2 tests
# explicitly override the relevant accessor/environment for their scenario.
os.environ.setdefault("QUANT_LIVE_SAFETY_PLANE_V2_MODE", "off")
os.environ.setdefault("QUANT_GOVERNANCE_MUTATION_COORDINATOR_V2_MODE", "enforce")

# ── Test isolation for the state store ──────────────────────────────────
# The suite already isolates three persistence backends:
#   * attribution DuckDB  (session fixture below)
#   * QUANT_SAFETY_STATE_DIR
#   * monitor.evolution_story JSONL
# PostgreSQL was the fourth and it was NOT isolated.  Every test that
# reached an ensure_*/persist_* helper wrote straight into the production
# `quant_audit` database.  Measured 2026-09-11: one full-suite run appended
# 38 `governance_command` events while all services were stopped
# (19:48:33 ~ 20:00:09).  Those rows are indistinguishable from real
# governance traffic (same producer `evolution_ledger`, same `evodec_`
# entity prefix), so they cannot be cleaned up after the fact.
#
# The fix is NOT to disable the state backend -- `backend/core/db.py`
# already routes on the *path*: `is_state_db_path(db) == (db == STATE_DB)`
# selects PostgreSQL, anything else falls back to SQLite.  Pointing
# `STATE_DB` at a throwaway file therefore keeps every connection helper
# working (tests that call `get_state_pg_conn()` get the SQLite fallback
# rather than a RuntimeError) while sending all writes to a temp file.
#
# `BACKEND`'s module constant cannot be rebound usefully: ``STATE_DB`` is
# baked into ~200 default arguments at import time, so those call sites keep
# pointing at ``data/state.db``.  The escape hatch therefore lives in
# ``db.is_state_db_path()``, which returns False for every path while
# ``QUANT_TEST_STATE_DIR`` is set.  That single predicate drives
# ``_use_pg()`` / ``is_sqlite_path()`` across the codebase, so all state
# traffic falls back to the sandbox SQLite file.
os.environ["QUANT_TEST_ISOLATED_STATE"] = "1"
os.environ["QUANT_TEST_STATE_DIR"] = os.path.join(
    tempfile.gettempdir(), "quant-test-state-%d" % os.getpid()
)
os.makedirs(os.environ["QUANT_TEST_STATE_DIR"], exist_ok=True)
os.environ["QUANT_ALLOW_PG_TESTS"] = "0"

# ── REBIND the sentinel constant ────────────────────────────────────────
# The env var above handles call sites that pass the *stale* default
# argument (`def f(db_path=STATE_DB)`), because ``is_state_db_path()``
# consults it at call time.  A second class of call site compares two
# module constants directly:
#
#     db.py:132   if path.resolve() == _DEFAULT_STATE_DB: raise RuntimeError
#     db.py:1100  if _normalize_db_path(STATE_DB).resolve() != _DEFAULT_STATE_DB:
#
# Those never look at the env var.  ``get_state_conn`` documents the
# intended contract -- "offline tests monkeypatch this name to point at an
# isolated SQLite fixture" -- so rebinding the module attribute is the
# sanctioned path.  ``_DEFAULT_STATE_DB`` stays at the real production
# path: it is the "am I un-isolated?" canary that both checks test against.
from backend.core import db as _db  # noqa: E402

_ORIGINAL_STATE_DB = _db.STATE_DB
_ISOLATED_STATE_DB = (
    Path(os.environ["QUANT_TEST_STATE_DIR"]) / "state.db"
).resolve()
_ISOLATED_STATE_DB.parent.mkdir(parents=True, exist_ok=True)
_db.STATE_DB = _ISOLATED_STATE_DB
_db._KNOWN_SQLITE_PATHS.add(_ISOLATED_STATE_DB)

def pytest_configure(config):  # noqa: ARG001
    """Fail loudly if something re-points the suite at production.

    Isolation itself is set up above (QUANT_TEST_STATE_DIR).  This hook
    exists so that a shell-exported DSN cannot silently defeat it: with
    QUANT_TEST_STATE_DIR set, ``is_state_db_path()`` is False for every
    path, so a PostgreSQL connection that *does* open is a bug worth
    hearing about rather than a quiet write into production.
    """
    if os.environ.get("QUANT_ALLOW_PG_TESTS") == "1":
        return
    if os.environ.get("QUANT_TEST_STATE_DIR") and (
        os.environ.get("QUANT_STATE_PG_DSN") or os.environ.get("QUANT_AUDIT_PG_DSN")
    ):
        raise RuntimeError(
            "Test isolation is active (QUANT_TEST_STATE_DIR is set) but a "
            "PostgreSQL DSN is also present. Unset QUANT_STATE_PG_DSN / "
            "QUANT_AUDIT_PG_DSN, or set QUANT_ALLOW_PG_TESTS=1 to test "
            "against PostgreSQL deliberately."
        )


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
def _isolate_live_safety_ledgers(tmp_path_factory):
    """Never let fault-injection tests latch the real demo runtime."""

    previous = os.environ.get("QUANT_SAFETY_STATE_DIR")
    os.environ["QUANT_SAFETY_STATE_DIR"] = str(tmp_path_factory.mktemp("live_safety"))
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("QUANT_SAFETY_STATE_DIR", None)
        else:
            os.environ["QUANT_SAFETY_STATE_DIR"] = previous


@pytest.fixture(scope="session", autouse=True)
def _isolate_evolution_story(tmp_path_factory):
    """D7: keep pytest processes from appending to the production story JSONL."""
    from monitor.evolution_story import EvolutionStory

    sandbox = tmp_path_factory.mktemp("evolution_story") / "evolution_story.jsonl"
    previous_instance = EvolutionStory._instance
    EvolutionStory.reset_singleton()
    EvolutionStory.shared(str(sandbox))
    try:
        yield
    finally:
        with EvolutionStory._lock:
            EvolutionStory._instance = previous_instance
        # 子进程兜底: 显式清掉 pytest 标记也无济于事的场景由
        # core._default_story_path() 的环境检测覆盖, 这里无需恢复文件系统。
