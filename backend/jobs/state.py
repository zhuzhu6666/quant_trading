"""Job state dataclass — lives in memory, not persisted (v1)."""
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal


@dataclass
class JobState:
    id: str
    kind: str
    status: Literal["queued", "running", "done", "error", "cancelled"] = "queued"
    progress_pct: float = 0.0
    current_step: str = ""
    started_at: datetime = field(default_factory=datetime.utcnow)
    finished_at: datetime | None = None
    params: dict[str, Any] = field(default_factory=dict)
    result: dict[str, Any] | None = None
    error: str | None = None
    log_tail: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "status": self.status,
            "progress_pct": self.progress_pct,
            "current_step": self.current_step,
            "started_at": self.started_at.isoformat() + "Z",
            "finished_at": self.finished_at.isoformat() + "Z" if self.finished_at else None,
            "params": self.params,
            "result": self.result,
            "error": self.error,
        }


def new_job_id() -> str:
    return uuid.uuid4().hex[:12]
