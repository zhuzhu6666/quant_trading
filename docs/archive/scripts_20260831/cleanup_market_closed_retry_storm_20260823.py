#!/usr/bin/env python3
"""One-shot cleanup of the 2026-08-23 market-closed close-retry storm.

The holding-timeout supervisor (_enforce_holding_timeout) retried a broker
close every ~6s across the weekend closure for position 284602893.  Each
attempt wrote one ``risk_decision`` (producer=live_ledger, entity_id=dec_*,
event_type=holding_timeout) plus one ``supervisor_trace``
(producer=position_supervisor, entity_id=psvtrace_*) with a full context
payload into canonical_v2.  The code fix (market-time budget +
deterministic-rejection suppression) prevents recurrence; this script
identifies redundant rows for suppression review.

Scope guardrails:
- Only the two storm producers/types inside the storm window.
- risk_decision events referenced by a *pending* training sample are kept
  (the labeller may still read them); excluded samples are terminal.
- Events with event_relation edges to surviving events are kept (FK safety).
- First and last storm row of each type are kept as audit anchors.
- This script is permanently preview-only.  canonical_v2 is append-only;
  physical deletion is not supported here.  Use the preview counts to design
  a separate, explicitly governed suppression projection.

The default invocation is read-only.  ``--apply`` is rejected explicitly and
cannot execute a DELETE.
"""

from __future__ import annotations

import sys
import time

sys.path.insert(0, "/home/ubuntu/quant_trading")

from backend.core.db import get_state_pg_conn  # noqa: E402
from backend.services.canonical_v2 import read_payload  # noqa: E402

POSITION_ID = "284602893"
WINDOW_START = "2026-08-23 01:55:00+08"
EVENT_TYPES = ("risk_decision", "supervisor_trace")
PRODUCER_BY_TYPE = {"risk_decision": "live_ledger", "supervisor_trace": "position_supervisor"}
# Fallback: if the wall clock is somehow past the expected reopen, stop here.
HARD_WINDOW_END = "2026-08-31 00:00:00+08"


def main() -> int:
    if "--apply" in sys.argv[1:]:
        print(
            "refusing --apply: canonical_v2 is append-only; "
            "this script never performs physical deletion",
            file=sys.stderr,
        )
        return 2

    conn = get_state_pg_conn()
    cur = conn.cursor()

    cur.execute("select extract(epoch from now()) as now_epoch")
    window_end_epoch = min(float(cur.fetchone()["now_epoch"]), _ts(HARD_WINDOW_END))
    window_end = time.strftime("%Y-%m-%d %H:%M:%S+08", time.gmtime(window_end_epoch + 8 * 3600))
    print(f"storm window: {WINDOW_START} .. {window_end} (exclusive)")

    counts: dict[str, int] = {}
    ids_by_type: dict[str, list[str]] = {}
    for etype in EVENT_TYPES:
        cur.execute(
            """
            select event_id, payload_hash from canonical_v2.event
            where producer = %s and event_type = %s
              and observed_at >= %s::timestamptz
              and observed_at < %s::timestamptz
            order by observed_at
            """,
            (PRODUCER_BY_TYPE[etype], etype, WINDOW_START, window_end),
        )
        ids: list[str] = []
        for row in cur.fetchall():
            try:
                payload = read_payload(conn, str(row["payload_hash"] or ""))
            except Exception as exc:  # noqa: BLE001
                print(f"skip unreadable payload event={row['event_id']}: {exc}")
                continue
            if (
                isinstance(payload, dict)
                and str(payload.get("position_id") or "") == POSITION_ID
            ):
                ids.append(str(row["event_id"]))
        counts[etype] = len(ids)
        ids_by_type[etype] = ids
        print(f"{etype}: {len(ids)} target-position storm rows")

    total = sum(counts.values())
    if not total:
        print("nothing to review; aborting without changes")
        return 1

    candidates: set[str] = set()
    for etype in EVENT_TYPES:
        candidates.update(ids_by_type[etype])

    # 1) Keep risk_decision events referenced by *pending* training samples.
    cur.execute(
        """
        select source_id from canonical_v2.training_sample_row
        where label_status = 'pending'
          and source_table like '%%risk_decision%%'
        """
    )
    pending_refs = {row["source_id"] for row in cur.fetchall()}
    cur.execute(
        """
        select event_id from canonical_v2.event
        where event_type = 'risk_decision' and producer = 'live_ledger'
          and entity_id = any(%s)
        """,
        (sorted(pending_refs),),
    )
    protected_pending = {row["event_id"] for row in cur.fetchall()}
    print(f"protected (pending-sample) decisions: {len(protected_pending)}")

    # 2) Keep events with relation edges to events outside the candidate set.
    cur.execute(
        """
        select from_event_id, to_event_id from canonical_v2.event_relation
        where from_event_id = any(%s) or to_event_id = any(%s)
        """,
        (sorted(candidates), sorted(candidates)),
    )
    blocked_by_relation: set[str] = set()
    relation_rows = cur.fetchall()
    for row in relation_rows:
        from_id = str(row["from_event_id"] or "")
        to_id = str(row["to_event_id"] or "")
        if from_id not in candidates or to_id not in candidates:
            blocked_by_relation.update({from_id, to_id} & candidates)
    print(f"relation rows touching storm: {len(relation_rows)}; blocked events: {len(blocked_by_relation)}")

    # 3) Audit anchors: first and last of each type always survive.
    anchors: set[str] = set()
    for etype in EVENT_TYPES:
        ids = ids_by_type[etype]
        if ids:
            anchors.update({ids[0], ids[-1]})

    candidates -= protected_pending | blocked_by_relation | anchors
    print(f"events requiring suppression review: {len(candidates)} "
          f"(protected {len(protected_pending)}, relation-blocked {len(blocked_by_relation)}, "
          f"anchors {len(anchors)})")
    if not candidates:
        print("nothing left to review; aborting without changes")
        return 1

    conn.rollback()
    print("dry-run only; canonical_v2 remains unchanged")
    return 0


def _ts(text: str) -> float:
    import datetime as dt

    return dt.datetime.fromisoformat(text).timestamp()


if __name__ == "__main__":
    raise SystemExit(main())
