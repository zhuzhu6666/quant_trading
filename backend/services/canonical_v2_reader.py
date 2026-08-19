#!/usr/bin/env python3
"""Canonical_v2 read-only reader facade (business-layer migration facade).

All read-only adapters that currently touch the four core legacy fact tables
(decision_ledger / order_lifecycle_event / position_lifecycle_event /
trade_outcome_review) may converge onto this facade during the cutover.  The
The facade reads ONLY from canonical_v2:

and falls back to the legacy table for teams/dev environments that still run
SQLite fixtures, marking ``source`` accordingly.  It never writes.

Functions:
    resolve_event(conn, legacy_table, primary_key) -> event row or None
    read_review / read_decision / read_order_event / read_position_event
        (by legacy primary key) -> {"source","event_id","entity_*","payload"}
    iter_reviews(conn, limit) -> records in observed order
    iter_decisions(conn, limit=0) -> bounded decision records
    read_trade_chain(conn, review_id) -> review + entry decision + order/position
        events reachable from canonical relations (derived_from / caused_by)
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

from backend.services.canonical_v2 import _db_time, _is_pg, _sql, read_payload  # noqa: E402
from backend.services.fact_envelope import observed_epoch

LEGACY_TABLE = {
    "review": "state_v1.trade_outcome_review",
    "decision": "state_v1.decision_ledger",
    "order": "state_v1.order_lifecycle_event",
    "position": "state_v1.position_lifecycle_event",
}
LEGACY_PK = {
    "review": "review_id",
    "decision": "decision_id",
    "order": "event_id",
    "position": "event_id",
}
LEGACY_ORDER_COLUMN = {
    "review": "created_at",
    "decision": "decision_ts",
    "order": "event_ts",
    "position": "event_ts",
}

EVENT_TYPE = {
    "review": "trade_review",
    "decision": "risk_decision",
    "order": "broker_execution",
    "position": "position_transition",
}

_ROW = tuple[int, str, str, str]


def _canonical_ready(conn: Any) -> bool:
    """Return True when the canonical_v2 schema/event table is reachable."""
    try:
        conn.execute(_sql(conn, "SELECT 1 FROM canonical_v2.event LIMIT 1")).fetchone()
        return True
    except Exception:
        return False


canonical_ready = _canonical_ready


def _legacy_table_sql(conn: Any, legacy_table: str) -> str:
    """SQLite fixtures keep bare table names; PostgreSQL uses state_v1.*."""
    if _is_pg(conn):
        return legacy_table
    return legacy_table.split(".", 1)[-1]


def _row_mapping(conn: Any, row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    if isinstance(row, Mapping):
        return dict(row)
    keys = getattr(row, "keys", None)
    if callable(keys):
        try:
            names = list(keys())
            return {name: row[name] for name in names}
        except Exception:
            return None
    return None


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


_LIVE_ID_PREFIX: dict[str, str] = {
    "state_v1.decision_ledger": "live_decision_",
    "state_v1.order_lifecycle_event": "live_ordevt_",
    "state_v1.position_lifecycle_event": "live_posevt_",
    "state_v1.trade_outcome_review": "live_review_",
    "state_v1.evolution_decision": "live_evol_",
    "state_v1.governance_mutation_intent": "live_gov_",
}


def _resolve(conn: Any, legacy_table: str, primary_key: str) -> dict[str, Any] | None:
    """Resolve a legacy primary key to its canonical event by the live-event id
    convention (the legacy_mapping table was dropped in S5; R1 removed the
    mapping read path). Consumers read canonical streams via iter_*, and the
    by-key read_* helpers derive the deterministic live event id."""
    if not _canonical_ready(conn):
        return None
    prefix = _LIVE_ID_PREFIX.get(legacy_table)
    if prefix is None:
        return None
    event_id = f"{prefix}{str(primary_key or '')}"
    if _event_row(conn, event_id) is None:
        return None
    return {"event_id": event_id, "payload_hash": None}


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
    return dict(row) if row is not None else None


def resolve_event(conn: Any, legacy_table: str, primary_key: str) -> dict[str, Any] | None:
    resolved = _resolve(conn, legacy_table, primary_key)
    if resolved is None:
        return None
    return _event_row(conn, resolved["event_id"])


def _legacy_fact(conn: Any, kind: str, primary_key: str) -> dict[str, Any] | None:
    """Read a legacy occurrence directly for SQLite fixtures / pre-cutover."""
    legacy_table = LEGACY_TABLE[kind]
    pk_column = LEGACY_PK[kind]
    table_sql = _legacy_table_sql(conn, legacy_table)
    try:
        row = conn.execute(
            _sql(conn, f"SELECT * FROM {table_sql} WHERE {pk_column}=? LIMIT 1"),
            (str(primary_key or ""),),
        ).fetchone()
    except Exception:
        return None
    if row is None:
        return None
    return {
        "source": "legacy",
        "kind": kind,
        "legacy_table": legacy_table,
        "legacy_primary_key": str(primary_key),
        "event_id": "",
        "event_type": "",
        "entity_type": "",
        "entity_id": "",
        "observed_at": None,
        "payload": _row_mapping(conn, row) or {},
    }


def _read_fact(conn: Any, kind: str, primary_key: str) -> dict[str, Any] | None:
    legacy_table = LEGACY_TABLE[kind]
    if not _canonical_ready(conn):
        return _legacy_fact(conn, kind, primary_key)
    resolved = _resolve(conn, legacy_table, primary_key)
    if resolved is None:
        return _legacy_fact(conn, kind, primary_key)
    event = _event_row(conn, resolved["event_id"])
    if event is None:
        return None
    payload = read_payload(conn, str(event.get("payload_hash") or ""))
    return {
        "source": "canonical",
        "kind": kind,
        "legacy_table": legacy_table,
        "legacy_primary_key": str(primary_key),
        "event_id": event["event_id"],
        "event_type": event["event_type"],
        "entity_type": event["entity_type"],
        "entity_id": event["entity_id"],
        "observed_at": event["observed_at"],
        "payload": payload,
    }


def read_review(conn: Any, review_id: str) -> dict[str, Any] | None:
    return _read_fact(conn, "review", review_id)


def read_decision(conn: Any, decision_id: str) -> dict[str, Any] | None:
    return _read_fact(conn, "decision", decision_id)


def read_order_event(conn: Any, event_id: str) -> dict[str, Any] | None:
    return _read_fact(conn, "order", event_id)


def read_position_event(conn: Any, event_id: str) -> dict[str, Any] | None:
    return _read_fact(conn, "position", event_id)


def iter_reviews(conn: Any, limit: int = 200) -> list[dict[str, Any]]:
    if not _canonical_ready(conn):
        return [
            {
                "review_id": str(row.get("review_id") or ""),
                "event_id": "",
                "source": "legacy",
                "payload": row,
            }
            for row in _legacy_rows(conn, "review", limit)
        ]
    query = (
        "SELECT e.event_id, e.entity_id, e.payload_hash FROM canonical_v2.event e "
        "WHERE e.event_type='trade_review' ORDER BY e.observed_at ASC, e.event_id ASC"
    )
    if limit and int(limit) > 0:
        query = _sql(conn, query + " LIMIT ?")
        rows = conn.execute(query, (int(limit),)).fetchall()
    else:
        query = _sql(conn, query)
        rows = conn.execute(query).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        payload = read_payload(conn, str(row["payload_hash"]))
        out.append(
            {
                "review_id": str(row["entity_id"] or ""),
                "event_id": str(row["event_id"] or ""),
                "source": "canonical",
                "payload": payload,
            }
        )
    return out


def _legacy_rows(conn: Any, kind: str, limit: int = 0) -> list[dict[str, Any]]:
    legacy_table = LEGACY_TABLE[kind]
    pk_column = LEGACY_PK[kind]
    order_column = LEGACY_ORDER_COLUMN[kind]
    table_sql = _legacy_table_sql(conn, legacy_table)
    if limit and limit > 0:
        rows = conn.execute(
            _sql(conn, f"SELECT * FROM {table_sql} ORDER BY {order_column} ASC, {pk_column} ASC LIMIT ?"),
            (int(limit),),
        ).fetchall()
    else:
        rows = conn.execute(
            _sql(conn, f"SELECT * FROM {table_sql} ORDER BY {order_column} ASC, {pk_column} ASC")
        ).fetchall()
    return [_row_mapping(conn, row) or {} for row in rows]


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
        for row in _legacy_rows(conn, "decision", limit):
            yield {
                "decision_id": str(row.get("decision_id") or ""),
                "event_id": "",
                "source": "legacy",
                "payload": row,
            }
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
    """Return {'count', 'latest'} for a fact kind.

    ``latest`` is the raw observed_at value (datetime on PostgreSQL).  When
    the canonical schema is not reachable (SQLite fixtures before cutover) the
    legacy occurrence table supplies the same statistics, so consumers never
    reimplement table SQL.  Returns None only when neither source exists.
    """
    event_type = EVENT_TYPE[kind]
    if _canonical_ready(conn):
        row = conn.execute(
            _sql(
                conn,
                "SELECT COUNT(*) AS n, MAX(observed_at) AS m FROM canonical_v2.event WHERE event_type=?",
            ),
            (event_type,),
        ).fetchone()
        if row is None:
            return None
        count = row["n"] if isinstance(row, Mapping) else row[0]
        latest = row["m"] if isinstance(row, Mapping) else row[1]
        return {"count": int(count or 0), "latest": latest}
    legacy_table = LEGACY_TABLE[kind]
    table_sql = _legacy_table_sql(conn, legacy_table)
    columns = tuple(timestamp_columns) if timestamp_columns else (LEGACY_ORDER_COLUMN[kind],)
    try:
        max_expr = ", ".join(f"MAX({column}) AS m_{column}" for column in columns)
        row = conn.execute(
            _sql(conn, f"SELECT COUNT(*) AS n, {max_expr} FROM {table_sql}")
        ).fetchone()
    except Exception:
        return None
    if row is None:
        return None
    count = row["n"] if isinstance(row, Mapping) else row[0]
    latest = 0.0
    for index, column in enumerate(columns, start=1):
        value = row[f"m_{column}"] if isinstance(row, Mapping) else row[index]
        epoch = observed_epoch(value)
        if epoch > latest:
            latest = epoch
    return {"count": int(count or 0), "latest": latest}


def read_trade_chain(conn: Any, review_id: str) -> dict[str, Any] | None:
    """Review + entry decision via derived_from, plus the decision's caused_by
    order/position events (broker_execution / position_transition)."""
    review = read_review(conn, review_id)
    if review is None:
        return None
    chain: dict[str, Any] = {"review": review, "decision": None, "orders": [], "positions": []}
    if not _canonical_ready(conn) or not review.get("event_id"):
        return chain
    decision_event_id: str | None = None
    derived = conn.execute(
        _sql(
            conn,
            "SELECT to_event_id FROM canonical_v2.event_relation "
            "WHERE from_event_id=? AND relation_type='derived_from'",
        ),
        (review["event_id"],),
    ).fetchall()
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
        if _resolve(conn, LEGACY_TABLE["review"], review_id) is not None:
            resolved += 1
    return {"requested": total, "resolved_canonical": resolved, "gap": total - resolved}


def _legacy_epoch(value: Any) -> float:
    epoch = observed_epoch(value)
    return epoch if epoch and epoch > 0 else 0.0


def _review_to_row(record: Mapping[str, Any]) -> dict[str, Any]:
    """Shape a canonical review record like a legacy trade_outcome_review row."""
    payload = record.get("payload") if isinstance(record, Mapping) else None
    if isinstance(payload, dict) and record.get("source") == "canonical":
        row = dict(payload)
        row["review_id"] = str(payload.get("review_id") or record.get("review_id") or "")
        if "created_at" in payload:
            row["created_at"] = _legacy_epoch(payload["created_at"])
        if isinstance(payload.get("failure_tags"), list):
            row["failure_tags_json"] = json.dumps(payload["failure_tags"])
        nested_review = payload.get("review")
        row["review_json"] = nested_review if isinstance(nested_review, dict) else {}
        row["review_archive_hash"] = ""
        return row
    if isinstance(payload, dict):
        # Legacy fallback record wraps the row dict in payload.
        row = dict(payload)
        row["review_id"] = str(payload.get("review_id") or record.get("review_id") or "")
        row["review_archive_hash"] = str(payload.get("review_archive_hash") or "")
        return row
    # Raw legacy row without a wrapper.
    row = dict(record)
    row["review_id"] = str(row.get("review_id") or record.get("review_id") or "")
    row["review_archive_hash"] = str(row.get("review_archive_hash") or "")
    return row


def _json_column(value: Any) -> Any:
    """Serialize a nested JSON value into the legacy text-column shape."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _order_to_row(record: Mapping[str, Any]) -> dict[str, Any]:
    """Shape a canonical order record like a legacy order_lifecycle_event row."""
    payload = record.get("payload") if isinstance(record, Mapping) else None
    if isinstance(payload, dict) and record.get("source") == "canonical":
        row = dict(payload)
        row["event_id"] = str(payload.get("event_id") or record.get("event_id") or "")
        for field in ("event_ts", "created_at"):
            if field in payload:
                row[field] = _legacy_epoch(payload[field])
        row.pop("details", None)
        row["details_json"] = _json_column(payload.get("details"))
        return row
    if isinstance(payload, dict):
        row = dict(payload)
        row["event_id"] = str(payload.get("event_id") or record.get("event_id") or "")
        return row
    row = dict(record)
    row["event_id"] = str(row.get("event_id") or record.get("event_id") or "")
    return row


