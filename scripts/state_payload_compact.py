#!/usr/bin/env python3
"""Intern large state payloads without removing event occurrences.

The default operation is read-only.  ``--apply`` requires an explicit
maintenance id and stores every original payload before replacing the large
JSON columns with references.  It never performs a physical table rewrite by
default.  ``--rewrite`` is a separate, explicit physical-rewrite request and
requires a distinct rewrite maintenance id.  ``--verify`` checks row metadata
digests and payload hashes.  ``--rollback`` restores the legacy JSON
projections from the payload tables; it never deletes an event.

The operator must stop all state writers before ``--apply``.  This script does
not stop systemd services or touch trading processes.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import sys
import time
from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Iterable, Iterator
from uuid import uuid4

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.core.db import (  # noqa: E402
    STATE_DB,
    connect_sqlite,
    get_state_pg_conn,
    is_state_db_path,
    state_table_columns,
    state_table_exists,
)
from backend.services.state_payloads import (  # noqa: E402
    BRAIN_ACTION_PLAN_EVAL_PAYLOAD_TABLE,
    MUTATION_PAYLOAD_TABLE,
    RUNTIME_CONFIG_PAYLOAD_TABLE,
    ensure_state_payload_schema,
    mutation_payload_hash,
    payload_hash,
)
from backend.services.supervisor_payload_contract import (  # noqa: E402
    bounded_review_projection,
    compact_supervisor_mapping,
)
from backend.services.state_payload_archive import (  # noqa: E402
    archive_json_payload,
    load_supervisor_trace_archive,
    restore_json_payload,
    supervisor_trace_archive_text,
)


RUNTIME_NAMESPACE = "runtime_config_payload.v1"
EVAL_NAMESPACE = "brain_action_plan_eval_payload.v1"
MANIFEST_VERSION = "state_payload_compaction_manifest.v1"
DEFAULT_ROW_BATCH_SIZE = 256
DEFAULT_READ_STATEMENT_TIMEOUT_SECONDS = 120
DEFAULT_WRITE_STATEMENT_TIMEOUT_SECONDS = 900
DEFAULT_BATCH_PAUSE_SECONDS = 0.01
DEFAULT_PAYLOAD_CHUNK_ROWS = 4096
FULL_PAYLOAD_STATEMENT_TIMEOUT_SECONDS = 300
SUPERVISOR_REVIEW_TABLES = ("position_supervisor_trace", "trade_outcome_review")
SUPERVISOR_RECURSIVE_KEYS = frozenset(
    {"supervisor_state", "latest_supervisor", "latest_protection", "candidate"}
)
MAX_REPORTED_RECURSIVE_PATH_PARTS = 14


def _is_pg(conn: Any) -> bool:
    return conn.__class__.__module__.split(".", 1)[0] == "psycopg"


def _sql(conn: Any, statement: str) -> str:
    return statement.replace("?", "%s") if _is_pg(conn) else statement


def _value(row: Any, key: str, index: int = 0, default: Any = None) -> Any:
    if row is None:
        return default
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        try:
            return row[index]
        except (KeyError, IndexError, TypeError):
            return default


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _configure_pg_session(conn: Any, *, write: bool) -> None:
    """Put hard resource bounds on operator connections.

    The compactor is deliberately run outside the application, but it still
    must not inherit an unrestricted PostgreSQL session.  The write timeout is
    longer because an explicitly requested physical rewrite can legitimately
    take minutes;
    read-only scans are bounded so a bad estimate cannot run indefinitely.
    """

    timeout_seconds = (
        DEFAULT_WRITE_STATEMENT_TIMEOUT_SECONDS
        if write
        else DEFAULT_READ_STATEMENT_TIMEOUT_SECONDS
    )
    settings = (
        f"SET statement_timeout = '{timeout_seconds}s'",
        "SET lock_timeout = '5s'",
        "SET idle_in_transaction_session_timeout = '60s'",
        "SET max_parallel_workers_per_gather = 0",
        "SET work_mem = '16MB'",
        "SET maintenance_work_mem = '128MB'",
    )
    for statement in settings:
        conn.execute(statement)
    conn.commit()


def _connect(db_path: str | Path, *, write: bool):
    if is_state_db_path(db_path):
        conn = get_state_pg_conn(read_only=not write)
        _configure_pg_session(conn, write=write)
        return conn
    conn = connect_sqlite(db_path)
    conn.row_factory = __import__("sqlite3").Row
    return conn


def _required_schema(conn: Any) -> list[str]:
    requirements = {
        "runtime_config_snapshot": {"config_version", "config_hash", "config_json", "payload_hash"},
        "brain_action_plan_eval": {
            "eval_id",
            "plan_id",
            "comparison_json",
            "evidence_refs_json",
            "boundary_json",
            "payload_hash",
            "evaluation_run_id",
        },
        "evolution_decision": {
            "decision_id",
            "decision_json",
            "payload_hash",
            "canonical_event_id",
            "projection_type",
        },
        RUNTIME_CONFIG_PAYLOAD_TABLE: {"payload_hash", "config_json"},
        BRAIN_ACTION_PLAN_EVAL_PAYLOAD_TABLE: {
            "payload_hash",
            "comparison_json",
            "evidence_refs_json",
            "boundary_json",
        },
        MUTATION_PAYLOAD_TABLE: {
            "payload_hash",
            "evidence_json",
            "risk_verdict_json",
            "before_json",
            "after_json",
            "result_json",
            "rollback_json",
        },
    }
    missing: list[str] = []
    for table, columns in requirements.items():
        if not state_table_exists(conn, table):
            missing.append(f"table:{table}")
            continue
        missing.extend(
            f"column:{table}.{column}"
            for column in sorted(columns - state_table_columns(conn, table))
        )
    return missing


def _iter_rows(
    conn: Any,
    query: str,
    params: tuple[Any, ...] = (),
    *,
    batch_size: int = DEFAULT_ROW_BATCH_SIZE,
) -> Iterator[Any]:
    """Stream rows without materialising a PostgreSQL result set in RAM."""

    if _is_pg(conn):
        cursor = conn.cursor(
            name=f"state_payload_{uuid4().hex}",
            withhold=True,
        )
        cursor.itersize = batch_size
        try:
            cursor.execute(_sql(conn, query), params)
            while True:
                rows = cursor.fetchmany(batch_size)
                if not rows:
                    break
                yield from rows
        finally:
            cursor.close()
        return

    cursor = conn.execute(_sql(conn, query), params)
    while True:
        rows = cursor.fetchmany(batch_size)
        if not rows:
            break
        yield from rows


class _PayloadStatsAccumulator:
    def __init__(self, digest_fn: Any | None = None) -> None:
        self._digest_fn = digest_fn
        self._counts: Counter[str] = Counter()
        self._totals: Counter[str] = Counter()
        self._max_lengths: dict[str, int] = {}
        self._total_bytes = 0
        self._rows = 0

    def add(self, raw: Any) -> None:
        value = str(raw or "{}")
        digest = (
            str(self._digest_fn(value))
            if self._digest_fn is not None
            else hashlib.sha256(value.encode("utf-8")).hexdigest()
        )
        size = len(value.encode("utf-8"))
        self._counts[digest] += 1
        self._totals[digest] += size
        self._max_lengths[digest] = max(self._max_lengths.get(digest, 0), size)
        self._total_bytes += size
        self._rows += 1

    def result(self) -> dict[str, Any]:
        redundant = sum(
            self._totals[key] - self._max_lengths[key]
            for key in self._totals
        )
        duplicate_payloads = sum(1 for count in self._counts.values() if count > 1)
        return {
            "rows": self._rows,
            "payload_total": len(self._counts),
            "distinct_payloads": len(self._counts),
            "duplicate_payload_count": duplicate_payloads,
            "duplicate_rows": max(0, self._rows - len(self._counts)),
            "duplicate_payload_rows": max(0, self._rows - len(self._counts)),
            "payload_bytes": self._total_bytes,
            "unique_payload_bytes": sum(self._max_lengths.values()),
            "redundant_payload_bytes": redundant,
            "largest_repeat_count": max(self._counts.values(), default=0),
            "payload_reference_counts": {
                key: int(self._counts[key]) for key in sorted(self._counts)
            },
            "payload_reference_bytes": {
                key: int(self._max_lengths[key])
                for key in sorted(self._max_lengths)
            },
        }


def _payload_stats(
    values: Iterable[str],
    *,
    digest_fn: Any | None = None,
) -> dict[str, Any]:
    accumulator = _PayloadStatsAccumulator(digest_fn)
    for raw in values:
        accumulator.add(raw)
    return accumulator.result()


def _scan_rows(
    rows: Iterable[Any],
    *,
    payload_fn: Any,
    metadata_fn: Any,
    event_id_fn: Any,
    digest_fn: Any,
) -> dict[str, Any]:
    """Collect payload and metadata statistics in one streaming pass."""

    payloads = _PayloadStatsAccumulator(digest_fn)
    metadata_digest = hashlib.sha256()
    event_ids: set[str] = set()
    count = 0
    for row in rows:
        payloads.add(payload_fn(row))
        metadata_digest.update(_json(metadata_fn(row)).encode("utf-8"))
        metadata_digest.update(b"\n")
        event_ids.add(str(event_id_fn(row)))
        count += 1
    result = payloads.result()
    result.update(
        {
            "count": count,
            "total_rows": count,
            "unique_event_ids": len(event_ids),
            "metadata_digest": metadata_digest.hexdigest(),
        }
    )
    return result


def _relation_sizes(conn: Any) -> dict[str, int]:
    tables = [
        "runtime_config_snapshot",
        "brain_action_plan_eval",
        "evolution_decision",
        "position_supervisor_trace",
        "trade_outcome_review",
        "state_payload_archive",
        RUNTIME_CONFIG_PAYLOAD_TABLE,
        BRAIN_ACTION_PLAN_EVAL_PAYLOAD_TABLE,
        MUTATION_PAYLOAD_TABLE,
    ]
    sizes: dict[str, int] = {}
    if _is_pg(conn):
        for table in tables:
            try:
                row = conn.execute(
                    "SELECT pg_total_relation_size(%s::regclass) AS bytes",
                    (table,),
                ).fetchone()
                sizes[table] = int(_value(row, "bytes", 0, 0) or 0)
            except Exception:
                sizes[table] = 0
        return sizes
    try:
        page_count = int(_value(conn.execute("PRAGMA page_count").fetchone(), 0, 0) or 0)
        page_size = int(_value(conn.execute("PRAGMA page_size").fetchone(), 0, 0) or 0)
        sizes["sqlite_database"] = page_count * page_size
    except Exception:
        sizes["sqlite_database"] = 0
    return sizes


def _maintenance_preflight(conn: Any) -> dict[str, Any]:
    """Check blockers without stopping or mutating any process."""

    if not _is_pg(conn):
        return {
            "ok": True,
            "mode": "sqlite_manual_maintenance_required",
            "active_writer_sessions": [],
            "pending_mutations": 0,
        }
    active_rows = conn.execute(
        """SELECT pid, application_name, state, query
           FROM pg_stat_activity
           WHERE datname=current_database()
             AND pid<>pg_backend_pid()
             AND (state='idle in transaction'
                  OR (state='active' AND query ~* '(insert|update|delete|alter|create|drop|vacuum|copy)'))"""
    ).fetchall()
    pending = 0
    if state_table_exists(conn, "governance_mutation_intent"):
        row = conn.execute(
            """SELECT COUNT(*) AS n FROM governance_mutation_intent
               WHERE status IN ('reserved', 'prepared')"""
        ).fetchone()
        pending = int(_value(row, "n", 0, 0) or 0)
    sessions = [
        {
            "pid": int(_value(row, "pid", 0, 0) or 0),
            "application_name": str(_value(row, "application_name", 1, "") or ""),
            "state": str(_value(row, "state", 2, "") or ""),
            "query": str(_value(row, "query", 3, "") or "")[:300],
        }
        for row in active_rows
    ]
    return {
        "ok": not sessions and pending == 0,
        "mode": "postgresql",
        "active_writer_sessions": sessions,
        "pending_mutations": pending,
    }


def _runtime_rows(
    conn: Any,
    *,
    batch_size: int = DEFAULT_ROW_BATCH_SIZE,
) -> Iterator[Any]:
    return _iter_rows(
        conn,
        """SELECT s.config_version, s.config_hash, s.source, s.run_id,
                  s.mutation_id, s.created_at, s.payload_hash,
                  COALESCE(p.config_json, NULLIF(s.config_json, '')) AS config_payload
           FROM runtime_config_snapshot s
           LEFT JOIN runtime_config_payload p ON p.payload_hash=s.payload_hash
           ORDER BY s.config_version""",
        batch_size=batch_size,
    )


def _eval_rows(
    conn: Any,
    *,
    batch_size: int = DEFAULT_ROW_BATCH_SIZE,
) -> Iterator[Any]:
    return _iter_rows(
        conn,
        """SELECT e.eval_id, e.plan_id, e.snapshot_id, e.action_type,
                  e.scope_type, e.status, e.comparison_verdict,
                  e.coverage_score, e.created_at, e.payload_hash,
                  e.evaluation_run_id,
                  COALESCE(p.comparison_json, NULLIF(e.comparison_json, '')) AS comparison_payload,
                  COALESCE(p.evidence_refs_json, NULLIF(e.evidence_refs_json, '')) AS evidence_payload,
                  COALESCE(p.boundary_json, NULLIF(e.boundary_json, '')) AS boundary_payload
           FROM brain_action_plan_eval e
           LEFT JOIN brain_action_plan_eval_payload p ON p.payload_hash=e.payload_hash
           ORDER BY e.created_at, e.eval_id""",
        batch_size=batch_size,
    )


_MUTATION_ROW_KEYS = (
    "decision_id", "run_id", "decision_type", "decision_json",
    "created_at", "payload_hash", "canonical_event_id", "projection_type",
    "evidence_json", "risk_verdict_json", "before_json", "after_json",
    "result_json", "rollback_json",
)


def _mutation_rows(
    conn: Any,
    *,
    batch_size: int = DEFAULT_ROW_BATCH_SIZE,
) -> Iterator[Any]:
    # Converged 8-column shape: rich semantic fields live in decision_json and
    # evidence/risk_verdict/before/after/result/rollback stay interned via
    # payload_hash -> mutation_payload. Rebuild the same key order the original
    # wide SELECT exposed so downstream name/index access keeps working.
    for row in _iter_rows(
        conn,
        """SELECT d.decision_id, d.run_id, d.decision_type, d.decision_json,
                  d.created_at, d.payload_hash, d.canonical_event_id, d.projection_type,
                  p.evidence_json, p.risk_verdict_json, p.before_json, p.after_json,
                  p.result_json, p.rollback_json
           FROM evolution_decision d
           LEFT JOIN mutation_payload p ON p.payload_hash=d.payload_hash
           ORDER BY d.created_at, d.decision_id""",
        batch_size=batch_size,
    ):
        r = {k: row[i] for i, k in enumerate(_MUTATION_ROW_KEYS)}
        meta = _loads_object(r["decision_json"])
        yield {
            "decision_id": r["decision_id"],
            "run_id": r["run_id"],
            "decision_type": r["decision_type"],
            "scope_type": meta.get("scope_type", ""),
            "scope_key": meta.get("scope_key", ""),
            "action": meta.get("action", ""),
            "status": meta.get("status", ""),
            "config_version": meta.get("config_version", 0),
            "config_hash": meta.get("config_hash", ""),
            "created_at": r["created_at"],
            "result_json": r.get("result_json") or "",
            "payload_hash": r["payload_hash"],
            "canonical_event_id": r["canonical_event_id"],
            "projection_type": r["projection_type"],
            "evidence_payload": r.get("evidence_json") or "",
            "risk_payload": r.get("risk_verdict_json") or "",
            "before_payload": r.get("before_json") or "",
            "after_payload": r.get("after_json") or "",
            "result_payload": r.get("result_json") or "",
            "rollback_payload": r.get("rollback_json") or "",
        }


def _pg_payload_definition(domain: str) -> dict[str, str]:
    """Return server-side expressions for one exact payload domain.

    PostgreSQL 16 provides the built-in ``sha256(bytea)`` function.  Keeping
    the hash and byte aggregation in the database is important here: a
    single historical mutation row can be much larger than its sampled
    average, so transferring batches of JSON to Python is not a safe memory
    bound.
    """

    if domain == "runtime_config_snapshot":
        return {
            "table": "runtime_config_snapshot",
            "event_id": "s.config_version",
            "stored_hash": "s.payload_hash",
            "from": "runtime_config_snapshot s LEFT JOIN runtime_config_payload p ON p.payload_hash=s.payload_hash",
            "raw": "COALESCE(p.config_json, NULLIF(s.config_json, ''), '{}')",
            "namespace": "runtime_config_payload.v1",
        }
    if domain == "brain_action_plan_eval":
        return {
            "table": "brain_action_plan_eval",
            "event_id": "e.eval_id",
            "stored_hash": "e.payload_hash",
            "from": "brain_action_plan_eval e LEFT JOIN brain_action_plan_eval_payload p ON p.payload_hash=e.payload_hash",
            "raw": (
                "COALESCE(p.comparison_json, NULLIF(e.comparison_json, ''), '{}') "
                "|| chr(0) || COALESCE(p.evidence_refs_json, NULLIF(e.evidence_refs_json, ''), '{}') "
                "|| chr(0) || COALESCE(p.boundary_json, NULLIF(e.boundary_json, ''), '{}')"
            ),
            "namespace": "brain_action_plan_eval_payload.v1",
        }
    if domain == "evolution_decision":
        parts = {
            "after_json": "COALESCE(p.after_json, '{}')",
            "before_json": "COALESCE(p.before_json, '{}')",
            "evidence_json": "COALESCE(p.evidence_json, '{}')",
            "result_json": "COALESCE(p.result_json, '{}')",
            "risk_verdict_json": "COALESCE(p.risk_verdict_json, '{}')",
            "rollback_json": "COALESCE(p.rollback_json, '{}')",
        }
        raw = " || chr(0) || ".join(
            f"'{key}=' || {value}" for key, value in parts.items()
        )
        return {
            "table": "evolution_decision",
            "event_id": "d.decision_id",
            "stored_hash": "d.payload_hash",
            "from": "evolution_decision d LEFT JOIN mutation_payload p ON p.payload_hash=d.payload_hash",
            "raw": raw,
            "namespace": "mutation_payload.v1",
        }
    raise ValueError(f"unknown_payload_domain:{domain}")


def _pg_payload_hash_input_and_size(domain: str) -> tuple[str, str]:
    """Build bytea expressions because PostgreSQL text cannot contain NUL."""

    zero = "decode('00', 'hex')"
    if domain == "runtime_config_snapshot":
        definition = _pg_payload_definition(domain)
        raw = definition["raw"]
        raw_bytes = f"convert_to(({raw}), 'UTF8')"
        return (
            "convert_to('runtime_config_payload.v1', 'UTF8') || "
            f"{zero} || {raw_bytes}",
            f"octet_length({raw_bytes})",
        )
    if domain == "brain_action_plan_eval":
        parts = (
            "COALESCE(p.comparison_json, NULLIF(e.comparison_json, ''), '{}')",
            "COALESCE(p.evidence_refs_json, NULLIF(e.evidence_refs_json, ''), '{}')",
            "COALESCE(p.boundary_json, NULLIF(e.boundary_json, ''), '{}')",
        )
        segments = [f"convert_to(({part}), 'UTF8')" for part in parts]
        hash_input = (
            "convert_to('brain_action_plan_eval_payload.v1', 'UTF8') || "
            f"{zero} || " + f" || {zero} || ".join(segments)
        )
        payload_size = " + ".join(f"octet_length({segment})" for segment in segments)
        return hash_input, f"({payload_size}) + 2"
    if domain == "evolution_decision":
        parts = (
            ("after_json", "COALESCE(p.after_json, '{}')"),
            ("before_json", "COALESCE(p.before_json, '{}')"),
            ("evidence_json", "COALESCE(p.evidence_json, '{}')"),
            ("result_json", "COALESCE(p.result_json, '{}')"),
            ("risk_verdict_json", "COALESCE(p.risk_verdict_json, '{}')"),
            ("rollback_json", "COALESCE(p.rollback_json, '{}')"),
        )
        segments = [
            f"convert_to('{key}=', 'UTF8') || convert_to(({value}), 'UTF8')"
            for key, value in parts
        ]
        hash_input = (
            "convert_to('mutation_payload.v1', 'UTF8') || "
            f"{zero} || " + f" || {zero} || ".join(segments)
        )
        payload_size = " + ".join(f"octet_length({segment})" for segment in segments)
        return hash_input, f"({payload_size}) + 5"
    raise ValueError(f"unknown_payload_domain:{domain}")


def _pg_payload_stats(
    conn: Any,
    domain: str,
    *,
    batch_size: int = DEFAULT_ROW_BATCH_SIZE,
    chunk_rows: int = DEFAULT_PAYLOAD_CHUNK_ROWS,
) -> dict[str, Any]:
    definition = _pg_payload_definition(domain)
    hash_input, payload_size = _pg_payload_hash_input_and_size(domain)
    source = (
        f"SELECT {definition['event_id']} AS event_id, "
        f"{hash_input} AS hash_input, {payload_size} AS payload_bytes "
        f"FROM {definition['from']}"
    )
    merged_counts: Counter[str] = Counter()
    merged_totals: Counter[str] = Counter()
    merged_max_lengths: dict[str, int] = {}
    last_event_id: Any | None = None
    total_rows = 0
    payload_bytes = 0
    def consume(rows: Iterable[Any]) -> None:
        nonlocal total_rows, payload_bytes
        for row in rows:
            digest = str(_value(row, "payload_hash") or "")
            refs = int(_value(row, "refs", 1, 0) or 0)
            total = int(_value(row, "total_bytes", 2, 0) or 0)
            unique = int(_value(row, "unique_bytes", 3, 0) or 0)
            merged_counts[digest] += refs
            merged_totals[digest] += total
            merged_max_lengths[digest] = max(
                merged_max_lengths.get(digest, 0), unique
            )
            total_rows += refs
            payload_bytes += total

    if chunk_rows == 0:
        conn.execute(
            f"SET statement_timeout = '{FULL_PAYLOAD_STATEMENT_TIMEOUT_SECONDS}s'"
        )
        rows = conn.execute(
            f"""
                SELECT payload_hash,
                       COUNT(*) AS refs,
                       SUM(payload_bytes)::bigint AS total_bytes,
                       MAX(payload_bytes)::bigint AS unique_bytes
                  FROM (
                        SELECT encode(sha256(source_rows.hash_input), 'hex') AS payload_hash,
                               source_rows.payload_bytes
                          FROM ({source}) AS source_rows
                       ) AS hashed_rows
                 GROUP BY payload_hash
                 ORDER BY payload_hash
            """
        ).fetchall()
        consume(rows)
        conn.rollback()
    else:
        while True:
            where = ""
            params: tuple[Any, ...] = ()
            if last_event_id is not None:
                where = "WHERE source_rows.event_id > %s"
                params = (last_event_id,)
            query = f"""
                WITH chunk AS (
                    SELECT source_rows.event_id,
                           encode(sha256(source_rows.hash_input), 'hex') AS payload_hash,
                           source_rows.payload_bytes
                      FROM ({source}) AS source_rows
                     {where}
                     ORDER BY source_rows.event_id
                     LIMIT {int(chunk_rows)}
                ), grouped AS (
                    SELECT payload_hash,
                           COUNT(*) AS refs,
                           SUM(payload_bytes)::bigint AS total_bytes,
                           MAX(payload_bytes)::bigint AS unique_bytes
                      FROM chunk
                     GROUP BY payload_hash
                )
                SELECT payload_hash, refs, total_bytes, unique_bytes,
                       (SELECT MAX(event_id) FROM chunk) AS next_event_id
                  FROM grouped
                 ORDER BY payload_hash
            """
            rows = conn.execute(query, params).fetchall()
            if not rows:
                conn.rollback()
                break
            next_event_id = _value(rows[0], "next_event_id", 4)
            if next_event_id is None or next_event_id == last_event_id:
                conn.rollback()
                raise RuntimeError(f"payload_chunk_did_not_advance:{domain}")
            consume(rows)
            last_event_id = next_event_id
            conn.rollback()
            time.sleep(DEFAULT_BATCH_PAUSE_SECONDS)
    duplicate_payload_count = sum(1 for count in merged_counts.values() if count > 1)
    duplicate_rows = sum(max(0, count - 1) for count in merged_counts.values())
    unique_payload_bytes = sum(merged_max_lengths.values())
    redundant_payload_bytes = sum(
        merged_totals[key] - merged_max_lengths[key] for key in merged_totals
    )
    return {
        "rows": total_rows,
        "payload_total": len(merged_counts),
        "distinct_payloads": len(merged_counts),
        "duplicate_payload_count": duplicate_payload_count,
        "duplicate_rows": duplicate_rows,
        "duplicate_payload_rows": duplicate_rows,
        "payload_bytes": payload_bytes,
        "unique_payload_bytes": unique_payload_bytes,
        "redundant_payload_bytes": redundant_payload_bytes,
        "largest_repeat_count": max(merged_counts.values(), default=0),
        "payload_reference_counts": {
            key: int(merged_counts[key]) for key in sorted(merged_counts)
        },
        "payload_reference_bytes": {
            key: int(merged_max_lengths[key])
            for key in sorted(merged_max_lengths)
        },
    }


def _pg_payload_hash_mismatches(
    conn: Any,
    domain: str,
    *,
    batch_size: int = DEFAULT_ROW_BATCH_SIZE,
) -> int:
    definition = _pg_payload_definition(domain)
    hash_input, _payload_size = _pg_payload_hash_input_and_size(domain)
    conn.execute(
        f"SET statement_timeout = '{FULL_PAYLOAD_STATEMENT_TIMEOUT_SECONDS}s'"
    )
    query = f"""
        SELECT COUNT(*) AS mismatches
          FROM (
                SELECT {definition['stored_hash']} AS stored_payload_hash,
                       encode(
                           sha256({hash_input}),
                           'hex'
                       ) AS expected_payload_hash
                  FROM {definition['from']}
               ) AS checked_rows
         WHERE COALESCE(stored_payload_hash, '') <> expected_payload_hash
    """
    try:
        row = conn.execute(query).fetchone()
    finally:
        conn.rollback()
    return int(_value(row, "mismatches", 0, 0) or 0)


def _audit_rows(
    conn: Any,
    *,
    batch_size: int = DEFAULT_ROW_BATCH_SIZE,
) -> Iterator[Any]:
    """Read only compact audit metadata on PostgreSQL.

    API linkage needs the decision id embedded in result JSON and the
    endpoint/user markers in evidence JSON, not the full JSON documents.  The
    server extracts those small facts before they cross the process boundary.
    SQLite fixtures keep using the legacy row shape.
    """

    if not _is_pg(conn):
        return _mutation_rows(conn, batch_size=batch_size)
    result_json = "COALESCE(p.result_json, '{}')"
    evidence_json = "COALESCE(p.evidence_json, '{}')"
    return _iter_rows(
        conn,
        f"""SELECT d.decision_id, d.run_id, d.decision_type,
                         (d.decision_json->>'scope_type') AS scope_type,
                         (d.decision_json->>'action') AS action,
                         (d.decision_json->>'status') AS status,
                         (d.decision_json->>'config_hash') AS config_hash,
                         d.created_at,
                         d.canonical_event_id, d.projection_type,
                         substring({result_json} FROM
                             '"decision_id"[[:space:]]*:[[:space:]]*"([^\\"]+)"'
                         ) AS direct_decision_id,
                         (
                           strpos({evidence_json}, '"endpoint"') > 0
                           OR strpos({evidence_json}, '"user"') > 0
                         ) AS api_evidence_marker
                    FROM evolution_decision d
                    LEFT JOIN mutation_payload p ON p.payload_hash=d.payload_hash
                   ORDER BY d.created_at, d.decision_id""",
        batch_size=batch_size,
    )


def _metadata_stats(
    rows: Iterable[Any],
    *,
    metadata_fn: Any,
    event_id_fn: Any,
) -> dict[str, Any]:
    digest = hashlib.sha256()
    event_ids: set[str] = set()
    count = 0
    for row in rows:
        digest.update(_json(metadata_fn(row)).encode("utf-8"))
        digest.update(b"\n")
        event_ids.add(str(event_id_fn(row)))
        count += 1
    return {
        "count": count,
        "total_rows": count,
        "unique_event_ids": len(event_ids),
        "metadata_digest": digest.hexdigest(),
    }


def _runtime_metadata_rows(
    conn: Any,
    *,
    batch_size: int = DEFAULT_ROW_BATCH_SIZE,
) -> Iterator[Any]:
    return _iter_rows(
        conn,
        """SELECT config_version, config_hash, source, run_id,
                         mutation_id, created_at
                  FROM runtime_config_snapshot
                 ORDER BY config_version""",
        batch_size=batch_size,
    )


def _eval_metadata_rows(
    conn: Any,
    *,
    batch_size: int = DEFAULT_ROW_BATCH_SIZE,
) -> Iterator[Any]:
    return _iter_rows(
        conn,
        """SELECT eval_id, plan_id, snapshot_id, action_type,
                         scope_type, status, comparison_verdict,
                         coverage_score, evaluation_run_id, created_at
                  FROM brain_action_plan_eval
                 ORDER BY created_at, eval_id""",
        batch_size=batch_size,
    )


_MUTATION_META_KEYS = (
    "decision_id", "run_id", "decision_type", "decision_json",
    "created_at",
)


def _mutation_metadata_rows(
    conn: Any,
    *,
    batch_size: int = DEFAULT_ROW_BATCH_SIZE,
) -> Iterator[Any]:
    for row in _iter_rows(
        conn,
        """SELECT decision_id, run_id, decision_type, decision_json, created_at
                 FROM evolution_decision
                ORDER BY created_at, decision_id""",
        batch_size=batch_size,
    ):
        r = {k: row[i] for i, k in enumerate(_MUTATION_META_KEYS)}
        meta = _loads_object(r["decision_json"])
        yield {
            "decision_id": r["decision_id"],
            "run_id": r["run_id"],
            "decision_type": r["decision_type"],
            "scope_type": meta.get("scope_type", ""),
            "scope_key": meta.get("scope_key", ""),
            "action": meta.get("action", ""),
            "status": meta.get("status", ""),
            "config_version": meta.get("config_version", 0),
            "config_hash": meta.get("config_hash", ""),
            "created_at": r["created_at"],
        }


def _metadata_manifest(
    conn: Any,
    *,
    batch_size: int = DEFAULT_ROW_BATCH_SIZE,
) -> dict[str, Any]:
    return {
        "runtime_config_snapshot": _metadata_stats(
            _runtime_metadata_rows(conn, batch_size=batch_size),
            metadata_fn=lambda row: (
                _value(row, "config_version", 0),
                _value(row, "config_hash", 1),
                _value(row, "source", 2),
                _value(row, "run_id", 3),
                _value(row, "mutation_id", 4),
                _value(row, "created_at", 5),
            ),
            event_id_fn=lambda row: _value(row, "config_version"),
        ),
        "brain_action_plan_eval": _metadata_stats(
            _eval_metadata_rows(conn, batch_size=batch_size),
            metadata_fn=lambda row: (
                _value(row, "eval_id", 0),
                _value(row, "plan_id", 1),
                _value(row, "snapshot_id", 2),
                _value(row, "action_type", 3),
                _value(row, "scope_type", 4),
                _value(row, "status", 5),
                _value(row, "comparison_verdict", 6),
                _value(row, "coverage_score", 7),
                _value(row, "evaluation_run_id", 8),
                _value(row, "created_at", 9),
            ),
            event_id_fn=lambda row: _value(row, "eval_id"),
        ),
        "evolution_decision": _metadata_stats(
            _mutation_metadata_rows(conn, batch_size=batch_size),
            metadata_fn=lambda row: (
                _value(row, "decision_id", 0),
                _value(row, "run_id", 1),
                _value(row, "decision_type", 2),
                _value(row, "scope_type", 3),
                _value(row, "scope_key", 4),
                _value(row, "action", 5),
                _value(row, "status", 6),
                _value(row, "config_version", 7),
                _value(row, "config_hash", 8),
                _value(row, "created_at", 9),
            ),
            event_id_fn=lambda row: _value(row, "decision_id"),
        ),
    }


def _loads_object(raw: Any) -> dict[str, Any]:
    try:
        value = json.loads(str(raw or "{}"))
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _is_api_audit(row: Any) -> bool:
    decision_type = str(_value(row, "decision_type") or "")
    compact_marker = _value(row, "api_evidence_marker", index=999, default=None)
    evidence = _loads_object(_value(row, "evidence_payload", 14, "{}"))
    return (
        str(_value(row, "projection_type") or "") in {
            "api",
            "api_canonical",
            "api_unmatched",
        }
        or decision_type == "manual_api_mutation"
        or (
            decision_type == "autonomous_mutation"
            and (
                str(_value(row, "scope_type") or "") == "api"
                or (
                    bool(compact_marker)
                    if compact_marker is not None
                    else bool(evidence.get("endpoint") or evidence.get("user"))
                )
            )
        )
    )


def _audit_fields_match(api_row: Any, canonical_row: Any) -> bool:
    for key in ("action", "status", "config_hash"):
        left = str(_value(api_row, key) or "")
        right = str(_value(canonical_row, key) or "")
        if left and right and left != right:
            return False
        if key == "config_hash" and bool(left) != bool(right):
            return False
    api_run = str(_value(api_row, "run_id") or "")
    canonical_run = str(_value(canonical_row, "run_id") or "")
    if api_run and canonical_run and api_run != canonical_run:
        return False
    api_ts = float(_value(api_row, "created_at") or 0.0)
    canonical_ts = float(_value(canonical_row, "created_at") or 0.0)
    if api_ts and canonical_ts:
        # The API projection is normally written after the canonical event.
        # Allow a small clock/order skew, but do not link unrelated history.
        delta = api_ts - canonical_ts
        if delta < -300.0 or delta > 86400.0:
            return False
    return True


def _audit_lineage(rows: list[Any]) -> tuple[dict[str, tuple[str, str]], dict[str, int]]:
    canonical_rows = {
        str(_value(row, "decision_id") or ""): row
        for row in rows
        if str(_value(row, "decision_id") or "") and not _is_api_audit(row)
    }
    buckets: dict[tuple[str, str, str], list[Any]] = {}
    for row in canonical_rows.values():
        key = (
            str(_value(row, "action") or ""),
            str(_value(row, "status") or ""),
            str(_value(row, "config_hash") or ""),
        )
        buckets.setdefault(key, []).append(row)

    assignments: dict[str, tuple[str, str]] = {}
    stats = {
        "canonical_events": len(canonical_rows),
        "api_audit_rows": 0,
        "linked": 0,
        "conflicts": 0,
        "unmatched": 0,
    }
    for row in rows:
        decision_id = str(_value(row, "decision_id") or "")
        if not decision_id:
            continue
        if not _is_api_audit(row):
            assignments[decision_id] = (decision_id, "canonical")
            continue
        stats["api_audit_rows"] += 1
        result = _loads_object(_value(row, "result_payload", 18, "{}"))
        direct_id = str(result.get("decision_id") or "")
        if not direct_id:
            stored_canonical = str(_value(row, "canonical_event_id") or "")
            if stored_canonical and stored_canonical != decision_id:
                direct_id = stored_canonical
        if direct_id:
            parent = canonical_rows.get(direct_id)
            if parent is not None and _audit_fields_match(row, parent):
                assignments[decision_id] = (direct_id, "api")
                stats["linked"] += 1
            else:
                assignments[decision_id] = (decision_id, "api_unmatched")
                stats["conflicts"] += 1
            continue

        key = (
            str(_value(row, "action") or ""),
            str(_value(row, "status") or ""),
            str(_value(row, "config_hash") or ""),
        )
        candidates = [item for item in buckets.get(key, []) if _audit_fields_match(row, item)]
        if len(candidates) == 1:
            assignments[decision_id] = (
                str(_value(candidates[0], "decision_id") or ""),
                "api",
            )
            stats["linked"] += 1
        elif len(candidates) > 1:
            assignments[decision_id] = (decision_id, "api_unmatched")
            stats["conflicts"] += 1
        else:
            assignments[decision_id] = (decision_id, "api_unmatched")
            stats["unmatched"] += 1
    return assignments, stats


def _compact_audit_row(row: Any) -> dict[str, Any]:
    """Keep only small audit metadata needed for lineage matching."""

    compact = {
        key: _value(row, key)
        for key in (
            "decision_id",
            "run_id",
            "action",
            "status",
            "config_hash",
            "created_at",
            "decision_type",
            "scope_type",
            "canonical_event_id",
            "projection_type",
        )
    }
    direct_id = str(_value(row, "direct_decision_id", index=999) or "")
    if not direct_id:
        result = _loads_object(_value(row, "result_payload", 18, "{}"))
        direct_id = str(result.get("decision_id") or "")
    compact["direct_decision_id"] = direct_id
    return compact


def _audit_lineage_from_compact(
    canonical_rows: dict[str, dict[str, Any]],
    api_rows: list[dict[str, Any]],
) -> tuple[dict[str, tuple[str, str]], dict[str, int]]:
    buckets: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for row in canonical_rows.values():
        key = (
            str(_value(row, "action") or ""),
            str(_value(row, "status") or ""),
            str(_value(row, "config_hash") or ""),
        )
        buckets.setdefault(key, []).append(row)

    assignments: dict[str, tuple[str, str]] = {
        decision_id: (decision_id, "canonical")
        for decision_id in canonical_rows
    }
    stats = {
        "canonical_events": len(canonical_rows),
        "api_audit_rows": len(api_rows),
        "linked": 0,
        "conflicts": 0,
        "unmatched": 0,
    }
    for row in api_rows:
        decision_id = str(_value(row, "decision_id") or "")
        if not decision_id:
            continue
        direct_id = str(_value(row, "direct_decision_id") or "")
        if not direct_id:
            stored_canonical = str(_value(row, "canonical_event_id") or "")
            if stored_canonical and stored_canonical != decision_id:
                direct_id = stored_canonical
        if direct_id:
            parent = canonical_rows.get(direct_id)
            if parent is not None and _audit_fields_match(row, parent):
                assignments[decision_id] = (direct_id, "api")
                stats["linked"] += 1
            else:
                assignments[decision_id] = (decision_id, "api_unmatched")
                stats["conflicts"] += 1
            continue

        key = (
            str(_value(row, "action") or ""),
            str(_value(row, "status") or ""),
            str(_value(row, "config_hash") or ""),
        )
        candidates = [
            item for item in buckets.get(key, [])
            if _audit_fields_match(row, item)
        ]
        if len(candidates) == 1:
            assignments[decision_id] = (
                str(_value(candidates[0], "decision_id") or ""),
                "api",
            )
            stats["linked"] += 1
        elif len(candidates) > 1:
            assignments[decision_id] = (decision_id, "api_unmatched")
            stats["conflicts"] += 1
        else:
            assignments[decision_id] = (decision_id, "api_unmatched")
            stats["unmatched"] += 1
    return assignments, stats


def _audit_lineage_stream(
    row_factory: Any,
) -> tuple[dict[str, tuple[str, str]], dict[str, int]]:
    """Build lineage from two streamed passes without retaining JSON payloads."""

    canonical_rows: dict[str, dict[str, Any]] = {}
    api_rows: list[dict[str, Any]] = []
    for row in row_factory():
        decision_id = str(_value(row, "decision_id") or "")
        if not decision_id:
            continue
        compact = _compact_audit_row(row)
        if _is_api_audit(row):
            api_rows.append(compact)
        else:
            canonical_rows[decision_id] = compact
    return _audit_lineage_from_compact(canonical_rows, api_rows)


def _audit_linkage_report(conn: Any) -> dict[str, int]:
    _assignments, stats = _audit_lineage_stream(lambda: _audit_rows(conn))
    return stats


def _mutation_digest_from_compound(raw: str) -> str:
    fields = (
        "evidence_json",
        "risk_verdict_json",
        "before_json",
        "after_json",
        "result_json",
        "rollback_json",
    )
    values = str(raw or "{}").split("\x00")
    return mutation_payload_hash(dict(zip(fields, values)))


def _domain_stats(
    conn: Any,
    domain: str,
    *,
    batch_size: int = DEFAULT_ROW_BATCH_SIZE,
    chunk_rows: int = DEFAULT_PAYLOAD_CHUNK_ROWS,
) -> dict[str, Any]:
    if _is_pg(conn):
        stats = _pg_payload_stats(
            conn,
            domain,
            batch_size=batch_size,
            chunk_rows=chunk_rows,
        )
        if domain == "runtime_config_snapshot":
            metadata = _metadata_stats(
                _runtime_metadata_rows(conn, batch_size=batch_size),
                metadata_fn=lambda row: (
                    _value(row, "config_version", 0),
                    _value(row, "config_hash", 1),
                    _value(row, "source", 2),
                    _value(row, "run_id", 3),
                    _value(row, "mutation_id", 4),
                    _value(row, "created_at", 5),
                ),
                event_id_fn=lambda row: _value(row, "config_version"),
            )
        elif domain == "brain_action_plan_eval":
            metadata = _metadata_stats(
                _eval_metadata_rows(conn, batch_size=batch_size),
                metadata_fn=lambda row: tuple(
                    _value(row, key, index)
                    for index, key in enumerate(
                        (
                            "eval_id",
                            "plan_id",
                            "snapshot_id",
                            "action_type",
                            "scope_type",
                            "status",
                            "comparison_verdict",
                            "coverage_score",
                            "evaluation_run_id",
                            "created_at",
                        )
                    )
                ),
                event_id_fn=lambda row: _value(row, "eval_id"),
            )
        elif domain == "evolution_decision":
            metadata = _metadata_stats(
                _mutation_metadata_rows(conn, batch_size=batch_size),
                metadata_fn=lambda row: tuple(
                    _value(row, key, index)
                    for index, key in enumerate(
                        (
                            "decision_id",
                            "run_id",
                            "decision_type",
                            "scope_type",
                            "scope_key",
                            "action",
                            "status",
                            "config_version",
                            "config_hash",
                            "created_at",
                        )
                    )
                ),
                event_id_fn=lambda row: _value(row, "decision_id"),
            )
        else:
            raise ValueError(f"unknown_payload_domain:{domain}")
        stats.update(metadata)
        return stats

    if domain == "runtime_config_snapshot":
        return _scan_rows(
            _runtime_rows(conn, batch_size=batch_size),
            payload_fn=lambda row: str(_value(row, "config_payload", 7, "{}") or "{}"),
            metadata_fn=lambda row: (
                _value(row, "config_version"),
                _value(row, "config_hash"),
                _value(row, "source"),
                _value(row, "run_id"),
                _value(row, "mutation_id"),
                _value(row, "created_at"),
            ),
            event_id_fn=lambda row: _value(row, "config_version"),
            digest_fn=lambda raw: payload_hash(raw, namespace=RUNTIME_NAMESPACE),
        )
    if domain == "brain_action_plan_eval":
        return _scan_rows(
            _eval_rows(conn, batch_size=batch_size),
            payload_fn=lambda row: "\x00".join(
                str(_value(row, field, index, "{}") or "{}")
                for index, field in (
                    (11, "comparison_payload"),
                    (12, "evidence_payload"),
                    (13, "boundary_payload"),
                )
            ),
            metadata_fn=lambda row: (
                _value(row, "eval_id"),
                _value(row, "plan_id"),
                _value(row, "snapshot_id"),
                _value(row, "action_type"),
                _value(row, "scope_type"),
                _value(row, "status"),
                _value(row, "comparison_verdict"),
                _value(row, "coverage_score"),
                _value(row, "evaluation_run_id"),
                _value(row, "created_at"),
            ),
            event_id_fn=lambda row: _value(row, "eval_id"),
            digest_fn=lambda raw: payload_hash(raw, namespace=EVAL_NAMESPACE),
        )
    if domain == "evolution_decision":
        return _scan_rows(
            _mutation_rows(conn, batch_size=1),
            payload_fn=lambda row: "\x00".join(
                str(_value(row, field, index, "{}") or "{}")
                for index, field in (
                    (14, "evidence_payload"),
                    (15, "risk_payload"),
                    (16, "before_payload"),
                    (17, "after_payload"),
                    (18, "result_payload"),
                    (19, "rollback_payload"),
                )
            ),
            metadata_fn=lambda row: (
                _value(row, "decision_id"),
                _value(row, "run_id"),
                _value(row, "decision_type"),
                _value(row, "scope_type"),
                _value(row, "scope_key"),
                _value(row, "action"),
                _value(row, "status"),
                _value(row, "config_version"),
                _value(row, "config_hash"),
                _value(row, "created_at"),
            ),
            event_id_fn=lambda row: _value(row, "decision_id"),
            digest_fn=_mutation_digest_from_compound,
        )
    raise ValueError(f"unknown_payload_domain:{domain}")


def _supervisor_review_schema_status(conn: Any) -> dict[str, Any]:
    requirements = {
        "state_payload_archive": {
            "archive_hash",
            "source_table",
            "source_id",
            "payload_kind",
            "raw_sha256",
            "payload_bytes",
        },
        "position_supervisor_trace": {
            "trace_id",
            "verdict_json",
            "verdict_archive_hash",
            "verdict_raw_sha256",
            "verdict_raw_bytes",
        },
        "trade_outcome_review": {
            "review_id",
            "review_json",
            "review_archive_hash",
            "review_raw_sha256",
            "review_raw_bytes",
        },
    }
    missing: list[str] = []
    for table, columns in requirements.items():
        if not state_table_exists(conn, table):
            missing.append(f"table:{table}")
            continue
        missing.extend(
            f"column:{table}.{column}"
            for column in sorted(columns - state_table_columns(conn, table))
        )
    return {"ready": not missing, "missing": missing}


def _supervisor_review_rows(conn: Any, table: str, *, batch_size: int) -> Iterator[Any]:
    if table == "position_supervisor_trace":
        return _iter_rows(
            conn,
            """
            SELECT trace_id, decision_id, position_id, event_ts, action, stage, outcome,
                   context_json, verdict_json, risk_verdict_json, execution_json,
                   created_at, config_version, config_hash
            FROM position_supervisor_trace
            ORDER BY position_id, event_ts, trace_id
            """,
            batch_size=batch_size,
        )
    if table == "trade_outcome_review":
        return _iter_rows(
            conn,
            """
            SELECT review_id, trade_id, position_id, created_at, review_json,
                   failure_tags_json, outcome_label, pnl, mae, mfe
            FROM trade_outcome_review
            ORDER BY created_at, review_id
            """,
            batch_size=batch_size,
        )
    raise ValueError(f"unknown_supervisor_review_table:{table}")


def _supervisor_review_source_counts(conn: Any) -> dict[str, int]:
    return {
        table: int(
            _value(
                conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone(),
                "n",
                0,
                0,
            )
            or 0
        )
        for table in SUPERVISOR_REVIEW_TABLES
    }


def _loads_object(raw: Any) -> Any:
    if isinstance(raw, (dict, list)):
        return raw
    try:
        value = json.loads(str(raw or "{}"))
    except Exception:
        return {}
    return value


def _walk_payload(value: Any, *, path: str = "$", depth: int = 0) -> tuple[int, Counter[str]]:
    max_depth = depth
    paths: Counter[str] = Counter()
    if isinstance(value, Mapping):
        for key, child in value.items():
            child_path = f"{path}.{key}"
            if str(key) in SUPERVISOR_RECURSIVE_KEYS and isinstance(child, (Mapping, list)):
                parts = child_path.split(".")
                if len(parts) > MAX_REPORTED_RECURSIVE_PATH_PARTS:
                    half = MAX_REPORTED_RECURSIVE_PATH_PARTS // 2
                    child_path = ".".join(
                        parts[:half]
                        + ["<recursive>"]
                        + parts[-(MAX_REPORTED_RECURSIVE_PATH_PARTS - half - 1) :]
                    )
                paths[child_path] += 1
            child_depth, child_paths = _walk_payload(child, path=child_path, depth=depth + 1)
            max_depth = max(max_depth, child_depth)
            paths.update(child_paths)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            child_depth, child_paths = _walk_payload(child, path=f"{path}[{index}]", depth=depth + 1)
            max_depth = max(max_depth, child_depth)
            paths.update(child_paths)
    return max_depth, paths


def _supervisor_trace_projection(row: Any) -> dict[str, Any]:
    return {
        "context": compact_supervisor_mapping(
            _loads_object(_value(row, "context_json", 7, "{}")),
            nested_keys=frozenset({"position", "account"}),
        ),
        "verdict": compact_supervisor_mapping(
            _loads_object(_value(row, "verdict_json", 8, "{}")),
            nested_keys=frozenset({"evidence", "recommended_controls", "supervisor_template"}),
        ),
        "risk_verdict": compact_supervisor_mapping(
            _loads_object(_value(row, "risk_verdict_json", 9, "{}")),
            nested_keys=frozenset({"evidence", "controls"}),
        ),
        "execution": compact_supervisor_mapping(
            _loads_object(_value(row, "execution_json", 10, "{}")),
            nested_keys=frozenset({"evidence", "controls"}),
        ),
    }


def _supervisor_review_consumer_coverage() -> dict[str, Any]:
    """Report direct JSON consumers before an operator is allowed to clear hot JSON."""
    direct_readers = [
        "alpha/reflection/reviewer.py",
        "backend/api/learning.py",
        "backend/api/risk.py",
        "backend/services/agent_briefing.py",
        "backend/services/agent_scorecard.py",
        "backend/services/autonomous_learning.py",
        "backend/services/backend_readiness.py",
        "backend/services/factor_counter_evidence.py",
        "backend/services/factor_pruning_candidates.py",
        "backend/services/learning_backfill.py",
        "backend/services/live_service.py",
        "backend/services/live_reentry_guard.py",
        "backend/services/memory_integrity.py",
        "backend/services/position_supervisor_governance.py",
        "backend/services/replay_harness.py",
        "backend/services/trade_lesson_memory.py",
        "backend/services/v16_brain_orchestrator.py",
        "backend/services/v16_brain_snapshot.py",
        "backend/services/v16_brain_planning.py",
        "backend/services/supervisor_counterfactual.py",
        "research/factor_governance_lightgbm.py",
        "research/features/feature_provider.py",
        "research/learning/experience_builder.py",
        "research/learning/governor.py",
        "scripts/backfill_controlled_close_learning.py",
        "scripts/backfill_entry_open_context.py",
        "scripts/phase_a_health_check.py",
        "scripts/phase_c_supervisor_check.py",
        "scripts/reconcile_trades_duckdb.py",
        "scripts/canonical_v2_vertical_shadow.py",
    ]
    migrated = {
        "alpha/reflection/reviewer.py": {
            "status": "migrated",
            "detail": "review deduplication reads use verified review archive references",
        },
        "research/position_quality_lightgbm.py": {
            "status": "migrated",
            "detail": "archive-aware review lookup with raw-byte budget and bounded trace stream",
        },
        "backend/services/v16_brain_snapshot.py": {
            "status": "migrated",
            "detail": "memory reads use verified review archive references with legacy inline fallback",
        },
        "backend/services/agent_scorecard.py": {
            "status": "migrated",
            "detail": "scorecard and attribution reads use verified review archive references",
        },
        "backend/services/agent_briefing.py": {
            "status": "migrated",
            "detail": "briefing experience reads use verified review archive references",
        },
        "backend/services/factor_pruning_candidates.py": {
            "status": "migrated",
            "detail": "candidate evidence filters use verified review archive references",
        },
        "backend/services/factor_counter_evidence.py": {
            "status": "migrated",
            "detail": "factor counter-evidence reads use verified review archive references",
        },
        "backend/services/memory_integrity.py": {
            "status": "migrated",
            "detail": "integrity quarantine checks use verified review archive references",
        },
        "backend/services/backend_readiness.py": {
            "status": "migrated",
            "detail": "counterfactual maturity checks use verified review archive references",
        },
        "backend/services/live_reentry_guard.py": {
            "status": "migrated",
            "detail": "re-entry review guard uses verified review archive references",
        },
        "backend/services/supervisor_counterfactual.py": {
            "status": "migrated",
            "detail": "counterfactual evaluation reads use verified review archive references",
        },
        "backend/services/v16_brain_orchestrator.py": {
            "status": "migrated",
            "detail": "brain status counterfactual reads use verified review archive references",
        },
        "backend/services/position_supervisor_governance.py": {
            "status": "migrated",
            "detail": "supervisor replay and candidate reads use verified review archive references",
        },
        "backend/services/learning_backfill.py": {
            "status": "migrated",
            "detail": "learning rebuild and regime backfill use verified review archive references",
        },
        "backend/services/trade_lesson_memory.py": {
            "status": "migrated",
            "detail": "lesson rebuild passes the live connection to the verified review archive loader",
        },
        "backend/services/replay_harness.py": {
            "status": "migrated",
            "detail": "review and supervisor trace replay rows restore verified archives before evaluation",
        },
        "research/factor_governance_lightgbm.py": {
            "status": "migrated",
            "detail": "factor training sample reads restore verified review archives before filtering",
        },
        "research/features/feature_provider.py": {
            "status": "migrated",
            "detail": "training feature reads restore verified review archives before sample construction",
        },
        "backend/services/v16_brain_planning.py": {
            "status": "migrated",
            "detail": "V16 evidence reads use bounded rows and restore verified review archives",
        },
        "backend/api/learning.py": {
            "status": "migrated",
            "detail": "learning review API reads restore verified archives before normalization",
        },
        "backend/api/risk.py": {
            "status": "migrated",
            "detail": "risk trace API reads restore verified archives before normalization",
        },
        "backend/services/autonomous_learning.py": {
            "status": "migrated",
            "detail": "sample materialization and backfills restore verified review archives",
        },
        "backend/services/live_service.py": {
            "status": "migrated",
            "detail": "risk metric review inputs restore verified review archives",
        },
        "research/learning/experience_builder.py": {
            "status": "migrated",
            "detail": "pure review transformer; database callers supply archive-restored payloads",
        },
        "research/learning/governor.py": {
            "status": "migrated",
            "detail": "governance effect review scans restore verified archives before filtering",
        },
        "scripts/backfill_controlled_close_learning.py": {
            "status": "migrated",
            "detail": "controlled backfill has no direct review JSON read and delegates archive-aware writer",
        },
        "scripts/backfill_entry_open_context.py": {
            "status": "migrated",
            "detail": "entry-context backfill restores archives and writes archive metadata on review updates",
        },
        "scripts/phase_a_health_check.py": {
            "status": "migrated",
            "detail": "health check uses the bounded stable review projection only; no recursive branch read",
        },
        "scripts/phase_c_supervisor_check.py": {
            "status": "migrated",
            "detail": "phase-C cases restore verified review archives before diagnostics",
        },
        "scripts/reconcile_trades_duckdb.py": {
            "status": "migrated",
            "detail": "state review context is restored before DuckDB reconciliation classification",
        },
        "scripts/canonical_v2_vertical_shadow.py": {
            "status": "migrated",
            "detail": "vertical shadow restores review archives before lineage and payload comparison",
        },
    }
    pending = [
        {"path": path, "status": "pending_archive_loader_migration"}
        for path in direct_readers
        if path not in migrated
    ]
    return {
        "loader": "backend.services.state_payload_archive.restore_json_payload",
        "migrated": list(migrated.values()),
        "pending": pending,
        "all_migrated": not pending,
    }


def _supervisor_review_stats(conn: Any, *, batch_size: int) -> dict[str, Any]:
    tables: dict[str, Any] = {}
    total_original_bytes = 0
    total_unique_raw_bytes = 0
    total_unique_gzip_bytes = 0
    for table in SUPERVISOR_REVIEW_TABLES:
        if not state_table_exists(conn, table):
            tables[table] = {"status": "missing_table"}
            continue
        counts: Counter[str] = Counter()
        raw_sizes: dict[str, int] = {}
        gzip_sizes: dict[str, int] = {}
        event_ids: set[str] = set()
        recursive_paths: Counter[str] = Counter()
        max_depth = 0
        oversized = 0
        original_bytes = 0
        projection_bytes = 0
        sha_digest = hashlib.sha256()
        top_payloads: list[dict[str, Any]] = []
        rows = 0
        for row in _supervisor_review_rows(conn, table, batch_size=batch_size):
            rows += 1
            event_id = str(
                _value(row, "trace_id" if table == "position_supervisor_trace" else "review_id") or ""
            )
            event_ids.add(event_id)
            if table == "position_supervisor_trace":
                context_raw_text = str(_value(row, "context_json", 7, "{}") or "{}")
                verdict_raw_text = str(_value(row, "verdict_json", 8, "{}") or "{}")
                risk_raw_text = str(_value(row, "risk_verdict_json", 9, "{}") or "{}")
                execution_raw_text = str(_value(row, "execution_json", 10, "{}") or "{}")
                raw_value = {
                    "context": _loads_object(context_raw_text),
                    "verdict": _loads_object(verdict_raw_text),
                    "risk_verdict": _loads_object(risk_raw_text),
                    "execution": _loads_object(execution_raw_text),
                }
                projected = _supervisor_trace_projection(row)
                payload_kind = "supervisor_trace"
            else:
                raw_text = str(_value(row, "review_json", 4, "{}") or "{}")
                raw_value = _loads_object(raw_text)
                projected = bounded_review_projection(raw_value if isinstance(raw_value, Mapping) else {})
                payload_kind = "review_json"
            raw_text = (
                str(_value(row, "review_json", 4, "{}") or "{}")
                if table == "trade_outcome_review"
                else supervisor_trace_archive_text(
                    context_json=context_raw_text,
                    verdict_json=verdict_raw_text,
                    risk_verdict_json=risk_raw_text,
                    execution_json=execution_raw_text,
                )
            )
            projected_text = _json(projected)
            raw_bytes = len(raw_text.encode("utf-8"))
            compressed_bytes = len(gzip.compress(raw_text.encode("utf-8"), compresslevel=6, mtime=0))
            digest = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
            counts[digest] += 1
            raw_sizes[digest] = raw_bytes
            gzip_sizes[digest] = compressed_bytes
            sha_digest.update(digest.encode("ascii"))
            sha_digest.update(b"\n")
            original_bytes += raw_bytes
            projection_bytes += len(projected_text.encode("utf-8"))
            max_depth_value, paths = _walk_payload(raw_value)
            max_depth = max(max_depth, max_depth_value)
            recursive_paths.update(paths)
            if raw_bytes >= 256 * 1024:
                oversized += 1
            top_payloads.append(
                {
                    "event_id": event_id,
                    "payload_kind": payload_kind,
                    "raw_sha256": digest,
                    "raw_bytes": raw_bytes,
                    "gzip_bytes": compressed_bytes,
                    "projection_bytes": len(projected_text.encode("utf-8")),
                }
            )
            # Keep dry-run memory bounded even if the table has millions of
            # events. The complete reference histogram remains available in
            # ``counts``; only the human-facing largest-payload sample is
            # capped at twenty rows.
            if len(top_payloads) > 20:
                top_payloads.sort(key=lambda item: item["raw_bytes"], reverse=True)
                del top_payloads[20:]
        top_payloads.sort(key=lambda item: item["raw_bytes"], reverse=True)
        unique_raw = sum(raw_sizes[digest] for digest, count in counts.items() if count)
        unique_gzip = sum(gzip_sizes[digest] for digest, count in counts.items() if count)
        duplicate_bytes = sum(
            raw_sizes[digest] * (count - 1)
            for digest, count in counts.items()
            if count > 1
        )
        total_original_bytes += original_bytes
        total_unique_raw_bytes += unique_raw
        total_unique_gzip_bytes += unique_gzip
        tables[table] = {
            "status": "ready",
            "rows": rows,
            "unique_event_ids": len(event_ids),
            "original_json_bytes": original_bytes,
            "unique_payload_count": len(counts),
            "duplicate_payload_count": sum(1 for count in counts.values() if count > 1),
            "duplicate_payload_bytes": duplicate_bytes,
            "oversized_json_count": oversized,
            "max_recursive_depth": max_depth,
            "recursive_paths": dict(sorted(recursive_paths.items())),
            "original_sha256_digest": sha_digest.hexdigest(),
            "unique_raw_bytes": unique_raw,
            "unique_gzip_bytes": unique_gzip,
            "bounded_projection_bytes": projection_bytes,
            "top_payloads": top_payloads[:20],
            "payload_reference_counts": {
                digest: int(count) for digest, count in counts.most_common(20)
            },
        }
    return {
        "schema_version": "state_supervisor_review_compaction_dry_run.v1",
        "tables": tables,
        "total_original_json_bytes": total_original_bytes,
        "total_unique_raw_bytes": total_unique_raw_bytes,
        "total_unique_gzip_bytes": total_unique_gzip_bytes,
        "estimated_archive_bytes": total_unique_gzip_bytes,
        "estimated_temporary_space_bytes": total_original_bytes + total_unique_gzip_bytes,
        "consumer_coverage": _supervisor_review_consumer_coverage(),
        "note": (
            "estimates only; no event row is removed or merged. Apply is refused "
            "until every direct consumer uses the archive loader."
        ),
    }


def _dry_run(
    conn: Any,
    *,
    batch_size: int = DEFAULT_ROW_BATCH_SIZE,
    chunk_rows: int = DEFAULT_PAYLOAD_CHUNK_ROWS,
    targets: str = "all",
) -> dict[str, Any]:
    """Report compaction value without transferring large PG JSON to Python."""

    target_payload = targets in {"payload", "all"}
    target_supervisor_review = targets in {"supervisor_review", "all"}
    skipped = {"status": "skipped", "reason": "target_not_selected"}
    runtime_stats = (
        _domain_stats(
            conn,
            "runtime_config_snapshot",
            batch_size=batch_size,
            chunk_rows=chunk_rows,
        )
        if target_payload
        else dict(skipped)
    )
    eval_stats = (
        _domain_stats(
            conn,
            "brain_action_plan_eval",
            batch_size=batch_size,
            chunk_rows=chunk_rows,
        )
        if target_payload
        else dict(skipped)
    )
    mutation_stats = (
        _domain_stats(
            conn,
            "evolution_decision",
            batch_size=batch_size,
            chunk_rows=chunk_rows,
        )
        if target_payload
        else dict(skipped)
    )
    stats = (runtime_stats, eval_stats, mutation_stats)
    current_relation_bytes = _relation_sizes(conn) if target_payload else {}
    compact_payload_bytes = sum(
        int(item.get("unique_payload_bytes") or 0) for item in stats
    )
    current_logical_payload_bytes = sum(
        int(item.get("payload_bytes") or 0) for item in stats
    )
    estimated_reclaimable = sum(
        int(item.get("redundant_payload_bytes") or 0) for item in stats
    )
    # This is a logical lower-bound estimate.  PostgreSQL tuple headers,
    # indexes, TOAST and free-space maps are deliberately not presented as
    # exact bytes; the post-apply relation size and df readings are the truth.
    estimated_event_projection_bytes = (
        int(runtime_stats.get("total_rows") or 0) * 2
        + int(eval_stats.get("total_rows") or 0) * 6
        + int(mutation_stats.get("total_rows") or 0) * 12
    )
    estimated_new_logical_bytes = compact_payload_bytes + estimated_event_projection_bytes
    current_relation_total = sum(current_relation_bytes.values())
    supervisor_review = (
        _supervisor_review_stats(conn, batch_size=batch_size)
        if target_supervisor_review
        else dict(skipped)
    )
    estimated_temporary_space = current_relation_total + estimated_new_logical_bytes
    if target_supervisor_review:
        estimated_temporary_space += int(
            supervisor_review.get("estimated_temporary_space_bytes") or 0
        )
    return {
        "schema_version": "state_payload_compaction_dry_run.v1",
        "ok": True,
        "read_only": True,
        "targets": targets,
        "digest_algorithm": "sha256",
        "runtime_config_snapshot": runtime_stats,
        "brain_action_plan_eval": eval_stats,
        "evolution_decision": mutation_stats,
        "audit_double_write": _audit_linkage_report(conn) if target_payload else dict(skipped),
        "supervisor_review": supervisor_review,
        "maintenance_preflight": _maintenance_preflight(conn),
        "relation_sizes_bytes": current_relation_bytes,
        "estimated_current_relation_bytes": current_relation_total,
        "estimated_compact_payload_bytes": compact_payload_bytes,
        "estimated_new_logical_bytes": estimated_new_logical_bytes,
        "estimated_temporary_space_bytes": estimated_temporary_space,
        "estimated_reclaimable_payload_bytes": estimated_reclaimable,
        "current_logical_payload_bytes": current_logical_payload_bytes,
        "note": (
            "estimates only; physical reclaim requires the full-stop apply/table rewrite, "
            "then pg_total_relation_size and df verification; event rows are not removed"
        ),
    }


def _apply_supervisor_review_payloads(
    conn: Any,
    *,
    batch_size: int = DEFAULT_ROW_BATCH_SIZE,
) -> dict[str, Any]:
    """Archive and bound supervisor/review JSON after all consumers migrate.

    This is deliberately fail-closed today: the coverage report names every
    known direct reader and prevents clearing the hot JSON while any reader
    still bypasses the archive loader. Once coverage is complete this
    path preserves every row and only changes payload storage plus archive
    metadata.
    """
    schema = _supervisor_review_schema_status(conn)
    if not schema["ready"]:
        raise RuntimeError("supervisor_review_schema_missing:" + json.dumps(schema["missing"], sort_keys=True))
    coverage = _supervisor_review_consumer_coverage()
    if not coverage["all_migrated"]:
        raise RuntimeError(
            "supervisor_review_consumer_migration_incomplete:"
            + json.dumps(coverage["pending"], sort_keys=True)
        )

    source_row_counts = _supervisor_review_source_counts(conn)
    counts = {table: 0 for table in SUPERVISOR_REVIEW_TABLES}
    for row in _supervisor_review_rows(conn, "position_supervisor_trace", batch_size=batch_size):
        context_raw_text = str(_value(row, "context_json", 7, "{}") or "{}")
        verdict_raw_text = str(_value(row, "verdict_json", 8, "{}") or "{}")
        risk_raw_text = str(_value(row, "risk_verdict_json", 9, "{}") or "{}")
        execution_raw_text = str(_value(row, "execution_json", 10, "{}") or "{}")
        raw_value = {
            "context": _loads_object(context_raw_text),
            "verdict": _loads_object(verdict_raw_text),
            "risk_verdict": _loads_object(risk_raw_text),
            "execution": _loads_object(execution_raw_text),
        }
        raw_text = supervisor_trace_archive_text(
            context_json=context_raw_text,
            verdict_json=verdict_raw_text,
            risk_verdict_json=risk_raw_text,
            execution_json=execution_raw_text,
        )
        archive = archive_json_payload(
            conn,
            source_table="position_supervisor_trace",
            source_id=str(_value(row, "trace_id") or ""),
            payload_kind="supervisor_trace",
            raw_json=raw_text,
        )
        if not archive:
            raise RuntimeError("state_payload_archive_unavailable")
        projected = _supervisor_trace_projection(row)
        conn.execute(
            _sql(
                conn,
                """
                UPDATE position_supervisor_trace
                SET context_json=?, verdict_json=?, risk_verdict_json=?, execution_json=?,
                    verdict_archive_hash=?, verdict_raw_sha256=?, verdict_raw_bytes=?
                WHERE trace_id=?
                """,
            ),
            (
                _json(projected["context"]),
                _json(projected["verdict"]),
                _json(projected["risk_verdict"]),
                _json(projected["execution"]),
                archive["archive_hash"],
                archive["raw_sha256"],
                archive["raw_bytes"],
                str(_value(row, "trace_id") or ""),
            ),
        )
        counts["position_supervisor_trace"] += 1

    for row in _supervisor_review_rows(conn, "trade_outcome_review", batch_size=batch_size):
        raw_text = str(_value(row, "review_json", 4, "{}") or "{}")
        raw_value = _loads_object(raw_text)
        archive = archive_json_payload(
            conn,
            source_table="trade_outcome_review",
            source_id=str(_value(row, "review_id") or ""),
            payload_kind="review_json",
            raw_json=raw_text,
        )
        if not archive:
            raise RuntimeError("state_payload_archive_unavailable")
        projected = bounded_review_projection(raw_value if isinstance(raw_value, Mapping) else {})
        conn.execute(
            _sql(
                conn,
                """
                UPDATE trade_outcome_review
                SET review_json=?, review_archive_hash=?, review_raw_sha256=?,
                    review_raw_bytes=?
                WHERE review_id=?
                """,
            ),
            (
                _json(projected),
                archive["archive_hash"],
                archive["raw_sha256"],
                archive["raw_bytes"],
                str(_value(row, "review_id") or ""),
            ),
        )
        counts["trade_outcome_review"] += 1
    conn.commit()
    return {
        "schema_version": "state_supervisor_review_compaction_apply.v1",
        "ok": True,
        "rows": counts,
        "source_row_counts_before": source_row_counts,
        "source_row_counts_after": _supervisor_review_source_counts(conn),
        "consumer_coverage": coverage,
        "rewrite": "controlled_relation_rewrite_required",
        "note": "No event row was deleted or merged; run table rewrite and verify before dropping old storage.",
    }


def _verify_supervisor_review_payloads(conn: Any) -> dict[str, Any]:
    schema = _supervisor_review_schema_status(conn)
    if not schema["ready"]:
        return {
            "schema_version": "state_supervisor_review_compaction_verify.v1",
            "ok": False,
            "status": "schema_not_ready",
            "missing": schema["missing"],
        }
    missing_refs = {table: 0 for table in SUPERVISOR_REVIEW_TABLES}
    sha_mismatches = {table: 0 for table in SUPERVISOR_REVIEW_TABLES}
    metadata_mismatches = {table: 0 for table in SUPERVISOR_REVIEW_TABLES}
    semantic_mismatches = {table: 0 for table in SUPERVISOR_REVIEW_TABLES}
    row_counts = {table: 0 for table in SUPERVISOR_REVIEW_TABLES}
    source_row_counts = _supervisor_review_source_counts(conn)
    for row in _iter_rows(
        conn,
        """
        SELECT trace_id, verdict_archive_hash, verdict_raw_sha256, verdict_raw_bytes
        FROM position_supervisor_trace
        WHERE verdict_archive_hash<>''
        """,
    ):
        row_counts["position_supervisor_trace"] += 1
        archive_hash = str(_value(row, "verdict_archive_hash") or "")
        try:
            restored = restore_json_payload(conn, archive_hash)
            digest = hashlib.sha256(restored.encode("utf-8")).hexdigest()
            if digest != str(_value(row, "verdict_raw_sha256") or ""):
                sha_mismatches["position_supervisor_trace"] += 1
            if len(restored.encode("utf-8")) != int(_value(row, "verdict_raw_bytes") or 0):
                metadata_mismatches["position_supervisor_trace"] += 1
            fields = load_supervisor_trace_archive(conn, archive_hash)
            if not all(str(fields.get(key) or "") for key in (
                "context_json", "verdict_json", "risk_verdict_json", "execution_json"
            )):
                semantic_mismatches["position_supervisor_trace"] += 1
        except KeyError:
            missing_refs["position_supervisor_trace"] += 1
        except Exception:
            metadata_mismatches["position_supervisor_trace"] += 1
    for row in _iter_rows(
        conn,
        """
        SELECT review_id, review_archive_hash, review_raw_sha256, review_raw_bytes
        FROM trade_outcome_review
        WHERE review_archive_hash<>''
        """,
    ):
        row_counts["trade_outcome_review"] += 1
        archive_hash = str(_value(row, "review_archive_hash") or "")
        try:
            restored = restore_json_payload(conn, archive_hash)
            digest = hashlib.sha256(restored.encode("utf-8")).hexdigest()
            if digest != str(_value(row, "review_raw_sha256") or ""):
                sha_mismatches["trade_outcome_review"] += 1
            if len(restored.encode("utf-8")) != int(_value(row, "review_raw_bytes") or 0):
                metadata_mismatches["trade_outcome_review"] += 1
            if not isinstance(json.loads(restored), dict):
                semantic_mismatches["trade_outcome_review"] += 1
        except KeyError:
            missing_refs["trade_outcome_review"] += 1
        except Exception:
            metadata_mismatches["trade_outcome_review"] += 1
    rows_without_archive_refs = {
        table: max(0, source_row_counts[table] - row_counts[table])
        for table in SUPERVISOR_REVIEW_TABLES
    }
    return {
        "schema_version": "state_supervisor_review_compaction_verify.v1",
        "ok": (
            not any(missing_refs.values())
            and not any(sha_mismatches.values())
            and not any(metadata_mismatches.values())
            and not any(semantic_mismatches.values())
            and not any(rows_without_archive_refs.values())
        ),
        "source_row_counts": source_row_counts,
        "row_counts_with_archive_refs": row_counts,
        "rows_without_archive_refs": rows_without_archive_refs,
        "missing_archive_refs": missing_refs,
        "sha256_mismatches": sha_mismatches,
        "archive_metadata_mismatches": metadata_mismatches,
        "semantic_mismatches": semantic_mismatches,
        "consumer_coverage": _supervisor_review_consumer_coverage(),
    }


def _rollback_supervisor_review_payloads(conn: Any) -> dict[str, Any]:
    """Restore hot JSON from verified archives without removing archive refs."""

    schema = _supervisor_review_schema_status(conn)
    if not schema["ready"]:
        raise RuntimeError("supervisor_review_schema_missing:" + json.dumps(schema["missing"], sort_keys=True))
    restored = {table: 0 for table in SUPERVISOR_REVIEW_TABLES}
    for row in _iter_rows(
        conn,
        """
        SELECT trace_id, verdict_archive_hash
        FROM position_supervisor_trace
        WHERE verdict_archive_hash<>''
        """,
    ):
        fields = load_supervisor_trace_archive(
            conn,
            str(_value(row, "verdict_archive_hash") or ""),
        )
        conn.execute(
            _sql(
                conn,
                """
                UPDATE position_supervisor_trace
                SET context_json=?, verdict_json=?, risk_verdict_json=?, execution_json=?
                WHERE trace_id=?
                """,
            ),
            (
                fields["context_json"],
                fields["verdict_json"],
                fields["risk_verdict_json"],
                fields["execution_json"],
                str(_value(row, "trace_id") or ""),
            ),
        )
        restored["position_supervisor_trace"] += 1
    for row in _iter_rows(
        conn,
        """
        SELECT review_id, review_archive_hash
        FROM trade_outcome_review
        WHERE review_archive_hash<>''
        """,
    ):
        raw = restore_json_payload(
            conn,
            str(_value(row, "review_archive_hash") or ""),
        )
        conn.execute(
            _sql(
                conn,
                """
                UPDATE trade_outcome_review
                SET review_json=?
                WHERE review_id=?
                """,
            ),
            (raw, str(_value(row, "review_id") or "")),
        )
        restored["trade_outcome_review"] += 1
    conn.commit()
    return {
        "schema_version": "state_supervisor_review_compaction_rollback.v1",
        "ok": True,
        "restored_rows": restored,
        "note": "Archive references were retained for post-rollback verification.",
    }


def _insert_runtime_payload(conn: Any, payload_hash_value: str, raw: str) -> None:
    conn.execute(
        _sql(
            conn,
            """INSERT INTO runtime_config_payload
               (payload_hash, config_json, byte_length, created_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(payload_hash) DO NOTHING""",
        ),
        (payload_hash_value, raw, len(raw.encode("utf-8")), time.time()),
    )


def _insert_eval_payload(conn: Any, payload_hash_value: str, parts: tuple[str, str, str]) -> None:
    conn.execute(
        _sql(
            conn,
            """INSERT INTO brain_action_plan_eval_payload
               (payload_hash, comparison_json, evidence_refs_json, boundary_json, byte_length, created_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(payload_hash) DO NOTHING""",
        ),
        (*((payload_hash_value,) + parts), sum(len(part.encode("utf-8")) for part in parts), time.time()),
    )


def _insert_mutation_payload(conn: Any, payload_hash_value: str, parts: tuple[str, ...]) -> None:
    conn.execute(
        _sql(
            conn,
            """INSERT INTO mutation_payload
               (payload_hash, evidence_json, risk_verdict_json, before_json, after_json,
                result_json, rollback_json, byte_length, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(payload_hash) DO NOTHING""",
        ),
        (*((payload_hash_value,) + parts), sum(len(part.encode("utf-8")) for part in parts), time.time()),
    )


def _pg_apply_payload_domain(conn: Any, domain: str) -> int:
    """Intern one domain using set-based PostgreSQL statements."""

    definition = _pg_payload_definition(domain)
    namespace = definition["namespace"].replace("'", "''")
    zero = "decode('00', 'hex')"

    if domain == "runtime_config_snapshot":
        raw = definition["raw"]
        source = (
            f"SELECT {definition['event_id']} AS event_id, {raw} AS raw "
            f"FROM {definition['from']}"
        )
        hashed = (
            "SELECT event_id, raw, "
            f"encode(sha256(convert_to('{namespace}', 'UTF8') || {zero} || convert_to(raw, 'UTF8')), 'hex') "
            "AS payload_hash "
            f"FROM ({source}) AS source_rows"
        )
        conn.execute(
            f"""INSERT INTO runtime_config_payload
                    (payload_hash, config_json, byte_length, created_at)
                SELECT payload_hash, raw, octet_length(raw),
                       extract(epoch FROM clock_timestamp())
                  FROM ({hashed}) AS payload_rows
             ON CONFLICT(payload_hash) DO NOTHING"""
        )
        conn.execute(
            f"""WITH payload_rows AS ({hashed})
                UPDATE runtime_config_snapshot AS target
                   SET payload_hash=payload_rows.payload_hash,
                       config_json='{{}}'
                  FROM payload_rows
                 WHERE target.config_version=payload_rows.event_id"""
        )
    elif domain == "brain_action_plan_eval":
        parts = (
            "COALESCE(p.comparison_json, NULLIF(e.comparison_json, ''), '{}')",
            "COALESCE(p.evidence_refs_json, NULLIF(e.evidence_refs_json, ''), '{}')",
            "COALESCE(p.boundary_json, NULLIF(e.boundary_json, ''), '{}')",
        )
        eval_source = (
            f"SELECT e.eval_id AS event_id, {parts[0]} AS comparison_json, "
            f"{parts[1]} AS evidence_refs_json, {parts[2]} AS boundary_json "
            "FROM brain_action_plan_eval e "
            "LEFT JOIN brain_action_plan_eval_payload p ON p.payload_hash=e.payload_hash"
        )
        eval_hash_input = (
            f"convert_to('{namespace}', 'UTF8') || {zero} || "
            "convert_to((comparison_json), 'UTF8') || "
            f"{zero} || convert_to((evidence_refs_json), 'UTF8') || "
            f"{zero} || convert_to((boundary_json), 'UTF8')"
        )
        eval_hashed = (
            "SELECT event_id, comparison_json, evidence_refs_json, boundary_json, "
            f"encode(sha256({eval_hash_input}), 'hex') "
            "AS payload_hash "
            f"FROM ({eval_source}) AS source_rows"
        )
        conn.execute(
            f"""INSERT INTO brain_action_plan_eval_payload
                    (payload_hash, comparison_json, evidence_refs_json,
                     boundary_json, byte_length, created_at)
                SELECT payload_hash, comparison_json, evidence_refs_json,
                       boundary_json,
                       octet_length(comparison_json)
                       + octet_length(evidence_refs_json)
                       + octet_length(boundary_json),
                       extract(epoch FROM clock_timestamp())
                  FROM ({eval_hashed}) AS payload_rows
             ON CONFLICT(payload_hash) DO NOTHING"""
        )
        conn.execute(
            f"""WITH payload_rows AS ({eval_hashed})
                UPDATE brain_action_plan_eval AS target
                   SET payload_hash=payload_rows.payload_hash,
                       comparison_json='{{}}',
                       evidence_refs_json='{{}}',
                       boundary_json='{{}}'
                  FROM payload_rows
                 WHERE target.eval_id=payload_rows.event_id"""
        )
    elif domain == "evolution_decision":
        parts = {
            "after_json": "COALESCE(p.after_json, '{}')",
            "before_json": "COALESCE(p.before_json, '{}')",
            "evidence_json": "COALESCE(p.evidence_json, '{}')",
            "result_json": "COALESCE(p.result_json, '{}')",
            "risk_verdict_json": "COALESCE(p.risk_verdict_json, '{}')",
            "rollback_json": "COALESCE(p.rollback_json, '{}')",
        }
        mutation_source = (
            "SELECT d.decision_id AS event_id, "
            + ", ".join(f"{value} AS {key}" for key, value in parts.items())
            + " FROM evolution_decision d "
            "LEFT JOIN mutation_payload p ON p.payload_hash=d.payload_hash"
        )
        hash_segments = [
            f"convert_to('{key}=', 'UTF8') || convert_to(({key}), 'UTF8')"
            for key in parts
        ]
        mutation_hash_input = (
            f"convert_to('{namespace}', 'UTF8') || {zero} || "
            + f" || {zero} || ".join(hash_segments)
        )
        mutation_hashed = (
            "SELECT event_id, "
            + ", ".join(parts)
            + ", "
            + f"encode(sha256({mutation_hash_input}), 'hex') "
            + "AS payload_hash FROM ("
            + mutation_source
            + ") AS source_rows"
        )
        conn.execute(
            f"""INSERT INTO mutation_payload
                    (payload_hash, evidence_json, risk_verdict_json,
                     before_json, after_json, result_json, rollback_json,
                     byte_length, created_at)
                SELECT payload_hash, evidence_json, risk_verdict_json,
                       before_json, after_json, result_json, rollback_json,
                       octet_length(evidence_json)
                       + octet_length(risk_verdict_json)
                       + octet_length(before_json)
                       + octet_length(after_json)
                       + octet_length(result_json)
                       + octet_length(rollback_json),
                       extract(epoch FROM clock_timestamp())
                  FROM ({mutation_hashed}) AS payload_rows
             ON CONFLICT(payload_hash) DO NOTHING"""
        )
        conn.execute(
            f"""WITH payload_rows AS ({mutation_hashed})
                UPDATE evolution_decision AS target
                  SET payload_hash=payload_rows.payload_hash
                  FROM payload_rows
                 WHERE target.decision_id=payload_rows.event_id"""
        )
    else:
        raise ValueError(f"unknown_payload_domain:{domain}")

    row = conn.execute(
        f"SELECT COUNT(*) AS total_rows FROM {definition['table']}"
    ).fetchone()
    conn.commit()
    return int(_value(row, "total_rows", 0, 0) or 0)


def _apply_payload_refs_pg(conn: Any) -> dict[str, Any]:
    lineage, lineage_stats = _audit_lineage_stream(lambda: _audit_rows(conn))
    runtime_count = _pg_apply_payload_domain(conn, "runtime_config_snapshot")
    eval_count = _pg_apply_payload_domain(conn, "brain_action_plan_eval")
    mutation_count = _pg_apply_payload_domain(conn, "evolution_decision")

    # Runtime PostgreSQL connections must not create schema objects (a
    # persisted, temp or staging table alike).  Apply lineage directly with
    # parameterized DML instead of a temporary lineage table.  This matches the
    # SQLite branch below and keeps the runtime connection schema-guard intact.
    with conn.cursor() as cursor:
        cursor.executemany(
            """UPDATE evolution_decision AS target
                  SET canonical_event_id=%s,
                      projection_type=%s
                WHERE target.decision_id=%s""",
            [
                (canonical_id, projection_type, decision_id)
                for decision_id, (canonical_id, projection_type) in lineage.items()
            ],
        )
    conn.commit()
    return {
        "runtime_config_snapshot": runtime_count,
        "brain_action_plan_eval": eval_count,
        "evolution_decision": mutation_count,
        "audit_double_write": lineage_stats,
    }


def _apply_payload_refs(
    conn: Any,
    *,
    batch_size: int = DEFAULT_ROW_BATCH_SIZE,
) -> dict[str, Any]:
    if _is_pg(conn):
        return _apply_payload_refs_pg(conn)
    runtime_count = 0
    eval_count = 0
    mutation_count = 0
    for row in _runtime_rows(conn, batch_size=batch_size):
        raw = str(_value(row, "config_payload", 6, "{}") or "{}")
        digest = payload_hash(raw, namespace=RUNTIME_NAMESPACE)
        _insert_runtime_payload(conn, digest, raw)
        conn.execute(
            _sql(
                conn,
                """UPDATE runtime_config_snapshot
                   SET payload_hash=?, config_json='{}'
                   WHERE config_version=?""",
            ),
            (digest, _value(row, "config_version")),
        )
        runtime_count += 1
        if runtime_count % batch_size == 0:
            conn.commit()
            time.sleep(DEFAULT_BATCH_PAUSE_SECONDS)

    for row in _eval_rows(conn, batch_size=batch_size):
        parts = tuple(
            str(_value(row, field, index, "{}") or "{}")
            for index, field in (
                (11, "comparison_payload"),
                (12, "evidence_payload"),
                (13, "boundary_payload"),
            )
        )
        digest = payload_hash("\x00".join(parts), namespace=EVAL_NAMESPACE)
        _insert_eval_payload(conn, digest, parts)
        conn.execute(
            _sql(
                conn,
                """UPDATE brain_action_plan_eval
                   SET payload_hash=?, comparison_json='{}', evidence_refs_json='{}', boundary_json='{}'
                   WHERE eval_id=?""",
            ),
            (digest, _value(row, "eval_id")),
        )
        eval_count += 1
        if eval_count % batch_size == 0:
            conn.commit()
            time.sleep(DEFAULT_BATCH_PAUSE_SECONDS)

    lineage, lineage_stats = _audit_lineage_stream(
        lambda: _mutation_rows(conn, batch_size=batch_size)
    )
    for row in _mutation_rows(conn, batch_size=batch_size):
        parts = tuple(
            str(_value(row, field, index, "{}") or "{}")
            for index, field in (
                (14, "evidence_payload"),
                (15, "risk_payload"),
                (16, "before_payload"),
                (17, "after_payload"),
                (18, "result_payload"),
                (19, "rollback_payload"),
            )
        )
        digest = mutation_payload_hash(
            dict(zip(("evidence_json", "risk_verdict_json", "before_json", "after_json", "result_json", "rollback_json"), parts))
        )
        _insert_mutation_payload(conn, digest, parts)
        decision_id = str(_value(row, "decision_id") or "")
        canonical_id, projection_type = lineage.get(
            decision_id, (decision_id, "canonical")
        )
        conn.execute(
            _sql(
                conn,
                """UPDATE evolution_decision
                   SET payload_hash=?, canonical_event_id=?, projection_type=?
                   WHERE decision_id=?""",
            ),
            (digest, canonical_id, projection_type, decision_id),
        )
        mutation_count += 1
        if mutation_count % batch_size == 0:
            conn.commit()
            time.sleep(DEFAULT_BATCH_PAUSE_SECONDS)

    conn.commit()
    return {
        "runtime_config_snapshot": runtime_count,
        "brain_action_plan_eval": eval_count,
        "evolution_decision": mutation_count,
        "audit_double_write": lineage_stats,
    }


def _write_manifest(
    path: Path,
    *,
    maintenance_id: str,
    before: dict[str, Any],
    before_relation_sizes: dict[str, int],
    apply_result: dict[str, Any],
) -> dict[str, Any]:
    manifest = {
        "manifest_version": MANIFEST_VERSION,
        "maintenance_id": maintenance_id,
        "created_at": time.time(),
        "before": before,
        "before_relation_sizes_bytes": before_relation_sizes,
        "apply_result": apply_result,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
    return manifest


def _write_dry_run_manifest(path: Path, *, targets: str, result: dict[str, Any]) -> str:
    manifest = {
        "manifest_version": MANIFEST_VERSION,
        "mode": "dry_run",
        "targets": targets,
        "created_at": time.time(),
        "result": result,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2),
        encoding="utf-8",
    )
    return str(path)


def _verify(conn: Any, manifest: dict[str, Any] | None = None) -> dict[str, Any]:
    missing_refs: dict[str, int] = {}
    for table, column, payload_table in (
        ("runtime_config_snapshot", "payload_hash", RUNTIME_CONFIG_PAYLOAD_TABLE),
        ("brain_action_plan_eval", "payload_hash", BRAIN_ACTION_PLAN_EVAL_PAYLOAD_TABLE),
        ("evolution_decision", "payload_hash", MUTATION_PAYLOAD_TABLE),
    ):
        row = conn.execute(
            _sql(
                conn,
                f"SELECT COUNT(*) AS n FROM {table} t LEFT JOIN {payload_table} p ON p.payload_hash=t.{column} "
                f"WHERE t.{column}='' OR p.payload_hash IS NULL",
            )
        ).fetchone()
        missing_refs[table] = int(_value(row, "n", 0, 0) or 0)
    hash_mismatches = {
        "runtime_config_snapshot": 0,
        "brain_action_plan_eval": 0,
        "evolution_decision": 0,
    }
    if _is_pg(conn):
        for domain in hash_mismatches:
            hash_mismatches[domain] = _pg_payload_hash_mismatches(conn, domain)
    else:
        for row in _runtime_rows(conn):
            raw = str(_value(row, "config_payload", 7, "{}") or "{}")
            expected = payload_hash(raw, namespace=RUNTIME_NAMESPACE)
            if str(_value(row, "payload_hash", 6, "") or "") != expected:
                hash_mismatches["runtime_config_snapshot"] += 1
        for row in _eval_rows(conn):
            parts = tuple(
                str(_value(row, field, index, "{}") or "{}")
                for index, field in (
                    (11, "comparison_payload"),
                    (12, "evidence_payload"),
                    (13, "boundary_payload"),
                )
            )
            expected = payload_hash("\x00".join(parts), namespace=EVAL_NAMESPACE)
            if str(_value(row, "payload_hash", 9, "") or "") != expected:
                hash_mismatches["brain_action_plan_eval"] += 1
        for row in _mutation_rows(conn, batch_size=1):
            parts = {
                target: str(_value(row, source, index, "{}") or "{}")
                for index, source, target in (
                    (14, "evidence_payload", "evidence_json"),
                    (15, "risk_payload", "risk_verdict_json"),
                    (16, "before_payload", "before_json"),
                    (17, "after_payload", "after_json"),
                    (18, "result_payload", "result_json"),
                    (19, "rollback_payload", "rollback_json"),
                )
            }
            expected = mutation_payload_hash(parts)
            if str(_value(row, "payload_hash", 11, "") or "") != expected:
                hash_mismatches["evolution_decision"] += 1
    current = _metadata_manifest(conn)
    result = {
        "schema_version": "state_payload_compaction_verify.v1",
        "ok": all(value == 0 for value in missing_refs.values())
        and all(value == 0 for value in hash_mismatches.values()),
        "missing_payload_refs": missing_refs,
        "payload_hash_mismatches": hash_mismatches,
        "current": current,
    }
    if manifest:
        result["metadata_unchanged"] = manifest.get("before") == current
        before_sizes = dict(manifest.get("before_relation_sizes_bytes") or {})
        after_sizes = _relation_sizes(conn)
        result["before_relation_sizes_bytes"] = before_sizes
        result["after_relation_sizes_bytes"] = after_sizes
        if before_sizes:
            result["relation_size_delta_bytes"] = {
                key: int(after_sizes.get(key, 0)) - int(value or 0)
                for key, value in before_sizes.items()
            }
        result["ok"] = bool(result["ok"] and result["metadata_unchanged"])
    return result


def _existing_reference_gaps(conn: Any) -> dict[str, int]:
    gaps: dict[str, int] = {}
    for table, column, payload_table in (
        ("runtime_config_snapshot", "payload_hash", RUNTIME_CONFIG_PAYLOAD_TABLE),
        ("brain_action_plan_eval", "payload_hash", BRAIN_ACTION_PLAN_EVAL_PAYLOAD_TABLE),
        ("evolution_decision", "payload_hash", MUTATION_PAYLOAD_TABLE),
    ):
        row = conn.execute(
            _sql(
                conn,
                f"SELECT COUNT(*) AS n FROM {table} t "
                f"LEFT JOIN {payload_table} p ON p.payload_hash=t.{column} "
                f"WHERE t.{column}<>'' AND p.payload_hash IS NULL",
            )
        ).fetchone()
        gaps[table] = int(_value(row, "n", 0, 0) or 0)
    return gaps


def _rollback(conn: Any) -> dict[str, Any]:
    conn.execute(
        _sql(
            conn,
            """UPDATE runtime_config_snapshot s
               SET config_json=p.config_json
               FROM runtime_config_payload p
               WHERE p.payload_hash=s.payload_hash""",
        )
        if _is_pg(conn)
        else """UPDATE runtime_config_snapshot
           SET config_json=(SELECT p.config_json FROM runtime_config_payload p
                            WHERE p.payload_hash=runtime_config_snapshot.payload_hash)
           WHERE payload_hash IN (SELECT payload_hash FROM runtime_config_payload)""",
    )
    conn.execute(
        _sql(
            conn,
            """UPDATE brain_action_plan_eval e
               SET comparison_json=p.comparison_json,
                   evidence_refs_json=p.evidence_refs_json,
                   boundary_json=p.boundary_json
               FROM brain_action_plan_eval_payload p
               WHERE p.payload_hash=e.payload_hash""",
        )
        if _is_pg(conn)
        else """UPDATE brain_action_plan_eval
           SET comparison_json=(SELECT p.comparison_json FROM brain_action_plan_eval_payload p WHERE p.payload_hash=brain_action_plan_eval.payload_hash),
               evidence_refs_json=(SELECT p.evidence_refs_json FROM brain_action_plan_eval_payload p WHERE p.payload_hash=brain_action_plan_eval.payload_hash),
               boundary_json=(SELECT p.boundary_json FROM brain_action_plan_eval_payload p WHERE p.payload_hash=brain_action_plan_eval.payload_hash)
           WHERE payload_hash IN (SELECT payload_hash FROM brain_action_plan_eval_payload)""",
    )
    # evolution_decision: post-convergence the six JSON projections live only in
    # mutation_payload (interned via payload_hash) and are rehydrated by readers
    # through the JOIN, so there is no inline wide-column copy to restore.
    conn.commit()
    return {"schema_version": "state_payload_compaction_rollback.v1", "ok": True}


def _rewrite_compacted_tables(
    conn: Any,
    *,
    include_supervisor_review: bool = False,
) -> dict[str, Any]:
    """Return disk space after the JSON projections have been cleared."""

    tables = ["runtime_config_snapshot", "brain_action_plan_eval", "evolution_decision"]
    if include_supervisor_review:
        tables.extend(SUPERVISOR_REVIEW_TABLES)
    if _is_pg(conn):
        conn.commit()
        conn.autocommit = True
        for table in tables:
            conn.execute(f"VACUUM (FULL, ANALYZE) {table}")
        return {"rewrite": "vacuum_full_analyze", "tables": tables}
    conn.commit()
    conn.execute("VACUUM")
    return {"rewrite": "sqlite_vacuum", "tables": tables}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Read-only statistics; default mode.")
    mode.add_argument("--apply", action="store_true", help="Backfill payload refs and JSON projections; no physical rewrite by default.")
    mode.add_argument("--verify", action="store_true", help="Verify payload refs and metadata digest.")
    mode.add_argument("--rollback", action="store_true", help="Restore JSON projections from payload tables.")
    parser.add_argument(
        "--rewrite",
        action="store_true",
        help="Explicitly run the physical table rewrite after apply; requires a distinct rewrite maintenance id.",
    )
    parser.add_argument("--db-path", default=str(STATE_DB))
    parser.add_argument("--maintenance-id", default="")
    parser.add_argument("--rewrite-maintenance-id", default="")
    parser.add_argument("--manifest", default="")
    parser.add_argument(
        "--targets",
        choices=("payload", "supervisor_review", "all"),
        default=None,
        help="Compaction domain; dry-run defaults to all, writes default to payload.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_ROW_BATCH_SIZE,
        help=f"Rows fetched and committed per batch (1-5000; default {DEFAULT_ROW_BATCH_SIZE}).",
    )
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=DEFAULT_PAYLOAD_CHUNK_ROWS,
        help=(
            "PostgreSQL payload-stat rows per keyset chunk "
            f"(0 for one bounded server-side scan, or 256-20000; default {DEFAULT_PAYLOAD_CHUNK_ROWS})."
        ),
    )
    args = parser.parse_args(argv)
    if not 1 <= args.batch_size <= 5000:
        parser.error("--batch-size must be between 1 and 5000")
    if args.chunk_size != 0 and not 256 <= args.chunk_size <= 20000:
        parser.error("--chunk-size must be 0 or between 256 and 20000")
    if args.rewrite and not args.apply:
        parser.error("--rewrite requires --apply")
    db_path = Path(args.db_path)
    write = bool(args.apply or args.rollback)
    default_targets = "payload" if (write or args.verify) else "all"
    targets = str(args.targets or default_targets)
    target_payload = targets in {"payload", "all"}
    target_supervisor_review = targets in {"supervisor_review", "all"}
    if write and not str(args.maintenance_id).strip():
        parser.error("--maintenance-id is required for --apply/--rollback")
    if write and not str(args.maintenance_id).startswith("maintenance_"):
        parser.error("--maintenance-id must start with maintenance_")
    if args.rewrite:
        rewrite_id = str(args.rewrite_maintenance_id).strip()
        if not rewrite_id:
            parser.error("--rewrite-maintenance-id is required for --rewrite")
        if rewrite_id == str(args.maintenance_id).strip():
            parser.error("--rewrite-maintenance-id must be distinct from --maintenance-id")
        if not rewrite_id.startswith("maintenance_"):
            parser.error("--rewrite-maintenance-id must start with maintenance_")
    manifest_path = Path(args.manifest or (ROOT / "data" / f"state_payload_compact_{args.maintenance_id or 'dry-run'}.json"))
    conn = None
    try:
        conn = _connect(db_path, write=write)
        if write and target_payload:
            ensure_state_payload_schema(db_path, conn)
        missing = _required_schema(conn) if target_payload else []
        if write and target_supervisor_review:
            missing.extend(_supervisor_review_schema_status(conn)["missing"])
        if missing:
            payload = {
                "schema_version": MANIFEST_VERSION,
                "ok": False,
                "error": "state_payload_schema_missing",
                "missing": missing,
                "read_only": not write,
            }
            print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
            return 2
        if args.verify:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else None
            result = _verify(conn, manifest) if target_payload else {
                "schema_version": "state_payload_compaction_verify.v1",
                "ok": True,
                "read_only": True,
            }
            if target_supervisor_review:
                supervisor_result = _verify_supervisor_review_payloads(conn)
                expected_counts = dict(
                    ((manifest or {}).get("apply_result") or {})
                    .get("supervisor_review", {})
                    .get("source_row_counts_before", {})
                )
                if expected_counts:
                    current_counts = dict(supervisor_result.get("source_row_counts") or {})
                    supervisor_result["source_row_counts_unchanged"] = current_counts == expected_counts
                    supervisor_result["source_row_counts_before"] = expected_counts
                    supervisor_result["ok"] = bool(
                        supervisor_result.get("ok") and current_counts == expected_counts
                    )
                result["supervisor_review"] = supervisor_result
                result["ok"] = bool(result.get("ok") and supervisor_result.get("ok"))
        elif args.rollback:
            result = _rollback(conn) if target_payload else {
                "schema_version": "state_payload_compaction_rollback.v1",
                "ok": True,
                "read_only": False,
            }
            if target_supervisor_review:
                result["supervisor_review"] = _rollback_supervisor_review_payloads(conn)
                result["ok"] = bool(result.get("ok") and result["supervisor_review"].get("ok"))
        elif args.apply:
            preflight = _maintenance_preflight(conn)
            if not preflight.get("ok"):
                raise RuntimeError(
                    "maintenance_preflight_failed:" + json.dumps(preflight, sort_keys=True)
                )
            if target_supervisor_review and not _supervisor_review_consumer_coverage()["all_migrated"]:
                raise RuntimeError(
                    "supervisor_review_consumer_migration_incomplete:"
                    + json.dumps(_supervisor_review_consumer_coverage()["pending"], sort_keys=True)
                )
            gaps = _existing_reference_gaps(conn)
            if target_payload and any(gaps.values()):
                raise RuntimeError(f"existing_payload_reference_gaps:{json.dumps(gaps, sort_keys=True)}")
            before = _metadata_manifest(conn, batch_size=args.batch_size)
            before_relation_sizes = _relation_sizes(conn)
            apply_result: dict[str, Any] = {}
            if target_payload:
                apply_result.update(_apply_payload_refs(conn, batch_size=args.batch_size))
            if target_supervisor_review:
                apply_result["supervisor_review"] = _apply_supervisor_review_payloads(
                    conn,
                    batch_size=args.batch_size,
                )
            apply_result["maintenance_preflight"] = preflight
            apply_result["rewrite"] = {
                "status": "skipped",
                "reason": "explicit_rewrite_required",
                "tables": [],
            }
            if args.rewrite and (target_payload or target_supervisor_review):
                apply_result.update(
                    _rewrite_compacted_tables(
                        conn,
                        include_supervisor_review=target_supervisor_review,
                    )
                )
                apply_result["rewrite_maintenance_id"] = str(args.rewrite_maintenance_id)
            apply_result["after_relation_sizes_bytes"] = _relation_sizes(conn)
            manifest = _write_manifest(
                manifest_path,
                maintenance_id=str(args.maintenance_id),
                before=before,
                before_relation_sizes=before_relation_sizes,
                apply_result=apply_result,
            )
            result = {"schema_version": MANIFEST_VERSION, "ok": True, "manifest": str(manifest_path), **apply_result}
        else:
            result = _dry_run(
                conn,
                batch_size=args.batch_size,
                chunk_rows=args.chunk_size,
                targets=targets,
            )
            if args.manifest:
                result["manifest"] = _write_dry_run_manifest(
                    manifest_path,
                    targets=targets,
                    result=result,
                )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0 if result.get("ok") else 2
    except Exception as exc:  # noqa: BLE001
        print(
            json.dumps(
                {"schema_version": MANIFEST_VERSION, "ok": False, "error_type": type(exc).__name__, "error": str(exc)},
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    finally:
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
