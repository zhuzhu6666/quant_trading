from __future__ import annotations

import re
import sqlite3
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from backend.core import db
from backend.core.state_schema_migrations import (
    STATE_SCHEMA_LEGACY_BASELINE_TABLES,
    STATE_SCHEMA_LATEST_VERSION,
    STATE_SCHEMA_MIN_VERSION,
    STATE_SCHEMA_MIGRATION_LOCK_ID,
    STATE_SCHEMA_MIGRATIONS,
    StateSchemaMigrationError,
    StateSchemaVersionError,
    require_state_schema_version,
    run_state_schema_migrations,
    state_schema_bootstrap_statements,
    state_schema_status,
)


class _Rows:
    def __init__(self, *, one: Any = None, all_rows: list[Any] | None = None) -> None:
        self._one = one
        self._all = list(all_rows or [])

    def fetchone(self):
        return self._one

    def fetchall(self):
        return list(self._all)


class _FakePgConn:
    def __init__(
        self,
        *,
        tables: set[str] | None = None,
        applied: dict[int, dict[str, Any]] | None = None,
        lock_acquired: bool = True,
    ) -> None:
        self.tables = set(
            STATE_SCHEMA_LEGACY_BASELINE_TABLES if tables is None else tables
        )
        self.applied = dict(applied or {})
        if self.applied:
            self.tables.add("state_schema_migration")
        self.lock_acquired = lock_acquired
        self.executed: list[tuple[str, Any]] = []
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def execute(self, sql: str, params: Any = None):
        normalized = " ".join(sql.split())
        self.executed.append((normalized, params))
        if "FROM information_schema.tables" in normalized:
            return _Rows(all_rows=[{"table_name": name} for name in sorted(self.tables)])
        if "FROM state_schema_migration" in normalized and normalized.startswith("SELECT version"):
            return _Rows(all_rows=[self.applied[key] for key in sorted(self.applied)])
        if "pg_try_advisory_xact_lock" in normalized:
            assert params == (STATE_SCHEMA_MIGRATION_LOCK_ID,)
            return _Rows(one={"acquired": self.lock_acquired})
        if normalized.startswith("CREATE TABLE IF NOT EXISTS state_schema_migration"):
            self.tables.add("state_schema_migration")
            return _Rows()
        if normalized.startswith("INSERT INTO state_schema_migration"):
            (
                version,
                migration_name,
                checksum,
                statement_count,
                runner_id,
                execution_ms,
                applied_at,
            ) = params
            self.applied[int(version)] = {
                "version": int(version),
                "migration_name": migration_name,
                "checksum": checksum,
                "statement_count": int(statement_count),
                "runner_id": runner_id,
                "execution_ms": float(execution_ms),
                "applied_at": float(applied_at),
            }
        return _Rows()

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed = True


def _applied_v1() -> dict[int, dict[str, Any]]:
    migration = STATE_SCHEMA_MIGRATIONS[0]
    return {
        1: {
            "version": 1,
            "migration_name": migration.name,
            "checksum": migration.checksum(),
            "statement_count": len(migration.statements()),
            "runner_id": "test",
            "execution_ms": 1.0,
            "applied_at": 1.0,
        }
    }


def _applied_all() -> dict[int, dict[str, Any]]:
    return {
        migration.version: {
            "version": migration.version,
            "migration_name": migration.name,
            "checksum": migration.checksum(),
            "statement_count": len(migration.statements()),
            "runner_id": "test",
            "execution_ms": 1.0,
            "applied_at": 1.0,
        }
        for migration in STATE_SCHEMA_MIGRATIONS
    }


def test_phase0b_migration_contains_required_tables_and_columns() -> None:
    sql = STATE_SCHEMA_MIGRATIONS[0].sql()

    for table in (
        "broker_execution_intent",
        "governance_mutation_intent",
        "factor_lifecycle_state",
        "factor_runtime_projection",
        "auth_session",
    ):
        assert f"CREATE TABLE {table}" in sql
    for column in (
        "execution_intent_id",
        "mutation_id",
        "governance_eligibility_version",
        "governance_effective_weight",
        "finalized_mutation_id",
        "applied_mutation_id",
    ):
        assert column in sql
    assert "factor_runtime_projection" in sql
    assert "config_version BIGINT" in sql
    assert "config_hash TEXT" in sql
    assert sql.count("ALTER TABLE autonomous_learning_sample") == 1
    assert "CREATE TABLE IF NOT EXISTS" not in sql
    assert "ALTER TABLE IF EXISTS" not in sql
    assert "ADD COLUMN IF NOT EXISTS" not in sql


