from __future__ import annotations

from pathlib import Path
import re
from types import SimpleNamespace

import pytest

from backend.core import state_store
from backend.core import db as db_module
from backend.core.state_store import (
    RuntimeStateConnection,
    RuntimeStateSchemaMissingError,
    RuntimeStateCursor,
    RuntimeStateSchemaWriteError,
    StateMigrationConnection,
    connect_state_migration_store,
    connect_state_store,
    is_state_schema_write_sql,
    validate_runtime_state_schema,
)


class _FakeConn:
    def __init__(self) -> None:
        self.executed: list[str] = []
        self.closed = False

    def execute(self, sql, *_args, **_kwargs):
        self.executed.append(str(sql))
        return self

    def close(self) -> None:
        self.closed = True


class _Rows:
    def __init__(self, *, one=None, all_rows=None) -> None:
        self.one = one
        self.all_rows = list(all_rows or [])

    def fetchone(self):
        return self.one

    def fetchall(self):
        return list(self.all_rows)


def test_schema_write_classifier_ignores_dml_and_comments() -> None:
    assert is_state_schema_write_sql("INSERT INTO runtime_kv VALUES (%s, %s, %s)") is False
    assert is_state_schema_write_sql(
        "INSERT INTO factor_runtime_projection VALUES (%s)\n"
        "ON CONFLICT (factor_id)\n"
        "DO UPDATE SET status=excluded.status"
    ) is False
    assert is_state_schema_write_sql("-- CREATE TABLE fake\nSELECT 1") is False
    assert is_state_schema_write_sql("CREATE TABLE IF NOT EXISTS runtime_kv (key TEXT)") is True
    assert is_state_schema_write_sql("ALTER TABLE jobs ADD COLUMN unsafe TEXT") is True
    assert is_state_schema_write_sql("DROP TABLE jobs") is True
    assert is_state_schema_write_sql("SELECT 1;\nDROP TABLE jobs") is True


def test_runtime_connection_rejects_unvalidated_schema_mutation() -> None:
    with pytest.raises(RuntimeStateSchemaWriteError, match="only schema writer"):
        RuntimeStateConnection.execute(object(), "DROP TABLE jobs")
    with pytest.raises(RuntimeStateSchemaWriteError, match="only schema writer"):
        RuntimeStateCursor.execute(
            SimpleNamespace(connection=object()),
            "DO $$ BEGIN NULL; END $$",
        )


def test_runtime_schema_guard_covers_bytes_queries_and_executemany() -> None:
    with pytest.raises(RuntimeStateSchemaWriteError, match="only schema writer"):
        RuntimeStateConnection.execute(object(), b"DROP TABLE jobs")
    with pytest.raises(RuntimeStateSchemaWriteError, match="only schema writer"):
        RuntimeStateCursor.executemany(
            SimpleNamespace(connection=object()),
            b"ALTER TABLE jobs ADD COLUMN unsafe TEXT",
            [()],
        )


def test_legacy_create_ensure_is_catalog_validation_only(monkeypatch) -> None:
    calls: list[str] = []

    def _execute(_conn, sql, _params=None):
        calls.append(" ".join(str(sql).split()))
        if "to_regclass" in str(sql):
            return _Rows(one={"object_name": "runtime_kv"})
        if "information_schema.columns" in str(sql):
            return _Rows(all_rows=[
                {"column_name": "key"},
                {"column_name": "value_json"},
                {"column_name": "updated_at"},
            ])
        raise AssertionError(f"unexpected SQL: {sql}")

    monkeypatch.setattr(state_store, "_base_execute", _execute)

    result = validate_runtime_state_schema(
        object(),
        """
        CREATE TABLE IF NOT EXISTS runtime_kv (
            key TEXT PRIMARY KEY,
            value_json TEXT NOT NULL DEFAULT '{}',
            updated_at REAL NOT NULL DEFAULT 0.0
        )
        """,
    )

    assert result["ok"] is True
    assert result["validated_statement_count"] == 1
    assert len(calls) == 2
    assert all(not call.startswith("CREATE") for call in calls)


