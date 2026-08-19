"""Shared payload interning for large PostgreSQL state records.

The event tables remain occurrence ledgers.  This module only interns the
large JSON payloads they point at; it never decides that two event rows are
the same event and never removes an occurrence.

PostgreSQL schema objects are migration-owned.  The SQLite branches exist for
unit tests and offline fixtures, matching the repository's state-store
boundary.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any, Mapping

from backend.core.db import (
    STATE_DB,
    connect_sqlite,
    get_state_pg_conn,
    is_state_db_path,
    state_table_columns,
    state_table_exists,
)
from backend.core.db_helpers import conn_is_pg as _is_pg


RUNTIME_CONFIG_PAYLOAD_TABLE = "runtime_config_payload"
BRAIN_ACTION_PLAN_EVAL_PAYLOAD_TABLE = "brain_action_plan_eval_payload"
MUTATION_PAYLOAD_TABLE = "mutation_payload"


def _sql(conn: Any, statement: str) -> str:
    return statement.replace("?", "%s") if _is_pg(conn) else statement


def _row_value(row: Any, key: str, index: int = 0, default: Any = None) -> Any:
    if row is None:
        return default
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        try:
            return row[index]
        except (KeyError, IndexError, TypeError):
            return default


def stable_json(value: Any) -> str:
    """Serialize JSON deterministically without changing numeric values."""

    return json.dumps(
        value if value is not None else {},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def payload_hash(raw: str, *, namespace: str = "") -> str:
    """Return a collision-resistant hash over the exact stored payload text."""

    value = f"{namespace}\x00{raw}".encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def mutation_payload_hash(parts: Mapping[str, str]) -> str:
    """Hash the six mutation JSON fields with explicit field names."""

    raw = "\x00".join(f"{key}={parts.get(key, '')}" for key in sorted(parts))
    return payload_hash(raw, namespace="mutation_payload.v1")


def _connect(db_path: str | Path = STATE_DB):
    conn = get_state_pg_conn() if is_state_db_path(db_path) else connect_sqlite(db_path)
    if not is_state_db_path(db_path):
        conn.row_factory = __import__("sqlite3").Row
    return conn


def _require_pg_contract(conn: Any) -> None:
    required_tables = {
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
    required_columns = {
        "runtime_config_snapshot": {"payload_hash"},
        "brain_action_plan_eval": {"payload_hash", "evaluation_run_id"},
        "evolution_decision": {"payload_hash", "canonical_event_id", "projection_type"},
    }
    missing: list[str] = []
    for table, columns in required_tables.items():
        if not state_table_exists(conn, table):
            missing.append(f"table:{table}")
            continue
        missing.extend(
            f"column:{table}.{column}"
            for column in sorted(columns - state_table_columns(conn, table))
        )
    for table, columns in required_columns.items():
        if not state_table_exists(conn, table):
            missing.append(f"table:{table}")
            continue
        missing.extend(
            f"column:{table}.{column}"
            for column in sorted(columns - state_table_columns(conn, table))
        )
    if missing:
        raise RuntimeError(
            "state_payload_schema_missing:"
            + ",".join(missing)
            + "; run scripts/state_schema_migrate.py --apply"
        )


def _ensure_sqlite_column(conn: Any, table: str, column: str, definition: str) -> None:
    if column not in state_table_columns(conn, table):
        conn.execute(f'ALTER TABLE "{table}" ADD COLUMN "{column}" {definition}')


def ensure_state_payload_schema(db_path: str | Path = STATE_DB, conn: Any | None = None) -> None:
    """Ensure payload objects exist for SQLite or validate the PG migration."""

    owned = conn is None
    active = conn or _connect(db_path)
    try:
        if _is_pg(active):
            _require_pg_contract(active)
            return

        active.execute(
            """CREATE TABLE IF NOT EXISTS runtime_config_payload (
                payload_hash TEXT PRIMARY KEY,
                config_json TEXT NOT NULL,
                byte_length INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL DEFAULT 0.0
            )"""
        )
        active.execute(
            """CREATE TABLE IF NOT EXISTS brain_action_plan_eval_payload (
                payload_hash TEXT PRIMARY KEY,
                comparison_json TEXT NOT NULL DEFAULT '{}',
                evidence_refs_json TEXT NOT NULL DEFAULT '{}',
                boundary_json TEXT NOT NULL DEFAULT '{}',
                byte_length INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL DEFAULT 0.0
            )"""
        )
        active.execute(
            """CREATE TABLE IF NOT EXISTS mutation_payload (
                payload_hash TEXT PRIMARY KEY,
                evidence_json TEXT NOT NULL DEFAULT '{}',
                risk_verdict_json TEXT NOT NULL DEFAULT '{}',
                before_json TEXT NOT NULL DEFAULT '{}',
                after_json TEXT NOT NULL DEFAULT '{}',
                result_json TEXT NOT NULL DEFAULT '{}',
                rollback_json TEXT NOT NULL DEFAULT '{}',
                byte_length INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL DEFAULT 0.0
            )"""
        )
        if state_table_exists(active, "runtime_config_snapshot"):
            _ensure_sqlite_column(active, "runtime_config_snapshot", "payload_hash", "TEXT NOT NULL DEFAULT ''")
        if state_table_exists(active, "brain_action_plan_eval"):
            _ensure_sqlite_column(active, "brain_action_plan_eval", "payload_hash", "TEXT NOT NULL DEFAULT ''")
            _ensure_sqlite_column(active, "brain_action_plan_eval", "evaluation_run_id", "TEXT NOT NULL DEFAULT ''")
        if state_table_exists(active, "evolution_decision"):
            _ensure_sqlite_column(active, "evolution_decision", "payload_hash", "TEXT NOT NULL DEFAULT ''")
            _ensure_sqlite_column(active, "evolution_decision", "canonical_event_id", "TEXT NOT NULL DEFAULT ''")
            _ensure_sqlite_column(active, "evolution_decision", "projection_type", "TEXT NOT NULL DEFAULT 'legacy'")
        if state_table_exists(active, "runtime_config_snapshot"):
            active.execute(
                "CREATE INDEX IF NOT EXISTS idx_runtime_config_snapshot_payload "
                "ON runtime_config_snapshot(payload_hash, config_version)"
            )
        if state_table_exists(active, "brain_action_plan_eval"):
            active.execute(
                "CREATE INDEX IF NOT EXISTS idx_brain_action_plan_eval_payload "
                "ON brain_action_plan_eval(payload_hash, created_at)"
            )
            active.execute(
                "CREATE INDEX IF NOT EXISTS idx_brain_action_plan_eval_run_plan "
                "ON brain_action_plan_eval(evaluation_run_id, plan_id)"
            )
            active.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_brain_action_plan_eval_run_plan_unique "
                "ON brain_action_plan_eval(evaluation_run_id, plan_id) "
                "WHERE evaluation_run_id <> ''"
            )
        if state_table_exists(active, "evolution_decision"):
            active.execute(
                "CREATE INDEX IF NOT EXISTS idx_evolution_decision_canonical "
                "ON evolution_decision(canonical_event_id, projection_type, created_at)"
            )
        if owned:
            active.commit()
    finally:
        if owned:
            active.close()


def put_runtime_config_payload(conn: Any, payload_hash_value: str, config_json: str, *, created_at: float | None = None) -> None:
    conn.execute(
        _sql(
            conn,
            """INSERT INTO runtime_config_payload
               (payload_hash, config_json, byte_length, created_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(payload_hash) DO NOTHING""",
        ),
        (payload_hash_value, config_json, len(config_json.encode("utf-8")), float(created_at or time.time())),
    )


def put_brain_action_plan_eval_payload(
    conn: Any,
    payload_hash_value: str,
    comparison_json: str,
    evidence_refs_json: str,
    boundary_json: str,
    *,
    created_at: float | None = None,
) -> None:
    conn.execute(
        _sql(
            conn,
            """INSERT INTO brain_action_plan_eval_payload
               (payload_hash, comparison_json, evidence_refs_json, boundary_json, byte_length, created_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(payload_hash) DO NOTHING""",
        ),
        (
            payload_hash_value,
            comparison_json,
            evidence_refs_json,
            boundary_json,
            sum(len(item.encode("utf-8")) for item in (comparison_json, evidence_refs_json, boundary_json)),
            float(created_at or time.time()),
        ),
    )


def put_mutation_payload(
    conn: Any,
    payload_hash_value: str,
    parts: Mapping[str, str],
    *,
    created_at: float | None = None,
) -> None:
    fields = (
        "evidence_json",
        "risk_verdict_json",
        "before_json",
        "after_json",
        "result_json",
        "rollback_json",
    )
    values = tuple(str(parts.get(field) or "{}") for field in fields)
    conn.execute(
        _sql(
            conn,
            """INSERT INTO mutation_payload
               (payload_hash, evidence_json, risk_verdict_json, before_json,
                after_json, result_json, rollback_json, byte_length, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(payload_hash) DO NOTHING""",
        ),
        (payload_hash_value, *values, sum(len(item.encode("utf-8")) for item in values), float(created_at or time.time())),
    )


def read_runtime_config_payload(conn: Any, payload_hash_value: str, fallback: str = "{}") -> str:
    if not payload_hash_value:
        return fallback
    row = conn.execute(
        _sql(conn, "SELECT config_json FROM runtime_config_payload WHERE payload_hash=? LIMIT 1"),
        (payload_hash_value,),
    ).fetchone()
    return str(_row_value(row, "config_json", 0, fallback) or fallback)


def read_brain_action_plan_eval_payload(conn: Any, payload_hash_value: str) -> dict[str, str]:
    if not payload_hash_value:
        return {}
    row = conn.execute(
        _sql(
            conn,
            """SELECT comparison_json, evidence_refs_json, boundary_json
               FROM brain_action_plan_eval_payload WHERE payload_hash=? LIMIT 1""",
        ),
        (payload_hash_value,),
    ).fetchone()
    if not row:
        return {}
    return {
        "comparison_json": str(_row_value(row, "comparison_json", 0, "{}") or "{}"),
        "evidence_refs_json": str(_row_value(row, "evidence_refs_json", 1, "{}") or "{}"),
        "boundary_json": str(_row_value(row, "boundary_json", 2, "{}") or "{}"),
    }


def read_mutation_payload(conn: Any, payload_hash_value: str) -> dict[str, str]:
    if not payload_hash_value:
        return {}
    row = conn.execute(
        _sql(
            conn,
            """SELECT evidence_json, risk_verdict_json, before_json, after_json,
                      result_json, rollback_json
               FROM mutation_payload WHERE payload_hash=? LIMIT 1""",
        ),
        (payload_hash_value,),
    ).fetchone()
    if not row:
        return {}
    return {
        field: str(_row_value(row, field, index, "{}") or "{}")
        for index, field in enumerate(
            (
                "evidence_json",
                "risk_verdict_json",
                "before_json",
                "after_json",
                "result_json",
                "rollback_json",
            )
        )
    }
