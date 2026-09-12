#!/usr/bin/env python3
"""L0-0R remediation (2026-09-12).

Facts re-verified before this script was written (read-only, 2026-09-12):
  restart_replay reviews           57  -> attribution_integrity='chain_broken'
  broker_close with backfill mark  27  -> attribution_integrity='restart_affected'
  other non-full reviews            2  -> removed from the learning pool only
  recovery_position_state guesses  38 -> close_reason='chain_broken'

The canonical event stream is immutable, so re-tagging writes a learning
revision (new latest event per review id).  experience_memory rows whose
review is no longer learning-eligible are RETAGGED in place with the new
attribution_integrity after a JSON backup.

Correction 2026-09-12 (v1.10): the first run deleted those rows instead.
Principle 4 (data is not reproducible, structure is) and section 5.3 of the
repair plan both forbid dropping samples; L0-0R asks for a fact-based retag,
not a purge.  The 86 deleted rows were restored from the backup and this
script now retags instead of deleting.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.core.db import get_state_pg_conn
from backend.core.db_helpers import execute as _execute
from backend.services.canonical_v2 import read_payloads, record_payload_event
from backend.services.canonical_v2_reader import iter_reviews

CHAIN_BROKEN = "chain_broken"
RESTART_AFFECTED = "restart_affected"
BACKUP_DIR = ROOT / "run_artifacts"


def _nested_review(review: dict) -> dict:
    payload = review.get("payload") or {}
    nested = payload.get("review")
    return nested if isinstance(nested, dict) else {}


def classify(reviews: list[dict]) -> tuple[list[dict], list[dict]]:
    """Return (revision_targets, pool_only_targets)."""

    revision_targets: list[dict] = []
    pool_only: list[dict] = []
    for review in reviews:
        nested = _nested_review(review)
        close_reason = str(nested.get("close_reason") or "")
        integrity = str(nested.get("attribution_integrity") or "")
        backfill = nested.get("close_reason_source_backfill")
        has_backfill = isinstance(backfill, dict) and bool(backfill)
        base = {
            "review_id": str(review.get("review_id") or ""),
            "close_reason": close_reason,
            "attribution_integrity": integrity,
            "has_backfill": has_backfill,
            "context_integrity": str(nested.get("context_integrity") or ""),
        }
        if close_reason == "restart_replay":
            revision_targets.append({**base, "new_integrity": CHAIN_BROKEN})
        elif close_reason == "broker_close" and has_backfill:
            revision_targets.append({**base, "new_integrity": RESTART_AFFECTED})
        elif integrity != "full":
            pool_only.append({**base, "new_integrity": None})
    return revision_targets, pool_only


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="report only (default)")
    mode.add_argument("--apply", action="store_true", help="write revisions and retag")
    args = parser.parse_args(argv)

    conn = get_state_pg_conn(read_only=not args.apply)
    try:
        reviews = iter_reviews(conn, limit=0)
        revision_targets, pool_only = classify(reviews)
        target_ids = [t["review_id"] for t in revision_targets + pool_only]

        print(f"logical reviews       : {len(reviews)}")
        print(f"revision targets      : {len(revision_targets)}")
        print(
            "  chain_broken        :",
            sum(1 for t in revision_targets if t["new_integrity"] == CHAIN_BROKEN),
        )
        print(
            "  restart_affected    :",
            sum(1 for t in revision_targets if t["new_integrity"] == RESTART_AFFECTED),
        )
        print(f"pool_only (no retag)  : {len(pool_only)}")
        print(f"review ids to retag   : {len(target_ids)}")

        placeholders = ", ".join("?" for _ in target_ids)
        memory_rows = _execute(
            conn,
            f"SELECT * FROM experience_memory WHERE source_id IN ({placeholders})",
            tuple(target_ids),
        ).fetchall() if target_ids else []
        print(f"experience_memory rows matched: {len(memory_rows)}")

        recovery_rows = _execute(
            conn,
            "SELECT * FROM recovery_position_state WHERE close_reason = ?",
            ("broker_position_not_found",),
        ).fetchall()
        print(f"recovery rows to retag : {len(recovery_rows)}")

        if not args.apply:
            print("dry-run only; rerun with --apply to execute")
            return 0

        now = time.time()
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        backup_path = BACKUP_DIR / f"l00r_remediation_backup_{int(now)}.json"
        backup = {
            "generated_at": now,
            "revision_targets": revision_targets,
            "pool_only_targets": pool_only,
            "experience_memory": [dict(row) for row in memory_rows],
            "recovery_position_state": [dict(row) for row in recovery_rows],
        }
        backup_path.write_text(json.dumps(backup, ensure_ascii=False, default=str))
        print("backup written:", backup_path)

        written = 0
        for target in revision_targets:
            review = next(
                r for r in reviews if str(r.get("review_id")) == target["review_id"]
            )
            payload = json.loads(json.dumps(review.get("payload") or {}, default=str))
            nested = payload.setdefault("review", {})
            nested["attribution_integrity"] = target["new_integrity"]
            payload["updated_at"] = now
            record_payload_event(
                conn,
                event_type="trade_review",
                entity_type="review",
                entity_id=target["review_id"],
                payload=payload,
                observed_at=now,
                producer="operator_remediation",
                payload_kind="trade_review",
                event_id=f"remediation_trade_review_{target['review_id']}",
                idempotency_key=f"remediation:{target['review_id']}",
            )
            written += 1
        print("revisions written:", written)

        retagged = _execute(
            conn,
            "UPDATE recovery_position_state SET close_reason = ?, updated_at = ? "
            "WHERE close_reason = ?",
            (CHAIN_BROKEN, now, "broker_position_not_found"),
        )
        print("recovery rows retagged:", getattr(retagged, "rowcount", "?"))

        retagged_rows = 0
        for target in revision_targets + pool_only:
            new_integrity = target["new_integrity"] or target.get("attribution_integrity")
            if not new_integrity:
                continue
            target_rows = _execute(
                conn,
                "SELECT experience_id, decision_context_json FROM experience_memory WHERE source_id = ?",
                (target["review_id"],),
            ).fetchall()
            for memory_row in target_rows:
                ctx = json.loads(memory_row["decision_context_json"] or "{}")
                ctx["attribution_integrity"] = new_integrity
                _execute(
                    conn,
                    "UPDATE experience_memory SET decision_context_json = ? WHERE experience_id = ?",
                    (json.dumps(ctx, ensure_ascii=False), memory_row["experience_id"]),
                )
                retagged_rows += 1
        print("experience_memory rows retagged (never deleted):", retagged_rows)

        conn.commit()
        print("committed")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
