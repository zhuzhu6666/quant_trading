"""Canonical v2 facts, payloads, lineage, and dataset references.

This module is intentionally isolated from the existing ``state_v1`` writers.
It provides small, shared write authorities for the new ``canonical_v2``
schema; callers own the transaction and must not use it as an implicit reader
side effect.  Historical backfill and production cutover are separate gates.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from backend.services.state_payloads import stable_json


CANONICAL_SCHEMA = "canonical_v2"
CANONICAL_PAYLOAD_SCHEMA = "canonical_payload.v1"
CANONICAL_EVENT_SCHEMA = "canonical_event.v1"
CANONICAL_SAMPLE_SCHEMA = "canonical_training_sample.v1"

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

LEGACY_MAPPING_CONFIDENCE = frozenset({"exact", "strong", "weak", "unresolved"})


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
    return statement.replace("?", "%s") if _is_pg(conn) else statement


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


def _same_value(field: str, left: Any, right: Any) -> bool:
    return stable_json(_comparable(field, left)) == stable_json(_comparable(field, right))


def _hash_payload(payload_kind: str, schema_version: str, raw_bytes: bytes) -> str:
    prefix = (
        f"{CANONICAL_PAYLOAD_SCHEMA}\x00{payload_kind}\x00{schema_version}\x00"
    ).encode("utf-8")
    return hashlib.sha256(prefix + raw_bytes).hexdigest()


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
            payload_hash=_hash_payload(str(payload_kind), str(schema_version), raw_bytes),
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
    expected_hash = _hash_payload(
        str(fields["payload_kind"] or ""),
        str(fields["schema_version"] or ""),
        raw,
    )
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


def put_legacy_mapping(
    conn: Any,
    *,
    legacy_table: str,
    legacy_primary_key: str,
    canonical_event_id: str | None = None,
    canonical_payload_hash: str | None = None,
    classification: str,
    mapping_confidence: str,
    unresolved_reason: str = "",
    migration_run_id: str,
) -> dict[str, Any]:
    """Record one auditable legacy mapping or an explicit quarantine row."""

    values = {
        "legacy_table": str(legacy_table or ""),
        "legacy_primary_key": str(legacy_primary_key or ""),
        "canonical_event_id": str(canonical_event_id or "") or None,
        "canonical_payload_hash": str(canonical_payload_hash or "") or None,
        "classification": str(classification or ""),
        "mapping_confidence": str(mapping_confidence or ""),
        "unresolved_reason": str(unresolved_reason or ""),
        "migration_run_id": str(migration_run_id or ""),
    }
    for field in (
        "legacy_table",
        "legacy_primary_key",
        "classification",
        "migration_run_id",
    ):
        if not values[field]:
            raise CanonicalV2Error(f"legacy mapping field must not be empty: {field}")
    if values["mapping_confidence"] not in LEGACY_MAPPING_CONFIDENCE:
        raise CanonicalV2Error(
            f"unsupported legacy mapping confidence: {values['mapping_confidence']}"
        )
    if values["mapping_confidence"] == "unresolved":
        if not values["unresolved_reason"]:
            raise CanonicalV2Error("unresolved legacy mapping requires unresolved_reason")
    elif values["canonical_event_id"] is None and values["canonical_payload_hash"] is None:
        raise CanonicalV2Error("resolved legacy mapping requires a canonical reference")

    columns = (
        "legacy_table",
        "legacy_primary_key",
        "canonical_event_id",
        "canonical_payload_hash",
        "classification",
        "mapping_confidence",
        "unresolved_reason",
        "migration_run_id",
    )
    existing = conn.execute(
        _sql(
            conn,
            "SELECT " + ", ".join(columns) + " FROM canonical_v2.legacy_mapping "
            "WHERE legacy_table=? AND legacy_primary_key=? AND migration_run_id=?",
        ),
        (
            values["legacy_table"],
            values["legacy_primary_key"],
            values["migration_run_id"],
        ),
    ).fetchone()
    if existing is not None:
        result = _row_dict(existing, columns)
        for field in columns:
            if not _same_value(field, result.get(field), values.get(field)):
                raise CanonicalV2ConflictError(
                    "immutable canonical legacy mapping conflict "
                    f"field={field} legacy_table={values['legacy_table']} "
                    f"legacy_primary_key={values['legacy_primary_key']}"
                )
        result["created"] = False
        return result
    conn.execute(
        _sql(
            conn,
            "INSERT INTO canonical_v2.legacy_mapping (" + ", ".join(columns) + ") "
            "VALUES (" + ", ".join("?" for _ in columns) + ")",
        ),
        tuple(values[field] for field in columns),
    )
    result = dict(values)
    result["created"] = True
    return result


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
    "put_legacy_mapping",
    "put_payload",
    "put_state_version",
    "put_training_sample",
    "read_payload",
    "start_projection_run",
]
