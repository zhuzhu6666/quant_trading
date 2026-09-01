#!/usr/bin/env python3
"""Canonical_v2 read-only reader facade.

The facade is the single fact-read authority. It reads only
``canonical_v2.event``, ``canonical_v2.payload_blob``,
``canonical_v2.event_relation``, and ``canonical_v2.training_sample_row``.
It never writes and never consults a retired fact table.

Functions:
    resolve_event(conn, kind, primary_key) -> event row or None
    read_review / read_decision / read_order_event / read_position_event
        (by canonical entity key) -> {"source","event_id","entity_*","payload"}
    read_review_event(conn, event_id) -> one immutable historical review revision
    iter_reviews(conn, limit) -> latest record per review id in observed order
    iter_decisions(conn, limit=0) -> bounded decision records
    read_trade_chain(conn, review_id) -> review + entry decision + order/position
        events reachable from canonical relations (derived_from / caused_by)
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

from backend.services.canonical_v2 import _db_time, _sql, read_payload  # noqa: E402
from backend.services.fact_envelope import observed_epoch

EVENT_TYPE = {
    "review": "trade_review",
    "decision": "risk_decision",
    "order": "broker_execution",
    "position": "position_transition",
}
EVENT_ID_PREFIX = {
    "review": "live_review_",
    "decision": "live_decision_",
    "order": "live_ordevt_",
    "position": "live_posevt_",
}

_ROW = tuple[int, str, str, str]

_REVIEW_EVENT_COLUMNS = (
    "event_id",
    "entity_id",
    "payload_hash",
    "observed_at",
    "candidate_count",
)
_REVIEW_RESOLVER = "canonical_review_latest.v1"
_REVIEW_ORDER = "event.observed_at,event.event_id"


def _canonical_ready(conn: Any) -> bool:
    """Return True when the canonical_v2 schema/event table is reachable."""
    try:
        conn.execute(_sql(conn, "SELECT 1 FROM canonical_v2.event LIMIT 1")).fetchone()
        return True
    except Exception:
        return False


canonical_ready = _canonical_ready


def _dict_row(row: Any, columns: tuple[str, ...]) -> dict[str, Any] | None:
    """Normalize any read row (dict / sqlite3.Row / psycopg Row / plain tuple)."""
    if row is None:
        return None
    if isinstance(row, Mapping):
        return dict(row)
    keys = getattr(row, "keys", None)
    if callable(keys):
        try:
            return {name: row[name] for name in keys()}
        except Exception:
            pass
    try:
        return dict(zip(columns, row))
    except Exception:
        return None


def _resolve(conn: Any, kind: str, primary_key: str) -> dict[str, Any] | None:
    """Resolve a canonical entity key by its deterministic live event id."""
    if not _canonical_ready(conn):
        return None
    prefix = EVENT_ID_PREFIX.get(kind)
    if prefix is None:
        return None
    event_id = f"{prefix}{str(primary_key or '')}"
    return _event_row(conn, event_id)


def _event_row(conn: Any, event_id: str) -> dict[str, Any] | None:
    if not _canonical_ready(conn):
        return None
    row = conn.execute(
        _sql(
            conn,
            "SELECT event_id, event_type, entity_type, entity_id, payload_hash, observed_at "
            "FROM canonical_v2.event WHERE event_id=?",
        ),
        (str(event_id or ""),),
    ).fetchone()
    if row is None:
        return None
    return _dict_row(
        row,
        (
            "event_id",
            "event_type",
            "entity_type",
            "entity_id",
            "payload_hash",
            "observed_at",
        ),
    )


def resolve_event(conn: Any, kind: str, primary_key: str) -> dict[str, Any] | None:
    return _resolve(conn, kind, primary_key)


def _read_fact(conn: Any, kind: str, primary_key: str) -> dict[str, Any] | None:
    event = _resolve(conn, kind, primary_key)
    if event is None:
        return None
    payload = read_payload(conn, str(event.get("payload_hash") or ""))
    return {
        "source": "canonical",
        "kind": kind,
        "event_id": event["event_id"],
        "event_type": event["event_type"],
        "entity_type": event["entity_type"],
        "entity_id": event["entity_id"],
        "observed_at": event["observed_at"],
        "payload": payload,
    }


def _read_fact_event(conn: Any, kind: str, event_id: str) -> dict[str, Any] | None:
    """Read one immutable event by its exact event id.

    This is intentionally separate from logical-key resolution: a learning
    review revision must remain addressable even when the default read path
    selects a newer revision for the same review id.
    """
    event = _event_row(conn, event_id)
    if event is None or event.get("event_type") != EVENT_TYPE[kind]:
        return None
    try:
        payload = read_payload(conn, str(event.get("payload_hash") or ""))
    except Exception:
        return None
    return {
        "source": "canonical",
        "kind": kind,
        "event_id": event["event_id"],
        "event_type": event["event_type"],
        "entity_type": event["entity_type"],
        "entity_id": event["entity_id"],
        "observed_at": event["observed_at"],
        "payload": payload,
    }


def read_review_event(conn: Any, event_id: str) -> dict[str, Any] | None:
    """Read a historical review revision by its immutable event id."""
    return _read_fact_event(conn, "review", event_id)


def _review_event_rows(
    conn: Any,
    review_id: str | None = None,
    *,
    latest_only: bool = True,
    limit: int = 0,
) -> list[dict[str, Any]]:
    """Read review event metadata with latest selection in SQL.

    The default path returns one row per logical review using the same
    ``(observed_at, event_id)`` ordering as the resolver.  ``latest_only=False``
    is reserved for metadata-only lineage fallback; it never restores old
    payload blobs.
    """
    if not _canonical_ready(conn):
        return []
    clauses = ["e.event_type='trade_review'"]
    params: list[Any] = []
    if review_id is not None:
        clauses.append("e.entity_id=?")
        params.append(str(review_id))
    ranked = (
        "WITH ranked_reviews AS ("
        "SELECT e.event_id, e.entity_id, e.payload_hash, e.observed_at, "
        "COUNT(*) OVER (PARTITION BY e.entity_id) AS candidate_count, "
        "ROW_NUMBER() OVER (PARTITION BY e.entity_id "
        "ORDER BY e.observed_at DESC, e.event_id DESC) AS review_rank "
        "FROM canonical_v2.event e WHERE "
        + " AND ".join(clauses)
        + ") SELECT event_id, entity_id, payload_hash, observed_at, candidate_count "
        "FROM ranked_reviews "
    )
    if latest_only:
        ranked += "WHERE review_rank=1 "
    ranked += "ORDER BY observed_at ASC, event_id ASC"
    if limit and int(limit) > 0:
        ranked += " LIMIT ?"
        params.append(int(limit))
    rows = conn.execute(
        _sql(
            conn,
            ranked,
        ),
        tuple(params),
    ).fetchall()
    return [item for item in (_dict_row(row, _REVIEW_EVENT_COLUMNS) for row in rows) if item]


def latest_review_observed_at_by_id(
    conn: Any,
    review_ids: Iterable[str],
) -> dict[str, float]:
    """Return latest canonical observation time for each requested review.

    Consumers that only need freshness metadata must not resolve each review
    through ``review_row``: that path decodes one canonical payload per ID.
    This projection reads the authoritative event timestamps in one query and
    leaves payload resolution to callers that actually need review content.
    """

    ids = list(dict.fromkeys(str(item) for item in review_ids if str(item)))
    if not ids or not _canonical_ready(conn):
        return {}
    placeholders = ",".join("?" for _ in ids)
    try:
        rows = conn.execute(
            _sql(
                conn,
                f"""
                SELECT entity_id, MAX(observed_at) AS observed_at
                FROM canonical_v2.event
                WHERE event_type='trade_review'
                  AND entity_id IN ({placeholders})
                GROUP BY entity_id
                """,
            ),
            tuple(ids),
        ).fetchall()
    except Exception:
        return {}
    return {
        str(row["entity_id"]): observed_epoch(row["observed_at"])
        for row in rows
        if str(row["entity_id"] or "")
        and observed_epoch(row["observed_at"]) > 0.0
    }


def _review_event_sort_key(event: Mapping[str, Any]) -> tuple[float, str]:
    # No explicit ``revision`` field exists in either the live writer or the
    # learning revision writer.  Both set canonical event.observed_at to the
    # review's observation/revision timestamp, so it is the authority; event
    # id is the deterministic tie-breaker for same-time revisions.
    return (observed_epoch(event.get("observed_at")), str(event.get("event_id") or ""))


def _review_resolution_from_rows(
    conn: Any,
    review_id: str,
    rows: Iterable[Mapping[str, Any]],
) -> tuple[dict[str, Any] | None, Mapping[str, Any] | None, dict[str, Any]]:
    candidates = sorted(
        (dict(row) for row in rows if str(row.get("entity_id") or "") == str(review_id)),
        key=_review_event_sort_key,
    )
    candidate_count = max(
        (int(row.get("candidate_count") or 0) for row in candidates),
        default=len(candidates),
    )
    evidence: dict[str, Any] = {
        "resolver": _REVIEW_RESOLVER,
        "order": _REVIEW_ORDER,
        "revision_source": "event.observed_at",
        "tie_breaker": "event.event_id",
        "review_id": str(review_id),
        "candidate_count": candidate_count,
        "invalid_candidates": [],
        "selected_event_id": "",
        "selected_observed_at": None,
        "status": "missing" if not candidates else "unresolved",
    }
    if not candidates:
        evidence["reason"] = "review_not_found"
        return None, None, evidence

    # The newest candidate is authoritative even when malformed.  Falling
    # back to an older payload would silently resurrect stale review facts.
    selected = candidates[-1]
    payload: Any = None
    try:
        payload = read_payload(conn, str(selected.get("payload_hash") or ""))
    except Exception as exc:
        evidence["invalid_candidates"].append(
            {
                "event_id": str(selected.get("event_id") or ""),
                "reason": "payload_unreadable",
                "error_type": type(exc).__name__,
            }
        )
        evidence["selected_event_id"] = str(selected.get("event_id") or "")
        evidence["selected_observed_at"] = selected.get("observed_at")
        evidence["status"] = "fail_closed"
        evidence["reason"] = "latest_revision_payload_unreadable"
        return None, None, evidence
    if not isinstance(payload, Mapping):
        evidence["invalid_candidates"].append(
            {
                "event_id": str(selected.get("event_id") or ""),
                "reason": "payload_not_object",
            }
        )
        evidence["selected_event_id"] = str(selected.get("event_id") or "")
        evidence["selected_observed_at"] = selected.get("observed_at")
        evidence["status"] = "fail_closed"
        evidence["reason"] = "latest_revision_payload_not_object"
        return None, None, evidence
    payload_review_id = str(payload.get("review_id") or "")
    # The current writers include review_id in every payload.  A few older
    # SQLite fixtures used a descriptive entity id (``review-r1``) while the
    # payload retained ``r1``; preserve that read-only compatibility only for
    # the deterministic live event id. Learning revisions must match exactly.
    live_fixture_alias = (
        str(selected.get("event_id") or "").startswith("live_review_")
        and bool(payload_review_id)
        and str(review_id).endswith(f"-{payload_review_id}")
    )
    if not payload_review_id or (
        payload_review_id != str(review_id) and not live_fixture_alias
    ):
        evidence["invalid_candidates"].append(
            {
                "event_id": str(selected.get("event_id") or ""),
                "reason": "payload_review_id_mismatch",
                "payload_review_id": payload_review_id,
            }
        )
        evidence["selected_event_id"] = str(selected.get("event_id") or "")
        evidence["selected_observed_at"] = selected.get("observed_at")
        evidence["status"] = "fail_closed"
        evidence["reason"] = "latest_revision_identity_mismatch"
        return None, None, evidence

    evidence["selected_event_id"] = str(selected.get("event_id") or "")
    evidence["selected_observed_at"] = selected.get("observed_at")
    evidence["status"] = "selected"
    evidence["reason"] = "latest_observed_at_then_event_id"
    return dict(selected), payload, evidence


def review_resolution_evidence(conn: Any, review_id: str) -> dict[str, Any]:
    """Expose deterministic latest-review selection evidence without mutation."""
    _, _, evidence = _review_resolution_from_rows(
        conn,
        str(review_id or ""),
        _review_event_rows(conn, review_id, latest_only=True),
    )
    return evidence


def resolve_latest_review(conn: Any, review_id: str) -> dict[str, Any] | None:
    """Resolve the latest review event, without hiding immutable revisions."""
    event, _, _ = _review_resolution_from_rows(
        conn,
        str(review_id or ""),
        _review_event_rows(conn, review_id, latest_only=True),
    )
    return event


def read_review(conn: Any, review_id: str) -> dict[str, Any] | None:
    review_id = str(review_id or "")
    event, payload, evidence = _review_resolution_from_rows(
        conn,
        review_id,
        _review_event_rows(conn, review_id, latest_only=True),
    )
    if event is None:
        return None
    if not isinstance(payload, Mapping):
        return None
    return {
        "source": "canonical",
        "kind": "review",
        "event_id": event["event_id"],
        "event_type": "trade_review",
        "entity_type": "review",
        "entity_id": event["entity_id"],
        "observed_at": event["observed_at"],
        "payload": dict(payload),
        "resolution": evidence,
    }


def read_decision(conn: Any, decision_id: str) -> dict[str, Any] | None:
    return _read_fact(conn, "decision", decision_id)


def read_order_event(conn: Any, event_id: str) -> dict[str, Any] | None:
    return _read_fact(conn, "order", event_id)


def read_position_event(conn: Any, event_id: str) -> dict[str, Any] | None:
    return _read_fact(conn, "position", event_id)


def iter_reviews(conn: Any, limit: int = 200) -> list[dict[str, Any]]:
    rows = _review_event_rows(conn, latest_only=True, limit=limit)
    out: list[dict[str, Any]] = []
    for row in rows:
        review_id = str(row.get("entity_id") or "")
        event, payload, evidence = _review_resolution_from_rows(conn, review_id, [row])
        if event is None:
            continue
        if not isinstance(payload, Mapping):
            continue
        out.append(
            {
                "review_id": review_id,
                "event_id": str(event.get("event_id") or ""),
                "source": "canonical",
                "payload": dict(payload),
                "observed_at": event.get("observed_at"),
                "resolution": evidence,
            }
        )
    out.sort(key=lambda item: (observed_epoch(item.get("observed_at")), str(item.get("event_id") or "")))
    if limit and int(limit) > 0:
        return out[: int(limit)]
    return out


def iter_decisions(
    conn: Any,
    limit: int = 0,
    batch_size: int = 2000,
    *,
    min_observed_epoch: float | None = None,
    max_observed_epoch: float | None = None,
    reverse: bool = False,
) -> Iterator[dict[str, Any]]:
    """Stream risk_decision events with bounded keyset pagination.

    ``min_observed_epoch`` / ``max_observed_epoch`` bound the stream by
    ``observed_at`` (window pruning), and ``reverse`` streams newest-first so
    "latest N" consumers stop early.  Ordering is always
    ``(observed_at, event_id)``, ASC by default or DESC in reverse mode.
    """
    if not _canonical_ready(conn):
        return
    clauses = ["e.event_type='risk_decision'"]
    params: list[Any] = []
    if min_observed_epoch is not None:
        clauses.append("e.observed_at >= ?")
        params.append(_db_time(conn, float(min_observed_epoch)))
    if max_observed_epoch is not None:
        clauses.append("e.observed_at <= ?")
        params.append(_db_time(conn, float(max_observed_epoch)))
    where = " WHERE " + " AND ".join(clauses)
    direction = "DESC" if reverse else "ASC"
    order_sql = f" ORDER BY e.observed_at {direction}, e.event_id {direction}"
    if limit and int(limit) > 0:
        rows = conn.execute(
            _sql(
                conn,
                "SELECT e.event_id, e.entity_id, e.payload_hash, e.observed_at "
                "FROM canonical_v2.event e" + where + order_sql + " LIMIT ?",
            ),
            (*params, int(limit)),
        ).fetchall()
        for row in rows:
            yield _decision_record(conn, row)
        return
    last_observed_at: Any = None
    last_event_id: str = ""
    batch = max(1, int(batch_size))
    while True:
        cursor_clauses = list(clauses)
        cursor_params: list[Any] = list(params)
        if last_observed_at is not None:
            operator = "<" if reverse else ">"
            cursor_clauses.append(f"(e.observed_at, e.event_id) {operator} (?, ?)")
            cursor_params.extend([last_observed_at, last_event_id])
        rows = conn.execute(
            _sql(
                conn,
                "SELECT e.event_id, e.entity_id, e.payload_hash, e.observed_at "
                "FROM canonical_v2.event e WHERE "
                + " AND ".join(cursor_clauses)
                + order_sql
                + " LIMIT ?",
            ),
            (*cursor_params, batch),
        ).fetchall()
        if not rows:
            break
        for row in rows:
            yield _decision_record(conn, row)
        last_observed_at = rows[-1]["observed_at"]
        last_event_id = str(rows[-1]["event_id"] or "")


def _decision_record(conn: Any, row: Any) -> dict[str, Any]:
    return {
        "decision_id": str(row["entity_id"] or ""),
        "event_id": str(row["event_id"] or ""),
        "source": "canonical",
        "payload": read_payload(conn, str(row["payload_hash"])),
    }


def canonical_fact_observation(
    conn: Any,
    kind: str,
    timestamp_columns: tuple[str, ...] | None = None,
) -> dict[str, Any] | None:
    """Return ``{'count', 'latest'}`` from the canonical event stream only.

    ``latest`` is the raw ``observed_at`` value (datetime on PostgreSQL).
    ``None`` means the canonical event stream is not reachable.
    """
    event_type = EVENT_TYPE[kind]
    try:
        row = conn.execute(
            _sql(
                conn,
                "SELECT COUNT(*) AS n, MAX(observed_at) AS m "
                "FROM canonical_v2.event WHERE event_type=?",
            ),
            (event_type,),
        ).fetchone()
    except Exception:
        return None
    if row is None:
        return None
    count = row["n"] if isinstance(row, Mapping) else row[0]
    latest = row["m"] if isinstance(row, Mapping) else row[1]
    return {"count": int(count or 0), "latest": latest}


def read_trade_chain(conn: Any, review_id: str) -> dict[str, Any] | None:
    """Review + entry decision via derived_from, plus the decision's caused_by
    order/position events (broker_execution / position_transition)."""
    review = read_review(conn, review_id)
    if review is None:
        return None
    chain: dict[str, Any] = {"review": review, "decision": None, "orders": [], "positions": []}
    if not review.get("event_id"):
        return chain
    decision_event_id: str | None = None
    # Learning revisions intentionally carry a new immutable event id but do
    # not rewrite lineage.  Prefer a relation on the selected revision, then
    # deterministically inspect older revisions for the original chain.
    review_event_ids = [str(review["event_id"])]
    for event in reversed(_review_event_rows(conn, review_id, latest_only=False)):
        event_id = str(event.get("event_id") or "")
        if event_id and event_id not in review_event_ids:
            review_event_ids.append(event_id)
    derived: list[Any] = []
    for review_event_id in review_event_ids:
        derived = conn.execute(
            _sql(
                conn,
                "SELECT to_event_id FROM canonical_v2.event_relation "
                "WHERE from_event_id=? AND relation_type='derived_from' "
                "ORDER BY to_event_id ASC",
            ),
            (review_event_id,),
        ).fetchall()
        if derived:
            break
    if derived:
        decision_event_id = str(derived[0]["to_event_id"] or "")
        chain["decision"] = _related_payload(conn, decision_event_id, "risk_decision")
    if decision_event_id:
        for rel in conn.execute(
            _sql(
                conn,
                "SELECT from_event_id FROM canonical_v2.event_relation "
                "WHERE to_event_id=? AND relation_type='caused_by'",
            ),
            (decision_event_id,),
        ).fetchall():
            fe = _related_payload(conn, str(rel["from_event_id"] or ""), "")
            if fe["event_type"] == "broker_execution":
                chain["orders"].append(fe)
            elif fe["event_type"] == "position_transition":
                chain["positions"].append(fe)
    return chain


def _related_payload(conn: Any, event_id: str, expected_type: str) -> dict[str, Any]:
    event = _event_row(conn, event_id)
    payload = read_payload(conn, str((event or {}).get("payload_hash") or "")) if event else None
    return {
        "event_id": event_id,
        "event_type": (event or {}).get("event_type") or "",
        "entity_id": (event or {}).get("entity_id") or "",
        "payload": payload,
    }


def summarize_reader_sources(conn: Any, review_ids: Iterable[str]) -> dict[str, Any]:
    """Report how many requested reviews resolve through canonical mapping (coverage)."""
    resolved = 0
    total = 0
    for review_id in review_ids:
        total += 1
        if _resolve(conn, "review", review_id) is not None:
            resolved += 1
    return {"requested": total, "resolved_canonical": resolved, "gap": total - resolved}


def _epoch_seconds(value: Any) -> float:
    epoch = observed_epoch(value)
    return epoch if epoch and epoch > 0 else 0.0


def _review_to_row(record: Mapping[str, Any]) -> dict[str, Any]:
    """Shape a canonical review record like the historical review row."""
    payload = record.get("payload") if isinstance(record, Mapping) else None
    row = dict(payload) if isinstance(payload, dict) else {}
    row["review_id"] = str(row.get("review_id") or record.get("review_id") or "")
    row["canonical_event_id"] = str(record.get("event_id") or "")
    row["canonical_observed_at"] = record.get("observed_at")
    if isinstance(record.get("resolution"), Mapping):
        row["canonical_resolution"] = dict(record["resolution"])
    if "created_at" in row:
        row["created_at"] = _epoch_seconds(row["created_at"])
    if isinstance(row.get("failure_tags"), list):
        row["failure_tags_json"] = json.dumps(row["failure_tags"])
    nested_review = row.get("review")
    row["review_json"] = nested_review if isinstance(nested_review, dict) else {}
    return row


def _json_column(value: Any) -> Any:
    """Serialize a nested JSON value into the legacy text-column shape."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _order_to_row(record: Mapping[str, Any]) -> dict[str, Any]:
    """Shape a canonical order record like the historical order row."""
    payload = record.get("payload") if isinstance(record, Mapping) else None
    row = dict(payload) if isinstance(payload, dict) else {}
    row["event_id"] = str(row.get("event_id") or record.get("event_id") or "")
    for field in ("event_ts", "created_at"):
        if field in row:
            row[field] = _epoch_seconds(row[field])
    row.pop("details", None)
    row["details_json"] = _json_column(payload.get("details") if isinstance(payload, dict) else None)
    return row