def test_phase3_migration_adds_projection_recovery_and_active_scope_gate() -> None:
    sql = STATE_SCHEMA_MIGRATIONS[1].sql()

    for column in (
        "projection_attempts",
        "projection_error_json",
        "rolled_back_at",
        "rollback_mutation_id",
        "superseded_at",
        "superseded_by_mutation_id",
    ):
        assert column in sql
    assert "idx_governance_mutation_active_scope" in sql
    assert "status IN ('reserved', 'prepared')" in sql


def test_phase5_migration_adds_durable_jobs_and_retires_runtime_ddl() -> None:
    sql = STATE_SCHEMA_MIGRATIONS[2].sql()

    for column in (
        "claim_token",
        "heartbeat_at",
        "lease_expires_at",
        "cancel_requested",
        "idempotency_key",
        "max_attempts",
        "attempt_count",
        "log_tail_json",
    ):
        assert column in sql
    assert "idx_jobs_claim_ready" in sql
    assert "idx_jobs_running_lease" in sql
    assert "idx_jobs_kind_idempotency" in sql
    assert "handler_version TEXT NOT NULL DEFAULT 'legacy'" in sql
    assert "SKIP LOCKED" not in sql
    for compatibility_object in (
        "supervisor_counterfactual_history",
        "learning_experiment_reservation",
        "nursery_exploration_reservation",
        "idx_proposal_registry_projection_key",
        "idx_v16_brain_command_claim",
    ):
        assert compatibility_object in sql


def test_phase5_runtime_schema_writer_retirement_materializes_worker_objects() -> None:
    sql = STATE_SCHEMA_MIGRATIONS[3].sql()

    for table in (
        "factor_governance_shadow_audit",
        "llm_advisory_audit",
        "meta_model_shadow_audit",
        "meta_shadow_report_snapshot",
        "model_influence_decision",
        "model_influence_effect",
        "offmarket_high_load_job_audit",
        "open_quality_shadow_audit",
        "position_quality_shadow_audit",
    ):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in sql
    for index in (
        "idx_factor_governance_audit_created",
        "idx_llm_advisory_audit_target",
        "idx_meta_shadow_report_snapshot_created",
        "idx_model_influence_decision_subject_ts",
        "idx_open_quality_shadow_audit_position",
        "idx_position_quality_shadow_audit_position",
        "idx_experience_memory_source",
        "idx_factor_catalog_snapshot_created",
    ):
        assert f"INDEX IF NOT EXISTS {index}" in sql


def test_phase3_governance_eligibility_migration_adds_weighted_contract() -> None:
    sql = STATE_SCHEMA_MIGRATIONS[4].sql()

    for column in (
        "governance_eligibility_fingerprint",
        "effective_sample_count",
        "weighted_win_count",
        "weighted_bad_loss_count",
        "weighted_avg_reward",
    ):
        assert column in sql
    assert "idx_autonomous_learning_governance_eligible" in sql
    assert "idx_policy_suggestion_governance_eligible" in sql


def test_phase3_factor_lifecycle_identity_migration_adds_unique_name_gate() -> None:
    sql = STATE_SCHEMA_MIGRATIONS[5].sql()

    assert "CREATE UNIQUE INDEX idx_factor_lifecycle_unique_name" in sql
    assert "ON factor_lifecycle_state(factor_name)" in sql


def test_phase3_v16_authority_freshness_migration_is_additive_and_backfills() -> None:
    sql = STATE_SCHEMA_MIGRATIONS[6].sql()

    assert "ADD COLUMN IF NOT EXISTS authority_issued_at" in sql
    assert "WHEN created_at > 0.0 THEN created_at" in sql
    assert "WHERE authority_issued_at <= 0.0" in sql
    assert "idx_v16_brain_command_authority" in sql


