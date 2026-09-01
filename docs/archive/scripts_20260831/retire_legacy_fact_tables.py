#!/usr/bin/env python3
"""Explicitly clear retired fact projections before the canonical-v2 cutover.

This is a one-time operator command, not a runtime compatibility path.  It
has a fixed allow-list matching migration 0030, is read-only by default, and
requires both ``--apply`` and ``--confirm-retired-facts`` before it can remove
anything.  Migration 0030 drops the now-empty tables afterwards.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path
from typing import Any

from psycopg import sql

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.core.db import get_state_pg_conn, state_pg_dsn  # noqa: E402
from backend.core.state_store import connect_state_migration_store  # noqa: E402


RETIRED_FACT_TABLES: tuple[tuple[str, str], ...] = (
    ("runtime", "decision_factor_snapshot"),
    ("runtime", "decision_ledger"),
    ("runtime", "autonomous_learning_sample"),
    ("runtime", "order_lifecycle_event"),
    ("runtime", "position_lifecycle_event"),
    ("runtime", "trade_outcome_review"),
    ("runtime", "position_supervisor_trace"),
    ("runtime", "supervisor_counterfactual_review"),
    ("runtime", "supervisor_counterfactual_history"),
    ("runtime", "state_payload_archive"),
    ("runtime", "decision_log"),
    ("runtime", "lifecycle_events"),
    ("canonical_v2", "legacy_mapping"),
)


def _qualified(schema: str, table: str) -> sql.Composed:
    return sql.SQL("{}.{}").format(sql.Identifier(schema), sql.Identifier(table))


def _existing_tables(conn: Any) -> set[tuple[str, str]]:
    rows = conn.execute(
        """
        SELECT table_schema, table_name
        FROM information_schema.tables
        WHERE table_type = 'BASE TABLE'
          AND table_schema = ANY(%s)
          AND table_name = ANY(%s)
        """,
        ([schema for schema, _ in RETIRED_FACT_TABLES], [table for _, table in RETIRED_FACT_TABLES]),
    ).fetchall()
    allowed = set(RETIRED_FACT_TABLES)
    return {
        (str(row["table_schema"]), str(row["table_name"]))
        for row in rows
        if (str(row["table_schema"]), str(row["table_name"])) in allowed
    }


def _snapshot(conn: Any) -> dict[str, dict[str, Any]]:
    existing = _existing_tables(conn)
    snapshot: dict[str, dict[str, Any]] = {}
    for schema, table in RETIRED_FACT_TABLES:
        key = f"{schema}.{table}"
        if (schema, table) not in existing:
            snapshot[key] = {"exists": False, "row_count": None}
            continue
        row = conn.execute(
            sql.SQL("SELECT count(*) AS row_count FROM {}")
            .format(_qualified(schema, table))
        ).fetchone()
        snapshot[key] = {"exists": True, "row_count": int(row["row_count"])}
    return snapshot


def _lock_existing_tables(conn: Any, existing: set[tuple[str, str]]) -> None:
    for schema, table in RETIRED_FACT_TABLES:
        if (schema, table) not in existing:
            continue
        conn.execute(
            sql.SQL("LOCK TABLE {} IN ACCESS EXCLUSIVE MODE")
            .format(_qualified(schema, table))
        )


def _report(
    *,
    run_id: str,
    mode: str,
    before: dict[str, dict[str, Any]],
    after: dict[str, dict[str, Any]] | None = None,
    deleted: dict[str, int] | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    nonempty = {
        key: value["row_count"]
        for key, value in before.items()
        if value["exists"] and int(value["row_count"] or 0) > 0
    }
    readiness_snapshot = after if after is not None else before
    payload: dict[str, Any] = {
        "schema_version": "retire_legacy_fact_tables.v1",
        "run_id": run_id,
        "mode": mode,
        "retired_fact_tables": [f"{schema}.{table}" for schema, table in RETIRED_FACT_TABLES],
        "before": before,
        "nonempty_before": nonempty,
        "ready_for_schema_migration": all(
            not value["exists"] or int(value["row_count"] or 0) == 0
            for value in readiness_snapshot.values()
        ),
    }
    if after is not None:
        payload["after"] = after
    if deleted is not None:
        payload["deleted"] = deleted
    if error is not None:
        payload["ok"] = False
        payload["error"] = error
    else:
        payload["ok"] = not nonempty if mode == "check" else all(
            not value["exists"] or int(value["row_count"] or 0) == 0
            for value in (after or {}).values()
        )
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Clear the fixed retired-table allow-list in one transaction.",
    )
    parser.add_argument(
        "--confirm-retired-facts",
        action="store_true",
        help="Required with --apply; confirms the retired rows may be discarded.",
    )
    args = parser.parse_args(argv)
    run_id = f"retire-legacy-{uuid.uuid4().hex}"

    if args.apply and not args.confirm_retired_facts:
        print(
            json.dumps(
                {
                    "schema_version": "retire_legacy_fact_tables.v1",
                    "run_id": run_id,
                    "mode": "apply",
                    "ok": False,
                    "error": "--apply requires --confirm-retired-facts",
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 2

    conn = None
    try:
        conn = (
            connect_state_migration_store(state_pg_dsn())
            if args.apply
            else get_state_pg_conn(read_only=True)
        )
        if not args.apply:
            report = _report(
                run_id=run_id,
                mode="check",
                before=_snapshot(conn),
            )
            print(json.dumps(report, ensure_ascii=False, sort_keys=True))
            return 0 if report["ok"] else 2

        conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            ("quant_trading.retire_legacy_fact_tables.v1",),
        )
        existing = _existing_tables(conn)
        _lock_existing_tables(conn, existing)
        before = _snapshot(conn)
        deleted: dict[str, int] = {}
        for schema, table in RETIRED_FACT_TABLES:
            key = f"{schema}.{table}"
            if (schema, table) not in existing:
                deleted[key] = 0
                continue
            cursor = conn.execute(
                sql.SQL("DELETE FROM {}")
                .format(_qualified(schema, table))
            )
            deleted[key] = int(cursor.rowcount)
        conn.commit()
        after = _snapshot(conn)
        report = _report(
            run_id=run_id,
            mode="apply",
            before=before,
            after=after,
            deleted=deleted,
        )
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 0 if report["ok"] else 2
    except Exception as exc:  # pragma: no cover - operational error path
        if conn is not None:
            conn.rollback()
        print(
            json.dumps(
                {
                    "schema_version": "retire_legacy_fact_tables.v1",
                    "run_id": run_id,
                    "mode": "apply" if args.apply else "check",
                    "ok": False,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 2
    finally:
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
