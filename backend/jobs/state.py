"""Public job state shared by local compatibility and PostgreSQL queue paths."""
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal


@dataclass
class JobState:
    id: str
    kind: str
    status: Literal[
        "pending", "queued", "retry_wait", "running", "done", "error", "cancelled"
    ] = "queued"
    progress_pct: float = 0.0
    current_step: str = ""
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime | None = None
    params: dict[str, Any] = field(default_factory=dict)
    result: dict[str, Any] | None = None
    error: str | None = None
    log_tail: list[str] = field(default_factory=list)
    priority: int = 0
    max_attempts: int = 1
    attempt_count: int = 0
    available_at: float = 0.0
    claimed_by: str = ""
    heartbeat_at: float = 0.0
    lease_expires_at: float = 0.0
    cancel_requested: bool = False
    idempotency_key: str = ""
    handler_version: str = "v1"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "status": self.status,
            "progress_pct": self.progress_pct,
            "current_step": self.current_step,
            "started_at": self.started_at.isoformat().replace("+00:00", "") + "Z",
            "finished_at": self.finished_at.isoformat().replace("+00:00", "") + "Z" if self.finished_at else None,
            "params": self.params,
            "result": self.result,
            "error": self.error,
            "log_tail": list(self.log_tail),
            "priority": self.priority,
            "max_attempts": self.max_attempts,
            "attempt_count": self.attempt_count,
            "available_at": self.available_at,
            "claimed_by": self.claimed_by,
            "heartbeat_at": self.heartbeat_at,
            "lease_expires_at": self.lease_expires_at,
            "cancel_requested": self.cancel_requested,
            "idempotency_key": self.idempotency_key,
            "handler_version": self.handler_version,
        }


def new_job_id() -> str:
    return uuid.uuid4().hex[:12]