def test_phase5_runtime_schema_contract_completion_is_additive() -> None:
    sql = STATE_SCHEMA_MIGRATIONS[7].sql()

    for table in ("runtime_kv", "canary_state"):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in sql
    for table, column in (
        ("canary_state", "fresh_evidence_bars"),
        ("autonomous_learning_sample", "evidence_contract_json"),
        ("position_supervisor_trace", "trace_integrity"),
        ("proposal_registry", "source_reliability_json"),
        ("brain_state_snapshot", "memory_json"),
        ("brain_medium_impact_governance", "candidate_id"),
        ("experience_memory", "append_source"),
    ):
        assert f"ALTER TABLE {table}" in sql
        assert column in sql
    assert "idx_experience_memory_source_append" in sql
    assert "ON experience_memory(source_table, source_id, append_source)" in sql
    assert "DROP " not in sql.upper()
    assert {
        "autonomous_learning_sample",
        "position_supervisor_trace",
        "proposal_registry",
        "brain_state_snapshot",
        "brain_medium_impact_governance",
        "experience_memory",
    } <= set(STATE_SCHEMA_LEGACY_BASELINE_TABLES)


def test_phase3_runtime_overlay_authority_migration_supports_minimal_baseline() -> None:
    sql = STATE_SCHEMA_MIGRATIONS[8].sql()

    assert "ADD COLUMN IF NOT EXISTS legacy_authority_json" in sql
    assert "ADD COLUMN IF NOT EXISTS updated_at" in sql
    assert "idx_runtime_config_overlay_mutation" in sql
    assert "ON runtime_config_overlay(mutation_id, updated_at)" in sql
    assert "DROP " not in sql.upper()


def test_proposal_registry_source_ref_contract_migration_is_additive() -> None:
    sql = STATE_SCHEMA_MIGRATIONS[9].sql()

    assert "ALTER TABLE proposal_registry" in sql
    assert "ADD COLUMN IF NOT EXISTS source_ref_id" in sql
    assert "ADD COLUMN IF NOT EXISTS updated_at" in sql
    assert "idx_proposal_registry_source_ref_updated_v2" in sql
    assert "ON proposal_registry(source_ref_id, updated_at DESC)" in sql
    assert "DROP " not in sql.upper()


def test_execution_price_repair_migration_is_additive() -> None:
    sql = STATE_SCHEMA_MIGRATIONS[10].sql()

    assert "ALTER TABLE ctrader_deals" in sql
    assert "ADD COLUMN IF NOT EXISTS raw_execution_price" in sql
    assert "ADD COLUMN IF NOT EXISTS price_contract" in sql
    assert "CREATE TABLE IF NOT EXISTS data_repair_run" in sql
    assert "CREATE TABLE IF NOT EXISTS data_repair_item" in sql
    assert "DROP " not in sql.upper()


def test_state_payload_dedupe_migration_adds_refs_without_data_cleanup() -> None:
    migration = next(item for item in STATE_SCHEMA_MIGRATIONS if item.version == 14)
    sql = migration.sql()

    assert migration.version == 14
    for table in (
        "runtime_config_payload",
        "brain_action_plan_eval_payload",
        "mutation_payload",
    ):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in sql
    for column in (
        "payload_hash",
        "evaluation_run_id",
        "canonical_event_id",
        "projection_type",
        "content_fingerprint",
    ):
        assert column in sql
    assert "VACUUM" not in sql.upper()
    assert "DELETE" not in sql.upper()
    assert "DROP " not in sql.upper()
    assert "idx_brain_action_plan_eval_run_plan_unique" in sql


def test_training_window_archive_migration_is_schema_only() -> None:
    migration = next(item for item in STATE_SCHEMA_MIGRATIONS if item.version == 15)
    sql = migration.sql()

    assert migration.version == 15
    for column in (
        "training_window_key",
        "phase",
        "worker_instance_id",
        "heartbeat_at",
        "input_bytes_estimate",
        "verdict_archive_hash",
        "review_archive_hash",
    ):
        assert column in sql
    assert "state_payload_archive" in sql
    assert "idx_offmarket_training_window_unique" in sql
    assert "VACUUM" not in sql.upper()
    assert "DELETE" not in sql.upper()


def test_legacy_fact_retirement_is_guarded_and_drops_only_retired_projections() -> None:
    migration = next(item for item in STATE_SCHEMA_MIGRATIONS if item.version == 30)
    sql = migration.sql()

    assert migration.name == "retire_legacy_fact_tables"
    assert "retired_fact_rows_must_be_empty" in sql
    assert "SELECT 1 / CASE" in sql
    for table in (
        "runtime.decision_ledger",
        "runtime.decision_factor_snapshot",
        "runtime.autonomous_learning_sample",
        "runtime.order_lifecycle_event",
        "runtime.position_lifecycle_event",
        "runtime.trade_outcome_review",
        "runtime.position_supervisor_trace",
        "runtime.supervisor_counterfactual_review",
        "runtime.supervisor_counterfactual_history",
        "runtime.decision_log",
        "runtime.lifecycle_events",
    ):
        assert f"DROP TABLE IF EXISTS {table}" in sql
    # These two artifacts may already be absent after the canonical cutover;
    # DROP remains idempotent, while the guard only queries tables guaranteed by
    # the runtime baseline and therefore remains executable on the current DB.
    assert "DROP TABLE IF EXISTS runtime.state_payload_archive" in sql
    assert "DROP TABLE IF EXISTS canonical_v2.legacy_mapping" in sql