def _position_to_row(record: Mapping[str, Any]) -> dict[str, Any]:
    """Shape a canonical position record like a legacy position_lifecycle_event row."""
    payload = record.get("payload") if isinstance(record, Mapping) else None
    if isinstance(payload, dict) and record.get("source") == "canonical":
        row = dict(payload)
        row["event_id"] = str(payload.get("event_id") or record.get("event_id") or "")
        for field in ("event_ts", "created_at"):
            if field in payload:
                row[field] = _legacy_epoch(payload[field])
        row.pop("details", None)
        row["details_json"] = _json_column(payload.get("details"))
        return row
    if isinstance(payload, dict):
        row = dict(payload)
        row["event_id"] = str(payload.get("event_id") or record.get("event_id") or "")
        return row
    row = dict(record)
    row["event_id"] = str(row.get("event_id") or record.get("event_id") or "")
    return row


def _decision_to_row(record: Mapping[str, Any]) -> dict[str, Any]:
    """Shape a canonical decision record like a legacy decision_ledger row."""
    payload = record.get("payload") if isinstance(record, Mapping) else None
    if isinstance(payload, dict) and record.get("source") == "canonical":
        row = dict(payload)
        row["decision_id"] = str(payload.get("decision_id") or record.get("decision_id") or "")
        if "decision_ts" in payload:
            row["decision_ts"] = _legacy_epoch(payload["decision_ts"])
        if "created_at" in payload:
            row["created_at"] = _legacy_epoch(payload["created_at"])
        for json_column, key in (
            ("portfolio_state_json", "portfolio_state"),
            ("risk_state_json", "risk_state"),
            ("action_json", "action"),
        ):
            value = payload.get(key)
            if value is not None:
                row[json_column] = json.dumps(value) if not isinstance(value, str) else value
        return row
    if isinstance(payload, dict):
        row = dict(payload)
        row["decision_id"] = str(payload.get("decision_id") or record.get("decision_id") or "")
        return row
    row = dict(record)
    row["decision_id"] = str(row.get("decision_id") or record.get("decision_id") or "")
    return row


