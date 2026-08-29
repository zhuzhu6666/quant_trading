"""Persistent, asynchronously refreshed projection for the expensive readiness graph."""
from __future__ import annotations

import json
import os
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable

from backend.core.db import STATE_DB, connect_sqlite, get_state_pg_conn, is_state_db_path
from backend.core.state_store import validate_runtime_state_schema
from backend.services.runtime_kv_store import set_on_conn as set_runtime_kv_on_conn


SNAPSHOT_KEY = "backend_readiness_snapshot.v1"
logger = logging.getLogger(__name__)
_PROCESS_STARTED_AT = time.time()


class _AsyncRefreshOwner:
    """Process-owned single-flight thread with explicit drain semantics."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._accepting = True
        self._generation = 0
        self._last_started_at = 0.0
        self._last_finished_at = 0.0
        self._last_error = ""

    def open(self) -> None:
        """Allow a new lifecycle generation to schedule refresh work."""

        with self._lock:
            self._accepting = True

    def start(self, target: Callable[[], None], *, age_seconds: Any) -> dict[str, Any]:
        with self._lock:
            if not self._accepting:
                return {
                    "ok": False,
                    "status": "refresh_draining",
                    "age_seconds": age_seconds,
                    "generation": self._generation,
                    "thread_alive": bool(self._thread and self._thread.is_alive()),
                }
            if self._thread is not None and self._thread.is_alive():
                return {
                    "ok": True,
                    "status": "refresh_in_progress",
                    "age_seconds": age_seconds,
                    "generation": self._generation,
                    "thread_alive": True,
                }
            self._generation += 1
            generation = self._generation
            self._last_started_at = time.time()
            self._last_error = ""

            def _run() -> None:
                try:
                    target()
                except Exception as exc:
                    with self._lock:
                        self._last_error = (
                            f"{type(exc).__name__}:{exc}"
                        )[:300]
                    logger.exception("backend readiness snapshot refresh failed")
                finally:
                    with self._lock:
                        self._last_finished_at = time.time()
                        if self._thread is threading.current_thread():
                            self._thread = None

            # This worker touches DuckDB/Pandas/native extensions.  It must be
            # non-daemon so interpreter teardown cannot unload those libraries
            # while refresh is still executing.  BackendRuntimeLifecycle joins
            # it explicitly during process drain.
            thread = threading.Thread(
                target=_run,
                name=f"backend_readiness_snapshot:{generation}",
                daemon=False,
            )
            self._thread = thread
            thread.start()
            return {
                "ok": True,
                "status": "refresh_started",
                "age_seconds": age_seconds,
                "generation": generation,
                "thread_alive": True,
            }

    def shutdown(self, *, timeout_sec: float) -> dict[str, Any]:
        """Reject new work and wait for the owned native-code worker."""

        with self._lock:
            self._accepting = False
            thread = self._thread
            generation = self._generation
        if thread is None:
            return {
                "ok": True,
                "status": "idle",
                "generation": generation,
                "thread_alive": False,
            }
        if thread is threading.current_thread():
            return {
                "ok": False,
                "status": "self_join_rejected",
                "generation": generation,
                "thread_alive": True,
            }
        thread.join(max(0.0, float(timeout_sec)))
        alive = thread.is_alive()
        return {
            "ok": not alive,
            "status": "timed_out" if alive else "completed",
            "generation": generation,
            "thread_alive": alive,
        }

    def status(self) -> dict[str, Any]:
        with self._lock:
            alive = bool(self._thread and self._thread.is_alive())
            return {
                "accepting": self._accepting,
                "generation": self._generation,
                "thread_alive": alive,
                "refresh_in_progress": alive,
                "refresh_started_at": self._last_started_at,
                "refresh_finished_at": self._last_finished_at,
                "last_refresh_error": self._last_error,
            }


_OWNER_REGISTRY_LOCK = threading.Lock()
_OWNER_REGISTRY: dict[str, _AsyncRefreshOwner] = {}


def _refresh_owner(db_path: Path) -> _AsyncRefreshOwner:
    key = str(db_path.expanduser().resolve())
    with _OWNER_REGISTRY_LOCK:
        owner = _OWNER_REGISTRY.get(key)
        if owner is None:
            owner = _AsyncRefreshOwner()
            _OWNER_REGISTRY[key] = owner
        return owner


class BackendReadinessSnapshotService:
    """Keep request handlers off the full sequential readiness build path."""

    def __init__(self, db_path: str | Path = STATE_DB):
        self.db_path = Path(db_path)
        self._refresh_owner = _refresh_owner(self.db_path)

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
        owner = self._refresh_owner.status()
        return {
            "ok": bool(payload),
            "status": "available" if payload else "invalid",
            "payload": payload if isinstance(payload, dict) else {},
            "updated_at": updated_at,
            "age_seconds": max(0.0, time.time() - updated_at) if updated_at else None,
            "refresh": owner,
        }

    def publish(self, payload: dict[str, Any]) -> dict[str, Any]:
        now = time.time()
        conn = self._conn()
        try:
            self._ensure(conn)
            set_runtime_kv_on_conn(
                conn,
                SNAPSHOT_KEY,
                payload,
                updated_at=now,
                ensure=False,
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
        from backend.core.static_feature_flags import (
            shared_static_feature_flags,
            static_feature_flags_fingerprint,
        )

        process_flags = shared_static_feature_flags().to_dict()
        payload.setdefault("snapshot", {})
        payload["snapshot"].update({
            "schema_version": "backend_readiness_snapshot.v1",
            "build_seconds": round(time.perf_counter() - started, 3),
            "generated_in_background": True,
            "process_static_feature_flags": {
                "schema_version": "static_feature_flags.v1",
                "values": process_flags,
                "fingerprint": static_feature_flags_fingerprint(process_flags),
                "pid": os.getpid(),
                "process_started_at": _PROCESS_STARTED_AT,
            },
        })
        self.publish(payload)
        return payload

    def refresh_async(self, *, max_age_seconds: float = 180.0) -> dict[str, Any]:
        current = self.latest()
        age = current.get("age_seconds")
        if current.get("ok") and age is not None and float(age) <= max_age_seconds:
            owner = self._refresh_owner.status()
            return {
                "ok": True,
                "status": "fresh",
                "age_seconds": age,
                "generation": owner["generation"],
                "thread_alive": owner["thread_alive"],
            }
        return self._refresh_owner.start(self.refresh, age_seconds=age)

    def open_async_refresh(self) -> None:
        """Open scheduling for the current backend lifecycle generation."""

        self._refresh_owner.open()

    def shutdown_async_refresh(self, *, timeout_sec: float = 30.0) -> dict[str, Any]:
        """Join this process-owned refresh worker before interpreter teardown."""

        return self._refresh_owner.shutdown(timeout_sec=timeout_sec)

    def async_refresh_status(self) -> dict[str, Any]:
        return self._refresh_owner.status()