def _position_to_row(record: Mapping[str, Any]) -> dict[str, Any]:
    """Shape a canonical position record like the historical position row."""
    payload = record.get("payload") if isinstance(record, Mapping) else None
    row = dict(payload) if isinstance(payload, dict) else {}
    row["event_id"] = str(row.get("event_id") or record.get("event_id") or "")
    for field in ("event_ts", "created_at"):
        if field in row:
            row[field] = _epoch_seconds(row[field])
    row.pop("details", None)
    row["details_json"] = _json_column(payload.get("details") if isinstance(payload, dict) else None)
    return row


def _decision_to_row(record: Mapping[str, Any]) -> dict[str, Any]:
    """Shape a canonical decision record like the historical decision row."""
    payload = record.get("payload") if isinstance(record, Mapping) else None
    row = dict(payload) if isinstance(payload, dict) else {}
    row["decision_id"] = str(row.get("decision_id") or record.get("decision_id") or "")
    if "decision_ts" in row:
        row["decision_ts"] = _epoch_seconds(row["decision_ts"])
    if "created_at" in row:
        row["created_at"] = _epoch_seconds(row["created_at"])
    for json_column, key in (
        ("portfolio_state_json", "portfolio_state"),
        ("risk_state_json", "risk_state"),
        ("action_json", "action"),
    ):
        value = row.get(key)
        if value is not None:
            row[json_column] = json.dumps(value) if not isinstance(value, str) else value
    return row


