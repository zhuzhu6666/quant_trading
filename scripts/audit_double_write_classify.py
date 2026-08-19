#!/usr/bin/env python3
"""Read-only bounded classification of audit_double_write conflicts/unmatched.

Prep step for canonical_v2 payload archive (plan 8.4 step 2).  Reuses the exact
same streamed audit rows and matching rules as scripts/state_payload_compact.py
so totals are comparable with the fresh dry-run manifest.  It never runs an
unbounded API-audit x canonical join and never loads large JSON into Python:
the PG query extracts only small metadata server-side.

Read-only: opens get_state_pg_conn(read_only=True), performs no writes.
"""

from __future__ import annotations

import hashlib
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from backend.core.db import get_state_pg_conn  # noqa: E402

from scripts.state_payload_compact import (  # noqa: E402
    _audit_fields_match,
    _audit_rows,
    _compact_audit_row,
    _is_api_audit,
    _value,
)

FIELDS = ("action", "status", "config_hash", "run_id", "created_at")


def mismatched_fields(api: dict[str, Any], canonical: dict[str, Any]) -> list[str]:
    diff: list[str] = []
    for key in FIELDS:
        left = str(_value(api, key) or "")
        right = str(_value(canonical, key) or "")
        if left and right and left != right:
            diff.append(key)
        elif key == "config_hash" and bool(left) != bool(right):
            diff.append(key)
    return diff


def classify(conn: Any) -> dict[str, Any]:
    canonical: dict[str, dict[str, Any]] = {}
    api_rows: list[dict[str, Any]] = []
    skipped_no_decision_id = 0
    for row in _audit_rows(conn):
        decision_id = str(_value(row, "decision_id") or "")
        if not decision_id:
            skipped_no_decision_id += 1
            continue
        compact = _compact_audit_row(row)
        if _is_api_audit(row):
            api_rows.append(compact)
        else:
            canonical[decision_id] = compact

    buckets: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for item in canonical.values():
        key = (str(_value(item, "action") or ""), str(_value(item, "status") or ""), str(_value(item, "config_hash") or ""))
        buckets.setdefault(key, []).append(item)

    reasons: Counter[str] = Counter()
    per_type: dict[str, Counter[str]] = {}
    samples: dict[str, list[str]] = {}
    digest = hashlib.sha256()
    conflict_field_detail: Counter[str] = Counter()

    for row in api_rows:
        decision_id = str(_value(row, "decision_id") or "")
        compact = tuple(
            (key, str(_value(row, key) or ""))
            for key in ("decision_id", "run_id", "action", "status", "config_hash", "created_at", "decision_type", "scope_type", "projection_type")
        )
        digest.update(json.dumps(compact, ensure_ascii=False, sort_keys=True).encode("utf-8"))
        digest.update(b"\n")

        direct = str(_value(row, "direct_decision_id") or "")
        if direct:
            target = canonical.get(direct)
            if target is not None:
                if _audit_fields_match(row, target):
                    reason = "linked_direct"
                else:
                    reason = "conflict_direct_field_mismatch"
                    conflict_field_detail.update(mismatched_fields(row, target) or ["unknown"])
            else:
                reason = "conflict_direct_target_missing"
        else:
            key = (str(_value(row, "action") or ""), str(_value(row, "status") or ""), str(_value(row, "config_hash") or ""))
            candidates = [item for item in buckets.get(key, []) if _audit_fields_match(row, item)]
            if len(candidates) == 1:
                reason = "linked_by_key"
            elif len(candidates) > 1:
                reason = "conflict_ambiguous_candidates"
            else:
                reason = "unmatched_no_candidate"

        reasons[reason] += 1
        ptype = str(_value(row, "projection_type") or "") or str(_value(row, "decision_type") or "") or "?"
        per_type.setdefault(reason, Counter())[ptype] += 1
        if len(samples.get(reason, [])) < 25:
            samples.setdefault(reason, []).append(decision_id)

    counts = {
        "api_audit_rows": len(api_rows),
        "canonical_events": len(canonical),
        "skipped_no_decision_id": skipped_no_decision_id,
        "linked": reasons.get("linked_direct", 0) + reasons.get("linked_by_key", 0),
        "conflicts": sum(v for k, v in reasons.items() if k.startswith("conflict")),
        "unmatched": reasons.get("unmatched_no_candidate", 0),
    }
    return {
        "counts": counts,
        "reasons": dict(reasons),
        "conflict_field_detail": dict(conflict_field_detail),
        "per_reason_projection_type": {k: dict(v) for k, v in per_type.items()},
        "sample_decision_ids": samples,
        "content_digest": digest.hexdigest(),
    }


def main() -> int:
    conn = None
    try:
        conn = get_state_pg_conn(read_only=True)
        result = classify(conn)
        result["schema_version"] = "audit_double_write_classify.v1"
        result["writes_performed"] = False
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
