"""Small, deterministic gzip archive for semantic-lossless JSON payloads."""

from __future__ import annotations

import gzip
import hashlib
import json
import time
from typing import Any


def _is_pg(conn: Any) -> bool:
    return conn.__class__.__module__.split(".", 1)[0] == "psycopg"


def _sql(conn: Any, sql: str) -> str:
    return sql.replace("?", "%s") if _is_pg(conn) else sql


def _table_exists(conn: Any) -> bool:
    if _is_pg(conn):
        row = conn.execute(
            """
            SELECT 1
            FROM information_schema.tables
            WHERE table_schema=current_schema() AND table_name='state_payload_archive'
            """
        ).fetchone()
        return row is not None
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='state_payload_archive'"
    ).fetchone()
    return row is not None


def archive_json_payload(
    conn: Any,
    *,
    source_table: str,
    source_id: str,
    payload_kind: str,
    raw_json: str,
) -> dict[str, Any] | None:
    """Store one compressed original and return its recovery metadata.

    The schema migration is intentionally separate. If an old SQLite fixture
    has no archive table, callers retain their existing full JSON behaviour;
    PostgreSQL production writers are expected to run the migration first.
    """
    if not _table_exists(conn):
        return None
    raw_bytes = str(raw_json or "").encode("utf-8")
    raw_sha256 = hashlib.sha256(raw_bytes).hexdigest()
    compressed = gzip.compress(raw_bytes, compresslevel=6, mtime=0)
    archive_hash = raw_sha256
    conn.execute(
        _sql(
            conn,
            """
            INSERT INTO state_payload_archive
            (archive_hash, source_table, source_id, payload_kind, codec,
             raw_sha256, raw_bytes, compressed_bytes, payload_bytes, created_at)
            VALUES (?, ?, ?, ?, 'gzip', ?, ?, ?, ?, ?)
            ON CONFLICT(archive_hash) DO NOTHING
            """,
        ),
        (
            archive_hash,
            str(source_table),
            str(source_id),
            str(payload_kind),
            raw_sha256,
            len(raw_bytes),
            len(compressed),
            compressed,
            time.time(),
        ),
    )
    return {
        "archive_hash": archive_hash,
        "raw_sha256": raw_sha256,
        "raw_bytes": len(raw_bytes),
        "compressed_bytes": len(compressed),
        "codec": "gzip",
    }


def restore_json_payload(conn: Any, archive_hash: str) -> str:
    """Restore and verify one archived JSON byte string."""
    row = conn.execute(
        _sql(
            conn,
            """
            SELECT codec, raw_sha256, raw_bytes, compressed_bytes, payload_bytes
            FROM state_payload_archive
            WHERE archive_hash=?
            """,
        ),
        (str(archive_hash or ""),),
    ).fetchone()
    if row is None:
        raise KeyError(f"missing state payload archive: {archive_hash}")
    if isinstance(row, dict):
        codec = row.get("codec")
        raw_sha256 = row.get("raw_sha256")
        raw_bytes = row.get("raw_bytes")
        compressed_bytes = row.get("compressed_bytes")
        payload_bytes = row.get("payload_bytes")
    else:
        codec, raw_sha256, raw_bytes, compressed_bytes, payload_bytes = row
    if str(codec or "") != "gzip":
        raise ValueError(f"unsupported payload archive codec: {codec}")
    compressed = bytes(payload_bytes or b"")
    if len(compressed) != int(compressed_bytes or 0):
        raise ValueError("state payload archive compressed length mismatch")
    restored = gzip.decompress(compressed)
    if len(restored) != int(raw_bytes or 0):
        raise ValueError("state payload archive raw length mismatch")
    restored_sha256 = hashlib.sha256(restored).hexdigest()
    if restored_sha256 != str(raw_sha256 or ""):
        raise ValueError("state payload archive SHA-256 mismatch")
    if restored_sha256 != str(archive_hash or ""):
        raise ValueError("state payload archive hash reference mismatch")
    return restored.decode("utf-8")


def resolve_json_payload(
    conn: Any,
    *,
    source_table: str,
    source_id: str,
    inline_json: Any,
    archive_hash: Any = "",
) -> str:
    """Return the complete JSON, preferring the verified archive reference.

    A non-empty archive reference is authoritative.  Missing/corrupt archive
    rows raise instead of silently treating a bounded hot projection as the
    original payload.  Empty references retain compatibility with pre-15
    fixtures and rows written before the archive columns existed.
    """
    reference = str(archive_hash or "").strip()
    if reference:
        return restore_json_payload(conn, reference)
    return str(inline_json or "{}")


def load_json_payload(
    conn: Any,
    *,
    source_table: str,
    source_id: str,
    inline_json: Any,
    archive_hash: Any = "",
    default: Any = None,
) -> Any:
    """Decode one inline-or-archived JSON payload without losing precision."""
    if not str(archive_hash or "").strip() and isinstance(inline_json, (dict, list)):
        return inline_json
    raw = resolve_json_payload(
        conn,
        source_table=source_table,
        source_id=source_id,
        inline_json=inline_json,
        archive_hash=archive_hash,
    )
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default if default is not None else {}


def json_text(value: Any) -> str:
    """Use the same stable JSON representation as state writers."""
    return json.dumps(value, ensure_ascii=False, default=str)


def supervisor_trace_archive_text(
    *,
    context_json: Any,
    verdict_json: Any,
    risk_verdict_json: Any,
    execution_json: Any,
) -> str:
    """Wrap trace JSON fields without reparsing or rounding their numbers."""

    return json.dumps(
        {
            "schema_version": "supervisor_trace_payload_archive.v1",
            "context_json": str(context_json or "{}"),
            "verdict_json": str(verdict_json or "{}"),
            "risk_verdict_json": str(risk_verdict_json or "{}"),
            "execution_json": str(execution_json or "{}"),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def load_supervisor_trace_archive(conn: Any, archive_hash: str) -> dict[str, str]:
    """Restore the exact JSON text for each archived trace field."""

    payload = json.loads(restore_json_payload(conn, archive_hash))
    if not isinstance(payload, dict):
        raise ValueError("invalid supervisor trace archive payload")
    if str(payload.get("schema_version") or "") != "supervisor_trace_payload_archive.v1":
        raise ValueError("unsupported supervisor trace archive payload")
    return {
        key: str(payload.get(key) or "{}")
        for key in (
            "context_json",
            "verdict_json",
            "risk_verdict_json",
            "execution_json",
        )
    }
