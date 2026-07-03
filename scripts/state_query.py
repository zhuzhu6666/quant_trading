#!/usr/bin/env python3
"""Read-only query helper for the runtime PostgreSQL state store.

Use this for operational state checks instead of opening data/state.db. The
SQLite state file has been removed; PostgreSQL state_v1 is the only runtime
state source.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from backend.core.db import get_state_pg_conn, state_backend  # noqa: E402


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    return str(value)


def _read_sql(args: argparse.Namespace) -> str:
    if args.sql:
        return str(args.sql)
    if args.file:
        return Path(args.file).read_text(encoding="utf-8")
    if not sys.stdin.isatty():
        return sys.stdin.read()
    raise SystemExit("provide --sql, --file, or pipe SQL on stdin")


def _validate_read_only(sql: str) -> str:
    stripped = sql.strip().rstrip(";").strip()
    if not stripped:
        raise SystemExit("empty SQL")
    lowered = stripped.lower()
    if ";" in stripped:
        raise SystemExit("multiple SQL statements are not allowed")
    allowed_prefixes = ("select ", "with ", "explain ")
    if not lowered.startswith(allowed_prefixes):
        raise SystemExit("only read-only SELECT/WITH/EXPLAIN SQL is allowed")
    blocked_tokens = (
        " insert ",
        " update ",
        " delete ",
        " upsert ",
        " merge ",
        " create ",
        " alter ",
        " drop ",
        " truncate ",
        " grant ",
        " revoke ",
        " copy ",
        " vacuum ",
        " call ",
    )
    padded = f" {lowered} "
    if any(token in padded for token in blocked_tokens):
        raise SystemExit("mutation/DDL tokens are not allowed in state_query.py")
    return stripped


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run a read-only SQL query against PostgreSQL state_v1."
    )
    parser.add_argument("--sql", help="SQL to execute. Only SELECT/WITH/EXPLAIN is allowed.")
    parser.add_argument("--file", help="Read SQL from a file.")
    parser.add_argument("--limit", type=int, default=0, help="Client-side row limit for output.")
    parser.add_argument("--compact", action="store_true", help="Emit compact JSON.")
    args = parser.parse_args()

    if state_backend() != "postgres":
        raise SystemExit(f"runtime state backend is not PostgreSQL: {state_backend()}")

    sql = _validate_read_only(_read_sql(args))
    conn = get_state_pg_conn(read_only=True)
    try:
        rows = [dict(row) for row in conn.execute(sql).fetchall()]
    finally:
        conn.rollback()
        conn.close()

    if args.limit > 0:
        rows = rows[: args.limit]
    if args.compact:
        print(json.dumps(rows, ensure_ascii=False, default=_json_default, separators=(",", ":")))
    else:
        print(json.dumps(rows, ensure_ascii=False, default=_json_default, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
