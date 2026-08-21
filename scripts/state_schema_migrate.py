#!/usr/bin/env python3
"""Check or explicitly apply versioned PostgreSQL ``runtime`` migrations."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.core.db import get_state_pg_conn, state_pg_dsn
from backend.core.state_store import connect_state_migration_store
from backend.core.state_schema_migrations import (
    STATE_SCHEMA_MIN_VERSION,
    StateSchemaError,
    run_state_schema_migrations,
    state_schema_status,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check or apply additive migrations for the PostgreSQL runtime schema."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--check",
        action="store_true",
        help="Read-only minimum-version check (the default).",
    )
    mode.add_argument(
        "--apply",
        action="store_true",
        help="Explicitly apply pending additive migrations.",
    )
    parser.add_argument(
        "--runner-id",
        default="",
        help="Optional non-secret audit label for the migration ledger.",
    )
    args = parser.parse_args(argv)

    conn = None
    try:
        conn = (
            connect_state_migration_store(state_pg_dsn())
            if args.apply
            else get_state_pg_conn(read_only=True)
        )
        if not args.apply:
            payload = state_schema_status(conn, minimum_version=STATE_SCHEMA_MIN_VERSION)
            print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
            return 0 if payload["ok"] else 2

        result = run_state_schema_migrations(conn, runner_id=args.runner_id)
        result["status"] = state_schema_status(
            conn,
            minimum_version=STATE_SCHEMA_MIN_VERSION,
        )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except StateSchemaError as exc:
        payload = {
            "ok": False,
            "error_type": type(exc).__name__,
            "error": str(exc),
        }
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return 2
    finally:
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
