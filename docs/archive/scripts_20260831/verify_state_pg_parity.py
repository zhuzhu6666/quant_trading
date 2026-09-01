#!/usr/bin/env python3
"""Read-only integrity check for the PostgreSQL runtime and canonical stores.

The old SQLite-versus-state-table comparison is retired.  ``runtime`` owns
mutable operational state, while ``canonical_v2`` owns immutable events,
relations, payloads, and learning facts.  They are different authority
domains, so this command checks their catalogs and invariants instead of
pretending that one is a row-for-row mirror of the other.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from backend.core.db import get_state_pg_conn  # noqa: E402
from backend.core.state_store import STATE_SCHEMA  # noqa: E402


CANONICAL_SCHEMA = "canonical_v2"
BASE_RUNTIME_TABLES = (
    "runtime_kv",
    "state_schema_migration",
)
STRICT_RUNTIME_TABLES = (
    "recovery_position_state",
    "ctrader_deals",
)
CANONICAL_TABLES = (
    "payload_blob",
    "event",
    "event_relation",
    "state_version",
    "training_sample",
    "dataset_manifest",
    "dataset_manifest_member",
    "projection_run",
    "training_sample_row",
)


def _quote_ident(value: str) -> str:
    return '"' + str(value).replace('"', '""') + '"'


def _schema_tables(conn: Any, schema: str) -> set[str]:
    rows = conn.execute(
        """
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema = %s
        ORDER BY table_name
        """,
        (schema,),
    ).fetchall()
    return {str(row["table_name"] if hasattr(row, "keys") else row[0]) for row in rows}


def _count(conn: Any, schema: str, table: str) -> int:
    row = conn.execute(
        f"SELECT count(*) AS n FROM {_quote_ident(schema)}.{_quote_ident(table)}"
    ).fetchone()
    return int(row["n"] if hasattr(row, "keys") else row[0])


def _application_schemas(conn: Any) -> list[str]:
    rows = conn.execute(
        """
        SELECT nspname
        FROM pg_namespace
        WHERE nspname NOT LIKE 'pg_%%'
          AND nspname <> 'information_schema'
          AND nspname NOT IN ('public', %s, %s)
        ORDER BY nspname
        """,
        (STATE_SCHEMA, CANONICAL_SCHEMA),
    ).fetchall()
    return [str(row["nspname"] if hasattr(row, "keys") else row[0]) for row in rows]


def _canonical_integrity(conn: Any, *, since_epoch: float | None) -> dict[str, Any]:
    event_where = ""
    event_params: tuple[Any, ...] = ()
    if since_epoch is not None:
        event_where = " WHERE observed_at > to_timestamp(%s)"
        event_params = (float(since_epoch),)

    event_rows = conn.execute(
        """
        SELECT event_type, count(*) AS n
        FROM canonical_v2.event
        """ + event_where + " GROUP BY event_type ORDER BY event_type",
        event_params,
    ).fetchall()
    event_types = {
        str(row["event_type"] if hasattr(row, "keys") else row[0]): int(
            row["n"] if hasattr(row, "keys") else row[1]
        )
        for row in event_rows
    }
    payload_orphans = conn.execute(
        """
        SELECT count(*) AS n
        FROM canonical_v2.event AS e
        LEFT JOIN canonical_v2.payload_blob AS p ON p.payload_hash=e.payload_hash
        WHERE p.payload_hash IS NULL
        """
    ).fetchone()
    relation_orphans = conn.execute(
        """
        SELECT count(*) AS n
        FROM canonical_v2.event_relation AS r
        LEFT JOIN canonical_v2.event AS f ON f.event_id=r.from_event_id
        LEFT JOIN canonical_v2.event AS t ON t.event_id=r.to_event_id
        WHERE f.event_id IS NULL OR t.event_id IS NULL
        """
    ).fetchone()

    def _value(row: Any) -> int:
        return int(row["n"] if hasattr(row, "keys") else row[0])

    return {
        "event_types": event_types,
        "event_count": sum(event_types.values()),
        "payload_count": _count(conn, CANONICAL_SCHEMA, "payload_blob"),
        "training_sample_row_count": _count(conn, CANONICAL_SCHEMA, "training_sample_row"),
        "payload_orphans": _value(payload_orphans),
        "relation_orphans": _value(relation_orphans),
    }


def build_report(
    conn: Any,
    *,
    since_epoch: float | None = None,
) -> dict[str, Any]:
    """Build a runtime/canonical integrity report on an existing connection."""

    runtime_tables = _schema_tables(conn, STATE_SCHEMA)
    canonical_tables = _schema_tables(conn, CANONICAL_SCHEMA)
    required_runtime = BASE_RUNTIME_TABLES + STRICT_RUNTIME_TABLES
    missing_runtime = sorted(set(required_runtime) - runtime_tables)
    missing_canonical = sorted(set(CANONICAL_TABLES) - canonical_tables)
    application_schemas = _application_schemas(conn)

    failures: list[str] = []
    if application_schemas:
        failures.append(f"unexpected_application_schemas={application_schemas}")
    if missing_runtime:
        failures.append(f"missing_runtime_tables={missing_runtime}")
    if missing_canonical:
        failures.append(f"missing_canonical_tables={missing_canonical}")

    canonical: dict[str, Any] = {}
    if not missing_canonical:
        canonical = _canonical_integrity(conn, since_epoch=since_epoch)
        if canonical["payload_orphans"]:
            failures.append(f"canonical_payload_orphans={canonical['payload_orphans']}")
        if canonical["relation_orphans"]:
            failures.append(f"canonical_relation_orphans={canonical['relation_orphans']}")

    migration: dict[str, Any] = {}
    if "state_schema_migration" in runtime_tables:
        row = conn.execute(
            "SELECT max(version) AS version, count(*) AS entries "
            "FROM runtime.state_schema_migration"
        ).fetchone()
        migration = {
            "current_version": int(row["version"] or 0),
            "entries": int(row["entries"] or 0),
        }

    return {
        "schema_version": "runtime_canonical_fact_check.v1",
        "ok": not failures,
        "authority": {
            "runtime_schema": STATE_SCHEMA,
            "canonical_schema": CANONICAL_SCHEMA,
            "row_parity_checked": False,
        },
        "since_epoch": since_epoch,
        "runtime": {
            "table_count": len(runtime_tables),
            "required_tables": list(required_runtime),
            "missing_tables": missing_runtime,
            "migration": migration,
        },
        "canonical": {
            "table_count": len(canonical_tables),
            "required_tables": list(CANONICAL_TABLES),
            "missing_tables": missing_canonical,
            **canonical,
        },
        "unexpected_application_schemas": application_schemas,
        "failures": failures,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--since-epoch",
        type=float,
        default=None,
        help="Limit canonical event counts to observed_at after this epoch.",
    )
    args = parser.parse_args(argv)

    conn = get_state_pg_conn(read_only=True)
    try:
        report = build_report(
            conn,
            since_epoch=args.since_epoch,
        )
    finally:
        conn.rollback()
        conn.close()

    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
