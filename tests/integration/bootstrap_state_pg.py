#!/usr/bin/env python3
"""Create the disposable pre-migration PostgreSQL baseline used by CI.

This is deliberately test-only.  Production databases must already contain
the historical ``state_v1`` baseline and are migrated exclusively through
``scripts/state_schema_migrate.py``.  The guard below refuses to run unless
GitHub-style CI is active and the target database name is explicitly test-like.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import psycopg
from psycopg.rows import dict_row


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.core.state_schema_migrations import (  # noqa: E402
    STATE_SCHEMA_MIN_VERSION,
    require_state_schema_version,
    run_state_schema_migrations,
)
from backend.core.state_store import (  # noqa: E402
    STATE_SCHEMA,
    connect_state_migration_store,
)


_BASELINE_DDL = (
    """CREATE TABLE autonomous_learning_sample (
        sample_id TEXT PRIMARY KEY,
        sample_type TEXT NOT NULL DEFAULT '',
        event_ts DOUBLE PRECISION NOT NULL DEFAULT 0.0
    )""",
    "CREATE TABLE decision_ledger (seed INTEGER)",
    "CREATE TABLE learning_application_effect (seed INTEGER)",
    "CREATE TABLE learning_application_log (seed INTEGER)",
    """CREATE TABLE learning_experiment_reservation (
        reservation_id TEXT PRIMARY KEY,
        scope_type TEXT NOT NULL DEFAULT '',
        scope_key TEXT NOT NULL DEFAULT '',
        action TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'reserved',
        application_id TEXT NOT NULL DEFAULT '',
        expires_at DOUBLE PRECISION NOT NULL DEFAULT 0.0,
        created_at DOUBLE PRECISION NOT NULL DEFAULT 0.0,
        updated_at DOUBLE PRECISION NOT NULL DEFAULT 0.0
    )""",
    """CREATE TABLE order_lifecycle_event (
        event_id TEXT PRIMARY KEY,
        event_ts DOUBLE PRECISION NOT NULL DEFAULT 0.0
    )""",
    """CREATE TABLE policy_suggestion (
        suggestion_id TEXT PRIMARY KEY,
        status TEXT NOT NULL DEFAULT 'proposed',
        created_at DOUBLE PRECISION NOT NULL DEFAULT 0.0
    )""",
    "CREATE TABLE runtime_config_overlay (seed INTEGER)",
    "CREATE TABLE runtime_config_snapshot (seed INTEGER)",
    """CREATE TABLE position_supervisor_trace (
        trace_id TEXT PRIMARY KEY,
        position_id TEXT NOT NULL DEFAULT '',
        created_at DOUBLE PRECISION NOT NULL DEFAULT 0.0
    )""",
    """CREATE TABLE brain_state_snapshot (
        snapshot_id TEXT PRIMARY KEY,
        created_at DOUBLE PRECISION NOT NULL DEFAULT 0.0
    )""",
    """CREATE TABLE brain_medium_impact_governance (
        governance_id TEXT PRIMARY KEY,
        created_at DOUBLE PRECISION NOT NULL DEFAULT 0.0
    )""",
    """CREATE TABLE v16_brain_command (
        command_id TEXT PRIMARY KEY,
        target_agent TEXT NOT NULL DEFAULT '',
        scope_type TEXT NOT NULL DEFAULT '',
        decision TEXT NOT NULL DEFAULT '',
        created_at DOUBLE PRECISION NOT NULL DEFAULT 0.0,
        updated_at DOUBLE PRECISION NOT NULL DEFAULT 0.0
    )""",
    """CREATE TABLE brain_governance_candidate_review (
        review_id TEXT PRIMARY KEY,
        candidate_id TEXT NOT NULL DEFAULT '',
        created_at DOUBLE PRECISION NOT NULL DEFAULT 0.0
    )""",
    """CREATE TABLE proposal_registry (
        proposal_id TEXT PRIMARY KEY,
        source_agent TEXT NOT NULL DEFAULT '',
        proposal_type TEXT NOT NULL DEFAULT '',
        control_surface TEXT NOT NULL DEFAULT '',
        target_scope TEXT NOT NULL DEFAULT ''
    )""",
    """CREATE TABLE jobs (
        id TEXT PRIMARY KEY,
        kind TEXT DEFAULT '',
        status TEXT DEFAULT 'pending',
        params_json TEXT DEFAULT '{}',
        result_json TEXT DEFAULT '{}',
        progress DOUBLE PRECISION DEFAULT 0.0,
        error TEXT DEFAULT '',
        created_at DOUBLE PRECISION,
        updated_at DOUBLE PRECISION
    )""",
    """CREATE TABLE experience_memory (
        experience_id TEXT PRIMARY KEY,
        source_table TEXT NOT NULL DEFAULT '',
        source_id TEXT NOT NULL DEFAULT '',
        created_at DOUBLE PRECISION NOT NULL DEFAULT 0.0
    )""",
    """CREATE TABLE experience_pattern_stats (
        scope_type TEXT NOT NULL,
        scope_key TEXT NOT NULL,
        PRIMARY KEY (scope_type, scope_key)
    )""",
    """CREATE TABLE factor_catalog_snapshot (
        snapshot_id TEXT PRIMARY KEY,
        created_at DOUBLE PRECISION NOT NULL DEFAULT 0.0
    )""",
)


def _require_disposable_ci_database(dsn: str) -> str:
    if os.environ.get("CI", "").strip().lower() != "true":
        raise RuntimeError("refusing PostgreSQL test bootstrap outside CI")
    with psycopg.connect(dsn, autocommit=True, row_factory=dict_row) as probe:
        database = str(
            probe.execute("SELECT current_database() AS name").fetchone()["name"] or ""
        )
    normalized = database.lower()
    if not (normalized.endswith("_test") or normalized.startswith("test_")):
        raise RuntimeError(
            f"refusing PostgreSQL test bootstrap for non-test database {database!r}"
        )
    return database


def main() -> int:
    dsn = os.environ.get("QUANT_STATE_PG_DSN", "").strip()
    if not dsn:
        raise RuntimeError("QUANT_STATE_PG_DSN is required")
    database = _require_disposable_ci_database(dsn)

    conn = connect_state_migration_store(dsn, schema=STATE_SCHEMA)
    try:
        existing = {
            str(row["table_name"])
            for row in conn.execute(
                """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema=current_schema()
                """
            ).fetchall()
        }
        if existing:
            status = require_state_schema_version(
                conn,
                minimum_version=STATE_SCHEMA_MIN_VERSION,
            )
            payload = {
                "ok": True,
                "database": database,
                "schema": STATE_SCHEMA,
                "bootstrap": "already_current",
                "status": status,
            }
            print(json.dumps(payload, sort_keys=True))
            return 0

        for statement in _BASELINE_DDL:
            conn.execute(statement)
        conn.commit()

        migrated = run_state_schema_migrations(conn, runner_id="github-actions-ci")
        status = require_state_schema_version(
            conn,
            minimum_version=STATE_SCHEMA_MIN_VERSION,
        )
        payload = {
            "ok": True,
            "database": database,
            "schema": STATE_SCHEMA,
            "bootstrap": "created_and_migrated",
            "migration": migrated,
            "status": status,
        }
        print(json.dumps(payload, sort_keys=True))
        return 0
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