def test_canonical_v2_foundation_migration_is_schema_only_and_reference_based() -> None:
    migration = next(
        item for item in STATE_SCHEMA_MIGRATIONS if item.version == 16
    )
    sql = migration.sql()

    assert migration.version == 16
    assert "CREATE SCHEMA IF NOT EXISTS canonical_v2" in sql
    for table in (
        "canonical_v2.payload_blob",
        "canonical_v2.event",
        "canonical_v2.event_relation",
        "canonical_v2.state_version",
        "canonical_v2.training_sample",
        "canonical_v2.dataset_manifest",
        "canonical_v2.dataset_manifest_member",
        "canonical_v2.projection_run",
        "canonical_v2.legacy_mapping",
    ):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in sql
    for column in (
        "payload_hash",
        "idempotency_key",
        "causation_id",
        "source_event_ids",
        "sample_digest",
        "source_watermark",
        "mapping_confidence",
    ):
        assert column in sql
    assert "TIMESTAMPTZ" in sql
    assert "REFERENCES canonical_v2.payload_blob" in sql
    assert "REFERENCES canonical_v2.event" in sql
    ddl = sql.upper()
    for forbidden in ("\nDELETE ", "\nUPDATE ", "\nVACUUM ", "\nDROP "):
        assert forbidden not in ddl


def test_schema_status_fails_closed_without_ledger() -> None:
    conn = _FakePgConn()

    status = state_schema_status(conn)

    assert status["ok"] is False
    assert status["current_version"] == 0
    assert status["missing_required_versions"] == list(
        range(1, STATE_SCHEMA_MIN_VERSION + 1)
    )
    with pytest.raises(
        StateSchemaVersionError,
        match=rf"current_version=0 minimum_version={STATE_SCHEMA_MIN_VERSION}",
    ):
        require_state_schema_version(conn)


def test_partial_legacy_baseline_is_not_migratable() -> None:
    conn = _FakePgConn(tables={"autonomous_learning_sample"})

    status = state_schema_status(conn, minimum_version=1)

    assert "runtime_config_snapshot" in status["missing_baseline_tables"]
    assert status["ok"] is False
    with pytest.raises(StateSchemaMigrationError, match="incomplete PostgreSQL state baseline"):
        run_state_schema_migrations(conn)


def test_runner_bootstraps_a_truly_empty_schema_before_migrations() -> None:
    conn = _FakePgConn(tables=set())

    result = run_state_schema_migrations(conn, runner_id="pytest-clean")

    assert result["bootstrap"]["applied"] is True
    assert result["bootstrap"]["statement_count"] > 0
    assert result["bootstrap"]["checksum"]
    assert any(
        "CREATE TABLE autonomous_learning_sample" in sql
        for sql, _params in conn.executed
    )
    bootstrap_sql = "\n".join(state_schema_bootstrap_statements())
    for table in STATE_SCHEMA_LEGACY_BASELINE_TABLES:
        assert (
            f"CREATE TABLE {table} " in bootstrap_sql
            or f"CREATE TABLE IF NOT EXISTS {table} " in bootstrap_sql
        )
    assert sum(
        "CREATE TABLE" in sql and "broker_execution_intent" in sql
        for sql, _params in conn.executed
    ) == 1
    assert result["applied_count"] == len(STATE_SCHEMA_MIGRATIONS)


def test_clean_bootstrap_defines_each_legacy_table_once() -> None:
    bootstrap_sql = "\n".join(state_schema_bootstrap_statements())
    table_names = re.findall(
        r"(?im)^CREATE TABLE(?: IF NOT EXISTS)?\s+(?:runtime\.)?([a-z_][a-z0-9_]*)",
        bootstrap_sql,
    )

    duplicates = sorted(
        table for table, count in Counter(table_names).items() if count > 1
    )
    assert duplicates == []


