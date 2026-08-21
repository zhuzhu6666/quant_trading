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
from backend.services.fact_envelope import observed_epoch


WATERMARK_KEY = "autonomous_learning.fact_watermark.v1"
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
            return {
                "schema_version": "autonomous_learning_fact_watermark.v1",
                "sources": sources,
                "fingerprint": self._fingerprint(sources),
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
        changed = current["fingerprint"] != previous.get("fingerprint")
        return {
            "ok": True,
            "status": "new_facts" if changed else "no_new_facts",
            "should_run": changed,
            "current": current,
            "previous_fingerprint": previous.get("fingerprint", ""),
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
