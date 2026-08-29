"""Versioned PostgreSQL migrations for the ``runtime`` PostgreSQL schema.

The application processes only validate a minimum schema version.  Applying
migrations is an explicit operator action through ``scripts/state_schema_migrate.py``.
Migrations are forward-only, checksum protected, and serialized by a
PostgreSQL transaction advisory lock. Most migrations are additive; explicitly
catalogued retirements guard against non-empty facts before dropping them.
"""

from __future__ import annotations

import hashlib
import os
import socket
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Sequence

from backend.core.db_helpers import row_value as _row_value


_PROJECT_ROOT = Path(__file__).resolve().parents[2]
STATE_SCHEMA_MIGRATION_DIR: Final[Path] = _PROJECT_ROOT / "migrations" / "state_pg"
STATE_SCHEMA_BOOTSTRAP_PATH: Final[Path] = (
    STATE_SCHEMA_MIGRATION_DIR / "bootstrap_legacy_baseline.sql"
)
STATE_SCHEMA_MIGRATION_TABLE: Final[str] = "state_schema_migration"
STATE_SCHEMA_MIGRATION_LOCK_ID: Final[int] = 0x5155414E54534D31  # ASCII: QUANTSM1

# Migrations 0001-0018 were written against this historical pre-ledger shape.
# A truly empty schema receives the same checked-in baseline inside the
# migration transaction.  A non-empty schema without the ledger must already
# contain every dependency below; partial legacy databases are never guessed
# into a deployable state.
STATE_SCHEMA_LEGACY_BASELINE_TABLES: Final[tuple[str, ...]] = (
    "autonomous_learning_sample",
    "autonomy_health_snapshot",
    "autonomy_scope_approval_event",
    "autonomy_scope_enforcement_event",
    "brain_action_plan",
    "brain_action_plan_eval",
    "brain_governance_candidate",
    "brain_governance_candidate_review",
    "brain_live_ready_guardrail",
    "brain_low_impact_execution",
    "brain_medium_impact_governance",
    "brain_memory",
    "brain_state_snapshot",
    "ctrader_deals",
    "decision_factor_snapshot",
    "decision_ledger",
    "evolution_decision",
    "evolution_events",
    "evolution_run",
    "experience_memory",
    "experience_pattern_stats",
    "experiments",
    "factor_catalog_snapshot",
    "factor_contribution_review",
    "factor_health",
    "incident_playbook_event",
    "incident_playbook_run",
    "jobs",
    "learning_application_effect",
    "learning_application_log",
    "learning_experiment_reservation",
    "live_autonomy_unlock_event",
    "model_canary_review",
    "model_canary_trial",
    "model_inference_audit",
    "model_permission_audit",
    "model_shadow_candidate",
    "order_lifecycle_event",
    "parameter_template_active",
    "parameter_template_registry",
    "parameter_template_release_candidate",
    "parameter_template_switch_log",
    "policy_suggestion",
    "position_lifecycle_event",
    "position_supervisor_trace",
    "proposal_registry",
    "recovery_position_state",
    "release_approval_event",
    "release_run",
    "replay_report",
    "runtime_config_overlay",
    "runtime_config_snapshot",
    "shadow_factor_perf",
    "supervisor_counterfactual_review",
    "trade_outcome_review",
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
        # Migration files are deliberately plain forward-only DDL. Stored
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
    StateSchemaMigration(10, "proposal_registry_source_ref_contract", "0010_proposal_registry_source_ref_contract.sql"),
    StateSchemaMigration(11, "execution_price_repair_ledger", "0011_execution_price_repair_ledger.sql"),
    StateSchemaMigration(12, "risk_daily_equity", "0012_risk_daily_equity.sql"),
    StateSchemaMigration(13, "decision_factor_snapshot_lineage", "0013_decision_factor_snapshot_lineage.sql"),
    StateSchemaMigration(14, "state_payload_dedupe", "0014_state_payload_dedupe.sql"),
    StateSchemaMigration(15, "training_window_and_payload_archive", "0015_training_window_and_payload_archive.sql"),
    StateSchemaMigration(16, "canonical_v2_foundation", "0016_canonical_v2_foundation.sql"),
    StateSchemaMigration(17, "canonical_v2_sample_domain", "0017_canonical_v2_sample_domain.sql"),
    StateSchemaMigration(18, "boundary_event_types", "0018_boundary_event_types.sql"),
    StateSchemaMigration(19, "secondary_index_backfill", "0019_secondary_index_backfill.sql"),
    StateSchemaMigration(20, "backfill_minimal_table_columns", "0020_backfill_minimal_table_columns.sql"),
    StateSchemaMigration(21, "create_missed_runtime_tables", "0021_create_missed_runtime_tables.sql"),
    StateSchemaMigration(22, "backfill_single_column_gaps", "0022_backfill_single_column_gaps.sql"),
    StateSchemaMigration(23, "rebuild_proposal_registry_index", "0023_rebuild_proposal_registry_index.sql"),
    StateSchemaMigration(24, "rebuild_jobs_claim_ready_index", "0024_rebuild_jobs_claim_ready_index.sql"),
    StateSchemaMigration(25, "restore_offmarket_training_window_unique", "0025_restore_offmarket_training_window_unique.sql"),
    StateSchemaMigration(26, "align_factor_runtime_projection", "0026_align_factor_runtime_projection.sql"),
    StateSchemaMigration(27, "build_backfilled_live_indexes", "0027_build_backfilled_live_indexes.sql"),
    StateSchemaMigration(28, "align_factor_lifecycle_state", "0028_align_factor_lifecycle_state.sql"),
    StateSchemaMigration(29, "runtime_broker_execution_intent", "0029_runtime_broker_execution_intent.sql"),
    StateSchemaMigration(30, "retire_legacy_fact_tables", "0030_retire_legacy_fact_tables.sql"),
    StateSchemaMigration(31, "align_factor_health", "0031_align_factor_health.sql"),
    StateSchemaMigration(32, "restore_jobs_primary_key", "0032_restore_jobs_primary_key.sql"),
    StateSchemaMigration(33, "factor_runtime_projection_primary_key", "0033_factor_runtime_projection_primary_key.sql"),
)
STATE_SCHEMA_LATEST_VERSION: Final[int] = STATE_SCHEMA_MIGRATIONS[-1].version
# Runtime code consumes the complete checked-in state contract.  A process
# must not start against an intermediate schema while an operator is applying
# the catalogued migrations.
STATE_SCHEMA_MIN_VERSION: Final[int] = STATE_SCHEMA_LATEST_VERSION


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


