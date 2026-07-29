#!/usr/bin/env python3
"""Capture a compact, non-secret state_v1 manifest before a pgBackRest backup.

The manifest is an operator artifact.  Store it beside the encrypted backup
in the approved object store; do not commit it or credentials to Git.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
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


def capture_manifest(dsn: str) -> dict[str, Any]:
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
    return {
        "schema_version": "state_backup_manifest.v1",
        "generated_at": time.time(),
        "state_schema": {
            "current_version": schema.get("current_version"),
            "minimum_version": schema.get("minimum_version"),
            "latest_known_version": schema.get("latest_known_version"),
            "ok": bool(schema.get("ok")),
        },
        "table_counts": counts,
        "memory_integrity": integrity,
    }


def _write_exclusive(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as handle:
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.write("\n")
        temporary = Path(handle.name)
    try:
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, help="Operator-controlled manifest destination")
    parser.add_argument("--state-dsn", default=state_pg_dsn(), help="Source state_v1 DSN; never printed")
    args = parser.parse_args()
    if not args.state_dsn:
        raise SystemExit("state PostgreSQL DSN is required")
    manifest = capture_manifest(args.state_dsn)
    _write_exclusive(Path(args.output), manifest)
    print(json.dumps({"ok": True, "output": str(args.output), "generated_at": manifest["generated_at"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