def test_schema_status_fails_closed_on_migration_checksum_mismatch() -> None:
    applied = _applied_all()
    applied[20]["checksum"] = "tampered-checksum"

    status = state_schema_status(_FakePgConn(applied=applied))

    assert status["current_version"] == STATE_SCHEMA_LATEST_VERSION
    assert status["missing_required_versions"] == []
    assert status["migration_mismatches"][0]["version"] == 20
    assert status["ok"] is False
    with pytest.raises(StateSchemaVersionError, match="checksum_or_name_mismatch"):
        require_state_schema_version(_FakePgConn(applied=applied))


def test_runner_applies_once_under_lock_and_records_checksum() -> None:
    conn = _FakePgConn()

    first = run_state_schema_migrations(conn, runner_id="pytest")
    migration_ddl_count = sum(
        "CREATE TABLE broker_execution_intent" in sql for sql, _params in conn.executed
    )
    second = run_state_schema_migrations(conn, runner_id="pytest")

    assert first["applied_count"] == len(STATE_SCHEMA_MIGRATIONS)
    assert first["current_version"] == STATE_SCHEMA_MIGRATIONS[-1].version
    assert first["applied"][0]["checksum"] == STATE_SCHEMA_MIGRATIONS[0].checksum()
    assert second["applied_count"] == 0
    assert second["current_version"] == STATE_SCHEMA_MIGRATIONS[-1].version
    assert migration_ddl_count == 1
    assert conn.commits == 2
    assert any(sql == "SET LOCAL lock_timeout = '5s'" for sql, _params in conn.executed)
    assert any("pg_try_advisory_xact_lock" in sql for sql, _params in conn.executed)


def test_runner_rejects_lock_contention_without_applying() -> None:
    conn = _FakePgConn(lock_acquired=False)

    with pytest.raises(StateSchemaMigrationError, match="holds the advisory lock"):
        run_state_schema_migrations(conn)

    assert conn.commits == 0
    assert conn.rollbacks == 1
    assert not any("CREATE TABLE broker_execution_intent" in sql for sql, _ in conn.executed)


def test_runner_rejects_checked_in_checksum_drift() -> None:
    applied = _applied_v1()
    applied[1] = {**applied[1], "checksum": "bad-checksum"}
    conn = _FakePgConn(applied=applied)

    with pytest.raises(StateSchemaMigrationError, match="does not match"):
        run_state_schema_migrations(conn)

    assert conn.commits == 0
    assert conn.rollbacks == 1


def test_init_state_db_only_checks_version_on_read_only_connection(monkeypatch) -> None:
    events: list[str] = []
    conn = _FakePgConn()
    read_only_calls: list[bool] = []

    def _connect(*, read_only: bool = False):
        read_only_calls.append(read_only)
        return conn

    monkeypatch.setattr(db, "get_state_pg_conn", _connect)
    monkeypatch.setattr(
        db,
        "require_state_schema_version",
        lambda _conn: events.append("version_gate"),
    )

    db.init_state_db()

    assert events == ["version_gate"]
    assert read_only_calls == [True]
    assert conn.commits == 0
    assert conn.closed is True
    assert not hasattr(db, "_ensure_state_schema_compatibility")


def test_backend_treats_schema_version_error_as_blocking_in_dry_run() -> None:
    from backend import app as app_module

    error = StateSchemaVersionError(
        {
            "current_version": 0,
            "minimum_version": 1,
            "missing_baseline_tables": [],
            "missing_required_versions": [1],
            "migration_mismatches": [],
        }
    )

    assert app_module._state_db_failure_is_blocking(
        error,
        SimpleNamespace(effective_send_orders=False),
    ) is True
    assert app_module._state_db_failure_is_blocking(
        RuntimeError("offline"),
        SimpleNamespace(effective_send_orders=False),
    ) is False