def state_schema_bootstrap_statements() -> tuple[str, ...]:
    """Return the clean-install legacy baseline consumed before migration 1."""
    try:
        value = STATE_SCHEMA_BOOTSTRAP_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        raise StateSchemaMigrationError(
            f"cannot read state schema bootstrap from {STATE_SCHEMA_BOOTSTRAP_PATH}: {exc}"
        ) from exc
    statements = tuple(part.strip() for part in value.split(";") if part.strip())
    if not statements:
        raise StateSchemaMigrationError("state schema bootstrap is empty")
    return statements


def _bootstrap_checksum() -> str:
    try:
        value = STATE_SCHEMA_BOOTSTRAP_PATH.read_bytes()
    except OSError as exc:
        raise StateSchemaMigrationError(
            f"cannot read state schema bootstrap from {STATE_SCHEMA_BOOTSTRAP_PATH}: {exc}"
        ) from exc
    return hashlib.sha256(value).hexdigest()


def _fresh_install_statement_is_superseded(
    migration: StateSchemaMigration,
    statement: str,
) -> bool:
    """Skip the obsolete v1 broker table copy; migration 29 is its sole owner."""
    if migration.version != 1:
        return False
    uncommented = "\n".join(
        line
        for line in str(statement or "").splitlines()
        if not line.lstrip().startswith("--")
    )
    normalized = " ".join(uncommented.lower().split())
    return normalized.startswith("create table broker_execution_intent ") or (
        normalized.startswith("create index idx_broker_execution_intent_")
        and " on broker_execution_intent" in normalized
    )


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
    missing_baseline_tables = (
        sorted(set(STATE_SCHEMA_LEGACY_BASELINE_TABLES) - tables)
        if tables and STATE_SCHEMA_MIGRATION_TABLE not in tables
        else []
    )
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

    applied_now: list[dict[str, Any]] = []
    effective_runner_id = str(runner_id or _default_runner_id())
    bootstrap = {
        "applied": False,
        "statement_count": 0,
        "checksum": "",
    }
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
        locked_tables = _schema_table_names(conn)
        clean_install = not locked_tables
        if locked_tables and STATE_SCHEMA_MIGRATION_TABLE not in locked_tables:
            missing_legacy = sorted(
                set(STATE_SCHEMA_LEGACY_BASELINE_TABLES) - locked_tables
            )
            if missing_legacy:
                raise StateSchemaMigrationError(
                    "refusing to migrate an incomplete PostgreSQL state baseline: "
                    + ",".join(missing_legacy)
                )
        if clean_install:
            bootstrap_statements = state_schema_bootstrap_statements()
            for statement in bootstrap_statements:
                conn.execute(statement)
            bootstrap = {
                "applied": True,
                "statement_count": len(bootstrap_statements),
                "checksum": _bootstrap_checksum(),
            }
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
            executed_statements = tuple(
                statement
                for statement in statements
                if not (
                    clean_install
                    and _fresh_install_statement_is_superseded(
                        migration,
                        statement,
                    )
                )
            )
            for statement in executed_statements:
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
                    len(executed_statements),
                    effective_runner_id,
                    execution_ms,
                    applied_at,
                ),
            )
            row = {
                "version": migration.version,
                "migration_name": migration.name,
                "checksum": checksum,
                "statement_count": len(executed_statements),
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
        "bootstrap": bootstrap,
        "runner_id": effective_runner_id,
    }