def test_explicit_runtime_schema_validation_rejects_dml() -> None:
    with pytest.raises(ValueError, match="DDL declarations only"):
        validate_runtime_state_schema(object(), "SELECT 1")


def test_legacy_create_ensure_fails_closed_on_missing_column(monkeypatch) -> None:
    def _execute(_conn, sql, _params=None):
        if "to_regclass" in str(sql):
            return _Rows(one={"object_name": "jobs"})
        return _Rows(all_rows=[{"column_name": "id"}])

    monkeypatch.setattr(state_store, "_base_execute", _execute)

    with pytest.raises(RuntimeStateSchemaMissingError, match="claim_token"):
        state_store._validate_runtime_schema_statement(
            object(),
            "CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, claim_token TEXT NOT NULL)",
        )


def test_index_catalog_validation_checks_table_and_key_definition(monkeypatch) -> None:
    existing_definition = {
        "sql": (
            "CREATE INDEX idx_experience_memory_source_append "
            "ON state_v1.experience_memory USING btree "
            "(source_table, source_id, append_source)"
        )
    }

    def _execute(_conn, sql, _params=None):
        if "to_regclass" in str(sql) and "pg_get_indexdef" not in str(sql):
            return _Rows(one={"object_name": "idx_experience_memory_source_append"})
        if "pg_get_indexdef" in str(sql):
            return _Rows(one={"index_definition": existing_definition["sql"]})
        raise AssertionError(f"unexpected SQL: {sql}")

    monkeypatch.setattr(state_store, "_base_execute", _execute)
    declaration = (
        "CREATE INDEX IF NOT EXISTS idx_experience_memory_source_append "
        "ON experience_memory(source_table, source_id, append_source)"
    )

    assert validate_runtime_state_schema(object(), declaration)["ok"] is True

    existing_definition["sql"] = (
        "CREATE INDEX idx_experience_memory_source_append "
        "ON state_v1.experience_memory USING btree "
        "(source_table, source_id, created_at)"
    )
    with pytest.raises(RuntimeStateSchemaMissingError, match="does not match"):
        validate_runtime_state_schema(object(), declaration)


def test_runtime_connector_never_creates_schema(monkeypatch) -> None:
    fake = _FakeConn()
    monkeypatch.setattr(
        RuntimeStateConnection,
        "connect",
        classmethod(lambda _cls, *_args, **_kwargs: fake),
    )

    conn = connect_state_store("postgresql://runtime", read_only=False)

    assert conn is fake
    assert fake.executed == ['SET search_path TO "state_v1", public']
    assert not any("CREATE" in sql.upper() for sql in fake.executed)


def test_runtime_read_only_connector_protects_current_and_future_transactions(
    monkeypatch,
) -> None:
    fake = _FakeConn()
    monkeypatch.setattr(
        RuntimeStateConnection,
        "connect",
        classmethod(lambda _cls, *_args, **_kwargs: fake),
    )

    connect_state_store("postgresql://runtime", read_only=True)

    assert fake.read_only is True
    assert fake.executed == ['SET search_path TO "state_v1", public']


def test_migration_connector_is_the_explicit_schema_writer(monkeypatch) -> None:
    fake = _FakeConn()
    monkeypatch.setattr(
        StateMigrationConnection,
        "connect",
        classmethod(lambda _cls, *_args, **_kwargs: fake),
    )

    conn = connect_state_migration_store("postgresql://migration")

    assert conn is fake
    assert fake.executed == [
        'CREATE SCHEMA IF NOT EXISTS "state_v1"',
        'SET search_path TO "state_v1", public',
    ]


