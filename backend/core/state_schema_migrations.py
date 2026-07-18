"""Versioned PostgreSQL migrations for the ``state_v1`` runtime schema.

The application processes only validate a minimum schema version.  Applying
migrations is an explicit operator action through ``scripts/state_schema_migrate.py``.
Migrations are additive, checksum protected, and serialized by a PostgreSQL
transaction advisory lock.
"""

from __future__ import annotations

import hashlib
import os
import socket
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Sequence


_PROJECT_ROOT = Path(__file__).resolve().parents[2]
STATE_SCHEMA_MIGRATION_DIR: Final[Path] = _PROJECT_ROOT / "migrations" / "state_pg"
STATE_SCHEMA_MIGRATION_TABLE: Final[str] = "state_schema_migration"
STATE_SCHEMA_MIGRATION_LOCK_ID: Final[int] = 0x5155414E54534D31  # ASCII: QUANTSM1

# Phase 0B starts from the already-cut-over state_v1 schema.  The explicit
# runner must never make an empty or partial schema look deployable merely by
# creating the new migration ledger and foundation tables.
STATE_SCHEMA_BASELINE_TABLES: Final[tuple[str, ...]] = (
    "autonomous_learning_sample",
    "brain_governance_candidate_review",
    "brain_medium_impact_governance",
    "brain_state_snapshot",
    "decision_ledger",
    "experience_memory",
    "experience_pattern_stats",
    "factor_catalog_snapshot",
    "jobs",
    "learning_application_effect",
    "learning_application_log",
    "learning_experiment_reservation",
    "order_lifecycle_event",
    "policy_suggestion",
    "position_supervisor_trace",
    "proposal_registry",
    "runtime_config_overlay",
    "runtime_config_snapshot",
    "v16_brain_command",
)


class StateSchemaError(RuntimeError):
    """Base class for state schema validation and migration failures."""


class StateSchemaMigrationError(StateSchemaError):
    """Raised when an explicit migration cannot be applied safely."""


class StateSchemaVersionError(StateSchemaError):
    """Raised when an application process sees an incompatible state schema."""

    def __init__(self, status: dict[str, Any]):
        self.status = dict(status)
        current = int(status.get("current_version") or 0)
        minimum = int(status.get("minimum_version") or 0)
        missing_tables = list(status.get("missing_baseline_tables") or [])
        missing_versions = list(status.get("missing_required_versions") or [])
        mismatches = list(status.get("migration_mismatches") or [])
        reasons: list[str] = []
        if current < minimum:
            reasons.append(f"current_version={current} minimum_version={minimum}")
        if missing_tables:
            reasons.append(f"missing_baseline_tables={','.join(missing_tables)}")
        if missing_versions:
            reasons.append(f"missing_required_versions={','.join(map(str, missing_versions))}")
        if mismatches:
            reasons.append("migration_checksum_or_name_mismatch")
        super().__init__("PostgreSQL state schema version gate failed: " + "; ".join(reasons))


@dataclass(frozen=True)
class StateSchemaMigration:
    version: int
    name: str
    filename: str

    @property
    def path(self) -> Path:
        return STATE_SCHEMA_MIGRATION_DIR / self.filename

    def sql(self) -> str:
        try:
            value = self.path.read_text(encoding="utf-8")
        except OSError as exc:
            raise StateSchemaMigrationError(
                f"cannot read state migration {self.version} from {self.path}: {exc}"
            ) from exc
        if not value.strip():
            raise StateSchemaMigrationError(f"state migration {self.version} is empty: {self.path}")
        return value

    def checksum(self) -> str:
        return hashlib.sha256(self.sql().encode("utf-8")).hexdigest()

    def statements(self) -> tuple[str, ...]:
        # Migration files are deliberately plain additive DDL. Stored
        # procedures/dollar-quoted bodies and semicolons inside SQL literals
        # are prohibited by contract; tests enforce that narrow format.
        return tuple(part.strip() for part in self.sql().split(";") if part.strip())


