"""Fact watermark for skipping autonomous-learning rebuilds with no new evidence."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from backend.core.db import STATE_DB, connect_sqlite, get_state_pg_conn, is_state_db_path
from backend.core.state_store import validate_runtime_state_schema
from backend.services.canonical_v2 import CANONICAL_EVENT_SCHEMA
from backend.services.fact_envelope import observed_epoch


WATERMARK_KEY = "autonomous_learning.fact_watermark.v1"
WATERMARK_SCHEMA_VERSION = "autonomous_learning_fact_watermark.v2"
SOURCE_IDENTITY_CONTRACT_VERSION = "canonical_v2_fact_source_identity.v1"
CANONICAL_FACT_TYPES = (
    "risk_decision",
    "broker_execution",
    "position_transition",
    "trade_review",
    "supervisor_trace",
    "counterfactual_review",
)


class LearningCycleWatermarkService:
    """Persist the source-fact frontier only after a successful learning cycle."""

    def __init__(self, db_path: str | Path = STATE_DB):
        self.db_path = Path(db_path)

    def _use_pg(self) -> bool:
        return is_state_db_path(self.db_path)

    def _sql(self, sql: str) -> str:
        return sql.replace("?", "%s") if self._use_pg() else sql

    def _conn(self, *, read_only: bool = False):
        conn = get_state_pg_conn(read_only=read_only) if self._use_pg() else connect_sqlite(self.db_path, read_only=read_only)
        if not self._use_pg():
            conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _fingerprint(sources: dict[str, Any]) -> str:
        canonical = json.dumps(sources, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _event_columns(self, conn: Any, event_table: str) -> set[str]:
        """Return event columns without touching payload/TOAST data."""
        if self._use_pg():
            rows = conn.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema='canonical_v2' AND table_name='event'
                """
            ).fetchall()
            return {str(row["column_name"]) for row in rows}
        rows = conn.execute(f"PRAGMA table_info({event_table})").fetchall()
        return {str(row["name"] if hasattr(row, "keys") else row[1]) for row in rows}

    def _storage_identity(self, conn: Any) -> dict[str, Any]:
        """Identify a storage rebuild using catalog/file metadata only.

        PostgreSQL changes relfilenode for TRUNCATE, VACUUM FULL, and a
        dump/restore-created relation while ordinary appends preserve it.  The
        SQLite fallback uses the database file identity for the same purpose.
        This is a generation signal, not a data authority; the event anchor
        and frontier remain part of the contract.
        """
        if self._use_pg():
            row = conn.execute(
                """
                SELECT c.oid::bigint AS relation_oid,
                       c.relfilenode::bigint AS relfilenode,
                       c.reltoastrelid::bigint AS toast_oid
                FROM pg_class c
                JOIN pg_namespace n ON n.oid=c.relnamespace
                WHERE n.nspname='canonical_v2' AND c.relname='event'
                """
            ).fetchone()
            if not row:
                return {"status": "unavailable", "reason": "event_relation_missing"}
            return {
                "status": "available",
                "relation_oid": int(row["relation_oid"]),
                "relfilenode": int(row["relfilenode"]),
                "toast_oid": int(row["toast_oid"]),
            }
        try:
            stat = self.db_path.stat()
            return {
                "status": "available",
                "device": int(stat.st_dev),
                "inode": int(stat.st_ino),
            }
        except OSError:
            return {"status": "unavailable", "reason": "state_file_missing"}

    def _source_anchor(self, conn: Any, event_table: str) -> dict[str, Any]:
        """Read one deterministic event anchor for append-stable source identity."""
        columns = self._event_columns(conn, event_table)
        available = [
            column
            for column in (
                "event_id",
                "event_type",
                "entity_id",
                "observed_at",
                "recorded_at",
                "created_at",
                "producer",
                "idempotency_key",
                "payload_hash",
            )
            if column in columns
        ]
        if not available or "event_type" not in columns:
            return {"status": "unavailable", "reason": "event_columns_missing"}
        order_columns = [
            column
            for column in ("recorded_at", "created_at", "observed_at", "event_id")
            if column in columns
        ]
        if not order_columns:
            order_columns = ["event_type"]
        placeholders = ", ".join("?" for _ in CANONICAL_FACT_TYPES)
        select_columns = list(available)
        if self._use_pg():
            # xmin changes when the anchor row is recreated, even when a
            # rebuild preserves its business event_id and payload hash.
            select_columns.append("xmin::text AS _source_row_xmin")
        row = conn.execute(
            self._sql(
                f"SELECT {', '.join(select_columns)} FROM {event_table} "
                f"WHERE event_type IN ({placeholders}) "
                f"ORDER BY {', '.join(order_columns)} LIMIT 1"
            ),
            CANONICAL_FACT_TYPES,
        ).fetchone()
        if not row:
            return {"status": "empty"}
        return {
            "status": "populated",
            "event": {
                column: row[column]
                for column in (*available, "_source_row_xmin")
                if column in row.keys() and row[column] is not None
            },
        }

    def current(self) -> dict[str, Any]:
        conn = self._conn(read_only=True)
        try:
            sources: dict[str, Any] = {}
            event_table = "canonical_v2.event" if self._use_pg() else "event"
            for event_type in CANONICAL_FACT_TYPES:
                try:
                    row = conn.execute(
                        self._sql(
                            "SELECT COUNT(*) AS row_count, MAX(observed_at) AS max_ts "
                            f"FROM {event_table} WHERE event_type=?"
                        ),
                        (event_type,),
                    ).fetchone()
                    raw_ts = row["max_ts"] if row else None
                    sources[event_type] = {
                        "row_count": int((row["row_count"] if row else 0) or 0),
                        "max_ts": observed_epoch(raw_ts),
                    }
                except Exception:
                    sources[event_type] = {"row_count": 0, "max_ts": 0.0, "unavailable": True}
            try:
                anchor = self._source_anchor(conn, event_table)
            except Exception:
                anchor = {"status": "unavailable", "reason": "event_anchor_query_failed"}
            try:
                storage = self._storage_identity(conn)
            except Exception:
                storage = {"status": "unavailable", "reason": "storage_identity_query_failed"}
            source_identity = {
                "contract_version": SOURCE_IDENTITY_CONTRACT_VERSION,
                "event_schema_version": CANONICAL_EVENT_SCHEMA,
                "storage": storage,
                "anchor": anchor,
            }
            source_fingerprint = self._fingerprint(source_identity)
            identity_status = (
                "unavailable"
                if anchor.get("status") == "unavailable" or storage.get("status") == "unavailable"
                else anchor.get("status", "unavailable")
            )
            return {
                "schema_version": WATERMARK_SCHEMA_VERSION,
                "source_identity_contract_version": SOURCE_IDENTITY_CONTRACT_VERSION,
                "source_identity_status": identity_status,
                "sources": sources,
                "source_fingerprint": source_fingerprint,
                # Keep the old field name as a read/display compatibility alias;
                # evaluation trusts only source_fingerprint plus its contract.
                "fingerprint": source_fingerprint,
            }
        finally:
            conn.close()

    def last_completed(self) -> dict[str, Any]:
        conn = self._conn(read_only=True)
        try:
            try:
                row = conn.execute(
                    self._sql("SELECT value_json, updated_at FROM runtime_kv WHERE key=?"),
                    (WATERMARK_KEY,),
                ).fetchone()
            except Exception:
                row = None
            if not row:
                return {}
            payload = json.loads(row["value_json"] or "{}")
            payload["updated_at"] = float(row["updated_at"] or 0.0)
            return payload
        finally:
            conn.close()

    def evaluate(self) -> dict[str, Any]:
        current = self.current()
        previous = self.last_completed()
        previous_fingerprint = str(previous.get("source_fingerprint") or "")
        previous_contract = str(previous.get("source_identity_contract_version") or "")
        if current.get("source_identity_status") == "unavailable":
            reason = "canonical_source_identity_unavailable"
            changed = True
        elif not previous_fingerprint or previous_contract != SOURCE_IDENTITY_CONTRACT_VERSION:
            reason = "legacy_watermark_missing_source_identity"
            changed = True
        else:
            previous_sources = previous.get("sources")
            current_sources = current.get("sources")
            if not isinstance(previous_sources, dict) or not isinstance(current_sources, dict):
                reason = "legacy_watermark_missing_sources"
                changed = True
            else:
                regressions = [
                    event_type
                    for event_type in CANONICAL_FACT_TYPES
                    if int((current_sources.get(event_type) or {}).get("row_count") or 0)
                    < int((previous_sources.get(event_type) or {}).get("row_count") or 0)
                ]
                if regressions:
                    reason = "canonical_source_count_regressed"
                    changed = True
                elif current.get("source_fingerprint") != previous_fingerprint:
                    reason = "canonical_source_identity_changed"
                    changed = True
                elif current_sources != previous_sources:
                    # The identity is append-stable; the per-type frontier is
                    # intentionally append-sensitive and drives new work.
                    reason = "canonical_fact_frontier_advanced"
                    changed = True
                else:
                    reason = "no_new_facts"
                    changed = False
        return {
            "ok": True,
            "status": "new_facts" if changed else "no_new_facts",
            "should_run": changed,
            "reason": reason,
            "current": current,
            "previous_fingerprint": previous_fingerprint,
        }

    def mark_completed(self, watermark: dict[str, Any]) -> None:
        conn = self._conn()
        now = time.time()
        try:
            declaration = self._sql("""
                CREATE TABLE IF NOT EXISTS runtime_kv (
                    key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL DEFAULT '{}',
                    updated_at REAL NOT NULL DEFAULT 0.0
                )
            """)
            if self._use_pg():
                validate_runtime_state_schema(conn, declaration)
            else:
                conn.execute(declaration)
            conn.execute(
                self._sql("""
                    INSERT INTO runtime_kv (key, value_json, updated_at) VALUES (?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at
                """),
                (WATERMARK_KEY, json.dumps(watermark, sort_keys=True, default=str), now),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
