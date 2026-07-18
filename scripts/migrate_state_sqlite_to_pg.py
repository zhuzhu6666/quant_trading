#!/usr/bin/env python3
"""Restore data from an external SQLite backup into an already-migrated schema.

This command is a data importer, not a schema bootstrapper.  Operators must
apply ``scripts/state_schema_migrate.py --apply`` first.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

import psycopg

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from backend.core.db import state_pg_dsn  # noqa: E402
from backend.core.state_store import STATE_SCHEMA  # noqa: E402


def quote_ident(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def sqlite_tables(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        """
        SELECT name
        FROM sqlite_master
        WHERE type='table' AND (name NOT LIKE 'sqlite_%' OR name='sqlite_sequence')
        ORDER BY name
        """
    ).fetchall()
    return [str(row[0]) for row in rows]


def table_info(conn: sqlite3.Connection, table: str) -> list[sqlite3.Row]:
    return list(conn.execute(f"PRAGMA table_info({quote_ident(table)})").fetchall())


def table_indexes(conn: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
    indexes: list[dict[str, Any]] = []
    for row in conn.execute(f"PRAGMA index_list({quote_ident(table)})").fetchall():
        name = str(row["name"])
        if name.startswith("sqlite_autoindex_"):
            continue
        cols = [str(info["name"]) for info in conn.execute(f"PRAGMA index_info({quote_ident(name)})").fetchall()]
        if not cols:
            continue
        indexes.append({"name": name, "unique": bool(row["unique"]), "columns": cols})
    return indexes


def csv_value(value: Any) -> Any:
    if isinstance(value, bytes):
        return value.hex()
    return value


def copy_table(sqlite_conn: sqlite3.Connection, pg_conn, table: str, schema: str, batch_size: int) -> int:
    cols = table_info(sqlite_conn, table)
    names = [str(row["name"]) for row in cols]
    if not names:
        return 0
    count = 0
    quoted_cols = ", ".join(quote_ident(name) for name in names)
    copy_sql = f"COPY {quote_ident(schema)}.{quote_ident(table)} ({quoted_cols}) FROM STDIN WITH (FORMAT CSV, NULL '\\N')"
    cursor = sqlite_conn.execute(f"SELECT {quoted_cols} FROM {quote_ident(table)}")
    while True:
        rows = cursor.fetchmany(batch_size)
        if not rows:
            break
        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator="\n")
        for row in rows:
            writer.writerow(["\\N" if value is None else csv_value(value) for value in row])
        buf.seek(0)
        with pg_conn.cursor().copy(copy_sql) as copy:
            copy.write(buf.read())
        count += len(rows)
    return count


def validate_target_schema(
    sqlite_conn: sqlite3.Connection,
    pg_conn,
    tables: list[str],
    schema: str,
) -> None:
    """Fail before data changes when the explicit migration is incomplete."""

    target_tables = {
        str(row[0])
        for row in pg_conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema=%s",
            (schema,),
        ).fetchall()
    }
    missing_tables = sorted(set(tables) - target_tables)
    missing_columns: dict[str, list[str]] = {}
    for table in sorted(set(tables) & target_tables):
        source_columns = {str(row["name"]) for row in table_info(sqlite_conn, table)}
        target_columns = {
            str(row[0])
            for row in pg_conn.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema=%s AND table_name=%s
                """,
                (schema, table),
            ).fetchall()
        }
        missing = sorted(source_columns - target_columns)
        if missing:
            missing_columns[table] = missing
    if missing_tables or missing_columns:
        raise RuntimeError(
            "target PostgreSQL schema is not migration-ready; run "
            "scripts/state_schema_migrate.py --apply first; "
            f"missing_tables={missing_tables}; missing_columns={missing_columns}"
        )


def sync_identity_sequences(pg_conn, schema: str) -> None:
    rows = pg_conn.execute(
        """
        SELECT table_name, column_name
        FROM information_schema.columns
        WHERE table_schema = %s AND identity_generation IS NOT NULL
        ORDER BY table_name, column_name
        """,
        (schema,),
    ).fetchall()
    for table, column in rows:
        seq = pg_conn.execute(
            "SELECT pg_get_serial_sequence(%s, %s)",
            (f"{schema}.{table}", column),
        ).fetchone()[0]
        if not seq:
            continue
        max_id = int(pg_conn.execute(
            f"SELECT COALESCE(MAX({quote_ident(column)}), 0) FROM {quote_ident(schema)}.{quote_ident(table)}"
        ).fetchone()[0] or 0)
        if max_id <= 0:
            pg_conn.execute("SELECT setval(%s, 1, false)", (seq,))
        else:
            pg_conn.execute("SELECT setval(%s, %s, true)", (seq, max_id))


def snapshot_report(sqlite_conn: sqlite3.Connection, tables: list[str], schema: str, backup_dir: Path) -> None:
    report: dict[str, Any] = {"schema": schema, "created_at": time.time(), "tables": {}}
    for table in tables:
        cols = table_info(sqlite_conn, table)
        report["tables"][table] = {
            "count": sqlite_conn.execute(f"SELECT COUNT(*) FROM {quote_ident(table)}").fetchone()[0],
            "columns": [dict(row) for row in cols],
            "indexes": table_indexes(sqlite_conn, table),
        }
    backup_dir.mkdir(parents=True, exist_ok=True)
    (backup_dir / "sqlite_state_manifest.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sqlite-db", default="", help="Explicit external SQLite backup path for one-off migration")
    parser.add_argument("--pg-dsn", default="")
    parser.add_argument("--schema", default=STATE_SCHEMA)
    parser.add_argument("--backup-dir", default="")
    parser.add_argument("--batch-size", type=int, default=5000)
    args = parser.parse_args()

    if not args.sqlite_db:
        raise SystemExit("SQLite cold backup is not retained locally; pass --sqlite-db explicitly if restoring from an external backup")
    sqlite_path = Path(args.sqlite_db)
    if not sqlite_path.exists():
        raise SystemExit(f"SQLite db not found: {sqlite_path}")
    if not os.access(sqlite_path, os.R_OK):
        raise SystemExit(f"SQLite cold backup is not readable: {sqlite_path}; restore read permission only for migration")
    dsn = args.pg_dsn or state_pg_dsn()
    if not dsn:
        raise SystemExit("PostgreSQL DSN missing; pass --pg-dsn or configure QUANT_STATE_PG_DSN")

    sqlite_conn = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    sqlite_conn.row_factory = sqlite3.Row
    try:
        integrity = sqlite_conn.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise SystemExit(f"SQLite integrity_check failed: {integrity}")
        tables = sqlite_tables(sqlite_conn)
        if not tables:
            raise SystemExit("No SQLite tables found")
        if args.backup_dir:
            snapshot_report(sqlite_conn, tables, args.schema, Path(args.backup_dir))
        with psycopg.connect(dsn, autocommit=False) as pg_conn:
            validate_target_schema(sqlite_conn, pg_conn, tables, args.schema)
            for table in tables:
                pg_conn.execute(f"TRUNCATE TABLE {quote_ident(args.schema)}.{quote_ident(table)}")
                copied = copy_table(sqlite_conn, pg_conn, table, args.schema, args.batch_size)
                pg_conn.commit()
                print(f"{table}\t{copied}")
            sync_identity_sequences(pg_conn, args.schema)
            pg_conn.commit()
    finally:
        sqlite_conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
