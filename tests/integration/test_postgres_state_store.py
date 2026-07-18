from __future__ import annotations

import os

import pytest
import psycopg
from psycopg.rows import dict_row


pytestmark = pytest.mark.postgres_integration


def test_postgres_state_store_connects_and_uses_state_schema() -> None:
    dsn = os.environ.get("QUANT_STATE_PG_DSN", "").strip()
    if not dsn:
        pytest.skip("QUANT_STATE_PG_DSN is not configured")

    from backend.core.state_store import connect_state_store

    conn = connect_state_store(dsn)
    try:
        schema = conn.execute("SELECT current_schema() AS schema").fetchone()["schema"]
        assert schema == "state_v1"
        conn.execute(
            "CREATE TEMP TABLE phase0b_pg_smoke (id INTEGER PRIMARY KEY, value TEXT NOT NULL)"
        )
        conn.execute("INSERT INTO phase0b_pg_smoke (id, value) VALUES (1, 'ok')")
        row = conn.execute("SELECT value FROM phase0b_pg_smoke WHERE id=1").fetchone()
        assert row["value"] == "ok"
        conn.rollback()
    finally:
        conn.close()


def test_versioned_state_migration_executes_in_disposable_pg_temp_schema() -> None:
    from backend.core.db import state_pg_dsn

    dsn = state_pg_dsn().strip()
    if not dsn:
        pytest.skip("QUANT_STATE_PG_DSN is not configured")

    from backend.core.state_schema_migrations import (
        STATE_SCHEMA_BASELINE_TABLES,
        require_state_schema_version,
        run_state_schema_migrations,
    )

    conn = psycopg.connect(dsn, autocommit=False, row_factory=dict_row)

    class _DeferredCommitConnection:
        """Keep runner DDL inside the test transaction for final rollback."""

        def __init__(self, inner):
            self.inner = inner
            self.commit_calls = 0

        def execute(self, sql, params=None):
            return self.inner.execute(sql, params)

        def commit(self):
            self.commit_calls += 1

        def rollback(self):
            return self.inner.rollback()

    wrapped = _DeferredCommitConnection(conn)
    try:
        conn.execute("SET search_path TO pg_temp")
        for table in STATE_SCHEMA_BASELINE_TABLES:
            if table == "order_lifecycle_event":
                conn.execute(
                    "CREATE TEMP TABLE order_lifecycle_event "
                    "(event_id TEXT PRIMARY KEY, event_ts DOUBLE PRECISION NOT NULL DEFAULT 0.0)"
                )
            else:
                conn.execute(f'CREATE TEMP TABLE "{table}" (seed INTEGER)')

        first = run_state_schema_migrations(wrapped, runner_id="pytest_pg_temp")
        second = run_state_schema_migrations(wrapped, runner_id="pytest_pg_temp")
        status = require_state_schema_version(wrapped)

        assert first["applied_count"] == 1
        assert second["applied_count"] == 0
        assert status["ok"] is True
        assert wrapped.commit_calls == 2
        assert conn.execute("SELECT to_regclass('broker_execution_intent') AS name").fetchone()["name"]
        projection_columns = {
            row["column_name"]
            for row in conn.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema=current_schema()
                  AND table_name='factor_runtime_projection'
                """
            ).fetchall()
        }
        assert {"mutation_id", "config_version", "config_hash"} <= projection_columns
    finally:
        conn.rollback()
        conn.close()