STATE_SCHEMA_MIGRATIONS: Final[tuple[StateSchemaMigration, ...]] = (
    StateSchemaMigration(1, "phase0b_foundation", "0001_phase0b_foundation.sql"),
    StateSchemaMigration(2, "phase3_governance_coordinator", "0002_phase3_governance_coordinator.sql"),
    StateSchemaMigration(3, "phase5_persistent_job_queue", "0003_phase5_persistent_job_queue.sql"),
    StateSchemaMigration(4, "phase5_runtime_schema_writer_retirement", "0004_phase5_runtime_schema_writer_retirement.sql"),
    StateSchemaMigration(5, "phase3_governance_eligibility_weighting", "0005_phase3_governance_eligibility_weighting.sql"),
    StateSchemaMigration(6, "phase3_factor_lifecycle_identity", "0006_phase3_factor_lifecycle_identity.sql"),
    StateSchemaMigration(7, "phase3_v16_authority_freshness", "0007_phase3_v16_authority_freshness.sql"),
    StateSchemaMigration(8, "phase5_runtime_schema_contract_completion", "0008_phase5_runtime_schema_contract_completion.sql"),
    StateSchemaMigration(9, "phase3_runtime_overlay_authority", "0009_phase3_runtime_overlay_authority.sql"),
)
STATE_SCHEMA_MIN_VERSION: Final[int] = 9
STATE_SCHEMA_LATEST_VERSION: Final[int] = STATE_SCHEMA_MIGRATIONS[-1].version


_LEDGER_DDL: Final[str] = """
CREATE TABLE IF NOT EXISTS state_schema_migration (
    version INTEGER PRIMARY KEY CHECK (version > 0),
    migration_name TEXT NOT NULL UNIQUE,
    checksum TEXT NOT NULL,
    statement_count INTEGER NOT NULL DEFAULT 0,
    runner_id TEXT NOT NULL DEFAULT '',
    execution_ms DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    applied_at DOUBLE PRECISION NOT NULL DEFAULT 0.0
)
"""


def _row_value(row: Any, key: str, index: int = 0, default: Any = None) -> Any:
    if row is None:
        return default
    if isinstance(row, dict):
        return row.get(key, default)
    try:
        return row[index]
    except (IndexError, KeyError, TypeError):
        return default


def _validate_catalog(migrations: Sequence[StateSchemaMigration]) -> None:
    versions = [int(item.version) for item in migrations]
    expected = list(range(1, len(versions) + 1))
    if versions != expected:
        raise StateSchemaMigrationError(
            f"state migration catalog must be contiguous from version 1: {versions}"
        )
    names = [item.name for item in migrations]
    if len(names) != len(set(names)):
        raise StateSchemaMigrationError("state migration names must be unique")


def _schema_table_names(conn: Any) -> set[str]:
    rows = conn.execute(
        """
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema = current_schema()
        """
    ).fetchall()
    return {str(_row_value(row, "table_name", 0, "") or "") for row in rows}


def _applied_migrations(conn: Any, *, ledger_exists: bool) -> dict[int, dict[str, Any]]:
    if not ledger_exists:
        return {}
    rows = conn.execute(
        """
        SELECT version, migration_name, checksum, statement_count,
               runner_id, execution_ms, applied_at
        FROM state_schema_migration
        ORDER BY version
        """
    ).fetchall()
    return {
        int(_row_value(row, "version", 0, 0) or 0): {
            "version": int(_row_value(row, "version", 0, 0) or 0),
            "migration_name": str(_row_value(row, "migration_name", 1, "") or ""),
            "checksum": str(_row_value(row, "checksum", 2, "") or ""),
            "statement_count": int(_row_value(row, "statement_count", 3, 0) or 0),
            "runner_id": str(_row_value(row, "runner_id", 4, "") or ""),
            "execution_ms": float(_row_value(row, "execution_ms", 5, 0.0) or 0.0),
            "applied_at": float(_row_value(row, "applied_at", 6, 0.0) or 0.0),
        }
        for row in rows
    }


def state_schema_status(
    conn: Any,
    *,
    minimum_version: int = STATE_SCHEMA_MIN_VERSION,
    migrations: Sequence[StateSchemaMigration] = STATE_SCHEMA_MIGRATIONS,
) -> dict[str, Any]:
    """Return the non-mutating PostgreSQL state schema compatibility status."""
    _validate_catalog(migrations)
    tables = _schema_table_names(conn)
    applied = _applied_migrations(
        conn,
        ledger_exists=STATE_SCHEMA_MIGRATION_TABLE in tables,
    )
    catalog = {item.version: item for item in migrations}
    mismatches: list[dict[str, Any]] = []
    for version, row in applied.items():
        expected = catalog.get(version)
        if expected is None:
            continue
        expected_checksum = expected.checksum()
        if row["migration_name"] != expected.name or row["checksum"] != expected_checksum:
            mismatches.append(
                {
                    "version": version,
                    "applied_name": row["migration_name"],
                    "expected_name": expected.name,
                    "applied_checksum": row["checksum"],
                    "expected_checksum": expected_checksum,
                }
            )
    required_versions = set(range(1, int(minimum_version) + 1))
    missing_required_versions = sorted(required_versions - set(applied))
    missing_baseline_tables = sorted(set(STATE_SCHEMA_BASELINE_TABLES) - tables)
    current_version = max(applied, default=0)
    ok = (
        not missing_baseline_tables
        and not missing_required_versions
        and not mismatches
        and current_version >= int(minimum_version)
    )
    return {
        "schema_version": "state_schema_status.v1",
        "ok": ok,
        "current_version": current_version,
        "minimum_version": int(minimum_version),
        "latest_known_version": max((item.version for item in migrations), default=0),
        "ledger_exists": STATE_SCHEMA_MIGRATION_TABLE in tables,
        "missing_baseline_tables": missing_baseline_tables,
        "missing_required_versions": missing_required_versions,
        "migration_mismatches": mismatches,
        "applied_migrations": [applied[key] for key in sorted(applied)],
    }


