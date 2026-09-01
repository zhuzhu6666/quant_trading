"""PostgreSQL-backed manager for durable research jobs."""
from __future__ import annotations

import threading
from typing import Any, Mapping

from backend.jobs.state import JobState


class JobManager:
    """Submit and query the canonical PostgreSQL research-job queue."""

    PERSISTENT_JOB_KINDS = frozenset(
        {
            "backtest",
            "discover",
            "tuning",
            "ab_test",
            "external_refresh",
            "sync",
            "factor_health",
            "parameter_template_validation",
        }
    )

    def __init__(self, *, persistent_queue: Any = None) -> None:
        self._persistent_queue = persistent_queue
        self._queue_lock = threading.Lock()

    def _queue(self):
        if self._persistent_queue is None:
            with self._queue_lock:
                if self._persistent_queue is None:
                    from backend.jobs.pg_queue import PgJobQueue

                    self._persistent_queue = PgJobQueue()
        return self._persistent_queue

    def submit(self, kind: str, params: Mapping[str, Any]) -> JobState:
        """Enqueue one supported research job in PostgreSQL."""
        normalized_kind = str(kind or "").strip()
        if normalized_kind not in self.PERSISTENT_JOB_KINDS:
            raise ValueError(f"unsupported_persistent_job_kind:{normalized_kind}")
        payload = dict(params or {})
        return self._queue().enqueue(
            normalized_kind,
            payload,
            idempotency_key=str(payload.get("_idempotency_key") or ""),
            priority=int(payload.get("_priority") or 0),
            max_attempts=max(1, int(payload.get("_max_attempts") or 3)),
        )

    def get(self, job_id: str) -> JobState | None:
        return self._queue().get(job_id)

    def list(
        self,
        kind: str | None = None,
        status: str | None = None,
    ) -> list[JobState]:
        return self._queue().list(kind=kind, status=status)

    def cancel(self, job_id: str) -> bool:
        return bool(self._queue().request_cancel(job_id))


_manager: JobManager | None = None
_manager_lock = threading.Lock()


def get_job_manager() -> JobManager:
    global _manager
    if _manager is None:
        with _manager_lock:
            if _manager is None:
                _manager = JobManager()
    return _manager