def review_row(conn: Any, review_id: str) -> dict[str, Any] | None:
    """Return a legacy-shaped review row resolved via canonical mapping."""
    record = read_review(conn, review_id)
    return _review_to_row(record) if record is not None else None


def decision_row(conn: Any, decision_id: str) -> dict[str, Any] | None:
    """Return a legacy-shaped decision row resolved via canonical mapping."""
    record = read_decision(conn, decision_id)
    return _decision_to_row(record) if record is not None else None


def iter_review_rows(conn: Any, limit: int = 100) -> list[dict[str, Any]]:
    return [_review_to_row(record) for record in iter_reviews(conn, int(limit) if limit and int(limit) > 0 else 0)]


def iter_review_rows_desc(conn: Any, limit: int = 100) -> list[dict[str, Any]]:
    """Newest-first legacy-shaped review rows from the canonical stream.

    The canonical event stream is observed-ASC; consumers that need the
    newest N reviews (legacy ``ORDER BY created_at DESC LIMIT ?`` semantics)
    sort the full stream once (721 rows) and truncate.
    """
    rows = iter_review_rows(conn, limit=0)
    rows.sort(
        key=lambda r: (float(r.get("created_at") or 0.0), str(r.get("review_id") or "")),
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
    can select by payload fields.  SQLite fixtures fall back to the legacy
    occurrence table.
    """
    if not _canonical_ready(conn):
        legacy_table = LEGACY_TABLE[kind]
        table_sql = _legacy_table_sql(conn, legacy_table)
        clauses: list[str] = []
        params: list[Any] = []
        if entity_id:
            clauses.append("position_id=?" if kind == "position" else "trade_id=?")
            params.append(str(entity_id))
        if legacy_event_type:
            clauses.append("event_type=?")
            params.append(str(legacy_event_type))
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        order = " ORDER BY event_ts ASC, event_id ASC"
        bound = (f" LIMIT {int(limit)}" if limit and int(limit) > 0 else "")
        rows = conn.execute(_sql(conn, f"SELECT * FROM {table_sql}{where}{order}{bound}"), tuple(params)).fetchall()
        return [
            {
                "source": "legacy",
                "kind": kind,
                "legacy_table": legacy_table,
                "legacy_primary_key": str((_row_mapping(conn, row) or {}).get("event_id") or ""),
                "event_id": str((_row_mapping(conn, row) or {}).get("event_id") or ""),
                "event_type": str((_row_mapping(conn, row) or {}).get("event_type") or ""),
                "entity_type": "position" if kind == "position" else "order",
                "entity_id": str((_row_mapping(conn, row) or {}).get("position_id") or (row[0] if row else "")),
                "observed_at": (row[3] if row else None),
                "payload": _row_mapping(conn, row) or {},
            }
            for row in rows
        ]
    event_type = EVENT_TYPE[kind]
    params = [event_type]
    clauses = ["e.event_type=?"]
    if entity_id:
        clauses.append("e.entity_id=?")
        params.append(entity_id)
    bound = (f" LIMIT {int(limit)}" if limit and int(limit) > 0 else "")
    rows = conn.execute(
        _sql(conn, "SELECT e.event_id, e.entity_id, e.payload_hash, e.observed_at FROM canonical_v2.event e WHERE " + " AND ".join(clauses) + " ORDER BY e.observed_at ASC, e.event_id ASC" + bound),
        tuple(params),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        payload = read_payload(conn, str(row["payload_hash"]))
        out.append(
            {
                "source": "canonical",
                "kind": kind,
                "legacy_table": LEGACY_TABLE[kind],
                "legacy_primary_key": str(row["event_id"] or ""),
                "event_id": str(row["event_id"] or ""),
                "event_type": EVENT_TYPE[kind],
                "entity_type": str(row["entity_id"] or "").split(":", 1)[0] if ":" in str(row["entity_id"] or "") else (str(row["entity_id"] or "")),
                "entity_id": str(row["entity_id"] or ""),
                "observed_at": row["observed_at"],
                "payload": payload if isinstance(payload, dict) else {},
            }
        )
    return out


def order_row(conn: Any, event_id: str) -> dict[str, Any] | None:
    """Return a legacy-shaped order_lifecycle_event row resolved via canonical."""
    record = read_order_event(conn, event_id)
    return _order_to_row(record) if record is not None else None


def position_row(conn: Any, event_id: str) -> dict[str, Any] | None:
    """Return a legacy-shaped position_lifecycle_event row resolved via canonical."""
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
    timeframe, event_id}}`` or None when the file is missing or invalid
    (callers keep their legacy fallback in that case).  Never writes.
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
    """Iterate canonical_v2.training_sample_row as legacy-shaped dicts."""
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


# ── decision_factor_snapshot derivation ───────────────────────────────────

DFS_COLUMNS = (
    "decision_id", "factor", "source", "raw_value", "normalized_value",
    "direction", "base_weight", "policy_weight", "shadow_score",
    "health_score", "gated", "gated_reason", "contribution_score",
    "generation", "artifact_hash", "definition_fingerprint",
    "runtime_selection_fingerprint", "config_hash", "lineage_status",
)


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

    Priority:
    1. Read from the canonical payload factor_snapshots field (new decisions).
    2. Fall back to legacy decision_factor_snapshot table (historical).
    """
    if _canonical_ready(conn):
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
            if row is not None:
                ph = row["payload_hash"] if isinstance(row, Mapping) else row[0]
                payload = read_payload(conn, str(ph))
                if isinstance(payload, dict) and payload.get("decision_id"):
                    snapshots = _parse_factor_snapshots(payload)
                    if snapshots:
                        if order_by_contribution:
                            snapshots.sort(
                                key=lambda r: abs(float(r.get("contribution_score") or 0)),
                                reverse=True,
                            )
                        return snapshots
        except Exception:
            pass
    try:
        cols = ", ".join(DFS_COLUMNS)
        order = " ORDER BY ABS(contribution_score) DESC, factor ASC" if order_by_contribution else ""
        rows = conn.execute(
            _sql(conn, f"SELECT {cols} FROM decision_factor_snapshot WHERE decision_id=?{order}"),
            (str(decision_id),),
        ).fetchall()
        return [dict(r) for r in rows] if rows else []
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
    Falls back to legacy when canonical payloads lack factor_snapshots.
    """
    if _canonical_ready(conn):
        try:
            rows = conn.execute(
                _sql(
                    conn,
                    "SELECT e.event_id, pb.payload_hash"
                    " FROM canonical_v2.event e"
                    " JOIN canonical_v2.payload_blob pb ON e.payload_hash = pb.payload_hash"
                    " WHERE e.event_type = 'risk_decision'"
                    " ORDER BY e.created_at DESC"
                    f" LIMIT {int(limit * 5)}",
                ),
            ).fetchall()
            result: list[dict[str, Any]] = []
            for event_row in rows:
                ph = event_row["payload_hash"] if isinstance(event_row, Mapping) else event_row[1]
                try:
                    payload = read_payload(conn, str(ph))
                except Exception:
                    continue
                for s in _parse_factor_snapshots(payload):
                    if str(s.get("factor")) == factor:
                        result.append(s)
                if len(result) >= limit:
                    break
            if result:
                return result[:limit]
        except Exception:
            pass
    try:
        cols = ", ".join(DFS_COLUMNS)
        rows = conn.execute(
            _sql(conn, f"SELECT {cols} FROM decision_factor_snapshot WHERE factor=? ORDER BY id DESC LIMIT {int(limit)}"),
            (str(factor),),
        ).fetchall()
        return [dict(r) for r in rows] if rows else []
    except Exception:
        return []


def count_decision_factor_snapshots(conn: Any, decision_id: str) -> int:
    """Return the count of factor snapshots for a decision."""
    if _canonical_ready(conn):
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
            if row is not None:
                ph = row["payload_hash"] if isinstance(row, Mapping) else row[0]
                payload = read_payload(conn, str(ph))
                if isinstance(payload, dict) and payload.get("decision_id"):
                    n = len(_parse_factor_snapshots(payload))
                    if n > 0:
                        return n
        except Exception:
            pass
    try:
        row = conn.execute(
            _sql(conn, "SELECT COUNT(*) AS n FROM decision_factor_snapshot WHERE decision_id=?"),
            (str(decision_id),),
        ).fetchone()
        if row is None:
            return 0
        return int(row["n"] if isinstance(row, Mapping) else row[0])
    except Exception:
        return 0


def iter_all_decision_factor_snapshots(
    conn: Any,
    decision_ids: list[str],
) -> list[dict[str, Any]]:
    """Return factor snapshot rows for multiple decisions (batch read)."""
    if not decision_ids:
        return []
    if _canonical_ready(conn):
        try:
            ph_list = ["%s"] * len(decision_ids)
            placeholders = ",".join(ph_list)
            rows = conn.execute(
                _sql(
                    conn,
                    f"SELECT entity_id, payload_hash FROM canonical_v2.event"
                    f" WHERE entity_id IN ({placeholders})"
                    f" AND event_type = 'risk_decision'",
                ),
                tuple(decision_ids),
            ).fetchall()
            result: list[dict[str, Any]] = []
            for event_row in rows:
                ph = event_row["payload_hash"] if isinstance(event_row, Mapping) else event_row[1]
                try:
                    payload = read_payload(conn, str(ph))
                except Exception:
                    continue
                result.extend(_parse_factor_snapshots(payload))
            if result:
                return result
        except Exception:
            pass
    try:
        cols = ", ".join(DFS_COLUMNS)
        placeholders = ",".join(["?"] * len(decision_ids))
        rows = conn.execute(
            _sql(conn, f"SELECT {cols} FROM decision_factor_snapshot WHERE decision_id IN ({placeholders})"),
            tuple(decision_ids),
        ).fetchall()
        return [dict(r) for r in rows] if rows else []
    except Exception:
        return []
