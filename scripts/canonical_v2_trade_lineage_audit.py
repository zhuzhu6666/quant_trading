#!/usr/bin/env python3
"""Audit and explicitly repair canonical trade-review lineage.

The default mode is bounded and read-only.  It reads at most ``--limit``
trade-review events, restores only those payload blobs, and resolves only the
explicit ``entry_decision_id`` / ``exit_decision_id`` values in each payload.
It never derives a parent from trade_id, position_id, ordering, or a nearby
event.

``--apply`` is an explicit, transactional repair mode.  It revalidates both
event endpoints in the same transaction before each idempotent relation
insert, verifies the relation after insertion, and rolls back on any failure.
Learning/backfill/revision producers are audited but are not auto-repaired;
their immutable revisions must not be used to rewrite the live chain.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Mapping

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from backend.core.db import get_state_pg_conn  # noqa: E402
from backend.services.canonical_v2 import (  # noqa: E402
    _sql,
    append_relation,
    read_payload,
)


REPORT_SCHEMA = "canonical_v2_trade_lineage_audit.v1"
LIVE_REVIEW_PRODUCER = "live_closed_position"
RELATIONS = {
    "entry": ("entry_decision_id", "derived_from"),
    "exit": ("exit_decision_id", "reviews"),
}


def _value(row: Any, key: str, index: int = 0, default: Any = None) -> Any:
    if row is None:
        return default
    if isinstance(row, Mapping):
        return row.get(key, default)
    try:
        return row[index]
    except (IndexError, KeyError, TypeError):
        return default


def _as_text(value: Any) -> str:
    return str(value or "").strip()


def _review_events(conn: Any, *, limit: int, offset: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        _sql(
            conn,
            """
            SELECT event_id, entity_id, payload_hash, producer, observed_at
            FROM canonical_v2.event
            WHERE event_type='trade_review'
            ORDER BY observed_at ASC, event_id ASC
            LIMIT ? OFFSET ?
            """,
        ),
        (int(limit), int(offset)),
    ).fetchall()
    return [
        {
            "event_id": _as_text(_value(row, "event_id", 0)),
            "review_id": _as_text(_value(row, "entity_id", 1)),
            "payload_hash": _as_text(_value(row, "payload_hash", 2)),
            "producer": _as_text(_value(row, "producer", 3)),
            "observed_at": _value(row, "observed_at", 4),
        }
        for row in rows
    ]


def _decision_event_ids(conn: Any, decision_id: str) -> list[str]:
    """Resolve one explicit logical decision ID only when it is unambiguous."""
    rows = conn.execute(
        _sql(
            conn,
            """
            SELECT event_id
            FROM canonical_v2.event
            WHERE event_type='risk_decision' AND entity_id=?
            ORDER BY event_id ASC
            LIMIT 2
            """,
        ),
        (str(decision_id),),
    ).fetchall()
    return [_as_text(_value(row, "event_id", 0)) for row in rows]


def _relation_exists(
    conn: Any,
    *,
    child_event_id: str,
    parent_event_id: str,
    relation_type: str,
) -> bool:
    row = conn.execute(
        _sql(
            conn,
            """
            SELECT 1
            FROM canonical_v2.event_relation
            WHERE from_event_id=? AND to_event_id=? AND relation_type=?
            LIMIT 1
            """,
        ),
        (child_event_id, parent_event_id, relation_type),
    ).fetchone()
    return row is not None


def _finding(
    review: Mapping[str, Any],
    *,
    role: str,
    decision_id: str,
    relation_type: str,
    status: str,
    parent_event_id: str = "",
    detail: str = "",
) -> dict[str, Any]:
    return {
        "review_event_id": str(review.get("event_id") or ""),
        "review_id": str(review.get("review_id") or ""),
        "producer": str(review.get("producer") or ""),
        "role": role,
        "decision_id": decision_id,
        "parent_event_id": parent_event_id,
        "relation_type": relation_type,
        "status": status,
        "detail": detail,
    }


def audit_lineage(conn: Any, *, limit: int = 100, offset: int = 0) -> dict[str, Any]:
    """Return a bounded audit report without mutating ``conn``."""
    if int(limit) <= 0:
        raise ValueError("limit must be positive")
    if int(offset) < 0:
        raise ValueError("offset must be non-negative")

    reviews = _review_events(conn, limit=int(limit), offset=int(offset))
    findings: list[dict[str, Any]] = []
    payload_errors = 0
    for review in reviews:
        try:
            payload = read_payload(conn, review["payload_hash"])
        except Exception as exc:  # retain the event, but never infer lineage
            payload_errors += 1
            for role, (payload_key, relation_type) in RELATIONS.items():
                findings.append(
                    _finding(
                        review,
                        role=role,
                        decision_id="",
                        relation_type=relation_type,
                        status="payload_error",
                        detail=f"{type(exc).__name__}: {exc}",
                    )
                )
            continue
        if not isinstance(payload, Mapping):
            payload_errors += 1
            for role, (_, relation_type) in RELATIONS.items():
                findings.append(
                    _finding(
                        review,
                        role=role,
                        decision_id="",
                        relation_type=relation_type,
                        status="payload_not_object",
                    )
                )
            continue

        for role, (payload_key, relation_type) in RELATIONS.items():
            decision_id = _as_text(payload.get(payload_key))
            if not decision_id:
                findings.append(
                    _finding(
                        review,
                        role=role,
                        decision_id="",
                        relation_type=relation_type,
                        status="missing_decision_id",
                    )
                )
                continue
            parent_ids = _decision_event_ids(conn, decision_id)
            if not parent_ids:
                findings.append(
                    _finding(
                        review,
                        role=role,
                        decision_id=decision_id,
                        relation_type=relation_type,
                        status="parent_missing",
                    )
                )
                continue
            if len(parent_ids) != 1:
                findings.append(
                    _finding(
                        review,
                        role=role,
                        decision_id=decision_id,
                        relation_type=relation_type,
                        status="parent_ambiguous",
                        detail=f"{len(parent_ids)} risk_decision events match explicit ID",
                    )
                )
                continue
            parent_event_id = parent_ids[0]
            if _relation_exists(
                conn,
                child_event_id=review["event_id"],
                parent_event_id=parent_event_id,
                relation_type=relation_type,
            ):
                status = (
                    "linked"
                    if review["producer"] == LIVE_REVIEW_PRODUCER
                    else "linked_non_live_producer"
                )
            else:
                status = (
                    "missing_relation"
                    if review["producer"] == LIVE_REVIEW_PRODUCER
                    else "unlinked_non_live_producer"
                )
            findings.append(
                _finding(
                    review,
                    role=role,
                    decision_id=decision_id,
                    parent_event_id=parent_event_id,
                    relation_type=relation_type,
                    status=status,
                )
            )

    counts: dict[str, int] = {}
    for item in findings:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    return {
        "schema_version": REPORT_SCHEMA,
        "mode": "dry_run",
        "read_only": True,
        "writes_performed": False,
        "limit": int(limit),
        "offset": int(offset),
        "reviews_scanned": len(reviews),
        "payload_errors": payload_errors,
        "counts": dict(sorted(counts.items())),
        "findings": findings,
    }


def apply_repairs(conn: Any, report: Mapping[str, Any]) -> dict[str, Any]:
    """Apply findings as candidates, never as an authority.

    The report may be stale or tampered with.  The child event and its current
    payload are therefore re-read before every write; the payload's explicit
    decision ID and the role's canonical relation type determine the only
    acceptable parent/edge.  Report values must match that fresh resolution.
    """
    applied: list[dict[str, Any]] = []
    already_linked: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    try:
        for item in report.get("findings", []):
            if not isinstance(item, Mapping) or item.get("status") != "missing_relation":
                continue
            role = _as_text(item.get("role"))
            relation_spec = RELATIONS.get(role)
            if relation_spec is None:
                skipped.append({**dict(item), "apply_status": "invalid_role"})
                continue
            payload_key, expected_relation_type = relation_spec
            child_id = _as_text(item.get("review_event_id"))
            planned_parent_id = _as_text(item.get("parent_event_id"))
            planned_relation_type = _as_text(item.get("relation_type"))
            if (
                not child_id
                or not planned_parent_id
                or planned_relation_type != expected_relation_type
            ):
                skipped.append({**dict(item), "apply_status": "invalid_plan"})
                continue

            # The report is only a candidate index.  Re-read the child producer
            # and payload so a forged producer or stale parent cannot authorize
            # a write.
            child = conn.execute(
                _sql(
                    conn,
                    "SELECT event_id, producer, payload_hash "
                    "FROM canonical_v2.event "
                    "WHERE event_id=? AND event_type='trade_review' LIMIT 1",
                ),
                (child_id,),
            ).fetchone()
            if child is None:
                skipped.append({**dict(item), "apply_status": "child_missing"})
                continue
            child_producer = _as_text(_value(child, "producer", 1))
            if child_producer != LIVE_REVIEW_PRODUCER:
                skipped.append({
                    **dict(item),
                    "apply_status": "child_not_live_producer",
                    "actual_producer": child_producer,
                })
                continue
            try:
                payload = read_payload(conn, _as_text(_value(child, "payload_hash", 2)))
            except Exception as exc:
                skipped.append({
                    **dict(item),
                    "apply_status": "child_payload_error",
                    "detail": f"{type(exc).__name__}: {exc}",
                })
                continue
            if not isinstance(payload, Mapping):
                skipped.append({**dict(item), "apply_status": "child_payload_not_object"})
                continue
            decision_id = _as_text(payload.get(payload_key))
            if not decision_id:
                skipped.append({**dict(item), "apply_status": "child_decision_id_missing"})
                continue
            parent_ids = _decision_event_ids(conn, decision_id)
            if len(parent_ids) != 1:
                skipped.append({
                    **dict(item),
                    "apply_status": "parent_not_unique",
                    "actual_decision_id": decision_id,
                })
                continue
            parent_id = parent_ids[0]
            if parent_id != planned_parent_id:
                skipped.append({
                    **dict(item),
                    "apply_status": "stale_plan_parent_mismatch",
                    "actual_parent_event_id": parent_id,
                    "actual_decision_id": decision_id,
                })
                continue
            parent = conn.execute(
                _sql(
                    conn,
                    "SELECT event_id, entity_id FROM canonical_v2.event "
                    "WHERE event_id=? AND event_type='risk_decision' LIMIT 1",
                ),
                (parent_id,),
            ).fetchone()
            if parent is None or _as_text(_value(parent, "entity_id", 1)) != decision_id:
                skipped.append({
                    **dict(item),
                    "apply_status": "parent_endpoint_mismatch",
                    "actual_decision_id": decision_id,
                })
                continue
            if _relation_exists(
                conn,
                child_event_id=child_id,
                parent_event_id=parent_id,
                relation_type=expected_relation_type,
            ):
                already_linked.append({**dict(item), "apply_status": "already_linked"})
                continue
            append_relation(
                conn,
                from_event_id=child_id,
                to_event_id=parent_id,
                relation_type=expected_relation_type,
            )
            if not _relation_exists(
                conn,
                child_event_id=child_id,
                parent_event_id=parent_id,
                relation_type=expected_relation_type,
            ):
                raise RuntimeError(f"relation verification failed: {child_id}->{parent_id}")
            applied.append({**dict(item), "apply_status": "applied"})
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {
        "schema_version": REPORT_SCHEMA,
        "mode": "apply",
        "read_only": False,
        "writes_performed": bool(applied),
        "applied": applied,
        "already_linked": already_linked,
        "skipped": skipped,
        "applied_count": len(applied),
        "already_linked_count": len(already_linked),
        "skipped_count": len(skipped),
    }


def _open_sqlite(path: str, *, writable: bool) -> sqlite3.Connection:
    if writable:
        conn = sqlite3.connect(path)
    else:
        conn = sqlite3.connect(f"file:{Path(path).resolve()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--sqlite", help="SQLite canonical fixture/database path (tests/offline only).")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Explicitly apply eligible live-review relations transactionally.",
    )
    args = parser.parse_args(argv)
    if args.limit <= 0 or args.offset < 0:
        parser.error("--limit must be positive and --offset must be non-negative")

    conn: Any = None
    try:
        conn = (
            _open_sqlite(args.sqlite, writable=args.apply)
            if args.sqlite
            else get_state_pg_conn(read_only=not args.apply)
        )
        report = audit_lineage(conn, limit=args.limit, offset=args.offset)
        if args.apply:
            repair = apply_repairs(conn, report)
            report = {
                **report,
                "audit": report,
                "repair": repair,
                "mode": "apply",
                "read_only": False,
                "writes_performed": bool(repair["applied_count"]),
            }
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, default=str))
        return 0
    except Exception as exc:
        if conn is not None:
            conn.rollback()
        print(
            json.dumps(
                {
                    "schema_version": REPORT_SCHEMA,
                    "mode": "apply" if args.apply else "dry_run",
                    "ok": False,
                    "error": f"{type(exc).__name__}: {exc}",
                    "writes_performed": False,
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
