"""Durable process capability projection for the persistent job worker."""
from __future__ import annotations

import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Sequence

from backend.core.db import STATE_DB, connect_sqlite, get_state_pg_conn, is_state_db_path
from backend.core.static_feature_flags import (
    shared_static_feature_flags,
    static_feature_flags_fingerprint,
)


STATUS_KEY = "persistent_job_worker.capability.v1"


class PersistentJobWorkerCapability:
    """Publish worker ownership without claiming or governing any job."""

    def __init__(
        self,
        *,
        worker_id: str,
        handler_kinds: Sequence[str],
        db_path: str | Path = STATE_DB,
        now: Callable[[], float] = time.time,
        heartbeat_interval_sec: float = 10.0,
    ) -> None:
        self.worker_id = str(worker_id or "").strip()
        if not self.worker_id:
            raise ValueError("worker_id_required")
        self.handler_kinds = tuple(sorted({str(kind) for kind in handler_kinds if kind}))
        if not self.handler_kinds:
            raise ValueError("handler_kinds_required")
        self.db_path = Path(db_path)
        self._now = now
        self._interval = max(1.0, float(heartbeat_interval_sec))
        self._lock = threading.Lock()
        self._last_publish_at = 0.0
        self._last_status = ""
        self._boot_id = str(uuid.uuid4())
        self._started_at = float(now())
        flags = shared_static_feature_flags().to_dict()
        self._process_flags = {
            "schema_version": "static_feature_flags.v1",
            "values": flags,
            "fingerprint": static_feature_flags_fingerprint(flags),
            "pid": int(os.getpid()),
            "process_started_at": self._started_at,
        }

    def publish(
        self,
        status: str,
        job_id: str = "",
        kind: str = "",
        *,
        force: bool = False,
    ) -> dict[str, Any]:
        now = float(self._now())
        normalized = str(status or "unknown")
        with self._lock:
            if (
                not force
                and normalized == self._last_status
                and now - self._last_publish_at < self._interval
            ):
                return {"ok": True, "status": "rate_limited"}
            payload = {
                "schema_version": "persistent_job_worker_capability.v1",
                "worker_id": self.worker_id,
                "boot_id": self._boot_id,
                "pid": int(os.getpid()),
                "started_at": self._started_at,
                "updated_at": now,
                "status": normalized,
                "current_job_id": str(job_id or ""),
                "current_kind": str(kind or ""),
                "handler_kinds": list(self.handler_kinds),
                "process_static_feature_flags": dict(self._process_flags),
            }
            conn = (
                get_state_pg_conn()
                if is_state_db_path(self.db_path)
                else connect_sqlite(self.db_path)
            )
            try:
                from backend.services.runtime_kv_store import set_on_conn

                set_on_conn(conn, STATUS_KEY, payload, updated_at=now, ensure=False)
                conn.commit()
            except Exception:
                try:
                    conn.rollback()
                except Exception:
                    pass
                raise
            finally:
                conn.close()
            self._last_publish_at = now
            self._last_status = normalized
            return payload
