#!/usr/bin/env python3
"""Read-only idempotency probe for the payload-compact lineage writeback.

Computes the audit lineage assignments (same rules as scripts/state_payload_compact.py)
and reports how many evolution_decision rows would have canonical_event_id /
projection_type changed by ``--apply --targets payload``.  Writes a short audit
artifact.  Read-only; no writes to the database.
"""

from __future__ import annotations

import hashlib
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from backend.core.db import get_state_pg_conn  # noqa: E402
from scripts.state_payload_compact import (  # noqa: E402
    _audit_lineage_from_compact,
    _audit_rows,
    _compact_audit_row,
    _is_api_audit,
    _value,
)

EXPECTED = {"api_audit_rows": 14471, "canonical_events": 30263, "linked": 55, "conflicts": 5927, "unmatched": 8489}


def probe(conn: Any) -> dict[str, Any]:
    canonical: dict[str, dict[str, Any]] = {}
    api: list[dict[str, Any]] = []
    for row in _audit_rows(conn):
        decision_id = str(_value(row, "decision_id") or "")
        if not decision_id:
            continue
        compact = _compact_audit_row(row)
        if _is_api_audit(row):
            api.append(compact)
        else:
            canonical[decision_id] = compact

    assignments, stats = _audit_lineage_from_compact(canonical, api)

    changed: list[dict[str, str]] = []
    unchanged = 0
    digest = hashlib.sha256()
    for row in _audit_rows(conn):
        decision_id = str(_value(row, "decision_id") or "")
        if decision_id not in assignments:
            continue
        canon_id, ptype = assignments[decision_id]
        stored_ptype = str(_value(row, "projection_type") or "") or ""
        stored_ceid = str(_value(row, "canonical_event_id") or "") or ""
        digest.update(
            json.dumps([decision_id, stored_ptype, stored_ceid, canon_id, ptype], ensure_ascii=False, sort_keys=True).encode()
        )
        digest.update(b"\n")
        if stored_ptype == ptype and stored_ceid == canon_id:
            unchanged += 1
        else:
            changed.append(
                {
                    "decision_id": decision_id,
                    "stored_projection_type": stored_ptype,
                    "stored_canonical_event_id": stored_ceid,
                    "matcher_projection_type": ptype,
                    "matcher_canonical_event_id": canon_id,
                }
            )

    return {
        "schema_version": "payload_apply_lineage_idempotency.v1",
        "writes_performed": False,
        "audit_stats": stats,
        "audit_stats_match_fresh_manifest": stats == EXPECTED,
        "total_decisions": len(assignments),
        "rows_unchanged": unchanged,
        "rows_would_change": len(changed),
        "changed_lineage_sample": changed,  # full list is small; keep for audit
        "content_digest": digest.hexdigest(),
    }


def main() -> int:
    conn = None
    try:
        conn = get_state_pg_conn(read_only=True)
        result = probe(conn)
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"ok": False, "error_type": type(exc).__name__, "error": str(exc)}), file=sys.stderr)
        return 2
    finally:
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