def test_legacy_sqlite_restore_script_contains_no_schema_writer() -> None:
    root = Path(__file__).resolve().parents[1]
    source = (root / "scripts" / "migrate_state_sqlite_to_pg.py").read_text(encoding="utf-8")
    for statement in (
        "CREATE SCHEMA",
        "DROP SCHEMA",
        "CREATE TABLE",
        "ALTER TABLE",
        "CREATE INDEX",
    ):
        assert statement not in source.upper()


def test_only_explicit_migration_cli_opens_migration_connection() -> None:
    root = Path(__file__).resolve().parents[1]
    callers: set[str] = set()
    for folder in ("backend", "scripts", "research"):
        for path in (root / folder).rglob("*.py"):
            if path == root / "backend" / "core" / "state_store.py":
                continue
            if "connect_state_migration_store(" in path.read_text(encoding="utf-8"):
                callers.add(path.relative_to(root).as_posix())
    assert callers == {"scripts/state_schema_migrate.py"}


def test_backend_and_learning_worker_schema_ensures_are_catalog_validations() -> None:
    root = Path(__file__).resolve().parents[1]
    explicit_validation_paths = (
        "backend/runtime/evolution_orchestrator.py",
        "backend/services/backend_readiness_snapshot.py",
        "backend/services/factor_catalog.py",
        "backend/services/learning_cycle_watermark.py",
        "backend/services/learning_experiment_admission.py",
        "backend/services/learning_research_jobs.py",
        "backend/services/model_influence.py",
        "backend/services/runtime_config_overlay.py",
        "backend/services/trade_lesson_memory.py",
        "research/factor_governance_lightgbm.py",
        "research/llm_advisory.py",
        "research/open_quality_lightgbm.py",
        "research/position_quality_lightgbm.py",
    )
    for relative in explicit_validation_paths:
        source = (root / relative).read_text(encoding="utf-8")
        assert "validate_runtime_state_schema" in source, relative


def test_runtime_state_ddl_objects_have_a_schema_contract() -> None:
    root = Path(__file__).resolve().parents[1]
    object_pattern = re.compile(
        r"CREATE\s+(?:UNIQUE\s+)?(?:TABLE|INDEX)\s+"
        r"(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z_][A-Za-z0-9_]*)",
        re.IGNORECASE,
    )
    runtime_objects: set[str] = set()
    for folder in ("backend/services", "backend/runtime", "research"):
        for path in (root / folder).rglob("*.py"):
            runtime_objects.update(
                match.group(1).lower()
                for match in object_pattern.finditer(path.read_text(encoding="utf-8"))
            )

    schema_source = (root / "backend/core/db.py").read_text(encoding="utf-8")
    for path in (root / "migrations/state_pg").glob("*.sql"):
        schema_source += "\n" + path.read_text(encoding="utf-8")
    contracted = {
        match.group(1).lower() for match in object_pattern.finditer(schema_source)
    }
    assert runtime_objects - contracted == set()


def test_runtime_init_validates_schemas_without_running_experiments_migration(
    monkeypatch,
    tmp_path,
) -> None:
    calls: list[str] = []
    experiments_path = tmp_path / "experiments.db"
    experiments_path.touch()
    monkeypatch.setattr(db_module, "_initialized", False)
    monkeypatch.setattr(db_module, "EXPERIMENTS_DB", experiments_path)
    monkeypatch.setattr(
        db_module,
        "init_state_db",
        lambda: calls.append("state_minimum_version"),
    )
    monkeypatch.setattr(
        db_module,
        "validate_experiments_db_schema",
        lambda path: calls.append(f"experiments_minimum_schema:{path.name}"),
    )
    monkeypatch.setattr(
        db_module,
        "init_experiments_db",
        lambda: (_ for _ in ()).throw(AssertionError("runtime schema mutation")),
    )

    db_module.init_all()

    assert calls == [
        "state_minimum_version",
        "experiments_minimum_schema:experiments.db",
    ]


