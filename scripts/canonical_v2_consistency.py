#!/usr/bin/env python3
"""Read-only canonical_v2 schema and reference consistency check."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from backend.core.db import get_state_pg_conn  # noqa: E402
from backend.services.canonical_v2 import read_payload  # noqa: E402


REPORT_SCHEMA = "canonical_v2_consistency.v1"
REQUIRED_TABLES = (
    "payload_blob",
    "event",
    "event_relation",
    "state_version",
    "training_sample",
    "dataset_manifest",
    "dataset_manifest_member",
    "projection_run",
    "legacy_mapping",
)


def _row_value(row: Any, key: str, index: int = 0, default: Any = None) -> Any:
    if row is None:
        return default
    if isinstance(row, dict):
        return row.get(key, default)
    try:
        return row[index]
    except (IndexError, KeyError, TypeError):
        return default


def _count(conn: Any, table: str) -> int:
    row = conn.execute(f"SELECT COUNT(*) AS row_count FROM canonical_v2.{table}").fetchone()
    return int(_row_value(row, "row_count", 0, 0) or 0)


def _reference_checks(conn: Any) -> dict[str, int]:
    checks = {
        "event_payload_orphans": """
            SELECT COUNT(*) FROM canonical_v2.event e
            LEFT JOIN canonical_v2.payload_blob p ON p.payload_hash=e.payload_hash
            WHERE p.payload_hash IS NULL
        """,
        "relation_from_orphans": """
            SELECT COUNT(*) FROM canonical_v2.event_relation r
            LEFT JOIN canonical_v2.event e ON e.event_id=r.from_event_id
            WHERE e.event_id IS NULL
        """,
        "relation_to_orphans": """
            SELECT COUNT(*) FROM canonical_v2.event_relation r
            LEFT JOIN canonical_v2.event e ON e.event_id=r.to_event_id
            WHERE e.event_id IS NULL
        """,
        "state_source_orphans": """
            SELECT COUNT(*) FROM canonical_v2.state_version s
            LEFT JOIN canonical_v2.event e ON e.event_id=s.source_event_id
            WHERE e.event_id IS NULL
        """,
        "state_payload_orphans": """
            SELECT COUNT(*) FROM canonical_v2.state_version s
            LEFT JOIN canonical_v2.payload_blob p ON p.payload_hash=s.payload_hash
            WHERE p.payload_hash IS NULL
        """,
        "sample_source_orphans": """
            SELECT COUNT(*) FROM canonical_v2.training_sample s
            WHERE EXISTS (
                SELECT 1
                FROM unnest(s.source_event_ids) AS source_event_id
                LEFT JOIN canonical_v2.event e ON e.event_id=source_event_id
                WHERE e.event_id IS NULL
            )
        """,
        "dataset_member_manifest_orphans": """
            SELECT COUNT(*) FROM canonical_v2.dataset_manifest_member m
            LEFT JOIN canonical_v2.dataset_manifest d ON d.dataset_id=m.dataset_id
            WHERE d.dataset_id IS NULL
        """,
        "dataset_member_sample_orphans": """
            SELECT COUNT(*) FROM canonical_v2.dataset_manifest_member m
            LEFT JOIN canonical_v2.training_sample s ON s.sample_id=m.sample_id
            WHERE s.sample_id IS NULL
        """,
        "legacy_event_orphans": """
            SELECT COUNT(*) FROM canonical_v2.legacy_mapping m
            LEFT JOIN canonical_v2.event e ON e.event_id=m.canonical_event_id
            WHERE m.canonical_event_id IS NOT NULL AND e.event_id IS NULL
        """,
        "legacy_payload_orphans": """
            SELECT COUNT(*) FROM canonical_v2.legacy_mapping m
            LEFT JOIN canonical_v2.payload_blob p ON p.payload_hash=m.canonical_payload_hash
            WHERE m.canonical_payload_hash IS NOT NULL AND p.payload_hash IS NULL
        """,
        "legacy_run_orphans": """
            SELECT COUNT(*) FROM canonical_v2.legacy_mapping m
            LEFT JOIN canonical_v2.projection_run r ON r.projection_run_id=m.migration_run_id
            WHERE r.projection_run_id IS NULL
        """,
    }
    result: dict[str, int] = {}
    for name, sql in checks.items():
        row = conn.execute(sql).fetchone()
        result[name] = int(_row_value(row, "count", 0, 0) or 0)
    return result


def _verify_payloads(conn: Any, *, max_payloads: int) -> dict[str, Any]:
    total = _count(conn, "payload_blob")
    checked = 0
    failures: Counter[str] = Counter()
    last_hash = ""
    while checked < total and checked < max_payloads:
        batch_limit = min(128, max_payloads - checked)
        cursor = conn.cursor(name=f"canonical_v2_payload_verify_{checked}")
        try:
            cursor.execute(
                """
                SELECT payload_hash
                FROM canonical_v2.payload_blob
                WHERE payload_hash > %s
                ORDER BY payload_hash ASC
                LIMIT %s
                """,
                (last_hash, batch_limit),
            )
            rows = cursor.fetchmany(batch_limit)
        finally:
            cursor.close()
        if not rows:
            break
        for row in rows:
            payload_hash = str(_row_value(row, "payload_hash", 0, "") or "")
            last_hash = payload_hash
            try:
                read_payload(conn, payload_hash)
            except Exception as exc:  # retain the first failure per class only
                failures[type(exc).__name__] += 1
            checked += 1
    return {
        "total": total,
        "checked": checked,
        "complete": checked >= total,
        "failures": dict(sorted(failures.items())),
    }


def inspect_consistency(*, max_payloads: int = 1000) -> dict[str, Any]:
    if int(max_payloads) <= 0:
        raise ValueError("max_payloads must be positive")
    conn = get_state_pg_conn(read_only=True)
    try:
        table_rows = conn.execute(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema='canonical_v2'
            """
        ).fetchall()
        tables = {str(_row_value(row, "table_name", 0, "") or "") for row in table_rows}
        missing = [table for table in REQUIRED_TABLES if table not in tables]
        if missing:
            return {
                "schema_version": REPORT_SCHEMA,
                "ok": False,
                "read_only": True,
                "writes_performed": False,
                "missing_tables": missing,
            }
        counts = {table: _count(conn, table) for table in REQUIRED_TABLES}
        references = _reference_checks(conn)
        payloads = _verify_payloads(conn, max_payloads=int(max_payloads))
        ok = not any(references.values()) and not payloads["failures"] and payloads["complete"]
        return {
            "schema_version": REPORT_SCHEMA,
            "ok": ok,
            "read_only": True,
            "writes_performed": False,
            "missing_tables": [],
            "row_counts": counts,
            "reference_orphans": references,
            "payload_integrity": payloads,
            "max_payloads": int(max_payloads),
        }
    finally:
        conn.rollback()
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only canonical_v2 consistency check")
    parser.add_argument("--max-payloads", type=int, default=1000)
    args = parser.parse_args()
    report = inspect_consistency(max_payloads=args.max_payloads)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, default=str))
    return 0 if report.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