def review_row(conn: Any, review_id: str) -> dict[str, Any] | None:
    """Return the historical review row shape from canonical facts."""
    record = read_review(conn, review_id)
    return _review_to_row(record) if record is not None else None


def decision_row(conn: Any, decision_id: str) -> dict[str, Any] | None:
    """Return the historical decision row shape from canonical facts."""
    record = read_decision(conn, decision_id)
    return _decision_to_row(record) if record is not None else None


def iter_review_rows(conn: Any, limit: int = 100) -> list[dict[str, Any]]:
    return [_review_to_row(record) for record in iter_reviews(conn, int(limit) if limit and int(limit) > 0 else 0)]


def iter_review_rows_desc(conn: Any, limit: int = 100) -> list[dict[str, Any]]:
    """Newest-first historical review rows from the canonical stream.

    The canonical event stream is observed-ASC; consumers that need the
    newest N reviews (``ORDER BY created_at DESC LIMIT ?`` semantics)
    sort the full stream once (721 rows) and truncate.
    """
    rows = iter_review_rows(conn, limit=0)
    rows.sort(
        key=lambda r: (
            observed_epoch(r.get("canonical_observed_at")),
            str(r.get("canonical_event_id") or ""),
        ),
        reverse=True,
    )
    if limit and int(limit) > 0:
        return rows[: int(limit)]
    return rows


