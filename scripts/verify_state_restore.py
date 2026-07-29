#!/usr/bin/env python3
"""Verify an isolated Windows-pull restore without starting or promoting it.

This script never restores data. It only connects to a separately restored
logical snapshot and fails closed if that DSN is textually the configured
production DSN.
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


def verify_restored_state(actual: dict[str, Any]) -> dict[str, Any]:
    """Fail closed on an invalid restored snapshot without inventing parity."""
    actual_schema = dict(actual.get("state_schema") or {})
    integrity = dict(actual.get("memory_integrity") or {})
    integrity_status = str(integrity.get("status") or "unavailable")
    schema_ok = bool(actual_schema.get("ok"))
    ok = schema_ok and integrity_status == "healthy"
    return {
        "ok": ok,
        "schema_version": "windows_pull_restore_verification.v1",
        "state_schema": {
            "actual_current_version": actual_schema.get("current_version"),
            "status": "healthy" if schema_ok else "invalid",
        },
        "table_counts": {
            "status": "observed_only",
            "values": actual.get("table_counts") or {},
        },
        "memory_integrity": integrity,
        "source_parity": "not_claimed_for_offline_logical_snapshot",
        "requires_manual_promotion": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--restored-dsn", required=True, help="Isolated restored database DSN; never printed")
    parser.add_argument("--confirm-isolated", action="store_true")
    args = parser.parse_args()
    if not args.confirm_isolated:
        raise SystemExit("refusing verification without --confirm-isolated")
    production_dsn = state_pg_dsn()
    if production_dsn and args.restored_dsn.strip() == production_dsn.strip():
        raise SystemExit("restored DSN matches configured production DSN")
    report = verify_restored_state(inspect_state(args.restored_dsn))
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
