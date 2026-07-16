"""Cross-process read model for the exact live factor selection."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from backend.core.db import STATE_DB, connect_sqlite, get_state_pg_conn, is_state_db_path


PROJECTION_KEY = "runtime_factor_selection.v1"


class RuntimeFactorSelectionProjectionService:
    def __init__(self, db_path: str | Path = STATE_DB):
        self.db_path = Path(db_path)

    def _use_pg(self) -> bool:
        return is_state_db_path(self.db_path)

    def _sql(self, sql: str) -> str:
        return sql.replace("?", "%s") if self._use_pg() else sql

    def _conn(self, *, read_only: bool = False):
        if self._use_pg():
            return get_state_pg_conn(read_only=read_only)
        conn = connect_sqlite(self.db_path, read_only=read_only)
        conn.row_factory = sqlite3.Row
        return conn

    def publish(self, selection: Any, *, source: str = "live_factor_pipeline") -> dict[str, Any]:
        now = time.time()
        payload = {
            "schema_version": "runtime_factor_selection.v1",
            "source": source,
            "selected_factor_ids": list(getattr(selection, "selected_factor_ids", []) or []),
            "excluded_factor_ids": list(getattr(selection, "excluded_factor_ids", []) or []),
            "reason_excluded": dict(getattr(selection, "reason_excluded", {}) or {}),
            "published_at": now,
        }
        conn = self._conn()
        try:
            conn.execute(self._sql("""
                CREATE TABLE IF NOT EXISTS runtime_kv (
                    key TEXT PRIMARY KEY, value_json TEXT NOT NULL DEFAULT '{}',
                    updated_at REAL NOT NULL DEFAULT 0.0
                )
            """))
            conn.execute(self._sql("""
                INSERT INTO runtime_kv (key, value_json, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,
                    updated_at=excluded.updated_at
            """), (PROJECTION_KEY, json.dumps(payload, ensure_ascii=False), now))
            conn.commit()
            return {**payload, "ok": True}
        finally:
            conn.close()

    def latest(self, *, max_age_seconds: float = 900.0) -> dict[str, Any]:
        conn = None
        try:
            conn = self._conn(read_only=True)
            row = conn.execute(self._sql(
                "SELECT value_json, updated_at FROM runtime_kv WHERE key=?"
            ), (PROJECTION_KEY,)).fetchone()
            if not row:
                return {"ok": False, "status": "missing"}
            raw = row["value_json"]
            payload = dict(raw) if isinstance(raw, dict) else json.loads(str(raw or "{}"))
            updated_at = float(row["updated_at"] or payload.get("published_at") or 0.0)
            age = max(0.0, time.time() - updated_at)
            fresh = age <= max(1.0, float(max_age_seconds))
            return {**payload, "ok": fresh, "status": "fresh" if fresh else "stale", "age_seconds": age}
        except Exception as exc:
            return {"ok": False, "status": "unavailable", "error": f"{type(exc).__name__}: {exc}"}
        finally:
            if conn is not None:
                conn.close()
