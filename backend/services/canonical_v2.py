"""Canonical facts, payloads, lineage, and dataset references.

This module is the single write authority for immutable business facts in
``canonical_v2``.  Mutable operational state remains in the PostgreSQL
``runtime`` schema; it is not a second fact ledger.  Callers own the
transaction and this module never performs implicit reads or commits.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from backend.core.hash import canonical_hash
from backend.services.state_payloads import stable_json


CANONICAL_SCHEMA = "canonical_v2"
CANONICAL_PAYLOAD_SCHEMA = "canonical_payload.v1"
CANONICAL_EVENT_SCHEMA = "canonical_event.v1"
CANONICAL_SAMPLE_SCHEMA = "canonical_training_sample.v1"

# SQLite is used only by isolated tests/offline tools.  It still uses the
# canonical contract so those callers cannot silently exercise the retired
# legacy fact tables.
CANONICAL_SQLITE_DDL = """
CREATE TABLE IF NOT EXISTS payload_blob (
    payload_hash TEXT PRIMARY KEY CHECK (payload_hash <> ''),
    payload_kind TEXT NOT NULL CHECK (payload_kind <> ''),
    schema_version TEXT NOT NULL CHECK (schema_version <> ''),
    canonical_bytes BLOB NOT NULL,
    codec TEXT NOT NULL DEFAULT 'gzip' CHECK (codec IN ('identity', 'gzip')),
    raw_sha256 TEXT NOT NULL CHECK (raw_sha256 <> ''),
    raw_bytes INTEGER NOT NULL CHECK (raw_bytes >= 0),
    compressed_bytes INTEGER NOT NULL CHECK (compressed_bytes >= 0),
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_canonical_v2_payload_kind_created
    ON payload_blob(payload_kind, created_at);
CREATE TABLE IF NOT EXISTS event (
    event_id TEXT PRIMARY KEY CHECK (event_id <> ''),
    event_type TEXT NOT NULL CHECK (event_type IN (
        'market_observation', 'broker_execution', 'position_transition',
        'risk_decision', 'factor_observation', 'governance_proposal',
        'governance_command', 'governance_effect', 'trade_review',
        'label_observation', 'training_run', 'counterfactual_review',
        'supervisor_trace', 'supervisor_evaluation', 'broker_deal'
    )),
    entity_type TEXT NOT NULL CHECK (entity_type <> ''),
    entity_id TEXT NOT NULL CHECK (entity_id <> ''),
    observed_at TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    producer TEXT NOT NULL CHECK (producer <> ''),
    producer_version TEXT NOT NULL DEFAULT '',
    schema_version TEXT NOT NULL CHECK (schema_version <> ''),
    correlation_id TEXT NOT NULL DEFAULT '',
    causation_id TEXT NOT NULL DEFAULT '',
    parent_event_id TEXT,
    idempotency_key TEXT NOT NULL DEFAULT '',
    payload_hash TEXT NOT NULL REFERENCES payload_blob(payload_hash),
    status TEXT NOT NULL DEFAULT 'recorded' CHECK (status <> ''),
    created_at TEXT NOT NULL,
    FOREIGN KEY (parent_event_id) REFERENCES event(event_id)
        DEFERRABLE INITIALLY DEFERRED
);
CREATE INDEX IF NOT EXISTS idx_canonical_v2_event_entity_time
    ON event(entity_type, entity_id, observed_at, event_id);
CREATE INDEX IF NOT EXISTS idx_canonical_v2_event_payload
    ON event(payload_hash, recorded_at);
CREATE INDEX IF NOT EXISTS idx_canonical_v2_event_causation
    ON event(causation_id, recorded_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_canonical_v2_event_idempotency
    ON event(producer, idempotency_key) WHERE idempotency_key <> '';
CREATE TABLE IF NOT EXISTS event_relation (
    from_event_id TEXT NOT NULL REFERENCES event(event_id),
    to_event_id TEXT NOT NULL REFERENCES event(event_id),
    relation_type TEXT NOT NULL CHECK (relation_type IN (
        'caused_by', 'derived_from', 'reviews', 'labels', 'uses_config',
        'uses_factor_state', 'produced_sample', 'included_in_dataset',
        'produced_artifact', 'governed_by'
    )),
    created_at TEXT NOT NULL,
    PRIMARY KEY (from_event_id, to_event_id, relation_type)
);
CREATE INDEX IF NOT EXISTS idx_canonical_v2_relation_to
    ON event_relation(to_event_id, relation_type);
CREATE TABLE IF NOT EXISTS state_version (
    state_version_id TEXT PRIMARY KEY CHECK (state_version_id <> ''),
    entity_type TEXT NOT NULL CHECK (entity_type <> ''),
    entity_id TEXT NOT NULL CHECK (entity_id <> ''),
    version INTEGER NOT NULL CHECK (version > 0),
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    source_event_id TEXT NOT NULL REFERENCES event(event_id),
    payload_hash TEXT NOT NULL REFERENCES payload_blob(payload_hash),
    created_at TEXT NOT NULL,
    UNIQUE (entity_type, entity_id, version),
    CHECK (valid_to IS NULL OR valid_to > valid_from)
);
CREATE INDEX IF NOT EXISTS idx_canonical_v2_state_version_entity
    ON state_version(entity_type, entity_id, version DESC);
CREATE TABLE IF NOT EXISTS training_sample (
    sample_id TEXT PRIMARY KEY CHECK (sample_id <> ''),
    sample_type TEXT NOT NULL CHECK (sample_type <> ''),
    source_event_ids TEXT NOT NULL CHECK (length(trim(source_event_ids)) > 2),
    feature_hash TEXT NOT NULL CHECK (feature_hash <> ''),
    feature_schema_hash TEXT NOT NULL CHECK (feature_schema_hash <> ''),
    label_hash TEXT NOT NULL CHECK (label_hash <> ''),
    trace_hash TEXT NOT NULL CHECK (trace_hash <> ''),
    evidence_contract TEXT NOT NULL DEFAULT '{}',
    config_version INTEGER NOT NULL DEFAULT 0 CHECK (config_version >= 0),
    config_hash TEXT NOT NULL DEFAULT '',
    horizon_minutes INTEGER NOT NULL CHECK (horizon_minutes > 0),
    target_source TEXT NOT NULL CHECK (target_source <> ''),
    sample_status TEXT NOT NULL DEFAULT 'candidate'
        CHECK (sample_status IN ('candidate', 'ready', 'quarantined', 'invalid')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_canonical_v2_training_sample_type_status
    ON training_sample(sample_type, sample_status, updated_at);
CREATE TABLE IF NOT EXISTS dataset_manifest (
    dataset_id TEXT PRIMARY KEY CHECK (dataset_id <> ''),
    purpose TEXT NOT NULL CHECK (purpose <> ''),
    training_window TEXT NOT NULL CHECK (training_window <> ''),
    horizon_minutes INTEGER NOT NULL CHECK (horizon_minutes > 0),
    query_contract_hash TEXT NOT NULL CHECK (query_contract_hash <> ''),
    sample_digest TEXT NOT NULL CHECK (sample_digest <> ''),
    feature_schema_hash TEXT NOT NULL CHECK (feature_schema_hash <> ''),
    label_contract_hash TEXT NOT NULL CHECK (label_contract_hash <> ''),
    target_source TEXT NOT NULL CHECK (target_source <> ''),
    config_hash TEXT NOT NULL DEFAULT '',
    source_watermark TEXT NOT NULL CHECK (source_watermark <> ''),
    code_commit TEXT NOT NULL CHECK (code_commit <> ''),
    artifact_hash TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'created' CHECK (status <> ''),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS dataset_manifest_member (
    dataset_id TEXT NOT NULL REFERENCES dataset_manifest(dataset_id),
    sample_id TEXT NOT NULL REFERENCES training_sample(sample_id),
    sample_order INTEGER NOT NULL CHECK (sample_order >= 0),
    sample_digest TEXT NOT NULL CHECK (sample_digest <> ''),
    PRIMARY KEY (dataset_id, sample_id),
    UNIQUE (dataset_id, sample_order)
);
CREATE TABLE IF NOT EXISTS projection_run (
    projection_run_id TEXT PRIMARY KEY CHECK (projection_run_id <> ''),
    run_kind TEXT NOT NULL DEFAULT 'projection' CHECK (run_kind IN ('projection', 'backfill')),
    projection_name TEXT NOT NULL CHECK (projection_name <> ''),
    source_watermark TEXT NOT NULL CHECK (source_watermark <> ''),
    code_version TEXT NOT NULL CHECK (code_version <> ''),
    input_digest TEXT NOT NULL CHECK (input_digest <> ''),
    output_digest TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL DEFAULT 'running'
        CHECK (status IN ('running', 'completed', 'failed', 'aborted')),
    error_code TEXT NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_canonical_v2_projection_identity
    ON projection_run(run_kind, projection_name, source_watermark, code_version, input_digest);
CREATE TABLE IF NOT EXISTS training_sample_row (
    sample_id TEXT PRIMARY KEY,
    sample_type TEXT NOT NULL DEFAULT '', source_table TEXT NOT NULL DEFAULT '',
    source_id TEXT NOT NULL DEFAULT '', decision_id TEXT NOT NULL DEFAULT '',
    trade_id TEXT NOT NULL DEFAULT '', position_id TEXT NOT NULL DEFAULT '',
    symbol TEXT NOT NULL DEFAULT '', timeframe TEXT NOT NULL DEFAULT '', event_ts REAL,
    label_status TEXT NOT NULL DEFAULT '', integrity TEXT NOT NULL DEFAULT '',
    train_weight REAL NOT NULL DEFAULT 1.0, features_json TEXT NOT NULL DEFAULT '{}',
    verdict_json TEXT NOT NULL DEFAULT '{}', label_json TEXT NOT NULL DEFAULT '{}',
    trace_json TEXT NOT NULL DEFAULT '{}', evidence_contract_json TEXT NOT NULL DEFAULT '{}',
    config_version INTEGER NOT NULL DEFAULT 0, config_hash TEXT NOT NULL DEFAULT '',
    evolution_run_id TEXT NOT NULL DEFAULT '', system_contaminated INTEGER NOT NULL DEFAULT 0,
    governance_eligible INTEGER NOT NULL DEFAULT 0, governance_effective_weight REAL NOT NULL DEFAULT 1.0,
    governance_eligibility_version TEXT NOT NULL DEFAULT '',
    governance_ineligible_reason TEXT NOT NULL DEFAULT '',
    governance_eligibility_fingerprint TEXT NOT NULL DEFAULT '',
    content_fingerprint TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tsr_sample_type_status
    ON training_sample_row(sample_type, label_status, governance_eligible);
CREATE INDEX IF NOT EXISTS idx_tsr_decision ON training_sample_row(decision_id);
CREATE INDEX IF NOT EXISTS idx_tsr_fingerprint
    ON training_sample_row(content_fingerprint, updated_at);
CREATE INDEX IF NOT EXISTS idx_tsr_event_ts ON training_sample_row(event_ts);
"""

EVENT_TYPES = frozenset(
    {
        "market_observation",
        "broker_execution",
        "position_transition",
        "risk_decision",
        "factor_observation",
        "governance_proposal",
        "governance_command",
        "governance_effect",
        "trade_review",
        "label_observation",
        "training_run",
        # P2 边界 4 域事件化
        "counterfactual_review",
        "supervisor_trace",
        "supervisor_evaluation",
        "broker_deal",
    }
)

RELATION_TYPES = frozenset(
    {
        "caused_by",
        "derived_from",
        "reviews",
        "labels",
        "uses_config",
        "uses_factor_state",
        "produced_sample",
        "included_in_dataset",
        "produced_artifact",
        "governed_by",
    }
)

# Stable edge direction for the live trade chain.  The child event is always
# ``from_event_id`` and the already-known parent decision is
# ``to_event_id``; this is the contract consumed by ``read_trade_chain``.
TRADE_LINEAGE_RELATION_TYPES = {
    "review_to_entry_decision": "derived_from",
    "review_to_exit_decision": "reviews",
    "order_to_decision": "caused_by",
    "position_to_decision": "caused_by",
}

class CanonicalV2Error(RuntimeError):
    """Base class for canonical v2 contract failures."""


class CanonicalV2ConflictError(CanonicalV2Error):
    """Raised when an idempotent or immutable object changes its identity."""


@dataclass(frozen=True)
class PayloadRef:
    payload_hash: str
    payload_kind: str
    schema_version: str
    raw_sha256: str
    raw_bytes: int
    compressed_bytes: int
    codec: str = "gzip"


def _is_pg(conn: Any) -> bool:
    return conn.__class__.__module__.split(".", 1)[0] == "psycopg"


def _sql(conn: Any, statement: str) -> str:
    if _is_pg(conn):
        return statement.replace("?", "%s")
    # SQLite fixtures keep canonical tables as bare names in the main database,
    # so the canonical_v2. schema prefix is dropped (PG keeps it).
    return statement.replace("canonical_v2.", "")


def _row_value(row: Any, key: str, index: int = 0, default: Any = None) -> Any:
    if row is None:
        return default
    if isinstance(row, Mapping):
        return row.get(key, default)
    try:
        return row[index]
    except (IndexError, KeyError, TypeError):
        return default


def _row_dict(row: Any, columns: Iterable[str]) -> dict[str, Any]:
    names = tuple(columns)
    return {name: _row_value(row, name, index) for index, name in enumerate(names)}


def _utc(value: Any | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _db_time(conn: Any, value: Any | None) -> Any:
    timestamp = _utc(value)
    return timestamp if _is_pg(conn) else timestamp.isoformat()


def _db_array(conn: Any, values: Iterable[str]) -> Any:
    items = list(values)
    return items if _is_pg(conn) else json.dumps(items, ensure_ascii=False, separators=(",", ":"))


def _comparable(field: str, value: Any) -> Any:
    if field in {"source_event_ids", "evidence_contract"} and isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _utc_value(value: Any) -> Any:
    """Normalize datetimes to UTC so the same instant compares equal."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    return value


def _same_value(field: str, left: Any, right: Any) -> bool:
    return stable_json(_comparable(field, _utc_value(left))) == stable_json(
        _comparable(field, _utc_value(right))
    )




def canonical_payload(value: Any, *, payload_kind: str, schema_version: str) -> tuple[PayloadRef, bytes]:
    """Return a deterministic content reference and compressed bytes."""

    if not str(payload_kind or "").strip():
        raise CanonicalV2Error("payload_kind must not be empty")
    if not str(schema_version or "").strip():
        raise CanonicalV2Error("payload schema_version must not be empty")
    raw = stable_json(value)
    raw_bytes = raw.encode("utf-8")
    compressed = gzip.compress(raw_bytes, compresslevel=6, mtime=0)
    raw_sha256 = hashlib.sha256(raw_bytes).hexdigest()
    return (
        PayloadRef(
            payload_hash=hashlib.sha256((f"{CANONICAL_PAYLOAD_SCHEMA}" + chr(0) + str(payload_kind) + chr(0) + str(schema_version) + chr(0)).encode("utf-8") + raw_bytes).hexdigest(),
            payload_kind=str(payload_kind),
            schema_version=str(schema_version),
            raw_sha256=raw_sha256,
            raw_bytes=len(raw_bytes),
            compressed_bytes=len(compressed),
        ),
        compressed,
    )


def put_payload(
    conn: Any,
    value: Any,
    *,
    payload_kind: str,
    schema_version: str,
    created_at: Any | None = None,
) -> PayloadRef:
    """Intern one payload without committing the caller's transaction."""

    ref, compressed = canonical_payload(
        value,
        payload_kind=payload_kind,
        schema_version=schema_version,
    )
    conn.execute(
        _sql(
            conn,
            """
            INSERT INTO canonical_v2.payload_blob
                (payload_hash, payload_kind, schema_version, canonical_bytes,
                 codec, raw_sha256, raw_bytes, compressed_bytes, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(payload_hash) DO NOTHING
            """,
        ),
        (
            ref.payload_hash,
            ref.payload_kind,
            ref.schema_version,
            compressed,
            ref.codec,
            ref.raw_sha256,
            ref.raw_bytes,
            ref.compressed_bytes,
            _db_time(conn, created_at),
        ),
    )
    return ref


def read_payload(conn: Any, payload_hash: str) -> Any:
    """Restore and verify one payload blob."""

    row = conn.execute(
        _sql(
            conn,
            """
            SELECT payload_kind, schema_version, canonical_bytes, codec,
                   raw_sha256, raw_bytes, compressed_bytes
            FROM canonical_v2.payload_blob
            WHERE payload_hash=?
            """,
        ),
        (str(payload_hash or ""),),
    ).fetchone()
    if row is None:
        raise KeyError(f"missing canonical_v2 payload: {payload_hash}")
    fields = _row_dict(
        row,
        (
            "payload_kind",
            "schema_version",
            "canonical_bytes",
            "codec",
            "raw_sha256",
            "raw_bytes",
            "compressed_bytes",
        ),
    )
    compressed = bytes(fields["canonical_bytes"] or b"")
    if len(compressed) != int(fields["compressed_bytes"] or 0):
        raise CanonicalV2Error("canonical_v2 compressed length mismatch")
    codec = str(fields["codec"] or "")
    if codec == "gzip":
        raw = gzip.decompress(compressed)
    elif codec == "identity":
        raw = compressed
    else:
        raise CanonicalV2Error(f"unsupported canonical_v2 payload codec: {codec}")
    if len(raw) != int(fields["raw_bytes"] or 0):
        raise CanonicalV2Error("canonical_v2 raw length mismatch")
    raw_sha256 = hashlib.sha256(raw).hexdigest()
    if raw_sha256 != str(fields["raw_sha256"] or ""):
        raise CanonicalV2Error("canonical_v2 raw SHA-256 mismatch")
    expected_hash = hashlib.sha256((CANONICAL_PAYLOAD_SCHEMA + chr(0) + str(fields["payload_kind"] or "") + chr(0) + str(fields["schema_version"] or "") + chr(0)).encode("utf-8") + raw).hexdigest()
    if expected_hash != str(payload_hash or ""):
        raise CanonicalV2Error("canonical_v2 payload hash mismatch")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CanonicalV2Error("canonical_v2 payload is not valid JSON") from exc


_EVENT_COLUMNS = (
    "event_id",
    "event_type",
    "entity_type",
    "entity_id",
    "observed_at",
    "recorded_at",
    "producer",
    "producer_version",
    "schema_version",
    "correlation_id",
    "causation_id",
    "parent_event_id",
    "idempotency_key",
    "payload_hash",
    "status",
    "created_at",
)


def _assert_same_event(existing: Mapping[str, Any], expected: Mapping[str, Any]) -> None:
    for field in (
        "event_type",
        "entity_type",
        "entity_id",
        "producer",
        "producer_version",
        "schema_version",
        "correlation_id",
        "causation_id",
        "parent_event_id",
        "idempotency_key",
        "payload_hash",
        "status",
    ):
        if str(existing.get(field) or "") != str(expected.get(field) or ""):
            raise CanonicalV2ConflictError(
                f"immutable canonical event conflict field={field} "
                f"event_id={existing.get('event_id')}"
            )


def append_event(
    conn: Any,
    *,
    event_type: str,
    entity_type: str,
    entity_id: str,
    payload_hash: str,
    producer: str,
    schema_version: str = CANONICAL_EVENT_SCHEMA,
    event_id: str | None = None,
    observed_at: Any | None = None,
    recorded_at: Any | None = None,
    producer_version: str = "",
    correlation_id: str = "",
    causation_id: str = "",
    parent_event_id: str = "",
    idempotency_key: str = "",
    status: str = "recorded",
) -> dict[str, Any]:
    """Append one immutable event or return its idempotent existing row."""

    values = {
        "event_id": str(event_id or uuid.uuid4().hex),
        "event_type": str(event_type or ""),
        "entity_type": str(entity_type or ""),
        "entity_id": str(entity_id or ""),
        "observed_at": _db_time(conn, observed_at),
        "recorded_at": _db_time(conn, recorded_at),
        "producer": str(producer or ""),
        "producer_version": str(producer_version or ""),
        "schema_version": str(schema_version or ""),
        "correlation_id": str(correlation_id or ""),
        "causation_id": str(causation_id or ""),
        "parent_event_id": str(parent_event_id or "") or None,
        "idempotency_key": str(idempotency_key or ""),
        "payload_hash": str(payload_hash or ""),
        "status": str(status or ""),
        "created_at": _db_time(conn, None),
    }
    if values["event_type"] not in EVENT_TYPES:
        raise CanonicalV2Error(f"unsupported canonical event_type: {values['event_type']}")
    for field in ("entity_type", "entity_id", "producer", "schema_version", "payload_hash", "status"):
        if not values[field]:
            raise CanonicalV2Error(f"canonical event field must not be empty: {field}")

    existing = None
    if values["idempotency_key"]:
        existing = conn.execute(
            _sql(
                conn,
                """
                SELECT event_id, event_type, entity_type, entity_id, observed_at,
                       recorded_at, producer, producer_version, schema_version,
                       correlation_id, causation_id, parent_event_id,
                       idempotency_key, payload_hash, status, created_at
                FROM canonical_v2.event
                WHERE producer=? AND idempotency_key<>'' AND idempotency_key=?
                LIMIT 1
                """,
            ),
            (values["producer"], values["idempotency_key"]),
        ).fetchone()
    if existing is None and values["event_id"]:
        existing = conn.execute(
            _sql(
                conn,
                """
                SELECT event_id, event_type, entity_type, entity_id, observed_at,
                       recorded_at, producer, producer_version, schema_version,
                       correlation_id, causation_id, parent_event_id,
                       idempotency_key, payload_hash, status, created_at
                FROM canonical_v2.event
                WHERE event_id=?
                LIMIT 1
                """,
            ),
            (values["event_id"],),
        ).fetchone()
    if existing is not None:
        existing_dict = _row_dict(existing, _EVENT_COLUMNS)
        _assert_same_event(existing_dict, values)
        existing_dict["created"] = False
        return existing_dict

    columns = (
        "event_id",
        "event_type",
        "entity_type",
        "entity_id",
        "observed_at",
        "recorded_at",
        "producer",
        "producer_version",
        "schema_version",
        "correlation_id",
        "causation_id",
        "parent_event_id",
        "idempotency_key",
        "payload_hash",
        "status",
        "created_at",
    )
    placeholders = ", ".join("?" for _ in columns)
    if values["idempotency_key"]:
        conflict = "ON CONFLICT (producer, idempotency_key) WHERE idempotency_key <> '' DO NOTHING"
    else:
        conflict = "ON CONFLICT (event_id) DO NOTHING"
    conn.execute(
        _sql(
            conn,
            f"""
            INSERT INTO canonical_v2.event ({', '.join(columns)})
            VALUES ({placeholders})
            {conflict}
            """,
        ),
        tuple(values[column] for column in columns),
    )
    lookup = (
        "SELECT " + ", ".join(columns) + ", created_at FROM canonical_v2.event WHERE producer=? AND idempotency_key=? LIMIT 1"
        if values["idempotency_key"]
        else "SELECT " + ", ".join(columns) + ", created_at FROM canonical_v2.event WHERE event_id=? LIMIT 1"
    )
    lookup_params = (
        (values["producer"], values["idempotency_key"])
        if values["idempotency_key"]
        else (values["event_id"],)
    )
    row = conn.execute(_sql(conn, lookup), lookup_params).fetchone()
    if row is None:
        raise CanonicalV2Error("canonical event insert did not return a row")
    result = _row_dict(row, _EVENT_COLUMNS)
    _assert_same_event(result, values)
    result["created"] = True
    return result


def append_relation(
    conn: Any,
    *,
    from_event_id: str,
    to_event_id: str,
    relation_type: str,
    created_at: Any | None = None,
) -> bool:
    """Insert one immutable lineage edge; return whether it was newly added."""

    if relation_type not in RELATION_TYPES:
        raise CanonicalV2Error(f"unsupported canonical relation_type: {relation_type}")
    cursor = conn.execute(
        _sql(
            conn,
            """
            INSERT INTO canonical_v2.event_relation
                (from_event_id, to_event_id, relation_type, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(from_event_id, to_event_id, relation_type) DO NOTHING
            """,
        ),
        (str(from_event_id), str(to_event_id), relation_type, _db_time(conn, created_at)),
    )
    return bool(getattr(cursor, "rowcount", 1))


def _link_trade_lineage(
    conn: Any,
    *,
    child_event_id: str,
    parent_event_id: str,
    relation_key: str,
    created_at: Any | None = None,
) -> dict[str, str]:
    """Link a newly written child to a known decision, or report unlinked.

    This helper deliberately performs no lookup by trade/position/entity.  A
    caller must supply the exact parent event ID.  The returned status is
    attached to the writer result so an event that committed without a
    relation cannot be mistaken for a complete chain.
    """
    relation_type = TRADE_LINEAGE_RELATION_TYPES[relation_key]
    child_id = str(child_event_id or "")
    parent_id = str(parent_event_id or "")
    if not parent_id:
        return {
            "lineage_status": "unlinked_parent_missing",
            "lineage_parent_event_id": "",
            "lineage_relation_type": relation_type,
        }
    if not child_id:
        return {
            "lineage_status": "unlinked_child_missing",
            "lineage_parent_event_id": parent_id,
            "lineage_relation_type": relation_type,
        }
    # Verify both endpoints on this caller-owned connection/transaction.  The
    # generic append_relation contract remains unchanged; this stricter check
    # is local to the live trade chain and prevents guessed parent IDs.
    endpoint_count = conn.execute(
        _sql(
            conn,
            "SELECT COUNT(*) AS n FROM canonical_v2.event "
            "WHERE event_id IN (?, ?)",
        ),
        (child_id, parent_id),
    ).fetchone()
    if int(_row_value(endpoint_count, "n", 0, 0) or 0) != 2:
        return {
            "lineage_status": "unlinked_parent_event_missing",
            "lineage_parent_event_id": parent_id,
            "lineage_relation_type": relation_type,
        }
    append_relation(
        conn,
        from_event_id=child_id,
        to_event_id=parent_id,
        relation_type=relation_type,
        created_at=created_at,
    )
    # ``append_relation`` returns False for an already-existing edge; the
    # endpoint check above proves that this still represents a linked chain.
    return {
        "lineage_status": "linked",
        "lineage_parent_event_id": parent_id,
        "lineage_relation_type": relation_type,
    }


def _lineage_existing_status(
    conn: Any,
    *,
    child_event_id: str,
    parent_event_id: str,
    relation_key: str,
) -> dict[str, str]:
    """Describe an idempotent existing event without mutating its lineage."""
    relation_type = TRADE_LINEAGE_RELATION_TYPES[relation_key]
    linked = conn.execute(
        _sql(
            conn,
            "SELECT 1 FROM canonical_v2.event_relation "
            "WHERE from_event_id=? AND to_event_id=? AND relation_type=? LIMIT 1",
        ),
        (str(child_event_id or ""), str(parent_event_id or ""), relation_type),
    ).fetchone() is not None
    return {
        "lineage_status": "linked" if linked else "existing_unmodified",
        "lineage_parent_event_id": str(parent_event_id or ""),
        "lineage_relation_type": relation_type,
    }


def _payload_iso(value: Any) -> str | None:
    """Normalize a timestamp to the ISO form used by canonical payloads."""
    if value is None:
        return None
    if isinstance(value, datetime):
        v = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        return v.astimezone(timezone.utc).isoformat()
    try:
        ts = float(value)
    except (TypeError, ValueError):
        return str(value)
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def ensure_sqlite_schema(conn: Any) -> None:
    """Install the canonical-only SQLite fixture schema on an open connection."""

    if _is_pg(conn):
        return
    conn.executescript(CANONICAL_SQLITE_DDL)


LIVE_INCREMENT_RUN = "live.increment.v2"


def record_payload_event(
    conn: Any,
    *,
    event_type: str,
    entity_type: str,
    entity_id: str,
    payload: Mapping[str, Any] | Any,
    observed_at: Any,
    producer: str,
    payload_kind: str | None = None,
    event_id: str | None = None,
    idempotency_key: str | None = None,
    causation_id: str = "",
    parent_event_id: str = "",
    status: str = "completed",
) -> dict[str, Any]:
    """Persist one immutable canonical event from an already-shaped payload."""

    normalized_event_type = str(event_type or "")
    normalized_entity_id = str(entity_id or "")
    if normalized_event_type not in EVENT_TYPES:
        raise CanonicalV2Error(f"unsupported canonical event_type: {normalized_event_type}")
    if not normalized_entity_id:
        raise CanonicalV2Error("canonical event entity_id must not be empty")
    ref = put_payload(
        conn,
        payload,
        payload_kind=str(payload_kind or normalized_event_type),
        schema_version=CANONICAL_PAYLOAD_SCHEMA,
        created_at=observed_at,
    )
    event = append_event(
        conn,
        event_id=str(event_id or "") or None,
        event_type=normalized_event_type,
        entity_type=str(entity_type or ""),
        entity_id=normalized_entity_id,
        payload_hash=ref.payload_hash,
        producer=str(producer or ""),
        observed_at=observed_at,
        idempotency_key=str(idempotency_key or event_id or normalized_entity_id),
        causation_id=str(causation_id or ""),
        parent_event_id=str(parent_event_id or ""),
        status=str(status or "completed"),
    )
    start_projection_run(
        conn,
        projection_run_id=LIVE_INCREMENT_RUN,
        run_kind="backfill",
        projection_name="live_increment",
        source_watermark="live",
        code_version=LIVE_INCREMENT_RUN,
        input_digest="live",
    )
    return event


def record_supervisor_trace_event(
    conn: Any,
    *,
    trace_id: str,
    decision_id: str = "",
    event_ts: Any,
    payload: Mapping[str, Any],
    producer: str = "position_supervisor",
) -> dict[str, Any]:
    """Write one supervisor trace as an immutable canonical event."""

    event = record_payload_event(
        conn,
        event_type="supervisor_trace",
        entity_type="position_supervisor_trace",
        entity_id=str(trace_id),
        payload=dict(payload),
        observed_at=event_ts,
        producer=producer,
        payload_kind="supervisor_trace",
        event_id=f"live_supervisor_trace_{str(trace_id)}",
        idempotency_key=str(trace_id),
        causation_id=(f"live_decision_{decision_id}" if decision_id else ""),
    )
    if decision_id and conn.execute(
        _sql(conn, "SELECT 1 FROM canonical_v2.event WHERE event_id=? LIMIT 1"),
        (f"live_decision_{str(decision_id)}",),
    ).fetchone() is not None:
        append_relation(
            conn,
            from_event_id=str(event["event_id"]),
            to_event_id=f"live_decision_{str(decision_id)}",
            relation_type="caused_by",
            created_at=event_ts,
        )
    return event


def record_supervisor_evaluation_event(
    conn: Any,
    *,
    position_id: str,
    decision_id: str = "",
    event_ts: Any,
    payload: Mapping[str, Any],
    producer: str = "position_supervisor",
) -> dict[str, Any]:
    """Write one bar-level supervisor evaluation as an immutable canonical
    event.  Payload is deliberately lean (posture/action/reason/progress,
    no position snapshot); full traces are written separately on action."""
    normalized_position = str(position_id or "")
    if not normalized_position:
        raise CanonicalV2Error("canonical supervisor evaluation requires position_id")
    bar_key = str((dict(payload) or {}).get("bar_key") or "")
    if not bar_key:
        raise CanonicalV2Error("canonical supervisor evaluation requires bar_key")
    event = record_payload_event(
        conn,
        event_type="supervisor_evaluation",
        entity_type="position_supervisor_evaluation",
        entity_id=normalized_position,
        payload=dict(payload),
        observed_at=event_ts,
        producer=producer,
        payload_kind="supervisor_evaluation",
        event_id=f"live_supervisor_evaluation_{normalized_position}_{bar_key}",
        idempotency_key=f"{normalized_position}:{bar_key}",
        causation_id=(f"live_decision_{decision_id}" if decision_id else ""),
    )
    if decision_id and conn.execute(
        _sql(conn, "SELECT 1 FROM canonical_v2.event WHERE event_id=? LIMIT 1"),
        (f"live_decision_{str(decision_id)}",),
    ).fetchone() is not None:
        append_relation(
            conn,
            from_event_id=str(event["event_id"]),
            to_event_id=f"live_decision_{str(decision_id)}",
            relation_type="caused_by",
            created_at=event_ts,
        )
    return event


def record_counterfactual_event(
    conn: Any,
    *,
    counterfactual_id: str,
    review_id: str = "",
    decision_id: str = "",
    trace_id: str = "",
    event_ts: Any,
    payload: Mapping[str, Any],
    producer: str = "supervisor_counterfactual",
) -> dict[str, Any]:
    """Write one counterfactual result as an immutable canonical event.

    Counterfactuals are derived evidence, not a mutable fact table.  The
    deterministic event/idempotency keys make a learning cycle retry safe;
    lineage edges are added only when the source event is present so an
    incomplete historical sample cannot invent a relation.
    """
    normalized_id = str(counterfactual_id or "")
    if not normalized_id:
        raise CanonicalV2Error("canonical counterfactual requires counterfactual_id")
    version_hash = hashlib.sha1(stable_json(dict(payload)).encode("utf-8")).hexdigest()[:20]
    event = record_payload_event(
        conn,
        event_type="counterfactual_review",
        entity_type="supervisor_counterfactual",
        entity_id=normalized_id,
        payload=dict(payload),
        observed_at=event_ts,
        producer=producer,
        payload_kind="counterfactual_review",
        event_id=f"live_counterfactual_{normalized_id}_{version_hash}",
        idempotency_key=f"{normalized_id}:{version_hash}",
    )
    source_ids = (
        (f"live_review_{str(review_id)}", "reviews"),
        (f"live_decision_{str(decision_id)}", "derived_from"),
        (f"live_supervisor_trace_{str(trace_id)}", "derived_from"),
    )
    for source_event_id, relation_type in source_ids:
        if not source_event_id.endswith("_"):
            exists = conn.execute(
                _sql(conn, "SELECT 1 FROM canonical_v2.event WHERE event_id=? LIMIT 1"),
                (source_event_id,),
            ).fetchone()
            if exists is not None:
                append_relation(
                    conn,
                    from_event_id=str(event["event_id"]),
                    to_event_id=source_event_id,
                    relation_type=relation_type,
                    created_at=event_ts,
                )
    return event


def record_decision_event(
    conn: Any,
    *,
    decision_id: str,
    event_type: str,
    symbol: str,
    timeframe: str,
    decision_ts: Any,
    trade_id: str = "",
    position_id: str = "",
    regime_id: str = "",
    regime_confidence: Any = None,
    policy_version: str = "",
    factor_set_version: str = "",
    action_score: Any = None,
    action_reason: str = "",
    action: Any = None,
    risk_state: Any = None,
    portfolio_state: Any = None,
    created_at: Any = None,
    producer: str = "live_ledger",
    factor_snapshots: list | None = None,
) -> dict[str, Any]:
    """Mirror one live decision into canonical within the caller's transaction.

    Idempotent per ``decision_id`` (event_id and idempotency_key derive from the
    decision id), so replaying the same ledger write returns the existing event
    instead of duplicating payload/event/mapping rows.  The payload shape
    mirrors the historical canonical decision backfill so
    ``canonical_v2_reader`` returns live decisions on the
    same ``risk_decision`` stream.

    When *factor_snapshots* is provided the per-factor detail is embedded in the
    payload, enabling consumers to derive factor data from the canonical event
    instead of reading a retired runtime projection table.
    """
    payload: dict[str, Any] = {
        "decision_id": str(decision_id or ""),
        "trade_id": str(trade_id or ""),
        "position_id": str(position_id or ""),
        "event_type": str(event_type or ""),
        "symbol": str(symbol or ""),
        "timeframe": str(timeframe or ""),
        "decision_ts": _payload_iso(decision_ts),
        "regime_id": str(regime_id or ""),
        "regime_confidence": regime_confidence,
        "policy_version": str(policy_version or ""),
        "factor_set_version": str(factor_set_version or ""),
        "action_score": action_score,
        "action_reason": str(action_reason or ""),
        "action": action,
        "risk_state": risk_state,
        "portfolio_state": portfolio_state,
        "created_at": _payload_iso(created_at),
    }
    if factor_snapshots:
        payload["factor_snapshots"] = factor_snapshots
    ref = put_payload(
        conn,
        payload,
        payload_kind="risk_decision",
        schema_version=CANONICAL_PAYLOAD_SCHEMA,
        created_at=decision_ts,
    )
    event = append_event(
        conn,
        event_id=f"live_decision_{str(decision_id or '')}",
        event_type="risk_decision",
        entity_type="decision",
        entity_id=str(decision_id or ""),
        payload_hash=ref.payload_hash,
        producer=producer,
        observed_at=decision_ts,
        idempotency_key=str(decision_id or ""),
        status="completed",
    )
    start_projection_run(
        conn,
        projection_run_id=LIVE_INCREMENT_RUN,
        run_kind="backfill",
        projection_name="live_increment",
        source_watermark="live",
        code_version=LIVE_INCREMENT_RUN,
        input_digest="live",
    )
    return event


def record_order_event(
    conn: Any,
    *,
    event_id: str,
    event_type: str,
    event_ts: Any,
    decision_id: str = "",
    trade_id: str = "",
    order_id: str = "",
    broker_order_id: str = "",
    price: Any = None,
    volume: Any = None,
    status: str = "",
    details: Any = None,
    producer: str = "live_ledger",
) -> dict[str, Any]:
    """Mirror one live order lifecycle event into canonical (idempotent)."""
    payload: dict[str, Any] = {
        "event_id": str(event_id or ""),
        "decision_id": str(decision_id or ""),
        "trade_id": str(trade_id or ""),
        "order_id": str(order_id or ""),
        "broker_order_id": str(broker_order_id or ""),
        "event_type": str(event_type or ""),
        "event_ts": _payload_iso(event_ts),
        "price": price,
        "volume": volume,
        "status": str(status or ""),
        "details": details,
    }
    ref = put_payload(
        conn, payload, payload_kind="broker_execution",
        schema_version=CANONICAL_PAYLOAD_SCHEMA, created_at=event_ts,
    )
    evt = append_event(
        conn,
        event_id=f"live_ordevt_{str(event_id or '')}",
        event_type="broker_execution",
        entity_type="order",
        entity_id=str(event_id or ""),
        payload_hash=ref.payload_hash,
        producer=producer,
        observed_at=event_ts,
        idempotency_key=str(event_id or ""),
        status="completed",
    )
    start_projection_run(
        conn, projection_run_id=LIVE_INCREMENT_RUN, run_kind="backfill",
        projection_name="live_increment", source_watermark="live",
        code_version=LIVE_INCREMENT_RUN, input_digest="live",
    )
    parent_event_id = f"live_decision_{str(decision_id)}" if str(decision_id or "") else ""
    evt.update(
        _link_trade_lineage(
            conn,
            child_event_id=str(evt["event_id"]),
            parent_event_id=parent_event_id,
            relation_key="order_to_decision",
            created_at=event_ts,
        )
        if evt.get("created")
        else _lineage_existing_status(
            conn,
            child_event_id=str(evt["event_id"]),
            parent_event_id=parent_event_id,
            relation_key="order_to_decision",
        )
    )
    return evt


def record_position_event(
    conn: Any,
    *,
    event_id: str,
    event_type: str,
    event_ts: Any,
    decision_id: str = "",
    position_id: str = "",
    trade_id: str = "",
    symbol: str = "",
    net_volume: Any = None,
    avg_price: Any = None,
    unrealized_pnl: Any = None,
    realized_pnl: Any = None,
    details: Any = None,
    producer: str = "live_ledger",
) -> dict[str, Any]:
    """Mirror one live position lifecycle event into canonical (idempotent)."""
    payload: dict[str, Any] = {
        "event_id": str(event_id or ""),
        "decision_id": str(decision_id or ""),
        "position_id": str(position_id or ""),
        "trade_id": str(trade_id or ""),
        "symbol": str(symbol or ""),
        "event_type": str(event_type or ""),
        "event_ts": _payload_iso(event_ts),
        "net_volume": net_volume,
        "avg_price": avg_price,
        "unrealized_pnl": unrealized_pnl,
        "realized_pnl": realized_pnl,
        "details": details,
    }
    ref = put_payload(
        conn, payload, payload_kind="position_transition",
        schema_version=CANONICAL_PAYLOAD_SCHEMA, created_at=event_ts,
    )
    evt = append_event(
        conn,
        event_id=f"live_posevt_{str(event_id or '')}",
        event_type="position_transition",
        entity_type="position",
        entity_id=str(event_id or ""),
        payload_hash=ref.payload_hash,
        producer=producer,
        observed_at=event_ts,
        idempotency_key=str(event_id or ""),
        status="completed",
    )
    start_projection_run(
        conn, projection_run_id=LIVE_INCREMENT_RUN, run_kind="backfill",
        projection_name="live_increment", source_watermark="live",
        code_version=LIVE_INCREMENT_RUN, input_digest="live",
    )
    parent_event_id = f"live_decision_{str(decision_id)}" if str(decision_id or "") else ""
    evt.update(
        _link_trade_lineage(
            conn,
            child_event_id=str(evt["event_id"]),
            parent_event_id=parent_event_id,
            relation_key="position_to_decision",
            created_at=event_ts,
        )
        if evt.get("created")
        else _lineage_existing_status(
            conn,
            child_event_id=str(evt["event_id"]),
            parent_event_id=parent_event_id,
            relation_key="position_to_decision",
        )
    )
    return evt


MUTATION_STAGE_FIELDS = (
    "mutation_id", "idempotency_key", "control_surface", "scope_type", "scope_key",
    "action", "actor", "source", "producer", "run_id", "risk_class", "status",
    "evidence_fingerprint", "v16_command_id",
    "target_config_version", "target_config_hash",
    "committed_config_version", "committed_config_hash", "domain_hash",
)


def record_governance_mutation_event(
    conn: Any,
    *,
    mutation_id: str,
    stage: str,
    stage_timestamp: Any,
    row_fields: Mapping[str, Any],
    producer: str = "governance_coordinator",
) -> dict[str, Any]:
    """Mirror one live governance mutation lifecycle stage into canonical.

    Payload shape mirrors the historical backfill stage facts so governance
    consumers see live mutations on the same ``governance_effect`` stream.
    Idempotent per (mutation_id, stage).
    """
    base: dict[str, Any] = {}
    for field in MUTATION_STAGE_FIELDS:
        value = row_fields.get(field)
        base[field] = _json_value(value)
    base["evidence_refs"] = _json_value(
        row_fields.get("evidence_refs_json") or row_fields.get("evidence_refs")
    )
    base["mutation_id"] = str(mutation_id or "")
    base["created_at"] = _payload_iso(row_fields.get("created_at"))
    base["updated_at"] = _payload_iso(row_fields.get("updated_at"))
    payload: dict[str, Any] = dict(base)
    payload["stage"] = str(stage or "")
    payload["stage_timestamp"] = _payload_iso(stage_timestamp)
    if stage == "committed":
        for key in ("before", "target", "patch", "rollback"):
            payload[key] = _json_value(row_fields.get(f"{key}_json"))
    elif stage == "aborted":
        for key in ("error_stage", "error_type", "error_message"):
            payload[key] = str(row_fields.get(key) or "")
    elif stage == "rolled_back":
        payload["rollback_mutation_id"] = str(row_fields.get("rollback_mutation_id") or "")
    elif stage == "superseded":
        payload["superseded_by_mutation_id"] = str(row_fields.get("superseded_by_mutation_id") or "")
    ref = put_payload(
        conn, payload, payload_kind="governance_mutation_intent",
        schema_version=CANONICAL_PAYLOAD_SCHEMA, created_at=stage_timestamp,
    )
    evt = append_event(
        conn,
        event_id=f"live_gov_{str(mutation_id or '')}_{str(stage or '')}",
        event_type="governance_effect",
        entity_type="governance",
        entity_id=str(mutation_id or ""),
        payload_hash=ref.payload_hash,
        producer=producer,
        observed_at=stage_timestamp,
        idempotency_key=f"{str(mutation_id or '')}:{str(stage or '')}",
        status="completed",
    )
    start_projection_run(
        conn, projection_run_id=LIVE_INCREMENT_RUN, run_kind="backfill",
        projection_name="live_increment", source_watermark="live",
        code_version=LIVE_INCREMENT_RUN, input_digest="live",
    )
    return evt


def record_parameter_template_lifecycle_event(
    conn: Any,
    *,
    lifecycle_id: str,
    event_ts: Any,
    factor_id: str,
    event: str,
    status: str,
    description: str,
    reason: str = "",
    score: Any = 0.0,
    candidate_id: str = "",
    template_id: str = "",
    regime_key: str = "",
    details: Mapping[str, Any] | None = None,
    producer: str = "parameter_template_validation",
) -> dict[str, Any]:
    """Write parameter-template lifecycle history to canonical governance facts."""

    normalized_id = str(lifecycle_id or uuid.uuid4().hex)
    payload = {
        "lifecycle_id": normalized_id,
        "factor_id": str(factor_id or ""),
        "factor": str(factor_id or ""),
        "event": str(event or ""),
        "status": str(status or ""),
        "source": "parameter_template",
        "description": str(description or ""),
        "reason": str(reason or ""),
        "score": float(score or 0.0),
        "candidate_id": str(candidate_id or ""),
        "template_id": str(template_id or ""),
        "regime_key": str(regime_key or ""),
        "details": _json_value(details or {}),
    }
    return record_payload_event(
        conn,
        event_type="governance_effect",
        entity_type="parameter_template_lifecycle",
        entity_id=normalized_id,
        payload=payload,
        observed_at=event_ts,
        producer=producer,
        payload_kind="parameter_template_lifecycle",
        event_id=f"live_param_tpl_lifecycle_{normalized_id}",
        idempotency_key=f"parameter_template_lifecycle:{normalized_id}",
    )


def record_factor_lifecycle_event(
    conn: Any,
    *,
    lifecycle_id: str,
    event_ts: Any,
    factor: str,
    event: str,
    source: str,
    description: str = "",
    score: Any = 0.0,
    status: str = "UNKNOWN",
    reason: str = "",
    producer: str = "registry_adapter",
) -> dict[str, Any]:
    """Write one immutable runtime factor lifecycle fact to canonical_v2."""

    normalized_id = str(lifecycle_id or "")
    normalized_factor = str(factor or "")
    if not normalized_id or not normalized_factor:
        raise CanonicalV2Error("canonical factor lifecycle requires id and factor")
    payload = {
        "lifecycle_id": normalized_id,
        "timestamp": float(event_ts or 0.0),
        "event": str(event or ""),
        "factor": normalized_factor,
        "source": str(source or ""),
        "factor_source": str(source or ""),
        "description": str(description or ""),
        "score": float(score or 0.0),
        "status": str(status or "UNKNOWN"),
        "reason": str(reason or ""),
    }
    return record_payload_event(
        conn,
        event_type="factor_observation",
        entity_type="factor_lifecycle",
        entity_id=normalized_id,
        payload=payload,
        observed_at=event_ts,
        producer=producer,
        payload_kind="factor_lifecycle",
        event_id=f"live_factor_lifecycle_{normalized_id}",
        idempotency_key=f"factor_lifecycle:{normalized_id}",
    )


def _json_value(value: Any) -> Any:
    """Pass through JSON-ready values; stringify structured payloads."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (dict, list)):
        return value
    return str(value)


def record_governance_command_event(
    conn: Any,
    *,
    decision_id: str,
    run_id: str = "",
    decision_type: str = "",
    scope_type: str = "",
    scope_key: str = "",
    action: str = "",
    status: str = "",
    config_version: Any = None,
    config_hash: str = "",
    created_at: Any = None,
    legacy_payload_hash: str = "",
    evidence: Any = None,
    risk_verdict: Any = None,
    before: Any = None,
    after: Any = None,
    result: Any = None,
    rollback: Any = None,
    producer: str = "evolution_ledger",
) -> dict[str, Any]:
    """Mirror one live evolution command into canonical (idempotent)."""
    payload: dict[str, Any] = {
        "decision_id": str(decision_id or ""),
        "run_id": str(run_id or ""),
        "decision_type": str(decision_type or ""),
        "scope_type": str(scope_type or ""),
        "scope_key": str(scope_key or ""),
        "action": str(action or ""),
        "status": str(status or ""),
        "config_version": config_version,
        "config_hash": str(config_hash or ""),
        "created_at": _payload_iso(created_at),
        "legacy_payload_hash": str(legacy_payload_hash or ""),
        "evidence": _json_value(evidence),
        "risk_verdict": _json_value(risk_verdict),
        "before": _json_value(before),
        "after": _json_value(after),
        "result": _json_value(result),
        "rollback": _json_value(rollback),
    }
    ref = put_payload(
        conn, payload, payload_kind="evolution_decision",
        schema_version=CANONICAL_PAYLOAD_SCHEMA, created_at=created_at,
    )
    evt = append_event(
        conn,
        event_id=f"live_evol_{str(decision_id or '')}",
        event_type="governance_command",
        entity_type="governance",
        entity_id=str(decision_id or ""),
        payload_hash=ref.payload_hash,
        producer=producer,
        observed_at=created_at,
        idempotency_key=str(decision_id or ""),
        status="completed",
    )
    start_projection_run(
        conn, projection_run_id=LIVE_INCREMENT_RUN, run_kind="backfill",
        projection_name="live_increment", source_watermark="live",
        code_version=LIVE_INCREMENT_RUN, input_digest="live",
    )
    return evt


SAMPLE_ROW_COLUMNS = (
    "sample_id", "sample_type", "source_table", "source_id", "decision_id",
    "trade_id", "position_id", "symbol", "timeframe", "event_ts",
    "label_status", "integrity", "train_weight", "features_json",
    "verdict_json", "label_json", "trace_json", "evidence_contract_json",
    "config_version", "config_hash", "evolution_run_id",
    "system_contaminated", "governance_eligible", "governance_effective_weight",
    "governance_eligibility_version", "governance_ineligible_reason",
    "governance_eligibility_fingerprint", "content_fingerprint",
    "created_at", "updated_at",
)


def record_sample_row(
    conn: Any,
    row: Mapping[str, Any],
    producer: str = "learning_worker",
) -> dict[str, Any]:
    """Mirror one live training sample row into canonical (idempotent upsert)."""
    sample_id = str(row.get("sample_id") or "")
    if not sample_id:
        raise CanonicalV2Error("canonical sample row requires sample_id")
    values = tuple(row.get(column) for column in SAMPLE_ROW_COLUMNS)
    placeholders = ", ".join("%s" if _is_pg(conn) else "?" for _ in SAMPLE_ROW_COLUMNS)
    update_cols = [c for c in SAMPLE_ROW_COLUMNS if c != "sample_id"]
    conn.execute(
        _sql(
            conn,
            f"""
            INSERT INTO canonical_v2.training_sample_row ({', '.join(SAMPLE_ROW_COLUMNS)})
            VALUES ({placeholders})
            ON CONFLICT(sample_id) DO UPDATE SET
                {', '.join(f"{c}=excluded.{c}" for c in update_cols)}
            """,
        ),
        values,
    )
    return {"sample_id": sample_id, "created": True}


def purge_sample_rows_without_source(
    conn: Any,
    *,
    sample_type: str,
    source_table: str,
    source_table_ref: str,
    source_key_col: str,
) -> int:
    """Delete canonical training sample rows whose source row no longer exists.

    Single-writer maintenance op for the training-sample domain (owned by this
    module).  ``source_table_ref`` / ``source_key_col`` are trusted internal
    identifiers (caller-supplied constants only, never raw SQL input).
    """
    sub_select = (
        f"SELECT 1 FROM {source_table_ref} s "
        f"WHERE s.{source_key_col} = canonical_v2.training_sample_row.source_id"
    )
    cur = conn.execute(
        _sql(
            conn,
            "DELETE FROM canonical_v2.training_sample_row "
            "WHERE sample_type=? AND source_table=? AND NOT EXISTS (" + sub_select + ")",
        ),
        (sample_type, source_table),
    )
    return int(getattr(cur, "rowcount", 0))


def record_review(
    conn: Any,
    *,
    review_id: str,
    trade_id: str = "",
    position_id: str = "",
    entry_decision_id: str = "",
    exit_decision_id: str = "",
    entry_quality: Any = None,
    hold_quality: Any = None,
    exit_quality: Any = None,
    regime_fit_score: Any = None,
    execution_quality: Any = None,
    pnl: Any = None,
    mae: Any = None,
    mfe: Any = None,
    outcome_label: str = "",
    failure_tags: Any = None,
    summary_text: str = "",
    review: Any = None,
    created_at: Any = None,
    producer: str = "live_closed_position",
) -> dict[str, Any]:
    """Mirror one live trade review into canonical within the caller's transaction.

    Idempotent per ``review_id`` (event_id and idempotency_key derive from the
    review id), so replaying the same live review returns the existing event
    instead of duplicating payload/event/mapping rows.  The payload shape mirrors
    the historical canonical review backfill so
    ``canonical_v2_reader.iter_reviews`` returns live reviews on the same
    ``trade_review`` stream.  This is the mandatory live writer that keeps the
    posterior/effect arbitration fed after the full data flush (A1).  Only a
    newly created event from the live producer receives entry/exit lineage;
    learning/backfill/revision producers cannot retroactively complete an
    older trade chain.
    """
    if not review_id:
        raise CanonicalV2Error("canonical trade review requires review_id")
    payload: dict[str, Any] = {
        "review_id": str(review_id or ""),
        "trade_id": str(trade_id or ""),
        "position_id": str(position_id or ""),
        "entry_decision_id": str(entry_decision_id or ""),
        "exit_decision_id": str(exit_decision_id or ""),
        "entry_quality": entry_quality,
        "hold_quality": hold_quality,
        "exit_quality": exit_quality,
        "regime_fit_score": regime_fit_score,
        "execution_quality": execution_quality,
        "pnl": pnl,
        "mae": mae,
        "mfe": mfe,
        "outcome_label": str(outcome_label or ""),
        "failure_tags": _json_value(failure_tags),
        "summary_text": str(summary_text or ""),
        "created_at": _payload_iso(created_at),
        "review": _json_value(review),
    }
    ref = put_payload(
        conn,
        payload,
        payload_kind="trade_review",
        schema_version=CANONICAL_PAYLOAD_SCHEMA,
        created_at=created_at,
    )
    evt = append_event(
        conn,
        event_id=f"live_review_{str(review_id or '')}",
        event_type="trade_review",
        entity_type="review",
        entity_id=str(review_id or ""),
        payload_hash=ref.payload_hash,
        producer=producer,
        observed_at=created_at,
        idempotency_key=str(review_id or ""),
        status="completed",
    )
    start_projection_run(
        conn,
        projection_run_id=LIVE_INCREMENT_RUN,
        run_kind="backfill",
        projection_name="live_increment",
        source_watermark="live",
        code_version=LIVE_INCREMENT_RUN,
        input_digest="live",
    )
    parent_event_id = (
        f"live_decision_{str(entry_decision_id)}"
        if str(entry_decision_id or "")
        else ""
    )
    evt.update(
        _link_trade_lineage(
            conn,
            child_event_id=str(evt["event_id"]),
            parent_event_id=parent_event_id,
            relation_key="review_to_entry_decision",
            created_at=created_at,
        )
        if evt.get("created") and producer == "live_closed_position"
        else _lineage_existing_status(
            conn,
            child_event_id=str(evt["event_id"]),
            parent_event_id=parent_event_id,
            relation_key="review_to_entry_decision",
        )
    )
    exit_event_id = (
        f"live_decision_{str(exit_decision_id)}"
        if str(exit_decision_id or "")
        else ""
    )
    if evt.get("created") and producer == "live_closed_position":
        exit_lineage = _link_trade_lineage(
            conn,
            child_event_id=str(evt["event_id"]),
            parent_event_id=exit_event_id,
            relation_key="review_to_exit_decision",
            created_at=created_at,
        )
    else:
        exit_lineage = _lineage_existing_status(
            conn,
            child_event_id=str(evt["event_id"]),
            parent_event_id=exit_event_id,
            relation_key="review_to_exit_decision",
        )
    evt.update(
        {
            "exit_lineage_status": exit_lineage["lineage_status"],
            "exit_lineage_parent_event_id": exit_event_id,
            "exit_lineage_relation_type": TRADE_LINEAGE_RELATION_TYPES[
                "review_to_exit_decision"
            ],
        }
    )
    return evt



def put_state_version(
    conn: Any,
    *,
    state_version_id: str,
    entity_type: str,
    entity_id: str,
    version: int,
    valid_from: Any,
    source_event_id: str,
    payload_hash: str,
    valid_to: Any | None = None,
    created_at: Any | None = None,
) -> dict[str, Any]:
    """Insert one immutable version, rejecting changes to an existing version."""

    values = {
        "state_version_id": str(state_version_id or ""),
        "entity_type": str(entity_type or ""),
        "entity_id": str(entity_id or ""),
        "version": int(version),
        "valid_from": _db_time(conn, valid_from),
        "valid_to": _db_time(conn, valid_to) if valid_to is not None else None,
        "source_event_id": str(source_event_id or ""),
        "payload_hash": str(payload_hash or ""),
        "created_at": _db_time(conn, created_at),
    }
    if not values["state_version_id"] or not values["entity_type"] or not values["entity_id"]:
        raise CanonicalV2Error("state version identity must not be empty")
    if values["version"] <= 0:
        raise CanonicalV2Error("state version must be positive")
    existing = conn.execute(
        _sql(
            conn,
            """
            SELECT state_version_id, entity_type, entity_id, version, valid_from,
                   valid_to, source_event_id, payload_hash, created_at
            FROM canonical_v2.state_version
            WHERE state_version_id=?
               OR (entity_type=? AND entity_id=? AND version=?)
            LIMIT 1
            """,
        ),
        (
            values["state_version_id"],
            values["entity_type"],
            values["entity_id"],
            values["version"],
        ),
    ).fetchone()
    columns = (
        "state_version_id",
        "entity_type",
        "entity_id",
        "version",
        "valid_from",
        "valid_to",
        "source_event_id",
        "payload_hash",
        "created_at",
    )
    if existing is not None:
        result = _row_dict(existing, columns)
        for field in columns[:-1]:
            if not _same_value(field, result.get(field), values.get(field)):
                raise CanonicalV2ConflictError(
                    f"immutable canonical state version conflict field={field} "
                    f"state_version_id={result.get('state_version_id')}"
                )
        result["created"] = False
        return result
    conn.execute(
        _sql(
            conn,
            """
            INSERT INTO canonical_v2.state_version
                (state_version_id, entity_type, entity_id, version,
                 valid_from, valid_to, source_event_id, payload_hash, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
        ),
        tuple(values[field] for field in columns),
    )
    result = dict(values)
    result["created"] = True
    return result


def put_training_sample(
    conn: Any,
    *,
    sample_id: str,
    sample_type: str,
    source_event_ids: Iterable[str],
    feature_hash: str,
    feature_schema_hash: str,
    label_hash: str,
    trace_hash: str,
    evidence_contract: Mapping[str, Any],
    config_version: int,
    config_hash: str,
    horizon_minutes: int,
    target_source: str,
    sample_status: str = "candidate",
    created_at: Any | None = None,
    updated_at: Any | None = None,
) -> dict[str, Any]:
    """Insert an immutable training sample projection with source lineage."""

    source_ids = tuple(str(item or "") for item in source_event_ids)
    if not source_ids or any(not item for item in source_ids):
        raise CanonicalV2Error("training sample requires non-empty source_event_ids")
    if len(set(source_ids)) != len(source_ids):
        raise CanonicalV2Error("training sample source_event_ids must be unique")
    values = {
        "sample_id": str(sample_id or ""),
        "sample_type": str(sample_type or ""),
        "source_event_ids": list(source_ids),
        "feature_hash": str(feature_hash or ""),
        "feature_schema_hash": str(feature_schema_hash or ""),
        "label_hash": str(label_hash or ""),
        "trace_hash": str(trace_hash or ""),
        "evidence_contract": stable_json(dict(evidence_contract)),
        "config_version": int(config_version),
        "config_hash": str(config_hash or ""),
        "horizon_minutes": int(horizon_minutes),
        "target_source": str(target_source or ""),
        "sample_status": str(sample_status or ""),
        "created_at": _db_time(conn, created_at),
        "updated_at": _db_time(conn, updated_at or created_at),
    }
    for field in (
        "sample_id",
        "sample_type",
        "feature_hash",
        "feature_schema_hash",
        "label_hash",
        "trace_hash",
        "target_source",
    ):
        if not values[field]:
            raise CanonicalV2Error(f"training sample field must not be empty: {field}")
    if values["horizon_minutes"] <= 0:
        raise CanonicalV2Error("training sample horizon_minutes must be positive")
    json_expression = "CAST(? AS JSONB)" if _is_pg(conn) else "?"
    columns = (
        "sample_id",
        "sample_type",
        "source_event_ids",
        "feature_hash",
        "feature_schema_hash",
        "label_hash",
        "trace_hash",
        "evidence_contract",
        "config_version",
        "config_hash",
        "horizon_minutes",
        "target_source",
        "sample_status",
        "created_at",
        "updated_at",
    )
    existing = conn.execute(
        _sql(
            conn,
            "SELECT " + ", ".join(columns) + " FROM canonical_v2.training_sample WHERE sample_id=?",
        ),
        (values["sample_id"],),
    ).fetchone()
    if existing is not None:
        result = _row_dict(existing, columns)
        for field in columns[:-2]:
            if not _same_value(field, result.get(field), values.get(field)):
                raise CanonicalV2ConflictError(
                    f"immutable canonical training sample conflict field={field} "
                    f"sample_id={values['sample_id']}"
                )
        result["created"] = False
        return result
    conn.execute(
        _sql(
            conn,
            f"""
            INSERT INTO canonical_v2.training_sample
                (sample_id, sample_type, source_event_ids, feature_hash,
                 feature_schema_hash, label_hash, trace_hash, evidence_contract,
                 config_version, config_hash, horizon_minutes, target_source,
                 sample_status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, {json_expression}, ?, ?, ?, ?, ?, ?, ?)
            """,
        ),
        tuple(
            _db_array(conn, values[field]) if field == "source_event_ids" else values[field]
            for field in columns
        ),
    )
    result = dict(values)
    result["created"] = True
    return result


def put_dataset_manifest(
    conn: Any,
    *,
    dataset_id: str,
    purpose: str,
    training_window: str,
    horizon_minutes: int,
    query_contract_hash: str,
    sample_digest: str,
    feature_schema_hash: str,
    label_contract_hash: str,
    target_source: str,
    config_hash: str,
    source_watermark: str,
    code_commit: str,
    artifact_hash: str = "",
    status: str = "created",
    created_at: Any | None = None,
) -> dict[str, Any]:
    """Insert one immutable dataset manifest."""

    values = {
        "dataset_id": str(dataset_id or ""),
        "purpose": str(purpose or ""),
        "training_window": str(training_window or ""),
        "horizon_minutes": int(horizon_minutes),
        "query_contract_hash": str(query_contract_hash or ""),
        "sample_digest": str(sample_digest or ""),
        "feature_schema_hash": str(feature_schema_hash or ""),
        "label_contract_hash": str(label_contract_hash or ""),
        "target_source": str(target_source or ""),
        "config_hash": str(config_hash or ""),
        "source_watermark": str(source_watermark or ""),
        "code_commit": str(code_commit or ""),
        "artifact_hash": str(artifact_hash or ""),
        "status": str(status or ""),
        "created_at": _db_time(conn, created_at),
    }
    for field in (
        "dataset_id",
        "purpose",
        "training_window",
        "query_contract_hash",
        "sample_digest",
        "feature_schema_hash",
        "label_contract_hash",
        "target_source",
        "source_watermark",
        "code_commit",
        "status",
    ):
        if not values[field]:
            raise CanonicalV2Error(f"dataset manifest field must not be empty: {field}")
    if values["horizon_minutes"] <= 0:
        raise CanonicalV2Error("dataset manifest horizon_minutes must be positive")
    columns = (
        "dataset_id",
        "purpose",
        "training_window",
        "horizon_minutes",
        "query_contract_hash",
        "sample_digest",
        "feature_schema_hash",
        "label_contract_hash",
        "target_source",
        "config_hash",
        "source_watermark",
        "code_commit",
        "artifact_hash",
        "status",
        "created_at",
    )
    existing = conn.execute(
        _sql(
            conn,
            "SELECT " + ", ".join(columns) + " FROM canonical_v2.dataset_manifest WHERE dataset_id=?",
        ),
        (values["dataset_id"],),
    ).fetchone()
    if existing is not None:
        result = _row_dict(existing, columns)
        for field in columns[:-1]:
            if stable_json(result.get(field)) != stable_json(values.get(field)):
                raise CanonicalV2ConflictError(
                    f"immutable canonical dataset conflict field={field} "
                    f"dataset_id={values['dataset_id']}"
                )
        result["created"] = False
        return result
    conn.execute(
        _sql(
            conn,
            "INSERT INTO canonical_v2.dataset_manifest (" + ", ".join(columns) + ") VALUES (" + ", ".join("?" for _ in columns) + ")",
        ),
        tuple(values[field] for field in columns),
    )
    result = dict(values)
    result["created"] = True
    return result


def put_dataset_members(
    conn: Any,
    *,
    dataset_id: str,
    members: Iterable[tuple[str, int, str]],
) -> int:
    """Insert ID-only dataset membership rows and return the inserted count.

    Membership is immutable.  A retry with the same key is a no-op only when
    both the order and digest are identical; silently accepting a changed
    membership would make a dataset manifest non-reproducible.
    """

    inserted = 0
    for sample_id, sample_order, sample_digest in members:
        normalized = (str(dataset_id), str(sample_id), int(sample_order), str(sample_digest))
        if not normalized[0] or not normalized[1] or not normalized[3] or normalized[2] < 0:
            raise CanonicalV2Error("dataset membership fields are invalid")
        existing = conn.execute(
            _sql(
                conn,
                """
                SELECT sample_order, sample_digest
                FROM canonical_v2.dataset_manifest_member
                WHERE dataset_id=? AND sample_id=?
                """,
            ),
            normalized[:2],
        ).fetchone()
        if existing is not None:
            existing_order = _row_value(existing, "sample_order", 0)
            existing_digest = _row_value(existing, "sample_digest", 1)
            if int(existing_order) != normalized[2] or str(existing_digest) != normalized[3]:
                raise CanonicalV2ConflictError(
                    "immutable canonical dataset member conflict "
                    f"dataset_id={normalized[0]} sample_id={normalized[1]}"
                )
            continue
        cursor = conn.execute(
            _sql(
                conn,
                """
                INSERT INTO canonical_v2.dataset_manifest_member
                    (dataset_id, sample_id, sample_order, sample_digest)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(dataset_id, sample_id) DO NOTHING
                """,
            ),
            normalized,
        )
        if bool(getattr(cursor, "rowcount", 1)):
            inserted += 1
    return inserted
def start_projection_run(
    conn: Any,
    *,
    projection_run_id: str,
    run_kind: str,
    projection_name: str,
    source_watermark: str,
    code_version: str,
    input_digest: str,
    started_at: Any | None = None,
) -> dict[str, Any]:
    """Create an idempotent projection/backfill run record."""

    normalized = {
        "projection_run_id": str(projection_run_id or ""),
        "run_kind": str(run_kind or ""),
        "projection_name": str(projection_name or ""),
        "source_watermark": str(source_watermark or ""),
        "code_version": str(code_version or ""),
        "input_digest": str(input_digest or ""),
    }
    if not all(normalized.values()):
        raise CanonicalV2Error("projection run identity fields must not be empty")
    if normalized["run_kind"] not in {"projection", "backfill"}:
        raise CanonicalV2Error(f"unsupported projection run kind: {normalized['run_kind']}")

    columns = (
        "projection_run_id",
        "run_kind",
        "projection_name",
        "source_watermark",
        "code_version",
        "input_digest",
        "output_digest",
        "started_at",
        "finished_at",
        "status",
        "error_code",
    )
    existing = conn.execute(
        _sql(
            conn,
            "SELECT " + ", ".join(columns) + " FROM canonical_v2.projection_run "
            "WHERE projection_run_id=?",
        ),
        (normalized["projection_run_id"],),
    ).fetchone()
    if existing is None:
        existing = conn.execute(
            _sql(
                conn,
                "SELECT " + ", ".join(columns) + " FROM canonical_v2.projection_run "
                "WHERE run_kind=? AND projection_name=? AND source_watermark=? "
                "AND code_version=? AND input_digest=?",
            ),
            (
                normalized["run_kind"],
                normalized["projection_name"],
                normalized["source_watermark"],
                normalized["code_version"],
                normalized["input_digest"],
            ),
        ).fetchone()
    if existing is not None:
        result = _row_dict(existing, columns)
        for field in (
            "run_kind",
            "projection_name",
            "source_watermark",
            "code_version",
            "input_digest",
        ):
            if not _same_value(field, result.get(field), normalized[field]):
                raise CanonicalV2ConflictError(
                    "immutable canonical projection run conflict "
                    f"field={field} projection_run_id={normalized['projection_run_id']}"
                )
        result["created"] = False
        return result

    values = (
        normalized["projection_run_id"],
        normalized["run_kind"],
        normalized["projection_name"],
        normalized["source_watermark"],
        normalized["code_version"],
        normalized["input_digest"],
        _db_time(conn, started_at),
    )
    conn.execute(
        _sql(
            conn,
            """
            INSERT INTO canonical_v2.projection_run
                (projection_run_id, run_kind, projection_name, source_watermark,
                 code_version, input_digest, started_at, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'running')
            ON CONFLICT(projection_run_id) DO NOTHING
            """,
        ),
        values,
    )
    row = conn.execute(
        _sql(
            conn,
            """
            SELECT projection_run_id, run_kind, projection_name, source_watermark,
                   code_version, input_digest, output_digest, started_at,
                   finished_at, status, error_code
            FROM canonical_v2.projection_run
            WHERE projection_run_id=?
            """,
        ),
        (normalized["projection_run_id"],),
    ).fetchone()
    if row is None:
        raise CanonicalV2Error("projection run insert did not return a row")
    result = _row_dict(
        row,
        columns,
    )
    result["created"] = True
    return result


def finish_projection_run(
    conn: Any,
    *,
    projection_run_id: str,
    status: str,
    output_digest: str = "",
    error_code: str = "",
    finished_at: Any | None = None,
) -> None:
    """Finish the mutable projection control record, never a canonical fact."""

    if status not in {"completed", "failed", "aborted"}:
        raise CanonicalV2Error(f"unsupported projection terminal status: {status}")
    projection_run_id = str(projection_run_id or "")
    if not projection_run_id:
        raise CanonicalV2Error("projection_run_id must not be empty")
    output_digest = str(output_digest or "")
    error_code = str(error_code or "")
    existing = conn.execute(
        _sql(
            conn,
            "SELECT status, output_digest, error_code FROM canonical_v2.projection_run "
            "WHERE projection_run_id=?",
        ),
        (projection_run_id,),
    ).fetchone()
    if existing is None:
        raise CanonicalV2Error(f"missing projection run: {projection_run_id}")
    existing_status = str(_row_value(existing, "status", 0) or "")
    if existing_status != "running":
        if (
            existing_status == status
            and str(_row_value(existing, "output_digest", 1) or "") == output_digest
            and str(_row_value(existing, "error_code", 2) or "") == error_code
        ):
            return
        raise CanonicalV2ConflictError(
            f"immutable terminal projection run conflict: {projection_run_id}"
        )
    conn.execute(
        _sql(
            conn,
            """
            UPDATE canonical_v2.projection_run
            SET status=?, output_digest=?, error_code=?, finished_at=?
            WHERE projection_run_id=? AND status='running'
            """,
        ),
        (status, output_digest, error_code, _db_time(conn, finished_at), projection_run_id),
    )


__all__ = [
    "CANONICAL_EVENT_SCHEMA",
    "CANONICAL_PAYLOAD_SCHEMA",
    "CANONICAL_SAMPLE_SCHEMA",
    "CanonicalV2ConflictError",
    "CanonicalV2Error",
    "PayloadRef",
    "append_event",
    "append_relation",
    "canonical_payload",
    "finish_projection_run",
    "put_dataset_manifest",
    "put_dataset_members",
    "put_payload",
    "put_state_version",
    "put_training_sample",
    "read_payload",
    "start_projection_run",
]
