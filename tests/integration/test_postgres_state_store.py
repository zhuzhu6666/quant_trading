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

    from backend.core.state_store import RuntimeStateSchemaMissingError, connect_state_store
    from backend.core.state_schema_migrations import (
        STATE_SCHEMA_MIN_VERSION,
        require_state_schema_version,
    )

    conn = connect_state_store(dsn)
    try:
        schema = conn.execute("SELECT current_schema() AS schema").fetchone()["schema"]
        assert schema == "state_v1"
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
        STATE_SCHEMA_BASELINE_TABLES,
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

        def execute(self, sql, params=None):
            return self.inner.execute(sql, params)

        def commit(self):
            self.commit_calls += 1

        def rollback(self):
            return self.inner.rollback()

    wrapped = _DeferredCommitConnection(conn)
    try:
        conn.execute("SET search_path TO pg_temp")
        conn.execute("CREATE TEMP TABLE migration_connection_ddl_smoke (value INTEGER)")
        for table in STATE_SCHEMA_BASELINE_TABLES:
            if table == "autonomous_learning_sample":
                conn.execute(
                    """CREATE TEMP TABLE autonomous_learning_sample (
                        sample_id TEXT PRIMARY KEY,
                        sample_type TEXT NOT NULL DEFAULT '',
                        event_ts DOUBLE PRECISION NOT NULL DEFAULT 0.0,
                        updated_at DOUBLE PRECISION NOT NULL DEFAULT 0.0
                    )"""
                )
            elif table == "policy_suggestion":
                conn.execute(
                    """CREATE TEMP TABLE policy_suggestion (
                        suggestion_id TEXT PRIMARY KEY,
                        status TEXT NOT NULL DEFAULT 'proposed',
                        created_at DOUBLE PRECISION NOT NULL DEFAULT 0.0
                    )"""
                )
            elif table == "order_lifecycle_event":
                conn.execute(
                    "CREATE TEMP TABLE order_lifecycle_event "
                    "(event_id TEXT PRIMARY KEY, event_ts DOUBLE PRECISION NOT NULL DEFAULT 0.0)"
                )
            elif table == "learning_experiment_reservation":
                conn.execute(
                    """CREATE TEMP TABLE learning_experiment_reservation (
                        reservation_id TEXT PRIMARY KEY,
                        scope_type TEXT NOT NULL DEFAULT '',
                        scope_key TEXT NOT NULL DEFAULT '',
                        action TEXT NOT NULL DEFAULT '',
                        status TEXT NOT NULL DEFAULT 'reserved',
                        application_id TEXT NOT NULL DEFAULT '',
                        expires_at DOUBLE PRECISION NOT NULL DEFAULT 0.0,
                        created_at DOUBLE PRECISION NOT NULL DEFAULT 0.0,
                        updated_at DOUBLE PRECISION NOT NULL DEFAULT 0.0
                    )"""
                )
            elif table == "nursery_exploration_reservation":
                conn.execute(
                    """CREATE TEMP TABLE nursery_exploration_reservation (
                        reservation_id TEXT NOT NULL,
                        trade_date TEXT NOT NULL,
                        reason TEXT NOT NULL,
                        setup_fingerprint TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'reserved',
                        expires_at DOUBLE PRECISION NOT NULL DEFAULT 0.0,
                        created_at DOUBLE PRECISION NOT NULL DEFAULT 0.0,
                        updated_at DOUBLE PRECISION NOT NULL DEFAULT 0.0,
                        PRIMARY KEY (reservation_id, reason)
                    )"""
                )
            elif table == "v16_brain_command":
                conn.execute(
                    """CREATE TEMP TABLE v16_brain_command (
                        command_id TEXT PRIMARY KEY,
                        target_agent TEXT NOT NULL DEFAULT '',
                        scope_type TEXT NOT NULL DEFAULT '',
                        decision TEXT NOT NULL DEFAULT '',
                        created_at DOUBLE PRECISION NOT NULL DEFAULT 0.0,
                        updated_at DOUBLE PRECISION NOT NULL DEFAULT 0.0
                    )"""
                )
            elif table == "brain_governance_candidate_review":
                conn.execute(
                    """CREATE TEMP TABLE brain_governance_candidate_review (
                        review_id TEXT PRIMARY KEY,
                        candidate_id TEXT NOT NULL DEFAULT '',
                        created_at DOUBLE PRECISION NOT NULL DEFAULT 0.0
                    )"""
                )
            elif table == "proposal_registry":
                conn.execute(
                    """CREATE TEMP TABLE proposal_registry (
                        proposal_id TEXT PRIMARY KEY,
                        source_agent TEXT NOT NULL DEFAULT '',
                        proposal_type TEXT NOT NULL DEFAULT '',
                        control_surface TEXT NOT NULL DEFAULT '',
                        target_scope TEXT NOT NULL DEFAULT ''
                    )"""
                )
            elif table == "jobs":
                conn.execute(
                    """CREATE TEMP TABLE jobs (
                        id TEXT PRIMARY KEY,
                        kind TEXT DEFAULT '',
                        status TEXT DEFAULT 'pending',
                        params_json TEXT DEFAULT '{}',
                        result_json TEXT DEFAULT '{}',
                        progress DOUBLE PRECISION DEFAULT 0.0,
                        error TEXT DEFAULT '',
                        created_at DOUBLE PRECISION,
                        updated_at DOUBLE PRECISION
                    )"""
                )
            elif table == "experience_memory":
                conn.execute(
                    """CREATE TEMP TABLE experience_memory (
                        experience_id TEXT PRIMARY KEY,
                        source_table TEXT NOT NULL DEFAULT '',
                        source_id TEXT NOT NULL DEFAULT '',
                        created_at DOUBLE PRECISION NOT NULL DEFAULT 0.0
                    )"""
                )
            elif table == "decision_factor_snapshot":
                conn.execute(
                    """CREATE TEMP TABLE decision_factor_snapshot (
                        id BIGSERIAL PRIMARY KEY,
                        decision_id TEXT NOT NULL DEFAULT '',
                        factor TEXT NOT NULL DEFAULT ''
                    )"""
                )
            elif table == "factor_catalog_snapshot":
                conn.execute(
                    """CREATE TEMP TABLE factor_catalog_snapshot (
                        snapshot_id TEXT PRIMARY KEY,
                        created_at DOUBLE PRECISION NOT NULL DEFAULT 0.0
                    )"""
                )
            elif table == "brain_action_plan_eval":
                conn.execute(
                    """CREATE TEMP TABLE brain_action_plan_eval (
                        eval_id TEXT PRIMARY KEY,
                        plan_id TEXT NOT NULL DEFAULT '',
                        created_at DOUBLE PRECISION NOT NULL DEFAULT 0.0
                    )"""
                )
            elif table == "evolution_decision":
                conn.execute(
                    """CREATE TEMP TABLE evolution_decision (
                        decision_id TEXT PRIMARY KEY,
                        created_at DOUBLE PRECISION NOT NULL DEFAULT 0.0
                    )"""
                )
            elif table == "trade_outcome_review":
                conn.execute(
                    """CREATE TEMP TABLE trade_outcome_review (
                        review_id TEXT PRIMARY KEY
                    )"""
                )
            elif table == "runtime_config_snapshot":
                conn.execute(
                    """CREATE TEMP TABLE runtime_config_snapshot (
                        config_version TEXT NOT NULL DEFAULT '',
                        created_at DOUBLE PRECISION NOT NULL DEFAULT 0.0
                    )"""
                )
            else:
                conn.execute(f'CREATE TEMP TABLE "{table}" (seed INTEGER)')

        first = run_state_schema_migrations(wrapped, runner_id="pytest_pg_temp")
        second = run_state_schema_migrations(wrapped, runner_id="pytest_pg_temp")
        status = require_state_schema_version(wrapped)

        assert first["applied_count"] == len(STATE_SCHEMA_MIGRATIONS)
        assert second["applied_count"] == 0
        assert status["ok"] is True
        assert wrapped.commit_calls == 2
        assert conn.execute("SELECT to_regclass('broker_execution_intent') AS name").fetchone()["name"]
        assert conn.execute("SELECT to_regclass('idx_jobs_claim_ready') AS name").fetchone()["name"]
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
        sample_columns = {
            row["column_name"]
            for row in conn.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema=current_schema()
                  AND table_name='autonomous_learning_sample'
                """
            ).fetchall()
        }
        assert {
            "system_contaminated",
            "governance_eligible",
            "governance_effective_weight",
            "governance_eligibility_version",
            "governance_eligibility_fingerprint",
            "governance_ineligible_reason",
        } <= sample_columns
        stats_columns = {
            row["column_name"]
            for row in conn.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema=current_schema()
                  AND table_name='experience_pattern_stats'
                """
            ).fetchall()
        }
        assert {
            "effective_sample_count",
            "weighted_win_count",
            "weighted_bad_loss_count",
            "weighted_avg_reward",
            "governance_eligibility_version",
            "governance_eligibility_fingerprint",
        } <= stats_columns
        runtime_writer_columns = {
            "factor_governance_shadow_audit": {"inference_id", "factor", "payload_json", "created_at"},
            "llm_advisory_audit": {"audit_id", "target_type", "response_json", "created_at"},
            "meta_model_shadow_audit": {"inference_id", "posture", "ledger_decision_id", "created_at"},
            "meta_shadow_report_snapshot": {"report_id", "model_type", "payload_json", "created_at"},
            "model_influence_decision": {"influence_id", "control_surface", "fused_decision_json", "created_at"},
            "model_influence_effect": {"effect_id", "influence_id", "outcome_json", "matured_at"},
            "offmarket_high_load_job_audit": {
                "audit_id", "job_name", "payload_json", "finished_at",
                "training_window_key", "phase", "worker_instance_id",
                "heartbeat_at", "input_bytes_estimate",
            },
            "state_payload_archive": {
                "archive_hash", "source_table", "source_id", "raw_sha256", "payload_bytes",
            },
            "open_quality_shadow_audit": {"inference_id", "decision_id", "quality_score", "created_at"},
            "position_quality_shadow_audit": {"inference_id", "position_id", "hold_score", "created_at"},
        }
        for table, required_columns in runtime_writer_columns.items():
            actual = {
                row["column_name"]
                for row in conn.execute(
                    """
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_schema=current_schema() AND table_name=%s
                    """,
                    (table,),
                ).fetchall()
            }
            assert required_columns <= actual, table
        for index in (
            "idx_factor_governance_audit_created",
            "idx_llm_advisory_audit_target",
            "idx_meta_shadow_report_snapshot_created",
            "idx_model_influence_decision_subject_ts",
            "idx_open_quality_shadow_audit_position",
            "idx_position_quality_shadow_audit_position",
            "idx_experience_memory_source",
            "idx_experience_memory_source_append",
            "idx_factor_catalog_snapshot_created",
        ):
            assert conn.execute("SELECT to_regclass(%s) AS name", (index,)).fetchone()["name"], index
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
