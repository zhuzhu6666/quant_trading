"""Single writer for the runtime_kv recovery/projection cache.

``runtime_kv`` is a read model, not a source of trading facts.  Its large
JSON values are nevertheless written frequently by live/readiness/learning
loops, so updating the value on every heartbeat creates unnecessary TOAST and
WAL churn.  This module compares a key-specific semantic projection and only
rewrites ``value_json`` when that projection changes.
"""

from __future__ import annotations

import json
import sqlite3
import time
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping

from backend.core.db import STATE_DB, connect_sqlite, get_state_pg_conn, is_state_db_path
from backend.core.db_helpers import dump_json, execute, load_json, row_value
from backend.core.state_store import validate_runtime_state_schema


RUNTIME_KV_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS runtime_kv (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL DEFAULT '{}',
    updated_at DOUBLE PRECISION NOT NULL DEFAULT 0.0
)
"""

# These paths are occurrence/heartbeat metadata for the named projections.
# Unknown keys deliberately have no normalizer: a new projection must opt into
# volatile-field handling explicitly instead of silently losing semantic data.
_VOLATILE_PATHS: dict[str, tuple[tuple[str, ...], ...]] = {
    "backend_readiness_snapshot.v1": (
        # Build duration is diagnostic telemetry, not readiness state; the
        # refresh loop measures it anew on every pass and must not force a
        # multi-megabyte TOAST rewrite when the readiness facts are unchanged.
        ("snapshot", "build_seconds"),
        ("snapshot", "generated_at"),
        ("snapshot", "published_at"),
        ("published_at",),
        ("generated_at",),
        ("updated_at",),
    ),
    "runtime_factor_selection.v1": (
        ("heartbeat_at",),
        ("published_at",),
        ("updated_at",),
    ),
    "runtime_health_projection.v1": (
        ("published_at",),
        ("updated_at",),
        ("ctrader", "updated_at"),
        ("live_loop", "updated_at"),
    ),
    "position_supervisor_selection.v1": (
        ("heartbeat_at",),
        ("published_at",),
        ("updated_at",),
    ),
    "evolution_cycle_watermark.v1": (
        ("completed_at",),
        ("updated_at",),
    ),
    "autonomous_learning.fact_watermark.v1": (
        ("completed_at",),
        ("updated_at",),
    ),
    "factor_governance_evidence_streak.v1": (
        ("updated_at",),
        ("last_new_evidence_at",),
    ),
    "learning_worker.capability.v2": (("updated_at",),),
    "persistent_job_worker.capability.v1": (("updated_at",),),
    "risk_metrics_snapshot.v2": (
        ("updated_at",),
        ("published_at",),
    ),
}

# ``backend_readiness_snapshot.v1`` is a deliberately rich diagnostic
# projection.  Its builder includes nested clock/age values from the live
# loop, reconciliation, freshness checks and duplicated v15 views.  Those
# values are useful while building the response, but they are not the
# readiness facts themselves: the row-level ``runtime_kv.updated_at`` is the
# freshness clock for this projection.  Keep this list scoped to this one
# projection; applying it globally would silently erase timestamps that are
# semantic in another state record.
_READINESS_VOLATILE_KEYS = frozenset(
    {
        "updated_at",
        "generated_at",
        "published_at",
        "heartbeat_at",
        "loaded_at",
        "checked_at",
        "planned_at",
        "observed_at",
        "created_at",
        "started_at",
        "finished_at",
        "ended_at",
        "opened_at",
        "last_success_at",
        "last_failure_at",
        "last_exit_at",
        "latest_indexed_at",
        "first_observed_at",
        "last_observed_at",
        "runtime_kv_updated_at",
        "account_updated_at",
        "positions_updated_at",
        "account_event_updated_at",
        "positions_event_updated_at",
        "account_reconcile_failed_at",
        "positions_reconcile_failed_at",
        "alpha_heartbeat_at",
        "safety_heartbeat_at",
        "build_seconds",
        "age_seconds",
        "age_sec",
        # Reconciliation/snapshot ids are fresh occurrence handles in this
        # read-only projection.  The underlying account/position facts,
        # status and error fields remain semantic and are retained.
        "account_reconcile_id",
        "positions_reconcile_id",
        "reconcile_id",
        "snapshot_id",
        "latest_snapshot_id",
        "first_snapshot_id",
    }
)


def _normalize_readiness_payload(value: Any) -> Any:
    """Remove nested refresh/age telemetry from the readiness projection.

    A few producers use names such as ``safety_heartbeat_age_sec`` and
    ``quote_age_seconds`` rather than one of the exact keys above.  Preserve
    configured limits (``max_age_seconds``) and all IDs, hashes, statuses,
    blockers, errors and process/build identity while dropping only those
    derived age values.
    """

    if isinstance(value, dict):
        normalized: dict[Any, Any] = {}
        for key, child in value.items():
            key_text = str(key)
            lowered = key_text.lower()
            is_age = (
                lowered.endswith("_age_sec") or lowered.endswith("_age_seconds")
            ) and lowered not in {"max_age_sec", "max_age_seconds"}
            if lowered in _READINESS_VOLATILE_KEYS or is_age:
                continue
            normalized[key] = _normalize_readiness_payload(child)
        return normalized
    if isinstance(value, list):
        return [_normalize_readiness_payload(item) for item in value]
    return value


def _remove_path(value: Any, path: tuple[str, ...]) -> None:
    if not path or not isinstance(value, dict):
        return
    if len(path) == 1:
        value.pop(path[0], None)
        return
    child = value.get(path[0])
    if isinstance(child, dict):
        _remove_path(child, path[1:])


def semantic_payload(key: str, value: Any) -> Any:
    """Return a deep-copied payload with only registered volatile paths removed."""

    payload = deepcopy(value if value is not None else {})
    if str(key) == "backend_readiness_snapshot.v1":
        payload = _normalize_readiness_payload(payload)
    for path in _VOLATILE_PATHS.get(str(key), ()):
        _remove_path(payload, path)
    return payload


def semantic_json(key: str, value: Any) -> str:
    return dump_json(semantic_payload(key, value))


def _use_pg_connection(conn: Any) -> bool:
    return conn.__class__.__module__.split(".", 1)[0] == "psycopg"


def ensure_runtime_kv_table(conn: Any) -> None:
    if _use_pg_connection(conn):
        validate_runtime_state_schema(conn, RUNTIME_KV_TABLE_DDL)
    else:
        conn.execute(RUNTIME_KV_TABLE_DDL)


def set_on_conn(
    conn: Any,
    key: str,
    value: Any,
    *,
    updated_at: float | None = None,
    ensure: bool = True,
) -> dict[str, Any]:
    """Write one key using the caller's transaction/connection.

    A semantic no-op updates only the row freshness timestamp.  This keeps
    health/readiness freshness accurate while avoiding a new TOAST value.
    """

    if ensure:
        ensure_runtime_kv_table(conn)
    ts = float(time.time() if updated_at is None else updated_at)
    key_text = str(key or "")
    value_json = dump_json(value if value is not None else {})
    row = execute(
        conn,
        "SELECT value_json FROM runtime_kv WHERE key=?",
        (key_text,),
    ).fetchone()
    if row is not None:
        existing_raw = row_value(row, "value_json", 0, "{}")
        existing = load_json(existing_raw, {})
        if semantic_json(key_text, existing) == semantic_json(key_text, value):
            execute(
                conn,
                "UPDATE runtime_kv SET updated_at=? WHERE key=?",
                (ts, key_text),
            )
            return {
                "key": key_text,
                "updated_at": ts,
                "changed": False,
                "heartbeat_only": True,
            }
    execute(
        conn,
        """
        INSERT INTO runtime_kv(key, value_json, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET
            value_json=excluded.value_json,
            updated_at=excluded.updated_at
        """,
        (key_text, value_json, ts),
    )
    return {
        "key": key_text,
        "updated_at": ts,
        "changed": True,
        "heartbeat_only": False,
    }


class RuntimeKVStore:
    """Connection-owning convenience facade around :func:`set_on_conn`."""

    def __init__(self, db_path: str | Path = STATE_DB):
        self.db_path = Path(db_path)

    def _conn(self, *, read_only: bool = False):
        if is_state_db_path(self.db_path):
            return get_state_pg_conn(read_only=read_only)
        conn = connect_sqlite(self.db_path, read_only=read_only)
        conn.row_factory = sqlite3.Row
        return conn

    def get(self, key: str, default: Any = None) -> Any:
        conn = self._conn(read_only=True)
        try:
            row = execute(
                conn,
                "SELECT value_json FROM runtime_kv WHERE key=?",
                (str(key or ""),),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return default
        return load_json(row_value(row, "value_json", 0, "{}"), default)

    def set(
        self,
        key: str,
        value: Any,
        *,
        updated_at: float | None = None,
    ) -> dict[str, Any]:
        conn = self._conn()
        try:
            result = set_on_conn(conn, key, value, updated_at=updated_at)
            conn.commit()
            return result
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


__all__ = [
    "RuntimeKVStore",
    "RUNTIME_KV_TABLE_DDL",
    "ensure_runtime_kv_table",
    "semantic_json",
    "semantic_payload",
    "set_on_conn",
]
