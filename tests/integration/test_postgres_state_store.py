from __future__ import annotations

import os
import uuid

import psycopg
import pytest
from psycopg import sql
from psycopg.rows import dict_row


pytestmark = pytest.mark.postgres_integration


def test_postgres_state_store_connects_and_uses_state_schema() -> None:
    dsn = os.environ.get("QUANT_STATE_PG_DSN", "").strip()
    if not dsn:
        pytest.skip("QUANT_STATE_PG_DSN is not configured")

    from backend.core.state_store import (
        STATE_SCHEMA,
        RuntimeStateSchemaMissingError,
        connect_state_store,
    )
    from backend.core.state_schema_migrations import (
        STATE_SCHEMA_MIN_VERSION,
        require_state_schema_version,
    )

    conn = connect_state_store(dsn)
    try:
        schema = conn.execute("SELECT current_schema() AS schema").fetchone()["schema"]
        assert schema == STATE_SCHEMA
        status = require_state_schema_version(conn)
        assert status["current_version"] >= STATE_SCHEMA_MIN_VERSION
        with pytest.raises(RuntimeStateSchemaMissingError):
            conn.execute(
                "CREATE TEMP TABLE phase0b_pg_smoke (id INTEGER PRIMARY KEY, value TEXT NOT NULL)"
            )
        conn.rollback()
    finally:
        conn.close()


def test_postgres_read_only_state_connection_stays_read_only_after_commit() -> None:
    dsn = os.environ.get("QUANT_STATE_PG_DSN", "").strip()
    if not dsn:
        pytest.skip("QUANT_STATE_PG_DSN is not configured")

    from backend.core.state_store import connect_state_store

    schema = f"pytest_state_readonly_{uuid.uuid4().hex}"
    admin = psycopg.connect(dsn, autocommit=False, row_factory=dict_row)
    admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    admin.execute(
        sql.SQL("CREATE TABLE {}.probe (value INTEGER)").format(sql.Identifier(schema))
    )
    admin.commit()
    conn = None
    try:
        conn = connect_state_store(dsn, read_only=True, schema=schema)
        assert conn.execute("SELECT COUNT(*) AS count FROM probe").fetchone()["count"] == 0
        conn.commit()
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            conn.execute("INSERT INTO probe(value) VALUES (1)")
        conn.rollback()
        # Rollback also starts a fresh default transaction on the next query;
        # the session default must keep that transaction read-only as well.
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            conn.execute("INSERT INTO probe(value) VALUES (2)")
        conn.rollback()
    finally:
        if conn is not None:
            conn.close()
        admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
        admin.commit()
        admin.close()


def test_versioned_state_migration_executes_in_disposable_pg_temp_schema() -> None:
    from backend.core.db import state_pg_dsn

    dsn = state_pg_dsn().strip()
    if not dsn:
        pytest.skip("QUANT_STATE_PG_DSN is not configured")

    from backend.core.state_schema_migrations import (
        STATE_SCHEMA_MIGRATIONS,
        require_state_schema_version,
        run_state_schema_migrations,
    )
    from backend.core.state_store import connect_state_migration_store

    conn = connect_state_migration_store(dsn)

    class _DeferredCommitConnection:
        """Keep runner DDL inside the test transaction for final rollback."""

        def __init__(self, inner):
            self.inner = inner
            self.commit_calls = 0

        def execute(self, statement, params=None):
            isolated = (
                str(statement)
                .replace("runtime.", "pg_temp.")
                .replace("canonical_v2.", "pg_temp.")
            )
            return self.inner.execute(isolated, params)

        def commit(self):
            self.commit_calls += 1

        def rollback(self):
            return self.inner.rollback()

    wrapped = _DeferredCommitConnection(conn)
    try:
        conn.execute("SET search_path TO pg_temp")
        # Materialize the session-local schema, then leave it truly empty so
        # the public runner must exercise its clean-install bootstrap.
        conn.execute("CREATE TEMP TABLE migration_connection_ddl_smoke (value INTEGER)")
        conn.execute("DROP TABLE migration_connection_ddl_smoke")

        first = run_state_schema_migrations(
            wrapped,
            runner_id="pytest_pg_temp",
        )
        second = run_state_schema_migrations(
            wrapped,
            runner_id="pytest_pg_temp",
        )
        status = require_state_schema_version(wrapped)

        assert first["bootstrap"]["applied"] is True
        assert first["bootstrap"]["statement_count"] > 0
        assert first["applied_count"] == len(STATE_SCHEMA_MIGRATIONS)
        assert second["bootstrap"]["applied"] is False
        assert second["applied_count"] == 0
        assert status["ok"] is True
        assert wrapped.commit_calls == 2
        for object_name in (
            "broker_execution_intent",
            "idx_jobs_claim_ready",
            "runtime_kv",
            "replay_report",
            "release_run",
        ):
            assert conn.execute(
                "SELECT to_regclass(%s) AS name",
                (object_name,),
            ).fetchone()["name"], object_name

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

        for retired_table in (
            "autonomous_learning_sample",
            "decision_ledger",
            "trade_outcome_review",
            "position_supervisor_trace",
        ):
            assert conn.execute(
                "SELECT to_regclass(%s) AS name",
                (retired_table,),
            ).fetchone()["name"] is None, retired_table

        jobs_primary_key = conn.execute(
            """
            SELECT count(*) AS count
            FROM pg_constraint
            WHERE conrelid=to_regclass('jobs')
              AND contype='p'
            """
        ).fetchone()["count"]
        assert jobs_primary_key == 1
    finally:
        conn.rollback()
        conn.close()


