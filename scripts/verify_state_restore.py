#!/usr/bin/env python3
"""Verify an isolated pgBackRest restore without starting or promoting it.

This script never invokes ``pgbackrest restore``.  It only connects to a DSN
that the operator has restored separately, compares it with the pre-backup
manifest, and fails closed if that DSN is textually the configured production
DSN.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.core.db import STATE_SCHEMA, state_pg_dsn
from backend.core.state_schema_migrations import STATE_SCHEMA_MIN_VERSION, state_schema_status
from backend.core.state_store import connect_state_store
from backend.services.memory_integrity import MemoryIntegrityReportService
from backend.services.postgres_backup_health import PostgresBackupHealthService


TABLES = ("trade_outcome_review", "experience_memory", "brain_memory")


def _connection_factory(dsn: str):
    return lambda *, read_only=True: connect_state_store(
        dsn,
        read_only=read_only,
        schema=STATE_SCHEMA,
    )


def inspect_state(dsn: str) -> dict[str, Any]:
    conn = _connection_factory(dsn)(read_only=True)
    try:
        schema = state_schema_status(conn, minimum_version=STATE_SCHEMA_MIN_VERSION)
        counts = {
            table: int(conn.execute(f'SELECT COUNT(*) AS n FROM "{table}"').fetchone()["n"] or 0)
            for table in TABLES
        }
    finally:
        conn.close()
    integrity = MemoryIntegrityReportService(
        connection_factory=_connection_factory(dsn)
    ).build()
    return {"state_schema": schema, "table_counts": counts, "memory_integrity": integrity}


def verify_manifest(expected: dict[str, Any], actual: dict[str, Any]) -> dict[str, Any]:
    expected_counts = dict(expected.get("table_counts") or {})
    actual_counts = dict(actual.get("table_counts") or {})
    count_mismatches = {
        table: {"expected": expected_counts.get(table), "actual": actual_counts.get(table)}
        for table in TABLES
        if expected_counts.get(table) != actual_counts.get(table)
    }
    expected_schema = dict(expected.get("state_schema") or {})
    actual_schema = dict(actual.get("state_schema") or {})
    schema_mismatch = (
        not actual_schema.get("ok")
        or int(actual_schema.get("current_version") or 0)
        < int(expected_schema.get("current_version") or 0)
    )
    integrity = dict(actual.get("memory_integrity") or {})
    integrity_status = str(integrity.get("status") or "unavailable")
    ok = not count_mismatches and not schema_mismatch and integrity_status == "healthy"
    return {
        "ok": ok,
        "schema_version": "state_restore_verification.v1",
        "state_schema": {
            "expected_current_version": expected_schema.get("current_version"),
            "actual_current_version": actual_schema.get("current_version"),
            "status": "matched" if not schema_mismatch else "mismatch",
        },
        "table_counts": {
            "status": "matched" if not count_mismatches else "mismatch",
            "mismatches": count_mismatches,
        },
        "memory_integrity": integrity,
        "requires_manual_promotion": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--restored-dsn", required=True, help="Isolated restored database DSN; never printed")
    parser.add_argument("--expected-manifest", required=True)
    parser.add_argument("--confirm-isolated", action="store_true")
    parser.add_argument(
        "--publish-production-health",
        action="store_true",
        help="Record only this completed drill result in the configured production health projection",
    )
    args = parser.parse_args()
    if not args.confirm_isolated:
        raise SystemExit("refusing verification without --confirm-isolated")
    production_dsn = state_pg_dsn()
    if production_dsn and args.restored_dsn.strip() == production_dsn.strip():
        raise SystemExit("restored DSN matches configured production DSN")
    expected = json.loads(Path(args.expected_manifest).read_text(encoding="utf-8"))
    if str(expected.get("schema_version") or "") != "state_backup_manifest.v1":
        raise SystemExit("expected manifest schema is unsupported")
    report = verify_manifest(expected, inspect_state(args.restored_dsn))
    if args.publish_production_health:
        report["production_health_projection"] = PostgresBackupHealthService().record_restore_drill(report)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
