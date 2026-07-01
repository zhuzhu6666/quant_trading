#!/usr/bin/env python3
"""Verify SQLite state.db and PostgreSQL state_v1 parity."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

import psycopg

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from backend.core.db import STATE_DB, state_pg_dsn  # noqa: E402
from backend.core.state_store import STATE_SCHEMA  # noqa: E402


KEY_TABLES = {
    "runtime_kv",
    "recovery_position_state",
    "decision_ledger",
    "decision_factor_snapshot",
    "ctrader_deals",
    "position_supervisor_trace",
    "trade_outcome_review",
    "autonomous_learning_sample",
    "learning_application_log",
    "learning_application_effect",
}


def quote_ident(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def sqlite_tables(conn: sqlite3.Connection) -> list[str]:
    return [
        str(row[0])
        for row in conn.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type='table' AND (name NOT LIKE 'sqlite_%' OR name='sqlite_sequence')
            ORDER BY name
            """
        ).fetchall()
    ]


def sqlite_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [str(row["name"]) for row in conn.execute(f"PRAGMA table_info({quote_ident(table)})").fetchall()]


def sqlite_pk_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    rows = conn.execute(f"PRAGMA table_info({quote_ident(table)})").fetchall()
    return [str(row["name"]) for row in sorted([row for row in rows if int(row["pk"] or 0)], key=lambda r: int(r["pk"]))]


def pg_tables(conn, schema: str) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = %s
            """,
            (schema,),
        ).fetchall()
    }


def pg_columns(conn, schema: str, table: str) -> list[str]:
    return [
        str(row[0])
        for row in conn.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = %s AND table_name = %s
            ORDER BY ordinal_position
            """,
            (schema, table),
        ).fetchall()
    ]


def row_digest(rows) -> str:
    h = hashlib.sha256()
    for row in rows:
        payload = json.dumps(list(row), ensure_ascii=False, default=str, separators=(",", ":"))
        h.update(payload.encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest()


def table_digest_sqlite(conn: sqlite3.Connection, table: str, columns: list[str], order_cols: list[str]) -> str:
    order = ", ".join(quote_ident(col) for col in (order_cols or columns))
    select = ", ".join(quote_ident(col) for col in columns)
    rows = conn.execute(f"SELECT {select} FROM {quote_ident(table)} ORDER BY {order}").fetchall()
    return row_digest(sorted([tuple(row) for row in rows], key=lambda row: json.dumps(row, ensure_ascii=False, default=str)))


def table_digest_pg(conn, schema: str, table: str, columns: list[str], order_cols: list[str]) -> str:
    order = ", ".join(quote_ident(col) for col in (order_cols or columns))
    select = ", ".join(quote_ident(col) for col in columns)
    rows = conn.execute(f"SELECT {select} FROM {quote_ident(schema)}.{quote_ident(table)} ORDER BY {order}").fetchall()
    return row_digest(sorted([tuple(row) for row in rows], key=lambda row: json.dumps(row, ensure_ascii=False, default=str)))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sqlite-db", default=str(STATE_DB))
    parser.add_argument("--pg-dsn", default="")
    parser.add_argument("--schema", default=STATE_SCHEMA)
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args()

    dsn = args.pg_dsn or state_pg_dsn()
    if not dsn:
        raise SystemExit("PostgreSQL DSN missing")
    sqlite_conn = sqlite3.connect(f"file:{Path(args.sqlite_db)}?mode=ro", uri=True)
    sqlite_conn.row_factory = sqlite3.Row
    failures: list[str] = []
    try:
        tables = sqlite_tables(sqlite_conn)
        with psycopg.connect(dsn) as pg_conn:
            existing_pg_tables = pg_tables(pg_conn, args.schema)
            for table in tables:
                if table not in existing_pg_tables:
                    failures.append(f"{table}: missing in PostgreSQL")
                    continue
                s_cols = sqlite_columns(sqlite_conn, table)
                p_cols = pg_columns(pg_conn, args.schema, table)
                if s_cols != p_cols:
                    failures.append(f"{table}: column mismatch sqlite={s_cols} pg={p_cols}")
                    continue
                s_count = sqlite_conn.execute(f"SELECT COUNT(*) FROM {quote_ident(table)}").fetchone()[0]
                p_count = pg_conn.execute(f"SELECT COUNT(*) FROM {quote_ident(args.schema)}.{quote_ident(table)}").fetchone()[0]
                if int(s_count) != int(p_count):
                    failures.append(f"{table}: count mismatch sqlite={s_count} pg={p_count}")
                    continue
                pk_cols = sqlite_pk_columns(sqlite_conn, table)
                if pk_cols:
                    pk_expr = " || '|' || ".join(f"COALESCE(CAST({quote_ident(col)} AS TEXT), '')" for col in pk_cols)
                    s_pk = sqlite_conn.execute(
                        f"SELECT COUNT(*), COUNT(DISTINCT {pk_expr}) FROM {quote_ident(table)}"
                    ).fetchone()
                    p_pk = pg_conn.execute(
                        f"SELECT COUNT(*), COUNT(DISTINCT {pk_expr}) FROM {quote_ident(args.schema)}.{quote_ident(table)}"
                    ).fetchone()
                    if tuple(s_pk) != tuple(p_pk):
                        failures.append(f"{table}: pk distinct mismatch sqlite={tuple(s_pk)} pg={tuple(p_pk)}")
                        continue
                should_digest = args.strict or table in KEY_TABLES or int(s_count) <= 10000
                if should_digest:
                    s_digest = table_digest_sqlite(sqlite_conn, table, s_cols, pk_cols)
                    p_digest = table_digest_pg(pg_conn, args.schema, table, s_cols, pk_cols)
                    if s_digest != p_digest:
                        failures.append(f"{table}: digest mismatch sqlite={s_digest} pg={p_digest}")
                        continue
                print(f"OK\t{table}\t{s_count}")
    finally:
        sqlite_conn.close()
    if failures:
        print("FAILURES:")
        for failure in failures:
            print(f"- {failure}")
        return 1
    print("PARITY_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
