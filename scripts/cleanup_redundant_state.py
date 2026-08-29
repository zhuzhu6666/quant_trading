#!/usr/bin/env python3
"""Inspect and remove semantic duplicates from mutable runtime projections.

This utility intentionally does not touch ``canonical_v2``.  The default is a
read-only report.  ``--apply`` performs the selected deletes in one
transaction, prints the retained/deleted counts, commits, and then runs plain
``VACUUM (ANALYZE)`` on affected tables.  No backup is created by this tool;
the caller should retain the check output as the deletion evidence.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.core.db import get_state_pg_conn  # noqa: E402
from backend.core.db_helpers import load_json  # noqa: E402
from backend.services.policy_suggestion_identity import (  # noqa: E402
    normalize_policy_suggestion_value,
)


def _value(row: Any, name: str, default: Any = "") -> Any:
    try:
        return row[name]
    except (KeyError, IndexError, TypeError):
        try:
            return row.get(name, default)
        except AttributeError:
            return default


def _key(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _table_exists(conn: Any, table: str) -> bool:
    row = conn.execute(
        """SELECT 1 FROM information_schema.tables
           WHERE table_schema=current_schema() AND table_name=%s LIMIT 1""",
        (table,),
    ).fetchone()
    return bool(row)


def _ordered_rows(conn: Any, sql: str) -> list[Any]:
    return list(conn.execute(sql).fetchall())


def _model_permission_base(row: Any) -> dict[str, Any]:
    """Return the permission result fields that define audit semantics.

    ``operation``/run metadata is deliberately absent.  Older rows predate
    the artifact identity field, so the cleanup pass can only treat those as
    the same semantic result when a single newer identity is present for the
    same base result; otherwise they remain isolated rather than being
    guessed across possible artifact replacements.
    """

    artifact_path = str(_value(row, "artifact_path") or "").strip()
    if artifact_path:
        path = Path(artifact_path).expanduser()
        if not path.is_absolute():
            path = ROOT / path
        try:
            artifact_path = str(path.resolve())
        except OSError:
            artifact_path = str(path)
    return {
        "model_type": str(_value(row, "model_type") or ""),
        "artifact_path": artifact_path,
        "status": str(_value(row, "status") or ""),
        "reason": str(_value(row, "reason") or ""),
        "capabilities": normalize_policy_suggestion_value(
            load_json(_value(row, "capabilities_json"), {})
        ),
        "violations": normalize_policy_suggestion_value(
            load_json(_value(row, "violations_json"), [])
        ),
    }


def _model_permission_duplicates(conn: Any) -> dict[str, Any]:
    if not _table_exists(conn, "model_permission_audit"):
        return {"table": "model_permission_audit", "total": 0, "groups": 0, "retain": 0, "delete": 0, "delete_ids": []}
    rows = _ordered_rows(
        conn,
        """SELECT audit_id, model_type, artifact_path, status, reason,
                  capabilities_json, violations_json, context_json, created_at
           FROM model_permission_audit
           ORDER BY created_at DESC, audit_id DESC""",
    )
    # First collect the identity-bearing rows by their semantic base.  If a
    # base has exactly one known artifact identity, legacy rows without that
    # field can be folded into it safely for this one-time cleanup.  Multiple
    # identities indicate an actual replacement history, so legacy rows stay
    # separate and are not guessed into one replacement.
    base_identities: dict[str, set[str]] = defaultdict(set)
    row_bases: list[tuple[Any, str, str]] = []
    for row in rows:
        base_key = _key(_model_permission_base(row))
        context = load_json(_value(row, "context_json"), {})
        identity = str(context.get("artifact_identity") or "") if isinstance(context, dict) else ""
        if identity:
            base_identities[base_key].add(identity)
        row_bases.append((row, base_key, identity))

    groups: dict[str, list[Any]] = defaultdict(list)
    for row, base_key, identity in row_bases:
        known = base_identities.get(base_key, set())
        if not identity and len(known) == 1:
            identity = next(iter(known))
        # A legacy-only base is grouped by its stable path/result fields; an
        # identity-bearing base is grouped by the actual artifact identity.
        group_key = _key({"base": base_key, "artifact_identity": identity})
        groups[group_key].append(row)
    delete_ids = [
        str(_value(row, "audit_id") or "")
        for grouped in groups.values()
        for row in grouped[1:]
        if str(_value(row, "audit_id") or "")
    ]
    return {
        "table": "model_permission_audit",
        "total": len(rows),
        "groups": len(groups),
        "retain": len(rows) - len(delete_ids),
        "delete": len(delete_ids),
        "delete_ids": delete_ids,
    }


def _policy_suggestion_duplicates(conn: Any) -> dict[str, Any]:
    if not _table_exists(conn, "policy_suggestion"):
        return {"table": "policy_suggestion", "total": 0, "groups": 0, "retain": 0, "delete": 0, "delete_ids": []}
    rows = _ordered_rows(
        conn,
        """SELECT suggestion_id, scope_type, scope_key, action, status,
                  evidence_json, governance_eligibility_fingerprint,
                  applied_mutation_id, created_at
           FROM policy_suggestion
           ORDER BY created_at DESC, suggestion_id DESC""",
    )
    groups: dict[str, list[Any]] = defaultdict(list)
    for row in rows:
        groups[
            _key(
                {
                    "scope_type": str(_value(row, "scope_type") or ""),
                    "scope_key": str(_value(row, "scope_key") or ""),
                    "action": str(_value(row, "action") or ""),
                    "status": str(_value(row, "status") or ""),
                    "evidence": normalize_policy_suggestion_value(
                        load_json(_value(row, "evidence_json"), {})
                    ),
                    "qualification_fingerprint": str(
                        _value(row, "governance_eligibility_fingerprint") or ""
                    ),
                }
            )
        ].append(row)
    delete_ids: list[str] = []
    for grouped in groups.values():
        # A committed mutation is a real fact and is never a duplicate merely
        # because another observation has the same visible suggestion fields.
        protected = {
            str(_value(row, "suggestion_id") or "")
            for row in grouped
            if str(_value(row, "applied_mutation_id") or "")
        }
        retained_unapplied = False
        for row in grouped:
            suggestion_id = str(_value(row, "suggestion_id") or "")
            if not suggestion_id or suggestion_id in protected:
                continue
            if retained_unapplied:
                delete_ids.append(suggestion_id)
            else:
                retained_unapplied = True
    return {
        "table": "policy_suggestion",
        "total": len(rows),
        "groups": len(groups),
        "retain": len(rows) - len(delete_ids),
        "delete": len(delete_ids),
        "delete_ids": delete_ids,
    }


def _factor_catalog_duplicates(conn: Any) -> dict[str, Any]:
    if not _table_exists(conn, "factor_catalog_snapshot"):
        return {"table": "factor_catalog_snapshot", "total": 0, "groups": 0, "retain": 0, "delete": 0, "delete_ids": []}
    rows = _ordered_rows(
        conn,
        """SELECT snapshot_id, catalog_hash, catalog_json, created_at
           FROM factor_catalog_snapshot
           ORDER BY created_at DESC, snapshot_id DESC""",
    )
    full_by_hash: dict[str, list[Any]] = defaultdict(list)
    for row in rows:
        digest = str(_value(row, "catalog_hash") or "")
        if digest and str(_value(row, "catalog_json") or "").lstrip().startswith("["):
            full_by_hash[digest].append(row)
    delete_ids = [
        str(_value(row, "snapshot_id") or "")
        for grouped in full_by_hash.values()
        for row in grouped[1:]
        if str(_value(row, "snapshot_id") or "")
    ]
    return {
        "table": "factor_catalog_snapshot",
        "total": len(rows),
        "groups": len(full_by_hash),
        "full_rows": sum(len(grouped) for grouped in full_by_hash.values()),
        "retain": len(rows) - len(delete_ids),
        "delete": len(delete_ids),
        "delete_ids": delete_ids,
    }


def _runtime_config_orphans(conn: Any) -> dict[str, Any]:
    if not (_table_exists(conn, "runtime_config_payload") and _table_exists(conn, "runtime_config_snapshot")):
        return {"table": "runtime_config_payload", "total": 0, "retain": 0, "delete": 0, "delete_ids": []}
    rows = _ordered_rows(
        conn,
        """SELECT p.payload_hash
           FROM runtime_config_payload p
           WHERE p.payload_hash <> ''
             AND NOT EXISTS (
                 SELECT 1 FROM runtime_config_snapshot s
                 WHERE s.payload_hash=p.payload_hash
             )""",
    )
    delete_ids = [str(_value(row, "payload_hash") or "") for row in rows if str(_value(row, "payload_hash") or "")]
    total = int(
        conn.execute("SELECT COUNT(*) AS n FROM runtime_config_payload").fetchone()["n"]
    )
    return {
        "table": "runtime_config_payload",
        "total": total,
        "retain": total - len(delete_ids),
        "delete": len(delete_ids),
        "delete_ids": delete_ids,
    }


def _projection_report(conn: Any) -> dict[str, Any]:
    if not _table_exists(conn, "factor_runtime_projection"):
        return {"table": "factor_runtime_projection", "total": 0, "stale_coordinator": 0, "delete": 0}
    total = int(conn.execute("SELECT COUNT(*) AS n FROM factor_runtime_projection").fetchone()["n"])
    stale = int(
        conn.execute(
            """SELECT COUNT(*) AS n FROM factor_runtime_projection
               WHERE process_role='governance_coordinator'
                 AND NOT (process_id='factor_lifecycle_service' AND boot_id='canonical')"""
        ).fetchone()["n"]
    )
    # Do not delete coordinator projections automatically: process identity is
    # deployment state, and the lifecycle service owns that pruning decision.
    return {
        "table": "factor_runtime_projection",
        "total": total,
        "stale_coordinator": stale,
        "delete": 0,
        "delete_ids": [],
    }


def inspect(conn: Any) -> dict[str, Any]:
    result = {
        "model_permission_audit": _model_permission_duplicates(conn),
        "policy_suggestion": _policy_suggestion_duplicates(conn),
        "factor_catalog_snapshot": _factor_catalog_duplicates(conn),
        "runtime_config_payload": _runtime_config_orphans(conn),
        "factor_runtime_projection": _projection_report(conn),
        "canonical_v2": {"delete": 0, "status": "untouched"},
    }
    result["delete_total"] = sum(int(item.get("delete") or 0) for item in result.values() if isinstance(item, dict))
    return result


def _delete_ids(conn: Any, table: str, column: str, ids: Iterable[str]) -> int:
    values = sorted({str(item) for item in ids if str(item)})
    if not values:
        return 0
    cursor = conn.execute(
        f"DELETE FROM {table} WHERE {column}=ANY(%s)",
        (values,),
    )
    return int(getattr(cursor, "rowcount", 0) or 0)


def apply_deletes(conn: Any, report: dict[str, Any]) -> dict[str, int]:
    deleted = {
        "model_permission_audit": _delete_ids(
            conn, "model_permission_audit", "audit_id", report["model_permission_audit"]["delete_ids"]
        ),
        "policy_suggestion": _delete_ids(
            conn, "policy_suggestion", "suggestion_id", report["policy_suggestion"]["delete_ids"]
        ),
        "factor_catalog_snapshot": _delete_ids(
            conn, "factor_catalog_snapshot", "snapshot_id", report["factor_catalog_snapshot"]["delete_ids"]
        ),
        "runtime_config_payload": _delete_ids(
            conn, "runtime_config_payload", "payload_hash", report["runtime_config_payload"]["delete_ids"]
        ),
        "factor_runtime_projection": 0,
    }
    return deleted


def _report_for_output(report: dict[str, Any]) -> dict[str, Any]:
    """Keep check output useful without printing tens of thousands of ids."""

    output: dict[str, Any] = {}
    for name, item in report.items():
        if not isinstance(item, dict):
            output[name] = item
            continue
        compact = dict(item)
        ids = list(compact.pop("delete_ids", []) or [])
        if ids:
            compact["delete_id_sample"] = ids[:10]
        output[name] = compact
    return output


def vacuum_analyze(tables: Iterable[str]) -> list[str]:
    vacuumed: list[str] = []
    conn = get_state_pg_conn()
    try:
        # ``get_state_pg_conn`` installs the search_path with a SET statement,
        # which starts a transaction on psycopg when autocommit is disabled.
        # VACUUM cannot run inside a transaction, so end that setup
        # transaction before switching this dedicated connection to
        # autocommit mode.
        conn.rollback()
        conn.autocommit = True
        # The connection helper's search_path SET is rolled back together
        # with the setup transaction above; restore it in autocommit mode.
        conn.execute("SET search_path TO runtime, public")
        for table in tables:
            try:
                conn.execute(f"VACUUM (ANALYZE) {table}")
                vacuumed.append(table)
            except Exception:
                # One unavailable table must not hide successful deletes in
                # the committed transaction; report the successful subset.
                continue
    finally:
        conn.close()
    return vacuumed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="read-only report (default)")
    mode.add_argument("--apply", action="store_true", help="delete only reported semantic duplicates")
    args = parser.parse_args(argv)

    conn = get_state_pg_conn()
    try:
        report = inspect(conn)
        if not args.apply:
            print(json.dumps({"mode": "check", "report": _report_for_output(report)}, ensure_ascii=False, sort_keys=True))
            return 0
        if not int(report.get("delete_total") or 0):
            print(json.dumps({"mode": "apply", "report": _report_for_output(report), "deleted": {}, "vacuumed": []}, ensure_ascii=False, sort_keys=True))
            return 0
        deleted = apply_deletes(conn, report)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    vacuumed = vacuum_analyze([table for table, count in deleted.items() if count])
    print(json.dumps({"mode": "apply", "report": _report_for_output(report), "deleted": deleted, "vacuumed": vacuumed}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