def iter_decision_rows(
    conn: Any,
    limit: int = 0,
    *,
    min_observed_epoch: float | None = None,
    max_observed_epoch: float | None = None,
    reverse: bool = False,
) -> Iterator[dict[str, Any]]:
    for record in iter_decisions(
        conn,
        limit=limit,
        min_observed_epoch=min_observed_epoch,
        max_observed_epoch=max_observed_epoch,
        reverse=reverse,
    ):
        yield _decision_to_row(record)


def iter_fact_events(
    conn: Any,
    kind: str,
    *,
    entity_id: str = "",
    legacy_event_type: str = "",
    limit: int = 0,
) -> list[dict[str, Any]]:
    """Iterate fact events (order/position) by entity id with payload restore.

    Returns a list of records shaped like ``read_*_event`` results so callers
    can select by payload fields. Entity filters are applied after payload
    restore because the canonical event identity is the lifecycle event id,
    while consumers commonly filter by the payload's position/trade id.
    """
    if not _canonical_ready(conn):
        return []
    event_type = EVENT_TYPE[kind]
    bound = f" LIMIT {int(limit)}" if limit and int(limit) > 0 and not entity_id else ""
    rows = conn.execute(
        _sql(
            conn,
            "SELECT e.event_id, e.event_type, e.entity_type, e.entity_id, "
            "e.payload_hash, e.observed_at FROM canonical_v2.event e "
            "WHERE e.event_type=? ORDER BY e.observed_at ASC, e.event_id ASC" + bound,
        ),
        (event_type,),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        payload = read_payload(conn, str(row["payload_hash"]))
        payload = payload if isinstance(payload, dict) else {}
        payload_entity_id = str(
            (payload.get("position_id") if kind == "position" else payload.get("trade_id")) or ""
        )
        if entity_id and str(entity_id) not in {
            payload_entity_id,
            str(row["entity_id"] or ""),
        }:
            continue
        if legacy_event_type and str(payload.get("event_type") or "") != str(legacy_event_type):
            continue
        out.append(
            {
                "source": "canonical",
                "kind": kind,
                "event_id": str(row["event_id"] or ""),
                "event_type": str(row["event_type"] or ""),
                "entity_type": str(row["entity_type"] or ""),
                "entity_id": str(row["entity_id"] or ""),
                "observed_at": row["observed_at"],
                "payload": payload,
            }
        )
        if limit and int(limit) > 0 and len(out) >= int(limit):
            break
    return out


def _json_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _json_value(value: Any, default: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _flatten_canonical_event(conn: Any, row: Any, event_type: str) -> dict[str, Any] | None:
    payload = read_payload(conn, str(row["payload_hash"] or ""))
    if not isinstance(payload, Mapping):
        return None
    result = dict(payload)
    canonical_event_id = str(row["event_id"] or "")
    result.setdefault("event_id", canonical_event_id)
    result["canonical_event_id"] = canonical_event_id
    result["event_type"] = event_type
    result.setdefault("entity_type", str(row["entity_type"] or ""))
    result.setdefault("entity_id", str(row["entity_id"] or ""))
    result.setdefault("observed_at", row["observed_at"])
    result.setdefault("created_at", row.get("created_at") if isinstance(row, Mapping) else None)
    if not result.get("created_at"):
        result["created_at"] = result.get("observed_at")
    for value_key, json_key, default in (
        ("context", "context_json", {}),
        ("verdict", "verdict_json", {}),
        ("risk_verdict", "risk_verdict_json", {}),
        ("execution", "execution_json", {}),
        ("horizons", "horizons_json", []),
        ("evidence", "evidence_json", {}),
    ):
        if json_key not in result and value_key in result:
            result[json_key] = _json_text(result[value_key])
        if value_key not in result and json_key in result:
            result[value_key] = _json_value(result[json_key], default)
    result["source"] = "canonical"
    return result


def _iter_canonical_payload_events(
    conn: Any,
    event_type: str,
    *,
    entity_type: str = "",
    limit: int = 0,
    reverse: bool = True,
    filters: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Read and flatten one canonical event stream without another source."""
    if not _canonical_ready(conn):
        return []
    expected = {str(key): str(value) for key, value in (filters or {}).items() if value not in (None, "")}
    direction = "DESC" if reverse else "ASC"
    bound = f" LIMIT {int(limit)}" if limit and int(limit) > 0 and not expected else ""
    entity_clause = " AND e.entity_type=?" if entity_type else ""
    query_params: list[Any] = [event_type]
    if entity_type:
        query_params.append(str(entity_type))
    rows = conn.execute(
        _sql(
            conn,
            "SELECT e.event_id, e.event_type, e.entity_type, e.entity_id, "
            "e.payload_hash, e.observed_at, e.created_at "
            "FROM canonical_v2.event e WHERE e.event_type=?"
            + entity_clause + " "
            f"ORDER BY e.observed_at {direction}, e.event_id {direction}" + bound,
        ),
        tuple(query_params),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        flattened = _flatten_canonical_event(conn, row, event_type)
        if flattened is None:
            continue
        if any(str(flattened.get(key) or "") != value for key, value in expected.items()):
            continue
        out.append(flattened)
        if limit and int(limit) > 0 and len(out) >= int(limit):
            break
    return out


def iter_parameter_template_lifecycle_rows(
    conn: Any,
    limit: int = 0,
    *,
    factor_id: str = "",
    candidate_id: str = "",
    reverse: bool = True,
) -> list[dict[str, Any]]:
    """Return parameter-template lifecycle facts from canonical governance events."""

    events = _iter_canonical_payload_events(
        conn,
        "governance_effect",
        entity_type="parameter_template_lifecycle",
        limit=limit,
        reverse=reverse,
        filters={"factor_id": factor_id, "candidate_id": candidate_id},
    )
    rows: list[dict[str, Any]] = []
    for item in events:
        rows.append(
            {
                "id": str(item.get("canonical_event_id") or item.get("event_id") or ""),
                "timestamp": observed_epoch(item.get("observed_at")),
                "event": str(item.get("event") or ""),
                "factor": str(item.get("factor_id") or item.get("factor") or ""),
                # ``_flatten_canonical_event`` marks the read authority as
                # ``canonical``. This domain reader must expose the business
                # source so API consumers can retain their governance
                # semantics without inspecting payload blobs themselves.
                "source": str(item.get("source") or "parameter_template")
                if str(item.get("source") or "") != "canonical"
                else "parameter_template",
                "description": str(item.get("description") or ""),
                "score": item.get("score", 0.0),
                "status": str(item.get("status") or ""),
                "reason": str(item.get("reason") or ""),
                "candidate_id": str(item.get("candidate_id") or ""),
                "template_id": str(item.get("template_id") or ""),
                "regime_key": str(item.get("regime_key") or ""),
                "details": item.get("details") if isinstance(item.get("details"), Mapping) else {},
                "canonical_event_id": str(item.get("canonical_event_id") or ""),
            }
        )
    return rows


def iter_factor_lifecycle_rows(
    conn: Any,
    limit: int = 0,
    *,
    factor: str = "",
    reverse: bool = True,
) -> list[dict[str, Any]]:
    """Return factor lifecycle facts from the canonical observation stream."""

    rows = _iter_canonical_payload_events(
        conn,
        "factor_observation",
        entity_type="factor_lifecycle",
        limit=limit,
        reverse=reverse,
        filters={"factor": factor},
    )
    for row in rows:
        row["source"] = str(row.get("factor_source") or row.get("source") or "")
    return rows


def iter_supervisor_trace_events(
    conn: Any,
    limit: int = 0,
    *,
    position_id: str = "",
    decision_id: str = "",
    trace_id: str = "",
    action: str = "",
    stage: str = "",
    outcome: str = "",
    reverse: bool = True,
) -> list[dict[str, Any]]:
    """Return flattened canonical ``supervisor_trace`` events."""
    return _iter_canonical_payload_events(
        conn,
        "supervisor_trace",
        limit=limit,
        reverse=reverse,
        filters={
            "position_id": position_id,
            "decision_id": decision_id,
            "trace_id": trace_id,
            "action": action,
            "stage": stage,
            "outcome": outcome,
        },
    )


def iter_supervisor_trace_rows(
    conn: Any,
    limit: int = 0,
    *,
    position_id: str = "",
    decision_id: str = "",
    trace_id: str = "",
    action: str = "",
    stage: str = "",
    outcome: str = "",
    reverse: bool = True,
) -> list[dict[str, Any]]:
    """Return canonical supervisor traces in the historical row shape."""
    return iter_supervisor_trace_events(
        conn,
        limit=limit,
        position_id=position_id,
        decision_id=decision_id,
        trace_id=trace_id,
        action=action,
        stage=stage,
        outcome=outcome,
        reverse=reverse,
    )


def iter_counterfactual_rows(
    conn: Any,
    limit: int = 0,
    *,
    position_id: str = "",
    review_id: str = "",
    counterfactual_id: str = "",
    label: str = "",
    reverse: bool = True,
) -> list[dict[str, Any]]:
    """Return flattened canonical ``counterfactual_review`` rows."""
    rows = _iter_canonical_payload_events(
        conn,
        "counterfactual_review",
        # A counterfactual can have multiple immutable maturity versions.  A
        # consumer sees only the newest version for each logical id; apply the
        # requested limit after that deduplication.
        limit=0,
        reverse=reverse,
        filters={
            "position_id": position_id,
            "review_id": review_id,
            "counterfactual_id": counterfactual_id,
            "label": label,
        },
    )
    latest: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = str(row.get("counterfactual_id") or row.get("entity_id") or "")
        if key and key not in latest:
            latest[key] = row
    result = list(latest.values())
    if limit and int(limit) > 0:
        return result[: int(limit)]
    return result


def order_row(conn: Any, event_id: str) -> dict[str, Any] | None:
    """Return the historical order row shape from canonical facts."""
    record = read_order_event(conn, event_id)
    return _order_to_row(record) if record is not None else None


def position_row(conn: Any, event_id: str) -> dict[str, Any] | None:
    """Return the historical position row shape from canonical facts."""
    record = read_position_event(conn, event_id)
    return _position_to_row(record) if record is not None else None


def iter_order_rows(conn: Any, limit: int = 100) -> list[dict[str, Any]]:
    return [_order_to_row(record) for record in iter_fact_events(conn, "order", limit=limit)]


def iter_position_rows(conn: Any, limit: int = 100) -> list[dict[str, Any]]:
    return [_position_to_row(record) for record in iter_fact_events(conn, "position", limit=limit)]


POSITION_DECISION_INDEX_SCHEMA = "position_decision_index.v1"


def load_position_decision_index(
    path: str | Path,
) -> dict[str, dict[str, Any]] | None:
    """Load the materialized position->entry decision index file.

    Returns ``{position_id: {decision_id, parent_decision_id, decision_ts,
    timeframe, event_id}}`` or None when the file is missing or invalid.
    Never writes.
    """
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    entries = payload.get("entries")
    if not isinstance(entries, dict):
        return None
    result: dict[str, dict[str, Any]] = {}
    for position_id, entry in entries.items():
        if isinstance(entry, dict):
            result[str(position_id)] = {
                "decision_id": str(entry.get("decision_id") or ""),
                "parent_decision_id": str(entry.get("parent_decision_id") or ""),
                "decision_ts": float(entry.get("decision_ts") or 0.0),
                "timeframe": str(entry.get("timeframe") or ""),
                "event_id": str(entry.get("event_id") or ""),
            }
    return result or None


# ---------------------------------------------------------------------------
# training_sample_row readers (P1 收尾 — 样本域读取单轨)
# ---------------------------------------------------------------------------

TRAINING_SAMPLE_COLUMNS = (
    "sample_id", "sample_type", "source_table", "source_id",
    "decision_id", "trade_id", "position_id", "symbol", "timeframe",
    "event_ts", "label_status", "integrity", "train_weight",
    "features_json", "verdict_json", "label_json", "trace_json",
    "evidence_contract_json", "config_version", "config_hash",
    "evolution_run_id", "system_contaminated", "governance_eligible",
    "governance_effective_weight", "governance_eligibility_version",
    "governance_ineligible_reason", "governance_eligibility_fingerprint",
    "content_fingerprint", "created_at", "updated_at",
)


def iter_training_sample_rows(
    conn: Any,
    *,
    limit: int = 0,
    sample_type: str | None = None,
    label_status: str | None = None,
    min_updated_at: float | None = None,
    governance_eligible: int | None = None,
    min_governance_weight: float | None = None,
    governance_eligibility_version: str | None = None,
    governance_eligibility_fingerprint_not_empty: bool = False,
    system_contaminated: int | None = None,
    decision_id: str | None = None,
    trade_id: str | None = None,
    position_id: str | None = None,
    order_by_event_ts: bool = False,
) -> list[dict[str, Any]]:
    """Iterate canonical_v2.training_sample_row in its consumer row shape."""
    where_clauses: list[str] = []
    params: list[Any] = []
    if sample_type:
        where_clauses.append("sample_type = ?")
        params.append(sample_type)
    if label_status:
        where_clauses.append("label_status = ?")
        params.append(label_status)
    if min_updated_at is not None:
        where_clauses.append("updated_at >= ?")
        params.append(min_updated_at)
    if governance_eligible is not None:
        where_clauses.append("governance_eligible = ?")
        params.append(governance_eligible)
    if min_governance_weight is not None:
        where_clauses.append("governance_effective_weight > ?")
        params.append(min_governance_weight)
    if governance_eligibility_version:
        where_clauses.append("governance_eligibility_version = ?")
        params.append(governance_eligibility_version)
    if governance_eligibility_fingerprint_not_empty:
        where_clauses.append("governance_eligibility_fingerprint <> ''")
    if system_contaminated is not None:
        where_clauses.append("system_contaminated = ?")
        params.append(system_contaminated)
    if decision_id:
        where_clauses.append("decision_id = ?")
        params.append(decision_id)
    if trade_id:
        where_clauses.append("trade_id = ?")
        params.append(trade_id)
    if position_id:
        where_clauses.append("position_id = ?")
        params.append(str(position_id))
    where = (" WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
    order = " ORDER BY event_ts DESC, created_at DESC" if order_by_event_ts else " ORDER BY created_at DESC"
    select_cols = ", ".join(TRAINING_SAMPLE_COLUMNS)
    q = f"SELECT {select_cols} FROM canonical_v2.training_sample_row{where}{order}"
    if limit and int(limit) > 0:
        q += f" LIMIT {int(limit)}"
    try:
        rows = conn.execute(_sql(conn, q), tuple(params)).fetchall()
        return [r for r in (_dict_row(x, TRAINING_SAMPLE_COLUMNS) for x in rows) if r]
    except Exception:
        return []


def get_training_sample_row(conn: Any, sample_id: str) -> dict[str, Any] | None:
    """Get a single training_sample_row by sample_id (canonical only)."""
    try:
        row = conn.execute(
            _sql(conn, f"SELECT {', '.join(TRAINING_SAMPLE_COLUMNS)} FROM canonical_v2.training_sample_row WHERE sample_id=? LIMIT 1"),
            (str(sample_id),),
        ).fetchone()
    except Exception:
        return None
    return _dict_row(row, TRAINING_SAMPLE_COLUMNS)


# ── canonical decision factor snapshot derivation ─────────────────────────


def _parse_factor_snapshots(payload: Any) -> list[dict[str, Any]]:
    """Extract per-factor snapshot rows from a canonical decision payload.

    Returns an empty list when the payload does not contain factor_snapshots
    (historical decisions written before the field was added).
    """
    if not isinstance(payload, dict):
        return []
    snapshots = payload.get("factor_snapshots")
    if not isinstance(snapshots, list):
        return []
    result: list[dict[str, Any]] = []
    for item in snapshots:
        if not isinstance(item, dict):
            continue
        result.append({
            "decision_id": str(item.get("decision_id", "")),
            "factor": str(item.get("factor", "")),
            "source": str(item.get("source", "")),
            "raw_value": item.get("raw_value"),
            "normalized_value": item.get("normalized_value"),
            "direction": float(item.get("direction", 0.0) or 0.0),
            "base_weight": float(item.get("base_weight", 0.0) or 0.0),
            "policy_weight": float(item.get("policy_weight", 0.0) or 0.0),
            "shadow_score": float(item.get("shadow_score", 0.0) or 0.0),
            "health_score": float(item.get("health_score", 0.0) or 0.0),
            "gated": int(1 if item.get("gated") else 0),
            "gated_reason": str(item.get("gated_reason", "")),
            "contribution_score": float(item.get("contribution_score", 0.0) or 0.0),
            "generation": int(item.get("generation", 0) or 0),
            "artifact_hash": str(item.get("artifact_hash", "")),
            "definition_fingerprint": str(item.get("definition_fingerprint", "")),
            "runtime_selection_fingerprint": str(item.get("runtime_selection_fingerprint", "")),
            "config_hash": str(item.get("config_hash", "")),
            "lineage_status": str(item.get("lineage_status", "")),
        })
    return result


def iter_decision_factor_snapshots(
    conn: Any,
    decision_id: str,
    *,
    order_by_contribution: bool = True,
) -> list[dict[str, Any]]:
    """Return per-factor snapshot rows for a decision.

    Snapshots are derived from the canonical risk-decision payload. A decision
    without embedded snapshots is an empty result, not a signal to query a
    second fact store.
    """
    if not _canonical_ready(conn):
        return []
    try:
        row = conn.execute(
            _sql(
                conn,
                "SELECT payload_hash FROM canonical_v2.event"
                " WHERE entity_id=? AND event_type='risk_decision'"
                " ORDER BY created_at DESC LIMIT 1",
            ),
            (str(decision_id),),
        ).fetchone()
        if row is None:
            return []
        ph = row["payload_hash"] if isinstance(row, Mapping) else row[0]
        snapshots = _parse_factor_snapshots(read_payload(conn, str(ph)))
        if order_by_contribution:
            snapshots.sort(
                key=lambda r: abs(float(r.get("contribution_score") or 0)),
                reverse=True,
            )
        return snapshots
    except Exception:
        return []


def iter_decision_factor_snapshots_by_factor(
    conn: Any,
    factor: str,
    *,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Return recent factor snapshot rows for a given factor name.

    Used by factor_cards and factor_redundancy for per-factor aggregate stats.
    A missing payload field yields no rows.
    """
    if not _canonical_ready(conn):
        return []
    try:
        limit_clause = f" LIMIT {max(1, int(limit)) * 5}" if limit and int(limit) > 0 else ""
        rows = conn.execute(
            _sql(
                conn,
                "SELECT e.payload_hash"
                " FROM canonical_v2.event e"
                " WHERE e.event_type = 'risk_decision'"
                " ORDER BY e.created_at DESC"
                + limit_clause,
            ),
        ).fetchall()
        result: list[dict[str, Any]] = []
        for event_row in rows:
            ph = event_row["payload_hash"] if isinstance(event_row, Mapping) else event_row[0]
            for snapshot in _parse_factor_snapshots(read_payload(conn, str(ph))):
                if str(snapshot.get("factor")) == factor:
                    result.append(snapshot)
            if limit > 0 and len(result) >= limit:
                break
        return result[: int(limit)] if limit and int(limit) > 0 else result
    except Exception:
        return []


def iter_decision_factor_snapshots_by_factors(
    conn: Any,
    factors: Iterable[str],
    *,
    limit: int = 100,
) -> dict[str, list[dict[str, Any]]]:
    """Return recent factor snapshots for several factors in one payload scan.

    The single-factor reader is useful for an isolated card, but running it
    once per live factor re-reads the same decision payloads. Redundancy
    analysis needs the same recent window for every factor, so share one event
    query and decode each payload only once.
    """

    ids = list(dict.fromkeys(str(item) for item in factors if str(item)))
    result = {factor: [] for factor in ids}
    if not ids or not _canonical_ready(conn):
        return result
    try:
        limit_clause = f" LIMIT {max(1, int(limit)) * 5}" if limit and int(limit) > 0 else ""
        rows = conn.execute(
            _sql(
                conn,
                "SELECT e.payload_hash"
                " FROM canonical_v2.event e"
                " WHERE e.event_type = 'risk_decision'"
                " ORDER BY e.created_at DESC"
                + limit_clause,
            ),
        ).fetchall()
        for event_row in rows:
            payload_hash = (
                event_row["payload_hash"]
                if isinstance(event_row, Mapping)
                else event_row[0]
            )
            for snapshot in _parse_factor_snapshots(read_payload(conn, str(payload_hash))):
                factor = str(snapshot.get("factor") or "")
                if factor in result and (
                    not limit or len(result[factor]) < int(limit)
                ):
                    result[factor].append(snapshot)
        if limit and int(limit) > 0:
            return {factor: values[: int(limit)] for factor, values in result.items()}
        return result
    except Exception:
        return result


def count_decision_factor_snapshots(conn: Any, decision_id: str) -> int:
    """Return the count of factor snapshots for a decision."""
    if not _canonical_ready(conn):
        return 0
    try:
        row = conn.execute(
            _sql(
                conn,
                "SELECT payload_hash FROM canonical_v2.event"
                " WHERE entity_id=? AND event_type='risk_decision'"
                " ORDER BY created_at DESC LIMIT 1",
            ),
            (str(decision_id),),
        ).fetchone()
        if row is None:
            return 0
        ph = row["payload_hash"] if isinstance(row, Mapping) else row[0]
        return len(_parse_factor_snapshots(read_payload(conn, str(ph))))
    except Exception:
        return 0


def iter_all_decision_factor_snapshots(
    conn: Any,
    decision_ids: list[str],
) -> list[dict[str, Any]]:
    """Return factor snapshot rows for multiple decisions (batch read)."""
    if not decision_ids:
        return []
    if not _canonical_ready(conn):
        return []
    try:
        placeholders = ",".join(["?"] * len(decision_ids))
        rows = conn.execute(
            _sql(
                conn,
                "SELECT entity_id, payload_hash FROM canonical_v2.event"
                f" WHERE entity_id IN ({placeholders})"
                " AND event_type = 'risk_decision'",
            ),
            tuple(decision_ids),
        ).fetchall()
        result: list[dict[str, Any]] = []
        for event_row in rows:
            ph = event_row["payload_hash"] if isinstance(event_row, Mapping) else event_row[1]
            result.extend(_parse_factor_snapshots(read_payload(conn, str(ph))))
        return result
    except Exception:
        return []