def test_auth_logout_revokes_entire_rotated_family_in_postgres(monkeypatch) -> None:
    dsn = os.environ.get("QUANT_STATE_PG_DSN", "").strip()
    if not dsn:
        pytest.skip("QUANT_STATE_PG_DSN is not configured")

    from backend.core import db as db_module
    from backend.services.auth_sessions import (
        create_refresh_session,
        revoke_refresh_session,
        rotate_refresh_session,
        session_family_ids,
        session_is_active,
        step_up_refresh_session,
    )

    schema = f"pytest_auth_session_{uuid.uuid4().hex}"
    admin = psycopg.connect(dsn, autocommit=False, row_factory=dict_row)
    admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    admin.execute(sql.SQL("SET search_path TO {}, public").format(sql.Identifier(schema)))
    admin.execute(
        """
        CREATE TABLE auth_session (
            session_id TEXT PRIMARY KEY,
            subject TEXT NOT NULL,
            token_jti TEXT NOT NULL DEFAULT '',
            token_hash TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'active',
            client_fingerprint TEXT NOT NULL DEFAULT '',
            ip_hash TEXT NOT NULL DEFAULT '',
            user_agent TEXT NOT NULL DEFAULT '',
            issued_at DOUBLE PRECISION NOT NULL DEFAULT 0.0,
            expires_at DOUBLE PRECISION NOT NULL DEFAULT 0.0,
            last_seen_at DOUBLE PRECISION NOT NULL DEFAULT 0.0,
            revoked_at DOUBLE PRECISION NOT NULL DEFAULT 0.0,
            revoked_by TEXT NOT NULL DEFAULT '',
            revoke_reason TEXT NOT NULL DEFAULT '',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at DOUBLE PRECISION NOT NULL DEFAULT 0.0,
            updated_at DOUBLE PRECISION NOT NULL DEFAULT 0.0
        )
        """
    )
    admin.commit()

    def _connect(*, read_only: bool = False):
        del read_only
        conn = psycopg.connect(dsn, autocommit=False, row_factory=dict_row)
        conn.execute(
            sql.SQL("SET search_path TO {}, public").format(sql.Identifier(schema))
        )
        return conn

    monkeypatch.delenv("QUANT_AUTH_SESSION_STORE", raising=False)
    monkeypatch.setattr(db_module, "get_state_pg_conn", _connect)
    try:
        first = create_refresh_session("operator", now=1_000.0, auth_time=1_000)
        stepped_up = step_up_refresh_session(
            first.session_id,
            subject="operator",
            family_id=first.family_id,
            now=1_050.0,
        )
        assert stepped_up.auth_time == 1_050
        second = rotate_refresh_session(first.refresh_token, now=1_100.0)
        assert second.auth_time == 1_050
        family_id, members = session_family_ids(second.session_id)
        assert family_id == first.family_id == second.family_id
        assert set(members) == {first.session_id, second.session_id}
        assert session_is_active(second.session_id, subject="operator", now=1_200.0)

        assert revoke_refresh_session(
            session_id=second.session_id,
            token=second.refresh_token,
            now=1_300.0,
        )
        assert session_is_active(first.session_id, subject="operator", now=1_301.0) is False
        assert session_is_active(second.session_id, subject="operator", now=1_301.0) is False
        rows = admin.execute(
            "SELECT session_id, status FROM auth_session ORDER BY session_id"
        ).fetchall()
        assert {str(row["status"]) for row in rows} == {"revoked"}
    finally:
        admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
        admin.commit()
        admin.close()
