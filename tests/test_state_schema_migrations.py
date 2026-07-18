from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from backend.core import db
from backend.core.state_schema_migrations import (
    STATE_SCHEMA_BASELINE_TABLES,
    STATE_SCHEMA_MIGRATION_LOCK_ID,
    STATE_SCHEMA_MIGRATIONS,
    StateSchemaMigrationError,
    StateSchemaVersionError,
    require_state_schema_version,
    run_state_schema_migrations,
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
        self.tables = set(STATE_SCHEMA_BASELINE_TABLES if tables is None else tables)
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


def test_schema_status_fails_closed_without_ledger() -> None:
    conn = _FakePgConn()

    status = state_schema_status(conn)

    assert status["ok"] is False
    assert status["current_version"] == 0
    assert status["missing_required_versions"] == [1]
    with pytest.raises(StateSchemaVersionError, match="current_version=0 minimum_version=1"):
        require_state_schema_version(conn)


def test_schema_status_requires_every_baseline_table() -> None:
    tables = set(STATE_SCHEMA_BASELINE_TABLES) - {"learning_experiment_reservation"}
    conn = _FakePgConn(tables=tables, applied=_applied_v1())

    status = state_schema_status(conn)

    assert status["ok"] is False
    assert status["missing_baseline_tables"] == ["learning_experiment_reservation"]


def test_runner_applies_once_under_lock_and_records_checksum() -> None:
    conn = _FakePgConn()

    first = run_state_schema_migrations(conn, runner_id="pytest")
    migration_ddl_count = sum(
        "CREATE TABLE broker_execution_intent" in sql for sql, _params in conn.executed
    )
    second = run_state_schema_migrations(conn, runner_id="pytest")

    assert first["applied_count"] == 1
    assert first["current_version"] == 1
    assert first["applied"][0]["checksum"] == STATE_SCHEMA_MIGRATIONS[0].checksum()
    assert second["applied_count"] == 0
    assert second["current_version"] == 1
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


def test_init_state_db_checks_version_before_legacy_compatibility(monkeypatch) -> None:
    events: list[str] = []
    conn = _FakePgConn()
    monkeypatch.setattr(db, "get_state_pg_conn", lambda: conn)
    monkeypatch.setattr(
        db,
        "require_state_schema_version",
        lambda _conn: events.append("version_gate"),
    )
    monkeypatch.setattr(
        db,
        "_ensure_state_schema_compatibility",
        lambda _conn: events.append("legacy_compatibility"),
    )

    db.init_state_db()

    assert events == ["version_gate", "legacy_compatibility"]
    assert conn.commits == 1
    assert conn.closed is True


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


def test_learning_worker_re_raises_schema_version_failure(monkeypatch) -> None:
    import backend.core.logging as logging_module
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

    with pytest.raises(StateSchemaVersionError):
        worker._bootstrap_runtime()


def test_cli_defaults_to_check_and_requires_explicit_apply(monkeypatch, capsys) -> None:
    import scripts.state_schema_migrate as command

    read_only_calls: list[bool] = []

    def _connect(*, read_only: bool):
        read_only_calls.append(read_only)
        return _FakePgConn(applied=_applied_v1())

    monkeypatch.setattr(command, "get_state_pg_conn", _connect)

    assert command.main([]) == 0
    assert command.main(["--apply", "--runner-id", "pytest"]) == 0

    assert read_only_calls == [True, False]
    assert '"ok": true' in capsys.readouterr().out
