"""Persistent, asynchronously refreshed projection for the expensive readiness graph."""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable

from backend.core.db import STATE_DB, connect_sqlite, get_state_pg_conn, is_state_db_path


SNAPSHOT_KEY = "backend_readiness_snapshot.v1"
_REFRESH_LOCK = threading.Lock()
logger = logging.getLogger(__name__)


class BackendReadinessSnapshotService:
    """Keep request handlers off the full sequential readiness build path."""

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

    def _ensure(self, conn: Any) -> None:
        conn.execute(self._sql("""
            CREATE TABLE IF NOT EXISTS runtime_kv (
                key TEXT PRIMARY KEY,
                value_json TEXT NOT NULL DEFAULT '{}',
                updated_at REAL NOT NULL DEFAULT 0.0
            )
        """))

    def latest(self) -> dict[str, Any]:
        conn = None
        try:
            conn = self._conn(read_only=True)
            row = conn.execute(
                self._sql("SELECT value_json, updated_at FROM runtime_kv WHERE key=?"),
                (SNAPSHOT_KEY,),
            ).fetchone()
        except Exception as exc:
            return {"ok": False, "status": "unavailable", "error": f"{type(exc).__name__}: {exc}"}
        finally:
            if conn is not None:
                conn.close()
        if not row:
            return {"ok": False, "status": "missing", "payload": {}, "age_seconds": None}
        try:
            payload = json.loads(row["value_json"] or "{}")
        except Exception:
            payload = {}
        updated_at = float(row["updated_at"] or 0.0)
        return {
            "ok": bool(payload),
            "status": "available" if payload else "invalid",
            "payload": payload if isinstance(payload, dict) else {},
            "updated_at": updated_at,
            "age_seconds": max(0.0, time.time() - updated_at) if updated_at else None,
        }

    def publish(self, payload: dict[str, Any]) -> dict[str, Any]:
        now = time.time()
        conn = self._conn()
        try:
            self._ensure(conn)
            conn.execute(
                self._sql("""
                    INSERT INTO runtime_kv (key, value_json, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET
                        value_json=excluded.value_json,
                        updated_at=excluded.updated_at
                """),
                (SNAPSHOT_KEY, json.dumps(payload, ensure_ascii=False, default=str), now),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return {"ok": True, "status": "published", "updated_at": now}

    def refresh(self, builder: Callable[[], dict[str, Any]] | None = None) -> dict[str, Any]:
        if builder is None:
            from backend.services.backend_readiness import BackendReadinessService

            builder = BackendReadinessService(db_path=self.db_path).build
        started = time.perf_counter()
        payload = builder()
        payload.setdefault("snapshot", {})
        payload["snapshot"].update({
            "schema_version": "backend_readiness_snapshot.v1",
            "build_seconds": round(time.perf_counter() - started, 3),
            "generated_in_background": True,
        })
        self.publish(payload)
        return payload

    def refresh_async(self, *, max_age_seconds: float = 180.0) -> dict[str, Any]:
        current = self.latest()
        age = current.get("age_seconds")
        if current.get("ok") and age is not None and float(age) <= max_age_seconds:
            return {"ok": True, "status": "fresh", "age_seconds": age}
        if _REFRESH_LOCK.locked():
            return {"ok": True, "status": "refresh_in_progress", "age_seconds": age}

        def _worker() -> None:
            if not _REFRESH_LOCK.acquire(blocking=False):
                return
            try:
                self.refresh()
            except Exception:
                logger.exception("backend readiness snapshot refresh failed")
            finally:
                _REFRESH_LOCK.release()

        threading.Thread(target=_worker, name="backend_readiness_snapshot", daemon=True).start()
        return {"ok": True, "status": "refresh_started", "age_seconds": age}