def require_state_schema_version(
    conn: Any,
    *,
    minimum_version: int = STATE_SCHEMA_MIN_VERSION,
    migrations: Sequence[StateSchemaMigration] = STATE_SCHEMA_MIGRATIONS,
) -> dict[str, Any]:
    """Fail closed when the state schema is below the runtime minimum."""
    status = state_schema_status(
        conn,
        minimum_version=minimum_version,
        migrations=migrations,
    )
    if not status["ok"]:
        raise StateSchemaVersionError(status)
    return status


def _default_runner_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def run_state_schema_migrations(
    conn: Any,
    *,
    runner_id: str = "",
    migrations: Sequence[StateSchemaMigration] = STATE_SCHEMA_MIGRATIONS,
) -> dict[str, Any]:
    """Apply pending additive migrations atomically under an advisory lock."""
    _validate_catalog(migrations)
    before = state_schema_status(conn, minimum_version=0, migrations=migrations)
    if before["missing_baseline_tables"]:
        raise StateSchemaMigrationError(
            "refusing to migrate an incomplete PostgreSQL state baseline: "
            + ",".join(before["missing_baseline_tables"])
        )

    applied_now: list[dict[str, Any]] = []
    effective_runner_id = str(runner_id or _default_runner_id())
    try:
        conn.execute("SET LOCAL lock_timeout = '5s'")
        conn.execute("SET LOCAL statement_timeout = '120s'")
        lock_row = conn.execute(
            "SELECT pg_try_advisory_xact_lock(%s) AS acquired",
            (STATE_SCHEMA_MIGRATION_LOCK_ID,),
        ).fetchone()
        if not bool(_row_value(lock_row, "acquired", 0, False)):
            raise StateSchemaMigrationError(
                "another state schema migration runner holds the advisory lock"
            )
        conn.execute(_LEDGER_DDL)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_state_schema_migration_applied "
            "ON state_schema_migration(applied_at)"
        )

        applied = _applied_migrations(conn, ledger_exists=True)
        known = {item.version: item for item in migrations}
        for version, row in applied.items():
            expected = known.get(version)
            if expected is None:
                continue
            if row["migration_name"] != expected.name or row["checksum"] != expected.checksum():
                raise StateSchemaMigrationError(
                    f"applied state migration {version} does not match the checked-in catalog"
                )

        for migration in migrations:
            if migration.version in applied:
                continue
            later_versions = sorted(version for version in applied if version > migration.version)
            if later_versions:
                raise StateSchemaMigrationError(
                    f"cannot apply state migration {migration.version} after versions {later_versions}"
                )
            statements = migration.statements()
            started = time.monotonic()
            for statement in statements:
                conn.execute(statement)
            execution_ms = (time.monotonic() - started) * 1000.0
            checksum = migration.checksum()
            applied_at = time.time()
            conn.execute(
                """
                INSERT INTO state_schema_migration
                    (version, migration_name, checksum, statement_count,
                     runner_id, execution_ms, applied_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    migration.version,
                    migration.name,
                    checksum,
                    len(statements),
                    effective_runner_id,
                    execution_ms,
                    applied_at,
                ),
            )
            row = {
                "version": migration.version,
                "migration_name": migration.name,
                "checksum": checksum,
                "statement_count": len(statements),
                "runner_id": effective_runner_id,
                "execution_ms": execution_ms,
                "applied_at": applied_at,
            }
            applied[migration.version] = row
            applied_now.append(row)

        # The advisory lock is transaction-scoped; publishing the ledger and
        # every additive DDL statement in one commit makes crash retry simple.
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    return {
        "schema_version": "state_schema_migration_run.v1",
        "ok": True,
        "previous_version": int(before.get("current_version") or 0),
        "current_version": max(applied, default=0),
        "latest_known_version": max((item.version for item in migrations), default=0),
        "applied_count": len(applied_now),
        "applied": applied_now,
        "runner_id": effective_runner_id,
    }