@pytest.mark.asyncio
async def test_backend_schema_gate_runs_before_overlay_restore(monkeypatch) -> None:
    import backend.app as app_module
    import backend.core.auth as auth_module
    import backend.services.execution_semantics as semantics_module
    import backend.services.runtime_config_startup as startup_module
    import backend.services.startup_status as startup_status_module

    events: list[str] = []
    error = StateSchemaVersionError(
        {
            "current_version": 0,
            "minimum_version": 1,
            "missing_baseline_tables": [],
            "missing_required_versions": [1],
            "migration_mismatches": [],
        }
    )
    monkeypatch.setattr(app_module, "setup_logging", lambda: None)
    monkeypatch.setattr(app_module, "_init_observability", lambda: None)
    monkeypatch.setattr(auth_module, "validate_auth_config", lambda: None)
    monkeypatch.setattr(startup_status_module, "clear_startup_issues", lambda: None)
    monkeypatch.setattr(startup_status_module, "record_startup_issue", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        startup_module,
        "load_yaml_runtime_config",
        lambda: (SimpleNamespace(), {}),
    )
    monkeypatch.setattr(
        semantics_module,
        "validate_execution_semantics",
        lambda _yaml, _runtime: SimpleNamespace(effective_send_orders=False),
    )
    monkeypatch.setattr(
        startup_module,
        "restore_runtime_config_on_startup",
        lambda *_args, **_kwargs: events.append("overlay_restore"),
    )
    monkeypatch.setattr(db, "init_all", lambda: (_ for _ in ()).throw(error))

    with pytest.raises(StateSchemaVersionError):
        async with app_module.lifespan(SimpleNamespace()):
            raise AssertionError("lifespan must not start")

    assert events == []


def test_learning_worker_re_raises_schema_version_failure(monkeypatch, tmp_path) -> None:
    import backend.core.logging as logging_module
    from backend.services.learning_worker_capability import LearningWorkerCapability
    import scripts.learning_worker as worker

    error = StateSchemaVersionError(
        {
            "current_version": 0,
            "minimum_version": 1,
            "missing_baseline_tables": [],
            "missing_required_versions": [1],
            "migration_mismatches": [],
        }
    )
    monkeypatch.setattr(logging_module, "setup_logging", lambda: None)
    monkeypatch.setattr(db, "init_all", lambda: (_ for _ in ()).throw(error))
    capability_db = tmp_path / "worker-capability.db"
    conn = sqlite3.connect(capability_db)
    try:
        conn.execute(
            "CREATE TABLE runtime_kv (key TEXT PRIMARY KEY, value_json TEXT NOT NULL, updated_at REAL NOT NULL)"
        )
        conn.commit()
    finally:
        conn.close()
    capability = LearningWorkerCapability(
        db_path=capability_db,
        boot_id="schema-version-failure-test",
    )

    with pytest.raises(StateSchemaVersionError):
        worker._bootstrap_runtime(capability)

    assert capability.snapshot()["boot_status"] == "failed"


def test_cli_defaults_to_check_and_requires_explicit_apply(monkeypatch, capsys) -> None:
    import scripts.state_schema_migrate as command

    read_only_calls: list[bool] = []

    def _connect(*, read_only: bool):
        read_only_calls.append(read_only)
        return _FakePgConn(applied=_applied_all())

    migration_calls: list[str] = []

    def _migration_connect(dsn: str):
        migration_calls.append(dsn)
        return _FakePgConn(applied=_applied_all())

    monkeypatch.setattr(command, "get_state_pg_conn", _connect)
    monkeypatch.setattr(command, "state_pg_dsn", lambda: "postgresql://migration-test")
    monkeypatch.setattr(command, "connect_state_migration_store", _migration_connect)

    assert command.main([]) == 0
    assert command.main(["--apply", "--runner-id", "pytest"]) == 0

    assert read_only_calls == [True]
    assert migration_calls == ["postgresql://migration-test"]
    assert '"ok": true' in capsys.readouterr().out


def test_ci_bootstraps_and_checks_disposable_postgres_before_integration() -> None:
    root = Path(__file__).resolve().parents[1]
    workflow_path = root / ".github" / "workflows" / "quality-gates.yml"
    if not workflow_path.exists():
        pytest.skip(".github/workflows/quality-gates.yml is not part of the server sparse checkout")
    workflow = workflow_path.read_text(
        encoding="utf-8"
    )

    bootstrap = "python tests/integration/bootstrap_state_pg.py"
    schema_check = "python scripts/state_schema_migrate.py --check"
    integration = "pytest -q -m postgres_integration --timeout=30"
    assert bootstrap in workflow
    assert schema_check in workflow
    assert integration in workflow
    assert workflow.index(bootstrap) < workflow.index(schema_check) < workflow.index(integration)


def test_ci_postgres_bootstrap_is_test_only_and_database_name_guarded() -> None:
    root = Path(__file__).resolve().parents[1]
    source = (root / "tests" / "integration" / "bootstrap_state_pg.py").read_text(
        encoding="utf-8"
    )

    assert 'os.environ.get("CI"' in source
    assert 'normalized.endswith("_test")' in source
    assert 'normalized.startswith("test_")' in source
    assert "run_state_schema_migrations" in source
