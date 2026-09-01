#!/usr/bin/env python3
"""Migrate the disposable PostgreSQL database used by CI.

The production migration runner now owns both a truly empty schema and legacy
upgrades.  This wrapper only adds the destructive-target guard required for a
CI service database; it carries no second baseline DDL copy.
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
        migrated = run_state_schema_migrations(conn, runner_id="github-actions-ci")
        status = require_state_schema_version(
            conn,
            minimum_version=STATE_SCHEMA_MIN_VERSION,
        )
        payload = {
            "ok": True,
            "database": database,
            "schema": STATE_SCHEMA,
            "bootstrap": (
                "created_and_migrated"
                if migrated["bootstrap"]["applied"]
                else "already_current"
            ),
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