def test_runtime_init_does_not_create_optional_experiments_store(
    monkeypatch,
    tmp_path,
) -> None:
    experiments_path = tmp_path / "missing-experiments.db"
    monkeypatch.setattr(db_module, "_initialized", False)
    monkeypatch.setattr(db_module, "EXPERIMENTS_DB", experiments_path)
    monkeypatch.setattr(db_module, "init_state_db", lambda: None)
    monkeypatch.setattr(
        db_module,
        "validate_experiments_db_schema",
        lambda _path: (_ for _ in ()).throw(AssertionError("optional store validated")),
    )

    db_module.init_all()

    assert not experiments_path.exists()


def test_experiments_schema_validation_is_read_only_and_fails_when_stale(
    tmp_path,
) -> None:
    import sqlite3

    db_path = tmp_path / "experiments.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("CREATE TABLE experiments (run_id TEXT PRIMARY KEY)")
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(RuntimeError, match="missing tables|missing columns"):
        db_module.validate_experiments_db_schema(db_path)

    conn = sqlite3.connect(db_path)
    try:
        # Validation must not have added compatibility columns.
        columns = {
            str(row[1])
            for row in conn.execute('PRAGMA table_info("experiments")').fetchall()
        }
    finally:
        conn.close()
    assert columns == {"run_id"}


def test_operator_experiments_migration_materializes_complete_schema(tmp_path) -> None:
    db_path = tmp_path / "operator-experiments.db"

    db_module.init_experiments_db(db_path)
    db_module.validate_experiments_db_schema(db_path)

    import sqlite3

    conn = sqlite3.connect(db_path)
    try:
        tables = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    finally:
        conn.close()
    assert {
        "experiments",
        "model_registry",
        "model_shadow_candidate",
        "model_canary_review",
        "model_canary_trial",
        "model_inference_audit",
    } <= tables


def test_production_experiments_prepare_is_validation_only(monkeypatch, tmp_path) -> None:
    import sqlite3

    db_path = tmp_path / "production-experiments.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("CREATE TABLE experiments (run_id TEXT PRIMARY KEY)")
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setattr(db_module, "EXPERIMENTS_DB", db_path)

    with pytest.raises(RuntimeError, match="missing tables|missing columns"):
        db_module.prepare_experiments_store(db_path)

    conn = sqlite3.connect(db_path)
    try:
        tables = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    finally:
        conn.close()
    assert tables == {"experiments"}


def test_experiments_runtime_classes_contain_no_schema_writer() -> None:
    root = Path(__file__).resolve().parents[1]
    for relative in (
        "research/experiment_tracker.py",
        "research/model_registry.py",
        "research/model_shadow_queue.py",
        "research/model_canary.py",
        "research/model_canary_executor.py",
        "research/model_inference_contract.py",
    ):
        source = (root / relative).read_text(encoding="utf-8").upper()
        assert "CREATE TABLE" not in source, relative
        assert "ALTER TABLE" not in source, relative
        assert "CREATE INDEX" not in source, relative


def test_experiments_schema_cli_defaults_to_check_and_requires_apply(
    monkeypatch,
    tmp_path,
    capsys,
) -> None:
    import scripts.experiments_schema_migrate as command

    db_path = tmp_path / "experiments.db"
    events: list[str] = []
    monkeypatch.setattr(command, "EXPERIMENTS_DB", db_path)
    monkeypatch.setattr(
        command,
        "validate_experiments_db_schema",
        lambda path: events.append(f"check:{path.name}"),
    )
    monkeypatch.setattr(
        command,
        "init_experiments_db",
        lambda path: events.append(f"apply:{path.name}"),
    )

    assert command.main([]) == 0
    assert command.main(["--apply"]) == 0
    assert events == [
        "check:experiments.db",
        "apply:experiments.db",
        "check:experiments.db",
    ]
    assert '"ok": true' in capsys.readouterr().out
